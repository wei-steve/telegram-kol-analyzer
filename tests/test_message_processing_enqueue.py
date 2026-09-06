"""Enqueue behaviour of the ingest role's three entry points.

``persist_live_message_event`` (live Telethon callback), history reconcile
and authoritative gap recovery all do the same thing in the queue pipeline:
persist, then idempotently create a ``message_processing_jobs`` row. None of
them runs recognition, strategy or execution - that is the worker's job.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import MessageProcessingJob, RawMessage
from telegram_kol_research.telegram_live_listener import (
    persist_live_message_event,
    recover_missing_authoritative_decisions,
    run_reconcile_once,
)
from telegram_kol_research.trading_settings import trading_settings_from_payload


BASE_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _FakeMessage:
    id = 42
    sender_id = 7
    message = "BTC 多"
    reply_to_msg_id = None
    date = BASE_NOW
    edit_date = None
    media = None

    async def get_sender(self):
        return SimpleNamespace(first_name="Alice", last_name="Trader")


class _FakeEvent:
    chat_id = 123
    message = _FakeMessage()


def _processing_result():
    return SimpleNamespace(
        recognition=SimpleNamespace(status="非策略"),
        assessment=SimpleNamespace(
            agreement_status="agreed",
            differences=[],
            mimo=SimpleNamespace(
                model="mimo-v2.5",
                status="非策略",
                payload={},
                error_message=None,
            ),
            deepseek_payload=None,
        ),
        automation={"status": "skipped", "reason": "mimo_no_action"},
    )


def _add_raw_message(
    session_factory,
    *,
    chat_id=9001,
    message_id=1,
    posted_at=BASE_NOW - timedelta(minutes=1),
):
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="BTC 多",
            posted_at=posted_at,
        )
        session.add(raw_message)
        session.commit()
        session.refresh(raw_message)
        return raw_message.id


@pytest.mark.parametrize("value", ["live", "disabled", True, [], {}, 1, None])
def test_message_pipeline_mode_rejects_values_that_could_enable_a_consumer(value):
    with pytest.raises(ValueError, match="message_pipeline_mode"):
        trading_settings_from_payload({"message_pipeline_mode": value})


def test_live_message_is_enqueued_pending_without_running_recognition(tmp_path):
    session_factory = create_session_factory(tmp_path / "live.db")
    processed = []

    result = asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=lambda raw_message_id: (
                processed.append(raw_message_id) or _processing_result()
            ),
        )
    )

    assert result["inserted_messages"] == 1
    assert processed == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.shadow is False
    assert job.last_reason == "queue_enqueued"


def test_live_enqueue_is_idempotent_across_a_replayed_event(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-replay.db")

    async def persist_once():
        return await persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
        )

    asyncio.run(persist_once())
    asyncio.run(persist_once())

    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"


def test_enqueue_failure_never_breaks_message_persistence(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "enqueue-failed.db")
    enqueue_calls = []

    def failing_enqueue(*args, **kwargs):
        enqueue_calls.append((args, kwargs))
        raise RuntimeError("db busy")

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener._enqueue_processing_jobs",
        failing_enqueue,
    )

    result = asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
        )
    )

    assert result["inserted_messages"] == 1
    assert len(enqueue_calls) == 1
    with session_factory() as session:
        assert session.query(MessageProcessingJob).count() == 0


def test_recovery_enqueue_is_idempotent_and_never_executes(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery.db")
    raw_message_id = _add_raw_message(session_factory)
    processed = []

    async def recover_once():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda candidate_id: (
                processed.append(candidate_id) or _processing_result()
            ),
            now_provider=lambda: BASE_NOW,
        )

    asyncio.run(recover_once())
    asyncio.run(recover_once())

    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()

    assert processed == []
    assert len(jobs) == 1
    assert jobs[0].raw_message_id == raw_message_id
    assert jobs[0].status == "pending"
    assert jobs[0].shadow is False
    assert jobs[0].last_reason == "recovery_enqueued"


def test_recovery_enqueues_expired_candidates_without_executing(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-expired.db")
    raw_message_id = _add_raw_message(
        session_factory,
        posted_at=BASE_NOW - timedelta(minutes=20),
    )
    processed = []

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
        )
    )

    assert processed == []
    assert result["expired_recovery_messages"] == 0
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.raw_message_id == raw_message_id
    assert job.status == "pending"
    assert job.last_reason == "recovery_enqueued"


def test_recovery_adopts_a_terminal_row_left_by_the_retired_shadow_pipeline(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-adopt.db")
    raw_message_id = _add_raw_message(session_factory)
    with session_factory() as session:
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_message_id,
                chat_id=9001,
                status="failed",
                last_reason="recovery_error:RuntimeError",
                completed_at=BASE_NOW - timedelta(seconds=30),
                shadow=True,
            )
        )
        session.commit()

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda _candidate_id: _processing_result(),
            now_provider=lambda: BASE_NOW,
        )
    )

    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.shadow is False
    assert job.last_reason == "recovery_enqueued"
    assert job.completed_at is None


def test_history_reconcile_enqueues_every_insert_without_executing(tmp_path):
    session_factory = create_session_factory(tmp_path / "history.db")
    processed = []

    async def discover_dialogs(_client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fetch_messages(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": message_id,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": f"message {message_id}",
                "posted_at": BASE_NOW - timedelta(seconds=message_id),
                "media": None,
            }
            for message_id in (77, 78)
        ]

    stats = asyncio.run(
        run_reconcile_once(
            client=object(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=lambda raw_message_id: (
                processed.append(raw_message_id) or _processing_result()
            ),
            discover_dialogs_fn=discover_dialogs,
            fetch_dialog_messages_fn=fetch_messages,
        )
    )

    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id)
            .all()
        )

    assert processed == []
    assert stats["recognition_status"] == "queued"
    assert len(jobs) == 2
    assert [job.status for job in jobs] == ["pending", "pending"]
    assert [job.shadow for job in jobs] == [False, False]
    assert [job.last_reason for job in jobs] == [
        "history_reconcile_enqueued",
        "history_reconcile_enqueued",
    ]
