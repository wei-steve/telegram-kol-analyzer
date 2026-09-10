"""Which exchange protection orders belong to one exact position, and why.

Phase 6. Until now "the stops on this position" came from
``protection_attribution.match_position_protection``: a ledger-driven matcher
that answers per instrument and turns the *whole* instrument ambiguous the
moment one pending TPSL row is not in the ledger
(``global_unowned_order_present``). That is safe but it stops the two writes
phase 6 needs -- a stop cannot be replaced while the matcher refuses to name
the old one.

This module answers the same question from the binding chain instead:

    verified entry leg (pos_id)
      -> position_protection_ledger rows for that pos_id
      -> plus any pending TPSL row whose ``TU`` equals that pos_id

``TU == posId`` is the relation the 6-pre-3 read-only study confirmed on 30 of
30 connectable frames, and it is the *only* thing that links a protection order
to a position: ``OS`` changes on every write (eighteen production writes, then
eighteen distinct order ids), so the new stop and the old stop share nothing
except the position they point at.

Three rules this module does not bend:

* **Nothing is claimed by symbol, direction, size, price, time proximity, id
  adjacency, clOrdId or tag** (hard rule 1). ``posSide`` is used only to
  *exclude* a row that protects the other direction, never to claim one.
* **An unattributable protection row freezes the position instead of being
  ignored.** A pending TPSL row on our instrument that no ledger row and no
  ``TU`` frame can place is either ours -- in which case leaving it armed
  defeats the replacement we are about to make -- or somebody else's, in which
  case cancelling it is a blind write. Neither is acceptable, so the caller is
  told to stop and a person decides.
* **Only ``triggerOrderType == "TPSL"`` rows are ever considered.** A
  ``Conditional`` row is a pending *entry*, not protection, and is never
  cancelled by anything here (ARCHITECTURE section 6).

This module reads. It resolves from data the caller already fetched and from
the database, and it makes no exchange call and no exchange write.
``plan_protection_adoptions`` names the ledger rows a caller must write before
it may act on an adopted order; writing them is
``adopt_protection_orders``' job and only the write path calls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from telegram_kol_research.models import (
    DeepcoinWsEvent,
    PositionProtectionLedger,
)
from telegram_kol_research.native_tpsl import (
    native_tpsl_row_order_types,
    normalize_native_tpsl,
    protection_order_position_sides,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


#: Ledger purposes that hold a position's downside. They are replaced together
#: and in the A-5e order: the position must never be without one of them.
STOP_PURPOSES = frozenset({"stop_loss", "backup_stop"})
#: Ledger purposes that reduce a position at a profit. Two of these live at
#: once means the same lots can be closed twice, so they are replaced in the
#: opposite order: cancel first, place second.
TAKE_PROFIT_PURPOSES = frozenset({"take_profit"})

#: Ledger statuses that mean "this order is believed to be live on the
#: exchange". A cancelled or superseded row names an order we already retired.
ACTIVE_LEDGER_STATUSES = frozenset({"verified", "protected", "active"})

ADOPTION_EVIDENCE_SOURCE = "exchange_adopted_by_tu"

GROUP_STOP = "stop"
GROUP_TAKE_PROFIT = "take_profit"

FREEZE_POSITION_NOT_VERIFIED = "position_ownership_not_verified"
FREEZE_PENDING_READ_INCOMPLETE = "pending_read_incomplete"
FREEZE_ORDER_UNATTRIBUTABLE = "protection_order_unattributable"
FREEZE_ORDER_IDENTITY_MISSING = "protection_order_identity_missing"
FREEZE_COMBINED_TPSL_ORDER = "protection_order_combines_stop_and_take_profit"
FREEZE_LEDGER_EXCHANGE_CONFLICT = "protection_order_ledger_exchange_conflict"


@dataclass(frozen=True, slots=True)
class ProtectionOrderRef:
    """One live protection order this position is proven to own."""

    order_id: str
    group: str
    purpose: str
    trigger_price: str | None
    size_text: str | None
    #: ``ledger`` -- a durable row already names it; ``exchange_adopted_by_tu``
    #: -- only the exchange and a ``TU`` frame do, so the caller must adopt it
    #: into the ledger before acting on it.
    source: str
    row: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProtectionAdoption:
    """A protection order the chain proved ours that the ledger does not know."""

    order_id: str
    purpose: str
    trigger_price: str | None
    size_text: str | None
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProtectionAuthority:
    """The exact protection set of one position, or the reason there is none."""

    status: str
    pos_id: str
    instrument_id: str
    side: str
    reason_code: str | None = None
    execution_binding_id: int | None = None
    execution_order_leg_id: int | None = None
    strategy_instance_id: str | None = None
    stop_orders: tuple[ProtectionOrderRef, ...] = ()
    take_profit_orders: tuple[ProtectionOrderRef, ...] = ()
    adoptions: tuple[ProtectionAdoption, ...] = ()
    unattributable_order_ids: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    def group(self, group: str) -> tuple[ProtectionOrderRef, ...]:
        if group == GROUP_STOP:
            return self.stop_orders
        if group == GROUP_TAKE_PROFIT:
            return self.take_profit_orders
        raise ValueError("protection_group_invalid")

    @property
    def order_ids(self) -> tuple[str, ...]:
        return tuple(
            item.order_id for item in (*self.stop_orders, *self.take_profit_orders)
        )


def resolve_protection_authority(
    session,
    *,
    venue: str,
    pos_id: str,
    instrument_id: str,
    side: str,
    pending_rows: Sequence[Mapping[str, Any]] | None,
) -> ProtectionAuthority:
    """Name this position's live protection orders from the binding chain.

    ``pending_rows`` is one ``trigger-orders-pending`` read for
    ``instrument_id``. ``None`` means the read failed or was not attempted:
    that is unknown, never "no protection" (hard rule 4), so it freezes.
    """

    pos_id = str(pos_id or "").strip()
    instrument_id = str(instrument_id or "").strip().upper()
    normalized_side = str(side or "").strip().lower()

    def frozen(
        reason: str,
        *,
        unattributable_order_ids: tuple[str, ...] = (),
        **evidence: Any,
    ) -> ProtectionAuthority:
        return ProtectionAuthority(
            status="frozen",
            pos_id=pos_id,
            instrument_id=instrument_id,
            side=normalized_side,
            reason_code=reason,
            unattributable_order_ids=unattributable_order_ids,
            evidence=dict(evidence),
        )

    try:
        leg = require_verified_position_ownership(
            session, venue=venue, pos_id=pos_id
        )
    except PositionAttributionError as exc:
        return frozen(FREEZE_POSITION_NOT_VERIFIED, detail=str(exc))
    if pending_rows is None:
        return frozen(FREEZE_PENDING_READ_INCOMPLETE)

    binding_id = int(leg.execution_binding_id)
    leg_id = int(leg.id)
    strategy_instance_id = str(leg.strategy_instance_id or "") or None

    ledger_by_order = _active_ledger_rows_by_order_id(session, venue=venue)
    stop_orders: list[ProtectionOrderRef] = []
    take_profit_orders: list[ProtectionOrderRef] = []
    adoptions: list[ProtectionAdoption] = []
    unattributable: list[str] = []
    considered = 0

    for row in pending_rows:
        if not isinstance(row, Mapping):
            continue
        order_types = native_tpsl_row_order_types(dict(row))
        if order_types != {"TPSL"}:
            # A ``Conditional`` row is a pending entry, and a row whose type is
            # absent or self-contradictory is not something this module may
            # cancel. Neither is protection this position owns.
            continue
        normalized = normalize_native_tpsl(dict(row))
        if normalized is None:
            continue
        if normalized.inst_id and normalized.inst_id != instrument_id:
            continue
        order_id = str(normalized.ord_id or "").strip()
        considered += 1
        if not order_id:
            # An order we cannot name is an order we cannot cancel, and it may
            # be this position's stop.
            return frozen(
                FREEZE_ORDER_IDENTITY_MISSING,
                instrument_id=instrument_id,
            )

        ledger_row = ledger_by_order.get(order_id)
        owner_pos_id = None
        source = None
        if ledger_row is not None:
            owner_pos_id = str(ledger_row.pos_id or "")
            source = "ledger"
        else:
            tu_pos_id = _trade_unit_pos_id(session, venue=venue, order_id=order_id)
            if tu_pos_id is None:
                tu_pos_id = str(normalized.pos_id or "").strip() or None
                if tu_pos_id is not None:
                    source = "exchange_row_position_id"
            else:
                source = ADOPTION_EVIDENCE_SOURCE
            owner_pos_id = tu_pos_id

        if owner_pos_id is None:
            row_sides = protection_order_position_sides(dict(row))
            if row_sides and normalized_side and normalized_side not in row_sides:
                # It protects the other direction, so it is not this position's.
                # Excluding a row by its own ``posSide`` is not claiming one.
                continue
            unattributable.append(order_id)
            continue
        if owner_pos_id != pos_id:
            continue

        exchange_pos_id = str(normalized.pos_id or "").strip()
        if (
            ledger_row is not None
            and exchange_pos_id
            and exchange_pos_id != owner_pos_id
        ):
            return frozen(
                FREEZE_LEDGER_EXCHANGE_CONFLICT,
                order_id=order_id,
                ledger_pos_id=owner_pos_id,
                exchange_pos_id=exchange_pos_id,
            )
        if normalized.pos_side and normalized_side and normalized.pos_side != normalized_side:
            return frozen(
                FREEZE_LEDGER_EXCHANGE_CONFLICT,
                order_id=order_id,
                detail="pos_side_mismatch",
                exchange_pos_side=normalized.pos_side,
            )

        has_stop = normalized.stop_loss_trigger_price is not None
        has_take_profit = normalized.take_profit_trigger_price is not None
        if has_stop and has_take_profit:
            # One order that carries both triggers cannot be replaced by group:
            # cancelling it to move the stop would also remove the take profit.
            return frozen(
                FREEZE_COMBINED_TPSL_ORDER,
                order_id=order_id,
            )
        if not has_stop and not has_take_profit:
            continue

        group = GROUP_STOP if has_stop else GROUP_TAKE_PROFIT
        purpose = _purpose_for(ledger_row, group=group)
        trigger_price = _text(
            normalized.stop_loss_trigger_price
            if has_stop
            else normalized.take_profit_trigger_price
        )
        size_text = _text(normalized.size)
        ref = ProtectionOrderRef(
            order_id=order_id,
            group=group,
            purpose=purpose,
            trigger_price=trigger_price,
            size_text=size_text,
            source=source or "ledger",
            row=dict(row),
        )
        if ledger_row is None:
            adoptions.append(
                ProtectionAdoption(
                    order_id=order_id,
                    purpose=purpose,
                    trigger_price=trigger_price,
                    size_text=size_text,
                    evidence={
                        "source": source,
                        "pos_id": pos_id,
                        "instrument_id": instrument_id,
                        "pos_side": normalized.pos_side,
                        "trigger_order_type": "TPSL",
                    },
                )
            )
        if group == GROUP_STOP:
            stop_orders.append(ref)
        else:
            take_profit_orders.append(ref)

    if unattributable:
        return frozen(
            FREEZE_ORDER_UNATTRIBUTABLE,
            unattributable_order_ids=tuple(sorted(unattributable)),
            instrument_id=instrument_id,
        )

    return ProtectionAuthority(
        status="resolved",
        pos_id=pos_id,
        instrument_id=instrument_id,
        side=normalized_side,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        strategy_instance_id=strategy_instance_id,
        stop_orders=tuple(stop_orders),
        take_profit_orders=tuple(take_profit_orders),
        adoptions=tuple(adoptions),
        evidence={"pending_tpsl_rows_considered": considered},
    )


def adopt_protection_orders(
    session,
    *,
    authority: ProtectionAuthority,
    venue: str,
    adopted_at: datetime,
) -> int:
    """Write the ledger rows for orders the chain proved this position owns.

    Called only from the write path, and only immediately before acting on the
    order. The row records how ownership was established -- ``TU == posId`` --
    so a later reader is never left guessing why an order it never submitted is
    in the ledger.
    """

    if not authority.resolved:
        raise ValueError("protection_authority_not_resolved")
    if authority.execution_binding_id is None or authority.execution_order_leg_id is None:
        raise ValueError("protection_authority_incomplete")
    written = 0
    for adoption in authority.adoptions:
        upsert_protection_ledger_row(
            session,
            venue=venue,
            execution_binding_id=int(authority.execution_binding_id),
            execution_order_leg_id=int(authority.execution_order_leg_id),
            strategy_instance_id=authority.strategy_instance_id,
            pos_id=authority.pos_id,
            instrument_id=authority.instrument_id,
            side=authority.side,
            order_id=adoption.order_id,
            purpose=adoption.purpose,
            trigger_price=adoption.trigger_price,
            size_text=adoption.size_text,
            status="verified",
            evidence_source=ADOPTION_EVIDENCE_SOURCE,
            evidence=dict(adoption.evidence),
            seen_at=adopted_at,
        )
        written += 1
    return written


def _active_ledger_rows_by_order_id(session, *, venue: str) -> dict[str, Any]:
    rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == str(venue or "deepcoin").lower())
        .filter(PositionProtectionLedger.status.in_(sorted(ACTIVE_LEDGER_STATUSES)))
        .all()
    )
    by_order: dict[str, Any] = {}
    for row in rows:
        order_id = str(row.order_id or "").strip()
        if order_id:
            by_order[order_id] = row
    return by_order


def _trade_unit_pos_id(session, *, venue: str, order_id: str) -> str | None:
    """The ``TU`` every ``TriggerOrder`` frame for this order agrees on.

    Frames repeat and may arrive out of order (hard rule 6), so this asks for
    agreement rather than for the newest row: two different ``TU`` values for
    one ``OS`` would mean the relation this phase rests on does not hold, and
    the honest answer there is "unknown", not "the last one wins".
    """

    rows = (
        session.query(DeepcoinWsEvent.trade_unit_id)
        .filter(DeepcoinWsEvent.venue == str(venue or "deepcoin").lower())
        .filter(DeepcoinWsEvent.channel == "TriggerOrder")
        .filter(DeepcoinWsEvent.order_sys_id == str(order_id))
        .distinct()
        .all()
    )
    values = {
        str(row[0]).strip()
        for row in rows
        if row[0] not in (None, "")
        and str(row[0]).strip().lower() not in {"default", "0"}
    }
    if len(values) != 1:
        return None
    return next(iter(values))


def _purpose_for(ledger_row: Any, *, group: str) -> str:
    if ledger_row is not None:
        purpose = str(ledger_row.purpose or "").lower()
        if group == GROUP_STOP and purpose in STOP_PURPOSES:
            return purpose
        if group == GROUP_TAKE_PROFIT and purpose in TAKE_PROFIT_PURPOSES:
            return purpose
    return "stop_loss" if group == GROUP_STOP else "take_profit"


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        normalized = format(value.normalize(), "f")
        return "0" if normalized == "-0" else normalized
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not parsed.is_finite():
        return str(value)
    normalized = format(parsed.normalize(), "f")
    return "0" if normalized == "-0" else normalized


def summarize_authority(authority: ProtectionAuthority) -> dict[str, Any]:
    """A small, stable dict for evidence rows and journal lines."""

    return {
        "status": authority.status,
        "reason_code": authority.reason_code,
        "pos_id": authority.pos_id,
        "instrument_id": authority.instrument_id,
        "side": authority.side,
        "stop_order_ids": [item.order_id for item in authority.stop_orders],
        "take_profit_order_ids": [
            item.order_id for item in authority.take_profit_orders
        ],
        "adopted_order_ids": [item.order_id for item in authority.adoptions],
        "unattributable_order_ids": list(authority.unattributable_order_ids),
        "evidence": dict(authority.evidence),
    }


def iter_group_order_ids(
    authority: ProtectionAuthority, groups: Iterable[str]
) -> tuple[str, ...]:
    order_ids: list[str] = []
    for group in groups:
        order_ids.extend(item.order_id for item in authority.group(group))
    return tuple(order_ids)
