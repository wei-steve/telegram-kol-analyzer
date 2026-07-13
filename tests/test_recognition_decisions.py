import json

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_recognition_decision,
    update_recognition_execution_outcome,
)


def test_recognition_decision_upserts_and_tracks_outcomes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    saved = save_recognition_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload={"lifecycle_event": {"event_type": "exit_position"}},
            auxiliary_model="deepseek-v4-flash",
            auxiliary_status="非策略",
            auxiliary_payload={"lifecycle_event": {"event_type": "none"}},
            agreement_status="disagreed",
            differences=["lifecycle_event.event_type"],
            prompt_versions={"mimo": {"trading.analysis.shared": 3}},
        ),
    )
    assert saved.raw_message_id == raw_id

    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
        notification_status="scheduled",
    )

    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.authoritative_model == "mimo-v2.5"
        assert json.loads(row.differences_json) == ["lifecycle_event.event_type"]
        assert json.loads(row.prompt_versions_json) == {
            "mimo": {"trading.analysis.shared": 3}
        }
        assert row.automation_status == "submitted"
        assert row.automation_reason == "close_position"
        assert row.notification_status == "scheduled"

    save_recognition_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload={"lifecycle_event": {"event_type": "exit_position"}},
            auxiliary_model="deepseek-v4-flash",
            auxiliary_status="非策略",
            auxiliary_payload={"lifecycle_event": {"event_type": "exit_position"}},
            agreement_status="agreed",
            differences=[],
        ),
    )
    with session_factory() as session:
        assert session.query(RecognitionDecision).count() == 1
        row = session.query(RecognitionDecision).one()
        assert row.agreement_status == "agreed"
        assert row.automation_status is None
        assert row.notification_status is None
        assert row.notification_error is None

    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
        notification_status="failed",
        notification_error="timeout",
    )
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="submitted",
        automation_reason="close_position",
        notification_status="sent",
    )
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_status == "sent"
        assert row.notification_error is None
