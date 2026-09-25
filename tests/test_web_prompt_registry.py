import yaml
from fastapi.testclient import TestClient

from telegram_kol_research.models import RawMessage
from telegram_kol_research.prompt_defaults import (
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.prompt_registry import save_prompt_draft
from telegram_kol_research.web_app import create_web_app


def _ai_config_file(tmp_path, *stage_models, models=None):
    """A v2 config file whose authoritative stage is bound to ``stage_models``.

    The prompt centre reads the bound models straight off this file, so the
    fixture is the binding rather than a vendor name.
    """

    entries = models if models is not None else [
        {"id": model_id, "supports_image": True} for model_id in stage_models
    ]
    path = tmp_path / "ai_recognition.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "providers": [
                    {
                        "id": "house",
                        "base_url": "https://house.example/v1",
                        "api_key": "k",
                        "enabled": True,
                    }
                ],
                "models": [
                    {
                        "id": entry["id"],
                        "provider_id": "house",
                        "model": entry["id"],
                        "supports_text": True,
                        "supports_image": entry.get("supports_image", True),
                        "enabled": True,
                    }
                    for entry in entries
                ],
                "stages": {"authoritative_recognition": list(stage_models)},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _app(tmp_path, *stage_models, models=None):
    return create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=_ai_config_file(
            tmp_path, *stage_models, models=models
        ),
    )


def _fake_runner(captured):
    def runner(session_factory, **kwargs):
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
                "model_id": kwargs.get("model_id", ""),
                "model": kwargs.get("model_id", ""),
                "stage_key": "authoritative_recognition",
            },
        )()

    return runner


def test_prompt_test_api_runs_each_requested_message_and_model(tmp_path):
    app = _app(tmp_path, "head-model", "second-model")
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
    app.state.prompt_test_runner = _fake_runner(captured)

    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test",
        json={
            "draft_version_id": detail.draft_version.id,
            "raw_message_ids": raw_ids,
            "model_ids": ["head-model", "second-model"],
        },
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 4
    assert {
        (item["raw_message_id"], item["model_id"]) for item in response.json()["items"]
    } == {
        (raw_id, model_id)
        for raw_id in raw_ids
        for model_id in ("head-model", "second-model")
    }
    assert len(captured) == 4
    assert {item["model_id"] for item in captured} == {"head-model", "second-model"}


def test_prompt_test_api_defaults_to_the_stage_chain_head(tmp_path):
    """No ``model_ids`` means the model production would call, not a vendor."""

    app = _app(tmp_path, "head-model", "second-model")
    with app.state.session_factory() as session:
        row = RawMessage(chat_id=1, message_id=1, text="first")
        session.add(row)
        session.commit()
        raw_id = row.id
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="default model",
    )
    captured = []
    app.state.prompt_test_runner = _fake_runner(captured)

    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test",
        json={"draft_version_id": detail.draft_version.id, "raw_message_ids": [raw_id]},
    )

    assert response.status_code == 200
    assert [item["model_id"] for item in response.json()["items"]] == ["head-model"]
    assert [item["model_id"] for item in captured] == ["head-model"]
    assert response.json()["items"][0]["stage_key"] == "authoritative_recognition"


def test_prompt_test_api_default_follows_a_rebind_of_the_stage(tmp_path):
    """The same request, a different binding, a different model tested."""

    app = _app(tmp_path, "head-model", "second-model")
    with app.state.session_factory() as session:
        row = RawMessage(chat_id=1, message_id=1, text="first")
        session.add(row)
        session.commit()
        raw_id = row.id
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="rebind",
    )
    captured = []
    app.state.prompt_test_runner = _fake_runner(captured)
    client = TestClient(app)
    body = {
        "draft_version_id": detail.draft_version.id,
        "raw_message_ids": [raw_id],
    }

    before = client.post(f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test", json=body)
    # Rebinding is a file edit; the three processes reload it on next use.
    _ai_config_file(tmp_path, "second-model", "head-model")
    after = client.post(f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test", json=body)

    assert [item["model_id"] for item in before.json()["items"]] == ["head-model"]
    assert [item["model_id"] for item in after.json()["items"]] == ["second-model"]


def test_prompt_test_api_rejects_a_model_outside_the_chain_before_any_call(tmp_path):
    app = _app(tmp_path, "head-model")
    with app.state.session_factory() as session:
        row = RawMessage(chat_id=1, message_id=1, text="first")
        session.add(row)
        session.commit()
        raw_id = row.id
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="unbound model",
    )
    calls = []
    app.state.prompt_test_runner = lambda *args, **kwargs: calls.append(kwargs)

    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test",
        json={
            "draft_version_id": detail.draft_version.id,
            "raw_message_ids": [raw_id],
            "model_ids": ["head-model", "not-bound"],
        },
    )

    assert response.status_code == 422
    assert "not a usable member" in response.json()["detail"]
    assert calls == []


