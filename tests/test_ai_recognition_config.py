from telegram_kol_research.ai_recognition_config import (
    DEFAULT_LIFECYCLE_EVENT_PROMPT,
    DEFAULT_MIMO_DIRECT_PROMPT,
    DEFAULT_RECOGNITION_PROMPT,
    AiModelConfig,
    AiProviderConfig,
    AiRecognitionConfig,
    build_authoritative_mimo_prompt,
    load_ai_recognition_config,
    save_ai_recognition_config,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.prompt_defaults import (
    DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
    DEFAULT_MIMO_VISION_PROMPT,
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    MIMO_VISION_PROMPT,
    SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
    SHARED_TRADING_PROMPT,
    build_prompt_seeds_from_legacy,
    seed_default_prompt_registry,
)
from telegram_kol_research.prompt_registry import get_prompt_detail


def test_default_trading_templates_have_strict_boundaries():
    assert '"recognition_result"' in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert '"lifecycle_event"' in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "58900-59300" in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "平加仓" in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "求稳可走" in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "图片模糊" not in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "DeepSeek" not in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "MiMo" not in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT

    assert "求稳可走" in DEFAULT_LIFECYCLE_EVENT_PROMPT
    assert '"targets": []' not in DEFAULT_LIFECYCLE_EVENT_PROMPT
    assert "只输出 target_lifecycle_id，不要输出 targets" in DEFAULT_LIFECYCLE_EVENT_PROMPT
    assert "非空 targets" in DEFAULT_LIFECYCLE_EVENT_PROMPT

    assert "图片模糊" in DEFAULT_MIMO_VISION_PROMPT
    assert "交易所截图" in DEFAULT_MIMO_VISION_PROMPT
    assert '"recognition_result"' not in DEFAULT_MIMO_VISION_PROMPT
    assert '"lifecycle_event"' not in DEFAULT_MIMO_VISION_PROMPT


def test_semantic_disagreement_review_prompt_has_closed_evidence_based_contract():
    prompt = DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT

    for marker in (
        '"independent_action"',
        '"action_type"',
        '"target_lifecycle_id"',
        '"symbol"',
        '"side"',
        '"stop_loss"',
        '"take_profit"',
        '"management_action"',
        '"evidence"',
        '"conflict_types"',
        '"material_disagreement"',
        '"suggested_severity"',
        '"confidence"',
        '"reason"',
    ):
        assert marker in prompt
    assert "独立解读当前消息" in prompt
    assert "必须引用当前消息中的证据" in prompt
    assert "不得修改交易" in prompt
    assert "不得声称能够读取图片像素" in prompt
    assert "none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update" in prompt
    assert "none | normal | critical" in prompt
    for conflict_type in (
        "actionability",
        "action_family",
        "full_vs_partial_exit",
        "symbol",
        "side",
        "target_lifecycle",
        "stop_intent",
        "urgent_exit_missed",
        "execution_unresolved",
        "non_material_price_detail",
        "wording_only",
    ):
        assert conflict_type in prompt


def test_semantic_disagreement_review_prompt_seed_is_registered_for_deepseek():
    seeds = build_prompt_seeds_from_legacy(AiRecognitionConfig())
    seed = {item.prompt_key: item for item in seeds}[
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT
    ]

    assert seed.category == "notification"
    assert seed.consumers == ("deepseek_disagreement_review",)
    assert seed.required_variables == ()
    assert seed.validation_profile == "semantic_disagreement_review"


def test_build_prompt_seeds_from_legacy_preserves_custom_text_experience():
    seeds = build_prompt_seeds_from_legacy(
        AiRecognitionConfig(
            recognition_prompt="CUSTOM ENTRY EXPERIENCE",
            lifecycle_event_prompt="CUSTOM EXIT EXPERIENCE",
            mimo_direct_prompt="CUSTOM IMAGE EXPERIENCE: read screenshots carefully.",
        )
    )
    by_key = {seed.prompt_key: seed for seed in seeds}

    assert "CUSTOM ENTRY EXPERIENCE" in by_key[SHARED_TRADING_PROMPT].content
    assert "CUSTOM EXIT EXPERIENCE" in by_key[SHARED_TRADING_PROMPT].content
    assert "CUSTOM IMAGE EXPERIENCE" in by_key[MIMO_VISION_PROMPT].content
    assert '"recognition_result"' not in by_key[MIMO_VISION_PROMPT].content


def test_build_prompt_seeds_preserves_suffixes_appended_to_legacy_defaults():
    seeds = build_prompt_seeds_from_legacy(
        AiRecognitionConfig(
            recognition_prompt=DEFAULT_RECOGNITION_PROMPT + "\nCUSTOM MARKET PLUS PRICE RULE",
            lifecycle_event_prompt=DEFAULT_LIFECYCLE_EVENT_PROMPT + "\nCUSTOM TEMPORARY EXIT RULE",
            mimo_direct_prompt=DEFAULT_MIMO_DIRECT_PROMPT + "\n图片自定义：优先读取持仓截图中的方向。",
        )
    )
    by_key = {seed.prompt_key: seed for seed in seeds}

    assert "CUSTOM MARKET PLUS PRICE RULE" in by_key[SHARED_TRADING_PROMPT].content
    assert "CUSTOM TEMPORARY EXIT RULE" in by_key[SHARED_TRADING_PROMPT].content
    assert "图片自定义" in by_key[MIMO_VISION_PROMPT].content


def test_seed_default_prompt_registry_never_overwrites_active_database_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    first_config = AiRecognitionConfig(
        recognition_prompt="FIRST ENTRY",
        lifecycle_event_prompt="FIRST EXIT",
        mimo_direct_prompt="FIRST IMAGE",
    )
    second_config = AiRecognitionConfig(
        recognition_prompt="SECOND ENTRY",
        lifecycle_event_prompt="SECOND EXIT",
        mimo_direct_prompt="SECOND IMAGE",
    )

    seed_default_prompt_registry(factory, first_config)
    seed_default_prompt_registry(factory, second_config)

    shared = get_prompt_detail(factory, SHARED_TRADING_PROMPT)
    assert "FIRST ENTRY" in shared.active_version.content
    assert "SECOND ENTRY" not in shared.active_version.content


def test_authoritative_mimo_prompt_inherits_all_text_experience_and_image_rules():
    prompt = build_authoritative_mimo_prompt(
        AiRecognitionConfig(
            recognition_prompt="CUSTOM ENTRY EXPERIENCE",
            lifecycle_event_prompt="CUSTOM EXIT EXPERIENCE",
            mimo_direct_prompt="CUSTOM IMAGE EXPERIENCE",
        )
    )

    assert "CUSTOM ENTRY EXPERIENCE" in prompt
    assert "CUSTOM EXIT EXPERIENCE" in prompt
    assert "CUSTOM IMAGE EXPERIENCE" in prompt
    assert "input_reading" in prompt
    assert "lifecycle_event" in prompt
    assert "不要补全图片" in prompt
    assert "recognition_result" in prompt
    assert "exit_position" in prompt
    assert "两个维度相互独立" in prompt


def test_load_ai_recognition_config_uses_defaults_when_file_is_missing(tmp_path):
    config = load_ai_recognition_config(tmp_path / "missing.yaml")

    assert config.mode == "local_rule_parser"
    assert config.active_text_model_id == "deepseek-v4-flash"
    assert config.active_image_model_id == "mimo-v2.5"
    assert config.text_provider.is_configured is False
    assert config.image_provider.is_configured is False
    assert config.recognition_prompt.startswith(DEFAULT_RECOGNITION_PROMPT)
    assert config.lifecycle_event_prompt.startswith(DEFAULT_LIFECYCLE_EVENT_PROMPT)
    assert config.mimo_direct_prompt.startswith(DEFAULT_MIMO_DIRECT_PROMPT)
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt
    assert "5.89-5.93" in config.recognition_prompt
    assert "58900-59300" in config.recognition_prompt
    assert "5.78" in config.lifecycle_event_prompt
    assert "57800" in config.lifecycle_event_prompt
    assert "平加仓" in config.lifecycle_event_prompt
    assert "保护成本" in config.lifecycle_event_prompt
    assert "6万/6.07/6.23" in config.mimo_direct_prompt


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
    assert config.lifecycle_event_prompt.startswith("Decide lifecycle events from context.")
    assert config.mimo_direct_prompt.startswith("Read images directly.")
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt
    assert "58900-59300" in config.recognition_prompt
    assert "57800" in config.lifecycle_event_prompt
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
    assert "58900-59300" in config.recognition_prompt


def test_load_ai_recognition_config_upgrades_mimo_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    config_path.write_text("mimo_direct_prompt: Read images directly.\n", encoding="utf-8")

    config = load_ai_recognition_config(config_path)

    assert config.mimo_direct_prompt.startswith("Read images directly.")
    assert "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1" in config.mimo_direct_prompt
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in config.mimo_direct_prompt
    assert "58900-59300" in config.mimo_direct_prompt


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
