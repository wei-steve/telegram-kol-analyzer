"""Realtime Telegram listener helpers for web live updates."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_
from sqlalchemy import tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig, load_ai_recognition_config
from telegram_kol_research.contextual_message_window import (
    fetch_missing_reply_target,
)
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.models import (
    MediaAsset,
    MessageProcessingJob,
    MessageRecognition,
    RawMessage,
    RecognitionDecision,
    utc_now,
)
from telegram_kol_research.raw_ingest import normalize_message_payload, persist_normalized_messages
from telegram_kol_research.raw_ingest import repair_history_checkpoints
from telegram_kol_research.runtime_incident_adapters import (
    capture_notification_failure,
    capture_runtime_incident_best_effort,
)
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    _save_terminal_authoritative_decision_in_session,
    _update_recognition_execution_outcome_in_session,
    claim_authoritative_failure_notification,
    update_recognition_execution_outcome,
)
from telegram_kol_research.source_message_deletion import record_source_message_deleted
from telegram_kol_research.system_operator_bot import (
    deliver_message_instruction_summary_notification,
    deliver_pending_message_instruction_summaries,
    system_operator_bot_enabled,
)
from telegram_kol_research.telegram_client import (
    _download_media_if_present,
    _format_sender_name,
    discover_dialogs,
    fetch_dialog_messages,
    filter_target_dialogs,
    is_usable_image_file,
    maybe_await,
)
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)
AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS = 60.0
DEFAULT_AUTHORITATIVE_GAP_RECOVERY_INTERVAL_SECONDS = 20.0
DEFAULT_STALL_EXPIRY_NOTIFICATION_MIN_INTERVAL_SECONDS = 300.0

_CRYPTO_FAILURE_TERMS = (
    "BTC", "ETH", "SOL", "DOGE", "BNB", "XRP", "ADA", "SUI", "ZEC",
    "HYPE", "LINK", "AVAX", "BCH", "LTC", "TON", "TRX", "DOT", "OP",
    "ARB", "PEPE", "WIF", "USDT", "USDC", "比特币", "大饼", "以太",
    "币圈", "合约",
)
_POSITION_FAILURE_TERMS = (
    "止盈", "止损", "平仓", "全平", "出局", "离场", "保本", "保护",
    "减仓", "仓位", "持仓", "多单", "空单", "开多", "开空", "做多",
    "做空", "挂单", "挂到", "挂在",
)
_EXTERNAL_MARKET_TERMS = (
    "美光", "MU", "INTEL", "DELL", "NVDA", "英伟达", "美股", "纳斯达克",
    "A股", "三星", "海力士",
)


async def _deliver_authoritative_instruction_summary(
    *,
    processing_result: Any,
    session_factory,
    raw_message_id: int,
    chat_title: str | None,
    notification_bot_config: Any | None,
    claimed_at=None,
) -> bool:
    automation = getattr(processing_result, "automation", None)
    if (
        not isinstance(automation, dict)
        or not isinstance(automation.get("items"), list)
        or not automation["items"]
        or not system_operator_bot_enabled(notification_bot_config)
    ):
        return False
    return await deliver_message_instruction_summary_notification(
        session_factory,
        config=notification_bot_config,
        raw_message_id=raw_message_id,
        chat_title=chat_title,
        claimed_at=claimed_at,
    )


def _enqueue_processing_jobs(
    session_factory,
    *,
    message_keys: list[tuple[int, int]] | None = None,
    raw_message_ids: list[int] | None = None,
    last_reason: str,
) -> list[int]:
    """Idempotently create queue jobs for the given messages."""

    with session_factory() as session:
        query = session.query(RawMessage.id, RawMessage.chat_id)
        if raw_message_ids is not None:
            normalized_ids = [int(raw_message_id) for raw_message_id in raw_message_ids]
            rows = query.filter(RawMessage.id.in_(normalized_ids)).all()
        else:
            normalized_keys = [
                (int(chat_id), int(message_id))
                for chat_id, message_id in (message_keys or [])
            ]
            rows = (
                query.filter(
                    tuple_(RawMessage.chat_id, RawMessage.message_id).in_(
                        normalized_keys
                    )
                ).all()
                if normalized_keys
                else []
            )
        if not rows:
            return []
        insert_statement = sqlite_insert(MessageProcessingJob).values(
            [
                {
                    "raw_message_id": int(row.id),
                    "chat_id": int(row.chat_id),
                    "status": "pending",
                    "attempt_count": 0,
                    "last_reason": last_reason,
                    "enqueued_at": utc_now(),
                    "shadow": False,
                }
                for row in rows
            ]
        )
        # Rows left behind by the retired shadow pipeline are adopted only once
        # they are terminal, or once a pending one has sat unclaimed long enough
        # to prove no consumer owns it.
        statement = insert_statement.on_conflict_do_update(
            index_elements=["raw_message_id"],
            set_={
                "chat_id": insert_statement.excluded.chat_id,
                "status": "pending",
                "attempt_count": 0,
                "next_attempt_at": None,
                "claim_token": None,
                "claimed_at": None,
                "last_reason": last_reason,
                "enqueued_at": insert_statement.excluded.enqueued_at,
                "completed_at": None,
                "shadow": False,
            },
            where=and_(
                MessageProcessingJob.shadow.is_(True),
                or_(
                    MessageProcessingJob.status.in_(
                        ("succeeded", "failed", "expired")
                    ),
                    and_(
                        MessageProcessingJob.status == "pending",
                        MessageProcessingJob.enqueued_at
                        <= utc_now() - timedelta(minutes=5),
                    ),
                ),
            ),
        )
        session.execute(statement)
        session.commit()
        return [int(row.id) for row in rows]


async def _try_enqueue_processing_jobs(
    session_factory,
    **kwargs,
) -> list[int]:
    try:
        return await asyncio.to_thread(
            _enqueue_processing_jobs,
            session_factory,
            **kwargs,
        )
    except Exception:
        logger.exception("message processing enqueue failed")
        return []


async def _persist_live_message_event_inline(
    *,
    event: Any,
    session_factory,
    broker,
    media_root: str | Path = "data/media",
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    reply_evidence_processor: Callable[[int], Any] | None = None,
    context_resolution_scheduler: Callable[..., int] | None = None,
    enqueue_hook: (
        Callable[[list[tuple[int, int]]], Awaitable[None]] | None
    ) = None,
) -> dict[str, int]:
    """Normalize and persist one live Telegram event into the existing raw ingest flow.

    When AI recognition config or a config path is provided, the freshly
    persisted message is immediately submitted for AI strategy recognition
    (text -> text AI, image -> GLM-OCR -> text AI, video -> skipped). When
    a path is provided it is loaded per message so prompt edits take effect
    without restarting the listener.
    """

    message = getattr(event, "message", None)
    if message is None:
        return {"inserted_messages": 0, "inserted_media_assets": 0, "processed_records": 0}

    sender = None
    get_sender = getattr(message, "get_sender", None)
    if callable(get_sender):
        sender = await get_sender()

    media_path = await _download_media_if_present(
        getattr(event, "client", None),
        dialog_id=getattr(event, "chat_id"),
        message=message,
        media_root=Path(media_root),
    ) if getattr(event, "client", None) is not None else None

    payload = {
        "chat_id": getattr(event, "chat_id"),
        "message_id": getattr(message, "id", None),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _format_sender_name(sender),
        "text": getattr(message, "message", None),
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "posted_at": getattr(message, "date", None),
        "edit_date": getattr(message, "edit_date", None),
        "media": {
            "kind": type(getattr(message, "media", None)).__name__.lower(),
            "path": media_path,
        }
        if getattr(message, "media", None) is not None
        else None,
    }
    record = normalize_message_payload(payload, archived_target_group=True)
    stats = persist_normalized_messages(
        session_factory,
        [record],
        sync_kind="live",
        broker=broker,
    )
    inserted_keys = stats.get("inserted_message_keys") or []
    event_time = getattr(message, "edit_date", None) or getattr(message, "date", None) or utc_now()
    live_ai_config = ai_recognition_config
    if ai_recognition_config_path is not None:
        live_ai_config = load_ai_recognition_config(ai_recognition_config_path)
    if inserted_keys and live_ai_config is not None:
        chat_id, message_id = inserted_keys[0]
        reply_to_message_id = getattr(message, "reply_to_msg_id", None)
        telegram_client = getattr(event, "client", None)
        if (
            authoritative_processor is not None
            and telegram_client is not None
            and reply_to_message_id is not None
        ):
            try:
                reply_available = await fetch_missing_reply_target(
                    telegram_client,
                    session_factory=session_factory,
                    chat_id=chat_id,
                    message_id=int(reply_to_message_id),
                    media_root=media_root,
                    broker=broker,
                )
                if reply_available and reply_evidence_processor is not None:
                    with session_factory() as session:
                        reply_raw_id = (
                            session.query(RawMessage.id)
                            .filter(
                                RawMessage.chat_id == int(chat_id),
                                RawMessage.message_id == int(reply_to_message_id),
                            )
                            .scalar()
                        )
                    if reply_raw_id is not None:
                        await asyncio.to_thread(
                            reply_evidence_processor,
                            int(reply_raw_id),
                        )
                if reply_available and context_resolution_scheduler is not None:
                    await asyncio.to_thread(
                        context_resolution_scheduler,
                        event_type="reply_target_available",
                        chat_id=int(chat_id),
                        occurred_at=event_time,
                    )
            except Exception:
                logger.exception(
                    "failed to recover Telegram reply target chat_id=%s message_id=%s",
                    chat_id,
                    reply_to_message_id,
                )
    if enqueue_hook is not None:
        await enqueue_hook(inserted_keys)
    return stats


@wraps(_persist_live_message_event_inline)
async def persist_live_message_event(*args, **kwargs) -> dict[str, int]:
    """Persist the event, then hand the message to the queue consumer."""

    session_factory = kwargs.get("session_factory")

    async def enqueue_after_persist(
        inserted_keys: list[tuple[int, int]],
    ) -> None:
        await _try_enqueue_processing_jobs(
            session_factory,
            message_keys=inserted_keys,
            last_reason="queue_enqueued",
        )

    kwargs["enqueue_hook"] = enqueue_after_persist
    return await _persist_live_message_event_inline(*args, **kwargs)


def _build_authoritative_notification_payload(
    *,
    raw_message: RawMessage,
    chat_title: str | None,
    processing_result: Any,
) -> dict[str, Any] | None:
    assessment = processing_result.assessment
    if assessment.agreement_status != "authoritative_failed":
        return None
    mimo_payload = assessment.mimo.payload if isinstance(assessment.mimo.payload, dict) else {}
    deepseek_payload = (
        assessment.deepseek_payload
        if isinstance(assessment.deepseek_payload, dict)
        else {}
    )
    return {
        "chat_title": chat_title,
        "chat_id": raw_message.chat_id,
        "message_id": raw_message.message_id,
        "posted_at": raw_message.posted_at,
        "text": raw_message.text,
        "agreement_status": assessment.agreement_status,
        "differences": assessment.differences,
        "deepseek": {
            "status": deepseek_payload.get("recognition_result") or "-",
            "kind": "auxiliary",
            "reason": deepseek_payload.get("reason") or "-",
        },
        "mimo": {
            "status": assessment.mimo.status,
            "kind": "authoritative",
            "reason": mimo_payload.get("reason") or assessment.mimo.error_message or "-",
        },
        "automation": processing_result.automation,
    }


def _schedule_authoritative_notification(
    *,
    session_factory,
    raw_message_id: int,
    sender,
    config,
    payload: dict[str, Any],
) -> asyncio.Task[None] | None:
    automation_status = str(
        payload.get("automation", {}).get("status") or "unknown"
    )
    automation_reason = payload.get("automation", {}).get("reason")
    if not claim_authoritative_failure_notification(
        session_factory,
        raw_message_id=raw_message_id,
        automation_status=automation_status,
        automation_reason=automation_reason,
    ):
        return None

    async def send_in_background() -> None:
        try:
            await sender(config=config, payload=payload)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.exception("authoritative recognition notification failed")
            await asyncio.to_thread(
                update_recognition_execution_outcome,
                session_factory,
                raw_message_id=raw_message_id,
                automation_status=automation_status,
                automation_reason=automation_reason,
                notification_status="failed",
                notification_error=type(exc).__name__,
            )
            await asyncio.to_thread(
                capture_runtime_incident_best_effort,
                capture_notification_failure,
                session_factory,
                source_kind="authoritative_notification",
                source_record_id=f"raw_message_{int(raw_message_id)}",
                error_type=type(exc).__name__,
                occurred_at=utc_now(),
            )
        else:
            await asyncio.to_thread(
                update_recognition_execution_outcome,
                session_factory,
                raw_message_id=raw_message_id,
                automation_status=automation_status,
                automation_reason=automation_reason,
                notification_status="sent",
            )

    return asyncio.create_task(send_in_background())


def _handle_authoritative_failure_notification(
    *,
    session_factory,
    raw_message_id: int,
    sender,
    config,
    payload: dict[str, Any] | None,
    retry_processor: Callable[[int], Any] | None = None,
    retry_delay_seconds: float = AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS,
) -> bool:
    if payload is None:
        return False
    notification_status = _classify_authoritative_failure_notification(payload)
    if notification_status.startswith("suppressed_"):
        update_recognition_execution_outcome(
            session_factory,
            raw_message_id=raw_message_id,
            automation_status=str(
                payload.get("automation", {}).get("status") or "unknown"
            ),
            automation_reason=payload.get("automation", {}).get("reason"),
            notification_status=notification_status,
        )
        return False
    _schedule_authoritative_notification(
        session_factory=session_factory,
        raw_message_id=raw_message_id,
        sender=sender,
        config=config,
        payload=payload,
    )
    if retry_processor is not None:
        _schedule_authoritative_failure_retry(
            raw_message_id=raw_message_id,
            retry_processor=retry_processor,
            retry_delay_seconds=retry_delay_seconds,
        )
    return True


def _schedule_authoritative_failure_retry(
    *,
    raw_message_id: int,
    retry_processor: Callable[[int], Any],
    retry_delay_seconds: float,
) -> None:
    async def retry_in_background() -> None:
        if retry_delay_seconds > 0:
            await asyncio.sleep(retry_delay_seconds)
        try:
            await asyncio.to_thread(retry_processor, raw_message_id)
        except Exception:  # pragma: no cover - defensive background path
            logger.exception("authoritative recognition delayed retry failed")

    asyncio.create_task(retry_in_background())


def _classify_authoritative_failure_notification(payload: dict[str, Any]) -> str:
    if str(payload.get("agreement_status") or "") != "authoritative_failed":
        return "scheduled"
    mimo = payload.get("mimo") if isinstance(payload.get("mimo"), dict) else {}
    if str(mimo.get("reason") or "").strip() == "message has no readable text or image":
        return "suppressed_empty_input"
    if _is_low_value_external_market_failure(str(payload.get("text") or "")):
        return "suppressed_low_value"
    return "scheduled"


def _is_low_value_external_market_failure(text: str) -> bool:
    normalized = text.upper()
    if not normalized.strip():
        return False
    has_external_marker = any(
        term.upper() in normalized for term in _EXTERNAL_MARKET_TERMS
    )
    if not has_external_marker:
        return False
    has_crypto_marker = any(term.upper() in normalized for term in _CRYPTO_FAILURE_TERMS)
    has_position_marker = any(term.upper() in normalized for term in _POSITION_FAILURE_TERMS)
    return not has_crypto_marker and not has_position_marker


EXPIRED_AFTER_SYSTEM_STALL = "expired_after_system_stall"
EXPIRED_STALE_INSTRUCTION = "expired_stale_instruction"


def _classify_expired_authoritative_recovery_gap(
    *,
    raw_message: RawMessage,
    now: datetime,
    loop_lag_snapshot: dict[str, Any] | None,
) -> str:
    """Classify why a message never got a decision before its window closed.

    ``expired_after_system_stall`` when a recorded event-loop stall overlaps
    the message's lifetime window (``posted_at`` .. ``now``) - the drop is a
    production incident, not a business decision, and is routed to the
    system operator. ``expired_stale_instruction`` otherwise, including
    whenever overlap cannot be established (no ``posted_at``, no snapshot, no
    recorded stall): the quieter classification is the fail-safe default when
    the evidence is missing, not just when it is negative.
    """

    posted_at = raw_message.posted_at
    if posted_at is None or not loop_lag_snapshot:
        return EXPIRED_STALE_INSTRUCTION
    last_stall_at_raw = loop_lag_snapshot.get("last_stall_at")
    if not last_stall_at_raw:
        return EXPIRED_STALE_INSTRUCTION
    try:
        last_stall_at = datetime.fromisoformat(str(last_stall_at_raw))
    except ValueError:
        return EXPIRED_STALE_INSTRUCTION
    if last_stall_at.tzinfo is None:
        last_stall_at = last_stall_at.replace(tzinfo=UTC)
    normalized_posted_at = (
        posted_at if posted_at.tzinfo is not None else posted_at.replace(tzinfo=UTC)
    )
    normalized_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if normalized_posted_at <= last_stall_at <= normalized_now:
        return EXPIRED_AFTER_SYSTEM_STALL
    return EXPIRED_STALE_INSTRUCTION


class StallExpiryNotificationRateLimiter:
    """Bound stall-induced expiry notifications to at most one per window.

    A single stall can expire a whole backlog of messages in one recovery
    pass. Without this, that burst would produce one Telegram message per
    expired message. This tracks only the monotonic time of the last
    notification actually sent and refuses another until
    ``min_interval_seconds`` has passed, no matter how many separate calls
    ask - "a bounded number of notifications, not one per message."
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = (
            DEFAULT_STALL_EXPIRY_NOTIFICATION_MIN_INTERVAL_SECONDS
        ),
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._last_notified_monotonic: float | None = None

    def should_notify(self) -> bool:
        now = self._monotonic()
        last = self._last_notified_monotonic
        if last is not None and now - last < self._min_interval_seconds:
            return False
        self._last_notified_monotonic = now
        return True


