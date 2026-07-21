"""Append-only lifecycle helpers for exact-leg take-profit order evidence."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionTakeProfitOrder,
    utc_now,
)


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


def _json(value: dict[str, object] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None
