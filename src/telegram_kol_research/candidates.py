"""Candidate confidence classification and report filtering helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate, Source
from telegram_kol_research.parsing.ocr_parser import (
    extract_text_from_image,
    image_signal_confidence,
    merge_caption_and_ocr_text,
)
from telegram_kol_research.parsing.text_parser import parse_signal_text
from telegram_kol_research.raw_ingest import NormalizedMessageRecord


@dataclass(slots=True)
class CandidateClassification:
    confidence: float
    review_status: str
    provenance: str = "text"


def classify_candidate(confidence: float, *, provenance: str = "text") -> CandidateClassification:
    """Assign a candidate review status from confidence and parse provenance."""

    if confidence >= 0.8:
        review_status = "confirmed"
    elif confidence >= 0.4:
        review_status = "pending"
    else:
        review_status = "rejected"
    return CandidateClassification(
        confidence=round(confidence, 2),
        review_status=review_status,
        provenance=provenance,
    )


def filter_strict_candidates(
    candidates: Iterable[CandidateClassification],
) -> list[CandidateClassification]:
    """Return only high-confidence confirmed candidates."""

    return [candidate for candidate in candidates if candidate.review_status == "confirmed"]


def filter_expanded_candidates(
    candidates: Iterable[CandidateClassification],
) -> list[CandidateClassification]:
    """Return candidates visible in the expanded report."""

    return [
        candidate
        for candidate in candidates
        if candidate.review_status in {"confirmed", "pending"}
    ]


def persist_text_signal_candidates(
    session_factory: sessionmaker,
    records: list[NormalizedMessageRecord],
) -> dict[str, int]:
    """Parse normalized text messages and persist signal candidates."""

    inserted_candidates = 0

    with session_factory() as session:
        for record in records:
            text_input = record.text.strip() if record.text else ""
            if not text_input and not record.media_path:
                continue

            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == record.chat_id,
                    RawMessage.message_id == record.message_id,
                )
                .one_or_none()
            )
            if raw_message is None:
                continue

            source = (
                session.query(Source)
                .filter(
                    Source.telegram_sender_id == record.sender_id,
                    Source.chat_id == record.chat_id,
                )
                .one_or_none()
            )
            if source is None:
                source = Source(
                    telegram_sender_id=record.sender_id,
                    chat_id=record.chat_id,
                    display_name=record.sender_name or str(record.sender_id or "unknown"),
                )
                session.add(source)
                session.flush()
            elif record.sender_name and source.display_name != record.sender_name:
                source.display_name = record.sender_name

            parsed = parse_signal_text(text_input) if text_input else None
            parse_source = "text"
            ocr_text = None

            if record.media_path:
                try:
                    ocr_text = extract_text_from_image(record.media_path)
                except RuntimeError:
                    ocr_text = None

                media_asset = (
                    session.query(MediaAsset)
                    .filter(
                        MediaAsset.raw_message_id == raw_message.id,
                        MediaAsset.local_path == record.media_path,
                    )
                    .order_by(MediaAsset.id.desc())
                    .first()
                )
                if media_asset is None:
                    media_asset = (
                        session.query(MediaAsset)
                        .filter(MediaAsset.raw_message_id == raw_message.id)
                        .order_by(MediaAsset.id.asc())
                        .first()
                    )
                if media_asset is not None and ocr_text is not None:
                    media_asset.ocr_text = ocr_text

                merged_text = merge_caption_and_ocr_text(record.text, ocr_text)
                if merged_text.strip():
                    ocr_parsed = parse_signal_text(merged_text)
                    ocr_parsed.confidence = image_signal_confidence(
                        ocr_parsed.confidence,
                        image_only=not bool(text_input),
                    )
                    if ocr_parsed.symbol and ocr_parsed.side:
                        parsed = ocr_parsed
                        parse_source = "text+ocr" if text_input else "ocr"

            if parsed is None or not parsed.symbol or not parsed.side:
                continue

            classification = classify_candidate(parsed.confidence, provenance=parse_source)
            existing_candidate = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == raw_message.id)
                .one_or_none()
            )

            entry_text = (
                f"{parsed.entry_range[0]:g}-{parsed.entry_range[1]:g}"
                if parsed.entry_range
                else None
            )
            stop_loss_text = f"{parsed.stop_loss:g}" if parsed.stop_loss is not None else None
            take_profit_text = (
                " / ".join(f"{take_profit:g}" for take_profit in parsed.take_profits)
                if parsed.take_profits
                else None
            )

            if existing_candidate is None:
                session.add(
                    SignalCandidate(
                        raw_message_id=raw_message.id,
                        source_id=source.id,
                        symbol=parsed.symbol,
                        side=parsed.side,
                        event_type=parsed.event_type,
                        entry_text=entry_text,
                        stop_loss_text=stop_loss_text,
                        take_profit_text=take_profit_text,
                        leverage_text=parsed.leverage,
                        parse_source=parse_source,
                        confidence=classification.confidence,
                        review_status=classification.review_status,
                    )
                )
                inserted_candidates += 1
            else:
                existing_candidate.source_id = source.id
                existing_candidate.symbol = parsed.symbol
                existing_candidate.side = parsed.side
                existing_candidate.event_type = parsed.event_type
                existing_candidate.entry_text = entry_text
                existing_candidate.stop_loss_text = stop_loss_text
                existing_candidate.take_profit_text = take_profit_text
                existing_candidate.leverage_text = parsed.leverage
                existing_candidate.parse_source = parse_source
                existing_candidate.confidence = classification.confidence
                existing_candidate.review_status = classification.review_status

        session.commit()

    return {"inserted_candidates": inserted_candidates, "processed_records": len(records)}
