from telegram_kol_research.deepcoin_order_builder import DeepcoinOrderDraftError
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


def test_build_deepcoin_order_draft_splits_long_limit_order_into_edge_and_midpoint():
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
                "price": 68100.0,
                "allocation_pct": 50.0,
                "risk_budget_usdt": 50.0,
                "client_order_id": "TK649760E806ACF61",
                "quantity": 0.083333,
                "quantity_unit": "base_asset_estimate",
                "estimated_stop_loss_usdt": 49.9998,
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
                "allocation_pct": 50.0,
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
                "allocation_pct": 20.0,
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


def test_build_deepcoin_order_draft_uses_upper_edge_for_conservative_short_limit_orders():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="68000-68200",
        )
    )

    assert draft["order_legs"][0]["price"] == 68100.0
    assert draft["order_legs"][1]["price"] == 68200.0
    assert draft["order_legs"][0]["side"] == "sell"
    assert draft["order_legs"][0]["position_side"] == "short"


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
    assert draft["order_legs"][1]["price"] == 1575.0
    assert draft["order_legs"][1]["risk_budget_usdt"] == 10.0
    assert draft["order_legs"][1]["quantity"] == 3.3
    assert draft["order_legs"][1]["estimated_stop_loss_usdt"] == 9.9
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
    assert draft["take_profit_legs"][0]["allocation_pct"] == 50.0
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
    assert draft["order_legs"][0]["price"] == 68100.0
    assert draft["order_legs"][0]["quantity"] == 83.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][0]["base_asset_estimate"] == 0.083333
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

    assert [leg["price"] for leg in draft["order_legs"]] == [59100.0, 58900.0]
    assert [leg["risk_budget_usdt"] for leg in draft["order_legs"]] == [10.0, 10.0]
    assert [leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]] == [9.1, 9.9]
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
