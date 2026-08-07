"""Durable source-order admission barrier for adjacent entry evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
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
    EntryPreamble,
    EntryStrategyFragment,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)
from telegram_kol_research.message_evidence import (
    normalize_entry_strategy_fragments,
)


ADJACENT_ENTRY_MAX_AGE = timedelta(minutes=30)
ADJACENT_ENTRY_MAX_MESSAGES_PER_SIDE = 20


@dataclass(frozen=True, slots=True)
class EntryAdmissionDecision:
    status: str
    reason_code: str | None
    proposed_status: str
    cutoff: SourceOrderKey
    selection: AdjacentEntryDecision


@dataclass(frozen=True, slots=True)
class EntryAssemblyWakeClaim:
    attempt_id: int
    strategy_raw_message_id: int
    claim_token: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fragment_signature(
    kind: str,
    symbol: str,
    side: str,
    payload: object,
) -> tuple[str, str, str, str]:
    normalized_payload = payload if isinstance(payload, dict) else {}
    if kind == "risk_multiplier":
        try:
            multiplier = Decimal(str(normalized_payload.get("risk_multiplier")))
        except (InvalidOperation, TypeError, ValueError):
            pass
        else:
            if multiplier.is_finite():
                normalized_payload = {
                    "risk_multiplier": format(multiplier.normalize(), "f")
                }
    return (
        str(kind),
        str(symbol).upper(),
        str(side).lower(),
        _canonical_json(normalized_payload),
    )


def _load_source_facts(
    session,
    *,
    strategy: RawMessage,
    candidate: SignalCandidate,
    assessed_at: datetime,
) -> tuple[list[AdjacentEntryFact], SourceOrderKey]:
    base_query = session.query(RawMessage).filter(
        RawMessage.chat_id == int(strategy.chat_id),
        RawMessage.id != int(strategy.id),
    )
    if strategy.posted_at is not None:
        lower = strategy.posted_at - ADJACENT_ENTRY_MAX_AGE
        upper = strategy.posted_at + ADJACENT_ENTRY_MAX_AGE
        before_query = base_query.filter(
            RawMessage.posted_at >= lower,
            RawMessage.posted_at <= strategy.posted_at,
        )
        after_query = base_query.filter(
            RawMessage.posted_at >= strategy.posted_at,
            RawMessage.posted_at <= upper,
        )
    else:
        before_query = base_query.filter(
            RawMessage.message_id <= int(strategy.message_id)
        )
        after_query = base_query.filter(
            RawMessage.message_id >= int(strategy.message_id)
        )
    before_rows = (
        before_query.order_by(
            RawMessage.posted_at.desc(),
            RawMessage.message_id.desc(),
            RawMessage.id.desc(),
        )
        .limit(ADJACENT_ENTRY_MAX_MESSAGES_PER_SIDE)
        .all()
    )
    after_rows = (
        after_query.order_by(
            RawMessage.posted_at.asc(),
            RawMessage.message_id.asc(),
            RawMessage.id.asc(),
        )
        .limit(ADJACENT_ENTRY_MAX_MESSAGES_PER_SIDE)
        .all()
    )
    raw_messages = [strategy, *before_rows, *after_rows]
    raw_messages = list({int(row.id): row for row in raw_messages}.values())
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
    preambles = (
        session.query(EntryPreamble)
        .filter(
            EntryPreamble.raw_message_id.in_(raw_ids),
            EntryPreamble.status == "pending",
        )
        .all()
    )
    all_candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id.in_(raw_ids))
        .all()
    )
    instruction_items = (
        session.query(MessageInstructionItem)
        .filter(MessageInstructionItem.raw_message_id.in_(raw_ids))
        .all()
    )
    item_raw_ids = {int(item.raw_message_id) for item in instruction_items}
    current_item_candidate_ids = {
        int(item.signal_candidate_id)
        for item in instruction_items
        if item.retired_at is None
    }
    candidates = [
        row
        for row in all_candidates
        if int(row.raw_message_id) not in item_raw_ids
        or int(row.id) in current_item_candidate_ids
    ]
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
    fragment_signatures_by_evidence: dict[
        int, Counter[tuple[str, str, str, str]]
    ] = {}
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
        signatures = fragment_signatures_by_evidence.setdefault(
            int(fragment.evidence_version_id), Counter()
        )
        signatures[
            _fragment_signature(
                str(fragment.fragment_kind),
                str(fragment.symbol),
                str(fragment.side),
                payload,
            )
        ] += 1
    for preamble in preambles:
        raw = raw_by_id[int(preamble.raw_message_id)]
        facts.append(
            AdjacentEntryFact(
                raw_message_id=int(raw.id),
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind="fragment",
                symbol=str(preamble.symbol),
                side=str(preamble.side),
                fragment_id=-int(preamble.id),
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": str(preamble.risk_multiplier)},
                evidence_version_id=int(preamble.evidence_version_id),
            )
        )
        represented_raw_ids.add(int(raw.id))
        signatures = fragment_signatures_by_evidence.setdefault(
            int(preamble.evidence_version_id), Counter()
        )
        signatures[
            _fragment_signature(
                "risk_multiplier",
                str(preamble.symbol),
                str(preamble.side),
                {"risk_multiplier": str(preamble.risk_multiplier)},
            )
        ] += 1
    candidate_raw_ids = {int(row.raw_message_id) for row in candidates}
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
        elif evidence is not None and evidence.extraction_status == "completed":
            try:
                normalized = json.loads(evidence.normalized_evidence_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                normalized = None
            application_pending = not isinstance(normalized, dict)
            if isinstance(normalized, dict):
                expected_fragments = normalize_entry_strategy_fragments(
                    normalized.get("entry_fragments")
                )
                expected_fragment_signatures = Counter(
                    _fragment_signature(
                        str(fragment.kind),
                        str(fragment.symbol),
                        str(fragment.side),
                        fragment.payload,
                    )
                    for fragment in expected_fragments
                )
                persisted_fragment_signatures = fragment_signatures_by_evidence.get(
                    int(evidence.id), Counter()
                )
                fragment_application_pending = any(
                    persisted_fragment_signatures[signature] < count
                    for signature, count in expected_fragment_signatures.items()
                )
                lifecycle = normalized.get("lifecycle_event")
                action_expected = (
                    str(normalized.get("recognition_result") or "") == "是策略"
                    or bool(normalized.get("strategy"))
                    or (
                        isinstance(lifecycle, dict)
                        and str(lifecycle.get("event_type") or "none") != "none"
                    )
                )
                application_pending = (
                    fragment_application_pending
                    or (action_expected and raw_id not in candidate_raw_ids)
                )
            facts.append(
                AdjacentEntryFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unresolved" if application_pending else "unrelated",
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
    session.execute(
        update(EntryAssemblyAttempt)
        .where(
            EntryAssemblyAttempt.strategy_raw_message_id
            == int(strategy_raw_message_id),
            EntryAssemblyAttempt.candidate_generation == candidate_generation,
            EntryAssemblyAttempt.status.in_(("shadow", "pending")),
            EntryAssemblyAttempt.fingerprint != fingerprint,
        )
        .values(status="expired", updated_at=now)
    )
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
    try:
        with session.begin_nested():
            session.add(attempt)
            session.flush()
        return attempt
    except IntegrityError:
        existing = (
            session.query(EntryAssemblyAttempt)
            .filter(EntryAssemblyAttempt.fingerprint == fingerprint)
            .one()
        )
        if existing.status in {"shadow", "pending"}:
            existing.status = desired_status
            existing.blocking_raw_message_ids_json = blockers_json
            existing.updated_at = now
        return existing


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
) -> tuple[EntryAssemblyWakeClaim, ...]:
    """Claim each strategy whose final unresolved source fact just became terminal."""

    claimed: list[EntryAssemblyWakeClaim] = []
    with session_factory() as session:
        stale_before = now - timedelta(minutes=5)
        stale_attempt_ids = {
            int(row_id)
            for (row_id,) in session.query(EntryAssemblyAttempt.id)
            .filter(
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claimed_at <= stale_before,
            )
            .all()
        }
        session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claimed_at <= stale_before,
            )
            .values(
                status="pending",
                wake_claim_token=None,
                wake_claimed_at=None,
                updated_at=now,
            )
        )
        pending_rows = (
            session.query(EntryAssemblyAttempt.id)
            .filter(EntryAssemblyAttempt.status == "pending")
            .order_by(EntryAssemblyAttempt.id.asc())
            .all()
        )
        attempt_ids = [int(row_id) for (row_id,) in pending_rows]
        session.commit()
    claim_limit = max(1, min(int(limit), 100))
    for attempt_id in attempt_ids:
        if len(claimed) >= claim_limit:
            break
        for _ in range(3):
            with session_factory() as session:
                attempt = session.get(EntryAssemblyAttempt, int(attempt_id))
                if attempt is None or attempt.status != "pending":
                    break
                old_blockers_json = attempt.blocking_raw_message_ids_json or "[]"
                try:
                    blockers = [int(value) for value in json.loads(old_blockers_json)]
                except (TypeError, ValueError, json.JSONDecodeError):
                    blockers = []
                stale_recovery = int(attempt.id) in stale_attempt_ids
                if not stale_recovery and int(completed_raw_message_id) not in blockers:
                    break
                remaining = (
                    []
                    if stale_recovery
                    else [
                        value
                        for value in blockers
                        if value != int(completed_raw_message_id)
                    ]
                )
                if remaining:
                    result = session.execute(
                        update(EntryAssemblyAttempt)
                        .where(
                            EntryAssemblyAttempt.id == int(attempt.id),
                            EntryAssemblyAttempt.status == "pending",
                            EntryAssemblyAttempt.blocking_raw_message_ids_json
                            == old_blockers_json,
                        )
                        .values(
                            blocking_raw_message_ids_json=_canonical_json(remaining),
                            updated_at=now,
                        )
                    )
                    session.commit()
                    if int(result.rowcount or 0) == 1:
                        break
                    continue
                claim_token = uuid.uuid4().hex
                result = session.execute(
                    update(EntryAssemblyAttempt)
                    .where(
                        EntryAssemblyAttempt.id == int(attempt.id),
                        EntryAssemblyAttempt.status == "pending",
                        EntryAssemblyAttempt.blocking_raw_message_ids_json
                        == old_blockers_json,
                    )
                    .values(
                        status="claimed",
                        wake_claim_token=claim_token,
                        wake_claimed_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                if int(result.rowcount or 0) == 1:
                    claimed.append(
                        EntryAssemblyWakeClaim(
                            attempt_id=int(attempt.id),
                            strategy_raw_message_id=int(
                                attempt.strategy_raw_message_id
                            ),
                            claim_token=claim_token,
                        )
                    )
                    break
    return tuple(claimed)


def finish_entry_assembly_wakeup(
    session_factory: sessionmaker,
    *,
    attempt_id: int,
    claim_token: str,
    succeeded: bool,
    now: datetime,
) -> None:
    with session_factory() as session:
        result = session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.id == int(attempt_id),
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claim_token == str(claim_token),
            )
            .values(
                status="woken" if succeeded else "pending",
                woken_at=now if succeeded else None,
                wake_claim_token=None,
                wake_claimed_at=None,
                updated_at=now,
            )
        )
        session.commit()
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("entry assembly wake claim identity changed")
