import json
from datetime import datetime

import pytest

import telegram_kol_research.recognition_decisions as decision_module

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    claim_authoritative_execution,
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


def test_a_new_authoritative_decision_holds_its_execution_lease_until_finalized(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)

    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))

    assert saved.comparison_status == "execution_pending"
    assert saved.comparison_claim_token
    assert saved.auxiliary_model is None
    assert saved.auxiliary_payload_json is None
    assert saved.comparison_attempts == 0

    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_running"
        assert row.comparison_claim_token == saved.comparison_claim_token

    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="submitted",
        automation_reason="close_position",
    )

    assert finalized.comparison_status == "completed"
    assert finalized.comparison_claim_token is None
    assert finalized.automation_status == "submitted"


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
    assert finalized.comparison_status == "completed"
    assert finalized.automation_reason == "new_generation"


@pytest.mark.parametrize(
    "protected_status", ["execution_running", "execution_uncertain"]
)
def test_active_or_uncertain_execution_rejects_pending_authoritative_overwrite(
    tmp_path,
    protected_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.comparison_status = protected_status
        session.commit()

    with pytest.raises(RuntimeError, match="already in progress|outcome is uncertain"):
        save_pending_authoritative_decision(
            session_factory,
            _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
        )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == protected_status
        assert row.comparison_claim_token == saved.comparison_claim_token


@pytest.mark.parametrize(
    "protected_status", ["execution_running", "execution_uncertain"]
)
def test_active_or_uncertain_execution_rejects_terminal_authoritative_overwrite(
    tmp_path,
    protected_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.comparison_status = protected_status
        session.commit()

    terminal = _record(
        raw_id,
        {"lifecycle_event": {"event_type": "position_update"}},
    )
    terminal = RecognitionDecisionRecord(
        **{
            **terminal.__dict__,
            "agreement_status": "authoritative_failed",
        }
    )
    with pytest.raises(RuntimeError, match="already in progress|outcome is uncertain"):
        save_terminal_authoritative_decision(session_factory, terminal)

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == protected_status
        assert row.comparison_claim_token == saved.comparison_claim_token


def test_finalizing_releases_the_execution_lease_into_the_terminal_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    saved = save_pending_authoritative_decision(session_factory, _record(raw_id))
    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
    )

    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=saved.comparison_claim_token,
        automation_status="submitted",
        automation_reason="close_position",
    )

    assert finalized.comparison_status == "completed"
    assert finalized.agreement_status == "review_disabled"
    assert finalized.automation_status == "submitted"
    assert finalized.automation_reason == "close_position"
    assert finalized.comparison_claim_token is None
    assert finalized.comparison_started_at is None
    assert finalized.comparison_next_attempt_at is None


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


def test_authoritative_failure_notification_claim_is_once_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "notification-claim.db")
    raw_id = _raw_message(session_factory)
    save_terminal_authoritative_decision(
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
            prompt_versions={"mimo": {}},
        ),
    )

    first = decision_module.claim_authoritative_failure_notification(
        session_factory,
        raw_message_id=raw_id,
        automation_status="skipped",
        automation_reason="mimo_authoritative_failed",
    )
    second = decision_module.claim_authoritative_failure_notification(
        session_factory,
        raw_message_id=raw_id,
        automation_status="skipped",
        automation_reason="mimo_authoritative_failed",
    )

    assert first is True
    assert second is False
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
    assert row.notification_status == "scheduled"
    assert row.notification_error is None
    assert row.automation_status == "skipped"
    assert row.automation_reason == "mimo_authoritative_failed"


def test_changed_authoritative_payload_resets_comparison_and_execution_outcome(tmp_path):
    """A re-analysis clears the retired review columns and keeps the notification.

    The columns stay on the table after the 2026-09-25 retirement, and so do
    these resets: production rows written before the retirement still carry
    review content, and a re-analysis has always wiped it. Nothing writes
    those columns any more, so the fixture fills them directly.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    _save_and_finalize(session_factory, _record(raw_id))
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.auxiliary_model = "some-reviewer"
        row.auxiliary_payload_json = json.dumps({"old": True})
        row.comparison_model = "some-reviewer"
        row.comparison_payload_json = json.dumps({"old": True})
        row.comparison_error = "boom"
        row.comparison_attempts = 3
        row.disagreement_severity = "critical"
        row.compared_at = datetime(2026, 7, 13, 12, 0)
        row.notification_fingerprint = "f" * 64
        row.notification_payload_json = json.dumps({"frozen": True})
        row.notification_status = "scheduled"
        session.commit()
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
    )

    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"lifecycle_event": {"event_type": "position_update"}}),
    )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "execution_pending"
        assert row.comparison_payload_json is None
        assert row.comparison_model is None
        assert row.comparison_error is None
        assert row.comparison_attempts == 0
        assert row.disagreement_severity is None
        assert row.compared_at is None
        assert row.auxiliary_payload_json is None
        assert row.automation_status is None
        assert row.automation_reason is None
        assert row.notification_fingerprint == "f" * 64
        assert row.notification_payload_json == json.dumps({"frozen": True})
        assert row.notification_status == "scheduled"


def test_prompt_versions_merge_across_an_unchanged_authoritative_resave(tmp_path):
    """``preserve_completed_review`` still merges prompt versions on a resave.

    The branch is named after the retired review, but what it decides is how a
    re-analysis of an *unchanged* payload treats the row it finds: the stored
    prompt versions are merged rather than replaced. That is re-analysis
    behaviour, not review behaviour, so the retirement leaves it alone.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id = _raw_message(session_factory)
    record = _record(raw_id)
    _save_and_finalize(session_factory, record)
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        row.prompt_versions_json = json.dumps({"context": 4, "mimo": {"x": 1}})
        session.commit()
    updated = RecognitionDecisionRecord(
        **{
            **record.__dict__,
            "prompt_versions": {"mimo": {"trading.analysis.shared": 5}},
        }
    )
    save_pending_authoritative_decision(session_factory, updated)

    with session_factory() as session:
        assert json.loads(session.query(RecognitionDecision).one().prompt_versions_json) == {
            "context": 4,
            "mimo": {"trading.analysis.shared": 5},
        }

