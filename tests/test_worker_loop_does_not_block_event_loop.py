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
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research import break_even_convergence_worker as be_worker
from telegram_kol_research import strategy_management_worker as mgmt_worker
from telegram_kol_research import system_operator_bot as operator_bot
from telegram_kol_research.config import RuntimeIncidentConfig
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


def _operator_settings(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        instruction_execution_contract_mode="disabled",
        instruction_execution_entry_after_item_id=0,
    )


def _start_operator_loop() -> asyncio.Task:
    return asyncio.create_task(
        operator_bot.run_runtime_incident_notification_loop(
            session_factory=object(),
            config=operator_bot.SystemOperatorBotConfig(
                bot_token="token", chat_id="1"
            ),
            interval_seconds=0.01,
            runtime_config=RuntimeIncidentConfig(),
        )
    )


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


def test_operator_maintenance_loop_leaves_the_event_loop_responsive(monkeypatch):
    """The Phase 1b target: this loop ran its tick inline every 5 seconds."""

    monkeypatch.setattr(
        operator_bot, "run_operator_maintenance_tick", _blocking_tick
    )
    monkeypatch.setattr(
        operator_bot, "load_trading_settings", _operator_settings
    )

    async def _no_delivery(*_args, **_kwargs) -> int:
        return 0

    monkeypatch.setattr(
        operator_bot, "deliver_runtime_incident_notifications", _no_delivery
    )

    async def scenario():
        return await _observe_loop_while(_start_operator_loop)

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS


def test_operator_tick_shares_the_management_worker_thread(monkeypatch):
    """All three ticks stay mutually exclusive on one thread.

    Before Phase 1 the three were mutually exclusive only because all three ran
    on the event loop. Giving the operator tick its own pool would introduce
    three-way concurrency on paths that touch execution contracts, entry
    admissions, and the exchange. This asserts the observable consequence.
    """

    guard = threading.Lock()
    active: list[str] = []
    overlaps: list[tuple[str, ...]] = []
    threads: set[str] = set()
    seen: set[str] = set()
    both_ran = threading.Event()

    def _record(label: str):
        def tick(*_args, **_kwargs) -> None:
            with guard:
                active.append(label)
                if len(active) > 1:
                    overlaps.append(tuple(active))
                threads.add(threading.current_thread().name)
            time.sleep(0.01)
            with guard:
                active.remove(label)
                seen.add(label)
                if seen == {"operator", "management"}:
                    both_ran.set()
        return tick

    monkeypatch.setattr(
        operator_bot, "run_operator_maintenance_tick", _record("operator")
    )
    monkeypatch.setattr(
        operator_bot, "load_trading_settings", _operator_settings
    )

    async def _no_delivery(*_args, **_kwargs) -> int:
        return 0

    monkeypatch.setattr(
        operator_bot, "deliver_runtime_incident_notifications", _no_delivery
    )
    monkeypatch.setattr(
        mgmt_worker, "run_strategy_management_worker_tick", _record("management")
    )
    monkeypatch.setattr(
        mgmt_worker,
        "load_trading_settings",
        lambda _session_factory: SimpleNamespace(
            live_management_execution_enabled=True
        ),
    )

    async def scenario():
        tasks = [
            _start_operator_loop(),
            asyncio.create_task(
                mgmt_worker.run_strategy_management_worker_loop(
                    session_factory=object(),
                    deepcoin_client_factory=lambda: object(),
                    interval_seconds=0.01,
                    max_batches=1,
                    now_provider=lambda: NOW,
                )
            ),
        ]
        try:
            await asyncio.wait_for(
                asyncio.to_thread(both_ran.wait, 10.0), 15.0
            )
            await asyncio.sleep(0.2)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())

    assert seen == {"operator", "management"}
    assert overlaps == []
    assert len(threads) == 1
    assert next(iter(threads)).startswith("mgmt-worker")


def test_operator_cycle_closes_its_client_on_the_worker_thread(monkeypatch):
    """Client construction, use, and close must not be split across threads."""

    events: list[tuple[str, str]] = []

    class _Client:
        def close(self) -> None:
            events.append(("close", threading.current_thread().name))

    def build() -> _Client:
        events.append(("build", threading.current_thread().name))
        return _Client()

    def tick(*_args, **kwargs) -> None:
        events.append(("tick", threading.current_thread().name))
        assert isinstance(kwargs["execution_reconciliation_client"], _Client)

    monkeypatch.setattr(operator_bot, "run_operator_maintenance_tick", tick)
    monkeypatch.setattr(
        operator_bot,
        "load_trading_settings",
        lambda _session_factory: SimpleNamespace(
            instruction_execution_contract_mode="shadow",
            instruction_execution_entry_after_item_id=0,
        ),
    )

    async def scenario():
        from telegram_kol_research.runtime_worker_executor import (
            run_on_management_worker,
        )

        await run_on_management_worker(
            operator_bot._run_operator_maintenance_cycle,
            object(),
            deepcoin_client_factory=build,
        )

    asyncio.run(scenario())

    assert [name for name, _ in events] == ["build", "tick", "close"]
    assert len({thread for _, thread in events}) == 1
    assert events[0][1].startswith("mgmt-worker")


