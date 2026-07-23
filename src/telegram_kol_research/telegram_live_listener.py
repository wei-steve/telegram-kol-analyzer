"""Realtime Telegram listener helpers for web live updates."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import or_
from sqlalchemy import tuple_

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig, load_ai_recognition_config
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.message_recognition import (
    filter_records_by_inserted_message_keys,
    recognize_message_now,
    recognize_records_with_ai_config,
)
from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.models import (
    MediaAsset,
    MessageRecognition,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    utc_now,
)
from telegram_kol_research.raw_ingest import normalize_message_payload, persist_normalized_messages
from telegram_kol_research.raw_ingest import repair_history_checkpoints
from telegram_kol_research.recognition_experiments import run_mimo_direct_for_message
from telegram_kol_research.recognition_decisions import update_recognition_execution_outcome
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_terminal_authoritative_decision,
)
from telegram_kol_research.strategy_alerts import process_strategy_alert_for_record
from telegram_kol_research.system_operator_bot import (
    deliver_message_instruction_summary_notification,
    deliver_pending_message_instruction_summaries,
    send_ai_recognition_conflict_review,
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
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates


logger = logging.getLogger(__name__)
AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS = 60.0
AUTHORITATIVE_GAP_RECOVERY_MAX_AGE = timedelta(minutes=15)

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
    system_operator_bot_config: Any | None,
    claimed_at=None,
) -> bool:
    automation = getattr(processing_result, "automation", None)
    if (
        not isinstance(automation, dict)
        or not isinstance(automation.get("items"), list)
        or not automation["items"]
        or not system_operator_bot_enabled(system_operator_bot_config)
    ):
        return False
    return await deliver_message_instruction_summary_notification(
        session_factory,
        config=system_operator_bot_config,
        raw_message_id=raw_message_id,
        chat_title=chat_title,
        claimed_at=claimed_at,
    )


async def persist_live_message_event(
    *,
    event: Any,
    session_factory,
    broker,
    media_root: str | Path = "data/media",
    chat_title: str | None = None,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    strategy_alert_processor=process_strategy_alert_for_record,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
    auto_trade_executor: Callable[[int], Any] | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    authoritative_failure_retry_delay_seconds: float = (
        AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS
    ),
    system_operator_bot_config: Any | None = None,
    system_operator_conflict_sender=send_ai_recognition_conflict_review,
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

    # ── Immediately run AI recognition on every newly persisted message ──
    inserted_keys = stats.get("inserted_message_keys") or []
    recog_result = None
    live_ai_config = ai_recognition_config
    if ai_recognition_config_path is not None:
        live_ai_config = load_ai_recognition_config(ai_recognition_config_path)
    if inserted_keys and live_ai_config is not None:
        chat_id, message_id = inserted_keys[0]
        with session_factory() as session:
            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == chat_id,
                    RawMessage.message_id == message_id,
                )
                .one_or_none()
            )
        if raw_message is not None:
            if authoritative_processor is not None:
                processing_result = await asyncio.to_thread(
                    authoritative_processor,
                    raw_message.id,
                )
                recog_result = processing_result.recognition
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
                        raw_message_id=raw_message.id,
                        sender=system_operator_conflict_sender,
                        config=system_operator_bot_config,
                        payload=conflict_payload,
                        retry_processor=authoritative_processor,
                        retry_delay_seconds=authoritative_failure_retry_delay_seconds,
                    )
                await _deliver_authoritative_instruction_summary(
                    processing_result=processing_result,
                    session_factory=session_factory,
                    raw_message_id=raw_message.id,
                    chat_title=chat_title,
                    system_operator_bot_config=system_operator_bot_config,
                )
            else:
                recog_result = await asyncio.to_thread(
                    recognize_message_now,
                    session_factory,
                    raw_message_id=raw_message.id,
                    ai_recognition_config=live_ai_config,
                )
                mimo_result = await asyncio.to_thread(
                    run_mimo_direct_for_message,
                    session_factory,
                    raw_message_id=raw_message.id,
                    ai_recognition_config=live_ai_config,
                    media_root=media_root,
                )
                conflict_payload = _build_ai_recognition_conflict_payload(
                    raw_message=raw_message,
                    chat_title=chat_title,
                    deepseek_result=recog_result,
                    mimo_result=mimo_result,
                )
                if conflict_payload is not None and system_operator_bot_enabled(system_operator_bot_config):
                    await system_operator_conflict_sender(
                        config=system_operator_bot_config,
                        payload=conflict_payload,
                    )
                    return stats
                if auto_trade_executor is not None:
                    await asyncio.to_thread(auto_trade_executor, raw_message.id)
            # Legacy lifecycle hook remains for callers that have not adopted
            # the authoritative processor. MiMo application owns this in production.
            if authoritative_processor is None and lifecycle_monitor is not None and recog_result is not None:
                ai_payload = recog_result.ai_payload or {}
                strategy_kind = ai_payload.get("strategy_kind")
                if strategy_kind == "exit":
                    strategy = ai_payload.get("strategy") or {}
                    symbol = str(strategy.get("symbol") or "")
                    side = str(strategy.get("side") or "")
                    if symbol and side:
                        await lifecycle_monitor.on_new_exit_signal(
                            chat_id=chat_id,
                            symbol=symbol,
                            side=side,
                            message_id=message_id,
                        )

    if (
        strategy_alert_config is not None
        and chat_title
        and (
            strategy_alert_enabled_for_title is None
            or strategy_alert_enabled_for_title(chat_title)
        )
    ):
        await strategy_alert_processor(
            session_factory=session_factory,
            record=record,
            chat_title=chat_title,
            config=strategy_alert_config,
            recognition_result=recog_result,
        )
    return stats


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
) -> None:
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_message_id,
        automation_status=str(payload.get("automation", {}).get("status") or "unknown"),
        automation_reason=payload.get("automation", {}).get("reason"),
        notification_status="scheduled",
    )

    async def send_in_background() -> None:
        try:
            await sender(config=config, payload=payload)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.exception("authoritative recognition notification failed")
            await asyncio.to_thread(
                update_recognition_execution_outcome,
                session_factory,
                raw_message_id=raw_message_id,
                automation_status=str(payload.get("automation", {}).get("status") or "unknown"),
                automation_reason=payload.get("automation", {}).get("reason"),
                notification_status="failed",
                notification_error=str(exc),
            )
        else:
            await asyncio.to_thread(
                update_recognition_execution_outcome,
                session_factory,
                raw_message_id=raw_message_id,
                automation_status=str(payload.get("automation", {}).get("status") or "unknown"),
                automation_reason=payload.get("automation", {}).get("reason"),
                notification_status="sent",
            )

    asyncio.create_task(send_in_background())


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


def _record_expired_authoritative_recovery_gap(
    session_factory,
    *,
    raw_message: RawMessage,
) -> None:
    """Persist a visible, non-executing terminal result for a stale gap."""

    reason = "authoritative_gap_recovery_expired"
    summary = (
        "消息未在 15 分钟内完成权威识别；为防止执行过期信号，未自动交易。"
    )
    save_terminal_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_message.id,
            input_kind="recovery_guard",
            authoritative_model="recovery_guard",
            authoritative_status="识别失败",
            authoritative_payload={"reason": reason, "summary": summary},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[reason],
            prompt_versions={},
        ),
    )
    with session_factory() as session:
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
        session.commit()
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_message.id,
        automation_status="skipped",
        automation_reason=reason,
        notification_status="suppressed_expired_recovery",
    )


def _build_ai_recognition_conflict_payload(
    *,
    raw_message: RawMessage,
    chat_title: str | None,
    deepseek_result: Any | None,
    mimo_result: Any | None,
) -> dict[str, Any] | None:
    deepseek = _classify_deepseek_recognition(deepseek_result)
    mimo = _classify_mimo_recognition(mimo_result)
    if deepseek is None or mimo is None:
        return None
    if deepseek["kind"] == mimo["kind"]:
        return None
    return {
        "chat_title": chat_title,
        "chat_id": raw_message.chat_id,
        "message_id": raw_message.message_id,
        "posted_at": raw_message.posted_at,
        "text": raw_message.text,
        "deepseek": deepseek,
        "mimo": mimo,
    }


def _classify_deepseek_recognition(result: Any | None) -> dict[str, str] | None:
    if result is None:
        return None
    status = str(getattr(result, "status", "") or "").strip()
    reason = str(getattr(result, "reason", "") or "").strip()
    parse_source = str(getattr(result, "parse_source", "") or "").strip()
    ai_payload = getattr(result, "ai_payload", None) or {}
    lifecycle_event = ai_payload.get("lifecycle_event") if isinstance(ai_payload, dict) else None
    if status == "识别失败":
        return None
    kind = "non_strategy"
    if status == "是策略" or parse_source == "lifecycle_ai":
        kind = "strategy_related"
    if isinstance(lifecycle_event, dict) and str(lifecycle_event.get("event_type") or "none") != "none":
        kind = "strategy_related"
    return {"status": status or "-", "kind": kind, "reason": reason or "-"}


def _classify_mimo_recognition(result: Any | None) -> dict[str, str] | None:
    if result is None or getattr(result, "error_message", None):
        return None
    status = str(getattr(result, "status", "") or "").strip()
    reason = str(getattr(result, "reason", "") or "").strip()
    if status in {"", "识别失败"}:
        return None
    kind = "strategy_related" if status != "非策略" else "non_strategy"
    return {"status": status, "kind": kind, "reason": reason or "-"}


async def run_live_listener(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
    auto_trade_executor: Callable[[int], Any] | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    system_operator_bot_config: Any | None = None,
    operation_lock: Any | None = None,
) -> None:
    """Attach Telethon new-message handlers and keep the client alive."""

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
        persist_kwargs = {
            "event": event,
            "session_factory": session_factory,
            "broker": broker,
            "media_root": media_root,
            "chat_title": title,
            "strategy_alert_config": strategy_alert_config,
            "strategy_alert_enabled_for_title": strategy_alert_enabled_for_title,
            "ai_recognition_config": ai_recognition_config,
            "ai_recognition_config_path": ai_recognition_config_path,
            "lifecycle_monitor": lifecycle_monitor,
            "auto_trade_executor": auto_trade_executor,
            "authoritative_processor": authoritative_processor,
            "system_operator_bot_config": system_operator_bot_config,
        }
        if operation_lock is None:
            await persist_live_message_event(**persist_kwargs)
        else:
            async with operation_lock:
                await persist_live_message_event(**persist_kwargs)

    add_event_handler = getattr(client, "add_event_handler", None)
    if not callable(add_event_handler):
        raise RuntimeError("Telegram client does not support realtime event handlers")

    connect = getattr(client, "connect", None)
    if callable(connect):
        await maybe_await(connect())

    add_event_handler(handle_new_message, events.NewMessage())
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
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
    auto_trade_executor: Callable[[int], Any] | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    system_operator_bot_config: Any | None = None,
    operation_lock: Any | None = None,
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
            "strategy_alert_config": strategy_alert_config,
            "strategy_alert_enabled_for_title": strategy_alert_enabled_for_title,
            "ai_recognition_config": ai_recognition_config,
            "ai_recognition_config_path": ai_recognition_config_path,
            "lifecycle_monitor": lifecycle_monitor,
            "auto_trade_executor": auto_trade_executor,
            "authoritative_processor": authoritative_processor,
            "system_operator_bot_config": system_operator_bot_config,
            "operation_lock": operation_lock,
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


async def run_reconcile_once(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    message_limit: int = 50,
    checkpoint_overlap: int = 5,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    strategy_alert_processor=process_strategy_alert_for_record,
    authoritative_processor: Callable[[int], Any] | None = None,
    system_operator_bot_config: Any | None = None,
    system_operator_conflict_sender=send_ai_recognition_conflict_review,
    authoritative_failure_retry_delay_seconds: float = (
        AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS
    ),
    discover_dialogs_fn=discover_dialogs,
    fetch_dialog_messages_fn=fetch_dialog_messages,
) -> dict[str, int]:
    """Fetch a recent overlap window and persist only messages newer than the history checkpoint."""

    repair_history_checkpoints(session_factory)
    if system_operator_bot_enabled(system_operator_bot_config):
        await deliver_pending_message_instruction_summaries(
            session_factory,
            config=system_operator_bot_config,
        )
    dialogs = await discover_dialogs_fn(client)
    matched_dialogs = filter_target_dialogs(dialogs, target_titles)

    recovered_messages = 0
    expired_recovery_messages = 0
    if authoritative_processor is not None:
        chat_titles_by_id = {
            int(dialog["id"]): str(dialog.get("title") or "")
            for dialog in matched_dialogs
            if dialog.get("id") is not None
        }
        recovery_cutoff = utc_now() - AUTHORITATIVE_GAP_RECOVERY_MAX_AGE
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
                missing_decision_query.filter(
                    RawMessage.posted_at >= recovery_cutoff
                )
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
        for raw_message in missing_decision_messages:
            try:
                processing_result = await asyncio.to_thread(
                    authoritative_processor,
                    raw_message.id,
                )
            except Exception:
                logger.exception(
                    "authoritative recognition recovery failed: raw_message_id=%s",
                    raw_message.id,
                )
                continue
            notification_payload = _build_authoritative_notification_payload(
                raw_message=raw_message,
                chat_title=chat_titles_by_id.get(raw_message.chat_id, ""),
                processing_result=processing_result,
            )
            if notification_payload is not None and system_operator_bot_enabled(
                system_operator_bot_config
            ):
                _handle_authoritative_failure_notification(
                    session_factory=session_factory,
                    raw_message_id=raw_message.id,
                    sender=system_operator_conflict_sender,
                    config=system_operator_bot_config,
                    payload=notification_payload,
                    retry_processor=authoritative_processor,
                    retry_delay_seconds=authoritative_failure_retry_delay_seconds,
                )
            await _deliver_authoritative_instruction_summary(
                processing_result=processing_result,
                session_factory=session_factory,
                raw_message_id=raw_message.id,
                chat_title=chat_titles_by_id.get(raw_message.chat_id, ""),
                system_operator_bot_config=system_operator_bot_config,
            )
            recovered_messages += 1
        for raw_message in expired_messages:
            _record_expired_authoritative_recovery_gap(
                session_factory,
                raw_message=raw_message,
            )
            expired_recovery_messages += 1

    history_checkpoints: dict[int, int] = {}
    with session_factory() as session:
        from telegram_kol_research.models import SyncCheckpoint

        checkpoints = (
            session.query(SyncCheckpoint)
            .filter(SyncCheckpoint.sync_kind == "history")
            .all()
        )
        history_checkpoints = {
            checkpoint.chat_id: int(checkpoint.last_message_id or 0)
            for checkpoint in checkpoints
        }

    inserted_messages = 0
    inserted_candidates = 0
    inserted_trade_ideas = 0

    for dialog in matched_dialogs:
        checkpoint_message_id = history_checkpoints.get(int(dialog.get("id") or 0), 0)
        replay_floor = max(0, checkpoint_message_id - checkpoint_overlap)

        # ── Also re-fetch messages whose media download failed earlier ──
        dialog_id = int(dialog.get("id") or 0)
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
        orphan_msg_ids = {
            row.message_id
            for row in media_rows
            if not _is_usable_downloaded_media_path(
                row.local_path,
                media_root=resolved_media_root,
            )
        }
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

        stats = persist_normalized_messages(
            session_factory,
            records,
            sync_kind="history",
            broker=broker,
        )
        inserted_messages += stats["inserted_messages"]
        inserted_records = filter_records_by_inserted_message_keys(records, stats)
        recognition_by_key: dict[tuple[int, int], Any] = {}
        if authoritative_processor is not None:
            inserted_keys = stats.get("inserted_message_keys") or []
            with session_factory() as session:
                raw_messages = (
                    session.query(RawMessage)
                    .filter(
                        tuple_(RawMessage.chat_id, RawMessage.message_id).in_(inserted_keys)
                    )
                    .all()
                    if inserted_keys
                    else []
                )
                candidate_count_before = session.query(SignalCandidate).count()
            for raw_message in raw_messages:
                processing_result = await asyncio.to_thread(
                    authoritative_processor,
                    raw_message.id,
                )
                key = (raw_message.chat_id, raw_message.message_id)
                recognition_by_key[key] = processing_result.recognition
                notification_payload = _build_authoritative_notification_payload(
                    raw_message=raw_message,
                    chat_title=str(dialog.get("title") or ""),
                    processing_result=processing_result,
                )
                if notification_payload is not None and system_operator_bot_enabled(
                    system_operator_bot_config
                ):
                    _handle_authoritative_failure_notification(
                        session_factory=session_factory,
                        raw_message_id=raw_message.id,
                        sender=system_operator_conflict_sender,
                        config=system_operator_bot_config,
                        payload=notification_payload,
                        retry_processor=authoritative_processor,
                        retry_delay_seconds=authoritative_failure_retry_delay_seconds,
                    )
                await _deliver_authoritative_instruction_summary(
                    processing_result=processing_result,
                    session_factory=session_factory,
                    raw_message_id=raw_message.id,
                    chat_title=str(dialog.get("title") or ""),
                    system_operator_bot_config=system_operator_bot_config,
                )
            with session_factory() as session:
                inserted_candidates += max(
                    0,
                    session.query(SignalCandidate).count() - candidate_count_before,
                )
        else:
            candidate_stats = recognize_records_with_ai_config(
                session_factory,
                inserted_records,
                fallback_recognizer=persist_text_signal_candidates,
            )
            inserted_candidates += candidate_stats["inserted_candidates"]
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]
        dialog_title = str(dialog.get("title") or "")
        if (
            strategy_alert_config is not None
            and (
                strategy_alert_enabled_for_title is None
                or strategy_alert_enabled_for_title(dialog_title)
            )
        ):
            for record in inserted_records:
                await strategy_alert_processor(
                    session_factory=session_factory,
                    record=record,
                    chat_title=dialog_title,
                    config=strategy_alert_config,
                    recognition_result=recognition_by_key.get(
                        (record.chat_id, record.message_id)
                    ),
                )

    return {
        "matched_dialogs": len(matched_dialogs),
        "inserted_messages": inserted_messages,
        "inserted_candidates": inserted_candidates,
        "inserted_trade_ideas": inserted_trade_ideas,
        "recovered_messages": recovered_messages,
        "expired_recovery_messages": expired_recovery_messages,
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
    operation_lock: Any | None = None,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    authoritative_processor: Callable[[int], Any] | None = None,
    system_operator_bot_config: Any | None = None,
    authoritative_failure_retry_delay_seconds: float = (
        AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS
    ),
) -> None:
    """Periodically replay a small recent history window for missed-message recovery."""

    while True:
        if operation_lock is None:
            await run_reconcile_once(
                client=client,
                session_factory=session_factory,
                broker=broker,
                target_titles=target_titles,
                media_root=media_root,
                message_limit=message_limit,
                strategy_alert_config=strategy_alert_config,
                strategy_alert_enabled_for_title=strategy_alert_enabled_for_title,
                authoritative_processor=authoritative_processor,
                system_operator_bot_config=system_operator_bot_config,
                authoritative_failure_retry_delay_seconds=authoritative_failure_retry_delay_seconds,
            )
        else:
            async with operation_lock:
                await run_reconcile_once(
                    client=client,
                    session_factory=session_factory,
                    broker=broker,
                    target_titles=target_titles,
                    media_root=media_root,
                    message_limit=message_limit,
                    strategy_alert_config=strategy_alert_config,
                    strategy_alert_enabled_for_title=strategy_alert_enabled_for_title,
                    authoritative_processor=authoritative_processor,
                    system_operator_bot_config=system_operator_bot_config,
                    authoritative_failure_retry_delay_seconds=authoritative_failure_retry_delay_seconds,
                )
        await asyncio.sleep(interval_seconds)
