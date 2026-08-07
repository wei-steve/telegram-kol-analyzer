"""Fail-closed health classification for exact owned position protections."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import PositionProtectionRevision


def current_protection_incident_health_status(
    session: Session,
    *,
    incident: PositionProtectionIncident,
) -> str:
    """Resolve an old incident only from a newer, exact, complete projection."""

    revision = (
        session.query(PositionProtectionRevision)
        .filter(
            PositionProtectionRevision.venue == str(incident.venue).lower(),
            PositionProtectionRevision.execution_binding_id
            == int(incident.execution_binding_id),
            PositionProtectionRevision.execution_order_leg_id
            == int(incident.execution_order_leg_id),
            PositionProtectionRevision.pos_id == str(incident.pos_id),
            PositionProtectionRevision.status == "active",
            PositionProtectionRevision.created_at > incident.created_at,
        )
        .order_by(PositionProtectionRevision.created_at.desc())
        .first()
    )
    if revision is None:
        return "current_risk"
    try:
        payload = json.loads(revision.protection_json)
    except (TypeError, ValueError):
        return "current_risk"
    replacements = payload.get("replacements") or []
    if not isinstance(replacements, list) or not replacements:
        return "current_risk"
    roles = {
        str(item.get("role") or "")
        for item in replacements
        if isinstance(item, dict)
    }
    order_ids = [
        str(item.get("order_id") or "")
        for item in replacements
        if isinstance(item, dict)
    ]
    declared_roles = {str(value) for value in payload.get("roles", [])}
    declared_order_ids = {
        str(value) for value in payload.get("order_ids", []) if value
    }
    if (
        not {"primary_stop", "backup_stop", "take_profit"}.issubset(roles)
        or declared_roles != roles
        or not all(order_ids)
        or len(order_ids) != len(set(order_ids))
        or declared_order_ids != set(order_ids)
    ):
        return "current_risk"

    ledgers = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == str(incident.venue).lower(),
            PositionProtectionLedger.execution_binding_id
            == int(incident.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id
            == int(incident.execution_order_leg_id),
            PositionProtectionLedger.pos_id == str(incident.pos_id),
            PositionProtectionLedger.status == "verified",
            PositionProtectionLedger.order_id.in_(sorted(set(order_ids))),
        )
        .all()
    )
    role_to_purpose = {
        "primary_stop": "stop_loss",
        "backup_stop": "backup_stop",
        "take_profit": "take_profit",
    }
    expected = {
        (
            str(item.get("order_id")),
            role_to_purpose.get(str(item.get("role")), ""),
            str(item.get("trigger_price") or ""),
            str(item.get("size_text") or ""),
        )
        for item in replacements
        if isinstance(item, dict)
    }
    actual = {
        (
            row.order_id,
            row.purpose,
            str(row.trigger_price or ""),
            str(row.size_text or ""),
        )
        for row in ledgers
    }
    if len(ledgers) != len(replacements) or actual != expected:
        return "current_risk"

    backup = (
        session.query(PositionBackupStopOrder.id)
        .filter(
            PositionBackupStopOrder.venue == str(incident.venue).lower(),
            PositionBackupStopOrder.execution_binding_id
            == int(incident.execution_binding_id),
            PositionBackupStopOrder.execution_order_leg_id
            == int(incident.execution_order_leg_id),
            PositionBackupStopOrder.pos_id == str(incident.pos_id),
            PositionBackupStopOrder.status == "active",
            PositionBackupStopOrder.order_id.in_(sorted(set(order_ids))),
        )
        .first()
    )
    return "resolved_by_verified_replacement" if backup else "current_risk"


def reconcile_position_protection_health(
    session: Session,
    *,
    positions: list[dict[str, Any]],
    pending_orders: list[dict[str, Any]],
    trigger_history: list[dict[str, Any]],
    snapshot_errors: dict[str, str],
    observed_at: datetime,
) -> int:
    """Record unhealthy owned stops without submitting replacement orders."""

    live_ids = {
        _text(row, "posId", "pos_id", "id")
        for row in positions
        if _nonzero_position(row) and _text(row, "posId", "pos_id", "id")
    }
    if not live_ids:
        return 0
    ledgers = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.pos_id.in_(sorted(live_ids)))
        .filter(
            PositionProtectionLedger.status.in_(
                ("verified", "protected", "protection_missing")
            )
        )
        .all()
    )
    backups = (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.pos_id.in_(sorted(live_ids)))
        .filter(PositionBackupStopOrder.status.in_(("active", "submitting", "unknown_exchange_outcome")))
        .all()
    )
    created = 0
    for row in [*ledgers, *backups]:
        order_id = str(getattr(row, "order_id", "") or "")
        pos_id = str(row.pos_id)
        if snapshot_errors:
            created += _incident(
                session, row=row, incident_type="protection_unknown",
                evidence={"errors": dict(sorted(snapshot_errors.items())), "order_id": order_id}, observed_at=observed_at,
            )
            continue
        histories = [
            item
            for item in trigger_history
            if _matches_owned_order(item, pos_id=pos_id, order_id=order_id)
        ]
        failed = next((item for item in histories if _trigger_failed(item)), None)
        if failed is not None:
            row.status = "stop_trigger_failed" if isinstance(row, PositionProtectionLedger) else "failed"
            row.updated_at = observed_at
            created += _incident(
                session, row=row, incident_type="stop_trigger_failed",
                evidence={"order_id": order_id, "exchange": _redacted_exchange_evidence(failed)}, observed_at=observed_at,
            )
            continue
        pending = [
            item
            for item in pending_orders
            if _matches_owned_order(item, pos_id=pos_id, order_id=order_id)
        ]
        if pending:
            row.status = "verified" if isinstance(row, PositionProtectionLedger) else "active"
            row.updated_at = observed_at
            continue
        conflicts = [
            item
            for item in pending_orders
            if _has_explicit_position_conflict(item, pos_id=pos_id, order_id=order_id)
        ]
        if conflicts:
            row.status = "protection_missing" if isinstance(row, PositionProtectionLedger) else "missing"
            row.updated_at = observed_at
            created += _incident(
                session,
                row=row,
                incident_type="protection_position_conflict",
                evidence={
                    "order_id": order_id,
                    "exchange": _redacted_exchange_evidence(conflicts[0]),
                },
                observed_at=observed_at,
            )
            continue
        if order_id and not any(_successful_close(item) for item in histories):
            row.status = "protection_missing" if isinstance(row, PositionProtectionLedger) else "missing"
            row.updated_at = observed_at
            created += _incident(
                session, row=row, incident_type="protection_missing",
                evidence={"order_id": order_id}, observed_at=observed_at,
            )
    return created


def _incident(session, *, row, incident_type, evidence, observed_at) -> int:
    fingerprint = hashlib.sha256(json.dumps({
        "venue": str(row.venue), "pos_id": str(row.pos_id), "order_id": str(getattr(row, "order_id", "") or ""),
        "incident_type": incident_type, "evidence": evidence,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if session.query(PositionProtectionIncident.id).filter(
        PositionProtectionIncident.fingerprint == fingerprint
    ).first() is not None:
        return 0
    session.add(PositionProtectionIncident(
        venue=str(row.venue), execution_binding_id=int(row.execution_binding_id),
        execution_order_leg_id=int(row.execution_order_leg_id), pos_id=str(row.pos_id),
        incident_type=incident_type, fingerprint=fingerprint,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        delivery_status="pending", created_at=observed_at, updated_at=observed_at,
    ))
    return 1


def _matches_owned_order(row: dict[str, Any], *, pos_id: str, order_id: str) -> bool:
    """Match an already-persisted exact order, allowing omitted exchange position IDs."""

    if not order_id or _text(row, "ordId", "orderId", "order_id") != order_id:
        return False
    exchange_pos_id = _text(row, "closePosId", "posId", "pos_id", "positionId")
    return not exchange_pos_id or exchange_pos_id == pos_id


def _has_explicit_position_conflict(
    row: dict[str, Any], *, pos_id: str, order_id: str
) -> bool:
    if not order_id or _text(row, "ordId", "orderId", "order_id") != order_id:
        return False
    exchange_pos_id = _text(row, "closePosId", "posId", "pos_id", "positionId")
    return bool(exchange_pos_id and exchange_pos_id != pos_id)


def _trigger_failed(row: dict[str, Any]) -> bool:
    trigger_time = _text(row, "triggerTime", "trigger_time")
    code = _text(row, "errorCode", "error_code", "sCode")
    return bool(trigger_time and trigger_time not in {"0", "0.0"} and code and code not in {"0", "00000"})


def _successful_close(row: dict[str, Any]) -> bool:
    return not _trigger_failed(row) and str(row.get("state") or "").lower() in {"filled", "closed", "completed"}


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _nonzero_position(row: dict[str, Any]) -> bool:
    try:
        return abs(float(row.get("pos") or row.get("size") or 0)) > 0
    except (TypeError, ValueError):
        return False


def _redacted_exchange_evidence(row: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(row[key]) for key in ("ordId", "triggerTime", "errorCode", "errorMsg")
        if row.get(key) not in (None, "")
    }
