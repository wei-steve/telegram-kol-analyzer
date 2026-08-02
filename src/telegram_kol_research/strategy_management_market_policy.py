"""Pure market-side policy for moving a position stop to break-even."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class BreakEvenMarketPolicyError(ValueError):
    """Raised when a break-even market decision cannot be made safely."""


@dataclass(frozen=True, slots=True)
class BreakEvenMarketDecision:
    side: str
    entry_price: str
    market_price: str
    comparison: str
    allowed: bool
    fallback_action: str | None


@dataclass(frozen=True, slots=True)
class BreakEvenProtectionDecision:
    market: BreakEvenMarketDecision
    action: str
    effective_stop_price: str | None


def assess_break_even_market(
    *, side: Any, entry_price: Any, market_price: Any
) -> BreakEvenMarketDecision:
    """Return whether an entry-price stop is strictly valid for this side."""

    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"long", "short"}:
        raise BreakEvenMarketPolicyError("break_even_side_invalid")
    entry = _positive_decimal(
        entry_price, reason="break_even_entry_price_invalid"
    )
    market = _positive_decimal(
        market_price, reason="break_even_market_price_invalid"
    )
    if entry < market:
        comparison = "entry_below_market"
    elif entry > market:
        comparison = "entry_above_market"
    else:
        comparison = "entry_equal_market"
    allowed = (
        comparison == "entry_below_market"
        if normalized_side == "long"
        else comparison == "entry_above_market"
    )
    return BreakEvenMarketDecision(
        side=normalized_side,
        entry_price=_format_decimal(entry),
        market_price=_format_decimal(market),
        comparison=comparison,
        allowed=allowed,
        fallback_action=None if allowed else "full_exit",
    )


def assess_break_even_with_existing_stop(
    *,
    side: Any,
    entry_price: Any,
    market_price: Any,
    existing_stop_prices: list[Any] | tuple[Any, ...],
) -> BreakEvenProtectionDecision:
    """Keep an already break-even-or-better stop, otherwise use market policy."""

    market = assess_break_even_market(
        side=side,
        entry_price=entry_price,
        market_price=market_price,
    )
    if not market.allowed:
        return BreakEvenProtectionDecision(
            market=market,
            action="full_exit",
            effective_stop_price=None,
        )
    entry = _positive_decimal(
        entry_price, reason="break_even_entry_price_invalid"
    )
    stops = [
        _positive_decimal(value, reason="break_even_existing_stop_invalid")
        for value in existing_stop_prices
    ]
    qualifying = [
        stop
        for stop in stops
        if (stop >= entry if market.side == "long" else stop <= entry)
    ]
    if qualifying:
        effective = (
            max(qualifying) if market.side == "long" else min(qualifying)
        )
        return BreakEvenProtectionDecision(
            market=market,
            action="keep_tighter_stop",
            effective_stop_price=_format_decimal(effective),
        )
    return BreakEvenProtectionDecision(
        market=market,
        action="set_break_even",
        effective_stop_price=None,
    )


def _positive_decimal(value: Any, *, reason: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BreakEvenMarketPolicyError(reason) from None
    if not number.is_finite() or number <= 0:
        raise BreakEvenMarketPolicyError(reason)
    return number


def _format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized == "-0" else normalized
