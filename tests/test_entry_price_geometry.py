import json
from decimal import Decimal
from pathlib import Path

import pytest

from telegram_kol_research.entry_price_geometry import (
    MARKET_REFERENCE_STALE,
    is_pure_market_entry_text,
    stale_market_reference_result,
    validate_candidate_entry_price_geometry,
    validate_entry_price_geometry,
    validate_order_draft_price_geometry,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "case",
    json.loads(
        (FIXTURES / "entry_price_geometry_real_failures.json").read_text(
            encoding="utf-8"
        )
    ),
    ids=lambda case: f"candidate-{case['candidate_id']}",
)
def test_real_failed_candidates_are_rejected_before_exchange(case):
    result = validate_entry_price_geometry(
        side=case["side"],
        entry_prices=case["entry_prices"],
        explicit_average_entry=case.get("explicit_average_entry"),
        stop_loss=case["stop_loss"],
        take_profit_prices=case["take_profit_prices"],
    )

    assert result.status == "invalid"
    assert result.reason_code == case["expected_reason"]
    assert result.offending_field == "stop_loss"


@pytest.mark.parametrize(
    ("side", "entry_prices", "stop_loss", "take_profit_prices"),
    [
        ("long", [68000], 67500, [69000]),
        ("long", [68000, 68200], 67500, [69000, 70000, 71000]),
        ("short", [68000], 68500, [67000]),
        ("short", [68000, 68200], 68500, [67000, 66000, 65000]),
    ],
)
def test_correct_long_and_short_geometry_is_not_rejected(
    side, entry_prices, stop_loss, take_profit_prices
):
    result = validate_entry_price_geometry(
        side=side,
        entry_prices=entry_prices,
        stop_loss=stop_loss,
        take_profit_prices=take_profit_prices,
    )

    assert result.status == "valid"
    assert result.reason_code is None


@pytest.mark.parametrize(
    ("side", "entry_prices", "stop_loss", "take_profit_prices", "field"),
    [
        ("long", [68000, 68200], 68100, [69000], "stop_loss"),
        ("short", [68000, 68200], 68100, [67000], "stop_loss"),
        ("long", [68000, 68200], 67500, [69000, 68100], "take_profit"),
        ("short", [68000, 68200], 68500, [67000, 68100], "take_profit"),
    ],
)
def test_wrong_stop_or_any_wrong_take_profit_is_rejected(
    side, entry_prices, stop_loss, take_profit_prices, field
):
    result = validate_entry_price_geometry(
        side=side,
        entry_prices=entry_prices,
        stop_loss=stop_loss,
        take_profit_prices=take_profit_prices,
    )

    assert result.status == "invalid"
    assert result.offending_field == field


@pytest.mark.parametrize(
    ("side", "stop_loss", "take_profit_prices", "field"),
    [
        ("long", 68000, [69000], "stop_loss"),
        ("long", 67500, [68200], "take_profit"),
        ("short", 68200, [67000], "stop_loss"),
        ("short", 68500, [68000], "take_profit"),
    ],
)
def test_entry_domain_boundary_equality_is_rejected(
    side, stop_loss, take_profit_prices, field
):
    result = validate_entry_price_geometry(
        side=side,
        entry_prices=[68000, 68200],
        stop_loss=stop_loss,
        take_profit_prices=take_profit_prices,
    )

    assert result.status == "invalid"
    assert result.reason_code == "entry_price_geometry_equal_boundary"
    assert result.offending_field == field


@pytest.mark.parametrize(
    ("entry_prices", "stop_loss", "take_profit_prices", "average"),
    [
        ([], 67500, [69000], None),
        ([68000], None, [69000], None),
        ([68000, 68200], 67500, ["10%"], None),
        ([68000, 68200], "67500 / 67400", [69000], None),
        ([68000, 68200], 67500, [69000], 69000),
    ],
)
def test_missing_or_ambiguous_geometry_is_indeterminate_and_never_passes(
    entry_prices, stop_loss, take_profit_prices, average
):
    result = validate_entry_price_geometry(
        side="long",
        entry_prices=entry_prices,
        explicit_average_entry=average,
        stop_loss=stop_loss,
        take_profit_prices=take_profit_prices,
    )

    assert result.status == "indeterminate"
    assert result.passed is False


