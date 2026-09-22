"""The ladder as pure arithmetic: rungs, reach, target, and what it would do.

Every rule here is the account owner's, recorded in
``docs/plans/2026-09-21-stop-ladder-policy.md`` section 4 and made binding by
``docs/plans/2026-09-23-stop-ladder-phase1-spec.md`` section 2:

* a rung is **this position's own take-profit ledger row**, never a price read
  out of the strategy text -- when a KOL moves a take profit the system
  cancels and re-places, so the sequence follows on its own;
* a rung is reached when its order is gone from a **complete** pending read,
  it is not one we cancelled, and either the trigger history says it fired
  cleanly or the position got smaller between two complete observations.
  **Quantity is never compared**;
* level N aims the stop at rung N-1, with level 1 aiming at the strategy's
  entry reference, and a target the market has already passed means close at
  market rather than fall back to something looser.
"""

from __future__ import annotations

import pytest

from deepcoin_production_rows import trigger_history_row

from telegram_kol_research.break_even_reference import (
    STRATEGY_FIRST_LEG,
    resolve_break_even_reference,
)
from telegram_kol_research.stop_ladder import (
    ACTION_CLOSE_AT_MARKET,
    ACTION_NO_CHANGE,
    ACTION_REPLACE_STOP,
    EVIDENCE_FORM_POSITION_DECREASE,
    EVIDENCE_FORM_TRIGGER_HISTORY,
    REASON_CANCEL_INTENT,
    REASON_ORDER_STILL_PENDING,
    REASON_SNAPSHOT_INCOMPLETE,
    filled_level_for_position,
    ladder_decision,
    ladder_target,
    rung_reached,
    rungs_for_position,
    strategy_filled_level,
)


SHORT_TPS = (("tp1", "79800"), ("tp2", "79100"), ("tp3", "78400"))
LONG_TPS = (("tp1", "82300"), ("tp2", "83000"), ("tp3", "83700"))


def _ledger(order_id, price, *, status="verified", purpose="take_profit", row_id=None):
    return {
        "id": row_id,
        "order_id": order_id,
        "trigger_price": price,
        "size_text": "3",
        "purpose": purpose,
        "status": status,
    }


def _rungs(pairs, *, side="short", **kwargs):
    return rungs_for_position(
        [_ledger(order_id, price, **kwargs) for order_id, price in pairs],
        side=side,
    )


# ---------------------------------------------------------------- sequence


def test_a_short_ladder_runs_down_from_the_entry():
    rungs = _rungs(SHORT_TPS)

    assert [(rung.level, rung.trigger_price) for rung in rungs] == [
        (1, "79800"),
        (2, "79100"),
        (3, "78400"),
    ]


def test_a_long_ladder_runs_up_from_the_entry():
    rungs = _rungs(LONG_TPS, side="long")

    assert [(rung.level, rung.trigger_price) for rung in rungs] == [
        (1, "82300"),
        (2, "83000"),
        (3, "83700"),
    ]


def test_the_sequence_is_the_profit_direction_not_the_insertion_order():
    rungs = rungs_for_position(
        [
            _ledger("tp3", "78400"),
            _ledger("tp1", "79800"),
            _ledger("tp2", "79100"),
        ],
        side="short",
    )

    assert [rung.order_id for rung in rungs] == ["tp1", "tp2", "tp3"]


@pytest.mark.parametrize("status", ["retired", "cancelled", "canceled", "superseded"])
def test_a_history_row_leaves_the_sequence_and_the_rest_renumber(status):
    rungs = rungs_for_position(
        [
            _ledger("tp1", "79800", status=status),
            _ledger("tp2", "79100"),
            _ledger("tp3", "78400"),
        ],
        side="short",
    )

    assert [(rung.level, rung.order_id) for rung in rungs] == [(1, "tp2"), (2, "tp3")]


def test_a_filled_row_stays_a_rung():
    """It filled; it is still the stage the ladder counts from."""

    rungs = rungs_for_position(
        [_ledger("tp1", "79800", status="filled"), _ledger("tp2", "79100")],
        side="short",
    )

    assert [(rung.level, rung.status) for rung in rungs] == [
        (1, "filled"),
        (2, "verified"),
    ]


