from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ExecutionEvent, RawMessage, StrategyLifecycle
from telegram_kol_research.web_queries import load_home_event_rows


def test_load_home_event_rows_merges_sources_newest_first(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=10,
                    message_id=20,
                    sender_name="Andy",
                    posted_at=datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                    text="BTC 现价做多",
                ),
                StrategyLifecycle(
                    chat_id=10,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
                ),
                ExecutionEvent(
                    action="submit_entry_order",
                    status="submitted",
                    kol_id="Andy",
                    chat_id=10,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    created_at=datetime(2026, 7, 12, 8, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

    rows = load_home_event_rows(session_factory)

    assert [row["kind"] for row in rows] == ["execution", "strategy", "message"]
    assert rows[0].keys() >= {
        "id",
        "kind",
        "occurred_at",
        "source_label",
        "title",
        "summary",
        "symbol",
        "side",
        "status",
        "destination",
    }
    assert rows[0]["destination"]["view"] == "positions"
    assert rows[1]["destination"]["view"] == "strategies"
    assert rows[2]["destination"] == {
        "view": "messages",
        "chat_id": 10,
        "message_id": 20,
    }


def test_load_home_event_rows_filters_kinds_and_applies_limit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=10,
                    message_id=message_id,
                    posted_at=datetime(2026, 7, 12, 8, message_id, tzinfo=UTC),
                    text=f"message {message_id}",
                )
                for message_id in (1, 2, 3)
            ]
        )
        session.commit()

    rows = load_home_event_rows(session_factory, kinds={"message"}, limit=2)

    assert [row["id"] for row in rows] == ["message:10:3", "message:10:2"]
    assert all(row["symbol"] is None and row["side"] is None for row in rows)
