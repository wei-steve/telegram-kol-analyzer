"""Reconcile close batches from one coherent, read-only exchange snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES


_ACTIVE_RECONCILIATION_STATUSES = frozenset(
    {
        "executing",
        "reserved",
        "submitted",
        "submit_unknown",
        "reconciling",
        "partial_failed",
    }
)
_CLOSE_ACTIONS = frozenset(
    {"partial_close", "full_close", "full_exit", "partial_then_break_even"}
)
_ORDER_ID_KEYS = ("ordId", "orderId", "order_id", "id")
_CLIENT_ORDER_ID_KEYS = ("clOrdId", "clientOrderId", "client_order_id")
_PROTECTION_PHASE_LEG_STATES = frozenset(
    {"succeeded", "restored", "recovery_required"}
)
_MANAGEABLE_ENTRY_LEG_STATES = frozenset(
    {"active", "open", "filled", "partial_closed"}
)
_DEFERRED_ENTRY_LEG_STATES = frozenset({"open", "pending", "submitted"})


@dataclass(frozen=True, slots=True)
class ManagementReconciliationResult:
    checked: int = 0
    succeeded: int = 0
    pending: int = 0
    frozen: int = 0


@serialized_position_authority_mutation
def reconcile_strategy_management_batches(
    session_factory: sessionmaker,
    *,
    snapshot: Any,
    reconciled_at: datetime | None = None,
    batch_ids: set[int] | tuple[int, ...] | None = None,
) -> ManagementReconciliationResult:
    """Apply exchange truth without submitting or retrying any order."""

    now = reconciled_at or datetime.now(UTC)
    if getattr(snapshot, "errors", {}).get("positions"):
        return ManagementReconciliationResult()

    position_rows = _positions_by_id(getattr(snapshot, "positions", []))
    order_rows = _regular_order_rows(snapshot)
    counts = {"checked": 0, "succeeded": 0, "pending": 0, "frozen": 0}

    with session_factory() as session:
        query = session.query(StrategyManagementBatch).filter(
            StrategyManagementBatch.status.in_(_ACTIVE_RECONCILIATION_STATUSES)
        )
        if batch_ids is not None:
            ids = tuple(int(batch_id) for batch_id in batch_ids)
            if not ids:
                return ManagementReconciliationResult()
            query = query.filter(StrategyManagementBatch.id.in_(ids))
        batches = (
            query
            .order_by(StrategyManagementBatch.planned_at.asc(), StrategyManagementBatch.id.asc())
            .all()
        )
        for batch in batches:
            if batch.effective_action not in _CLOSE_ACTIONS:
                continue
            legs = (
                session.query(StrategyManagementLeg)
                .filter(StrategyManagementLeg.management_batch_id == batch.id)
                .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
                .all()
            )
            if _composite_protection_phase_started(batch, legs):
                continue
            counts["checked"] += 1
            if not legs or not _identity_is_exact(session, batch, legs):
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason="management_reconciliation_identity_mismatch",
                    now=now,
                )
                counts["frozen"] += 1
                continue

            binding = session.get(ExecutionBinding, batch.execution_binding_id)
            expected_instrument = normalize_deepcoin_swap_instrument(binding.symbol)

            for leg in legs:
                _reconcile_leg(
                    leg,
                    position_rows=position_rows,
                    order_rows=order_rows,
                    snapshot=snapshot,
                    expected_instrument=expected_instrument,
                    now=now,
                )

            statuses = [str(leg.status or "") for leg in legs]
            if all(status == "confirmed" for status in statuses):
                batch.reconciled_at = now
                batch.updated_at = now
                if batch.effective_action == "partial_then_break_even":
                    batch.status = "protection_ready"
                    batch.reason_code = "management_close_confirmed_protection_ready"
                    batch.completed_at = None
                    counts["pending"] += 1
                else:
                    batch.status = "succeeded"
                    batch.reason_code = "management_close_exchange_confirmed"
                    batch.completed_at = now
                    counts["succeeded"] += 1
                if batch.effective_action in {"full_close", "full_exit"}:
                    _terminalize_full_close(session, batch=batch, legs=legs, now=now)
                else:
                    _confirm_partial_close(session, batch=batch, now=now)
            elif "failed" in statuses:
                _freeze_batch(
                    session,
                    batch,
                    status="partial_failed",
                    reason="one_or_more_close_legs_failed",
                    now=now,
                )
                counts["frozen"] += 1
            elif any(status in {"partial", "inconsistent", "submit_unknown"} for status in statuses):
                reason = (
                    "management_close_submission_unresolved"
                    if "submit_unknown" in statuses
                    else "management_close_result_requires_recovery"
                )
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason=reason,
                    now=now,
                )
                counts["frozen"] += 1
            elif "confirmed" in statuses:
                if batch.effective_action in {"full_close", "full_exit"}:
                    # Exact full-exit legs may settle on different snapshots;
                    # waiting is safe because no request is ever resubmitted.
                    batch.status = "reconciling"
                    batch.reason_code = "management_close_legs_partially_confirmed"
                    batch.updated_at = now
                    counts["pending"] += 1
                else:
                    _freeze_batch(
                        session,
                        batch,
                        status="recovery_required",
                        reason="management_close_legs_partially_confirmed",
                        now=now,
                    )
                    counts["frozen"] += 1
            else:
                batch.status = "reconciling"
                batch.reason_code = "management_close_pending_exchange_confirmation"
                batch.updated_at = now
                counts["pending"] += 1
        session.commit()

    return ManagementReconciliationResult(**counts)


def _composite_protection_phase_started(batch, legs) -> bool:
    """Keep close reconciliation permanently out after phase hand-off."""

    if batch.effective_action != "partial_then_break_even":
        return False
    reason = str(batch.reason_code or "")
    return bool(
        batch.status == "protection_ready"
        or reason.startswith("protection_")
        or reason.startswith("all_position_protection_")
        or any(str(leg.status or "") in _PROTECTION_PHASE_LEG_STATES for leg in legs)
    )


def _reconcile_leg(
    leg: StrategyManagementLeg,
    *,
    position_rows: dict[str, list[dict[str, Any]]],
    order_rows: list[dict[str, Any]],
    snapshot: Any,
    expected_instrument: str,
    now: datetime,
) -> None:
    if leg.status == "failed":
        return

    matching_orders = _matching_orders(leg, order_rows)
    matching_order, ambiguous = _resolve_matching_order(matching_orders)
    identity_conflict = _order_identity_conflicts(leg, matching_orders)
    if ambiguous or identity_conflict:
        leg.status = "inconsistent"
        leg.last_error = _json(
            {
                "reason": (
                    "management_close_order_identity_conflict"
                    if identity_conflict
                    else "management_close_order_identity_ambiguous"
                )
            }
        )
        leg.last_exchange_snapshot_json = _leg_snapshot(
            leg, position_rows, matching_orders
        )
        leg.updated_at = now
        return

    if leg.status == "inconsistent":
        return

    if leg.status in {"reserved", "submit_unknown"}:
        if matching_order is None:
            leg.status = "submit_unknown"
            leg.last_error = _json({"reason": "management_close_order_not_found"})
            leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
            leg.updated_at = now
            return
        order = matching_order
        order_id = _first_string(order, *_ORDER_ID_KEYS)
        if leg.exchange_order_id and order_id and str(leg.exchange_order_id) != order_id:
            leg.status = "inconsistent"
            leg.last_error = _json({"reason": "management_close_order_id_conflict"})
            leg.updated_at = now
            return
        leg.exchange_order_id = order_id or leg.exchange_order_id
        leg.status = "submitted"
        leg.last_error = None
    elif leg.status in {"submitted", "partial"} and matching_order is None:
        # Position movement without the exact regular-order identity could be a
        # manual or unrelated close. Preserve the non-retryable pending state.
        leg.last_error = _json({"reason": "management_close_order_not_found"})
        leg.last_exchange_snapshot_json = _leg_snapshot(
            leg, position_rows, matching_orders
        )
        leg.updated_at = now
        return

    rows = position_rows.get(str(leg.pos_id), [])
    if len(rows) > 1:
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_position_snapshot_ambiguous"})
        leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
        leg.updated_at = now
        return

    if rows:
        instrument = _first_string(rows[0], "instId", "instrumentId", "symbol")
        if instrument and instrument.upper() != expected_instrument:
            leg.status = "inconsistent"
            leg.last_error = _json({"reason": "management_position_instrument_mismatch"})
            leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
            leg.updated_at = now
            return

    try:
        before = _positive_decimal(leg.preflight_size)
        planned = _positive_decimal(leg.planned_close_size)
        current = Decimal("0") if not rows else _position_size(rows[0])
    except (InvalidOperation, ValueError):
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_position_size_invalid"})
        leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
        leg.updated_at = now
        return

    expected = before - planned
    if expected < 0 or current < 0 or current > before or current < expected:
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_close_size_inconsistent"})
    elif current == expected:
        leg.status = "confirmed"
        leg.last_error = None
    elif current == before:
        # A known submitted order may still be live. An unresolved submission
        # was returned above unless exact order identity was found.
        leg.status = "submitted"
        leg.last_error = None
    else:
        leg.status = "partial"
        leg.last_error = _json({"reason": "management_close_partially_filled"})
    leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
    leg.updated_at = now


def _identity_is_exact(session, batch, legs) -> bool:
    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    if (
        binding is None
        or lifecycle is None
        or binding.strategy_instance_id != batch.strategy_instance_id
        or lifecycle.execution_binding_id != batch.execution_binding_id
        or lifecycle.lifecycle_status != "entered"
        or lifecycle.exit_reason is not None
        or lifecycle.exited_at is not None
        or binding.status not in {"open", "active", "stale"}
        or (
            binding.status == "stale"
            and binding.last_exchange_status
            != "verified_position_missing_from_exchange"
        )
    ):
        return False
    managed_identity = {
        (int(leg.execution_order_leg_id), str(leg.pos_id)) for leg in legs
    }
    deferred_leg_ids = _snapshot_deferred_entry_leg_ids(batch)
    if deferred_leg_ids is None:
        return False
    seen: set[str] = set()
    for leg in legs:
        if not leg.pos_id or str(leg.pos_id) in seen:
            return False
        seen.add(str(leg.pos_id))
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        entry_status = str(getattr(entry, "status", "") or "").lower()
        if (
            entry is None
            or entry.execution_binding_id != batch.execution_binding_id
            or entry.strategy_instance_id != batch.strategy_instance_id
            or entry.purpose != "entry"
            or entry.pos_id != leg.pos_id
            or entry.attribution_status != "verified"
            or entry_status in TERMINAL_ENTRY_LEG_STATES
            or entry_status not in _MANAGEABLE_ENTRY_LEG_STATES
            or entry.terminal_reason is not None
        ):
            return False
    all_entry_rows = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    accepted_deferred_ids: set[int] = set()
    for row in all_entry_rows:
        if row.strategy_instance_id != batch.strategy_instance_id:
            return False
        row_identity = (int(row.id), str(row.pos_id)) if row.pos_id else None
        if row_identity in managed_identity:
            continue
        if int(row.id) in deferred_leg_ids:
            if batch.effective_action in {"full_close", "full_exit"}:
                if _is_management_cancelled_deferred_entry_leg(row):
                    accepted_deferred_ids.add(int(row.id))
                    continue
            elif _is_deferred_pending_entry_leg(row):
                accepted_deferred_ids.add(int(row.id))
                continue
        return False
    return accepted_deferred_ids == deferred_leg_ids


def _snapshot_deferred_entry_leg_ids(batch) -> set[int] | None:
    try:
        snapshot = json.loads(batch.target_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        return None
    values = identity.get("deferred_entry_leg_ids", [])
    if (
        not isinstance(values, list)
        or any(type(value) is not int or value <= 0 for value in values)
        or len(set(values)) != len(values)
    ):
        return None
    return set(values)


def _is_deferred_pending_entry_leg(entry: ExecutionOrderLeg) -> bool:
    status = str(entry.status or "").lower()
    state = str(entry.attribution_status or "unassigned")
    return bool(
        status in _DEFERRED_ENTRY_LEG_STATES
        and status not in TERMINAL_ENTRY_LEG_STATES
        and entry.terminal_reason is None
        and not entry.pos_id
        and state not in {"attribution_conflict", "evidence_unavailable"}
    )


def _is_management_cancelled_deferred_entry_leg(entry: ExecutionOrderLeg) -> bool:
    return bool(
        str(entry.status or "").lower() == "cancelled"
        and entry.terminal_reason
        == "management_full_close_cancelled_unfilled_entry_leg"
        and not entry.pos_id
    )


def _terminalize_full_close(session, *, batch, legs, now: datetime) -> None:
    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    if binding is None or lifecycle is None:
        raise RuntimeError("management_reconciliation_identity_disappeared")
    for leg in legs:
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        if entry is None:
            raise RuntimeError("management_entry_leg_disappeared")
        entry.status = "closed"
        entry.terminal_reason = "management_full_close_confirmed"
        entry.last_verified_at = now
        entry.updated_at = now
    binding.status = "closed"
    binding.pos_id = None
    binding.last_exchange_status = "management_full_close_confirmed"
    binding.recovered_at = now
    binding.updated_at = now
    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "kol_signal"
    lifecycle.exited_at = now
    raw = session.get(RawMessage, batch.raw_message_id)
    lifecycle.management_signal_message_id = (
        int(raw.message_id) if raw is not None else None
    )
    lifecycle.management_action = "full_close_confirmed"
    lifecycle.updated_at = now


def _confirm_partial_close(session, *, batch, now: datetime) -> None:
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    raw = session.get(RawMessage, batch.raw_message_id)
    if lifecycle is None or raw is None:
        raise RuntimeError("management_reconciliation_identity_disappeared")
    lifecycle.management_signal_message_id = int(raw.message_id)
    lifecycle.management_action = "partial_close_confirmed"
    lifecycle.management_note = (
        "Deepcoin exchange confirmed every planned close leg."
    )
    lifecycle.updated_at = now


def _freeze_batch(session, batch, *, status: str, reason: str, now: datetime) -> None:
    batch.status = status
    batch.reason_code = reason
    batch.reconciled_at = None
    batch.completed_at = None
    batch.updated_at = now
    from telegram_kol_research.system_operator_bot import (
        persist_strategy_management_notification_in_session,
    )

    persist_strategy_management_notification_in_session(session, batch)


def _positions_by_id(rows: Any) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        pos_id = _first_string(row, "posId", "pos_id", "id")
        if pos_id:
            result.setdefault(pos_id, []).append(row)
    return result


def _regular_order_rows(snapshot: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in ("open_orders", "order_history", "trade_fills"):
        rows = getattr(snapshot, source, [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            identity = (
                _first_string(row, *_ORDER_ID_KEYS) or "",
                _first_string(row, *_CLIENT_ORDER_ID_KEYS) or "",
            )
            if identity == ("", "") or identity in seen:
                continue
            seen.add(identity)
            result.append(row)
    return result


def _matching_orders(leg, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for row in rows:
        order_id = _first_string(row, *_ORDER_ID_KEYS)
        client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
        if (
            leg.exchange_order_id
            and order_id == str(leg.exchange_order_id)
        ) or (
            leg.client_order_id
            and client_id == str(leg.client_order_id)
        ):
            matches.append(row)
    return matches


def _resolve_matching_order(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    if not rows:
        return None, False
    order_ids = {
        value for row in rows if (value := _first_string(row, *_ORDER_ID_KEYS))
    }
    client_ids = {
        value
        for row in rows
        if (value := _first_string(row, *_CLIENT_ORDER_ID_KEYS))
    }
    if len(order_ids) > 1 or len(client_ids) > 1:
        return None, True
    connected = {0}
    connected_order_ids = {
        value
        for value in (_first_string(rows[0], *_ORDER_ID_KEYS),)
        if value
    }
    connected_client_ids = {
        value
        for value in (_first_string(rows[0], *_CLIENT_ORDER_ID_KEYS),)
        if value
    }
    changed = True
    while changed:
        changed = False
        for index, row in enumerate(rows):
            if index in connected:
                continue
            order_id = _first_string(row, *_ORDER_ID_KEYS)
            client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
            if not (
                (order_id and order_id in connected_order_ids)
                or (client_id and client_id in connected_client_ids)
            ):
                continue
            connected.add(index)
            if order_id:
                connected_order_ids.add(order_id)
            if client_id:
                connected_client_ids.add(client_id)
            changed = True
    if len(connected) != len(rows):
        return None, True
    merged: dict[str, Any] = {}
    for row in rows:
        merged.update(row)
    if order_ids:
        merged["ordId"] = next(iter(order_ids))
    if client_ids:
        merged["clOrdId"] = next(iter(client_ids))
    return merged, False


def _order_identity_conflicts(leg, rows: list[dict[str, Any]]) -> bool:
    durable_order_id = str(leg.exchange_order_id) if leg.exchange_order_id else None
    durable_client_id = str(leg.client_order_id) if leg.client_order_id else None
    for row in rows:
        order_id = _first_string(row, *_ORDER_ID_KEYS)
        client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
        if durable_order_id and order_id and order_id != durable_order_id:
            return True
        if durable_client_id and client_id and client_id != durable_client_id:
            return True
    return False


def _position_size(row: dict[str, Any]) -> Decimal:
    for key in ("pos", "size", "sz", "positionSize", "position_size"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                result = abs(Decimal(str(value)))
            except InvalidOperation as exc:
                raise ValueError("invalid position size") from exc
            if not result.is_finite():
                raise ValueError("position size must be finite")
            return result
    raise ValueError("position size missing")


def _positive_decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("size must be finite and positive")
    return result


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _leg_snapshot(leg, positions, orders) -> str:
    return _json(
        {
            "position_rows": positions.get(str(leg.pos_id), []),
            "matching_regular_orders": orders,
        }
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
