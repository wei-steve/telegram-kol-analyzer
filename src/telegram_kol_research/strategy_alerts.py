"""AI-assisted realtime strategy alert forwarding."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.llm_chat import _load_env_file_values
from telegram_kol_research.models import RawMessage, SignalCandidate, StrategyAlert, StrategyLifecycle
from telegram_kol_research.prompt_composition import render_registered_prompt, render_template_strict
from telegram_kol_research.prompt_defaults import STRATEGY_ALERT_PROMPT, seed_default_prompt_registry
from telegram_kol_research.prompt_registry import PromptInvocationRecord, record_prompt_invocation
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.time_utils import utc_naive_to_local


@dataclass(slots=True)
class StrategyAlertConfig:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    timeout_seconds: float
    bot_token: str
    alert_chat_id: str
    confidence_threshold: float = 0.6
    max_chars: int = 1200


@dataclass(slots=True)
class AlertDecision:
    is_strategy: bool
    strategy_kind: str
    confidence: float
    kol_label: str
    reason_short: str


@dataclass(slots=True)
class StrategyAlertEvent:
    alert_type: str
    strategy_kind: str
    is_strategy: bool
    confidence: float
    reason_short: str
    symbol: str | None
    side: str | None
    order_type: str | None
    management_action: str | None
    entry_price: str | None
    take_profit: str | None
    stop_loss: str | None
    posted_at: datetime | None
    original_text: str


LLMRequester = Callable[..., Awaitable[str]]
BotSender = Callable[..., Awaitable[None]]


def load_strategy_alert_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> StrategyAlertConfig:
    """Load alert forwarding configuration from environment variables."""

    env = dict(
        _load_env_file_values(
            env_file_paths
            or [
                ".env",
                "config/llm.env",
                "config/telegram.env",
            ]
        )
    )
    env.update(environ or os.environ)
    llm_model = env.get("TELEGRAM_KOL_ALERT_LLM_MODEL") or env.get(
        "TELEGRAM_KOL_LLM_MODEL",
        "gpt-4.1-mini",
    )
    return StrategyAlertConfig(
        llm_base_url=env.get("TELEGRAM_KOL_LLM_BASE_URL", "http://127.0.0.1:8317"),
        llm_api_key=env.get("TELEGRAM_KOL_LLM_API_KEY", ""),
        llm_model=llm_model,
        timeout_seconds=float(env.get("TELEGRAM_KOL_LLM_TIMEOUT_SECONDS", "60")),
        bot_token=env.get("TELEGRAM_KOL_ALERT_BOT_TOKEN", ""),
        alert_chat_id=env.get("TELEGRAM_KOL_ALERT_CHAT_ID", ""),
        confidence_threshold=float(
            env.get("TELEGRAM_KOL_ALERT_CONFIDENCE_THRESHOLD", "0.6")
        ),
        max_chars=int(env.get("TELEGRAM_KOL_ALERT_MAX_CHARS", "1200")),
    )


def strategy_alerts_enabled(config: StrategyAlertConfig) -> bool:
    """Return whether both bot destination settings are present."""

    return bool(config.bot_token and config.alert_chat_id)


def build_strategy_alert_prompt(
    *,
    template: str,
    chat_title: str,
    sender_name: str | None,
    text: str,
    max_chars: int = 1200,
) -> str:
    """Build a compact classification prompt for one Telegram text message."""

    trimmed_text = text.strip()
    if len(trimmed_text) > max_chars:
        trimmed_text = f"{trimmed_text[:max_chars].rstrip()}\n[truncated]"
    first_line = trimmed_text.splitlines()[0].strip() if trimmed_text else ""
    return render_template_strict(
        template,
        chat_title=chat_title,
        sender_name=sender_name or "",
        first_line=first_line,
        message_text=trimmed_text,
    )


def parse_alert_decision(content: str) -> AlertDecision:
    """Parse the model's compact JSON decision."""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return AlertDecision(
            is_strategy=False,
            strategy_kind="error",
            confidence=0.0,
            kol_label="",
            reason_short="Invalid JSON from AI",
        )

    strategy_kind = str(payload.get("strategy_kind") or "other").lower()
    if strategy_kind not in {"entry", "exit", "other", "error"}:
        strategy_kind = "other"
    confidence = _coerce_confidence(payload.get("confidence"))
    confidence = max(0.0, min(1.0, confidence))
    return AlertDecision(
        is_strategy=bool(payload.get("is_strategy")),
        strategy_kind=strategy_kind,
        confidence=confidence,
        kol_label=str(payload.get("kol_label") or "").strip(),
        reason_short=str(payload.get("reason_short") or "").strip(),
    )


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        normalized = value.strip().lower()
        mapped = {
            "高": 0.85,
            "中": 0.65,
            "低": 0.4,
            "high": 0.85,
            "medium": 0.65,
            "mid": 0.65,
            "low": 0.4,
        }.get(normalized)
        if mapped is not None:
            return mapped
        try:
            return max(0.0, min(1.0, float(normalized)))
        except ValueError:
            return 0.0
    return 0.0


