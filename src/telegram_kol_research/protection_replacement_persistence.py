"""Atomic role-aware persistence for verified protection replacements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from telegram_kol_research.models import (
    PositionBackupStopOrder,
    PositionProtectionLeg,
    PositionProtectionRevision,
    utc_now,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.protection_revisions import activate_protection_revision


ProtectionRole = Literal["primary_stop", "backup_stop", "take_profit"]


@dataclass(frozen=True, slots=True)
class VerifiedProtectionReplacement:
    role: ProtectionRole
    order_id: str
    trigger_price: str
    size_text: str | None


def persist_verified_protection_replacement(
    session,
    *,
    venue: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    strategy_instance_id: str | None,
    pos_id: str,
    instrument_id: str,
    side: str,
    source: str,
    replacement_identity: str,
    replacements: tuple[VerifiedProtectionReplacement, ...],
    seen_at,
) -> PositionProtectionRevision:
    """Converge ledger, logical roles, backup ownership, and revision atomically."""

    roles = [row.role for row in replacements]
    order_ids = [str(row.order_id) for row in replacements]
    if not replacements or len(order_ids) != len(set(order_ids)):
        raise ValueError("replacement_order_identity_invalid")
    if roles.count("primary_stop") != 1 or roles.count("backup_stop") != 1:
        raise ValueError("replacement_stop_roles_incomplete")

    revision_payload = {
        "replacement_identity": str(replacement_identity),
        "order_ids": order_ids,
        "roles": roles,
        "replacements": [
            {
                "role": row.role,
                "order_id": row.order_id,
                "trigger_price": row.trigger_price,
                "size_text": row.size_text,
            }
            for row in replacements
        ],
    }
    existing_revision = (
        session.query(PositionProtectionRevision)
        .filter_by(venue=str(venue).lower(), pos_id=str(pos_id), status="active")
        .one_or_none()
    )
    if existing_revision is not None:
        try:
            existing_payload = json.loads(existing_revision.protection_json)
        except (TypeError, ValueError):
            existing_payload = {}
        if existing_payload.get("replacement_identity") == replacement_identity:
            if existing_payload.get("order_ids") != order_ids:
                raise ValueError("replacement_identity_order_conflict")
            return existing_revision

    role_to_purpose = {
        "primary_stop": "stop_loss",
        "backup_stop": "backup_stop",
        "take_profit": "take_profit",
    }
    now = seen_at or utc_now()
    for replacement in replacements:
        upsert_protection_ledger_row(
            session,
            venue=venue,
            execution_binding_id=execution_binding_id,
            execution_order_leg_id=execution_order_leg_id,
            strategy_instance_id=strategy_instance_id,
            pos_id=pos_id,
            instrument_id=instrument_id,
            side=side,
            order_id=replacement.order_id,
            purpose=role_to_purpose[replacement.role],
            trigger_price=replacement.trigger_price,
            size_text=replacement.size_text,
            status="verified",
            evidence_source=source,
            evidence={"replacement_identity": replacement_identity, "role": replacement.role},
            seen_at=now,
        )

    active_roles = (
        session.query(PositionProtectionLeg)
        .filter_by(
            venue=str(venue).lower(),
            execution_order_leg_id=int(execution_order_leg_id),
            pos_id=str(pos_id),
            status="verified",
        )
        .all()
    )
    for row in active_roles:
        row.status = "superseded"
        row.updated_at = now

    next_indexes: dict[str, int] = {}
    for replacement in replacements:
        if replacement.role not in next_indexes:
            maximum = max(
                (
                    int(row.leg_index)
                    for row in session.query(PositionProtectionLeg).filter_by(
                        venue=str(venue).lower(),
                        execution_order_leg_id=int(execution_order_leg_id),
                        role=replacement.role,
                    )
                ),
                default=0,
            )
            next_indexes[replacement.role] = maximum
        next_indexes[replacement.role] += 1
        session.add(
            PositionProtectionLeg(
                venue=str(venue).lower(),
                execution_binding_id=int(execution_binding_id),
                execution_order_leg_id=int(execution_order_leg_id),
                role=replacement.role,
                leg_index=next_indexes[replacement.role],
                planned_trigger_price=replacement.trigger_price,
                planned_size=replacement.size_text,
                pos_id=str(pos_id),
                exchange_order_id=str(replacement.order_id),
                status="verified",
                readback_evidence_json=json.dumps(
                    {"source": source, "replacement_identity": replacement_identity},
                    sort_keys=True,
                ),
                created_at=now,
                updated_at=now,
            )
        )

    for backup in session.query(PositionBackupStopOrder).filter(
        PositionBackupStopOrder.venue == str(venue).lower(),
        PositionBackupStopOrder.pos_id == str(pos_id),
        PositionBackupStopOrder.status.in_(
            ("submitting", "active", "unknown_exchange_outcome")
        ),
    ):
        backup.status = "superseded"
        backup.completed_at = now
        backup.updated_at = now
    backup_replacement = next(
        row for row in replacements if row.role == "backup_stop"
    )
    session.add(
        PositionBackupStopOrder(
            venue=str(venue).lower(),
            execution_binding_id=int(execution_binding_id),
            execution_order_leg_id=int(execution_order_leg_id),
            pos_id=str(pos_id),
            instrument_id=str(instrument_id),
            side=str(side).lower(),
            trigger_price=backup_replacement.trigger_price,
            order_id=backup_replacement.order_id,
            client_order_id=f"replacement:{replacement_identity}:backup",
            status="active",
            request_json=json.dumps({"replacement_identity": replacement_identity}, sort_keys=True),
            response_json=json.dumps({"order_id": backup_replacement.order_id}, sort_keys=True),
            submitted_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    revision = activate_protection_revision(
        session,
        venue=venue,
        execution_binding_id=execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        strategy_instance_id=strategy_instance_id,
        pos_id=pos_id,
        source=source,
        protection_json=revision_payload,
    )
    session.flush()
    return revision
