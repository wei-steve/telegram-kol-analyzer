from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_app import create_web_app


def test_index_page_shows_group_list_and_messages(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="hello web",
            )
        )
        session.commit()

    client = TestClient(
        create_web_app(
            database_path=database_path,
            live_listener_status_reason="缺少 Telegram API 凭据",
            now_provider=lambda: datetime(2026, 4, 21, tzinfo=UTC),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "hello web" in response.text
    assert "77" in response.text
    assert "data-group-link" in response.text
    assert "Conversation" in response.text
    assert "研究报告流" in response.text
    assert "该群默认提示词" in response.text
    assert "群组分析偏好" in response.text
    assert "仅影响当前群，下次提问立即生效" in response.text
    assert "data-group-prompt-panel" in response.text
    assert "data-ai-workbench" in response.text
    assert "data-ai-report-feed" in response.text
    assert "data-ai-history-scroll" in response.text
    assert "data-ai-composer" in response.text
    assert "data-clear-ai-history" in response.text
    assert "data-layout-scroll-panel" in response.text
    assert 'textarea name="question"' in response.text
    assert "Scope" not in response.text
    assert "Posted after" not in response.text
    assert "默认分析当前群最近 50 条消息" in response.text
    assert "data-message-select" not in response.text
    assert "data-ai-output" not in response.text
    assert "data-ai-sources" not in response.text
    assert "source-preview" not in response.text
    assert "最后入库时间：2026-04-02 00:00" in response.text
    assert "实时监听未启用" in response.text
    assert "缺少 Telegram API 凭据" in response.text
    assert "数据库最新消息时间：2026-04-02 00:00" in response.text
    assert "数据新鲜度：456.0 小时未刷新" in response.text
    assert "刷新模式：仅本地快照" in response.text
    assert "data-message-sticky-header" in response.text


def test_index_page_renders_media_as_compact_preview_links(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=1,
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            sender_name="Demo Group",
            text="hello media",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="data/media/77/1.jpg",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    assert 'class="media-link"' in response.text
    assert 'class="message-media-preview"' in response.text
