from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.raw_ingest import (
    NormalizedMessageRecord,
    persist_normalized_messages,
)


def test_persist_normalized_messages_publishes_inserted_message_events(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()

    persist_normalized_messages(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=1,
                message_id=10,
                sender_id=None,
                sender_name="Alice",
                text="new",
                reply_to_message_id=None,
                media_kind=None,
                media_path=None,
                media_payload=None,
                archived_target_group=True,
                posted_at=None,
                edit_date=None,
                raw_payload="{}",
            )
        ],
        broker=broker,
    )

    assert broker.published_events[-1]["message_id"] == 10
