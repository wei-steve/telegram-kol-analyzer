import pytest

import telegram_kol_research.deepcoin_order_builder as deepcoin_order_builder

from telegram_kol_research.deepcoin_order_builder import DeepcoinOrderDraftError
from telegram_kol_research.deepcoin_order_builder import (
    _coalesce_equivalent_entry_legs,
)
from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.recovery_live_submit import build_deepcoin_trigger_order_payload


def _payload_preview(**overrides):
    values = {
        "venue": "deepcoin",
        "contract": "BTC-USDT",
        "order_type": "limit",
        "open_side": "buy",
        "position_side": "long",
        "entry_range": "68000-68200",
        "stop_loss": "67500",
        "take_profit": "69000-70000-71000",
        "risk_budget_usdt": 100.0,
        "source": {
            "kol_id": "alice",
            "chat_id": 100,
            "message_id": 55,
        },
    }
    values.update(overrides)
    return values


def _btc_contract_spec():
    return DeepcoinContractSpec(
        instrument_id="BTC-USDT-SWAP",
        contract_value=0.001,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.1,
    )


def test_build_deepcoin_order_draft_splits_long_limit_order_into_range_endpoints():
    draft = build_deepcoin_order_draft(_payload_preview())

    assert draft == {
        "venue": "deepcoin",
        "dry_run_only": True,
        "executable": False,
        "blocking_reason_codes": ["contract_size_unverified"],
        "strategy_instance_id": "deepcoin:100:55:BTC:long",
        "symbol": "BTC",
        "instrument_id": "BTC-USDT-SWAP",
        "margin_mode": "cross",
        "position_mode": "split",
        "order_legs": [
            {
                "side": "buy",
                "position_side": "long",
                "order_type": "limit",
                "price": 68200.0,
                "allocation_pct": 58.333333333333336,
                "risk_budget_usdt": 58.3333,
                "client_order_id": "TK649760E806ACF61",
                "quantity": 0.083333,
                "quantity_unit": "base_asset_estimate",
                "estimated_stop_loss_usdt": 58.3331,
            },
            {
                "side": "buy",
                "position_side": "long",
                "order_type": "limit",
                "price": 68000.0,
                "allocation_pct": 41.666666666666664,
                "risk_budget_usdt": 41.6667,
                "client_order_id": "TK729D11F4739D2A2",
                "quantity": 0.083333,
                "quantity_unit": "base_asset_estimate",
                "estimated_stop_loss_usdt": 41.6665,
            },
        ],
        "stop_loss": 67500.0,
        "take_profit_legs": [
            {
                "index": 1,
                "price": 69000.0,
                "allocation_pct": 40.0,
                "order_type": "market_on_trigger",
            },
            {
                "index": 2,
                "price": 70000.0,
                "allocation_pct": 30.0,
                "order_type": "market_on_trigger",
            },
            {
                "index": 3,
                "price": 71000.0,
                "allocation_pct": 30.0,
                "order_type": "market_on_trigger",
            },
        ],
        "risk_budget_usdt": 100.0,
        "source": {
            "kol_id": "alice",
            "chat_id": 100,
            "message_id": 55,
        },
        "notes": [
            "offline_constructor_only",
            "default_cross_margin_split_position",
            "strategy_instance_id_required_for_exit_matching",
            "quantity_uses_linear_price_risk_estimate",
            "limit_edge_selection_side_aware_default",
            "contract_size_must_be_verified_before_live_order",
        ],
    }


def test_pending_limit_entry_embeds_primary_market_stop_without_position_id():
    payload = build_deepcoin_trigger_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 63200.0,
            "order_legs": [{"position_side": "long"}],
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 64000.0,
            "quantity": 6.0,
        },
    )

    assert payload["slTriggerPx"] == "63200.0"
    assert payload["slTriggerPxType"] == "last"
    assert payload["slOrdPx"] == "-1"
    assert "posId" not in payload


