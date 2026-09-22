"""What automatic break-even convergence would do, computed and never done.

Phase 6h, shadow half. ``break_even_convergence_executor`` has refused every
candidate it has ever seen: its preflight reads a TPSL row from
``trigger-orders-pending`` using a **position row's** vocabulary --
``row["posId"]`` and ``row["slTriggerPx"]`` -- and a TPSL row carries neither.
``posId`` is absent from those rows entirely and the price is spelled
``slTriggerPrice``. Either condition alone fails on every row, every time, so
``break_even_existing_stop_drift`` is raised before any decision is reached
(A-13 found the symptom; the reading was restated in phase 6h after an earlier
description of it -- "it reads the position row" -- turned out to be wrong).

This module answers the same question the executor asks, with the vocabulary
read through :mod:`deepcoin_trigger_rows`, and **writes nothing anywhere**. It
does not submit, cancel, or reserve; it does not touch the ledger. Its whole
output is a per-round summary for the reconcile log, so that the corrected
reading can be watched against real positions before it is allowed to decide
anything.

Two things it is built to make visible, because neither is obvious from the
code being replaced:

* **Whether the corrected reading actually resolves.** ``legacy_would_refuse``
  re-runs the old predicate on the same rows. While the defect stands it is
  expected to equal the number of stops examined -- a paired observable where
  one side is the live read and the other is the bug, so a round in which they
  agree is a round in which nothing was learned, and one where they diverge is
  the fix taking effect.
* **How many stops a break-even would cancel.** The executor collects *every*
  ledger stop row for the position, and since phase 6f a protected position has
  two: the primary and the backup placed beside it. So ``set_break_even`` would
  cancel both and leave a single stop at the entry price. That may well be
  intended -- a stop at break-even is tighter than either -- but it is not
  visible anywhere in the executor, and it is the kind of thing that should be
  read off a shadow round rather than discovered by a position losing its
  backup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, DecimalException
from typing import Any, Mapping, Sequence

from telegram_kol_research.deepcoin_trigger_rows import (
    position_id_or_none,
    stop_trigger_price,
)
from telegram_kol_research.models import (
    PositionBackupStopOrder,
    PositionProtectionLeg,
    PositionProtectionLedger,
)
from telegram_kol_research.stop_ladder import (
    ACTION_CLOSE_AT_MARKET,
    ACTION_NO_CHANGE,
    ACTION_REPLACE_STOP,
)
from telegram_kol_research.strategy_management_market_policy import (
    BreakEvenMarketPolicyError,
    assess_break_even_with_existing_stop,
)

logger = logging.getLogger(__name__)

STOP_PURPOSES = ("stop_loss", "combined")
LEDGER_STATUSES = ("verified", "protected")


@dataclass(frozen=True, slots=True)
class BreakEvenShadowRow:
    """One position's answer, as the corrected reading sees it."""

    pos_id: str
    instrument_id: str
    side: str
    entry_price: str | None = None
    market_price: str | None = None
    action: str | None = None
    target_stop_price: str | None = None
    current_stop_prices: tuple[str, ...] = ()
    would_cancel_order_ids: tuple[str, ...] = ()
    would_close_size: str | None = None
    would_close_endpoint: str | None = None
    would_close_ord_type: str | None = None
    would_close_cancels_stops_first: bool | None = None
    stops_examined: int = 0
    stops_resolved: int = 0
    legacy_would_refuse: int = 0
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class StopLadderShadowRow:
    """One position's ladder answer for this round."""

    pos_id: str
    instrument_id: str
    side: str
    filled_level: int = 0
    would_action: str = ACTION_NO_CHANGE
    target_price: str | None = None
    target_source: str | None = None
    market_price: str | None = None
    effective_stop_price: str | None = None
    existing_stops: tuple[str, ...] = ()
    rungs: tuple[dict[str, Any], ...] = ()
    per_position_levels: Mapping[str, int] = field(default_factory=dict)
    execution_binding_id: int | None = None
    strategy_instance_id: str | None = None
    symbol: str | None = None
    reason_code: str | None = None
    recorded: bool = False

    def detail(self) -> dict[str, Any]:
        return {
            "pos_id": self.pos_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "filled_level": self.filled_level,
            "would_action": self.would_action,
            "target_price": self.target_price,
            "target_source": self.target_source,
            "market_price": self.market_price,
            "effective_stop_price": self.effective_stop_price,
            "existing_stops": list(self.existing_stops),
            "rungs": [dict(rung) for rung in self.rungs],
            "per_position_levels": dict(self.per_position_levels),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class StopLadderShadowResult:
    """What the ladder would have done, and how much of it was new."""

    mode: str = "disabled"
    configured_mode: str = "disabled"
    rows: tuple[StopLadderShadowRow, ...] = ()
    events_written: int = 0
    counts_by_action: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "configured_mode": self.configured_mode,
            "events_written": self.events_written,
            "counts_by_action": dict(self.counts_by_action),
            "rows": [row.detail() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class BreakEvenShadowResult:
    positions_seen: int = 0
    rows: tuple[BreakEvenShadowRow, ...] = ()
    read_failures: tuple[str, ...] = ()
    counts_by_action: dict[str, int] = field(default_factory=dict)
    stops_examined: int = 0
    stops_resolved: int = 0
    legacy_would_refuse: int = 0
    would_cancel_total: int = 0
    would_close_positions: int = 0
    stop_ladder: StopLadderShadowResult = field(
        default_factory=StopLadderShadowResult
    )

    def summary(self) -> dict[str, Any]:
        """The shape the reconcile round log carries."""

        return {
            "stop_ladder": self.stop_ladder.summary(),
            "positions_seen": self.positions_seen,
            "counts_by_action": dict(self.counts_by_action),
            "stops_examined": self.stops_examined,
            "stops_resolved": self.stops_resolved,
            "legacy_would_refuse": self.legacy_would_refuse,
            "would_cancel_total": self.would_cancel_total,
            "would_close_positions": self.would_close_positions,
            "read_failures": list(self.read_failures),
            "rows": [
                {
                    "pos_id": row.pos_id,
                    "action": row.action,
                    "entry_price": row.entry_price,
                    "market_price": row.market_price,
                    "target_stop_price": row.target_stop_price,
                    "current_stop_prices": list(row.current_stop_prices),
                    "would_cancel_order_ids": list(row.would_cancel_order_ids),
                    "would_close_size": row.would_close_size,
                    "would_close_endpoint": row.would_close_endpoint,
                    "would_close_ord_type": row.would_close_ord_type,
                    "would_close_cancels_stops_first": (
                        row.would_close_cancels_stops_first
                    ),
                    "stops_examined": row.stops_examined,
                    "stops_resolved": row.stops_resolved,
                    "legacy_would_refuse": row.legacy_would_refuse,
                    "reason_code": row.reason_code,
                }
                for row in self.rows
            ],
        }


def run_break_even_shadow_pass(
    session_factory,
    *,
    deepcoin_client: Any,
    now: datetime,
    venue: str = "deepcoin",
) -> BreakEvenShadowResult:
    """Compute, for every live position, what a break-even convergence would do.

    ``now`` is the round's moment; the break-even half is time-independent,
    while the stop-ladder half stamps the events it records with it.
    """

    try:
        positions = list(deepcoin_client.list_positions())
    except Exception:
        logger.warning("break-even shadow could not read positions", exc_info=True)
        return BreakEvenShadowResult(read_failures=("positions",))

    live = [row for row in positions if isinstance(row, Mapping) and _has_size(row)]
    pending_by_instrument: dict[str, list[Any] | None] = {}
    read_failures: list[str] = []
    for row in live:
        instrument_id = (_text(row, "instId", "inst_id") or "").upper()
        if not instrument_id or instrument_id in pending_by_instrument:
            continue
        try:
            pending_by_instrument[instrument_id] = list(
                deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
            )
        except Exception:
            logger.warning(
                "break-even shadow could not read pending trigger orders inst_id=%s",
                instrument_id,
                exc_info=True,
            )
            pending_by_instrument[instrument_id] = None
            read_failures.append(instrument_id)

    rows: list[BreakEvenShadowRow] = []
    for position in live:
        instrument_id = (_text(position, "instId", "inst_id") or "").upper()
        pos_id = _text(position, "posId", "pos_id", "id")
        if not instrument_id or not pos_id:
            continue
        rows.append(
            _shadow_one_position(
                session_factory,
                position=position,
                pos_id=pos_id,
                instrument_id=instrument_id,
                pending_rows=pending_by_instrument.get(instrument_id),
                venue=venue,
            )
        )

    counts: dict[str, int] = {}
    for row in rows:
        key = row.action or row.reason_code or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return BreakEvenShadowResult(
        stop_ladder=run_stop_ladder_shadow(
            session_factory, positions=live, now=now, venue=venue
        ),
        positions_seen=len(live),
        rows=tuple(rows),
        read_failures=tuple(read_failures),
        counts_by_action=counts,
        stops_examined=sum(row.stops_examined for row in rows),
        stops_resolved=sum(row.stops_resolved for row in rows),
        legacy_would_refuse=sum(row.legacy_would_refuse for row in rows),
        would_cancel_total=sum(len(row.would_cancel_order_ids) for row in rows),
        would_close_positions=sum(
            1 for row in rows if row.action == "full_exit" and row.would_close_size
        ),
    )


def _shadow_one_position(
    session_factory,
    *,
    position: Mapping[str, Any],
    pos_id: str,
    instrument_id: str,
    pending_rows: Sequence[Any] | None,
    venue: str,
) -> BreakEvenShadowRow:
    side = (_text(position, "posSide", "pos_side", "side") or "").lower()
    entry_price = _text(position, "avgPx", "avg_px")
    market_price = _text(position, "lastPx", "markPx", "last_px")
    base = BreakEvenShadowRow(
        pos_id=pos_id,
        instrument_id=instrument_id,
        side=side,
        entry_price=entry_price,
        market_price=market_price,
    )
    if pending_rows is None:
        # Hard rule 4: an unreadable read is unknown, never "no stops".
        return _with(base, reason_code="pending_unreadable")
    if not entry_price or not market_price or side not in ("long", "short"):
        return _with(base, reason_code="position_fields_incomplete")

    with session_factory() as session:
        ledger_stops = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.venue == venue)
            .filter(PositionProtectionLedger.pos_id == pos_id)
            .filter(PositionProtectionLedger.purpose.in_(STOP_PURPOSES))
            .filter(PositionProtectionLedger.status.in_(LEDGER_STATUSES))
            .order_by(PositionProtectionLedger.id.asc())
            .all()
        )
        ledger = [
            (str(row.order_id or "").strip(), str(row.trigger_price or "").strip())
            for row in ledger_stops
        ]
        backup_order_ids = _backup_stop_order_ids(
            session, venue=venue, pos_id=pos_id
        )
    if not ledger:
        return _with(base, reason_code="no_ledger_stop")

    # Attribution is by order id. A TPSL row carries no posId at all, so a
    # posId equality test can only ever fail -- which is the defect this
    # module exists to measure, not to repeat.
    by_order_id = {}
    for row in pending_rows:
        if not isinstance(row, Mapping):
            continue
        order_id = _text(row, "ordId", "orderId", "ord_id")
        if order_id:
            by_order_id[order_id] = row

    stop_prices: list[str] = []
    stop_order_ids: list[str] = []
    legacy_refusals = 0
    for order_id, ledger_price in ledger:
        exchange_row = by_order_id.get(order_id)
        if exchange_row is None:
            return _with(
                base,
                stops_examined=len(ledger),
                legacy_would_refuse=len(ledger),
                reason_code="ledger_stop_absent_from_exchange",
            )
        # The old predicate, run on the same row purely to record what it
        # would have answered. Its two halves are independently fatal.
        if str(exchange_row.get("posId") or "") != pos_id or not _decimal_equal(
            exchange_row.get("slTriggerPx"), ledger_price
        ):
            legacy_refusals += 1
        # Order id is the attribution. A position id, when the row happens to
        # carry one, is only allowed to *contradict* -- never to be required.
        # A TPSL row returns ``None`` here and that is the answer, not a
        # mismatch: requiring equality against ``""`` is precisely how the
        # executor refused every candidate it ever saw.
        row_pos_id = position_id_or_none(exchange_row)
        if row_pos_id is not None and row_pos_id != pos_id:
            return _with(
                base,
                stops_examined=len(ledger),
                legacy_would_refuse=len(ledger),
                reason_code="stop_order_claimed_by_another_position",
            )
        venue_price = stop_trigger_price(exchange_row)
        if venue_price is None or not _decimal_equal(venue_price, ledger_price):
            return _with(
                base,
                stops_examined=len(ledger),
                legacy_would_refuse=len(ledger),
                reason_code="existing_stop_drift",
            )
        stop_prices.append(ledger_price)
        stop_order_ids.append(order_id)

    try:
        decision = assess_break_even_with_existing_stop(
            side=side,
            entry_price=entry_price,
            market_price=market_price,
            existing_stop_prices=stop_prices,
        )
    except BreakEvenMarketPolicyError as exc:
        return _with(
            base,
            stops_examined=len(ledger),
            stops_resolved=len(stop_prices),
            legacy_would_refuse=legacy_refusals,
            current_stop_prices=tuple(stop_prices),
            reason_code=f"market_policy_refused:{exc}",
        )

    # A break-even replacement touches the **primary stop only**. The backup
    # beside it is not re-priced here: `trigger_backup_stop_executor` recomputes
    # it from the new primary on a later round and replaces the old one in the
    # A-5e order. Cancelling both here would replace two stops with one and
    # leave the position without a backup in between, which is the opposite of
    # what the backup exists for.
    primary_order_ids = [
        order_id for order_id in stop_order_ids if order_id not in backup_order_ids
    ]
    if decision.action == "set_break_even" and len(primary_order_ids) != 1:
        # Not "cancel what we can identify". If the primary cannot be named
        # exactly, the replacement set is unknown, and unknown is not a subset.
        return _with(
            base,
            action=decision.action,
            target_stop_price=entry_price,
            current_stop_prices=tuple(stop_prices),
            stops_examined=len(ledger),
            stops_resolved=len(stop_prices),
            legacy_would_refuse=legacy_refusals,
            reason_code=f"primary_stop_not_exactly_one:{len(primary_order_ids)}",
        )
    would_cancel = (
        tuple(primary_order_ids) if decision.action == "set_break_even" else ()
    )
    # ``full_exit`` is not "no action" -- it is a market close of the whole
    # position, and the shadow has to say so. Recording only the action name
    # would tell a reviewer which branch runs and nothing about what it does,
    # which is exactly the unreviewable record phase 6f had to fix once
    # already. ``cancels_stops_first`` is False and stated rather than omitted:
    # the branch issues no cancel, so the stops resting on the position are
    # untouched by it.
    close_fields: dict[str, Any] = {}
    if decision.action == "full_exit":
        close_fields = {
            "would_close_size": _text(position, "pos", "sz", "size", "availPos"),
            "would_close_endpoint": "close_position",
            "would_close_ord_type": "market",
            "would_close_cancels_stops_first": False,
        }
    return _with(
        base,
        **close_fields,
        action=decision.action,
        target_stop_price=(
            entry_price if decision.action == "set_break_even"
            else decision.effective_stop_price
        ),
        current_stop_prices=tuple(stop_prices),
        would_cancel_order_ids=would_cancel,
        stops_examined=len(ledger),
        stops_resolved=len(stop_prices),
        legacy_would_refuse=legacy_refusals,
    )


