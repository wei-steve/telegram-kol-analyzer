from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
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
    assert 'data-groups-list' in response.text
    assert response.text.index("newer group") < response.text.index("older group")
    assert 'data-chat-id="77"' in response.text
    assert response.text.count("is-active") == 1


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
    assert response.text.index("data-load-more") < response.text.index("first")
    assert "data-message-select" not in response.text


def test_group_messages_route_renders_messages_in_chronological_order(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=1, sender_name="Alice", text="older"),
                RawMessage(chat_id=88, message_id=2, sender_name="Alice", text="newer"),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/88/messages")

    assert response.status_code == 200
    assert response.text.index("older") < response.text.index("newer")


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
    assert "2026-04-19 09:30" in response.text
