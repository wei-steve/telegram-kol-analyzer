"""Messages an MiMo provider outage kept from being recognised (step-18, step 3).

On 2026-09-12 the provider refused every call for fourteen hours. Each message
that arrived in that time failed recognition, then aged past the 15-minute
recovery window and was given up for good -- the window measured the outage,
not our processing. This module answers three questions so the rest of the
pipeline can treat those messages differently from ordinary slow ones:

* **Was this message delayed by a provider outage?** Answered from durable
  facts only: whether the append-only ``mimo_recognition_attempts`` audit holds
  a ``mimo_provider_unavailable.*`` row for it. A marker on the queue job would
  not survive a claim (``last_reason`` is overwritten) or a crash mid-replay,
  and losing it would let a fourteen-hour-old entry execute as a new one.
* **How old is it, not counting the outage?** Its effective age: wall-clock age
  minus the part of it the provider was unavailable, measured on the message's
  own attempts -- from its first unavailable attempt to the first attempt,
  by any message, that shows the provider answering again.
* **Which messages should be replayed after recovery?** auto_trade groups
  only, the two "no decision was produced" outcomes only, oldest first.

It decides nothing about execution. What a replayed message may do is decided
at the execution gate (``auto_trade_execution``), by the ruling of 2026-09-12:
entries are never replayed, management is replayed only while young and while
its target position is still open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.mimo_provider_health import (
    REQUEST_REJECTED_ERROR_CODE_PREFIX,
    RESPONSE_INVALID_ERROR_CODE,
    UNAVAILABLE_ERROR_CODE_PREFIX,
)
from telegram_kol_research.models import (
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.recognition_failure_attribution import (
    AUTHORITY_NOT_PRODUCED_REASONS,
)


#: The recovery window the ruling applies to replayed management instructions.
REPLAY_MAX_EFFECTIVE_AGE = timedelta(minutes=15)
REPLAY_QUEUE_REASON = "provider_outage_replay"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class MessageOutageSpan:
    """The part of a message's life the provider could not serve it."""

    first_unavailable_at: datetime
    last_unavailable_at: datetime
    recovered_at: datetime | None

    def overlap(self, *, start: datetime, end: datetime) -> timedelta:
        span_end = self.recovered_at or end
        lower = max(_aware(start), self.first_unavailable_at)
        upper = min(_aware(end), span_end)
        return max(timedelta(0), upper - lower)