def format_strategy_alert_message(
    *,
    chat_title: str,
    decision: AlertDecision,
    original_text: str,
) -> str:
    """Format a Telegram Bot API message for a strategy alert."""

    kind_label = {"entry": "进场", "exit": "离场"}.get(
        decision.strategy_kind,
        decision.strategy_kind or "策略",
    )
    text = original_text.strip()
    return "\n".join(
        [
            f"KOL群组：{chat_title}",
            f"类型：{kind_label}",
            "原文：",
            text,
        ]
    )


def format_structured_strategy_alert_message(
    *,
    chat_title: str,
    event: StrategyAlertEvent,
    message_id: int,
) -> str:
    """Format a unified human-readable Telegram alert message."""

    local_time = utc_naive_to_local(event.posted_at)
    time_text = (
        local_time.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
        if local_time is not None
        else "-"
    )
    side_text = {"long": "多", "short": "空"}.get(
        (event.side or "").lower(),
        event.side or "-",
    )
    order_type_text = _format_order_type_label(event.order_type)
    management_action_text = _format_management_action_label(event.management_action)
    confidence_text = f"{event.confidence:.2f}" if event.confidence else "-"
    text = event.original_text.strip() or "-"
    lines = [
        "【KOL策略提醒】",
        f"信号类型: {event.alert_type}",
        f"策略类别: {event.strategy_kind}",
        f"群组: {chat_title}",
        f"时间: {time_text}",
        "",
        f"交易对: {event.symbol or '-'}",
        f"方向: {side_text}",
        f"入场方式: {order_type_text}",
        f"入场价格: {event.entry_price or '-'}",
        f"止盈价格: {event.take_profit or '-'}",
        f"止损价格: {event.stop_loss or '-'}",
    ]
    if management_action_text != "-":
        lines.append(f"管理动作: {management_action_text}")
    lines.extend(
        [
            f"置信度: {confidence_text}",
            f"消息ID: {message_id}",
            "",
            "原文:",
            text,
        ]
    )
    return "\n".join(lines)


