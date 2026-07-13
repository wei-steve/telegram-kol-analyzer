"""Pure normalization and severity rules for semantic AI review results.

MiMo remains authoritative.  This module only classifies the materiality of an
independent DeepSeek review; it performs no persistence, network, or execution
work.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any


ACTION_TYPES = frozenset(
    {
        "none",
        "entry",
        "entry_confirm",
        "cancel_entry",
        "exit_full",
        "exit_partial",
        "position_update",
    }
)
SEVERITIES = frozenset({"none", "normal", "critical"})
CONFLICT_TYPES = (
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
)
_CONFLICT_TYPE_SET = frozenset(CONFLICT_TYPES)
_MODEL_CRITICAL_CONFLICTS = frozenset(
    {
        "actionability",
        "action_family",
        "full_vs_partial_exit",
        "symbol",
        "side",
        "target_lifecycle",
        "stop_intent",
        "urgent_exit_missed",
        "execution_unresolved",
    }
)
_ACTIONABLE_TYPES = ACTION_TYPES - {"none"}
_PARTIAL_MANAGEMENT_ACTIONS = frozenset(
    {
        "partial_take_profit",
        "partial_exit",
        "reduce_position",
        "trim_position",
        "减仓",
        "部分止盈",
        "分批止盈",
    }
)
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:万|w)?$", re.IGNORECASE)
_PRICE_SEPARATOR = re.compile(r"\s*(?:/|／|~|～|至|到|—|–|-)\s*")


@dataclass(frozen=True)
class SemanticReviewDecision:
    agreement_status: str
    severity: str
    conflict_types: tuple[str, ...]
    differences: tuple[str, ...]
    reason: str


def normalize_price(value: Any) -> Decimal | tuple[Decimal, ...] | str | None:
    """Return a stable representation for scalar, range, and list prices."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        items: list[Decimal | str] = []
        for item in value:
            normalized = normalize_price(item)
            if normalized is None:
                continue
            if isinstance(normalized, tuple):
                items.extend(normalized)
            else:
                items.append(normalized)
        return _collapse_price_items(items)
    if isinstance(value, (int, float, Decimal)):
        return _decimal_or_text(value)

    text = _compact_text(value)
    if not text:
        return None
    text = text.replace(",", "")
    text = re.sub(r"(?:附近|左右|about|approx(?:imately)?)$", "", text).strip()
    if _NUMBER.fullmatch(text):
        return _decimal_or_text(text)
    parts = _PRICE_SEPARATOR.split(text)
    if len(parts) > 1 and all(_NUMBER.fullmatch(part) for part in parts):
        return _collapse_price_items([_decimal_or_text(part) for part in parts])
    return text


