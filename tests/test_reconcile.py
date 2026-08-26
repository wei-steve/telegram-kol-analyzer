import asyncio
import time
from datetime import UTC, datetime

from telegram_kol_research import telegram_live_listener as live_listener
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.reconcile import build_reconcile_window


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
