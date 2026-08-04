"""Deterministic policy for authoritative position-management instructions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.strategy_management_contracts import (
    COMPOSITE_MANAGEMENT_CONTRACT_VERSION,
    ManagementInstructionContract,
)


DEFAULT_PARTIAL_CLOSE_FRACTION = 0.50
DEFAULT_TAIL_CLOSE_FRACTION = 0.80
FULL_EXIT_ACTIONS = frozenset({"exit_full", "full_exit", "close_position"})

_PARTIAL_TERMS = (
    "第一止盈",
    "第一个止盈",
    "首个止盈",
    "止盈一部分",
    "部分止盈",
    "分批止盈",
    "提前止盈",
    "减仓",
    "减半",
    "平加仓",
    "移动止盈",
)
_TAIL_TERMS = ("只留一点尾仓", "只留尾仓", "留一点尾仓", "保留底仓", "留底仓")
_BREAK_EVEN_TERMS = (
    "移动止损",
    "止损移动到开仓价",
    "移动止损至开仓价",
    "移动止损到开仓价",
    "止损到成本",
    "止损好成本",
    "成本保护",
    "保护成本",
    "保本止损",
    "带保护",
    "推保护",
    "上推保护",
    "保护价",
    "保护止损",
)
_FULL_EXIT_TERMS = (
    "全部止盈出局",
    "全部平仓",
    "全部平",
    "全平",
    "全部出局",
    "清仓",
    "止损出局",
    "止盈出局",
    "出局吧",
)
_CANCEL_ENTRY_TERMS = (
    "策略先取消",
    "取消策略",
    "撤销入场",
    "取消入场",
    "取消挂单",
    "撤销挂单",
)
_RISK_INCREASING_TERMS = ("加仓", "补仓", "再做一次", "重新进场", "反手")


@dataclass(frozen=True, slots=True)
class ManagementDirective:
    intent: str
    fraction: float | None
    symbol: str | None
    side: str | None
    stop_loss: str | None
    risk_reducing: bool
    fanout_allowed: bool
    cancel_deferred_entries: bool
    reason_code: str
    strategy_thread_id: int | None = None
    stop_price_source: str | None = None


def resolve_management_directive(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementDirective:
    """Convert one authoritative lifecycle event into deterministic policy."""

    normalized_text = str(text or "").strip().lower()
    event_type = str(lifecycle_event.get("event_type") or "").strip().lower()
    raw_action = str(lifecycle_event.get("management_action") or "").strip().lower()
    combined = " ".join((normalized_text, raw_action))
    symbol = _normalized_optional(lifecycle_event.get("symbol"), upper=True)
    side = _normalized_optional(lifecycle_event.get("side"), upper=False)
    stop_loss = _normalized_optional(lifecycle_event.get("stop_loss"), upper=False)
    strategy_thread_id = _positive_int_or_none(
        lifecycle_event.get("strategy_thread_id")
    )
    current_message_stop = (
        stop_loss
        if stop_loss is not None
        and _text_contains_explicit_stop_value(normalized_text, stop_loss)
        else None
    )
    current_message_stop_source = (
        "current_message_text" if current_message_stop is not None else None
    )
    has_partial = _has_partial_clause(combined, raw_action)
    has_break_even = _has_break_even_clause(combined, raw_action)
    has_protection = has_break_even or current_message_stop is not None

    if any(term in combined for term in _CANCEL_ENTRY_TERMS) or event_type == "cancel_entry":
        return ManagementDirective(
            intent="cancel_entry",
            fraction=None,
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            risk_reducing=True,
            fanout_allowed=False,
            cancel_deferred_entries=True,
            reason_code="explicit_cancel_entry",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    risk_increasing = raw_action in {
        "add_position", "increase_position", "open_position", "reverse_position",
    } or any(
        term in combined.replace("平加仓", "")
        for term in _RISK_INCREASING_TERMS
    )
    if risk_increasing:
        return ManagementDirective(
            intent=raw_action or "risk_increasing",
            fraction=None,
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            risk_reducing=False,
            fanout_allowed=False,
            cancel_deferred_entries=False,
            reason_code="risk_increasing_fanout_forbidden",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if event_type in {
        "exit_position", "exit_full", "full_exit", "close_position",
    } or raw_action in FULL_EXIT_ACTIONS or (
        any(term in combined for term in _FULL_EXIT_TERMS)
        and not any(
            term in combined
            for term in ("剩余仓位", "剩余持仓", "其余仓位", "剩下仓位")
        )
    ):
        return _directive(
            "full_exit",
            symbol=symbol,
            side=side,
            reason_code="explicit_full_exit",
            strategy_thread_id=strategy_thread_id,
        )

    if (
        raw_action in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"}
        and not has_partial
    ):
        if stop_loss is None:
            return ManagementDirective(
                intent="adjust_stop_loss",
                fraction=None,
                symbol=symbol,
                side=side,
                stop_loss=None,
                risk_reducing=False,
                fanout_allowed=False,
                cancel_deferred_entries=False,
                reason_code="stop_adjustment_direction_not_verified",
                strategy_thread_id=strategy_thread_id,
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if (
        event_type == "position_update"
        and current_message_stop is not None
        and not has_partial
    ):
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=current_message_stop,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if any(term in combined for term in _TAIL_TERMS):
        return _directive(
            "partial_take_profit",
            fraction=DEFAULT_TAIL_CLOSE_FRACTION,
            symbol=symbol,
            side=side,
            reason_code=(
                "tail_retention_preferred_over_optional_exit"
                if any(term in combined for term in _FULL_EXIT_TERMS)
                or "出局" in combined
                else "tail_retention"
            ),
            strategy_thread_id=strategy_thread_id,
        )

    if has_partial:
        fraction = _management_fraction(lifecycle_event, combined)
        intent = "partial_then_break_even" if has_protection else "partial_take_profit"
        return _directive(
            intent,
            fraction=(
                DEFAULT_PARTIAL_CLOSE_FRACTION if fraction is None else fraction
            ),
            symbol=symbol,
            side=side,
            reason_code=(
                "partial_then_break_even"
                if has_protection
                else "partial_risk_reduction"
            ),
            strategy_thread_id=strategy_thread_id,
            stop_loss=current_message_stop if has_protection else None,
            stop_price_source=(
                current_message_stop_source if has_protection else None
            ),
        )

    if has_break_even:
        return _directive(
            "move_stop_to_break_even",
            symbol=symbol,
            side=side,
            reason_code="break_even_protection",
            strategy_thread_id=strategy_thread_id,
            stop_loss=current_message_stop,
            stop_price_source=current_message_stop_source,
        )

    if raw_action in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"} or (
        stop_loss is not None and event_type == "position_update"
    ):
        if stop_loss is None:
            return ManagementDirective(
                intent="adjust_stop_loss",
                fraction=None,
                symbol=symbol,
                side=side,
                stop_loss=None,
                risk_reducing=False,
                fanout_allowed=False,
                cancel_deferred_entries=False,
                reason_code="stop_adjustment_direction_not_verified",
                strategy_thread_id=strategy_thread_id,
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
        )

    return ManagementDirective(
        intent="none",
        fraction=None,
        symbol=symbol,
        side=side,
        stop_loss=stop_loss,
        risk_reducing=False,
        fanout_allowed=False,
        cancel_deferred_entries=False,
        reason_code="no_actionable_risk_reduction",
        strategy_thread_id=strategy_thread_id,
    )


def build_management_instruction_contract(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementInstructionContract:
    """Build the complete immutable contract for one composite instruction."""

    directive = resolve_management_directive(
        text=text,
        lifecycle_event=lifecycle_event,
    )
    if directive.intent != "partial_then_break_even" or directive.fraction is None:
        raise ValueError("management_instruction_is_not_composite")
    if directive.stop_loss is not None:
        stop_mode = "explicit_price"
        stop_price = directive.stop_loss
        stop_price_source = directive.stop_price_source
    else:
        stop_mode = "actual_entry_price"
        stop_price = None
        stop_price_source = None
    return ManagementInstructionContract(
        version=COMPOSITE_MANAGEMENT_CONTRACT_VERSION,
        target_lifecycle_id=_positive_int_or_none(
            lifecycle_event.get("target_lifecycle_id")
        ),
        strategy_instance_id=_normalized_optional(
            lifecycle_event.get("strategy_instance_id"), upper=False
        ),
        symbol=directive.symbol,
        side=directive.side,
        close_fraction=str(directive.fraction),
        stop_mode=stop_mode,
        stop_price=stop_price,
        stop_price_source=stop_price_source,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=directive.cancel_deferred_entries,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text=str(text or "").strip(),
    )


def _has_partial_clause(combined: str, raw_action: str) -> bool:
    return (
        "partial_take_profit" in raw_action
        or any(term in combined for term in _PARTIAL_TERMS)
        or bool(_close_percentage_values(combined))
        or bool(_retained_percentage_values(combined))
        or "一半" in combined
        or "半仓" in combined
    )


def _has_break_even_clause(combined: str, raw_action: str) -> bool:
    return (
        any(
            term in raw_action
            for term in (
                "move_stop_to_protect",
                "move_stop_to_break_even",
                "breakeven",
                "break_even",
            )
        )
        or any(term in combined for term in _BREAK_EVEN_TERMS)
        or (
            "止损" in combined
            and any(term in combined for term in ("开仓价", "入场价", "成本价"))
        )
    )


def _directive(
    intent: str,
    *,
    fraction: float | None = None,
    symbol: str | None,
    side: str | None,
    stop_loss: str | None = None,
    reason_code: str,
    strategy_thread_id: int | None = None,
    stop_price_source: str | None = None,
) -> ManagementDirective:
    return ManagementDirective(
        intent=intent,
        fraction=fraction,
        symbol=symbol,
        side=side,
        stop_loss=stop_loss,
        risk_reducing=True,
        fanout_allowed=True,
        cancel_deferred_entries=intent in {
            "cancel_entry",
            "partial_take_profit",
            "partial_then_break_even",
            "full_exit",
        },
        reason_code=reason_code,
        strategy_thread_id=strategy_thread_id,
        stop_price_source=stop_price_source,
    )


def _positive_int_or_none(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _management_fraction(
    lifecycle_event: Mapping[str, Any], combined_text: str
) -> float | None:
    values: list[float] = []
    for key in ("management_fraction", "close_fraction", "fraction"):
        value = _fraction_value(lifecycle_event.get(key))
        if value is not None:
            values.append(value)
    values.extend(_close_percentage_values(combined_text))
    values.extend(1.0 - value for value in _retained_percentage_values(combined_text))
    if values:
        first = values[0]
        if any(abs(value - first) > 1e-9 for value in values[1:]):
            raise ValueError("management_fraction_ambiguous")
        return first
    if "一半" in combined_text or "半仓" in combined_text:
        return 0.5
    return None


def _close_percentage_values(text: str) -> list[float]:
    values = re.findall(
        r"(?:止盈|减仓|平仓|平掉|出掉|出局)[^\d%％]{0,12}"
        r"(\d+(?:\.\d+)?)\s*[%％]",
        text,
    )
    return [
        fraction
        for value in values
        if (fraction := _fraction_value(f"{value}%")) is not None
    ]


def _retained_percentage_values(text: str) -> list[float]:
    values = re.findall(
        r"(?:保留|剩余|留下|留)[^\d%％]{0,12}(\d+(?:\.\d+)?)\s*[%％]",
        text,
    )
    return [
        fraction
        for value in values
        if (fraction := _fraction_value(f"{value}%")) is not None
    ]


def _fraction_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    is_percent = text.endswith(("%", "％"))
    if is_percent:
        text = text[:-1].strip()
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return None
    if is_percent or numeric > 1:
        numeric /= 100
    return numeric if 0 < numeric <= 1 else None


def _normalized_optional(value: Any, *, upper: bool) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.upper() if upper else normalized.lower()


def _text_contains_explicit_stop_value(text: str, value: str) -> bool:
    try:
        expected = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not expected.is_finite() or expected <= 0:
        return False
    for match in re.finditer(
        r"(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])",
        text,
    ):
        if (
            match.start() > 0
            and text[match.start() - 1] in "百千万亿"
        ) or (
            match.end() < len(text)
            and text[match.end()] in "百千万亿"
        ):
            continue
        token = match.group(0)
        try:
            observed = Decimal(token.replace(",", ""))
        except InvalidOperation:
            continue
        if observed == expected:
            prefix = text[max(0, match.start() - 32):match.start()]
            suffix = text[match.end():min(len(text), match.end() + 16)]
            if re.search(
                r"(?:止损|stop\s*loss|\bmove\s+stop(?:\s+to)?|\bsl\b|"
                r"保护(?:到|至|价)|保本(?:到|至))",
                prefix,
            ) or re.match(
                r"\s*(?:(?:作为|设为|当作)\s*)?(?:止损|stop\s*loss|\bsl\b)",
                suffix,
            ):
                return True
    return False
