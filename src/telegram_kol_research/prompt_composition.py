"""Resolve, validate, and compose registered AI prompts."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.prompt_defaults import (
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.prompt_registry import resolve_active_prompt


class PromptCompositionError(RuntimeError):
    """Raised when a prompt cannot be safely composed or rendered."""


@dataclass(frozen=True)
class PromptComposition:
    system_prompt: str
    context: str
    version_map: dict[str, int]


@dataclass(frozen=True)
class RenderedPrompt:
    content: str
    version_map: dict[str, int]


@dataclass(frozen=True)
class PromptValidationResult:
    success: bool
    errors: tuple[str, ...]


def compose_trading_prompt(
    session_factory: sessionmaker,
    *,
    model_kind: str,
    context: str,
) -> PromptComposition:
    if model_kind not in {"deepseek", "mimo"}:
        raise PromptCompositionError(
            f"unsupported trading model kind: {model_kind}"
        )
    shared = resolve_active_prompt(session_factory, SHARED_TRADING_PROMPT)
    prompts = [shared]
    if model_kind == "mimo":
        prompts.append(resolve_active_prompt(session_factory, MIMO_VISION_PROMPT))
    return PromptComposition(
        system_prompt="\n\n".join(item.content.strip() for item in prompts),
        context=context,
        version_map={item.prompt_key: item.version_id for item in prompts},
    )


def _template_variables(template: str) -> set[str]:
    variables: set[str] = set()
    try:
        parsed = Formatter().parse(template)
        for _, field_name, _, _ in parsed:
            if field_name:
                variables.add(field_name)
    except ValueError as exc:
        raise PromptCompositionError(f"invalid template syntax: {exc}") from exc
    return variables


def render_template_strict(template: str, **variables: Any) -> str:
    referenced = _template_variables(template)
    supplied = set(variables)
    missing = referenced - supplied
    if missing:
        raise PromptCompositionError(
            f"missing template variables: {', '.join(sorted(missing))}"
        )
    unknown = supplied - referenced
    if unknown:
        raise PromptCompositionError(
            f"unknown template variables: {', '.join(sorted(unknown))}"
        )
    try:
        return template.format(**variables)
    except (KeyError, ValueError) as exc:
        raise PromptCompositionError(f"failed to render template: {exc}") from exc


def validate_prompt_content(
    prompt_key: str,
    content: str,
    *,
    validation_profile: str,
    required_variables: tuple[str, ...],
) -> PromptValidationResult:
    normalized = content.strip()
    errors: list[str] = []
    if not normalized:
        errors.append("提示词不能为空")
        return PromptValidationResult(False, tuple(errors))

    if validation_profile == "trading_shared":
        required_schema_markers = (
            '"recognition_result"', '"reason"', '"strategy"', '"symbol"',
            '"side"', '"entry"', '"stop_loss"', '"take_profit"',
            '"leverage"', '"order_type"', '"lifecycle_event"',
            '"event_type"', '"target_lifecycle_id"', '"management_action"',
            '"input_reading"', '"observed_text"', '"image_quality"',
            '"confidence"',
            '"entry_fragments"',
        )
        for marker in required_schema_markers:
            if marker not in normalized:
                errors.append(f"统一交易模板缺少必需字段 {marker}")
        for enum_value in (
            "entry_confirm", "cancel_entry", "exit_position", "position_update",
            "none", "market", "limit", "long", "short",
        ):
            if enum_value not in normalized:
                errors.append(f"统一交易模板缺少必需枚举 {enum_value}")
    elif validation_profile == "mimo_vision":
        forbidden_contract_markers = (
            '"recognition_result"', '"strategy"', '"lifecycle_event"',
            '"event_type"', '"input_reading"', "只输出一个 JSON",
            "只输出 JSON", "Return JSON", "JSON 对象",
        )
        if any(marker in normalized for marker in forbidden_contract_markers):
            errors.append("MiMo 图片模板不能重新定义统一输出结构")
        if not any(
            marker in normalized.lower()
            for marker in ("图片", "截图", "图表", "image", "screenshot")
        ):
            errors.append("MiMo 图片模板必须包含图片读取规则")
    elif validation_profile == "semantic_disagreement_review":
        required_schema_markers = (
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
        )
        required_contract_markers = (
            "none",
            "entry",
            "entry_confirm",
            "cancel_entry",
            "exit_full",
            "exit_partial",
            "position_update",
            "normal",
            "critical",
            "actionability",
            "action_family",
            "full_vs_partial_exit",
            "target_lifecycle",
            "stop_intent",
            "urgent_exit_missed",
            "execution_unresolved",
            "non_material_price_detail",
            "wording_only",
            "独立解读当前消息",
            "必须引用当前消息中的证据",
            "不得修改交易",
            "不得声称能够读取图片像素",
            "只输出一个 JSON 对象",
            "不得添加额外字段",
        )
        required_closed_contracts = (
            "none | entry | entry_confirm | cancel_entry | exit_full | "
            "exit_partial | position_update",
            "none | normal | critical",
            "actionability, action_family, full_vs_partial_exit, symbol, side, "
            "target_lifecycle, stop_intent, urgent_exit_missed, "
            "execution_unresolved, non_material_price_detail, wording_only",
        )
        for marker in required_schema_markers:
            if marker not in normalized:
                errors.append(f"语义分歧复核模板缺少必需字段 {marker}")
        for marker in required_contract_markers:
            if marker not in normalized:
                errors.append(f"语义分歧复核模板缺少必需契约 {marker}")
        for contract in required_closed_contracts:
            if contract not in normalized:
                errors.append(f"语义分歧复核模板缺少闭合枚举 {contract}")

    if required_variables:
        try:
            referenced = _template_variables(normalized)
        except PromptCompositionError as exc:
            errors.append(str(exc))
            referenced = set()
        required = set(required_variables)
        missing = required - referenced
        unknown = referenced - required
        if missing:
            errors.append(
                f"缺少必需模板变量: {', '.join(sorted(missing))}"
            )
        if unknown:
            errors.append(
                f"存在未登记模板变量: {', '.join(sorted(unknown))}"
            )

    return PromptValidationResult(not errors, tuple(errors))


def render_registered_prompt(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    variables: dict[str, Any] | None = None,
    chat_id: int | None = None,
) -> RenderedPrompt:
    resolved = resolve_active_prompt(
        session_factory,
        prompt_key,
        chat_id=chat_id,
    )
    validation = validate_prompt_content(
        prompt_key,
        resolved.content,
        validation_profile=resolved.validation_profile,
        required_variables=resolved.required_variables,
    )
    if not validation.success:
        raise PromptCompositionError("; ".join(validation.errors))
    supplied = variables or {}
    content = (
        render_template_strict(resolved.content, **supplied)
        if resolved.required_variables or supplied
        else resolved.content
    )
    return RenderedPrompt(
        content=content,
        version_map={resolved.prompt_key: resolved.version_id},
    )
