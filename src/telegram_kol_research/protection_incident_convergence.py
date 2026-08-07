"""Bounded, read-only classification of historical protection incidents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    PositionProtectionRevision,
)
from telegram_kol_research.protection_health import (
    current_protection_incident_health_status,
)


PROTECTION_INCIDENT_CLASSIFICATIONS = (
    "resolved_by_current_exchange_evidence",
    "current_risk",
    "historical_terminal",
    "evidence_insufficient",
)
_TERMINAL_BINDING_STATES = frozenset(
    {"closed", "cancelled", "completed", "failed", "resolved", "superseded"}
)
_TERMINAL_LEG_STATES = frozenset(
    {"closed", "cancelled", "completed", "failed", "filled", "resolved", "superseded"}
)


def audit_protection_incident_convergence(
    session_factory,
    *,
    snapshot: Any,
    limit: int = 100,
    database_evidence_stable: bool = True,
) -> dict[str, Any]:
    """Classify every incident while returning at most 100 redacted references."""

    bounded_limit = max(1, min(int(limit), 100))
    snapshot_errors = dict(getattr(snapshot, "errors", {}) or {})
    exchange_complete = not snapshot_errors
    positions = [
        row
        for row in list(getattr(snapshot, "positions", []) or [])
        if isinstance(row, dict)
    ]
    pending = [
        row
        for row in list(getattr(snapshot, "pending_trigger_orders", []) or [])
        if isinstance(row, dict)
    ]
    live_pos_ids = {
        pos_id
        for row in positions
        if _position_is_live(row) and (pos_id := _text(row, "posId", "pos_id"))
    }

    with session_factory() as session:
        incidents = (
            session.query(PositionProtectionIncident)
            .order_by(PositionProtectionIncident.created_at, PositionProtectionIncident.id)
            .all()
        )
        classified = [
            (
                _classify_incident(
                    session,
                    incident=incident,
                    live_pos_ids=live_pos_ids,
                    pending=pending,
                    exchange_complete=exchange_complete,
                ),
                incident,
            )
            for incident in incidents
        ]

    counts = Counter(classification for classification, _incident in classified)
    priority = {
        "resolved_by_current_exchange_evidence": 0,
        "current_risk": 1,
        "historical_terminal": 2,
        "evidence_insufficient": 3,
    }
    classified.sort(
        key=lambda item: (
            priority[item[0]],
            item[1].created_at,
            int(item[1].id),
        )
    )
    returned = classified[:bounded_limit]
    truncated = len(classified) > bounded_limit
    return {
        "schema_version": 1,
        "mode": "read_only",
        "limit": bounded_limit,
        "counts": {
            name: int(counts.get(name, 0))
            for name in PROTECTION_INCIDENT_CLASSIFICATIONS
        },
        "incident_total": len(classified),
        "incidents_returned": len(returned),
        "incidents_truncated": truncated,
        "exchange_snapshot_complete": exchange_complete,
        "database_evidence_stable": bool(database_evidence_stable),
        "output_complete": (
            exchange_complete and bool(database_evidence_stable) and not truncated
        ),
        "incidents": [
            {
                "incident_ref": _ref("incident", incident.id),
                "position_ref": _ref("position", incident.pos_id),
                "classification": classification,
                "incident_type": str(incident.incident_type),
            }
            for classification, incident in returned
        ],
    }


def _classify_incident(
    session,
    *,
    incident: PositionProtectionIncident,
    live_pos_ids: set[str],
    pending: list[dict[str, Any]],
    exchange_complete: bool,
) -> str:
    if not exchange_complete or not str(incident.pos_id or "").strip():
        return "evidence_insufficient"
    if str(incident.pos_id) in live_pos_ids:
        if (
            current_protection_incident_health_status(session, incident=incident)
            == "resolved_by_verified_replacement"
            and _replacement_visible_on_exchange(session, incident=incident, pending=pending)
        ):
            return "resolved_by_current_exchange_evidence"
        return "current_risk"

    binding = session.get(ExecutionBinding, int(incident.execution_binding_id))
    leg = session.get(ExecutionOrderLeg, int(incident.execution_order_leg_id))
    if (
        binding is not None
        and leg is not None
        and (
            str(binding.status or "").lower() in _TERMINAL_BINDING_STATES
            or str(leg.status or "").lower() in _TERMINAL_LEG_STATES
        )
    ):
        return "historical_terminal"
    return "evidence_insufficient"


def _replacement_visible_on_exchange(session, *, incident, pending) -> bool:
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
        return False
    try:
        payload = json.loads(revision.protection_json)
    except (TypeError, ValueError):
        return False
    expected = {
        str(value) for value in payload.get("order_ids", []) if str(value or "")
    }
    visible = {
        order_id
        for row in pending
        if _text(row, "posId", "pos_id") == str(incident.pos_id)
        and (order_id := _text(row, "ordId", "orderId", "order_id"))
    }
    return bool(expected) and expected.issubset(visible)


def _position_is_live(row: dict[str, Any]) -> bool:
    try:
        return abs(float(row.get("pos") or row.get("size") or 0)) > 0
    except (TypeError, ValueError):
        return False


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _ref(kind: str, value: Any) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"
