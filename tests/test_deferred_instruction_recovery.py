"""A-3: a deferred instruction is resumed when its exit ends, or expires loudly.

The 2026-09-07 incident: ``source_execution_barrier`` answered ``hold``, the
message was recorded ``deferred / waiting_source_deletion_exit``, the deletion
exit later finished, and nothing ever came back for the message. It already had
a decision row, so the authoritative gap recovery did not see it as missing
either. 29 instruction items sat ``pending`` with ``last_progress_at`` null,
the oldest from 2026-07-22, including an auto-trade entry (raw 15169) whose
lifecycle showed ``entered`` with no binding.
"""

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deferred_instruction_recovery import (
    DEFERRED_EXPIRED_REASON,
    DEFERRED_HOLD_REASON,
    DEFERRED_RESUME_JOB_REASON,
    expire_stale_deferred_instructions,
    resume_instructions_deferred_by_exit,
)
from telegram_kol_research.models import (
    MessageInstructionItem,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
    source_execution_barrier,
)


NOW = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def _decision(raw_message_id: int, *, reason: str, updated_at: datetime):
    return RecognitionDecision(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="mimo",
        authoritative_status="是策略",
        authoritative_payload_json="{}",
        agreement_status="agreed",
        automation_status="deferred",
        automation_reason=reason,
        updated_at=updated_at.replace(tzinfo=None),
        created_at=updated_at.replace(tzinfo=None),
    )


