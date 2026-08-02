"""Durable, bounded observations of exact Deepcoin positions and TPSL rows."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy.orm import Session

from telegram_kol_research.models import PositionReconciliationObservation


def build_position_observation_payload(
    *,
    position: dict[str, Any],
    pending_tpsl: Iterable[dict[str, Any]],
    complete: bool,
) -> dict[str, Any]:
    """Return a stable payload containing only evidence needed for reconciliation."""

    pos_id = _required_string(position, "posId", "pos_id", "id")
    instrument_id = _required_string(position, "instId", "inst_id")
    side = _required_string(position, "posSide", "pos_side", "side").lower()
    size_text = _decimal_text(_required_string(position, "pos", "size"))
    avg_entry_price = _optional_decimal_text(
        _first_string(position, "avgPx", "avg_px", "openAvgPx", "open_avg_px")
    )
    normalized_orders = []
    for row in pending_tpsl:
        order_pos_id = _first_string(row, "posId", "pos_id", "closePosId")
        if order_pos_id and order_pos_id != pos_id:
            continue
        order_id = _first_string(row, "ordId", "orderId", "order_id", "id")
        if not order_id:
            continue
        normalized_orders.append(
            {
                "order_id": order_id,
                "pos_id": order_pos_id,
                "side": _optional_lower_string(row, "side"),
                "position_side": _optional_lower_string(
                    row, "posSide", "pos_side"
                ),
                "size_text": _optional_decimal_text(
                    _first_string(row, "sz", "size")
                ),
                "trigger_price": _optional_decimal_text(
                    _first_string(
                        row,
                        "triggerPx",
                        "trigger_px",
                        "tpTriggerPx",
                        "slTriggerPx",
                    )
                ),
            }
        )
    normalized_orders.sort(
        key=lambda item: (
            item["order_id"],
            item["trigger_price"] or "",
            item["size_text"] or "",
        )
    )
    return {
        "pos_id": pos_id,
        "instrument_id": instrument_id.upper(),
        "side": side,
        "size_text": size_text,
        "avg_entry_price": avg_entry_price,
        "pending_tpsl": normalized_orders,
        "snapshot_complete": bool(complete),
    }


def record_position_reconciliation_observation(
    session: Session,
    *,
    venue: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    strategy_instance_id: str,
    position: dict[str, Any],
    pending_tpsl: Iterable[dict[str, Any]],
    snapshot_complete: bool,
    observed_at: datetime,
) -> PositionReconciliationObservation:
    """Persist or adopt one immutable observation without committing the session."""

    normalized_venue = str(venue or "").strip().lower()
    normalized_strategy = str(strategy_instance_id or "").strip()
    if not normalized_venue or not normalized_strategy:
        raise ValueError("position_observation_identity_invalid")
    payload = build_position_observation_payload(
        position=position,
        pending_tpsl=pending_tpsl,
        complete=snapshot_complete,
    )
    encoded = _normalized_json(payload)
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    existing = (
        session.query(PositionReconciliationObservation)
        .filter_by(
            venue=normalized_venue,
            pos_id=payload["pos_id"],
            snapshot_fingerprint=fingerprint,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    row = PositionReconciliationObservation(
        venue=normalized_venue,
        execution_binding_id=int(execution_binding_id),
        execution_order_leg_id=int(execution_order_leg_id),
        strategy_instance_id=normalized_strategy,
        pos_id=payload["pos_id"],
        instrument_id=payload["instrument_id"],
        side=payload["side"],
        size_text=payload["size_text"],
        avg_entry_price=payload["avg_entry_price"],
        pending_tpsl_json=_normalized_json(payload["pending_tpsl"]),
        snapshot_complete=payload["snapshot_complete"],
        snapshot_fingerprint=fingerprint,
        observed_at=observed_at,
    )
    session.add(row)
    session.flush()
    return row


def _required_string(payload: dict[str, Any], *keys: str) -> str:
    value = _first_string(payload, *keys)
    if value is None:
        raise ValueError("position_observation_required_field_missing")
    return value


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def _optional_lower_string(payload: dict[str, Any], *keys: str) -> str | None:
    value = _first_string(payload, *keys)
    return value.lower() if value is not None else None


def _optional_decimal_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _decimal_text(value)


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("position_observation_decimal_invalid") from None
    if not number.is_finite() or number < 0:
        raise ValueError("position_observation_decimal_invalid")
    normalized = format(number.normalize(), "f")
    return "0" if normalized == "-0" else normalized


def _normalized_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
