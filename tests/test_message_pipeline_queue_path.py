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


def test_worker_lifespan_starts_and_stops_with_the_app(tmp_path):
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
