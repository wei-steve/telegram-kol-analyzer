"""Fail-closed bridge from the legacy worker to leased cancellation authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Iterable
import uuid

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from telegram_kol_research.models import (
    EntryRevisionReplacement,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageProcessingJob,
    PositionMutationIntent,
    PositionProtectionLeg,
    RawMessage,
    RepairConfirmationToken,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    _authority_document,
)
from telegram_kol_research.trading_settings import (
    _persist_trading_settings_in_session,
    _settings_row_and_payload_in_session,
    trading_settings_from_payload,
)


LEGACY_RUNTIME_DRAIN_BRIDGE_KEY = "legacy_runtime_drain_bridge"
LEGACY_RUNTIME_WORKER_SERVICE = "telegram-kol-worker.service"
_SCHEMA_VERSION = 3
_STATES = frozenset(
    {
        "frozen",
        "fenced",
        "handed_off",
        "cancelling",
        "unknown_locked",
        "drained",
        "released_for_deploy",
    }
)
_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "active_order_id",
        "bridge_token",
        "production_sha",
        "service_name",
        "worker_pid",
        "worker_start_ticks",
        "authority_production_sha",
        "authority_service_name",
        "authority_worker_pid",
        "authority_worker_start_ticks",
        "handoff_at",
        "frozen_at",
        "freeze_raw_message_id",
        "original_auto_trade_enabled",
        "original_legacy_entry_submission_frozen",
        "original_entry_revision_v2_mode",
        "reason_code",
        "reviewed_order_ids",
        "fenced_batch_ids",
        "completed_order_ids",
        "drain_evidence_fingerprint",
        "drained_at",
        "write_boundary_reached",
        "updated_at",
    }
)
_TERMINAL_REVISION_BATCH_STATES = frozenset(
    {"succeeded", "blocked", "failed", "recovery_required"}
)
_TERMINAL_ENTRY_LEG_STATES = frozenset(
    {"cancelled", "canceled", "expired", "rejected"}
)
_SHA = re.compile(r"[0-9a-f]{40}")
_ORDER_ID = re.compile(r"[0-9]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REASON_CODE = re.compile(r"[a-z0-9_]{1,64}")
_PREWRITE_REFUSAL_REASONS = frozenset(
    {
        "exact_pending_cancel_write_gate_blocked",
        "legacy_bridge_worker_identity_unavailable",
        "legacy_bridge_worker_identity_drift",
        "legacy_bridge_write_gate_blocked",
    }
)


@dataclass(frozen=True, slots=True)
class LegacyRuntimeIdentity:
    production_sha: str
    worker_pid: int
    worker_start_ticks: int
    service_name: str = LEGACY_RUNTIME_WORKER_SERVICE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "production_sha",
            _sha(self.production_sha, field_name="production_sha"),
        )
        if isinstance(self.worker_pid, bool) or int(self.worker_pid) <= 0:
            raise ValueError("worker_pid must be positive")
        if (
            isinstance(self.worker_start_ticks, bool)
            or int(self.worker_start_ticks) <= 0
        ):
            raise ValueError("worker_start_ticks must be positive")
        object.__setattr__(self, "worker_pid", int(self.worker_pid))
        object.__setattr__(
            self, "worker_start_ticks", int(self.worker_start_ticks)
        )
        if self.service_name != LEGACY_RUNTIME_WORKER_SERVICE:
            raise ValueError("service_name must identify the worker unit")


@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainBridgePlan:
    mode: str
    state: str
    planned_at: datetime
    fingerprint: str
    conflicts: tuple[dict[str, str], ...]
    fenced_batch_ids: tuple[int, ...]
    completed_order_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainBridgeResult:
    status: str
    reason_code: str | None = None
    bridge_token: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainEvidence:
    reviewed_order_ids: tuple[str, ...]
    completed_order_ids: tuple[str, ...]
    plan_fingerprint: str
    action_count: int
    conflict_count: int
    positions_count: int
    regular_order_count: int
    pending_trigger_count: int
    unidentified_pending_count: int
    unreviewed_pending_count: int
    fill_conflict_count: int
    queries_complete: bool
    history_complete: bool
    observed_at: datetime
    fingerprint: str


def build_legacy_runtime_drain_bridge_plan(
    session_factory,
    *,
    runtime_identity: LegacyRuntimeIdentity,
    expected_production_sha: str,
    reviewed_order_ids: Iterable[str],
    planned_at: datetime,
) -> LegacyRuntimeDrainBridgePlan:
    """Build one deterministic read-only projection of the legacy bridge."""

    expected_sha = _sha(
        expected_production_sha,
        field_name="expected_production_sha",
    )
    reviewed = _reviewed_order_ids(reviewed_order_ids)
    observed_at = _aware_utc(planned_at, field_name="planned_at")
    with session_factory() as session:
        return _build_plan_in_session(
            session,
            runtime_identity=runtime_identity,
            expected_sha=expected_sha,
            reviewed=reviewed,
            observed_at=observed_at,
        )


def _build_plan_in_session(
    session,
    *,
    runtime_identity: LegacyRuntimeIdentity,
    expected_sha: str,
    reviewed: tuple[str, ...],
    observed_at: datetime,
) -> LegacyRuntimeDrainBridgePlan:
    conflicts: list[dict[str, str]] = []
    state = "absent"
    fenced_batch_ids: tuple[int, ...] = ()
    completed_order_ids: tuple[str, ...] = ()
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
        .one_or_none()
    )
    document = None if row is None else _bridge_document(row.value_json)
    if runtime_identity.production_sha != expected_sha:
        conflicts.append({"reason": "legacy_bridge_production_sha_drift"})
    if row is not None and document is None:
        state = "invalid"
        conflicts.append({"reason": "legacy_bridge_state_invalid"})
    elif document is not None:
        state = str(document["state"])
        fenced_batch_ids = tuple(document["fenced_batch_ids"])
        completed_order_ids = tuple(document["completed_order_ids"])
        if document["authority_production_sha"] != expected_sha:
            conflicts.append({"reason": "legacy_bridge_production_sha_drift"})
        if (
            document["authority_service_name"]
            != runtime_identity.service_name
            or document["authority_worker_pid"] != runtime_identity.worker_pid
            or document["authority_worker_start_ticks"]
            != runtime_identity.worker_start_ticks
        ):
            conflicts.append({"reason": "legacy_bridge_worker_identity_drift"})
        if tuple(document["reviewed_order_ids"]) != reviewed:
            conflicts.append({"reason": "legacy_bridge_reviewed_set_drift"})

    unknown_reason = _bridge_unknown_mutation_reason(
        session,
        reviewed_order_ids=reviewed,
    )
    if unknown_reason is not None:
        conflicts.append({"reason": unknown_reason})

    active_batches = tuple(
        (
            int(batch_id),
            str(status),
            claim_token is not None,
            claimed_at is not None,
        )
        for batch_id, status, claim_token, claimed_at in session.query(
            StrategyRevisionBatch.id,
            StrategyRevisionBatch.status,
            StrategyRevisionBatch.advance_claim_token,
            StrategyRevisionBatch.advance_claimed_at,
        )
        .filter(
            StrategyRevisionBatch.status.not_in(_TERMINAL_REVISION_BATCH_STATES)
        )
        .order_by(StrategyRevisionBatch.id.asc())
        .all()
    )
    _settings_row, settings_payload = _settings_row_and_payload_in_session(session)
    try:
        settings = trading_settings_from_payload(settings_payload)
        settings_snapshot: dict[str, Any] = {
            "auto_trade_enabled": settings.auto_trade_enabled,
            "entry_revision_v2_mode": settings.entry_revision_v2_mode,
            "message_pipeline_mode": settings.message_pipeline_mode,
        }
    except ValueError:
        settings_snapshot = {"invalid": True}
        conflicts.append({"reason": "legacy_bridge_settings_invalid"})
    payload = {
        "active_batches": [list(value) for value in active_batches],
        "completed_order_ids": list(completed_order_ids),
        "expected_production_sha": expected_sha,
        "fenced_batch_ids": list(fenced_batch_ids),
        "reviewed_order_ids": list(reviewed),
        "runtime_identity": {
            "production_sha": runtime_identity.production_sha,
            "service_name": runtime_identity.service_name,
            "worker_pid": runtime_identity.worker_pid,
            "worker_start_ticks": runtime_identity.worker_start_ticks,
        },
        "settings": settings_snapshot,
        "state": state,
    }
    return LegacyRuntimeDrainBridgePlan(
        mode="dry_run",
        state=state,
        planned_at=observed_at,
        fingerprint=hashlib.sha256(_canonical_json(payload).encode()).hexdigest(),
        conflicts=tuple(conflicts),
        fenced_batch_ids=fenced_batch_ids,
        completed_order_ids=completed_order_ids,
    )


def freeze_legacy_runtime_drain_bridge(
    session_factory,
    *,
    plan: LegacyRuntimeDrainBridgePlan,
    runtime_identity: LegacyRuntimeIdentity,
    reviewed_order_ids: Iterable[str],
    expected_fingerprint: str,
    confirmation_token: str,
    frozen_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Atomically freeze new entry/revision writes and publish bridge state."""

    reviewed = _reviewed_order_ids(reviewed_order_ids)
    observed_at = _aware_utc(frozen_at, field_name="frozen_at")
    if (
        plan.mode != "dry_run"
        or plan.state != "absent"
        or plan.conflicts
        or not _fingerprint_matches(plan.fingerprint, expected_fingerprint)
    ):
        return _result("blocked", "legacy_bridge_plan_mismatch")
    clean_confirmation = _confirmation_token(confirmation_token)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current_plan = _build_plan_in_session(
                session,
                runtime_identity=runtime_identity,
                expected_sha=runtime_identity.production_sha,
                reviewed=reviewed,
                observed_at=plan.planned_at,
            )
            if (
                current_plan.state != "absent"
                or current_plan.conflicts
                or current_plan.fingerprint != plan.fingerprint
            ):
                session.rollback()
                return _result("blocked", "legacy_bridge_plan_mismatch")
            existing = (
                session.query(TradingSetting)
                .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
                .one_or_none()
            )
            if existing is not None:
                session.rollback()
                return _result("blocked", "legacy_bridge_already_present")
            authority_row = (
                session.query(TradingSetting)
                .filter(
                    TradingSetting.key
                    == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
                )
                .one_or_none()
            )
            if authority_row is not None:
                authority = _authority_document(authority_row.value_json)
                if authority is None:
                    session.rollback()
                    return _result(
                        "blocked", "legacy_bridge_entry_authority_unknown"
                    )
                if authority["state"] == "held":
                    session.rollback()
                    return _result(
                        "blocked", "legacy_bridge_entry_authority_busy"
                    )
            settings_row, persisted_payload = _settings_row_and_payload_in_session(
                session
            )
            try:
                settings = trading_settings_from_payload(persisted_payload)
            except ValueError:
                session.rollback()
                return _result("blocked", "legacy_bridge_settings_invalid")
            if settings.message_pipeline_mode != "queue":
                session.rollback()
                return _result(
                    "blocked", "legacy_bridge_message_pipeline_not_queue"
                )
            bridge_token = uuid.uuid4().hex
            watermark = int(
                session.query(func.max(RawMessage.id)).scalar() or 0
            )
            frozen_settings = trading_settings_from_payload(
                {
                    **settings.to_dict(),
                    "auto_trade_enabled": False,
                    "legacy_entry_submission_frozen": True,
                    "entry_revision_v2_mode": "disabled",
                }
            )
            _persist_trading_settings_in_session(
                session,
                frozen_settings,
                updated_at=observed_at,
                row=settings_row,
                persisted_payload=persisted_payload,
            )
            document = {
                "active_order_id": None,
                "bridge_token": bridge_token,
                "completed_order_ids": [],
                "drain_evidence_fingerprint": None,
                "drained_at": None,
                "fenced_batch_ids": [],
                "freeze_raw_message_id": watermark,
                "frozen_at": observed_at.isoformat(),
                "original_auto_trade_enabled": settings.auto_trade_enabled,
                "original_legacy_entry_submission_frozen": (
                    settings.legacy_entry_submission_frozen
                ),
                "original_entry_revision_v2_mode": settings.entry_revision_v2_mode,
                "production_sha": runtime_identity.production_sha,
                "service_name": runtime_identity.service_name,
                "authority_production_sha": runtime_identity.production_sha,
                "authority_service_name": runtime_identity.service_name,
                "reason_code": None,
                "reviewed_order_ids": list(reviewed),
                "schema_version": _SCHEMA_VERSION,
                "state": "frozen",
                "updated_at": observed_at.isoformat(),
                "worker_pid": runtime_identity.worker_pid,
                "worker_start_ticks": runtime_identity.worker_start_ticks,
                "authority_worker_pid": runtime_identity.worker_pid,
                "authority_worker_start_ticks": (
                    runtime_identity.worker_start_ticks
                ),
                "handoff_at": None,
                "write_boundary_reached": False,
            }
            session.add(
                TradingSetting(
                    key=LEGACY_RUNTIME_DRAIN_BRIDGE_KEY,
                    value_json=_canonical_json(document),
                    updated_at=observed_at,
                )
            )
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="freeze_legacy_runtime_drain_bridge",
                action_id=plan.fingerprint,
                pos_id=f"legacy-bridge:{bridge_token}",
                consumed_at=observed_at,
            )
            session.commit()
            return LegacyRuntimeDrainBridgeResult(
                status="frozen",
                bridge_token=bridge_token,
            )
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def fence_legacy_runtime_revisions(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    confirmation_token: str,
    fenced_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Install exact null-time claims that the unchanged worker cannot steal."""

    clean_token = _bridge_token(bridge_token)
    clean_confirmation = _confirmation_token(confirmation_token)
    observed_at = _aware_utc(fenced_at, field_name="fenced_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"frozen"},
            )
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            settings_reason = _frozen_settings_reason(session)
            if settings_reason is not None:
                session.rollback()
                return _result("blocked", settings_reason)
            unknown_reason = _bridge_unknown_mutation_reason(
                session,
                reviewed_order_ids=tuple(document["reviewed_order_ids"]),
            )
            if unknown_reason is not None:
                session.rollback()
                return _result("blocked", unknown_reason)
            prefreeze_job = (
                session.query(MessageProcessingJob.id)
                .filter(
                    MessageProcessingJob.shadow.is_(False),
                    MessageProcessingJob.status == "claimed",
                    MessageProcessingJob.raw_message_id
                    <= int(document["freeze_raw_message_id"]),
                )
                .first()
            )
            if prefreeze_job is not None:
                session.rollback()
                return _result(
                    "blocked", "legacy_bridge_prefreeze_jobs_active"
                )
            active_batches = (
                _active_revision_batches_query(session)
                .order_by(StrategyRevisionBatch.id.asc())
                .all()
            )
            if any(
                batch.advance_claim_token is not None
                or batch.advance_claimed_at is not None
                for batch in active_batches
            ):
                session.rollback()
                return _result(
                    "blocked", "legacy_bridge_foreign_revision_claim"
                )
            batch_ids = [int(batch.id) for batch in active_batches]
            for batch in active_batches:
                batch.advance_claim_token = clean_token
                batch.advance_claimed_at = None
                batch.updated_at = observed_at
            document["state"] = "fenced"
            document["fenced_batch_ids"] = batch_ids
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="fence_legacy_runtime_revisions",
                action_id=clean_token,
                pos_id=f"legacy-bridge:{clean_token}",
                consumed_at=observed_at,
            )
            session.commit()
            return LegacyRuntimeDrainBridgeResult(
                status="fenced",
                bridge_token=clean_token,
            )
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def rollback_legacy_runtime_drain_bridge(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    confirmation_token: str,
    rolled_back_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Restore pre-freeze settings only before any reviewed write boundary."""

    clean_token = _bridge_token(bridge_token)
    clean_confirmation = _confirmation_token(confirmation_token)
    observed_at = _aware_utc(rolled_back_at, field_name="rolled_back_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"frozen", "fenced", "handed_off"},
            )
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            if document["write_boundary_reached"] or document["completed_order_ids"]:
                session.rollback()
                return _result("blocked", "legacy_bridge_rollback_forbidden")
            error = _frozen_settings_reason(session)
            if error is None and document["state"] in {"fenced", "handed_off"}:
                error = _revision_sentinel_reason(session, document=document)
            if error is None:
                error = _bridge_unknown_mutation_reason(
                    session,
                    reviewed_order_ids=tuple(document["reviewed_order_ids"]),
                )
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            batches = (
                session.query(StrategyRevisionBatch)
                .filter(
                    StrategyRevisionBatch.id.in_(document["fenced_batch_ids"])
                )
                .all()
                if document["fenced_batch_ids"]
                else []
            )
            if len(batches) != len(document["fenced_batch_ids"]) or any(
                batch.advance_claim_token != clean_token
                or batch.advance_claimed_at is not None
                for batch in batches
            ):
                session.rollback()
                return _result("blocked", "legacy_bridge_sentinel_drift")
            for batch in batches:
                batch.advance_claim_token = None
                batch.advance_claimed_at = None
                batch.updated_at = observed_at
            settings_row, persisted_payload = _settings_row_and_payload_in_session(
                session
            )
            try:
                settings = trading_settings_from_payload(persisted_payload)
                restored = trading_settings_from_payload(
                    {
                        **settings.to_dict(),
                        "auto_trade_enabled": document[
                            "original_auto_trade_enabled"
                        ],
                        "legacy_entry_submission_frozen": document[
                            "original_legacy_entry_submission_frozen"
                        ],
                        "entry_revision_v2_mode": document[
                            "original_entry_revision_v2_mode"
                        ],
                    }
                )
            except ValueError:
                session.rollback()
                return _result("blocked", "legacy_bridge_settings_invalid")
            _persist_trading_settings_in_session(
                session,
                restored,
                updated_at=observed_at,
                row=settings_row,
                persisted_payload=persisted_payload,
            )
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="rollback_legacy_runtime_drain_bridge",
                action_id=clean_token,
                pos_id=f"legacy-bridge:{clean_token}",
                consumed_at=observed_at,
            )
            session.delete(row)
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="rolled_back")
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def handoff_legacy_runtime_drain_bridge(
    session_factory,
    *,
    bridge_token: str,
    candidate_runtime_identity: LegacyRuntimeIdentity,
    expected_candidate_sha: str,
    confirmation_token: str,
    handed_off_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Transfer a zero-write fenced bridge to one exact candidate worker."""

    clean_token = _bridge_token(bridge_token)
    expected_sha = _sha(
        expected_candidate_sha,
        field_name="expected_candidate_sha",
    )
    clean_confirmation = _confirmation_token(confirmation_token)
    observed_at = _aware_utc(handed_off_at, field_name="handed_off_at")
    if candidate_runtime_identity.production_sha != expected_sha:
        return _result("blocked", "legacy_bridge_candidate_sha_drift")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = (
                session.query(TradingSetting)
                .filter(
                    TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY
                )
                .one_or_none()
            )
            document = None if row is None else _bridge_document(row.value_json)
            if row is None or document is None:
                session.rollback()
                return _result("blocked", "legacy_bridge_state_invalid")
            if document["state"] != "fenced":
                session.rollback()
                return _result("blocked", "legacy_bridge_state_mismatch")
            if document["bridge_token"] != clean_token:
                session.rollback()
                return _result("blocked", "legacy_bridge_owner_mismatch")
            if expected_sha == document["production_sha"]:
                session.rollback()
                return _result(
                    "blocked", "legacy_bridge_candidate_not_distinct"
                )
            if (
                document["authority_production_sha"]
                != document["production_sha"]
                or document["authority_service_name"]
                != document["service_name"]
                or document["authority_worker_pid"] != document["worker_pid"]
                or document["authority_worker_start_ticks"]
                != document["worker_start_ticks"]
                or document["handoff_at"] is not None
            ):
                session.rollback()
                return _result("blocked", "legacy_bridge_state_invalid")
            if (
                document["write_boundary_reached"]
                or document["completed_order_ids"]
            ):
                session.rollback()
                return _result(
                    "blocked",
                    "legacy_bridge_handoff_write_boundary_reached",
                )
            error = _frozen_settings_reason(session)
            if error is None:
                error = _revision_sentinel_reason(
                    session,
                    document=document,
                )
            if error is None:
                error = _bridge_unknown_mutation_reason(
                    session,
                    reviewed_order_ids=tuple(document["reviewed_order_ids"]),
                )
            if error is None:
                error = _inner_authority_reason(session)
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            document["state"] = "handed_off"
            document["authority_production_sha"] = expected_sha
            document["authority_service_name"] = (
                candidate_runtime_identity.service_name
            )
            document["authority_worker_pid"] = (
                candidate_runtime_identity.worker_pid
            )
            document["authority_worker_start_ticks"] = (
                candidate_runtime_identity.worker_start_ticks
            )
            document["handoff_at"] = observed_at.isoformat()
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="handoff_legacy_runtime_drain_bridge",
                action_id=clean_token,
                pos_id=f"legacy-bridge:{clean_token}",
                consumed_at=observed_at,
            )
            session.commit()
            return LegacyRuntimeDrainBridgeResult(
                status="handed_off",
                bridge_token=clean_token,
            )
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def begin_legacy_runtime_bridge_cancellation(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    order_id: str,
    started_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Cross the write boundary for one exact reviewed order."""

    clean_token = _bridge_token(bridge_token)
    clean_order_id = _order_id(order_id)
    observed_at = _aware_utc(started_at, field_name="started_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"fenced", "handed_off"},
            )
            if error is None:
                error = _bridge_cancellation_invariant_reason(
                    session,
                    document=document,
                    order_id=clean_order_id,
                )
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            document["state"] = "cancelling"
            document["active_order_id"] = clean_order_id
            document["reason_code"] = None
            document["write_boundary_reached"] = True
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="cancelling")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def complete_legacy_runtime_bridge_cancellation(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    order_id: str,
    completed_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Record one fully terminalized cancellation and retain the outer fence."""

    clean_token = _bridge_token(bridge_token)
    clean_order_id = _order_id(order_id)
    observed_at = _aware_utc(completed_at, field_name="completed_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"cancelling"},
            )
            if error is None and document["active_order_id"] != clean_order_id:
                error = "legacy_bridge_active_order_mismatch"
            if error is None:
                error = _frozen_settings_reason(session)
            if error is None:
                error = _revision_sentinel_reason(session, document=document)
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            completed = set(document["completed_order_ids"])
            if clean_order_id in completed:
                session.rollback()
                return _result("blocked", "legacy_bridge_order_already_completed")
            completed.add(clean_order_id)
            document["state"] = (
                "handed_off" if document["handoff_at"] is not None else "fenced"
            )
            document["active_order_id"] = None
            document["completed_order_ids"] = sorted(completed)
            document["reason_code"] = None
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status=document["state"])
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def mark_legacy_runtime_bridge_unknown(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    order_id: str,
    reason_code: str,
    observed_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Permanently lock the bridge after a potentially submitted write."""

    clean_token = _bridge_token(bridge_token)
    clean_order_id = _order_id(order_id)
    clean_reason = _reason_code(reason_code)
    timestamp = _aware_utc(observed_at, field_name="observed_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"cancelling", "unknown_locked"},
            )
            if error is None and document["active_order_id"] != clean_order_id:
                error = "legacy_bridge_active_order_mismatch"
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            if document["state"] == "unknown_locked":
                session.rollback()
                return LegacyRuntimeDrainBridgeResult(status="unknown_locked")
            document["state"] = "unknown_locked"
            document["reason_code"] = clean_reason
            document["updated_at"] = timestamp.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = timestamp
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="unknown_locked")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def build_legacy_runtime_drain_evidence(
    *,
    reviewed_order_ids: Iterable[str],
    completed_order_ids: Iterable[str],
    plan_fingerprint: str,
    action_count: int,
    conflict_count: int,
    positions_count: int,
    regular_order_count: int,
    pending_trigger_count: int,
    unidentified_pending_count: int,
    unreviewed_pending_count: int,
    fill_conflict_count: int,
    queries_complete: bool,
    history_complete: bool,
    observed_at: datetime,
) -> LegacyRuntimeDrainEvidence:
    """Build bounded, raw-response-free evidence for the final drain gate."""

    reviewed = _reviewed_order_ids(reviewed_order_ids)
    completed = _completed_order_ids(
        list(completed_order_ids),
        reviewed=reviewed,
    )
    clean_plan_fingerprint = _sha256(
        plan_fingerprint,
        field_name="plan_fingerprint",
    )
    counts = {
        "action_count": _nonnegative_int(action_count, field_name="action_count"),
        "conflict_count": _nonnegative_int(
            conflict_count, field_name="conflict_count"
        ),
        "positions_count": _nonnegative_int(
            positions_count, field_name="positions_count"
        ),
        "regular_order_count": _nonnegative_int(
            regular_order_count, field_name="regular_order_count"
        ),
        "pending_trigger_count": _nonnegative_int(
            pending_trigger_count, field_name="pending_trigger_count"
        ),
        "unidentified_pending_count": _nonnegative_int(
            unidentified_pending_count,
            field_name="unidentified_pending_count",
        ),
        "unreviewed_pending_count": _nonnegative_int(
            unreviewed_pending_count,
            field_name="unreviewed_pending_count",
        ),
        "fill_conflict_count": _nonnegative_int(
            fill_conflict_count, field_name="fill_conflict_count"
        ),
    }
    if type(queries_complete) is not bool or type(history_complete) is not bool:
        raise ValueError("drain completeness flags must be booleans")
    timestamp = _aware_utc(observed_at, field_name="observed_at")
    payload = {
        **counts,
        "completed_order_ids": list(completed),
        "history_complete": history_complete,
        "plan_fingerprint": clean_plan_fingerprint,
        "queries_complete": queries_complete,
        "reviewed_order_ids": list(reviewed),
    }
    return LegacyRuntimeDrainEvidence(
        reviewed_order_ids=reviewed,
        completed_order_ids=completed,
        plan_fingerprint=clean_plan_fingerprint,
        queries_complete=queries_complete,
        history_complete=history_complete,
        observed_at=timestamp,
        fingerprint=hashlib.sha256(_canonical_json(payload).encode()).hexdigest(),
        **counts,
    )


