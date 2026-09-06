import asyncio
from datetime import datetime
from types import SimpleNamespace

from PIL import Image
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.models import (
    MediaAsset,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SyncCheckpoint,
    utc_now,
)
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_live_listener import (
    _is_usable_downloaded_media_path,
    recover_missing_authoritative_decisions,
    run_live_listener,
    run_reconcile_once,
)


class _FakeClient:
    pass


def test_media_replay_accepts_usable_legacy_windows_style_path(tmp_path):
    image_path = tmp_path / "media" / "9001" / "77.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1)).save(image_path, format="JPEG")

    assert _is_usable_downloaded_media_path(
        "data\\media\\9001\\77.jpg",
        media_root=tmp_path / "media",
    )


class _FakeListenerClient:
    def __init__(self):
        self.connect_calls = 0
        self.handlers = []
        self.run_until_disconnected_calls = 0

    async def connect(self):
        self.connect_calls += 1

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    async def run_until_disconnected(self):
        self.run_until_disconnected_calls += 1


async def _fake_discover_dialogs(client):
    return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]


async def _fake_fetch_dialog_messages(client, dialog, limit, media_root="data/media"):
    return [
        {
            "chat_id": 9001,
            "message_id": 77,
            "sender_id": 501,
            "sender_name": "VIP BTC Room",
            "text": "already seen",
            "posted_at": "2026-04-10T08:30:00+00:00",
            "media": None,
        },
        {
            "chat_id": 9001,
            "message_id": 78,
            "sender_id": 501,
            "sender_name": "VIP BTC Room",
            "text": "fresh message",
            "posted_at": "2026-04-10T08:45:00+00:00",
            "media": None,
        },
    ]


def test_run_reconcile_once_persists_only_messages_newer_than_checkpoint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    with session_factory() as session:
        raw_messages = session.query(RawMessage).order_by(RawMessage.message_id).all()
        checkpoint = (
            session.query(SyncCheckpoint)
            .filter(
                SyncCheckpoint.chat_id == 9001,
                SyncCheckpoint.sync_kind == "history",
            )
            .one()
        )

    assert stats["inserted_messages"] == 1
    assert [message.message_id for message in raw_messages] == [78]
    assert checkpoint.last_message_id == 78


def test_run_reconcile_once_bounds_dialog_discovery_to_archived_folder(tmp_path):
    calls = []

    async def discover_archived_dialogs(_client, *, archived_only=False):
        calls.append(archived_only)
        return []

    stats = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=create_session_factory(tmp_path / "research.db"),
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=discover_archived_dialogs,
        )
    )

    assert calls == [True]
    assert stats["matched_dialogs"] == 0


def test_history_reconcile_without_authority_persists_raw_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "authority-required.db")

    stats = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=None,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert stats["inserted_messages"] == 2
    assert stats["inserted_candidates"] == 0
    assert stats["inserted_trade_ideas"] == 0
    assert stats["recognition_status"] == "queued"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(RecognitionDecision).count() == 0


def test_reconcile_enqueues_each_new_message_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed: list[int] = []

    def run_pass():
        return asyncio.run(
            run_reconcile_once(
                client=_FakeClient(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=processed.append,
                discover_dialogs_fn=_fake_discover_dialogs,
                fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
            )
        )

    first = run_pass()
    second = run_pass()

    assert first["inserted_messages"] == 2
    assert second["inserted_messages"] == 0
    assert processed == []
    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id)
            .all()
        )
        enqueued_message_ids = {
            session.get(RawMessage, job.raw_message_id).message_id for job in jobs
        }
    assert enqueued_message_ids == {77, 78}
    assert [job.status for job in jobs] == ["pending", "pending"]
    assert {job.last_reason for job in jobs} == {"history_reconcile_enqueued"}


def test_reconcile_leaves_persisted_messages_without_a_decision_to_the_worker(
    tmp_path,
):
    """Reconcile compares against Telegram, not against the database.

    A message that is already persisted but has no authoritative decision is
    the worker's ``run_authoritative_gap_recovery_loop`` to find, on a 20s
    cadence, without any Telegram call. Reconcile must not enqueue it a second
    time from its own 300s Telegram-coupled pass.
    """

    session_factory = create_session_factory(tmp_path / "recovery-gap.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=88,
            text="BTC entry missing a decision",
            posted_at=utc_now(),
        )
        session.add(raw_message)
        session.commit()
        session.refresh(raw_message)
        raw_message_id = raw_message.id

    processed: list[int] = []

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    for _ in range(2):
        asyncio.run(
            run_reconcile_once(
                client=_FakeClient(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=processed.append,
                discover_dialogs_fn=_fake_discover_dialogs,
                fetch_dialog_messages_fn=no_messages,
            )
        )

    assert processed == []
    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()
    assert jobs == []

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
        )
    )

    assert processed == []
    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()
    assert [job.raw_message_id for job in jobs] == [raw_message_id]
    assert jobs[0].status == "pending"
    assert jobs[0].last_reason == "recovery_enqueued"


