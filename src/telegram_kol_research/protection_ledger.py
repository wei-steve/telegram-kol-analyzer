"""Durable local evidence for Deepcoin position TPSL ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from telegram_kol_research.models import PositionProtectionLedger, utc_now


_ACTIVE_OWNERSHIP_STATUSES = frozenset({"verified", "protected"})


@dataclass(frozen=True, slots=True)
class ProtectionOwnership:
    venue: str
    order_id: str
    pos_id: str
    status: str
    purpose: str


@dataclass(frozen=True, slots=True)
class ProtectionOwnershipConflict:
    order_id: str
    pos_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AccountProtectionOwnership:
    by_order_id: Mapping[str, ProtectionOwnership]
    by_pos_id: Mapping[str, tuple[ProtectionOwnership, ...]]
    conflicts: tuple[ProtectionOwnershipConflict, ...]
    stale_order_ids: tuple[str, ...]

    def owner_for_order(self, order_id: object) -> ProtectionOwnership | None:
        return self.by_order_id.get(str(order_id or "").strip())

    def orders_for_position(self, pos_id: object) -> tuple[str, ...]:
        return tuple(
            row.order_id
            for row in self.by_pos_id.get(str(pos_id or "").strip(), ())
        )


def load_account_protection_ownership(
    session,
    *,
    venue: str = "deepcoin",
    live_pos_ids: Iterable[str] = (),
) -> AccountProtectionOwnership:
    """Load the sole account-wide TPSL owner index from the canonical ledger."""

    normalized_venue = str(venue or "deepcoin").lower()
    rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == normalized_venue)
        .order_by(
            PositionProtectionLedger.order_id.asc(),
            PositionProtectionLedger.id.asc(),
        )
        .all()
    )
    return build_account_protection_ownership(
        rows,
        venue=normalized_venue,
        live_pos_ids=live_pos_ids,
    )


def build_account_protection_ownership(
    rows: Iterable[object],
    *,
    venue: str = "deepcoin",
    live_pos_ids: Iterable[str] = (),
) -> AccountProtectionOwnership:
    """Build an exact owner index without symbol, side, price, size, or time."""

    normalized_venue = str(venue or "deepcoin").lower()
    live_ids = {
        str(pos_id).strip()
        for pos_id in live_pos_ids
        if str(pos_id or "").strip()
    }
    grouped: dict[str, list[ProtectionOwnership]] = {}
    for row in rows:
        row_venue = str(_field(row, "venue") or "deepcoin").lower()
        status = str(_field(row, "status") or "").lower()
        order_id = str(_field(row, "order_id") or "").strip()
        pos_id = str(_field(row, "pos_id") or "").strip()
        if (
            row_venue != normalized_venue
            or status not in _ACTIVE_OWNERSHIP_STATUSES
            or not order_id
            or not pos_id
        ):
            continue
        grouped.setdefault(order_id, []).append(
            ProtectionOwnership(
                venue=row_venue,
                order_id=order_id,
                pos_id=pos_id,
                status=status,
                purpose=str(_field(row, "purpose") or ""),
            )
        )

    by_order_id: dict[str, ProtectionOwnership] = {}
    conflicts: list[ProtectionOwnershipConflict] = []
    for order_id, candidates in sorted(grouped.items()):
        pos_ids = tuple(sorted({candidate.pos_id for candidate in candidates}))
        if len(pos_ids) != 1:
            conflicts.append(
                ProtectionOwnershipConflict(order_id=order_id, pos_ids=pos_ids)
            )
            continue
        by_order_id[order_id] = candidates[-1]

    by_pos_id_mutable: dict[str, list[ProtectionOwnership]] = {}
    for owner in by_order_id.values():
        by_pos_id_mutable.setdefault(owner.pos_id, []).append(owner)
    by_pos_id = {
        pos_id: tuple(sorted(owners, key=lambda owner: owner.order_id))
        for pos_id, owners in sorted(by_pos_id_mutable.items())
    }
    stale = tuple(
        sorted(
            owner.order_id
            for owner in by_order_id.values()
            if live_ids and owner.pos_id not in live_ids
        )
    )
    return AccountProtectionOwnership(
        by_order_id=MappingProxyType(dict(sorted(by_order_id.items()))),
        by_pos_id=MappingProxyType(by_pos_id),
        conflicts=tuple(conflicts),
        stale_order_ids=stale,
    )


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


def list_verified_account_ledger_rows(
    session, *, venue: str = "deepcoin"
) -> list[PositionProtectionLedger]:
    """Return every verified TPSL owner row for one venue/account."""

    return (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == str(venue or "deepcoin").lower())
        .filter(PositionProtectionLedger.status == "verified")
        .order_by(
            PositionProtectionLedger.pos_id.asc(),
            PositionProtectionLedger.order_id.asc(),
        )
        .all()
    )


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _field(value: object, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
