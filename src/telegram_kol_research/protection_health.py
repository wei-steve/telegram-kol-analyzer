"""Fail-closed health classification for exact owned position protections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionHealthObservation
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import PositionProtectionRevision
from telegram_kol_research.models import PositionTakeProfitOrder
from telegram_kol_research.protection_snapshot import build_position_protection_audit
from telegram_kol_research.take_profit_fill_predicate import (
    EVIDENCE_FORM_BY_TIER,
    take_profit_fill_proven,
)


CURRENT_PROTECTION_HEALTH_CLASSIFICATIONS = frozenset(
    {
        "healthy_current_evidence",
        "recovery_required",
        "evidence_insufficient",
    }
)


@dataclass(frozen=True, slots=True)
class CurrentPositionProtectionHealth:
    venue: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    classification: str
    reason_codes: tuple[str, ...]
    evidence_fingerprint: str
    exchange_snapshot_fingerprint: str
    source_incident_ids: tuple[int, ...]
    primary_order_id: str | None = None
    backup_order_id: str | None = None
    take_profit_order_ids: tuple[str, ...] = ()


def classify_current_position_protection_health(
    session: Session,
    *,
    venue: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    position: dict[str, Any] | None,
    open_positions: list[dict[str, Any]],
    pending_trigger_orders: list[dict[str, Any]],
    pending_tpsl_observations: list[dict[str, Any]],
    snapshot_errors: dict[str, Any],
    account_ownership: Any,
    exchange_snapshot_fingerprint: str,
    source_incident_ids: tuple[int, ...] = (),
) -> CurrentPositionProtectionHealth:
    """Classify one exact position from a single coherent exchange snapshot."""

    normalized_venue = str(venue or "deepcoin").lower()
    clean_pos_id = str(pos_id or "").strip()
    scope = {
        "venue": normalized_venue,
        "execution_binding_id": int(execution_binding_id),
        "execution_order_leg_id": int(execution_order_leg_id),
        "raw_pos_id": clean_pos_id,
        "pos_id_ref": _hash_ref("position", clean_pos_id),
        "exchange_snapshot_fingerprint": str(exchange_snapshot_fingerprint),
    }
    if snapshot_errors:
        return _health_result(
            scope=scope,
            classification="evidence_insufficient",
            reason_codes=("exchange_snapshot_error",),
            source_incident_ids=source_incident_ids,
        )
    if position is None or not clean_pos_id:
        return _health_result(
            scope=scope,
            classification="evidence_insufficient",
            reason_codes=("target_live_position_unavailable",),
            source_incident_ids=source_incident_ids,
        )
    if _text(position, "posId", "pos_id", "id") != clean_pos_id:
        return _health_result(
            scope=scope,
            classification="evidence_insufficient",
            reason_codes=("target_position_identity_conflict",),
            source_incident_ids=source_incident_ids,
        )
    instrument_id = _text(position, "instId", "inst_id").upper()
    complete = any(
        str(row.get("instrument_id") or "").upper() == instrument_id
        and row.get("complete") is True
        for row in pending_tpsl_observations
        if isinstance(row, dict)
    )
    if not instrument_id or not complete:
        return _health_result(
            scope=scope,
            classification="evidence_insufficient",
            reason_codes=("target_protection_snapshot_incomplete",),
            source_incident_ids=source_incident_ids,
        )

    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == normalized_venue,
            PositionProtectionLedger.execution_binding_id
            == int(execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionProtectionLedger.pos_id == clean_pos_id,
        )
        .all()
    )
    backup_rows = (
        session.query(PositionBackupStopOrder)
        .filter(
            PositionBackupStopOrder.venue == normalized_venue,
            PositionBackupStopOrder.execution_binding_id
            == int(execution_binding_id),
            PositionBackupStopOrder.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionBackupStopOrder.pos_id == clean_pos_id,
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
        return _health_result(
            scope=scope,
            classification="recovery_required",
            reason_codes=("verified_backup_stop_missing",),
            source_incident_ids=source_incident_ids,
        )
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
            PositionTakeProfitOrder.venue == normalized_venue,
            PositionTakeProfitOrder.execution_binding_id
            == int(execution_binding_id),
            PositionTakeProfitOrder.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionTakeProfitOrder.pos_id == clean_pos_id,
        )
        .all()
    )
    revisions = (
        session.query(PositionProtectionRevision)
        .filter(
            PositionProtectionRevision.venue == normalized_venue,
            PositionProtectionRevision.execution_binding_id
            == int(execution_binding_id),
            PositionProtectionRevision.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionProtectionRevision.pos_id == clean_pos_id,
        )
        .all()
    )
    audit = build_position_protection_audit(
        position=position,
        protection_ledger=primary_ledger_rows,
        backup_stops=backup_rows,
        take_profit_orders=take_profit_rows,
        pending_trigger_orders=pending_trigger_orders,
        freeze_reasons=(),
        protection_revisions=revisions,
        open_positions=open_positions,
        account_ownership=account_ownership,
    )
    primary_order_id = str(audit["primary_stop"].get("order_id") or "")
    selected_backup_id = str(audit["backup_stop"].get("order_id") or "")
    take_profit_order_ids = tuple(
        sorted(
            str(row.get("order_id") or "")
            for row in audit.get("take_profits", [])
            if str(row.get("order_id") or "")
        )
    )
    selected_order_ids = {
        primary_order_id,
        selected_backup_id,
        *take_profit_order_ids,
    }
    selected_order_ids.discard("")
    owners_are_exact = bool(selected_order_ids) and all(
        (owner := account_ownership.owner_for_order(order_id)) is not None
        and owner.pos_id == clean_pos_id
        for order_id in selected_order_ids
    )
    position_side = _text(position, "posSide", "pos_side", "side").lower()
    current_ledger_by_order = {
        str(row.order_id): row
        for row in ledger_rows
        if str(row.status or "").lower() in {"verified", "protected"}
        and str(row.order_id or "").strip()
    }
    local_rows_match = bool(position_side) and all(
        (row := current_ledger_by_order.get(order_id)) is not None
        and str(row.instrument_id or "").upper() == instrument_id
        and str(row.side or "").lower() == position_side
        for order_id in selected_order_ids
    )
    backup_row_matches = bool(
        str(active_backups[0].instrument_id or "").upper() == instrument_id
        and str(active_backups[0].side or "").lower() == position_side
    )
    reasons = set(str(reason) for reason in audit.get("freeze_reasons", []))
    if int(audit.get("verified_take_profit_count") or 0) < 1:
        reasons.add("verified_take_profit_missing")
    if not owners_are_exact:
        reasons.add("protection_ownership_conflict")
    if not local_rows_match or not backup_row_matches:
        reasons.add("protection_scope_mismatch")
    if not primary_order_id:
        reasons.add("verified_primary_stop_missing")
    if selected_backup_id != backup_order_id:
        reasons.add("verified_backup_stop_missing")
    if primary_order_id and primary_order_id == selected_backup_id:
        reasons.add("protection_role_identity_conflict")
    healthy = bool(
        audit.get("protected") is True
        and int(audit.get("verified_take_profit_count") or 0) >= 1
        and primary_order_id
        and selected_backup_id == backup_order_id
        and primary_order_id != selected_backup_id
        and owners_are_exact
        and local_rows_match
        and backup_row_matches
    )
    return _health_result(
        scope=scope,
        classification=(
            "healthy_current_evidence" if healthy else "recovery_required"
        ),
        reason_codes=() if healthy else tuple(sorted(reasons)),
        source_incident_ids=source_incident_ids,
        primary_order_id=primary_order_id or None,
        backup_order_id=selected_backup_id or None,
        take_profit_order_ids=take_profit_order_ids,
    )


def record_position_protection_health_observation(
    session: Session,
    *,
    result: CurrentPositionProtectionHealth,
    observed_at: datetime,
) -> PositionProtectionHealthObservation:
    """Append one bounded observation without changing its source evidence."""

    summary = {
        "reason_codes": list(result.reason_codes),
        "primary_order_ref": _hash_ref("order", result.primary_order_id),
        "backup_order_ref": _hash_ref("order", result.backup_order_id),
        "take_profit_order_refs": [
            _hash_ref("order", order_id)
            for order_id in result.take_profit_order_ids
        ],
    }
    row = PositionProtectionHealthObservation(
        venue=result.venue,
        execution_binding_id=result.execution_binding_id,
        execution_order_leg_id=result.execution_order_leg_id,
        pos_id=result.pos_id,
        classification=result.classification,
        evidence_fingerprint=result.evidence_fingerprint,
        exchange_snapshot_fingerprint=result.exchange_snapshot_fingerprint,
        source_incident_ids_json=json.dumps(
            list(result.source_incident_ids), separators=(",", ":")
        ),
        summary_json=json.dumps(summary, sort_keys=True, separators=(",", ":")),
        observed_at=observed_at,
        created_at=observed_at,
    )
    session.add(row)
    session.flush()
    return row


def _health_result(
    *,
    scope: dict[str, Any],
    classification: str,
    reason_codes: tuple[str, ...],
    source_incident_ids: tuple[int, ...],
    primary_order_id: str | None = None,
    backup_order_id: str | None = None,
    take_profit_order_ids: tuple[str, ...] = (),
) -> CurrentPositionProtectionHealth:
    if classification not in CURRENT_PROTECTION_HEALTH_CLASSIFICATIONS:
        raise ValueError("current protection health classification is invalid")
    normalized_incident_ids = tuple(
        sorted({int(value) for value in source_incident_ids})
    )[:100]
    fingerprint_payload = {
        "venue": scope["venue"],
        "execution_binding_id": scope["execution_binding_id"],
        "execution_order_leg_id": scope["execution_order_leg_id"],
        "pos_id_ref": scope["pos_id_ref"],
        "exchange_snapshot_fingerprint": scope[
            "exchange_snapshot_fingerprint"
        ],
        "classification": classification,
        "reason_codes": list(reason_codes),
        "source_incident_ids": list(normalized_incident_ids),
        "primary_order_ref": _hash_ref("order", primary_order_id),
        "backup_order_ref": _hash_ref("order", backup_order_id),
        "take_profit_order_refs": [
            _hash_ref("order", order_id) for order_id in take_profit_order_ids
        ],
    }
    return CurrentPositionProtectionHealth(
        venue=str(scope["venue"]),
        execution_binding_id=int(scope["execution_binding_id"]),
        execution_order_leg_id=int(scope["execution_order_leg_id"]),
        pos_id=str(scope.get("raw_pos_id") or ""),
        classification=classification,
        reason_codes=tuple(reason_codes),
        evidence_fingerprint=hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        exchange_snapshot_fingerprint=str(
            scope["exchange_snapshot_fingerprint"]
        ),
        source_incident_ids=normalized_incident_ids,
        primary_order_id=primary_order_id,
        backup_order_id=backup_order_id,
        take_profit_order_ids=tuple(take_profit_order_ids),
    )


def _hash_ref(kind: str, value: object) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def current_protection_incident_health_status(
    session: Session,
    *,
    incident: PositionProtectionIncident,
) -> str:
    """Resolve an old incident only from a newer, exact, complete projection."""

    if str(incident.incident_type) in {
        "native_stop_visible_ownership_unverified",
        "native_stop_ownership_management_blocked",
    }:
        recovered = (
            session.query(PositionProtectionIncident)
            .filter(
                PositionProtectionIncident.venue == str(incident.venue).lower(),
                PositionProtectionIncident.execution_binding_id
                == int(incident.execution_binding_id),
                PositionProtectionIncident.execution_order_leg_id
                == int(incident.execution_order_leg_id),
                PositionProtectionIncident.pos_id == str(incident.pos_id),
                PositionProtectionIncident.incident_type == "ownership_recovered",
                PositionProtectionIncident.created_at > incident.created_at,
            )
            .order_by(PositionProtectionIncident.created_at.desc())
            .first()
        )
        if recovered is not None:
            try:
                recovered_evidence = json.loads(recovered.evidence_json)
            except (TypeError, ValueError):
                recovered_evidence = {}
            recovered_order_id = str(
                recovered_evidence.get("order_id") or ""
            ).strip()
            verified = (
                session.query(PositionProtectionLedger.id)
                .filter(
                    PositionProtectionLedger.venue == str(incident.venue).lower(),
                    PositionProtectionLedger.execution_binding_id
                    == int(incident.execution_binding_id),
                    PositionProtectionLedger.execution_order_leg_id
                    == int(incident.execution_order_leg_id),
                    PositionProtectionLedger.pos_id == str(incident.pos_id),
                    PositionProtectionLedger.order_id == recovered_order_id,
                    PositionProtectionLedger.status == "verified",
                )
                .first()
            )
            if recovered_order_id and verified is not None:
                return "resolved_by_verified_attribution"

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


#: How long a live position may hold a single stop before a person is told.
#: The reconcile loop runs every 30 seconds and the backup-stop executor gets
#: its chance on each pass, so ten minutes is twenty missed opportunities --
#: comfortably past "it is being worked on" and well short of the 16.5 hours a
#: position actually went uncovered on 2026-09-23 without anyone being told.
SINGLE_STOP_ALERT_AFTER_SECONDS = 600

#: Ledger purposes that count as a stop for coverage. Take-profit rows are not
#: protection against the loss side and must never make a position look covered.
_STOP_COVERAGE_PURPOSES = frozenset({"stop_loss", "backup_stop"})


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _report_single_stop_coverage(
    session: Session,
    *,
    live_ids: set[str],
    ledgers: list[Any],
    backups: list[Any],
    observed_at: datetime,
) -> int:
    """Tell a person when a live position has been holding one stop too long.

    The 2026-07-26 design requires a verified second stop on every filled auto
    entry leg and keeps no feature flag for it, but nothing ever checked that
    the requirement was being met. When it silently stopped being met, the gap
    ran for over two weeks and was found by a person looking at a page.

    Read-only: it records an incident and submits nothing. A position with *no*
    stop at all is deliberately left to ``protection_missing`` -- that is a
    different, louder fact and reporting it twice under two names would make
    both harder to act on.
    """

    stops_by_pos: dict[str, list[Any]] = {}
    for row in ledgers:
        if str(getattr(row, "status", "") or "").lower() not in {
            "verified",
            "protected",
        }:
            continue
        if str(getattr(row, "purpose", "") or "").lower() not in _STOP_COVERAGE_PURPOSES:
            continue
        stops_by_pos.setdefault(str(row.pos_id), []).append(row)
    backups_by_pos: dict[str, list[Any]] = {}
    for row in backups:
        backups_by_pos.setdefault(str(row.pos_id), []).append(row)

    deadline = _naive_utc(observed_at)
    created = 0
    for pos_id in sorted(live_ids):
        stops = stops_by_pos.get(pos_id, [])
        if not stops:
            # No stop at all -- `protection_missing` owns this case.
            continue
        if len(stops) + len(backups_by_pos.get(pos_id, [])) >= 2:
            continue
        oldest = min(
            stops,
            key=lambda row: _naive_utc(getattr(row, "first_seen_at", None))
            or deadline,
        )
        since = _naive_utc(getattr(oldest, "first_seen_at", None))
        if since is None or deadline is None:
            continue
        if (deadline - since).total_seconds() < SINGLE_STOP_ALERT_AFTER_SECONDS:
            continue
        created += _incident(
            session,
            row=oldest,
            incident_type="single_stop_coverage",
            # Deliberately without an age: the fingerprint covers the evidence,
            # so a value that changes every pass would file a fresh incident
            # every thirty seconds instead of one that stays reported.
            evidence={
                "order_id": str(getattr(oldest, "order_id", "") or ""),
                "primary_stop": str(getattr(oldest, "trigger_price", "") or ""),
                "stop_count": len(stops) + len(backups_by_pos.get(pos_id, [])),
                "since": since.isoformat(),
            },
            observed_at=observed_at,
        )
    return created


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
    created = _report_single_stop_coverage(
        session,
        live_ids=live_ids,
        ledgers=ledgers,
        backups=backups,
        observed_at=observed_at,
    )
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
        if not order_id or any(_successful_close(item) for item in histories):
            continue
        filled = _proven_filled_take_profit(
            session, row=row, order_id=order_id, histories=histories
        )
        if filled is not None:
            # A take profit that actually traded is not missing protection.
            # ``trigger-orders-history`` has no ``state`` field, so
            # ``_successful_close`` could never see one and every filled take
            # profit was recorded as ``protection_missing`` with a critical
            # incident beside it (production leg 579, eight seconds after TP1
            # filled). ``filled`` is terminal: it is in no reader's active set,
            # so nothing tries to cancel or replace it afterwards.
            record_take_profit_ledger_fill(
                row,
                order_id=order_id,
                evidence={
                    "evidence_tier": filled.evidence_tier,
                    "evidence_form": EVIDENCE_FORM_BY_TIER.get(
                        filled.evidence_tier or ""
                    ),
                    "evidence": dict(filled.evidence),
                },
                observed_at=observed_at,
            )
            continue
        row.status = "protection_missing" if isinstance(row, PositionProtectionLedger) else "missing"
        row.updated_at = observed_at
        created += _incident(
            session, row=row, incident_type="protection_missing",
            evidence={"order_id": order_id}, observed_at=observed_at,
        )
    return created


def record_take_profit_ledger_fill(
    row: PositionProtectionLedger,
    *,
    order_id: str,
    evidence: dict[str, Any],
    observed_at: datetime,
) -> None:
    """Mark one take-profit ledger row as filled.  **The only writer of this.**

    Two callers reach the same conclusion by different routes -- this module's
    reconcile round, which sees one order's history, and
    ``stop_ladder_records.reconcile_take_profit_fill_levels``, which knows
    where that order sits in the position's ladder -- and both share one
    predicate (:mod:`take_profit_fill_predicate`) and one write.  Two writers
    with two spellings of "filled" would be two sets of rules for the same
    word, and the ladder derives its level from whatever is written here.

    ``filled`` is a terminal status in every reader's eyes: the audit in
    ``docs/composite-upstream-fix-status.md`` section 3 checked each
    ``status.in_(...)`` and none of the active sets contains it, so a row that
    filled stops being cancelled, replaced or re-verified.
    """

    row.status = "filled"
    row.updated_at = observed_at
    row.evidence_json = _merged_evidence(
        row.evidence_json,
        take_profit_fill={"order_id": str(order_id), **dict(evidence)},
    )


def _proven_filled_take_profit(session, *, row, order_id, histories):
    """The verdict for a take-profit ledger row that filled, or ``None``.

    Only ``position_protection_ledger`` take-profit rows are considered. A
    stop that triggered closes the position, and a closed position has already
    left ``live_ids`` before this function can be reached.
    """

    if not isinstance(row, PositionProtectionLedger):
        return None
    if str(row.purpose or "").lower() not in {"take_profit", "tp", "profit"}:
        return None
    recorded = [
        str(value[0] or "")
        for value in session.query(PositionTakeProfitOrder.status)
        .filter(PositionTakeProfitOrder.venue == str(row.venue or "deepcoin").lower())
        .filter(PositionTakeProfitOrder.order_id == order_id)
        .all()
    ]
    verdict = take_profit_fill_proven(
        order_id=order_id,
        recorded_order_statuses=recorded,
        trigger_history=histories,
        # Reaching here means ``snapshot_errors`` was empty, so both the
        # pending and the history reads for this round succeeded.
        pending_snapshot_complete=True,
    )
    return verdict if verdict.proven else None


def _merged_evidence(existing: str | None, **added: Any) -> str:
    try:
        loaded = json.loads(existing or "{}")
    except (TypeError, ValueError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    loaded.update(added)
    return json.dumps(loaded, ensure_ascii=False, sort_keys=True)


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
