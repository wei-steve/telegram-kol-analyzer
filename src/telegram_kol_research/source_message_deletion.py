"""Durable identity and safety state for deleted Telegram source messages."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from functools import wraps
import json
from threading import RLock
from typing import Any

from sqlalchemy import or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.entry_preambles import (
    invalidate_pending_entry_preamble_in_session,
)

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    RawMessage,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    TelegramSourceMessageEvent,
    TradeSignal,
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


_SOURCE_EXECUTION_LOCKS: dict[str, RLock] = {}
_SOURCE_EXECUTION_LOCKS_GUARD = RLock()


def _source_execution_lock(session_factory: sessionmaker) -> RLock:
    bind = session_factory.kw.get("bind")
    key = str(getattr(bind, "url", bind))
    with _SOURCE_EXECUTION_LOCKS_GUARD:
        return _SOURCE_EXECUTION_LOCKS.setdefault(key, RLock())


@contextmanager
def source_message_execution_authority(session_factory: sessionmaker):
    """Serialize deletion commits with final entry-order submissions."""

    with _source_execution_lock(session_factory):
        yield


def serialized_source_message_execution(func):
    """Hold source authority until exchange identities are durably ledgered."""

    @wraps(func)
    def wrapped(session_factory, *args, **kwargs):
        with source_message_execution_authority(session_factory):
            return func(session_factory, *args, **kwargs)

    return wrapped


def deletion_event_fingerprint(*, chat_id: int, message_id: int) -> str:
    identity = f"telegram:message_deleted:{int(chat_id)}:{int(message_id)}"
    return sha256(identity.encode("utf-8")).hexdigest()


def source_execution_barrier(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> SourceExecutionBarrierDecision:
    """Fail closed for a deleted source and sequence overlapping reposts."""

    with source_message_execution_authority(session_factory):
        with session_factory() as session:
            raw_message = session.get(RawMessage, int(raw_message_id))
            if raw_message is None:
                return SourceExecutionBarrierDecision(status="allow")
            event = (
                session.query(TelegramSourceMessageEvent)
                .filter(
                    TelegramSourceMessageEvent.event_type == "message_deleted",
                    TelegramSourceMessageEvent.chat_id == raw_message.chat_id,
                    TelegramSourceMessageEvent.message_id == raw_message.message_id,
                )
                .one_or_none()
            )
            if event is not None:
                deletion_exit = _bind_deletion_event_in_session(
                    session,
                    event=event,
                    updated_at=datetime.now(UTC),
                )
                session.commit()
                if event.binding_state == "bound":
                    return SourceExecutionBarrierDecision(
                        status="block",
                        reason="source_message_deleted",
                        blocking_exit_id=int(deletion_exit.id),
                    )
            if str(raw_message.source_status or "active") == "deleted":
                deletion_exit = (
                    session.query(SourceMessageDeletionExit)
                    .filter(SourceMessageDeletionExit.raw_message_id == raw_message.id)
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


def source_identity_execution_barrier(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
) -> SourceExecutionBarrierDecision:
    """Apply the raw-message barrier from an exact source identity."""

    with session_factory() as session:
        raw_message_id = (
            session.query(RawMessage.id)
            .filter(
                RawMessage.chat_id == int(chat_id),
                RawMessage.message_id == int(message_id),
                RawMessage.archived_target_group.is_(True),
            )
            .order_by(RawMessage.id.asc())
            .scalar()
        )
    if raw_message_id is None:
        return SourceExecutionBarrierDecision(status="allow")
    return source_execution_barrier(
        session_factory,
        raw_message_id=int(raw_message_id),
    )


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
    with source_message_execution_authority(session_factory):
        with session_factory() as session:
            session.execute(
                sqlite_insert(TelegramSourceMessageEvent)
                .values(
                    event_type="message_deleted",
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    raw_message_id=None,
                    event_fingerprint=fingerprint,
                    binding_state="unbound",
                    telegram_event_json=event_json,
                    occurred_at=occurred_at,
                    created_at=occurred_at,
                    updated_at=occurred_at,
                )
                .on_conflict_do_nothing(index_elements=["event_fingerprint"])
            )
            event = (
                session.query(TelegramSourceMessageEvent)
                .filter(TelegramSourceMessageEvent.event_fingerprint == fingerprint)
                .one()
            )
            session.execute(
                sqlite_insert(SourceMessageDeletionExit)
                .values(
                    source_event_id=event.id,
                    raw_message_id=None,
                    target_lifecycle_id=None,
                    execution_binding_id=None,
                    strategy_instance_id=None,
                    target_fingerprint=None,
                    state="unbound",
                    attempt_count=0,
                    created_at=occurred_at,
                    updated_at=occurred_at,
                )
                .on_conflict_do_nothing(index_elements=["source_event_id"])
            )
            deletion_exit = _bind_deletion_event_in_session(
                session,
                event=event,
                updated_at=occurred_at,
            )
            session.commit()
            return SourceMessageDeletionRecord(
                event_id=int(event.id),
                exit_id=int(deletion_exit.id),
                event_fingerprint=str(event.event_fingerprint),
                chat_id=int(event.chat_id),
                message_id=int(event.message_id),
                raw_message_id=(
                    int(event.raw_message_id)
                    if event.raw_message_id is not None
                    else None
                ),
                binding_state=str(event.binding_state),
                exit_state=str(deletion_exit.state),
                deleted_at=event.occurred_at,
            )


def _bind_deletion_event_in_session(
    session,
    *,
    event: TelegramSourceMessageEvent,
    updated_at: datetime,
) -> SourceMessageDeletionExit:
    deletion_exit = (
        session.query(SourceMessageDeletionExit)
        .filter(SourceMessageDeletionExit.source_event_id == event.id)
        .one()
    )
    raw_message = (
        session.query(RawMessage)
        .filter(
            RawMessage.chat_id == int(event.chat_id),
            RawMessage.message_id == int(event.message_id),
            RawMessage.archived_target_group.is_(True),
        )
        .order_by(RawMessage.id.asc())
        .first()
    )
    if raw_message is None:
        return deletion_exit

    raw_message.source_status = "deleted"
    raw_message.deleted_at = event.occurred_at
    raw_message.deletion_event_fingerprint = event.event_fingerprint
    invalidate_pending_entry_preamble_in_session(
        session,
        raw_message_id=int(raw_message.id),
        now=updated_at,
    )
    event.raw_message_id = raw_message.id
    event.binding_state = "bound"
    event.updated_at = updated_at
    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(
            StrategyLifecycle.chat_id == int(event.chat_id),
            StrategyLifecycle.message_id == int(event.message_id),
        )
        .one_or_none()
    )
    binding = None
    if lifecycle is not None and lifecycle.execution_binding_id is not None:
        binding = session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
    if binding is None:
        binding = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.chat_id == int(event.chat_id),
                ExecutionBinding.message_id == int(event.message_id),
            )
            .order_by(ExecutionBinding.id.asc())
            .first()
        )
    has_trade_signal = (
        session.query(TradeSignal.id)
        .filter(
            TradeSignal.chat_id == int(event.chat_id),
            TradeSignal.message_id == int(event.message_id),
        )
        .first()
        is not None
    )
    has_signal_candidate = (
        session.query(SignalCandidate.id)
        .filter(SignalCandidate.raw_message_id == int(raw_message.id))
        .first()
        is not None
    )
    has_execution_event = (
        session.query(ExecutionEvent.id)
        .filter(
            ExecutionEvent.chat_id == int(event.chat_id),
            ExecutionEvent.message_id == int(event.message_id),
            ExecutionEvent.action.not_in(
                {
                    "source_message_deletion_outcome",
                    "terminal_entry_cleanup_outcome",
                }
            ),
            or_(
                ExecutionEvent.order_id.is_not(None),
                ExecutionEvent.client_order_id.is_not(None),
                ExecutionEvent.pos_id.is_not(None),
                ExecutionEvent.request_json.is_not(None),
                ExecutionEvent.response_json.is_not(None),
            ),
        )
        .first()
        is not None
    )
    new_strategy_evidence = bool(
        lifecycle is not None
        or binding is not None
        or has_signal_candidate
        or has_trade_signal
        or has_execution_event
    )
    reopening_ignored_exit = bool(
        deletion_exit.state == "succeeded"
        and deletion_exit.last_reason == "non_strategy_or_unlinked"
        and new_strategy_evidence
    )
    exit_target_is_mutable = (
        deletion_exit.state in {"unbound", "pending"} or reopening_ignored_exit
    )
    if exit_target_is_mutable:
        deletion_exit.raw_message_id = raw_message.id
        deletion_exit.target_lifecycle_id = (
            lifecycle.id if lifecycle is not None else None
        )
        deletion_exit.execution_binding_id = binding.id if binding is not None else None
        deletion_exit.strategy_instance_id = (
            binding.strategy_instance_id if binding is not None else None
        )
        if deletion_exit.state == "unbound" or reopening_ignored_exit:
            deletion_exit.state = "pending"
            deletion_exit.last_reason = None
            deletion_exit.last_error = None
            deletion_exit.completed_at = None
            event.processing_status = "recorded"
            event.reason_code = None
            event.completed_at = None
        deletion_exit.updated_at = updated_at
        target_identity = {
            "event_id": int(event.id),
            "raw_message_id": int(raw_message.id),
            "target_lifecycle_id": (
                int(lifecycle.id) if lifecycle is not None else None
            ),
            "execution_binding_id": int(binding.id) if binding is not None else None,
            "strategy_instance_id": (
                str(binding.strategy_instance_id)
                if binding is not None and binding.strategy_instance_id is not None
                else None
            ),
        }
        deletion_exit.target_fingerprint = sha256(
            json.dumps(target_identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if (
            lifecycle is None
            and binding is None
            and not has_signal_candidate
            and not has_trade_signal
            and not has_execution_event
        ):
            deletion_exit.state = "succeeded"
            deletion_exit.last_reason = "non_strategy_or_unlinked"
            deletion_exit.last_error = None
            deletion_exit.claim_token = None
            deletion_exit.claimed_at = None
            deletion_exit.completed_at = updated_at
            event.processing_status = "ignored"
            event.reason_code = "non_strategy_or_unlinked"
            event.completed_at = updated_at
    if exit_target_is_mutable and lifecycle is not None and lifecycle.lifecycle_status not in {
        "exited",
        "expired",
        "invalidated",
        "cancelled",
    }:
        lifecycle.management_action = "source_deletion_exit_pending"
        lifecycle.management_note = "source_message_deleted"
        lifecycle.updated_at = updated_at
    return deletion_exit
