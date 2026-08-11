"""Closed, source-attributed contract for authoritative MiMo v2 results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


CONTRACT_VERSION = "mimo-authoritative-v2"
INTENT_TYPES = frozenset(
    {
        "new_strategy",
        "position_management",
        "exit",
        "cancel_entry",
        "strategy_revision",
        "entry_context",
        "position_report",
        "market_commentary",
        "non_trading",
        "unclear",
    }
)
ACTION_KINDS = frozenset(
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
IMAGE_TYPES = frozenset(
    {
        "strategy_screenshot",
        "position_screenshot",
        "order_screenshot",
        "market_chart",
        "profit_review",
        "advertisement",
        "unrelated",
        "unknown",
    }
)
IMAGE_QUALITIES = frozenset({"clear", "blurry", "cropped", "unreadable"})
EVIDENCE_SOURCES = frozenset({"text", "image", "both"})
MAX_INTENTS = 8
MAX_IMAGES = 8
MAX_EVIDENCE_REFS = 24
MAX_CONFLICTS = 24
MAX_SUMMARY_LENGTH = 2_000
MAX_REASON_LENGTH = 4_000
MAX_OBSERVED_TEXT_LENGTH = 10_000

_TOP_LEVEL_FIELDS = {
    "contract_version",
    "summary",
    "confidence",
    "intents",
    "evidence",
}
_INTENT_FIELDS = {
    "intent_type",
    "action",
    "reason",
    "confidence",
    "evidence_refs",
}
_ACTION_FIELDS = {"kind", "target", "strategy", "parameters"}
_TARGET_FIELDS = {"lifecycle_id", "thread_id"}
_EVIDENCE_FIELDS = {"text", "images", "conflicts"}
_TEXT_EVIDENCE_FIELDS = {"observed_text", "fields"}
_IMAGE_EVIDENCE_FIELDS = {
    "asset_id",
    "image_type",
    "quality",
    "observed_text",
    "summary",
    "fields",
    "confidence",
}
_FIELD_EVIDENCE_FIELDS = {"value", "source", "confidence"}
_STRATEGY_ALLOWED_FIELDS = {
    "symbol",
    "side",
    "entry",
    "stop_loss",
    "take_profit",
    "leverage",
    "order_type",
}
_ACTIONABLE_INTENT_TYPES = {
    "new_strategy",
    "position_management",
    "exit",
    "cancel_entry",
    "strategy_revision",
}
_INTENT_ACTIONS = {
    "new_strategy": {"entry"},
    "position_management": {
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
        "risk_update",
    },
    "exit": {"full_exit", "partial_exit"},
    "cancel_entry": {"cancel_pending_entry"},
    "strategy_revision": {"replace_entry"},
}


class MimoV2ContractError(ValueError):
    """Raised when a MiMo v2 response is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class MimoV2Action:
    kind: str
    target_lifecycle_id: int | None
    target_thread_id: int | None
    strategy: dict[str, Any] | None
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MimoV2Intent:
    intent_type: str
    action: MimoV2Action | None
    reason: str
    confidence: float
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MimoV2TextEvidence:
    observed_text: str
    fields: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MimoV2ImageEvidence:
    asset_id: int
    image_type: str
    quality: str
    observed_text: str
    summary: str
    fields: dict[str, dict[str, Any]]
    confidence: float


