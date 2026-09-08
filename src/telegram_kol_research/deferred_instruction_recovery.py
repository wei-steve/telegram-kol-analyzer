"""Resume and expire instructions deferred behind a source-deletion exit.

A-3. ``source_execution_barrier`` answers ``hold`` while an overlapping
deleted source still has an unfinished exit; the caller then persists
``automation_status='deferred'`` with reason ``waiting_source_deletion_exit``
and stops. Nothing ever revisited that message: it owns a decision row, so the
authoritative gap recovery -- which looks for messages with *no* decision --
never saw it, and the deletion worker only enqueued its own notification. On
2026-09-07 that silently dropped an auto-trade entry (raw 15169, lifecycle 1096
showing ``entered`` with no binding) and left 29 instruction items ``pending``,
the oldest from 2026-07-22, every one with ``last_progress_at`` null.

This module closes both ends of that gap:

* :func:`resume_instructions_deferred_by_exit` runs when a deletion exit
  reaches a terminal state and re-arms the messages that exit was holding.
* :func:`expire_stale_deferred_instructions` runs on the worker's gap-recovery
  cadence and turns a deferral that outlived its timeout into a durable,
  always-notified incident instead of silence.

Neither one executes anything. Resume only puts a message back on the queue so
the normal path re-evaluates it under the barrier; expiry only marks and
alerts. An expired entry is never submitted late.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MessageInstructionItem,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
)


logger = logging.getLogger(__name__)

#: The reason ``source_execution_barrier`` records when it answers ``hold``.
DEFERRED_HOLD_REASON = "waiting_source_deletion_exit"
#: The queue reason a resumed message carries, so the row says why it came back.
DEFERRED_RESUME_JOB_REASON = "deferred_resume"
#: The terminal reason an outlived deferral is marked with. It is deliberately
#: not ``waiting_source_deletion_exit`` any more: that difference is what stops
#: the resume path from ever picking the message up again.
DEFERRED_EXPIRED_REASON = "deferred_expired"
DEFERRED_EXPIRED_INCIDENT_TYPE = "deferred_instruction_expired"
#: Instruction-item statuses that are still waiting on something.
_OPEN_ITEM_STATUSES = ("pending", "executing")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _candidate_symbol_side_pairs(session, *, raw_message_id: int) -> set[tuple[str, str]]:
    rows = (
        session.query(SignalCandidate.symbol, SignalCandidate.side)
        .filter(
            SignalCandidate.raw_message_id == int(raw_message_id),
            SignalCandidate.symbol.is_not(None),
            SignalCandidate.side.is_not(None),
        )
        .all()
    )
    return {
        (str(symbol), str(side))
        for symbol, side in rows
        if str(symbol or "").strip() and str(side or "").strip()
    }


def _latest_candidate_symbol_side(
    session, *, raw_message_id: int
) -> tuple[str, str] | None:
    """Mirror the barrier's own choice of which candidate speaks for a message."""

    row = (
        session.query(SignalCandidate.symbol, SignalCandidate.side)
        .filter(
            SignalCandidate.raw_message_id == int(raw_message_id),
            SignalCandidate.symbol.is_not(None),
            SignalCandidate.side.is_not(None),
        )
        .order_by(SignalCandidate.id.desc())
        .first()
    )
    if row is None:
        return None
    symbol = str(row[0] or "").strip().upper()
    side = str(row[1] or "").strip().lower()
    if not symbol or not side:
        return None
    return symbol, side


def _still_held(session, *, raw_message: RawMessage, symbol: str, side: str) -> bool:
    """Read-only mirror of the barrier's overlapping-exit hold query.

    A deletion exit can end terminal without releasing anything: the barrier
    releases on ``succeeded`` alone, so an exit that finishes
    ``recovery_required`` keeps holding. Re-arming a message that is still held
    would only spend a fresh authoritative assessment to record the same
    deferral again, so the resume path asks this first and skips those. They
    stay for :func:`expire_stale_deferred_instructions`, which is the path that
    is supposed to end them.
    """

    overlapping_exit = (
        session.query(SourceMessageDeletionExit.id)
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
        .first()
    )
    return overlapping_exit is not None


