"""Manual review workflow helpers for ambiguous signal candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RawMessage, SignalCandidate


def list_pending_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only candidates that still require manual review."""

    return [candidate for candidate in candidates if candidate.get("review_status") == "pending"]


def apply_review_decision(
    candidate: dict[str, Any],
    *,
    decision: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Apply a manual review decision while preserving an audit note."""

    if decision not in {"confirmed", "rejected"}:
        raise ValueError("decision must be 'confirmed' or 'rejected'")

    reviewed = dict(candidate)
    reviewed["review_status"] = decision
    if note:
        reviewed["review_note"] = note
    return reviewed


def load_candidates(path: str | Path) -> list[dict[str, Any]]:
    """Load candidate records from a local JSON file."""

    candidate_path = Path(path)
    if not candidate_path.exists():
        return []
    return json.loads(candidate_path.read_text(encoding="utf-8"))


def write_candidates(path: str | Path, candidates: list[dict[str, Any]]) -> Path:
    """Persist candidate records to a local JSON file."""

    candidate_path = Path(path)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    return candidate_path


def list_pending_candidates_from_db(session_factory: sessionmaker) -> list[dict[str, Any]]:
    """Return pending candidates from the SQLite database with message context."""

    with session_factory() as session:
        rows = (
            session.query(SignalCandidate, RawMessage)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .filter(SignalCandidate.review_status == "pending")
            .all()
        )

    return [
        {
            "id": candidate.id,
            "review_status": candidate.review_status,
            "review_note": candidate.review_note,
            "confidence": candidate.confidence,
            "symbol": candidate.symbol,
            "side": candidate.side,
            "source": raw_message.sender_name,
            "chat_id": raw_message.chat_id,
            "message_id": raw_message.message_id,
            "text": raw_message.text,
        }
        for candidate, raw_message in rows
    ]


def apply_review_decision_to_db(
    session_factory: sessionmaker,
    *,
    candidate_id: int,
    decision: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Apply a manual review decision directly to the SQLite database."""

    if decision not in {"confirmed", "rejected"}:
        raise ValueError("decision must be 'confirmed' or 'rejected'")

    with session_factory() as session:
        candidate = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.id == candidate_id)
            .one_or_none()
        )
        if candidate is None:
            raise LookupError(f"candidate_id {candidate_id} not found")

        candidate.review_status = decision
        if note is not None:
            candidate.review_note = note
        session.commit()
        session.refresh(candidate)

        return {
            "id": candidate.id,
            "review_status": candidate.review_status,
            "review_note": candidate.review_note,
        }
