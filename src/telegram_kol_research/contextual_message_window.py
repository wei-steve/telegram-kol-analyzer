"""Build bounded message and Telegram reply context for AI resolution."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    MessageEvidenceVersion,
    RawMessage,
    StrategyLifecycle,
    StrategyMessageLink,
)
from telegram_kol_research.raw_ingest import (
    normalize_message_payload,
    persist_normalized_messages,
)
from telegram_kol_research.telegram_client import (
    _download_media_if_present,
    _format_sender_name,
    maybe_await,
)


@dataclass(frozen=True, slots=True)
class StrategyLinkContext:
    strategy_thread_id: int
    relation_kind: str
    status: str
    confidence: float
    message_evidence_version_id: int | None


@dataclass(frozen=True, slots=True)
class ContextMessage:
    raw_message_id: int | None
    message_id: int
    posted_at: str | None
    text: str | None
    reply_to_message_id: int | None
    evidence_version_id: int | None
    normalized_evidence: dict[str, Any]
    text_evidence: dict[str, Any]
    image_evidence: dict[str, Any]
    resolution_status: str = "resolved"
    strategy_links: tuple[StrategyLinkContext, ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveStrategyContext:
    lifecycle_id: int
    strategy_thread_id: int | None
    source_message_id: int
    signal_at: str
    entered_at: str | None
    symbol: str
    side: str
    status: str
    entry_range_low: float | None
    entry_range_high: float | None
    stop_loss: float | None
    take_profit: str | None
    execution_binding_id: int | None


@dataclass(frozen=True, slots=True)
class ContextualMessageWindow:
    current: ContextMessage
    messages: tuple[ContextMessage, ...]
    reply_chain: tuple[ContextMessage, ...]
    active_strategies: tuple[ActiveStrategyContext, ...]
    errors: tuple[str, ...]


async def fetch_missing_reply_target(
    telegram_client,
    *,
    session_factory,
    chat_id: int,
    message_id: int,
    media_root,
    broker=None,
) -> bool:
    """Fetch and persist one exact missing reply target through raw ingest."""

    with session_factory() as session:
        existing = (
            session.query(RawMessage.id)
            .filter(
                RawMessage.chat_id == int(chat_id),
                RawMessage.message_id == int(message_id),
            )
            .first()
        )
    if existing is not None:
        return True
    get_messages = getattr(telegram_client, "get_messages", None)
    if not callable(get_messages):
        return False
    message = await maybe_await(
        get_messages(int(chat_id), ids=int(message_id))
    )
    if isinstance(message, (list, tuple)):
        message = message[0] if message else None
    if message is None or getattr(message, "id", None) is None:
        return False
    sender = None
    get_sender = getattr(message, "get_sender", None)
    if callable(get_sender):
        sender = await maybe_await(get_sender())
    media_path = None
    if getattr(message, "media", None) is not None:
        media_path = await _download_media_if_present(
            telegram_client,
            dialog_id=int(chat_id),
            message=message,
            media_root=Path(media_root),
        )
    payload = {
        "chat_id": int(chat_id),
        "message_id": int(message.id),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _format_sender_name(sender),
        "text": getattr(message, "message", None),
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "posted_at": getattr(message, "date", None),
        "edit_date": getattr(message, "edit_date", None),
        "media": (
            {
                "kind": type(message.media).__name__.lower(),
                "path": media_path,
            }
            if getattr(message, "media", None) is not None
            else None
        ),
    }
    record = normalize_message_payload(payload, archived_target_group=True)
    persist_normalized_messages(
        session_factory,
        [record],
        sync_kind="reply_recovery",
        broker=broker,
    )
    return True


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _current_evidence(
    session: Session,
    raw_message_id: int,
) -> MessageEvidenceVersion | None:
    return (
        session.query(MessageEvidenceVersion)
        .filter(
            MessageEvidenceVersion.raw_message_id == int(raw_message_id),
            MessageEvidenceVersion.superseded_at.is_(None),
        )
        .order_by(MessageEvidenceVersion.version.desc())
        .first()
    )


def _message_context(
    session: Session,
    row: RawMessage,
) -> ContextMessage:
    evidence = _current_evidence(session, int(row.id))
    normalized: dict[str, Any] = {}
    text_evidence: dict[str, Any] = {}
    image_evidence: dict[str, Any] = {}
    if evidence is not None:
        normalized = _json_object(evidence.normalized_evidence_json)
        text_evidence = _json_object(evidence.text_evidence_json)
        image_evidence = _json_object(evidence.image_evidence_json)
    links = (
        session.query(StrategyMessageLink)
        .filter(
            StrategyMessageLink.raw_message_id == int(row.id),
            StrategyMessageLink.status == "active",
        )
        .order_by(
            StrategyMessageLink.strategy_thread_id.asc(),
            StrategyMessageLink.id.asc(),
        )
        .all()
    )
    return ContextMessage(
        raw_message_id=int(row.id),
        message_id=int(row.message_id),
        posted_at=_iso(row.posted_at),
        text=row.text,
        reply_to_message_id=row.reply_to_message_id,
        evidence_version_id=(int(evidence.id) if evidence is not None else None),
        normalized_evidence=normalized,
        text_evidence=text_evidence,
        image_evidence=image_evidence,
        strategy_links=tuple(
            StrategyLinkContext(
                strategy_thread_id=int(link.strategy_thread_id),
                relation_kind=str(link.relation_kind),
                status=str(link.status),
                confidence=float(link.confidence),
                message_evidence_version_id=(
                    int(link.message_evidence_version_id)
                    if link.message_evidence_version_id is not None
                    else None
                ),
            )
            for link in links
        ),
    )


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_contextual_message_window(
    session: Session,
    *,
    raw_message_id: int,
    max_age_hours: int = 72,
    max_messages: int = 50,
    max_reply_depth: int = 5,
) -> ContextualMessageWindow:
    """Build one bounded, chronological, reply-aware context window."""

    if max_age_hours < 1 or max_messages < 1 or max_reply_depth < 1:
        raise ValueError("context window bounds must be positive")
    current = session.get(RawMessage, int(raw_message_id))
    if current is None:
        raise LookupError("raw message not found")

    recent_query = (
        session.query(RawMessage)
        .filter(
            RawMessage.chat_id == int(current.chat_id),
            RawMessage.id != int(current.id),
        )
    )
    if current.posted_at is not None:
        recent_query = recent_query.filter(
            RawMessage.posted_at >= (
                current.posted_at - timedelta(hours=max_age_hours)
            ),
            RawMessage.posted_at <= current.posted_at,
        )
    recent_rows = (
        recent_query.order_by(
            RawMessage.posted_at.desc(),
            RawMessage.message_id.desc(),
        )
        .limit(max_messages)
        .all()
    )
    messages = tuple(
        _message_context(session, row)
        for row in reversed(recent_rows)
    )

    errors: list[str] = []
    reply_chain: list[ContextMessage] = []
    next_message_id = current.reply_to_message_id
    seen = {int(current.message_id)}
    depth = 0
    while next_message_id is not None and depth < max_reply_depth:
        next_id = int(next_message_id)
        if next_id in seen:
            errors.append("reply_cycle_detected")
            break
        seen.add(next_id)
        target = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == int(current.chat_id),
                RawMessage.message_id == next_id,
            )
            .one_or_none()
        )
        if target is None:
            reply_chain.append(
                ContextMessage(
                    raw_message_id=None,
                    message_id=next_id,
                    posted_at=None,
                    text=None,
                    reply_to_message_id=None,
                    evidence_version_id=None,
                    normalized_evidence={},
                    text_evidence={},
                    image_evidence={},
                    resolution_status="missing",
                )
            )
            errors.append("reply_target_unavailable")
            break
        reply_chain.append(_message_context(session, target))
        next_message_id = target.reply_to_message_id
        depth += 1
    if next_message_id is not None and depth >= max_reply_depth:
        errors.append("reply_depth_exceeded")

    active_rows = (
        session.query(StrategyLifecycle)
        .filter(
            StrategyLifecycle.chat_id == int(current.chat_id),
            StrategyLifecycle.lifecycle_status.in_(
                ("pending_entry", "entered", "holding", "expired")
            ),
        )
        .order_by(StrategyLifecycle.signal_at.asc(), StrategyLifecycle.id.asc())
        .limit(50)
        .all()
    )
    active_strategies = tuple(
        ActiveStrategyContext(
            lifecycle_id=int(row.id),
            strategy_thread_id=(
                int(row.strategy_thread_id)
                if row.strategy_thread_id is not None
                else None
            ),
            source_message_id=int(row.message_id),
            signal_at=_iso(row.signal_at) or "",
            entered_at=_iso(row.entered_at),
            symbol=str(row.symbol),
            side=str(row.side),
            status=str(row.lifecycle_status),
            entry_range_low=row.entry_range_low,
            entry_range_high=row.entry_range_high,
            stop_loss=row.stop_loss,
            take_profit=row.take_profit,
            execution_binding_id=(
                int(row.execution_binding_id)
                if row.execution_binding_id is not None
                else None
            ),
        )
        for row in active_rows
    )
    return ContextualMessageWindow(
        current=_message_context(session, current),
        messages=messages,
        reply_chain=tuple(reply_chain),
        active_strategies=active_strategies,
        errors=tuple(dict.fromkeys(errors)),
    )


def render_authoritative_context(window: ContextualMessageWindow) -> str:
    """Render the typed window for the existing authoritative prompt."""

    recent_rows = [asdict(row) for row in window.messages]
    reply_rows = [asdict(row) for row in window.reply_chain]
    active_rows = [asdict(row) for row in window.active_strategies]
    return "\n".join(
        [
            "Current message context:",
            json.dumps(
                asdict(window.current),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "Recent context:",
            json.dumps(recent_rows, ensure_ascii=False, sort_keys=True),
            "Reply context:",
            json.dumps(reply_rows, ensure_ascii=False, sort_keys=True),
            "Active strategies:",
            json.dumps(active_rows, ensure_ascii=False, sort_keys=True),
            "Context errors:",
            json.dumps(list(window.errors), ensure_ascii=False, sort_keys=True),
        ]
    )
