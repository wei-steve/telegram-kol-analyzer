"""Persistence helpers for exchange order/position attribution bindings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    client_order_id: str | None = None
    pos_id: str | None = None
    margin_mode: str = "cross"
    position_mode: str = "split"
    payload: dict[str, Any] | None = None
    last_exchange_status: str | None = None
    status: str = "open"
    strategy_instance_id: str | None = None


@dataclass(slots=True)
class ExecutionReconciliationResult:
    active: int = 0
    open: int = 0
    stale: int = 0
    updated: int = 0


def build_strategy_instance_id(
    *,
    venue: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
) -> str:
    """Build a stable local strategy key used across restart recovery."""

    return (
        f"{venue.lower()}:{int(chat_id)}:{int(message_id)}:"
        f"{symbol.upper()}:{side.lower()}"
    )


def build_client_order_id(
    *,
    strategy_instance_id: str,
    leg_index: int = 1,
    purpose: str = "entry",
) -> str:
    """Build a deterministic client order id that remains stable after restarts."""

    normalized = (
        strategy_instance_id.replace(":", "-")
        .replace("_", "-")
        .lower()
    )
    client_order_id = f"tkol-{normalized}-{purpose}-{leg_index}"
    return client_order_id[:64]


def upsert_execution_binding(
    session_factory: sessionmaker,
    record: ExecutionBindingRecord,
) -> int:
    """Create or update the local exchange binding for one source strategy."""

    symbol = record.symbol.upper()
    side = record.side.lower()
    venue = record.venue.lower()
    strategy_instance_id = record.strategy_instance_id or build_strategy_instance_id(
        venue=venue,
        chat_id=record.chat_id,
        message_id=record.message_id,
        symbol=symbol,
        side=side,
    )
    payload_json = (
        json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        if record.payload is not None
        else None
    )

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

        binding.strategy_instance_id = strategy_instance_id
        binding.kol_id = record.kol_id
        binding.order_id = record.order_id
        binding.client_order_id = record.client_order_id
        binding.pos_id = record.pos_id
        binding.margin_mode = _normalize_margin_mode(record.margin_mode)
        binding.position_mode = _normalize_position_mode(record.position_mode)
        binding.payload_json = payload_json
        binding.last_exchange_status = record.last_exchange_status
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
                client_order_id=row.client_order_id,
            )
            for row in rows
        ]


def reconcile_deepcoin_execution_bindings(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
    recovered_at: datetime | None = None,
) -> ExecutionReconciliationResult:
    """Refresh persisted Deepcoin bindings against exchange open orders/positions.

    This is intentionally read-only against Deepcoin: on process restart it
    marks locally known strategies as active/open/stale using the exchange's
    current state, so later close/TP/SL handling can continue from durable data.
    """

    now = recovered_at or datetime.now(UTC)
    positions = client.list_positions()
    orders = client.list_open_orders()
    positions_by_pos_id = {
        _first_string(position, "posId", "pos_id", "id"): position
        for position in positions
        if _first_string(position, "posId", "pos_id", "id")
    }
    orders_by_order_id = {
        _first_string(order, "ordId", "orderId", "order_id", "id"): order
        for order in orders
        if _first_string(order, "ordId", "orderId", "order_id", "id")
    }
    orders_by_client_order_id = {
        _first_string(order, "clOrdId", "clientOrderId", "client_order_id"): order
        for order in orders
        if _first_string(order, "clOrdId", "clientOrderId", "client_order_id")
    }

    result = ExecutionReconciliationResult()
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active", "unknown"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        for row in rows:
            row.strategy_instance_id = row.strategy_instance_id or build_strategy_instance_id(
                venue=row.venue,
                chat_id=row.chat_id,
                message_id=row.message_id,
                symbol=row.symbol,
                side=row.side,
            )
            position = positions_by_pos_id.get(row.pos_id or "")
            order = orders_by_order_id.get(row.order_id or "")
            if order is None:
                order = orders_by_client_order_id.get(row.client_order_id or "")

            if position is not None and _has_nonzero_size(position):
                row.status = "active"
                row.last_exchange_status = "position_active"
                result.active += 1
            elif order is not None and _is_open_order_state(order):
                row.status = "open"
                row.last_exchange_status = "order_open"
                result.open += 1
            else:
                row.status = "stale"
                row.last_exchange_status = "not_found_on_exchange"
                result.stale += 1
            row.recovered_at = now
            row.updated_at = now
            result.updated += 1
        session.commit()
    return result


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
                "client_order_id": row.client_order_id,
                "pos_id": row.pos_id,
                "strategy_instance_id": row.strategy_instance_id,
                "margin_mode": row.margin_mode,
                "position_mode": row.position_mode,
                "status": row.status,
                "last_exchange_status": row.last_exchange_status,
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


def _normalize_margin_mode(value: str | None) -> str:
    text = str(value or "cross").lower()
    if text in {"cross", "crossed", "full", "全仓"}:
        return "cross"
    if text in {"isolated", "fixed", "逐仓"}:
        return "isolated"
    return "cross"


def _normalize_position_mode(value: str | None) -> str:
    text = str(value or "split").lower()
    if text in {"split", "hedge", "long_short", "分仓"}:
        return "split"
    if text in {"net", "merged", "one_way", "合仓"}:
        return "net"
    return "split"


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _has_nonzero_size(position: dict[str, Any]) -> bool:
    size = position.get("pos")
    if size in (None, ""):
        size = position.get("size")
    try:
        return abs(float(size or 0)) > 0
    except (TypeError, ValueError):
        return False


def _is_open_order_state(order: dict[str, Any]) -> bool:
    state = str(order.get("state") or order.get("status") or "").lower()
    if not state:
        return True
    return state in {"live", "open", "partially_filled", "partial"}
