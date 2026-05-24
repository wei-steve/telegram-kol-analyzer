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
from telegram_kol_research.models import RawMessage, StrategyAlert
from telegram_kol_research.raw_ingest import NormalizedMessageRecord


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
    return "\n".join(
        [
            "Classify one Telegram trading-group message.",
            "Goal: identify entry or exit strategy messages. Prefer recall over precision.",
            "Extract kol_label from the first line when it names a trader; otherwise use an empty string.",
            'strategy_kind must be one of: "entry", "exit", "other".',
            "confidence must be a number from 0 to 1, for example 0.85. Do not return words like high, medium, low, 高, 中, or 低.",
            "Return compact JSON only with keys: is_strategy, strategy_kind, confidence, kol_label, reason_short.",
            f"chat_title={chat_title}",
            f"sender_name={sender_name or ''}",
            f"first_line={first_line}",
            "message_text:",
            trimmed_text,
        ]
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

    prompt = build_strategy_alert_prompt(
        chat_title=chat_title,
        sender_name=record.sender_name,
        text=text,
        max_chars=config.max_chars,
    )
    try:
        content = await _retry_async(
            lambda: llm_requester(config=config, prompt=prompt),
            attempts=3,
        )
    except Exception as exc:
        _update_alert(
            session_factory,
            record=record,
            status="ai_failed",
            error_message=str(exc),
        )
        return {"status": "ai_failed", "error": str(exc)}

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
