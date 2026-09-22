"""The stop ladder, as arithmetic: which rung filled, and where the stop goes.

The account owner's rule, in one line: **a stop follows the take profits that
actually traded, one rung behind.**  Reached stage 1 and the stop goes to the
strategy's entry price; reached stage 2 and it goes to the first take-profit
price; stage N goes to stage N-1, with "stage 0" being the entry
(``docs/plans/2026-09-21-stop-ladder-policy.md`` section 2).

Three decisions in that rule are easy to implement wrongly, so they are stated
here and each one is a test:

* **A rung is a ledger row, not a price in the message.**  The ladder is this
  position's own ``purpose='take_profit'`` protection ledger rows, minus the
  ``retired``/``cancelled``/``superseded`` ones, sorted along the profit
  direction -- a long's rungs ascend, a short's descend.  When a KOL moves a
  take profit, the system cancels the old order and places a new one, so the
  sequence follows without anything here re-reading the strategy text; and a
  position whose ladder was shortened for size simply has fewer rungs
  (policy section 4, ruling 4).
* **Quantity is never compared.**  A stage that filled partially, or whose
  size never matched the allocation percentage, is still a stage that filled.
  Reachedness is "did this order trade", nothing more (ruling 3).
* **Unprovable means unchanged.**  Every function here refuses rather than
  guesses, and refusing leaves the stop exactly where it is -- the position
  keeps the protection it already has, which is the safe direction.  Nothing
  in this module alerts: the user asked for counters, not alarms.

Everything here is pure.  No session, no client, no clock, and no exception:
an unreadable price, an unknown side or a malformed row yields "no rung", "not
reached" or "no change".  The persistence side lives in
:mod:`telegram_kol_research.stop_ladder_records`, and the exchange side does
not exist in phase 1 at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from telegram_kol_research.strategy_management_market_policy import (
    stop_is_at_least_as_protective,
)
from telegram_kol_research.take_profit_fill_predicate import (
    EVIDENCE_FORM_BY_TIER,
    EVIDENCE_FORM_POSITION_DECREASE,
    EVIDENCE_FORM_TRIGGER_HISTORY,
    take_profit_fill_proven,
)

#: ``position_protection_ledger.purpose`` values that are take profits. The
#: same set ``protection_health`` uses to decide what may be written ``filled``.
TAKE_PROFIT_LEDGER_PURPOSES = frozenset({"take_profit", "tp", "profit"})

#: Ledger statuses that mean "this row is history": the order is not part of
#: the ladder any more, and the rungs below it move up a number.
RUNG_HISTORY_STATUSES = frozenset({"retired", "cancelled", "canceled", "superseded"})

#: The ledger status a reached rung carries.  One writer only
#: (``protection_health.record_take_profit_ledger_fill``).
LEDGER_FILLED_STATUS = "filled"

ACTION_REPLACE_STOP = "replace_stop"
ACTION_CLOSE_AT_MARKET = "close_at_market"
ACTION_NO_CHANGE = "no_change"

TARGET_SOURCE_NO_FILL = "no_take_profit_filled"
TARGET_SOURCE_BREAK_EVEN_REFERENCE = "break_even_reference"


def take_profit_level_source(level: int) -> str:
    return f"strategy_take_profit_level_{int(level)}"


REASON_ORDER_STILL_PENDING = "stop_ladder_order_still_pending"
REASON_CANCEL_INTENT = "stop_ladder_cancel_intended"
REASON_LEDGER_HISTORY = "stop_ladder_ledger_row_is_history"
REASON_SNAPSHOT_INCOMPLETE = "stop_ladder_pending_snapshot_incomplete"
REASON_NO_FILL = "stop_ladder_no_take_profit_filled"
REASON_NO_RUNG_BELOW = "stop_ladder_rung_below_level_missing"
REASON_REFERENCE_MISSING = "stop_ladder_break_even_reference_missing"
REASON_SIDE_INVALID = "stop_ladder_side_invalid"
REASON_MARKET_PRICE_INVALID = "stop_ladder_market_price_invalid"
REASON_TARGET_MISSING = "stop_ladder_target_missing"
#: Not an alert. The spec is explicit: an unprovable rung is counted and the
#: stop stays where it is.
REASON_UNPROVEN = "stop_ladder_unproven"

_SIDES = frozenset({"long", "short"})


@dataclass(frozen=True, slots=True)
class LadderRung:
    """One take-profit order of this position, numbered along the profit direction."""

    level: int
    order_id: str
    trigger_price: str
    size_text: str | None = None
    status: str = ""
    ledger_row_id: int | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "order_id": self.order_id,
            "trigger_price": self.trigger_price,
            "size_text": self.size_text,
            "status": self.status,
            "ledger_row_id": self.ledger_row_id,
        }


@dataclass(frozen=True, slots=True)
class RungVerdict:
    """Whether one rung has been reached, and on what evidence."""

    order_id: str
    level: int
    reached: bool
    evidence_form: str | None = None
    reason_code: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "level": self.level,
            "reached": self.reached,
            "evidence_form": self.evidence_form,
            "reason_code": self.reason_code,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class LadderTarget:
    """Where the stop should be aiming, given the level the strategy reached."""

    level: int
    price: str | None
    source: str
    reason_code: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "price": self.price,
            "source": self.source,
            "reason_code": self.reason_code,
            **({"evidence": dict(self.evidence)} if self.evidence else {}),
        }


@dataclass(frozen=True, slots=True)
class LadderDecision:
    """What a ladder round would do -- computed, never done, in phase 1."""

    would_action: str
    level: int
    target_price: str | None = None
    target_source: str | None = None
    market_price: str | None = None
    effective_stop_price: str | None = None
    reason_code: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "would_action": self.would_action,
            "level": self.level,
            "target_price": self.target_price,
            "target_source": self.target_source,
            "market_price": self.market_price,
            "effective_stop_price": self.effective_stop_price,
            "reason_code": self.reason_code,
        }


def rungs_for_position(
    ledger_rows: Iterable[Any] | None, *, side: Any
) -> tuple[LadderRung, ...]:
    """This position's ladder, numbered from the entry outwards.

    ``ledger_rows`` are ``position_protection_ledger`` rows -- ORM objects or
    mappings alike.  Rows that are not take profits, rows the ladder has
    already written off as history, and rows without a usable order id or
    trigger price are dropped, because a rung that cannot be named cannot be
    counted either.
    """

    normalized_side = _side(side)
    if normalized_side is None:
        return ()
    candidates: list[tuple[Decimal, str, LadderRung]] = []
    for row in _iterable(ledger_rows):
        purpose = _text(_attr(row, "purpose")).lower()
        if purpose not in TAKE_PROFIT_LEDGER_PURPOSES:
            continue
        status = _text(_attr(row, "status")).lower()
        if status in RUNG_HISTORY_STATUSES:
            continue
        order_id = _text(_attr(row, "order_id"))
        price = _positive_decimal(_attr(row, "trigger_price"))
        if not order_id or price is None:
            continue
        candidates.append(
            (
                price,
                order_id,
                LadderRung(
                    level=0,
                    order_id=order_id,
                    trigger_price=_text(_attr(row, "trigger_price")),
                    size_text=_text(_attr(row, "size_text")) or None,
                    status=status,
                    ledger_row_id=_integer(_attr(row, "id")),
                ),
            )
        )
    # The profit direction is the only ordering that means anything: a long
    # takes profit above the entry, a short below it. Order id breaks a tie so
    # that two stages at one price still number deterministically.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=normalized_side == "short")
    return tuple(
        LadderRung(
            level=index,
            order_id=rung.order_id,
            trigger_price=rung.trigger_price,
            size_text=rung.size_text,
            status=rung.status,
            ledger_row_id=rung.ledger_row_id,
        )
        for index, (_price, _order_id, rung) in enumerate(candidates, start=1)
    )


def rung_reached(
    *,
    rung: LadderRung,
    pending_order_ids: Iterable[Any] = (),
    pending_snapshot_complete: bool = False,
    trigger_history: Iterable[Mapping[str, Any]] = (),
    cancel_intent_order_ids: Iterable[Any] = (),
    position_decrease_proven: bool | None = None,
) -> RungVerdict:
    """Has this rung traded?  Four exclusions first, then the shared predicate.

    The exclusions are the whole safety of the thing, and each of them answers
    a way a take-profit order can leave ``trigger-orders-pending`` without
    having filled: an incomplete read (it may still be there), our own cancel
    intent (we took it off), a ledger row already written off as history
    (replaced), and the order still being listed (it never left).  Only then
    is :func:`take_profit_fill_proven` asked, with form A its clean trigger
    history row and form B the position decrease.
    """

    order_id = _text(getattr(rung, "order_id", ""))
    level = _integer(getattr(rung, "level", 0)) or 0
    if not order_id:
        return RungVerdict(
            order_id=order_id, level=level, reached=False, reason_code=REASON_UNPROVEN
        )
    if _text(getattr(rung, "status", "")).lower() in RUNG_HISTORY_STATUSES:
        return RungVerdict(
            order_id=order_id,
            level=level,
            reached=False,
            reason_code=REASON_LEDGER_HISTORY,
        )
    if order_id in _text_set(cancel_intent_order_ids):
        return RungVerdict(
            order_id=order_id,
            level=level,
            reached=False,
            reason_code=REASON_CANCEL_INTENT,
        )
    if not pending_snapshot_complete:
        return RungVerdict(
            order_id=order_id,
            level=level,
            reached=False,
            reason_code=REASON_SNAPSHOT_INCOMPLETE,
        )
    if order_id in _text_set(pending_order_ids):
        return RungVerdict(
            order_id=order_id,
            level=level,
            reached=False,
            reason_code=REASON_ORDER_STILL_PENDING,
        )
    verdict = take_profit_fill_proven(
        order_id=order_id,
        trigger_history=trigger_history,
        pending_snapshot_complete=True,
        position_decrease_proven=position_decrease_proven,
    )
    if not verdict.proven:
        return RungVerdict(
            order_id=order_id,
            level=level,
            reached=False,
            reason_code=verdict.reason_code,
            evidence=dict(verdict.evidence),
        )
    return RungVerdict(
        order_id=order_id,
        level=level,
        reached=True,
        evidence_form=EVIDENCE_FORM_BY_TIER.get(
            verdict.evidence_tier or "", EVIDENCE_FORM_TRIGGER_HISTORY
        ),
        evidence={
            **dict(verdict.evidence),
            "evidence_tier": verdict.evidence_tier,
        },
    )


def filled_level_for_position(verdicts: Iterable[RungVerdict] | None) -> int:
    """The highest rung of this position that has been reached, or 0."""

    levels = [
        _integer(getattr(verdict, "level", 0)) or 0
        for verdict in _iterable(verdicts)
        if getattr(verdict, "reached", False)
    ]
    return max(levels) if levels else 0


def strategy_filled_level(levels_by_position: Mapping[str, Any] | None) -> int:
    """The strategy's level is the highest any of its legs reached (ruling 1).

    Taking the minimum would leave the second leg holding the strategy stop
    after the price has already been through the first take profit -- the
    opposite of the rule.
    """

    levels = [
        _integer(value) or 0
        for value in (levels_by_position or {}).values()
    ]
    return max(levels) if levels else 0


def ladder_target(
    *,
    side: Any,
    filled_level: Any,
    rungs_by_position: Mapping[str, Sequence[LadderRung]] | None = None,
    levels_by_position: Mapping[str, Any] | None = None,
    break_even_reference_price: Any = None,
    break_even_reference_source: Any = None,
) -> LadderTarget:
    """Where the stop aims: level 1 the entry reference, level N rung N-1.

    With two legs at the same level the more protective of their two rung
    prices wins -- higher for a long, lower for a short -- because the rule
    moves both legs' stops together and the looser of the two would give back
    protection the other leg had already earned.
    """

    level = _integer(filled_level) or 0
    normalized_side = _side(side)
    if normalized_side is None:
        return LadderTarget(
            level=level,
            price=None,
            source=TARGET_SOURCE_NO_FILL,
            reason_code=REASON_SIDE_INVALID,
        )
    if level <= 0:
        return LadderTarget(
            level=0,
            price=None,
            source=TARGET_SOURCE_NO_FILL,
            reason_code=REASON_NO_FILL,
        )
    if level == 1:
        price = _positive_decimal(break_even_reference_price)
        if price is None:
            return LadderTarget(
                level=level,
                price=None,
                source=TARGET_SOURCE_BREAK_EVEN_REFERENCE,
                reason_code=REASON_REFERENCE_MISSING,
            )
        return LadderTarget(
            level=level,
            price=_text(break_even_reference_price),
            source=(
                _text(break_even_reference_source)
                or TARGET_SOURCE_BREAK_EVEN_REFERENCE
            ),
        )

    source = take_profit_level_source(level - 1)
    prices: list[Decimal] = []
    for pos_id, rungs in (rungs_by_position or {}).items():
        if (_integer((levels_by_position or {}).get(pos_id)) or 0) != level:
            # Only a leg that actually reached this level may name the price:
            # another leg's ladder can be at completely different prices.
            continue
        for rung in _iterable(rungs):
            if (_integer(getattr(rung, "level", 0)) or 0) != level - 1:
                continue
            price = _positive_decimal(getattr(rung, "trigger_price", None))
            if price is not None:
                prices.append(price)
    if not prices:
        return LadderTarget(
            level=level,
            price=None,
            source=source,
            reason_code=REASON_NO_RUNG_BELOW,
        )
    chosen = max(prices) if normalized_side == "long" else min(prices)
    return LadderTarget(level=level, price=_format(chosen), source=source)


def ladder_decision(
    *,
    side: Any,
    target: LadderTarget | None,
    market_price: Any,
    existing_stop_prices: Iterable[Any] = (),
) -> LadderDecision:
    """What this position's stop would become.  Computed only, in every mode.

    Three answers and no fourth: replace the stop, close at market because the
    market has already passed the target (the user's own ruling -- "that
    generally means the price has gone against us"), or change nothing because
    the stop resting there already protects at least as much.
    """

    level = _integer(getattr(target, "level", 0)) or 0
    target_price = getattr(target, "price", None)
    target_source = getattr(target, "source", None)
    normalized_side = _side(side)
    if normalized_side is None:
        return LadderDecision(
            would_action=ACTION_NO_CHANGE,
            level=level,
            reason_code=REASON_SIDE_INVALID,
        )
    if target is None or target_price is None:
        return LadderDecision(
            would_action=ACTION_NO_CHANGE,
            level=level,
            target_source=target_source,
            reason_code=getattr(target, "reason_code", None) or REASON_TARGET_MISSING,
        )
    goal = _positive_decimal(target_price)
    market = _positive_decimal(market_price)
    if goal is None or market is None:
        return LadderDecision(
            would_action=ACTION_NO_CHANGE,
            level=level,
            target_price=_text(target_price) or None,
            target_source=target_source,
            market_price=_text(market_price) or None,
            reason_code=REASON_MARKET_PRICE_INVALID,
        )
    market_text = _format(market)
    if (normalized_side == "long" and goal >= market) or (
        normalized_side == "short" and goal <= market
    ):
        return LadderDecision(
            would_action=ACTION_CLOSE_AT_MARKET,
            level=level,
            target_price=_format(goal),
            target_source=target_source,
            market_price=market_text,
        )
    protective = [
        stop
        for stop in (
            _positive_decimal(value) for value in _iterable(existing_stop_prices)
        )
        if stop is not None
        and stop_is_at_least_as_protective(
            existing=stop,
            target=goal,
            side=normalized_side,
            market_price=market,
        )
    ]
    if protective:
        effective = max(protective) if normalized_side == "long" else min(protective)
        return LadderDecision(
            would_action=ACTION_NO_CHANGE,
            level=level,
            target_price=_format(goal),
            target_source=target_source,
            market_price=market_text,
            effective_stop_price=_format(effective),
            reason_code="stop_ladder_existing_stop_already_protective",
        )
    return LadderDecision(
        would_action=ACTION_REPLACE_STOP,
        level=level,
        target_price=_format(goal),
        target_source=target_source,
        market_price=market_text,
    )


# ------------------------------------------------------------------ readers


def _attr(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _iterable(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _text_set(values: Iterable[Any]) -> set[str]:
    return {text for value in _iterable(values) if (text := _text(value))}


def _side(value: Any) -> str | None:
    normalized = _text(value).lower()
    return normalized if normalized in _SIDES else None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(_text(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _format(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"", "-0"} else normalized


__all__ = [
    "ACTION_CLOSE_AT_MARKET",
    "ACTION_NO_CHANGE",
    "ACTION_REPLACE_STOP",
    "EVIDENCE_FORM_POSITION_DECREASE",
    "EVIDENCE_FORM_TRIGGER_HISTORY",
    "LEDGER_FILLED_STATUS",
    "LadderDecision",
    "LadderRung",
    "LadderTarget",
    "RUNG_HISTORY_STATUSES",
    "RungVerdict",
    "TAKE_PROFIT_LEDGER_PURPOSES",
    "filled_level_for_position",
    "ladder_decision",
    "ladder_target",
    "rung_reached",
    "rungs_for_position",
    "strategy_filled_level",
    "take_profit_level_source",
]
