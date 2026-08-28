"""Fail-closed bridge from the legacy worker to leased cancellation authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Iterable
import uuid

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from telegram_kol_research.models import (
    MessageProcessingJob,
    RawMessage,
    RepairConfirmationToken,
    StrategyRevisionBatch,
    TradingSetting,
)
from telegram_kol_research.trading_settings import (
    _persist_trading_settings_in_session,
    _settings_row_and_payload_in_session,
    trading_settings_from_payload,
)


LEGACY_RUNTIME_DRAIN_BRIDGE_KEY = "legacy_runtime_drain_bridge"
_SCHEMA_VERSION = 1
_STATES = frozenset(
    {
        "frozen",
        "fenced",
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
        "bridge_token",
        "production_sha",
        "worker_pid",
        "worker_start_ticks",
        "frozen_at",
        "freeze_raw_message_id",
        "original_auto_trade_enabled",
        "original_entry_revision_v2_mode",
        "reviewed_order_ids",
        "fenced_batch_ids",
        "completed_order_ids",
        "write_boundary_reached",
        "updated_at",
    }
)
_TERMINAL_REVISION_BATCH_STATES = frozenset(
    {"succeeded", "blocked", "failed", "recovery_required"}
)
_SHA = re.compile(r"[0-9a-f]{40}")
_ORDER_ID = re.compile(r"[0-9]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class LegacyRuntimeIdentity:
    production_sha: str
    worker_pid: int
    worker_start_ticks: int

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
        if document["production_sha"] != expected_sha:
            conflicts.append({"reason": "legacy_bridge_production_sha_drift"})
        if (
            document["worker_pid"] != runtime_identity.worker_pid
            or document["worker_start_ticks"]
            != runtime_identity.worker_start_ticks
        ):
            conflicts.append({"reason": "legacy_bridge_worker_identity_drift"})
        if tuple(document["reviewed_order_ids"]) != reviewed:
            conflicts.append({"reason": "legacy_bridge_reviewed_set_drift"})

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
        "planned_at": observed_at.isoformat(),
        "reviewed_order_ids": list(reviewed),
        "runtime_identity": {
            "production_sha": runtime_identity.production_sha,
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
                "bridge_token": bridge_token,
                "completed_order_ids": [],
                "fenced_batch_ids": [],
                "freeze_raw_message_id": watermark,
                "frozen_at": observed_at.isoformat(),
                "original_auto_trade_enabled": settings.auto_trade_enabled,
                "original_entry_revision_v2_mode": settings.entry_revision_v2_mode,
                "production_sha": runtime_identity.production_sha,
                "reviewed_order_ids": list(reviewed),
                "schema_version": _SCHEMA_VERSION,
                "state": "frozen",
                "updated_at": observed_at.isoformat(),
                "worker_pid": runtime_identity.worker_pid,
                "worker_start_ticks": runtime_identity.worker_start_ticks,
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
    fenced_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Install exact null-time claims that the unchanged worker cannot steal."""

    clean_token = _bridge_token(bridge_token)
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
                session.query(StrategyRevisionBatch)
                .filter(
                    StrategyRevisionBatch.status.not_in(
                        _TERMINAL_REVISION_BATCH_STATES
                    )
                )
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
            session.commit()
            return LegacyRuntimeDrainBridgeResult(
                status="fenced",
                bridge_token=clean_token,
            )
    except SQLAlchemyError:
        return _result("blocked", "legacy_bridge_database_unavailable")


def rollback_legacy_runtime_drain_bridge(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    rolled_back_at: datetime,
) -> LegacyRuntimeDrainBridgeResult:
    """Restore pre-freeze settings only before any reviewed write boundary."""

    clean_token = _bridge_token(bridge_token)
    observed_at = _aware_utc(rolled_back_at, field_name="rolled_back_at")
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row, document, error = _owned_bridge_in_session(
                session,
                bridge_token=clean_token,
                runtime_identity=runtime_identity,
                allowed_states={"frozen", "fenced"},
            )
            if error is not None:
                session.rollback()
                return _result("blocked", error)
            if document["write_boundary_reached"] or document["completed_order_ids"]:
                session.rollback()
                return _result("blocked", "legacy_bridge_rollback_forbidden")
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
            session.delete(row)
            session.commit()
            return LegacyRuntimeDrainBridgeResult(status="rolled_back")
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
    watermark = value.get("freeze_raw_message_id")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < minimum
        for item, minimum in (
            (worker_pid, 1),
            (worker_start_ticks, 1),
            (watermark, 0),
        )
    ):
        return None
    if type(value.get("original_auto_trade_enabled")) is not bool:
        return None
    if value.get("original_entry_revision_v2_mode") not in {
        "disabled",
        "shadow",
        "live",
    }:
        return None
    if type(value.get("write_boundary_reached")) is not bool:
        return None
    return {
        **value,
        "production_sha": production_sha,
        "frozen_at": frozen_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "reviewed_order_ids": list(reviewed),
        "fenced_batch_ids": list(fenced),
        "completed_order_ids": list(completed),
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
    if document["production_sha"] != runtime_identity.production_sha:
        return row, document, "legacy_bridge_production_sha_drift"
    if (
        document["worker_pid"] != runtime_identity.worker_pid
        or document["worker_start_ticks"] != runtime_identity.worker_start_ticks
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
    if settings.entry_revision_v2_mode != "disabled":
        return "legacy_bridge_revision_not_disabled"
    if settings.message_pipeline_mode != "queue":
        return "legacy_bridge_message_pipeline_not_queue"
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
