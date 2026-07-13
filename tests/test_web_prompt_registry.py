import json

from fastapi.testclient import TestClient

from telegram_kol_research.models import AiPromptTestRun, RawMessage
from telegram_kol_research.prompt_defaults import (
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.prompt_registry import record_prompt_validation, save_prompt_draft
from telegram_kol_research.web_app import create_web_app


def test_prompt_test_api_runs_each_requested_message_and_model(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        rows = [
            RawMessage(chat_id=1, message_id=1, text="first"),
            RawMessage(chat_id=1, message_id=2, text="second"),
        ]
        session.add_all(rows)
        session.commit()
        raw_ids = [row.id for row in rows]
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="api test",
    )
    captured = []

    def fake_runner(session_factory, **kwargs):
        captured.append(kwargs)
        return type(
            "Result",
            (),
            {
                "test_run_id": len(captured),
                "active_payload": {"recognition_result": "非策略"},
                "draft_payload": {"recognition_result": "非策略"},
                "differences": [],
                "duration_ms": 1,
                "error_message": None,
            },
        )()

    app.state.prompt_test_runner = fake_runner
    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test",
        json={
            "draft_version_id": detail.draft_version.id,
            "raw_message_ids": raw_ids,
            "model_kinds": ["mimo", "deepseek"],
        },
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 4
    assert {(item["raw_message_id"], item["model_kind"]) for item in response.json()["items"]} == {
        (raw_id, model_kind)
        for raw_id in raw_ids
        for model_kind in ("mimo", "deepseek")
    }
    assert len(captured) == 4


def test_trading_prompt_publish_requires_completed_historical_test(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="untested draft",
    )
    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/publish",
        json={
            "expected_draft_version_id": detail.draft_version.id,
            "expected_active_version_id": detail.active_version.id,
        },
    )

    assert response.status_code == 409
    assert "historical test" in response.json()["detail"]


def test_mimo_vision_test_rejects_mixed_models_before_any_call(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        row = RawMessage(chat_id=1, message_id=1, text="image caption")
        session.add(row)
        session.commit()
        raw_id = row.id
    detail = save_prompt_draft(
        app.state.session_factory,
        MIMO_VISION_PROMPT,
        content="读取图片和截图，不得猜测。",
        change_note="vision test",
    )
    calls = []
    app.state.prompt_test_runner = lambda *args, **kwargs: calls.append(kwargs)

    response = TestClient(app).post(
        f"/api/ai-prompts/{MIMO_VISION_PROMPT}/test",
        json={
            "draft_version_id": detail.draft_version.id,
            "raw_message_ids": [raw_id],
            "model_kinds": ["mimo", "deepseek"],
        },
    )

    assert response.status_code == 422
    assert calls == []


def test_shared_prompt_publish_requires_current_coverage_from_both_models(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content=DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\n补充覆盖测试经验。",
        change_note="coverage gate",
    )
    record_prompt_validation(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        expected_draft_version_id=detail.draft_version.id,
        success=True,
        errors=(),
    )
    vision = TestClient(app).get(f"/api/ai-prompts/{MIMO_VISION_PROMPT}").json()
    with app.state.session_factory() as session:
        session.add(
            AiPromptTestRun(
                prompt_definition_id=detail.definition_id,
                draft_version_id=detail.draft_version.id,
                model="mimo-v2.5",
                model_kind="mimo",
                active_prompt_versions_json=json.dumps({
                    SHARED_TRADING_PROMPT: detail.active_version.id,
                    MIMO_VISION_PROMPT: vision["active_version"]["id"],
                }, sort_keys=True),
                status="completed",
                differences_json="[]",
            )
        )
        session.commit()

    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/publish",
        json={
            "expected_draft_version_id": detail.draft_version.id,
            "expected_active_version_id": detail.active_version.id,
        },
    )

    assert response.status_code == 409
    assert "deepseek" in response.json()["detail"]
