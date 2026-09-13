"""Schema v2 of ``config/ai_recognition.yaml``: providers, models, stages.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §3/§3.1.
"""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from telegram_kol_research.ai_recognition_config import (
    AI_CONFIG_SCHEMA_VERSION,
    AI_STAGE_DEFINITIONS,
    AI_STAGE_KEYS,
    AiModel,
    AiModelConfig,
    AiProvider,
    AiProviderConfig,
    AiRecognitionConfig,
    AiRecognitionConfigValidationError,
    build_ai_config_view,
    load_ai_recognition_config,
    migrate_v1_ai_config,
    resolve_stage_models,
    save_ai_recognition_config,
)
from telegram_kol_research.cli import app as cli_app


EXAMPLE_CONFIG = Path("config/ai_recognition.example.yaml")


def _write_v1(path: Path) -> Path:
    path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _load(path: Path) -> AiRecognitionConfig:
    with pytest.warns(DeprecationWarning):
        return load_ai_recognition_config(path)


# ---------------------------------------------------------------------------
# The stage catalogue
# ---------------------------------------------------------------------------


def test_stage_catalogue_matches_the_design_table():
    assert AI_STAGE_KEYS == (
        "authoritative_recognition",
        "context_resolution",
        "semantic_review",
        "strategy_alert",
        "research_chat",
        "batch_text_recognition",
        "batch_image_recognition",
    )
    by_key = {item.stage_key: item for item in AI_STAGE_DEFINITIONS}
    authoritative = by_key["authoritative_recognition"]
    assert authoritative.requires_text and authoritative.requires_image
    assert authoritative.capability_label == "文本 + 图片"
    assert authoritative.production_path is True
    image = by_key["batch_image_recognition"]
    assert image.requires_image and not image.requires_text
    assert image.capability_label == "图片"
    assert image.production_path is False
    for text_stage in (
        "context_resolution",
        "semantic_review",
        "strategy_alert",
        "research_chat",
        "batch_text_recognition",
    ):
        assert by_key[text_stage].requires_text
        assert not by_key[text_stage].requires_image
    assert by_key["strategy_alert"].env_fallback
    assert by_key["research_chat"].env_fallback


def test_runtime_incident_agent_is_deliberately_not_a_stage():
    assert "runtime_incident_agent" not in AI_STAGE_KEYS


# ---------------------------------------------------------------------------
# §3.1 migration
# ---------------------------------------------------------------------------


def test_example_v1_config_migrates_every_stage_to_production_behaviour(tmp_path):
    config = _load(_write_v1(tmp_path / "ai_recognition.yaml"))

    assert {provider.id for provider in config.providers} == {
        "deepseek",
        "zhipu",
        "mimo",
    }
    assert {model.id for model in config.models} == {
        "deepseek-v4-flash",
        "glm-ocr",
        "mimo-v2.5",
    }
    assert config.stages == {
        "authoritative_recognition": ["mimo-v2.5"],
        "context_resolution": ["deepseek-v4-flash"],
        "semantic_review": ["deepseek-v4-flash"],
        "strategy_alert": [],
        "research_chat": [],
        "batch_text_recognition": ["deepseek-v4-flash"],
        "batch_image_recognition": ["glm-ocr"],
    }


def test_authoritative_stage_follows_find_mimo_model_not_active_image_model(tmp_path):
    """The v1 file says the image model is GLM-OCR; production calls MiMo."""

    config = _load(_write_v1(tmp_path / "ai_recognition.yaml"))

    assert config.active_image_model_id == "glm-ocr"
    assert config.stages["authoritative_recognition"] == ["mimo-v2.5"]
    chain = resolve_stage_models(config, "authoritative_recognition")
    assert [model.model for model in chain] == ["mimo-v2.5"]
    assert chain[0].base_url == "https://api.xiaomimimo.com/v1"


