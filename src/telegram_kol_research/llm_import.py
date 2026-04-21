"""Import LLM adjudication results back into the local research database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RawMessage, SignalCandidate, Source
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates


POSITIVE_CLASSIFICATIONS = {"entry_signal", "update_signal", "close_signal", "needs_review"}


def import_llm_adjudication_results(
    session_factory: sessionmaker,
    input_path: str | Path,
    *,
    confirmation_threshold: float = 0.8,
) -> dict[str, int]:
    """Import adjudication JSON and persist candidate/review updates."""

    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("LLM adjudication payload must include an 'items' array")

    processed_items = 0
    created_candidates = 0
    updated_candidates = 0
    rejected_candidates = 0

    with session_factory() as session:
        for item in items:
            raw_message_id = item.get("raw_message_id")
            if not isinstance(raw_message_id, int):
                raise ValueError("Each adjudication item must include integer raw_message_id")

            raw_message = (
                session.query(RawMessage)
                .filter(RawMessage.id == raw_message_id)
                .one_or_none()
            )
            if raw_message is None:
                raise LookupError(f"raw_message_id {raw_message_id} not found")

            existing_candidate = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == raw_message.id)
                .one_or_none()
            )

            classification = item.get("classification")
            signal_kind = item.get("signal_kind") or "unknown"
            confidence = float(item.get("confidence") or 0.0)
            needs_review = bool(item.get("needs_review"))
            normalized_signal = item.get("normalized_signal") or {}
            reasoning_short = item.get("reasoning_short") or ""

            if classification == "not_signal":
                if existing_candidate is not None:
                    existing_candidate.parse_source = "llm"
                    existing_candidate.review_status = "rejected"
                    existing_candidate.confidence = confidence
                    existing_candidate.event_type = signal_kind
                    existing_candidate.review_note = _build_review_note(
                        classification=classification,
                        signal_kind=signal_kind,
                        confidence=confidence,
                        reasoning_short=reasoning_short,
                    )
                    updated_candidates += 1
                    rejected_candidates += 1
                processed_items += 1
                continue

            if classification not in POSITIVE_CLASSIFICATIONS:
                raise ValueError(f"Unsupported classification: {classification}")

            source = _get_or_create_source(session, raw_message)
            review_status = _resolve_review_status(
                classification=classification,
                needs_review=needs_review,
                confidence=confidence,
                confirmation_threshold=confirmation_threshold,
            )
            note = _build_review_note(
                classification=classification,
                signal_kind=signal_kind,
                confidence=confidence,
                reasoning_short=reasoning_short,
            )

            if existing_candidate is None:
                session.add(
                    SignalCandidate(
                        raw_message_id=raw_message.id,
                        source_id=source.id if source is not None else None,
                        symbol=normalized_signal.get("symbol"),
                        side=normalized_signal.get("side"),
                        event_type=signal_kind,
                        entry_text=normalized_signal.get("entry_text"),
                        stop_loss_text=normalized_signal.get("stop_loss_text"),
                        take_profit_text=normalized_signal.get("take_profit_text"),
                        leverage_text=normalized_signal.get("leverage_text"),
                        parse_source="llm",
                        confidence=round(confidence, 2),
                        review_status=review_status,
                        review_note=note,
                    )
                )
                created_candidates += 1
            else:
                existing_candidate.source_id = source.id if source is not None else None
                existing_candidate.symbol = normalized_signal.get("symbol")
                existing_candidate.side = normalized_signal.get("side")
                existing_candidate.event_type = signal_kind
                existing_candidate.entry_text = normalized_signal.get("entry_text")
                existing_candidate.stop_loss_text = normalized_signal.get("stop_loss_text")
                existing_candidate.take_profit_text = normalized_signal.get("take_profit_text")
                existing_candidate.leverage_text = normalized_signal.get("leverage_text")
                existing_candidate.parse_source = "llm"
                existing_candidate.confidence = round(confidence, 2)
                existing_candidate.review_status = review_status
                existing_candidate.review_note = note
                updated_candidates += 1
                if review_status == "rejected":
                    rejected_candidates += 1

            processed_items += 1

        session.commit()

    trade_stats = persist_trade_ideas_from_candidates(session_factory)
    return {
        "processed_items": processed_items,
        "created_candidates": created_candidates,
        "updated_candidates": updated_candidates,
        "rejected_candidates": rejected_candidates,
        "inserted_trade_ideas": trade_stats["inserted_trade_ideas"],
        "inserted_trade_updates": trade_stats["inserted_trade_updates"],
    }


def _get_or_create_source(session, raw_message: RawMessage) -> Source:
    source = (
        session.query(Source)
        .filter(
            Source.telegram_sender_id == raw_message.sender_id,
            Source.chat_id == raw_message.chat_id,
        )
        .one_or_none()
    )
    if source is None:
        source = Source(
            telegram_sender_id=raw_message.sender_id,
            chat_id=raw_message.chat_id,
            display_name=raw_message.sender_name or str(raw_message.sender_id or "unknown"),
        )
        session.add(source)
        session.flush()
    elif raw_message.sender_name and source.display_name != raw_message.sender_name:
        source.display_name = raw_message.sender_name
    return source


def _resolve_review_status(
    *,
    classification: str,
    needs_review: bool,
    confidence: float,
    confirmation_threshold: float,
) -> str:
    if classification == "not_signal":
        return "rejected"
    if classification == "needs_review" or needs_review:
        return "pending"
    if confidence >= confirmation_threshold:
        return "confirmed"
    return "pending"


def _build_review_note(
    *,
    classification: str,
    signal_kind: str,
    confidence: float,
    reasoning_short: str,
) -> str:
    return (
        f"LLM adjudication [{classification}/{signal_kind}] "
        f"confidence={confidence:.2f}: {reasoning_short}"
    ).strip()