#: The ``execution_events.action`` each ladder answer is recorded under.
STOP_LADDER_EVENT_ACTION = {
    ACTION_REPLACE_STOP: "stop_ladder_would_replace",
    ACTION_CLOSE_AT_MARKET: "stop_ladder_would_close",
    ACTION_NO_CHANGE: "stop_ladder_no_change",
}
STOP_LADDER_EVENT_ACTIONS = frozenset(STOP_LADDER_EVENT_ACTION.values())


def run_stop_ladder_shadow(
    session_factory,
    *,
    positions: Sequence[Mapping[str, Any]],
    now: datetime,
    venue: str = "deepcoin",
) -> StopLadderShadowResult:
    """What the ladder would do to each live position, recorded and not done.

    ``disabled`` is exactly nothing: no derivation, no row, no event.  Under
    ``shadow`` -- which ``live`` also resolves to in phase 1 -- each position
    gets one line saying which rung its own order-level evidence proves, where
    the stop would go and what it would take to put it there.  The line is
    written only when it differs from the last one for that position, because
    a record that repeats every two seconds is a record nobody reads.
    """

    from telegram_kol_research.trading_settings import load_trading_settings

    try:
        settings = load_trading_settings(session_factory)
    except Exception:  # pragma: no cover - a settings read must not break a round
        logger.warning("stop ladder shadow could not read settings", exc_info=True)
        return StopLadderShadowResult()
    configured = str(getattr(settings, "stop_ladder_mode", "disabled"))
    mode = settings.effective_stop_ladder_mode
    if mode == "disabled":
        return StopLadderShadowResult(mode=mode, configured_mode=configured)

    rows: list[StopLadderShadowRow] = []
    written = 0
    for position in positions:
        try:
            row = _stop_ladder_one_position(
                session_factory, position=position, venue=venue
            )
        except Exception:  # pragma: no cover - the shadow decides nothing
            logger.warning("stop ladder shadow failed for a position", exc_info=True)
            continue
        if row is None:
            continue
        try:
            recorded = _record_stop_ladder_event(
                session_factory, row=row, venue=venue, now=now
            )
        except Exception:  # pragma: no cover - evidence must not break a round
            logger.warning("stop ladder shadow could not record an event", exc_info=True)
            recorded = False
        rows.append(_with_recorded(row, recorded))
        written += int(recorded)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.would_action] = counts.get(row.would_action, 0) + 1
    return StopLadderShadowResult(
        mode=mode,
        configured_mode=configured,
        rows=tuple(rows),
        events_written=written,
        counts_by_action=counts,
    )


