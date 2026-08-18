"""Regression guard: neither management worker loop may block the event loop.

Phase 0 measured production at a p99 loop lag of 8.8 s, with the loop
unavailable roughly 76% of wall clock, because both of these loops called a
synchronous tick directly inside an ``async def``. This test is what stops that
from coming back: it drives each loop with a fake tick that blocks its thread
for a meaningful interval and asserts that a coroutine sharing the event loop
keeps running on schedule.

The assertion is on loop responsiveness, not on wall-clock duration, so it
stays robust on a loaded machine.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research import break_even_convergence_worker as be_worker
from telegram_kol_research import strategy_management_worker as mgmt_worker
from telegram_kol_research.runtime_worker_executor import (
    shutdown_management_worker_executor,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

TICK_BLOCK_SECONDS = 0.25
HEARTBEAT_INTERVAL_SECONDS = 0.01
OBSERVE_SECONDS = 0.6
# An unblocked loop produces ~60 heartbeats in the observation window; a loop
# blocked by the tick produces ~2. The threshold sits far from both.
MIN_HEARTBEATS = 15


@pytest.fixture(autouse=True)
def _fresh_executor():
    shutdown_management_worker_executor(wait=True)
    yield
    shutdown_management_worker_executor(wait=True)


async def _observe_loop_while(make_task) -> tuple[int, float]:
    """Run one worker loop and report heartbeat count and worst gap."""

    beats: list[float] = []

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            beats.append(time.perf_counter())

    heartbeat_task = asyncio.create_task(heartbeat())
    worker_task = make_task()
    started = time.perf_counter()
    try:
        await asyncio.sleep(OBSERVE_SECONDS)
    finally:
        worker_task.cancel()
        heartbeat_task.cancel()
        for task in (worker_task, heartbeat_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

    marks = [started, *beats]
    worst_gap = max(
        (later - earlier for earlier, later in zip(marks, marks[1:])),
        default=0.0,
    )
    return len(beats), worst_gap


def _blocking_tick(*_args, **_kwargs) -> None:
    time.sleep(TICK_BLOCK_SECONDS)


def test_strategy_management_loop_leaves_the_event_loop_responsive(monkeypatch):
    monkeypatch.setattr(
        mgmt_worker, "run_strategy_management_worker_tick", _blocking_tick
    )
    monkeypatch.setattr(
        mgmt_worker,
        "load_trading_settings",
        lambda _session_factory: SimpleNamespace(
            live_management_execution_enabled=True
        ),
    )

    async def scenario():
        return await _observe_loop_while(
            lambda: asyncio.create_task(
                mgmt_worker.run_strategy_management_worker_loop(
                    session_factory=object(),
                    deepcoin_client_factory=lambda: object(),
                    interval_seconds=0.01,
                    max_batches=1,
                    now_provider=lambda: NOW,
                )
            )
        )

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS


def test_break_even_convergence_loop_leaves_the_event_loop_responsive(monkeypatch):
    monkeypatch.setattr(
        be_worker, "run_break_even_convergence_worker_tick", _blocking_tick
    )

    async def scenario():
        return await _observe_loop_while(
            lambda: asyncio.create_task(
                be_worker.run_break_even_convergence_worker_loop(
                    object(),
                    deepcoin_client_factory=lambda: object(),
                    interval_seconds=0.01,
                    now_provider=lambda: NOW,
                )
            )
        )

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS


def test_a_tick_left_on_the_event_loop_would_fail_this_guard(monkeypatch):
    """Prove the guard has teeth by reproducing the pre-Phase-1 shape."""

    async def blocking_loop() -> None:
        while True:
            _blocking_tick()
            await asyncio.sleep(0.01)

    async def scenario():
        return await _observe_loop_while(
            lambda: asyncio.create_task(blocking_loop())
        )

    beats, worst_gap = asyncio.run(scenario())

    assert beats < MIN_HEARTBEATS
    assert worst_gap >= TICK_BLOCK_SECONDS
