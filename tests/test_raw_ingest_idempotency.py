from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.raw_ingest import (
    NormalizedMessageRecord,
    persist_normalized_messages,
)


def test_replaying_same_chat_and_message_id_updates_existing_row_without_duplicates(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = NormalizedMessageRecord(
        chat_id=1,
        message_id=10,
        sender_id=None,
        sender_name="Alice",
        text="first",
        reply_to_message_id=None,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=True,
        posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        edit_date=None,
        raw_payload="{}",
    )
    edited = NormalizedMessageRecord(
        chat_id=1,
        message_id=10,
        sender_id=None,
        sender_name="Alice",
        text="edited",
        reply_to_message_id=None,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=True,
        posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        edit_date=datetime(2026, 4, 17, 8, 5, tzinfo=UTC),
        raw_payload="{}",
    )

    persist_normalized_messages(session_factory, [first])
    persist_normalized_messages(session_factory, [edited])

    with session_factory() as session:
        rows = session.query(RawMessage).all()

    assert len(rows) == 1
    assert rows[0].text == "edited"
