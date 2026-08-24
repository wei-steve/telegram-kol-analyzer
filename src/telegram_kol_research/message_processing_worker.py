"""Durable post-persist message processing and queue worker primitives."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import or_, text

from telegram_kol_research.models import RawMessage, utc_now
from telegram_kol_research.models import MessageProcessingJob
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.system_operator_bot import system_operator_bot_enabled
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)
DEFAULT_CLAIM_STALE_AFTER = timedelta(minutes=5)
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 15.0
DEFAULT_RETRY_MAX_SECONDS = 300.0
_EMPTY_INPUT_AUTHORITATIVE_FAILURE_REASON = (
    "message has no readable text or image"
)
_TERMINAL_EMPTY_INPUT_QUEUE_REASON = (
    "terminal_authoritative_failure:empty_input"
)


@dataclass(frozen=True, slots=True)
class MessageProcessingClaim:
    job_id: int
    raw_message_id: int
    chat_id: int
    attempt_count: int
    claim_token: str
    source_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MessageProcessingWorkerResult:
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    failed: int = 0
    expired: int = 0


class MessageProcessingActivity:
    """Event-loop-owned counters for bounded durable chat lanes."""

    def __init__(self) -> None:
        self._configured_limit: int | None = None
        self._active_by_chat: dict[int, int] = {}
        self._active = 0
        self._peak_since_limit_change = 0
        self._last_refill_claimed = 0
        self._total_started = 0
        self._limit_applied_at: datetime | None = None

    def apply_limit(self, limit: int, *, applied_at: datetime) -> None:
        normalized = int(limit)
        if normalized == self._configured_limit:
            return
        self._configured_limit = normalized
        self._peak_since_limit_change = self._active
        self._limit_applied_at = applied_at

    def enter(self, chat_id: int) -> None:
        normalized_chat_id = int(chat_id)
        self._active_by_chat[normalized_chat_id] = (
            self._active_by_chat.get(normalized_chat_id, 0) + 1
        )
        self._active += 1
        self._total_started += 1
        self._peak_since_limit_change = max(
            self._peak_since_limit_change,
            self._active,
        )

    def leave(self, chat_id: int) -> None:
        normalized_chat_id = int(chat_id)
        chat_count = self._active_by_chat.get(normalized_chat_id, 0)
        if chat_count <= 0 or self._active <= 0:
            raise RuntimeError("message processing activity counter underflow")
        if chat_count == 1:
            del self._active_by_chat[normalized_chat_id]
        else:
            self._active_by_chat[normalized_chat_id] = chat_count - 1
        self._active -= 1

    def note_refill(self, claimed: int) -> None:
        self._last_refill_claimed = max(0, int(claimed))

    def snapshot(self) -> dict[str, Any]:
        return {
            "configured_max_parallel_chats": self._configured_limit,
            "active_chat_lanes": self._active,
            "peak_active_chat_lanes_since_limit_change": (
                self._peak_since_limit_change
            ),
            "last_refill_claimed": self._last_refill_claimed,
            "total_started": self._total_started,
            "limit_applied_at": (
                self._limit_applied_at.isoformat()
                if self._limit_applied_at is not None
                else None
            ),
        }


class AuthoritativeProcessingFailed(RuntimeError):
    """A returned authoritative failure that the durable queue must retry."""


class TerminalAuthoritativeProcessingFailed(RuntimeError):
    """A returned authoritative failure that is fully handled without retry."""

    queue_reason = _TERMINAL_EMPTY_INPUT_QUEUE_REASON


def _is_terminal_empty_input_authoritative_failure(
    processing_result: Any,
) -> bool:
    assessment = getattr(processing_result, "assessment", None)
    if getattr(assessment, "agreement_status", None) != "authoritative_failed":
        return False
    mimo = getattr(assessment, "mimo", None)
    error_message = getattr(mimo, "error_message", None)
    return (
        isinstance(error_message, str)
        and error_message.strip() == _EMPTY_INPUT_AUTHORITATIVE_FAILURE_REASON
    )


async def process_message_job(
    session_factory,
    *,
    raw_message_id: int,
    chat_title: str | None = None,
    record: NormalizedMessageRecord | None = None,
    recognition_enabled: bool = True,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    strategy_alert_processor: Callable[..., Any] | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    context_resolution_scheduler: Callable[..., int] | None = None,
    context_resolution_worker: Callable[[], Any] | None = None,
    authoritative_failure_retry_delay_seconds: float = 60.0,
    system_operator_bot_config: Any | None = None,
    notification_bot_config: Any | None = None,
    system_operator_conflict_sender: Callable[..., Any] | None = None,
    retry_authoritative_failure: bool = True,
) -> Any | None:
    """Run the former inline post-persist chain for one durable raw row.

    The function deliberately receives the existing processors as dependencies:
    it changes only the invocation boundary, not recognition, policy resolution,
    execution, alerting, or notification semantics.
    """

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError(f"raw message not found: {int(raw_message_id)}")
        # Detach the row before any blocking processor runs. All accessed fields
        # are scalar and loaded above.
        session.expunge(raw_message)

    event_time: datetime = (
        raw_message.edit_date or raw_message.posted_at or utc_now()
    )
    if context_resolution_scheduler is not None:
        await asyncio.to_thread(
            context_resolution_scheduler,
            event_type="next_same_chat_message",
            chat_id=int(raw_message.chat_id),
            occurred_at=event_time,
        )
        if raw_message.edit_date is not None:
            await asyncio.to_thread(
                context_resolution_scheduler,
                event_type="message_edited",
                raw_message_id=int(raw_message.id),
                occurred_at=event_time,
            )

    processing_result = None
    recognition_result = None
    if recognition_enabled:
        if authoritative_processor is not None:
            processing_result = await asyncio.to_thread(
                authoritative_processor,
                int(raw_message.id),
            )
            recognition_result = processing_result.recognition

            # Import lazily to keep the extracted worker independent of the
            # Telethon listener at module import time.
            from telegram_kol_research.telegram_live_listener import (
                _build_authoritative_notification_payload,
                _deliver_authoritative_instruction_summary,
                _handle_authoritative_failure_notification,
            )

            if (
                processing_result.assessment.agreement_status
                == "authoritative_failed"
                and system_operator_bot_enabled(system_operator_bot_config)
            ):
                conflict_payload = _build_authoritative_notification_payload(
                    raw_message=raw_message,
                    chat_title=chat_title,
                    processing_result=processing_result,
                )
                _handle_authoritative_failure_notification(
                    session_factory=session_factory,
                    raw_message_id=int(raw_message.id),
                    sender=system_operator_conflict_sender,
                    config=system_operator_bot_config,
                    payload=conflict_payload,
                    retry_processor=(
                        authoritative_processor
                        if retry_authoritative_failure
                        else None
                    ),
                    retry_delay_seconds=(
                        authoritative_failure_retry_delay_seconds
                    ),
                )
            await _deliver_authoritative_instruction_summary(
                processing_result=processing_result,
                session_factory=session_factory,
                raw_message_id=int(raw_message.id),
                chat_title=chat_title,
                notification_bot_config=notification_bot_config,
            )
            if context_resolution_scheduler is not None:
                await asyncio.to_thread(
                    context_resolution_scheduler,
                    event_type="evidence_version_changed",
                    raw_message_id=int(raw_message.id),
                    occurred_at=event_time,
                )
            if (
                processing_result.assessment.agreement_status
                == "authoritative_failed"
                and not retry_authoritative_failure
            ):
                if _is_terminal_empty_input_authoritative_failure(
                    processing_result
                ):
                    raise TerminalAuthoritativeProcessingFailed(
                        _TERMINAL_EMPTY_INPUT_QUEUE_REASON
                    )
                raise AuthoritativeProcessingFailed(
                    "authoritative processor returned authoritative_failed"
                )
        else:
            import logging

            logging.getLogger(__name__).error(
                "recognition authority unavailable raw_message_id=%s "
                "reason=authoritative_processor_required",
                raw_message.id,
            )

    if (
        strategy_alert_processor is not None
        and strategy_alert_config is not None
        and chat_title
        and (
            strategy_alert_enabled_for_title is None
            or strategy_alert_enabled_for_title(chat_title)
        )
    ):
        if record is None:
            record = _record_from_raw_message(raw_message)
        await strategy_alert_processor(
            session_factory=session_factory,
            record=record,
            chat_title=chat_title,
            config=strategy_alert_config,
            recognition_result=recognition_result,
        )
    if context_resolution_worker is not None:
        await asyncio.to_thread(context_resolution_worker)
    return processing_result


def _record_from_raw_message(raw_message: RawMessage) -> NormalizedMessageRecord:
    """Rebuild the strategy-alert input from the durable raw row."""

    return NormalizedMessageRecord(
        chat_id=int(raw_message.chat_id),
        message_id=int(raw_message.message_id),
        sender_id=raw_message.sender_id,
        sender_name=raw_message.sender_name,
        text=raw_message.text,
        reply_to_message_id=raw_message.reply_to_message_id,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=bool(raw_message.archived_target_group),
        posted_at=raw_message.posted_at,
        edit_date=raw_message.edit_date,
        raw_payload=raw_message.raw_payload or "{}",
    )


def claim_message_processing_jobs(
    session_factory,
    *,
    claimed_at: datetime | None = None,
    stale_after: timedelta = DEFAULT_CLAIM_STALE_AFTER,
    limit: int = 20,
) -> list[MessageProcessingClaim]:
    """Atomically claim the oldest due job in each available chat lane."""

    claim_time = _naive_utc(claimed_at or utc_now())
    stale_before = claim_time - stale_after
    claim_limit = max(0, int(limit))
    if claim_limit == 0:
        return []

    claims: list[MessageProcessingClaim] = []
    with session_factory() as session:
        # SQLite is the production store. BEGIN IMMEDIATE makes selection plus
        # conditional updates one short cross-process claim transaction.
        session.execute(text("BEGIN IMMEDIATE"))
        rows = (
            session.query(MessageProcessingJob)
            .filter(
                MessageProcessingJob.status.in_(("pending", "claimed")),
                MessageProcessingJob.shadow.is_(False),
            )
            .order_by(
                MessageProcessingJob.chat_id.asc(),
                MessageProcessingJob.raw_message_id.asc(),
            )
            .all()
        )
        selected_chats: set[int] = set()
        for row in rows:
            chat_id = int(row.chat_id)
            if chat_id in selected_chats:
                continue
            # The oldest non-terminal row owns the lane even when its retry is
            # not due or its lease is still live. Later rows cannot overtake it.
            if row.status == "pending":
                if (
                    row.next_attempt_at is not None
                    and row.next_attempt_at > claim_time
                ):
                    selected_chats.add(chat_id)
                    continue
            elif row.claimed_at is None or row.claimed_at > stale_before:
                selected_chats.add(chat_id)
                continue

            token = uuid4().hex
            conditions = [
                MessageProcessingJob.id == int(row.id),
                MessageProcessingJob.shadow.is_(False),
            ]
            if row.status == "pending":
                conditions.extend(
                    [
                        MessageProcessingJob.status == "pending",
                        or_(
                            MessageProcessingJob.next_attempt_at.is_(None),
                            MessageProcessingJob.next_attempt_at <= claim_time,
                        ),
                    ]
                )
            else:
                conditions.extend(
                    [
                        MessageProcessingJob.status == "claimed",
                        MessageProcessingJob.claimed_at <= stale_before,
                    ]
                )
            updated = (
                session.query(MessageProcessingJob)
                .filter(*conditions)
                .update(
                    {
                        MessageProcessingJob.status: "claimed",
                        MessageProcessingJob.claim_token: token,
                        MessageProcessingJob.claimed_at: claim_time,
                        MessageProcessingJob.next_attempt_at: None,
                        MessageProcessingJob.last_reason: (
                            "stale_claim_reclaimed"
                            if row.status == "claimed"
                            else "worker_claimed"
                        ),
                    },
                    synchronize_session=False,
                )
            )
            selected_chats.add(chat_id)
            if updated == 1:
                claims.append(
                    MessageProcessingClaim(
                        job_id=int(row.id),
                        raw_message_id=int(row.raw_message_id),
                        chat_id=chat_id,
                        attempt_count=int(row.attempt_count or 0),
                        claim_token=token,
                        source_reason=row.last_reason,
                    )
                )
                if len(claims) >= claim_limit:
                    break
        session.commit()
    return claims


def _settle_message_processing_job(
    session_factory,
    *,
    claim: MessageProcessingClaim,
    status: str,
    reason: str,
    completed_at: datetime,
) -> bool:
    with session_factory() as session:
        updated = (
            session.query(MessageProcessingJob)
            .filter(
                MessageProcessingJob.id == claim.job_id,
                MessageProcessingJob.status == "claimed",
                MessageProcessingJob.claim_token == claim.claim_token,
            )
            .update(
                {
                    MessageProcessingJob.status: status,
                    MessageProcessingJob.claim_token: None,
                    MessageProcessingJob.claimed_at: None,
                    MessageProcessingJob.next_attempt_at: None,
                    MessageProcessingJob.last_reason: reason[:128],
                    MessageProcessingJob.completed_at: _naive_utc(completed_at),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return updated == 1


def _defer_or_fail_message_processing_job(
    session_factory,
    *,
    claim: MessageProcessingClaim,
    error: BaseException,
    failed_at: datetime,
    max_attempts: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
) -> tuple[str, str]:
    attempt_count = int(claim.attempt_count) + 1
    terminal = attempt_count >= max(1, int(max_attempts))
    reason = f"processing_error:{type(error).__name__}"
    next_attempt_at = None
    status = "failed" if terminal else "pending"
    if not terminal:
        delay_seconds = min(
            max(0.0, float(retry_max_seconds)),
            max(0.0, float(retry_base_seconds)) * (2 ** (attempt_count - 1)),
        )
        next_attempt_at = _naive_utc(failed_at) + timedelta(seconds=delay_seconds)
    with session_factory() as session:
        updated = (
            session.query(MessageProcessingJob)
            .filter(
                MessageProcessingJob.id == claim.job_id,
                MessageProcessingJob.status == "claimed",
                MessageProcessingJob.claim_token == claim.claim_token,
            )
            .update(
                {
                    MessageProcessingJob.status: status,
                    MessageProcessingJob.attempt_count: attempt_count,
                    MessageProcessingJob.next_attempt_at: next_attempt_at,
                    MessageProcessingJob.claim_token: None,
                    MessageProcessingJob.claimed_at: None,
                    MessageProcessingJob.last_reason: reason,
                    MessageProcessingJob.completed_at: (
                        _naive_utc(failed_at) if terminal else None
                    ),
                },
                synchronize_session=False,
            )
        )
        session.commit()
    return (status if updated == 1 else "stale_claim"), reason


async def run_message_processing_worker_tick(
    session_factory,
    *,
    now: datetime | None = None,
    limit: int = 20,
    stale_after: timedelta = DEFAULT_CLAIM_STALE_AFTER,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    job_processor: Callable[..., Awaitable[Any]] = process_message_job,
    process_kwargs: dict[str, Any] | None = None,
    chat_title_provider: Callable[[int], str | None] | None = None,
    loop_lag_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    terminal_failure_notifier: Callable[..., Any] | None = None,
    activity: MessageProcessingActivity | None = None,
) -> MessageProcessingWorkerResult:
    """Claim one ordered job per chat and process chat lanes concurrently."""

    tick_time = now or utc_now()
    claims = await asyncio.to_thread(
        claim_message_processing_jobs,
        session_factory,
        claimed_at=tick_time,
        stale_after=stale_after,
        limit=limit,
    )
    counts = {"succeeded": 0, "retried": 0, "failed": 0, "expired": 0}

    async def _run_claim_body(claim: MessageProcessingClaim) -> None:
        try:
            expiry = await asyncio.to_thread(
                _classify_claim_expiry,
                session_factory,
                claim=claim,
                now=tick_time,
                loop_lag_snapshot_provider=loop_lag_snapshot_provider,
            )
            if expiry is not None:
                classification, raw_message = expiry
                from telegram_kol_research.telegram_live_listener import (
                    _record_expired_authoritative_recovery_gap,
                )

                await asyncio.to_thread(
                    _record_expired_authoritative_recovery_gap,
                    session_factory,
                    raw_message=raw_message,
                    classification=classification,
                )
                settled = await asyncio.to_thread(
                    _settle_message_processing_job,
                    session_factory,
                    claim=claim,
                    status="expired",
                    reason=classification,
                    completed_at=tick_time,
                )
                if settled:
                    counts["expired"] += 1
                return

            kwargs = dict(process_kwargs or {})
            source_reason = str(claim.source_reason or "")
            if source_reason.startswith("recovery_"):
                kwargs["strategy_alert_config"] = None
                kwargs["context_resolution_scheduler"] = None
                kwargs["context_resolution_worker"] = None
            elif source_reason.startswith("history_reconcile_"):
                kwargs["context_resolution_scheduler"] = None
                kwargs["context_resolution_worker"] = None
            if chat_title_provider is not None:
                kwargs["chat_title"] = chat_title_provider(claim.chat_id)
            await job_processor(
                session_factory,
                raw_message_id=claim.raw_message_id,
                retry_authoritative_failure=False,
                **kwargs,
            )
            if source_reason.startswith("history_reconcile_"):
                from telegram_kol_research.trade_merge import (
                    persist_trade_ideas_from_candidates,
                )

                await asyncio.to_thread(
                    persist_trade_ideas_from_candidates,
                    session_factory,
                )
        except asyncio.CancelledError:
            # Leave the durable claim for lease-based restart recovery.
            raise
        except TerminalAuthoritativeProcessingFailed as exc:
            settled = await asyncio.to_thread(
                _settle_message_processing_job,
                session_factory,
                claim=claim,
                status="succeeded",
                reason=exc.queue_reason,
                completed_at=tick_time,
            )
            if settled:
                counts["succeeded"] += 1
            return
        except BaseException as exc:
            status, reason = await asyncio.to_thread(
                _defer_or_fail_message_processing_job,
                session_factory,
                claim=claim,
                error=exc,
                failed_at=tick_time,
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
            )
            if status == "pending":
                counts["retried"] += 1
            elif status == "failed":
                counts["failed"] += 1
                if terminal_failure_notifier is not None:
                    try:
                        notification = terminal_failure_notifier(claim, reason)
                        if inspect.isawaitable(notification):
                            await notification
                    except Exception:
                        logger.exception(
                            "message processing terminal failure notification failed "
                            "raw_message_id=%s",
                            claim.raw_message_id,
                        )
            logger.exception(
                "message processing job failed raw_message_id=%s status=%s",
                claim.raw_message_id,
                status,
            )
            return
        settled = await asyncio.to_thread(
            _settle_message_processing_job,
            session_factory,
            claim=claim,
            status="succeeded",
            reason="worker_completed",
            completed_at=tick_time,
        )
        if settled:
            counts["succeeded"] += 1

    async def run_claim(claim: MessageProcessingClaim) -> None:
        if activity is not None:
            activity.enter(claim.chat_id)
        try:
            await _run_claim_body(claim)
        finally:
            if activity is not None:
                activity.leave(claim.chat_id)

    if claims:
        await asyncio.gather(*(run_claim(claim) for claim in claims))
    return MessageProcessingWorkerResult(claimed=len(claims), **counts)


def _load_trading_settings_with_observed_at(session_factory):
    """Load the cap and capture its observation time on the same worker thread."""

    return load_trading_settings(session_factory), utc_now()


async def run_message_processing_worker_loop(
    session_factory,
    *,
    interval_seconds: float = 0.5,
    activity: MessageProcessingActivity | None = None,
    **tick_kwargs,
) -> None:
    """Consume queue jobs only while the dynamic pipeline mode is ``queue``."""

    lane_activity = activity or MessageProcessingActivity()
    in_flight: set[asyncio.Task[MessageProcessingWorkerResult]] = set()
    interval = max(0.01, float(interval_seconds))
    try:
        while True:
            settings, observed_at = await asyncio.to_thread(
                _load_trading_settings_with_observed_at,
                session_factory,
            )
            cap = settings.message_processing_max_parallel_chats
            lane_activity.apply_limit(cap, applied_at=observed_at)

            if settings.message_pipeline_mode != "queue":
                if in_flight:
                    await asyncio.gather(*in_flight)
                return

            while len(in_flight) < cap:
                in_flight.add(
                    asyncio.create_task(
                        run_message_processing_worker_tick(
                            session_factory,
                            limit=1,
                            activity=lane_activity,
                            **tick_kwargs,
                        )
                    )
                )

            done, pending = await asyncio.wait(
                in_flight,
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            in_flight = set(pending)
            claimed = 0
            for task in done:
                claimed += task.result().claimed
            lane_activity.note_refill(claimed)
            if done and claimed == 0:
                await asyncio.sleep(interval)
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)


def _classify_claim_expiry(
    session_factory,
    *,
    claim: MessageProcessingClaim,
    now: datetime,
    loop_lag_snapshot_provider: Callable[[], dict[str, Any]] | None,
) -> tuple[str, RawMessage] | None:
    with session_factory() as session:
        raw_message = session.get(RawMessage, claim.raw_message_id)
        if raw_message is None:
            raise LookupError(f"raw message not found: {claim.raw_message_id}")
        session.expunge(raw_message)
    posted_at = raw_message.posted_at
    if posted_at is None:
        return None
    settings = load_trading_settings(session_factory)
    maximum_age = timedelta(
        minutes=float(settings.authoritative_gap_recovery_max_age_minutes)
    )
    if _aware_utc(posted_at) > _aware_utc(now) - maximum_age:
        return None
    from telegram_kol_research.telegram_live_listener import (
        _classify_expired_authoritative_recovery_gap,
    )

    snapshot = (
        loop_lag_snapshot_provider()
        if loop_lag_snapshot_provider is not None
        else None
    )
    return (
        _classify_expired_authoritative_recovery_gap(
            raw_message=raw_message,
            now=now,
            loop_lag_snapshot=snapshot,
        ),
        raw_message,
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