def build_strategy_alert_event_from_recognition(
    *,
    session_factory: sessionmaker,
    record: NormalizedMessageRecord,
    recognition_result: Any | None,
) -> StrategyAlertEvent | None:
    """Build an alert event from persisted recognition/candidate/lifecycle state."""

    raw_message, candidate, lifecycle = _load_alert_context(session_factory, record)
    if raw_message is None or candidate is None:
        return None
    if candidate.event_type == "duplicate_entry_signal":
        return None

    event_type = candidate.event_type or ""
    parse_source = candidate.parse_source or ""
    ai_payload = getattr(recognition_result, "ai_payload", None) or {}
    strategy_payload = ai_payload.get("strategy") if isinstance(ai_payload.get("strategy"), dict) else {}
    order_type = _resolve_order_type(
        strategy_payload.get("order_type") if strategy_payload else None,
        candidate.entry_text,
        event_type=event_type,
        parse_source=parse_source,
    )
    management_action = lifecycle.management_action if lifecycle is not None else None
    strategy_kind = _strategy_kind_for_candidate(candidate, lifecycle)
    alert_type = _alert_type_for_candidate(
        candidate,
        lifecycle,
        order_type=order_type,
        management_action=management_action,
    )
    if alert_type is None:
        return None

    confidence = float(candidate.confidence or 0.0)
    reason = str(ai_payload.get("reason") or getattr(recognition_result, "reason", None) or "").strip()

    entry_price = candidate.entry_text
    take_profit = candidate.take_profit_text
    stop_loss = candidate.stop_loss_text
    if lifecycle is not None:
        if event_type == "entry_signal" and parse_source == "lifecycle_ai":
            entry_price = _format_number(lifecycle.entry_price_actual) or entry_price
        elif event_type == "close_signal":
            entry_price = (
                _format_number(lifecycle.entry_price_actual)
                or _format_lifecycle_entry_range(lifecycle)
                or entry_price
            )
        else:
            entry_price = entry_price or _format_lifecycle_entry_range(lifecycle)
        take_profit = take_profit or lifecycle.take_profit
        stop_loss = stop_loss or _format_number(lifecycle.stop_loss)

    return StrategyAlertEvent(
        alert_type=alert_type,
        strategy_kind=strategy_kind,
        is_strategy=True,
        confidence=confidence,
        reason_short=reason,
        symbol=candidate.symbol or (lifecycle.symbol if lifecycle is not None else None),
        side=candidate.side or (lifecycle.side if lifecycle is not None else None),
        order_type=order_type,
        management_action=management_action,
        entry_price=entry_price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        posted_at=raw_message.posted_at or record.posted_at,
        original_text=raw_message.text or record.text or "",
    )


