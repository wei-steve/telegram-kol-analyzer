"""Deepcoin symbol eligibility as an explicit three-way intersection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Iterable

from telegram_kol_research.deepcoin_contract_spec_cache import (
    DeepcoinContractSpecSnapshot,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


@dataclass(frozen=True)
class DeepcoinSymbolCapabilityDecision:
    symbol: str
    instrument_id: str
    globally_allowed: bool
    venue_supported: bool
    venue_state: str | None
    tradable: bool
    reason: str
    contract_spec: DeepcoinContractSpec | None
    fetched_at: datetime | None
    expires_at: datetime | None
    source_digest_sha256: str | None


def decide_deepcoin_symbol_capability(
    symbol: str,
    *,
    global_allowed: Iterable[str],
    snapshot: DeepcoinContractSpecSnapshot | None,
    now: datetime,
) -> DeepcoinSymbolCapabilityDecision:
    """Decide new-entry eligibility without treating missing state as support."""

    normalized_symbol = str(symbol).strip().upper()
    instrument_id = f"{normalized_symbol}-USDT-SWAP"
    allowed = {
        str(allowed_symbol).strip().upper()
        for allowed_symbol in global_allowed
        if str(allowed_symbol).strip()
    }
    globally_allowed = normalized_symbol in allowed
    if not globally_allowed:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=False,
            snapshot=snapshot,
            reason="global_not_allowed",
        )
    if snapshot is None:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=None,
            reason="contract_spec_sync_unavailable",
        )

    normalized_now = _aware_utc(now)
    if normalized_now < snapshot.fetched_at:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=snapshot,
            reason="contract_spec_invalid",
        )
    if normalized_now >= snapshot.expires_at:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=snapshot,
            reason="contract_spec_stale",
        )

    capability = snapshot.capabilities_by_instrument_id.get(instrument_id)
    if capability is None:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=snapshot,
            reason="venue_instrument_unsupported",
        )
    if capability.state != "live":
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=snapshot,
            reason="venue_instrument_not_live",
            venue_state=capability.state,
        )
    try:
        contract_spec = capability.contract_spec
    except ValueError:
        return _decision(
            symbol=normalized_symbol,
            instrument_id=instrument_id,
            globally_allowed=True,
            snapshot=snapshot,
            reason="contract_spec_invalid",
            venue_state=capability.state,
        )
    return _decision(
        symbol=normalized_symbol,
        instrument_id=instrument_id,
        globally_allowed=True,
        snapshot=snapshot,
        reason="tradable",
        venue_state=capability.state,
        contract_spec=contract_spec,
    )


def _decision(
    *,
    symbol: str,
    instrument_id: str,
    globally_allowed: bool,
    snapshot: DeepcoinContractSpecSnapshot | None,
    reason: str,
    venue_state: str | None = None,
    contract_spec: DeepcoinContractSpec | None = None,
) -> DeepcoinSymbolCapabilityDecision:
    return DeepcoinSymbolCapabilityDecision(
        symbol=symbol,
        instrument_id=instrument_id,
        globally_allowed=globally_allowed,
        venue_supported=venue_state is not None,
        venue_state=venue_state,
        tradable=reason == "tradable",
        reason=reason,
        contract_spec=contract_spec,
        fetched_at=snapshot.fetched_at if snapshot is not None else None,
        expires_at=snapshot.expires_at if snapshot is not None else None,
        source_digest_sha256=(
            snapshot.source_digest_sha256 if snapshot is not None else None
        ),
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)