def test_build_deepcoin_order_draft_uses_low_then_high_for_short_limit_orders():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="68000-68200",
        )
    )

    assert draft["order_legs"][0]["price"] == 68000.0
    assert draft["order_legs"][1]["price"] == 68200.0
    assert draft["order_legs"][0]["side"] == "sell"
    assert draft["order_legs"][0]["position_side"] == "short"


def test_build_deepcoin_order_draft_sorts_short_take_profit_from_nearest_to_farthest():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            take_profit="57000 / 59000 / 58000",
        )
    )

    assert [leg["price"] for leg in draft["take_profit_legs"]] == [
        59000.0,
        58000.0,
        57000.0,
    ]
    assert [leg["allocation_pct"] for leg in draft["take_profit_legs"]] == [
        40.0,
        30.0,
        30.0,
    ]


def test_build_deepcoin_order_draft_uses_equal_allocations_for_two_take_profits():
    draft = build_deepcoin_order_draft(
        _payload_preview(take_profit="69000 / 70000"),
    )

    assert [leg["allocation_pct"] for leg in draft["take_profit_legs"]] == [50.0, 50.0]


def test_build_deepcoin_order_draft_preserves_four_long_take_profit_targets():
    draft = build_deepcoin_order_draft(
        _payload_preview(take_profit="69000-70000-71000-72000")
    )

    assert [leg["price"] for leg in draft["take_profit_legs"]] == [
        69000.0, 70000.0, 71000.0, 72000.0,
    ]
    assert [leg["allocation_pct"] for leg in draft["take_profit_legs"]] == [
        40.0, 20.0, 20.0, 20.0,
    ]


def test_build_deepcoin_order_draft_preserves_five_short_take_profit_targets():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell", position_side="short",
            take_profit="64200-65250-64750-65150-63800",
        )
    )

    assert [leg["price"] for leg in draft["take_profit_legs"]] == [
        65250.0, 65150.0, 64750.0, 64200.0, 63800.0,
    ]
    assert [leg["allocation_pct"] for leg in draft["take_profit_legs"]] == [
        40.0, 15.0, 15.0, 15.0, 15.0,
    ]


def test_long_range_outside_fixed_threshold_uses_independent_offsets():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            current_price="60700",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("limit", 61090.0),
        ("limit", 60080.0),
    ]


def test_short_range_uses_subtracted_fixed_offsets():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="60000-61000",
            stop_loss="62000",
            current_price="60300",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("limit", 59910.0),
        ("limit", 60920.0),
    ]


def test_long_range_inside_fixed_threshold_uses_market_and_second_offset():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            current_price="60850",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("market", 60850.0),
        ("limit", 60080.0),
    ]


def test_hybrid_market_leg_does_not_apply_first_limit_offset():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="PEPE-USDT",
            open_side="sell",
            position_side="short",
            entry_range="1-2",
            stop_loss="3",
            current_price="1",
            market_leg_threshold="0.1",
            first_limit_offset="1",
            second_limit_offset="0.2",
        )
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("market", 1.0),
        ("limit", 1.8),
    ]


def test_zero_market_threshold_disables_hybrid_at_exact_anchor():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            current_price="61000",
            market_leg_threshold="0",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("limit", 61090.0),
        ("limit", 60080.0),
    ]


def test_zero_offsets_use_original_range_endpoints():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            first_limit_offset="0",
            second_limit_offset="0",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [61000.0, 60000.0]


def test_small_fixed_offsets_survive_until_tick_normalization():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="PEPE-USDT",
            entry_range="0.000010-0.000015",
            stop_loss="0.000005",
            take_profit=None,
            first_limit_offset="0.0000014",
            second_limit_offset="0.0000024",
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="PEPE-USDT-SWAP",
            contract_value=1,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.000001,
        ),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [
        0.000016,
        0.000012,
    ]


def test_fixed_offsets_apply_before_tick_normalization_for_unaligned_range():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="SOL-USDT",
            entry_range="100.01-100.09",
            stop_loss="90",
            take_profit=None,
            first_limit_offset="0.05",
            second_limit_offset="0.05",
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="SOL-USDT-SWAP",
            contract_value=1,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [100.1, 100.0]


