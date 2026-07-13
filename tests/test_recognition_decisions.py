import json
from datetime import datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    SemanticReviewClaim,
    claim_authoritative_execution,
    claim_critical_notification,
    claim_next_semantic_review,
    complete_semantic_review,
    fail_semantic_review,
    finalize_authoritative_automation_outcome,
    save_pending_authoritative_decision,
    save_terminal_authoritative_decision,
    update_recognition_execution_outcome,
)


def _raw_message(session_factory, message_id=2):
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=message_id, text="BTC short")
        session.add(raw)
        session.commit()
        return raw.id


def _record(raw_message_id, payload=None):
    return RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="非策略",
        authoritative_payload=payload
        or {"lifecycle_event": {"event_type": "exit_position"}},
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="pending",
        differences=[],
        prompt_versions={"mimo": {"trading.analysis.shared": 3}},
    )


def _claim(session_factory):
    now = datetime(2026, 7, 13, 12, 0)
    claimed = claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    )
    assert claimed is not None
    assert isinstance(claimed, SemanticReviewClaim)
    return claimed


def _save_and_finalize(session_factory, record):
    saved = save_pending_authoritative_decision(session_factory, record)
    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=record.raw_message_id,
        authoritative_generation=saved.comparison_claim_token,
    )
    return finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=record.raw_message_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="skipped",
        automation_reason="test_setup",
    )


def test_new_authoritative_decision_is_unclaimable_until_automation_finalizes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)

    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    assert saved.comparison_status == "execution_pending"
    assert saved.comparison_claim_token
    assert saved.auxiliary_model is None
    assert saved.auxiliary_payload_json is None
    assert saved.comparison_attempts == 0
    now = datetime(2026, 7, 13, 12, 0)
    assert claim_next_semantic_review(
        session_factory,
        now=now,
        stale_before=now - timedelta(minutes=5),
    ) is None

    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_running"
    assert claim_next_semantic_review(
        session_factory,
        now=now,
        stale_before=now - timedelta(minutes=5),
    ) is None

    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="submitted",
        automation_reason="close_position",
    )

    assert finalized.comparison_status == "pending"
    assert finalized.automation_status == "submitted"
    claim = claim_next_semantic_review(
        session_factory,
        now=now,
        stale_before=now - timedelta(minutes=5),
    )
    assert claim is not None
    assert claim.raw_message_id == raw_id


def test_stale_automation_generation_cannot_publish_new_rerecognition(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    first = save_pending_authoritative_decision(session_factory, _record(raw_id))
    second = save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
    )

    with pytest.raises(RuntimeError, match="stale"):
        finalize_authoritative_automation_outcome(
            session_factory,
            raw_message_id=raw_id,
            authoritative_generation=first.comparison_claim_token,
            automation_status="submitted",
            automation_reason="stale_close",
        )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_pending"
        assert row.automation_status is None

    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=second.comparison_claim_token,
    )
    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=second.comparison_claim_token,
        automation_status="skipped",
        automation_reason="new_generation",
    )
    assert finalized.comparison_status == "pending"
    assert finalized.automation_reason == "new_generation"


def test_terminal_authoritative_failure_preserves_notification_metadata(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.notification_fingerprint = "existing-alert"
        row.notification_status = "sent"
        session.commit()

    failed = save_terminal_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="识别失败",
            authoritative_payload={},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[],
            prompt_versions={"mimo": {"trading.analysis.shared": 12}},
        ),
    )

    assert failed.comparison_status == "completed"
    assert failed.comparison_claim_token is None
    assert failed.agreement_status == "authoritative_failed"
    assert failed.notification_fingerprint == "existing-alert"
    assert failed.notification_status == "sent"