def _stop_ladder_one_position(
    session_factory, *, position: Mapping[str, Any], venue: str
) -> StopLadderShadowRow | None:
    from telegram_kol_research.break_even_reference import (
        resolve_break_even_reference,
    )
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        StrategyLifecycle,
    )
    from telegram_kol_research.stop_ladder import ladder_decision, ladder_target
    from telegram_kol_research.stop_ladder_records import derive_filled_tp_level

    pos_id = _text(position, "posId", "pos_id", "id")
    instrument_id = (_text(position, "instId", "inst_id") or "").upper()
    side = (_text(position, "posSide", "pos_side", "side") or "").lower()
    market_price = _text(position, "lastPx", "markPx", "last_px")
    if not pos_id or side not in ("long", "short"):
        return None

    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.venue == venue)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.pos_id == pos_id)
            .order_by(ExecutionOrderLeg.id.desc())
            .first()
        )
        if leg is None:
            return None
        binding = session.get(ExecutionBinding, int(leg.execution_binding_id))
        live_legs = [
            row
            for row in session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.venue == venue)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(leg.execution_binding_id)
            )
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
            if str(row.pos_id or "").strip()
            and str(row.status or "").lower() in {"active", "partially_filled"}
            and str(row.attribution_status or "").lower() == "verified"
        ]
        live_pos_ids = {str(row.pos_id).strip() for row in live_legs} or {pos_id}
        ladder = derive_filled_tp_level(
            session,
            execution_binding_id=int(leg.execution_binding_id),
            pos_ids=sorted(live_pos_ids),
        )
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter_by(execution_binding_id=int(leg.execution_binding_id))
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
        existing_stops = tuple(
            price
            for price in (
                str(row.trigger_price or "").strip()
                for row in session.query(PositionProtectionLedger)
                .filter(PositionProtectionLedger.venue == venue)
                .filter(PositionProtectionLedger.pos_id == pos_id)
                .filter(PositionProtectionLedger.purpose.in_(STOP_PURPOSES))
                .filter(PositionProtectionLedger.status.in_(LEDGER_STATUSES))
                .order_by(PositionProtectionLedger.id.asc())
                .all()
            )
            if price
        )
        strategy_instance_id = (
            str(binding.strategy_instance_id or "") if binding is not None else None
        )
        symbol = str(binding.symbol) if binding is not None else None
        binding_id = int(leg.execution_binding_id)

    # The entry reference is the already-released rule (R1): which entry legs
    # still hold a position decides which end of the strategy's range the
    # level-1 target is. The ladder does not re-invent it.
    reference = resolve_break_even_reference(
        side=side,
        entry_range_low=getattr(lifecycle, "entry_range_low", None),
        entry_range_high=getattr(lifecycle, "entry_range_high", None),
        open_entry_leg_indexes=[leg_row.leg_index for leg_row in live_legs],
        actual_avg_entry_price=_text(position, "avgPx", "avg_px"),
    )
    target = ladder_target(
        side=side,
        filled_level=ladder.filled_level,
        rungs_by_position=ladder.rungs_by_position,
        levels_by_position=ladder.levels_by_position,
        break_even_reference_price=reference.price,
        break_even_reference_source=reference.source,
    )
    decision = ladder_decision(
        side=side,
        target=target,
        market_price=market_price,
        existing_stop_prices=existing_stops,
    )
    own_rungs = ladder.rungs_by_position.get(pos_id, ())
    return StopLadderShadowRow(
        pos_id=pos_id,
        instrument_id=instrument_id,
        side=side,
        filled_level=ladder.filled_level,
        would_action=decision.would_action,
        target_price=decision.target_price,
        target_source=decision.target_source,
        market_price=decision.market_price or market_price,
        effective_stop_price=decision.effective_stop_price,
        existing_stops=existing_stops,
        rungs=tuple(rung.as_evidence() for rung in own_rungs),
        per_position_levels=ladder.levels_by_position,
        execution_binding_id=binding_id,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        reason_code=decision.reason_code,
    )