async def request_strategy_alert_decision(
    *,
    config: StrategyAlertConfig,
    prompt: str,
) -> str:
    """Request a one-message strategy classification from the LLM proxy."""

    headers = {"Content-Type": "application/json"}
    if config.llm_api_key:
        headers["Authorization"] = f"Bearer {config.llm_api_key}"
    payload = {
        "model": config.llm_model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"{config.llm_base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else ""


async def send_strategy_alert_bot_message(
    *,
    config: StrategyAlertConfig,
    text: str,
) -> None:
    """Send the formatted strategy alert through Telegram Bot API."""

    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json={
                "chat_id": config.alert_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()


async def process_strategy_alert_for_record(
    *,
    session_factory: sessionmaker,
    record: NormalizedMessageRecord,
    chat_title: str,
    config: StrategyAlertConfig,
    recognition_result: Any | None = None,
    llm_requester: LLMRequester = request_strategy_alert_decision,
    bot_sender: BotSender = send_strategy_alert_bot_message,
) -> dict[str, Any]:
    """Classify and optionally forward one normalized message with idempotency."""

    alert_status = _get_or_create_alert(
        session_factory=session_factory,
        record=record,
        chat_title=chat_title,
    )
    if alert_status == "sent":
        return {"status": "already_sent"}

    event = build_strategy_alert_event_from_recognition(
        session_factory=session_factory,
        record=record,
        recognition_result=recognition_result,
    )
    if event is not None:
        message = format_structured_strategy_alert_message(
            chat_title=chat_title,
            event=event,
            message_id=record.message_id,
        )
        try:
            await _retry_async(
                lambda: bot_sender(config=config, text=message),
                attempts=3,
            )
        except Exception as exc:
            _update_alert(
                session_factory,
                record=record,
                status="bot_failed",
                is_strategy=event.is_strategy,
                strategy_kind=event.strategy_kind,
                ai_confidence=event.confidence,
                reason_short=event.reason_short,
                error_message=str(exc),
            )
            return {"status": "bot_failed", "error": str(exc), "event": event}

        _update_alert(
            session_factory,
            record=record,
            status="sent",
            is_strategy=event.is_strategy,
            strategy_kind=event.strategy_kind,
            ai_confidence=event.confidence,
            reason_short=event.reason_short,
            error_message=None,
            forwarded_at=datetime.now(UTC),
        )
        return {"status": "sent", "event": event}

    if recognition_result is not None:
        _update_alert(
            session_factory,
            record=record,
            status="ignored_not_strategy",
            is_strategy=False,
            strategy_kind="other",
            ai_confidence=None,
            reason_short=getattr(recognition_result, "reason", None),
        )
        return {"status": "ignored_not_strategy"}

    text = (record.text or "").strip()
    if not text:
        _update_alert(
            session_factory,
            record=record,
            status="skipped_empty_text",
            is_strategy=False,
            strategy_kind="other",
            ai_confidence=0.0,
            reason_short="Empty text message; media/OCR is not processed in v1.",
        )
        return {"status": "skipped_empty_text"}

    seed_default_prompt_registry(session_factory)
    trimmed_text = text
    if len(trimmed_text) > config.max_chars:
        trimmed_text = f"{trimmed_text[:config.max_chars].rstrip()}\n[truncated]"
    rendered = render_registered_prompt(
        session_factory,
        STRATEGY_ALERT_PROMPT,
        variables={
            "chat_title": chat_title,
            "sender_name": record.sender_name or "",
            "first_line": trimmed_text.splitlines()[0].strip(),
            "message_text": trimmed_text,
        },
    )
    prompt = rendered.content
    invocation_status = "success"
    invocation_error = None
    try:
        content = await _retry_async(
            lambda: llm_requester(config=config, prompt=prompt),
            attempts=3,
        )
    except Exception as exc:
        invocation_status = "error"
        invocation_error = str(exc)
        _update_alert(
            session_factory,
            record=record,
            status="ai_failed",
            error_message=str(exc),
        )
        return {"status": "ai_failed", "error": str(exc)}
    finally:
        raw_message, _, _ = _load_alert_context(session_factory, record)
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="strategy_alert",
                correlation_key=f"strategy_alert:{record.chat_id}:{record.message_id}",
                raw_message_id=raw_message.id if raw_message is not None else None,
                chat_id=record.chat_id,
                model=config.llm_model,
                prompt_versions=rendered.version_map,
                status=invocation_status,
                error_message=invocation_error,
            ),
        )

    decision = parse_alert_decision(content)
    should_send = (
        decision.is_strategy
        and decision.strategy_kind in {"entry", "exit"}
    )
    if not should_send:
        _update_alert(
            session_factory,
            record=record,
            status="ignored_low_confidence",
            is_strategy=decision.is_strategy,
            strategy_kind=decision.strategy_kind,
            ai_confidence=decision.confidence,
            kol_label=decision.kol_label,
            reason_short=decision.reason_short,
        )
        return {"status": "ignored_low_confidence", "decision": decision}

    message = format_strategy_alert_message(
        chat_title=chat_title,
        decision=decision,
        original_text=text,
    )
    try:
        await _retry_async(
            lambda: bot_sender(config=config, text=message),
            attempts=3,
        )
    except Exception as exc:
        _update_alert(
            session_factory,
            record=record,
            status="bot_failed",
            is_strategy=decision.is_strategy,
            strategy_kind=decision.strategy_kind,
            ai_confidence=decision.confidence,
            kol_label=decision.kol_label,
            reason_short=decision.reason_short,
            error_message=str(exc),
        )
        return {"status": "bot_failed", "error": str(exc), "decision": decision}

    _update_alert(
        session_factory,
        record=record,
        status="sent",
        is_strategy=decision.is_strategy,
        strategy_kind=decision.strategy_kind,
        ai_confidence=decision.confidence,
        kol_label=decision.kol_label,
        reason_short=decision.reason_short,
        error_message=None,
        forwarded_at=datetime.now(UTC),
    )
    return {"status": "sent", "decision": decision}