@dataclass(frozen=True, slots=True)
class MimoV2Evidence:
    text: MimoV2TextEvidence
    images: tuple[MimoV2ImageEvidence, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MimoV2Result:
    contract_version: str
    summary: str
    confidence: float
    intents: tuple[MimoV2Intent, ...]
    evidence: MimoV2Evidence


def parse_mimo_v2_payload(payload: Mapping[str, Any]) -> MimoV2Result:
    """Parse a strict MiMo v2 result without interpreting free-form prose."""

    if not isinstance(payload, Mapping):
        raise MimoV2ContractError("payload_not_object")
    _require_exact_fields(payload, _TOP_LEVEL_FIELDS, "top_level_fields_invalid")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise MimoV2ContractError("contract_version_invalid")

    summary = _bounded_text(
        payload.get("summary"),
        field="summary",
        max_length=MAX_SUMMARY_LENGTH,
    )
    confidence = _confidence(payload.get("confidence"), field="confidence")
    evidence = _parse_evidence(payload.get("evidence"))
    intents = _parse_intents(payload.get("intents"), evidence=evidence)
    return MimoV2Result(
        contract_version=CONTRACT_VERSION,
        summary=summary,
        confidence=confidence,
        intents=intents,
        evidence=evidence,
    )


def _parse_intents(
    raw: Any,
    *,
    evidence: MimoV2Evidence,
) -> tuple[MimoV2Intent, ...]:
    if not isinstance(raw, list):
        raise MimoV2ContractError("intents_not_list")
    if len(raw) > MAX_INTENTS:
        raise MimoV2ContractError("intent_count_exceeded")
    parsed: list[MimoV2Intent] = []
    seen_actions: set[str] = set()
    for ordinal, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise MimoV2ContractError(f"intent_{ordinal}_not_object")
        _require_exact_fields(
            row,
            _INTENT_FIELDS,
            f"intent_{ordinal}_fields_invalid",
        )
        intent_type = str(row.get("intent_type") or "").strip()
        if intent_type not in INTENT_TYPES:
            raise MimoV2ContractError(f"intent_{ordinal}_type_invalid")
        action = _parse_action(
            row.get("action"),
            ordinal=ordinal,
            intent_type=intent_type,
        )
        if intent_type in _ACTIONABLE_INTENT_TYPES and action is None:
            raise MimoV2ContractError(f"intent_{ordinal}_action_missing")
        if intent_type not in _ACTIONABLE_INTENT_TYPES and action is not None:
            raise MimoV2ContractError(f"intent_{ordinal}_action_not_allowed")
        if action is not None:
            identity = _action_identity(action)
            if identity in seen_actions:
                raise MimoV2ContractError("duplicate_action")
            seen_actions.add(identity)
        refs = _parse_evidence_refs(
            row.get("evidence_refs"),
            ordinal=ordinal,
            evidence=evidence,
        )
        parsed.append(
            MimoV2Intent(
                intent_type=intent_type,
                action=action,
                reason=_bounded_text(
                    row.get("reason"),
                    field=f"intent_{ordinal}_reason",
                    max_length=MAX_REASON_LENGTH,
                ),
                confidence=_confidence(
                    row.get("confidence"),
                    field=f"intent_{ordinal}_confidence",
                ),
                evidence_refs=refs,
            )
        )
    return tuple(parsed)


def _parse_action(
    raw: Any,
    *,
    ordinal: int,
    intent_type: str,
) -> MimoV2Action | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"intent_{ordinal}_action_not_object")
    _require_exact_fields(
        raw,
        _ACTION_FIELDS,
        f"intent_{ordinal}_action_fields_invalid",
    )
    kind = str(raw.get("kind") or "").strip()
    if kind not in ACTION_KINDS:
        raise MimoV2ContractError(f"intent_{ordinal}_action_kind_invalid")
    if kind not in _INTENT_ACTIONS.get(intent_type, set()):
        raise MimoV2ContractError(f"intent_{ordinal}_action_intent_mismatch")

    target = raw.get("target")
    if not isinstance(target, Mapping):
        raise MimoV2ContractError(f"intent_{ordinal}_target_not_object")
    _require_exact_fields(
        target,
        _TARGET_FIELDS,
        f"intent_{ordinal}_target_fields_invalid",
    )
    lifecycle_id = _optional_positive_int(
        target.get("lifecycle_id"),
        field=f"intent_{ordinal}_target_lifecycle_id",
    )
    thread_id = _optional_positive_int(
        target.get("thread_id"),
        field=f"intent_{ordinal}_target_thread_id",
    )

    strategy = raw.get("strategy")
    if kind in {"entry", "replace_entry"}:
        strategy = _parse_complete_strategy(strategy, ordinal=ordinal)
    elif strategy not in (None, {}):
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_not_allowed")
    else:
        strategy = None
    parameters = raw.get("parameters")
    if not isinstance(parameters, Mapping):
        raise MimoV2ContractError(f"intent_{ordinal}_parameters_not_object")
    return MimoV2Action(
        kind=kind,
        target_lifecycle_id=lifecycle_id,
        target_thread_id=thread_id,
        strategy=dict(strategy) if isinstance(strategy, Mapping) else None,
        parameters=dict(parameters),
    )


