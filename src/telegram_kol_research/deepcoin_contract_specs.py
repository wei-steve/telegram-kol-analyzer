"""Deepcoin contract specification helpers.

The module is intentionally offline-only. Runtime code can inject a provider
that obtains specs from a public API, cache, or manually verified config.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

import yaml


@dataclass(frozen=True)
class DeepcoinContractSpec:
    """Verified contract sizing rules for a Deepcoin swap instrument."""

    instrument_id: str
    contract_value: float
    quantity_step: float
    min_quantity: float
    price_tick: float

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        if self.contract_value <= 0:
            raise ValueError("contract_value must be positive")
        if self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive")
        if self.min_quantity <= 0:
            raise ValueError("min_quantity must be positive")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")

    def to_dict(self) -> dict[str, float | str]:
        values = asdict(self)
        values["instrument_id"] = self.instrument_id.upper()
        values["contract_value"] = float(self.contract_value)
        values["quantity_step"] = float(self.quantity_step)
        values["min_quantity"] = float(self.min_quantity)
        values["price_tick"] = float(self.price_tick)
        return values


class DeepcoinContractSpecProvider(Protocol):
    """Provider interface for verified contract specs."""

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        """Return a verified spec for an instrument, if available."""


@dataclass(frozen=True)
class StaticDeepcoinContractSpecProvider:
    """In-memory provider backed by manually verified contract specs."""

    specs_by_instrument_id: dict[str, DeepcoinContractSpec]

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        return self.specs_by_instrument_id.get(instrument_id.upper())


def load_deepcoin_contract_specs(
    config_path: str | Path,
    *,
    required: bool = True,
) -> StaticDeepcoinContractSpecProvider:
    """Load verified Deepcoin contract specs from YAML."""

    path = Path(config_path)
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return StaticDeepcoinContractSpecProvider(specs_by_instrument_id={})

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = [
        _parse_contract_spec(contract_data)
        for contract_data in raw_data.get("contracts", [])
    ]
    return StaticDeepcoinContractSpecProvider(
        specs_by_instrument_id={spec.instrument_id.upper(): spec for spec in specs}
    )


def _parse_contract_spec(raw_data: dict[str, Any]) -> DeepcoinContractSpec:
    return DeepcoinContractSpec(
        instrument_id=str(raw_data["instrument_id"]).upper(),
        contract_value=float(raw_data["contract_value"]),
        quantity_step=float(raw_data["quantity_step"]),
        min_quantity=float(raw_data["min_quantity"]),
        price_tick=float(raw_data["price_tick"]),
    )
