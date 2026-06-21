from datetime import UTC, datetime
import re

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MediaAsset,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.web_app import create_web_app


def test_group_messages_route_returns_partial_for_selected_group(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=77,
                    message_id=1,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="group 77",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    text="group 88",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "group 88" in response.text
    assert "group 77" not in response.text


def test_groups_route_returns_latest_activity_sorted_partial(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=77,
                    message_id=1,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    sender_name="older group",
                    text="older",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    sender_name="newer group",
                    text="newer",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups?selected_chat_id=77")

    assert response.status_code == 200
    assert 'kol-strategy-list' in response.text
    assert response.text.index("newer group") < response.text.index("older group")
    assert 'data-chat-id="77"' in response.text
    assert response.text.count("is-active") >= 1


def test_groups_route_uses_lifecycle_counts_for_sidebar_badges(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                sender_name="strategy group",
                text="group",
            )
        )
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=10,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=11,
                    symbol="ETH",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=3200,
                    entry_range_high=3220,
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=12,
                    symbol="SOL",
                    side="short",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=180,
                    entry_range_high=181,
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=13,
                    symbol="QQ",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
                StrategyLifecycle(
                    chat_id=77,
                    message_id=14,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 4, 2, tzinfo=UTC),
                    entry_range_low=6.22,
                    entry_range_high=6.27,
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups?selected_chat_id=77")

    assert response.status_code == 200
    assert re.search(
        r'class="kol-status-badge kol-status-holding"[^>]*>\s*[^<]*1\s*</span>',
        response.text,
    )
    assert re.search(
        r'class="kol-status-badge kol-status-pending"[^>]*>\s*[^<]*2\s*</span>',
        response.text,
    )
    assert re.search(r'3\s*[^<]*</span>', response.text)


def test_group_messages_route_supports_search_and_sender_filters(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88, message_id=1, sender_name="Alice", text="BTC long"
                ),
                RawMessage(
                    chat_id=88, message_id=2, sender_name="Bob", text="BTC short"
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages?search_text=BTC&sender_name=Alice")

    assert response.status_code == 200
    assert "BTC long" in response.text
    assert "BTC short" not in response.text


def test_group_messages_route_renders_filter_state_and_load_more_button(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=1, sender_name="Alice", text="first"),
                RawMessage(
                    chat_id=88, message_id=2, sender_name="Alice", text="second"
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages?sender_name=Ali")

    assert response.status_code == 200
    assert 'value=""' in response.text
    assert 'value="Ali"' in response.text
    assert "data-load-more" in response.text
    assert 'data-before-message-id="1"' in response.text
    assert 'data-latest-message-id="2"' in response.text
    assert response.text.index('data-message-list') < response.text.index('message-list-footer')
    assert response.text.index("second") < response.text.index("first")
    assert "data-message-select" not in response.text


def test_group_messages_route_renders_messages_newest_first(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=1,
                    sender_name="Alice",
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    text="older",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=2,
                    sender_name="Alice",
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="newer",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert response.text.index('class="message-text">newer</p>') < response.text.index(
        'class="message-text">older</p>'
    )


def test_group_messages_route_renders_posted_at_timestamp_for_each_message(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=7,
                sender_name="Alice",
                posted_at=datetime(2026, 4, 19, 9, 30, tzinfo=UTC),
                text="timed message",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "timed message" in response.text
    assert "2026-04-19 17:30" in response.text


def test_group_messages_route_shows_ai_strategy_detection_results(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        strategy_message = RawMessage(
            chat_id=88,
            message_id=3,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 3, tzinfo=UTC),
            text="BTC long 68000-68200 SL 67500 TP 69000/70000",
        )
        text_message = RawMessage(
            chat_id=88,
            message_id=2,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            text="普通聊天",
        )
        video_message = RawMessage(
            chat_id=88,
            message_id=1,
            sender_name="Alice",
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="视频复盘",
        )
        session.add_all([strategy_message, text_message, video_message])
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=strategy_message.id,
                source_id=None,
                symbol="BTC",
                side="long",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000/70000",
                leverage_text="20x",
                event_type="entry_signal",
                parse_source="text",
                confidence=0.91,
            )
        )
        session.add(
            MediaAsset(
                raw_message_id=video_message.id,
                kind="messagemediadocument",
                mime_type="video/mp4",
                local_path="data/media/88/1.mp4",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert response.text.count("AI识别结果：") == 3
    assert "AI识别结果：是策略" in response.text
    assert "策略内容：" in response.text
    assert "BTC long" in response.text
    assert "Entry 68000-68200" in response.text
    assert "SL 67500" in response.text
    assert "TP 69000/70000" in response.text
    assert "20x" in response.text
    assert "AI识别结果：待识别" in response.text
    assert "AI识别结果：非策略" in response.text
    assert "视频消息默认跳过" in response.text


def test_group_messages_route_renders_immediate_recognition_button(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=1,
                sender_name="Alice",
                text="BTC long 68000-68200",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert "立即识别" in response.text
    assert "data-recognize-message" in response.text
    assert 'data-raw-message-id="1"' in response.text
