import asyncio
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research import telegram_live_listener as live_listener
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.reconcile import build_reconcile_window
from telegram_kol_research.trading_settings import save_trading_settings


def test_build_reconcile_window_replays_small_safety_window_after_checkpoint():
    start_at, end_at = build_reconcile_window(
        checkpoint_message_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        now=datetime(2026, 4, 17, 9, 0, tzinfo=UTC),
        safety_minutes=15,
    )

    assert start_at.isoformat().startswith("2026-04-17T07:45")
    assert end_at.isoformat().startswith("2026-04-17T09:00")


def test_run_reconcile_once_offloads_blocking_database_setup(monkeypatch, tmp_path):
    block_seconds = 0.25
    heartbeat_interval = 0.01
    minimum_heartbeats = 10
    session_factory = create_session_factory(tmp_path / "research.db")

    def blocking_checkpoint_repair(_session_factory):
        time.sleep(block_seconds)

    async def no_dialogs(_client, **_kwargs):
        return []

    monkeypatch.setattr(
        live_listener,
        "repair_history_checkpoints",
        blocking_checkpoint_repair,
    )

    async def scenario() -> int:
        beats = 0
        reconcile_task = asyncio.create_task(
            live_listener.run_reconcile_once(
                client=object(),
                session_factory=session_factory,
                broker=None,
                target_titles=set(),
                discover_dialogs_fn=no_dialogs,
            )
        )

        async def heartbeat() -> None:
            nonlocal beats
            while not reconcile_task.done():
                await asyncio.sleep(heartbeat_interval)
                beats += 1

        heartbeat_task = asyncio.create_task(heartbeat())
        await reconcile_task
        await heartbeat_task
        return beats

    beats = asyncio.run(scenario())

    assert beats >= minimum_heartbeats


def test_reconcile_database_write_drains_before_cancellation(monkeypatch, tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    persistence_started = threading.Event()
    persistence_release = threading.Event()
    persistence_finished = threading.Event()

    async def one_dialog(_client, **_kwargs):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def one_message(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": 1,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh message",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": None,
            }
        ]

    def blocking_persistence(*_args, **_kwargs):
        persistence_started.set()
        assert persistence_release.wait(5.0)
        persistence_finished.set()
        return {"inserted_messages": 1, "inserted_message_keys": [(9001, 1)]}

    monkeypatch.setattr(
        live_listener,
        "_persist_history_reconcile_records",
        blocking_persistence,
    )

    async def scenario():
        reconcile_task = asyncio.create_task(
            live_listener.run_reconcile_once(
                client=object(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                discover_dialogs_fn=one_dialog,
                fetch_dialog_messages_fn=one_message,
            )
        )
        assert await asyncio.to_thread(persistence_started.wait, 5.0)
        reconcile_task.cancel()
        await asyncio.sleep(0.05)
        try:
            assert not reconcile_task.done()
        finally:
            persistence_release.set()
        with pytest.raises(asyncio.CancelledError):
            await reconcile_task

    try:
        asyncio.run(scenario())
    finally:
        persistence_release.set()

    assert persistence_finished.is_set()


def test_reconcile_broker_publish_is_safe_with_an_active_loop_subscriber(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()

    async def one_dialog(_client, **_kwargs):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def one_message(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": 1,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh message",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": None,
            }
        ]

    async def scenario() -> str:
        stream = broker.stream()
        assert await anext(stream) == ": keep-alive\n\n"
        try:
            await live_listener.run_reconcile_once(
                client=object(),
                session_factory=session_factory,
                broker=broker,
                target_titles={"VIP BTC Room"},
                discover_dialogs_fn=one_dialog,
                fetch_dialog_messages_fn=one_message,
            )
            return await asyncio.wait_for(anext(stream), timeout=1.0)
        finally:
            await stream.aclose()

    payload = asyncio.run(scenario())

    assert '"chat_id": 9001' in payload
    assert '"message_id": 1' in payload


def test_non_queue_reconcile_database_projection_leaves_loop_responsive(
    monkeypatch,
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    # inline path: scheduled for removal in cleanup step 3
    save_trading_settings(session_factory, {"message_pipeline_mode": "inline"})
    block_seconds = 0.25
    heartbeat_interval = 0.01

    async def one_dialog(_client, **_kwargs):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def one_message(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": 1,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh message",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": None,
            }
        ]

    def authoritative_processor(_raw_message_id):
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
            automation={"status": "skipped", "reason": "test"},
        )

    def blocking_trade_projection(_session_factory):
        time.sleep(block_seconds)
        return {"inserted_trade_ideas": 0}

    monkeypatch.setattr(
        live_listener,
        "persist_trade_ideas_from_candidates",
        blocking_trade_projection,
    )

    async def scenario() -> tuple[int, float]:
        beats: list[float] = []

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(heartbeat_interval)
                beats.append(time.perf_counter())

        heartbeat_task = asyncio.create_task(heartbeat())
        started = time.perf_counter()
        try:
            await live_listener.run_reconcile_once(
                client=object(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=authoritative_processor,
                discover_dialogs_fn=one_dialog,
                fetch_dialog_messages_fn=one_message,
            )
        finally:
            heartbeat_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await heartbeat_task
        marks = [started, *beats, time.perf_counter()]
        worst_gap = max(
            later - earlier for earlier, later in zip(marks, marks[1:])
        )
        return len(beats), worst_gap

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= 10
    assert worst_gap < block_seconds