def _parse_complete_strategy(raw: Any, *, ordinal: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_missing")
    if set(raw) - _STRATEGY_ALLOWED_FIELDS:
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_fields_invalid")
    required = ("symbol", "side", "entry", "stop_loss", "take_profit")
    if any(raw.get(field) in (None, "") for field in required):
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_incomplete")
    side = str(raw.get("side") or "").strip().lower()
    if side not in {"long", "short"}:
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_side_invalid")
    strategy = dict(raw)
    strategy["symbol"] = str(raw["symbol"]).strip().upper()
    strategy["side"] = side
    for field in ("entry", "stop_loss", "take_profit"):
        strategy[field] = str(raw[field]).strip()
    return strategy


def _parse_evidence(raw: Any) -> MimoV2Evidence:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError("evidence_not_object")
    _require_exact_fields(raw, _EVIDENCE_FIELDS, "evidence_fields_invalid")
    text = _parse_text_evidence(raw.get("text"))
    images_raw = raw.get("images")
    if not isinstance(images_raw, list):
        raise MimoV2ContractError("images_not_list")
    if len(images_raw) > MAX_IMAGES:
        raise MimoV2ContractError("image_count_exceeded")
    images: list[MimoV2ImageEvidence] = []
    seen_asset_ids: set[int] = set()
    for ordinal, image in enumerate(images_raw):
        parsed = _parse_image_evidence(image, ordinal=ordinal)
        if parsed.asset_id in seen_asset_ids:
            raise MimoV2ContractError("duplicate_image_asset_id")
        seen_asset_ids.add(parsed.asset_id)
        images.append(parsed)
    conflicts_raw = raw.get("conflicts")
    if not isinstance(conflicts_raw, list) or not all(
        isinstance(value, str) for value in conflicts_raw
    ):
        raise MimoV2ContractError("conflicts_invalid")
    if len(conflicts_raw) > MAX_CONFLICTS:
        raise MimoV2ContractError("conflict_count_exceeded")
    return MimoV2Evidence(
        text=text,
        images=tuple(images),
        conflicts=tuple(value.strip() for value in conflicts_raw),
    )


def _parse_text_evidence(raw: Any) -> MimoV2TextEvidence:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError("text_evidence_not_object")
    _require_exact_fields(raw, _TEXT_EVIDENCE_FIELDS, "text_evidence_fields_invalid")
    return MimoV2TextEvidence(
        observed_text=_bounded_text(
            raw.get("observed_text"),
            field="text_observed_text",
            max_length=MAX_OBSERVED_TEXT_LENGTH,
        ),
        fields=_parse_evidence_field_map(raw.get("fields"), prefix="text"),
    )


def _parse_image_evidence(raw: Any, *, ordinal: int) -> MimoV2ImageEvidence:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"image_{ordinal}_not_object")
    _require_exact_fields(
        raw,
        _IMAGE_EVIDENCE_FIELDS,
        f"image_{ordinal}_fields_invalid",
    )
    asset_id = _optional_positive_int(
        raw.get("asset_id"),
        field=f"image_{ordinal}_asset_id",
    )
    if asset_id is None:
        raise MimoV2ContractError(f"image_{ordinal}_asset_id_invalid")
    image_type = str(raw.get("image_type") or "").strip()
    if image_type not in IMAGE_TYPES:
        raise MimoV2ContractError(f"image_{ordinal}_type_invalid")
    quality = str(raw.get("quality") or "").strip()
    if quality not in IMAGE_QUALITIES:
        raise MimoV2ContractError(f"image_{ordinal}_quality_invalid")
    return MimoV2ImageEvidence(
        asset_id=asset_id,
        image_type=image_type,
        quality=quality,
        observed_text=_bounded_text(
            raw.get("observed_text"),
            field=f"image_{ordinal}_observed_text",
            max_length=MAX_OBSERVED_TEXT_LENGTH,
        ),
        summary=_bounded_text(
            raw.get("summary"),
            field=f"image_{ordinal}_summary",
            max_length=MAX_SUMMARY_LENGTH,
        ),
        fields=_parse_evidence_field_map(
            raw.get("fields"),
            prefix=f"image_{ordinal}",
        ),
        confidence=_confidence(
            raw.get("confidence"),
            field=f"image_{ordinal}_confidence",
        ),
    )


