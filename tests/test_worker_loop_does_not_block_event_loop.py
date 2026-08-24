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
import logging
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research import break_even_convergence_worker as be_worker
from telegram_kol_research import strategy_management_worker as mgmt_worker
from telegram_kol_research import system_operator_bot as operator_bot
from telegram_kol_research import telegram_bot_commands as bot_commands
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


def test_system_operator_callback_loop_leaves_event_loop_responsive(monkeypatch):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    update_sent = False

    async def _get_one_update(*_args, **_kwargs):
        nonlocal update_sent
        if not update_sent:
            update_sent = True
            return [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": "unknown:1",
                        "message": {"message_id": 7},
                    },
                }
            ]
        await asyncio.sleep(3600)

    async def _noop(*_args, **_kwargs):
        return None

    async def _zero(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(bot_commands.httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient())
    monkeypatch.setattr(bot_commands, "_delete_webhook", _noop)
    monkeypatch.setattr(bot_commands, "_latest_update_offset", _zero)
    monkeypatch.setattr(bot_commands, "_get_updates", _get_one_update)
    monkeypatch.setattr(bot_commands, "_message_is_from_alert_chat", lambda *_args: True)
    monkeypatch.setattr(bot_commands, "_answer_callback_query", _noop)
    monkeypatch.setattr(bot_commands, "process_system_operator_callback_data", _blocking_tick)

    async def scenario():
        return await _observe_loop_while(
            lambda: asyncio.create_task(
                bot_commands.run_system_operator_bot_command_loop(
                    config=bot_commands.SystemOperatorBotConfig(
                        bot_token="token", chat_id="1"
                    ),
                    session_factory=object(),
                    poll_interval_seconds=0.01,
                )
            )
        )

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS


def test_system_operator_callback_builds_and_runs_on_management_thread(monkeypatch):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    threads: list[tuple[str, str, int]] = []
    update_sent = False

    def _build_client():
        threads.append(
            ("build", threading.current_thread().name, threading.get_ident())
        )
        return object()

    def _process_callback(*_args, **_kwargs):
        threads.append(
            ("process", threading.current_thread().name, threading.get_ident())
        )
        return None

    async def _get_one_update(*_args, **_kwargs):
        nonlocal update_sent
        if not update_sent:
            update_sent = True
            return [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": "expiry_refresh:1",
                        "message": {"message_id": 7},
                    },
                }
            ]
        await asyncio.sleep(3600)

    async def _noop(*_args, **_kwargs):
        return None

    async def _zero(*_args, **_kwargs):
        return 0

    async def scenario():
        callback_answered = asyncio.Event()

        async def _answer(*_args, **_kwargs):
            callback_answered.set()

        monkeypatch.setattr(bot_commands.httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient())
        monkeypatch.setattr(bot_commands, "_delete_webhook", _noop)
        monkeypatch.setattr(bot_commands, "_latest_update_offset", _zero)
        monkeypatch.setattr(bot_commands, "_get_updates", _get_one_update)
        monkeypatch.setattr(bot_commands, "_message_is_from_alert_chat", lambda *_args: True)
        monkeypatch.setattr(bot_commands, "_answer_callback_query", _answer)
        monkeypatch.setattr(
            bot_commands, "process_system_operator_callback_data", _process_callback
        )
        task = asyncio.create_task(
            bot_commands.run_system_operator_bot_command_loop(
                config=bot_commands.SystemOperatorBotConfig(
                    bot_token="token", chat_id="1"
                ),
                session_factory=object(),
                deepcoin_client_factory=_build_client,
                poll_interval_seconds=0.01,
            )
        )
        try:
            await asyncio.wait_for(callback_answered.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())

    assert [name for name, _thread, _ident in threads] == ["build", "process"]
    assert len({ident for _name, _thread, ident in threads}) == 1
    assert threads[0][1].startswith("mgmt-worker")