def mark_legacy_runtime_bridge_drained(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    evidence: LegacyRuntimeDrainEvidence,
    confirmation_token: str,
    drained_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Mark all seven reviewed orders drained after complete fresh evidence."""

    clean_token = _bridge_token(bridge_token)
    clean_confirmation = _confirmation_token(confirmation_token)
    observed_at = _aware_utc(drained_at, field_name="drained_at")
    evidence_reason = _drain_evidence_reason(evidence)
    if evidence_reason is None:
        evidence_reason = _drain_evidence_freshness_reason(
            evidence,
            transition_at=observed_at,
        )
    if evidence_reason is not None:
        return _result("blocked", evidence_reason)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"fenced", "handed_off"},
            )
            if error is None and tuple(document["reviewed_order_ids"]) != tuple(
                evidence.reviewed_order_ids
            ):
                error = "legacy_bridge_reviewed_set_drift"
            if error is None and tuple(document["completed_order_ids"]) != tuple(
                document["reviewed_order_ids"]
            ):
                error = "legacy_bridge_reviewed_set_incomplete"
            if error is None and tuple(evidence.completed_order_ids) != tuple(
                document["reviewed_order_ids"]
            ):
                error = "legacy_bridge_evidence_set_incomplete"
            if error is None:
                error = _frozen_settings_reason(session)
            if error is None:
                error = _revision_sentinel_reason(session, document=document)
            if error is None:
                error = _bridge_unknown_mutation_reason(
                    session,
                    reviewed_order_ids=tuple(document["reviewed_order_ids"]),
                )
            if error is None:
                error = _local_drain_reason(session, document=document)
            if error is None:
                error = _inner_authority_reason(session)
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            document["state"] = "drained"
            document["drained_at"] = observed_at.isoformat()
            document["drain_evidence_fingerprint"] = evidence.fingerprint
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="mark_legacy_runtime_bridge_drained",
                action_id=evidence.fingerprint,
                pos_id=f"legacy-bridge:{clean_token}",
                consumed_at=observed_at,
            )
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="drained")
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def release_legacy_runtime_bridge_for_deploy(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    evidence: LegacyRuntimeDrainEvidence,
    expected_drain_evidence_fingerprint: str,
    confirmation_token: str,
    released_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Release exact legacy revision sentinels while settings stay frozen."""

    clean_token = _bridge_token(bridge_token)
    expected_evidence = _sha256(
        expected_drain_evidence_fingerprint,
        field_name="expected_drain_evidence_fingerprint",
    )
    clean_confirmation = _confirmation_token(confirmation_token)
    observed_at = _aware_utc(released_at, field_name="released_at")
    evidence_reason = _drain_evidence_reason(evidence)
    if evidence_reason is None:
        evidence_reason = _drain_evidence_freshness_reason(
            evidence,
            transition_at=observed_at,
        )
    if evidence_reason is not None:
        return _result("blocked", evidence_reason)
    if evidence.fingerprint != expected_evidence:
        return _result("blocked", "legacy_bridge_drain_evidence_drift")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"drained"},
            )
            if (
                error is None
                and document["drain_evidence_fingerprint"] != expected_evidence
            ):
                error = "legacy_bridge_drain_evidence_drift"
            if error is None and tuple(evidence.reviewed_order_ids) != tuple(
                document["reviewed_order_ids"]
            ):
                error = "legacy_bridge_reviewed_set_drift"
            if error is None and tuple(evidence.completed_order_ids) != tuple(
                document["reviewed_order_ids"]
            ):
                error = "legacy_bridge_evidence_set_incomplete"
            if error is None:
                error = _frozen_settings_reason(session)
            if error is None:
                error = _revision_sentinel_reason(session, document=document)
            if error is None:
                error = _bridge_unknown_mutation_reason(
                    session,
                    reviewed_order_ids=tuple(document["reviewed_order_ids"]),
                )
            if error is None:
                error = _local_drain_reason(session, document=document)
            if error is None:
                error = _inner_authority_reason(session)
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            batches = (
                session.query(StrategyRevisionBatch)
                .filter(
                    StrategyRevisionBatch.id.in_(document["fenced_batch_ids"])
                )
                .all()
                if document["fenced_batch_ids"]
                else []
            )
            for batch in batches:
                batch.advance_claim_token = None
                batch.advance_claimed_at = None
                batch.updated_at = observed_at
            document["state"] = "released_for_deploy"
            document["updated_at"] = observed_at.isoformat()
            row.value_json = _canonical_json(document)
            row.updated_at = observed_at
            _consume_confirmation_in_session(
                session,
                confirmation_token=clean_confirmation,
                action_kind="release_legacy_runtime_bridge_for_deploy",
                action_id=expected_evidence,
                pos_id=f"legacy-bridge:{clean_token}",
                consumed_at=observed_at,
            )
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="released_for_deploy")
    except IntegrityError:
        return _result("blocked", "legacy_bridge_confirmation_used")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def read_local_legacy_worker_identity(
    *,
    checkout_path: Path,
    expected_production_sha: str,
    service_name: str,
    proc_root: Path = Path("/proc"),
    command_runner=subprocess.run,
) -> LegacyRuntimeIdentity:
    """Read exact checkout HEAD and stable Linux PID/start-tick identity."""

    expected_sha = _sha(
        expected_production_sha,
        field_name="expected_production_sha",
    )
    checkout = Path(checkout_path)
    if checkout.is_symlink() or not checkout.is_dir():
        raise ValueError("checkout path is invalid")
    checkout = checkout.resolve()
    clean_service = str(service_name or "").strip()
    if clean_service != LEGACY_RUNTIME_WORKER_SERVICE:
        raise ValueError("service_name must identify the worker unit")
    root_text = _run_bounded_command(
        command_runner,
        ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
    )
    if Path(root_text).resolve() != checkout:
        raise ValueError("checkout root mismatch")
    head = _run_bounded_command(
        command_runner,
        ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"],
    )
    if head != expected_sha:
        raise ValueError("production SHA mismatch")
    pid_text = _run_bounded_command(
        command_runner,
        [
            "systemctl",
            "show",
            clean_service,
            "--property",
            "MainPID",
            "--value",
        ],
    )
    if not pid_text.isdigit() or int(pid_text) <= 1:
        raise ValueError("service MainPID is invalid")
    pid = int(pid_text)
    stat_path = Path(proc_root) / str(pid) / "stat"
    first_ticks = _read_proc_start_ticks(stat_path)
    _read_proc_worker_evidence(
        Path(proc_root) / str(pid),
        checkout=checkout,
    )
    confirmed_pid_text = _run_bounded_command(
        command_runner,
        [
            "systemctl",
            "show",
            clean_service,
            "--property",
            "MainPID",
            "--value",
        ],
    )
    if confirmed_pid_text != pid_text:
        raise ValueError("service MainPID changed during identity read")
    second_ticks = _read_proc_start_ticks(stat_path)
    if first_ticks != second_ticks:
        raise ValueError("proc stat changed during identity read")
    return LegacyRuntimeIdentity(
        production_sha=head,
        worker_pid=pid,
        worker_start_ticks=first_ticks,
        service_name=clean_service,
    )