_STALL_EXPIRY_NOTIFICATION_RATE_LIMITER = StallExpiryNotificationRateLimiter()


def _record_expired_authoritative_recovery_gap(
    session_factory,
    *,
    raw_message: RawMessage,
    classification: str = EXPIRED_STALE_INSTRUCTION,
) -> None:
    """Persist a visible, non-executing terminal result for a stale gap.

    ``classification`` records *why* the gap was never recovered -
    :data:`EXPIRED_AFTER_SYSTEM_STALL` (a production incident) or
    :data:`EXPIRED_STALE_INSTRUCTION` (an ordinary timeout) - as an
    additional field. The persisted ``automation_reason`` stays exactly
    ``"authoritative_gap_recovery_expired"`` regardless of classification,
    because existing callers and tests already depend on that exact string;
    only the summary text and the new ``expiry_classification`` payload key
    vary.
    """

    with session_factory() as session:
        attached_raw_message = session.get(RawMessage, raw_message.id)
        if attached_raw_message is None:
            raise LookupError(f"Raw message not found: {raw_message.id}")
        _record_expired_authoritative_recovery_gap_in_session(
            session,
            raw_message=attached_raw_message,
            classification=classification,
        )
        session.commit()


def _record_expired_authoritative_recovery_gap_in_session(
    session,
    *,
    raw_message: RawMessage,
    classification: str = EXPIRED_STALE_INSTRUCTION,
) -> None:
    """Persist the existing expired audit in the caller's transaction."""

    reason = "authoritative_gap_recovery_expired"
    summary = (
        "消息未在窗口期内完成权威识别，原因为系统故障（事件循环阻塞）；"
        "为防止执行过期信号，未自动交易。"
        if classification == EXPIRED_AFTER_SYSTEM_STALL
        else "消息未在 15 分钟内完成权威识别；为防止执行过期信号，未自动交易。"
    )
    _save_terminal_authoritative_decision_in_session(
        session,
        RecognitionDecisionRecord(
            raw_message_id=raw_message.id,
            input_kind="recovery_guard",
            authoritative_model="recovery_guard",
            authoritative_status="识别失败",
            authoritative_payload={
                "reason": reason,
                "summary": summary,
                "expiry_classification": classification,
            },
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[reason],
            prompt_versions={},
        ),
    )
    recognition = (
        session.query(MessageRecognition)
        .filter(MessageRecognition.raw_message_id == raw_message.id)
        .one_or_none()
    )
    if recognition is None:
        session.add(
            MessageRecognition(
                raw_message_id=raw_message.id,
                status="识别失败",
                reason=summary,
                summary=summary,
                engine="recovery_guard",
            )
        )
    else:
        recognition.status = "识别失败"
        recognition.reason = summary
        recognition.summary = summary
        recognition.engine = "recovery_guard"
    _update_recognition_execution_outcome_in_session(
        session,
        raw_message_id=raw_message.id,
        automation_status="skipped",
        automation_reason=reason,
        notification_status="suppressed_expired_recovery",
    )


