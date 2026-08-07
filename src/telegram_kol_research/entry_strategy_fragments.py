"""Persistence for authoritative, non-executable adjacent entry fragments."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.message_evidence import (
    EntryStrategyFragmentEvidence,
    normalize_entry_strategy_fragments,
)
from telegram_kol_research.models import EntryStrategyFragment, RawMessage


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(
    *,
    raw_message_id: int,
    evidence_version_id: int,
    recognition_generation: str,
    evidence: EntryStrategyFragmentEvidence,
) -> str:
    payload = {
        "raw_message_id": int(raw_message_id),
        "evidence_version_id": int(evidence_version_id),
        "recognition_generation": str(recognition_generation),
        "fragment": evidence.to_dict(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def persist_authoritative_entry_fragments(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    evidence_version_id: int,
    recognition_generation: str,
    payload: Mapping[str, Any],
    mode: str,
    now: datetime,
) -> tuple[EntryStrategyFragment, ...]:
    """Persist one current fragment set without owning any execution action."""

    if mode == "disabled":
        return ()
    if mode not in {"shadow", "live"}:
        raise ValueError(
            "entry message assembly v2 mode must be disabled, shadow, or live"
        )
    lifecycle = payload.get("lifecycle_event")
    if not isinstance(lifecycle, Mapping) or str(
        lifecycle.get("event_type") or "none"
    ) != "none":
        return ()
    evidence_rows = normalize_entry_strategy_fragments(payload.get("entry_fragments"))
    fingerprints = [
        _fingerprint(
            raw_message_id=raw_message_id,
            evidence_version_id=evidence_version_id,
            recognition_generation=recognition_generation,
            evidence=evidence,
        )
        for evidence in evidence_rows
    ]
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        invalidation = update(EntryStrategyFragment).where(
            EntryStrategyFragment.raw_message_id == int(raw_message_id),
            EntryStrategyFragment.status == "pending",
        )
        if fingerprints:
            invalidation = invalidation.where(
                ~EntryStrategyFragment.fingerprint.in_(fingerprints)
            )
        session.execute(
            invalidation.values(
                status="invalidated",
                invalidated_at=now,
                updated_at=now,
            )
        )
        rows: list[EntryStrategyFragment] = []
        for evidence, fingerprint in zip(evidence_rows, fingerprints, strict=True):
            existing = (
                session.query(EntryStrategyFragment)
                .filter(EntryStrategyFragment.fingerprint == fingerprint)
                .one_or_none()
            )
            if existing is not None:
                rows.append(existing)
                continue
            row = EntryStrategyFragment(
                raw_message_id=int(raw_message.id),
                chat_id=int(raw_message.chat_id),
                message_id=int(raw_message.message_id),
                symbol=evidence.symbol,
                side=evidence.side,
                fragment_kind=evidence.kind,
                payload_json=_canonical_json(evidence.payload),
                evidence_version_id=int(evidence_version_id),
                recognition_generation=str(recognition_generation),
                source_relationship="unresolved",
                status="pending",
                reason=evidence.reason,
                fingerprint=fingerprint,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            rows.append(row)
        session.commit()
        for row in rows:
            session.refresh(row)
            session.expunge(row)
        return tuple(rows)
