"""Dry-run gate for future live Deepcoin recovery order submission."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_order_confirmations import (
    has_ready_recovery_order_confirmation,
)


_ENTRY_CAPABILITY_REASONS = {
    "symbol_not_allowed",
    "venue_instrument_unsupported",
    "venue_instrument_not_live",
    "contract_spec_missing",
    "contract_spec_invalid",
    "contract_spec_stale",
    "contract_spec_sync_unavailable",
}


def validate_recovery_live_submit_gate(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> dict[str, object]:
    """Validate every precondition for a future live submit without submitting."""

    symbol_key = symbol.upper()
    side_key = side.lower()
    checks = {
        "ready_confirmation": has_ready_recovery_order_confirmation(
            session_factory,
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol_key,
            side=side_key,
        ),
        "execution_queue_item": False,
        "no_active_binding": not _active_binding_exists(
            session_factory,
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol_key,
            side=side_key,
        ),
        "order_draft_ready": False,
    }
    reason_codes: list[str] = []
    confirmation_result: dict[str, object] | None = None

    if not checks["ready_confirmation"]:
        reason_codes.append("missing_ready_confirmation")
    if not checks["no_active_binding"]:
        reason_codes.append("active_binding_exists")

    try:
        confirmation_result = confirm_recovery_order_dry_run(
            session_factory,
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol_key,
            side=side_key,
            contract_spec_provider=contract_spec_provider,
        )
        checks["execution_queue_item"] = True
        checks["order_draft_ready"] = bool(confirmation_result["ready_for_live_order"])
        confirmation_reasons = [
            str(code) for code in confirmation_result["reason_codes"]
        ]
        capability_reasons = [
            code for code in confirmation_reasons if code in _ENTRY_CAPABILITY_REASONS
        ]
        if capability_reasons:
            reason_codes = capability_reasons
        else:
            reason_codes.extend(confirmation_reasons)
    except LookupError:
        reason_codes.append("execution_queue_item_not_found")

    if not checks["order_draft_ready"] and "execution_queue_item_not_found" not in reason_codes:
        if confirmation_result is None or not confirmation_result.get("reason_codes"):
            reason_codes.append("order_draft_not_ready")

    reason_codes = _dedupe(reason_codes)
    return {
        "would_submit": not reason_codes and all(checks.values()),
        "dry_run_only": True,
        "reason_codes": reason_codes,
        "checks": checks,
        "source": {
            "chat_id": chat_id,
            "message_id": message_id,
            "symbol": symbol_key,
            "side": side_key,
        },
        "deepcoin_order_draft": (
            confirmation_result.get("deepcoin_order_draft") if confirmation_result else None
        ),
    }


def _active_binding_exists(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
) -> bool:
    with session_factory() as session:
        return (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == chat_id)
            .filter(ExecutionBinding.message_id == message_id)
            .filter(ExecutionBinding.symbol == symbol.upper())
            .filter(ExecutionBinding.side == side.lower())
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .one_or_none()
            is not None
        )


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values
