from telegram_kol_research.candidates import classify_candidate


def test_classify_candidate_marks_low_confidence_as_pending_review():
    result = classify_candidate(confidence=0.42)
    assert result.review_status == "pending"
