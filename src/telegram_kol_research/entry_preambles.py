"""Persistence helpers for authoritative, non-executable entry preambles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.message_evidence import (
    EntryPreambleEvidence,
    normalize_entry_preamble_evidence,
)
from telegram_kol_research.models import EntryPreamble, RawMessage


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _preamble_fingerprint(
    *,
    raw_message_id: int,
    evidence_version_id: int,
    recognition_generation: str,
    evidence: EntryPreambleEvidence,
) -> str:
    payload = {
        "raw_message_id": int(raw_message_id),
        "evidence_version_id": int(evidence_version_id),
        "recognition_generation": str(recognition_generation),
        "entry_context": evidence.to_dict(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def persist_entry_preamble_in_session(
    session: Session,
    *,
    raw_message: RawMessage,
    evidence_version_id: int,
    recognition_generation: str,
    evidence: EntryPreambleEvidence,
    now: datetime,
) -> EntryPreamble:
    """Persist validated authority evidence without owning the transaction."""

    fingerprint = _preamble_fingerprint(
        raw_message_id=int(raw_message.id),
        evidence_version_id=int(evidence_version_id),
        recognition_generation=str(recognition_generation),
        evidence=evidence,
    )
    existing = (
        session.query(EntryPreamble)
        .filter(EntryPreamble.fingerprint == fingerprint)
        .one_or_none()
    )
    if existing is not None:
        return existing
    row = EntryPreamble(
        raw_message_id=int(raw_message.id),
        chat_id=int(raw_message.chat_id),
        message_id=int(raw_message.message_id),
        symbol=evidence.symbol,
        side=evidence.side,
        risk_multiplier=evidence.to_dict()["risk_multiplier"],
        evidence_version_id=int(evidence_version_id),
        recognition_generation=str(recognition_generation),
        fingerprint=fingerprint,
        status="pending",
        reason=evidence.reason,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def invalidate_pending_entry_preamble_in_session(
    session: Session,
    *,
    raw_message_id: int,
    now: datetime,
) -> int:
    """Invalidate only mutable pending context for one source message."""

    result = session.execute(
        update(EntryPreamble)
        .where(
            EntryPreamble.raw_message_id == int(raw_message_id),
            EntryPreamble.status == "pending",
        )
        .values(status="invalidated", invalidated_at=now, updated_at=now)
    )
    return int(result.rowcount or 0)


def persist_authoritative_entry_preamble(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    evidence_version_id: int,
    recognition_generation: str,
    payload: Mapping[str, Any],
    mode: str,
    now: datetime,
) -> EntryPreamble | None:
    """Persist a validated fragment only when rollout collection is enabled."""

    if mode == "disabled":
        return None
    if mode not in {"shadow", "live"}:
        raise ValueError("entry preamble mode must be disabled, shadow, or live")
    evidence = normalize_entry_preamble_evidence(payload.get("entry_context"))
    if evidence is None:
        return None
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        row = persist_entry_preamble_in_session(
            session,
            raw_message=raw_message,
            evidence_version_id=int(evidence_version_id),
            recognition_generation=str(recognition_generation),
            evidence=evidence,
            now=now,
        )
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row