def _start_reconcile_loop(*, session_factory=None, client=None) -> asyncio.Task:
    from telegram_kol_research import web_app

    return asyncio.create_task(
        web_app.run_deepcoin_execution_reconcile_loop(
            session_factory=session_factory if session_factory is not None else object(),
            deepcoin_client_factory=lambda: client if client is not None else object(),
            interval_seconds=1,
            now_provider=lambda: NOW,
        )
    )


def test_deepcoin_reconcile_loop_leaves_the_event_loop_responsive(monkeypatch):
    """Phase 1c attributed 19 of 20 production stalls to this loop."""

    from telegram_kol_research import web_app

    class _Client:
        def list_open_orders(self):  # presence enables the reconcile branch
            return []

    monkeypatch.setattr(
        web_app, "reconcile_deepcoin_execution_bindings", _blocking_tick
    )
    monkeypatch.setattr(
        web_app, "sync_manual_closed_deepcoin_positions", lambda *a, **k: None
    )
    monkeypatch.setattr(web_app, "system_operator_bot_enabled", lambda _c: False)

    async def scenario():
        return await _observe_loop_while(
            lambda: _start_reconcile_loop(client=_Client())
        )

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS


def test_deepcoin_reconcile_runs_on_the_shared_management_worker_thread(monkeypatch):
    from telegram_kol_research import web_app

    class _Client:
        def list_open_orders(self):
            return []

    threads: set[str] = set()
    ran = threading.Event()

    def record(*_args, **_kwargs) -> None:
        threads.add(threading.current_thread().name)
        ran.set()

    monkeypatch.setattr(web_app, "reconcile_deepcoin_execution_bindings", record)
    monkeypatch.setattr(web_app, "sync_manual_closed_deepcoin_positions", record)
    monkeypatch.setattr(web_app, "system_operator_bot_enabled", lambda _c: False)

    async def scenario():
        task = _start_reconcile_loop(client=_Client())
        try:
            await asyncio.wait_for(asyncio.to_thread(ran.wait, 10.0), 15.0)
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert len(threads) == 1
    assert next(iter(threads)).startswith("mgmt-worker")


def test_reconcile_loop_skips_later_segments_when_an_early_one_raises(monkeypatch):
    """Splitting the body across submissions must not change ordering on error.

    In the original the whole body sat in one try, so a raise in the reconcile
    skipped the manual-close sync and every delivery after it. The segmented
    version must behave identically.
    """

    from telegram_kol_research import web_app

    class _Client:
        def list_open_orders(self):
            return []

    calls: list[str] = []
    raised = threading.Event()

    def exploding_reconcile(*_args, **_kwargs) -> None:
        calls.append("reconcile")
        raised.set()
        raise RuntimeError("reconcile exploded")

    def later_sync(*_args, **_kwargs) -> None:
        calls.append("sync_manual_closed")

    async def later_delivery(*_args, **_kwargs) -> int:
        calls.append("delivery")
        return 0

    monkeypatch.setattr(
        web_app, "reconcile_deepcoin_execution_bindings", exploding_reconcile
    )
    monkeypatch.setattr(web_app, "sync_manual_closed_deepcoin_positions", later_sync)
    monkeypatch.setattr(web_app, "system_operator_bot_enabled", lambda _c: True)
    monkeypatch.setattr(
        web_app, "deliver_pending_position_attribution_incidents", later_delivery
    )
    monkeypatch.setattr(
        web_app, "deliver_pending_position_protection_incidents", later_delivery
    )
    monkeypatch.setattr(
        web_app, "deliver_terminal_entry_cleanup_notifications", later_delivery
    )

    async def scenario():
        task = _start_reconcile_loop(client=_Client())
        try:
            await asyncio.wait_for(asyncio.to_thread(raised.wait, 10.0), 15.0)
            await asyncio.sleep(0.1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert calls
    assert set(calls) == {"reconcile"}, calls
