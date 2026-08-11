"""Pure MiMo v2 projection onto the current authoritative payload contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from telegram_kol_research.authoritative_instructions import (
    AuthoritativeInstruction,
    AuthoritativeInstructionError,
    normalize_authoritative_instructions,
)
from telegram_kol_research.mimo_v2_contract import (
    MimoV2Action,
    MimoV2Intent,
    MimoV2Result,
)


_DIRECT_INSTRUCTION_KINDS = frozenset(
    {
        "entry",
        "cancel_pending_entry",
        "replace_entry",
        "full_exit",
        "partial_exit",
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
        "risk_update",
    }
)
_DIRECT_LIFECYCLE_KINDS = frozenset(
    {
        "confirm_entry",
        "cancel_pending_entry",
        "full_exit",
        "partial_exit",
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
        "risk_update",
    }
)
_MANAGEMENT_LIFECYCLE_MAP = {
    "cancel_pending_entry": ("cancel_entry", "cancel_pending_entry"),
    "full_exit": ("exit_position", "exit_full"),
    "partial_exit": ("exit_position", "exit_partial"),
    "partial_take_profit": ("position_update", "partial_take_profit"),
    "move_stop_to_protect": ("position_update", "move_stop_to_protect"),
    "hold_update": ("position_update", "hold_update"),
    "risk_update": ("position_update", "risk_update"),
}
_PARTIAL_THEN_PROTECT_KINDS = frozenset(
    {"partial_take_profit", "move_stop_to_protect"}
)


class MimoV2ExecutionAdapterError(ValueError):
    """Raised when v2 semantics cannot fit the current execution view safely."""


@dataclass(frozen=True, slots=True)
class AdaptedMimoV2Payload:
    payload: dict[str, Any]
    canonical_v2_json: str
    canonical_v2_fingerprint: str
    projection_fingerprint: str

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]


def adapt_mimo_v2_to_current_payload(
    result: MimoV2Result,
) -> AdaptedMimoV2Payload:
    """Project validated semantics without reading prose, state, or external data."""

    actionable = [intent for intent in result.intents if intent.action is not None]
    entries = _intents_with_kind(actionable, "entry")
    replacements = _intents_with_kind(actionable, "replace_entry")
    lifecycle_intents = [
        intent
        for intent in actionable
        if intent.action is not None
        and intent.action.kind in _DIRECT_LIFECYCLE_KINDS
    ]
    direct_instruction_intents = [
        intent
        for intent in actionable
        if intent.action is not None
        and intent.action.kind in _DIRECT_INSTRUCTION_KINDS
    ]
    fragments = _intents_with_kind(actionable, "entry_fragment")

    if len(entries) > 1:
        raise MimoV2ExecutionAdapterError("unsupported_multiple_entries")
    for intent in lifecycle_intents:
        action = _required_action(intent)
        if (
            action.target_lifecycle_id is None
            and action.target_thread_id is not None
        ):
            raise MimoV2ExecutionAdapterError("unsupported_thread_only_target")
    composite_management = _supported_composite_management(lifecycle_intents)
    if len(lifecycle_intents) > 1 and composite_management is None:
        raise MimoV2ExecutionAdapterError(
            "unsupported_multiple_lifecycle_actions"
        )
    if len(replacements) > 1:
        raise MimoV2ExecutionAdapterError("unsupported_multiple_replacements")
    if replacements and len(direct_instruction_intents) > 1:
        raise MimoV2ExecutionAdapterError("unsupported_replacement_combination")
    risk_fragments = [
        intent
        for intent in fragments
        if intent.action is not None
        and intent.action.parameters.get("fragment_kind") == "risk_multiplier"
    ]
    if len(risk_fragments) > 1:
        raise MimoV2ExecutionAdapterError("unsupported_multiple_entry_contexts")

    instructions = _normalized_instruction_rows(
        direct_instruction_intents,
        composite_management=composite_management,
    )
    if entries:
        strategy_intent = entries[0]
    elif replacements:
        strategy_intent = replacements[0]
    else:
        strategy_intent = None
    strategy = (
        _plain(strategy_intent.action.strategy)
        if strategy_intent is not None and strategy_intent.action is not None
        else {}
    )
    lifecycle_event = (
        _composite_lifecycle_event(*composite_management)
        if composite_management is not None
        else _lifecycle_event(lifecycle_intents[0])
        if lifecycle_intents
        else {
            "event_type": "none",
            "confidence": 0.0,
            "reason": "没有可投影的生命周期动作",
        }
    )
    payload: dict[str, Any] = {
        "instructions": instructions,
        "recognition_result": "是策略" if entries else "非策略",
        "reason": result.summary,
        "summary": result.summary,
        "strategy": strategy,
        "lifecycle_event": lifecycle_event,
        "evidence": _evidence_payload(result),
        "input_reading": {
            "observed_text": result.evidence.text.observed_text,
            "image_quality": _aggregate_image_quality(result),
        },
        "confidence": result.confidence,
    }
    if fragments:
        fragment_rows = [_entry_fragment_row(intent) for intent in fragments]
        payload["entry_fragments"] = fragment_rows
        if risk_fragments:
            payload["entry_context"] = _entry_context_row(risk_fragments[0])

    canonical_v2_json = _canonical_json(_canonical_result_payload(result))
    projection_json = _canonical_json(_execution_projection(payload))
    return AdaptedMimoV2Payload(
        payload=payload,
        canonical_v2_json=canonical_v2_json,
        canonical_v2_fingerprint=_fingerprint(canonical_v2_json),
        projection_fingerprint=_fingerprint(projection_json),
    )


def _intents_with_kind(
    intents: list[MimoV2Intent],
    kind: str,
) -> list[MimoV2Intent]:
    return [
        intent
        for intent in intents
        if intent.action is not None and intent.action.kind == kind
    ]


def _normalized_instruction_rows(
    intents: list[MimoV2Intent],
    *,
    composite_management: tuple[MimoV2Intent, MimoV2Intent] | None,
) -> list[dict[str, Any]]:
    composite_kinds = (
        _PARTIAL_THEN_PROTECT_KINDS
        if composite_management is not None
        else frozenset()
    )
    raw = [
        _instruction_payload(intent)
        for intent in intents
        if _required_action(intent).kind not in composite_kinds
    ]
    if composite_management is not None:
        raw.append(_composite_instruction_payload(*composite_management))
    try:
        normalized = normalize_authoritative_instructions({"instructions": raw})
    except AuthoritativeInstructionError as exc:
        raise MimoV2ExecutionAdapterError(
            f"current_instruction_contract_rejected:{exc}"
        ) from exc
    return [_normalized_instruction_payload(row) for row in normalized]


def _supported_composite_management(
    intents: list[MimoV2Intent],
) -> tuple[MimoV2Intent, MimoV2Intent] | None:
    if len(intents) != 2:
        return None
    by_kind = {_required_action(intent).kind: intent for intent in intents}
    if frozenset(by_kind) != _PARTIAL_THEN_PROTECT_KINDS:
        return None
    partial = by_kind["partial_take_profit"]
    protect = by_kind["move_stop_to_protect"]
    partial_action = _required_action(partial)
    protect_action = _required_action(protect)
    if (
        partial_action.target_lifecycle_id
        != protect_action.target_lifecycle_id
        or partial_action.target_thread_id != protect_action.target_thread_id
    ):
        raise MimoV2ExecutionAdapterError(
            "unsupported_composite_target_disagreement"
        )
    return partial, protect


def _composite_instruction_payload(
    partial: MimoV2Intent,
    protect: MimoV2Intent,
) -> dict[str, Any]:
    partial_action = _required_action(partial)
    protect_action = _required_action(protect)
    return {
        "kind": "partial_take_profit",
        "confidence": min(partial.confidence, protect.confidence),
        "reason": _combined_reason(partial, protect),
        "strategy": None,
        "target": {
            "lifecycle_id": partial_action.target_lifecycle_id,
            "thread_id": partial_action.target_thread_id,
        },
        "parameters": {
            **_plain(partial_action.parameters),
            **_plain(protect_action.parameters),
        },
    }


def _instruction_payload(intent: MimoV2Intent) -> dict[str, Any]:
    action = _required_action(intent)
    return {
        "kind": action.kind,
        "confidence": intent.confidence,
        "reason": intent.reason,
        "strategy": _plain(action.strategy) if action.strategy is not None else None,
        "target": {
            "lifecycle_id": action.target_lifecycle_id,
            "thread_id": action.target_thread_id,
        },
        "parameters": _plain(action.parameters),
    }


def _normalized_instruction_payload(
    instruction: AuthoritativeInstruction,
) -> dict[str, Any]:
    return {
        "kind": instruction.kind,
        "confidence": instruction.confidence,
        "reason": instruction.reason,
        "strategy": (
            dict(instruction.strategy) if instruction.strategy is not None else None
        ),
        "target": {
            "lifecycle_id": instruction.target_lifecycle_id,
            "thread_id": instruction.target_thread_id,
        },
        "parameters": dict(instruction.parameters or {}),
    }


def _lifecycle_event(intent: MimoV2Intent) -> dict[str, Any]:
    action = _required_action(intent)
    if action.kind == "confirm_entry":
        event = {
            "event_type": "entry_confirm",
            "target_lifecycle_id": action.target_lifecycle_id,
            **_plain(action.parameters),
            "confidence": intent.confidence,
            "reason": intent.reason,
        }
        return event
    event_type, management_action = _MANAGEMENT_LIFECYCLE_MAP[action.kind]
    return {
        "event_type": event_type,
        "management_action": management_action,
        "target_lifecycle_id": action.target_lifecycle_id,
        **_plain(action.parameters),
        "confidence": intent.confidence,
        "reason": intent.reason,
    }


def _composite_lifecycle_event(
    partial: MimoV2Intent,
    protect: MimoV2Intent,
) -> dict[str, Any]:
    partial_action = _required_action(partial)
    protect_action = _required_action(protect)
    return {
        "event_type": "position_update",
        "management_action": "partial_take_profit, move_stop_to_protect",
        "target_lifecycle_id": partial_action.target_lifecycle_id,
        **_plain(partial_action.parameters),
        **_plain(protect_action.parameters),
        "confidence": min(partial.confidence, protect.confidence),
        "reason": _combined_reason(partial, protect),
    }


def _combined_reason(first: MimoV2Intent, second: MimoV2Intent) -> str:
    if first.reason == second.reason:
        return first.reason
    return f"{first.reason}；{second.reason}"


def _entry_fragment_row(intent: MimoV2Intent) -> dict[str, Any]:
    action = _required_action(intent)
    parameters = _plain(action.parameters)
    fragment_kind = parameters.pop("fragment_kind")
    return {
        "kind": fragment_kind,
        **parameters,
        "confidence": intent.confidence,
        "reason": intent.reason,
    }


def _entry_context_row(intent: MimoV2Intent) -> dict[str, Any]:
    fragment = _entry_fragment_row(intent)
    return {
        "kind": "entry_preamble",
        "symbol": fragment["symbol"],
        "side": fragment["side"],
        "risk_multiplier": fragment["risk_multiplier"],
        "confidence": fragment["confidence"],
        "reason": fragment["reason"],
    }


def _evidence_payload(result: MimoV2Result) -> dict[str, Any]:
    return {
        "text": {
            "observed_text": result.evidence.text.observed_text,
            "fields": _plain(result.evidence.text.fields),
        },
        "images": [
            {
                "asset_id": image.asset_id,
                "image_type": image.image_type,
                "quality": image.quality,
                "observed_text": image.observed_text,
                "summary": image.summary,
                "fields": _plain(image.fields),
                "confidence": image.confidence,
            }
            for image in result.evidence.images
        ],
        "conflicts": list(result.evidence.conflicts),
    }


def _aggregate_image_quality(result: MimoV2Result) -> str:
    if not result.evidence.images:
        return "none"
    priority = {"clear": 0, "blurry": 1, "cropped": 2, "unreadable": 3}
    return max(
        (image.quality for image in result.evidence.images),
        key=priority.__getitem__,
    )


def _canonical_result_payload(result: MimoV2Result) -> dict[str, Any]:
    return {
        "contract_version": result.contract_version,
        "summary": result.summary,
        "confidence": result.confidence,
        "intents": [
            {
                "intent_type": intent.intent_type,
                "action": (
                    _canonical_action_payload(intent.action)
                    if intent.action is not None
                    else None
                ),
                "reason": intent.reason,
                "confidence": intent.confidence,
                "evidence_refs": list(intent.evidence_refs),
            }
            for intent in result.intents
        ],
        "evidence": _evidence_payload(result),
    }


def _canonical_action_payload(action: MimoV2Action) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "target": {
            "lifecycle_id": action.target_lifecycle_id,
            "thread_id": action.target_thread_id,
        },
        "strategy": _plain(action.strategy) if action.strategy is not None else None,
        "parameters": _plain(action.parameters),
    }


def _execution_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "instructions": [
            {key: _plain(value) for key, value in row.items() if key != "reason"}
            for row in payload["instructions"]
        ],
        "recognition_result": payload["recognition_result"],
        "strategy": _plain(payload["strategy"]),
        "lifecycle_event": {
            key: _plain(value)
            for key, value in payload["lifecycle_event"].items()
            if key != "reason"
        },
        "confidence": payload["confidence"],
        "input_reading": {
            "observed_text": payload["input_reading"]["observed_text"],
        },
    }
    if "entry_context" in payload:
        projection["entry_context"] = {
            key: _plain(value)
            for key, value in payload["entry_context"].items()
            if key != "reason"
        }
    if "entry_fragments" in payload:
        projection["entry_fragments"] = [
            {key: _plain(value) for key, value in row.items() if key != "reason"}
            for row in payload["entry_fragments"]
        ]
    return projection


def _required_action(intent: MimoV2Intent) -> MimoV2Action:
    if intent.action is None:
        raise MimoV2ExecutionAdapterError("action_missing_after_contract_validation")
    return intent.action


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
