"""Bounded, read-only classification of historical protection incidents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionProtectionRevision,
    PositionTakeProfitOrder,
)
from telegram_kol_research.protection_ledger import (
    load_account_protection_ownership,
)
from telegram_kol_research.protection_health import (
    current_protection_incident_health_status,
)
from telegram_kol_research.protection_snapshot import (
    build_position_protection_audit,
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
    )

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
        current_scope_cache: dict[tuple[str, int, int, str], bool] = {}
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
    current_scope_cache: dict[tuple[str, int, int, str], bool],
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
        ) or _current_protection_visible_on_exchange(
            session,
            incident=incident,
            position=live_positions[str(incident.pos_id)],
            positions=positions,
            pending=pending,
            complete_instruments=complete_instruments,
            account_ownership=account_ownership,
            cache=current_scope_cache,
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


def _current_protection_visible_on_exchange(
    session,
    *,
    incident: PositionProtectionIncident,
    position: dict[str, Any],
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    complete_instruments: set[str],
    account_ownership: Any,
    cache: dict[tuple[str, int, int, str], bool],
) -> bool:
    """Prove exact current protection without rewriting historical evidence."""

    venue = str(incident.venue or "deepcoin").lower()
    binding_id = int(incident.execution_binding_id)
    leg_id = int(incident.execution_order_leg_id)
    pos_id = str(incident.pos_id)
    scope = (venue, binding_id, leg_id, pos_id)
    if scope in cache:
        return cache[scope]

    instrument_id = _text(position, "instId", "inst_id").upper()
    if not instrument_id or instrument_id not in complete_instruments:
        cache[scope] = False
        return False

    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == venue,
            PositionProtectionLedger.execution_binding_id == binding_id,
            PositionProtectionLedger.execution_order_leg_id == leg_id,
            PositionProtectionLedger.pos_id == pos_id,
        )
        .all()
    )
    backup_rows = (
        session.query(PositionBackupStopOrder)
        .filter(
            PositionBackupStopOrder.venue == venue,
            PositionBackupStopOrder.execution_binding_id == binding_id,
            PositionBackupStopOrder.execution_order_leg_id == leg_id,
            PositionBackupStopOrder.pos_id == pos_id,
        )
        .all()
    )
    active_backups = [
        row
        for row in backup_rows
        if str(row.status or "").lower() == "active"
        and str(row.order_id or "").strip()
    ]
    if len(active_backups) != 1:
        cache[scope] = False
        return False
    backup_order_id = str(active_backups[0].order_id)
    primary_ledger_rows = [
        row
        for row in ledger_rows
        if not (
            str(row.purpose or "").lower() == "stop_loss"
            and str(row.order_id or "") == backup_order_id
        )
    ]
    take_profit_rows = (
        session.query(PositionTakeProfitOrder)
        .filter(
            PositionTakeProfitOrder.venue == venue,
            PositionTakeProfitOrder.execution_binding_id == binding_id,
            PositionTakeProfitOrder.execution_order_leg_id == leg_id,
            PositionTakeProfitOrder.pos_id == pos_id,
        )
        .all()
    )
    revisions = (
        session.query(PositionProtectionRevision)
        .filter(
            PositionProtectionRevision.venue == venue,
            PositionProtectionRevision.execution_binding_id == binding_id,
            PositionProtectionRevision.execution_order_leg_id == leg_id,
            PositionProtectionRevision.pos_id == pos_id,
        )
        .all()
    )
    audit = build_position_protection_audit(
        position=position,
        protection_ledger=primary_ledger_rows,
        backup_stops=backup_rows,
        take_profit_orders=take_profit_rows,
        pending_trigger_orders=pending,
        freeze_reasons=(),
        protection_revisions=revisions,
        open_positions=positions,
        account_ownership=account_ownership,
    )
    primary_order_id = str(audit["primary_stop"].get("order_id") or "")
    selected_backup_id = str(audit["backup_stop"].get("order_id") or "")
    selected_order_ids = {
        primary_order_id,
        selected_backup_id,
        *(
            str(row.get("order_id") or "")
            for row in audit.get("take_profits", [])
        ),
    }
    selected_order_ids.discard("")
    owners_are_exact = bool(selected_order_ids) and all(
        (owner := account_ownership.owner_for_order(order_id)) is not None
        and owner.pos_id == pos_id
        for order_id in selected_order_ids
    )
    result = bool(
        audit.get("protected") is True
        and int(audit.get("verified_take_profit_count") or 0) >= 1
        and primary_order_id
        and selected_backup_id == backup_order_id
        and primary_order_id != selected_backup_id
        and owners_are_exact
    )
    cache[scope] = result
    return result


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