def test_comparison_completion_preserves_automation_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    claim = _claim(session_factory)
    assert claim.raw_message_id == raw_id
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
    )

    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=claim.token,
        model="deepseek-v4-flash",
        auxiliary_payload={"lifecycle_event": {"event_type": "none"}},
        comparison_payload={"material": True},
        agreement_status="disagreed",
        severity="critical",
        differences=["lifecycle_event.event_type"],
        prompt_versions={"deepseek": 4},
        compared_at=datetime(2026, 7, 13, 12, 0),
    )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "completed"
        assert row.automation_status == "submitted"
        assert row.automation_reason == "close_position"
        assert json.loads(row.comparison_payload_json) == {"material": True}


def test_only_one_worker_claims_pending_review_and_stale_work_is_reclaimable(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    now = datetime(2026, 7, 13, 12, 0)

    first = claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    )
    assert first is not None
    assert first.raw_message_id == raw_id
    assert claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    ) is None
    reclaimed = claim_next_semantic_review(
        session_factory,
        now=now + timedelta(minutes=10),
        stale_before=now + timedelta(minutes=5),
    )
    assert reclaimed is not None
    assert reclaimed.raw_message_id == raw_id
    assert reclaimed.token != first.token


def test_failure_tracks_attempt_and_only_requeues_with_retry_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    retry_id = _raw_message(session_factory, message_id=2)
    terminal_id = _raw_message(session_factory, message_id=3)
    _save_and_finalize(session_factory, _record(retry_id))
    _save_and_finalize(session_factory, _record(terminal_id))
    retry_at = datetime(2026, 7, 13, 12, 30)

    retry_claim = _claim(session_factory)
    assert retry_claim.raw_message_id == retry_id
    fail_semantic_review(
        session_factory,
        raw_message_id=retry_id,
        claim_token=retry_claim.token,
        error="timeout",
        next_attempt_at=retry_at,
    )
    terminal_claim = _claim(session_factory)
    assert terminal_claim.raw_message_id == terminal_id
    fail_semantic_review(
        session_factory,
        raw_message_id=terminal_id,
        claim_token=terminal_claim.token,
        error="invalid payload",
        next_attempt_at=None,
    )

    with session_factory() as session:
        retry = session.query(RecognitionDecision).filter_by(raw_message_id=retry_id).one()
        terminal = session.query(RecognitionDecision).filter_by(raw_message_id=terminal_id).one()
        assert (retry.comparison_status, retry.comparison_attempts) == ("pending", 1)
        assert retry.comparison_next_attempt_at == retry_at
        assert (terminal.comparison_status, terminal.comparison_attempts) == ("failed", 1)


def test_notification_claim_is_once_per_raw_message_and_survives_resaves(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    original = _record(raw_id)
    _save_and_finalize(session_factory, original)
    claim = _claim(session_factory)
    assert claim.raw_message_id == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=claim.token,
        model="deepseek",
        auxiliary_payload={},
        comparison_payload={},
        agreement_status="disagreed",
        severity="critical",
        differences=["status"],
        prompt_versions={},
        compared_at=datetime(2026, 7, 13, 12, 0),
    )

    assert claim_critical_notification(
        session_factory, raw_message_id=raw_id, fingerprint="first"
    ) is True
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
        notification_status="sent",
    )
    assert claim_critical_notification(
        session_factory, raw_message_id=raw_id, fingerprint="first"
    ) is False
    save_pending_authoritative_decision(session_factory, original)
    assert claim_critical_notification(
        session_factory, raw_message_id=raw_id, fingerprint="second"
    ) is False

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_fingerprint == "first"
        assert row.notification_status == "sent"


def test_failed_notification_delivery_keeps_original_claim_reserved(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    claim = _claim(session_factory)
    assert claim.raw_message_id == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=claim.token,
        model="deepseek",
        auxiliary_payload={},
        comparison_payload={},
        agreement_status="disagreed",
        severity="critical",
        differences=["status"],
        prompt_versions={},
        compared_at=datetime(2026, 7, 13, 12, 0),
    )
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.notification_status = "failed"
        session.commit()

    assert claim_critical_notification(
        session_factory, raw_message_id=raw_id, fingerprint="replacement"
    ) is False


