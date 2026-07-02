"""Durable trade-signal queue between strategy recognition and execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TradeIdea
from telegram_kol_research.models import TradeSignal


@dataclass(slots=True)
class TradeSignalRecord:
    id: int
    signal_uid: str
    strategy_instance_id: str | None
    source_type: str
    venue: str
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    action: str
    status: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None = None


def build_trade_signal_uid(
    *,
    venue: str,
    source_type: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    action: str,
) -> str:
    return (
        f"{venue.lower()}:{source_type.lower()}:{int(chat_id)}:{int(message_id)}:"
        f"{symbol.upper()}:{side.lower()}:{action.lower()}"
    )


def enqueue_trade_signal(
    session_factory: sessionmaker,
    *,
    venue: str,
    source_type: str,
    kol_id: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    action: str,
    payload: dict[str, Any],
    strategy_instance_id: str | None = None,
    enqueued_at: datetime | None = None,
) -> TradeSignalRecord:
    """Create or refresh one durable trade signal."""

    normalized_venue = venue.lower()
    normalized_source_type = source_type.lower()
    normalized_symbol = symbol.upper()
    normalized_side = side.lower()
    normalized_action = action.lower()
    signal_uid = build_trade_signal_uid(
        venue=normalized_venue,
        source_type=normalized_source_type,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
        action=normalized_action,
    )
    resolved_strategy_instance_id = strategy_instance_id or build_strategy_instance_id(
        venue=normalized_venue,
        chat_id=chat_id,
        message_id=message_id,
        symbol=normalized_symbol,
        side=normalized_side,
    )
    now = enqueued_at or datetime.now(UTC)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    with session_factory() as session:
        row = (
            session.query(TradeSignal)
            .filter(TradeSignal.signal_uid == signal_uid)
            .one_or_none()
        )
        if row is None:
            row = TradeSignal(
                signal_uid=signal_uid,
                venue=normalized_venue,
                source_type=normalized_source_type,
                kol_id=kol_id,
                chat_id=chat_id,
                message_id=message_id,
                symbol=normalized_symbol,
                side=normalized_side,
                action=normalized_action,
                payload_json=payload_json,
            )
            session.add(row)
            session.flush()

        row.strategy_instance_id = resolved_strategy_instance_id
        row.kol_id = kol_id
        row.payload_json = payload_json
        if row.status in {"failed", "rejected"}:
            row.status = "pending"
            row.last_error = None
        row.updated_at = now
        session.commit()
        return _row_to_record(row)


def load_trade_signal(
    session_factory: sessionmaker,
    signal_id: int,
) -> TradeSignalRecord:
    with session_factory() as session:
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        return _row_to_record(row)


def list_pending_trade_signals(
    session_factory: sessionmaker,
    *,
    venue: str = "deepcoin",
    limit: int = 50,
) -> list[TradeSignalRecord]:
    with session_factory() as session:
        rows = (
            session.query(TradeSignal)
            .filter(TradeSignal.venue == venue.lower())
            .filter(TradeSignal.status == "pending")
            .order_by(TradeSignal.created_at.asc(), TradeSignal.id.asc())
            .limit(limit)
            .all()
        )
        return [_row_to_record(row) for row in rows]


def mark_trade_signal_submitted(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    result: dict[str, Any],
    processed_at: datetime | None = None,
) -> None:
    now = processed_at or datetime.now(UTC)
    with session_factory() as session:
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        row.status = "submitted"
        row.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        row.last_error = None
        row.processed_at = now
        row.updated_at = now
        session.commit()


def mark_trade_signal_failed(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    error: str,
    failed_at: datetime | None = None,
) -> None:
    now = failed_at or datetime.now(UTC)
    with session_factory() as session:
        row = session.get(TradeSignal, signal_id)
        if row is None:
            raise LookupError("trade signal not found")
        row.status = "failed"
        row.last_error = error
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = now
        if row.action == "open_position":
            _mark_lifecycle_auto_trade_failed(session, row, now)
        session.commit()


def _mark_lifecycle_auto_trade_failed(session, row: TradeSignal, failed_at: datetime) -> None:
    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == row.chat_id)
        .filter(StrategyLifecycle.message_id == row.message_id)
        .filter(StrategyLifecycle.symbol == row.symbol)
        .filter(StrategyLifecycle.side == row.side)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        return

    lifecycle.lifecycle_status = "invalidated"
    lifecycle.exit_reason = "auto_trade_failed"
    lifecycle.exited_at = failed_at
    lifecycle.updated_at = failed_at
    if lifecycle.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = failed_at


def _row_to_record(row: TradeSignal) -> TradeSignalRecord:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return TradeSignalRecord(
        id=row.id,
        signal_uid=row.signal_uid,
        strategy_instance_id=row.strategy_instance_id,
        source_type=row.source_type,
        venue=row.venue,
        kol_id=row.kol_id,
        chat_id=row.chat_id,
        message_id=row.message_id,
        symbol=row.symbol,
        side=row.side,
        action=row.action,
        status=row.status,
        payload=payload,
        attempts=row.attempts,
        last_error=row.last_error,
    )
