"""The break-even target is the strategy's price, and it never fails closed."""

import json

import pytest

from telegram_kol_research.break_even_reference import (
    ACTUAL_FILL_NO_STRATEGY_PRICE,
    BREAK_EVEN_REFERENCE_PRICE_KEY,
    BREAK_EVEN_REFERENCE_SOURCE_KEY,
    MESSAGE_EXPLICIT_TIGHTER,
    STRATEGY_FIRST_LEG,
    STRATEGY_MIDPOINT,
    STRATEGY_SECOND_LEG,
    STRATEGY_SINGLE_PRICE,
    BreakEvenReference,
    adopted_break_even_reference,
    break_even_target_price,
    planned_tpsl_reference_fields,
    resolve_break_even_reference,
)


def _resolve(**overrides):
    values = {
        "side": "short",
        "entry_range_low": 80500,
        "entry_range_high": 81600,
        "open_entry_leg_indexes": [1],
        "actual_avg_entry_price": "80436",
    }
    values.update(overrides)
    return resolve_break_even_reference(**values)


@pytest.mark.parametrize(
    "side,indexes,expected_price,expected_source",
    [
        # Short: leg 1 is planned at the low end, leg 2 at the high end.
        ("short", [1], "80500", STRATEGY_FIRST_LEG),
        ("short", [2], "81600", STRATEGY_SECOND_LEG),
        ("short", [1, 2], "81050", STRATEGY_MIDPOINT),
        ("short", [2, 1], "81050", STRATEGY_MIDPOINT),
        # Long is the mirror image.
        ("long", [1], "81600", STRATEGY_FIRST_LEG),
        ("long", [2], "80500", STRATEGY_SECOND_LEG),
        ("long", [1, 2], "81050", STRATEGY_MIDPOINT),
    ],
)
def test_each_open_leg_set_names_one_strategy_price(
    side, indexes, expected_price, expected_source
):
    reference = _resolve(side=side, open_entry_leg_indexes=indexes)

    assert (reference.price, reference.source) == (
        expected_price,
        expected_source,
    )
    assert reference.is_strategy_price


def test_the_midpoint_is_the_plain_average_of_the_two_ends():
    assert _resolve(
        entry_range_low="100", entry_range_high="101",
        open_entry_leg_indexes=[1, 2],
    ).price == "100.5"


@pytest.mark.parametrize("side", ["long", "short"])
def test_a_single_entry_price_is_that_price_whatever_is_open(side):
    for indexes in ([1], [2], [1, 2], [], [7]):
        reference = _resolve(
            side=side,
            entry_range_low="2480",
            entry_range_high="2480",
            open_entry_leg_indexes=indexes,
        )
        assert (reference.price, reference.source) == (
            "2480",
            STRATEGY_SINGLE_PRICE,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        # No entry price at all: a pure market strategy.
        {"entry_range_low": None, "entry_range_high": None},
        {"entry_range_low": 80500, "entry_range_high": None},
        {"entry_range_low": None, "entry_range_high": 81600},
        # An inverted or impossible range is not a range.
        {"entry_range_low": 81600, "entry_range_high": 80500},
        {"entry_range_low": 0, "entry_range_high": 81600},
        {"entry_range_low": -1, "entry_range_high": 81600},
        {"entry_range_low": "not-a-price", "entry_range_high": 81600},
        {"entry_range_low": float("nan"), "entry_range_high": 81600},
        {"entry_range_low": float("inf"), "entry_range_high": 81600},
        # A side we cannot read cannot pick an end of the range.
        {"side": None},
        {"side": ""},
        {"side": "buy"},
        # Leg indexes outside the two entry legs say nothing about the range.
        {"open_entry_leg_indexes": []},
        {"open_entry_leg_indexes": [0]},
        {"open_entry_leg_indexes": [3]},
        {"open_entry_leg_indexes": [1, 3]},
        {"open_entry_leg_indexes": ["x"]},
        {"open_entry_leg_indexes": None},
    ],
)
def test_anything_unusable_falls_back_to_our_own_fill(overrides):
    reference = _resolve(**overrides)

    assert reference.source == ACTUAL_FILL_NO_STRATEGY_PRICE
    assert reference.price == "80436"
    assert not reference.is_strategy_price