def stop_ladder_dedupe_key(row: StopLadderShadowRow) -> str:
    """``(pos_id, level, would_action, target_price)`` -- the spec's key."""

    return "|".join(
        [
            row.pos_id,
            str(row.filled_level),
            row.would_action,
            row.target_price or "-",
        ]
    )


def _record_stop_ladder_event(
    session_factory, *, row: StopLadderShadowRow, venue: str, now: datetime
) -> bool:
    """Write one shadow event, unless it repeats the last one for this position."""

    import json

    from telegram_kol_research.models import ExecutionEvent

    action = STOP_LADDER_EVENT_ACTION.get(row.would_action)
    if action is None:
        return False
    key = stop_ladder_dedupe_key(row)
    detail = {**row.detail(), "dedupe_key": key, "recorded_at": now.isoformat()}
    with session_factory() as session:
        previous = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.venue == venue)
            .filter(ExecutionEvent.pos_id == row.pos_id)
            .filter(ExecutionEvent.action.in_(sorted(STOP_LADDER_EVENT_ACTIONS)))
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        if previous is not None:
            try:
                previous_key = json.loads(previous.after_json or "{}").get("dedupe_key")
            except (TypeError, ValueError):
                previous_key = None
            if previous_key == key:
                return False
        session.add(
            ExecutionEvent(
                execution_binding_id=row.execution_binding_id,
                strategy_instance_id=row.strategy_instance_id,
                venue=venue,
                action=action,
                status="shadow",
                symbol=row.symbol,
                side=row.side,
                pos_id=row.pos_id,
                reason=row.reason_code,
                after_json=json.dumps(detail, ensure_ascii=False, sort_keys=True),
                created_at=now.replace(tzinfo=None) if now.tzinfo else now,
            )
        )
        session.commit()
    return True


