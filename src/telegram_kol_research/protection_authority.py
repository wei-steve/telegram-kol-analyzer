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

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from telegram_kol_research.models import (
    DeepcoinWsEvent,
    ExecutionOrderLeg,
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

#: The ``TU`` a protection order carries before its position exists. A migrated
#: limit entry leg puts ``slTriggerPx`` on the order itself, so the exchange
#: arms that stop while the entry is still resting -- and it shows up in
#: ``trigger-orders-pending`` as a ``TPSL`` row with no position id and this
#: value, which is *indistinguishable in shape* from an ownerless stop on an
#: open position. Observed in production 2026-09-10: binding 347's two resting
#: entries froze every BTC short position for as long as they rested.
TRADE_UNIT_BEFORE_POSITION = "default"

#: Entry-leg states in which the exchange may be holding that leg's attached
#: stop while no position exists yet.
PENDING_ENTRY_LEG_STATUSES = frozenset(
    {"pending", "submitted", "live", "partially_filled"}
)

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
    #: What the exchange showed for this order at resolution time.
    trigger_price: str | None
    size_text: str | None
    #: What our durable record says it should be. ``None`` for an order the
    #: ledger does not know yet -- there the two are equal by construction at
    #: adoption time, and saying so is more honest than pretending the check
    #: compared two independent sources.
    ledger_trigger_price: str | None
    ledger_size_text: str | None
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
    excluded_pending_entry_order_ids: tuple[str, ...] = ()
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

    excluded: list[str] = []

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
            excluded_pending_entry_order_ids=tuple(excluded),
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
    pending_entry_signatures: set[tuple[str, str, str, str]] | None = None
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
            trade_units = _trade_unit_values(session, venue=venue, order_id=order_id)
            if trade_units == {TRADE_UNIT_BEFORE_POSITION}:
                # No position exists for this order yet. If it also matches one
                # of our own resting entry legs exactly, it is that leg's
                # attached stop -- not an ownerless stop on an open position.
                # Excluding it is not claiming it: nothing here reads, cancels
                # or adopts it, and being wrong costs one untouched order.
                if pending_entry_signatures is None:
                    pending_entry_signatures = _pending_entry_stop_signatures(
                        session, venue=venue
                    )
                if _row_signature(normalized) in pending_entry_signatures:
                    excluded.append(order_id)
                    continue
            tu_pos_id = _sole_position_trade_unit(trade_units)
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
            ledger_trigger_price=(
                _text(ledger_row.trigger_price) if ledger_row is not None else None
            ),
            ledger_size_text=(
                _text(ledger_row.size_text) if ledger_row is not None else None
            ),
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
        excluded_pending_entry_order_ids=tuple(excluded),
        evidence={"pending_tpsl_rows_considered": considered},
    )


CANCEL_TARGET_NOT_RESOLVED = "protection_cancel_target_not_resolved"
CANCEL_TARGET_NOT_TPSL = "protection_cancel_target_not_tpsl"
CANCEL_TARGET_INSTRUMENT_CHANGED = "protection_cancel_target_instrument_changed"
CANCEL_TARGET_SIDE_CHANGED = "protection_cancel_target_side_changed"
CANCEL_TARGET_TRIGGER_CHANGED = "protection_cancel_target_trigger_changed"
CANCEL_TARGET_SIZE_CHANGED = "protection_cancel_target_size_changed"
CANCEL_TARGET_ABSENT = "protection_cancel_target_absent"


def evaluate_cancel_precheck(
    authority: ProtectionAuthority,
    pending_rows: Sequence[Mapping[str, Any]] | None,
    order_id: str,
) -> str | None:
    """Whether this exact order is still what the chain resolved. ``None`` = yes.

    The read that produced ``authority`` and the cancel that follows are two
    moments, and between them the exchange can have replaced, filled or resized
    the order. Cancelling on the strength of the older read is how a live stop
    gets removed by accident, so the order is looked at again **by its exact
    id** and all four attributes have to still agree: instrument, ``posSide``,
    trigger price and size.

    Two answers are deliberately not "go ahead":

    * the order is no longer listed -- it is not cancelled on the strength of a
      stale read, and "already gone" is the caller's decision to interpret;
    * the pending list could not be read -- unknown is never permission
      (hard rule 4).
    """

    expected = {
        item.order_id: item
        for item in (*authority.stop_orders, *authority.take_profit_orders)
    }
    item = expected.get(str(order_id))
    if item is None:
        return CANCEL_TARGET_NOT_RESOLVED
    if pending_rows is None:
        return FREEZE_PENDING_READ_INCOMPLETE
    for row in pending_rows:
        if not isinstance(row, Mapping):
            continue
        normalized = normalize_native_tpsl(dict(row))
        observed = _order_identity(dict(row))
        if observed != str(order_id):
            continue
        if normalized is None:
            return CANCEL_TARGET_NOT_TPSL
        if normalized.inst_id and normalized.inst_id != authority.instrument_id:
            return CANCEL_TARGET_INSTRUMENT_CHANGED
        if (
            normalized.pos_side
            and authority.side
            and normalized.pos_side != authority.side
        ):
            return CANCEL_TARGET_SIDE_CHANGED
        observed_trigger = (
            normalized.stop_loss_trigger_price
            if item.group == GROUP_STOP
            else normalized.take_profit_trigger_price
        )
        # Compared against what this authority saw when it resolved, which is
        # the point of the check: the question is whether the order drifted
        # between the read that named it and the cancel about to remove it, and
        # answering it requires two reads of the same order at two moments.
        #
        # Deliberately *not* compared against the ledger. A ledger row can be
        # stale relative to the exchange for legitimate reasons -- a staged
        # take profit that partially filled leaves our recorded size behind the
        # live one -- and blocking a cancel on that would break a management
        # instruction that works today. Identity is the order id; these four
        # fields corroborate that the same order is still the same order.
        # Ledger drift is worth *observing* (the shadow counts it) but it is
        # not this gate's question.
        if _text(observed_trigger) != item.trigger_price:
            return CANCEL_TARGET_TRIGGER_CHANGED
        if _text(normalized.size) != item.size_text:
            return CANCEL_TARGET_SIZE_CHANGED
        return None
    return CANCEL_TARGET_ABSENT