async def run_live_listener(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    reply_evidence_processor: Callable[[int], Any] | None = None,
    context_resolution_scheduler: Callable[..., int] | None = None,
    operation_lock: KeyedAsyncLockRegistry | None = None,
    source_deletion_recorder: Callable[..., Any] | None = None,
) -> None:
    """Attach Telethon source-message handlers and keep the client alive.

    ``operation_lock`` is the ``ingest`` process's
    :class:`~telegram_kol_research.keyed_async_locks.KeyedAsyncLockRegistry`.
    Every handler takes only its own chat's lock, so messages from one chat
    stay in arrival order while unrelated chats proceed concurrently. ``None``
    means no locking and is a test-only opt-out.
    """

    try:
        from telethon import events
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Telethon is not installed in the current environment. Install project dependencies first."
        ) from exc

    async def handle_new_message(event: Any) -> None:
        chat = await maybe_await(event.get_chat()) if hasattr(event, "get_chat") else None
        title = getattr(chat, "title", None)
        if target_titles and title not in target_titles:
            return
        raw_chat_id = getattr(event, "chat_id", None)
        chat_id = int(raw_chat_id) if raw_chat_id is not None else None
        persist_kwargs = {
            "event": event,
            "session_factory": session_factory,
            "broker": broker,
            "media_root": media_root,
            "ai_recognition_config": ai_recognition_config,
            "ai_recognition_config_path": ai_recognition_config_path,
            "authoritative_processor": authoritative_processor,
            "reply_evidence_processor": reply_evidence_processor,
            "context_resolution_scheduler": context_resolution_scheduler,
        }
        if operation_lock is None:
            await persist_live_message_event(**persist_kwargs)
        else:
            async with operation_lock.lock(chat_id):
                await persist_live_message_event(**persist_kwargs)

    async def handle_deleted_message(event: Any) -> None:
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            logger.warning(
                "Ignoring Telegram MessageDeleted event without exact chat_id"
            )
            return
        deleted_ids = [
            int(message_id)
            for message_id in (getattr(event, "deleted_ids", None) or [])
        ]
        if not deleted_ids:
            return
        recorder = source_deletion_recorder or record_source_message_deleted
        event_payload = {
            "chat_id": int(chat_id),
            "deleted_ids": deleted_ids,
        }

        async def persist_deletions() -> None:
            for message_id in deleted_ids:
                await maybe_await(
                    recorder(
                        session_factory,
                        chat_id=int(chat_id),
                        message_id=message_id,
                        deleted_at=utc_now(),
                        telegram_event=event_payload,
                    )
                )

        if operation_lock is None:
            await persist_deletions()
        else:
            async with operation_lock.lock(int(chat_id)):
                await persist_deletions()

    add_event_handler = getattr(client, "add_event_handler", None)
    if not callable(add_event_handler):
        raise RuntimeError("Telegram client does not support realtime event handlers")

    connect = getattr(client, "connect", None)
    if callable(connect):
        await maybe_await(connect())

    add_event_handler(handle_new_message, events.NewMessage())
    add_event_handler(handle_deleted_message, events.MessageDeleted())
    run_until_disconnected = getattr(client, "run_until_disconnected", None)
    if callable(run_until_disconnected):
        await maybe_await(run_until_disconnected())


