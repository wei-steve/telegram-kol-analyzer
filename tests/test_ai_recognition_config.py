from telegram_kol_research.ai_recognition_config import (
    DEFAULT_LIFECYCLE_EVENT_PROMPT,
    DEFAULT_MIMO_DIRECT_PROMPT,
    DEFAULT_RECOGNITION_PROMPT,
    AiModelConfig,
    AiProviderConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
    save_ai_recognition_config,
)


def test_load_ai_recognition_config_uses_defaults_when_file_is_missing(tmp_path):
    config = load_ai_recognition_config(tmp_path / "missing.yaml")

    assert config.mode == "local_rule_parser"
    assert config.recognition_prompt == DEFAULT_RECOGNITION_PROMPT
    assert config.lifecycle_event_prompt == DEFAULT_LIFECYCLE_EVENT_PROMPT
    assert config.mimo_direct_prompt.startswith(DEFAULT_MIMO_DIRECT_PROMPT)
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt


def test_save_and_load_ai_recognition_config_round_trips_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="只识别明确包含进场、止损、止盈的消息。",
            lifecycle_event_prompt="Decide lifecycle events from context.",
            mimo_direct_prompt="Read images directly.",
            mode="local_rule_parser",
        ),
    )

    config = load_ai_recognition_config(config_path)

    assert config.recognition_prompt.startswith("只识别明确包含进场、止损、止盈的消息。")
    assert config.lifecycle_event_prompt == "Decide lifecycle events from context."
    assert config.mimo_direct_prompt.startswith("Read images directly.")
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt
    assert config.mode == "local_rule_parser"


def test_load_ai_recognition_config_upgrades_existing_normalized_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text(
        "\n".join(
            [
                "recognition_prompt: |-",
                "  \u53ea\u8bc6\u522b\u660e\u786e\u7b56\u7565\u3002",
                "  ",
                "  \u3010\u7b56\u7565\u5b57\u6bb5\u7edf\u4e00\u683c\u5f0f\u3011",
                "  - strategy.entry \u5fc5\u987b\u662f\u5b57\u7b26\u4e32\u3002",
            ]
        ),
        encoding="utf-8",
    )

    config = load_ai_recognition_config(config_path)

    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.recognition_prompt


def test_load_ai_recognition_config_upgrades_mimo_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text("mimo_direct_prompt: Read images directly.\n", encoding="utf-8")

    config = load_ai_recognition_config(config_path)

    assert config.mimo_direct_prompt.startswith("Read images directly.")
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt


def test_load_ai_recognition_config_seeds_models_from_legacy_providers(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text(
        "\n".join(
            [
                "mode: ai_provider",
                "text_provider:",
                "  base_url: https://api.deepseek.com",
                "  api_key: deepseek-key",
                "  model: deepseek-v4-flash",
                "image_provider:",
                "  base_url: https://api.xiaomimimo.com/v1",
                "  api_key: mimo-key",
                "  model: mimo-v2.5",
            ]
        ),
        encoding="utf-8",
    )

    config = load_ai_recognition_config(config_path)

    models_by_id = {model.id: model for model in config.ai_models}
    assert {"deepseek-v4-flash", "glm-ocr", "mimo-v2.5"}.issubset(models_by_id)
    assert models_by_id["deepseek-v4-flash"].api_key == "deepseek-key"
    assert models_by_id["mimo-v2.5"].api_key == "mimo-key"
    assert config.active_text_model_id == "deepseek-v4-flash"
    assert config.active_image_model_id == "mimo-v2.5"
    assert config.image_provider.model == "mimo-v2.5"


def test_save_ai_recognition_config_uses_active_model_selection(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"

    config = save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="Only strict strategies.",
            mode="ai_provider",
            text_provider=AiProviderConfig(),
            image_provider=AiProviderConfig(),
            active_text_model_id="deepseek-v4-flash",
            active_image_model_id="mimo-v2.5",
            ai_models=[
                AiModelConfig(
                    id="deepseek-v4-flash",
                    label="DeepSeek V4 Flash",
                    base_url="https://api.deepseek.com",
                    api_key="deepseek-key",
                    model="deepseek-v4-flash",
                    supports_text=True,
                    supports_image=False,
                ),
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                ),
            ],
        ),
    )

    assert config.text_provider.model == "deepseek-v4-flash"
    assert config.text_provider.api_key == "deepseek-key"
    assert config.image_provider.model == "mimo-v2.5"
    assert config.image_provider.api_key == "mimo-key"

    reloaded = load_ai_recognition_config(config_path)
    assert reloaded.active_text_model_id == "deepseek-v4-flash"
    assert reloaded.active_image_model_id == "mimo-v2.5"
