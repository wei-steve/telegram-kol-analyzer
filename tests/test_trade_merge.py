from telegram_kol_research.trade_merge import merge_candidate_batch


def test_merge_candidate_batch_uses_reply_chain_first():
    candidates = [
        {"message_id": 100, "symbol": "BTC", "side": "long", "event_type": "entry_signal"},
        {"message_id": 101, "reply_to_message_id": 100, "symbol": "BTC", "side": "long", "event_type": "stop_loss_update"},
    ]
    trades = merge_candidate_batch(candidates)
    assert len(trades) == 1
    assert trades[0]["events"][1]["event_type"] == "stop_loss_update"