def _with_recorded(row: StopLadderShadowRow, recorded: bool) -> StopLadderShadowRow:
    return StopLadderShadowRow(
        pos_id=row.pos_id,
        instrument_id=row.instrument_id,
        side=row.side,
        filled_level=row.filled_level,
        would_action=row.would_action,
        target_price=row.target_price,
        target_source=row.target_source,
        market_price=row.market_price,
        effective_stop_price=row.effective_stop_price,
        existing_stops=row.existing_stops,
        rungs=row.rungs,
        per_position_levels=row.per_position_levels,
        execution_binding_id=row.execution_binding_id,
        strategy_instance_id=row.strategy_instance_id,
        symbol=row.symbol,
        reason_code=row.reason_code,
        recorded=recorded,
    )


def _backup_stop_order_ids(session, *, venue: str, pos_id: str) -> frozenset[str]:
    """Order ids on this position that are backup stops, from both records.

    The union of two sources on purpose, and the union is the safe direction:
    marking one order too many as a backup can only shrink the replacement set
    (and, if it swallows the primary, produce a refusal), whereas missing one
    would let a break-even cancel the backup. Over-refusing costs a round;
    under-refusing costs the position its second stop.
    """

    order_ids: set[str] = set()
    for row in (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.venue == venue)
        .filter(PositionBackupStopOrder.pos_id == pos_id)
        .all()
    ):
        text = str(getattr(row, "order_id", "") or "").strip()
        if text:
            order_ids.add(text)
    for row in (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.venue == venue)
        .filter(PositionProtectionLeg.pos_id == pos_id)
        .filter(PositionProtectionLeg.role == "backup_stop")
        .all()
    ):
        text = str(getattr(row, "exchange_order_id", "") or "").strip()
        if text:
            order_ids.add(text)
    return frozenset(order_ids)


