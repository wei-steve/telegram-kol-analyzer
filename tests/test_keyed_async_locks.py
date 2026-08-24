"""Per-key asyncio lock registry: isolation, FIFO ordering, and bounded growth."""

from __future__ import annotations

import asyncio

import pytest

from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry


async def _wait_for_snapshot(
    registry: KeyedAsyncLockRegistry, **expected: object
) -> None:
    for _ in range(100):
        snapshot = registry.snapshot()
        if all(snapshot[name] == value for name, value in expected.items()):
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"registry state did not reach {expected!r}; last state was {snapshot!r}"
    )


def test_two_different_keys_proceed_concurrently():
    registry = KeyedAsyncLockRegistry()
    order: list[str] = []
    a_entered = asyncio.Event()
    b_entered = asyncio.Event()

    async def hold_a():
        async with registry.lock("chat-a"):
            order.append("a:enter")
            a_entered.set()
            await b_entered.wait()
            order.append("a:exit")

    async def hold_b():
        await a_entered.wait()
        async with registry.lock("chat-b"):
            order.append("b:enter")
            b_entered.set()
            order.append("b:exit")

    async def scenario():
        await asyncio.wait_for(asyncio.gather(hold_a(), hold_b()), timeout=5.0)

    asyncio.run(scenario())

    assert order == ["a:enter", "b:enter", "b:exit", "a:exit"]


def test_same_key_serializes():
    registry = KeyedAsyncLockRegistry()
    active = 0
    overlapped = False

    async def work(label: str):
        nonlocal active, overlapped
        async with registry.lock("chat-x"):
            active += 1
            if active > 1:
                overlapped = True
            await asyncio.sleep(0.01)
            active -= 1

    async def scenario():
        await asyncio.wait_for(
            asyncio.gather(*(work(str(i)) for i in range(5))), timeout=5.0
        )

    asyncio.run(scenario())

    assert overlapped is False


def test_ordering_within_a_key_is_fifo():
    registry = KeyedAsyncLockRegistry()
    order: list[int] = []

    async def worker(index: int, started: asyncio.Event, release_gate: asyncio.Event):
        async with registry.lock("chat-y"):
            order.append(index)
            started.set()
            if index == 0:
                await release_gate.wait()

    async def scenario():
        release_gate = asyncio.Event()
        first_started = asyncio.Event()
        starts = [asyncio.Event() for _ in range(4)]
        first_task = asyncio.create_task(worker(0, first_started, release_gate))
        await first_started.wait()

        later_tasks = [
            asyncio.create_task(worker(i, starts[i], release_gate))
            for i in range(1, 4)
        ]
        # Give each waiter a chance to queue on the lock in submission order
        # before releasing it, so FIFO order is actually being exercised.
        await asyncio.sleep(0.05)
        release_gate.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, *later_tasks), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == [0, 1, 2, 3]


def test_lock_all_is_deadlock_free_under_concurrent_per_key_acquisition():
    registry = KeyedAsyncLockRegistry()
    events: list[str] = []

    async def holder(key: str, ready: asyncio.Event, release_gate: asyncio.Event):
        async with registry.lock(key):
            ready.set()
            await release_gate.wait()

    async def cross_chat_operation(label: str):
        async with registry.lock_all():
            events.append(f"{label}:enter")
            await asyncio.sleep(0.01)
            events.append(f"{label}:exit")

    async def scenario():
        ready_1 = asyncio.Event()
        ready_2 = asyncio.Event()
        release_gate = asyncio.Event()
        holder_tasks = [
            asyncio.create_task(holder("chat-1", ready_1, release_gate)),
            asyncio.create_task(holder("chat-2", ready_2, release_gate)),
        ]
        await asyncio.gather(ready_1.wait(), ready_2.wait())

        # Two concurrent lock_all() calls, both wanting the same two locks
        # held by the tasks above. If they acquired in different relative
        # orders this would deadlock; asyncio.wait_for's timeout is the
        # tripwire.
        cross_tasks = [
            asyncio.create_task(cross_chat_operation("all-1")),
            asyncio.create_task(cross_chat_operation("all-2")),
        ]
        await asyncio.sleep(0.02)
        release_gate.set()
        await asyncio.wait_for(
            asyncio.gather(*holder_tasks, *cross_tasks), timeout=5.0
        )

    asyncio.run(scenario())

    # No deadlock, and every lock_all()'s enter/exit pair is contiguous -
    # nothing interleaved inside a held lock_all().
    all_1_span = (events.index("all-1:enter"), events.index("all-1:exit"))
    all_2_span = (events.index("all-2:enter"), events.index("all-2:exit"))
    assert all_1_span[1] - all_1_span[0] == 1
    assert all_2_span[1] - all_2_span[0] == 1