def legacy_runtime_bridge_revision_gate_state(
    session,
    *,
    runtime_identity: LegacyRuntimeIdentity | None,
    reviewed_order_ids: Iterable[str],
) -> str:
    """Classify the optional bridge for the cancellation authority gate."""

    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
        .one_or_none()
    )
    if row is None:
        return "absent"
    document = _bridge_document(row.value_json)
    if document is None or runtime_identity is None:
        return "blocked"
    try:
        reviewed = _reviewed_order_ids(reviewed_order_ids)
    except ValueError:
        return "blocked"
    if (
        document["state"] not in {"fenced", "handed_off", "drained"}
        or document["authority_production_sha"]
        != runtime_identity.production_sha
        or document["authority_service_name"]
        != runtime_identity.service_name
        or document["authority_worker_pid"] != runtime_identity.worker_pid
        or document["authority_worker_start_ticks"]
        != runtime_identity.worker_start_ticks
        or tuple(document["reviewed_order_ids"]) != reviewed
        or _frozen_settings_reason(session) is not None
        or _revision_sentinel_reason(session, document=document) is not None
    ):
        return "blocked"
    return "fenced"


def validate_legacy_runtime_bridge_cancellation_ready(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    order_id: str,
) -> LegacyRuntimeDrainBridgeResult:
    """Read-only precheck used before any exchange-backed replanning."""

    clean_token = _bridge_token(bridge_token)
    clean_order_id = _order_id(order_id)
    try:
        with session_factory() as session:
            _row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"fenced", "handed_off"},
            )
            if error is None:
                error = _bridge_cancellation_invariant_reason(
                    session,
                    document=document,
                    order_id=clean_order_id,
                )
            if error is not None:
                return _result("blocked", error)
            return LegacyRuntimeDrainBridgeResult(status="ready")
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def _bridge_document(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != _DOCUMENT_KEYS:
        return None
    if value.get("schema_version") != _SCHEMA_VERSION:
        return None
    if value.get("state") not in _STATES:
        return None
    if not isinstance(value.get("bridge_token"), str) or not _TOKEN.fullmatch(
        value["bridge_token"]
    ):
        return None
    try:
        production_sha = _sha(
            value.get("production_sha"), field_name="production_sha"
        )
        authority_production_sha = _sha(
            value.get("authority_production_sha"),
            field_name="authority_production_sha",
        )
        frozen_at = _aware_utc_text(value.get("frozen_at"))
        updated_at = _aware_utc_text(value.get("updated_at"))
        reviewed = _reviewed_order_ids(value.get("reviewed_order_ids", ()))
        fenced = _positive_unique_ints(value.get("fenced_batch_ids"))
        completed = _completed_order_ids(
            value.get("completed_order_ids"), reviewed=reviewed
        )
    except ValueError:
        return None
    worker_pid = value.get("worker_pid")
    worker_start_ticks = value.get("worker_start_ticks")
    authority_worker_pid = value.get("authority_worker_pid")
    authority_worker_start_ticks = value.get(
        "authority_worker_start_ticks"
    )
    watermark = value.get("freeze_raw_message_id")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < minimum
        for item, minimum in (
            (worker_pid, 1),
            (worker_start_ticks, 1),
            (authority_worker_pid, 1),
            (authority_worker_start_ticks, 1),
            (watermark, 0),
        )
    ):
        return None
    if type(value.get("original_auto_trade_enabled")) is not bool:
        return None
    if value.get("service_name") != LEGACY_RUNTIME_WORKER_SERVICE:
        return None
    if value.get("authority_service_name") != LEGACY_RUNTIME_WORKER_SERVICE:
        return None
    if type(value.get("original_legacy_entry_submission_frozen")) is not bool:
        return None
    if value.get("original_entry_revision_v2_mode") not in {
        "disabled",
        "shadow",
        "live",
    }:
        return None
    if type(value.get("write_boundary_reached")) is not bool:
        return None
    active_order_id = value.get("active_order_id")
    reason_code = value.get("reason_code")
    if active_order_id is not None and (
        not isinstance(active_order_id, str)
        or not _ORDER_ID.fullmatch(active_order_id)
        or active_order_id not in reviewed
    ):
        return None
    if reason_code is not None and (
        not isinstance(reason_code, str)
        or not _REASON_CODE.fullmatch(reason_code)
    ):
        return None
    if value["state"] == "cancelling" and (
        active_order_id is None or reason_code is not None
    ):
        return None
    if value["state"] == "unknown_locked" and (
        active_order_id is None or reason_code is None
    ):
        return None
    if value["state"] not in {"cancelling", "unknown_locked"} and (
        active_order_id is not None or reason_code is not None
    ):
        return None
    handoff_at = value.get("handoff_at")
    authority_is_legacy = (
        authority_production_sha == production_sha
        and value["authority_service_name"] == value["service_name"]
        and authority_worker_pid == worker_pid
        and authority_worker_start_ticks == worker_start_ticks
    )
    if handoff_at is None:
        if not authority_is_legacy or value["state"] == "handed_off":
            return None
    else:
        try:
            handoff_at = _aware_utc_text(handoff_at).isoformat()
        except ValueError:
            return None
        if authority_is_legacy or value["state"] in {"frozen", "fenced"}:
            return None
    drained_at = value.get("drained_at")
    drain_fingerprint = value.get("drain_evidence_fingerprint")
    if value["state"] in {"drained", "released_for_deploy"}:
        try:
            drained_at = _aware_utc_text(drained_at).isoformat()
            drain_fingerprint = _sha256(
                drain_fingerprint,
                field_name="drain_evidence_fingerprint",
            )
        except ValueError:
            return None
        if tuple(completed) != tuple(reviewed):
            return None
    elif drained_at is not None or drain_fingerprint is not None:
        return None
    return {
        **value,
        "production_sha": production_sha,
        "authority_production_sha": authority_production_sha,
        "frozen_at": frozen_at.isoformat(),
        "handoff_at": handoff_at,
        "updated_at": updated_at.isoformat(),
        "reviewed_order_ids": list(reviewed),
        "fenced_batch_ids": list(fenced),
        "completed_order_ids": list(completed),
        "drained_at": drained_at,
        "drain_evidence_fingerprint": drain_fingerprint,
    }


