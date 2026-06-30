import pytest

from telegram_kol_research.deepcoin_order_matching import DeepcoinOrderMatchError
from telegram_kol_research.deepcoin_order_matching import extract_pending_protection_orders
from telegram_kol_research.deepcoin_order_matching import pending_tpsl_order_ids_for_position
from telegram_kol_research.deepcoin_order_matching import resolve_stop_loss_adjustment_target
from telegram_kol_research.deepcoin_order_matching import select_position_tpsl_orders
from telegram_kol_research.deepcoin_readonly import DeepcoinOrderBinding


def _binding(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "source_message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "pos_id": "pos-1",
        "order_id": "entry-1",
        "client_order_id": "client-entry-1",
    }
    values.update(overrides)
    return DeepcoinOrderBinding(**values)


def test_extract_pending_protection_orders_splits_tpsl_payload_into_price_legs():
    orders = extract_pending_protection_orders(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "tpsl-1",
                "posId": "pos-1",
                "slTriggerPrice": "67500",
                "tpTriggerPrice": "69000",
                "sz": "5",
                "cTime": "1782801429000",
            }
        ]
    )

    assert [(order.purpose, order.trigger_price) for order in orders] == [
        ("stop_loss", 67500.0),
        ("take_profit", 69000.0),
    ]
    assert orders[0].trigger_order_id == "tpsl-1"
    assert orders[0].pos_id == "pos-1"


def test_resolve_stop_loss_adjustment_target_prefers_matching_position_id():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id="pos-1"),
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "stop-1",
                "posId": "pos-1",
                "slTriggerPrice": "67500",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "stop-2",
                "posId": "pos-2",
                "slTriggerPrice": "67400",
            },
        ],
    )

    assert target.action == "replace_pending_stop_loss"
    assert target.reason == "matched_pending_stop_loss_by_pos_id"
    assert target.order is not None
    assert target.order.trigger_order_id == "stop-1"


def test_resolve_stop_loss_adjustment_target_matches_known_client_order_id():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id=None, client_order_id="sl-client-1"),
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "clOrdId": "sl-client-1",
                "slTriggerPx": "67500",
            }
        ],
    )

    assert target.action == "replace_pending_stop_loss"
    assert target.reason == "matched_pending_stop_loss_by_order_id"
    assert target.order is not None
    assert target.order.client_order_id == "sl-client-1"


def test_resolve_stop_loss_adjustment_target_rejects_ambiguous_symbol_side_matches():
    with pytest.raises(DeepcoinOrderMatchError) as exc:
        resolve_stop_loss_adjustment_target(
            binding=_binding(pos_id=None, order_id=None, client_order_id=None),
            pending_trigger_orders=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "orderId": "stop-1",
                    "slTriggerPrice": "67500",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "orderId": "stop-2",
                    "slTriggerPrice": "67400",
                },
            ],
        )

    assert str(exc.value) == "ambiguous_stop_loss_orders_for_symbol_side"


def test_resolve_stop_loss_adjustment_target_falls_back_to_active_position_sltp():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id="pos-1"),
        pending_trigger_orders=[],
        live_positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "pos": "3",
            }
        ],
    )

    assert target.action == "set_position_sltp"
    assert target.reason == "no_pending_stop_loss_but_active_position_found"
    assert target.pos_id == "pos-1"


def test_resolve_stop_loss_adjustment_target_falls_back_to_open_entry_order_sltp():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id=None, order_id="entry-1,entry-2"),
        pending_trigger_orders=[],
        live_positions=[],
    )

    assert target.action == "replace_order_sltp"
    assert target.reason == "no_position_yet_replace_open_entry_order_sltp"
    assert target.order_id == "entry-1"


def test_select_position_tpsl_orders_matches_deepcoin_zero_size_rows_by_position_time():
    position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-eth",
        "posSide": "long",
        "pos": "5.2",
        "cTime": "1782788831000",
    }
    orders = [
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782788831000",
            "triggerOrderType": "TPSL",
            "ordId": "tp-1",
            "tpTriggerPrice": "1680",
        },
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782788831000",
            "triggerOrderType": "TPSL",
            "ordId": "sl-1",
            "slTriggerPrice": "1555",
        },
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782800000000",
            "triggerOrderType": "TPSL",
            "ordId": "other-position",
            "slTriggerPrice": "1500",
        },
    ]

    matched = select_position_tpsl_orders(position=position, pending_trigger_orders=orders)

    assert [order["ordId"] for order in matched] == ["tp-1", "sl-1"]
    assert pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=orders,
    ) == ["tp-1", "sl-1"]


def test_select_position_tpsl_orders_deduplicates_combined_tpsl_order_id():
    position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-eth",
        "posSide": "long",
        "pos": "1",
        "cTime": "1782801429000",
    }
    orders = [
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "posId": "pos-eth",
            "sz": "1",
            "cTime": "1782801429000",
            "triggerOrderType": "TPSL",
            "ordId": "combined-1",
            "tpTriggerPrice": "1600",
            "slTriggerPrice": "1560",
        },
    ]

    assert pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=orders,
    ) == ["combined-1"]