def launch_live_listener_task(
    *,
    runner: Callable[..., Awaitable[None]],
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    reply_evidence_processor: Callable[[int], Any] | None = None,
    context_resolution_scheduler: Callable[..., int] | None = None,
    operation_lock: KeyedAsyncLockRegistry | None = None,
    source_deletion_recorder: Callable[..., Any] | None = None,
) -> asyncio.Task[None]:
    """Schedule the realtime listener in the current event loop."""

    kwargs = _filter_callable_kwargs(
        runner,
        {
            "client": client,
            "session_factory": session_factory,
            "broker": broker,
            "target_titles": target_titles,
            "media_root": media_root,
            "ai_recognition_config": ai_recognition_config,
            "ai_recognition_config_path": ai_recognition_config_path,
            "authoritative_processor": authoritative_processor,
            "reply_evidence_processor": reply_evidence_processor,
            "context_resolution_scheduler": context_resolution_scheduler,
            "operation_lock": operation_lock,
            "source_deletion_recorder": source_deletion_recorder,
        },
    )
    return asyncio.create_task(runner(**kwargs))


def _filter_callable_kwargs(callback: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(callback)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }


def _is_usable_downloaded_media_path(
    local_path: str | None,
    *,
    media_root: Path,
) -> bool:
    if not local_path:
        return False
    candidate = resolve_media_path(local_path, media_root=media_root)
    if candidate is None:
        return False
    return is_usable_image_file(candidate)