def test_system_operator_callback_cancellation_waits_for_inflight_management_unit(
    monkeypatch,
):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    update_sent = False
    processing_started = threading.Event()
    processing_release = threading.Event()
    processing_finished = threading.Event()

    async def _get_one_update(*_args, **_kwargs):
        nonlocal update_sent
        if not update_sent:
            update_sent = True
            return [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": "unknown:1",
                        "message": {"message_id": 7},
                    },
                }
            ]
        await asyncio.sleep(3600)

    async def _noop(*_args, **_kwargs):
        return None

    async def _zero(*_args, **_kwargs):
        return 0

    def _blocking_callback(*_args, **_kwargs):
        processing_started.set()
        processing_release.wait(timeout=5)
        processing_finished.set()
        return None

    monkeypatch.setattr(
        bot_commands.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(),
    )
    monkeypatch.setattr(bot_commands, "_delete_webhook", _noop)
    monkeypatch.setattr(bot_commands, "_latest_update_offset", _zero)
    monkeypatch.setattr(bot_commands, "_get_updates", _get_one_update)
    monkeypatch.setattr(
        bot_commands,
        "_message_is_from_alert_chat",
        lambda *_args: True,
    )
    monkeypatch.setattr(bot_commands, "_answer_callback_query", _noop)
    monkeypatch.setattr(
        bot_commands,
        "process_system_operator_callback_data",
        _blocking_callback,
    )

    async def scenario():
        task = asyncio.create_task(
            bot_commands.run_system_operator_bot_command_loop(
                config=bot_commands.SystemOperatorBotConfig(
                    bot_token="token",
                    chat_id="1",
                ),
                session_factory=object(),
                poll_interval_seconds=0.01,
            )
        )
        try:
            assert await asyncio.to_thread(processing_started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done()
            assert not processing_finished.is_set()
        finally:
            processing_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert processing_finished.is_set()

    asyncio.run(scenario())


def test_system_operator_callback_cancellation_cancels_queued_management_unit_without_waiting(
    monkeypatch,
):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    update_sent = False
    blocker_started = threading.Event()
    blocker_release = threading.Event()
    callback_submitted = threading.Event()
    callback_started = threading.Event()
    real_run_on_management_worker = bot_commands.run_on_management_worker

    def _block_management_worker():
        blocker_started.set()
        blocker_release.wait(timeout=5)

    async def _record_callback_submission(*args, **kwargs):
        callback_submitted.set()
        return await real_run_on_management_worker(*args, **kwargs)

    async def _get_one_update(*_args, **_kwargs):
        nonlocal update_sent
        if not update_sent:
            update_sent = True
            return [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": "unknown:1",
                        "message": {"message_id": 7},
                    },
                }
            ]
        await asyncio.sleep(3600)

    async def _noop(*_args, **_kwargs):
        return None

    async def _zero(*_args, **_kwargs):
        return 0

    def _callback(*_args, **_kwargs):
        callback_started.set()
        return None

    monkeypatch.setattr(
        bot_commands.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(),
    )
    monkeypatch.setattr(bot_commands, "_delete_webhook", _noop)
    monkeypatch.setattr(bot_commands, "_latest_update_offset", _zero)
    monkeypatch.setattr(bot_commands, "_get_updates", _get_one_update)
    monkeypatch.setattr(
        bot_commands,
        "_message_is_from_alert_chat",
        lambda *_args: True,
    )
    monkeypatch.setattr(bot_commands, "_answer_callback_query", _noop)
    monkeypatch.setattr(
        bot_commands,
        "process_system_operator_callback_data",
        _callback,
    )
    monkeypatch.setattr(
        bot_commands,
        "run_on_management_worker",
        _record_callback_submission,
    )

    async def scenario():
        blocker_task = asyncio.create_task(
            real_run_on_management_worker(_block_management_worker)
        )
        task = None
        try:
            assert await asyncio.to_thread(blocker_started.wait, 2)
            task = asyncio.create_task(
                bot_commands.run_system_operator_bot_command_loop(
                    config=bot_commands.SystemOperatorBotConfig(
                        bot_token="token",
                        chat_id="1",
                    ),
                    session_factory=object(),
                    poll_interval_seconds=0.01,
                )
            )
            assert await asyncio.to_thread(callback_submitted.wait, 2)
            assert not callback_started.is_set()
            task.cancel()
            await asyncio.sleep(0.05)
            cancelled_without_waiting = task.done()
        finally:
            blocker_release.set()
            await blocker_task

        assert task is not None
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        return cancelled_without_waiting

    cancelled_without_waiting = asyncio.run(scenario())

    assert cancelled_without_waiting is True
    assert not callback_started.is_set()


def test_system_operator_callback_logs_worker_failure_before_propagating_cancellation(
    monkeypatch,
    caplog,
):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    update_sent = False
    processing_started = threading.Event()
    processing_release = threading.Event()

    async def _get_one_update(*_args, **_kwargs):
        nonlocal update_sent
        if not update_sent:
            update_sent = True
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "callback-41",
                        "data": "unknown:41",
                        "message": {"message_id": 7},
                    },
                }
            ]
        await asyncio.sleep(3600)

    async def _noop(*_args, **_kwargs):
        return None

    async def _zero(*_args, **_kwargs):
        return 0

    def _failing_callback(*_args, **_kwargs):
        processing_started.set()
        processing_release.wait(timeout=5)
        raise RuntimeError("callback drain failed")

    monkeypatch.setattr(
        bot_commands.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(),
    )
    monkeypatch.setattr(bot_commands, "_delete_webhook", _noop)
    monkeypatch.setattr(bot_commands, "_latest_update_offset", _zero)
    monkeypatch.setattr(bot_commands, "_get_updates", _get_one_update)
    monkeypatch.setattr(
        bot_commands,
        "_message_is_from_alert_chat",
        lambda *_args: True,
    )
    monkeypatch.setattr(bot_commands, "_answer_callback_query", _noop)
    monkeypatch.setattr(
        bot_commands,
        "process_system_operator_callback_data",
        _failing_callback,
    )
    caplog.set_level(logging.ERROR, logger=bot_commands.__name__)

    async def scenario():
        task = asyncio.create_task(
            bot_commands.run_system_operator_bot_command_loop(
                config=bot_commands.SystemOperatorBotConfig(
                    bot_token="token",
                    chat_id="1",
                ),
                session_factory=object(),
                poll_interval_seconds=0.01,
            )
        )
        try:
            assert await asyncio.to_thread(processing_started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
        finally:
            processing_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())

    matching = [
        record
        for record in caplog.records
        if record.getMessage()
        == "System operator bot failed to process update_id=41"
    ]
    assert len(matching) == 1
    assert matching[0].exc_info is not None
    assert isinstance(matching[0].exc_info[1], RuntimeError)


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


