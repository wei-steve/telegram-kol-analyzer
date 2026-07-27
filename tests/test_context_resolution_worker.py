from datetime import UTC, datetime, timedelta
import json

import pytest

from telegram_kol_research.context_resolution_worker import (
    claim_next_context_reanalysis,
    run_context_resolution_once,
    schedule_context_reanalysis,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)


def _persist_unresolved(
    session_factory,
    *,
    chat_id=100,
    message_id=10,
    fingerprint="sha256:old",
    triggers=(
        "reply_target_available",
        "exchange_state_changed",
        "strategy_state_changed",
        "message_edited",
        "evidence_version_changed",
    ),
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="更新策略",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint=fingerprint,
            model="deepseek",
            prompt_versions_json="{}",
            request_summary_json="{}",
            decision_json=json.dumps(
                {
                    "decision": "unresolved",
                    "target_thread_ids": [],
                    "management_action": None,
                    "confidence": 0.4,
                    "supporting_message_ids": [message_id],
                    "opposing_message_ids": [],
                    "conflict_types": ["target_ambiguous"],
                    "risk_reducing_fanout_allowed": False,
                    "reanalysis_triggers": list(triggers),
                    "reason": "等待更多上下文",
                }
            ),
            status="completed",
            reanalysis_triggers_json=json.dumps(list(triggers)),
            attempts=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(attempt)
        session.commit()
        return raw.id, attempt.id


@pytest.mark.parametrize(
    ("event_type", "expected_trigger"),
    [
        ("reply_target_available", "reply_target_available"),
        ("entry_leg_status_changed", "strategy_state_changed"),
        ("exchange_snapshot_changed", "exchange_state_changed"),
        ("message_edited", "message_edited"),
        ("evidence_version_changed", "evidence_version_changed"),
    ],
)
def test_reanalysis_is_scheduled_for_supported_context_changes(
    tmp_path,
    event_type,
    expected_trigger,
):
    session_factory = create_session_factory(tmp_path / f"{event_type}.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)

    scheduled = schedule_context_reanalysis(
        session_factory,
        event_type=event_type,
        raw_message_id=raw_id,
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert scheduled == 1
    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        assert attempt.status == "pending_reanalysis"
        assert json.loads(attempt.trigger_event_json)["trigger"] == expected_trigger


def test_next_same_chat_message_schedules_unresolved_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "same-chat.db")
    _, attempt_id = _persist_unresolved(session_factory, chat_id=88)

    scheduled = schedule_context_reanalysis(
        session_factory,
        event_type="next_same_chat_message",
        chat_id=88,
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert scheduled == 1
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "pending_reanalysis"
        )


def test_concurrent_workers_claim_one_generation_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "claim.db")
    raw_id, _ = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    first = claim_next_context_reanalysis(
        session_factory,
        now=NOW,
        stale_before=NOW - timedelta(minutes=5),
    )
    second = claim_next_context_reanalysis(
        session_factory,
        now=NOW,
        stale_before=NOW - timedelta(minutes=5),
    )

    assert first is not None
    assert second is None
    assert first.raw_message_id == raw_id


def test_unchanged_fingerprint_does_not_call_ai(tmp_path):
    session_factory = create_session_factory(tmp_path / "same-fingerprint.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:old",
        reanalyze=lambda *_: (_ for _ in ()).throw(
            AssertionError("unchanged context must not call AI")
        ),
        now=NOW,
    )

    assert result["status"] == "context_unchanged"
    with session_factory() as session:
        assert session.get(ContextResolutionAttempt, attempt_id).status == "completed"


def test_disabled_or_removed_chat_is_never_reanalyzed(tmp_path):
    session_factory = create_session_factory(tmp_path / "disabled.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    calls = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: calls.append("resolver") or {"status": "completed"},
        now=NOW,
        is_eligible=lambda _: False,
    )

    assert result["status"] == "blocked_disabled"
    assert calls == []
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "blocked_disabled"
        )


def test_new_fingerprint_runs_once_and_supersedes_old_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "new-fingerprint.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="exchange_snapshot_changed",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    calls = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda message_id, fingerprint: calls.append(
            (message_id, fingerprint)
        )
        or {"status": "completed"},
        now=NOW,
    )

    assert result["status"] == "completed"
    assert calls == [(raw_id, "sha256:new")]
    with session_factory() as session:
        assert session.get(ContextResolutionAttempt, attempt_id).status == "superseded"


@pytest.mark.parametrize(
    "terminal_status",
    ["submitted", "submit_unknown", "succeeded", "unknown", "reconciled"],
)
def test_terminal_instruction_is_never_replayed(tmp_path, terminal_status):
    session_factory = create_session_factory(tmp_path / f"{terminal_status}.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=raw_id,
            event_type="position_update",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw_id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                idempotency_key=f"terminal-{terminal_status}",
                status=terminal_status,
            )
        )
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: (_ for _ in ()).throw(
            AssertionError("terminal instruction must not replay")
        ),
        now=NOW,
    )

    assert result["status"] == "blocked_execution_terminal"
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "blocked_execution_terminal"
        )


def test_final_failure_notifies_once_after_bounded_attempts(tmp_path):
    session_factory = create_session_factory(tmp_path / "failure.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        attempt.attempts = 3
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    notices = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")),
        notify_final_failure=lambda payload: notices.append(payload),
        max_attempts=3,
        now=NOW,
    )

    assert result["status"] == "exhausted"
    assert len(notices) == 1
