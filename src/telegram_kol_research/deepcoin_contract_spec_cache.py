"""Validation models for authoritative Deepcoin contract-spec snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any
from typing import Mapping

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION = 1
DEEPCOIN_CONTRACT_SPEC_SOURCE_PATH = "/deepcoin/market/instruments?instType=SWAP"

_INSTRUMENT_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
_REQUIRED_FIELDS = (
    "instType",
    "instId",
    "ctVal",
    "lotSz",
    "minSz",
    "tickSz",
    "state",
)
_NUMERIC_FIELDS = ("ctVal", "lotSz", "minSz", "tickSz")


@dataclass(frozen=True)
class DeepcoinInstrumentCapability:
    """One validated USDT perpetual, including non-live instruments."""

    instrument_id: str
    state: str
    contract_value: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal

    @property
    def contract_spec(self) -> DeepcoinContractSpec:
        """Convert exact exchange values to the legacy execution spec type."""

        return DeepcoinContractSpec(
            instrument_id=self.instrument_id,
            contract_value=_finite_float(self.contract_value, field="ctVal"),
            quantity_step=_finite_float(self.quantity_step, field="lotSz"),
            min_quantity=_finite_float(self.min_quantity, field="minSz"),
            price_tick=_finite_float(self.price_tick, field="tickSz"),
        )


@dataclass(frozen=True)
class DeepcoinContractSpecSnapshot:
    """An immutable, complete view of Deepcoin's validated USDT swaps."""

    schema_version: int
    venue: str
    source_path: str
    fetched_at: datetime
    expires_at: datetime
    source_digest_sha256: str
    capabilities_by_instrument_id: Mapping[str, DeepcoinInstrumentCapability]

    def __post_init__(self) -> None:
        # A frozen dataclass alone does not protect a caller-owned mutable dict.
        # Copying before wrapping makes the snapshot immutable at its boundary.
        object.__setattr__(
            self,
            "capabilities_by_instrument_id",
            MappingProxyType(dict(self.capabilities_by_instrument_id)),
        )

    @property
    def specs_by_instrument_id(self) -> Mapping[str, DeepcoinContractSpec]:
        """Return only live specifications eligible for new-entry decisions."""

        return MappingProxyType(
            {
                instrument_id: capability.contract_spec
                for instrument_id, capability in self.capabilities_by_instrument_id.items()
                if capability.state == "live"
            }
        )

    @property
    def states_by_instrument_id(self) -> Mapping[str, str]:
        """Return platform states for live and non-live validated instruments."""

        return MappingProxyType(
            {
                instrument_id: capability.state
                for instrument_id, capability in self.capabilities_by_instrument_id.items()
            }
        )

    def to_instrument_rows(self) -> list[dict[str, str]]:
        """Return canonical JSON-safe rows without losing Decimal precision."""

        return [
            {
                "instType": "SWAP",
                "instId": capability.instrument_id,
                "ctVal": _canonical_decimal(capability.contract_value),
                "lotSz": _canonical_decimal(capability.quantity_step),
                "minSz": _canonical_decimal(capability.min_quantity),
                "tickSz": _canonical_decimal(capability.price_tick),
                "state": capability.state,
            }
            for _, capability in sorted(self.capabilities_by_instrument_id.items())
        ]


@dataclass(frozen=True)
class _ValidatedInstrument:
    instrument_id: str
    state: str
    contract_value: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal


