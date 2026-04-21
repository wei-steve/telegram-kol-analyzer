from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_queries import load_group_rows


def test_load_group_rows_orders_groups_by_latest_message_desc(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=1,
                    message_id=1,
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    text="older",
                ),
                RawMessage(
                    chat_id=2,
                    message_id=1,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="newer",
                ),
            ]
        )
        session.commit()

    rows = load_group_rows(session_factory)

    assert [row["chat_id"] for row in rows] == [2, 1]


def test_load_group_rows_uses_latest_sender_name_and_custom_label(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=77,
                    message_id=1,
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    sender_name="大镖客 11分组",
                    text="older",
                ),
                RawMessage(
                    chat_id=77,
                    message_id=2,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    sender_name="大镖客 11分组",
                    text="newer",
                ),
            ]
        )
        session.commit()

    rows = load_group_rows(
        session_factory,
        group_labels_by_title={"大镖客 11分组": "大镖客"},
    )

    assert rows[0]["title"] == "大镖客"
    assert rows[0]["raw_title"] == "大镖客 11分组"
