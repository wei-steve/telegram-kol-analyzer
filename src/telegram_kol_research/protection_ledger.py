"""Durable local evidence for Deepcoin position TPSL ownership."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

from telegram_kol_research.models import PositionProtectionLedger, utc_now


def upsert_protection_ledger_row(
    session,
    *,
    venue: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    strategy_instance_id: str | None,
    pos_id: str,
    instrument_id: str,
    side: str,
    order_id: str,
    purpose: str,
    trigger_price: str | None,
    size_text: str | None,
    status: str,
    evidence_source: str,
    evidence: dict[str, Any] | None,
    seen_at: datetime | None = None,
) -> PositionProtectionLedger | None:
    """Create or update one protection-ledger row keyed by venue/order id."""

    clean_order_id = str(order_id or "").strip()
    if not clean_order_id:
        return None
    now = seen_at or utc_now()
    clean_venue = str(venue or "deepcoin").lower()
    row = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == clean_venue)
        .filter(PositionProtectionLedger.order_id == clean_order_id)
        .one_or_none()
    )
    if row is None:
        row = PositionProtectionLedger(
            venue=clean_venue,
            order_id=clean_order_id,
            first_seen_at=now,
            created_at=now,
        )
        session.add(row)

    row.execution_binding_id = int(execution_binding_id)
    row.execution_order_leg_id = int(execution_order_leg_id)
    row.strategy_instance_id = strategy_instance_id
    row.pos_id = str(pos_id)
    row.instrument_id = str(instrument_id).upper()
    row.side = str(side).lower()
    row.purpose = str(purpose)
    row.trigger_price = None if trigger_price is None else str(trigger_price)
    row.size_text = None if size_text is None else str(size_text)
    row.status = str(status or "verified")
    row.evidence_source = str(evidence_source)
    row.evidence_json = _compact_json(evidence or {})
    row.last_seen_at = now
    row.last_verified_at = now if row.status == "verified" else row.last_verified_at
    row.updated_at = now
    session.flush()
    return row


def list_verified_ledger_rows_for_positions(
    session, pos_ids: Iterable[str], *, venue: str = "deepcoin"
) -> list[PositionProtectionLedger]:
    clean_pos_ids = sorted({str(pos_id) for pos_id in pos_ids if str(pos_id or "")})
    if not clean_pos_ids:
        return []
    return (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == str(venue or "deepcoin").lower())
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.pos_id.in_(clean_pos_ids))
        .order_by(
            PositionProtectionLedger.pos_id.asc(),
            PositionProtectionLedger.order_id.asc(),
        )
        .all()
    )


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