def _touch_open_instruction_items(
    session, *, raw_message_id: int, now: datetime
) -> int:
    """Record that something moved for this message's still-open items."""

    return (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.raw_message_id == int(raw_message_id),
            MessageInstructionItem.status.in_(_OPEN_ITEM_STATUSES),
            MessageInstructionItem.retired_at.is_(None),
        )
        .update(
            {MessageInstructionItem.last_progress_at: now},
            synchronize_session=False,
        )
    )


def find_messages_deferred_by_exit(
    session_factory: sessionmaker,
    *,
    deletion_exit_id: int,
    now: datetime,
    timeout_minutes: float,
) -> list[int]:
    """Return the raw messages this finished exit was holding, read-only.

    The association key is the barrier's own: same chat, a different raw
    message, and the deleted message's ``(symbol, side)`` equal to the held
    message's latest candidate. Messages already past ``timeout_minutes`` are
    left out so a resume can never race the expiry path into executing a stale
    entry.
    """

    now = _naive_utc(now)
    cutoff = now - timedelta(minutes=float(timeout_minutes))
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, int(deletion_exit_id))
        if deletion_exit is None or deletion_exit.raw_message_id is None:
            return []
        deleted_message = session.get(RawMessage, int(deletion_exit.raw_message_id))
        if deleted_message is None:
            return []
        held_pairs = _candidate_symbol_side_pairs(
            session, raw_message_id=int(deleted_message.id)
        )
        if not held_pairs:
            return []
        deferred_rows = (
            session.query(RawMessage, RecognitionDecision.updated_at)
            .join(
                RecognitionDecision,
                RecognitionDecision.raw_message_id == RawMessage.id,
            )
            .filter(
                RawMessage.chat_id == deleted_message.chat_id,
                RawMessage.id != deleted_message.id,
                RawMessage.source_status != "deleted",
                RecognitionDecision.automation_status == "deferred",
                RecognitionDecision.automation_reason == DEFERRED_HOLD_REASON,
            )
            .order_by(RawMessage.id.asc())
            .all()
        )
        resumable: list[int] = []
        for raw_message, decided_at in deferred_rows:
            pair = _latest_candidate_symbol_side(
                session, raw_message_id=int(raw_message.id)
            )
            if pair is None or pair not in held_pairs:
                continue
            if decided_at is not None and _naive_utc(decided_at) <= cutoff:
                # Past the timeout. Only the expiry path may end this one.
                continue
            if _still_held(
                session,
                raw_message=raw_message,
                symbol=pair[0],
                side=pair[1],
            ):
                continue
            resumable.append(int(raw_message.id))
    return resumable


def resume_instructions_deferred_by_exit(
    session_factory: sessionmaker,
    *,
    deletion_exit_id: int,
    now: datetime | None = None,
    timeout_minutes: float | None = None,
    enqueue=None,
) -> list[int]:
    """Re-arm the queue for every message this finished exit was holding.

    Idempotent by construction: the enqueue upsert only re-arms a *terminal*
    job row, so a second call while the first resume is still pending or
    claimed changes nothing.
    """

    now = _naive_utc(now or datetime.now(UTC))
    if timeout_minutes is None:
        from telegram_kol_research.trading_settings import load_trading_settings

        timeout_minutes = float(
            load_trading_settings(session_factory).deferred_resume_timeout_minutes
        )
    raw_message_ids = find_messages_deferred_by_exit(
        session_factory,
        deletion_exit_id=int(deletion_exit_id),
        now=now,
        timeout_minutes=float(timeout_minutes),
    )
    if not raw_message_ids:
        return []
    if enqueue is None:
        from telegram_kol_research.telegram_live_listener import (
            _enqueue_processing_jobs,
        )

        enqueue = _enqueue_processing_jobs
    enqueue(
        session_factory,
        raw_message_ids=list(raw_message_ids),
        last_reason=DEFERRED_RESUME_JOB_REASON,
        resume_terminal_jobs=True,
    )
    with session_factory() as session:
        for raw_message_id in raw_message_ids:
            _touch_open_instruction_items(
                session, raw_message_id=int(raw_message_id), now=now
            )
        session.commit()
    logger.info(
        "resumed deferred instructions exit_id=%s raw_message_ids=%s",
        int(deletion_exit_id),
        raw_message_ids,
    )
    return raw_message_ids


