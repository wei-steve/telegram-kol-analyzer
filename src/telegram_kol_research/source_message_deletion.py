"""Durable identity and safety state for deleted Telegram source messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    RawMessage,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    TelegramSourceMessageEvent,
)


@dataclass(frozen=True)
class SourceMessageDeletionRecord:
    event_id: int
    exit_id: int
    event_fingerprint: str
    chat_id: int
    message_id: int
    raw_message_id: int | None
    binding_state: str
    exit_state: str
    deleted_at: datetime


@dataclass(frozen=True)
class SourceExecutionBarrierDecision:
    status: str
    reason: str | None = None
    blocking_exit_id: int | None = None


def deletion_event_fingerprint(*, chat_id: int, message_id: int) -> str:
    identity = f"telegram:message_deleted:{int(chat_id)}:{int(message_id)}"
    return sha256(identity.encode("utf-8")).hexdigest()


def source_execution_barrier(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> SourceExecutionBarrierDecision:
    """Fail closed for a deleted source and sequence overlapping reposts."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            return SourceExecutionBarrierDecision(status="allow")
        if str(raw_message.source_status or "active") == "deleted":
            deletion_exit = (
                session.query(SourceMessageDeletionExit)
                .filter(
                    SourceMessageDeletionExit.raw_message_id == raw_message.id
                )
                .order_by(SourceMessageDeletionExit.id.asc())
                .first()
            )
            return SourceExecutionBarrierDecision(
                status="block",
                reason="source_message_deleted",
                blocking_exit_id=(
                    int(deletion_exit.id) if deletion_exit is not None else None
                ),
            )

        candidate = (
            session.query(SignalCandidate)
            .filter(
                SignalCandidate.raw_message_id == raw_message.id,
                SignalCandidate.symbol.is_not(None),
                SignalCandidate.side.is_not(None),
            )
            .order_by(SignalCandidate.id.desc())
            .first()
        )
        if candidate is None:
            return SourceExecutionBarrierDecision(status="allow")
        symbol = str(candidate.symbol or "").strip().upper()
        side = str(candidate.side or "").strip().lower()
        if not symbol or not side:
            return SourceExecutionBarrierDecision(status="allow")

        overlapping_exit = (
            session.query(SourceMessageDeletionExit)
            .join(
                RawMessage,
                RawMessage.id == SourceMessageDeletionExit.raw_message_id,
            )
            .join(
                SignalCandidate,
                SignalCandidate.raw_message_id == RawMessage.id,
            )
            .filter(
                RawMessage.chat_id == raw_message.chat_id,
                RawMessage.id != raw_message.id,
                RawMessage.source_status == "deleted",
                SignalCandidate.symbol == symbol,
                SignalCandidate.side == side,
                SourceMessageDeletionExit.state != "succeeded",
            )
            .order_by(SourceMessageDeletionExit.id.asc())
            .first()
        )
        if overlapping_exit is not None:
            return SourceExecutionBarrierDecision(
                status="hold",
                reason="waiting_source_deletion_exit",
                blocking_exit_id=int(overlapping_exit.id),
            )
        return SourceExecutionBarrierDecision(status="allow")


def record_source_message_deleted(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    deleted_at: datetime | None = None,
    telegram_event: dict[str, Any] | None = None,
) -> SourceMessageDeletionRecord:
    """Record one exact Telegram deletion and create its idempotent exit job."""

    occurred_at = deleted_at or datetime.now(UTC)
    fingerprint = deletion_event_fingerprint(
        chat_id=chat_id,
        message_id=message_id,
    )
    event_json = json.dumps(
        telegram_event or {}, ensure_ascii=False, sort_keys=True, default=str
    )
    with session_factory() as session:
        event = (
            session.query(TelegramSourceMessageEvent)
            .filter(
                TelegramSourceMessageEvent.event_fingerprint == fingerprint
            )
            .one_or_none()
        )
        if event is None:
            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == int(chat_id),
                    RawMessage.message_id == int(message_id),
                )
                .order_by(RawMessage.id.asc())
                .first()
            )
            binding_state = "bound" if raw_message is not None else "unbound"
            event = TelegramSourceMessageEvent(
                event_type="message_deleted",
                chat_id=int(chat_id),
                message_id=int(message_id),
                raw_message_id=raw_message.id if raw_message is not None else None,
                event_fingerprint=fingerprint,
                binding_state=binding_state,
                telegram_event_json=event_json,
                occurred_at=occurred_at,
                updated_at=occurred_at,
            )
            session.add(event)
            session.flush()

            lifecycle = None
            binding = None
            if raw_message is not None:
                raw_message.source_status = "deleted"
                raw_message.deleted_at = occurred_at
                raw_message.deletion_event_fingerprint = fingerprint
                lifecycle = (
                    session.query(StrategyLifecycle)
                    .filter(
                        StrategyLifecycle.chat_id == int(chat_id),
                        StrategyLifecycle.message_id == int(message_id),
                    )
                    .one_or_none()
                )
                if lifecycle is not None and lifecycle.execution_binding_id is not None:
                    binding = session.get(
                        ExecutionBinding, int(lifecycle.execution_binding_id)
                    )
                if binding is None:
                    binding = (
                        session.query(ExecutionBinding)
                        .filter(
                            ExecutionBinding.chat_id == int(chat_id),
                            ExecutionBinding.message_id == int(message_id),
                        )
                        .order_by(ExecutionBinding.id.asc())
                        .first()
                    )

            deletion_exit = SourceMessageDeletionExit(
                source_event_id=event.id,
                raw_message_id=raw_message.id if raw_message is not None else None,
                target_lifecycle_id=lifecycle.id if lifecycle is not None else None,
                strategy_instance_id=(
                    binding.strategy_instance_id if binding is not None else None
                ),
                state="pending" if raw_message is not None else "unbound",
                updated_at=occurred_at,
            )
            session.add(deletion_exit)
            session.commit()
        else:
            deletion_exit = (
                session.query(SourceMessageDeletionExit)
                .filter(SourceMessageDeletionExit.source_event_id == event.id)
                .one()
            )

        return SourceMessageDeletionRecord(
            event_id=int(event.id),
            exit_id=int(deletion_exit.id),
            event_fingerprint=str(event.event_fingerprint),
            chat_id=int(event.chat_id),
            message_id=int(event.message_id),
            raw_message_id=(
                int(event.raw_message_id) if event.raw_message_id is not None else None
            ),
            binding_state=str(event.binding_state),
            exit_state=str(deletion_exit.state),
            deleted_at=event.occurred_at,
        )