def test_range_endpoints_normalized_to_zero_are_rejected():
    with pytest.raises(
        DeepcoinOrderDraftError,
        match="non-positive price after tick normalization",
    ):
        build_deepcoin_order_draft(
            _payload_preview(
                contract="PEPE-USDT",
                entry_range="0.000001-0.000002",
                stop_loss="0.0000005",
                take_profit=None,
                first_limit_offset="0",
                second_limit_offset="0",
            ),
            contract_spec=DeepcoinContractSpec(
                instrument_id="PEPE-USDT-SWAP",
                contract_value=1,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.00001,
            ),
        )


@pytest.mark.parametrize(
    ("order_type", "entry_range"),
    [
        ("market", "0.000001-0.000002"),
        ("limit", "0.000001-0.000001"),
    ],
)
def test_single_and_explicit_market_prices_normalized_to_zero_are_rejected(
    order_type,
    entry_range,
):
    with pytest.raises(
        DeepcoinOrderDraftError,
        match="entry price produces non-positive price after tick normalization",
    ):
        build_deepcoin_order_draft(
            _payload_preview(
                contract="PEPE-USDT",
                order_type=order_type,
                entry_range=entry_range,
                stop_loss="0.0000005",
                take_profit=None,
            ),
            contract_spec=DeepcoinContractSpec(
                instrument_id="PEPE-USDT-SWAP",
                contract_value=1,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.00001,
            ),
        )


def test_hybrid_market_price_normalized_to_zero_is_rejected():
    with pytest.raises(
        DeepcoinOrderDraftError,
        match="hybrid market entry produces non-positive price",
    ):
        build_deepcoin_order_draft(
            _payload_preview(
                contract="PEPE-USDT",
                entry_range="0.000001-0.000002",
                current_price="0.0000015",
                stop_loss="0.0000005",
                take_profit=None,
                market_leg_threshold="0.000001",
            ),
            contract_spec=DeepcoinContractSpec(
                instrument_id="PEPE-USDT-SWAP",
                contract_value=1,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.00001,
            ),
        )


def test_equivalent_normalized_fixed_limit_legs_coalesce():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="PEPE-USDT",
            entry_range="0.000010-0.000011",
            stop_loss="0.000005",
            take_profit=None,
            first_limit_offset="0",
            second_limit_offset="0.000001",
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="PEPE-USDT-SWAP",
            contract_value=1,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.000001,
        ),
    )

    assert len(draft["order_legs"]) == 1
    assert draft["order_legs"][0]["price"] == 0.000011
    assert draft["order_legs"][0]["allocation_pct"] == 100.0
    assert draft["order_legs"][0]["merged_from_leg_indices"] == [1, 2]


def test_fixed_offset_rejects_nonpositive_short_limit_price():
    try:
        build_deepcoin_order_draft(
            _payload_preview(
                contract="PEPE-USDT",
                open_side="sell",
                position_side="short",
                entry_range="1-2",
                stop_loss="3",
                first_limit_offset="1",
            )
        )
    except DeepcoinOrderDraftError as exc:
        assert "fixed entry offset produces non-positive price" in str(exc)
    else:
        raise AssertionError("expected non-positive fixed entry price to fail")


def test_fixed_offset_rejects_price_normalized_to_zero():
    with pytest.raises(
        DeepcoinOrderDraftError,
        match="fixed entry offset produces non-positive price",
    ):
        build_deepcoin_order_draft(
            _payload_preview(
                contract="PEPE-USDT",
                open_side="sell",
                position_side="short",
                entry_range="0.000001-0.000002",
                stop_loss="0.000003",
                take_profit=None,
                second_limit_offset="0.0000016",
            ),
            contract_spec=DeepcoinContractSpec(
                instrument_id="PEPE-USDT-SWAP",
                contract_value=1,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.000001,
            ),
        )


def test_fixed_offset_rejects_value_outside_finite_float_range():
    try:
        build_deepcoin_order_draft(
            _payload_preview(
                entry_range="1-2",
                stop_loss="0.5",
                first_limit_offset="1e1000",
            )
        )
    except DeepcoinOrderDraftError as exc:
        assert "first_limit_offset" in str(exc)
    else:
        raise AssertionError("expected oversized fixed offset to fail")