def test_live_listener_waits_for_its_own_chat_lock(monkeypatch):
    client = _FakeListenerClient()
    processed: list[object] = []

    async def fake_persist_live_message_event(**kwargs):
        processed.append(kwargs["event"])
        return {}

    class Event:
        message = object()
        chat_id = 555

        async def get_chat(self):
            return SimpleNamespace(title="VIP BTC Room")

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist_live_message_event,
    )

    async def scenario():
        registry = KeyedAsyncLockRegistry()
        await run_live_listener(
            client=client,
            session_factory=None,
            broker=None,
            target_titles={"VIP BTC Room"},
            operation_lock=registry,
        )
        handler, _ = client.handlers[0]
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_chat():
            async with registry.lock(555):
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_chat())
        await holder_entered.wait()
        task = asyncio.create_task(handler(Event()))
        await asyncio.sleep(0)
        assert processed == []
        release_holder.set()
        await asyncio.wait_for(asyncio.gather(holder, task), timeout=5.0)

    asyncio.run(scenario())


def test_reconcile_retries_failed_summary_without_new_messages(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "summary-retry.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=9001, message_id=88, text="ETH long")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
            )
        )
        session.flush()
        item = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw.id,
        )[0]
        item.status = "succeeded"
        item.result_json = '{"status":"completed"}'
        item.summary_notification_status = "failed"
        raw_id = raw.id
        session.commit()

    delivered: list[int] = []

    async def fake_deliver(*args, **kwargs):
        delivered.append(kwargs["raw_message_id"])
        return True

    monkeypatch.setattr(
        "telegram_kol_research.system_operator_bot."
        "deliver_message_instruction_summary_notification",
        fake_deliver,
    )

    async def no_dialogs(client):
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            notification_bot_config=SystemOperatorBotConfig(
                bot_token="notification-token",
                chat_id="notification-chat",
            ),
            discover_dialogs_fn=no_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert delivered == [raw_id]


def test_run_reconcile_once_limits_media_downloads_to_new_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    captured = {}

    async def fake_fetch_dialog_messages(client, dialog, **kwargs):
        captured.update(kwargs)
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "overlap image",
                "posted_at": "2026-04-10T08:30:00+00:00",
                "media": {"kind": "photo", "path": None},
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh image",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": {"kind": "photo", "path": "9001/78.jpg"},
            },
        ]

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    assert stats["inserted_messages"] == 1
    assert captured["media_download_min_message_id"] == 77
    assert captured["media_download_message_ids"] == set()


def test_run_reconcile_once_retries_zero_byte_media_within_overlap(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    media_root = tmp_path / "media"
    zero_byte_path = media_root / "9001" / "77.jpg"
    zero_byte_path.parent.mkdir(parents=True)
    zero_byte_path.write_bytes(b"")
    with session_factory() as session:
        message = RawMessage(chat_id=9001, message_id=77, text="cached image")
        session.add(message)
        session.flush()
        session.add(MediaAsset(raw_message_id=message.id, kind="photo", local_path="9001/77.jpg"))
        session.add(SyncCheckpoint(chat_id=9001, sync_kind="history", last_message_id=77))
        session.commit()

    captured = {}

    async def fake_fetch_dialog_messages(client, dialog, **kwargs):
        captured.update(kwargs)
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            media_root=media_root,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    assert captured["media_download_message_ids"] == {77}


def test_run_reconcile_once_does_not_expand_window_for_old_missing_media(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old_message = RawMessage(
            chat_id=9001,
            message_id=10,
            text="old cached image",
            posted_at=datetime(2026, 4, 1, 8, 30),
        )
        session.add(old_message)
        session.flush()
        session.add(MediaAsset(raw_message_id=old_message.id, kind="photo", local_path=None))
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=77,
                text="checkpoint message",
                posted_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    async def fake_fetch_dialog_messages(client, dialog, limit, media_root="data/media"):
        return [
            {
                "chat_id": 9001,
                "message_id": 10,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "old cached image replay",
                "posted_at": "2026-04-01T08:30:00+00:00",
                "media": {"kind": "photo", "path": "media/10.jpg"},
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh message",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": None,
            },
        ]

    stats = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    with session_factory() as session:
        old_message = session.query(RawMessage).filter(RawMessage.message_id == 10).one()
        enqueued_message_ids = {
            session.get(RawMessage, job.raw_message_id).message_id
            for job in session.query(MessageProcessingJob).all()
        }

    assert stats["inserted_messages"] == 1
    assert old_message.text == "old cached image"
    assert enqueued_message_ids == {78}


def test_run_live_listener_connects_client_before_waiting_for_events():
    client = _FakeListenerClient()

    __import__("asyncio").run(
        run_live_listener(
            client=client,
            session_factory=None,
            broker=None,
            target_titles={"VIP BTC Room"},
        )
    )

    assert client.connect_calls == 1
    assert client.run_until_disconnected_calls == 1
    assert [type(event_filter).__name__ for _, event_filter in client.handlers] == [
        "NewMessage",
        "MessageDeleted",
    ]
