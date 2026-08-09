"""Dry-run confirmation gate for recovery execution previews."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_order_confirmations import (
    upsert_ready_recovery_order_confirmation,
)
from telegram_kol_research.trading_settings import load_trading_settings


@dataclass(frozen=True)
class DeepcoinEntryCapabilityGateResult:
    """One pinned new-entry capability decision used through persistence."""

    symbol: str
    instrument_id: str
    allowed: bool
    reason: str
    contract_spec: DeepcoinContractSpec | None
    contract_spec_snapshot: dict[str, str] | None


@dataclass(frozen=True)
class _PinnedContractSpecProvider:
    instrument_id: str
    contract_spec: DeepcoinContractSpec

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        if str(instrument_id).strip().upper() != self.instrument_id:
            return None
        return self.contract_spec


def evaluate_deepcoin_entry_capability(
    session_factory: sessionmaker,
    *,
    symbol: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> DeepcoinEntryCapabilityGateResult:
    """Apply global allowlist first, then pin the venue/spec decision."""

    symbol_key = str(symbol).strip().upper()
    instrument_id = f"{symbol_key}-USDT-SWAP"
    settings = load_trading_settings(session_factory)
    allowed_symbols = {
        str(item).strip().upper() for item in settings.allowed_symbols
    }
    if not symbol_key or symbol_key not in allowed_symbols:
        return DeepcoinEntryCapabilityGateResult(
            symbol=symbol_key,
            instrument_id=instrument_id,
            allowed=False,
            reason="symbol_not_allowed",
            contract_spec=None,
            contract_spec_snapshot=None,
        )

    lookup_provider: Any = contract_spec_provider
    mode = getattr(contract_spec_provider, "mode", None)
    if mode == "live":
        lookup_provider = getattr(
            contract_spec_provider,
            "authoritative_provider",
            contract_spec_provider,
        )
    lookup = getattr(lookup_provider, "lookup_contract_spec", None)
    if callable(lookup):
        snapshot_before_lookup = getattr(lookup_provider, "snapshot", None)
        try:
            lookup_result = lookup(instrument_id)
        except Exception:
            return _blocked_capability(
                symbol_key, instrument_id, "contract_spec_sync_unavailable"
            )
        reason = str(getattr(lookup_result, "reason", "") or "")
        if reason != "available":
            return _blocked_capability(
                symbol_key,
                instrument_id,
                reason or "contract_spec_invalid",
            )
        contract_spec = getattr(lookup_result, "contract_spec", None)
        snapshot_after_lookup = getattr(lookup_provider, "snapshot", None)
        if (
            snapshot_before_lookup is None
            or snapshot_after_lookup is not snapshot_before_lookup
        ):
            return _blocked_capability(
                symbol_key, instrument_id, "contract_spec_invalid"
            )
        snapshot = snapshot_before_lookup
    else:
        try:
            contract_spec = (
                contract_spec_provider.get_contract_spec(instrument_id)
                if contract_spec_provider is not None
                else None
            )
        except Exception:
            return _blocked_capability(
                symbol_key, instrument_id, "contract_spec_sync_unavailable"
            )
        snapshot = None
    if not isinstance(contract_spec, DeepcoinContractSpec):
        return _blocked_capability(
            symbol_key, instrument_id, "contract_spec_missing"
        )
    if contract_spec.instrument_id.upper() != instrument_id:
        return _blocked_capability(
            symbol_key, instrument_id, "contract_spec_invalid"
        )
    snapshot_evidence = _snapshot_evidence(snapshot)
    if callable(lookup) and snapshot_evidence is None:
        return _blocked_capability(
            symbol_key, instrument_id, "contract_spec_invalid"
        )
    return DeepcoinEntryCapabilityGateResult(
        symbol=symbol_key,
        instrument_id=instrument_id,
        allowed=True,
        reason="tradable",
        contract_spec=contract_spec,
        contract_spec_snapshot=snapshot_evidence,
    )


def pinned_contract_spec_provider(
    decision: DeepcoinEntryCapabilityGateResult,
) -> DeepcoinContractSpecProvider:
    if not decision.allowed or decision.contract_spec is None:
        raise ValueError(decision.reason)
    return _PinnedContractSpecProvider(
        instrument_id=decision.instrument_id,
        contract_spec=decision.contract_spec,
    )


def attach_contract_spec_evidence(
    draft: dict[str, Any],
    decision: DeepcoinEntryCapabilityGateResult,
) -> dict[str, Any]:
    if not decision.allowed or decision.contract_spec is None:
        raise ValueError(decision.reason)
    accepted = {**draft, "contract_spec": decision.contract_spec.to_dict()}
    if decision.contract_spec_snapshot is not None:
        accepted["contract_spec_snapshot"] = dict(
            decision.contract_spec_snapshot
        )
    return accepted


def _blocked_capability(
    symbol: str, instrument_id: str, reason: str
) -> DeepcoinEntryCapabilityGateResult:
    return DeepcoinEntryCapabilityGateResult(
        symbol=symbol,
        instrument_id=instrument_id,
        allowed=False,
        reason=reason,
        contract_spec=None,
        contract_spec_snapshot=None,
    )


def _snapshot_evidence(snapshot: Any) -> dict[str, str] | None:
    if snapshot is None:
        return None
    digest = getattr(snapshot, "source_digest_sha256", None)
    fetched_at = getattr(snapshot, "fetched_at", None)
    expires_at = getattr(snapshot, "expires_at", None)
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest.lower())
        or not isinstance(fetched_at, datetime)
        or not isinstance(expires_at, datetime)
        or fetched_at.tzinfo is None
        or fetched_at.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or expires_at <= fetched_at
    ):
        return None
    return {
        "source_digest_sha256": digest,
        "fetched_at": fetched_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def confirm_recovery_order_dry_run(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    persist_ready_confirmation: bool = False,
    confirmed_at: datetime | None = None,
) -> dict[str, object]:
    """Re-check a recovery execution preview without placing a live order."""

    capability = evaluate_deepcoin_entry_capability(
        session_factory,
        symbol=symbol,
        contract_spec_provider=contract_spec_provider,
    )
    if not capability.allowed:
        return {
            "ready_for_live_order": False,
            "dry_run_only": True,
            "reason_codes": [capability.reason],
            "contract_spec_status": {"code": "missing"},
            "payload_preview": None,
            "deepcoin_order_draft": {
                "instrument_id": capability.instrument_id,
                "blocking_reason_codes": [capability.reason],
            },
            "source": {
                "chat_id": chat_id,
                "message_id": message_id,
                "symbol": capability.symbol,
                "side": side.lower(),
            },
            "ready_confirmation": None,
        }
    preview = _find_execution_preview(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=pinned_contract_spec_provider(capability),
    )
    draft = attach_contract_spec_evidence(
        preview["deepcoin_order_draft"], capability
    )
    reason_codes = _final_reason_codes(draft)
    result = {
        "ready_for_live_order": not reason_codes,
        "dry_run_only": True,
        "reason_codes": reason_codes,
        "contract_spec_status": preview["contract_spec_status"],
        "payload_preview": preview["payload_preview"],
        "deepcoin_order_draft": draft,
        "source": {
            "chat_id": chat_id,
            "message_id": message_id,
            "symbol": symbol.upper(),
            "side": side.lower(),
        },
        "ready_confirmation": None,
    }
    if result["ready_for_live_order"] and persist_ready_confirmation:
        result["ready_confirmation"] = upsert_ready_recovery_order_confirmation(
            session_factory,
            confirmation_payload=result,
            confirmed_at=confirmed_at,
        )
    return result


def _find_execution_preview(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> dict[str, object]:
    symbol_key = symbol.upper()
    side_key = side.lower()
    for preview in list_recovery_execution_previews(
        session_factory,
        limit=500,
        contract_spec_provider=contract_spec_provider,
    ):
        if (
            preview["chat_id"] == chat_id
            and preview["message_id"] == message_id
            and str(preview["symbol"]).upper() == symbol_key
            and str(preview["side"]).lower() == side_key
        ):
            return preview
    raise LookupError("recovery execution item not found")


def _final_reason_codes(deepcoin_order_draft: dict[str, object]) -> list[str]:
    reason_codes = [
        str(reason_code)
        for reason_code in deepcoin_order_draft.get("blocking_reason_codes", [])
    ]
    if reason_codes:
        return _dedupe(reason_codes)
    order_legs = deepcoin_order_draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        reason_codes.append("missing_order_legs")
        return reason_codes

    for order_leg in order_legs:
        if not isinstance(order_leg, dict):
            reason_codes.append("invalid_order_leg")
            continue
        if order_leg.get("quantity_unit") != "contracts":
            reason_codes.append("quantity_not_in_contracts")
        quantity = order_leg.get("quantity")
        if not isinstance(quantity, int | float) or quantity <= 0:
            reason_codes.append("non_positive_quantity")
    return _dedupe(reason_codes)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values