def test_changed_authoritative_payload_resets_comparison_and_execution_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    claim = _claim(session_factory)
    assert claim.raw_message_id == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=claim.token,
        model="deepseek",
        auxiliary_payload={},
        comparison_payload={"old": True},
        agreement_status="disagreed",
        severity="critical",
        differences=["status"],
        prompt_versions={},
        compared_at=datetime(2026, 7, 13, 12, 0),
    )
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
    )
    assert claim_critical_notification(
        session_factory, raw_message_id=raw_id, fingerprint="old-review"
    )

    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
    )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_pending"
        assert row.comparison_payload_json is None
        assert row.auxiliary_payload_json is None
        assert row.automation_status is None
        assert row.automation_reason is None
        assert row.notification_fingerprint == "old-review"
        assert row.notification_status == "scheduled"


def test_stale_completion_cannot_overwrite_changed_authoritative_payload(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    now = datetime(2026, 7, 13, 12, 0)
    stale_claim = claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    )
    assert stale_claim is not None
    assert stale_claim.raw_message_id == raw_id
    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
    )

    assert complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=stale_claim.token,
        model="deepseek",
        auxiliary_payload={"lifecycle_event": {"event_type": "none"}},
        comparison_payload={"stale": True},
        agreement_status="disagreed",
        severity="critical",
        differences=["status"],
        prompt_versions={"deepseek": 4},
        compared_at=now + timedelta(minutes=1),
    ) is False

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_pending"
        assert row.comparison_payload_json is None


def test_prompt_versions_merge_across_authoritative_resave_and_comparison(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    record = _record(raw_id)
    _save_and_finalize(session_factory, record)
    updated = RecognitionDecisionRecord(
        **{
            **record.__dict__,
            "prompt_versions": {"mimo": {"trading.analysis.shared": 5}},
        }
    )
    _save_and_finalize(session_factory, updated)
    claim = _claim(session_factory)
    assert claim.raw_message_id == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=claim.token,
        model="deepseek",
        auxiliary_payload={},
        comparison_payload={},
        agreement_status="agreed",
        severity="none",
        differences=[],
        prompt_versions={"deepseek": 4},
        compared_at=datetime(2026, 7, 13, 12, 0),
    )

    with session_factory() as session:
        assert json.loads(session.query(RecognitionDecision).one().prompt_versions_json) == {
            "deepseek": 4,
            "mimo": {"trading.analysis.shared": 5},
        }


def test_stale_worker_cannot_complete_or_fail_after_new_worker_reclaims(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    started_at = datetime(2026, 7, 13, 12, 0)
    worker_a = claim_next_semantic_review(
        session_factory,
        now=started_at,
        stale_before=started_at - timedelta(minutes=5),
    )
    worker_b = claim_next_semantic_review(
        session_factory,
        now=started_at + timedelta(minutes=10),
        stale_before=started_at + timedelta(minutes=5),
    )
    assert worker_a is not None
    assert worker_b is not None
    assert worker_a.token != worker_b.token

    assert complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=worker_a.token,
        model="deepseek-a",
        auxiliary_payload={"worker": "a"},
        comparison_payload={"worker": "a"},
        agreement_status="disagreed",
        severity="critical",
        differences=["stale"],
        prompt_versions={"deepseek": 1},
        compared_at=started_at + timedelta(minutes=11),
    ) is False
    assert fail_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=worker_a.token,
        error="stale worker failed",
        next_attempt_at=None,
    ) is False

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "running"
        assert row.comparison_claim_token == worker_b.token
        assert row.comparison_error is None

    assert complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
        claim_token=worker_b.token,
        model="deepseek-b",
        auxiliary_payload={"worker": "b"},
        comparison_payload={"worker": "b"},
        agreement_status="agreed",
        severity="none",
        differences=[],
        prompt_versions={"deepseek": 2},
        compared_at=started_at + timedelta(minutes=12),
    ) is True

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "completed"
        assert row.comparison_claim_token is None
        assert json.loads(row.comparison_payload_json) == {"worker": "b"}