def expire_stale_deferred_instructions(
    session_factory: sessionmaker,
    *,
    now: datetime | None = None,
    timeout_minutes: float | None = None,
    capture=None,
) -> list[int]:
    """End deferrals that outlived the timeout, loudly and without executing.

    The message keeps ``automation_status='deferred'`` -- it really was
    deferred, and nothing about it succeeded -- but its reason becomes
    ``deferred_expired``, which is both the operator-visible fact and the flag
    that stops the resume path from touching it later. Its open instruction
    items are marked ``escalation_state='expired'``. **No entry or management
    instruction is submitted late**: the whole point is that a stale
    instruction is a decision for a person, not for this loop.
    """

    now = _naive_utc(now or datetime.now(UTC))
    if timeout_minutes is None:
        from telegram_kol_research.trading_settings import load_trading_settings

        timeout_minutes = float(
            load_trading_settings(session_factory).deferred_resume_timeout_minutes
        )
    cutoff = now - timedelta(minutes=float(timeout_minutes))
    with session_factory() as session:
        rows = (
            session.query(RecognitionDecision.id, RecognitionDecision.raw_message_id)
            .filter(
                RecognitionDecision.automation_status == "deferred",
                RecognitionDecision.automation_reason == DEFERRED_HOLD_REASON,
                RecognitionDecision.updated_at <= cutoff,
            )
            .order_by(RecognitionDecision.raw_message_id.asc())
            .all()
        )
        expired: list[int] = []
        for decision_id, raw_message_id in rows:
            # Compare-and-set on the exact reason: a concurrent resume that
            # already moved this row wins, and this pass skips it.
            updated = (
                session.query(RecognitionDecision)
                .filter(
                    RecognitionDecision.id == int(decision_id),
                    RecognitionDecision.automation_status == "deferred",
                    RecognitionDecision.automation_reason == DEFERRED_HOLD_REASON,
                )
                .update(
                    {
                        RecognitionDecision.automation_reason: (
                            DEFERRED_EXPIRED_REASON
                        ),
                        RecognitionDecision.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                continue
            session.query(MessageInstructionItem).filter(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.status.in_(_OPEN_ITEM_STATUSES),
                MessageInstructionItem.retired_at.is_(None),
            ).update(
                {
                    MessageInstructionItem.last_progress_at: now,
                    MessageInstructionItem.escalation_state: "expired",
                },
                synchronize_session=False,
            )
            expired.append(int(raw_message_id))
        session.commit()
    if not expired:
        return []
    if capture is None:
        from telegram_kol_research.runtime_incident_adapters import (
            capture_deferred_instruction_expired,
            capture_runtime_incident_best_effort,
        )

        def capture(raw_message_id: int) -> None:
            capture_runtime_incident_best_effort(
                capture_deferred_instruction_expired,
                session_factory,
                raw_message_id=int(raw_message_id),
                deferred_minutes=int(timeout_minutes),
                occurred_at=now,
            )

    for raw_message_id in expired:
        try:
            capture(int(raw_message_id))
        except Exception:
            # The ledger change is already committed and is the durable fact.
            logger.warning(
                "deferred instruction expiry incident capture raised "
                "raw_message_id=%s",
                raw_message_id,
                exc_info=True,
            )
    logger.warning(
        "deferred instructions expired without execution raw_message_ids=%s",
        expired,
    )
    return expired