def test_tick_normalization_can_turn_a_strict_value_into_equal_boundary():
    result = validate_entry_price_geometry(
        side="long",
        entry_prices=[Decimal("68000.19")],
        stop_loss=Decimal("68000.11"),
        take_profit_prices=[Decimal("69000")],
        price_tick=Decimal("0.1"),
    )

    assert result.status == "invalid"
    assert result.reason_code == "entry_price_geometry_equal_boundary"
    assert result.normalized_entry_prices == ("68000.1",)
    assert result.normalized_stop_loss == "68000.1"


def test_candidate_parser_accepts_labels_and_proven_average_without_guessing():
    result = validate_candidate_entry_price_geometry(
        side="short",
        entry_text="79500-76500区域，均价78000附近",
        stop_loss_text="止损点位 81000",
        take_profit_text="止盈点位 75000 / 74000",
        symbol="BTC",
    )

    assert result.status == "valid"
    assert result.normalized_entry_prices == ("79500", "76500")
    assert result.normalized_explicit_average_entry == "78000"


@pytest.mark.parametrize(
    "relative_stop",
    [
        "止损 100点",
        "stop 100 points below entry",
        "stop 100 pts below entry",
        "stop 2 percent below entry",
        "止损 100刀",
        "stop 100 USDT below entry",
        "进场价-100U",
        "entry price minus 100",
        "entry price plus 100",
        "entry price below 100",
        "止损距离 500U",
        "风险距离 500U",
        "SL gap 500",
        "止损偏移 500",
    ],
)
def test_candidate_parser_rejects_relative_prices_instead_of_guessing_absolute_values(
    relative_stop,
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="68000",
        stop_loss_text=relative_stop,
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == "stop_loss"


def test_numbered_take_profit_labels_do_not_become_prices():
    for take_profit_text in (
        "TP1 69000 TP2 70000",
        "止盈1:69000 止盈2:70000",
    ):
        result = validate_candidate_entry_price_geometry(
            side="long",
            entry_text="68000",
            stop_loss_text="67500",
            take_profit_text=take_profit_text,
            symbol="BTC",
        )

        assert result.status == "valid"
        assert result.normalized_take_profit_prices == ("69000", "70000")


@pytest.mark.parametrize(
    "take_profit_text",
    ["TP 69000", "Take profit 69000", "止盈 69000", "目标 69000"],
)
def test_unnumbered_take_profit_labels_preserve_the_full_price(take_profit_text):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="68000",
        stop_loss_text="67500",
        take_profit_text=take_profit_text,
        symbol="BTC",
    )

    assert result.status == "valid"
    assert result.normalized_take_profit_prices == ("69000",)


