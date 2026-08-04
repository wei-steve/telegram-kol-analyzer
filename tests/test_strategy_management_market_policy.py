from __future__ import annotations

import pytest

from telegram_kol_research.strategy_management_market_policy import (
    BreakEvenMarketPolicyError,
    assess_break_even_market,
    assess_break_even_with_existing_stop,
    plan_composite_stop_replacement,
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


@pytest.mark.parametrize(
    ("side", "entry_price", "market_price", "stop_price"),
    [
        ("long", "100", "110", "101"),
        ("long", "100", "110", "100"),
        ("short", "100", "90", "99"),
        ("short", "100", "90", "100"),
    ],
)
def test_existing_break_even_or_tighter_stop_is_kept_without_a_write(
    side, entry_price, market_price, stop_price
):
    decision = assess_break_even_with_existing_stop(
        side=side,
        entry_price=entry_price,
        market_price=market_price,
        existing_stop_prices=[stop_price],
    )

    assert decision.action == "keep_tighter_stop"
    assert decision.effective_stop_price == stop_price
    assert decision.market.allowed is True


@pytest.mark.parametrize(
    ("side", "entry_price", "market_price", "stop_price", "action"),
    [
        ("long", "100", "110", "99", "set_break_even"),
        ("short", "100", "90", "101", "set_break_even"),
        ("long", "100", "99", "98", "full_exit"),
        ("short", "100", "101", "102", "full_exit"),
    ],
)
def test_weaker_stop_never_overrides_market_side_decision(
    side, entry_price, market_price, stop_price, action
):
    decision = assess_break_even_with_existing_stop(
        side=side,
        entry_price=entry_price,
        market_price=market_price,
        existing_stop_prices=[stop_price],
    )

    assert decision.action == action


@pytest.mark.parametrize(
    ("side", "requested", "market", "tick", "expected_primary", "expected_backup"),
    [
        ("long", "62700.08", "64000", "0.1", "62700", "62574.6"),
        ("short", "62841.61", "62000", "0.1", "62841.7", "62967.4"),
    ],
)
def test_composite_stop_plan_rounds_safely_and_builds_backup(
    side, requested, market, tick, expected_primary, expected_backup
):
    decision = plan_composite_stop_replacement(
        side=side,
        requested_stop=requested,
        market_price=market,
        price_tick=tick,
        backup_buffer_bps="20",
        existing_stop_prices=[],
    )

    assert decision.action == "replace"
    assert decision.primary_stop == expected_primary
    assert decision.backup_stop == expected_backup


def test_composite_stop_plan_keeps_an_already_tighter_stop():
    decision = plan_composite_stop_replacement(
        side="long",
        requested_stop="62700",
        market_price="64000",
        price_tick="0.1",
        backup_buffer_bps="20",
        existing_stop_prices=["63000"],
    )

    assert decision.action == "keep_tighter_stop"
    assert decision.primary_stop == "63000"


def test_composite_stop_plan_refuses_market_already_through_stop():
    with pytest.raises(
        BreakEvenMarketPolicyError, match="requested_stop_market_side_invalid"
    ):
        plan_composite_stop_replacement(
            side="long",
            requested_stop="62700",
            market_price="62600",
            price_tick="0.1",
            backup_buffer_bps="20",
            existing_stop_prices=[],
        )
