"""Durable per-candidate work items for multi-instruction messages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import case, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


MANAGEMENT_EVENT_TYPES = frozenset({"close_signal", "position_update"})
FINISH_STATUSES = frozenset({"submitted", "succeeded", "failed", "unknown"})
ERROR_STATUSES = frozenset({"failed", "unknown"})


def create_message_instruction_items_in_session(
    session: Session,
    *,
    raw_message_id: int,
) -> list[MessageInstructionItem]:
    """Create one stable item per actionable candidate and return sequence order."""

    raw_message = session.get(RawMessage, int(raw_message_id))
    if raw_message is None:
        raise LookupError("raw message not found")

    kind_order = case(
        (SignalCandidate.event_type.in_(sorted(MANAGEMENT_EVENT_TYPES)), 0),
        (SignalCandidate.event_type == "entry_signal", 1),
        else_=2,
    )
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == int(raw_message_id))
        .filter(
            SignalCandidate.event_type.in_(
                [*sorted(MANAGEMENT_EVENT_TYPES), "entry_signal"]
            )
        )
        .order_by(kind_order, SignalCandidate.id)
        .all()
    )
    existing_by_candidate_id = {
        item.signal_candidate_id: item
        for item in session.query(MessageInstructionItem)
        .filter(MessageInstructionItem.raw_message_id == int(raw_message_id))
        .all()
    }

    items: list[MessageInstructionItem] = []
    for sequence, candidate in enumerate(candidates):
        instruction_kind = _instruction_kind(candidate)
        strategy_instance_id = _candidate_strategy_instance_id(
            session,
            raw_message=raw_message,
            candidate=candidate,
            instruction_kind=instruction_kind,
        )
        item = existing_by_candidate_id.get(candidate.id)
        if item is None:
            idempotency_key = _idempotency_key(
                raw_message_id=raw_message.id,
                signal_candidate_id=candidate.id,
                instruction_kind=instruction_kind,
                target_lifecycle_id=candidate.target_lifecycle_id,
                strategy_instance_id=strategy_instance_id,
            )
            try:
                with session.begin_nested():
                    item = MessageInstructionItem(
                        raw_message_id=raw_message.id,
                        signal_candidate_id=candidate.id,
                        sequence=sequence,
                        instruction_kind=instruction_kind,
                        strategy_instance_id=strategy_instance_id,
                        idempotency_key=idempotency_key,
                        status="pending",
                    )
                    session.add(item)
                    session.flush()
            except IntegrityError:
                item = (
                    session.query(MessageInstructionItem)
                    .filter(
                        MessageInstructionItem.raw_message_id == raw_message.id,
                        MessageInstructionItem.signal_candidate_id == candidate.id,
                    )
                    .one_or_none()
                )
                if item is None:
                    item = (
                        session.query(MessageInstructionItem)
                        .filter(
                            MessageInstructionItem.idempotency_key
                            == idempotency_key
                        )
                        .one_or_none()
                    )
                if item is None:
                    raise
            existing_by_candidate_id[candidate.id] = item
        item.sequence = sequence
        items.append(item)

    session.flush()
    return items


def claim_next_message_instruction_item(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    now: datetime,
) -> MessageInstructionItem | None:
    """Atomically claim the first pending item without bypassing active work."""

    with session_factory() as session:
        pending_item = aliased(MessageInstructionItem)
        executing_item = aliased(MessageInstructionItem)
        next_item_id = (
            select(pending_item.id)
            .where(
                pending_item.raw_message_id == int(raw_message_id),
                pending_item.status == "pending",
                ~exists(
                    select(executing_item.id).where(
                        executing_item.raw_message_id == int(raw_message_id),
                        executing_item.status == "executing",
                    )
                ),
            )
            .order_by(pending_item.sequence, pending_item.id)
            .limit(1)
            .scalar_subquery()
        )
        item_id = session.scalar(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id == next_item_id,
                MessageInstructionItem.status == "pending",
            )
            .values(status="executing", updated_at=now)
            .returning(MessageInstructionItem.id)
        )
        if item_id is None:
            session.rollback()
            return None

        session.commit()
        item = session.get(MessageInstructionItem, item_id)
        if item is None:
            raise RuntimeError("claimed instruction item disappeared")
        session.expunge(item)
        return item


def finish_message_instruction_item(
    session_factory: sessionmaker,
    *,
    item_id: int,
    status: str,
    result: dict,
    now: datetime,
) -> None:
    """Finish a claimed item exactly once and persist its result channel."""

    if status not in FINISH_STATUSES:
        raise ValueError(f"unsupported finish status: {status}")

    payload_json = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    values = {
        "status": status,
        "result_json": None if status in ERROR_STATUSES else payload_json,
        "error_json": payload_json if status in ERROR_STATUSES else None,
        "updated_at": now,
    }
    with session_factory() as session:
        update_result = session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id == int(item_id),
                MessageInstructionItem.status == "executing",
            )
            .values(**values)
        )
        if update_result.rowcount != 1:
            session.rollback()
            raise RuntimeError("instruction item is missing or not executing")
        session.commit()


def list_message_instruction_item_results(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> list[dict]:
    """Return stable, sequence-ordered public results for one message."""

    with session_factory() as session:
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == int(raw_message_id))
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        return [_public_item_result(item) for item in items]


def _public_item_result(item: MessageInstructionItem) -> dict:
    summary = {
        "item_id": item.id,
        "instruction_kind": item.instruction_kind,
        "status": item.status,
    }
    payload_text = (
        item.error_json if item.status in ERROR_STATUSES else item.result_json
    )
    payload = json.loads(payload_text) if payload_text else None
    if item.status in ERROR_STATUSES:
        summary["reason"] = _payload_reason(payload, fallback=item.status)
    elif payload is not None:
        summary["result"] = payload
    else:
        summary["reason"] = item.status
    return summary


def _payload_reason(payload, *, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("reason", "message", "error"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    if payload not in (None, ""):
        return str(payload)
    return fallback


def _instruction_kind(candidate: SignalCandidate) -> str:
    if candidate.event_type in MANAGEMENT_EVENT_TYPES:
        return "management"
    return "entry"


def _candidate_strategy_instance_id(
    session: Session,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    instruction_kind: str,
) -> str | None:
    if instruction_kind == "entry":
        if not candidate.symbol or not candidate.side:
            return None
        return build_strategy_instance_id(
            venue="deepcoin",
            chat_id=raw_message.chat_id,
            message_id=raw_message.message_id,
            symbol=candidate.symbol,
            side=candidate.side,
        )

    if candidate.target_lifecycle_id is None:
        return None
    lifecycle = session.get(StrategyLifecycle, candidate.target_lifecycle_id)
    if lifecycle is None:
        return None
    binding = (
        session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if lifecycle.execution_binding_id is not None
        else None
    )
    if binding is not None and binding.strategy_instance_id:
        return str(binding.strategy_instance_id)
    return build_strategy_instance_id(
        venue="deepcoin",
        chat_id=lifecycle.chat_id,
        message_id=lifecycle.message_id,
        symbol=lifecycle.symbol,
        side=lifecycle.side,
    )


def _idempotency_key(
    *,
    raw_message_id: int,
    signal_candidate_id: int,
    instruction_kind: str,
    target_lifecycle_id: int | None,
    strategy_instance_id: str | None,
) -> str:
    canonical = ":".join(
        (
            str(raw_message_id),
            str(signal_candidate_id),
            instruction_kind,
            str(target_lifecycle_id) if target_lifecycle_id is not None else "",
            strategy_instance_id or "",
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