def test_lock_all_held_blocks_a_future_key():
    registry = KeyedAsyncLockRegistry()
    order: list[str] = []

    async def scenario():
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        future_attempted = asyncio.Event()
        future_entered = asyncio.Event()

        async def hold_all():
            async with registry.lock_all():
                order.append("writer:enter")
                writer_entered.set()
                await release_writer.wait()
                order.append("writer:exit")

        async def use_future_key():
            future_attempted.set()
            async with registry.lock("chat-future"):
                order.append("future:enter")
                future_entered.set()

        writer_task = asyncio.create_task(hold_all())
        await writer_entered.wait()
        future_task = asyncio.create_task(use_future_key())
        await future_attempted.wait()
        await asyncio.sleep(0)

        assert not future_entered.is_set()
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(writer_task, future_task), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["writer:enter", "writer:exit", "future:enter"]


def test_waiting_lock_all_blocks_a_future_key_until_old_readers_drain():
    registry = KeyedAsyncLockRegistry()
    order: list[str] = []

    async def scenario():
        reader_entered = asyncio.Event()
        release_reader = asyncio.Event()
        writer_attempted = asyncio.Event()
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        future_attempted = asyncio.Event()
        future_entered = asyncio.Event()

        async def hold_old_reader():
            async with registry.lock("chat-old"):
                reader_entered.set()
                await release_reader.wait()
                order.append("reader:exit")

        async def hold_all():
            writer_attempted.set()
            async with registry.lock_all():
                order.append("writer:enter")
                writer_entered.set()
                await release_writer.wait()
                order.append("writer:exit")

        async def use_future_key():
            future_attempted.set()
            async with registry.lock("chat-future"):
                order.append("future:enter")
                future_entered.set()

        reader_task = asyncio.create_task(hold_old_reader())
        await reader_entered.wait()
        writer_task = asyncio.create_task(hold_all())
        await writer_attempted.wait()
        await asyncio.sleep(0)
        future_task = asyncio.create_task(use_future_key())
        await future_attempted.wait()
        await asyncio.sleep(0)

        assert not writer_entered.is_set()
        assert not future_entered.is_set()

        release_reader.set()
        await writer_entered.wait()
        assert not future_entered.is_set()
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(reader_task, writer_task, future_task), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["reader:exit", "writer:enter", "writer:exit", "future:enter"]


