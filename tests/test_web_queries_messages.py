from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.web_queries import load_group_messages


def test_load_group_messages_includes_media_and_orders_newest_first(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        older = RawMessage(
            chat_id=9,
            message_id=1,
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="older",
        )
        newer = RawMessage(
            chat_id=9,
            message_id=2,
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            text="newer",
        )
        session.add_all([older, newer])
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=newer.id, kind="photo", local_path="data/media/9/2.jpg"
            )
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    assert rows[0]["message_id"] == 2
    assert rows[0]["media_assets"][0]["local_path"] == "data/media/9/2.jpg"


def test_load_group_messages_can_load_older_page(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9,
                    message_id=1,
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    text="oldest",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=2,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="middle",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=3,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    text="newest",
                ),
            ]
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=2, before_message_id=3)

    assert [row["message_id"] for row in rows] == [2, 1]


def test_load_group_messages_can_filter_by_text_and_sender(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9, message_id=1, sender_name="Alice", text="BTC long"
                ),
                RawMessage(
                    chat_id=9, message_id=2, sender_name="Bob", text="ETH short"
                ),
                RawMessage(
                    chat_id=9, message_id=3, sender_name="Alice", text="Macro note"
                ),
            ]
        )
        session.commit()

    rows = load_group_messages(
        session_factory, chat_id=9, limit=10, search_text="BTC", sender_name="Alice"
    )

    assert len(rows) == 1
    assert rows[0]["text"] == "BTC long"
