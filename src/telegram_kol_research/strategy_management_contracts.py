"""Immutable, canonical contracts for composite position management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


COMPOSITE_MANAGEMENT_CONTRACT_VERSION = 2
ALLOWED_COMPONENTS = frozenset(
    {
        "cancel_deferred_entries",
        "consume_take_profit_stage",
        "converge_partial_close",
        "replace_remaining_protection",
    }
)


def _canonical_decimal(value: str | Decimal, *, field_name: str) -> str:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    normalized = format(decimal_value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


@dataclass(frozen=True, slots=True)
class ManagementInstructionContract:
    version: int
    target_lifecycle_id: int | None
    strategy_instance_id: str | None
    symbol: str | None
    side: str | None
    close_fraction: str | None
    stop_mode: str | None
    stop_price: str | None
    stop_price_source: str | None
    take_profit_consumption: str | None
    cancel_deferred_entries: bool
    required_components: tuple[str, ...]
    current_message_text: str

    def __post_init__(self) -> None:
        if self.version != COMPOSITE_MANAGEMENT_CONTRACT_VERSION:
            raise ValueError(f"unsupported management contract version: {self.version}")
        components = tuple(str(value).strip() for value in self.required_components)
        if any(not value for value in components):
            raise ValueError("required_components cannot contain empty values")
        if len(components) != len(set(components)):
            raise ValueError("duplicate required_components are forbidden")
        unknown_components = set(components) - ALLOWED_COMPONENTS
        if unknown_components:
            raise ValueError(
                "unknown required_components: "
                + ", ".join(sorted(unknown_components))
            )
        object.__setattr__(self, "required_components", components)

        if self.close_fraction is not None:
            fraction = _canonical_decimal(
                self.close_fraction, field_name="close_fraction"
            )
            decimal_fraction = Decimal(fraction)
            if decimal_fraction <= 0 or decimal_fraction > 1:
                raise ValueError("close_fraction must be in (0, 1]")
            object.__setattr__(self, "close_fraction", fraction)

        if self.stop_mode not in {None, "explicit_price", "actual_entry_price"}:
            raise ValueError(f"unsupported stop_mode: {self.stop_mode}")
        if self.stop_price is not None:
            stop_price = _canonical_decimal(
                self.stop_price, field_name="stop_price"
            )
            if Decimal(stop_price) <= 0:
                raise ValueError("stop_price must be positive")
            object.__setattr__(self, "stop_price", stop_price)
        if self.stop_mode == "explicit_price":
            if self.stop_price is None:
                raise ValueError("explicit_price requires stop_price")
            # Necessary origin evidence only: QQ/phone numbers and timestamps
            # also occur in text. The position-aware management stop gate must
            # independently validate meaning, direction and price deviation.
            if (
                self.stop_price_source != "current_message_text"
                or not str(self.current_message_text or "").strip()
            ):
                raise ValueError(
                    "explicit stop requires current_message_text provenance"
                )
        elif self.stop_price is not None:
            raise ValueError("stop_price is only valid for explicit_price")
        if self.take_profit_consumption not in {None, "consume_first_stage"}:
            raise ValueError("unsupported take_profit_consumption")
        if not isinstance(self.cancel_deferred_entries, bool):
            raise ValueError("cancel_deferred_entries must be boolean")


def serialize_management_contract(contract: ManagementInstructionContract) -> str:
    return json.dumps(
        asdict(contract),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def management_contract_fingerprint(
    contract: ManagementInstructionContract,
) -> str:
    payload = serialize_management_contract(contract).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_management_contract(value: str) -> ManagementInstructionContract:
    try:
        payload: Any = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("management contract must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("management contract must be a JSON object")
    if isinstance(payload.get("required_components"), list):
        payload["required_components"] = tuple(payload["required_components"])
    try:
        return ManagementInstructionContract(**payload)
    except TypeError as exc:
        raise ValueError("management contract schema is invalid") from exc
