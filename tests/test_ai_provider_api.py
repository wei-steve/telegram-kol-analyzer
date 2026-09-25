"""The two pages' APIs: providers and models, and the per-stage chains.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §5.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telegram_kol_research.mimo_provider_probe import ProbeOutcome
from telegram_kol_research.web_app import create_web_app


def _client(tmp_path, *, prober=None) -> TestClient:
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text(
        Path("tests/fixtures/ai_recognition_v1_sample.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            ai_recognition_config_path=config_path,
            ai_provider_prober=prober,
        )
    )


# ---------------------------------------------------------------------------
# GET / PUT /api/ai-providers
# ---------------------------------------------------------------------------


def test_providers_are_listed_with_their_models_and_no_keys(tmp_path):
    payload = _client(tmp_path).get("/api/ai-providers").json()

    by_id = {item["id"]: item for item in payload["providers"]}
    assert set(by_id) == {"deepseek", "zhipu", "mimo"}
    assert by_id["mimo"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert by_id["mimo"]["api_key_configured"] is True
    assert by_id["mimo"]["api_key_last4"] == "_key"
    assert "api_key" not in by_id["mimo"]
    models = {item["id"]: item for item in payload["models"]}
    assert models["mimo-v2.5"]["provider_id"] == "mimo"
    assert models["mimo-v2.5"]["supports_image"] is True


def test_an_empty_key_keeps_the_stored_one(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    response = client.put(
        "/api/ai-providers",
        json={
            "providers": [
                {**provider, "api_key": "", "label": f"{provider['label']} 改"}
                for provider in current["providers"]
            ],
            "models": current["models"],
        },
    )

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["providers"]}
    assert by_id["mimo"]["api_key_configured"] is True
    assert by_id["mimo"]["api_key_last4"] == "_key"
    assert by_id["mimo"]["label"].endswith("改")


def test_a_new_key_replaces_the_stored_one(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()
    providers = [
        {**provider, "api_key": "sk-rotated-9876" if provider["id"] == "mimo" else ""}
        for provider in current["providers"]
    ]

    payload = client.put(
        "/api/ai-providers",
        json={"providers": providers, "models": current["models"]},
    ).json()

    by_id = {item["id"]: item for item in payload["providers"]}
    assert by_id["mimo"]["api_key_last4"] == "9876"


def test_the_append_v1_switch_round_trips_and_names_the_endpoint(tmp_path):
    """§10: the /v1 question is answered explicitly, and shown as a URL."""

    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()
    by_id = {item["id"]: item for item in current["providers"]}

    # A file written before the switch existed infers it, and the inferred
    # answer is what those providers have always used.
    assert by_id["deepseek"]["append_v1"] is True
    assert by_id["deepseek"]["chat_completions_url"] == (
        "https://api.deepseek.com/v1/chat/completions"
    )
    assert by_id["mimo"]["append_v1"] is False
    assert by_id["mimo"]["chat_completions_url"] == (
        "https://api.xiaomimimo.com/v1/chat/completions"
    )

    saved = client.put(
        "/api/ai-providers",
        json={
            "providers": [
                {**provider, "api_key": "", "append_v1": provider["id"] == "mimo"}
                for provider in current["providers"]
            ],
            "models": current["models"],
        },
    ).json()

    after = {item["id"]: item for item in saved["providers"]}
    assert after["mimo"]["append_v1"] is True
    # Already ends in /v1, so turning the switch on does not add a second one.
    assert after["mimo"]["chat_completions_url"] == (
        "https://api.xiaomimimo.com/v1/chat/completions"
    )
    assert after["deepseek"]["append_v1"] is False
    assert after["deepseek"]["chat_completions_url"] == (
        "https://api.deepseek.com/chat/completions"
    )
    # And it survives a reload rather than being re-inferred.
    reloaded = {
        item["id"]: item
        for item in client.get("/api/ai-providers").json()["providers"]
    }
    assert reloaded["deepseek"]["append_v1"] is False


def test_a_provider_saved_without_the_switch_gets_the_inferred_one(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    saved = client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"]
            + [
                {
                    "id": "bare",
                    "label": "Bare host",
                    "base_url": "https://api.newthing.com",
                    "api_key": "sk-bare",
                    "timeout_seconds": 60,
                    "enabled": True,
                }
            ],
            "models": current["models"],
        },
    ).json()

    bare = next(item for item in saved["providers"] if item["id"] == "bare")
    assert bare["append_v1"] is True
    assert bare["chat_completions_url"] == (
        "https://api.newthing.com/v1/chat/completions"
    )


def test_the_switch_reaches_the_model_that_calls_the_provider(tmp_path):
    """A chain member has to address the endpoint its provider asked for."""

    from telegram_kol_research.ai_model_router import resolve_stage_chain
    from telegram_kol_research.ai_endpoints import chat_completions_url
    from telegram_kol_research.ai_recognition_config import (
        load_ai_recognition_config,
    )

    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()
    client.put(
        "/api/ai-providers",
        json={
            "providers": [
                {**provider, "api_key": "", "append_v1": provider["id"] == "mimo"}
                for provider in current["providers"]
            ],
            "models": current["models"],
        },
    )

    with pytest.warns(DeprecationWarning):
        config = load_ai_recognition_config(
            tmp_path / "ai_recognition.yaml"
        )
    head = resolve_stage_chain(config, "authoritative_recognition")[0]

    assert head.append_v1 is True
    assert head.provider.append_v1 is True
    assert chat_completions_url(head.base_url, head.append_v1) == (
        "https://api.xiaomimimo.com/v1/chat/completions"
    )


def test_adding_a_provider_and_a_model_round_trips(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"]
            + [
                {
                    "id": "spare",
                    "label": "Spare",
                    "base_url": "https://spare.example.com/v1",
                    "api_key": "sk-spare",
                    "timeout_seconds": 45,
                    "enabled": True,
                }
            ],
            "models": current["models"]
            + [
                {
                    "id": "spare-vision",
                    "provider_id": "spare",
                    "model": "spare-vision",
                    "label": "Spare vision",
                    "supports_text": True,
                    "supports_image": True,
                    "enabled": True,
                }
            ],
        },
    )
    reloaded = client.get("/api/ai-providers").json()

    by_id = {item["id"]: item for item in reloaded["providers"]}
    assert by_id["spare"]["timeout_seconds"] == 45
    assert by_id["spare"]["api_key_last4"] == "pare"
    assert "spare-vision" in {item["id"] for item in reloaded["models"]}


def test_deleting_a_model_a_stage_still_uses_is_refused(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    response = client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"],
            "models": [
                item for item in current["models"] if item["id"] != "mimo-v2.5"
            ],
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "mimo-v2.5" in detail
    assert "authoritative_recognition" in detail
    # Nothing was written: the stage still calls the model it called before.
    assert client.get("/api/ai-stages").json()["stages"][
        "authoritative_recognition"
    ] == ["mimo-v2.5"]


def test_deleting_a_provider_a_stage_still_uses_is_refused(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    response = client.put(
        "/api/ai-providers",
        json={
            "providers": [
                item for item in current["providers"] if item["id"] != "mimo"
            ],
            "models": current["models"],
        },
    )

    assert response.status_code == 422
    assert "authoritative_recognition" in response.json()["detail"]


def test_a_model_on_an_unknown_provider_is_a_422_not_a_500(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    response = client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"],
            "models": current["models"]
            + [{"id": "ghost", "provider_id": "nowhere", "model": "ghost"}],
        },
    )

    assert response.status_code == 422
    assert "nowhere" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/ai-providers/{id}/test
# ---------------------------------------------------------------------------


def test_the_connection_test_reports_a_healthy_provider(tmp_path):
    client = _client(
        tmp_path,
        prober=lambda model_config: ProbeOutcome(
            ok=True, latency_ms=123, http_status=200
        ),
    )

    response = client.post(
        "/api/ai-providers/mimo/test", json={"model_id": "mimo-v2.5"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "http_status": 200,
        "latency_ms": 123,
        "failure_class": None,
        "kind": None,
        "error_type": None,
    }


def test_the_connection_test_names_why_an_unreachable_provider_failed(tmp_path):
    client = _client(
        tmp_path,
        prober=lambda model_config: ProbeOutcome(
            ok=False,
            latency_ms=41,
            http_status=None,
            failure_class="provider_unavailable",
            kind="network_error",
            error_type="ConnectError",
        ),
    )

    payload = client.post(
        "/api/ai-providers/mimo/test", json={"model_id": "mimo-v2.5"}
    ).json()

    assert payload["ok"] is False
    assert payload["failure_class"] == "provider_unavailable"
    assert payload["kind"] == "network_error"
    assert payload["error_type"] == "ConnectError"


def test_the_connection_test_refuses_a_model_of_another_provider(tmp_path):
    client = _client(tmp_path, prober=lambda model_config: ProbeOutcome(True, 1))

    assert (
        client.post(
            "/api/ai-providers/mimo/test", json={"model_id": "glm-ocr"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/ai-providers/nowhere/test", json={"model_id": "mimo-v2.5"}
        ).status_code
        == 404
    )


def test_the_connection_test_sends_the_stored_key_without_returning_it(tmp_path):
    seen = {}

    def prober(model_config):
        seen["api_key"] = model_config.api_key
        seen["base_url"] = model_config.base_url
        return ProbeOutcome(ok=True, latency_ms=5, http_status=200)

    payload = _client(tmp_path, prober=prober).post(
        "/api/ai-providers/mimo/test", json={"model_id": "mimo-v2.5"}
    ).json()

    assert seen["api_key"] == "your_xiaomi_mimo_api_key"
    assert seen["base_url"] == "https://api.xiaomimimo.com/v1"
    assert "your_xiaomi_mimo_api_key" not in str(payload)


# ---------------------------------------------------------------------------
# GET / PUT /api/ai-stages
# ---------------------------------------------------------------------------


def test_stages_report_the_catalogue_the_bindings_and_what_routes(tmp_path):
    payload = _client(tmp_path).get("/api/ai-stages").json()

    keys = [item["stage_key"] for item in payload["definitions"]]
    assert keys == [
        "authoritative_recognition",
        "context_resolution",
        "strategy_alert",
        "batch_text_recognition",
        "batch_image_recognition",
    ]
    authoritative = payload["definitions"][0]
    assert authoritative["capability_label"] == "文本 + 图片"
    assert authoritative["production_path"] is True
    assert payload["stages"]["authoritative_recognition"] == ["mimo-v2.5"]
    assert payload["effective"]["authoritative_recognition"][0]["role"] == "主用"
    assert payload["stages"]["strategy_alert"] == []
    assert payload["definitions"][2]["env_fallback"]


def test_a_fallback_can_be_added_and_reordered(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()
    client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"]
            + [
                {
                    "id": "spare",
                    "label": "Spare",
                    "base_url": "https://spare.example.com/v1",
                    "api_key": "sk-spare",
                    "timeout_seconds": 60,
                    "enabled": True,
                }
            ],
            "models": current["models"]
            + [
                {
                    "id": "spare-vision",
                    "provider_id": "spare",
                    "model": "spare-vision",
                    "supports_text": True,
                    "supports_image": True,
                    "enabled": True,
                }
            ],
        },
    )

    saved = client.put(
        "/api/ai-stages",
        json={
            "stages": {
                "authoritative_recognition": ["mimo-v2.5", "spare-vision"],
            }
        },
    ).json()

    assert saved["stages"]["authoritative_recognition"] == [
        "mimo-v2.5",
        "spare-vision",
    ]
    assert [item["role"] for item in saved["effective"]["authoritative_recognition"]] == [
        "主用",
        "备用 1",
    ]
    # Reading it back gives the same order.
    assert client.get("/api/ai-stages").json()["stages"][
        "authoritative_recognition"
    ] == ["mimo-v2.5", "spare-vision"]

    promoted = client.put(
        "/api/ai-stages",
        json={
            "stages": {
                "authoritative_recognition": ["spare-vision", "mimo-v2.5"],
            }
        },
    ).json()
    assert promoted["effective"]["authoritative_recognition"][0]["id"] == "spare-vision"


def test_a_stage_left_out_of_the_body_keeps_its_binding(tmp_path):
    client = _client(tmp_path)

    saved = client.put(
        "/api/ai-stages", json={"stages": {"strategy_alert": ["mimo-v2.5"]}}
    ).json()

    assert saved["stages"]["strategy_alert"] == ["mimo-v2.5"]
    assert saved["stages"]["authoritative_recognition"] == ["mimo-v2.5"]


def test_binding_a_model_that_cannot_serve_the_stage_is_a_422(tmp_path):
    client = _client(tmp_path)

    response = client.put(
        "/api/ai-stages",
        json={"stages": {"authoritative_recognition": ["deepseek-v4-flash"]}},
    )

    assert response.status_code == 422
    assert "authoritative_recognition" in response.json()["detail"]
    assert client.get("/api/ai-stages").json()["stages"][
        "authoritative_recognition"
    ] == ["mimo-v2.5"]


def test_a_malformed_stage_body_is_a_422(tmp_path):
    client = _client(tmp_path)

    assert client.put("/api/ai-stages", json={"stages": []}).status_code == 422
    assert (
        client.put(
            "/api/ai-stages", json={"stages": {"strategy_alert": 7}}
        ).status_code
        == 422
    )


def test_a_disabled_model_stays_bound_but_stops_routing(tmp_path):
    client = _client(tmp_path)
    current = client.get("/api/ai-providers").json()

    client.put(
        "/api/ai-providers",
        json={
            "providers": current["providers"],
            "models": [
                {**item, "enabled": item["id"] != "mimo-v2.5"}
                for item in current["models"]
            ],
        },
    )
    payload = client.get("/api/ai-stages").json()

    assert payload["stages"]["authoritative_recognition"] == ["mimo-v2.5"]
    assert payload["effective"]["authoritative_recognition"] == []


def test_the_worker_sees_a_saved_chain_without_a_restart(tmp_path):
    """The file is the contract; every process re-reads it per message."""

    from telegram_kol_research.ai_model_router import resolve_stage_chain
    from telegram_kol_research.ai_recognition_config import (
        load_ai_recognition_config,
    )

    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text(
        Path("tests/fixtures/ai_recognition_v1_sample.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            ai_recognition_config_path=config_path,
        )
    )

    client.put(
        "/api/ai-stages",
        json={"stages": {"strategy_alert": ["mimo-v2.5"]}},
    )

    with pytest.warns(DeprecationWarning):
        config = load_ai_recognition_config(config_path)
    assert [
        model.id for model in resolve_stage_chain(config, "strategy_alert")
    ] == ["mimo-v2.5"]
