"""Durable per-candidate work items for multi-instruction messages."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import and_, case, exists, or_, select, update
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


MANAGEMENT_EVENT_TYPES = frozenset(
    {"close_signal", "position_update", "strategy_revision"}
)
FINISH_STATUSES = frozenset({"submitted", "succeeded", "failed", "unknown"})
ERROR_STATUSES = frozenset({"failed", "unknown"})
SUMMARY_NOTIFICATION_LEASE = timedelta(minutes=5)
VISIBILITY_RETRY_DELAYS = (5, 15, 30, 60, 120, 300)
VISIBILITY_RETRY_DEADLINE = timedelta(hours=6)
VISIBILITY_RETRY_CLAIM_LEASE = timedelta(minutes=5)
VISIBILITY_DEFER_REASONS = frozenset(
    {
        "target_strategy_binding_not_visible_yet",
        "preceding_entry_context_unresolved",
    }
)


def should_defer_instruction_result(result: dict) -> bool:
    return (
        str(result.get("status") or "") == "deferred"
        and str(result.get("reason") or "") in VISIBILITY_DEFER_REASONS
    )


def create_message_instruction_items_in_session(
    session: Session,
    *,
    raw_message_id: int,
    candidate_ids: Collection[int] | None = None,
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
    candidate_query = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == int(raw_message_id))
        .filter(
            SignalCandidate.event_type.in_(
                [*sorted(MANAGEMENT_EVENT_TYPES), "entry_signal"]
            )
        )
    )
    if candidate_ids is not None:
        normalized_candidate_ids = {int(candidate_id) for candidate_id in candidate_ids}
        if not normalized_candidate_ids:
            return []
        candidate_query = candidate_query.filter(
            SignalCandidate.id.in_(normalized_candidate_ids)
        )
    candidates = candidate_query.order_by(kind_order, SignalCandidate.id).all()
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
        item.retired_at = None
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
                pending_item.retired_at.is_(None),
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
                MessageInstructionItem.retired_at.is_(None),
                or_(
                    MessageInstructionItem.visibility_next_attempt_at.is_(None),
                    MessageInstructionItem.visibility_next_attempt_at <= now,
                ),
            )
            .values(status="executing", updated_at=now)
            .returning(MessageInstructionItem.id)
        )
        if item_id is None:
            session.commit()
            return None

        session.commit()
        item = session.get(MessageInstructionItem, item_id)
        if item is None:
            raise RuntimeError("claimed instruction item disappeared")
        session.expunge(item)
        return item


def defer_message_instruction_item_for_visibility(
    session_factory: sessionmaker,
    *,
    item_id: int,
    result: dict,
    now: datetime,
) -> str:
    """Return an executing item to pending with bounded persistent backoff."""

    payload_json = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, int(item_id))
        if item is None or item.status != "executing":
            raise RuntimeError("instruction item is missing or not executing")
        first_failed_at = item.visibility_first_failed_at or now
        deadline = first_failed_at + VISIBILITY_RETRY_DEADLINE
        if not _datetime_after(deadline, now):
            item.status = "failed"
            item.error_json = json.dumps(
                {
                    **result,
                    "reason": "target_strategy_binding_visibility_retry_expired",
                    "priority": "high",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            item.result_json = None
            item.visibility_next_attempt_at = None
            item.summary_notification_status = "pending"
            item.summary_notification_claim_token = None
            item.summary_notification_claimed_at = None
            item.updated_at = now
            session.commit()
            return "failed"

        attempts = int(item.visibility_retry_attempts or 0) + 1
        delay = VISIBILITY_RETRY_DELAYS[
            min(attempts - 1, len(VISIBILITY_RETRY_DELAYS) - 1)
        ]
        item.status = "pending"
        item.result_json = payload_json
        item.error_json = None
        item.visibility_first_failed_at = first_failed_at
        item.visibility_retry_attempts = attempts
        item.visibility_next_attempt_at = now + timedelta(seconds=delay)
        item.updated_at = now
        session.commit()
        return "pending"


def claim_next_visibility_retry_instruction_item(
    session_factory: sessionmaker,
    *,
    now: datetime,
) -> MessageInstructionItem | None:
    """Atomically claim the oldest due management visibility retry."""

    with session_factory() as session:
        candidate = aliased(MessageInstructionItem)
        earlier = aliased(MessageInstructionItem)
        stale_cutoff = now - VISIBILITY_RETRY_CLAIM_LEASE
        expiry_cutoff = now - VISIBILITY_RETRY_DEADLINE
        expired_error_json = json.dumps(
            {
                "reason": "target_strategy_binding_visibility_retry_expired",
                "priority": "high",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.visibility_first_failed_at.is_not(None),
                MessageInstructionItem.visibility_first_failed_at <= expiry_cutoff,
                or_(
                    MessageInstructionItem.status == "pending",
                    and_(
                        MessageInstructionItem.status == "executing",
                        MessageInstructionItem.updated_at <= stale_cutoff,
                    ),
                ),
            )
            .values(
                status="failed",
                result_json=None,
                error_json=expired_error_json,
                visibility_next_attempt_at=None,
                summary_notification_status="pending",
                summary_notification_claim_token=None,
                summary_notification_claimed_at=None,
                updated_at=now,
            )
        )
        session.flush()
        claimable = or_(
            and_(
                candidate.status == "pending",
                candidate.visibility_next_attempt_at.is_not(None),
                candidate.visibility_next_attempt_at <= now,
            ),
            and_(
                candidate.status == "executing",
                candidate.updated_at <= stale_cutoff,
            ),
        )
        next_item_id = (
            select(candidate.id)
            .where(
                candidate.retired_at.is_(None),
                candidate.instruction_kind == "management",
                candidate.visibility_first_failed_at.is_not(None),
                claimable,
                ~exists(
                    select(earlier.id).where(
                        earlier.raw_message_id == candidate.raw_message_id,
                        earlier.retired_at.is_(None),
                        earlier.status.in_(("pending", "executing")),
                        or_(
                            earlier.sequence < candidate.sequence,
                            and_(
                                earlier.sequence == candidate.sequence,
                                earlier.id < candidate.id,
                            ),
                        ),
                    )
                ),
            )
            .order_by(
                candidate.visibility_next_attempt_at.asc(),
                candidate.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        item_id = session.scalar(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id == next_item_id,
                MessageInstructionItem.retired_at.is_(None),
                or_(
                    and_(
                        MessageInstructionItem.status == "pending",
                        MessageInstructionItem.visibility_next_attempt_at.is_not(
                            None
                        ),
                        MessageInstructionItem.visibility_next_attempt_at <= now,
                    ),
                    and_(
                        MessageInstructionItem.status == "executing",
                        MessageInstructionItem.updated_at <= stale_cutoff,
                    ),
                ),
            )
            .values(status="executing", updated_at=now)
            .returning(MessageInstructionItem.id)
        )
        if item_id is None:
            session.commit()
            return None
        session.commit()
        item = session.get(MessageInstructionItem, item_id)
        if item is None:
            raise RuntimeError("claimed visibility retry item disappeared")
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
            .filter(MessageInstructionItem.retired_at.is_(None))
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        return [_public_item_result(item) for item in items]


def has_message_instruction_items(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> bool:
    """Return whether orchestration was ever projected for the message."""

    with session_factory() as session:
        return (
            session.query(MessageInstructionItem.id)
            .filter(MessageInstructionItem.raw_message_id == int(raw_message_id))
            .first()
            is not None
        )


def claim_message_instruction_summary(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    claimed_at: datetime,
    chat_title: str | None = None,
) -> dict | None:
    """Claim one terminal active-item summary before any external delivery."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == int(raw_message_id))
            .filter(MessageInstructionItem.retired_at.is_(None))
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        if not items or any(item.status not in FINISH_STATUSES for item in items):
            return None
        lease_cutoff = claimed_at - SUMMARY_NOTIFICATION_LEASE
        if any(
            item.summary_notification_status == "delivering"
            and item.summary_notification_claimed_at is not None
            and _datetime_after(item.summary_notification_claimed_at, lease_cutoff)
            for item in items
        ):
            return None
        claimable_item_ids = [
            item.id
            for item in items
            if item.summary_notification_status != "delivered"
            and (
                item.summary_notification_status != "delivering"
                or item.summary_notification_claimed_at is None
                or not _datetime_after(
                    item.summary_notification_claimed_at,
                    lease_cutoff,
                )
            )
        ]
        if not claimable_item_ids:
            return None

        claim_token = uuid.uuid4().hex
        claimed = session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id.in_(claimable_item_ids),
                MessageInstructionItem.retired_at.is_(None),
                or_(
                    MessageInstructionItem.summary_notification_status.in_(
                        ["pending", "failed"]
                    ),
                    MessageInstructionItem.summary_notification_status.is_(None),
                    and_(
                        MessageInstructionItem.summary_notification_status
                        == "delivering",
                        or_(
                            MessageInstructionItem.summary_notification_claimed_at.is_(
                                None
                            ),
                            MessageInstructionItem.summary_notification_claimed_at
                            <= lease_cutoff,
                        ),
                    ),
                ),
            )
            .values(
                summary_notification_status="delivering",
                summary_notification_claim_token=claim_token,
                summary_notification_claimed_at=claimed_at,
                summary_notification_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != len(claimable_item_ids):
            session.rollback()
            return None
        payload = {
            "raw_message_id": raw_message.id,
            "chat_id": raw_message.chat_id,
            "message_id": raw_message.message_id,
            "chat_title": chat_title or raw_message.sender_name,
            "notification_id": _summary_notification_id(items),
            "notification_claim_token": claim_token,
            "notification_item_ids": claimable_item_ids,
            "items": [_public_item_result(item) for item in items],
        }
        session.commit()
        return payload


def finish_message_instruction_summary_delivery(
    session_factory: sessionmaker,
    *,
    claim_token: str,
    item_ids: Collection[int],
    delivered: bool,
    completed_at: datetime,
    error: str | None = None,
) -> None:
    """Finalize one leased summary delivery without accepting a stale worker."""

    normalized_item_ids = [int(item_id) for item_id in item_ids]
    if not normalized_item_ids:
        raise ValueError("notification item ids are required")
    values = {
        "summary_notification_status": "delivered" if delivered else "failed",
        "summary_notification_claim_token": None,
        "summary_notification_error": (
            None if delivered else str(error or "delivery_failed")[:1000]
        ),
        "summary_notified_at": completed_at if delivered else None,
    }
    with session_factory() as session:
        finalized = session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id.in_(normalized_item_ids),
                MessageInstructionItem.summary_notification_status == "delivering",
                MessageInstructionItem.summary_notification_claim_token == claim_token,
            )
            .values(**values)
        )
        if finalized.rowcount != len(normalized_item_ids):
            session.rollback()
            raise RuntimeError("message instruction summary claim is stale")
        session.commit()


def _summary_notification_id(items: list[MessageInstructionItem]) -> str:
    canonical = ":".join(item.idempotency_key for item in items)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _datetime_after(value: datetime, boundary: datetime) -> bool:
    if value.tzinfo is None and boundary.tzinfo is not None:
        boundary = boundary.replace(tzinfo=None)
    elif value.tzinfo is not None and boundary.tzinfo is None:
        value = value.replace(tzinfo=None)
    return value > boundary


def _public_item_result(item: MessageInstructionItem) -> dict:
    summary = {
        "item_id": item.id,
        "sequence": item.sequence,
        "instruction_kind": item.instruction_kind,
        "strategy_instance_id": item.strategy_instance_id,
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