def ledger_drift(authority: ProtectionAuthority) -> dict[str, str]:
    """Where the durable record and the exchange disagree about our own orders.

    Not a gate: a stale ledger row is a reason to look, not a reason to refuse
    a cancel (see :func:`evaluate_cancel_precheck`). It is counted because a
    stricter rule keyed on the ledger would only be safe if this were rare, and
    "rare" is a measurement nobody has made.
    """

    drift: dict[str, str] = {}
    for item in (*authority.stop_orders, *authority.take_profit_orders):
        if item.ledger_trigger_price is None:
            continue
        if item.ledger_trigger_price != item.trigger_price:
            drift[item.order_id] = "trigger_price"
        elif item.ledger_size_text != item.size_text:
            drift[item.order_id] = "size"
    return drift


def _order_identity(row: Mapping[str, Any]) -> str:
    for key in ("ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


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


def _trade_unit_values(session, *, venue: str, order_id: str) -> set[str]:
    """Every distinct ``TU`` this order's ``TriggerOrder`` frames carry.

    ``default`` is kept rather than filtered here: "the position does not exist
    yet" and "no frame ever arrived" are different facts, and only the first
    one may exclude a row as a resting entry's stop.
    """

    rows = (
        session.query(DeepcoinWsEvent.trade_unit_id)
        .filter(DeepcoinWsEvent.venue == str(venue or "deepcoin").lower())
        .filter(DeepcoinWsEvent.channel == "TriggerOrder")
        .filter(DeepcoinWsEvent.order_sys_id == str(order_id))
        .distinct()
        .all()
    )
    return {
        str(row[0]).strip()
        for row in rows
        if row[0] not in (None, "") and str(row[0]).strip() != "0"
    }


def _sole_position_trade_unit(trade_units: set[str]) -> str | None:
    """The one real posId these frames agree on, or ``None``.

    Frames repeat and may arrive out of order (hard rule 6), so this asks for
    agreement rather than for the newest row: two different ``TU`` values for
    one ``OS`` would mean the relation this phase rests on does not hold, and
    the honest answer there is "unknown", not "the last one wins". A ``TU`` that
    flipped from ``default`` to a posId leaves both values behind, and only the
    posId is a position.
    """

    positions = {
        value for value in trade_units if value != TRADE_UNIT_BEFORE_POSITION
    }
    if len(positions) != 1:
        return None
    return next(iter(positions))


def _pending_entry_stop_signatures(
    session, *, venue: str
) -> set[tuple[str, str, str, str]]:
    """``(instId, posSide, sz, slTriggerPx)`` of every resting entry leg's stop.

    Read from our own durable leg rows, never from the exchange: the question
    is "did we ask for this stop as part of an entry", and only our record can
    answer it.
    """

    rows = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == str(venue or "deepcoin").lower())
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.status.in_(sorted(PENDING_ENTRY_LEG_STATUSES)))
        .all()
    )
    signatures: set[tuple[str, str, str, str]] = set()
    for row in rows:
        try:
            request = json.loads(row.request_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(request, dict):
            continue
        instrument = str(request.get("instId") or "").strip().upper()
        pos_side = str(request.get("posSide") or "").strip().lower()
        size = _text(request.get("sz"))
        stop = _text(request.get("slTriggerPx"))
        if instrument and pos_side and size and stop:
            signatures.add((instrument, pos_side, size, stop))
    return signatures


def _row_signature(normalized: Any) -> tuple[str, str, str, str]:
    return (
        str(normalized.inst_id or "").upper(),
        str(normalized.pos_side or "").lower(),
        _text(normalized.size) or "",
        _text(normalized.stop_loss_trigger_price) or "",
    )


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
        "excluded_pending_entry_order_ids": list(
            authority.excluded_pending_entry_order_ids
        ),
        "evidence": dict(authority.evidence),
    }


def iter_group_order_ids(
    authority: ProtectionAuthority, groups: Iterable[str]
) -> tuple[str, ...]:
    order_ids: list[str] = []
    for group in groups:
        order_ids.extend(item.order_id for item in authority.group(group))
    return tuple(order_ids)
