from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage, SyncCheckpoint
from telegram_kol_research.telegram_live_listener import run_live_listener, run_reconcile_once


class _FakeClient:
    pass


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


def test_reconcile_processes_each_new_message_authoritatively_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed: list[int] = []

    def authoritative_processor(raw_message_id):
        processed.append(raw_message_id)
        return SimpleNamespace(
            recognition=SimpleNamespace(status="非策略"),
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="非策略",
                    payload={},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "group_not_auto_trade"},
        )

    first = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )
    second = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert first["inserted_messages"] == 2
    assert second["inserted_messages"] == 0
    assert len(processed) == 2
    with session_factory() as session:
        processed_message_ids = {
            session.get(RawMessage, raw_message_id).message_id
            for raw_message_id in processed
        }
    assert processed_message_ids == {77, 78}


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

    processed = []

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

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs["record"].message_id)
        return {"status": "sent"}

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    with session_factory() as session:
        old_message = session.query(RawMessage).filter(RawMessage.message_id == 10).one()

    assert stats["inserted_messages"] == 1
    assert old_message.text == "old cached image"
    assert processed == [78]


def test_run_reconcile_once_triggers_strategy_alert_processor_for_fresh_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert len(processed) == 2
    assert [item["record"].message_id for item in processed] == [77, 78]
    assert {item["chat_title"] for item in processed} == {"VIP BTC Room"}


def test_run_reconcile_once_skips_strategy_alert_processor_when_group_ai_is_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_enabled_for_title=lambda title: False,
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert processed == []


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
    assert len(client.handlers) == 1
