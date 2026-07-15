"""Reconcile close batches from one coherent, read-only exchange snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

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


_ACTIVE_RECONCILIATION_STATUSES = frozenset(
    {"executing", "reconciling", "partial_failed", "recovery_required"}
)
_CLOSE_ACTIONS = frozenset({"partial_close", "full_close", "full_exit"})
_ORDER_ID_KEYS = ("ordId", "orderId", "order_id", "id")
_CLIENT_ORDER_ID_KEYS = ("clOrdId", "clientOrderId", "client_order_id")


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
) -> ManagementReconciliationResult:
    """Apply exchange truth without submitting or retrying any order."""

    now = reconciled_at or datetime.now(UTC)
    if getattr(snapshot, "errors", {}).get("positions"):
        return ManagementReconciliationResult()

    position_rows = _positions_by_id(getattr(snapshot, "positions", []))
    order_rows = _regular_order_rows(snapshot)
    counts = {"checked": 0, "succeeded": 0, "pending": 0, "frozen": 0}

    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status.in_(_ACTIVE_RECONCILIATION_STATUSES))
            .order_by(StrategyManagementBatch.planned_at.asc(), StrategyManagementBatch.id.asc())
            .all()
        )
        for batch in batches:
            if batch.effective_action not in _CLOSE_ACTIONS:
                continue
            counts["checked"] += 1
            legs = (
                session.query(StrategyManagementLeg)
                .filter(StrategyManagementLeg.management_batch_id == batch.id)
                .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
                .all()
            )
            if not legs or not _identity_is_exact(session, batch, legs):
                _freeze_batch(
                    batch,
                    status="recovery_required",
                    reason="management_reconciliation_identity_mismatch",
                    now=now,
                )
                counts["frozen"] += 1
                continue

            binding = session.get(ExecutionBinding, batch.execution_binding_id)
            expected_instrument = f"{str(binding.symbol).upper()}-USDT-SWAP"

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
                batch.status = "succeeded"
                batch.reason_code = "management_close_exchange_confirmed"
                batch.reconciled_at = now
                batch.completed_at = now
                batch.updated_at = now
                if batch.effective_action in {"full_close", "full_exit"}:
                    _terminalize_full_close(session, batch=batch, legs=legs, now=now)
                counts["succeeded"] += 1
            elif "failed" in statuses:
                _freeze_batch(
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
                _freeze_batch(batch, status="recovery_required", reason=reason, now=now)
                counts["frozen"] += 1
            elif "confirmed" in statuses:
                _freeze_batch(
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
    if leg.status in {"reserved", "submit_unknown"}:
        matching_order, ambiguous = _resolve_matching_order(matching_orders)
        if matching_order is None:
            leg.status = "inconsistent" if ambiguous else "submit_unknown"
            leg.last_error = _json(
                {
                    "reason": (
                        "management_close_order_identity_ambiguous"
                        if ambiguous
                        else "management_close_order_not_found"
                    )
                }
            )
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
    ):
        return False
    seen: set[str] = set()
    for leg in legs:
        if not leg.pos_id or str(leg.pos_id) in seen:
            return False
        seen.add(str(leg.pos_id))
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        if (
            entry is None
            or entry.execution_binding_id != batch.execution_binding_id
            or entry.strategy_instance_id != batch.strategy_instance_id
            or entry.purpose != "entry"
            or entry.pos_id != leg.pos_id
            or entry.attribution_status != "verified"
        ):
            return False
    exact_entry_rows = (
        session.query(ExecutionOrderLeg.id, ExecutionOrderLeg.pos_id)
        .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
        .filter(ExecutionOrderLeg.strategy_instance_id == batch.strategy_instance_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.attribution_status == "verified")
        .filter(ExecutionOrderLeg.pos_id.is_not(None))
        .all()
    )
    return {(int(row.id), str(row.pos_id)) for row in exact_entry_rows} == {
        (int(leg.execution_order_leg_id), str(leg.pos_id)) for leg in legs
    }


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


def _freeze_batch(batch, *, status: str, reason: str, now: datetime) -> None:
    batch.status = status
    batch.reason_code = reason
    batch.reconciled_at = None
    batch.completed_at = None
    batch.updated_at = now


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
    merged: dict[str, Any] = {}
    for row in rows:
        merged.update(row)
    if order_ids:
        merged["ordId"] = next(iter(order_ids))
    if client_ids:
        merged["clOrdId"] = next(iter(client_ids))
    return merged, False


def _position_size(row: dict[str, Any]) -> Decimal:
    for key in ("pos", "size", "sz", "positionSize", "position_size"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return abs(Decimal(str(value)))
            except InvalidOperation as exc:
                raise ValueError("invalid position size") from exc
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
