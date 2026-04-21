from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, SyncCheckpoint
from telegram_kol_research.telegram_live_listener import run_reconcile_once


class _FakeClient:
    pass


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