def test_migration_maps_a_mimo_entry_found_by_model_name(tmp_path):
    """``_find_mimo_model`` matches on id **or** model name; so does migration."""

    _providers, _models, stages = migrate_v1_ai_config(
        [
            AiModelConfig(
                id="house-multimodal",
                label="House",
                base_url="https://api.xiaomimimo.com/v1",
                api_key="k",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
            )
        ],
        active_text_model_id="house-multimodal",
    )

    assert stages["authoritative_recognition"] == ["house-multimodal"]


def test_migration_is_idempotent_through_the_derived_v1_view(tmp_path):
    config = _load(_write_v1(tmp_path / "ai_recognition.yaml"))

    again = migrate_v1_ai_config(
        config.ai_models,
        active_text_model_id=config.active_text_model_id,
        active_image_model_id=config.active_image_model_id,
        context_resolution_model_id=config.context_resolution_model_id,
    )

    assert again[0] == config.providers
    assert again[1] == config.models
    assert again[2] == config.stages


def test_loading_a_v1_file_never_rewrites_it(tmp_path):
    path = _write_v1(tmp_path / "ai_recognition.yaml")
    before = path.read_bytes()

    _load(path)

    assert path.read_bytes() == before
    assert "schema_version" not in yaml.safe_load(before.decode("utf-8"))


def test_v1_derived_view_is_unchanged_by_the_migration(tmp_path):
    config = _load(_write_v1(tmp_path / "ai_recognition.yaml"))

    assert config.active_text_model_id == "deepseek-v4-flash"
    assert config.active_image_model_id == "glm-ocr"
    assert config.context_resolution_model_id == "deepseek-v4-flash"
    assert config.text_provider.model == "deepseek-v4-flash"
    assert config.text_provider.base_url == "https://api.deepseek.com"
    assert config.image_provider.model == "glm-ocr"
    assert [model.id for model in config.ai_models] == [
        "deepseek-v4-flash",
        "glm-ocr",
        "mimo-v2.5",
    ]


def test_two_keys_on_one_host_become_two_providers():
    providers, models, _stages = migrate_v1_ai_config(
        [
            AiModelConfig(
                id="a",
                label="A",
                base_url="https://api.deepseek.com",
                api_key="first",
                model="deepseek-v4-flash",
            ),
            AiModelConfig(
                id="b",
                label="B",
                base_url="https://api.deepseek.com",
                api_key="second",
                model="deepseek-reasoner",
            ),
        ],
        active_text_model_id="a",
    )

    assert [provider.id for provider in providers] == ["deepseek", "deepseek-2"]
    assert [model.provider_id for model in models] == ["deepseek", "deepseek-2"]


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_save_writes_schema_v2_and_reloads_identically(tmp_path):
    path = tmp_path / "ai_recognition.yaml"
    loaded = _load(_write_v1(path))

    saved = save_ai_recognition_config(path, loaded)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    reloaded = _load(path)

    assert raw["schema_version"] == AI_CONFIG_SCHEMA_VERSION
    assert raw["stages"]["authoritative_recognition"] == ["mimo-v2.5"]
    assert reloaded.providers == saved.providers
    assert reloaded.models == saved.models
    assert reloaded.stages == saved.stages
    assert reloaded.ai_models == saved.ai_models
    assert reloaded.text_provider == saved.text_provider
    assert reloaded.image_provider == saved.image_provider
    assert reloaded.active_text_model_id == saved.active_text_model_id
    assert reloaded.active_image_model_id == saved.active_image_model_id
    assert reloaded.context_resolution_model_id == saved.context_resolution_model_id


def test_saved_file_keeps_the_v1_keys_as_a_rollback_mirror(tmp_path):
    path = tmp_path / "ai_recognition.yaml"
    save_ai_recognition_config(path, _load(_write_v1(path)))

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["active_text_model_id"] == "deepseek-v4-flash"
    assert raw["text_provider"]["model"] == "deepseek-v4-flash"
    assert {model["id"] for model in raw["ai_models"]} == {
        "deepseek-v4-flash",
        "glm-ocr",
        "mimo-v2.5",
    }


