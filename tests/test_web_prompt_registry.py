
from fastapi.testclient import TestClient

from telegram_kol_research.models import RawMessage
from telegram_kol_research.prompt_defaults import (
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.prompt_registry import save_prompt_draft
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