def test_context_resolution_scheduler_batch_runs_on_the_worker_thread():
    from telegram_kol_research.runtime_worker_executor import (
        run_on_management_worker,
    )
    from telegram_kol_research.lifecycle_monitor import (
        _run_context_resolution_scheduler_batch,
    )

    seen: list[tuple[str, int]] = []
    threads: set[str] = set()

    def scheduler(**event):
        threads.add(threading.current_thread().name)
        seen.append((event["event_type"], event["chat_id"]))

    events = [
        {"event_type": "entry_leg_status_changed", "chat_id": 7, "occurred_at": NOW},
        {"event_type": "exchange_snapshot_changed", "chat_id": 3, "occurred_at": NOW},
        {"event_type": "exchange_snapshot_changed", "chat_id": 9, "occurred_at": NOW},
    ]

    async def scenario():
        await run_on_management_worker(
            _run_context_resolution_scheduler_batch, scheduler, events
        )

    asyncio.run(scenario())

    assert seen == [
        ("entry_leg_status_changed", 7),
        ("exchange_snapshot_changed", 3),
        ("exchange_snapshot_changed", 9),
    ]
    assert len(threads) == 1
    assert next(iter(threads)).startswith("mgmt-worker")


def test_context_resolution_scheduler_batch_forwards_payloads_unchanged():
    from telegram_kol_research.runtime_worker_executor import (
        run_on_management_worker,
    )
    from telegram_kol_research.lifecycle_monitor import (
        _run_context_resolution_scheduler_batch,
    )

    received: list[dict] = []
    events = [
        {"event_type": "entry_leg_status_changed", "chat_id": 1, "occurred_at": NOW},
    ]

    async def scenario():
        await run_on_management_worker(
            _run_context_resolution_scheduler_batch,
            lambda **event: received.append(event),
            events,
        )

    asyncio.run(scenario())

    assert received == events


def test_lifecycle_cycle_makes_no_submission_when_nothing_is_scheduled(monkeypatch):
    """An empty cycle must not pay a worker hop, and must not call the scheduler."""

    from telegram_kol_research import runtime_worker_executor as rwe

    calls = {"submitted": 0}
    real = rwe.run_on_management_worker

    async def counting(fn, /, *args, **kwargs):
        calls["submitted"] += 1
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(rwe, "run_on_management_worker", counting)

    from telegram_kol_research.lifecycle_monitor import (
        _run_context_resolution_scheduler_batch,
    )

    scheduled: list = []

    async def scenario():
        events: list = []
        if events:  # mirrors the guard in _run_one_cycle
            await counting(
                _run_context_resolution_scheduler_batch,
                lambda **e: scheduled.append(e),
                events,
            )

    asyncio.run(scenario())

    assert calls["submitted"] == 0
    assert scheduled == []


def test_scheduler_batch_blocking_leaves_the_event_loop_responsive():
    """The whole point: N+M database round trips no longer run on the loop."""

    from telegram_kol_research.runtime_worker_executor import (
        run_on_management_worker,
    )
    from telegram_kol_research.lifecycle_monitor import (
        _run_context_resolution_scheduler_batch,
    )

    def slow_scheduler(**_event):
        time.sleep(TICK_BLOCK_SECONDS / 5)

    events = [
        {"event_type": "exchange_snapshot_changed", "chat_id": i, "occurred_at": NOW}
        for i in range(5)
    ]

    async def scenario():
        async def run_batch():
            while True:
                await run_on_management_worker(
                    _run_context_resolution_scheduler_batch, slow_scheduler, events
                )
                await asyncio.sleep(0.01)

        return await _observe_loop_while(lambda: asyncio.create_task(run_batch()))

    beats, worst_gap = asyncio.run(scenario())

    assert beats >= MIN_HEARTBEATS
    assert worst_gap < TICK_BLOCK_SECONDS
