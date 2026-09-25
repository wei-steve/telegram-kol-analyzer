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
        "strategy.alert.classifier",
    }.issubset(by_key)
    assert "lifecycle_event" in by_key["trading.analysis.shared"]["active_version"]["content"]


ALERT_DRAFT = (
    "Classify one message.\n"
    "chat_title={chat_title}\n"
    "sender_name={sender_name}\n"
    "first_line={first_line}\n"
    "message_text:\n{message_text}"
)


def test_prompt_api_requires_validation_before_publish_and_supports_rollback(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    detail = client.get("/api/ai-prompts/strategy.alert.classifier").json()
    original = detail["active_version"]

    draft_response = client.put(
        "/api/ai-prompts/strategy.alert.classifier/draft",
        json={
            # The alert profile only asks for its four template variables.
            "content": ALERT_DRAFT,
            "change_note": "clarify evidence rules",
            "expected_active_version_id": original["id"],
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()["draft_version"]
    assert draft_response.json()["active_version"]["id"] == original["id"]

    blocked = client.post(
        "/api/ai-prompts/strategy.alert.classifier/publish",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert blocked.status_code == 409

    validation = client.post(
        "/api/ai-prompts/strategy.alert.classifier/validate",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert validation.status_code == 200
    assert validation.json()["success"] is True

    published = client.post(
        "/api/ai-prompts/strategy.alert.classifier/publish",
        json={
            "expected_draft_version_id": draft["id"],
            "expected_active_version_id": original["id"],
        },
    )
    assert published.status_code == 200
    current = published.json()["active_version"]
    assert current["content"] == ALERT_DRAFT

    rolled_back = client.post(
        "/api/ai-prompts/strategy.alert.classifier/rollback",
        json={
            "source_version_id": original["id"],
            "expected_active_version_id": current["id"],
            "change_note": "restore previous production prompt",
        },
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["active_version"]["content"] == original["content"]


def test_a_trading_prompt_publishes_on_validation_alone(tmp_path):
    """Publishing no longer demands historical A/B runs for two model kinds.

    That gate named "mimo" and "deepseek" in a system that now picks its models
    from the stage bindings, and its second kind resolved to a stage that is not
    on the production path. On 2026-09-25 that provider began answering 402 and
    a validated, correct draft became unpublishable for a reason that had
    nothing to do with the draft. What remains is what reads the draft itself:
    validation, and the compare-and-swap on the version ids.
    """

    from telegram_kol_research.prompt_defaults import (
        DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    )

    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    original = client.get("/api/ai-prompts/trading.analysis.shared").json()[
        "active_version"
    ]
    draft = client.put(
        "/api/ai-prompts/trading.analysis.shared/draft",
        json={
            "content": DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\n- 追加一条无害规则。",
            "change_note": "publishable without any historical test run",
            "expected_active_version_id": original["id"],
        },
    ).json()["draft_version"]

    # Unvalidated is still refused: that gate reads the draft, so it stays.
    blocked = client.post(
        "/api/ai-prompts/trading.analysis.shared/publish",
        json={
            "expected_draft_version_id": draft["id"],
            "expected_active_version_id": original["id"],
        },
    )
    assert blocked.status_code == 409

    validation = client.post(
        "/api/ai-prompts/trading.analysis.shared/validate",
        json={"expected_draft_version_id": draft["id"]},
    )
    assert validation.status_code == 200, validation.json()
    assert validation.json()["success"] is True, validation.json()["errors"]

    # No /test call in between -- that is the point.
    published = client.post(
        "/api/ai-prompts/trading.analysis.shared/publish",
        json={
            "expected_draft_version_id": draft["id"],
            "expected_active_version_id": original["id"],
        },
    )
    assert published.status_code == 200, published.json()
    assert published.json()["active_version"]["id"] == draft["id"]


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
