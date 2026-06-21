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


def list_active_positions(
    session_factory: sessionmaker,
    *,
    chat_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List entered-but-not-exited strategies with detailed signal info.

    Combines execution bindings and open trade ideas.  Supports optional
    chat_id filter for per-group display.  Skips strategies that have been
    closed via trade updates or close signals.
    """

    from telegram_kol_research.models import (
        TradeIdea, SignalCandidate, RawMessage, TradeUpdate,
    )
    from sqlalchemy import and_

    results: list[dict[str, object]] = []

    with session_factory() as session:
        # ------------------------------------------------------------------
        # 1. Active execution bindings (exchange-tracked positions)
        # ------------------------------------------------------------------
        bindings_q = session.query(ExecutionBinding).filter(
            ExecutionBinding.status.in_(["open", "active"])
        )
        if chat_id is not None:
            bindings_q = bindings_q.filter(ExecutionBinding.chat_id == chat_id)
        bindings = bindings_q.order_by(ExecutionBinding.created_at.desc()).limit(limit).all()

        already_covered: set[tuple[int, str, str]] = set()
        for row in bindings:
            key = (row.chat_id, row.symbol.upper(), row.side.lower())
            results.append({
                "source": "execution",
                "id": row.id,
                "kol_id": row.kol_id,
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "symbol": row.symbol,
                "side": row.side,
                "venue": row.venue,
                "order_id": row.order_id,
                "pos_id": row.pos_id,
                "status": row.status,
                "entry_text": None,
                "stop_loss_text": None,
                "take_profit_text": None,
                "confidence": None,
                "posted_at": row.created_at,
                "opened_at": row.created_at,
                "closed": False,
            })
            already_covered.add(key)

        # ------------------------------------------------------------------
        # 2. Open trade ideas with full signal details
        # ------------------------------------------------------------------
        open_trades_q = (
            session.query(TradeIdea, SignalCandidate, RawMessage)
            .join(SignalCandidate, TradeIdea.primary_signal_candidate_id == SignalCandidate.id)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .filter(TradeIdea.status == "open")
        )
        if chat_id is not None:
            open_trades_q = open_trades_q.filter(RawMessage.chat_id == chat_id)
        open_trades = (
            open_trades_q
            .order_by(TradeIdea.opened_at.desc().nullslast(), TradeIdea.id.desc())
            .limit(limit)
            .all()
        )

        # Collect all trade_idea ids for exit-check
        trade_ids = [
            trade.id for trade, _, _ in open_trades
        ]

        # ------------------------------------------------------------------
        # 3. Pre-load close signals and trade updates for exit detection
        # ------------------------------------------------------------------
        closed_trade_ids: set[int] = set()
        if trade_ids:
            # Close trade updates
            close_updates = (
                session.query(TradeUpdate.trade_idea_id)
                .filter(
                    TradeUpdate.trade_idea_id.in_(trade_ids),
                    TradeUpdate.update_type.in_([
                        "close", "close_signal", "stop_loss_hit",
                        "take_profit_hit", "manual_close", "closed",
                    ]),
                )
                .all()
            )
            closed_trade_ids.update(row[0] for row in close_updates)

            # Close signal candidates for same chat+symbol+side pairs
            for trade, candidate, raw_msg in open_trades:
                if trade.id in closed_trade_ids:
                    continue
                close_exists = session.query(SignalCandidate).filter(
                    SignalCandidate.raw_message_id.in_(
                        session.query(RawMessage.id).filter(
                            RawMessage.chat_id == raw_msg.chat_id,
                            RawMessage.posted_at > raw_msg.posted_at,
                        )
                    ),
                    SignalCandidate.symbol == candidate.symbol,
                    SignalCandidate.side == candidate.side,
                    SignalCandidate.event_type.in_(["close_signal", "stop_loss_update"]),
                ).first()
                if close_exists is not None:
                    closed_trade_ids.add(trade.id)

        # ------------------------------------------------------------------
        # 4. Build result rows for open (non-exited) trade ideas
        # ------------------------------------------------------------------
        for trade, candidate, raw_msg in open_trades:
            if trade.id in closed_trade_ids:
                continue
            key = (raw_msg.chat_id, (trade.symbol or "").upper(), (trade.side or "").lower())
            if key in already_covered:
                continue
            results.append({
                "source": "trade_idea",
                "id": trade.id,
                "kol_id": (candidate.source_id or raw_msg.sender_name or "unknown"),
                "chat_id": raw_msg.chat_id,
                "message_id": raw_msg.message_id,
                "symbol": trade.symbol or "?",
                "side": trade.side or "?",
                "venue": "",
                "order_id": None,
                "pos_id": None,
                "status": trade.status,
                "entry_text": candidate.entry_text,
                "stop_loss_text": candidate.stop_loss_text,
                "take_profit_text": candidate.take_profit_text,
                "confidence": trade.confidence,
                "posted_at": raw_msg.posted_at,
                "opened_at": trade.opened_at,
                "closed": False,
            })
            already_covered.add(key)

    return results[:limit]
