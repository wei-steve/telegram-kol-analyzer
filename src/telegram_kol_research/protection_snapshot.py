"""Redacted completeness evidence for Deepcoin pending-TPSL reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from telegram_kol_research.models import PendingTpslSnapshotObservation
from telegram_kol_research.native_tpsl import (
    NativeTpslExpectation,
    match_native_tpsl_order,
    normalize_native_tpsl,
)
from telegram_kol_research.protection_ledger import AccountProtectionOwnership


_PAGINATION_KEYS = frozenset({"cursor", "nextcursor", "page", "total", "hasmore"})
_CURRENT_LEDGER_STATUSES = frozenset({"active", "pending_readback", "protected", "verified"})
_CURRENT_BACKUP_STATUSES = frozenset(
    {"active", "pending_readback", "submitting", "unknown_exchange_outcome"}
)
_CURRENT_TAKE_PROFIT_STATUSES = frozenset(
    {"active", "cancel_requested", "pending_readback", "submitting"}
)


def build_position_protection_audit(
    *,
    position: dict[str, Any],
    protection_ledger: Iterable[object],
    backup_stops: Iterable[object],
    take_profit_orders: Iterable[object],
    pending_trigger_orders: Iterable[dict[str, Any]],
    freeze_reasons: Iterable[object] = (),
    protection_revisions: Iterable[object] = (),
    open_positions: Iterable[dict[str, Any]] | None = None,
    account_ownership: AccountProtectionOwnership | None = None,
) -> dict[str, Any]:
    """Summarize one position's protection from read-only evidence.

    An API acknowledgement alone is deliberately not verification.  A native
    order is verified only when its pending-TPSL row can be matched back to its
    durable local record.
    """

    pos_id = _text(position.get("posId") or position.get("pos_id") or position.get("id"))
    pending = [row for row in pending_trigger_orders if isinstance(row, dict)]
    scope = list(open_positions) if open_positions is not None else [position]
    ledger_rows = [row for row in protection_ledger if _field(row, "pos_id") == pos_id]
    backup_rows = [row for row in backup_stops if _field(row, "pos_id") == pos_id]
    take_profit_rows = [row for row in take_profit_orders if _field(row, "pos_id") == pos_id]
    current_ledger_rows = [
        row for row in ledger_rows
        if _row_status_is_current(row, _CURRENT_LEDGER_STATUSES)
    ]
    current_backup_rows = [
        row for row in backup_rows
        if _row_status_is_current(row, _CURRENT_BACKUP_STATUSES)
    ]
    terminal_ledger_take_profit_ids = {
        order_id
        for row in ledger_rows
        if _text(_field(row, "purpose")) == "take_profit"
        and not _row_status_is_current(row, _CURRENT_LEDGER_STATUSES)
        and (order_id := _text(_field(row, "order_id")))
    }
    current_take_profit_rows = [
        row for row in take_profit_rows
        if _row_status_is_current(row, _CURRENT_TAKE_PROFIT_STATUSES)
        and _text(_field(row, "order_id")) not in terminal_ledger_take_profit_ids
    ]

    primary_rows = [
        row for row in current_ledger_rows
        if _text(_field(row, "purpose")) in {"stop_loss", "combined"}
    ]
    primary = _primary_stop_summary(position, pending, scope, primary_rows)
    backup = _backup_stop_summary(
        position,
        pending,
        scope,
        ledger_rows=[
            row for row in current_ledger_rows
            if _text(_field(row, "purpose")) == "backup_stop"
        ],
        backup_rows=current_backup_rows,
    )
    take_profits = _take_profit_summaries(
        position,
        pending,
        scope,
        ledger_rows=current_ledger_rows,
        take_profit_rows=current_take_profit_rows,
    )

    known_order_ids = {
        order_id
        for row in [
            *current_ledger_rows,
            *current_backup_rows,
            *current_take_profit_rows,
        ]
        if (order_id := _text(_field(row, "order_id")))
    }
    if account_ownership is None:
        manual_order_ids = sorted(
            {
                order.ord_id
                for raw in pending
                if (order := normalize_native_tpsl(raw))
                and order.ord_id
                and order.ord_id not in known_order_ids
                and _unowned_native_order_can_affect_position(order, position)
            }
        )
        ownership_conflict = False
    else:
        manual_order_ids = sorted(
            {
                order.ord_id
                for raw in pending
                if (order := normalize_native_tpsl(raw))
                and order.ord_id
                and account_ownership.owner_for_order(order.ord_id) is None
                and order.pos_id == pos_id
            }
        )
        ownership_conflict = any(
            pos_id in conflict.pos_ids
            for conflict in account_ownership.conflicts
        )
    active_freeze_reasons = _active_freeze_reasons(
        pos_id=pos_id,
        freeze_reasons=freeze_reasons,
        protection_revisions=protection_revisions,
    )
    reasons = {
        _text(_field(reason, "reason") or _field(reason, "incident_type") or reason)
        for reason in active_freeze_reasons
        if _text(_field(reason, "reason") or _field(reason, "incident_type") or reason)
    }
    if primary["verification_status"] == "none":
        reasons.add("primary_stop_missing")
    elif primary["verification_status"] != "verified":
        reasons.add(f"primary_stop_{primary['verification_status']}")
    if backup["protocol"] == "none":
        reasons.add("backup_stop_missing")
    elif backup["verification_status"] != "verified":
        reasons.add(f"backup_stop_{backup['verification_status']}")
    if any(item["verification_status"] == "submitted_response" for item in take_profits):
        reasons.add("submitted_response_not_verified")
    if any(item["verification_status"] == "ambiguous" for item in take_profits):
        reasons.add("take_profit_ambiguous")
    if manual_order_ids:
        reasons.add("manual_or_unowned_native_tpsl")
    if ownership_conflict:
        reasons.add("protection_ownership_conflict")

    protected = (
        primary["verification_status"] == "verified"
        and backup["protocol"] == "native"
        and backup["verification_status"] == "verified"
        and all(item["verification_status"] == "verified" for item in take_profits)
        and not manual_order_ids
        and not ownership_conflict
    )
    matching_strategies = sorted(
        {
            item["matching_strategy"]
            for item in [primary, backup, *take_profits]
            if item["matching_strategy"] not in {"not_applicable", "none"}
        }
    )
    return {
        "pos_id": pos_id,
        "primary_stop": primary,
        "backup_stop": backup,
        "take_profits": take_profits,
        "matching_strategies": matching_strategies,
        "manual_order_detected": bool(manual_order_ids),
        "manual_order_ids": manual_order_ids,
        "has_verified_stop": primary["verification_status"] == "verified",
        "has_verified_backup_stop": (
            backup["protocol"] == "native"
            and backup["verification_status"] == "verified"
        ),
        "verified_take_profit_count": sum(
            item["verification_status"] == "verified"
            for item in take_profits
        ),
        "has_unowned_orders": bool(manual_order_ids),
        "ownership_conflict": ownership_conflict,
        "readback_complete": all(
            item["verification_status"] == "verified"
            for item in [primary, backup, *take_profits]
        ),
        "automation_safe": protected,
        "freeze_reasons": sorted(reasons),
        "protected": protected,
    }


_TRANSIENT_REPLACEMENT_FREEZE_REASONS = frozenset(
    {"backup_stop_blocked", "protection_missing"}
)


def _active_freeze_reasons(*, pos_id, freeze_reasons, protection_revisions):
    reasons = list(freeze_reasons)
    complete_revisions = []
    for revision in protection_revisions:
        if (
            _text(_field(revision, "pos_id")) != pos_id
            or _text(_field(revision, "status")) != "active"
        ):
            continue
        payload = _json_value(_field(revision, "protection_json"))
        if not {"primary_stop", "backup_stop", "take_profit"}.issubset(
            set(payload.get("roles") or [])
        ):
            continue
        created_at = _field(revision, "created_at")
        if isinstance(created_at, datetime):
            complete_revisions.append(created_at)
    if not complete_revisions:
        return reasons
    newest = max(_utc_naive(value) for value in complete_revisions)
    active = []
    for reason in reasons:
        reason_code = _text(
            _field(reason, "reason") or _field(reason, "incident_type") or reason
        )
        created_at = _field(reason, "created_at")
        recovered = (
            reason_code in _TRANSIENT_REPLACEMENT_FREEZE_REASONS
            and isinstance(created_at, datetime)
            and _utc_naive(created_at) < newest
        )
        if not recovered:
            active.append(reason)
    return active


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _primary_stop_summary(position, pending, scope, rows):
    if not rows:
        return _empty_stop(source="none")
    row = _latest_row(rows)
    source = "entry" if "entry" in (_text(_field(row, "evidence_source")) or "") else "native"
    return {
        "source": source,
        **_native_verification_summary(position, pending, scope, row, purpose="stop_loss"),
    }


def _backup_stop_summary(position, pending, scope, *, ledger_rows, backup_rows):
    rows = ledger_rows or backup_rows
    if not rows:
        return {
            "protocol": "none",
            "verification_status": "none",
            "matching_strategy": "none",
            "order_id": None,
        }
    row = _latest_row(rows)
    request = _json_value(_field(row, "request_json"))
    protocol = (
        "native"
        if _text(_field(row, "purpose")) == "backup_stop"
        or any(key in request for key in ("slTriggerPx", "slTriggerPrice"))
        else "generic"
    )
    if protocol == "generic":
        return {
            "protocol": "generic",
            "verification_status": "unverified_exchange",
            "matching_strategy": "not_applicable",
            "order_id": _text(_field(row, "order_id")),
        }
    return {
        "protocol": "native",
        **_native_verification_summary(position, pending, scope, row, purpose="stop_loss"),
    }


def _take_profit_summaries(position, pending, scope, *, ledger_rows, take_profit_rows):
    rows_by_id: dict[str, object] = {}
    for row in [
        *[
            row for row in ledger_rows
            if _text(_field(row, "purpose")) == "take_profit"
        ],
        *take_profit_rows,
    ]:
        if (order_id := _text(_field(row, "order_id"))):
            rows_by_id[order_id] = row
    return [
        _native_verification_summary(position, pending, scope, row, purpose="take_profit")
        for _, row in sorted(rows_by_id.items())
    ]


def _native_verification_summary(position, pending, scope, row, *, purpose):
    order_id = _text(_field(row, "order_id"))
    trigger_price = _field(row, "trigger_price")
    size_text = _field(row, "size_text")
    evidence_source = _text(_field(row, "evidence_source")) or ""
    actual = next(
        (
            order for raw in pending
            if (order := normalize_native_tpsl(raw)) and order.ord_id == order_id
        ),
        None,
    )
    expected_size = size_text if _decimal(size_text) is not None else (actual.size if actual else None)
    status = "not_read_back"
    if not order_id or _decimal(trigger_price) is None or expected_size is None:
        status = "invalid_local_evidence"
    else:
        try:
            match = match_native_tpsl_order(
                position,
                pending,
                NativeTpslExpectation(
                    purpose=purpose,
                    trigger_price=trigger_price,
                    size=expected_size,
                    ord_id=order_id,
                ),
                open_positions=scope,
            )
            status = match.status
        except (TypeError, ValueError):
            status = "invalid_local_evidence"
    if status == "not_found":
        if _text(_field(row, "status")) == "pending_readback":
            status = "pending_readback"
        elif evidence_source == "tpsl_write_response":
            status = "submitted_response"
        else:
            status = "not_read_back"
    return {
        "order_id": order_id,
        "verification_status": status,
        "matching_strategy": "order_id" if order_id else "none",
    }


def _empty_stop(*, source):
    return {
        "source": source,
        "verification_status": "none",
        "matching_strategy": "none",
        "order_id": None,
    }


def _unowned_native_order_can_affect_position(order, position):
    pos_id = _text(position.get("posId") or position.get("pos_id") or position.get("id"))
    return bool(order.pos_id and order.pos_id == pos_id)


def _latest_row(rows):
    return sorted(rows, key=lambda row: str(_field(row, "id") or ""))[-1]


def _row_status_is_current(row, allowed_statuses) -> bool:
    status = (_text(_field(row, "status")) or "").lower()
    return not status or status in allowed_statuses


def _field(value, name):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _text(value):
    return str(value).strip() if value not in (None, "") else None


def _json_value(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def observe_pending_tpsl(
    *,
    instrument_id: str,
    response: dict[str, Any],
    expected_order_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Return safe evidence; never infer that an incomplete list means absent TPSL."""

    normalized_keys = {str(key).lower() for key in response}
    unknown_pagination = bool(normalized_keys.intersection(_PAGINATION_KEYS))
    data = response.get("data")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return {
            "instrument_id": str(instrument_id).upper(),
            "complete": False,
            "reason": "invalid_pending_tpsl_schema",
            "order_ids": [],
            "expected_order_ids_visible": False,
        }
    order_ids = sorted({str(row.get("ordId") or row.get("orderId") or "").strip() for row in data} - {""})
    expected = {str(value).strip() for value in expected_order_ids if str(value).strip()}
    return {
        "instrument_id": str(instrument_id).upper(),
        "complete": not unknown_pagination,
        "reason": "pagination_metadata_unsupported" if unknown_pagination else None,
        "response_count": len(data),
        "order_ids": order_ids,
        "expected_order_ids_visible": expected.issubset(set(order_ids)) and not unknown_pagination,
    }


def record_pending_tpsl_observation(
    session_factory,
    *,
    observation: dict[str, Any],
    venue: str = "deepcoin",
) -> int:
    """Append a redacted pending-TPSL observation for later recovery audit."""

    order_ids = observation.get("order_ids")
    if not isinstance(order_ids, list):
        order_ids = []
    response_count = observation.get("response_count")
    with session_factory() as session:
        row = PendingTpslSnapshotObservation(
            venue=str(venue).lower(),
            instrument_id=str(observation.get("instrument_id") or "").upper(),
            response_count=(int(response_count) if isinstance(response_count, int) else None),
            order_ids_json=json.dumps(
                sorted({str(value) for value in order_ids if str(value)}),
                ensure_ascii=False,
            ),
            complete=bool(observation.get("complete")),
            reason=(str(observation["reason"]) if observation.get("reason") else None),
        )
        session.add(row)
        session.commit()
        return int(row.id)
