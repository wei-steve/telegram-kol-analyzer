from fastapi.testclient import TestClient
from datetime import UTC, datetime
import httpx

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.llm_chat import LLMProxyConfig
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_app import create_web_app


def test_chat_api_rejects_missing_question(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post("/api/chat", json={})

    assert response.status_code == 422


def test_refresh_api_reports_missing_telegram_credentials(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post("/api/refresh")

    assert response.status_code == 503
    assert "TELEGRAM_API_ID is required" in response.json()["detail"]


def test_refresh_api_runs_reconcile_once_when_credentials_are_available(tmp_path):
    captured: dict[str, object] = {}

    def fake_auth_loader():
        from telegram_kol_research.telegram_client import TelegramAuthConfig

        return TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        )

    def fake_create_client(auth_config):
        captured["auth_config"] = auth_config
        return object()

    async def fake_reconcile_once(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        message_limit=50,
        checkpoint_overlap=5,
        discover_dialogs_fn=None,
        fetch_dialog_messages_fn=None,
    ):
        captured["client"] = client
        captured["target_titles"] = set(target_titles)
        return {
            "matched_dialogs": 2,
            "inserted_messages": 3,
            "inserted_candidates": 1,
            "inserted_trade_ideas": 1,
        }

    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
    )
    app.state.telegram_auth_loader = fake_auth_loader
    app.state.telegram_client_factory = fake_create_client
    app.state.reconcile_once_runner = fake_reconcile_once
    client = TestClient(app)

    response = client.post("/api/refresh")

    assert response.status_code == 200
    assert response.json()["inserted_messages"] == 3
    assert captured["target_titles"] == {"Demo Group"}