@pytest.mark.parametrize(
    "stop_loss_text",
    ["止损 67500U", "SL 67500 USDT", "$67500", "67500美元"],
)
def test_absolute_price_currency_units_are_not_mistaken_for_relative_offsets(
    stop_loss_text,
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="68000",
        stop_loss_text=stop_loss_text,
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "valid"
    assert result.normalized_stop_loss == "67500"


@pytest.mark.parametrize(
    ("entry_text", "stop_loss_text", "take_profit_text"),
    [
        ("BTC 68000", "67500", "69000"),
        ("68000", "BTC止损67500", "69000"),
        ("68000", "67500", "BTC TP 69000"),
        ("68000", "67500", "take profits 69000"),
    ],
)
def test_matching_symbol_and_common_absolute_labels_are_accepted(
    entry_text, stop_loss_text, take_profit_text
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text=stop_loss_text,
        take_profit_text=take_profit_text,
        symbol="BTC",
    )

    assert result.status == "valid"


def test_mismatched_symbol_token_is_not_silently_discarded():
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="ETH 68000",
        stop_loss_text="67500",
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.offending_field == "entry_prices"


@pytest.mark.parametrize("stop_loss_text", ["-67500", "SL -67500", "+67500"])
def test_signed_candidate_price_is_not_silently_converted_to_positive(
    stop_loss_text,
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="68000",
        stop_loss_text=stop_loss_text,
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.offending_field == "stop_loss"


@pytest.mark.parametrize(
    ("entry_text", "take_profit_text", "offending_field"),
    [
        ("-68000", "69000", "entry_prices"),
        ("68000", "TP -69000", "take_profit"),
    ],
)
def test_signed_entry_or_take_profit_is_not_silently_made_positive(
    entry_text, take_profit_text, offending_field
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text="67500",
        take_profit_text=take_profit_text,
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.offending_field == offending_field


@pytest.mark.parametrize(
    ("entry_text", "stop_loss_text", "take_profit_text", "offending_field"),
    [
        (".5", ".4", ".6", "entry_prices"),
        ("68000", ".4", "69000", "stop_loss"),
        ("68000", "67500", ".6", "take_profit"),
        ("68000..67000", "65000", "69000", "entry_prices"),
    ],
)
def test_malformed_decimal_syntax_is_not_silently_reinterpreted_as_prices(
    entry_text,
    stop_loss_text,
    take_profit_text,
    offending_field,
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text=stop_loss_text,
        take_profit_text=take_profit_text,
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == offending_field


@pytest.mark.parametrize(
    "entry_text",
    [
        "市价-100U",
        "现价+100U",
        "market price minus 100U",
        "current price below 100U",
        "market / 2",
        "现价/挂单67000",
        "市价进场",
        "市价进场/挂单67000",
    ],
)
def test_market_relative_entry_expression_is_not_treated_as_absolute(entry_text):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text="67500",
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.offending_field == "entry_prices"


@pytest.mark.parametrize(
    "entry_text",
    [
        "市价 68000",
        "现价68000/挂单67000",
        "market price 68000/limit 67000",
        "补仓 68000",
        "加仓点位 68000",
        "首仓68000/补仓67000",
        "入场1:68000 入场2:67000",
        "市价进场/68000",
        "市价进场/68000附近",
        "市价进场68000",
        "市价进场：68000",
        "现价入场/68000",
        "market entry/68000",
    ],
)
def test_proven_absolute_market_and_multi_leg_entries_are_accepted(entry_text):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text="65000",
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "valid"


@pytest.mark.parametrize(
    "entry_text",
    ["68000 - 67000", "Entry: 68000 - 67000"],
)
def test_spaced_ascii_hyphen_entry_range_is_not_mistaken_for_negative_price(
    entry_text,
):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text="65000",
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "valid"
    assert result.normalized_entry_prices == ("68000", "67000")


def test_market_entry_with_reference_price_keeps_its_price_and_direction():
    """``市价进场/2415`` is the shape the recognition prompt asks MiMo for.

    It is one leg -- market, reference price 2415 -- not a priceless market leg
    beside a priced limit leg, so the price survives into ``entry_values`` and
    the long/short geometry still decides the outcome.
    """

    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="市价进场/2415",
        stop_loss_text="2355",
        take_profit_text="2620",
        symbol="ETH",
    )

    assert result.status == "valid"
    assert result.normalized_entry_prices == ("2415",)

    inverted = validate_candidate_entry_price_geometry(
        side="short",
        entry_text="市价进场/2415",
        stop_loss_text="2355",
        take_profit_text="2620",
        symbol="ETH",
    )

    assert inverted.status == "invalid"


def test_proven_absolute_hybrid_market_and_limit_legs_are_accepted():
    result = validate_candidate_entry_price_geometry(
        side="short",
        entry_text="1695市价/1765挂单",
        stop_loss_text="1830",
        take_profit_text="1673/1638",
        symbol="ETH",
    )

    assert result.status == "valid"


@pytest.mark.parametrize(
    "stop_loss_text",
    [
        "止损位 67500",
        "止损价位 67500",
        "SL price 67500",
        "止损：67500！！！",
    ],
)
def test_common_absolute_stop_labels_and_punctuation_are_accepted(stop_loss_text):
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="68000",
        stop_loss_text=stop_loss_text,
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "valid"


@pytest.mark.parametrize("missing_field", ["side", "position_side"])
def test_final_draft_rejects_missing_leg_direction_even_with_expected_side(
    missing_field,
):
    leg = {"position_side": "long", "side": "buy", "price": 68000}
    leg.pop(missing_field)
    result = validate_order_draft_price_geometry(
        {
            "order_legs": [leg],
            "stop_loss": 67500,
            "take_profit_legs": [{"price": 69000}],
            "contract_spec": {"price_tick": 0.1},
        },
        expected_position_side="long",
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == "side"


@pytest.mark.parametrize(
    "malformed_take_profit_legs",
    [{}, 0, False, ""],
)
def test_final_draft_rejects_explicit_malformed_take_profit_container(
    malformed_take_profit_legs,
):
    result = validate_order_draft_price_geometry(
        {
            "order_legs": [
                {
                    "position_side": "long",
                    "side": "buy",
                    "price": 68000,
                }
            ],
            "stop_loss": 67500,
            "take_profit_legs": malformed_take_profit_legs,
            "contract_spec": {"price_tick": 0.1},
        }
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == "take_profit"


@pytest.mark.parametrize(
    "direction_override",
    [
        {"leg_side": "sell"},
        {"top_position_side": "short"},
        {"top_open_side": "sell"},
    ],
)
def test_final_draft_rejects_conflicting_direction_aliases(direction_override):
    draft = {
        "order_legs": [
            {"position_side": "long", "side": "buy", "price": 68000}
        ],
        "stop_loss": 67500,
        "take_profit_legs": [{"price": 69000}],
        "contract_spec": {"price_tick": 0.1},
    }
    if "leg_side" in direction_override:
        draft["order_legs"][0]["side"] = direction_override["leg_side"]
    if "top_position_side" in direction_override:
        draft["position_side"] = direction_override["top_position_side"]
    if "top_open_side" in direction_override:
        draft["open_side"] = direction_override["top_open_side"]

    result = validate_order_draft_price_geometry(draft)

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == "side"


@pytest.mark.parametrize(
    "entry_text",
    ["市价", "市价进场", "现价做多", "market", "BTC市价进场"],
)
def test_price_less_market_entry_text_is_recognised(entry_text):
    assert is_pure_market_entry_text(entry_text, symbol="BTC") is True


@pytest.mark.parametrize(
    "entry_text",
    [
        "",
        None,
        "市价-100U",
        "现价/挂单67000",
        "market / 2",
        "等回调再市价",
        "市价进场/2415",
        "68000-68200",
    ],
)
def test_texts_that_are_not_a_price_less_market_entry(entry_text):
    """An empty entry text is never a market entry, whatever the body says."""

    assert is_pure_market_entry_text(entry_text, symbol="BTC") is False


@pytest.mark.parametrize(
    "entry_text",
    ["市价", "市价进场", "现价做多", "market"],
)
def test_price_less_market_entry_stays_indeterminate_by_default(entry_text):
    """The opt-in is the only thing that admits a price-less market entry."""

    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text=entry_text,
        stop_loss_text="67500",
        take_profit_text="69000",
        symbol="BTC",
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert result.offending_field == "entry_prices"
    assert result.reference_entry_required is False


def test_price_less_market_entry_with_a_stop_proves_the_protection_side():
    result = validate_candidate_entry_price_geometry(
        side="long",
        entry_text="市价",
        stop_loss_text="76000",
        take_profit_text=None,
        symbol="BTC",
        allow_reference_entry=True,
    )

    assert result.status == "valid"
    assert result.reason_code is None
    assert result.reference_entry_required is True
    assert result.normalized_stop_loss == "76000"
    assert result.normalized_entry_prices == ()
    assert result.bounded_evidence()["reference_entry_required"] is True


@pytest.mark.parametrize(
    ("side", "stop_loss_text", "take_profit_text", "status", "reason", "field"),
    [
        (
            "long",
            "76000",
            "75000",
            "invalid",
            "entry_price_geometry_take_profit_side_invalid",
            "take_profit",
        ),
        (
            "long",
            "76000",
            "76000",
            "invalid",
            "entry_price_geometry_equal_boundary",
            "take_profit",
        ),
        (
            "long",
            None,
            "78000",
            "indeterminate",
            "entry_price_geometry_required_value_missing",
            "stop_loss",
        ),
        (
            "long",
            "5%",
            None,
            "indeterminate",
            "entry_price_geometry_ambiguous",
            "stop_loss",
        ),
        (
            "short",
            "76000",
            "78000",
            "invalid",
            "entry_price_geometry_take_profit_side_invalid",
            "take_profit",
        ),
        (
            "sideways",
            "76000",
            None,
            "indeterminate",
            "entry_price_geometry_ambiguous",
            "side",
        ),
    ],
)
def test_price_less_market_entry_still_refuses_a_broken_protection_side(
    side, stop_loss_text, take_profit_text, status, reason, field
):
    result = validate_candidate_entry_price_geometry(
        side=side,
        entry_text="市价",
        stop_loss_text=stop_loss_text,
        take_profit_text=take_profit_text,
        symbol="BTC",
        allow_reference_entry=True,
    )

    assert result.status == status
    assert result.reason_code == reason
    assert result.offending_field == field
    assert result.reference_entry_required is False


@pytest.mark.parametrize(
    ("side", "stop_loss_text", "take_profit_text", "reference"),
    [
        ("long", "76000", "78000", 76500.0),
        ("short", "76000", "74000", 75740.0),
    ],
)
def test_price_less_market_entry_takes_the_live_price_as_its_entry(
    side, stop_loss_text, take_profit_text, reference
):
    result = validate_candidate_entry_price_geometry(
        side=side,
        entry_text="市价",
        stop_loss_text=stop_loss_text,
        take_profit_text=take_profit_text,
        symbol="BTC",
        reference_price=reference,
        resolved_entry_prices=(reference, reference),
        allow_reference_entry=True,
    )

    rendered = format(Decimal(str(reference)).normalize(), "f")
    assert result.status == "valid"
    assert result.normalized_entry_prices == (rendered, rendered)
    assert result.entry_min == rendered
    assert result.entry_max == rendered
    # The proof is complete once the live price is in hand, so the caller is no
    # longer owed anything: the flag exists to say "still owed", not "was".
    assert result.reference_entry_required is False


@pytest.mark.parametrize(
    ("side", "reference"),
    [("long", 75740.0), ("short", 76500.0)],
)
def test_price_less_market_entry_is_refused_when_the_live_price_crossed_the_stop(
    side, reference
):
    """The 17019 shape: a long whose stop already sits above the live price."""

    result = validate_candidate_entry_price_geometry(
        side=side,
        entry_text="市价",
        stop_loss_text="76000",
        take_profit_text=None,
        symbol="BTC",
        reference_price=reference,
        resolved_entry_prices=(reference, reference),
        allow_reference_entry=True,
    )

    assert result.status == "invalid"
    assert result.reason_code == "entry_price_geometry_stop_side_invalid"
    assert result.offending_field == "stop_loss"


@pytest.mark.parametrize(
    ("entry_text", "stop_loss_text", "take_profit_text"),
    [
        ("市价进场/2415", "2355", "2620"),
        ("77300", "76000", "79000"),
        ("现价/挂单67000", "66000", "69000"),
        ("68000-68200", "67500", "69000 / 70000"),
        ("市价 68000", "67500", "69000"),
        ("市价-100U", "67500", "69000"),
        ("market / 2", "67500", "69000"),
        ("市价进场/挂单67000", "66000", "69000"),
        ("等回调再市价", "67500", "69000"),
        ("", "67500", "69000"),
        (None, "67500", "69000"),
    ],
)
@pytest.mark.parametrize("reference", [None, 68100.0])
def test_opt_in_changes_nothing_for_a_text_that_is_not_a_price_less_market_entry(
    entry_text, stop_loss_text, take_profit_text, reference
):
    """Every non-pure-market text answers identically with the flag either way."""

    kwargs = dict(
        side="long",
        entry_text=entry_text,
        stop_loss_text=stop_loss_text,
        take_profit_text=take_profit_text,
        symbol="BTC",
        reference_price=reference,
        resolved_entry_prices=None if reference is None else (reference, reference),
    )

    assert validate_candidate_entry_price_geometry(
        **kwargs, allow_reference_entry=True
    ) == validate_candidate_entry_price_geometry(**kwargs)


def test_stale_market_reference_result_is_a_bounded_refusal():
    result = stale_market_reference_result("市价")

    assert result.status == "indeterminate"
    assert result.reason_code == MARKET_REFERENCE_STALE
    assert result.offending_field == "entry_prices"
    assert result.offending_value == "市价"
    # ``execution_events`` truncates the reason code at 64 characters, and a
    # truncated code would not match any branch that reads it back.
    assert len(MARKET_REFERENCE_STALE) <= 64


def test_final_draft_without_contract_tick_is_indeterminate():
    result = validate_order_draft_price_geometry(
        {
            "order_legs": [
                {"position_side": "long", "side": "buy", "price": 68000}
            ],
            "stop_loss": 67500,
            "take_profit_legs": [{"price": 69000}],
            "contract_spec": {},
        }
    )

    assert result.status == "indeterminate"
    assert result.reason_code == "entry_price_geometry_required_value_missing"
    assert result.offending_field == "price_tick"