def test_a_leg_set_covering_both_entry_legs_still_uses_the_midpoint():
    assert _resolve(open_entry_leg_indexes=[1, 2, 3]).source == STRATEGY_MIDPOINT


@pytest.mark.parametrize(
    "value", [None, "", "not-a-price", 0, -5, float("nan")]
)
def test_an_unusable_actual_fill_is_reported_as_absent_not_raised(value):
    reference = _resolve(side=None, actual_avg_entry_price=value)

    assert reference.price is None
    assert reference.source == ACTUAL_FILL_NO_STRATEGY_PRICE


def test_the_evidence_echoes_the_inputs_and_is_serializable():
    evidence = _resolve().evidence

    assert evidence == {
        "side": "short",
        "entry_range_low": "80500",
        "entry_range_high": "81600",
        "open_entry_leg_indexes": [1],
    }
    assert json.loads(json.dumps(evidence, ensure_ascii=False)) == evidence


def test_an_adopted_message_price_records_what_it_superseded():
    adopted = adopted_break_even_reference(_resolve(), price="80000")

    assert (adopted.price, adopted.source) == ("80000", MESSAGE_EXPLICIT_TIGHTER)
    assert adopted.evidence["superseded_source"] == STRATEGY_FIRST_LEG
    assert adopted.evidence["superseded_price"] == "80500"
    assert adopted.is_strategy_price


def test_only_a_strategy_price_is_written_into_the_planned_tpsl():
    assert planned_tpsl_reference_fields(None) == {}
    assert planned_tpsl_reference_fields(_resolve(side=None)) == {}
    assert planned_tpsl_reference_fields(_resolve()) == {
        BREAK_EVEN_REFERENCE_PRICE_KEY: "80500",
        BREAK_EVEN_REFERENCE_SOURCE_KEY: STRATEGY_FIRST_LEG,
    }
    assert planned_tpsl_reference_fields(
        BreakEvenReference(price=None, source=STRATEGY_FIRST_LEG, evidence={})
    ) == {}


class _Leg:
    def __init__(self, *, avg_entry_price="80436", **kwargs):
        self.avg_entry_price = avg_entry_price
        for name, value in kwargs.items():
            setattr(self, name, value)


@pytest.mark.parametrize(
    "leg",
    [
        # A batch planned before this rule existed.
        _Leg(planned_tpsl=None),
        _Leg(planned_tpsl_json=None),
        _Leg(planned_tpsl_json=""),
        _Leg(planned_tpsl={"intent": "move_stop_to_break_even"}),
        # Junk in the column is not a price, and must not become one.
        _Leg(planned_tpsl_json="{not json"),
        _Leg(planned_tpsl_json="[]"),
        _Leg(planned_tpsl={BREAK_EVEN_REFERENCE_PRICE_KEY: "0"}),
        _Leg(planned_tpsl={BREAK_EVEN_REFERENCE_PRICE_KEY: "-1"}),
        _Leg(planned_tpsl={BREAK_EVEN_REFERENCE_PRICE_KEY: "nope"}),
        _Leg(planned_tpsl={BREAK_EVEN_REFERENCE_PRICE_KEY: None}),
    ],
)
def test_without_a_usable_reference_the_target_is_the_actual_fill(leg):
    assert break_even_target_price(leg) == "80436"


def test_the_reference_wins_from_either_shape_of_leg():
    fields = {BREAK_EVEN_REFERENCE_PRICE_KEY: "80500"}

    assert break_even_target_price(_Leg(planned_tpsl=fields)) == "80500"
    assert (
        break_even_target_price(_Leg(planned_tpsl_json=json.dumps(fields)))
        == "80500"
    )
