"""The shared single-worker executor used by both management worker loops."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from telegram_kol_research.runtime_worker_executor import (
    MANAGEMENT_WORKER_MAX_WORKERS,
    MANAGEMENT_WORKER_THREAD_NAME_PREFIX,
    get_management_worker_executor,
    run_on_management_worker,
    shutdown_management_worker_executor,
)


@pytest.fixture(autouse=True)
def _fresh_executor():
    shutdown_management_worker_executor(wait=True)
    yield
    shutdown_management_worker_executor(wait=True)


def test_executor_is_a_single_shared_instance():
    first = get_management_worker_executor()
    second = get_management_worker_executor()

    assert first is second
    assert first._max_workers == MANAGEMENT_WORKER_MAX_WORKERS == 1


def test_submissions_run_on_exactly_one_worker_thread():
    async def scenario():
        thread_names = []

        def record() -> str:
            name = threading.current_thread().name
            thread_names.append(name)
            return name

        for _ in range(6):
            await run_on_management_worker(record)
        return thread_names

    thread_names = asyncio.run(scenario())

    assert len(thread_names) == 6
    assert len(set(thread_names)) == 1
    assert thread_names[0].startswith(MANAGEMENT_WORKER_THREAD_NAME_PREFIX)
    assert thread_names[0] != threading.current_thread().name


def test_submissions_execute_serially_in_submission_order():
    order: list[str] = []
    overlapped: list[str] = []
    active = 0
    guard = threading.Lock()

    def work(label: str) -> str:
        nonlocal active
        with guard:
            active += 1
            if active > 1:
                overlapped.append(label)
        order.append(f"start:{label}")
        time.sleep(0.02)
        order.append(f"end:{label}")
        with guard:
            active -= 1
        return label

    async def scenario():
        return await asyncio.gather(
            *(run_on_management_worker(work, label) for label in "abcd")
        )

    results = asyncio.run(scenario())

    assert results == ["a", "b", "c", "d"]
    assert overlapped == []
    assert order == [
        "start:a", "end:a",
        "start:b", "end:b",
        "start:c", "end:c",
        "start:d", "end:d",
    ]


def test_exceptions_propagate_to_the_awaiting_coroutine_unchanged():
    sentinel = RuntimeError("tick exploded")

    def boom() -> None:
        raise sentinel

    async def scenario():
        with pytest.raises(RuntimeError) as excinfo:
            await run_on_management_worker(boom)
        return excinfo.value

    assert asyncio.run(scenario()) is sentinel


def test_keyword_arguments_are_forwarded():
    def combine(first, *, second, third=3):
        return (first, second, third)

    async def scenario():
        return await run_on_management_worker(combine, 1, second=2)

    assert asyncio.run(scenario()) == (1, 2, 3)


def test_shutdown_is_idempotent_and_recreates_on_next_use():
    first = get_management_worker_executor()

    shutdown_management_worker_executor(wait=True)
    shutdown_management_worker_executor(wait=True)
    shutdown_management_worker_executor(wait=False)

    second = get_management_worker_executor()

    assert second is not first
    assert asyncio.run(run_on_management_worker(lambda: "alive")) == "alive"


def test_shutdown_without_waiting_returns_while_a_call_is_in_flight():
    release = threading.Event()
    started = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(5.0)
        return "done"

    executor = get_management_worker_executor()
    future = executor.submit(blocking)
    assert started.wait(5.0)

    began = time.perf_counter()
    shutdown_management_worker_executor(wait=False)
    elapsed = time.perf_counter() - began

    assert elapsed < 1.0
    release.set()
    assert future.result(5.0) == "done"


def test_cancelling_the_await_leaves_the_in_flight_call_running():
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(5.0)
        finished.set()
        return "done"

    async def scenario():
        task = asyncio.create_task(run_on_management_worker(blocking))
        await asyncio.to_thread(started.wait, 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
        release.set()
        await asyncio.to_thread(finished.wait, 5.0)

    asyncio.run(scenario())

    assert finished.is_set()