def test_image_prompt_test_rejects_a_model_that_cannot_read_images(tmp_path):
    """Capability, not vendor: the image model is accepted, the text one is not.

    One config, one difference. Both models are bound to the same stage, so
    the refusal is about ``supports_image`` and nothing else.
    """

    app = _app(
        tmp_path,
        "sees-images",
        "text-only",
        models=[
            {"id": "sees-images", "supports_image": True},
            {"id": "text-only", "supports_image": False},
        ],
    )
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
    captured = []
    app.state.prompt_test_runner = _fake_runner(captured)
    client = TestClient(app)
    body = {"draft_version_id": detail.draft_version.id, "raw_message_ids": [raw_id]}

    accepted = client.post(
        f"/api/ai-prompts/{MIMO_VISION_PROMPT}/test",
        json={**body, "model_ids": ["sees-images"]},
    )
    refused = client.post(
        f"/api/ai-prompts/{MIMO_VISION_PROMPT}/test",
        json={**body, "model_ids": ["text-only"]},
    )

    assert accepted.status_code == 200
    assert [item["model_id"] for item in accepted.json()["items"]] == ["sees-images"]
    assert refused.status_code == 422
    assert "cannot read images" in refused.json()["detail"]
    assert [item["model_id"] for item in captured] == ["sees-images"]


def test_prompt_test_api_reports_an_unbound_stage_instead_of_guessing(tmp_path):
    app = _app(tmp_path)
    with app.state.session_factory() as session:
        row = RawMessage(chat_id=1, message_id=1, text="first")
        session.add(row)
        session.commit()
        raw_id = row.id
    detail = save_prompt_draft(
        app.state.session_factory,
        SHARED_TRADING_PROMPT,
        content='{"recognition_result":"test"}',
        change_note="empty chain",
    )
    calls = []
    app.state.prompt_test_runner = lambda *args, **kwargs: calls.append(kwargs)

    response = TestClient(app).post(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test",
        json={"draft_version_id": detail.draft_version.id, "raw_message_ids": [raw_id]},
    )

    assert response.status_code == 422
    assert "has no usable model" in response.json()["detail"]
    assert calls == []


def test_test_models_endpoint_lists_the_stage_chain_with_the_head_as_default(tmp_path):
    app = _app(tmp_path, "head-model", "second-model")
    response = TestClient(app).get(
        f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test-models"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stage_key"] == "authoritative_recognition"
    assert [item["id"] for item in payload["items"]] == ["head-model", "second-model"]
    assert [item["is_default"] for item in payload["items"]] == [True, False]


def test_test_models_endpoint_offers_the_image_prompt_only_image_models(tmp_path):
    app = _app(
        tmp_path,
        "sees-images",
        "text-only",
        models=[
            {"id": "sees-images", "supports_image": True},
            {"id": "text-only", "supports_image": False},
        ],
    )
    client = TestClient(app)

    vision = client.get(f"/api/ai-prompts/{MIMO_VISION_PROMPT}/test-models").json()
    shared = client.get(f"/api/ai-prompts/{SHARED_TRADING_PROMPT}/test-models").json()

    assert [item["id"] for item in vision["items"]] == ["sees-images"]
    # The text-only model is filtered out of the authoritative chain itself,
    # so neither prompt offers it -- the stage requires image support.
    assert [item["id"] for item in shared["items"]] == ["sees-images"]


def test_test_models_endpoint_refuses_a_non_trading_prompt(tmp_path):
    app = _app(tmp_path, "head-model")
    response = TestClient(app).get(
        "/api/ai-prompts/research.chat.group/test-models"
    )

    assert response.status_code in (404, 422)
