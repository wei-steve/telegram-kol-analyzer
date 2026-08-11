"""Closed, source-attributed contract for authoritative MiMo v2 results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


CONTRACT_VERSION = "mimo-authoritative-v2"
INTENT_TYPES = frozenset(
    {
        "new_strategy",
        "entry_confirmation",
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
        "confirm_entry",
        "entry_fragment",
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
MAX_EVIDENCE_FIELDS = 32
MAX_EVIDENCE_FIELD_NAME_LENGTH = 64
MAX_EVIDENCE_FIELD_VALUE_LENGTH = 2_000
MAX_PARAMETER_TEXT_LENGTH = 255
MAX_DECIMAL_TEXT_LENGTH = 128
MAX_SQLITE_INTEGER = 2**63 - 1

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
    "entry_confirmation",
    "position_management",
    "exit",
    "cancel_entry",
    "strategy_revision",
    "entry_context",
}
_INTENT_ACTIONS = {
    "new_strategy": {"entry"},
    "entry_confirmation": {"confirm_entry"},
    "position_management": {
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
        "risk_update",
    },
    "exit": {"full_exit", "partial_exit"},
    "cancel_entry": {"cancel_pending_entry"},
    "strategy_revision": {"replace_entry"},
    "entry_context": {"entry_fragment"},
}
_ACTION_PARAMETER_FIELDS = {
    "entry": frozenset(),
    "confirm_entry": frozenset({"entry_price"}),
    "entry_fragment": frozenset(
        {
            "fragment_kind",
            "symbol",
            "side",
            "risk_multiplier",
            "allocations",
            "entry_price",
        }
    ),
    "cancel_pending_entry": frozenset(),
    "replace_entry": frozenset(),
    "full_exit": frozenset({"exit_price"}),
    "partial_exit": frozenset({"exit_price", "management_fraction"}),
    "partial_take_profit": frozenset(
        {"exit_price", "take_profit", "management_fraction"}
    ),
    "move_stop_to_protect": frozenset({"stop_loss"}),
    "hold_update": frozenset({"stop_loss", "take_profit"}),
    "risk_update": frozenset(
        {"stop_loss", "take_profit", "risk_multiplier", "leverage"}
    ),
}


class MimoV2ContractError(ValueError):
    """Raised when a MiMo v2 response is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class MimoV2Action:
    kind: str
    target_lifecycle_id: int | None
    target_thread_id: int | None
    strategy: Mapping[str, Any] | None
    parameters: Mapping[str, Any]


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
    fields: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class MimoV2ImageEvidence:
    asset_id: int
    image_type: str
    quality: str
    observed_text: str
    summary: str
    fields: Mapping[str, Mapping[str, Any]]
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
    if not raw:
        raise MimoV2ContractError("intent_count_invalid")
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
        if intent_type in _ACTIONABLE_INTENT_TYPES and not refs:
            raise MimoV2ContractError(
                f"intent_{ordinal}_evidence_refs_missing"
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
    parameters = _parse_action_parameters(
        raw.get("parameters"),
        ordinal=ordinal,
        kind=kind,
        lifecycle_id=lifecycle_id,
        thread_id=thread_id,
    )
    return MimoV2Action(
        kind=kind,
        target_lifecycle_id=lifecycle_id,
        target_thread_id=thread_id,
        strategy=(
            MappingProxyType(dict(strategy))
            if isinstance(strategy, Mapping)
            else None
        ),
        parameters=MappingProxyType(parameters),
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
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol.strip()) > 32:
        raise MimoV2ContractError(f"intent_{ordinal}_strategy_symbol_invalid")
    strategy = dict(raw)
    strategy["symbol"] = symbol.strip().upper()
    strategy["side"] = side
    for field in ("entry", "stop_loss", "take_profit"):
        strategy[field] = _bounded_parameter_text(
            raw[field],
            error=f"intent_{ordinal}_strategy_{field}_invalid",
        )
    leverage = raw.get("leverage")
    if leverage is not None:
        strategy["leverage"] = _bounded_parameter_text(
            leverage,
            error=f"intent_{ordinal}_strategy_leverage_invalid",
            max_length=32,
        )
    order_type = raw.get("order_type")
    if order_type is not None:
        if not isinstance(order_type, str) or order_type.strip() not in {
            "market",
            "limit",
            "market+limit",
        }:
            raise MimoV2ContractError(
                f"intent_{ordinal}_strategy_order_type_invalid"
            )
        strategy["order_type"] = order_type.strip()
    return strategy


def _parse_action_parameters(
    raw: Any,
    *,
    ordinal: int,
    kind: str,
    lifecycle_id: int | None,
    thread_id: int | None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"intent_{ordinal}_parameters_not_object")
    allowed = _ACTION_PARAMETER_FIELDS[kind]
    if set(raw) - allowed:
        raise MimoV2ContractError(
            f"intent_{ordinal}_parameters_fields_invalid"
        )
    if kind in {"entry", "replace_entry", "cancel_pending_entry"} and raw:
        raise MimoV2ContractError(
            f"intent_{ordinal}_parameters_fields_invalid"
        )
    if kind == "entry_fragment":
        if lifecycle_id is not None or thread_id is not None:
            raise MimoV2ContractError(
                f"intent_{ordinal}_entry_fragment_target_not_allowed"
            )
        return _parse_entry_fragment_parameters(raw, ordinal=ordinal)

    normalized: dict[str, Any] = {}
    for field, value in raw.items():
        if field == "management_fraction":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < float(value) <= 1
            ):
                raise MimoV2ContractError(
                    f"intent_{ordinal}_management_fraction_invalid"
                )
            normalized[field] = float(value)
        elif field == "risk_multiplier":
            normalized[field] = _bounded_positive_decimal(
                value,
                error=f"intent_{ordinal}_risk_multiplier_invalid",
                maximum=Decimal("1"),
            )
        elif field == "leverage":
            normalized[field] = _bounded_parameter_text(
                value,
                error=f"intent_{ordinal}_leverage_invalid",
                max_length=32,
            )
        else:
            normalized[field] = _bounded_parameter_text(
                value,
                error=f"intent_{ordinal}_{field}_invalid",
            )
    return normalized


