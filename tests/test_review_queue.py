from telegram_kol_research.review_queue import list_pending_candidates


def test_list_pending_candidates_returns_only_pending_records():
    candidates = [
        {"id": 1, "review_status": "pending"},
        {"id": 2, "review_status": "confirmed"},
    ]
    pending = list_pending_candidates(candidates)
    assert pending == [{"id": 1, "review_status": "pending"}]
