"""Append-only protection-version helpers."""

from __future__ import annotations

import json

from telegram_kol_research.models import PositionProtectionRevision, utc_now


def activate_protection_revision(
    session,
    *,
    execution_binding_id: int,
    execution_order_leg_id: int,
    strategy_instance_id: str | None,
    pos_id: str,
    source: str,
    protection_json: dict,
    venue: str = "deepcoin",
) -> PositionProtectionRevision:
    """Record a new active revision and retain the former revision as history."""

    now = utc_now()
    prior = (
        session.query(PositionProtectionRevision)
        .filter(PositionProtectionRevision.venue == str(venue).lower())
        .filter(PositionProtectionRevision.pos_id == str(pos_id))
        .filter(PositionProtectionRevision.status == "active")
        .one_or_none()
    )
    if prior is not None:
        prior.status = "superseded"
        prior.updated_at = now
    row = PositionProtectionRevision(
        venue=str(venue).lower(),
        execution_binding_id=int(execution_binding_id),
        execution_order_leg_id=int(execution_order_leg_id),
        strategy_instance_id=strategy_instance_id,
        pos_id=str(pos_id),
        previous_revision_id=prior.id if prior is not None else None,
        source=str(source),
        status="active",
        protection_json=json.dumps(protection_json, ensure_ascii=False, sort_keys=True),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row