def normalize_mimo_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a MiMo entry/lifecycle payload to semantic-review vocabulary."""

    strategy = payload.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    lifecycle = payload.get("lifecycle_event")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    management = _normalize_management_action(lifecycle.get("management_action"))
    event_type = _token(lifecycle.get("event_type"))

    if event_type in {"exit_position", "exit_full", "full_exit", "close_position"}:
        action_type = "exit_full"
    elif event_type in {"position_update", "update_position"}:
        action_type = (
            "exit_partial"
            if _is_partial_management(management)
            else "position_update"
        )
    elif event_type in {"entry_confirm", "confirm_entry"}:
        action_type = "entry_confirm"
    elif event_type in {"cancel_entry", "entry_cancel"}:
        action_type = "cancel_entry"
    elif event_type in {"exit_partial", "partial_exit"}:
        action_type = "exit_partial"
    elif _is_entry(payload.get("recognition_result")):
        action_type = "entry"
    else:
        action_type = "none"

    source = lifecycle if action_type not in {"entry", "none"} else strategy
    return _normalized_action(
        action_type=action_type,
        source=source,
        management_action=management,
    )


def validate_review_payload(payload: dict[str, Any]) -> None:
    """Validate the closed JSON contract emitted by semantic review."""

    if not isinstance(payload, dict):
        raise ValueError("review payload must be an object")
    required = {
        "independent_action",
        "evidence",
        "conflict_types",
        "material_disagreement",
        "suggested_severity",
        "confidence",
        "reason",
    }
    if set(payload) != required:
        raise ValueError("review payload fields do not match the closed contract")
    action = payload["independent_action"]
    action_fields = {
        "action_type",
        "target_lifecycle_id",
        "symbol",
        "side",
        "stop_loss",
        "take_profit",
        "management_action",
    }
    if not isinstance(action, dict) or set(action) != action_fields:
        raise ValueError("independent_action fields do not match the closed contract")
    if _token(action.get("action_type")) not in ACTION_TYPES:
        raise ValueError("invalid independent_action.action_type")
    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError("evidence must be a list of strings")
    conflicts = payload["conflict_types"]
    if (
        not isinstance(conflicts, list)
        or not all(isinstance(item, str) for item in conflicts)
        or any(_token(item) not in _CONFLICT_TYPE_SET for item in conflicts)
    ):
        raise ValueError("conflict_types contains an unsupported value")
    if not isinstance(payload["material_disagreement"], bool):
        raise ValueError("material_disagreement must be a boolean")
    if _token(payload["suggested_severity"]) not in SEVERITIES:
        raise ValueError("invalid suggested_severity")
    confidence = payload["confidence"]
    if isinstance(confidence, bool):
        raise ValueError("confidence must be numeric")
    try:
        numeric_confidence = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not 0 <= numeric_confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if not isinstance(payload["reason"], str):
        raise ValueError("reason must be a string")


def decide_semantic_severity(
    *,
    mimo_payload: dict[str, Any],
    review_payload: dict[str, Any],
    automation: dict[str, Any],
    input_kind: str,
    critical_confidence: float = 0.80,
) -> SemanticReviewDecision:
    """Classify disagreement while preserving deterministic safety floors."""

    validate_review_payload(review_payload)
    if not 0 <= critical_confidence <= 1:
        raise ValueError("critical_confidence must be between 0 and 1")

    mimo = normalize_mimo_action(mimo_payload)
    review = _normalize_review_action(review_payload["independent_action"])
    differences = _action_differences(mimo, review)
    deterministic_conflicts = _deterministic_conflicts(mimo, review)
    review_conflicts = tuple(
        _token(item) for item in review_payload["conflict_types"]
    )
    conflicts = _ordered_unique((*deterministic_conflicts, *review_conflicts))

    code_critical = bool(deterministic_conflicts)
    model_critical = _allows_model_critical(
        review_payload,
        review_conflicts=review_conflicts,
        automation=automation,
        input_kind=input_kind,
        critical_confidence=critical_confidence,
    )
    review_claims_difference = bool(
        review_payload["material_disagreement"] or review_conflicts
    )
    disagreed = bool(differences or review_claims_difference)

    if code_critical:
        severity = "critical"
        reason = "Deterministic material conflict: " + ", ".join(
            deterministic_conflicts
        )
    elif model_critical:
        severity = "critical"
        reason = str(review_payload["reason"]).strip() or "Supported semantic critical escalation"
    elif disagreed:
        severity = "normal"
        reason = str(review_payload["reason"]).strip() or "Non-critical semantic difference"
    else:
        severity = "none"
        reason = "Normalized meanings are equivalent"

    return SemanticReviewDecision(
        agreement_status="disagreed" if disagreed else "agreed",
        severity=severity,
        conflict_types=conflicts,
        differences=differences,
        reason=reason,
    )


def _normalize_review_action(action: dict[str, Any]) -> dict[str, Any]:
    management = _normalize_management_action(action.get("management_action"))
    action_type = _token(action.get("action_type"))
    if action_type == "position_update" and _is_partial_management(management):
        action_type = "exit_partial"
    return _normalized_action(
        action_type=action_type,
        source=action,
        management_action=management,
    )


def _normalized_action(
    *,
    action_type: str,
    source: dict[str, Any],
    management_action: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "target_lifecycle_id": _normalize_identifier(source.get("target_lifecycle_id")),
        "symbol": _normalize_symbol(source.get("symbol")),
        "side": _normalize_side(source.get("side")),
        "stop_loss": normalize_price(source.get("stop_loss")),
        "take_profit": normalize_price(source.get("take_profit")),
        "management_action": management_action,
    }


def _action_differences(
    mimo: dict[str, Any], review: dict[str, Any]
) -> tuple[str, ...]:
    fields = (
        "action_type",
        "target_lifecycle_id",
        "symbol",
        "side",
        "stop_loss",
        "take_profit",
        "management_action",
    )
    return tuple(field for field in fields if mimo[field] != review[field])


def _deterministic_conflicts(
    mimo: dict[str, Any], review: dict[str, Any]
) -> tuple[str, ...]:
    mimo_action = mimo["action_type"]
    review_action = review["action_type"]
    conflicts: list[str] = []
    if (mimo_action in _ACTIONABLE_TYPES) != (review_action in _ACTIONABLE_TYPES):
        conflicts.append("actionability")
        if mimo_action == "exit_full" and review_action == "none":
            conflicts.append("urgent_exit_missed")
    elif mimo_action in _ACTIONABLE_TYPES and mimo_action != review_action:
        if {mimo_action, review_action} == {"exit_full", "exit_partial"}:
            conflicts.append("full_vs_partial_exit")
        else:
            conflicts.append("action_family")

    if mimo_action in _ACTIONABLE_TYPES and review_action in _ACTIONABLE_TYPES:
        for field, conflict in (
            ("symbol", "symbol"),
            ("side", "side"),
            ("target_lifecycle_id", "target_lifecycle"),
        ):
            if mimo[field] != review[field]:
                conflicts.append(conflict)
    return _ordered_unique(conflicts)


def _allows_model_critical(
    review_payload: dict[str, Any],
    *,
    review_conflicts: tuple[str, ...],
    automation: dict[str, Any],
    input_kind: str,
    critical_confidence: float,
) -> bool:
    if _token(review_payload["suggested_severity"]) != "critical":
        return False
    if not review_payload["material_disagreement"]:
        return False
    if not any(item in _MODEL_CRITICAL_CONFLICTS for item in review_conflicts):
        return False
    evidence = review_payload["evidence"]
    if not any(item.strip() for item in evidence):
        return False
    if float(review_payload["confidence"]) < critical_confidence:
        return False
    if "image" in _token(input_kind) or _token(input_kind) in {"photo", "media"}:
        return _has_text_evidence(automation)
    return True


def _has_text_evidence(automation: dict[str, Any]) -> bool:
    if automation.get("has_independent_text_evidence") is True:
        return True
    for key in ("text_evidence", "message_text", "source_text"):
        value = automation.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _normalize_management_action(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[,，、;/|]+", str(value))
    normalized = {_token(item).replace(" ", "_") for item in raw_items if _token(item)}
    return tuple(sorted(normalized))


def _is_partial_management(actions: tuple[str, ...]) -> bool:
    return bool(_PARTIAL_MANAGEMENT_ACTIONS.intersection(actions))


def _normalize_identifier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value).lower()
    normalized = normalize_price(value)
    if isinstance(normalized, Decimal):
        return format(normalized, "f")
    return _compact_text(value)


def _normalize_symbol(value: Any) -> str | None:
    if value is None:
        return None
    symbol = re.sub(r"[\s_\-/]", "", str(value)).upper()
    symbol = re.sub(r"(?:USDT|USD|PERP)$", "", symbol)
    return symbol or None


def _normalize_side(value: Any) -> str | None:
    side = _token(value).replace(" ", "")
    if side in {"long", "buy", "多", "多单", "做多"}:
        return "long"
    if side in {"short", "sell", "空", "空单", "做空"}:
        return "short"
    return side or None


def _is_entry(value: Any) -> bool:
    return _token(value).replace(" ", "") in {
        "是策略",
        "策略",
        "actionable",
        "entry",
        "true",
        "yes",
    }


def _decimal_or_text(value: Any) -> Decimal | str:
    text = str(value).strip().lower().replace(",", "")
    multiplier = Decimal("10000") if text.endswith(("万", "w")) else Decimal("1")
    if multiplier != 1:
        text = text[:-1]
    try:
        number = Decimal(text) * multiplier
    except (InvalidOperation, ValueError):
        return _compact_text(value)
    if not number.is_finite():
        return _compact_text(value)
    return number.normalize()


def _collapse_price_items(
    items: list[Decimal | str],
) -> Decimal | tuple[Decimal, ...] | str | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    if all(isinstance(item, Decimal) for item in items):
        return tuple(sorted(set(items)))
    normalized = tuple(sorted({str(item) for item in items}))
    return normalized[0] if len(normalized) == 1 else " / ".join(normalized)


def _ordered_unique(values: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _compact_text(value: Any) -> str:
    return " ".join(str(value).strip().lower().split())


def _token(value: Any) -> str:
    return _compact_text(value).replace("-", "_")