def test_chat_api_accepts_question_and_chat_scope(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(RawMessage(chat_id=1, message_id=10, text="hello source"))
        session.commit()

    app = create_web_app(database_path=database_path)
    app.state.chat_requester = lambda **_: "Proxy answer [1]"
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "question": "Summarize",
            "chat_id": 1,
            "group_prompt": "Prioritize recent changes",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Proxy answer [1]"
    assert response.json()["sources"][0]["raw_message_id"] is not None
    assert response.json()["sources"][0]["label"].startswith("[1]")
    assert (
        response.json()["proxy_payload"]["messages"][1]["content"]
        == "Group prompt:\nPrioritize recent changes"
    )


def test_chat_api_defaults_to_latest_50_messages_for_current_group(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "research.db"
    app = create_web_app(database_path=database_path)
    captured: dict[str, object] = {}

    def fake_load_group_messages(
        session_factory,
        *,
        chat_id,
        limit,
        before_message_id=None,
        search_text=None,
        sender_name=None,
    ):
        captured["chat_id"] = chat_id
        captured["limit"] = limit
        return [
            {
                "raw_message_id": 1,
                "message_id": 10,
                "sender_name": "Alice",
                "text": "hello",
                "reply_context": None,
                "media_assets": [],
            }
        ]

    def fake_requester(**kwargs):
        return "Default answer [1]"

    monkeypatch.setattr(
        "telegram_kol_research.web_app.load_group_messages", fake_load_group_messages
    )
    app.state.chat_requester = fake_requester
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat", json={"question": "总结这个群最近在讨论什么", "chat_id": 1}
    )

    assert response.status_code == 200
    assert captured["chat_id"] == 1
    assert captured["limit"] == 50


def test_chat_api_builds_scope_context_in_chronological_order(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=1,
                    message_id=10,
                    text="older",
                    posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
                ),
                RawMessage(
                    chat_id=1,
                    message_id=11,
                    text="newer",
                    posted_at=datetime(2026, 4, 17, 9, 0, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

    captured = {}

    def fake_requester(**kwargs):
        captured.update(kwargs)
        return "Chronology answer [1]"

    app = create_web_app(database_path=database_path)
    app.state.chat_requester = fake_requester
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat", json={"question": "Analyze recent changes", "chat_id": 1}
    )

    assert response.status_code == 200
    assert captured["scope_context"].index("text=older") < captured[
        "scope_context"
    ].index("text=newer")


def test_chat_api_uses_question_requested_recent_message_limit(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    app = create_web_app(database_path=database_path)
    captured: dict[str, object] = {}

    def fake_load_group_messages(
        session_factory,
        *,
        chat_id,
        limit,
        before_message_id=None,
        search_text=None,
        sender_name=None,
    ):
        captured["chat_id"] = chat_id
        captured["limit"] = limit
        return [
            {
                "raw_message_id": 1,
                "message_id": 10,
                "sender_name": "Alice",
                "text": "hello",
                "reply_context": None,
                "media_assets": [],
            }
        ]

    def fake_requester(**kwargs):
        return "Count answer [1]"

    monkeypatch.setattr(
        "telegram_kol_research.web_app.load_group_messages", fake_load_group_messages
    )
    app.state.chat_requester = fake_requester
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat", json={"question": "请总结最近100条消息", "chat_id": 1}
    )

    assert response.status_code == 200
    assert captured["chat_id"] == 1
    assert captured["limit"] == 100


def test_chat_api_ignores_legacy_selected_scope_fields_and_uses_default_recent_group_scope(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        first = RawMessage(chat_id=1, message_id=10, text="keep me")
        second = RawMessage(chat_id=1, message_id=11, text="ignore me")
        session.add_all([first, second])
        session.commit()
        selected_raw_message_id = first.id
    captured = {}

    def fake_requester(**kwargs):
        captured.update(kwargs)
        return "Scoped answer [1]"

    app = create_web_app(database_path=database_path)
    app.state.chat_requester = fake_requester
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "question": "Analyze selection",
            "chat_id": 1,
            "scope_mode": "selected",
            "selected_message_ids": [selected_raw_message_id],
        },
    )

    assert response.status_code == 200
    assert "keep me" in captured["scope_context"]
    assert "ignore me" in captured["scope_context"]


def test_chat_api_ignores_legacy_time_window_fields_and_uses_default_recent_group_scope(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=1,
                    message_id=10,
                    text="older",
                    posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
                ),
                RawMessage(
                    chat_id=1,
                    message_id=11,
                    text="newer",
                    posted_at=datetime(2026, 4, 17, 9, 0, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

    captured = {}

    def fake_requester(**kwargs):
        captured.update(kwargs)
        return "Window answer [1]"

    app = create_web_app(database_path=database_path)
    app.state.chat_requester = fake_requester
    app.state.llm_proxy_config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-test",
        timeout_seconds=5,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "question": "Analyze window",
            "chat_id": 1,
            "scope_mode": "time_window",
            "posted_after": "2026-04-17T08:30:00",
            "posted_before": "2026-04-17T09:30:00",
        },
    )

    assert response.status_code == 200
    assert "newer" in captured["scope_context"]
    assert "older" in captured["scope_context"]


def test_chat_api_returns_clear_error_when_proxy_request_fails(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")

    def failing_requester(**kwargs):
        raise httpx.ConnectError("proxy unavailable")

    app.state.chat_requester = failing_requester
    client = TestClient(app)

    response = client.post("/api/chat", json={"question": "Summarize", "chat_id": 1})

    assert response.status_code == 502
    assert (
        response.json()["detail"]
        == "AI proxy request failed. Check CLIProxyAPI connectivity and credentials."
    )


def test_chat_api_surfaces_image_input_model_error_from_proxy(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    request = httpx.Request("POST", "http://proxy.test/v1/chat/completions")
    proxy_response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "message": 'Cannot read "image.png" (this model does not support image input). Inform the user.'
            }
        },
    )

    def failing_requester(**kwargs):
        raise httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=proxy_response,
        )

    app.state.chat_requester = failing_requester
    client = TestClient(app)

    response = client.post("/api/chat", json={"question": "Summarize", "chat_id": 1})

    assert response.status_code == 502
    assert (
        response.json()["detail"]
        == "当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。"
    )


def test_chat_api_maps_image_input_error_text_even_when_proxy_returns_success(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")

    def failing_requester(**kwargs):
        raise httpx.HTTPError(
            'ERROR: Cannot read "image.png" (this model does not support image input). Inform the user.'
        )

    app.state.chat_requester = failing_requester
    client = TestClient(app)

    response = client.post("/api/chat", json={"question": "Summarize", "chat_id": 1})

    assert response.status_code == 502
    assert (
        response.json()["detail"]
        == "当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。"
    )


def test_chat_api_surfaces_proxy_auth_error_from_proxy(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    request = httpx.Request("POST", "http://proxy.test/v1/chat/completions")
    proxy_response = httpx.Response(
        401,
        request=request,
        json={"error": {"message": "Unauthorized"}},
    )

    def failing_requester(**kwargs):
        raise httpx.HTTPStatusError(
            "unauthorized",
            request=request,
            response=proxy_response,
        )

    app.state.chat_requester = failing_requester
    client = TestClient(app)

    response = client.post("/api/chat", json={"question": "Summarize", "chat_id": 1})

    assert response.status_code == 502
    assert (
        response.json()["detail"]
        == "AI 代理鉴权失败。请检查 TELEGRAM_KOL_LLM_API_KEY 是否已设置且有效。"
    )
