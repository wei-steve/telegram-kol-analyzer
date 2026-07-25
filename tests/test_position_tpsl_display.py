from telegram_kol_research.position_tpsl_display import build_position_tpsl_display


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
