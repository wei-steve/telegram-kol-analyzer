"""Where a break-even stop goes: the strategy's price, not our own fill.

Range entries are submitted as two legs and the first one is deliberately
greedy -- it takes whatever the market is offering at that instant rather than
waiting at the strategy's own price (``deepcoin_order_builder``:
``_range_entry_leg_prices``, plus the hybrid market replacement).  That choice
is ours, so its cost is ours; the *exit* level belongs to the strategy.  On
2026-09-20 the difference was 760 points on a BTC short, and breaking even at
our own fill would have meant leaving at a price the KOL never named.

So the target of a break-even stop is derived from the strategy's entry range
and from *which* entry legs still hold a position:

============================  ================================================
only entry leg 1 holds        the strategy's leg-1 price (short = range low,
                              long = range high)
only entry leg 2 holds        the other end of the range
both legs hold                the plain midpoint ``(low + high) / 2``
one single entry price        that price
no entry price at all         our actual average fill -- the behaviour that
                              existed before this module, unchanged
============================  ================================================

Two things this module deliberately does not do.  It never raises: an
unparseable range, a missing side or an unrecognised leg index all fall back to
the actual fill, because refusing to break even is the one answer the user
ruled out.  And it never touches the *identity* use of
``avg_entry_price`` -- the comparison against the exchange's ``avgPx`` that
decides whether a position is still the one we planned against.  That is what
:func:`break_even_target_price` is for: one accessor for the target price, so
the two meanings the column used to carry are separable at every call site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

#: Keys the planner writes into a management leg's ``planned_tpsl_json`` and
#: the execution side reads back.  A batch planned before 2026-09-21 carries
#: neither, which is exactly the "no reference" case.
BREAK_EVEN_REFERENCE_PRICE_KEY = "break_even_reference_price"
BREAK_EVEN_REFERENCE_SOURCE_KEY = "break_even_reference_source"

STRATEGY_FIRST_LEG = "strategy_first_leg"
STRATEGY_SECOND_LEG = "strategy_second_leg"
STRATEGY_MIDPOINT = "strategy_midpoint"
STRATEGY_SINGLE_PRICE = "strategy_single_price"
ACTUAL_FILL_NO_STRATEGY_PRICE = "actual_fill_no_strategy_price"
#: The message named a placeable stop tighter than the strategy's own price.
MESSAGE_EXPLICIT_TIGHTER = "message_explicit_tighter"

STRATEGY_SOURCES = frozenset(
    {
        STRATEGY_FIRST_LEG,
        STRATEGY_SECOND_LEG,
        STRATEGY_MIDPOINT,
        STRATEGY_SINGLE_PRICE,
        MESSAGE_EXPLICIT_TIGHTER,
    }
)


@dataclass(frozen=True, slots=True)
class BreakEvenReference:
    """One batch's break-even target, and the inputs that chose it."""

    price: str | None
    source: str
    evidence: dict[str, Any]

    @property
    def is_strategy_price(self) -> bool:
        """Whether this reference says anything the actual fill does not."""

        return self.source in STRATEGY_SOURCES and self.price is not None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "source": self.source,
            "evidence": dict(self.evidence),
        }