@pytest.mark.parametrize("invalid", ["-1", -0.1, "nan", "inf", True, {}, [], ""])
def test_fixed_entry_thresholds_reject_invalid_decimals(invalid):
    with pytest.raises(DeepcoinOrderDraftError, match="market_leg_threshold"):
        build_deepcoin_order_draft(
            _payload_preview(market_leg_threshold=invalid)
        )


def test_explicit_market_order_ignores_fixed_range_thresholds():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            order_type="market",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert len(draft["order_legs"]) == 1
    assert draft["order_legs"][0]["order_type"] == "market"
    assert draft["order_legs"][0]["allocation_pct"] == 100.0


def test_build_deepcoin_order_draft_builds_single_market_order_leg():
    draft = build_deepcoin_order_draft(
        _payload_preview(order_type="market"),
        contract_spec=DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP",
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )

    assert draft["blocking_reason_codes"] == []
    assert len(draft["order_legs"]) == 1
    assert draft["order_legs"][0]["order_type"] == "market"
    assert draft["order_legs"][0]["allocation_pct"] == 100.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"


def test_build_single_price_limit_entry_as_one_full_risk_leg():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="63700-63700", stop_loss="62500"),
        contract_spec=_btc_contract_spec(),
    )

    assert len(draft["order_legs"]) == 1
    assert draft["order_legs"][0]["price"] == 63700.0
    assert draft["order_legs"][0]["allocation_pct"] == 100.0
    assert draft["order_legs"][0]["risk_budget_usdt"] == 100.0
    assert draft["order_legs"][0]["client_order_id"].endswith("1")
    assert draft["order_legs"][0]["quantity"] == 83


def test_build_true_range_limit_entry_retains_two_configured_style_legs():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="63300-63700",
            stop_loss="62500",
            entry_range_order_style="eager",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [63700.0, 63300.0]
    assert [leg["order_type"] for leg in draft["order_legs"]] == ["limit", "limit"]
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [60.0, 40.0]


def test_true_range_balances_quantity_by_stop_distance():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="70000-71000",
            stop_loss="68000",
            risk_budget_usdt=20.0,
        )
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [71000.0, 70000.0]
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [60.0, 40.0]
    assert [leg["risk_budget_usdt"] for leg in draft["order_legs"]] == [12.0, 8.0]
    assert [leg["quantity"] for leg in draft["order_legs"]] == [0.004, 0.004]


def test_short_true_range_prioritizes_the_first_triggering_leg():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="68000-70000",
            stop_loss="71000",
            risk_budget_usdt=20.0,
        )
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [68000.0, 70000.0]
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [65.0, 35.0]
    assert sum(leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]) <= 20.0001


def test_true_range_caps_first_leg_risk_at_sixty_five_percent():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="70000-72000",
            stop_loss="69000",
            risk_budget_usdt=20.0,
        )
    )

    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [65.0, 35.0]
    assert sum(leg["risk_budget_usdt"] for leg in draft["order_legs"]) == 20.0
    assert sum(leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]) <= 20.0001


def test_true_range_sizes_against_the_normalized_submitted_stop():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="SOL-USDT",
            entry_range="100.1-100.2",
            stop_loss="100.09",
            risk_budget_usdt=20.0,
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="SOL-USDT-SWAP",
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )

    assert draft["stop_loss"] == 100.0
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [65.0, 35.0]
    assert [leg["base_asset_estimate"] for leg in draft["order_legs"]] == [65.0, 70.0]
    assert sum(leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]) <= 20.0


def test_true_range_without_stop_loss_keeps_equal_risk_allocation():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="70000-71000", stop_loss=None)
    )

    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [50.0, 50.0]
    assert [leg["quantity"] for leg in draft["order_legs"]] == [None, None]


def test_build_one_tick_conservative_range_retains_two_distinct_normalized_legs():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="63700.0-63700.1", stop_loss="62500"),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [63700.1, 63700.0]
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == pytest.approx(
        [50.002083246531356, 49.997916753468644]
    )