def _owned_bridge_in_session(
    session,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    allowed_states: set[str],
):
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
        .one_or_none()
    )
    document = None if row is None else _bridge_document(row.value_json)
    if row is None or document is None:
        return row, document, "legacy_bridge_state_invalid"
    if document["state"] not in allowed_states:
        return row, document, "legacy_bridge_state_mismatch"
    if document["bridge_token"] != bridge_token:
        return row, document, "legacy_bridge_owner_mismatch"
    if (
        document["authority_production_sha"]
        != runtime_identity.production_sha
    ):
        return row, document, "legacy_bridge_production_sha_drift"
    if document["authority_service_name"] != runtime_identity.service_name:
        return row, document, "legacy_bridge_worker_identity_drift"
    if (
        document["authority_worker_pid"] != runtime_identity.worker_pid
        or document["authority_worker_start_ticks"]
        != runtime_identity.worker_start_ticks
    ):
        return row, document, "legacy_bridge_worker_identity_drift"
    return row, document, None


def _frozen_settings_reason(session) -> str | None:
    _row, payload = _settings_row_and_payload_in_session(session)
    try:
        settings = trading_settings_from_payload(payload)
    except ValueError:
        return "legacy_bridge_settings_invalid"
    if settings.auto_trade_enabled is not False:
        return "legacy_bridge_auto_trade_not_frozen"
    if settings.legacy_entry_submission_frozen is not True:
        return "legacy_bridge_entry_submission_not_frozen"
    if settings.entry_revision_v2_mode != "disabled":
        return "legacy_bridge_revision_not_disabled"
    if settings.message_pipeline_mode != "queue":
        return "legacy_bridge_message_pipeline_not_queue"
    return None