def _load_gap_recovery_candidates(
    session_factory,
    *,
    chat_titles_by_id: dict[int, str],
    now: datetime,
    message_limit: int,
) -> tuple[list[RawMessage], list[RawMessage]]:
    """Synchronous DB read: raw messages missing a decision, split by age.

    Reads the currently configured recovery window fresh from
    ``trading_settings`` on every call (Task 3: no restart needed to widen
    it after a stall) and returns ``(missing_decision_messages,
    expired_messages)``. Callers that must not block the event loop run this
    through ``asyncio.to_thread`` - it is a plain synchronous function
    specifically so that works.
    """

    if not chat_titles_by_id:
        return [], []
    settings = load_trading_settings(session_factory)
    recovery_cutoff = now - timedelta(
        minutes=settings.authoritative_gap_recovery_max_age_minutes
    )
    with session_factory() as session:
        missing_decision_query = (
            session.query(RawMessage)
            .outerjoin(
                RecognitionDecision,
                RecognitionDecision.raw_message_id == RawMessage.id,
            )
            .filter(RawMessage.chat_id.in_(chat_titles_by_id))
            .filter(RecognitionDecision.id.is_(None))
        )
        missing_decision_messages = (
            missing_decision_query.filter(RawMessage.posted_at >= recovery_cutoff)
            .order_by(
                RawMessage.posted_at.asc(),
                RawMessage.message_id.asc(),
                RawMessage.id.asc(),
            )
            .limit(message_limit)
            .all()
        )
        expired_messages = (
            missing_decision_query.filter(
                or_(
                    RawMessage.posted_at < recovery_cutoff,
                    RawMessage.posted_at.is_(None),
                )
            )
            .order_by(
                RawMessage.posted_at.asc(),
                RawMessage.message_id.asc(),
                RawMessage.id.asc(),
            )
            .limit(message_limit)
            .all()
        )
    return missing_decision_messages, expired_messages