def test_stops_and_other_purposes_are_not_rungs():
    rungs = rungs_for_position(
        [
            _ledger("sl", "82300", purpose="stop_loss"),
            _ledger("bk", "82400", purpose="backup_stop"),
            _ledger("tp1", "79800"),
        ],
        side="short",
    )

    assert [rung.order_id for rung in rungs] == ["tp1"]


def test_a_ladder_can_be_five_rungs_deep():
    rungs = _rungs(
        (("a", "79800"), ("b", "79100"), ("c", "78400"), ("d", "77700"), ("e", "77000"))
    )

    assert [rung.level for rung in rungs] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"order_id": "", "trigger_price": "79800", "purpose": "take_profit"}],
        [{"order_id": "tp1", "trigger_price": "0", "purpose": "take_profit"}],
        [{"order_id": "tp1", "trigger_price": "abc", "purpose": "take_profit"}],
        [{"order_id": "tp1", "purpose": "take_profit"}],
    ],
)
def test_unusable_rows_never_raise_and_never_become_rungs(rows):
    assert rungs_for_position(rows, side="short") == ()


def test_an_unknown_side_has_no_sequence_at_all():
    assert _rungs(SHORT_TPS, side="sideways") == ()
    assert rungs_for_position(None, side="short") == ()


# -------------------------------------------------------------- reachedness


def _history(order_id, **overrides):
    return trigger_history_row(
        ord_id=order_id,
        inst_id="BTC-USDT-SWAP",
        pos_side="short",
        trigger_price="79800",
        size="3",
        **overrides,
    )


def test_form_a_a_clean_trigger_history_row_reaches_the_rung():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=("tp2", "tp3"),
        pending_snapshot_complete=True,
        trigger_history=[_history("tp1")],
    )

    assert verdict.reached
    assert verdict.evidence_form == EVIDENCE_FORM_TRIGGER_HISTORY


def test_form_b_any_decrease_at_all_reaches_the_rung():
    """"Any amount" is the ruling: a partial fill must not stall the ladder."""

    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=("tp2", "tp3"),
        pending_snapshot_complete=True,
        trigger_history=[],
        position_decrease_proven=True,
    )

    assert verdict.reached
    assert verdict.evidence_form == EVIDENCE_FORM_POSITION_DECREASE


def test_an_order_we_asked_to_cancel_is_never_a_fill():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=(),
        pending_snapshot_complete=True,
        trigger_history=[_history("tp1")],
        cancel_intent_order_ids=("tp1",),
        position_decrease_proven=True,
    )

    assert not verdict.reached
    assert verdict.reason_code == REASON_CANCEL_INTENT


def test_an_order_still_resting_on_the_exchange_is_not_a_fill():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=("tp1", "tp2"),
        pending_snapshot_complete=True,
        trigger_history=[_history("tp1")],
    )

    assert not verdict.reached
    assert verdict.reason_code == REASON_ORDER_STILL_PENDING


def test_an_incomplete_snapshot_decides_nothing():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=(),
        pending_snapshot_complete=False,
        trigger_history=[_history("tp1")],
        position_decrease_proven=True,
    )

    assert not verdict.reached
    assert verdict.reason_code == REASON_SNAPSHOT_INCOMPLETE


def test_a_failed_trigger_is_not_a_fill_even_with_a_smaller_position():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=(),
        pending_snapshot_complete=True,
        trigger_history=[_history("tp1", error_code="51004")],
        position_decrease_proven=True,
    )

    assert not verdict.reached
    assert verdict.reason_code == "take_profit_trigger_failed"


def test_a_position_that_did_not_shrink_proves_nothing():
    rung = _rungs(SHORT_TPS)[0]

    verdict = rung_reached(
        rung=rung,
        pending_order_ids=(),
        pending_snapshot_complete=True,
        trigger_history=[],
        position_decrease_proven=False,
    )

    assert not verdict.reached


def test_the_level_is_the_highest_rung_reached():
    rungs = _rungs(SHORT_TPS)
    verdicts = [
        rung_reached(
            rung=rung,
            pending_order_ids=("tp3",),
            pending_snapshot_complete=True,
            trigger_history=[_history(rung.order_id)],
        )
        for rung in rungs
    ]

    assert filled_level_for_position(verdicts) == 2
    assert filled_level_for_position([]) == 0


