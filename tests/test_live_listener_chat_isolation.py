"""Chat isolation and ordering under message_lock_mode.

These exercise the real wiring in telegram_live_listener.run_live_listener
against a real MessageLockProvider and KeyedAsyncLockRegistry, with
persist_live_message_event faked out (no real network, no real AI
recognition). Slowness is simulated with asyncio.Event gates so ordering
is asserted deterministically rather than by timing.
"""

from __future__ import annotations

import asyncio

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.message_lock_provider import MessageLockProvider
from telegram_kol_research.telegram_live_listener import run_live_listener
from telegram_kol_research.trading_settings import save_trading_settings


class _ChatEvent:
    def __init__(self, *, chat_id: int, label: str) -> None:
        self.chat_id = chat_id
        self.label = label


class _FakeListenerClient:
    def __init__(self) -> None:
        self.handlers = []

    async def connect(self):
        pass

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    async def run_until_disconnected(self):
        pass


def _make_provider(session_factory, *, mode: str) -> MessageLockProvider:
    save_trading_settings(session_factory, {"message_lock_mode": mode})
    return MessageLockProvider(
        session_factory=session_factory,
        global_lock=asyncio.Lock(),
        registry=KeyedAsyncLockRegistry(),
    )


async def _fake_discover_dialogs_none(client):
    return []


async def _fake_fetch_no_messages(client, dialog, limit, media_root="data/media"):
    return []


async def _event_set_after_turns(event: asyncio.Event, *, turns: int = 100) -> bool:
    for _ in range(turns):
        if event.is_set():
            return True
        await asyncio.sleep(0)
    return event.is_set()


def test_provider_lock_all_waits_for_global_operation_and_blocks_new_global_work(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
    provider = MessageLockProvider(
        session_factory=session_factory,
        global_lock=asyncio.Lock(),
        registry=registry,
    )

    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        writer_attempted = asyncio.Event()
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        future_attempted = asyncio.Event()
        future_entered = asyncio.Event()

        async def first_global_operation():
            async with provider(101):
                first_entered.set()
                await release_first.wait()

        async def cross_chat_operation():
            writer_attempted.set()
            async with provider.lock_all():
                writer_entered.set()
                await release_writer.wait()

        async def future_global_operation():
            future_attempted.set()
            async with provider(202):
                future_entered.set()

        save_trading_settings(session_factory, {"message_lock_mode": "global"})
        first_task = asyncio.create_task(first_global_operation())
        await first_entered.wait()

        save_trading_settings(session_factory, {"message_lock_mode": "per_chat"})
        writer_task = asyncio.create_task(cross_chat_operation())
        await writer_attempted.wait()
        await asyncio.sleep(0)
        assert not writer_entered.is_set()

        save_trading_settings(session_factory, {"message_lock_mode": "global"})
        future_task = asyncio.create_task(future_global_operation())
        await future_attempted.wait()
        await asyncio.sleep(0)
        assert not future_entered.is_set()

        release_first.set()
        await writer_entered.wait()
        assert not future_entered.is_set()
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, writer_task, future_task), timeout=5.0
        )

    asyncio.run(scenario())


def test_provider_lock_all_waits_for_per_chat_operations_and_blocks_new_chat(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    provider = _make_provider(session_factory, mode="per_chat")
    order: list[str] = []

    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        writer_attempted = asyncio.Event()
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        future_attempted = asyncio.Event()
        future_entered = asyncio.Event()

        async def first_chat():
            async with provider(101):
                first_entered.set()
                await release_first.wait()
                order.append("first:exit")

        async def cross_chat_operation():
            writer_attempted.set()
            async with provider.lock_all():
                order.append("writer:enter")
                writer_entered.set()
                await release_writer.wait()
                order.append("writer:exit")

        async def future_chat():
            future_attempted.set()
            async with provider(202):
                order.append("future:enter")
                future_entered.set()

        first_task = asyncio.create_task(first_chat())
        await first_entered.wait()
        writer_task = asyncio.create_task(cross_chat_operation())
        await writer_attempted.wait()
        await asyncio.sleep(0)
        future_task = asyncio.create_task(future_chat())
        await future_attempted.wait()
        await asyncio.sleep(0)

        assert not writer_entered.is_set()
        assert not future_entered.is_set()
        release_first.set()
        await writer_entered.wait()
        assert not future_entered.is_set()
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, writer_task, future_task), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["first:exit", "writer:enter", "writer:exit", "future:enter"]


