from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_prompt_api_lists_every_registered_ai_prompt_with_active_content(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/api/ai-prompts")

    assert response.status_code == 200
    by_key = {item["prompt_key"]: item for item in response.json()["items"]}
    assert {
        "trading.analysis.shared",
        "trading.analysis.mimo_vision",
        "research.chat.system",
        "strategy.alert.classifier",
    }.issubset(by_key)
    assert "lifecycle_event" in by_key["trading.analysis.shared"]["active_version"]["content"]


def test_prompt_api_requires_validation_before_publish_and_supports_rollback(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    detail = client.get("/api/ai-prompts/research.chat.system").json()
    original = detail["active_version"]

    draft_response = client.put(
        "/api/ai-prompts/research.chat.system/draft",
        json={
            "content": "Published research prompt v2",
            "change_note": "clarify evidence rules",
            "expected_active_version_id": original["id"],
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()["draft_version"]
    assert draft_response.json()["active_version"]["id"] == original["id"]

    blocked = client.post(
        "/api/ai-prompts/research.chat.system/publish",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert blocked.status_code == 409

    validation = client.post(
        "/api/ai-prompts/research.chat.system/validate",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert validation.status_code == 200
    assert validation.json()["success"] is True

    published = client.post(
        "/api/ai-prompts/research.chat.system/publish",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert published.status_code == 200
    current = published.json()["active_version"]
    assert current["content"] == "Published research prompt v2"

    rolled_back = client.post(
        "/api/ai-prompts/research.chat.system/rollback",
        json={
            "source_version_id": original["id"],
            "expected_active_version_id": current["id"],
            "change_note": "restore previous production prompt",
        },
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["active_version"]["content"] == original["content"]


def test_prompt_api_reports_template_validation_errors(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    detail = client.get("/api/ai-prompts/strategy.alert.classifier").json()
    draft = client.put(
        "/api/ai-prompts/strategy.alert.classifier/draft",
        json={
            "content": "Missing all registered variables",
            "change_note": "invalid test",
            "expected_active_version_id": detail["active_version"]["id"],
        },
    ).json()["draft_version"]

    response = client.post(
        "/api/ai-prompts/strategy.alert.classifier/validate",
        json={"expected_draft_version_id": draft["id"]},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert any("缺少必需模板变量" in error for error in response.json()["errors"])


def test_prompt_api_can_create_a_group_scoped_draft_without_making_it_live(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.put(
        "/api/ai-prompts/research.chat.group/draft?chat_id=88",
        json={
            "content": "Prioritize lifecycle changes for this group.",
            "change_note": "add group-specific research focus",
        },
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["scope_chat_id"] == 88
    assert detail["active_version"]["content"] != detail["draft_version"]["content"]
    assert detail["draft_version"]["content"] == "Prioritize lifecycle changes for this group."
