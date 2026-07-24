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
        .filter(PositionProtectionLedger.status.in_(("verified", "protected")))
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
        histories = [item for item in trigger_history if _matches(item, pos_id=pos_id, order_id=order_id)]
        failed = next((item for item in histories if _trigger_failed(item)), None)
        if failed is not None:
            row.status = "stop_trigger_failed" if isinstance(row, PositionProtectionLedger) else "failed"
            row.updated_at = observed_at
            created += _incident(
                session, row=row, incident_type="stop_trigger_failed",
                evidence={"order_id": order_id, "exchange": _redacted_exchange_evidence(failed)}, observed_at=observed_at,
            )
            continue
        if order_id and not any(_matches(item, pos_id=pos_id, order_id=order_id) for item in pending_orders):
            if not any(_successful_close(item) for item in histories):
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


def _matches(row: dict[str, Any], *, pos_id: str, order_id: str) -> bool:
    return _text(row, "closePosId", "posId", "pos_id", "positionId") == pos_id and (
        not order_id or _text(row, "ordId", "orderId", "order_id") == order_id
    )


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
