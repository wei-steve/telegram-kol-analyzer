"""Validated per-action contract for authoritative message recognition."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


MAX_AUTHORITATIVE_INSTRUCTIONS = 8
ENTRY_KINDS = frozenset({"entry"})
MANAGEMENT_KINDS = frozenset(
    {
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
SUPPORTED_INSTRUCTION_KINDS = ENTRY_KINDS | MANAGEMENT_KINDS
_MANAGEMENT_PRIORITY = {
    "cancel_pending_entry": 0,
    "full_exit": 0,
    "partial_exit": 0,
    "partial_take_profit": 0,
    "move_stop_to_protect": 0,
    "risk_update": 0,
    "replace_entry": 1,
    "hold_update": 2,
    "entry": 3,
}


class AuthoritativeInstructionError(ValueError):
    """Raised when authoritative instructions are unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class AuthoritativeInstruction:
    kind: str
    confidence: float
    reason: str | None = None
    strategy: dict[str, Any] | None = None
    target_lifecycle_id: int | None = None
    target_thread_id: int | None = None
    parameters: dict[str, Any] | None = None

    @property
    def instruction_kind(self) -> str:
        return "entry" if self.kind == "entry" else "management"


def normalize_authoritative_instructions(
    payload: Mapping[str, Any],
) -> tuple[AuthoritativeInstruction, ...]:
    """Return bounded, canonical instructions without parsing free-form text."""

    explicit = payload.get("instructions")
    if explicit is not None:
        if not isinstance(explicit, list):
            raise AuthoritativeInstructionError("instructions_not_list")
        raw_rows = list(explicit)
    else:
        raw_rows = _legacy_instruction_rows(payload)
    if len(raw_rows) > MAX_AUTHORITATIVE_INSTRUCTIONS:
        raise AuthoritativeInstructionError("instruction_count_exceeded")

    normalized = [
        _normalize_instruction_row(row, ordinal=index)
        for index, row in enumerate(raw_rows)
    ]
    seen: set[str] = set()
    for row in normalized:
        fingerprint = _instruction_identity(row)
        if fingerprint in seen:
            raise AuthoritativeInstructionError("duplicate_instruction")
        seen.add(fingerprint)
    ordered = sorted(
        enumerate(normalized),
        key=lambda item: (_MANAGEMENT_PRIORITY[item[1].kind], item[0]),
    )
    return tuple(row for _, row in ordered)


def _normalize_instruction_row(
    raw: Any,
    *,
    ordinal: int,
) -> AuthoritativeInstruction:
    if not isinstance(raw, Mapping):
        raise AuthoritativeInstructionError(f"instruction_{ordinal}_not_object")
    kind = _canonical_kind(raw.get("kind") or raw.get("action"))
    if kind not in SUPPORTED_INSTRUCTION_KINDS:
        raise AuthoritativeInstructionError(f"instruction_{ordinal}_kind_invalid")
    confidence = _confidence(raw.get("confidence"), ordinal=ordinal)
    strategy = raw.get("strategy")
    if kind in ENTRY_KINDS | {"replace_entry"}:
        strategy = _complete_strategy(strategy, ordinal=ordinal)
    elif strategy not in (None, {}):
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_management_has_strategy"
        )
    else:
        strategy = None
    target = raw.get("target") if isinstance(raw.get("target"), Mapping) else {}
    target_lifecycle_id = _optional_positive_int(
        raw.get("target_lifecycle_id", target.get("lifecycle_id")),
        field=f"instruction_{ordinal}_target_lifecycle_id",
    )
    target_thread_id = _optional_positive_int(
        raw.get("target_thread_id", target.get("thread_id")),
        field=f"instruction_{ordinal}_target_thread_id",
    )
    parameters = raw.get("parameters")
    if parameters is not None and not isinstance(parameters, Mapping):
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_parameters_not_object"
        )
    return AuthoritativeInstruction(
        kind=kind,
        confidence=confidence,
        reason=_optional_text(raw.get("reason")),
        strategy=dict(strategy) if strategy is not None else None,
        target_lifecycle_id=target_lifecycle_id,
        target_thread_id=target_thread_id,
        parameters=dict(parameters) if isinstance(parameters, Mapping) else None,
    )


def _legacy_instruction_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lifecycle = payload.get("lifecycle_event")
    if isinstance(lifecycle, Mapping):
        management_kind = _legacy_management_kind(lifecycle)
        if management_kind is not None:
            rows.append(
                {
                    "kind": management_kind,
                    "confidence": lifecycle.get(
                        "confidence", payload.get("confidence", 0.0)
                    ),
                    "reason": lifecycle.get("reason") or payload.get("reason"),
                    "target_lifecycle_id": lifecycle.get("target_lifecycle_id"),
                    "parameters": {
                        key: lifecycle.get(key)
                        for key in (
                            "management_action",
                            "management_fraction",
                            "stop_loss",
                            "take_profit",
                            "exit_price",
                        )
                        if lifecycle.get(key) not in (None, "")
                    },
                }
            )
    strategy = payload.get("strategy")
    if (
        str(payload.get("recognition_result") or "") == "是策略"
        and isinstance(strategy, Mapping)
        and any(value not in (None, "") for value in strategy.values())
    ):
        rows.append(
            {
                "kind": "entry",
                "confidence": payload.get("confidence", 0.0),
                "reason": payload.get("reason"),
                "strategy": dict(strategy),
            }
        )
    return rows


def _legacy_management_kind(lifecycle: Mapping[str, Any]) -> str | None:
    event_type = str(lifecycle.get("event_type") or "none").strip().lower()
    action = _canonical_kind(lifecycle.get("management_action"))
    if event_type == "cancel_entry":
        return "cancel_pending_entry"
    if event_type == "exit_position":
        return action if action in {"partial_exit", "full_exit"} else "full_exit"
    if event_type == "position_update":
        return action if action in MANAGEMENT_KINDS else "hold_update"
    return None


def _canonical_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "cancel_entry": "cancel_pending_entry",
        "exit_full": "full_exit",
        "exit_partial": "partial_exit",
        "position_update": "hold_update",
    }
    return aliases.get(text, text)


def _complete_strategy(value: Any, *, ordinal: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthoritativeInstructionError(f"instruction_{ordinal}_strategy_missing")
    strategy = {str(key): item for key, item in value.items()}
    required = ("symbol", "side", "entry", "stop_loss", "take_profit")
    if any(strategy.get(field) in (None, "") for field in required):
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_strategy_incomplete"
        )
    side = str(strategy["side"]).strip().lower()
    if side not in {"long", "short"}:
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_strategy_side_invalid"
        )
    strategy["symbol"] = str(strategy["symbol"]).strip().upper()
    strategy["side"] = side
    for field in ("entry", "stop_loss", "take_profit"):
        strategy[field] = str(strategy[field]).strip()
    return strategy


def _confidence(value: Any, *, ordinal: int) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_confidence_invalid"
        ) from exc
    if not 0.0 <= parsed <= 1.0:
        raise AuthoritativeInstructionError(
            f"instruction_{ordinal}_confidence_invalid"
        )
    return parsed


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AuthoritativeInstructionError(f"{field}_invalid") from exc
    if parsed <= 0:
        raise AuthoritativeInstructionError(f"{field}_invalid")
    return parsed


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()[:1000] or None


def _instruction_identity(row: AuthoritativeInstruction) -> str:
    return json.dumps(
        {
            "kind": row.kind,
            "strategy": row.strategy,
            "target_lifecycle_id": row.target_lifecycle_id,
            "target_thread_id": row.target_thread_id,
            "parameters": row.parameters,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