def test_saving_a_two_model_chain_round_trips_the_order(tmp_path):
    path = tmp_path / "ai_recognition.yaml"
    loaded = _load(_write_v1(path))
    with_backup = replace(
        loaded,
        models=loaded.models
        + [
            AiModel(
                id="backup-vision",
                provider_id="deepseek",
                model="deepseek-vision",
                label="Backup vision",
                supports_text=True,
                supports_image=True,
            )
        ],
        stages={
            **loaded.stages,
            "authoritative_recognition": ["mimo-v2.5", "backup-vision"],
        },
    )

    save_ai_recognition_config(path, with_backup)
    reloaded = _load(path)

    assert reloaded.stages["authoritative_recognition"] == [
        "mimo-v2.5",
        "backup-vision",
    ]
    assert [
        model.id for model in resolve_stage_models(reloaded, "authoritative_recognition")
    ] == ["mimo-v2.5", "backup-vision"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _minimal_v2(**overrides) -> AiRecognitionConfig:
    base = dict(
        providers=[
            AiProvider(id="mimo", base_url="https://api.xiaomimimo.com/v1", api_key="k")
        ],
        models=[
            AiModel(
                id="mimo-v2.5",
                provider_id="mimo",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
            )
        ],
        stages={"authoritative_recognition": ["mimo-v2.5"]},
    )
    base.update(overrides)
    return AiRecognitionConfig(**base)


def test_save_refuses_a_stage_member_that_cannot_serve_the_stage(tmp_path):
    config = _minimal_v2(
        models=[
            AiModel(
                id="text-only",
                provider_id="mimo",
                model="text-only",
                supports_text=True,
                supports_image=False,
            )
        ],
        stages={"authoritative_recognition": ["text-only"]},
    )

    with pytest.raises(AiRecognitionConfigValidationError) as error:
        save_ai_recognition_config(tmp_path / "ai.yaml", config)

    assert "authoritative_recognition" in str(error.value)
    assert not (tmp_path / "ai.yaml").exists()


def test_save_refuses_a_model_on_an_unknown_provider(tmp_path):
    config = _minimal_v2(
        models=[AiModel(id="ghost", provider_id="nowhere", model="ghost")],
        stages={},
    )

    with pytest.raises(AiRecognitionConfigValidationError) as error:
        save_ai_recognition_config(tmp_path / "ai.yaml", config)

    assert "nowhere" in str(error.value)


def test_save_refuses_duplicate_ids(tmp_path):
    config = _minimal_v2(
        providers=[
            AiProvider(id="mimo", base_url="https://a.example.com"),
            AiProvider(id="mimo", base_url="https://b.example.com"),
        ],
        stages={},
    )

    with pytest.raises(AiRecognitionConfigValidationError) as error:
        save_ai_recognition_config(tmp_path / "ai.yaml", config)

    assert "duplicate provider id" in str(error.value)


def test_load_skips_a_broken_stage_member_and_warns_instead_of_raising(tmp_path):
    path = tmp_path / "ai_recognition.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "mode": "ai_provider",
                "providers": [
                    {
                        "id": "mimo",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "api_key": "k",
                    }
                ],
                "models": [
                    {
                        "id": "mimo-v2.5",
                        "provider_id": "mimo",
                        "model": "mimo-v2.5",
                        "supports_text": True,
                        "supports_image": True,
                    }
                ],
                "stages": {
                    "authoritative_recognition": ["mimo-v2.5", "does-not-exist"],
                    "not_a_stage": ["mimo-v2.5"],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    config = load_ai_recognition_config(path)

    assert config.stages["authoritative_recognition"] == ["mimo-v2.5"]
    assert "not_a_stage" not in config.stages
    assert any("does-not-exist" in message for message in config.config_warnings)
    assert any("not_a_stage" in message for message in config.config_warnings)


def test_disabled_members_stay_bound_but_do_not_route(tmp_path):
    config = _minimal_v2(
        providers=[
            AiProvider(id="mimo", base_url="https://api.xiaomimimo.com/v1", api_key="k"),
            AiProvider(
                id="spare",
                base_url="https://spare.example.com",
                api_key="k",
                enabled=False,
            ),
        ],
        models=[
            AiModel(
                id="mimo-v2.5",
                provider_id="mimo",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
                enabled=False,
            ),
            AiModel(
                id="spare-vision",
                provider_id="spare",
                model="spare-vision",
                supports_text=True,
                supports_image=True,
            ),
        ],
        stages={"authoritative_recognition": ["mimo-v2.5", "spare-vision"]},
    )

    path = tmp_path / "ai.yaml"
    saved = save_ai_recognition_config(path, config)
    reloaded = load_ai_recognition_config(path)

    assert saved.stages["authoritative_recognition"] == ["mimo-v2.5", "spare-vision"]
    assert reloaded.stages["authoritative_recognition"] == [
        "mimo-v2.5",
        "spare-vision",
    ]
    assert resolve_stage_models(reloaded, "authoritative_recognition") == []


def test_a_provider_without_a_base_url_does_not_route(tmp_path):
    config = _minimal_v2(
        providers=[AiProvider(id="mimo", base_url="", api_key="")],
    )

    assert resolve_stage_models(config, "authoritative_recognition") == []


# ---------------------------------------------------------------------------
# The v1 fields still steer their own stage
# ---------------------------------------------------------------------------


def test_a_v1_shaped_save_keeps_fallbacks_it_could_not_express(tmp_path):
    path = tmp_path / "ai_recognition.yaml"
    loaded = _load(_write_v1(path))
    save_ai_recognition_config(
        path,
        replace(
            loaded,
            models=loaded.models
            + [
                AiModel(
                    id="backup-vision",
                    provider_id="deepseek",
                    model="deepseek-vision",
                    supports_text=True,
                    supports_image=True,
                )
            ],
            stages={
                **loaded.stages,
                "authoritative_recognition": ["mimo-v2.5", "backup-vision"],
            },
        ),
    )

    # What the old ``POST /api/ai-recognition-config`` form sends: v1 fields
    # only, no stages at all.
    legacy = _load(path)
    save_ai_recognition_config(
        path,
        AiRecognitionConfig(
            mode="ai_provider",
            text_provider=legacy.text_provider,
            image_provider=legacy.image_provider,
            ai_models=legacy.ai_models,
            active_text_model_id="deepseek-v4-flash",
            active_image_model_id="glm-ocr",
            context_resolution_model_id="deepseek-v4-flash",
        ),
    )

    after = _load(path)
    assert after.stages["authoritative_recognition"] == ["mimo-v2.5", "backup-vision"]
    assert after.active_text_model_id == "deepseek-v4-flash"
    assert after.active_image_model_id == "glm-ocr"


def test_replacing_the_context_model_id_promotes_it_to_the_chain_head(tmp_path):
    """``context_authority_cutover`` changes the model exactly this way."""

    path = tmp_path / "ai_recognition.yaml"
    loaded = _load(_write_v1(path))

    saved = save_ai_recognition_config(
        tmp_path / "candidate.yaml",
        replace(loaded, context_resolution_model_id="mimo-v2.5"),
    )

    assert saved.stages["context_resolution"][0] == "mimo-v2.5"
    assert saved.context_resolution_model_id == "mimo-v2.5"
    assert "deepseek-v4-flash" in saved.stages["context_resolution"]


def test_a_v1_shaped_save_is_never_refused_for_a_binding_it_cannot_express(tmp_path):
    """The old form has no way to fix a stage; refusing it would be a 500."""

    path = tmp_path / "ai_recognition.yaml"

    saved = save_ai_recognition_config(
        path,
        AiRecognitionConfig(
            mode="ai_provider",
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    # The person unticked "image" on the model list; the
                    # authoritative stage needs it.
                    supports_image=False,
                )
            ],
            active_text_model_id="mimo-v2.5",
        ),
    )

    assert saved.stages["authoritative_recognition"] == []
    assert path.exists()


