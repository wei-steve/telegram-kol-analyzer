import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MessageProcessingJob, RawMessage
from telegram_kol_research.message_processing_worker import (
    claim_message_processing_jobs,
    process_message_job,
    run_message_processing_worker_tick,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _processing_result():
    return SimpleNamespace(
        recognition=SimpleNamespace(status="非策略"),
        assessment=SimpleNamespace(agreement_status="agreed"),
        automation={"status": "skipped", "reason": "mimo_no_action"},
    )


def _authoritative_failure_result(reason):
    return SimpleNamespace(
        recognition=SimpleNamespace(status="识别失败", reason=reason),
        assessment=SimpleNamespace(
            agreement_status="authoritative_failed",
            mimo=SimpleNamespace(error_message=reason),
        ),
        automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
    )


def test_process_message_job_runs_the_post_persist_chain_from_raw_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "worker.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=123,
            message_id=42,
            sender_name="Alice Trader",
            text="BTC 多",
            posted_at=NOW,
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)
        raw_message_id = raw.id

    processed = []
    scheduled = []
    context_runs = []

    result = asyncio.run(
        process_message_job(
            session_factory,
            raw_message_id=raw_message_id,
            authoritative_processor=lambda candidate_id: (
                processed.append(candidate_id) or _processing_result()
            ),
            context_resolution_scheduler=lambda **event: scheduled.append(event),
            context_resolution_worker=lambda: context_runs.append("ran"),
        )
    )

    assert processed == [raw_message_id]
    assert result.recognition.status == "非策略"
    assert [event["event_type"] for event in scheduled] == [
        "next_same_chat_message",
        "evidence_version_changed",
    ]
    assert context_runs == ["ran"]


def _add_job(session_factory, *, chat_id, message_id, posted_at=NOW):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=f"message {message_id}",
            posted_at=posted_at,
        )
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=chat_id,
                status="pending",
                shadow=False,
                enqueued_at=NOW,
            )
        )
        session.commit()
        return raw.id


def test_claim_is_atomic_and_only_claims_one_ordered_job_per_chat(tmp_path):
    session_factory = create_session_factory(tmp_path / "atomic.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    _add_job(session_factory, chat_id=1, message_id=2)
    other_chat = _add_job(session_factory, chat_id=2, message_id=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: claim_message_processing_jobs(
                    session_factory,
                    claimed_at=NOW,
                    limit=10,
                ),
                range(2),
            )
        )

    claimed_ids = [claim.raw_message_id for result in results for claim in result]
    assert sorted(claimed_ids) == sorted([first, other_chat])
    assert len(claimed_ids) == len(set(claimed_ids))


def test_stale_claim_is_reclaimed_and_completed(tmp_path):
    session_factory = create_session_factory(tmp_path / "reclaim.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    first = claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW,
        stale_after=timedelta(seconds=30),
    )
    assert first[0].raw_message_id == raw_id

    processed = []

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        processed.append(raw_message_id)

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=31),
            stale_after=timedelta(seconds=30),
            job_processor=processor,
        )
    )

    assert result.succeeded == 1
    assert processed == [raw_id]
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.claim_token is None


def test_retry_backoff_is_durable_and_max_attempts_are_terminal(tmp_path):
    session_factory = create_session_factory(tmp_path / "retry.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    notifications = []

    async def failing_processor(*_args, **_kwargs):
        raise RuntimeError("transient")

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )
    assert first.retried == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        assert job.status == "pending"
        assert job.attempt_count == 1
        assert job.next_attempt_at == (NOW + timedelta(seconds=10)).replace(tzinfo=None)

    before_due = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=9),
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
        )
    )
    assert before_due.claimed == 0

    terminal = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=10),
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )
    assert terminal.failed == 1
    assert len(notifications) == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "failed"
    assert job.attempt_count == 2
    assert job.next_attempt_at is None


def test_chat_lanes_are_ordered_while_different_chats_run_concurrently(tmp_path):
    session_factory = create_session_factory(tmp_path / "lanes.db")
    chat_one_first = _add_job(session_factory, chat_id=1, message_id=1)
    chat_one_second = _add_job(session_factory, chat_id=1, message_id=2)
    chat_two = _add_job(session_factory, chat_id=2, message_id=1)
    started = []
    both_started = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        started.append(raw_message_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            job_processor=processor,
        )
    )
    assert first.succeeded == 2
    assert set(started) == {chat_one_first, chat_two}
    assert chat_one_second not in started

    asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=1),
            job_processor=lambda *_args, **kwargs: asyncio.sleep(
                0, result=started.append(kwargs["raw_message_id"])
            ),
        )
    )
    assert started[-1] == chat_one_second


def test_expired_job_is_never_executed(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    _add_job(
        session_factory,
        chat_id=1,
        message_id=1,
        posted_at=NOW - timedelta(minutes=20),
    )
    processed = []

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            job_processor=lambda *_args, **kwargs: asyncio.sleep(
                0, result=processed.append(kwargs["raw_message_id"])
            ),
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
        )
    )

    assert result.expired == 1
    assert processed == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "expired"
    assert job.last_reason == "expired_stale_instruction"


def test_consumer_never_claims_dormant_shadow_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow-is-dormant.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        job.shadow = True
        session.commit()

    claims = claim_message_processing_jobs(session_factory, claimed_at=NOW)

    assert claims == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.raw_message_id == raw_id
    assert job.status == "pending"


def test_returned_authoritative_failure_uses_durable_retry(tmp_path):
    session_factory = create_session_factory(tmp_path / "authoritative-failed.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    failed_result = _authoritative_failure_result("provider timeout")

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: failed_result,
            },
        )
    )

    assert result.retried == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.attempt_count == 1
    assert job.last_reason == "processing_error:AuthoritativeProcessingFailed"


def test_empty_input_authoritative_failure_settles_once_without_retry_or_notifier(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "empty-input-terminal.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    failed_result = _authoritative_failure_result(
        "message has no readable text or image"
    )
    notifications = []
    downstream = []

    async def strategy_processor(**_kwargs):
        downstream.append("strategy")

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: failed_result,
                "strategy_alert_config": SimpleNamespace(),
                "strategy_alert_processor": strategy_processor,
                "context_resolution_worker": lambda: downstream.append("context"),
            },
            chat_title_provider=lambda _chat_id: "group",
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )

    assert result.succeeded == 1
    assert result.retried == 0
    assert result.failed == 0
    assert notifications == []
    assert downstream == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.attempt_count == 0
    assert job.next_attempt_at is None
    assert job.completed_at == NOW.replace(tzinfo=None)
    assert job.last_reason == "terminal_authoritative_failure:empty_input"


def test_empty_input_terminal_outcome_releases_same_chat_lane(tmp_path):
    session_factory = create_session_factory(tmp_path / "empty-input-lane.db")
    first_raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    second_raw_id = _add_job(session_factory, chat_id=1, message_id=2)
    processed = []

    def processor(raw_message_id):
        processed.append(raw_message_id)
        if raw_message_id == first_raw_id:
            return _authoritative_failure_result(
                "message has no readable text or image"
            )
        return _processing_result()

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={"authoritative_processor": processor},
        )
    )
    second = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=1),
            process_kwargs={"authoritative_processor": processor},
        )
    )

    assert first.succeeded == 1
    assert first.retried == 0
    assert second.succeeded == 1
    assert processed == [first_raw_id, second_raw_id]
    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.id.asc())
            .all()
        )
    assert [job.status for job in jobs] == ["succeeded", "succeeded"]
    assert [job.last_reason for job in jobs] == [
        "terminal_authoritative_failure:empty_input",
        "worker_completed",
    ]
