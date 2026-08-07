"""Atomic role-aware persistence for verified protection replacements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from telegram_kol_research.models import (
    PositionBackupStopOrder,
    PositionProtectionLeg,
    PositionProtectionRevision,
    PositionProtectionLedger,
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
    matching_revision = None
    for candidate in session.query(PositionProtectionRevision).filter_by(
        venue=str(venue).lower(), pos_id=str(pos_id)
    ):
        try:
            existing_payload = json.loads(candidate.protection_json)
        except (TypeError, ValueError):
            continue
        if existing_payload.get("replacement_identity") == replacement_identity:
            matching_revision = candidate
            if existing_payload != revision_payload:
                raise ValueError("replacement_identity_payload_conflict")
            break
    if matching_revision is not None and matching_revision.status != "active":
        raise ValueError("stale_replacement_replay_forbidden")
    if matching_revision is not None and _projection_complete(
        session,
        venue=venue,
        execution_binding_id=execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        pos_id=pos_id,
        replacements=replacements,
    ):
        return matching_revision

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

    backup_replacement = next(
        row for row in replacements if row.role == "backup_stop"
    )
    existing_backup = (
        session.query(PositionBackupStopOrder)
        .filter(
            PositionBackupStopOrder.venue == str(venue).lower(),
            PositionBackupStopOrder.order_id == backup_replacement.order_id,
        )
        .one_or_none()
    )
    for backup in session.query(PositionBackupStopOrder).filter(
        PositionBackupStopOrder.venue == str(venue).lower(),
        PositionBackupStopOrder.pos_id == str(pos_id),
        PositionBackupStopOrder.status.in_(
            ("submitting", "active", "unknown_exchange_outcome")
        ),
    ):
        if backup is not existing_backup:
            backup.status = "superseded"
            backup.completed_at = now
            backup.updated_at = now
    backup_values = {
        "venue": str(venue).lower(),
        "execution_binding_id": int(execution_binding_id),
        "execution_order_leg_id": int(execution_order_leg_id),
        "pos_id": str(pos_id),
        "instrument_id": str(instrument_id),
        "side": str(side).lower(),
        "trigger_price": backup_replacement.trigger_price,
        "order_id": backup_replacement.order_id,
        "client_order_id": f"replacement:{replacement_identity}:backup",
        "status": "active",
        "request_json": json.dumps(
            {"replacement_identity": replacement_identity}, sort_keys=True
        ),
        "response_json": json.dumps(
            {"order_id": backup_replacement.order_id}, sort_keys=True
        ),
        "submitted_at": now,
        "updated_at": now,
    }
    if existing_backup is None:
        session.add(PositionBackupStopOrder(**backup_values, created_at=now))
    else:
        for key, value in backup_values.items():
            setattr(existing_backup, key, value)
        existing_backup.completed_at = None
    revision = matching_revision or activate_protection_revision(
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


def _projection_complete(
    session,
    *,
    venue,
    execution_binding_id,
    execution_order_leg_id,
    pos_id,
    replacements,
) -> bool:
    expected_roles = [
        (row.role, row.order_id, row.trigger_price, row.size_text)
        for row in replacements
    ]
    role_records = (
        session.query(PositionProtectionLeg)
        .filter_by(
            venue=str(venue).lower(),
            execution_binding_id=int(execution_binding_id),
            execution_order_leg_id=int(execution_order_leg_id),
            pos_id=str(pos_id),
            status="verified",
        )
        .all()
    )
    actual_roles = [
        (
            row.role,
            row.exchange_order_id,
            row.planned_trigger_price,
            row.planned_size,
        )
        for row in role_records
    ]
    role_to_purpose = {
        "primary_stop": "stop_loss",
        "backup_stop": "backup_stop",
        "take_profit": "take_profit",
    }
    expected_ledgers = [
        (
            row.order_id,
            role_to_purpose[row.role],
            row.trigger_price,
            row.size_text,
        )
        for row in replacements
    ]
    ledger_records = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == str(venue).lower(),
            PositionProtectionLedger.execution_binding_id
            == int(execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionProtectionLedger.pos_id == str(pos_id),
            PositionProtectionLedger.status == "verified",
        )
        .all()
    )
    actual_ledgers = [
        (row.order_id, row.purpose, row.trigger_price, row.size_text)
        for row in ledger_records
        if row.order_id in {item.order_id for item in replacements}
    ]
    backup = session.query(PositionBackupStopOrder).filter_by(
        venue=str(venue).lower(),
        execution_binding_id=int(execution_binding_id),
        execution_order_leg_id=int(execution_order_leg_id),
        pos_id=str(pos_id),
        status="active",
    ).one_or_none()
    expected_backup = next(row for row in replacements if row.role == "backup_stop")
    return (
        len(actual_roles) == len(expected_roles)
        and set(actual_roles) == set(expected_roles)
        and len(actual_ledgers) == len(expected_ledgers)
        and set(actual_ledgers) == set(expected_ledgers)
        and backup is not None
        and backup.order_id == expected_backup.order_id
        and backup.trigger_price == expected_backup.trigger_price
    )
