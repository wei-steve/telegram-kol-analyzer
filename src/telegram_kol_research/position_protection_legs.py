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
            existing.planned_trigger_price != planned_trigger_price
            or existing.planned_size != planned_size
        ):
            raise ValueError("protection_leg_planned_values_immutable")
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