def _with(row: BreakEvenShadowRow, **changes: Any) -> BreakEvenShadowRow:
    values = {
        "pos_id": row.pos_id,
        "instrument_id": row.instrument_id,
        "side": row.side,
        "entry_price": row.entry_price,
        "market_price": row.market_price,
        "action": row.action,
        "target_stop_price": row.target_stop_price,
        "current_stop_prices": row.current_stop_prices,
        "would_cancel_order_ids": row.would_cancel_order_ids,
        "would_close_size": row.would_close_size,
        "would_close_endpoint": row.would_close_endpoint,
        "would_close_ord_type": row.would_close_ord_type,
        "would_close_cancels_stops_first": row.would_close_cancels_stops_first,
        "stops_examined": row.stops_examined,
        "stops_resolved": row.stops_resolved,
        "legacy_would_refuse": row.legacy_would_refuse,
        "reason_code": row.reason_code,
    }
    values.update(changes)
    return BreakEvenShadowRow(**values)


def _decimal_equal(left: Any, right: Any) -> bool:
    """Prices are numbers. Comparing them as text rejected 27 positions (6f-1)."""

    if left is None or right is None:
        return False
    try:
        a = Decimal(str(left))
        b = Decimal(str(right))
    except (DecimalException, ValueError):
        return False
    if not (a.is_finite() and b.is_finite()):
        return False
    return a == b


def _has_size(row: Mapping[str, Any]) -> bool:
    for key in ("pos", "sz", "size", "availPos"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return abs(float(str(value))) > 0
        except (TypeError, ValueError):
            continue
    return False


def _text(row: Mapping[str, Any], *keys: str) -> str | None:
    """Local reader for fields the venue spells the same everywhere.

    Prices and position ids do **not** go through this -- they go through
    :mod:`deepcoin_trigger_rows`, whose whole purpose is that forgetting one of
    the venue's three vocabularies becomes an ImportError rather than a
    silently wrong value.
    """

    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return None


__all__ = [
    "BreakEvenShadowResult",
    "BreakEvenShadowRow",
    "STOP_LADDER_EVENT_ACTIONS",
    "StopLadderShadowResult",
    "StopLadderShadowRow",
    "run_break_even_shadow_pass",
    "run_stop_ladder_shadow",
    "stop_ladder_dedupe_key",
]