def load_message_outage_span(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> MessageOutageSpan | None:
    """``None`` when no attempt for this message was refused by the provider."""

    answered_filter = or_(
        MimoRecognitionAttempt.status == "completed",
        MimoRecognitionAttempt.error_code.startswith(REQUEST_REJECTED_ERROR_CODE_PREFIX),
        MimoRecognitionAttempt.error_code == RESPONSE_INVALID_ERROR_CODE,
    )
    with session_factory() as session:
        failures = (
            session.query(
                MimoRecognitionAttempt.started_at,
                MimoRecognitionAttempt.completed_at,
            )
            .join(MimoRecognitionRun, MimoRecognitionRun.id == MimoRecognitionAttempt.run_id)
            .filter(
                MimoRecognitionRun.raw_message_id == int(raw_message_id),
                MimoRecognitionAttempt.error_code.startswith(
                    UNAVAILABLE_ERROR_CODE_PREFIX
                ),
            )
            .order_by(MimoRecognitionAttempt.completed_at.asc())
            .all()
        )
        # Rule A (2026-09-13): a failure while other calls were being answered
        # is an isolated request -- a hung connection -- not the provider being
        # down. Counting it would mark a message "delayed by an outage", age it
        # without that time and refuse it as a late entry: carrying a false
        # alarm into the trading path, which matters more than the alarm.
        kept: list[datetime] = []
        for request_started_at, request_completed_at in failures:
            overlapped = (
                session.query(MimoRecognitionAttempt.id)
                .filter(
                    answered_filter,
                    MimoRecognitionAttempt.completed_at >= request_started_at,
                    MimoRecognitionAttempt.completed_at <= request_completed_at,
                )
                .first()
            )
            if overlapped is None:
                kept.append(request_completed_at)
        if not kept:
            return None
        first = _aware(kept[0])
        last = _aware(kept[-1])
        answered = (
            session.query(MimoRecognitionAttempt.completed_at)
            .filter(
                MimoRecognitionAttempt.completed_at > _naive(last),
                or_(
                    MimoRecognitionAttempt.status == "completed",
                    MimoRecognitionAttempt.error_code.startswith(
                        REQUEST_REJECTED_ERROR_CODE_PREFIX
                    ),
                    MimoRecognitionAttempt.error_code == RESPONSE_INVALID_ERROR_CODE,
                ),
            )
            .order_by(MimoRecognitionAttempt.completed_at.asc())
            .first()
        )
    return MessageOutageSpan(
        first_unavailable_at=first,
        last_unavailable_at=last,
        recovered_at=_aware(answered[0]) if answered is not None else None,
    )


def effective_message_age(
    *,
    posted_at: datetime,
    now: datetime,
    span: MessageOutageSpan | None,
) -> timedelta:
    """Wall-clock age, minus the time the provider was unavailable to it.

    A message never refused by the provider keeps its plain age: ordinary slow
    processing still spends the recovery window, as before.
    """

    age = max(timedelta(0), _aware(now) - _aware(posted_at))
    if span is None:
        return age
    return max(timedelta(0), age - span.overlap(start=posted_at, end=now))


def _delayed_message_ids_query(session, *, since: datetime):
    return (
        session.query(MimoRecognitionRun.raw_message_id)
        .join(
            MimoRecognitionAttempt,
            MimoRecognitionAttempt.run_id == MimoRecognitionRun.id,
        )
        .filter(
            MimoRecognitionAttempt.error_code.startswith(UNAVAILABLE_ERROR_CODE_PREFIX),
            MimoRecognitionAttempt.completed_at >= _naive(since),
        )
    )


def delayed_chat_ids(session_factory: sessionmaker, *, since: datetime) -> list[int]:
    """Chats with at least one message the provider refused since ``since``."""

    with session_factory() as session:
        rows = (
            session.query(RawMessage.chat_id)
            .filter(RawMessage.id.in_(_delayed_message_ids_query(session, since=since)))
            .distinct()
            .all()
        )
    return sorted({int(row[0]) for row in rows})


def select_replay_candidates(
    session_factory: sessionmaker,
    *,
    auto_trade_chat_ids: Iterable[int],
    since: datetime,
    recovered_at: datetime,
    limit: int = 500,
) -> list[int]:
    """raw_message ids to replay, oldest first.

    Only messages whose recorded outcome is "no authoritative decision was
    produced", whose own attempts were refused by the provider at or after
    ``since`` (the outage start), and that have **not been tried again since
    ``recovered_at``**. That last condition is the replay's own dedup: it lives
    in the append-only run audit, so a replay is never queued twice even if
    the notice about it could not be recorded, and a message whose replay
    failed for another reason is not queued again forever.
    """

    chat_ids = sorted({int(value) for value in auto_trade_chat_ids})
    if not chat_ids:
        return []
    with session_factory() as session:
        retried = session.query(MimoRecognitionRun.raw_message_id).filter(
            MimoRecognitionRun.created_at > _naive(recovered_at)
        )
        rows = (
            session.query(RawMessage.id)
            .join(RecognitionDecision, RecognitionDecision.raw_message_id == RawMessage.id)
            .filter(
                RawMessage.chat_id.in_(chat_ids),
                RecognitionDecision.automation_reason.in_(
                    sorted(AUTHORITY_NOT_PRODUCED_REASONS)
                ),
                RawMessage.id.in_(_delayed_message_ids_query(session, since=since)),
                ~RawMessage.id.in_(retried),
            )
            .order_by(RawMessage.posted_at.asc(), RawMessage.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
    return [int(row[0]) for row in rows]


def run_provider_outage_replay_tick(
    session_factory: sessionmaker,
    *,
    group_trading_mode_provider: Callable[[int], Any] | None,
    now: datetime | None = None,
    enqueue: Callable[..., Any] | None = None,
    capture_started: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """After an announced recovery, give the delayed auto_trade messages back.

    Runs every gap-recovery tick and is idempotent by construction (see
    ``select_replay_candidates``). Every state that queues nothing says why, so
    the loop can log a change of state rather than stay silent.
    """

    from telegram_kol_research.mimo_provider_health import (
        RECOVERED_INCIDENT_TYPE,
        _incident_recorded,
        load_latest_provider_outage,
    )

    current = _aware(now or datetime.now(UTC))
    outage = load_latest_provider_outage(session_factory)
    if outage is None or outage.recovered_at is None:
        return {"state": "no_recovered_outage"}
    if not _incident_recorded(
        session_factory,
        incident_type=RECOVERED_INCIDENT_TYPE,
        source_record_id=outage.key,
    ):
        # Replay follows the recovery notice, so a person is told the outage
        # ended before messages from it start producing their own notices.
        return {"state": "recovery_not_yet_announced"}
    if group_trading_mode_provider is None:
        return {"state": "no_group_mode_provider"}
    chats = auto_trade_chat_ids(
        delayed_chat_ids(session_factory, since=outage.started_at),
        group_trading_mode_provider,
    )
    if not chats:
        return {"state": "no_delayed_auto_trade_chats"}
    ids = select_replay_candidates(
        session_factory,
        auto_trade_chat_ids=chats,
        since=outage.started_at,
        recovered_at=outage.recovered_at,
    )
    if not ids:
        return {"state": "nothing_to_replay"}
    if enqueue is None:
        from telegram_kol_research.telegram_live_listener import (
            _enqueue_processing_jobs as enqueue,
        )
    enqueue(
        session_factory,
        raw_message_ids=ids,
        last_reason=REPLAY_QUEUE_REASON,
        resume_terminal_jobs=True,
    )
    if not _incident_recorded(
        session_factory,
        incident_type="provider_outage_replay_started",
        source_record_id=outage.key,
    ):
        if capture_started is None:
            from telegram_kol_research.runtime_incident_adapters import (
                capture_provider_outage_replay_started,
                capture_runtime_incident_best_effort,
            )

            def capture_started(**kwargs: Any) -> None:
                capture_runtime_incident_best_effort(
                    capture_provider_outage_replay_started,
                    session_factory,
                    **kwargs,
                )

        capture_started(
            outage_key=outage.key,
            started_at=outage.started_at,
            recovered_at=outage.recovered_at,
            message_count=len(ids),
            occurred_at=current,
        )
    return {"state": "replay_enqueued", "messages": len(ids)}


def auto_trade_chat_ids(
    candidate_chat_ids: Iterable[int],
    group_trading_mode_provider: Callable[[int], Any] | None,
) -> list[int]:
    """The subset of chats configured to trade. Unknown provider: none.

    Replaying into a group whose mode cannot be read would recognise and
    notify on messages nobody asked to act on; the conservative side here is
    to replay nothing and say so, which the caller does.
    """

    if group_trading_mode_provider is None:
        return []
    selected = []
    for chat_id in candidate_chat_ids:
        try:
            mode = str(group_trading_mode_provider(int(chat_id)) or "")
        except Exception:
            continue
        if mode == "auto_trade":
            selected.append(int(chat_id))
    return selected


# --------------------------------------------------------------------------
# What a delayed message may do at execution time (ruling of 2026-09-12)
# --------------------------------------------------------------------------

ENTRY_NOT_REPLAYED = "provider_outage_entry_not_replayed"
MANAGEMENT_TOO_OLD = "provider_outage_management_too_old"
MANAGEMENT_TARGET_UNKNOWN = "provider_outage_management_target_unknown"
MANAGEMENT_SNAPSHOT_STALE = "provider_outage_management_snapshot_stale"
MANAGEMENT_TARGET_NOT_OPEN = "provider_outage_management_target_not_open"
MANAGEMENT_TARGET_OPEN = "provider_outage_management_target_open"


@dataclass(frozen=True, slots=True)
class ReplayVerdict:
    delayed: bool
    effective_age: timedelta | None = None
    span: MessageOutageSpan | None = None


def replay_verdict(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    now: datetime,
) -> ReplayVerdict:
    """Was this message delayed by a provider outage, and how old is it without it.

    ``delayed=False`` means the ordinary path applies unchanged -- this is the
    answer for every message the provider never refused.
    """

    span = load_message_outage_span(session_factory, raw_message_id=raw_message_id)
    if span is None:
        return ReplayVerdict(delayed=False)
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        posted_at = raw_message.posted_at if raw_message is not None else None
    if posted_at is None:
        # A delayed message with no time of its own cannot be shown to be
        # young, so it is treated as too old: nothing executes on a guess.
        return ReplayVerdict(delayed=True, effective_age=None, span=span)
    return ReplayVerdict(
        delayed=True,
        effective_age=effective_message_age(posted_at=posted_at, now=now, span=span),
        span=span,
    )


def management_replay_allowed(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    target_lifecycle_ids: Iterable[int | None],
    now: datetime,
) -> tuple[bool, str | None]:
    """Whether a management instruction delayed by an outage may still execute.

    Returns ``(True, None)`` for a message the outage never touched. For one it
    did, both conditions of the ruling must hold: effective age within the
    recovery window, and every target position still open on the exchange as
    the reconcile loop last saw it. A snapshot too old to judge is not "the
    position is gone" -- it is "we do not know", and that does not execute.
    """

    verdict = replay_verdict(session_factory, raw_message_id=raw_message_id, now=now)
    if not verdict.delayed:
        return True, None
    if verdict.effective_age is None or verdict.effective_age > REPLAY_MAX_EFFECTIVE_AGE:
        return False, MANAGEMENT_TOO_OLD
    lifecycle_ids = sorted({int(value) for value in target_lifecycle_ids if value})
    if not lifecycle_ids:
        return False, MANAGEMENT_TARGET_UNKNOWN
    from telegram_kol_research.management_target_verification import (
        load_verified_position_ids,
        verify_lifecycle_targets,
    )

    with session_factory() as session:
        verified_position_ids = load_verified_position_ids(session, now=now)
        if verified_position_ids is None:
            return False, MANAGEMENT_SNAPSHOT_STALE
        verdicts = verify_lifecycle_targets(
            session,
            lifecycle_ids,
            verified_position_ids=verified_position_ids,
        )
    if verdicts and all(item.verified for item in verdicts.values()):
        return True, MANAGEMENT_TARGET_OPEN
    return False, MANAGEMENT_TARGET_NOT_OPEN


def hold_delayed_management_for_confirmation(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    now: datetime,
) -> tuple[int, ...]:
    """Park a delayed message's management items that may not replay.

    Runs before any item is claimed. It has to: finishing an item requires it
    to be ``executing``, and an item parked after its claim would make that
    finish raise and fail the whole message. Parking a still-``pending`` item
    through the existing confirmation channel keeps it out of the claim query
    and puts ``/choose`` / ``/dismiss`` in front of a person.

    Returns the item ids parked; ``()`` when the message was not delayed, has
    no pending management items, or they may replay.
    """

    verdict = replay_verdict(session_factory, raw_message_id=raw_message_id, now=now)
    if not verdict.delayed:
        return ()
    from telegram_kol_research.models import MessageInstructionItem, SignalCandidate

    with session_factory() as session:
        rows = (
            session.query(SignalCandidate.target_lifecycle_id)
            .join(
                MessageInstructionItem,
                MessageInstructionItem.signal_candidate_id == SignalCandidate.id,
            )
            .filter(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.status == "pending",
                MessageInstructionItem.instruction_kind != "entry",
            )
            .all()
        )
    if not rows:
        return ()
    allowed, reason = management_replay_allowed(
        session_factory,
        raw_message_id=raw_message_id,
        target_lifecycle_ids=[row[0] for row in rows],
        now=now,
    )
    if allowed:
        return ()
    from telegram_kol_research.management_target_verification import (
        request_management_target_confirmation,
    )

    return request_management_target_confirmation(
        session_factory,
        raw_message_id=int(raw_message_id),
        candidates=(),
        snapshot_stale=reason == MANAGEMENT_SNAPSHOT_STALE,
        now=now,
    )


def notify_management_not_replayed(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    chat_id: int,
    message_text: str | None,
    posted_at: datetime | None,
    reason_code: str,
    now: datetime,
    capture: Callable[..., Any] | None = None,
) -> None:
    """A delayed management instruction reached execution and was refused.

    The confirmation channel only speaks when it parked something; a message
    with no instruction item parks nothing, so this is its own voice.
    """

    if capture is None:
        from telegram_kol_research.runtime_incident_adapters import (
            capture_provider_outage_management_not_replayed,
            capture_runtime_incident_best_effort,
        )

        def capture(**kwargs: Any) -> None:
            capture_runtime_incident_best_effort(
                capture_provider_outage_management_not_replayed,
                session_factory,
                **kwargs,
            )

    capture(
        raw_message_id=int(raw_message_id),
        chat_id=int(chat_id),
        message_text=message_text or "",
        posted_at=posted_at,
        reason_code=reason_code,
        occurred_at=now,
    )


def notify_entry_not_replayed(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    chat_id: int,
    message_text: str | None,
    posted_at: datetime | None,
    symbol: str | None,
    side: str | None,
    entry_text: str | None,
    now: datetime,
    capture: Callable[..., Any] | None = None,
) -> None:
    """Tell a person an entry was not executed because an outage delayed it.

    Carries what the ruling asks for -- the message text, the group, when it
    was posted and the price range it named -- so the person can decide
    without opening a database session.
    """

    if capture is None:
        from telegram_kol_research.runtime_incident_adapters import (
            capture_provider_outage_entry_not_replayed,
            capture_runtime_incident_best_effort,
        )

        def capture(**kwargs: Any) -> None:
            capture_runtime_incident_best_effort(
                capture_provider_outage_entry_not_replayed,
                session_factory,
                **kwargs,
            )

    capture(
        raw_message_id=int(raw_message_id),
        chat_id=int(chat_id),
        message_text=message_text or "",
        posted_at=posted_at,
        entry_summary=" ".join(
            part for part in (symbol or "", side or "", entry_text or "") if part
        ),
        occurred_at=now,
    )