def resolve_break_even_reference(
    *,
    side: Any,
    entry_range_low: Any,
    entry_range_high: Any,
    open_entry_leg_indexes: Iterable[Any],
    actual_avg_entry_price: Any,
) -> BreakEvenReference:
    """Return the price a break-even stop for this batch should aim at.

    Never raises.  Every unusable input lands on the actual fill, which is
    what this path did before the strategy price existed.
    """

    normalized_side = str(side or "").strip().lower()
    low = _positive_decimal(entry_range_low)
    high = _positive_decimal(entry_range_high)
    indexes = _leg_indexes(open_entry_leg_indexes)
    evidence: dict[str, Any] = {
        "side": normalized_side,
        "entry_range_low": _text(low),
        "entry_range_high": _text(high),
        "open_entry_leg_indexes": sorted(indexes),
    }
    fallback = BreakEvenReference(
        price=_fallback_price(actual_avg_entry_price),
        source=ACTUAL_FILL_NO_STRATEGY_PRICE,
        evidence=evidence,
    )
    if normalized_side not in {"long", "short"} or low is None or high is None:
        return fallback
    if low == high:
        # One entry price: both legs mean the same thing, so which legs are
        # open cannot change the answer.
        return BreakEvenReference(
            price=_text(low), source=STRATEGY_SINGLE_PRICE, evidence=evidence
        )
    if low > high:
        return fallback
    if {1, 2} <= indexes:
        return BreakEvenReference(
            price=_text((low + high) / Decimal(2)),
            source=STRATEGY_MIDPOINT,
            evidence=evidence,
        )
    if indexes == {1}:
        return BreakEvenReference(
            price=_text(low if normalized_side == "short" else high),
            source=STRATEGY_FIRST_LEG,
            evidence=evidence,
        )
    if indexes == {2}:
        return BreakEvenReference(
            price=_text(high if normalized_side == "short" else low),
            source=STRATEGY_SECOND_LEG,
            evidence=evidence,
        )
    return fallback


def adopted_break_even_reference(
    reference: BreakEvenReference, *, price: Any
) -> BreakEvenReference:
    """The same reference, moved to a tighter price the message named."""

    return BreakEvenReference(
        price=_text(_positive_decimal(price)),
        source=MESSAGE_EXPLICIT_TIGHTER,
        evidence={
            **reference.evidence,
            "superseded_source": reference.source,
            "superseded_price": reference.price,
        },
    )


def planned_tpsl_reference_fields(
    reference: BreakEvenReference | None,
) -> dict[str, str]:
    """The planned-TPSL keys this reference contributes.

    A reference that resolved to our own fill contributes nothing at all: the
    leg's ``avg_entry_price`` already says that, and writing it twice would
    make a batch that behaves identically look different from one planned
    before this rule existed.
    """

    if reference is None or not reference.is_strategy_price:
        return {}
    return {
        BREAK_EVEN_REFERENCE_PRICE_KEY: str(reference.price),
        BREAK_EVEN_REFERENCE_SOURCE_KEY: reference.source,
    }


def break_even_target_price(leg: Any) -> str | None:
    """The price this leg's break-even stop aims at.

    The strategy reference when the batch carries one, and otherwise the
    leg's own ``avg_entry_price`` -- verbatim, so a batch planned before
    2026-09-21 executes byte for byte as it would have.  This is the *only*
    reader of the reference; every comparison against the exchange's own
    ``avgPx`` keeps reading ``avg_entry_price`` directly.
    """

    planned = _planned_tpsl_mapping(leg)
    reference = _positive_decimal(planned.get(BREAK_EVEN_REFERENCE_PRICE_KEY))
    if reference is None:
        return getattr(leg, "avg_entry_price", None)
    return str(planned[BREAK_EVEN_REFERENCE_PRICE_KEY]).strip()


def _planned_tpsl_mapping(leg: Any) -> dict[str, Any]:
    planned = getattr(leg, "planned_tpsl", None)
    if planned is None:
        raw = getattr(leg, "planned_tpsl_json", None)
        if raw in (None, ""):
            return {}
        try:
            planned = json.loads(str(raw))
        except (TypeError, ValueError):
            return {}
    return planned if isinstance(planned, dict) else {}


def _leg_indexes(values: Iterable[Any]) -> set[int]:
    indexes: set[int] = set()
    try:
        candidates = list(values)
    except TypeError:
        return indexes
    for value in candidates:
        try:
            indexes.add(int(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return indexes


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return number if number.is_finite() and number > 0 else None


def _fallback_price(value: Any) -> str | None:
    number = _positive_decimal(value)
    return None if number is None else _text(number)


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"", "-0"} else normalized
