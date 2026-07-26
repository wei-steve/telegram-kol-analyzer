from __future__ import annotations

import pytest

from telegram_kol_research.strategy_management_market_policy import (
    BreakEvenMarketPolicyError,
    assess_break_even_market,
)


@pytest.mark.parametrize(
    ("side", "entry_price", "market_price", "allowed", "comparison"),
    [
        ("long", "100", "101", True, "entry_below_market"),
        ("long", "101", "101", False, "entry_equal_market"),
        ("long", "102", "101", False, "entry_above_market"),
        ("short", "101", "100", True, "entry_above_market"),
        ("short", "100", "100", False, "entry_equal_market"),
        ("short", "100", "101", False, "entry_below_market"),
    ],
)
def test_assess_break_even_market_uses_strict_directional_boundary(
    side, entry_price, market_price, allowed, comparison
):
    decision = assess_break_even_market(
        side=side,
        entry_price=entry_price,
        market_price=market_price,
    )

    assert decision.side == side
    assert decision.entry_price == entry_price
    assert decision.market_price == market_price
    assert decision.allowed is allowed
    assert decision.comparison == comparison
    assert decision.fallback_action == (None if allowed else "full_exit")


@pytest.mark.parametrize(
    ("side", "entry_price", "market_price"),
    [
        ("flat", "100", "101"),
        ("long", None, "101"),
        ("long", "0", "101"),
        ("long", "NaN", "101"),
        ("short", "100", None),
        ("short", "100", "-1"),
        ("short", "100", "Infinity"),
    ],
)
def test_assess_break_even_market_rejects_unusable_inputs(
    side, entry_price, market_price
):
    with pytest.raises(BreakEvenMarketPolicyError):
        assess_break_even_market(
            side=side,
            entry_price=entry_price,
            market_price=market_price,
        )
