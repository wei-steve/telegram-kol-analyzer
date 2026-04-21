"""Dataset export helpers for LLM-ready message adjudication."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate, Source, TradeIdea


def export_dataset_jsonl(
    session_factory: sessionmaker,
    output_path: str | Path,
    *,
    review_only: bool = False,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = False,
) -> Path:
    """Export raw-message-centered adjudication rows as JSONL."""

    with session_factory() as session:
        raw_messages = (
            session.query(RawMessage)
            .order_by(RawMessage.chat_id, RawMessage.message_id)
            .all()
        )

        exported_records: list[dict[str, Any]] = []
        for raw_message in raw_messages:
            media_assets = (
                session.query(MediaAsset)
                .filter(MediaAsset.raw_message_id == raw_message.id)
                .order_by(MediaAsset.id.asc())
                .all()
            )
            candidate = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == raw_message.id)
                .one_or_none()
            )

            source = None
            trade_idea = None
            if candidate is not None and candidate.source_id is not None:
                source = (
                    session.query(Source)
                    .filter(Source.id == candidate.source_id)
                    .one_or_none()
                )
                trade_idea = (
                    session.query(TradeIdea)
                    .filter(TradeIdea.primary_signal_candidate_id == candidate.id)
                    .one_or_none()
                )

            reply_context = None
            if raw_message.reply_to_message_id is not None:
                replied_message = (
                    session.query(RawMessage)
                    .filter(
                        RawMessage.chat_id == raw_message.chat_id,
                        RawMessage.message_id == raw_message.reply_to_message_id,
                    )
                    .one_or_none()
                )
                if replied_message is not None:
                    reply_context = {
                        "message_id": replied_message.message_id,
                        "sender_name": replied_message.sender_name,
                        "text": replied_message.text,
                    }

            record = {
                "raw_message_id": raw_message.id,
                "chat_id": raw_message.chat_id,
                "message_id": raw_message.message_id,
                "sender_id": raw_message.sender_id,
                "sender_name": raw_message.sender_name,
                "posted_at": raw_message.posted_at.isoformat() if raw_message.posted_at else None,
                "edit_date": raw_message.edit_date.isoformat() if raw_message.edit_date else None,
                "text": raw_message.text,
                "raw_payload": _loads_json(raw_message.raw_payload),
                "reply_to_message_id": raw_message.reply_to_message_id,
                "reply_context": reply_context,
                "media_assets": [
                    {
                        "id": media_asset.id,
                        "kind": media_asset.kind,
                        "mime_type": media_asset.mime_type,
                        "local_path": media_asset.local_path,
                        "ocr_text": media_asset.ocr_text,
                    }
                    for media_asset in media_assets
                ],
                "candidate": _serialize_candidate(candidate),
                "source": _serialize_source(source),
                "trade_idea": _serialize_trade_idea(trade_idea),
            }
            if review_only and not _should_export_for_review(candidate, confidence_threshold):
                continue
            if signal_like_only and not _looks_signal_like(record):
                continue

            exported_records.append(record)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in exported_records)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
    return path


def _loads_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _serialize_candidate(candidate: SignalCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "id": candidate.id,
        "source_id": candidate.source_id,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "event_type": candidate.event_type,
        "entry_text": candidate.entry_text,
        "stop_loss_text": candidate.stop_loss_text,
        "take_profit_text": candidate.take_profit_text,
        "leverage_text": candidate.leverage_text,
        "parse_source": candidate.parse_source,
        "confidence": candidate.confidence,
        "review_status": candidate.review_status,
        "review_note": candidate.review_note,
    }


def _serialize_source(source: Source | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "id": source.id,
        "telegram_sender_id": source.telegram_sender_id,
        "chat_id": source.chat_id,
        "username": source.username,
        "display_name": source.display_name,
        "custom_label": source.custom_label,
    }


def _serialize_trade_idea(trade_idea: TradeIdea | None) -> dict[str, Any] | None:
    if trade_idea is None:
        return None
    return {
        "id": trade_idea.id,
        "source_id": trade_idea.source_id,
        "chat_id": trade_idea.chat_id,
        "symbol": trade_idea.symbol,
        "side": trade_idea.side,
        "status": trade_idea.status,
        "confidence": trade_idea.confidence,
        "opened_at": trade_idea.opened_at.isoformat() if trade_idea.opened_at else None,
        "closed_at": trade_idea.closed_at.isoformat() if trade_idea.closed_at else None,
        "pnl_r_multiple": trade_idea.pnl_r_multiple,
    }


def _should_export_for_review(
    candidate: SignalCandidate | None,
    confidence_threshold: float,
) -> bool:
    if candidate is None:
        return True
    if candidate.review_status == "pending":
        return True
    return candidate.confidence < confidence_threshold


def _looks_signal_like(record: dict[str, Any]) -> bool:
    return _signal_score(record) >= 4


def _signal_score(record: dict[str, Any]) -> int:
    if record.get("candidate") is not None:
        return 5

    media_assets = record.get("media_assets") or []
    score = 0
    if record.get("reply_context") is not None:
        score += 2
    if any((asset.get("ocr_text") or "").strip() for asset in media_assets):
        score += 2

    text = _normalize_signal_text(record.get("text") or "")
    if not text:
        return score

    lowered = text.lower()
    asset_patterns = [
        r"[#$][A-Za-z]{2,10}\b",
        r"\b(?:btc|eth|sol|xrp|zec|tao|xmr|gold|silver)\b",
        r"(?:比特币|以太币|白银|黄金)",
    ]
    action_patterns = [
        r"\b(?:long|short|buy|sell|bullish|bearish|add more|adding|take off|bounce|reclaim)\b",
        r"(?:做多|做空|多单|空单|加仓|补仓|开仓|平仓|止盈|止损|抄底|看涨|看空|爆仓|反弹|破位|支撑|压力)",
    ]
    price_patterns = [
        r"\d+\s*-\s*\d+",
        r"\d+(?:\.\d+)?",
    ]
    noise_patterns = [
        r"(?:加入会员|直播教学|具体可以咨询助理|都是骗子|premium channel|vip group)",
    ]

    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in asset_patterns):
        score += 2
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in action_patterns):
        score += 2
    if any(marker in text for marker in ("方向：", "建仓：", "止损：", "止盈：", "进场点位：")):
        score += 2
    price_matches = sum(
        len(re.findall(pattern, lowered, flags=re.IGNORECASE))
        for pattern in price_patterns
    )
    if price_matches >= 2:
        score += 1
    elif price_matches == 1 and score >= 2:
        score += 1

    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in noise_patterns):
        score -= 2

    return score


def _normalize_signal_text(text: str) -> str:
    normalized_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("________________"):
            continue
        if re.search(r"QQ:\d+", stripped):
            stripped = re.sub(r"@?\S*\s*QQ:\d+", "", stripped).strip()
        if stripped:
            normalized_lines.append(stripped)
    return "\n".join(normalized_lines)