def _bridge_cancellation_invariant_reason(
    session,
    *,
    document: dict[str, Any],
    order_id: str,
) -> str | None:
    if order_id not in document["reviewed_order_ids"]:
        return "legacy_bridge_order_not_reviewed"
    if order_id in document["completed_order_ids"]:
        return "legacy_bridge_order_already_completed"
    settings_reason = _frozen_settings_reason(session)
    if settings_reason is not None:
        return settings_reason
    return _revision_sentinel_reason(session, document=document)


def _revision_sentinel_reason(
    session,
    *,
    document: dict[str, Any],
) -> str | None:
    active_ids = tuple(
        int(batch_id)
        for (batch_id,) in _active_revision_batches_query(session)
        .with_entities(StrategyRevisionBatch.id)
        .order_by(StrategyRevisionBatch.id.asc())
        .all()
    )
    fenced_ids = tuple(document["fenced_batch_ids"])
    if active_ids != fenced_ids:
        return "legacy_bridge_active_revision_set_drift"
    claimed_ids = tuple(
        int(batch_id)
        for (batch_id,) in _active_revision_batches_query(session)
        .with_entities(StrategyRevisionBatch.id)
        .filter(
            (
                (StrategyRevisionBatch.advance_claim_token.is_not(None))
                | (StrategyRevisionBatch.advance_claimed_at.is_not(None))
            ),
        )
        .order_by(StrategyRevisionBatch.id.asc())
        .all()
    )
    if claimed_ids != fenced_ids:
        return "legacy_bridge_revision_claim_set_drift"
    if not fenced_ids:
        return None
    batches = (
        session.query(StrategyRevisionBatch)
        .filter(StrategyRevisionBatch.id.in_(fenced_ids))
        .all()
    )
    if len(batches) != len(fenced_ids) or any(
        batch.advance_claim_token != document["bridge_token"]
        or batch.advance_claimed_at is not None
        for batch in batches
    ):
        return "legacy_bridge_sentinel_drift"
    return None


