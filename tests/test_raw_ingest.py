from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import SyncCheckpoint
from telegram_kol_research.raw_ingest import normalize_message_payload
from telegram_kol_research.raw_ingest import persist_normalized_messages


def test_normalize_message_payload_keeps_reply_and_media_metadata():
    payload = {
        "chat_id": 1001,
        "message_id": 77,
        "sender_id": 501,
        "text": "BTC long 68000-68200",
        "reply_to_msg_id": 70,
        "media": {"kind": "photo", "path": "media/77.jpg"},
    }
    normalized = normalize_message_payload(payload)
    assert normalized.reply_to_message_id == 70
    assert normalized.media_kind == "photo"


def test_normalize_message_payload_serializes_datetime_fields_in_raw_payload():
    payload = {
        "chat_id": 1001,
        "message_id": 78,
        "sender_id": 502,
        "text": "BTC short 68000",
        "posted_at": datetime(2026, 4, 10, 8, 30, tzinfo=UTC),
        "edit_date": datetime(2026, 4, 10, 8, 45, tzinfo=UTC),
        "media": {"kind": "photo", "path": "media/78.jpg"},
    }

    normalized = normalize_message_payload(payload)

    assert "2026-04-10T08:30:00+00:00" in normalized.raw_payload
    assert "2026-04-10T08:45:00+00:00" in normalized.raw_payload


def test_persist_normalized_messages_updates_checkpoint_to_latest_message_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    older = normalize_message_payload(
        {
            "chat_id": 1001,
            "message_id": 77,
            "posted_at": datetime(2026, 4, 10, 8, 30, tzinfo=UTC),
        }
    )
    newer = normalize_message_payload(
        {
            "chat_id": 1001,
            "message_id": 78,
            "posted_at": datetime(2026, 4, 10, 8, 45, tzinfo=UTC),
        }
    )

    persist_normalized_messages(session_factory, [newer, older], sync_kind="history")

    with session_factory() as session:
        checkpoint = session.query(SyncCheckpoint).filter(SyncCheckpoint.chat_id == 1001).one()

    assert checkpoint.last_message_id == 78
    assert checkpoint.last_message_at == datetime(2026, 4, 10, 8, 45)
