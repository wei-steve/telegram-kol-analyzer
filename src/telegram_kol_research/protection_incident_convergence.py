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
    PositionProtectionLedger,
    PositionProtectionRevision,
)
from telegram_kol_research.protection_ledger import (
    load_account_protection_ownership,
)
from telegram_kol_research.protection_health import (
    classify_current_position_protection_health,
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
    pending_observations = [
        row
        for row in list(getattr(snapshot, "pending_tpsl_observations", []) or [])
        if isinstance(row, dict)
    ]
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
    live_positions = {
        pos_id: row
        for row in positions
        if _position_is_live(row) and (pos_id := _text(row, "posId", "pos_id"))
    }
    complete_instruments = {
        str(row.get("instrument_id") or "").upper()
        for row in pending_observations
        if row.get("complete") is True
        and str(row.get("instrument_id") or "").strip()
    }
    exchange_complete = (
        not snapshot_errors
        and all(row.get("complete") is True for row in pending_observations)
        and (not live_pos_ids or bool(pending_observations))
        and all(
            (instrument_id := _text(row, "instId", "inst_id").upper())
            and instrument_id in complete_instruments
            for row in live_positions.values()
        )
    )
    exchange_snapshot_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "positions": positions,
                "pending_trigger_orders": pending,
                "pending_tpsl_observations": pending_observations,
                "errors": snapshot_errors,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    counts: Counter[str] = Counter()
    buckets: dict[str, list[dict[str, str]]] = {
        name: [] for name in PROTECTION_INCIDENT_CLASSIFICATIONS
    }
    incident_total = 0
    with session_factory() as session:
        account_ownership = load_account_protection_ownership(
            session,
            live_pos_ids=live_pos_ids,
        )
        current_scope_cache: dict[tuple[str, int, int, str], str] = {}
        incidents = (
            session.query(PositionProtectionIncident)
            .order_by(PositionProtectionIncident.created_at, PositionProtectionIncident.id)
            .yield_per(100)
        )
        for incident in incidents:
            classification = _classify_incident(
                session,
                incident=incident,
                live_pos_ids=live_pos_ids,
                pending=pending,
                pending_observations=pending_observations,
                exchange_complete=exchange_complete,
                live_positions=live_positions,
                positions=positions,
                complete_instruments=complete_instruments,
                account_ownership=account_ownership,
                current_scope_cache=current_scope_cache,
                exchange_snapshot_fingerprint=exchange_snapshot_fingerprint,
            )
            incident_total += 1
            counts[classification] += 1
            bucket = buckets[classification]
            if len(bucket) < bounded_limit:
                bucket.append(
                    {
                        "incident_ref": _ref("incident", incident.id),
                        "position_ref": _ref("position", incident.pos_id),
                        "classification": classification,
                        "incident_type_ref": _ref("type", incident.incident_type),
                    }
                )

    returned = [
        item
        for name in PROTECTION_INCIDENT_CLASSIFICATIONS
        for item in buckets[name]
    ][:bounded_limit]
    truncated = incident_total > bounded_limit
    return {
        "schema_version": 1,
        "mode": "read_only",
        "limit": bounded_limit,
        "counts": {
            name: int(counts.get(name, 0))
            for name in PROTECTION_INCIDENT_CLASSIFICATIONS
        },
        "incident_total": incident_total,
        "incidents_returned": len(returned),
        "incidents_truncated": truncated,
        "exchange_snapshot_complete": exchange_complete,
        "database_evidence_stable": bool(database_evidence_stable),
        "output_complete": (
            exchange_complete and bool(database_evidence_stable) and not truncated
        ),
        "incidents": returned,
    }


def _classify_incident(
    session,
    *,
    incident: PositionProtectionIncident,
    live_pos_ids: set[str],
    pending: list[dict[str, Any]],
    pending_observations: list[dict[str, Any]],
    exchange_complete: bool,
    live_positions: dict[str, dict[str, Any]],
    positions: list[dict[str, Any]],
    complete_instruments: set[str],
    account_ownership: Any,
    current_scope_cache: dict[tuple[str, int, int, str], str],
    exchange_snapshot_fingerprint: str,
) -> str:
    if not exchange_complete or not str(incident.pos_id or "").strip():
        return "evidence_insufficient"
    if str(incident.pos_id) in live_pos_ids:
        if (
            current_protection_incident_health_status(session, incident=incident)
            == "resolved_by_verified_replacement"
            and _replacement_visible_on_exchange(
                session,
                incident=incident,
                pending=pending,
                pending_observations=pending_observations,
            )
        ) or _current_health_classification(
            session,
            incident=incident,
            position=live_positions[str(incident.pos_id)],
            positions=positions,
            pending=pending,
            pending_observations=pending_observations,
            snapshot_errors={},
            account_ownership=account_ownership,
            exchange_snapshot_fingerprint=exchange_snapshot_fingerprint,
            cache=current_scope_cache,
        ) == "healthy_current_evidence":
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


def _current_health_classification(
    session,
    *,
    incident: PositionProtectionIncident,
    position: dict[str, Any],
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    pending_observations: list[dict[str, Any]],
    snapshot_errors: dict[str, Any],
    account_ownership: Any,
    exchange_snapshot_fingerprint: str,
    cache: dict[tuple[str, int, int, str], str],
) -> str:
    """Reuse one exact current-health decision for all incidents in a scope."""

    venue = str(incident.venue or "deepcoin").lower()
    binding_id = int(incident.execution_binding_id)
    leg_id = int(incident.execution_order_leg_id)
    pos_id = str(incident.pos_id)
    scope = (venue, binding_id, leg_id, pos_id)
    if scope in cache:
        return cache[scope]
    result = classify_current_position_protection_health(
        session,
        venue=venue,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id=pos_id,
        position=position,
        open_positions=positions,
        pending_trigger_orders=pending,
        pending_tpsl_observations=pending_observations,
        snapshot_errors=snapshot_errors,
        account_ownership=account_ownership,
        exchange_snapshot_fingerprint=exchange_snapshot_fingerprint,
        source_incident_ids=(int(incident.id),),
    )
    cache[scope] = str(result.classification)
    return cache[scope]


def _replacement_visible_on_exchange(
    session, *, incident, pending, pending_observations
) -> bool:
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
    instruments = {
        str(row.instrument_id)
        for row in session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == str(incident.venue).lower(),
            PositionProtectionLedger.execution_binding_id
            == int(incident.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id
            == int(incident.execution_order_leg_id),
            PositionProtectionLedger.pos_id == str(incident.pos_id),
            PositionProtectionLedger.status == "verified",
            PositionProtectionLedger.order_id.in_(sorted(expected)),
        )
        .all()
    }
    complete_instruments = {
        str(row.get("instrument_id") or "")
        for row in pending_observations
        if row.get("complete") is True
    }
    if len(instruments) != 1 or not instruments.issubset(complete_instruments):
        return False
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
