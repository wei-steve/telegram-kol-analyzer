"""Asking a provider which models its key can actually call.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.2.

The preset catalogue says what models.dev knew on a given day; this says what
this account can reach now, which is the only answer that matters before
binding a model to a production stage. Every failure has to come back named --
"your key is wrong" and "this host does not answer" are different problems --
rather than as an exception the page turns into "保存失败".
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from telegram_kol_research.ai_endpoints import models_url
from telegram_kol_research.ai_provider_models import (
    ANTHROPIC_VERSION,
    MODEL_LIST_TIMEOUT_SECONDS,
    ProviderModelListing,
    list_provider_models,
)
from telegram_kol_research.ai_stage_catalog import AiProvider
from telegram_kol_research.web_app import create_web_app


def _client(tmp_path, **kwargs) -> TestClient:
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text(
        Path("tests/fixtures/ai_recognition_v1_sample.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            ai_recognition_config_path=config_path,
            **kwargs,
        )
    )





class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://api.example.com/v1/models")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}'",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeClient:
    def __init__(self, response=None, error=None, seen=None, timeout=None):
        self._response = response
        self._error = error
        self._seen = seen if seen is not None else {}
        self._seen["timeout"] = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self._seen["url"] = url
        self._seen["headers"] = dict(headers or {})
        if self._error is not None:
            raise self._error
        return self._response


def _factory(response=None, error=None, seen=None):
    def build(timeout=None):
        return _FakeClient(response=response, error=error, seen=seen, timeout=timeout)

    return build


def test_a_model_listing_reads_the_openai_shape():
    seen: dict = {}
    provider = AiProvider(
        id="acme", base_url="https://api.groq.com/openai/v1", api_key="sk-acme"
    )

    listing = list_provider_models(
        provider,
        client_factory=_factory(
            _FakeResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": "b-model", "owned_by": "acme"},
                        {"id": "a-model"},
                        {"id": "a-model"},
                    ],
                }
            ),
            seen=seen,
        ),
    )

    assert listing.ok
    assert [item["id"] for item in listing.models] == ["a-model", "b-model"]
    assert seen["url"] == models_url("https://api.groq.com/openai/v1")
    assert seen["timeout"] == MODEL_LIST_TIMEOUT_SECONDS


def test_the_anthropic_headers_travel_with_every_request():
    seen: dict = {}

    list_provider_models(
        AiProvider(id="anthropic", base_url="https://api.anthropic.com/v1", api_key="k"),
        client_factory=_factory(_FakeResponse({"data": [{"id": "claude"}]}), seen=seen),
    )

    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["headers"]["x-api-key"] == "k"
    assert seen["headers"]["anthropic-version"] == ANTHROPIC_VERSION


def test_a_keyless_local_runtime_sends_no_authorization():
    seen: dict = {}

    list_provider_models(
        AiProvider(id="ollama", base_url="http://127.0.0.1:11434/v1", api_key=""),
        client_factory=_factory(_FakeResponse({"data": [{"id": "llama"}]}), seen=seen),
    )

    assert "Authorization" not in seen["headers"]
    assert "x-api-key" not in seen["headers"]


def test_a_rejected_key_is_named_not_raised():
    listing = list_provider_models(
        AiProvider(id="acme", base_url="https://api.example.com/v1", api_key="bad"),
        client_factory=_factory(_FakeResponse({}, status_code=401)),
    )

    assert listing.ok is False
    assert listing.failure_class == "provider_unavailable"
    assert listing.kind == "auth_rejected"
    assert listing.http_status == 401


def test_an_unreachable_host_is_named_not_raised():
    listing = list_provider_models(
        AiProvider(id="acme", base_url="http://127.0.0.1:9/v1", api_key="k"),
        client_factory=_factory(error=httpx.ConnectError("refused")),
    )

    assert listing.failure_class == "provider_unavailable"
    assert listing.kind == "network_error"


def test_an_empty_listing_is_a_failure_not_an_empty_page():
    listing = list_provider_models(
        AiProvider(id="acme", base_url="https://api.example.com/v1", api_key="k"),
        client_factory=_factory(_FakeResponse({"data": []})),
    )

    assert listing.ok is False
    assert listing.failure_class == "response_invalid"


def test_a_provider_without_a_base_url_is_not_called():
    listing = list_provider_models(AiProvider(id="acme", base_url=""))

    assert listing.ok is False
    assert listing.failure_class == "not_configured"


def test_the_model_api_annotates_capability_from_the_preset_catalogue(tmp_path):
    client = _client(
        tmp_path,
        ai_model_lister=lambda provider: ProviderModelListing(
            models=[
                {"id": "mimo-v2.5", "owned_by": "xiaomi"},
                {"id": "totally-unknown", "owned_by": ""},
            ]
        ),
    )

    payload = client.post("/api/ai-providers/mimo/models", json={}).json()

    by_id = {item["id"]: item for item in payload["models"]}
    assert by_id["mimo-v2.5"]["supports_image"] is True
    assert by_id["totally-unknown"]["supports_image"] is False
    assert payload["error"] is None


def test_the_model_api_reports_a_failure_class_instead_of_a_500(tmp_path):
    client = _client(
        tmp_path,
        ai_model_lister=lambda provider: ProviderModelListing(
            error="Client error '401'",
            failure_class="provider_unavailable",
            http_status=401,
        ),
    )

    response = client.post("/api/ai-providers/deepseek/models", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"] == []
    assert payload["failure_class"] == "provider_unavailable"
    assert payload["http_status"] == 401


def test_the_model_api_refuses_an_unknown_provider(tmp_path):
    client = _client(tmp_path, ai_model_lister=lambda provider: ProviderModelListing())

    assert (
        client.post("/api/ai-providers/nowhere/models", json={}).status_code == 404
    )


def test_the_model_api_uses_the_stored_key_without_returning_it(tmp_path):
    seen: dict = {}

    def lister(provider):
        seen["api_key"] = provider.api_key
        seen["base_url"] = provider.base_url
        return ProviderModelListing(models=[{"id": "deepseek-chat", "owned_by": ""}])

    payload = _client(tmp_path, ai_model_lister=lister).post(
        "/api/ai-providers/deepseek/models", json={}
    ).json()

    assert seen["api_key"] == "your_deepseek_api_key"
    assert seen["base_url"] == "https://api.deepseek.com"
    assert "your_deepseek_api_key" not in str(payload)
