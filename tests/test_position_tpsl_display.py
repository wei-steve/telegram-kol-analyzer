import pytest

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import StaticDeepcoinContractSpecProvider
from telegram_kol_research.position_tpsl_display import build_position_tpsl_display
from telegram_kol_research.protection_ledger import (
    build_account_protection_ownership,
)


def _btc_specs():
    return StaticDeepcoinContractSpecProvider(
        specs_by_instrument_id={
            "BTC-USDT-SWAP": DeepcoinContractSpec(
                instrument_id="BTC-USDT-SWAP",
                contract_value=0.001,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.1,
            )
        }
    )


def test_display_joins_direct_position_id_and_splits_combined_tpsl():
    result = build_position_tpsl_display(
        positions=[{"posId": "pos-a", "instId": "BTC-USDT-SWAP", "posSide": "long"}],
        pending_orders=[
            {
                "ordId": "combined-1",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "sz": "3",
                "slTriggerPx": "62000",
                "tpTriggerPx": "68000",
            }
        ],
        exact_order_position_ids={},
    )

    assert [(row.kind, row.order_id) for row in result.by_pos_id["pos-a"]] == [
        ("take_profit", "combined-1"),
        ("stop_loss", "combined-1"),
    ]
    assert result.unattributed == []


def test_display_uses_verified_local_order_mapping_and_leaves_unknown_zero_size_unattributed():
    result = build_position_tpsl_display(
        positions=[
            {"posId": "a", "instId": "BTC-USDT-SWAP", "posSide": "long"},
            {"posId": "b", "instId": "BTC-USDT-SWAP", "posSide": "long"},
        ],
        pending_orders=[
            {
                "ordId": "known-stop",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "side": "sell",
                "sz": "3",
                "slTriggerPx": "62000",
            },
            {
                "ordId": "unknown-stop",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "side": "sell",
                "sz": "0",
                "slTriggerPx": "61000",
            },
        ],
        exact_order_position_ids={"known-stop": "a"},
    )

    assert [row.order_id for row in result.by_pos_id["a"]] == ["known-stop"]
    assert result.by_pos_id["b"] == []
    assert [row.order_id for row in result.unattributed] == ["unknown-stop"]
    assert result.unattributed[0].current_position_size_text is None
    assert (
        result.unattributed[0].size_display_text
        == "全部仓位（具体仓位未归属）"
    )


def test_display_rejects_conflicting_or_missing_position_owner():
    result = build_position_tpsl_display(
        positions=[{"posId": "a", "instId": "BTC-USDT-SWAP", "posSide": "long"}],
        pending_orders=[
            {
                "ordId": "stop-1",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "sz": "3",
                "slTriggerPx": "62000",
            }
        ],
        exact_order_position_ids={"stop-1": {"a", "other"}},
    )

    assert result.by_pos_id["a"] == []
    assert [row.order_id for row in result.unattributed] == ["stop-1"]


def test_display_renders_zero_size_as_full_remaining_position_snapshot():
    result = build_position_tpsl_display(
        positions=[
            {
                "posId": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "10",
            }
        ],
        pending_orders=[
            {
                "ordId": "full-stop",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": "0",
                "slTriggerPx": "63895.725",
            }
        ],
        exact_order_position_ids={},
        contract_spec_provider=_btc_specs(),
    )

    row = result.by_pos_id["pos-a"][0]
    assert row.size_mode == "full_position"
    assert row.raw_size_text == "0"
    assert row.current_position_size_text == "10"
    assert (
        row.size_display_text
        == "全部剩余仓位（当前 10 contracts / 0.01 BTC）"
    )


def test_display_renders_partial_size_in_contracts_and_base_asset():
    result = build_position_tpsl_display(
        positions=[
            {
                "posId": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "pos": "10",
            }
        ],
        pending_orders=[
            {
                "ordId": "partial-tp",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": "2",
                "tpTriggerPx": "66330",
            }
        ],
        exact_order_position_ids={},
        contract_spec_provider=_btc_specs(),
    )

    row = result.by_pos_id["pos-a"][0]
    assert row.size_mode == "partial"
    assert row.raw_size_text == "2"
    assert row.current_position_size_text is None
    assert row.size_display_text == "2 contracts / 0.002 BTC"


