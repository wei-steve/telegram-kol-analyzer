"""Durable source-order admission barrier for adjacent entry evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.adjacent_entry_assembly import (
    AdjacentEntryDecision,
    AdjacentEntryFact,
    EntryStrategyFact,
    SourceOrderKey,
    select_adjacent_entry_fragments,
    source_order_key,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    EntryStrategyFragment,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    RawMessage,
    SignalCandidate,
)


@dataclass(frozen=True, slots=True)
class EntryAdmissionDecision:
    status: str
    reason_code: str | None
    proposed_status: str
    cutoff: SourceOrderKey
    selection: AdjacentEntryDecision


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_source_facts(
    session,
    *,
    strategy: RawMessage,
    candidate: SignalCandidate,
    assessed_at: datetime,
) -> tuple[list[AdjacentEntryFact], SourceOrderKey]:
    raw_messages = (
        session.query(RawMessage)
        .filter(RawMessage.chat_id == int(strategy.chat_id))
        .all()
    )
    cutoff = max(
        (
            source_order_key(raw.posted_at, raw.message_id, raw.id)
            for raw in raw_messages
        ),
        default=source_order_key(strategy.posted_at, strategy.message_id, strategy.id),
    )
    raw_by_id = {
        int(raw.id): raw for raw in raw_messages if int(raw.id) != int(strategy.id)
    }
    if not raw_by_id:
        return [], cutoff
    raw_ids = tuple(raw_by_id)
    fragments = (
        session.query(EntryStrategyFragment)
        .filter(
            EntryStrategyFragment.raw_message_id.in_(raw_ids),
            EntryStrategyFragment.status == "pending",
        )
        .all()
    )
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id.in_(raw_ids))
        .all()
    )
    evidence_rows = (
        session.query(MessageEvidenceVersion)
        .filter(
            MessageEvidenceVersion.raw_message_id.in_(raw_ids),
            MessageEvidenceVersion.superseded_at.is_(None),
        )
        .order_by(
            MessageEvidenceVersion.raw_message_id.asc(),
            MessageEvidenceVersion.version.desc(),
        )
        .all()
    )
    evidence_by_raw: dict[int, MessageEvidenceVersion] = {}
    for evidence in evidence_rows:
        evidence_by_raw.setdefault(int(evidence.raw_message_id), evidence)
    active_claim_ids = {
        int(raw_id)
        for (raw_id,) in session.query(MessageEvidenceExtractionClaim.raw_message_id)
        .filter(
            MessageEvidenceExtractionClaim.raw_message_id.in_(raw_ids),
            MessageEvidenceExtractionClaim.lease_expires_at > assessed_at,
        )
        .all()
    }
    facts: list[AdjacentEntryFact] = []
    represented_raw_ids: set[int] = set()
    for fragment in fragments:
        raw = raw_by_id[int(fragment.raw_message_id)]
        try:
            payload = json.loads(fragment.payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        facts.append(
            AdjacentEntryFact(
                raw_message_id=int(raw.id),
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind="fragment",
                symbol=str(fragment.symbol),
                side=str(fragment.side),
                fragment_id=int(fragment.id),
                fragment_kind=str(fragment.fragment_kind),
                payload=payload if isinstance(payload, dict) else {},
                evidence_version_id=int(fragment.evidence_version_id),
            )
        )
        represented_raw_ids.add(int(raw.id))
    for other_candidate in candidates:
        raw = raw_by_id[int(other_candidate.raw_message_id)]
        if other_candidate.event_type == "entry_signal":
            other_side = str(other_candidate.side or "").lower()
            same_symbol = str(other_candidate.symbol or "").upper() == str(
                candidate.symbol or ""
            ).upper()
            kind = (
                "opposite_entry"
                if same_symbol and other_side != str(candidate.side or "").lower()
                else "complete_entry"
            )
        elif other_candidate.event_type == "strategy_revision":
            kind = "replacement"
        elif other_candidate.event_type == "close_signal" or (
            other_candidate.management_action in {"cancel_entry", "cancel"}
        ):
            kind = "cancel_entry"
        else:
            continue
        facts.append(
            AdjacentEntryFact(
                raw_message_id=int(raw.id),
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind=kind,
                symbol=other_candidate.symbol,
                side=other_candidate.side,
            )
        )
        represented_raw_ids.add(int(raw.id))
    for raw_id, raw in raw_by_id.items():
        if raw_id in active_claim_ids:
            facts.append(
                AdjacentEntryFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unresolved",
                )
            )
            continue
        evidence = evidence_by_raw.get(raw_id)
        if evidence is None and raw_id not in represented_raw_ids:
            facts.append(
                AdjacentEntryFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unresolved",
                )
            )
        elif evidence is not None and evidence.extraction_status not in {
            "completed",
            "failed",
            "expired",
        }:
            facts.append(
                AdjacentEntryFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unresolved",
                )
            )
        elif raw_id not in represented_raw_ids:
            facts.append(
                AdjacentEntryFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unrelated",
                )
            )
    return facts, cutoff


def _persist_attempt(
    session,
    *,
    strategy_raw_message_id: int,
    signal_candidate_id: int,
    candidate_generation: str,
    cutoff: SourceOrderKey,
    blocking_ids: list[int],
    mode: str,
    now: datetime,
) -> EntryAssemblyAttempt:
    fingerprint_payload = {
        "strategy_raw_message_id": int(strategy_raw_message_id),
        "candidate_generation": candidate_generation,
        "cutoff": [cutoff[0].isoformat(), cutoff[1], cutoff[2]],
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    existing = (
        session.query(EntryAssemblyAttempt)
        .filter(EntryAssemblyAttempt.fingerprint == fingerprint)
        .one_or_none()
    )
    desired_status = "shadow" if mode == "shadow" else "pending"
    blockers_json = _canonical_json(sorted(set(int(value) for value in blocking_ids)))
    if existing is not None:
        if existing.status in {"shadow", "pending"}:
            existing.status = desired_status
            existing.blocking_raw_message_ids_json = blockers_json
            existing.updated_at = now
        return existing
    attempt = EntryAssemblyAttempt(
        strategy_raw_message_id=int(strategy_raw_message_id),
        signal_candidate_id=int(signal_candidate_id),
        candidate_generation=candidate_generation,
        cutoff_posted_at=cutoff[0],
        cutoff_message_id=int(cutoff[1]),
        cutoff_raw_message_id=int(cutoff[2]),
        blocking_raw_message_ids_json=blockers_json,
        status=desired_status,
        fingerprint=fingerprint,
        created_at=now,
        updated_at=now,
    )
    session.add(attempt)
    session.flush()
    return attempt


def assess_entry_assembly_admission(
    session_factory: sessionmaker,
    *,
    strategy_raw_message_id: int,
    signal_candidate_id: int,
    mode: str,
    assessed_at: datetime,
) -> EntryAdmissionDecision:
    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("entry assembly admission mode must be disabled, shadow, or live")
    with session_factory() as session:
        strategy = session.get(RawMessage, int(strategy_raw_message_id))
        candidate = session.get(SignalCandidate, int(signal_candidate_id))
        if strategy is None or candidate is None:
            raise LookupError("strategy message or candidate not found")
        if int(candidate.raw_message_id) != int(strategy.id):
            raise ValueError("candidate does not belong to strategy message")
        facts, cutoff = _load_source_facts(
            session,
            strategy=strategy,
            candidate=candidate,
            assessed_at=assessed_at,
        )
        selection = select_adjacent_entry_fragments(
            strategy=EntryStrategyFact(
                raw_message_id=int(strategy.id),
                message_id=int(strategy.message_id),
                posted_at=strategy.posted_at,
                symbol=str(candidate.symbol or ""),
                side=str(candidate.side or ""),
            ),
            facts=facts,
            cutoff=cutoff,
        )
        proposed_status = "deferred" if selection.status == "pending" else selection.status
        if selection.status == "pending" and mode in {"shadow", "live"}:
            blocking_ids = list(selection.pending_raw_message_ids)
            _persist_attempt(
                session,
                strategy_raw_message_id=int(strategy.id),
                signal_candidate_id=int(candidate.id),
                candidate_generation=str(
                    candidate.recognition_generation or f"candidate:{candidate.id}"
                ),
                cutoff=cutoff,
                blocking_ids=blocking_ids,
                mode=mode,
                now=assessed_at,
            )
            session.commit()
        if mode == "live" and selection.status == "pending":
            return EntryAdmissionDecision(
                "deferred", selection.reason_code, proposed_status, cutoff, selection
            )
        if mode == "live" and selection.status == "blocked":
            return EntryAdmissionDecision(
                "blocked", selection.reason_code, proposed_status, cutoff, selection
            )
        return EntryAdmissionDecision(
            "ready", None, proposed_status, cutoff, selection
        )


def claim_ready_entry_assembly_wakeups(
    session_factory: sessionmaker,
    *,
    completed_raw_message_id: int,
    now: datetime,
    limit: int = 20,
) -> tuple[int, ...]:
    """Claim each strategy whose final unresolved source fact just became terminal."""

    claimed: list[int] = []
    with session_factory() as session:
        attempts = (
            session.query(EntryAssemblyAttempt)
            .filter(EntryAssemblyAttempt.status == "pending")
            .order_by(EntryAssemblyAttempt.id.asc())
            .limit(max(1, min(int(limit), 100)))
            .all()
        )
        for attempt in attempts:
            try:
                blockers = [
                    int(value)
                    for value in json.loads(attempt.blocking_raw_message_ids_json or "[]")
                ]
            except (TypeError, ValueError, json.JSONDecodeError):
                blockers = []
            if int(completed_raw_message_id) not in blockers:
                continue
            remaining = [
                value for value in blockers if value != int(completed_raw_message_id)
            ]
            attempt.blocking_raw_message_ids_json = _canonical_json(remaining)
            attempt.updated_at = now
            if remaining:
                continue
            claim_token = uuid.uuid4().hex
            result = session.execute(
                update(EntryAssemblyAttempt)
                .where(
                    EntryAssemblyAttempt.id == int(attempt.id),
                    EntryAssemblyAttempt.status == "pending",
                )
                .values(
                    status="claimed",
                    wake_claim_token=claim_token,
                    wake_claimed_at=now,
                    updated_at=now,
                )
            )
            if int(result.rowcount or 0) == 1:
                claimed.append(int(attempt.strategy_raw_message_id))
        session.commit()
    return tuple(claimed)


def finish_entry_assembly_wakeup(
    session_factory: sessionmaker,
    *,
    strategy_raw_message_id: int,
    succeeded: bool,
    now: datetime,
) -> None:
    with session_factory() as session:
        attempt = (
            session.query(EntryAssemblyAttempt)
            .filter(
                EntryAssemblyAttempt.strategy_raw_message_id
                == int(strategy_raw_message_id),
                EntryAssemblyAttempt.status == "claimed",
            )
            .order_by(EntryAssemblyAttempt.id.desc())
            .first()
        )
        if attempt is None:
            return
        attempt.status = "woken" if succeeded else "pending"
        attempt.woken_at = now if succeeded else None
        attempt.wake_claim_token = None
        attempt.wake_claimed_at = None
        attempt.updated_at = now
        session.commit()
