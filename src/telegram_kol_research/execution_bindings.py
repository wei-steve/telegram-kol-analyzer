"""Persistence helpers for exchange order/position attribution bindings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_readonly import (
    DeepcoinOrderBinding,
    DeepcoinReadOnlyAccountState,
    DeepcoinReadOnlyClient,
)
from telegram_kol_research.models import ExecutionBinding


@dataclass(slots=True)
class ExecutionBindingRecord:
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    venue: str = "deepcoin"
    order_id: str | None = None
    pos_id: str | None = None
    status: str = "open"


def upsert_execution_binding(
    session_factory: sessionmaker,
    record: ExecutionBindingRecord,
) -> int:
    """Create or update the local exchange binding for one source strategy."""

    symbol = record.symbol.upper()
    side = record.side.lower()
    venue = record.venue.lower()

    with session_factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.venue == venue,
                ExecutionBinding.chat_id == record.chat_id,
                ExecutionBinding.message_id == record.message_id,
                ExecutionBinding.symbol == symbol,
                ExecutionBinding.side == side,
            )
            .one_or_none()
        )
        if binding is None:
            binding = ExecutionBinding(
                kol_id=record.kol_id,
                chat_id=record.chat_id,
                message_id=record.message_id,
                symbol=symbol,
                side=side,
                venue=venue,
            )
            session.add(binding)
            session.flush()

        binding.kol_id = record.kol_id
        binding.order_id = record.order_id
        binding.pos_id = record.pos_id
        binding.status = record.status
        binding.updated_at = datetime.now(UTC)
        binding_id = binding.id
        session.commit()

    return binding_id


def load_deepcoin_order_bindings(
    session_factory: sessionmaker,
) -> list[DeepcoinOrderBinding]:
    """Load active/open Deepcoin bindings for read-only account state mapping."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )

        return [
            DeepcoinOrderBinding(
                kol_id=row.kol_id,
                chat_id=row.chat_id,
                source_message_id=row.message_id,
                symbol=row.symbol,
                side=row.side,
                pos_id=row.pos_id,
                order_id=row.order_id,
            )
            for row in rows
        ]


def build_deepcoin_account_state(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
) -> DeepcoinReadOnlyAccountState:
    """Build a read-only Deepcoin account-state provider from persisted bindings."""

    return DeepcoinReadOnlyAccountState(
        client=client,
        bindings=load_deepcoin_order_bindings(session_factory),
    )
