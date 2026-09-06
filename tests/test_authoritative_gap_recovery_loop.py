"""Regression tests for the extracted gap-recovery loop.

Covers:
1. Gap recovery runs without any Telegram client present.
2. A message missing a decision is enqueued within one fast-loop interval.
3. Recovery is bounded per pass.
4. Gap recovery never executes anything itself - expired candidates are
   enqueued like any other, and the worker classifies and records them.
5. ``run_reconcile_once`` enqueues both recoverable and expired candidates.
"""

import asyncio
import inspect
from datetime import datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    utc_now,
)
from telegram_kol_research.telegram_live_listener import (
    StallExpiryNotificationRateLimiter,
    recover_missing_authoritative_decisions,
    run_authoritative_gap_recovery_loop,
    run_reconcile_once,
)


BASE_NOW = datetime(2026, 8, 19, 12, 0, 0)


class _FakeClient:
    pass


async def _fake_discover_dialogs(client):
    return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]


async def _no_messages(client, dialog, limit, media_root="data/media"):
    return []


def _add_raw_message(session_factory, *, chat_id=9001, message_id=1, posted_at, text="BTC 多"):
    with session_factory() as session:
        message = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            posted_at=posted_at,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return message.id


def _jobs(session_factory):
    with session_factory() as session:
        return (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id)
            .all()
        )


# ── 1. no Telegram client anywhere in the call graph ──


def test_recover_missing_authoritative_decisions_has_no_client_parameter():
    """The whole point of the extraction: no Telegram dependency at all."""

    signature = inspect.signature(recover_missing_authoritative_decisions)
    assert "client" not in signature.parameters
    assert "discover_dialogs_fn" not in signature.parameters


def test_gap_recovery_runs_without_any_telegram_client(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-client.db")
    raw_message_id = _add_raw_message(
        session_factory, posted_at=BASE_NOW - timedelta(minutes=1)
    )

    processed: list[int] = []

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
        )

    asyncio.run(scenario())

    assert processed == []
    jobs = _jobs(session_factory)
    assert [job.raw_message_id for job in jobs] == [raw_message_id]
    assert jobs[0].status == "pending"
    assert jobs[0].last_reason == "recovery_enqueued"


def test_gap_recovery_without_an_authoritative_processor_enqueues_nothing(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-processor.db")
    _add_raw_message(session_factory, posted_at=BASE_NOW - timedelta(minutes=1))

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=None,
            now_provider=lambda: BASE_NOW,
        )
    )

    assert result["recovered_messages"] == 0
    assert _jobs(session_factory) == []


# ── 2. enqueued within one fast-loop interval ──


def test_message_enqueued_within_one_fast_loop_interval(tmp_path):
    session_factory = create_session_factory(tmp_path / "fast-loop.db")
    # run_authoritative_gap_recovery_loop has no now_provider of its own - it
    # always resolves recover_missing_authoritative_decisions's real-clock
    # default, so the fixture message must be recent by wall-clock time, not
    # relative to the fixed BASE_NOW used elsewhere in this file.
    raw_message_id = _add_raw_message(session_factory, posted_at=utc_now())

    async def scenario():
        loop = asyncio.get_running_loop()

        def provider():
            return {9001: "VIP BTC Room"}

        task = loop.create_task(
            run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=lambda _raw_message_id: None,
                chat_titles_by_id_provider=provider,
                interval_seconds=0.01,
            )
        )
        try:
            deadline = loop.time() + 3.0
            while not _jobs(session_factory) and loop.time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    jobs = _jobs(session_factory)
    assert [job.raw_message_id for job in jobs] == [raw_message_id]
    assert jobs[0].last_reason == "recovery_enqueued"


# ── 3. bounded per pass ──


def test_recovery_is_bounded_per_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "bounded.db")
    for index in range(5):
        _add_raw_message(
            session_factory,
            message_id=index + 1,
            posted_at=BASE_NOW - timedelta(minutes=1, seconds=index),
        )

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda _raw_message_id: None,
            message_limit=2,
            now_provider=lambda: BASE_NOW,
        )
    )

    assert len(_jobs(session_factory)) == 2


# ── 4. expired candidates are enqueued, never executed or recorded here ──


def test_expired_message_is_enqueued_without_being_executed_or_recorded(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    raw_message_id = _add_raw_message(
        session_factory, posted_at=BASE_NOW - timedelta(minutes=20)
    )

    processed: list[int] = []

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
        )
    )

    assert processed == []
    assert result["expired_recovery_messages"] == 0
    jobs = _jobs(session_factory)
    assert [job.raw_message_id for job in jobs] == [raw_message_id]
    assert jobs[0].status == "pending"
    with session_factory() as session:
        # Classifying and recording the expiry belongs to the worker.
        assert session.query(RecognitionDecision).count() == 0


def test_rate_limiter_allows_a_second_notification_after_the_window():
    clock = {"value": 0.0}
    limiter = StallExpiryNotificationRateLimiter(
        min_interval_seconds=300.0,
        monotonic=lambda: clock["value"],
    )
    assert limiter.should_notify() is True
    clock["value"] = 100.0
    assert limiter.should_notify() is False
    clock["value"] = 301.0
    assert limiter.should_notify() is True


# ── 5. this loop, not run_reconcile_once, owns the database-side gap ──


def test_this_loop_enqueues_recoverable_and_expired_candidates(tmp_path):
    session_factory = create_session_factory(tmp_path / "gap-both-kinds.db")
    recoverable_id = _add_raw_message(
        session_factory,
        message_id=1,
        posted_at=utc_now(),
    )
    expired_id = _add_raw_message(
        session_factory,
        message_id=2,
        posted_at=datetime(2026, 4, 10, 9, 0),
    )

    processed: list[int] = []

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
        )
    )

    assert processed == []
    jobs = _jobs(session_factory)
    assert [job.raw_message_id for job in jobs] == [recoverable_id, expired_id]
    assert {job.last_reason for job in jobs} == {"recovery_enqueued"}
    with session_factory() as session:
        assert session.query(RecognitionDecision).count() == 0


def test_run_reconcile_once_no_longer_duplicates_this_loop(tmp_path):
    """The ingest reconcile pass must not re-do the worker's database sweep."""

    session_factory = create_session_factory(tmp_path / "reconcile.db")
    _add_raw_message(session_factory, message_id=1, posted_at=utc_now())
    _add_raw_message(
        session_factory,
        message_id=2,
        posted_at=datetime(2026, 4, 10, 9, 0),
    )

    processed: list[int] = []

    result = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=processed.append,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_no_messages,
        )
    )

    assert processed == []
    assert result["recognition_status"] == "queued"
    assert "recovered_messages" not in result
    assert _jobs(session_factory) == []