def test_build_range_coalesces_equivalent_normalized_entry_legs(monkeypatch):
    monkeypatch.setattr(
        deepcoin_order_builder,
        "_range_entry_leg_prices",
        lambda **_kwargs: (63700.0, 63700.0),
    )

    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="63700-63701",
            stop_loss="62500",
            entry_range_order_style="eager",
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP",
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=1,
        ),
    )

    assert draft["order_legs"] == [
        {
            "side": "buy",
            "position_side": "long",
            "order_type": "limit",
            "price": 63700.0,
            "allocation_pct": 100.0,
            "risk_budget_usdt": 100.0,
            "client_order_id": "TK649760E806ACF61",
            "quantity": 82.0,
            "quantity_unit": "contracts",
            "base_asset_estimate": 0.083334,
            "estimated_stop_loss_usdt": 98.4,
            "merged_from_leg_indices": [1, 2],
        }
    ]


def test_coalesce_equivalent_entry_legs_preserves_distinct_economic_identities():
    base_leg = {
        "side": "buy",
        "position_side": "long",
        "order_type": "limit",
        "price": 63700.0,
        "allocation_pct": 10.0,
        "risk_budget_usdt": 10.0,
        "client_order_id": "first",
        "quantity": 8.0,
        "quantity_unit": "contracts",
        "base_asset_estimate": 0.008,
        "estimated_stop_loss_usdt": 9.6,
    }
    variants = [
        {**base_leg, "client_order_id": "different-price", "price": 63701.0},
        {**base_leg, "client_order_id": "different-type", "order_type": "market"},
        {**base_leg, "client_order_id": "different-side", "side": "sell"},
        {**base_leg, "client_order_id": "different-position", "position_side": "short"},
        {
            **base_leg,
            "client_order_id": "different-unit",
            "quantity_unit": "base_asset_estimate",
        },
    ]

    result = _coalesce_equivalent_entry_legs(
        [base_leg, *variants, {**base_leg, "client_order_id": "duplicate"}]
    )

    assert [leg["client_order_id"] for leg in result] == [
        "first",
        "different-price",
        "different-type",
        "different-side",
        "different-position",
        "different-unit",
    ]
    assert result[0]["allocation_pct"] == 20.0
    assert result[0]["merged_from_leg_indices"] == [1, 7]
    assert all("merged_from_leg_indices" not in leg for leg in result[1:])


def test_build_deepcoin_order_draft_hybrid_range_entry_near_upper_edge():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            contract="ETH-USDT",
            entry_range="1565-1585",
            stop_loss="1545",
            take_profit="1605/1625/1645",
            risk_budget_usdt=20.0,
            current_price=1585.0,
            market_leg_threshold="4",
            first_limit_offset="2",
            second_limit_offset="2",
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP",
            contract_value=0.1,
            quantity_step=0.1,
            min_quantity=0.1,
            price_tick=0.01,
        ),
    )

    assert draft["blocking_reason_codes"] == []
    assert draft["order_legs"][0]["order_type"] == "market"
    assert draft["order_legs"][0]["price"] == 1585.0
    assert draft["order_legs"][0]["risk_budget_usdt"] == 12.9032
    assert draft["order_legs"][0]["quantity"] == 3.2
    assert draft["order_legs"][0]["estimated_stop_loss_usdt"] == 12.8
    assert draft["order_legs"][1]["order_type"] == "limit"
    assert draft["order_legs"][1]["price"] == 1567.0
    assert draft["order_legs"][1]["risk_budget_usdt"] == 7.09677
    assert draft["order_legs"][1]["quantity"] == 3.2
    assert draft["order_legs"][1]["estimated_stop_loss_usdt"] == 7.04
    assert "range_entry_hybrid_market_dynamic_risk_limit" in draft["notes"]