def _active_revision_batches_query(session):
    return session.query(StrategyRevisionBatch).filter(
        StrategyRevisionBatch.status.not_in(
            _TERMINAL_REVISION_BATCH_STATES
        )
    )


def _drain_evidence_reason(
    evidence: LegacyRuntimeDrainEvidence,
) -> str | None:
    if not isinstance(evidence, LegacyRuntimeDrainEvidence):
        return "legacy_bridge_drain_evidence_invalid"
    rebuilt = build_legacy_runtime_drain_evidence(
        reviewed_order_ids=evidence.reviewed_order_ids,
        completed_order_ids=evidence.completed_order_ids,
        plan_fingerprint=evidence.plan_fingerprint,
        action_count=evidence.action_count,
        conflict_count=evidence.conflict_count,
        positions_count=evidence.positions_count,
        regular_order_count=evidence.regular_order_count,
        pending_trigger_count=evidence.pending_trigger_count,
        unidentified_pending_count=evidence.unidentified_pending_count,
        unreviewed_pending_count=evidence.unreviewed_pending_count,
        fill_conflict_count=evidence.fill_conflict_count,
        queries_complete=evidence.queries_complete,
        history_complete=evidence.history_complete,
        observed_at=evidence.observed_at,
    )
    if rebuilt != evidence:
        return "legacy_bridge_drain_evidence_invalid"
    if not evidence.queries_complete:
        return "legacy_bridge_exchange_query_incomplete"
    if not evidence.history_complete:
        return "legacy_bridge_history_incomplete"
    if evidence.positions_count:
        return "legacy_bridge_position_present"
    if evidence.regular_order_count:
        return "legacy_bridge_regular_order_present"
    if evidence.pending_trigger_count:
        return "legacy_bridge_pending_trigger_present"
    if evidence.unidentified_pending_count:
        return "legacy_bridge_unidentified_pending_trigger"
    if evidence.unreviewed_pending_count:
        return "legacy_bridge_unreviewed_pending_trigger"
    if evidence.fill_conflict_count:
        return "legacy_bridge_fill_conflict"
    if evidence.conflict_count:
        return "legacy_bridge_drain_plan_conflict"
    if evidence.action_count:
        return "legacy_bridge_reviewed_set_incomplete"
    return None


def _drain_evidence_freshness_reason(
    evidence: LegacyRuntimeDrainEvidence,
    *,
    transition_at: datetime,
) -> str | None:
    age_seconds = (transition_at - evidence.observed_at).total_seconds()
    if age_seconds < 0 or age_seconds > 60:
        return "legacy_bridge_exchange_evidence_stale"
    return None


def _bridge_unknown_mutation_reason(
    session,
    *,
    reviewed_order_ids: tuple[str, ...],
) -> str | None:
    """Recheck target-related or unowned write ambiguity inside the gate tx."""

    reviewed = tuple(reviewed_order_ids)
    target_legs = tuple(
        (int(leg_id), int(binding_id))
        for leg_id, binding_id in session.query(
            ExecutionOrderLeg.id,
            ExecutionOrderLeg.execution_binding_id,
        )
        .filter(ExecutionOrderLeg.order_id.in_(reviewed))
        .all()
    )
    leg_ids = {leg_id for leg_id, _binding_id in target_legs}
    binding_ids = {binding_id for _leg_id, binding_id in target_legs}
    lifecycle_ids = {
        int(lifecycle_id)
        for (lifecycle_id,) in session.query(StrategyLifecycle.id)
        .filter(StrategyLifecycle.execution_binding_id.in_(binding_ids))
        .all()
    }
    possible_cancel_rows = (
        session.query(PositionMutationIntent)
        .filter(
            PositionMutationIntent.operation
            == "cancel_reviewed_pending_entry",
            PositionMutationIntent.status != "confirmed",
            (
                PositionMutationIntent.order_id.in_(reviewed)
                | PositionMutationIntent.order_id.is_(None)
                | (PositionMutationIntent.order_id == "")
            ),
        )
        .all()
    )
    if any(
        not is_valid_reviewed_pending_entry_prewrite_refusal(row)
        for row in possible_cancel_rows
    ):
        return "legacy_bridge_unknown_mutation_present"

    unknown_revision_leg = (
        session.query(StrategyRevisionLeg.id)
        .outerjoin(
            StrategyRevisionBatch,
            StrategyRevisionBatch.id == StrategyRevisionLeg.revision_batch_id,
        )
        .filter(
            StrategyRevisionLeg.status.in_(
                {"cancel_submitting", "submit_unknown"}
            ),
            (
                StrategyRevisionBatch.id.is_(None)
                | StrategyRevisionBatch.execution_binding_id.in_(binding_ids)
                | StrategyRevisionBatch.target_lifecycle_id.in_(lifecycle_ids)
                | StrategyRevisionLeg.execution_order_leg_id.in_(leg_ids)
                | StrategyRevisionLeg.order_id.in_(reviewed)
            ),
        )
        .first()
    )
    if unknown_revision_leg is not None:
        return "legacy_bridge_unknown_mutation_present"

    unknown_replacement = (
        session.query(EntryRevisionReplacement.id)
        .outerjoin(
            StrategyRevisionBatch,
            StrategyRevisionBatch.id
            == EntryRevisionReplacement.revision_batch_id,
        )
        .filter(
            EntryRevisionReplacement.status.in_(
                {"submit_reserved", "submitted"}
            ),
            (
                StrategyRevisionBatch.id.is_(None)
                | StrategyRevisionBatch.execution_binding_id.in_(binding_ids)
                | StrategyRevisionBatch.target_lifecycle_id.in_(lifecycle_ids)
                | EntryRevisionReplacement.execution_order_leg_id.in_(leg_ids)
                | EntryRevisionReplacement.order_id.in_(reviewed)
            ),
        )
        .first()
    )
    if unknown_replacement is not None:
        return "legacy_bridge_unknown_mutation_present"
    return None


