"""Deterministic policy for authoritative position-management instructions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


DEFAULT_PARTIAL_CLOSE_FRACTION = 0.50
DEFAULT_TAIL_CLOSE_FRACTION = 0.80

_PARTIAL_TERMS = (
    "第一止盈",
    "第一个止盈",
    "首个止盈",
    "止盈位",
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
)
_CANCEL_ENTRY_TERMS = ("策略先取消", "取消策略", "撤销入场", "取消入场")
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


def resolve_management_directive(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementDirective:
    """Convert one authoritative lifecycle event into deterministic policy."""

    normalized_text = str(text or "").strip().lower()
    event_type = str(lifecycle_event.get("event_type") or "").strip().lower()
    raw_action = str(lifecycle_event.get("management_action") or "").strip().lower()
    combined = " ".join(
        (
            normalized_text,
            str(lifecycle_event.get("reason") or "").strip().lower(),
            raw_action,
        )
    )
    symbol = _normalized_optional(lifecycle_event.get("symbol"), upper=True)
    side = _normalized_optional(lifecycle_event.get("side"), upper=False)
    stop_loss = _normalized_optional(lifecycle_event.get("stop_loss"), upper=False)

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
        )

    if raw_action in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"}:
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
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
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
        )

    if event_type in {
        "exit_position", "exit_full", "full_exit", "close_position",
    } or any(term in combined for term in _FULL_EXIT_TERMS):
        return _directive(
            "full_exit", symbol=symbol, side=side, reason_code="explicit_full_exit",
        )

    has_partial = (
        "partial_take_profit" in raw_action
        or any(term in combined for term in _PARTIAL_TERMS)
        or bool(_close_percentage_values(combined))
        or bool(_retained_percentage_values(combined))
        or "一半" in combined
        or "半仓" in combined
    )
    has_break_even = (
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
    )
    if has_partial:
        fraction = _management_fraction(lifecycle_event, combined)
        intent = "partial_then_break_even" if has_break_even else "partial_take_profit"
        return _directive(
            intent,
            fraction=(
                DEFAULT_PARTIAL_CLOSE_FRACTION if fraction is None else fraction
            ),
            symbol=symbol,
            side=side,
            reason_code=(
                "partial_then_break_even"
                if has_break_even
                else "partial_risk_reduction"
            ),
        )

    if has_break_even:
        return _directive(
            "move_stop_to_break_even",
            symbol=symbol,
            side=side,
            reason_code="break_even_protection",
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
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
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
    )


def _directive(
    intent: str,
    *,
    fraction: float | None = None,
    symbol: str | None,
    side: str | None,
    stop_loss: str | None = None,
    reason_code: str,
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
    )


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
