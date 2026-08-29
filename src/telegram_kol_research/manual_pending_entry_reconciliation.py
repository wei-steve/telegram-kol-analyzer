"""Reconcile pending entries after an operator cancels them at Deepcoin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from sqlalchemy import text

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidenceRefused,
    build_deepcoin_maintenance_evidence,
    deepcoin_order_id,
    require_fresh_deepcoin_maintenance_evidence,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    StrategyLifecycle,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.reviewed_pending_entry_targets import (
    ReviewedPendingEntryTarget,
)


_GOVERNED_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)


@dataclass(frozen=True, slots=True)
class ManualPendingEntryReconciliationPlan:
    status: str
    reason_code: str | None
    target_order_ids: tuple[str, ...]
    evidence_sha256: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ManualPendingEntryReconciliationResult:
    status: str
    terminalized_count: int
    authority_seeded: bool
    backup_path: Path


def build_manual_pending_entry_reconciliation_plan(
    session_factory,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    now: datetime | None = None,
) -> ManualPendingEntryReconciliationPlan:
    """Prove all exchange entries are gone and local targets are exact."""

    observed_at = _timestamp(now or datetime.now(UTC))
    reviewed = tuple(targets)
    order_ids = tuple(target.order_id for target in reviewed)
    if not reviewed or len(set(order_ids)) != len(order_ids):
        return _blocked("canonical_target_set_invalid", order_ids)
    evidence = build_deepcoin_maintenance_evidence(
        deepcoin_client,
        instruments=_GOVERNED_INSTRUMENTS,
        target_order_id="manual-cancel-all",
        expected_target_pending_count=0,
        observed_at=observed_at if now is not None else None,
    )
    freshness_now = observed_at if now is not None else _timestamp(datetime.now(UTC))
    try:
        require_fresh_deepcoin_maintenance_evidence(evidence, now=freshness_now)
    except DeepcoinMaintenanceEvidenceRefused:
        return _blocked(
            evidence.reason_code or "exchange_snapshot_unknown",
            order_ids,
            evidence_sha256=evidence.fingerprint,
        )
    if evidence.positions:
        return _blocked("live_position_present", order_ids, evidence.fingerprint)
    if evidence.regular_orders:
        return _blocked("regular_order_present", order_ids, evidence.fingerprint)
    if evidence.pending_triggers:
        return _blocked("pending_trigger_present", order_ids, evidence.fingerprint)
    target_order_ids = set(order_ids)
    if any(deepcoin_order_id(row) in target_order_ids for row in evidence.fills):
        return _blocked("target_fill_present", order_ids, evidence.fingerprint)
    target_history = (
        row
        for row in evidence.trigger_history
        if deepcoin_order_id(row) in target_order_ids
    )
    if any(
        str(row.get("state") or row.get("status") or "").strip().lower()
        not in {"cancelled", "canceled"}
        for row in target_history
    ):
        return _blocked(
            "target_history_not_cancelled",
            order_ids,
            evidence.fingerprint,
        )

    completed_count = 0
    with session_factory() as session:
        for target in reviewed:
            if _target_completed(session, target):
                completed_count += 1
                continue
            reason = _target_reason(session, target)
            if reason is not None:
                return _blocked(reason, order_ids, evidence.fingerprint)
        authority = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one_or_none()
        )
        if authority is not None and not _valid_idle_authority(authority.value_json):
            return _blocked(
                "entry_revision_exchange_authority_not_idle",
                order_ids,
                evidence.fingerprint,
            )

    if completed_count not in {0, len(reviewed)}:
        return _blocked(
            "partial_local_terminalization",
            order_ids,
            evidence.fingerprint,
        )
    fingerprint = _fingerprint(
        {
            "evidence_sha256": evidence.fingerprint,
            "target_order_ids": order_ids,
        }
    )
    return ManualPendingEntryReconciliationPlan(
        status="completed" if completed_count == len(reviewed) else "ready",
        reason_code=None,
        target_order_ids=order_ids,
        evidence_sha256=evidence.fingerprint,
        fingerprint=fingerprint,
    )


def apply_manual_pending_entry_reconciliation(
    session_factory,
    *,
    database_path: Path,
    backup_path: Path,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    expected_fingerprint: str,
    now: datetime | None = None,
) -> ManualPendingEntryReconciliationResult:
    """Back up SQLite and terminalize all reviewed targets in one transaction."""

    observed_at = _timestamp(now or datetime.now(UTC))
    reviewed = tuple(targets)
    fresh = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        targets=reviewed,
        now=observed_at,
    )
    if fresh.status != "ready" or fresh.fingerprint != expected_fingerprint:
        raise ValueError(fresh.reason_code or "manual_reconciliation_plan_drift")
    _create_verified_backup(Path(database_path), Path(backup_path))

    authority_seeded = False
    with session_factory() as session:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            for target in reviewed:
                reason = _target_reason(session, target)
                if reason is not None:
                    raise ValueError(reason)
            authority = (
                session.query(TradingSetting)
                .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
                .one_or_none()
            )
            if authority is None:
                session.add(
                    TradingSetting(
                        key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                        value_json=_canonical_json(
                            {
                                "generation": 0,
                                "released_at": observed_at.isoformat(),
                                "schema_version": 2,
                                "state": "idle",
                            }
                        ),
                        updated_at=observed_at,
                    )
                )
                authority_seeded = True
            elif not _valid_idle_authority(authority.value_json):
                raise ValueError("entry_revision_exchange_authority_not_idle")

            affected_bindings: set[int] = set()
            for target in reviewed:
                _terminalize_target(session_factory, session, target, observed_at)
                affected_bindings.add(target.execution_binding_id)
            session.flush()
            for binding_id in affected_bindings:
                _terminalize_binding(session, binding_id, observed_at)
            session.commit()
        except Exception:
            session.rollback()
            raise

    return ManualPendingEntryReconciliationResult(
        status="completed",
        terminalized_count=len(reviewed),
        authority_seeded=authority_seeded,
        backup_path=Path(backup_path),
    )


def _target_reason(session, target: ReviewedPendingEntryTarget) -> str | None:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    binding = session.get(ExecutionBinding, target.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
    intents = (
        session.query(TriggerProtectionIntent)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    protection = (
        session.query(PositionProtectionLeg)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    events = (
        session.query(ExecutionEvent)
        .filter_by(
            action="reconcile_manual_pending_entry_cancel",
            order_id=target.order_id,
        )
        .count()
    )
    if events:
        return "manual_reconciliation_already_recorded"
    if not (
        leg is not None
        and binding is not None
        and lifecycle is not None
        and leg.execution_binding_id == target.execution_binding_id
        and leg.order_id == target.order_id
        and leg.purpose == "entry"
        and str(leg.status or "").lower() in {"pending", "open", "submitted"}
        and str(binding.status or "").lower() in {"open", "active"}
        and lifecycle.execution_binding_id == target.execution_binding_id
        and lifecycle.lifecycle_status == "pending_entry"
        and len(intents) == 1
        and intents[0].execution_binding_id == target.execution_binding_id
        and intents[0].parent_trigger_order_id == target.order_id
        and intents[0].request_fingerprint == target.request_fingerprint
        and intents[0].recovery_state in {"pending", "retrying"}
        and len(protection) == 2
        and {row.role for row in protection} == {"primary_stop", "backup_stop"}
        and all(row.status in {"planned", "waiting_fill"} for row in protection)
        and len(convergence) == 1
        and convergence[0].status
        in {"waiting_backup_stop", "waiting_position", "ready"}
    ):
        return "reviewed_local_state_changed"
    return None


def _target_completed(session, target: ReviewedPendingEntryTarget) -> bool:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    binding = session.get(ExecutionBinding, target.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
    intents = (
        session.query(TriggerProtectionIntent)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    protection = (
        session.query(PositionProtectionLeg)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    events = (
        session.query(ExecutionEvent)
        .filter_by(
            action="reconcile_manual_pending_entry_cancel",
            order_id=target.order_id,
            status="confirmed",
        )
        .count()
    )
    return bool(
        leg is not None
        and binding is not None
        and lifecycle is not None
        and leg.order_id == target.order_id
        and leg.status == "cancelled"
        and binding.status == "cancelled"
        and lifecycle.lifecycle_status == "expired"
        and len(intents) == 1
        and intents[0].recovery_state == "resolved"
        and intents[0].recovery_disposition == "terminal"
        and len(protection) == 2
        and all(row.status == "cancelled" for row in protection)
        and len(convergence) == 1
        and convergence[0].status == "completed"
        and events == 1
    )


def _terminalize_target(session_factory, session, target, observed_at) -> None:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    leg.status = "cancelled"
    leg.terminal_reason = "operator_cancelled_unfilled_entry_leg"
    leg.last_verified_at = observed_at
    leg.updated_at = observed_at
    intent = (
        session.query(TriggerProtectionIntent)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .one()
    )
    intent.recovery_state = "resolved"
    intent.recovery_disposition = "terminal"
    intent.last_reason_code = "parent_trigger_cancelled_before_entry"
    intent.next_attempt_at = None
    intent.updated_at = observed_at
    for row in (
        session.query(PositionProtectionLeg)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    ):
        row.status = "cancelled"
        row.updated_at = observed_at
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .one()
    )
    convergence.status = "completed"
    convergence.reason_code = "parent_trigger_cancelled_before_entry"
    convergence.completed_at = observed_at
    convergence.updated_at = observed_at
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="reconcile_manual_pending_entry_cancel",
            status="confirmed",
            execution_binding_id=target.execution_binding_id,
            venue="deepcoin",
            symbol=target.instrument_id.split("-")[0],
            side="long",
            order_id=target.order_id,
            reason="operator_confirmed_all_entry_orders_cancelled",
            after={"pending": False, "terminalized": True},
            created_at=observed_at,
        ),
        session=session,
    )


def _terminalize_binding(session, binding_id: int, observed_at: datetime) -> None:
    binding = session.get(ExecutionBinding, binding_id)
    entry_legs = (
        session.query(ExecutionOrderLeg)
        .filter_by(execution_binding_id=binding_id, purpose="entry")
        .all()
    )
    if not entry_legs or any(
        str(row.status or "").lower()
        not in {"cancelled", "canceled", "expired", "rejected"}
        for row in entry_legs
    ):
        return
    binding.status = "cancelled"
    binding.last_exchange_status = "operator_cancelled_pending_entries"
    binding.updated_at = observed_at
    lifecycle = (
        session.query(StrategyLifecycle)
        .filter_by(execution_binding_id=binding_id)
        .one()
    )
    lifecycle.lifecycle_status = "expired"
    lifecycle.exit_reason = "expired"
    lifecycle.exited_at = observed_at
    lifecycle.management_action = "operator_cancelled_pending_entries"
    lifecycle.management_note = "All unfilled entry orders were cancelled at Deepcoin."
    lifecycle.expiry_review_next_at = None
    lifecycle.updated_at = observed_at


def _create_verified_backup(database_path: Path, backup_path: Path) -> None:
    if backup_path.exists() or not backup_path.parent.is_dir():
        raise ValueError("backup_path_invalid")
    source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ValueError("backup_quick_check_failed")
    finally:
        destination.close()
        source.close()


def _valid_idle_authority(value_json: str) -> bool:
    try:
        value = json.loads(value_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(value, dict)
        and set(value) == {"generation", "released_at", "schema_version", "state"}
        and value.get("schema_version") == 2
        and value.get("state") == "idle"
        and isinstance(value.get("generation"), int)
        and not isinstance(value.get("generation"), bool)
        and value["generation"] >= 0
        and isinstance(value.get("released_at"), str)
    )


def _blocked(reason, order_ids, evidence_sha256=""):
    return ManualPendingEntryReconciliationPlan(
        status="blocked",
        reason_code=reason,
        target_order_ids=tuple(order_ids),
        evidence_sha256=evidence_sha256,
        fingerprint="",
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