def is_valid_reviewed_pending_entry_prewrite_refusal(
    row: PositionMutationIntent,
) -> bool:
    """Accept only exact durable proof that no reviewed cancel was submitted."""

    if (
        not isinstance(row, PositionMutationIntent)
        or row.venue != "deepcoin"
        or row.operation != "cancel_reviewed_pending_entry"
        or row.status != "prewrite_refused"
        or row.submitted_at is not None
        or row.confirmed_at is not None
        or not isinstance(row.order_id, str)
        or not 1 <= len(row.order_id) <= 64
        or row.pos_id != f"pending-entry:{row.order_id}"
        or not _is_sha256(row.authority_fingerprint)
        or not _is_sha256(row.request_fingerprint)
    ):
        return False
    prefix = f"reviewed-pending-entry-cancel:{row.order_id}:"
    if not row.idempotency_key.startswith(prefix) or not _is_sha256(
        row.idempotency_key[len(prefix) :]
    ):
        return False
    request = _strict_json_object(row.request_json)
    response = _strict_json_object(row.response_json)
    error = _strict_json_object(row.error_json)
    if (
        request is None
        or set(request) != {"instId", "ordId"}
        or request.get("ordId") != row.order_id
        or not isinstance(request.get("instId"), str)
        or not 1 <= len(request["instId"]) <= 64
        or row.request_fingerprint != _payload_fingerprint(request)
        or response != {"submitted": False}
        or error is None
        or set(error) != {"reason"}
        or error.get("reason") not in _PREWRITE_REFUSAL_REASONS
    ):
        return False
    return True


def _strict_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _local_drain_reason(session, *, document: dict[str, Any]) -> str | None:
    for order_id in document["reviewed_order_ids"]:
        intents = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.operation
                == "cancel_reviewed_pending_entry",
                PositionMutationIntent.order_id == order_id,
            )
            .all()
        )
        if len(intents) != 1 or intents[0].status != "confirmed":
            return "legacy_bridge_local_state_incomplete"
        intent = intents[0]
        leg = session.get(ExecutionOrderLeg, intent.execution_order_leg_id)
        binding = session.get(ExecutionBinding, intent.execution_binding_id)
        lifecycles = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.execution_binding_id
                == intent.execution_binding_id
            )
            .all()
        )
        lifecycle = lifecycles[0] if len(lifecycles) == 1 else None
        binding_entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id
                == intent.execution_binding_id,
                ExecutionOrderLeg.purpose == "entry",
            )
            .all()
        )
        if (
            leg is None
            or binding is None
            or lifecycle is None
            or leg.order_id != order_id
            or leg.execution_binding_id != intent.execution_binding_id
            or leg.strategy_instance_id != intent.strategy_instance_id
            or str(leg.venue or "").lower() != "deepcoin"
            or leg.purpose != "entry"
            or leg.pos_id not in (None, "")
            or leg.status not in {"cancelled", "canceled"}
            or leg.terminal_reason != "operator_cancelled_unfilled_entry_leg"
        ):
            return "legacy_bridge_local_state_incomplete"
        events = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "cancel_reviewed_pending_entry",
                ExecutionEvent.order_id == order_id,
                ExecutionEvent.status == "confirmed",
            )
            .all()
        )
        protection_intents = (
            session.query(TriggerProtectionIntent)
            .filter(
                TriggerProtectionIntent.venue == "deepcoin",
                TriggerProtectionIntent.execution_order_leg_id == leg.id,
            )
            .all()
        )
        protection_legs = (
            session.query(PositionProtectionLeg)
            .filter(
                PositionProtectionLeg.venue == "deepcoin",
                PositionProtectionLeg.execution_order_leg_id == leg.id,
            )
            .all()
        )
        convergence = (
            session.query(TriggerTakeProfitConvergence)
            .filter(
                TriggerTakeProfitConvergence.venue == "deepcoin",
                TriggerTakeProfitConvergence.execution_order_leg_id == leg.id,
            )
            .all()
        )
        event = events[0] if len(events) == 1 else None
        stored_request = _json_dict(leg.request_json)
        cancel_request = _json_dict(intent.request_json)
        cancel_response = _json_dict(intent.response_json)
        event_before = _json_dict(
            event.before_json if event is not None else None
        )
        expected_event_before = {
            "action_id": event_before.get("action_id"),
            "exchange_row_fingerprint": event_before.get(
                "exchange_row_fingerprint"
            ),
            "plan_fingerprint": event_before.get("plan_fingerprint"),
        }
        expected_authority = {
            **expected_event_before,
            "request_json_fingerprint": _payload_fingerprint(stored_request),
        }
        if not (
            event is not None
            and intent.venue == "deepcoin"
            and intent.pos_id == f"pending-entry:{order_id}"
            and binding.strategy_instance_id == intent.strategy_instance_id
            and str(binding.venue or "").lower() == "deepcoin"
            and str(binding.symbol or "").upper()
            == cancel_request.get("instId", "").removesuffix("-USDT-SWAP")
            and str(binding.side or "").lower() == "long"
            and str(binding.status or "").lower() == "cancelled"
            and binding.last_exchange_status
            == "reviewed_pending_entries_cancelled"
            and bool(binding_entry_legs)
            and all(
                str(row.status or "").lower() in _TERMINAL_ENTRY_LEG_STATES
                for row in binding_entry_legs
            )
            and lifecycle.execution_binding_id == intent.execution_binding_id
            and str(lifecycle.symbol or "").upper()
            == cancel_request.get("instId", "").removesuffix("-USDT-SWAP")
            and str(lifecycle.side or "").lower() == "long"
            and str(lifecycle.lifecycle_status or "") == "expired"
            and lifecycle.exit_reason == "expired"
            and lifecycle.exited_at is not None
            and lifecycle.management_action
            == "reviewed_pending_entries_cancelled"
            and event.execution_binding_id == intent.execution_binding_id
            and event.strategy_instance_id == intent.strategy_instance_id
            and event.venue == "deepcoin"
            and event.symbol == cancel_request.get("instId", "").split("-")[0]
            and event.side == "long"
            and event.reason == "reviewed_stale_pending_entry_cancelled"
            and event_before == expected_event_before
            and all(
                _is_sha256(expected_event_before[field])
                for field in expected_event_before
            )
            and _json_dict(event.after_json)
            == {"pending": False, "terminalized": True}
            and _json_dict(event.request_json) == cancel_request
            and _json_dict(event.response_json) == cancel_response
            and cancel_request.get("ordId") == order_id
            and isinstance(cancel_request.get("instId"), str)
            and bool(cancel_request["instId"])
            and cancel_response == {"code": "0", "order_id": order_id}
            and intent.idempotency_key
            == (
                f"reviewed-pending-entry-cancel:{order_id}:"
                f"{expected_event_before['action_id']}"
            )
            and intent.request_fingerprint
            == _payload_fingerprint(cancel_request)
            and intent.authority_fingerprint
            == _payload_fingerprint(expected_authority)
            and intent.error_json in (None, "")
            and intent.submitted_at is not None
            and intent.confirmed_at is not None
            and len(protection_intents) == 1
            and protection_intents[0].execution_binding_id
            == intent.execution_binding_id
            and protection_intents[0].parent_trigger_order_id == order_id
            and protection_intents[0].recovery_state == "resolved"
            and protection_intents[0].recovery_disposition == "terminal"
            and protection_intents[0].last_reason_code
            == "parent_trigger_cancelled_before_entry"
            and len(protection_legs) == 2
            and {row.role for row in protection_legs}
            == {"primary_stop", "backup_stop"}
            and all(
                row.status == "cancelled"
                and row.execution_binding_id == intent.execution_binding_id
                and row.parent_entry_order_id == order_id
                for row in protection_legs
            )
            and len(convergence) == 1
            and convergence[0].execution_binding_id
            == intent.execution_binding_id
            and convergence[0].status == "completed"
            and convergence[0].reason_code
            == "parent_trigger_cancelled_before_entry"
            and convergence[0].completed_at is not None
        ):
            return "legacy_bridge_local_state_incomplete"
    return None


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _inner_authority_reason(session) -> str | None:
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
        .one_or_none()
    )
    if row is None:
        return None
    document = _authority_document(row.value_json)
    if document is None:
        return "legacy_bridge_inner_authority_invalid"
    if document["state"] != "idle":
        return "legacy_bridge_inner_authority_held"
    return None


