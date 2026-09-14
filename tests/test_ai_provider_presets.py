"""The shipped preset catalogue, and asking a provider what it can serve.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.1/§9.2.

The user's complaint was that three presets is not enough. These pin what the
catalogue must contain, that it is generated rather than hand-kept, and that
the live model listing names its failures instead of throwing them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from telegram_kol_research.ai_provider_presets import (
    PRESET_FILE,
    find_preset,
    load_provider_presets,
    preset_models_by_id,
)
from telegram_kol_research.web_app import create_web_app


# The §9.1 table, by group.
EXPECTED = {
    "domestic": {
        "deepseek": "https://api.deepseek.com",
        "zhipuai": "https://open.bigmodel.cn/api/paas/v4",
        "xiaomi": "https://api.xiaomimimo.com/v1",
        "alibaba-cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "moonshotai-cn": "https://api.moonshot.cn/v1",
        "siliconflow-cn": "https://api.siliconflow.cn/v1",
        "stepfun": "https://api.stepfun.com/v1",
        "minimax-cn": "https://api.minimaxi.com/v1",
        "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "international": {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "google": "https://generativelanguage.googleapis.com/v1beta/openai",
        "xai": "https://api.x.ai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "mistral": "https://api.mistral.ai/v1",
    },
    "local": {
        "ollama": "http://127.0.0.1:11434/v1",
        "lmstudio": "http://127.0.0.1:1234/v1",
    },
    "custom": {"custom": ""},
}


# ---------------------------------------------------------------------------
# The catalogue file
# ---------------------------------------------------------------------------


def test_the_catalogue_has_every_provider_the_design_lists():
    catalogue = load_provider_presets()

    by_group: dict[str, dict[str, str]] = {}
    for provider in catalogue["providers"]:
        by_group.setdefault(provider["group"], {})[provider["id"]] = provider["base_url"]

    assert by_group == EXPECTED
    assert catalogue["group_order"] == [
        "domestic",
        "international",
        "local",
        "custom",
    ]
    assert catalogue["group_labels"]["domestic"] == "国内"


#: The button text, verbatim from the §9.1 table. Brand names stay in Latin
#: script (DeepSeek, OpenAI, Groq); everything with a Chinese name uses it.
EXPECTED_LABELS = {
    "deepseek": "DeepSeek",
    "zhipuai": "智谱 GLM",
    "xiaomi": "小米 MiMo",
    "alibaba-cn": "阿里百炼（通义 Qwen）",
    "moonshotai-cn": "月之暗面 Kimi",
    "siliconflow-cn": "硅基流动",
    "stepfun": "阶跃星辰",
    "minimax-cn": "MiniMax",
    "volcengine": "火山方舟（豆包）",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google Gemini",
    "xai": "xAI Grok",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "mistral": "Mistral",
    "ollama": "Ollama（本机）",
    "lmstudio": "LM Studio（本机）",
    "custom": "自定义（OpenAI 兼容）",
}


def test_every_preset_carries_the_label_the_design_specifies():
    catalogue = load_provider_presets()

    assert catalogue["generated_at"]
    assert "models.dev" in catalogue["source"]
    assert {
        provider["id"]: provider["label"] for provider in catalogue["providers"]
    } == EXPECTED_LABELS


def test_a_local_runtime_needs_no_key_and_ships_no_guessed_models():
    for preset_id in ("ollama", "lmstudio"):
        preset = find_preset(preset_id)
        assert preset["requires_api_key"] is False
        # Whatever a person downloaded locally is not something we can guess.
        assert preset["models"] == []
    assert find_preset("deepseek")["requires_api_key"] is True


def test_no_provider_ships_more_than_the_cap():
    catalogue = load_provider_presets()
    cap = catalogue["max_models_per_provider"]

    assert cap == 8
    for provider in catalogue["providers"]:
        assert len(provider["models"]) <= cap, provider["id"]


def test_preset_models_are_chat_models_with_a_capability_flag():
    catalogue = load_provider_presets()

    for provider in catalogue["providers"]:
        for model in provider["models"]:
            assert model["id"].strip()
            assert model["supports_text"] is True
            assert isinstance(model["supports_image"], bool)
            assert not any(
                marker in model["id"].lower()
                for marker in ("tts", "whisper", "embedding", "realtime", "dall-e")
            ), model["id"]


def test_the_big_domestic_providers_carry_a_multimodal_model():
    """The authoritative stage needs text+image, so a preset has to offer one."""

    for preset_id in ("alibaba-cn", "zhipuai", "xiaomi"):
        preset = find_preset(preset_id)
        assert any(model["supports_image"] for model in preset["models"]), preset_id


def test_the_model_index_answers_capability_by_id():
    index = preset_models_by_id()

    assert index["mimo-v2.5"]["supports_image"] is True
    assert "deepseek" in {
        provider["id"] for provider in load_provider_presets()["providers"]
    }


def test_an_unreadable_catalogue_still_offers_the_custom_preset(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")

    catalogue = load_provider_presets(broken)

    assert [item["id"] for item in catalogue["providers"]] == ["custom"]


def test_the_catalogue_is_generated_not_hand_maintained():
    """It must be reproducible from the script, or it will rot in place."""

    sys.path.insert(0, "scripts")
    try:
        import build_ai_provider_presets as builder
    finally:
        sys.path.pop(0)

    shipped = json.loads(PRESET_FILE.read_text(encoding="utf-8"))
    assert [preset.preset_id for preset in builder.PRESETS] == [
        provider["id"] for provider in shipped["providers"]
    ]
    assert builder.MAX_MODELS_PER_PROVIDER == shipped["max_models_per_provider"]


def test_the_generator_filters_and_orders_the_way_the_design_says():
    sys.path.insert(0, "scripts")
    try:
        import build_ai_provider_presets as builder
    finally:
        sys.path.pop(0)

    source = {
        "acme": {
            "name": "Acme",
            "doc": "https://acme.example/docs",
            "models": {
                "acme-tts-1": {
                    "name": "Acme TTS",
                    "release_date": "2026-09-01",
                    "modalities": {"input": ["text"], "output": ["audio"]},
                },
                "acme-embedding": {
                    "name": "Acme Embedding",
                    "release_date": "2026-09-01",
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "acme-vision": {
                    "name": "Acme Vision",
                    "release_date": "2026-08-01",
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                },
                "acme-old": {
                    "name": "Acme Old",
                    "release_date": "2024-01-01",
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "acme-new": {
                    "name": "Acme New",
                    "release_date": "2026-09-09",
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
            },
        }
    }

    picked = builder.select_models(source["acme"], limit=8)

    # Newest first; the audio and embedding models are gone by name.
    assert [item["id"] for item in picked] == ["acme-new", "acme-vision", "acme-old"]
    assert picked[1]["supports_image"] is True
    assert picked[0]["supports_image"] is False


def test_the_generator_honours_the_per_provider_cap():
    sys.path.insert(0, "scripts")
    try:
        import build_ai_provider_presets as builder
    finally:
        sys.path.pop(0)

    provider = {
        "models": {
            f"m-{index:02d}": {
                "name": f"M{index}",
                "release_date": f"2026-01-{index + 1:02d}",
                "modalities": {"input": ["text"], "output": ["text"]},
            }
            for index in range(20)
        }
    }

    assert len(builder.select_models(provider, limit=8)) == 8


# ---------------------------------------------------------------------------
# GET /api/ai-provider-presets
# ---------------------------------------------------------------------------


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


def test_the_preset_api_serves_the_whole_catalogue(tmp_path):
    payload = _client(tmp_path).get("/api/ai-provider-presets").json()

    assert payload["group_order"] == ["domestic", "international", "local", "custom"]
    ids = {provider["id"] for provider in payload["providers"]}
    assert {"alibaba-cn", "openai", "ollama", "custom"}.issubset(ids)
    assert len(payload["providers"]) == 19
    assert payload["generated_at"]