def test_display_full_position_without_contract_spec_keeps_contract_snapshot():
    result = build_position_tpsl_display(
        positions=[
            {
                "posId": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "pos": "10",
            }
        ],
        pending_orders=[
            {
                "ordId": "full-stop",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": "0",
                "slTriggerPx": "63000",
            }
        ],
        exact_order_position_ids={},
    )

    row = result.by_pos_id["pos-a"][0]
    assert row.size_display_text == "全部剩余仓位（当前 10 contracts）"


def test_display_unattributed_full_position_never_uses_a_position_snapshot():
    result = build_position_tpsl_display(
        positions=[
            {"posId": "a", "instId": "BTC-USDT-SWAP", "pos": "10"},
            {"posId": "b", "instId": "BTC-USDT-SWAP", "pos": "20"},
        ],
        pending_orders=[
            {
                "ordId": "unknown-stop",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "sz": "0",
                "slTriggerPx": "61000",
            }
        ],
        exact_order_position_ids={},
        contract_spec_provider=_btc_specs(),
    )

    assert result.by_pos_id == {"a": [], "b": []}
    assert result.unattributed[0].current_position_size_text is None
    assert (
        result.unattributed[0].size_display_text
        == "全部仓位（具体仓位未归属）"
    )


@pytest.mark.parametrize("raw_size", [None, "", "0", "0.0"])
def test_display_empty_or_numeric_zero_size_is_full_position(raw_size):
    result = build_position_tpsl_display(
        positions=[
            {"posId": "pos-a", "instId": "BTC-USDT-SWAP", "pos": "10"}
        ],
        pending_orders=[
            {
                "ordId": "full-stop",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": raw_size,
                "slTriggerPx": "61000",
            }
        ],
        exact_order_position_ids={},
    )

    assert result.by_pos_id["pos-a"][0].size_mode == "full_position"


def test_display_invalid_size_does_not_break_snapshot():
    result = build_position_tpsl_display(
        positions=[
            {"posId": "pos-a", "instId": "BTC-USDT-SWAP", "pos": "10"}
        ],
        pending_orders=[
            {
                "ordId": "odd-tp",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": "not-a-number",
                "tpTriggerPx": "67000",
            }
        ],
        exact_order_position_ids={},
        contract_spec_provider=_btc_specs(),
    )

    row = result.by_pos_id["pos-a"][0]
    assert row.size_mode == "partial"
    assert row.size_display_text == "not-a-number contracts"


def test_account_ledger_assigns_same_side_full_position_orders_exactly():
    positions = [
        {"posId": "pos-a", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"},
        {"posId": "pos-b", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "5"},
    ]
    pending = [
        {
            "ordId": "sl-a",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPrice": "61000",
        },
        {
            "ordId": "sl-b",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPrice": "60000",
        },
    ]
    ownership = build_account_protection_ownership(
        [
            {
                "venue": "deepcoin",
                "order_id": "sl-a",
                "pos_id": "pos-a",
                "status": "verified",
                "purpose": "stop_loss",
            },
            {
                "venue": "deepcoin",
                "order_id": "sl-b",
                "pos_id": "pos-b",
                "status": "verified",
                "purpose": "stop_loss",
            },
        ],
        live_pos_ids={"pos-a", "pos-b"},
    )

    result = build_position_tpsl_display(
        positions=positions,
        pending_orders=pending,
        account_ownership=ownership,
    )

    assert [row.order_id for row in result.by_pos_id["pos-a"]] == ["sl-a"]
    assert [row.order_id for row in result.by_pos_id["pos-b"]] == ["sl-b"]
    assert result.unattributed == []
