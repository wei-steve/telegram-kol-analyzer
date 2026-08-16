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
    DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT,
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
    MIMO_V2_AUTHORITATIVE_PROMPT,
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
    STRATEGY_ALERT_PROMPT,
    SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
)


def test_shared_prompt_requires_source_separated_multimodal_evidence():
    prompt = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT

    assert '"evidence"' in prompt
    assert '"text"' in prompt
    assert '"images"' in prompt
    assert '"image_type"' in prompt
    assert '"conflicts"' in prompt
    assert "不得静默合并" in prompt
    assert '"entry_fragments"' in prompt
    assert "全仓操作" in prompt
    assert "正常仓位操作" in prompt
    assert "两个点位各半仓" in prompt
    assert "补仓价格" in prompt
    assert "区间" in prompt and "推断半仓" in prompt
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
    _seed(
        factory,
        key=MIMO_V2_AUTHORITATIVE_PROMPT,
        content=DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT,
        profile="mimo_v2_authoritative",
    )
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


def test_mimo_v2_composition_has_one_contract_and_preserves_v1(prompt_factory):
    legacy = compose_trading_prompt(
        prompt_factory,
        model_kind="mimo",
        context="C",
    )
    explicit_v1 = compose_trading_prompt(
        prompt_factory,
        model_kind="mimo",
        context="C",
        contract_version="v1",
    )
    v2 = compose_trading_prompt(
        prompt_factory,
        model_kind="mimo",
        context="C",
        contract_version="v2",
    )

    assert explicit_v1 == legacy
    assert '"contract_version": "mimo-authoritative-v2"' not in legacy.system_prompt
    assert v2.system_prompt.count('"contract_version": "mimo-authoritative-v2"') == 1
    assert "生命周期事件与仓位管理" in v2.system_prompt
    assert "entry_confirmation" in v2.system_prompt
    assert "confirm_entry" in v2.system_prompt
    assert "entry_fragment" in v2.system_prompt
    assert "leg_allocation" in v2.system_prompt
    assert "supplemental_entry" in v2.system_prompt
    assert "图文证据分离" in v2.system_prompt
    assert "每张图片" in v2.system_prompt
    assert v2.version_map.keys() == {MIMO_V2_AUTHORITATIVE_PROMPT}


def test_deepseek_rejects_mimo_v2_contract(prompt_factory):
    with pytest.raises(PromptCompositionError, match="MiMo v2"):
        compose_trading_prompt(
            prompt_factory,
            model_kind="deepseek",
            context="C",
            contract_version="v2",
        )


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


def test_mimo_v2_validation_accepts_closed_default_contract():
    result = validate_prompt_content(
        MIMO_V2_AUTHORITATIVE_PROMPT,
        DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT,
        validation_profile="mimo_v2_authoritative",
        required_variables=(),
    )

    assert result.success is True


@pytest.mark.parametrize(
    "required_rule",
    (
        "同一目标的 risk_update 已包含继续持有语义时，不要再输出 hold_update",
        "同一目标的 move_stop_to_protect 与止盈挂单设置同时出现时",
        "management_fraction 必须输出 0 到 1 之间的 JSON 数字",
        "cancel_pending_entry 的 parameters 必须是空对象",
        "所有价格类 parameters 必须输出字符串",
        "thread_id 不能替代 lifecycle_id",
        "明确写出 priority 或 P1/P2 时，只把最高优先级方案输出为 entry",
        "到达止盈点并明确保留剩余仓位",
    ),
)
def test_mimo_v2_default_prompt_closes_replay_compatibility_rules(required_rule):
    assert required_rule in DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT


@pytest.mark.parametrize(
    "marker",
    (
        '"intents"',
        '"intent_type"',
        '"evidence"',
        '"images"',
        '"source"',
        "不得静默合并",
        "entry_confirmation + confirm_entry",
        "entry_context + entry_fragment",
        "leg_allocation=[0.5,0.5]",
        "supplemental_entry",
        "只输出一个 JSON 对象",
        "不得添加额外字段",
    ),
)
def test_mimo_v2_validation_rejects_missing_contract_or_safety_marker(marker):
    result = validate_prompt_content(
        MIMO_V2_AUTHORITATIVE_PROMPT,
        DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT.replace(marker, ""),
        validation_profile="mimo_v2_authoritative",
        required_variables=(),
    )

    assert result.success is False
    assert any(marker in error for error in result.errors)


def test_semantic_disagreement_review_validation_accepts_default_prompt():
    result = validate_prompt_content(
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        validation_profile="semantic_disagreement_review",
        required_variables=(),
    )

    assert result.success is True


@pytest.mark.parametrize(
    "marker",
    (
        '"independent_action"',
        '"target_lifecycle_id"',
        '"evidence"',
        '"conflict_types"',
        '"material_disagreement"',
        '"reason"',
    ),
)
def test_semantic_disagreement_review_validation_rejects_missing_schema_marker(marker):
    result = validate_prompt_content(
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT.replace(marker, ""),
        validation_profile="semantic_disagreement_review",
        required_variables=(),
    )

    assert result.success is False
    assert any(marker in error for error in result.errors)


@pytest.mark.parametrize(
    "marker",
    (
        "entry_confirm",
        "exit_partial",
        "critical",
        "urgent_exit_missed",
        "wording_only",
        "不得声称能够读取图片像素",
        "不得修改交易",
        "必须引用当前消息中的证据",
    ),
)
def test_semantic_disagreement_review_validation_rejects_weakened_safety_contract(marker):
    result = validate_prompt_content(
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT.replace(marker, ""),
        validation_profile="semantic_disagreement_review",
        required_variables=(),
    )

    assert result.success is False
    assert any(marker in error for error in result.errors)


@pytest.mark.parametrize(
    "closed_contract",
    (
        "none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update",
        "none | normal | critical",
        "actionability, action_family, full_vs_partial_exit, symbol, side, "
        "target_lifecycle, stop_intent, urgent_exit_missed, execution_unresolved, "
        "non_material_price_detail, wording_only",
    ),
)
def test_semantic_disagreement_review_validation_requires_closed_enums(closed_contract):
    result = validate_prompt_content(
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT.replace(closed_contract, ""),
        validation_profile="semantic_disagreement_review",
        required_variables=(),
    )

    assert result.success is False
    assert any("闭合枚举" in error for error in result.errors)


@pytest.mark.parametrize(
    "directive",
    (
        "只输出一个 JSON 对象",
        "不得添加额外字段",
    ),
)
def test_semantic_disagreement_review_validation_requires_closed_json_directives(
    directive,
):
    result = validate_prompt_content(
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT.replace(directive, ""),
        validation_profile="semantic_disagreement_review",
        required_variables=(),
    )

    assert result.success is False
    assert any(directive in error for error in result.errors)


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