async def recover_missing_authoritative_decisions(
    session_factory,
    *,
    chat_titles_by_id: dict[int, str],
    authoritative_processor: Callable[[int], Any] | None,
    message_limit: int = 50,
    now_provider: Callable[[], datetime] = utc_now,
) -> dict[str, int]:
    """Enqueue messages that still have no authoritative decision row.

    Performs **no Telegram network calls** of any kind - ``chat_titles_by_id``
    is supplied entirely by the caller, from local configuration or the
    database, never from ``discover_dialogs``. Its only caller is
    :func:`run_authoritative_gap_recovery_loop`, the ``worker`` role's fast
    DB-only compensator. The ``ingest`` role's periodic reconcile does not
    call it: reconcile compares against Telegram history to find messages that
    were never persisted, this compares against the database to find persisted
    messages that were never processed.

    This function makes no business decision itself. It selects the candidates
    and hands them to ``message_processing_jobs``; the ``worker`` role then
    recognises them, and classifies and records the ones that have expired.
    The returned counters therefore stay at zero and exist only so callers
    that report them keep their response shape.

    ``authoritative_processor`` is still read as the gate that says recognition
    authority is configured at all: with no processor there is nothing for the
    queue to run, so nothing is enqueued.
    """

    result: dict[str, int] = {
        "recovered_messages": 0,
        "expired_recovery_messages": 0,
        EXPIRED_AFTER_SYSTEM_STALL: 0,
        EXPIRED_STALE_INSTRUCTION: 0,
    }
    if authoritative_processor is None or not chat_titles_by_id:
        return result

    now = now_provider()
    missing_decision_messages, expired_messages = await asyncio.to_thread(
        _load_gap_recovery_candidates,
        session_factory,
        chat_titles_by_id=chat_titles_by_id,
        now=now,
        message_limit=message_limit,
    )
    await _try_enqueue_processing_jobs(
        session_factory,
        raw_message_ids=[
            int(raw_message.id)
            for raw_message in [
                *missing_decision_messages,
                *expired_messages,
            ]
        ],
        last_reason="recovery_enqueued",
    )
    return result


def _repair_reconcile_history_checkpoints(session_factory) -> None:
    repair_history_checkpoints(session_factory)


class _CancellableReconcileDatabaseUnit:
    def __init__(self, operation, /, *args, **kwargs) -> None:
        self._operation = operation
        self._args = args
        self._kwargs = kwargs
        self._state_lock = threading.Lock()
        self._cancelled_before_start = False
        self._started = False

    def cancel_if_queued(self) -> bool:
        with self._state_lock:
            if self._started:
                return False
            self._cancelled_before_start = True
            return True

    def __call__(self):
        with self._state_lock:
            if self._cancelled_before_start:
                return None
            self._started = True
        return self._operation(*self._args, **self._kwargs)


