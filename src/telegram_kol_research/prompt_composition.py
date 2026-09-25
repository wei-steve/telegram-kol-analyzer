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
    contract_version: str = "v1",
) -> PromptComposition:
    if model_kind not in {"deepseek", "mimo"}:
        raise PromptCompositionError(
            f"unsupported trading model kind: {model_kind}"
        )
    if contract_version != "v1":
        raise PromptCompositionError(
            f"unsupported trading contract version: {contract_version}"
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
            # ``"entry_fragments"`` was here and is deliberately not any more.
            # It was added to this list after the active production version was
            # published (2026-08-05) and that version has never carried the
            # field, so the requirement has never been met by the prompt that
            # actually runs -- and because this gate runs on save/publish and
            # never on render, nothing surfaced it. The effect was that *no*
            # new version built on the live one could be published at all.
            # entry_fragments has also never appeared in production output (0
            # of the last 939 authoritative decisions), so requiring it here
            # asserted a contract the system does not have. Restore it once the
            # live prompt actually carries the field; the separate
            # investigation into why it never shipped owns that decision.
            # The first-pass classification contract (design §2). A shared
            # trading template without these cannot be published, so no prompt
            # version can silently drop the field once it exists. This runs on
            # the draft validate/publish path only -- ``compose_trading_prompt``
            # renders the active version without validating it, so the currently
            # active production version is never rejected at recognition time.
            '"message_classes"',
            '"class"',
            '"target"',
            '"resolution"',
        )
        for marker in required_schema_markers:
            if marker not in normalized:
                errors.append(f"统一交易模板缺少必需字段 {marker}")
        for enum_value in (
            "entry_confirm", "cancel_entry", "exit_position", "position_update",
            "none", "market", "limit", "long", "short",
            "新策略", "策略管理", "仓位管理", "闲话", "图片不可读",
            "exact", "forthcoming", "unknown",
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
    elif validation_profile == "mimo_v2_authoritative":
        required_schema_markers = (
            '"contract_version": "mimo-authoritative-v2"',
            '"summary"',
            '"confidence"',
            '"intents"',
            '"intent_type"',
            '"action"',
            '"kind"',
            '"target"',
            '"strategy"',
            '"parameters"',
            '"reason"',
            '"evidence_refs"',
            '"evidence"',
            '"text"',
            '"images"',
            '"asset_id"',
            '"image_type"',
            '"quality"',
            '"observed_text"',
            '"fields"',
            '"source"',
            '"conflicts"',
        )
        required_contract_markers = (
            "new_strategy | entry_confirmation | position_management | exit | cancel_entry | "
            "strategy_revision | entry_context | position_report | "
            "market_commentary | non_trading | unclear",
            "entry | confirm_entry | entry_fragment | cancel_pending_entry | "
            "replace_entry | full_exit | partial_exit | partial_take_profit | "
            "move_stop_to_protect | hold_update | risk_update",
            "entry_confirmation + confirm_entry",
            "entry_context + entry_fragment",
            "leg_allocation=[0.5,0.5]",
            "supplemental_entry",
            "strategy_screenshot、position_screenshot、order_screenshot、"
            "market_chart、profit_review、advertisement、unrelated、unknown",
            "clear、blurry、cropped、unreadable",
            "source 只能是 text、image、both",
            "text:observed_text",
            "image:<asset_id>:observed_text",
            "生命周期事件与仓位管理",
            "图文证据分离",
            "每张图片",
            "不得静默合并",
            "不得把旧上下文复制成当前意图",
            "只输出一个 JSON 对象",
            "不得添加额外字段",
        )
        for marker in required_schema_markers:
            if marker not in normalized:
                errors.append(f"MiMo v2 模板缺少必需字段 {marker}")
        for marker in required_contract_markers:
            if marker not in normalized:
                errors.append(f"MiMo v2 模板缺少必需契约 {marker}")
        if normalized.count('"contract_version": "mimo-authoritative-v2"') != 1:
            errors.append("MiMo v2 模板必须且只能定义一次契约版本")
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
