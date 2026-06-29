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


def test_build_deepcoin_order_draft_splits_long_limit_order_into_edge_and_midpoint():
    draft = build_deepcoin_order_draft(_payload_preview())

    assert draft == {
        "venue": "deepcoin",
        "dry_run_only": True,
        "executable": False,
        "blocking_reason_codes": ["contract_size_unverified"],
        "symbol": "BTC",
        "instrument_id": "BTC-USDT-SWAP",
        "margin_mode": "isolated",
        "position_mode": "split",
        "order_legs": [
            {
                "side": "buy",
                "position_side": "long",
                "order_type": "limit",
                "price": 68100.0,
                "allocation_pct": 50.0,
                "quantity": 0.083333,
                "quantity_unit": "base_asset_estimate",
            },
            {
                "side": "buy",
                "position_side": "long",
                "order_type": "limit",
                "price": 68000.0,
                "allocation_pct": 50.0,
                "quantity": 0.1,
                "quantity_unit": "base_asset_estimate",
            },
        ],
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


def test_build_deepcoin_order_draft_rejects_unsupported_market_order_preview():
    try:
        build_deepcoin_order_draft(_payload_preview(order_type="market"))
    except DeepcoinOrderDraftError as exc:
        assert "unsupported order_type" in str(exc)
    else:
        raise AssertionError("expected unsupported order type to fail")


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
    assert draft["order_legs"][1]["quantity"] == 100.0
    assert draft["order_legs"][1]["quantity_unit"] == "contracts"
    assert "contract_spec_applied" in draft["notes"]
    assert "quantity_rounded_down_to_step" in draft["notes"]
    assert "price_rounded_to_tick" in draft["notes"]


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
