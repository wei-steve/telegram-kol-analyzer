import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.message_processing_worker import (
    run_message_processing_worker_tick,
)
from telegram_kol_research.models import MessageProcessingJob, RawMessage
from telegram_kol_research.telegram_live_listener import (
    persist_live_message_event,
    recover_missing_authoritative_decisions,
    run_reconcile_once,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.web_app import create_web_app


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _Message:
    id = 42
    sender_id = 7
    message = "BTC 多"
    reply_to_msg_id = None
    date = NOW
    edit_date = None
    media = None

    async def get_sender(self):
        return SimpleNamespace(first_name="Alice", last_name="Trader")


class _Event:
    chat_id = 123
    message = _Message()


def _result():
    return SimpleNamespace(
        recognition=SimpleNamespace(status="策略", reason="same-decision"),
        assessment=SimpleNamespace(agreement_status="agreed"),
        automation={"status": "executed", "reason": "same-outcome"},
    )


def test_queue_listener_only_persists_and_enqueues_until_worker_runs(tmp_path):
    session_factory = create_session_factory(tmp_path / "queue.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "queue"})
    calls = []

    asyncio.run(
        persist_live_message_event(
            event=_Event(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=lambda raw_id: calls.append(raw_id) or _result(),
        )
    )

    assert calls == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        assert job.status == "pending"
        assert job.shadow is False

    worker_result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda raw_id: calls.append(raw_id) or _result(),
            },
        )
    )
    assert worker_result.succeeded == 1
    assert len(calls) == 1


def test_inline_and_shadow_never_run_the_queue_consumer_or_double_process(tmp_path):
    for mode in ("inline", "shadow"):
        session_factory = create_session_factory(tmp_path / f"{mode}.db")
        save_trading_settings(session_factory, {"message_pipeline_mode": mode})
        calls = []
        asyncio.run(
            persist_live_message_event(
                event=_Event(),
                session_factory=session_factory,
                broker=LiveUpdateBroker(),
                media_root=tmp_path / "media",
                ai_recognition_config=AiRecognitionConfig(),
                authoritative_processor=lambda raw_id: calls.append(raw_id) or _result(),
            )
        )
        asyncio.run(
            run_message_processing_worker_tick(
                session_factory,
                now=NOW,
                process_kwargs={
                    "authoritative_processor": lambda raw_id: calls.append(raw_id) or _result(),
                },
            )
        )
        assert len(calls) == 1


def test_worker_lifespan_exists_only_when_queue_mode_is_active(tmp_path):
    started = threading.Event()
    stopped = threading.Event()

    async def fake_worker(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    app = create_web_app(
        database_path=tmp_path / "research.db",
        message_processing_worker_runner=fake_worker,
    )
    save_trading_settings(
        app.state.session_factory,
        {"message_pipeline_mode": "queue"},
    )
    with TestClient(app):
        assert started.wait(timeout=1)
        assert app.state.message_processing_worker_task is not None
    assert stopped.wait(timeout=1)
    assert app.state.message_processing_worker_task is None

    inline_started = threading.Event()

    async def forbidden_worker(**_kwargs):
        inline_started.set()

    inline_app = create_web_app(
        database_path=tmp_path / "inline-research.db",
        message_processing_worker_runner=forbidden_worker,
    )
    with TestClient(inline_app):
        assert inline_app.state.message_processing_worker_task is None
    assert not inline_started.is_set()


def test_queue_mode_disables_direct_recovery_processing(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "queue"})
    with session_factory() as session:
        raw = RawMessage(
            chat_id=9001,
            message_id=1,
            text="BTC 多",
            posted_at=NOW,
        )
        session.add(raw)
        session.commit()
    calls = []

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda raw_id: calls.append(raw_id) or _result(),
            now_provider=lambda: NOW,
        )
    )

    assert calls == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.shadow is False


def test_shadow_rollback_does_not_adopt_or_reprocess_claimed_queue_job(tmp_path):
    session_factory = create_session_factory(tmp_path / "claimed-rollback.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "queue"})
    with session_factory() as session:
        raw = RawMessage(
            chat_id=9001,
            message_id=2,
            text="BTC 多",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        raw_message_id = int(raw.id)
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_message_id,
                chat_id=9001,
                status="pending",
                last_reason="queue_enqueued",
                shadow=False,
                enqueued_at=NOW,
            )
        )
        session.commit()

    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    worker_calls = []
    recovery_calls = []

    async def blocking_worker_processor(_session_factory, **kwargs):
        worker_calls.append(kwargs["raw_message_id"])
        worker_started.set()
        await release_worker.wait()

    async def exercise_boundary():
        worker_task = asyncio.create_task(
            run_message_processing_worker_tick(
                session_factory,
                now=NOW,
                job_processor=blocking_worker_processor,
            )
        )
        await worker_started.wait()
        save_trading_settings(
            session_factory,
            {"message_pipeline_mode": "shadow"},
        )
        recovery_result = await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda candidate_id: (
                recovery_calls.append(candidate_id) or _result()
            ),
            now_provider=lambda: NOW,
        )
        release_worker.set()
        worker_result = await worker_task
        return recovery_result, worker_result

    recovery_result, worker_result = asyncio.run(exercise_boundary())

    assert worker_calls == [raw_message_id]
    assert recovery_calls == []
    assert recovery_result["recovered_messages"] == 0
    assert worker_result.succeeded == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.shadow is False
    assert job.last_reason == "worker_completed"


def test_shadow_rollback_adopts_unclaimed_pending_queue_job_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "pending-rollback.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    with session_factory() as session:
        raw = RawMessage(
            chat_id=9001,
            message_id=3,
            text="BTC 多",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        raw_message_id = int(raw.id)
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_message_id,
                chat_id=9001,
                status="pending",
                last_reason="queue_enqueued",
                shadow=False,
                enqueued_at=NOW,
            )
        )
        session.commit()
    calls = []

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda candidate_id: (
                calls.append(candidate_id) or _result()
            ),
            now_provider=lambda: NOW,
        )
    )

    assert calls == [raw_message_id]
    assert result["recovered_messages"] == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.shadow is True
    assert job.last_reason == "recovery_completed"


def test_queue_mode_disables_direct_history_reconcile_processing(tmp_path):
    session_factory = create_session_factory(tmp_path / "history.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "queue"})
    calls = []

    async def discover(_client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fetch(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 1,
                "sender_name": "VIP",
                "text": "BTC 多",
                "posted_at": NOW,
                "media": None,
            }
        ]

    asyncio.run(
        run_reconcile_once(
            client=object(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=lambda raw_id: calls.append(raw_id) or _result(),
            discover_dialogs_fn=discover,
            fetch_dialog_messages_fn=fetch,
        )
    )

    assert calls == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.shadow is False


def test_queue_job_is_not_visible_until_reply_recovery_finishes(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "reply-order.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "queue"})
    observed_job_counts = []

    async def recover_reply(*_args, **_kwargs):
        with session_factory() as session:
            observed_job_counts.append(session.query(MessageProcessingJob).count())
        return False

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.fetch_missing_reply_target",
        recover_reply,
    )
    message = SimpleNamespace(**vars(_Message))
    message.id = 43
    message.reply_to_msg_id = 41
    message.get_sender = _Message().get_sender
    event = SimpleNamespace(chat_id=123, message=message, client=object())

    asyncio.run(
        persist_live_message_event(
            event=event,
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=lambda _raw_id: _result(),
        )
    )

    assert observed_job_counts == [0]
    with session_factory() as session:
        assert session.query(MessageProcessingJob).count() == 1
