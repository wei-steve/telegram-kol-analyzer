from telegram_kol_research.protection_snapshot import observe_pending_tpsl


def test_observation_marks_unknown_pagination_as_incomplete():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}], "nextCursor": "abc"},
    )

    assert observation["complete"] is False
    assert observation["order_ids"] == ["tp-1"]
    assert observation["reason"] == "pagination_metadata_unsupported"


def test_observation_proves_expected_exact_order_ids_visible():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}, {"ordId": "sl-1"}]},
        expected_order_ids={"tp-1", "sl-1"},
    )

    assert observation["complete"] is True
    assert observation["expected_order_ids_visible"] is True