def _consume_confirmation_in_session(
    session,
    *,
    confirmation_token: str,
    action_kind: str,
    action_id: str,
    pos_id: str,
    consumed_at: datetime,
) -> None:
    session.add(
        RepairConfirmationToken(
            token_hash=hashlib.sha256(confirmation_token.encode()).hexdigest(),
            action_kind=action_kind,
            action_id=action_id,
            pos_id=pos_id,
            consumed_at=consumed_at,
        )
    )


def _confirmation_token(value: Any) -> str:
    clean = str(value or "").strip()
    if len(clean) < 8 or len(clean) > 256:
        raise ValueError("confirmation_token is required")
    return clean


def _bridge_token(value: Any) -> str:
    clean = str(value or "")
    if not _TOKEN.fullmatch(clean):
        raise ValueError("bridge_token is invalid")
    return clean


def _order_id(value: Any) -> str:
    clean = str(value or "")
    if not _ORDER_ID.fullmatch(clean):
        raise ValueError("order_id is invalid")
    return clean


def _reason_code(value: Any) -> str:
    clean = str(value or "")
    if not _REASON_CODE.fullmatch(clean):
        raise ValueError("reason_code is invalid")
    return clean


def _fingerprint_matches(actual: Any, expected: Any) -> bool:
    expected_text = str(expected or "")
    return bool(
        isinstance(actual, str)
        and len(actual) == 64
        and expected_text == actual
        and all(character in "0123456789abcdef" for character in actual)
    )


def _result(status: str, reason_code: str) -> LegacyRuntimeDrainBridgeResult:
    return LegacyRuntimeDrainBridgeResult(status=status, reason_code=reason_code)


def _sha(value: Any, *, field_name: str) -> str:
    text = str(value or "")
    if not _SHA.fullmatch(text):
        raise ValueError(f"{field_name} must be a lowercase full SHA")
    return text


def _sha256(value: Any, *, field_name: str) -> str:
    text_value = str(value or "")
    if len(text_value) != 64 or any(
        character not in "0123456789abcdef" for character in text_value
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA256")
    return text_value


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _run_bounded_command(command_runner, argv: list[str]) -> str:
    try:
        completed = command_runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("runtime identity command failed") from exc
    if completed.returncode != 0:
        raise ValueError("runtime identity command failed")
    output = str(completed.stdout or "")
    if len(output) > 4096:
        raise ValueError("runtime identity command output is too large")
    clean = output.strip()
    if not clean or "\n" in clean or "\r" in clean:
        raise ValueError("runtime identity command output is invalid")
    return clean


def _read_proc_worker_evidence(
    pid_root: Path,
    *,
    checkout: Path,
) -> None:
    try:
        directory = os.open(
            pid_root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ValueError("proc worker identity is unavailable") from exc
    try:
        try:
            cwd_text = os.readlink("cwd", dir_fd=directory)
        except OSError as exc:
            raise ValueError("proc cwd is unavailable") from exc
        if (
            len(cwd_text) > 4096
            or not Path(cwd_text).is_absolute()
            or Path(cwd_text).resolve() != checkout
        ):
            raise ValueError("proc cwd does not match checkout")
        try:
            cmdline_descriptor = os.open(
                "cmdline",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
        except OSError as exc:
            raise ValueError("proc cmdline is unavailable") from exc
        try:
            metadata = os.fstat(cmdline_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("proc cmdline is invalid")
            raw_cmdline = os.read(cmdline_descriptor, 4097)
        except OSError as exc:
            raise ValueError("proc cmdline is unavailable") from exc
        finally:
            os.close(cmdline_descriptor)
    finally:
        os.close(directory)
    if len(raw_cmdline) > 4096:
        raise ValueError("proc cmdline is too large")
    if not raw_cmdline or not raw_cmdline.endswith(b"\0"):
        raise ValueError("proc cmdline is invalid")
    try:
        tokens = [
            token.decode("utf-8")
            for token in raw_cmdline[:-1].split(b"\0")
        ]
    except UnicodeDecodeError as exc:
        raise ValueError("proc cmdline is invalid") from exc
    if (
        len(tokens) < 4
        or len(tokens) > 128
        or any(not token or len(token) > 1024 for token in tokens)
        or Path(tokens[0]).name != "telegram-kol-research"
        or tokens[1] != "web"
        or tokens.count("--runtime-role") != 1
    ):
        raise ValueError("proc cmdline is invalid")
    role_index = tokens.index("--runtime-role")
    if role_index + 1 >= len(tokens) or tokens[role_index + 1] != "worker":
        raise ValueError("proc cmdline does not identify worker role")


def _read_proc_start_ticks(stat_path: Path) -> int:
    try:
        descriptor = os.open(
            stat_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ValueError("proc stat is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("proc stat is invalid")
        raw_bytes = os.read(descriptor, 4097)
    except OSError as exc:
        raise ValueError("proc stat is unavailable") from exc
    finally:
        os.close(descriptor)
    if len(raw_bytes) > 4096:
        raise ValueError("proc stat is too large")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("proc stat is invalid") from exc
    close_paren = raw.rfind(")")
    if close_paren <= 0:
        raise ValueError("proc stat is invalid")
    fields = raw[close_paren + 1 :].strip().split()
    if len(fields) <= 19 or not fields[19].isdigit():
        raise ValueError("proc stat is invalid")
    start_ticks = int(fields[19])
    if start_ticks <= 0:
        raise ValueError("proc stat is invalid")
    return start_ticks


def _reviewed_order_ids(values: Iterable[str]) -> tuple[str, ...]:
    try:
        result = tuple(str(value) for value in values)
    except TypeError as exc:
        raise ValueError("reviewed_order_ids must be iterable") from exc
    if (
        len(result) != 7
        or len(set(result)) != len(result)
        or any(not _ORDER_ID.fullmatch(value) for value in result)
    ):
        raise ValueError("reviewed_order_ids must contain seven unique ids")
    return result


def _positive_unique_ints(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list):
        raise ValueError("batch ids must be a list")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError("batch ids must be positive integers")
    if len(values) != len(set(values)) or values != sorted(values):
        raise ValueError("batch ids must be sorted and unique")
    return tuple(values)


def _completed_order_ids(
    values: Any,
    *,
    reviewed: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("completed order ids must be a list")
    completed = tuple(str(value) for value in values)
    if (
        len(completed) != len(set(completed))
        or any(value not in reviewed for value in completed)
        or list(completed) != sorted(completed)
    ):
        raise ValueError("completed order ids must be a sorted reviewed subset")
    return completed


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _aware_utc_text(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("timestamp must be bounded text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    return _aware_utc(parsed, field_name="timestamp")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
