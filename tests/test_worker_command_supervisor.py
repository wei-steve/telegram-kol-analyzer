"""The worker role's durable command supervisor."""

import asyncio

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import save_trading_settings


def test_mode_supervisor_enters_the_queue_runner_exactly_once(tmp_path):
    from telegram_kol_research.worker_command_executor import (
        supervise_worker_command_mode,
    )

    session_factory = create_session_factory(tmp_path / "supervisor.db")
    calls = []
    finished = asyncio.Event()

    async def queue_runner(_session_factory, **_kwargs):
        calls.append("queue")
        finished.set()

    async def scenario():
        save_trading_settings(session_factory, {"worker_command_mode": "queue"})
        task = asyncio.create_task(
            supervise_worker_command_mode(
                session_factory,
                dependencies=object(),
                queue_runner=queue_runner,
                interval_seconds=0.001,
            )
        )
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())

    assert calls == ["queue"]


def test_mode_supervisor_cancellation_does_not_wait_for_blocking_adapter(tmp_path):
    from telegram_kol_research.worker_command_executor import (
        supervise_worker_command_mode,
    )

    session_factory = create_session_factory(tmp_path / "cancel.db")
    started = asyncio.Event()

    async def stuck_runner(_session_factory, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        save_trading_settings(session_factory, {"worker_command_mode": "queue"})
        task = asyncio.create_task(
            supervise_worker_command_mode(
                session_factory,
                dependencies=object(),
                queue_runner=stuck_runner,
                interval_seconds=0.001,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(scenario())
