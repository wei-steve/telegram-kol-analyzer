"""Durable persistence primitives for exact-strategy management batches."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import inspect, update
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.db import MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME
from telegram_kol_research.db import REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES
from telegram_kol_research.models import ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE
from telegram_kol_research.models import StrategyManagementBatch
from telegram_kol_research.models import StrategyManagementLeg
from telegram_kol_research.models import PositionProtectionLedger


RECOVERABLE_BATCH_STATUSES = frozenset(
    {"executing", "reserved", "submitted", "submit_unknown", "reconciling"}
)
TEMPORARY_VISIBILITY_REASONS = frozenset(
    {
        "protection_missing_cancellable_order_id",
        "target_protection_snapshot_incomplete",
    }
)
UNSET = object()


def resolve_proven_restored_protection_failure_for_market_successor_in_session(
    session: Session,
    *,
    strategy_instance_id: str,
    target_lifecycle_id: int,
    execution_binding_id: int,
    live_pos_ids: set[str],
    pending_tpsl_rows: Sequence[dict[str, Any]],
    pending_tpsl_snapshot_complete: bool,
    resolved_at: datetime,
) -> str:
    """Resolve one restored predecessor only from current exchange and ledger proof."""

    _require_management_unique_indexes(session)
    active = (
        session.query(StrategyManagementBatch)
        .filter(
            StrategyManagementBatch.strategy_instance_id
            == str(strategy_instance_id)
        )
        .filter(
            StrategyManagementBatch.status.not_in(
                ("succeeded", "blocked", "resolved")
            )
        )
        .order_by(StrategyManagementBatch.id.asc())
        .all()
    )
    if not active:
        return "clear"
    if not pending_tpsl_snapshot_complete:
        return "blocked"
    if len(active) != 1:
        return "blocked"
    batch = active[0]
    if (
        batch.status != "partial_failed"
        or batch.reason_code
        != "protection_replacement_failed_and_restored"
        or batch.target_lifecycle_id != int(target_lifecycle_id)
        or batch.execution_binding_id != int(execution_binding_id)
        or batch.intent != "move_stop_to_break_even"
        or batch.effective_action
        not in {"move_stop_to_break_even", "break_even_by_market"}
    ):
        return "blocked"
    legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(
            StrategyManagementLeg.leg_index.asc(),
            StrategyManagementLeg.id.asc(),
        )
        .all()
    )
    if not legs or {str(leg.pos_id) for leg in legs} != {
        str(pos_id) for pos_id in live_pos_ids
    }:
        return "blocked"
    pending_by_order_id = {
        order_id: row
        for row in pending_tpsl_rows
        if isinstance(row, dict)
        and (order_id := _pending_order_id(row)) is not None
    }
    for leg in legs:
        if leg.status != "restored" or not _has_restored_protection_evidence(
            leg
        ):
            return "blocked"
        try:
            old_tpsl = json.loads(leg.old_tpsl_json or "")
            response = json.loads(leg.response_json or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return "blocked"
        old_order_ids = {
            str(order_id)
            for order_id in old_tpsl.get("order_ids") or []
            if str(order_id)
        }
        restore_rows = response.get("restore_rows")
        if not old_order_ids or not isinstance(restore_rows, list):
            return "blocked"
        restored_order_ids = {
            order_id
            for restore_row in restore_rows
            if (order_id := _response_order_id(restore_row)) is not None
        }
        if (
            len(restored_order_ids) != len(old_order_ids)
            or any(
                order_id not in pending_by_order_id
                for order_id in restored_order_ids
            )
            or any(order_id in pending_by_order_id for order_id in old_order_ids)
        ):
            return "blocked"
        ledger_rows = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id
                == int(execution_binding_id),
                PositionProtectionLedger.execution_order_leg_id
                == int(leg.execution_order_leg_id),
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
            .all()
        )
        ledger_by_order_id = {
            str(row.order_id): row for row in ledger_rows if row.order_id
        }
        if any(
            order_id not in ledger_by_order_id
            or str(ledger_by_order_id[order_id].status).lower()
            != "cancelled"
            for order_id in old_order_ids
        ) or any(
            order_id not in ledger_by_order_id
            or str(ledger_by_order_id[order_id].status).lower()
            != "verified"
            or ledger_by_order_id[order_id].strategy_instance_id
            != str(strategy_instance_id)
            or ledger_by_order_id[order_id].evidence_source
            != "management_tpsl_restore"
            or not _ledger_matches_pending_tpsl(
                ledger_by_order_id[order_id],
                pending_by_order_id[order_id],
                expected_pos_id=str(leg.pos_id),
            )
            for order_id in restored_order_ids
        ):
            return "blocked"
    batch.status = "resolved"
    batch.reason_code = (
        "superseded_by_break_even_market_after_protection_restored"
    )
    batch.completed_at = resolved_at
    batch.updated_at = resolved_at
    session.flush()
    return "resolved"


def _response_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("ordId", "orderId", "id"):
        if response.get(key) not in (None, ""):
            return str(response[key])
    data = response.get("data")
    if isinstance(data, dict):
        return _response_order_id(data)
    if isinstance(data, list) and len(data) == 1:
        return _response_order_id(data[0])
    return None


def _pending_order_id(row: dict[str, Any]) -> str | None:
    for key in ("ordId", "orderId", "order_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _ledger_matches_pending_tpsl(
    ledger: PositionProtectionLedger,
    row: dict[str, Any],
    *,
    expected_pos_id: str,
) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    row_pos_id = str(
        row.get("posId") or row.get("pos_id") or row.get("positionId") or ""
    ).strip()
    if row_pos_id and row_pos_id != expected_pos_id:
        return False
    if str(row.get("instId") or "").upper() != str(
        ledger.instrument_id or ""
    ).upper():
        return False
    side = str(row.get("posSide") or row.get("side") or "").lower()
    side = {"buy": "long", "sell": "short"}.get(side, side)
    if side != str(ledger.side or "").lower():
        return False
    ledger_price = _decimal_or_none(ledger.trigger_price)
    current_price = _pending_trigger_price(row, str(ledger.purpose or ""))
    if ledger_price is None or current_price != ledger_price:
        return False
    ledger_size = _decimal_or_none(ledger.size_text)
    row_size = _decimal_or_none(row.get("sz") or row.get("size"))
    return (
        ledger_size is not None
        and row_size is not None
        and row_size == ledger_size
    )


def _pending_trigger_price(
    row: dict[str, Any], purpose: str
) -> Decimal | None:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose in {"stop_loss", "sl", "loss"}
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = _decimal_or_none(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def resolve_restored_protection_failure_for_full_exit_in_session(
    session: Session,
    *,
    strategy_instance_id: str,
    target_lifecycle_id: int,
    execution_binding_id: int,
    resolved_at: datetime,
) -> str:
    """Release only a fully restored protection failure for a later full exit.

    The active-strategy index intentionally retains ``partial_failed`` batches.
    A protection-only predecessor is the one exception: it may be terminalized
    only when every exact position leg confirms that its original protection
    was restored.  Any other active predecessor remains a hard stop.
    """

    _require_management_unique_indexes(session)
    active = (
        session.query(StrategyManagementBatch)
        .filter(StrategyManagementBatch.strategy_instance_id == strategy_instance_id)
        .filter(StrategyManagementBatch.status.not_in(("succeeded", "blocked", "resolved")))
        .order_by(StrategyManagementBatch.id.asc())
        .all()
    )
    if not active:
        return "clear"
    if len(active) != 1:
        return "blocked"
    batch = active[0]
    supported_protection_pairs = {
        ("adjust_stop_loss", "adjust_stop_loss"),
        ("move_stop_to_break_even", "move_stop_to_break_even"),
        ("move_stop_to_break_even", "break_even_by_market"),
    }
    if (
        batch.status != "partial_failed"
        or batch.reason_code != "protection_replacement_failed_and_restored"
        or batch.target_lifecycle_id != target_lifecycle_id
        or batch.execution_binding_id != execution_binding_id
        or (batch.intent, batch.effective_action) not in supported_protection_pairs
    ):
        return "blocked"
    legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
        .all()
    )
    if not legs or any(
        leg.status != "restored" or not _has_restored_protection_evidence(leg)
        for leg in legs
    ):
        return "blocked"
    batch.status = "resolved"
    batch.reason_code = "superseded_by_full_exit_after_protection_restored"
    batch.completed_at = resolved_at
    batch.updated_at = resolved_at
    session.flush()
    return "resolved"


def _has_restored_protection_evidence(leg: StrategyManagementLeg) -> bool:
    try:
        old_tpsl = json.loads(leg.old_tpsl_json or "")
        planned_tpsl = json.loads(leg.planned_tpsl_json or "")
        error = json.loads(leg.last_error or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(old_tpsl, dict)
        and bool(old_tpsl)
        and isinstance(planned_tpsl, dict)
        and bool(planned_tpsl)
        and isinstance(error, dict)
        and error.get("stage") == "replace_protection"
        and error.get("restore_error") is None
    )


def race_resolved_successor_fingerprint(
    *,
    parent_batch_id: int,
    parent_target_fingerprint: str,
    resolved_position_ids: Sequence[str],
) -> str:
    """Return the idempotency key for one evidence-resolved close successor.

    A cancellation race may create a position after the parent batch froze its
    target.  The successor must never overwrite that immutable parent, yet a
    worker retry must resolve to the same successor.
    """

    canonical = {
        "kind": "cancel_entry_race_successor_v1",
        "parent_batch_id": int(parent_batch_id),
        "parent_target_fingerprint": str(parent_target_fingerprint),
        "resolved_position_ids": sorted({str(value) for value in resolved_position_ids}),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_race_resolved_successor_batch(
    session_factory: sessionmaker,
    *,
    parent_batch_id: int,
    resolved_position_ids: Sequence[str],
    target_snapshot: dict[str, Any],
    legs: Sequence[ManagementLegCreate],
    planned_at: datetime | None = None,
) -> ManagementBatchRecord:
    """Atomically terminalize one proven race parent and create its successor."""

    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        _require_management_unique_indexes(session)
        parent = session.get(StrategyManagementBatch, int(parent_batch_id))
        if (
            parent is None
            or parent.status != "recovery_required"
            or parent.reason_code != "deferred_entry_cancel_race_detected"
        ):
            raise ManagementSchemaSafetyError("race_successor_parent_not_ready")
        parent_snapshot = json.loads(parent.target_snapshot_json or "{}")
        successor_snapshot = dict(target_snapshot)
        successor_snapshot["race_resolved_successor_of"] = parent.id
        successor_fingerprint = race_resolved_successor_fingerprint(
            parent_batch_id=parent.id,
            parent_target_fingerprint=parent.target_fingerprint,
            resolved_position_ids=resolved_position_ids,
        )
        parent.status = "resolved"
        parent.reason_code = "deferred_entry_cancel_race_resolved"
        parent.completed_at = now
        parent.updated_at = now
        target_fingerprint = hashlib.sha256(
            json.dumps(successor_snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        successor_id = create_management_batch_in_session(
            session,
            idempotency_fingerprint=successor_fingerprint,
            raw_message_id=parent.raw_message_id,
            recognition_decision_id=parent.recognition_decision_id,
            recognition_generation=parent.recognition_generation,
            target_lifecycle_id=parent.target_lifecycle_id,
            strategy_instance_id=parent.strategy_instance_id,
            execution_binding_id=parent.execution_binding_id,
            intent="full_exit",
            effective_action="full_exit",
            execution_mode=parent.execution_mode,
            requested_fraction=None,
            effective_fraction=1.0,
            partial_round_before=parent.partial_round_before,
            target_fingerprint=target_fingerprint,
            target_snapshot=successor_snapshot,
            legs=legs,
            planned_at=now,
            status="ready",
        )
        session.commit()
    return load_management_batch(session_factory, successor_id)


class ManagementSchemaSafetyError(RuntimeError):
    """Raised when database uniqueness cannot safely serialize mutations."""


@dataclass(frozen=True, slots=True)
class ManagementLegCreate:
    execution_order_leg_id: int
    pos_id: str
    leg_index: int
    status: str = "planned"
    preflight_size: str | None = None
    planned_close_size: str | None = None
    avg_entry_price: str | None = None
    quantity_step: str | None = None
    old_tpsl: Any = None
    planned_tpsl: Any = None
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    request: Any = None
    response: Any = None
    last_error: Any = None
    last_exchange_snapshot: Any = None


@dataclass(frozen=True, slots=True)
class ManagementLegRecord:
    id: int
    management_batch_id: int
    execution_order_leg_id: int
    pos_id: str
    leg_index: int
    status: str
    preflight_size: str | None
    planned_close_size: str | None
    avg_entry_price: str | None
    quantity_step: str | None
    old_tpsl: Any
    planned_tpsl: Any
    client_order_id: str | None
    exchange_order_id: str | None
    request: Any
    response: Any
    last_error: Any
    last_exchange_snapshot: Any
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementBatchRecord:
    id: int
    idempotency_fingerprint: str
    raw_message_id: int
    recognition_decision_id: int
    recognition_generation: str
    target_lifecycle_id: int
    strategy_instance_id: str
    execution_binding_id: int
    intent: str
    effective_action: str
    execution_mode: str
    requested_fraction: float | None
    effective_fraction: float | None
    partial_round_before: int
    status: str
    reason_code: str | None
    target_fingerprint: str
    target_snapshot: Any
    planned_at: datetime
    started_at: datetime | None
    reconciled_at: datetime | None
    completed_at: datetime | None
    notification_state: str | None
    notification_fingerprint: str | None
    visibility_first_failed_at: datetime | None
    visibility_retry_attempts: int
    visibility_next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime
    legs: tuple[ManagementLegRecord, ...]


def create_management_batch(
    session_factory: sessionmaker,
    *,
    idempotency_fingerprint: str,
    raw_message_id: int,
    recognition_decision_id: int,
    recognition_generation: str,
    target_lifecycle_id: int,
    strategy_instance_id: str,
    execution_binding_id: int,
    intent: str,
    effective_action: str,
    execution_mode: str = "disabled",
    requested_fraction: float | None,
    effective_fraction: float | None,
    partial_round_before: int,
    target_fingerprint: str,
    target_snapshot: Any,
    legs: Sequence[ManagementLegCreate],
    planned_at: datetime | None = None,
    status: str = "ready",
    reason_code: str | None = None,
    notification_state: str | None = "pending",
    visibility_first_failed_at: datetime | None = None,
    visibility_retry_attempts: int = 0,
    visibility_next_attempt_at: datetime | None = None,
) -> ManagementBatchRecord:
    """Atomically persist one immutable batch target and all of its legs."""

    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        batch_id = create_management_batch_in_session(
            session,
            idempotency_fingerprint=idempotency_fingerprint,
            raw_message_id=raw_message_id,
            recognition_decision_id=recognition_decision_id,
            recognition_generation=recognition_generation,
            target_lifecycle_id=target_lifecycle_id,
            strategy_instance_id=strategy_instance_id,
            execution_binding_id=execution_binding_id,
            intent=intent,
            effective_action=effective_action,
            execution_mode=execution_mode,
            requested_fraction=requested_fraction,
            effective_fraction=effective_fraction,
            partial_round_before=partial_round_before,
            target_fingerprint=target_fingerprint,
            target_snapshot=target_snapshot,
            legs=legs,
            planned_at=now,
            status=status,
            reason_code=reason_code,
            notification_state=notification_state,
            visibility_first_failed_at=visibility_first_failed_at,
            visibility_retry_attempts=visibility_retry_attempts,
            visibility_next_attempt_at=visibility_next_attempt_at,
        )
        session.commit()
    return load_management_batch(session_factory, batch_id)


def create_management_batch_in_session(
    session: Session,
    *,
    idempotency_fingerprint: str,
    raw_message_id: int,
    recognition_decision_id: int,
    recognition_generation: str,
    target_lifecycle_id: int,
    strategy_instance_id: str,
    execution_binding_id: int,
    intent: str,
    effective_action: str,
    execution_mode: str = "disabled",
    requested_fraction: float | None,
    effective_fraction: float | None,
    partial_round_before: int,
    target_fingerprint: str,
    target_snapshot: Any,
    legs: Sequence[ManagementLegCreate],
    planned_at: datetime,
    status: str = "ready",
    reason_code: str | None = None,
    notification_state: str | None = "pending",
    visibility_first_failed_at: datetime | None = None,
    visibility_retry_attempts: int = 0,
    visibility_next_attempt_at: datetime | None = None,
    validate_current_state: Callable[[Session], None] | None = None,
) -> int:
    """Insert a batch in the caller transaction after an immediate state gate."""

    _require_management_unique_indexes(session)
    if validate_current_state is not None:
        validate_current_state(session)
    batch = StrategyManagementBatch(
        idempotency_fingerprint=idempotency_fingerprint,
        raw_message_id=raw_message_id,
        recognition_decision_id=recognition_decision_id,
        recognition_generation=recognition_generation,
        target_lifecycle_id=target_lifecycle_id,
        strategy_instance_id=strategy_instance_id,
        execution_binding_id=execution_binding_id,
        intent=intent,
        effective_action=effective_action,
        execution_mode=execution_mode,
        requested_fraction=requested_fraction,
        effective_fraction=effective_fraction,
        partial_round_before=partial_round_before,
        status=status,
        reason_code=reason_code,
        target_fingerprint=target_fingerprint,
        target_snapshot_json=_encode_json(target_snapshot) or "{}",
        planned_at=planned_at,
        notification_state=notification_state,
        visibility_first_failed_at=visibility_first_failed_at,
        visibility_retry_attempts=visibility_retry_attempts,
        visibility_next_attempt_at=visibility_next_attempt_at,
        completed_at=(
            planned_at if status in {"succeeded", "blocked", "resolved"} else None
        ),
        created_at=planned_at,
        updated_at=planned_at,
    )
    session.add(batch)
    session.flush()
    for leg in legs:
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                leg_index=leg.leg_index,
                status=leg.status,
                preflight_size=leg.preflight_size,
                planned_close_size=leg.planned_close_size,
                avg_entry_price=leg.avg_entry_price,
                quantity_step=leg.quantity_step,
                old_tpsl_json=_encode_json(leg.old_tpsl),
                planned_tpsl_json=_encode_json(leg.planned_tpsl),
                client_order_id=leg.client_order_id,
                exchange_order_id=leg.exchange_order_id,
                request_json=_encode_json(leg.request),
                response_json=_encode_json(leg.response),
                last_error=_encode_json(leg.last_error),
                last_exchange_snapshot_json=_encode_json(
                    leg.last_exchange_snapshot
                ),
                created_at=planned_at,
                updated_at=planned_at,
            )
        )
    session.flush()
    if batch.status in {
        "blocked",
        "partial_failed",
        "submit_unknown",
        "recovery_required",
    }:
        from telegram_kol_research.system_operator_bot import (
            persist_strategy_management_notification_in_session,
        )

        persist_strategy_management_notification_in_session(session, batch)
    return batch.id


def load_management_batch(
    session_factory: sessionmaker, batch_id: int
) -> ManagementBatchRecord:
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        if batch is None:
            raise LookupError("management batch not found")
        return _batch_to_record(session, batch)


def claim_ready_batch(
    session_factory: sessionmaker,
    batch_id: int,
    *,
    claimed_at: datetime | None = None,
) -> ManagementBatchRecord | None:
    """Atomically claim exactly one ready batch for execution."""

    now = claimed_at or datetime.now(UTC)
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == batch_id,
                StrategyManagementBatch.status == "ready",
            )
            .values(status="executing", started_at=now, updated_at=now)
        )
        session.commit()
        if result.rowcount != 1:
            return None
    return load_management_batch(session_factory, batch_id)


def claim_worker_batch(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    expected_status: str,
    claimed_at: datetime | None = None,
) -> bool:
    """CAS one newly executable batch so only the database winner may submit."""

    if expected_status not in {"ready", "protection_ready"}:
        return False
    now = claimed_at or datetime.now(UTC)
    values: dict[str, Any] = {
        "status": "executing",
        "started_at": now,
        "updated_at": now,
    }
    if expected_status == "protection_ready":
        values["reason_code"] = "protection_phase_executing"
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == int(batch_id),
                StrategyManagementBatch.status == expected_status,
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def transition_batch(
    session_factory: sessionmaker,
    batch_id: int,
    *,
    expected_statuses: Iterable[str],
    new_status: str,
    transitioned_at: datetime | None = None,
    reason_code: Any = UNSET,
) -> bool:
    now = transitioned_at or datetime.now(UTC)
    expected = tuple(expected_statuses)
    if not expected:
        return False
    values: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
    }
    if reason_code is not UNSET:
        values["reason_code"] = reason_code
    if new_status == "executing":
        values["started_at"] = now
    if new_status == "succeeded":
        values["reconciled_at"] = now
    if new_status in {"succeeded", "blocked", "resolved"}:
        values["completed_at"] = now
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == batch_id,
                StrategyManagementBatch.status.in_(expected),
            )
            .values(**values)
        )
        if result.rowcount == 1 and new_status in {
            "blocked", "partial_failed", "submit_unknown", "recovery_required"
        }:
            from telegram_kol_research.system_operator_bot import (
                persist_strategy_management_notification_in_session,
            )

            batch = session.get(StrategyManagementBatch, batch_id)
            persist_strategy_management_notification_in_session(session, batch)
        session.commit()
        return result.rowcount == 1


def transition_leg(
    session_factory: sessionmaker,
    leg_id: int,
    *,
    expected_statuses: Iterable[str],
    new_status: str,
    transitioned_at: datetime | None = None,
    client_order_id: Any = UNSET,
    exchange_order_id: Any = UNSET,
    request: Any = UNSET,
    response: Any = UNSET,
    last_error: Any = UNSET,
    last_exchange_snapshot: Any = UNSET,
) -> bool:
    now = transitioned_at or datetime.now(UTC)
    expected = tuple(expected_statuses)
    if not expected:
        return False
    values: dict[str, Any] = {"status": new_status, "updated_at": now}
    optional_values = {
        "client_order_id": client_order_id,
        "exchange_order_id": exchange_order_id,
        "request_json": request,
        "response_json": response,
        "last_error": last_error,
        "last_exchange_snapshot_json": last_exchange_snapshot,
    }
    for key, value in optional_values.items():
        if value is UNSET:
            continue
        values[key] = (
            value
            if key in {"client_order_id", "exchange_order_id"}
            else _encode_json(value)
        )
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementLeg)
            .where(
                StrategyManagementLeg.id == leg_id,
                StrategyManagementLeg.status.in_(expected),
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def list_recoverable_batches(
    session_factory: sessionmaker, *, limit: int = 50
) -> list[ManagementBatchRecord]:
    if limit <= 0:
        return []
    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status.in_(RECOVERABLE_BATCH_STATUSES))
            .order_by(
                StrategyManagementBatch.planned_at.asc(),
                StrategyManagementBatch.id.asc(),
            )
            .limit(limit)
            .all()
        )
        return [_batch_to_record(session, batch) for batch in batches]


def list_worker_batches(
    session_factory: sessionmaker,
    *,
    limit: int = 10,
    prefer_recovery: bool = False,
) -> list[ManagementBatchRecord]:
    """Return bounded automatic work; operator-paused states are never eligible."""

    if limit <= 0:
        return []
    with session_factory() as session:
        def load_lane(statuses):
            return (
                session.query(StrategyManagementBatch)
                .filter(StrategyManagementBatch.status.in_(statuses))
                .order_by(
                    StrategyManagementBatch.planned_at.asc(),
                    StrategyManagementBatch.id.asc(),
                )
                .limit(limit)
                .all()
            )

        executable = load_lane({"ready", "protection_ready"})
        recovery = load_lane(RECOVERABLE_BATCH_STATUSES)
        recovery += (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status == "recovery_required")
            .filter(
                StrategyManagementBatch.reason_code
                == "deferred_entry_cancel_race_detected"
            )
            .order_by(
                StrategyManagementBatch.planned_at.asc(),
                StrategyManagementBatch.id.asc(),
            )
            .limit(limit)
            .all()
        )
        temporary_visibility = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status == "blocked")
            .filter(StrategyManagementBatch.reason_code.in_(TEMPORARY_VISIBILITY_REASONS))
            .filter(StrategyManagementBatch.visibility_next_attempt_at <= datetime.now(UTC))
            .order_by(StrategyManagementBatch.planned_at.asc(), StrategyManagementBatch.id.asc())
            .limit(limit)
            .all()
        )
        recovery = recovery + temporary_visibility
        lanes = (
            (recovery, executable) if prefer_recovery else (executable, recovery)
        )
        batches = []
        # Independent lanes prevent an old reconciliation backlog from hiding
        # fresh exact-strategy work behind one global SQL LIMIT.
        while len(batches) < limit and any(lanes):
            for lane in lanes:
                if lane:
                    batches.append(lane.pop(0))
                    if len(batches) >= limit:
                        break
        return [_batch_to_record(session, batch) for batch in batches]


def _batch_to_record(session, batch: StrategyManagementBatch) -> ManagementBatchRecord:
    legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
        .all()
    )
    return ManagementBatchRecord(
        id=batch.id,
        idempotency_fingerprint=batch.idempotency_fingerprint,
        raw_message_id=batch.raw_message_id,
        recognition_decision_id=batch.recognition_decision_id,
        recognition_generation=batch.recognition_generation,
        target_lifecycle_id=batch.target_lifecycle_id,
        strategy_instance_id=batch.strategy_instance_id,
        execution_binding_id=batch.execution_binding_id,
        intent=batch.intent,
        effective_action=batch.effective_action,
        execution_mode=batch.execution_mode,
        requested_fraction=batch.requested_fraction,
        effective_fraction=batch.effective_fraction,
        partial_round_before=batch.partial_round_before,
        status=batch.status,
        reason_code=batch.reason_code,
        target_fingerprint=batch.target_fingerprint,
        target_snapshot=_decode_json(batch.target_snapshot_json),
        planned_at=_utc(batch.planned_at),
        started_at=_utc(batch.started_at),
        reconciled_at=_utc(batch.reconciled_at),
        completed_at=_utc(batch.completed_at),
        notification_state=batch.notification_state,
        notification_fingerprint=batch.notification_fingerprint,
        visibility_first_failed_at=_utc(batch.visibility_first_failed_at),
        visibility_retry_attempts=int(batch.visibility_retry_attempts or 0),
        visibility_next_attempt_at=_utc(batch.visibility_next_attempt_at),
        created_at=_utc(batch.created_at),
        updated_at=_utc(batch.updated_at),
        legs=tuple(_leg_to_record(leg) for leg in legs),
    )


def _leg_to_record(leg: StrategyManagementLeg) -> ManagementLegRecord:
    return ManagementLegRecord(
        id=leg.id,
        management_batch_id=leg.management_batch_id,
        execution_order_leg_id=leg.execution_order_leg_id,
        pos_id=leg.pos_id,
        leg_index=leg.leg_index,
        status=leg.status,
        preflight_size=leg.preflight_size,
        planned_close_size=leg.planned_close_size,
        avg_entry_price=leg.avg_entry_price,
        quantity_step=leg.quantity_step,
        old_tpsl=_decode_json(leg.old_tpsl_json),
        planned_tpsl=_decode_json(leg.planned_tpsl_json),
        client_order_id=leg.client_order_id,
        exchange_order_id=leg.exchange_order_id,
        request=_decode_json(leg.request_json),
        response=_decode_json(leg.response_json),
        last_error=_decode_json(leg.last_error),
        last_exchange_snapshot=_decode_json(leg.last_exchange_snapshot_json),
        created_at=_utc(leg.created_at),
        updated_at=_utc(leg.updated_at),
    )


def _encode_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_management_unique_indexes(session) -> None:
    expected = {
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME: (
            "strategy_management_batches",
            ["idempotency_fingerprint"],
        ),
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME: (
            "strategy_management_batches",
            ["strategy_instance_id"],
        ),
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME: (
            "strategy_management_legs",
            ["management_batch_id", "pos_id"],
        ),
        MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME: (
            "strategy_management_market_decisions",
            ["management_batch_id"],
        ),
    }
    inspector = inspect(session.connection())
    observed = {
        index["name"]: index
        for table_name in {table for table, _columns in expected.values()}
        for index in inspector.get_indexes(table_name)
    }
    unsafe = []
    for index_name in sorted(REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES):
        _expected_table, expected_columns = expected[index_name]
        index = observed.get(index_name)
        if (
            index is None
            or not index.get("unique")
            or index.get("column_names") != expected_columns
        ):
            unsafe.append(index_name)
            continue
        if index_name == MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME:
            where = str(index.get("dialect_options", {}).get("sqlite_where", ""))
            if (
                session.get_bind().dialect.name == "sqlite"
                and _normalize_sql_predicate(where)
                != _normalize_sql_predicate(ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE)
            ):
                unsafe.append(index_name)
    if unsafe:
        session.rollback()
        raise ManagementSchemaSafetyError(
            "management database safety indexes are missing or invalid: "
            + ", ".join(unsafe)
        )


def _normalize_sql_predicate(predicate: str) -> tuple[str, tuple[str, ...]]:
    literals: list[str] = []

    def replace_literal(match: re.Match[str]) -> str:
        literals.append(match.group(1).replace("''", "'"))
        return "?"

    structure = re.sub(r"'((?:''|[^'])*)'", replace_literal, predicate)
    structure = re.sub(r'[\s"`\[\]]+', "", structure).lower()
    return structure, tuple(literals)
