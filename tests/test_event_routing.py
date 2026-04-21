from telegram_kol_research.listener import should_process_event


def test_should_process_event_rejects_untracked_chat():
    event = {"chat_id": 42}
    tracked_chat_ids = {1001}
    assert should_process_event(event, tracked_chat_ids) is False