def validate_deepcoin_instrument_snapshot(
    rows: object,
    *,
    fetched_at: datetime,
    ttl: timedelta,
    source_path: str = DEEPCOIN_CONTRACT_SPEC_SOURCE_PATH,
) -> DeepcoinContractSpecSnapshot:
    """Validate a complete API candidate or reject it without partial results."""

    _validate_snapshot_timing(fetched_at=fetched_at, ttl=ttl)
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("source_path must be a non-empty string")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Deepcoin instrument snapshot must be a non-empty list")

    validated_by_id: dict[str, _ValidatedInstrument] = {}
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"Deepcoin instrument row {index} must be a mapping")

        inst_type = _required_string(raw_row, "instType", row_index=index)
        if inst_type != "SWAP":
            raise ValueError(f"Deepcoin instrument row {index} instType must be SWAP")

        instrument_id = _required_string(raw_row, "instId", row_index=index)
        if not _INSTRUMENT_ID_PATTERN.fullmatch(instrument_id):
            raise ValueError(f"Deepcoin instrument row {index} has invalid instId")

        # The endpoint can legitimately contain swaps settled in other quote
        # currencies. They are outside this USDT-only execution capability.
        if not instrument_id.endswith("-USDT-SWAP"):
            continue

        for field in _REQUIRED_FIELDS:
            if field not in raw_row:
                raise ValueError(
                    f"Deepcoin instrument row {index} is missing required field {field}"
                )

        if instrument_id in validated_by_id:
            raise ValueError(f"duplicate Deepcoin instrument id {instrument_id}")

        state = _required_string(raw_row, "state", row_index=index).strip().lower()
        if not state:
            raise ValueError(f"Deepcoin instrument row {index} has invalid state")
        numbers = {
            field: _positive_decimal(raw_row[field], field=field, row_index=index)
            for field in _NUMERIC_FIELDS
        }
        if not _is_integral_multiple(numbers["minSz"], numbers["lotSz"]):
            raise ValueError(
                f"Deepcoin instrument row {index} minSz must be compatible with lotSz"
            )

        validated_by_id[instrument_id] = _ValidatedInstrument(
            instrument_id=instrument_id,
            state=state,
            contract_value=numbers["ctVal"],
            quantity_step=numbers["lotSz"],
            min_quantity=numbers["minSz"],
            price_tick=numbers["tickSz"],
        )

    if not validated_by_id:
        raise ValueError("Deepcoin instrument snapshot has no USDT SWAP instruments")

    capabilities = {
        instrument_id: DeepcoinInstrumentCapability(
            instrument_id=instrument_id,
            state=validated.state,
            contract_value=validated.contract_value,
            quantity_step=validated.quantity_step,
            min_quantity=validated.min_quantity,
            price_tick=validated.price_tick,
        )
        for instrument_id, validated in sorted(validated_by_id.items())
    }
    # Exercise the legacy float conversion only after the entire response has
    # passed structural and Decimal validation. Non-live rows are checked too,
    # because they remain part of the platform capability snapshot.
    for capability in capabilities.values():
        capability.contract_spec
    digest = _snapshot_digest(validated_by_id)
    normalized_fetched_at = fetched_at.astimezone(timezone.utc)
    return DeepcoinContractSpecSnapshot(
        schema_version=DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION,
        venue="deepcoin",
        source_path=source_path.strip(),
        fetched_at=normalized_fetched_at,
        expires_at=normalized_fetched_at + ttl,
        source_digest_sha256=digest,
        capabilities_by_instrument_id=capabilities,
    )


def _validate_snapshot_timing(*, fetched_at: datetime, ttl: timedelta) -> None:
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    if fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ValueError("ttl must be positive")


def _required_string(row: Mapping[str, Any], field: str, *, row_index: int) -> str:
    if field not in row:
        raise ValueError(
            f"Deepcoin instrument row {row_index} is missing required field {field}"
        )
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")
    return value


def _positive_decimal(value: Any, *, field: str, row_index: int) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Deepcoin instrument row {row_index} has invalid {field}"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")
    return parsed


def _is_integral_multiple(value: Decimal, step: Decimal) -> bool:
    value_numerator, value_denominator = value.as_integer_ratio()
    step_numerator, step_denominator = step.as_integer_ratio()
    ratio_numerator = value_numerator * step_denominator
    ratio_denominator = value_denominator * step_numerator
    return ratio_numerator % ratio_denominator == 0


def _snapshot_digest(instruments: Mapping[str, _ValidatedInstrument]) -> str:
    normalized_rows = [
        {
            "instId": instrument.instrument_id,
            "instType": "SWAP",
            "ctVal": _canonical_decimal(instrument.contract_value),
            "lotSz": _canonical_decimal(instrument.quantity_step),
            "minSz": _canonical_decimal(instrument.min_quantity),
            "state": instrument.state,
            "tickSz": _canonical_decimal(instrument.price_tick),
        }
        for _, instrument in sorted(instruments.items())
    ]
    normalized_json = json.dumps(
        normalized_rows,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    # Decimal.normalize() applies the active arithmetic context and can round
    # high-precision exchange metadata. Fixed-point formatting is exact.
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _finite_float(value: Decimal, *, field: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(
            f"Deepcoin instrument {field} cannot be represented as a positive finite value"
        )
    return converted