def test_provider_resolves_mode_only_after_shared_admission(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
    global_lock = asyncio.Lock()
    provider = MessageLockProvider(
        session_factory=session_factory,
        global_lock=global_lock,
        registry=registry,
    )

    async def scenario():
        attempted = asyncio.Event()
        entered = asyncio.Event()

        async def precreated_caller(context):
            attempted.set()
            async with context:
                entered.set()

        save_trading_settings(session_factory, {"message_lock_mode": "global"})
        await global_lock.acquire()
        caller_task = None
        try:
            async with registry._exclusive_admission():
                context = provider(101)
                caller_task = asyncio.create_task(precreated_caller(context))
                await attempted.wait()
                await asyncio.sleep(0)
                assert not entered.is_set()
                save_trading_settings(
                    session_factory, {"message_lock_mode": "per_chat"}
                )

            assert await _event_set_after_turns(entered)
        finally:
            global_lock.release()
            if caller_task is not None:
                await asyncio.wait_for(caller_task, timeout=5.0)

    asyncio.run(scenario())


def test_global_rollback_serializes_two_different_chats_again(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    provider = _make_provider(session_factory, mode="per_chat")
    order: list[str] = []

    async def scenario():
        save_trading_settings(session_factory, {"message_lock_mode": "global"})
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_attempted = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_chat():
            async with provider(101):
                order.append("first:enter")
                first_entered.set()
                await release_first.wait()
                order.append("first:exit")

        async def second_chat():
            second_attempted.set()
            async with provider(202):
                order.append("second:enter")
                second_entered.set()

        first_task = asyncio.create_task(first_chat())
        await first_entered.wait()
        second_task = asyncio.create_task(second_chat())
        await second_attempted.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, second_task), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["first:enter", "first:exit", "second:enter"]


def test_per_chat_mode_a_slow_chat_does_not_delay_another_chat(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    provider = _make_provider(session_factory, mode="per_chat")
    client = _FakeListenerClient()
    order: list[str] = []
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def fake_persist(**kwargs):
        event = kwargs["event"]
        order.append(f"start:{event.label}")
        if event.label == "a":
            a_started.set()
            await release_a.wait()
        order.append(f"end:{event.label}")
        return {}

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist,
    )

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles=set(),
            operation_lock=provider,
        )
        handler, _ = client.handlers[0]
        task_a = asyncio.create_task(handler(_ChatEvent(chat_id=111, label="a")))
        await a_started.wait()
        task_b = asyncio.create_task(handler(_ChatEvent(chat_id=222, label="b")))
        await asyncio.wait_for(task_b, timeout=5.0)
        assert order == ["start:a", "start:b", "end:b"]
        release_a.set()
        await asyncio.wait_for(task_a, timeout=5.0)

    asyncio.run(scenario())

    assert order == ["start:a", "start:b", "end:b", "end:a"]


def test_per_chat_mode_serializes_the_same_chat_in_arrival_order(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    provider = _make_provider(session_factory, mode="per_chat")
    client = _FakeListenerClient()
    order: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_persist(**kwargs):
        event = kwargs["event"]
        order.append(f"start:{event.label}")
        if event.label == "first":
            first_started.set()
            await release_first.wait()
        order.append(f"end:{event.label}")
        return {}

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist,
    )

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles=set(),
            operation_lock=provider,
        )
        handler, _ = client.handlers[0]
        task_first = asyncio.create_task(
            handler(_ChatEvent(chat_id=111, label="first"))
        )
        await first_started.wait()
        task_second = asyncio.create_task(
            handler(_ChatEvent(chat_id=111, label="second"))
        )
        await asyncio.sleep(0.02)
        assert order == ["start:first"]
        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(task_first, task_second), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["start:first", "end:first", "start:second", "end:second"]


def _load_mode(session_factory) -> str:
    from telegram_kol_research.trading_settings import load_trading_settings

    return load_trading_settings(session_factory).message_lock_mode


def test_global_mode_is_byte_for_byte_the_old_behavior(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    assert (
        _load_mode(session_factory) == "global"
    ), "message_lock_mode must default to global"
    provider = _make_provider(session_factory, mode="global")
    client = _FakeListenerClient()
    order: list[str] = []
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def fake_persist(**kwargs):
        event = kwargs["event"]
        order.append(f"start:{event.label}")
        if event.label == "a":
            a_started.set()
            await release_a.wait()
        order.append(f"end:{event.label}")
        return {}

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist,
    )

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles=set(),
            operation_lock=provider,
        )
        handler, _ = client.handlers[0]
        task_a = asyncio.create_task(handler(_ChatEvent(chat_id=111, label="a")))
        await a_started.wait()
        task_b = asyncio.create_task(handler(_ChatEvent(chat_id=222, label="b")))
        await asyncio.sleep(0.02)
        # Different chats still serialize on the single shared lock: b has not
        # even started while a is in flight.
        assert order == ["start:a"]
        release_a.set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)

    asyncio.run(scenario())

    assert order == ["start:a", "end:a", "start:b", "end:b"]
