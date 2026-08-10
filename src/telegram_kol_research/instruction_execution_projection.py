"""Future-watermarked projection of authoritative instruction contracts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.instruction_execution_contracts import (
    load_or_create_instruction_execution_contract,
)
from telegram_kol_research.models import (
    InstructionExecutionContract,
    MessageInstructionItem,
    SignalCandidate,
    utc_now,
)
from telegram_kol_research.trading_settings import (
    TradingSettings,
    load_trading_settings,
)


def project_instruction_execution_contracts(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    settings: TradingSettings | None = None,
    projected_at: datetime | None = None,
) -> tuple[InstructionExecutionContract, ...]:
    """Project only current MiMo-authoritative items above strict ID watermarks."""

    active_settings = settings or load_trading_settings(session_factory)
    if active_settings.instruction_execution_contract_mode == "disabled":
        return ()
    now = projected_at or utc_now()
    with session_factory() as session:
        rows = (
            session.query(MessageInstructionItem)
            .join(
                SignalCandidate,
                SignalCandidate.id == MessageInstructionItem.signal_candidate_id,
            )
            .filter(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.retired_at.is_(None),
                SignalCandidate.parse_source == "mimo_authoritative",
            )
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        eligible = [
            (int(item.id), item.execution_deadline_at)
            for item in rows
            if _above_watermark(item, active_settings)
        ]

    return tuple(
        load_or_create_instruction_execution_contract(
            session_factory,
            message_instruction_item_id=item_id,
            projected_at=now,
            deadline_at=deadline_at,
        )
        for item_id, deadline_at in eligible
    )


def _above_watermark(
    item: MessageInstructionItem,
    settings: TradingSettings,
) -> bool:
    if item.instruction_kind == "entry":
        watermark = settings.instruction_execution_entry_after_item_id
    elif item.instruction_kind == "management":
        watermark = settings.instruction_execution_management_after_item_id
    else:
        return False
    return int(item.id) > int(watermark)


def instruction_execution_mode_for_item(
    item: MessageInstructionItem,
    settings: TradingSettings,
) -> str:
    """Return rollout mode only for items strictly above their future watermark."""

    if settings.instruction_execution_contract_mode == "disabled":
        return "disabled"
    if not _above_watermark(item, settings):
        return "disabled"
    return settings.instruction_execution_contract_mode