def test_waiting_lock_all_is_not_starved_by_continuous_new_keys():
    registry = KeyedAsyncLockRegistry()
    order: list[str] = []

    async def scenario():
        reader_entered = asyncio.Event()
        release_reader = asyncio.Event()
        writer_attempted = asyncio.Event()
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        future_attempted = [asyncio.Event() for _ in range(12)]
        future_entered = [asyncio.Event() for _ in range(12)]

        async def hold_old_reader():
            async with registry.lock("chat-old"):
                reader_entered.set()
                await release_reader.wait()

        async def hold_all():
            writer_attempted.set()
            async with registry.lock_all():
                order.append("writer:enter")
                writer_entered.set()
                await release_writer.wait()
                order.append("writer:exit")

        async def use_new_key(index: int):
            future_attempted[index].set()
            async with registry.lock(f"chat-new-{index}"):
                order.append(f"future-{index}:enter")
                future_entered[index].set()

        reader_task = asyncio.create_task(hold_old_reader())
        await reader_entered.wait()
        writer_task = asyncio.create_task(hold_all())
        await writer_attempted.wait()
        await asyncio.sleep(0)

        future_tasks = [
            asyncio.create_task(use_new_key(index)) for index in range(12)
        ]
        await asyncio.gather(*(event.wait() for event in future_attempted))
        await asyncio.sleep(0)

        assert not any(event.is_set() for event in future_entered)
        release_reader.set()
        await writer_entered.wait()
        assert not any(event.is_set() for event in future_entered)
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(reader_task, writer_task, *future_tasks), timeout=5.0
        )

    asyncio.run(scenario())

    assert order[:2] == ["writer:enter", "writer:exit"]


def test_multiple_lock_all_callers_are_exclusive_without_deadlock():
    registry = KeyedAsyncLockRegistry()
    order: list[str] = []
    active_writers = 0
    peak_writers = 0

    async def scenario():
        nonlocal active_writers, peak_writers
        reader_entered = asyncio.Event()
        release_reader = asyncio.Event()
        release_first_writer = asyncio.Event()

        async def hold_reader():
            async with registry.lock("chat-old"):
                reader_entered.set()
                await release_reader.wait()

        async def writer(label: str):
            nonlocal active_writers, peak_writers
            async with registry.lock_all():
                active_writers += 1
                peak_writers = max(peak_writers, active_writers)
                order.append(f"{label}:enter")
                if label == "writer-1":
                    await release_first_writer.wait()
                order.append(f"{label}:exit")
                active_writers -= 1

        reader_task = asyncio.create_task(hold_reader())
        await reader_entered.wait()
        writer_tasks = [
            asyncio.create_task(writer("writer-1")),
            asyncio.create_task(writer("writer-2")),
        ]
        await _wait_for_snapshot(registry, waiting_exclusive_admissions=2)
        release_reader.set()
        await _wait_for_snapshot(registry, exclusive_admission_active=True)
        assert order == ["writer-1:enter"]
        release_first_writer.set()
        await asyncio.wait_for(
            asyncio.gather(reader_task, *writer_tasks), timeout=5.0
        )

    asyncio.run(scenario())

    assert peak_writers == 1
    assert order == [
        "writer-1:enter",
        "writer-1:exit",
        "writer-2:enter",
        "writer-2:exit",
    ]


def test_cancelled_waiting_lock_all_restores_reader_admission():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        reader_entered = asyncio.Event()
        release_reader = asyncio.Event()
        future_entered = asyncio.Event()

        async def hold_reader():
            async with registry.lock("chat-old"):
                reader_entered.set()
                await release_reader.wait()

        async def waiting_writer():
            async with registry.lock_all():
                raise AssertionError("cancelled writer unexpectedly entered")

        async def future_reader():
            async with registry.lock("chat-future"):
                future_entered.set()

        reader_task = asyncio.create_task(hold_reader())
        await reader_entered.wait()
        writer_task = asyncio.create_task(waiting_writer())
        await _wait_for_snapshot(registry, waiting_exclusive_admissions=1)

        writer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer_task
        await _wait_for_snapshot(registry, waiting_exclusive_admissions=0)

        future_task = asyncio.create_task(future_reader())
        await asyncio.wait_for(future_entered.wait(), timeout=5.0)
        release_reader.set()
        await asyncio.wait_for(
            asyncio.gather(reader_task, future_task), timeout=5.0
        )

    asyncio.run(scenario())

    assert registry.known_key_count() == 0