async def _run_reconcile_database_slice(operation, /, *args, **kwargs):
    database_unit = _CancellableReconcileDatabaseUnit(
        operation,
        *args,
        **kwargs,
    )
    processing_task = asyncio.create_task(asyncio.to_thread(database_unit))
    try:
        return await asyncio.shield(processing_task)
    except asyncio.CancelledError:
        if database_unit.cancel_if_queued():
            processing_task.cancel()
            try:
                await processing_task
            except asyncio.CancelledError:
                pass
            raise
        while not processing_task.done():
            try:
                await asyncio.shield(processing_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if processing_task.done() and not processing_task.cancelled():
            processing_error = processing_task.exception()
            if processing_error is not None:
                logger.error(
                    "Reconcile database slice failed during cancellation",
                    exc_info=(
                        type(processing_error),
                        processing_error,
                        processing_error.__traceback__,
                    ),
                )
        raise


def _load_history_checkpoint_projection(session_factory) -> dict[int, int]:
    from telegram_kol_research.models import SyncCheckpoint

    with session_factory() as session:
        checkpoints = (
            session.query(SyncCheckpoint)
            .filter(SyncCheckpoint.sync_kind == "history")
            .all()
        )
        return {
            checkpoint.chat_id: int(checkpoint.last_message_id or 0)
            for checkpoint in checkpoints
        }


def _load_orphan_media_message_ids(
    session_factory,
    *,
    dialog_id: int,
    replay_floor: int,
    media_root: str | Path,
) -> set[int]:
    with session_factory() as session:
        media_rows = (
            session.query(RawMessage.message_id, MediaAsset.local_path)
            .join(MediaAsset, MediaAsset.raw_message_id == RawMessage.id)
            .filter(
                RawMessage.chat_id == dialog_id,
                RawMessage.message_id > replay_floor,
            )
            .all()
        )
    resolved_media_root = Path(media_root)
    return {
        int(row.message_id)
        for row in media_rows
        if not _is_usable_downloaded_media_path(
            row.local_path,
            media_root=resolved_media_root,
        )
    }


def _persist_history_reconcile_records(
    session_factory,
    *,
    records,
    broker,
) -> dict[str, Any]:
    return persist_normalized_messages(
        session_factory,
        records,
        sync_kind="history",
        broker=broker,
    )


@asynccontextmanager
async def _chat_write_lock(
    registry: KeyedAsyncLockRegistry | None,
    chat_id: int,
) -> AsyncIterator[None]:
    """Hold ``chat_id``'s lock, or nothing when no registry is wired."""

    if registry is None:
        yield
        return
    async with registry.lock(chat_id):
        yield


async def run_reconcile_once(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    message_limit: int = 50,
    checkpoint_overlap: int = 5,
    authoritative_processor: Callable[[int], Any] | None = None,
    notification_bot_config: Any | None = None,
    discover_dialogs_fn=discover_dialogs,
    fetch_dialog_messages_fn=fetch_dialog_messages,
    chat_operation_lock: KeyedAsyncLockRegistry | None = None,
) -> dict[str, int]:
    """Fetch a recent overlap window and persist only messages newer than the history checkpoint.

    This is the ``ingest`` role's compensator against **Telegram**: it replays
    a small recent history window to find messages the live listener never
    received, persists them, and enqueues them. Finding *persisted* messages
    that were never processed is the ``worker`` role's job
    (:func:`run_authoritative_gap_recovery_loop`), not this one.

    ``chat_operation_lock``, when given, is the ``ingest`` process's
    :class:`~telegram_kol_research.keyed_async_locks.KeyedAsyncLockRegistry`.
    Only the per-dialog persist-and-enqueue slice runs under that dialog's own
    lock, never dialog discovery or the Telegram fetch calls. That keeps a
    reconcile pass from blocking live traffic for its whole duration while
    still serializing writes to ``raw_messages`` for one chat against the live
    handler for that same chat.
    """

    await _run_reconcile_database_slice(
        _repair_reconcile_history_checkpoints,
        session_factory,
    )
    if system_operator_bot_enabled(notification_bot_config):
        await deliver_pending_message_instruction_summaries(
            session_factory,
            config=notification_bot_config,
        )
    discovery_kwargs = _filter_callable_kwargs(
        discover_dialogs_fn,
        {"archived_only": True},
    )
    dialogs = await discover_dialogs_fn(client, **discovery_kwargs)
    matched_dialogs = filter_target_dialogs(dialogs, target_titles)

    history_checkpoints = await _run_reconcile_database_slice(
        _load_history_checkpoint_projection,
        session_factory,
    )

    inserted_messages = 0
    inserted_candidates = 0
    inserted_trade_ideas = 0
    recognition_status = "queued"

    for dialog in matched_dialogs:
        checkpoint_message_id = history_checkpoints.get(int(dialog.get("id") or 0), 0)
        replay_floor = max(0, checkpoint_message_id - checkpoint_overlap)

        # ── Also re-fetch messages whose media download failed earlier ──
        dialog_id = int(dialog.get("id") or 0)
        orphan_msg_ids = await _run_reconcile_database_slice(
            _load_orphan_media_message_ids,
            session_factory,
            dialog_id=dialog_id,
            replay_floor=replay_floor,
            media_root=media_root,
        )
        fetch_kwargs = _filter_callable_kwargs(
            fetch_dialog_messages_fn,
            {
                "limit": message_limit,
                "media_root": media_root,
                "media_download_min_message_id": checkpoint_message_id,
                "media_download_message_ids": orphan_msg_ids,
            },
        )
        payloads = await fetch_dialog_messages_fn(client, dialog, **fetch_kwargs)

        payloads = [
            payload
            for payload in payloads
            if int(payload.get("message_id") or 0) > replay_floor
        ]
        records = [
            normalize_message_payload(payload, archived_target_group=True)
            for payload in payloads
            if int(payload.get("message_id") or 0) > checkpoint_message_id
            or int(payload.get("message_id") or 0) in orphan_msg_ids
        ]
        if not records:
            continue

        # Only this slice takes the chat's lock: it is the one place where a
        # reconcile pass and the live handler for the same chat both write
        # raw_messages, and raw_messages has no unique key on
        # (chat_id, message_id) to fall back on.
        async with _chat_write_lock(chat_operation_lock, dialog_id):
            stats = await _run_reconcile_database_slice(
                _persist_history_reconcile_records,
                session_factory,
                records=records,
                broker=broker,
            )
            inserted_messages += stats["inserted_messages"]
            inserted_keys = stats.get("inserted_message_keys") or []
            await _try_enqueue_processing_jobs(
                session_factory,
                message_keys=inserted_keys,
                last_reason="history_reconcile_enqueued",
            )
        if authoritative_processor is None:
            logger.error(
                "history recognition authority unavailable "
                "reason=authoritative_processor_required"
            )

    return {
        "matched_dialogs": len(matched_dialogs),
        "inserted_messages": inserted_messages,
        "inserted_candidates": inserted_candidates,
        "inserted_trade_ideas": inserted_trade_ideas,
        "recognition_status": recognition_status,
    }


async def run_periodic_reconcile(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    interval_seconds: int = 300,
    message_limit: int = 50,
    operation_lock: KeyedAsyncLockRegistry | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    notification_bot_config: Any | None = None,
) -> None:
    """Periodically replay a small recent history window for missed-message recovery.

    This is the ``ingest`` role's ``reconcile`` singleton task. It compares
    against **Telegram history** and exists for exactly one failure: a message
    the live listener never received at all. Messages that were persisted but
    never processed are the ``worker`` role's
    :func:`run_authoritative_gap_recovery_loop`, on a much faster cadence.

    ``operation_lock`` is the ``ingest`` process's
    :class:`~telegram_kol_research.keyed_async_locks.KeyedAsyncLockRegistry`,
    handed straight to ``run_reconcile_once``. The pass as a whole holds no
    lock, so however long a Telegram fetch takes it never blocks live traffic;
    only the per-chat persist slice serializes against that chat's live
    handler.
    """

    while True:
        await run_reconcile_once(
            client=client,
            session_factory=session_factory,
            broker=broker,
            target_titles=target_titles,
            media_root=media_root,
            message_limit=message_limit,
            authoritative_processor=authoritative_processor,
            notification_bot_config=notification_bot_config,
            chat_operation_lock=operation_lock,
        )
        await asyncio.sleep(interval_seconds)


async def run_authoritative_gap_recovery_loop(
    *,
    session_factory,
    authoritative_processor: Callable[[int], Any] | None,
    chat_titles_by_id_provider: Callable[[], dict[int, str]],
    interval_seconds: float = DEFAULT_AUTHORITATIVE_GAP_RECOVERY_INTERVAL_SECONDS,
    message_limit: int = 50,
) -> None:
    """Recover missing authoritative decisions on a fast, network-free cadence.

    This loop runs in the ``worker`` role as the ``authoritative_gap_recovery_loop``
    singleton task and compares against the **database**: it finds messages
    that were persisted but still have no authoritative decision and enqueues
    them. Finding messages that were never persisted at all is the ``ingest``
    role's ``run_periodic_reconcile``, which is the only one of the two that
    talks to Telegram.

    It resolves ``chat_titles_by_id`` on every tick via
    ``chat_titles_by_id_provider`` - local configuration or the database,
    **never** ``discover_dialogs`` - so a stalled or unhealthy Telegram
    session cannot also block recovery of already-persisted local messages.
    ``chat_titles_by_id_provider`` is called through ``asyncio.to_thread`` so
    that even a database-backed provider cannot block the loop; the actual
    recovery work (:func:`recover_missing_authoritative_decisions`) already
    offloads its own synchronous work the same way.

    No lock is taken. This loop only inserts into ``message_processing_jobs``,
    idempotently, and it runs in the ``worker`` process, which holds neither
    the Telegram session nor the ``ingest`` process's lock registry. The
    execution-side mutual exclusion that does matter lives in
    :mod:`telegram_kol_research.position_authority_lock`.
    """

    while True:
        try:
            if authoritative_processor is not None:
                # Off the loop: the provider may be database-backed, and
                # this runs every iteration of an unconditional while-loop -
                # exactly the shape the event-loop blocking census exists to
                # catch.
                chat_titles_by_id = await asyncio.to_thread(
                    chat_titles_by_id_provider
                )
                if chat_titles_by_id:
                    await recover_missing_authoritative_decisions(
                        session_factory,
                        chat_titles_by_id=chat_titles_by_id,
                        authoritative_processor=authoritative_processor,
                        message_limit=message_limit,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("authoritative gap recovery loop tick failed")
        await asyncio.sleep(interval_seconds)
