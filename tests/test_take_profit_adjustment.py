"""Take-profit adjustment: recognition and per-position planning (pure).

Design: docs/plans/2026-09-29-take-profit-adjustment-design.md, section 5.5
for the percentage rule. The replay messages are real production text and
recognition payloads, nothing else.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from telegram_kol_research import management_directives
from telegram_kol_research.management_directives import resolve_management_directive
from telegram_kol_research.take_profit_adjustment import (
    MODE_FULL_RESET,
    MODE_PRICES_ONLY,
    MODE_RATIOS_ONLY,
    MODE_SINGLE_TIER,
    PLAN_ALREADY_SATISFIED,
    PLAN_READY,
    PLAN_REFUSED,
    REASON_ALL_TIERS_CROSSED,
    REASON_PRICE_MISSING,
    REASON_SIZE_BELOW_MINIMUM,
    REASON_TIER_COUNT_AMBIGUOUS,
    TakeProfitInstruction,
    classify_take_profit_instruction,
    plan_take_profit_adjustment,
    proportional_allocations,
)
from telegram_kol_research.trigger_take_profit_convergence_executor import (
    _allocate_sizes,
)

FIXTURE = Path(__file__).parent / "fixtures" / "take_profit_adjustment_replay.json"
MESSAGES = {
    int(row["raw_message_id"]): row
    for row in json.loads(FIXTURE.read_text(encoding="utf-8"))["messages"]
}


def _event(raw_id: int, **overrides):
    event = dict(MESSAGES[raw_id]["lifecycle_event"] or {})
    event.pop("targets", None)
    event.setdefault("event_type", "position_update")
    event.update(overrides)
    return event


def _classify(raw_id: int, **overrides):
    return classify_take_profit_instruction(
        MESSAGES[raw_id]["text"], _event(raw_id, **overrides)
    )


# ---------------------------------------------------------------- recognition


def test_18199_two_tiers_fifty_each_is_an_allocation_not_a_close():
    instruction = _classify(18199)

    assert instruction.mode == MODE_RATIOS_ONLY
    assert instruction.prices == ()
    assert instruction.allocations == ("50", "50")
    assert instruction.stop_loss is None


def test_ouyang_template_takes_the_take_profit_and_the_labelled_stop():
    btc = _classify(13848)
    eth = _classify(17745)

    assert (btc.mode, btc.prices, btc.stop_loss) == (MODE_PRICES_ONLY, ("73070",), "78700")
    assert (eth.mode, eth.prices, eth.stop_loss) == (MODE_PRICES_ONLY, ("2710",), "2624")


def test_14306_first_tier_with_setting_word_is_a_single_tier():
    instruction = _classify(14306)

    assert instruction.mode == MODE_SINGLE_TIER
    assert instruction.prices == ("79100",)
    assert instruction.allocations == ("50",)
    assert instruction.tier_index == 1


def test_18294_percentage_without_price_is_an_allocation_without_price():
    """The support zone "84000-84400附近有支撑" is not a take-profit price."""

    instruction = _classify(18294)

    assert instruction.mode == MODE_RATIOS_ONLY
    assert instruction.prices == ()
    assert instruction.allocations == ("50",)


def test_19670_price_near_take_profit_percent_is_a_single_tier():
    instruction = _classify(19670)

    assert instruction.mode == MODE_SINGLE_TIER
    assert instruction.prices == ("82500",)
    assert instruction.allocations == ("30",)


def test_17526_remaining_target_is_a_single_price_reset():
    instruction = _classify(17526)

    assert instruction.mode == MODE_PRICES_ONLY
    assert instruction.prices == ("79400",)


def test_17526_with_several_explicit_targets_is_not_classified():
    assert _classify(17526, _explicit_multi_target=True) is None


@pytest.mark.parametrize("raw_id", [18532, 13957, 14085, 17900, 17901, 18154, 19597, 16873])
def test_conditional_or_immediate_messages_are_not_take_profit_adjustments(raw_id):
    event = _event(raw_id) if MESSAGES[raw_id]["lifecycle_event"] else {
        "event_type": "position_update",
        "management_action": "partial_take_profit",
    }
    assert classify_take_profit_instruction(MESSAGES[raw_id]["text"], event) is None


@pytest.mark.parametrize(
    "text",
    [
        "现价83800，止盈50%，剩下的挂好止盈",  # immediate word in the percent's clause
        "BTC多单目前获利600点，止盈40%，剩余仓位上移至80600",
        "止盈30%，剩下两个止盈位各50%",  # one percent is a close: whole message stays on the reduction path
        "现价挂单止盈位各50%",  # immediate word inside the allocation clause itself
    ],
)
def test_an_immediate_percentage_keeps_the_reduction_path(text):
    assert classify_take_profit_instruction(text, {"event_type": "position_update"}) is None


def test_immediate_word_only_counts_in_its_own_clause():
    """18199 opens with "浮盈中": another clause, so the allocation stands."""

    instruction = classify_take_profit_instruction(
        "比特币目前获利1000点，提前设置两个止盈位各50%",
        {"event_type": "position_update"},
    )
    assert instruction is not None
    assert instruction.allocations == ("50", "50")


@pytest.mark.parametrize(
    ("text", "prices"),
    [
        ("止盈改到84500", ("84500",)),
        ("止盈上移至2750附近", ("2750",)),
        ("止盈位下移至81000", ("81000",)),
        ("止盈位：84000/82000", ("84000", "82000")),
        ("第一止盈位：79100", ("79100",)),
    ],
)
def test_price_forms(text, prices):
    instruction = classify_take_profit_instruction(text, {"event_type": "position_update"})
    assert instruction.mode == MODE_PRICES_ONLY
    assert instruction.prices == prices


def test_contact_numbers_are_never_prices():
    instruction = classify_take_profit_instruction(
        "止盈位：73070\n@Tarderfengge QQ:158241758", {"event_type": "position_update"}
    )
    assert instruction.prices == ("73070",)


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "exit_position"},
        {"event_type": "position_update", "management_action": "exit_partial"},
        {"event_type": "position_update", "management_action": "full_exit"},
        {"event_type": "position_update", "_explicit_multi_target": True},
    ],
)
def test_exit_cancel_and_multi_target_events_are_vetoed(event):
    assert classify_take_profit_instruction("止盈位：73070", event) is None


def test_observed_text_part_is_read_when_raw_text_has_nothing():
    text = "看图\n \n止盈位：2710"
    instruction = classify_take_profit_instruction(text, {"event_type": "position_update"})
    assert instruction.prices == ("2710",)


def test_instruction_round_trips_through_its_dict():
    instruction = _classify(14306)
    assert TakeProfitInstruction.from_dict(instruction.to_dict()) == instruction


# ---------------------------------------------------------------- directive


def test_directive_hook_turns_the_allocation_messages_into_adjust_take_profit():
    for raw_id in (18199, 14306, 18294, 19670, 17526):
        directive = resolve_management_directive(
            text=MESSAGES[raw_id]["text"], lifecycle_event=_event(raw_id)
        )
        assert directive.intent == "adjust_take_profit", raw_id
        assert directive.fraction is None
        assert directive.fanout_allowed is False
        assert directive.cancel_deferred_entries is False
        assert directive.risk_reducing is True


def test_18199_is_never_a_partial_take_profit():
    directive = resolve_management_directive(
        text=MESSAGES[18199]["text"], lifecycle_event=_event(18199)
    )
    assert directive.intent != "partial_take_profit"
    assert directive.fraction is None


def test_ouyang_directive_carries_the_labelled_stop():
    directive = resolve_management_directive(
        text=MESSAGES[13848]["text"], lifecycle_event=_event(13848)
    )
    assert directive.intent == "adjust_take_profit"
    assert directive.stop_loss == "78700"
    assert directive.stop_price_source == "current_message_text"


@pytest.mark.parametrize(
    ("raw_id", "event", "expected"),
    [
        (
            13957,
            {"event_type": "position_update", "management_action": "partial_take_profit", "stop_loss": "78000"},
            ("partial_then_break_even", 0.3),
        ),
        (
            14085,
            {"event_type": "position_update", "management_action": "partial_take_profit"},
            ("partial_then_break_even", 0.3),
        ),
        (17900, None, ("partial_take_profit", 0.4)),
        (17901, None, ("partial_take_profit", 0.4)),
        (18154, None, ("partial_take_profit", 0.6)),
        (19597, None, ("partial_then_break_even", 0.5)),
        (16873, None, ("partial_then_break_even", 0.5)),
        (18532, None, ("none", None)),
    ],
)
def test_regressions_keep_their_intent_and_fraction(monkeypatch, raw_id, event, expected):
    event = event or _event(raw_id)
    text = MESSAGES[raw_id]["text"]
    actual = resolve_management_directive(text=text, lifecycle_event=event)
    monkeypatch.setattr(
        management_directives, "_take_profit_adjustment_directive", lambda **_: None
    )
    without_hook = resolve_management_directive(text=text, lifecycle_event=event)

    assert (actual.intent, actual.fraction) == expected
    assert actual == without_hook


# ---------------------------------------------------------------- planning


def _instruction(mode, prices=(), allocations=(), tier_index=None):
    return TakeProfitInstruction(
        mode=mode,
        prices=tuple(prices),
        allocations=tuple(allocations),
        tier_index=tier_index,
        stop_loss=None,
    )


def _plan(instruction, **kwargs):
    defaults = {
        "side": "short",
        "remaining_size": "15",
        "last_price": "85000",
        "existing_take_profits": [],
        "filled_prices": (),
        "strategy_take_profit_prices": (),
        "min_quantity": "1",
        "quantity_step": "1",
    }
    defaults.update(kwargs)
    return plan_take_profit_adjustment(instruction=instruction, **defaults)


def test_18199_replay_already_matches_seven_and_eight():
    plan = _plan(
        _classify_as_instruction(18199),
        existing_take_profits=[("84000", "7", "tp-84000"), ("82000", "8", "tp-82000")],
        strategy_take_profit_prices=["84000", "82000"],
    )

    assert plan.status == PLAN_ALREADY_SATISFIED
    assert plan.targets == (("84000", "7"), ("82000", "8"))
    assert plan.cancel_order_ids == () and plan.place == ()


def test_18199_variant_ten_and_five_is_rebuilt_to_seven_and_eight():
    plan = _plan(
        _classify_as_instruction(18199),
        existing_take_profits=[("84000", "10", "tp-a"), ("82000", "5", "tp-b")],
        strategy_take_profit_prices=["84000", "82000"],
    )

    assert plan.status == PLAN_READY
    assert plan.targets == (("84000", "7"), ("82000", "8"))
    assert set(plan.cancel_order_ids) == {"tp-a", "tp-b"}
    assert plan.place == (("84000", "7"), ("82000", "8"))


def _classify_as_instruction(raw_id):
    instruction = _classify(raw_id)
    assert instruction is not None
    return instruction


def test_ratios_without_any_price_are_refused_as_price_missing():
    plan = _plan(_classify_as_instruction(18294), strategy_take_profit_prices=["84000"])

    assert plan.status == PLAN_REFUSED
    assert plan.reason_code == REASON_PRICE_MISSING


def test_full_ratio_set_without_strategy_price_is_price_missing():
    plan = _plan(_classify_as_instruction(18199), strategy_take_profit_prices=[])

    assert (plan.status, plan.reason_code) == (PLAN_REFUSED, REASON_PRICE_MISSING)


def test_ratio_count_that_matches_no_strategy_tiers_is_ambiguous():
    plan = _plan(
        _classify_as_instruction(18199),
        strategy_take_profit_prices=["84000", "83000", "82000"],
    )

    assert (plan.status, plan.reason_code) == (PLAN_REFUSED, REASON_TIER_COUNT_AMBIGUOUS)


def test_ouyang_single_price_replaces_both_tiers_with_one():
    plan = _plan(
        _classify_as_instruction(13848),
        remaining_size="12",
        last_price="76000",
        existing_take_profits=[("74000", "6", "a"), ("72000", "6", "b")],
    )

    assert plan.status == PLAN_READY
    assert plan.targets == (("73070", "12"),)
    assert plan.allocations == ("100",)
    assert set(plan.cancel_order_ids) == {"a", "b"}


def test_14306_replaces_only_the_first_tier_with_half_of_the_remaining():
    plan = _plan(
        _classify_as_instruction(14306),
        side="long",
        remaining_size="10",
        last_price="78600",
        existing_take_profits=[("79000", "5", "tp1"), ("80000", "5", "tp2")],
    )

    assert plan.status == PLAN_READY
    assert plan.targets == (("79100", "5"), ("80000", "5"))
    assert plan.cancel_order_ids == ("tp1",)
    assert plan.keep_order_ids == ("tp2",)
    assert plan.place == (("79100", "5"),)


def test_single_tier_trims_the_farthest_other_tier_when_the_total_overflows():
    plan = _plan(
        _classify_as_instruction(19670),
        remaining_size="15",
        last_price="84000",
        existing_take_profits=[("83000", "7", "near"), ("81000", "8", "far")],
    )

    # 82500 x 30% of 15 = 4; others keep 7 + 8 = 15, so 4 comes off the far tier.
    assert plan.status == PLAN_READY
    assert plan.targets == (("83000", "7"), ("82500", "4"), ("81000", "4"))
    assert plan.keep_order_ids == ("near",)
    assert plan.cancel_order_ids == ("far",)
    assert set(plan.place) == {("82500", "4"), ("81000", "4")}


def test_19670_never_closes_anything_at_market():
    plan = _plan(
        _classify_as_instruction(19670),
        remaining_size="10",
        last_price="84000",
        existing_take_profits=[],
    )

    assert plan.status == PLAN_READY
    assert plan.targets == (("82500", "3"),)
    assert plan.cancel_order_ids == ()


def test_17526_remaining_position_becomes_one_tier_and_the_filled_tier_stays_out():
    plan = _plan(
        _classify_as_instruction(17526),
        side="long",
        remaining_size="6",
        last_price="78500",
        existing_take_profits=[("80500", "6", "tp2")],
        filled_prices=["78000"],
    )

    assert plan.status == PLAN_READY
    assert plan.targets == (("79400", "6"),)
    assert plan.cancel_order_ids == ("tp2",)


def test_a_listed_price_that_already_filled_is_dropped():
    plan = _plan(
        _instruction(MODE_PRICES_ONLY, ["78000", "79400"]),
        side="long",
        remaining_size="6",
        last_price="77500",
        existing_take_profits=[("80500", "6", "tp2")],
        filled_prices=["78000"],
    )

    assert plan.targets == (("79400", "6"),)
    assert plan.dropped_filled == ("78000",)


def test_two_tiers_to_three_on_three_contracts_shrinks_from_the_farthest():
    plan = _plan(
        _instruction(MODE_PRICES_ONLY, ["73000", "72000", "71000"]),
        remaining_size="3",
        last_price="75000",
        existing_take_profits=[("73500", "1", "a"), ("72500", "2", "b")],
    )

    # Default table 40/30/30 -> 1.2/0.9/0.9 floors under the one-lot minimum;
    # the farthest tier goes and 40/30 is renormalised: 1 + remainder 2.
    assert plan.status == PLAN_READY
    assert plan.targets == (("73000", "1"), ("72000", "2"))


def test_same_tier_count_keeps_our_existing_split():
    plan = _plan(
        _instruction(MODE_PRICES_ONLY, ["83500", "81500"]),
        existing_take_profits=[("84000", "4", "a"), ("82000", "11", "b")],
    )

    assert plan.targets == (("83500", "4"), ("81500", "11"))


def test_a_crossed_tier_is_dropped_and_its_share_moves_to_the_rest():
    plan = _plan(
        _instruction(MODE_FULL_RESET, ["85500", "84000"], ["50", "50"]),
        remaining_size="10",
        last_price="85000",
    )

    assert plan.status == PLAN_READY
    assert plan.dropped_crossed == ("85500",)
    assert plan.targets == (("84000", "10"),)


def test_every_tier_crossed_is_refused():
    plan = _plan(
        _instruction(MODE_PRICES_ONLY, ["85500"]),
        last_price="85000",
    )

    assert (plan.status, plan.reason_code) == (PLAN_REFUSED, REASON_ALL_TIERS_CROSSED)
    assert plan.dropped_crossed == ("85500",)


def test_below_minimum_is_refused_and_nothing_is_touched():
    plan = _plan(
        _instruction(MODE_PRICES_ONLY, ["84000"]),
        remaining_size="1",
        min_quantity="2",
        existing_take_profits=[],
    )

    assert (plan.status, plan.reason_code) == (PLAN_REFUSED, REASON_SIZE_BELOW_MINIMUM)
    assert plan.cancel_order_ids == () and plan.place == ()


def test_each_position_is_sized_from_its_own_remaining_size():
    instruction = _instruction(MODE_FULL_RESET, ["84000", "82000"], ["50", "50"])
    first = _plan(instruction, remaining_size="15")
    second = _plan(instruction, remaining_size="4")

    assert first.targets == (("84000", "7"), ("82000", "8"))
    assert second.targets == (("84000", "2"), ("82000", "2"))


@pytest.mark.parametrize(
    "sizes", [["7", "8"], ["1", "2"], ["4", "11"], ["3", "3", "4"], ["12"], ["0.3", "0.7"]]
)
def test_proportional_allocations_reproduce_the_sizes_in_the_convergence_sizer(sizes):
    shares = proportional_allocations(sizes)
    total = sum(Decimal(value) for value in sizes)
    step = Decimal("0.1") if any("." in value for value in sizes) else Decimal("1")

    assert sum(Decimal(value) for value in shares) == Decimal("100")
    reproduced = _allocate_sizes(
        total,
        [Decimal(value) for value in shares],
        quantity_step=step,
        minimum_quantity=step,
    )
    assert [Decimal(str(value)) for value in reproduced] == [Decimal(value) for value in sizes]