def test_the_strategy_level_is_the_highest_of_its_legs():
    assert strategy_filled_level({"pos-1": 1, "pos-2": 2}) == 2
    assert strategy_filled_level({}) == 0
    assert strategy_filled_level({"pos-1": 0}) == 0


# ------------------------------------------------------------------ target


def _target(filled_level, *, side="short", rungs=None, levels=None, reference="80500"):
    rung_map = {"pos-1": rungs if rungs is not None else _rungs(SHORT_TPS, side=side)}
    level_map = levels if levels is not None else {"pos-1": filled_level}
    return ladder_target(
        side=side,
        filled_level=filled_level,
        rungs_by_position=rung_map,
        levels_by_position=level_map,
        break_even_reference_price=reference,
        break_even_reference_source=STRATEGY_FIRST_LEG,
    )


def test_level_zero_leaves_the_strategy_stop_alone():
    target = _target(0)

    assert target.price is None
    assert target.level == 0


def test_level_one_is_the_strategy_entry_reference():
    target = _target(1)

    assert target.price == "80500"
    assert target.source == STRATEGY_FIRST_LEG


def test_level_two_is_the_first_rung_price():
    target = _target(2)

    assert target.price == "79800"
    assert target.source == "strategy_take_profit_level_1"


def test_level_three_is_the_second_rung_price():
    target = _target(3)

    assert target.price == "79100"
    assert target.source == "strategy_take_profit_level_2"


def test_two_legs_at_the_same_level_take_the_more_protective_price():
    short = ladder_target(
        side="short",
        filled_level=2,
        rungs_by_position={
            "pos-1": _rungs((("a", "79800"), ("b", "79100"))),
            "pos-2": _rungs((("c", "79700"), ("d", "79000"))),
        },
        levels_by_position={"pos-1": 2, "pos-2": 2},
        break_even_reference_price="80500",
    )
    long = ladder_target(
        side="long",
        filled_level=2,
        rungs_by_position={
            "pos-1": _rungs((("a", "82300"), ("b", "83000")), side="long"),
            "pos-2": _rungs((("c", "82400"), ("d", "83100")), side="long"),
        },
        levels_by_position={"pos-1": 2, "pos-2": 2},
        break_even_reference_price="81600",
    )

    assert short.price == "79700"
    assert long.price == "82400"


def test_only_the_leg_that_reached_the_level_supplies_the_price():
    target = ladder_target(
        side="short",
        filled_level=2,
        rungs_by_position={
            "pos-1": _rungs((("a", "79800"), ("b", "79100"))),
            "pos-2": _rungs((("c", "70000"), ("d", "60000"))),
        },
        levels_by_position={"pos-1": 2, "pos-2": 0},
        break_even_reference_price="80500",
    )

    assert target.price == "79800"


def test_a_level_with_no_rung_beneath_it_has_no_target():
    target = ladder_target(
        side="short",
        filled_level=2,
        rungs_by_position={"pos-1": ()},
        levels_by_position={"pos-1": 2},
        break_even_reference_price="80500",
    )

    assert target.price is None
    assert target.reason_code is not None


def test_a_missing_entry_reference_leaves_level_one_without_a_target():
    target = _target(1, reference=None)

    assert target.price is None
    assert target.reason_code is not None


# ---------------------------------------------------------------- decision


def test_a_placeable_target_would_replace_the_stop():
    decision = ladder_decision(
        side="short", target=_target(1), market_price="79790", existing_stop_prices=["82300"]
    )

    assert decision.would_action == ACTION_REPLACE_STOP
    assert decision.target_price == "80500"


def test_a_target_the_market_has_passed_would_close_at_market():
    decision = ladder_decision(
        side="short", target=_target(2), market_price="79850", existing_stop_prices=["82300"]
    )

    assert decision.would_action == ACTION_CLOSE_AT_MARKET
    assert decision.target_price == "79800"


def test_an_existing_stop_that_already_protects_more_changes_nothing():
    decision = ladder_decision(
        side="short", target=_target(2), market_price="79200", existing_stop_prices=["79500"]
    )

    assert decision.would_action == ACTION_NO_CHANGE
    assert decision.effective_stop_price == "79500"


