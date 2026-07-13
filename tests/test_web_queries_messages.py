import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage, RecognitionDecision
from telegram_kol_research.web_queries import load_group_messages, load_messages_in_time_window


def test_load_group_messages_includes_media_and_orders_newest_first_within_page(tmp_path):
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

    assert [row["message_id"] for row in rows] == [2, 1]
    assert rows[0]["media_assets"][0]["local_path"] == "data/media/9/2.jpg"


def test_load_group_messages_limits_excessive_media_assets_per_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9,
            message_id=1,
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="many images",
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    local_path="data/media/9/with-path.jpg",
                ),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    ocr_text="BTC long",
                ),
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
            ]
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    media_assets = rows[0]["media_assets"]
    assert len(media_assets) == 3
    assert any(asset["ocr_text"] == "BTC long" for asset in media_assets)
    assert any(asset["local_path"] == "data/media/9/with-path.jpg" for asset in media_assets)


def test_load_group_messages_returns_posted_at_in_local_display_timezone(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9,
                message_id=1,
                posted_at=datetime(2026, 6, 12, 8, 30),
                text="utc stored",
            )
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    assert rows[0]["posted_at"] == datetime(2026, 6, 12, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


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


def test_load_messages_in_time_window_normalizes_aware_local_bounds_to_utc(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9,
                    message_id=1,
                    posted_at=datetime(2026, 6, 12, 7, 59),
                    text="before",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=2,
                    posted_at=datetime(2026, 6, 12, 8, 30),
                    text="inside",
                ),
            ]
        )
        session.commit()

    rows = load_messages_in_time_window(
        session_factory,
        chat_id=9,
        posted_after=datetime(2026, 6, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        posted_before=datetime(2026, 6, 12, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        limit=10,
    )

    assert [row["message_id"] for row in rows] == [2]


def test_load_group_messages_serializes_semantic_review_decisions_in_one_bulk_query(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        messages = [
            RawMessage(chat_id=9, message_id=index, text=f"message {index}")
            for index in range(1, 4)
        ]
        session.add_all(messages)
        session.flush()
        session.add_all(
            [
                RecognitionDecision(
                    raw_message_id=message.id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status="disagreed",
                    differences_json='["take_profit"]',
                    comparison_status="completed",
                    disagreement_severity="normal",
                    comparison_model="deepseek-v4-flash",
                    comparison_payload_json=json.dumps(
                        {
                            "reason": "止盈细节不同",
                            "conflict_types": ["non_material_price_detail"],
                        },
                        ensure_ascii=False,
                    ),
                )
                for message in messages
            ]
        )
        session.commit()

    decision_queries: list[str] = []
    engine = session_factory.kw["bind"]

    def track_decision_queries(_conn, _cursor, statement, _parameters, _context, _many):
        if "recognition_decisions" in statement.lower():
            decision_queries.append(statement)

    event.listen(engine, "before_cursor_execute", track_decision_queries)
    try:
        rows = load_group_messages(session_factory, chat_id=9, limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", track_decision_queries)

    assert len(decision_queries) == 1
    assert rows[0]["semantic_review"] == {
        "status": "completed",
        "severity": "normal",
        "label": "普通差异",
        "reason": "止盈细节不同",
        "conflict_types": ["non_material_price_detail"],
        "model": "deepseek-v4-flash",
    }


def test_load_group_messages_defensively_serializes_malformed_semantic_review_json(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=9, message_id=1, text="BTC long")
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="disagreed",
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="critical",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json="not-json",
            )
        )
        session.commit()

    row = load_group_messages(session_factory, chat_id=9, limit=10)[0]

    assert row["semantic_review"] == {
        "status": "completed",
        "severity": "critical",
        "label": "严重分歧",
        "reason": None,
        "conflict_types": [],
        "model": "deepseek-v4-flash",
    }
