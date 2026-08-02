"""Append-only lifecycle helpers for exact-leg take-profit order evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLeg,
    PositionReconciliationObservation,
    PositionTakeProfitOrder,
    TriggerTakeProfitConvergence,
    utc_now,
)
from telegram_kol_research.native_tpsl import native_tpsl_take_profit_is_market


def record_take_profit_order(
    session: Session,
    *,
    venue: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    order_id: str,
    trigger_price: str,
    size_text: str | None,
    created_at: datetime | None = None,
    evidence: dict[str, object] | None = None,
    trigger_take_profit_convergence_id: int | None = None,
) -> PositionTakeProfitOrder:
    """Record one exchange TP order after exact leg ownership is verified."""

    venue = _required_text(venue, "venue").lower()
    pos_id = _required_text(pos_id, "position ID")
    order_id = _required_text(order_id, "order ID")
    trigger_price = _required_text(trigger_price, "trigger price")
    _require_native_tpsl_readback(
        evidence,
        order_id=order_id,
        trigger_price=trigger_price,
        size_text=size_text,
    )
    _require_exact_leg_ownership(
        session,
        execution_binding_id=execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        pos_id=pos_id,
        venue=venue,
    )
    existing = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.venue == venue)
        .filter(PositionTakeProfitOrder.order_id == order_id)
        .one_or_none()
    )
    if existing is not None:
        if (
            int(existing.execution_binding_id) != int(execution_binding_id)
            or int(existing.execution_order_leg_id) != int(execution_order_leg_id)
            or existing.pos_id != pos_id
            or existing.trigger_price != trigger_price
            or existing.size_text != size_text
        ):
            raise ValueError("take-profit order identity is already owned")
        return existing
    now = created_at or utc_now()
    row = PositionTakeProfitOrder(
        venue=venue,
        execution_binding_id=int(execution_binding_id),
        execution_order_leg_id=int(execution_order_leg_id),
        trigger_take_profit_convergence_id=trigger_take_profit_convergence_id,
        pos_id=pos_id,
        order_id=order_id,
        trigger_price=trigger_price,
        size_text=size_text,
        status="active",
        evidence_json=_json(evidence),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def record_take_profit_cancel_requested(
    session: Session,
    row: PositionTakeProfitOrder,
    *,
    request: dict[str, object],
    requested_at: datetime | None = None,
) -> PositionTakeProfitOrder:
    if row.status == "cancel_requested":
        return row
    if row.status != "active":
        raise ValueError("only active take-profit orders can be cancelled")
    row.status = "cancel_requested"
    row.cancel_request_json = _json(request)
    row.cancel_requested_at = requested_at or utc_now()
    row.updated_at = row.cancel_requested_at
    session.flush()
    return row


def record_take_profit_cancelled(
    session: Session,
    row: PositionTakeProfitOrder,
    *,
    response: dict[str, object],
    cancelled_at: datetime | None = None,
) -> PositionTakeProfitOrder:
    if row.status == "cancelled":
        return row
    if row.status != "cancel_requested":
        raise ValueError("take-profit cancellation was not requested")
    row.status = "cancelled"
    row.cancel_response_json = _json(response)
    row.cancelled_at = cancelled_at or utc_now()
    row.completed_at = row.cancelled_at
    row.updated_at = row.cancelled_at
    session.flush()
    return row


def reconcile_trigger_take_profit_order_history(
    session: Session,
    *,
    positions: list[dict],
    pending_orders: list[dict],
    trigger_history: list[dict],
    order_history: list[dict] | None = None,
    trade_fills: list[dict] | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Reconcile TP audit records from read-only exchange observations.

    A reduced position is never treated as a TP fill unless a known TP order
    has a terminal history row.  That rule keeps generic/manual partial exits
    from silently changing the staged plan.
    """

    now = observed_at or utc_now()
    pending_ids = {_order_id(row) for row in pending_orders if isinstance(row, dict)}
    history_by_id = {
        order_id: row for row in trigger_history if isinstance(row, dict)
        if (order_id := _order_id(row)) is not None
    }
    active_rows = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.status == "active")
        .all()
    )
    for row in active_rows:
        protection_leg = (
            session.query(PositionProtectionLeg)
            .filter_by(
                venue=row.venue,
                execution_order_leg_id=row.execution_order_leg_id,
                role="take_profit",
                exchange_order_id=row.order_id,
            )
            .one_or_none()
        )
        if protection_leg is not None and int(protection_leg.leg_index) == 1:
            from telegram_kol_research.take_profit_fill_evidence import (
                prove_first_take_profit_fill,
            )

            observations = (
                session.query(PositionReconciliationObservation)
                .filter_by(venue=row.venue, pos_id=row.pos_id)
                .order_by(
                    PositionReconciliationObservation.observed_at.desc(),
                    PositionReconciliationObservation.id.desc(),
                )
                .limit(2)
                .all()
            )
            current_observation = observations[0] if observations else None
            previous_observation = observations[1] if len(observations) > 1 else None
            conflicting_mutations = _conflicting_position_mutations(
                session,
                pos_id=row.pos_id,
                previous_observation=previous_observation,
                current_observation=current_observation,
            )
            binding = session.get(ExecutionBinding, row.execution_binding_id)
            if binding is None:
                continue
            proof = prove_first_take_profit_fill(
                tp_order=row,
                protection_leg=protection_leg,
                expected_side=str(binding.side),
                previous_observation=previous_observation,
                current_observation=current_observation,
                trigger_history=trigger_history,
                order_history=order_history or [],
                trade_fills=trade_fills or [],
                conflicting_mutations=conflicting_mutations,
            )
            if proof.proven:
                _record_proven_tp1_fill(
                    session=session,
                    row=row,
                    protection_leg=protection_leg,
                    proof=proof,
                    observed_at=now,
                )
                continue
        history = history_by_id.get(row.order_id)
        if row.order_id in pending_ids or history is None:
            continue
        terminal = _terminal_order_status(history)
        if terminal is None:
            continue
        row.status = terminal
        row.completed_at = now
        row.updated_at = now

    convergences = (
        session.query(TriggerTakeProfitConvergence)
        .filter(TriggerTakeProfitConvergence.status == "submitted")
        .all()
    )
    for convergence in convergences:
        if not convergence.pos_id:
            continue
        live_size = _live_position_size(
            positions,
            pos_id=str(convergence.pos_id),
            binding_id=int(convergence.execution_binding_id),
            session=session,
        )
        if live_size is None:
            continue
        orders = (
            session.query(PositionTakeProfitOrder)
            .filter(PositionTakeProfitOrder.trigger_take_profit_convergence_id == convergence.id)
            .all()
        )
        if not orders:
            continue
        planned_size = sum((_decimal_or_zero(row.size_text) for row in orders), Decimal("0"))
        terminal_fills = any(row.status == "filled" for row in orders)
        if live_size < planned_size and not terminal_fills:
            convergence.status = "conflicted"
            convergence.reason_code = "convergence_partial_position_unexplained"
            convergence.completed_at = now
            convergence.updated_at = now
    session.flush()


