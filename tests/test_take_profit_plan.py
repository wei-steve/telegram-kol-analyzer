from decimal import Decimal

import pytest

from telegram_kol_research.take_profit_plan import TakeProfitPlanError
from telegram_kol_research.take_profit_plan import build_take_profit_plan


def test_four_targets_default_to_front_loaded_equal_remainder():
    plan = build_take_profit_plan(
        prices=[67500, 68500, 69500, 70500], side="long",
        configured_allocations=[50, 30, 20],
    )

    assert [(leg.price, leg.allocation_pct) for leg in plan.legs] == [
        ("67500", "40"), ("68500", "20"),
        ("69500", "20"), ("70500", "20"),
    ]


def test_five_short_targets_are_nearest_first_and_use_40_15_default():
    plan = build_take_profit_plan(
        prices=[64200, 65250, 64750, 65150, 63800], side="short",
        configured_allocations=[50, 30, 20],
    )

    assert [leg.price for leg in plan.legs] == [
        "65250", "65150", "64750", "64200", "63800",
    ]
    assert [leg.allocation_pct for leg in plan.legs] == [
        "40", "15", "15", "15", "15",
    ]


@pytest.mark.parametrize(("prices", "expected"), [
    ([69000], ["100"]),
    ([69000, 70000], ["50", "50"]),
    ([69000, 70000, 71000], ["40", "30", "30"]),
])
def test_legacy_target_counts_keep_existing_defaults(prices, expected):
    plan = build_take_profit_plan(prices=prices, side="long", configured_allocations=[40, 30, 30])

    assert [leg.allocation_pct for leg in plan.legs] == expected


def test_exact_length_custom_five_stage_allocation_overrides_default():
    plan = build_take_profit_plan(
        prices=[1, 2, 3, 4, 5], side="long",
        configured_allocations=[30, 25, 20, 15, 10],
    )

    assert [leg.allocation_pct for leg in plan.legs] == ["30", "25", "20", "15", "10"]


@pytest.mark.parametrize("prices", [
    [1, 1], [1, 0], [1, 2, 3, 4, 5, 6],
])
def test_invalid_target_prices_fail_closed(prices):
    with pytest.raises(TakeProfitPlanError):
        build_take_profit_plan(prices=prices, side="long", configured_allocations=[])


def test_btc_four_stage_quantities_sum_exactly_to_position():
    plan = build_take_profit_plan(
        prices=[67500, 68500, 69500, 70500], side="long",
        configured_allocations=[], quantity="25", quantity_step="1", minimum_quantity="1",
    )

    assert [leg.quantity for leg in plan.legs] == ["10", "5", "5", "5"]
    assert sum(Decimal(leg.quantity) for leg in plan.legs) == Decimal("25")


def test_eth_four_stage_quantities_use_decimal_step_and_exact_remainder():
    plan = build_take_profit_plan(
        prices=[1900, 1920, 1940, 1960], side="long",
        configured_allocations=[], quantity="2.4", quantity_step="0.1", minimum_quantity="0.1",
    )

    assert [leg.quantity for leg in plan.legs] == ["0.9", "0.4", "0.4", "0.7"]
    assert sum(Decimal(leg.quantity) for leg in plan.legs) == Decimal("2.4")


def test_undersized_position_fails_instead_of_dropping_a_target():
    with pytest.raises(TakeProfitPlanError, match="minimum"):
        build_take_profit_plan(
            prices=[1, 2, 3, 4, 5], side="long", configured_allocations=[],
            quantity="0.3", quantity_step="0.1", minimum_quantity="0.1",
        )
