"""Chat isolation, ordering, and reconcile independence in the ingest process.

These exercise the real wiring in ``telegram_live_listener`` against a real
``KeyedAsyncLockRegistry`` - the ingest process's only in-process lock - with
the persist calls faked out (no real network, no real AI recognition).
Slowness is simulated with ``asyncio.Event`` gates so ordering is asserted
deterministically rather than by timing.

Three guarantees:

1. two messages from the same chat serialize, in arrival order;
2. two messages from different chats do not;
3. a reconcile pass and the live listener do not block each other, while the
   one slice where both write ``raw_messages`` for the same chat still
   serializes.
"""

from __future__ import annotations

import asyncio

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.telegram_live_listener import (
    run_live_listener,
    run_reconcile_once,
)


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


def _listener_handler(client: _FakeListenerClient):
    handler, _ = client.handlers[0]
    return handler


def test_same_chat_serializes_in_arrival_order(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
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
            operation_lock=registry,
        )
        handler = _listener_handler(client)
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


def test_different_chats_run_in_parallel(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
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
            operation_lock=registry,
        )
        handler = _listener_handler(client)
        task_a = asyncio.create_task(handler(_ChatEvent(chat_id=111, label="a")))
        await a_started.wait()
        task_b = asyncio.create_task(handler(_ChatEvent(chat_id=222, label="b")))
        # b runs to completion while a is still parked mid-persist.
        await asyncio.wait_for(task_b, timeout=5.0)
        assert order == ["start:a", "start:b", "end:b"]
        release_a.set()
        await asyncio.wait_for(task_a, timeout=5.0)

    asyncio.run(scenario())

    assert order == ["start:a", "start:b", "end:b", "end:a"]


def test_deleted_message_handler_takes_only_its_own_chat_lock(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
    client = _FakeListenerClient()
    order: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_persist(**kwargs):
        order.append("start:live")
        first_started.set()
        await release_first.wait()
        order.append("end:live")
        return {}

    def recorder(_session_factory, **kwargs):
        order.append(f"deleted:{kwargs['chat_id']}")

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist,
    )

    class _DeletedEvent:
        def __init__(self, chat_id: int) -> None:
            self.chat_id = chat_id
            self.deleted_ids = [1]

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles=set(),
            operation_lock=registry,
            source_deletion_recorder=recorder,
        )
        new_handler = client.handlers[0][0]
        deleted_handler = client.handlers[1][0]
        live_task = asyncio.create_task(
            new_handler(_ChatEvent(chat_id=111, label="live"))
        )
        await first_started.wait()
        # A different chat's deletion is not held up by the parked live
        # message; the same chat's deletion is.
        await asyncio.wait_for(deleted_handler(_DeletedEvent(222)), timeout=5.0)
        assert order == ["start:live", "deleted:222"]
        same_chat = asyncio.create_task(deleted_handler(_DeletedEvent(111)))
        await asyncio.sleep(0.02)
        assert order == ["start:live", "deleted:222"]
        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(live_task, same_chat), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == ["start:live", "deleted:222", "end:live", "deleted:111"]


def _reconcile_dialog(chat_id: int) -> dict[str, object]:
    return {"id": chat_id, "title": f"chat-{chat_id}", "archived": True}


def test_reconcile_and_live_do_not_block_each_other(tmp_path, monkeypatch):
    """A reconcile pass holds no lock across its Telegram fetch."""

    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
    client = _FakeListenerClient()
    order: list[str] = []
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def fake_persist(**kwargs):
        order.append("live:done")
        return {}

    async def fake_discover_dialogs(_client, **_kwargs):
        return [_reconcile_dialog(111)]

    async def fake_fetch(_client, _dialog, **_kwargs):
        order.append("reconcile:fetch_start")
        fetch_started.set()
        await release_fetch.wait()
        order.append("reconcile:fetch_end")
        return []

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
            operation_lock=registry,
        )
        handler = _listener_handler(client)
        reconcile_task = asyncio.create_task(
            run_reconcile_once(
                client=client,
                session_factory=session_factory,
                broker=None,
                target_titles={"chat-111"},
                discover_dialogs_fn=fake_discover_dialogs,
                fetch_dialog_messages_fn=fake_fetch,
                chat_operation_lock=registry,
            )
        )
        await fetch_started.wait()
        # Same chat as the reconcile pass, and it still gets through: the
        # Telegram fetch is not under the lock.
        await asyncio.wait_for(
            handler(_ChatEvent(chat_id=111, label="live")), timeout=5.0
        )
        assert order == ["reconcile:fetch_start", "live:done"]
        release_fetch.set()
        await asyncio.wait_for(reconcile_task, timeout=5.0)

    asyncio.run(scenario())

    assert order == [
        "reconcile:fetch_start",
        "live:done",
        "reconcile:fetch_end",
    ]


def test_reconcile_persist_slice_serializes_against_the_same_chat(
    tmp_path, monkeypatch
):
    """The one place both paths write raw_messages is still serialized."""

    session_factory = create_session_factory(tmp_path / "research.db")
    registry = KeyedAsyncLockRegistry()
    client = _FakeListenerClient()
    order: list[str] = []
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def fake_live_persist(**kwargs):
        order.append("live:done")
        return {}

    async def fake_discover_dialogs(_client, **_kwargs):
        return [_reconcile_dialog(111)]

    async def fake_fetch(_client, _dialog, **_kwargs):
        return [{"message_id": 7, "chat_id": 111, "text": "hi"}]

    def fake_normalize(payload, **_kwargs):
        return payload

    async def fake_slice(operation, *args, **kwargs):
        if operation.__name__ == "_persist_history_reconcile_records":
            order.append("reconcile:persist_start")
            persist_started.set()
            await release_persist.wait()
            order.append("reconcile:persist_end")
            return {"inserted_messages": 0, "inserted_message_keys": []}
        if operation.__name__ == "_load_history_checkpoint_projection":
            return {}
        if operation.__name__ == "_load_orphan_media_message_ids":
            return []
        return None

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_live_persist,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.normalize_message_payload",
        fake_normalize,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener._run_reconcile_database_slice",
        fake_slice,
    )

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles=set(),
            operation_lock=registry,
        )
        handler = _listener_handler(client)
        reconcile_task = asyncio.create_task(
            run_reconcile_once(
                client=client,
                session_factory=session_factory,
                broker=None,
                target_titles={"chat-111"},
                discover_dialogs_fn=fake_discover_dialogs,
                fetch_dialog_messages_fn=fake_fetch,
                chat_operation_lock=registry,
            )
        )
        await persist_started.wait()
        blocked = asyncio.create_task(
            handler(_ChatEvent(chat_id=111, label="live"))
        )
        await asyncio.sleep(0.02)
        assert order == ["reconcile:persist_start"]
        # A different chat is unaffected while that write slice is held.
        await asyncio.wait_for(
            handler(_ChatEvent(chat_id=222, label="other")), timeout=5.0
        )
        assert order == ["reconcile:persist_start", "live:done"]
        release_persist.set()
        await asyncio.wait_for(
            asyncio.gather(reconcile_task, blocked), timeout=5.0
        )

    asyncio.run(scenario())

    assert order == [
        "reconcile:persist_start",
        "live:done",
        "reconcile:persist_end",
        "live:done",
    ]
