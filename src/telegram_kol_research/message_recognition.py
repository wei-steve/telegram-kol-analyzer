"""Immediate message-level strategy recognition."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MediaAsset,
    MessageRecognition,
    RawMessage,
    SignalCandidate,
    utc_now,
)
from telegram_kol_research.parsing.text_parser import parse_signal_text


BLOCKED_SYMBOLS = {
    "QQ",
    "VX",
    "WX",
    "VIP",
    "HTTP",
    "HTTPS",
}

ENTRY_TERMS = [
    "建仓",
    "入场",
    "进场",
    "挂单",
    "现价开",
    "市价",
    "开仓",
    "open",
    "entry",
    "enter",
]

POSITION_MANAGEMENT_TERMS = [
    "继续持有",
    "持有",
    "浮亏",
    "浮盈",
    "上推",
    "保护价",
    "分批止盈",
    "减仓",
    "补仓",
    "别追",
    "不要逆势加仓",
]


@dataclass(frozen=True)
class MessageRecognitionResult:
    raw_message_id: int
    status: str
    summary: str | None = None
    reason: str | None = None


def recognize_message_now(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> MessageRecognitionResult:
    """Run V1 immediate recognition for one raw message and persist the result."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")

        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
        if _has_video_like_media(media_assets):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="视频消息默认跳过",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        if _has_image_like_media(media_assets) and not (raw_message.text or "").strip():
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="待识别",
                reason="图片识别等待 OCR/AI 接入",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        parsed = parse_signal_text(raw_message.text or "")
        if not _is_actionable_entry_signal(raw_message.text or "", parsed):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="未识别到可执行新入场策略",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        candidate = _upsert_signal_candidate(session, raw_message, parsed)
        result = MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="是策略",
            summary=_format_candidate_summary(candidate),
        )
        _upsert_recognition(session, result)
        session.commit()
        return result


def _upsert_signal_candidate(session, raw_message: RawMessage, parsed) -> SignalCandidate:
    candidate = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .order_by(SignalCandidate.id.asc())
        .first()
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source="text",
            review_status="pending",
        )
        session.add(candidate)

    candidate.symbol = parsed.symbol
    candidate.side = parsed.side
    candidate.event_type = parsed.event_type
    candidate.entry_text = _format_entry_range(parsed.entry_range)
    candidate.stop_loss_text = _format_number(parsed.stop_loss)
    candidate.take_profit_text = " / ".join(
        _format_number(value) for value in parsed.take_profits
    ) or None
    candidate.leverage_text = parsed.leverage
    candidate.confidence = parsed.confidence
    return candidate


def _is_actionable_entry_signal(text: str, parsed) -> bool:
    if parsed.confidence < 0.4:
        return False
    if parsed.event_type != "entry_signal":
        return False
    if not parsed.symbol or parsed.symbol.upper() in BLOCKED_SYMBOLS:
        return False
    if not parsed.side:
        return False
    if not _has_entry_instruction(text, parsed):
        return False
    if parsed.stop_loss is None and not parsed.take_profits:
        return False
    if _looks_like_position_management(text) and not _has_entry_instruction(text, parsed):
        return False
    return True


def _has_entry_instruction(text: str, parsed) -> bool:
    if parsed.entry_range is not None:
        return True
    lowered = text.lower()
    return any(term in text or term in lowered for term in ENTRY_TERMS)


def _looks_like_position_management(text: str) -> bool:
    return any(term in text for term in POSITION_MANAGEMENT_TERMS)


def _upsert_recognition(session, result: MessageRecognitionResult) -> MessageRecognition:
    recognition = (
        session.query(MessageRecognition)
        .filter(MessageRecognition.raw_message_id == result.raw_message_id)
        .one_or_none()
    )
    if recognition is None:
        recognition = MessageRecognition(raw_message_id=result.raw_message_id, status=result.status)
        session.add(recognition)
    recognition.status = result.status
    recognition.reason = result.reason
    recognition.summary = result.summary
    recognition.engine = "local_rule_parser"
    recognition.updated_at = utc_now()
    return recognition


def _format_candidate_summary(candidate: SignalCandidate) -> str:
    parts: list[str] = []
    symbol_side = " ".join(value for value in [candidate.symbol, candidate.side] if value)
    if symbol_side:
        parts.append(symbol_side)
    if candidate.entry_text:
        parts.append(f"Entry {candidate.entry_text}")
    if candidate.stop_loss_text:
        parts.append(f"SL {candidate.stop_loss_text}")
    if candidate.take_profit_text:
        parts.append(f"TP {candidate.take_profit_text}")
    if candidate.leverage_text:
        parts.append(candidate.leverage_text)
    return "；".join(parts) or "已命中策略候选"


def _format_entry_range(entry_range: tuple[float, float] | None) -> str | None:
    if entry_range is None:
        return None
    return f"{_format_number(entry_range[0])}-{_format_number(entry_range[1])}"


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _has_video_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "video" in media_kind or "document" in media_kind or mime_type.startswith("video/"):
            return True
    return False


def _has_image_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "photo" in media_kind or "image" in media_kind or mime_type.startswith("image/"):
            return True
    return False