def _held_message_fixture(tmp_path, *, decided_at=NOW):
    """One deleted source, one repost the barrier holds behind its exit."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old = RawMessage(
            chat_id=10,
            message_id=20,
            text="BTC long old",
            archived_target_group=True,
        )
        repost = RawMessage(
            chat_id=10,
            message_id=21,
            text="BTC long repost",
            archived_target_group=True,
        )
        session.add_all([old, repost])
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=old.id,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                ),
                SignalCandidate(
                    raw_message_id=repost.id,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                ),
            ]
        )
        session.flush()
        candidate_id = (
            session.query(SignalCandidate.id)
            .filter(SignalCandidate.raw_message_id == repost.id)
            .scalar()
        )
        session.add_all(
            [
                _decision(
                    repost.id,
                    reason=DEFERRED_HOLD_REASON,
                    updated_at=decided_at,
                ),
                MessageInstructionItem(
                    raw_message_id=repost.id,
                    signal_candidate_id=candidate_id,
                    sequence=0,
                    instruction_kind="entry",
                    idempotency_key=f"item-{repost.id}",
                    status="pending",
                ),
                # The settled job row the incident actually had: without the
                # resume widening, the idempotent upsert is a no-op on this.
                MessageProcessingJob(
                    raw_message_id=repost.id,
                    chat_id=10,
                    status="succeeded",
                    attempt_count=1,
                    last_reason="worker_completed",
                    enqueued_at=decided_at.replace(tzinfo=None),
                    shadow=False,
                ),
            ]
        )
        session.commit()
        repost_id = int(repost.id)
    record_source_message_deleted(session_factory, chat_id=10, message_id=20)
    assert (
        source_execution_barrier(
            session_factory, raw_message_id=repost_id
        ).status
        == "hold"
    )
    with session_factory() as session:
        exit_id = int(session.query(SourceMessageDeletionExit.id).scalar())
    return session_factory, repost_id, exit_id


def _finish_exit(session_factory, exit_id: int, state: str) -> None:
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, exit_id)
        deletion_exit.state = state
        session.commit()


def test_a_finished_exit_re_enqueues_the_message_it_was_holding(tmp_path):
    session_factory, repost_id, exit_id = _held_message_fixture(tmp_path)
    _finish_exit(session_factory, exit_id, "succeeded")

    resumed = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=1),
        timeout_minutes=30,
    )

    assert resumed == [repost_id]
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        assert job.status == "pending"
        assert job.last_reason == DEFERRED_RESUME_JOB_REASON
        # attempt_count back to zero is what keeps the reprocess from being
        # short-circuited as an automatic retry of the recorded deferral.
        assert job.attempt_count == 0
        item = session.query(MessageInstructionItem).one()
        assert item.last_progress_at is not None


def test_resuming_twice_enqueues_the_message_exactly_once(tmp_path):
    session_factory, repost_id, exit_id = _held_message_fixture(tmp_path)
    _finish_exit(session_factory, exit_id, "succeeded")

    first = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=1),
        timeout_minutes=30,
    )
    with session_factory() as session:
        enqueued_at = session.query(MessageProcessingJob).one().enqueued_at
    second = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=2),
        timeout_minutes=30,
    )

    assert first == [repost_id]
    assert second == [repost_id]
    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()
        assert len(jobs) == 1
        # The pending row the first resume armed is left exactly as it was.
        assert jobs[0].status == "pending"
        assert jobs[0].enqueued_at == enqueued_at


def test_an_exit_that_ends_without_releasing_the_barrier_does_not_re_enqueue(
    tmp_path,
):
    """``recovery_required`` is terminal but the barrier still holds on it."""

    session_factory, _repost_id, exit_id = _held_message_fixture(tmp_path)
    _finish_exit(session_factory, exit_id, "recovery_required")

    resumed = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=1),
        timeout_minutes=30,
    )

    assert resumed == []
    with session_factory() as session:
        assert session.query(MessageProcessingJob).one().status == "succeeded"


def test_a_deferral_past_the_timeout_is_left_to_the_expiry_path(tmp_path):
    session_factory, _repost_id, exit_id = _held_message_fixture(tmp_path)
    _finish_exit(session_factory, exit_id, "succeeded")

    resumed = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=31),
        timeout_minutes=30,
    )

    assert resumed == []
    with session_factory() as session:
        assert session.query(MessageProcessingJob).one().status == "succeeded"


def test_an_unrelated_deferred_message_is_not_resumed(tmp_path):
    session_factory, _repost_id, exit_id = _held_message_fixture(tmp_path)
    with session_factory() as session:
        other = RawMessage(
            chat_id=10,
            message_id=99,
            text="ETH short unrelated",
            archived_target_group=True,
        )
        session.add(other)
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=other.id,
                    symbol="ETH",
                    side="short",
                    review_status="approved",
                ),
                _decision(
                    other.id, reason=DEFERRED_HOLD_REASON, updated_at=NOW
                ),
            ]
        )
        session.commit()
        other_id = int(other.id)
    _finish_exit(session_factory, exit_id, "succeeded")

    resumed = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=1),
        timeout_minutes=30,
    )

    assert other_id not in resumed


def test_an_outlived_deferral_expires_with_an_incident_and_no_execution(tmp_path):
    session_factory, repost_id, _exit_id = _held_message_fixture(tmp_path)
    captured: list[int] = []

    expired = expire_stale_deferred_instructions(
        session_factory,
        now=NOW + timedelta(minutes=31),
        timeout_minutes=30,
        capture=captured.append,
    )

    assert expired == [repost_id]
    assert captured == [repost_id]
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.automation_status == "deferred"
        assert decision.automation_reason == DEFERRED_EXPIRED_REASON
        item = session.query(MessageInstructionItem).one()
        assert item.escalation_state == "expired"
        assert item.last_progress_at is not None
        # Nothing was queued: an expired instruction is never executed late.
        assert session.query(MessageProcessingJob).one().status == "succeeded"


def test_a_deferral_inside_the_window_is_not_expired(tmp_path):
    session_factory, _repost_id, _exit_id = _held_message_fixture(tmp_path)

    expired = expire_stale_deferred_instructions(
        session_factory,
        now=NOW + timedelta(minutes=29),
        timeout_minutes=30,
        capture=lambda raw_message_id: None,
    )

    assert expired == []
    with session_factory() as session:
        assert (
            session.query(RecognitionDecision).one().automation_reason
            == DEFERRED_HOLD_REASON
        )


def test_expiry_is_recorded_once_and_blocks_a_later_resume(tmp_path):
    session_factory, _repost_id, exit_id = _held_message_fixture(tmp_path)
    captured: list[int] = []

    first = expire_stale_deferred_instructions(
        session_factory,
        now=NOW + timedelta(minutes=31),
        timeout_minutes=30,
        capture=captured.append,
    )
    second = expire_stale_deferred_instructions(
        session_factory,
        now=NOW + timedelta(minutes=32),
        timeout_minutes=30,
        capture=captured.append,
    )
    _finish_exit(session_factory, exit_id, "succeeded")
    resumed = resume_instructions_deferred_by_exit(
        session_factory,
        deletion_exit_id=exit_id,
        now=NOW + timedelta(minutes=33),
        timeout_minutes=30,
    )

    assert len(first) == 1
    assert second == []
    assert len(captured) == 1
    assert resumed == []


@pytest.mark.parametrize("state", ["succeeded", "recovery_required"])
def test_the_deletion_worker_resumes_only_on_a_barrier_releasing_terminal_state(
    tmp_path, state
):
    """The worker hook fires for every terminal state; the filter is inside."""

    from telegram_kol_research import source_message_deletion_worker as worker

    session_factory, repost_id, exit_id = _held_message_fixture(tmp_path)
    _finish_exit(session_factory, exit_id, state)

    worker._resume_instructions_behind_finished_exits(
        session_factory,
        exit_ids={exit_id},
        now=(NOW + timedelta(minutes=1)).replace(tzinfo=None),
    )

    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    if state == "succeeded":
        assert job.status == "pending"
        assert job.last_reason == DEFERRED_RESUME_JOB_REASON
        assert repost_id == job.raw_message_id
    else:
        assert job.status == "succeeded"


def test_the_expiry_incident_survives_the_summary_contract(tmp_path):
    """The real recorder, not a stub: the summary field set is closed.

    A-2 learned this the hard way -- ``_capture`` swallows a rejected summary,
    so a summary the bounds check refuses is indistinguishable from silence,
    which is exactly the failure this alert exists to end.
    """

    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_deferred_instruction_expired,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"deferred_instruction_expired"})
    )

    recorded = capture_deferred_instruction_expired(
        session_factory,
        config=config,
        raw_message_id=15169,
        deferred_minutes=30,
        occurred_at=NOW,
    )

    assert recorded is not None
    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "deferred_instruction_expired"
        assert incident.severity == "high"
        # An operator can act on the alert without a database session.
        assert '"raw_message_id":15169' in incident.redacted_summary
        assert '"operation":"raw_message_15169"' in incident.redacted_summary
