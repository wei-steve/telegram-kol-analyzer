import telegram_kol_research.deepcoin_order_builder as deepcoin_order_builder

from telegram_kol_research.deepcoin_order_builder import DeepcoinOrderDraftError
from telegram_kol_research.deepcoin_order_builder import (
    _coalesce_equivalent_entry_legs,
)
from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


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
                "allocation_pct": 50.0,
                "risk_budget_usdt": 50.0,
                "client_order_id": "TK649760E806ACF61",
                "quantity": 0.071429,
                "quantity_unit": "base_asset_estimate",
                "estimated_stop_loss_usdt": 50.0003,
            },
            {
                "side": "buy",
                "position_side": "long",
                "order_type": "limit",
                "price": 68000.0,
                "allocation_pct": 50.0,
                "risk_budget_usdt": 50.0,
                "client_order_id": "TK729D11F4739D2A2",
                "quantity": 0.1,
                "quantity_unit": "base_asset_estimate",
                "estimated_stop_loss_usdt": 50.0,
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


def test_build_deepcoin_order_draft_eager_long_uses_adjusted_range_endpoints():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="59800-57800",
            stop_loss="57000",
            take_profit="61000-62300-63800",
            entry_range_order_style="eager",
            max_market_entry_deviation_pct=0.15,
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [59889.7, 57886.7]
    assert [leg["order_type"] for leg in draft["order_legs"]] == ["limit", "limit"]
    assert [leg["position_side"] for leg in draft["order_legs"]] == ["long", "long"]


def test_build_deepcoin_order_draft_eager_short_uses_adjusted_range_endpoints():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="59800-57800",
            stop_loss="60600",
            take_profit="57000-56000",
            entry_range_order_style="eager",
            max_market_entry_deviation_pct=0.15,
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [57713.3, 59710.3]
    assert [leg["order_type"] for leg in draft["order_legs"]] == ["limit", "limit"]
    assert [leg["position_side"] for leg in draft["order_legs"]] == ["short", "short"]


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
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [50.0, 50.0]


def test_build_one_tick_conservative_range_retains_two_distinct_normalized_legs():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="63700.0-63700.1", stop_loss="62500"),
        contract_spec=_btc_contract_spec(),
    )

    assert [leg["price"] for leg in draft["order_legs"]] == [63700.1, 63700.0]
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [50.0, 50.0]


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
            max_market_entry_deviation_pct=0.15,
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
    assert draft["order_legs"][0]["risk_budget_usdt"] == 10.0
    assert draft["order_legs"][0]["quantity"] == 2.5
    assert draft["order_legs"][0]["estimated_stop_loss_usdt"] == 10.0
    assert draft["order_legs"][1]["order_type"] == "limit"
    assert draft["order_legs"][1]["price"] == 1567.34
    assert draft["order_legs"][1]["risk_budget_usdt"] == 10.0
    assert draft["order_legs"][1]["quantity"] == 4.4
    assert draft["order_legs"][1]["estimated_stop_loss_usdt"] == 9.8296
    assert "range_entry_hybrid_market_half_limit_half" in draft["notes"]


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
    assert draft["order_legs"][0]["quantity"] == 71.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][0]["base_asset_estimate"] == 0.071418
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
    assert [leg["risk_budget_usdt"] for leg in draft["order_legs"]] == [10.0, 10.0]
    assert [leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]] == [9.0, 9.9]
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
    assert draft["order_legs"][0]["base_asset_estimate"] == 0.071429


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