def _load_alert_context(
    session_factory: sessionmaker,
    record: NormalizedMessageRecord,
) -> tuple[RawMessage | None, SignalCandidate | None, StrategyLifecycle | None]:
    with session_factory() as session:
        raw_message = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == record.chat_id,
                RawMessage.message_id == record.message_id,
            )
            .one_or_none()
        )
        if raw_message is None:
            return None, None, None
        candidate = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message.id)
            .order_by(SignalCandidate.id.asc())
            .first()
        )
        lifecycle = _find_related_lifecycle(session, raw_message, candidate)
        return raw_message, candidate, lifecycle


def _find_related_lifecycle(
    session,
    raw_message: RawMessage,
    candidate: SignalCandidate | None,
) -> StrategyLifecycle | None:
    if candidate is None:
        return None
    if candidate.event_type in {"entry_signal", "duplicate_entry_signal"}:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.chat_id == raw_message.chat_id,
                StrategyLifecycle.message_id == raw_message.message_id,
            )
            .one_or_none()
        )
        if lifecycle is not None:
            return lifecycle
    query = session.query(StrategyLifecycle).filter(StrategyLifecycle.chat_id == raw_message.chat_id)
    if candidate.symbol:
        query = query.filter(StrategyLifecycle.symbol == candidate.symbol)
    if candidate.side:
        query = query.filter(StrategyLifecycle.side == candidate.side)
    query = query.filter(
        (
            StrategyLifecycle.entry_signal_message_id == raw_message.message_id
        )
        | (
            StrategyLifecycle.exit_signal_message_id == raw_message.message_id
        )
        | (
            StrategyLifecycle.management_signal_message_id == raw_message.message_id
        )
    )
    return query.order_by(StrategyLifecycle.updated_at.desc(), StrategyLifecycle.id.desc()).first()


def _alert_type_for_candidate(
    candidate: SignalCandidate,
    lifecycle: StrategyLifecycle | None,
    *,
    order_type: str | None,
    management_action: str | None,
) -> str | None:
    if candidate.event_type == "entry_signal":
        if candidate.parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}:
            return "临时入场"
        return _entry_alert_type(order_type)
    if candidate.event_type == "close_signal":
        if lifecycle is not None and lifecycle.exit_reason == "cancelled":
            return "取消挂单"
        return "临时离场"
    if candidate.event_type == "position_update":
        return _management_alert_type(management_action)
    if candidate.event_type == "strategy_correction":
        return "策略参数调整"
    return None


def _strategy_kind_for_candidate(
    candidate: SignalCandidate,
    lifecycle: StrategyLifecycle | None,
) -> str:
    if candidate.event_type == "entry_signal":
        return "entry"
    if candidate.event_type == "close_signal":
        return "cancel_entry" if lifecycle is not None and lifecycle.exit_reason == "cancelled" else "exit"
    if candidate.event_type == "position_update":
        return "position_update"
    return candidate.event_type or "other"


def _format_lifecycle_entry_range(lifecycle: StrategyLifecycle) -> str | None:
    low = _format_number(lifecycle.entry_range_low)
    high = _format_number(lifecycle.entry_range_high)
    if not low and not high:
        return None
    if low and high and low != high:
        return f"{low}-{high}"
    return low or high


def _format_number(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:g}"


def _entry_alert_type(order_type: str | None) -> str:
    if order_type == "market":
        return "市价入场"
    if order_type == "market+limit":
        return "市价+限价入场"
    if order_type == "limit":
        return "限价入场"
    return "策略入场"


def _management_alert_type(management_action: str | None) -> str:
    normalized = (management_action or "").strip().lower()
    if "partial_take_profit" in normalized:
        return "部分止盈"
    if "move_stop_to_protect" in normalized:
        return "临时调整止损"
    if "risk_update" in normalized:
        return "调整止盈止损"
    if "hold_update" in normalized:
        return "持仓更新"
    if "strategy_correction" in normalized:
        return "策略参数调整"
    return "仓位管理"


