"""Dry-run confirmation gate for recovery execution previews."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_order_confirmations import (
    upsert_ready_recovery_order_confirmation,
)


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

    preview = _find_execution_preview(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
    )
    draft = preview["deepcoin_order_draft"]
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
