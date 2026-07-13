import json
from datetime import datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    claim_critical_notification,
    claim_next_semantic_review,
    complete_semantic_review,
    fail_semantic_review,
    save_pending_authoritative_decision,
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
    return claimed


def test_new_authoritative_decision_is_pending_without_auxiliary_payload(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)

    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    assert saved.comparison_status == "pending"
    assert saved.auxiliary_model is None
    assert saved.auxiliary_payload_json is None
    assert saved.comparison_attempts == 0


def test_comparison_completion_preserves_automation_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert _claim(session_factory) == raw_id
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
    )

    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
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
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    now = datetime(2026, 7, 13, 12, 0)

    assert claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    ) == raw_id
    assert claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    ) is None
    assert claim_next_semantic_review(
        session_factory,
        now=now + timedelta(minutes=10),
        stale_before=now + timedelta(minutes=5),
    ) == raw_id


def test_failure_tracks_attempt_and_only_requeues_with_retry_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    retry_id = _raw_message(session_factory, message_id=2)
    terminal_id = _raw_message(session_factory, message_id=3)
    save_pending_authoritative_decision(session_factory, _record(retry_id))
    save_pending_authoritative_decision(session_factory, _record(terminal_id))
    retry_at = datetime(2026, 7, 13, 12, 30)

    assert _claim(session_factory) == retry_id
    fail_semantic_review(
        session_factory,
        raw_message_id=retry_id,
        error="timeout",
        next_attempt_at=retry_at,
    )
    assert _claim(session_factory) == terminal_id
    fail_semantic_review(
        session_factory,
        raw_message_id=terminal_id,
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
    save_pending_authoritative_decision(session_factory, original)
    assert _claim(session_factory) == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
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
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert _claim(session_factory) == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
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


def test_changed_authoritative_payload_resets_comparison_not_execution_or_notification(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert _claim(session_factory) == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
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
        assert row.comparison_status == "pending"
        assert row.comparison_payload_json is None
        assert row.auxiliary_payload_json is None
        assert row.automation_status == "submitted"
        assert row.notification_fingerprint == "old-review"
        assert row.notification_status == "scheduled"


def test_stale_completion_cannot_overwrite_changed_authoritative_payload(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    save_pending_authoritative_decision(session_factory, _record(raw_id))
    now = datetime(2026, 7, 13, 12, 0)
    assert claim_next_semantic_review(
        session_factory, now=now, stale_before=now - timedelta(minutes=5)
    ) == raw_id
    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
    )

    with pytest.raises(RuntimeError, match="not claimed"):
        complete_semantic_review(
            session_factory,
            raw_message_id=raw_id,
            model="deepseek",
            auxiliary_payload={"lifecycle_event": {"event_type": "none"}},
            comparison_payload={"stale": True},
            agreement_status="disagreed",
            severity="critical",
            differences=["status"],
            prompt_versions={"deepseek": 4},
            compared_at=now + timedelta(minutes=1),
        )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "pending"
        assert row.comparison_payload_json is None


def test_prompt_versions_merge_across_authoritative_resave_and_comparison(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    record = _record(raw_id)
    save_pending_authoritative_decision(session_factory, record)
    updated = RecognitionDecisionRecord(
        **{
            **record.__dict__,
            "prompt_versions": {"mimo": {"trading.analysis.shared": 5}},
        }
    )
    save_pending_authoritative_decision(session_factory, updated)
    assert _claim(session_factory) == raw_id
    complete_semantic_review(
        session_factory,
        raw_message_id=raw_id,
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