def _conflicting_position_mutations(
    session: Session,
    *,
    pos_id: str,
    previous_observation: PositionReconciliationObservation | None,
    current_observation: PositionReconciliationObservation | None,
) -> list[PositionMutationIntent]:
    if previous_observation is None or current_observation is None:
        return []
    return (
        session.query(PositionMutationIntent)
        .filter(PositionMutationIntent.pos_id == pos_id)
        .filter(PositionMutationIntent.operation == "close_position")
        .filter(PositionMutationIntent.reserved_at >= previous_observation.observed_at)
        .filter(PositionMutationIntent.reserved_at <= current_observation.observed_at)
        .all()
    )


def _record_proven_tp1_fill(
    *,
    session: Session,
    row: PositionTakeProfitOrder,
    protection_leg: PositionProtectionLeg,
    proof,
    observed_at: datetime,
) -> None:
    evidence = _load_evidence(row.evidence_json)
    evidence["tp1_fill"] = {
        "evidence_tier": proof.evidence_tier,
        "trigger_order_id": proof.trigger_order_id,
        "filled_size": proof.filled_size,
        "reason_code": proof.reason_code,
        "evidence": proof.evidence,
    }
    row.status = "filled"
    row.evidence_json = _json(evidence)
    row.completed_at = observed_at
    row.updated_at = observed_at
    from telegram_kol_research.position_protection_legs import (
        record_verified_take_profit_fill,
    )

    record_verified_take_profit_fill(
        session,
        protection_leg,
        evidence=evidence["tp1_fill"],
        completed_at=observed_at,
    )


def _load_evidence(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _require_exact_leg_ownership(
    session: Session,
    *,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    venue: str,
) -> None:
    leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    binding = session.get(ExecutionBinding, execution_binding_id)
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.venue).lower() != venue
        or str(leg.purpose) != "entry"
        or str(leg.attribution_status) != "verified"
        or str(leg.pos_id or "") != pos_id
        or pos_id not in {item.strip() for item in str(binding.pos_id or "").split(",") if item.strip()}
    ):
        raise ValueError("take-profit order requires verified exact leg ownership")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty")
    return value.strip()


def _require_native_tpsl_readback(
    evidence: dict[str, object] | None,
    *,
    order_id: str,
    trigger_price: str,
    size_text: str | None,
) -> None:
    """Keep REST acknowledgement alone from becoming active TP evidence."""

    if not isinstance(evidence, dict) or evidence.get("source") != "native_tpsl_pending_readback":
        raise ValueError("take-profit order requires native TPSL pending readback evidence")
    native = evidence.get("native_tpsl")
    if not isinstance(native, dict):
        raise ValueError("take-profit order requires native TPSL pending readback evidence")
    if (
        str(native.get("triggerOrderType") or "").upper() != "TPSL"
        or str(native.get("ordId") or native.get("orderId") or "") != order_id
        or not native_tpsl_take_profit_is_market(native)
        or not _same_decimal(native.get("tpTriggerPx") or native.get("tpTriggerPrice"), trigger_price)
        or not _same_decimal(native.get("sz"), size_text)
    ):
        raise ValueError("take-profit order requires native TPSL pending readback evidence")


def _same_decimal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _json(value: dict[str, object] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _order_id(row: dict) -> str | None:
    value = row.get("ordId") or row.get("orderId") or row.get("order_id")
    return str(value) if value not in (None, "") else None


def _terminal_order_status(row: dict) -> str | None:
    status = str(row.get("state") or row.get("status") or row.get("ordState") or "").lower()
    if status in {"filled", "success", "executed"}:
        return "filled"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"expired", "failed", "rejected"}:
        return "expired"
    return None


def _live_position_size(positions: list[dict], *, pos_id: str, binding_id: int, session: Session) -> Decimal | None:
    binding = session.get(ExecutionBinding, binding_id)
    if binding is None:
        return None
    inst_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    matches = [
        row for row in positions if isinstance(row, dict)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posSide") or row.get("pos_side") or "").lower() == str(binding.side).lower()
        and str(row.get("mrgPosition") or row.get("posMode") or "").lower() == "split"
        and _decimal_or_zero(row.get("pos")) > 0
    ]
    return _decimal_or_zero(matches[0].get("pos")) if len(matches) == 1 else None


def _decimal_or_zero(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")