def test_level_zero_changes_nothing():
    decision = ladder_decision(
        side="short", target=_target(0), market_price="80000", existing_stop_prices=["82300"]
    )

    assert decision.would_action == ACTION_NO_CHANGE


def test_an_unreadable_market_price_changes_nothing_and_never_raises():
    decision = ladder_decision(
        side="short", target=_target(1), market_price=None, existing_stop_prices=[]
    )

    assert decision.would_action == ACTION_NO_CHANGE
    assert decision.reason_code is not None


def test_an_unreadable_existing_stop_is_never_treated_as_protective():
    decision = ladder_decision(
        side="short",
        target=_target(1),
        market_price="79790",
        existing_stop_prices=["", None, "not-a-price"],
    )

    assert decision.would_action == ACTION_REPLACE_STOP


# ------------------------------------------------------- the task's shape


SHORT_RANGE = {"low": "80500", "high": "81600"}


def _short_reference(open_legs=(1,)):
    return resolve_break_even_reference(
        side="short",
        entry_range_low=SHORT_RANGE["low"],
        entry_range_high=SHORT_RANGE["high"],
        open_entry_leg_indexes=open_legs,
        actual_avg_entry_price="80436",
    )


def _short_case(filled_level, market_price, existing=("82300",)):
    reference = _short_reference()
    rungs = _rungs(SHORT_TPS)
    target = ladder_target(
        side="short",
        filled_level=filled_level,
        rungs_by_position={"pos-1": rungs},
        levels_by_position={"pos-1": filled_level},
        break_even_reference_price=reference.price,
        break_even_reference_source=reference.source,
    )
    return ladder_decision(
        side="short",
        target=target,
        market_price=market_price,
        existing_stop_prices=list(existing),
    )


def test_production_shape_tp1_filled_moves_the_stop_to_the_range_low():
    decision = _short_case(1, "79790")

    assert (decision.level, decision.target_price, decision.would_action) == (
        1,
        "80500",
        ACTION_REPLACE_STOP,
    )
    assert decision.target_source == STRATEGY_FIRST_LEG


def test_production_shape_tp2_filled_moves_the_stop_to_the_first_take_profit():
    decision = _short_case(2, "79090")

    assert (decision.level, decision.target_price, decision.would_action) == (
        2,
        "79800",
        ACTION_REPLACE_STOP,
    )


def test_production_shape_tp2_filled_with_the_market_back_above_closes():
    decision = _short_case(2, "79850")

    assert decision.would_action == ACTION_CLOSE_AT_MARKET


def test_production_shape_a_tighter_hand_set_stop_is_kept():
    decision = _short_case(2, "79200", existing=("79500",))

    assert decision.would_action == ACTION_NO_CHANGE
    assert decision.effective_stop_price == "79500"


def test_production_shape_long_mirror():
    reference = resolve_break_even_reference(
        side="long",
        entry_range_low="80500",
        entry_range_high="81600",
        open_entry_leg_indexes=(1,),
        actual_avg_entry_price="81664",
    )
    rungs = _rungs(LONG_TPS, side="long")

    def decide(level, market, existing=("79700",)):  # noqa: D401 - local helper
        target = ladder_target(
            side="long",
            filled_level=level,
            rungs_by_position={"pos-1": rungs},
            levels_by_position={"pos-1": level},
            break_even_reference_price=reference.price,
            break_even_reference_source=reference.source,
        )
        return ladder_decision(
            side="long",
            target=target,
            market_price=market,
            existing_stop_prices=list(existing),
        )

    assert reference.price == "81600"
    first = decide(1, "82310")
    assert (first.target_price, first.would_action) == ("81600", ACTION_REPLACE_STOP)
    second = decide(2, "83010")
    assert (second.target_price, second.would_action) == ("82300", ACTION_REPLACE_STOP)
    passed = decide(2, "82290")
    assert passed.would_action == ACTION_CLOSE_AT_MARKET
    kept = decide(2, "83010", existing=("82500",))
    assert kept.would_action == ACTION_NO_CHANGE
    assert kept.effective_stop_price == "82500"