def test_an_id_the_old_form_allowed_still_saves(tmp_path):
    """Model ids were never validated; upper case is somebody's real config."""

    path = tmp_path / "ai_recognition.yaml"

    saved = save_ai_recognition_config(
        path,
        AiRecognitionConfig(
            mode="ai_provider",
            ai_models=[
                AiModelConfig(
                    id="Qwen-Max",
                    label="Qwen Max",
                    base_url="https://api.example.com/v1",
                    api_key="k",
                    model="qwen-max",
                    supports_text=True,
                )
            ],
            active_text_model_id="Qwen-Max",
        ),
    )

    assert {model.id for model in saved.models} >= {"Qwen-Max"}
    assert saved.stages["batch_text_recognition"] == ["Qwen-Max"]
    assert _load(path).stages["batch_text_recognition"] == ["Qwen-Max"]


def test_an_id_that_cannot_be_a_key_is_repaired_rather_than_dropped(tmp_path):
    path = tmp_path / "ai_recognition.yaml"

    saved = save_ai_recognition_config(
        path,
        AiRecognitionConfig(
            mode="ai_provider",
            ai_models=[
                AiModelConfig(
                    id="我的 模型!",
                    label="Mine",
                    base_url="https://api.example.com/v1",
                    api_key="k",
                    model="mine-v1",
                    supports_text=True,
                )
            ],
            active_text_model_id="我的 模型!",
        ),
    )

    repaired = [
        model.id
        for model in saved.models
        if model.model == "mine-v1"
    ]
    assert len(repaired) == 1
    assert saved.stages["batch_text_recognition"] == repaired


