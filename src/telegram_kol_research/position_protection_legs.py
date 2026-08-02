"""Durable lifecycle records for each planned DeepCoin protection leg."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from telegram_kol_research.models import ExecutionOrderLeg, PositionProtectionLeg


_ROLES = frozenset({"primary_stop", "backup_stop", "take_profit"})


def create_or_get_protection_leg(
    session: Session,
    *,
    venue: str,
    execution_order_leg_id: int,
    role: str,
    leg_index: int,
    planned_trigger_price: str | None,
    planned_size: str | None,
) -> PositionProtectionLeg:
    if role not in _ROLES:
        raise ValueError("protection_leg_role_invalid")
    if leg_index < 1:
        raise ValueError("protection_leg_index_invalid")
    entry_leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    if entry_leg is None:
        raise ValueError("protection_leg_entry_missing")
    if str(entry_leg.purpose or "") != "entry":
        raise ValueError("protection_leg_requires_entry_leg")
    existing = (
        session.query(PositionProtectionLeg)
        .filter_by(
            venue=venue,
            execution_order_leg_id=execution_order_leg_id,
            role=role,
            leg_index=leg_index,
        )
        .one_or_none()
    )
    if existing is not None:
        if (
            planned_trigger_price is not None
            and existing.planned_trigger_price is not None
            and existing.planned_trigger_price != planned_trigger_price
        ) or (
            planned_size is not None
            and existing.planned_size is not None
            and existing.planned_size != planned_size
        ):
            raise ValueError("protection_leg_planned_values_immutable")
        if existing.planned_trigger_price is None and planned_trigger_price is not None:
            existing.planned_trigger_price = planned_trigger_price
        if existing.planned_size is None and planned_size is not None:
            existing.planned_size = planned_size
        return existing
    created = PositionProtectionLeg(
        venue=venue,
        execution_binding_id=int(entry_leg.execution_binding_id),
        execution_order_leg_id=execution_order_leg_id,
        role=role,
        leg_index=leg_index,
        planned_trigger_price=planned_trigger_price,
        planned_size=planned_size,
    )
    session.add(created)
    session.flush()
    return created


def materialize_verified_position_protection(
    session: Session,
    *,
    venue: str,
    execution_order_leg_id: int,
    pos_id: str,
    primary_order_id: str,
    primary_stop: str,
    backup_stop: str | None = None,
    take_profits: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
) -> list[PositionProtectionLeg]:
    """Create the durable logical protection model for an exact legacy position.

    This only records verified identity and planned protection; it never writes
    to the exchange.  All fields are immutable through the existing binders.
    """

    primary = create_or_get_protection_leg(
        session, venue=venue, execution_order_leg_id=execution_order_leg_id,
        role="primary_stop", leg_index=1, planned_trigger_price=primary_stop,
        planned_size=None,
    )
    bind_filled_position(session, primary, pos_id=pos_id)
    bind_verified_exchange_order(
        session, primary, exchange_order_id=primary_order_id,
        readback_evidence={"source": "persisted_primary_ledger", "ordId": primary_order_id, "posId": pos_id},
    )
    result = [primary]
    if backup_stop is not None:
        backup = create_or_get_protection_leg(
            session, venue=venue, execution_order_leg_id=execution_order_leg_id,
            role="backup_stop", leg_index=1, planned_trigger_price=backup_stop,
            planned_size="0",
        )
        bind_filled_position(session, backup, pos_id=pos_id)
        result.append(backup)
    for index, (price, size) in enumerate(take_profits, start=1):
        take_profit = create_or_get_protection_leg(
            session, venue=venue, execution_order_leg_id=execution_order_leg_id,
            role="take_profit", leg_index=index, planned_trigger_price=price,
            planned_size=size,
        )
        bind_filled_position(session, take_profit, pos_id=pos_id)
        result.append(take_profit)
    return result


def bind_parent_entry_order(
    session: Session,
    protection_leg: PositionProtectionLeg,
    *,
    parent_entry_order_id: str,
) -> PositionProtectionLeg:
    _bind_immutable_text(protection_leg, "parent_entry_order_id", parent_entry_order_id)
    _touch(protection_leg)
    session.flush()
    return protection_leg


def bind_filled_position(
    session: Session,
    protection_leg: PositionProtectionLeg,
    *,
    pos_id: str,
) -> PositionProtectionLeg:
    _bind_immutable_text(protection_leg, "pos_id", pos_id)
    if protection_leg.status == "planned":
        protection_leg.status = "waiting_fill"
    _touch(protection_leg)
    session.flush()
    return protection_leg


def bind_verified_filled_position_protection(
    session: Session,
    *,
    execution_order_leg_id: int,
    pos_id: str,
) -> list[PositionProtectionLeg]:
    """Bind a verified filled entry position to all preplanned protection legs."""

    normalized_pos_id = str(pos_id or "").strip()
    entry_leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    if entry_leg is None:
        raise ValueError("protection_leg_entry_missing")
    if (
        str(entry_leg.status or "").lower() != "active"
        or str(entry_leg.attribution_status or "").lower() != "verified"
        or not normalized_pos_id
        or str(entry_leg.pos_id or "").strip() != normalized_pos_id
    ):
        raise ValueError("protection_leg_entry_not_verified_filled")
    rows = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.execution_order_leg_id
            == int(execution_order_leg_id)
        )
        .order_by(PositionProtectionLeg.id.asc())
        .all()
    )
    for row in rows:
        bind_filled_position(session, row, pos_id=normalized_pos_id)
    return rows


def bind_verified_exchange_order(
    session: Session,
    protection_leg: PositionProtectionLeg,
    *,
    exchange_order_id: str,
    readback_evidence: dict[str, Any],
) -> PositionProtectionLeg:
    if not protection_leg.pos_id:
        raise ValueError("protection_leg_position_required")
    _bind_immutable_text(protection_leg, "exchange_order_id", exchange_order_id)
    protection_leg.readback_evidence_json = _normalized_json(readback_evidence)
    protection_leg.status = "verified"
    _touch(protection_leg)
    session.flush()
    return protection_leg


def _bind_immutable_text(
    protection_leg: PositionProtectionLeg, field: str, value: str
) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"protection_leg_{field}_required")
    existing = str(getattr(protection_leg, field) or "").strip()
    if existing and existing != normalized:
        raise ValueError(f"protection_leg_{field}_conflict")
    setattr(protection_leg, field, normalized)


def _normalized_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _touch(protection_leg: PositionProtectionLeg) -> None:
    protection_leg.updated_at = datetime.now(UTC)