def _parse_entry_fragment_parameters(
    raw: Mapping[str, Any],
    *,
    ordinal: int,
) -> dict[str, Any]:
    fragment_kind = str(raw.get("fragment_kind") or "").strip()
    common = {"fragment_kind", "symbol", "side"}
    kind_fields = {
        "risk_multiplier": common | {"risk_multiplier"},
        "leg_allocation": common | {"allocations"},
        "supplemental_entry": common | {"entry_price"},
    }
    expected = kind_fields.get(fragment_kind)
    if expected is None or set(raw) != expected:
        raise MimoV2ContractError(
            f"intent_{ordinal}_entry_fragment_fields_invalid"
        )
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol.strip()) > 32:
        raise MimoV2ContractError(
            f"intent_{ordinal}_entry_fragment_symbol_invalid"
        )
    side = str(raw.get("side") or "").strip().lower()
    if side not in {"long", "short"}:
        raise MimoV2ContractError(
            f"intent_{ordinal}_entry_fragment_side_invalid"
        )
    normalized: dict[str, Any] = {
        "fragment_kind": fragment_kind,
        "symbol": symbol.strip().upper(),
        "side": side,
    }
    if fragment_kind == "risk_multiplier":
        normalized["risk_multiplier"] = _bounded_positive_decimal(
            raw.get("risk_multiplier"),
            error=f"intent_{ordinal}_entry_fragment_multiplier_invalid",
            maximum=Decimal("1"),
        )
    elif fragment_kind == "leg_allocation":
        values = raw.get("allocations")
        if not isinstance(values, list) or not 1 <= len(values) <= 5:
            raise MimoV2ContractError(
                f"intent_{ordinal}_entry_fragment_allocations_invalid"
            )
        allocations = tuple(
            _bounded_positive_decimal(
                value,
                error=f"intent_{ordinal}_entry_fragment_allocations_invalid",
                maximum=Decimal("1"),
            )
            for value in values
        )
        if sum(Decimal(value) for value in allocations) != Decimal("1"):
            raise MimoV2ContractError(
                f"intent_{ordinal}_entry_fragment_allocations_invalid"
            )
        normalized["allocations"] = allocations
    else:
        normalized["entry_price"] = _bounded_positive_decimal(
            raw.get("entry_price"),
            error=f"intent_{ordinal}_entry_fragment_price_invalid",
        )
    return normalized


