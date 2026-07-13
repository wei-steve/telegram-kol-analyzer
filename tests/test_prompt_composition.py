import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.prompt_composition import (
    PromptCompositionError,
    compose_trading_prompt,
    render_registered_prompt,
    render_template_strict,
    validate_prompt_content,
)
from telegram_kol_research.prompt_defaults import (
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
    STRATEGY_ALERT_PROMPT,
)
from telegram_kol_research.prompt_registry import PromptSeed, seed_prompt_definition


def _seed(
    factory,
    *,
    key: str,
    content: str,
    profile: str = "plain_system",
    variables: tuple[str, ...] = (),
):
    return seed_prompt_definition(
        factory,
        PromptSeed(
            prompt_key=key,
            display_name=key,
            description=key,
            category="test",
            consumers=("test",),
            required_variables=variables,
            validation_profile=profile,
            content=content,
        ),
    )


@pytest.fixture
def prompt_factory(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    _seed(factory, key=SHARED_TRADING_PROMPT, content="A_MARKER")
    _seed(factory, key=MIMO_VISION_PROMPT, content="B_MARKER IMAGE_ONLY_MARKER")
    return factory


def test_deepseek_uses_a_and_context_but_never_b(prompt_factory):
    composition = compose_trading_prompt(
        prompt_factory,
        model_kind="deepseek",
        context="C",
    )

    assert composition.system_prompt == "A_MARKER"
    assert composition.context == "C"
    assert composition.version_map.keys() == {SHARED_TRADING_PROMPT}
    assert "IMAGE_ONLY_MARKER" not in composition.system_prompt


def test_mimo_uses_a_and_b_exactly_once(prompt_factory):
    composition = compose_trading_prompt(
        prompt_factory,
        model_kind="mimo",
        context="C",
    )

    assert composition.system_prompt.count("A_MARKER") == 1
    assert composition.system_prompt.count("B_MARKER") == 1
    assert composition.context == "C"
    assert composition.version_map.keys() == {
        SHARED_TRADING_PROMPT,
        MIMO_VISION_PROMPT,
    }


def test_unknown_trading_model_kind_fails_closed(prompt_factory):
    with pytest.raises(PromptCompositionError, match="unsupported trading model"):
        compose_trading_prompt(prompt_factory, model_kind="other", context="C")


def test_shared_trading_validation_requires_canonical_schema():
    invalid = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        "Only explain trades.",
        validation_profile="trading_shared",
        required_variables=(),
    )
    valid = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
        validation_profile="trading_shared",
        required_variables=(),
    )

    assert invalid.success is False
    assert any("recognition_result" in error for error in invalid.errors)
    assert valid.success is True


def test_shared_trading_validation_rejects_partial_schema_and_missing_enums():
    result = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        '{"recognition_result": "", "strategy": {}, "lifecycle_event": {}}',
        validation_profile="trading_shared",
        required_variables=(),
    )

    assert result.success is False
    assert any("target_lifecycle_id" in error for error in result.errors)
    assert any("exit_position" in error for error in result.errors)


def test_mimo_vision_validation_rejects_a_second_output_contract():
    invalid = validate_prompt_content(
        MIMO_VISION_PROMPT,
        '读取图片。输出 {"recognition_result": "是策略"}',
        validation_profile="mimo_vision",
        required_variables=(),
    )
    valid = validate_prompt_content(
        MIMO_VISION_PROMPT,
        "读取图片、截图和图表；图片模糊时不要猜测。",
        validation_profile="mimo_vision",
        required_variables=(),
    )

    assert invalid.success is False
    assert any("输出结构" in error for error in invalid.errors)
    assert valid.success is True


def test_mimo_vision_validation_rejects_json_output_instruction():
    result = validate_prompt_content(
        MIMO_VISION_PROMPT,
        "读取图片并只输出 JSON 对象。",
        validation_profile="mimo_vision",
        required_variables=(),
    )

    assert result.success is False
    assert any("输出结构" in error for error in result.errors)


def test_strict_template_renderer_rejects_missing_and_unknown_variables():
    with pytest.raises(PromptCompositionError, match="missing template variables"):
        render_template_strict("Hello {name}")
    with pytest.raises(PromptCompositionError, match="unknown template variables"):
        render_template_strict("Hello {name}", name="Alice", extra="value")

    assert render_template_strict("Hello {name}", name="Alice") == "Hello Alice"


def test_render_registered_prompt_returns_version_audit(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    detail = _seed(
        factory,
        key=STRATEGY_ALERT_PROMPT,
        content="Chat={chat_title}; Text={message_text}",
        profile="strategy_alert",
        variables=("chat_title", "message_text"),
    )

    rendered = render_registered_prompt(
        factory,
        STRATEGY_ALERT_PROMPT,
        variables={"chat_title": "VIP", "message_text": "BTC long"},
    )

    assert rendered.content == "Chat=VIP; Text=BTC long"
    assert rendered.version_map == {
        STRATEGY_ALERT_PROMPT: detail.active_version.id
    }