def test_build_deepcoin_order_draft_uses_configured_kol_code_for_client_order_id():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            order_type="market",
            contract="ETH-USDT",
            open_side="sell",
            position_side="short",
            entry_range="2500-2500",
            stop_loss="2600",
            source={
                "kol_id": "group:-1002409877375",
                "chat_id": -1002409877375,
                "message_id": 8248,
            },
        ),
        contract_spec=DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP",
            contract_value=0.1,
            quantity_step=0.1,
            min_quantity=0.1,
            price_tick=0.01,
        ),
    )

    assert draft["source"]["kol_code"] == "FG"
    assert draft["order_legs"][0]["client_order_id"] == "TKFG8248E1"
    assert draft["order_legs"][0]["client_order_id"].isalnum()
    assert len(draft["order_legs"][0]["client_order_id"]) <= 20


def test_build_deepcoin_order_draft_rejects_missing_entry_range():
    try:
        build_deepcoin_order_draft(_payload_preview(entry_range=None))
    except DeepcoinOrderDraftError as exc:
        assert "entry_range is required" in str(exc)
    else:
        raise AssertionError("expected missing entry range to fail")


def test_build_deepcoin_order_draft_blocks_quantity_when_stop_loss_is_missing():
    draft = build_deepcoin_order_draft(_payload_preview(stop_loss=None))

    assert draft["blocking_reason_codes"] == ["missing_stop_loss"]
    assert draft["order_legs"][0]["quantity"] is None
    assert draft["take_profit_legs"][0]["allocation_pct"] == 40.0
    assert draft["notes"] == [
        "offline_constructor_only",
        "default_cross_margin_split_position",
        "strategy_instance_id_required_for_exit_matching",
        "quantity_requires_stop_loss_or_manual_sizing",
        "limit_edge_selection_side_aware_default",
    ]


def test_build_deepcoin_order_draft_converts_base_estimate_with_contract_spec():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="68000.04-68200.06"),
        contract_spec=DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP",
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )

    assert draft["blocking_reason_codes"] == []
    assert draft["contract_spec"] == {
        "instrument_id": "BTC-USDT-SWAP",
        "contract_value": 0.001,
        "quantity_step": 1.0,
        "min_quantity": 1.0,
        "price_tick": 0.1,
    }
    assert draft["order_legs"][0]["price"] == 68200.1
    assert draft["order_legs"][0]["quantity"] == 83.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][0]["base_asset_estimate"] == 0.083326
    assert draft["order_legs"][1]["price"] == 68000.0


def test_build_deepcoin_order_draft_expands_btc_wan_shorthand_prices():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="5.89-5.93附近",
            stop_loss="5.78",
            risk_budget_usdt=20.0,
            take_profit="6万附近 / 6.07附近 / 6.23",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [59300.0, 58900.0]
    assert [leg["risk_budget_usdt"] for leg in draft["order_legs"]] == [
        11.5385,
        8.46154,
    ]
    assert [leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]] == [
        10.5,
        7.7,
    ]
    assert draft["stop_loss"] == 57800.0
    assert [leg["price"] for leg in draft["take_profit_legs"]] == [
        60000.0,
        60700.0,
        62300.0,
    ]
    assert draft["blocking_reason_codes"] == []
    assert draft["order_legs"][1]["quantity_unit"] == "contracts"


def test_build_deepcoin_order_draft_blocks_when_contract_quantity_is_below_minimum():
    draft = build_deepcoin_order_draft(
        _payload_preview(),
        contract_spec=DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP",
            contract_value=1,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )

    assert draft["blocking_reason_codes"] == ["quantity_below_minimum"]
    assert draft["order_legs"][0]["quantity"] == 0.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][0]["base_asset_estimate"] == 0.083333


def test_build_deepcoin_order_draft_rejects_mismatched_contract_spec():
    try:
        build_deepcoin_order_draft(
            _payload_preview(),
            contract_spec=DeepcoinContractSpec(
                instrument_id="ETH-USDT-SWAP",
                contract_value=0.01,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.01,
            ),
        )
    except DeepcoinOrderDraftError as exc:
        assert "contract_spec instrument_id mismatch" in str(exc)
    else:
        raise AssertionError("expected mismatched contract spec to fail")
