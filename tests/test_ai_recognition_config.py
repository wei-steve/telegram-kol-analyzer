from telegram_kol_research.ai_recognition_config import (
    DEFAULT_RECOGNITION_PROMPT,
    AiRecognitionConfig,
    load_ai_recognition_config,
    save_ai_recognition_config,
)


def test_load_ai_recognition_config_uses_defaults_when_file_is_missing(tmp_path):
    config = load_ai_recognition_config(tmp_path / "missing.yaml")

    assert config.mode == "local_rule_parser"
    assert config.recognition_prompt == DEFAULT_RECOGNITION_PROMPT


def test_save_and_load_ai_recognition_config_round_trips_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="只识别明确包含进场、止损、止盈的消息。",
            mode="local_rule_parser",
        ),
    )

    config = load_ai_recognition_config(config_path)

    assert config.recognition_prompt == "只识别明确包含进场、止损、止盈的消息。"
    assert config.mode == "local_rule_parser"
