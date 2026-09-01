"""Non-authoritative context-resolution invocation shadow policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping


MULTIPLE_CANDIDATES_TRIGGER = "multiple_same_source_candidates"


@dataclass(frozen=True)
class ContextResolutionShadowPolicy:
    action_patterns: tuple[str, ...]
    market_chart_types: frozenset[str]

    def replace(self, **changes: Any) -> "ContextResolutionShadowPolicy":
        return replace(self, **changes)


DEFAULT_CONTEXT_RESOLUTION_SHADOW_POLICY = ContextResolutionShadowPolicy(
    action_patterns=(
        "进场",
        "直接进",
        "开仓",
        "做多",
        "做空",
        "买入",
        "卖出",
        "挂单",
        "加仓",
        "减仓",
        "止盈",
        "止损",
        "平仓",
        "出局",
        "撤单",
        "取消",
        "保本",
        "保护价",
        r"设(?:置)?保护",
        "推保护",
        "继续持有",
        "多单",
        "空单",
        "头仓",
        "打上",
        "加个",
        "持有",
        "入场",
        "跑掉",
        "名称错",
        "看价格",
    ),
    market_chart_types=frozenset({"market_chart"}),
)


@dataclass(frozen=True)
class ContextResolutionShadowResult:
    would_trigger: bool
    conditions: tuple[str, ...]
    matched_action_patterns: tuple[str, ...]
    agrees_with_authoritative: bool
    disagreement_direction: str | None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _matching_patterns(
    text: str,
    patterns: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        pattern
        for pattern in patterns
        if re.search(pattern, text, flags=re.IGNORECASE) is not None
    )


def evaluate_context_resolution_shadow(
    *,
    request_payload: Mapping[str, Any],
    authoritative_triggers: tuple[str, ...],
    authoritative_would_trigger: bool,
    policy: ContextResolutionShadowPolicy = (
        DEFAULT_CONTEXT_RESOLUTION_SHADOW_POLICY
    ),
) -> ContextResolutionShadowResult:
    """Evaluate the proposed tightening without controlling provider invocation."""

    first_pass = _mapping(request_payload.get("mimo_first_pass"))
    lifecycle_event = _mapping(first_pass.get("lifecycle_event"))
    input_reading = _mapping(first_pass.get("input_reading"))
    current_message = _mapping(request_payload.get("current_message"))
    evidence = _mapping(request_payload.get("saved_evidence"))

    raw_text = str(current_message.get("text") or "").strip()
    observed_text = str(input_reading.get("observed_text") or "").strip()
    raw_matches = _matching_patterns(raw_text, policy.action_patterns)
    observed_matches = _matching_patterns(
        observed_text,
        policy.action_patterns,
    )

    conditions: list[str] = []
    matched_patterns: list[str] = []
    for trigger in authoritative_triggers:
        if trigger != MULTIPLE_CANDIDATES_TRIGGER:
            conditions.append(f"authoritative:{trigger}")

    multiple_allowed = False
    if MULTIPLE_CANDIDATES_TRIGGER in authoritative_triggers:
        if str(first_pass.get("recognition_result") or "") == "是策略":
            conditions.append("first_pass_strategy")
            multiple_allowed = True
        if str(lifecycle_event.get("event_type") or "none") != "none":
            conditions.append("lifecycle_event_present")
            multiple_allowed = True
        if raw_matches:
            conditions.append("raw_text_action")
            matched_patterns.extend(raw_matches)
            multiple_allowed = True
        if observed_matches:
            conditions.append("observed_text_action")
            matched_patterns.extend(observed_matches)
            multiple_allowed = True
        images = evidence.get("images")
        image_types = {
            str(image.get("image_type") or "")
            for image in images
            if isinstance(images, list) and isinstance(image, Mapping)
        } if isinstance(images, list) else set()
        if (
            not raw_text
            and not observed_text
            and image_types.intersection(policy.market_chart_types)
        ):
            conditions.append("empty_text_market_chart")
            multiple_allowed = True

    would_trigger = bool(
        any(
            trigger != MULTIPLE_CANDIDATES_TRIGGER
            for trigger in authoritative_triggers
        )
        or multiple_allowed
    )
    agrees = would_trigger == bool(authoritative_would_trigger)
    direction = None
    if not agrees:
        direction = (
            "shadow_would_extra_trigger"
            if would_trigger
            else "shadow_would_skip"
        )
    return ContextResolutionShadowResult(
        would_trigger=would_trigger,
        conditions=tuple(dict.fromkeys(conditions)),
        matched_action_patterns=tuple(dict.fromkeys(matched_patterns)),
        agrees_with_authoritative=agrees,
        disagreement_direction=direction,
    )
