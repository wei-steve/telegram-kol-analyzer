"""Durable staged take-profit convergence records for trigger entries."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    TriggerTakeProfitConvergence,
    utc_now,
)


def create_or_get_trigger_take_profit_convergence(
    session: Session,
    *,
    venue: str,
    execution_order_leg_id: int,
    desired_take_profits: list[dict[str, object]],
    created_at: datetime | None = None,
) -> TriggerTakeProfitConvergence:
    """Save one immutable take-profit plan per trigger entry leg."""

    normalized_venue = _normalized_venue(venue)
    normalized_plan = _normalized_take_profit_plan(desired_take_profits)
    if not normalized_plan:
        raise ValueError("trigger take-profit convergence requires a target")
    plan_json = json.dumps(normalized_plan, ensure_ascii=False, separators=(",", ":"))
    existing = (
        session.query(TriggerTakeProfitConvergence)
        .filter(TriggerTakeProfitConvergence.venue == normalized_venue)
        .filter(TriggerTakeProfitConvergence.execution_order_leg_id == execution_order_leg_id)
        .one_or_none()
    )
    if existing is not None:
        if existing.desired_take_profits_json != plan_json:
            raise ValueError("immutable staged take-profit plan differs")
        return existing
    leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    if leg is None:
        raise ValueError("execution order leg does not exist")
    if str(leg.purpose) != "entry" or str(leg.order_kind) != "trigger_limit":
        raise ValueError("staged take-profit convergence requires a trigger entry leg")
    if str(leg.venue).lower() != normalized_venue:
        raise ValueError("execution order leg venue differs from convergence venue")
    now = created_at or utc_now()
    row = TriggerTakeProfitConvergence(
        venue=normalized_venue,
        execution_binding_id=int(leg.execution_binding_id),
        execution_order_leg_id=int(leg.id),
        desired_take_profits_json=plan_json,
        status="waiting_position",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def mark_trigger_take_profit_convergence_ready(
    session: Session,
    convergence: TriggerTakeProfitConvergence,
    *,
    ready_at: datetime | None = None,
) -> TriggerTakeProfitConvergence:
    """Attach a queue item only to its reconciled, exact live split position."""

    if convergence.status not in {"waiting_position", "waiting_backup_stop", "ready"}:
        return convergence
    leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
    binding = session.get(ExecutionBinding, convergence.execution_binding_id)
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.purpose) != "entry"
        or str(leg.order_kind) != "trigger_limit"
        or str(leg.status).lower() != "active"
        or str(leg.attribution_status) != "verified"
        or not str(leg.pos_id or "").strip()
        or str(leg.pos_id) not in _split_ids(binding.pos_id)
    ):
        return convergence
    pos_id = str(leg.pos_id)
    if convergence.pos_id not in (None, pos_id):
        raise ValueError("convergence position identity is immutable")
    convergence.pos_id = pos_id
    convergence.status = "ready"
    convergence.reason_code = None
    convergence.updated_at = ready_at or utc_now()
    session.flush()
    return convergence


def _normalized_take_profit_plan(items: list[dict[str, object]]) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise ValueError("staged take-profit plan must be a list")
    normalized: list[dict[str, str]] = []
    prices: set[str] = set()
    allocation_total = Decimal("0")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("staged take-profit target must be an object")
        price = _positive_decimal(item.get("price"), label="take-profit price")
        allocation = _positive_decimal(item.get("allocation_pct"), label="take-profit allocation")
        price_text = _decimal_text(price)
        if price_text in prices:
            raise ValueError("staged take-profit prices must be unique")
        prices.add(price_text)
        allocation_total += allocation
        normalized.append({"allocation_pct": _decimal_text(allocation), "price": price_text})
    if allocation_total != Decimal("100"):
        raise ValueError("staged take-profit allocations must total 100")
    return normalized


def _positive_decimal(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _normalized_venue(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("venue must be nonempty")
    return value.strip().lower()


def _split_ids(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}