def _format_management_action_label(management_action: str | None) -> str:
    if not management_action:
        return "-"
    label = _management_alert_type(management_action)
    return f"{label} ({management_action})"


def _format_order_type_label(order_type: str | None) -> str:
    return {
        "market": "市价",
        "limit": "限价",
        "market+limit": "市价+限价",
    }.get(order_type or "", "-")


def _resolve_order_type(
    raw_order_type: Any,
    entry_text: str | None,
    *,
    event_type: str,
    parse_source: str,
) -> str | None:
    normalized = _normalize_order_type(raw_order_type)
    if normalized is not None:
        return normalized
    if event_type == "entry_signal" and parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}:
        return "market"
    return _infer_order_type_from_entry(entry_text)


def _normalize_order_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower().replace("_", "-")
    text = text.replace(" ", "")
    if any(token in text for token in ["market+limit", "market/limit", "市价+限价", "市价/限价"]):
        return "market+limit"
    if any(token in text for token in ["market", "市价", "现价", "直接"]):
        if any(token in text for token in ["limit", "挂单", "限价"]):
            return "market+limit"
        return "market"
    if any(token in text for token in ["limit", "挂单", "限价"]):
        return "limit"
    return None


def _infer_order_type_from_entry(entry_text: str | None) -> str | None:
    if not entry_text:
        return None
    text = entry_text.strip().lower()
    has_market = any(token in text for token in ["market", "市价", "现价", "直接"])
    has_limit = any(token in text for token in ["limit", "挂单", "限价"])
    if has_market and has_limit:
        return "market+limit"
    if has_market:
        return "market"
    if has_limit or "-" in text or "/" in text:
        return "limit"
    return None


async def _retry_async(callback: Callable[[], Awaitable[Any]], *, attempts: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await callback()
        except Exception as exc:  # pragma: no cover - exact subclasses are injected in tests
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0)
    if last_error is not None:
        raise last_error
    raise RuntimeError("retry attempts must be positive")


def _get_or_create_alert(
    *,
    session_factory: sessionmaker,
    record: NormalizedMessageRecord,
    chat_title: str,
) -> str:
    with session_factory() as session:
        alert = (
            session.query(StrategyAlert)
            .filter(
                StrategyAlert.chat_id == record.chat_id,
                StrategyAlert.message_id == record.message_id,
            )
            .one_or_none()
        )
        if alert is not None:
            return alert.status

        raw_message = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == record.chat_id,
                RawMessage.message_id == record.message_id,
            )
            .one_or_none()
        )
        alert = StrategyAlert(
            chat_id=record.chat_id,
            message_id=record.message_id,
            raw_message_id=raw_message.id if raw_message is not None else None,
            chat_title=chat_title,
            sender_name=record.sender_name,
            original_text=record.text,
            status="pending",
        )
        session.add(alert)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            alert = (
                session.query(StrategyAlert)
                .filter(
                    StrategyAlert.chat_id == record.chat_id,
                    StrategyAlert.message_id == record.message_id,
                )
                .one()
            )
        return alert.status


def _update_alert(
    session_factory: sessionmaker,
    *,
    record: NormalizedMessageRecord,
    status: str,
    is_strategy: bool | None = None,
    strategy_kind: str | None = None,
    ai_confidence: float | None = None,
    kol_label: str | None = None,
    reason_short: str | None = None,
    error_message: str | None = None,
    forwarded_at: datetime | None = None,
) -> None:
    with session_factory() as session:
        alert = (
            session.query(StrategyAlert)
            .filter(
                StrategyAlert.chat_id == record.chat_id,
                StrategyAlert.message_id == record.message_id,
            )
            .one()
        )
        alert.status = status
        alert.is_strategy = is_strategy
        alert.strategy_kind = strategy_kind
        alert.ai_confidence = ai_confidence
        alert.kol_label = kol_label
        alert.reason_short = reason_short
        alert.error_message = error_message
        alert.forwarded_at = forwarded_at
        alert.updated_at = datetime.now(UTC)
        session.commit()