def _bounded_positive_decimal(
    value: Any,
    *,
    error: str,
    maximum: Decimal | None = None,
) -> str:
    if isinstance(value, bool):
        raise MimoV2ContractError(error)
    raw = str(value).strip()
    if not raw or len(raw) > MAX_DECIMAL_TEXT_LENGTH:
        raise MimoV2ContractError(error)
    if "e" in raw.lower():
        _, exponent = raw.lower().rsplit("e", 1)
        exponent_digits = exponent.lstrip("+-")
        if (
            not exponent_digits.isdigit()
            or len(exponent_digits) > 4
            or abs(int(exponent)) > MAX_DECIMAL_TEXT_LENGTH
        ):
            raise MimoV2ContractError(error)
    try:
        decimal = Decimal(raw)
    except (DecimalException, InvalidOperation, TypeError, ValueError) as exc:
        raise MimoV2ContractError(error) from exc
    if not decimal.is_finite() or decimal <= 0:
        raise MimoV2ContractError(error)
    if maximum is not None and decimal > maximum:
        raise MimoV2ContractError(error)
    if len(decimal.as_tuple().digits) > MAX_DECIMAL_TEXT_LENGTH:
        raise MimoV2ContractError(error)
    if not -MAX_DECIMAL_TEXT_LENGTH <= decimal.adjusted() <= MAX_DECIMAL_TEXT_LENGTH:
        raise MimoV2ContractError(error)
    try:
        normalized = format(decimal.normalize(), "f")
    except DecimalException as exc:
        raise MimoV2ContractError(error) from exc
    if len(normalized) > MAX_DECIMAL_TEXT_LENGTH:
        raise MimoV2ContractError(error)
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _bounded_parameter_text(
    value: Any,
    *,
    error: str,
    max_length: int = MAX_PARAMETER_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise MimoV2ContractError(error)
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise MimoV2ContractError(error)
    return normalized


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
    conflicts = tuple(
        _bounded_text(
            value,
            field=f"conflict_{ordinal}",
            max_length=MAX_REASON_LENGTH,
        )
        for ordinal, value in enumerate(conflicts_raw)
    )
    return MimoV2Evidence(
        text=text,
        images=tuple(images),
        conflicts=conflicts,
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


def _parse_evidence_field_map(
    raw: Any,
    *,
    prefix: str,
) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise MimoV2ContractError(f"{prefix}_evidence_fields_not_object")
    if len(raw) > MAX_EVIDENCE_FIELDS:
        raise MimoV2ContractError(f"{prefix}_evidence_field_count_exceeded")
    fields: dict[str, Mapping[str, Any]] = {}
    for name, value in raw.items():
        field_name = str(name).strip()
        if (
            not field_name
            or len(field_name) > MAX_EVIDENCE_FIELD_NAME_LENGTH
            or not isinstance(value, Mapping)
        ):
            raise MimoV2ContractError(f"{prefix}_field_invalid")
        _require_exact_fields(
            value,
            _FIELD_EVIDENCE_FIELDS,
            f"{prefix}_{field_name}_fields_invalid",
        )
        source = str(value.get("source") or "").strip()
        if source not in EVIDENCE_SOURCES:
            raise MimoV2ContractError(f"{prefix}_{field_name}_source_invalid")
        fields[field_name] = MappingProxyType(
            {
                "value": _parse_evidence_field_value(
                    value.get("value"),
                    field=f"{prefix}_{field_name}",
                ),
                "source": source,
                "confidence": _confidence(
                    value.get("confidence"),
                    field=f"{prefix}_{field_name}_confidence",
                ),
            }
        )
    return MappingProxyType(fields)


def _parse_evidence_field_value(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if len(str(value)) > 128:
            raise MimoV2ContractError(f"{field}_field_value_too_long")
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise MimoV2ContractError(f"{field}_field_value_invalid")
        return value
    if isinstance(value, str):
        if len(value) > MAX_EVIDENCE_FIELD_VALUE_LENGTH:
            raise MimoV2ContractError(f"{field}_field_value_too_long")
        return value
    raise MimoV2ContractError(f"{field}_field_value_invalid")


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
            "strategy": _plain_json_value(action.strategy),
            "parameters": _plain_json_value(action.parameters),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json_value(item) for item in value]
    return value


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
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw <= 0
        or raw > MAX_SQLITE_INTEGER
    ):
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