def _parse_evidence_field_map(raw: Any, *, prefix: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"{prefix}_evidence_fields_not_object")
    fields: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        field_name = str(name).strip()
        if not field_name or not isinstance(value, Mapping):
            raise MimoV2ContractError(f"{prefix}_field_invalid")
        _require_exact_fields(
            value,
            _FIELD_EVIDENCE_FIELDS,
            f"{prefix}_{field_name}_fields_invalid",
        )
        source = str(value.get("source") or "").strip()
        if source not in EVIDENCE_SOURCES:
            raise MimoV2ContractError(f"{prefix}_{field_name}_source_invalid")
        fields[field_name] = {
            "value": value.get("value"),
            "source": source,
            "confidence": _confidence(
                value.get("confidence"),
                field=f"{prefix}_{field_name}_confidence",
            ),
        }
    return fields


def _parse_evidence_refs(
    raw: Any,
    *,
    ordinal: int,
    evidence: MimoV2Evidence,
) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise MimoV2ContractError(f"intent_{ordinal}_evidence_refs_invalid")
    if len(raw) > MAX_EVIDENCE_REFS:
        raise MimoV2ContractError(f"intent_{ordinal}_evidence_ref_count_exceeded")
    images = {image.asset_id: image for image in evidence.images}
    seen: set[str] = set()
    refs: list[str] = []
    for value in raw:
        ref = value.strip()
        if ref in seen:
            raise MimoV2ContractError(f"intent_{ordinal}_evidence_ref_duplicate")
        seen.add(ref)
        parts = ref.split(":")
        if len(parts) == 2 and parts[0] == "text":
            field = parts[1]
            if field != "observed_text" and field not in evidence.text.fields:
                raise MimoV2ContractError(
                    f"intent_{ordinal}_evidence_ref_field_missing"
                )
        elif len(parts) == 3 and parts[0] == "image":
            try:
                asset_id = int(parts[1])
            except ValueError as exc:
                raise MimoV2ContractError(
                    f"intent_{ordinal}_evidence_ref_invalid"
                ) from exc
            image = images.get(asset_id)
            if image is None:
                raise MimoV2ContractError(
                    f"intent_{ordinal}_evidence_ref_image_missing"
                )
            field = parts[2]
            if field not in {
                "quality",
                "observed_text",
                "summary",
                "confidence",
            } and field not in image.fields:
                raise MimoV2ContractError(
                    f"intent_{ordinal}_evidence_ref_field_missing"
                )
        else:
            raise MimoV2ContractError(f"intent_{ordinal}_evidence_ref_invalid")
        refs.append(ref)
    return tuple(refs)


def _action_identity(action: MimoV2Action) -> str:
    return json.dumps(
        {
            "kind": action.kind,
            "target": {
                "lifecycle_id": action.target_lifecycle_id,
                "thread_id": action.target_thread_id,
            },
            "strategy": action.strategy,
            "parameters": action.parameters,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _confidence(raw: Any, *, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise MimoV2ContractError(f"{field}_invalid")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise MimoV2ContractError(f"{field}_invalid")
    return value


def _optional_positive_int(raw: Any, *, field: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise MimoV2ContractError(f"{field}_invalid")
    return int(raw)


def _bounded_text(raw: Any, *, field: str, max_length: int) -> str:
    if not isinstance(raw, str):
        raise MimoV2ContractError(f"{field}_invalid")
    value = raw.strip()
    if len(value) > max_length:
        raise MimoV2ContractError(f"{field}_too_long")
    return value


def _require_exact_fields(
    raw: Mapping[str, Any],
    expected: set[str],
    error: str,
) -> None:
    if set(raw) != expected:
        raise MimoV2ContractError(error)