# ---------------------------------------------------------------------------
# Masked view and CLI
# ---------------------------------------------------------------------------


def test_config_view_masks_api_keys(tmp_path):
    config = _load(_write_v1(tmp_path / "ai_recognition.yaml"))

    view = build_ai_config_view(config)

    serialized = yaml.safe_dump(view, allow_unicode=True)
    assert "your_xiaomi_mimo_api_key" not in serialized
    mimo = next(item for item in view["providers"] if item["id"] == "mimo")
    assert mimo["api_key_configured"] is True
    assert mimo["api_key_last4"] == "_key"
    assert "api_key" not in mimo
    assert view["effective"]["authoritative_recognition"][0]["role"] == "主用"


def test_ai_config_show_prints_the_effective_chain(tmp_path):
    path = _write_v1(tmp_path / "ai_recognition.yaml")

    result = CliRunner().invoke(
        cli_app, ["ai-config-show", "--ai-config-path", str(path)]
    )

    assert result.exit_code == 0, result.output
    assert "authoritative_recognition" in result.output
    assert "主用: mimo-v2.5" in result.output
    assert "your_xiaomi_mimo_api_key" not in result.output
    assert "****_key" in result.output


def test_ai_config_show_json_is_machine_readable(tmp_path):
    import json

    path = _write_v1(tmp_path / "ai_recognition.yaml")

    result = CliRunner().invoke(
        cli_app, ["ai-config-show", "--ai-config-path", str(path), "--json"]
    )

    payload = json.loads(result.output)
    assert payload["stages"]["authoritative_recognition"] == ["mimo-v2.5"]
    assert payload["schema_version"] == AI_CONFIG_SCHEMA_VERSION


def test_a_hand_built_config_keeps_exactly_what_it_was_given():
    """The v1 fields are plain fields: nothing derives over a caller's value."""

    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.com", model="x", api_key="k"
        ),
        active_text_model_id="x",
    )

    assert config.text_provider.model == "x"
    assert config.active_text_model_id == "x"
    assert config.providers == []
    assert config.stages == {}
