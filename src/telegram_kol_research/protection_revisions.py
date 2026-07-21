"""Append-only protection-version helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


def record_replacing_protection_revision(
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
    """Append a replacement that cannot supersede confirmed protection yet."""

    now = utc_now()
    prior = (
        session.query(PositionProtectionRevision)
        .filter(PositionProtectionRevision.venue == str(venue).lower())
        .filter(PositionProtectionRevision.pos_id == str(pos_id))
        .filter(PositionProtectionRevision.status == "active")
        .one_or_none()
    )
    row = PositionProtectionRevision(
        venue=str(venue).lower(),
        execution_binding_id=int(execution_binding_id),
        execution_order_leg_id=int(execution_order_leg_id),
        strategy_instance_id=strategy_instance_id,
        pos_id=str(pos_id),
        previous_revision_id=prior.id if prior is not None else None,
        source=str(source),
        status="replacing",
        protection_json=json.dumps(protection_json, ensure_ascii=False, sort_keys=True),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def confirm_visible_protection_revision(
    session,
    *,
    venue: str,
    pos_id: str,
    visible_order_ids: set[str],
) -> bool:
    """Activate one replacement only when every expected order ID is visible."""

    replacements = (
        session.query(PositionProtectionRevision)
        .filter(PositionProtectionRevision.venue == str(venue).lower())
        .filter(PositionProtectionRevision.pos_id == str(pos_id))
        .filter(PositionProtectionRevision.status == "replacing")
        .order_by(PositionProtectionRevision.created_at, PositionProtectionRevision.id)
        .all()
    )
    normalized_visible = {str(value) for value in visible_order_ids if str(value)}
    for replacement in replacements:
        try:
            protection = json.loads(replacement.protection_json)
        except (TypeError, ValueError):
            continue
        expected = {
            str(value) for value in protection.get("order_ids", []) if str(value)
        } if isinstance(protection, dict) else set()
        if not expected or not expected.issubset(normalized_visible):
            continue
        prior = (
            session.query(PositionProtectionRevision)
            .filter(PositionProtectionRevision.venue == str(venue).lower())
            .filter(PositionProtectionRevision.pos_id == str(pos_id))
            .filter(PositionProtectionRevision.status == "active")
            .one_or_none()
        )
        now = utc_now()
        if prior is not None:
            prior.status = "superseded"
            prior.updated_at = now
            replacement.previous_revision_id = prior.id
        replacement.status = "active"
        replacement.updated_at = now
        return True
    return False


def expire_unconfirmed_protection_revisions(
    session, *, now: datetime | None = None
) -> list[int]:
    """Terminalize replacements not confirmed within the five-minute window."""

    reference = now or utc_now()
    if reference.tzinfo is not None:
        reference = reference.astimezone(UTC).replace(tzinfo=None)
    expired_ids: list[int] = []
    for revision in (
        session.query(PositionProtectionRevision)
        .filter(PositionProtectionRevision.status == "replacing")
        .all()
    ):
        created = revision.created_at
        if created.tzinfo is not None:
            created = created.astimezone(UTC).replace(tzinfo=None)
        if reference < created + timedelta(minutes=5):
            continue
        revision.status = "visibility_expired"
        revision.updated_at = reference
        expired_ids.append(int(revision.id))
    return expired_ids