def test_cancelled_held_lock_all_releases_exclusive_admission():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        writer_entered = asyncio.Event()

        async def hold_writer():
            async with registry.lock_all():
                writer_entered.set()
                await asyncio.Event().wait()

        writer_task = asyncio.create_task(hold_writer())
        await writer_entered.wait()
        writer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer_task

        await _wait_for_snapshot(registry, exclusive_admission_active=False)
        async with registry.lock("chat-after-cancel"):
            pass

    asyncio.run(scenario())

    assert registry.known_key_count() == 0


def test_exception_inside_lock_all_releases_exclusive_admission():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        with pytest.raises(RuntimeError, match="writer failed"):
            async with registry.lock_all():
                raise RuntimeError("writer failed")

        await _wait_for_snapshot(registry, exclusive_admission_active=False)
        async with registry.lock("chat-after-error"):
            pass

    asyncio.run(scenario())

    assert registry.known_key_count() == 0


def test_cancelled_key_waiter_releases_ref_and_shared_admission():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def owner():
            async with registry.lock("chat-shared"):
                owner_entered.set()
                await release_owner.wait()

        async def waiter():
            async with registry.lock("chat-shared"):
                raise AssertionError("cancelled key waiter unexpectedly entered")

        owner_task = asyncio.create_task(owner())
        await owner_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await _wait_for_snapshot(
            registry,
            active_shared_admissions=2,
            known_key_count=1,
        )

        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        await _wait_for_snapshot(
            registry,
            active_shared_admissions=1,
            known_key_count=1,
        )

        release_owner.set()
        await asyncio.wait_for(owner_task, timeout=5.0)

    asyncio.run(scenario())

    assert registry.snapshot() == {
        "active_shared_admissions": 0,
        "waiting_exclusive_admissions": 0,
        "exclusive_admission_active": False,
        "known_key_count": 0,
    }


def test_registry_cleans_keys_after_mixed_reader_writer_cancellation():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def owner():
            async with registry.lock("chat-mixed"):
                owner_entered.set()
                await release_owner.wait()

        async def key_waiter():
            async with registry.lock("chat-mixed"):
                raise AssertionError("cancelled key waiter unexpectedly entered")

        async def writer():
            async with registry.lock_all():
                raise AssertionError("cancelled writer unexpectedly entered")

        owner_task = asyncio.create_task(owner())
        await owner_entered.wait()
        key_waiter_task = asyncio.create_task(key_waiter())
        await _wait_for_snapshot(registry, active_shared_admissions=2)
        writer_task = asyncio.create_task(writer())
        await _wait_for_snapshot(registry, waiting_exclusive_admissions=1)

        key_waiter_task.cancel()
        writer_task.cancel()
        for task in (key_waiter_task, writer_task):
            with pytest.raises(asyncio.CancelledError):
                await task

        release_owner.set()
        await asyncio.wait_for(owner_task, timeout=5.0)

    asyncio.run(scenario())

    assert registry.snapshot() == {
        "active_shared_admissions": 0,
        "waiting_exclusive_admissions": 0,
        "exclusive_admission_active": False,
        "known_key_count": 0,
    }


def test_registry_stays_bounded_after_locks_release():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        for i in range(500):
            async with registry.lock(f"chat-{i}"):
                pass

    asyncio.run(scenario())

    assert registry.known_key_count() == 0


def test_registry_key_count_reflects_only_in_flight_keys():
    registry = KeyedAsyncLockRegistry()

    async def scenario():
        entered = asyncio.Event()
        release_gate = asyncio.Event()

        async def hold(key: str):
            async with registry.lock(key):
                entered.set()
                await release_gate.wait()

        task = asyncio.create_task(hold("chat-held"))
        await entered.wait()
        assert registry.known_key_count() == 1

        release_gate.set()
        await asyncio.wait_for(task, timeout=5.0)
        assert registry.known_key_count() == 0

    asyncio.run(scenario())
