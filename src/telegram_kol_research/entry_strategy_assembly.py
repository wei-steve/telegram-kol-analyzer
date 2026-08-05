"""Deterministically attach one prior sizing preamble to an entry strategy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    EntryPreamble,
    EntryStrategyAssembly,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    RawMessage,
    SignalCandidate,
)


HARD_BOUNDARY_KINDS = frozenset(
    {"complete_entry", "cancel_entry", "opposite_entry", "replacement"}
)


@dataclass(frozen=True, slots=True)
class PriorMessageFact:
    raw_message_id: int
    message_id: int
    posted_at: datetime | None
    kind: str
    symbol: str | None = None
    side: str | None = None
    preamble_id: int | None = None
    risk_multiplier: Decimal = Decimal("1")


@dataclass(frozen=True, slots=True)
class EntryAssemblyDecision:
    status: str
    reason_code: str | None
    preamble_id: int | None
    risk_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class EntryAssemblyResult:
    status: str
    reason_code: str | None
    mode: str
    proposed_risk_multiplier: Decimal
    effective_risk_multiplier: Decimal
    preamble_id: int | None = None
    preamble_message_id: int | None = None
    strategy_message_id: int | None = None
    assembly_id: int | None = None
    assembly_fingerprint: str | None = None


def _source_key(
    posted_at: datetime | None,
    message_id: int,
    raw_message_id: int,
) -> tuple[datetime, int, int]:
    value = posted_at or datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value, int(message_id), int(raw_message_id)


def select_entry_preamble(
    *,
    strategy_posted_at: datetime | None,
    strategy_message_id: int,
    strategy_raw_message_id: int,
    symbol: str,
    side: str,
    prior_facts: list[PriorMessageFact],
) -> EntryAssemblyDecision:
    """Select from normalized prior facts using Telegram source order only."""

    strategy_key = _source_key(
        strategy_posted_at, strategy_message_id, strategy_raw_message_id
    )
    normalized_symbol = str(symbol).strip().upper()
    normalized_side = str(side).strip().lower()
    candidates: list[PriorMessageFact] = []
    unresolved = False
    for fact in sorted(
        prior_facts,
        key=lambda item: _source_key(
            item.posted_at, item.message_id, item.raw_message_id
        ),
    ):
        if _source_key(fact.posted_at, fact.message_id, fact.raw_message_id) >= strategy_key:
            continue
        if fact.kind == "unrelated":
            continue
        if fact.kind == "unresolved":
            unresolved = True
            continue
        if fact.kind in HARD_BOUNDARY_KINDS:
            candidates.clear()
            unresolved = False
            continue
        if fact.kind != "entry_preamble":
            continue
        if (
            str(fact.symbol or "").upper() != normalized_symbol
            or str(fact.side or "").lower() != normalized_side
        ):
            candidates.clear()
            continue
        if (
            not fact.risk_multiplier.is_finite()
            or fact.risk_multiplier <= Decimal("0")
            or fact.risk_multiplier > Decimal("1")
        ):
            return EntryAssemblyDecision(
                "blocked",
                "entry_preamble_multiplier_invalid",
                None,
                Decimal("1"),
            )
        candidates.append(fact)

    if unresolved:
        return EntryAssemblyDecision(
            status="unresolved",
            reason_code="preceding_entry_context_unresolved",
            preamble_id=None,
            risk_multiplier=Decimal("1"),
        )
    if not candidates:
        return EntryAssemblyDecision("none", None, None, Decimal("1"))
    if len(candidates) != 1:
        return EntryAssemblyDecision(
            "blocked",
            "entry_preamble_ambiguous",
            None,
            Decimal("1"),
        )
    selected = candidates[0]
    return EntryAssemblyDecision(
        "ready",
        None,
        selected.preamble_id,
        selected.risk_multiplier,
    )


def _load_prior_facts(
    session, *, strategy_message: RawMessage, assembled_at: datetime
) -> list[PriorMessageFact]:
    raw_messages = (
        session.query(RawMessage)
        .filter(RawMessage.chat_id == int(strategy_message.chat_id))
        .all()
    )
    strategy_key = _source_key(
        strategy_message.posted_at,
        strategy_message.message_id,
        strategy_message.id,
    )
    prior = [
        row
        for row in raw_messages
        if _source_key(row.posted_at, row.message_id, row.id) < strategy_key
    ]
    if not prior:
        return []
    raw_by_id = {int(row.id): row for row in prior}
    raw_ids = set(raw_by_id)
    preambles = (
        session.query(EntryPreamble)
        .filter(
            EntryPreamble.raw_message_id.in_(raw_ids),
            EntryPreamble.status == "pending",
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
    current_evidence_by_raw_id: dict[int, MessageEvidenceVersion] = {}
    for row in evidence_rows:
        current_evidence_by_raw_id.setdefault(int(row.raw_message_id), row)
    unresolved_ids = {
        int(row[0])
        for row in session.query(MessageEvidenceExtractionClaim.raw_message_id)
        .filter(
            MessageEvidenceExtractionClaim.raw_message_id.in_(raw_ids),
            MessageEvidenceExtractionClaim.lease_expires_at > assembled_at,
        )
        .all()
    }
    facts: list[PriorMessageFact] = []
    preamble_evidence_ids_by_raw_id: dict[int, set[int]] = {}
    for preamble in preambles:
        raw = raw_by_id[int(preamble.raw_message_id)]
        try:
            multiplier = Decimal(str(preamble.risk_multiplier))
        except InvalidOperation:
            multiplier = Decimal("1")
        facts.append(
            PriorMessageFact(
                raw_message_id=int(raw.id),
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind="entry_preamble",
                symbol=str(preamble.symbol),
                side=str(preamble.side),
                preamble_id=int(preamble.id),
                risk_multiplier=multiplier,
            )
        )
        preamble_evidence_ids_by_raw_id.setdefault(
            int(preamble.raw_message_id), set()
        ).add(int(preamble.evidence_version_id))
    candidate_raw_ids = {int(row.raw_message_id) for row in candidates}
    for candidate in candidates:
        raw = raw_by_id[int(candidate.raw_message_id)]
        if candidate.event_type == "entry_signal":
            kind = "complete_entry"
        elif candidate.event_type == "strategy_revision":
            kind = "replacement"
        elif candidate.event_type == "close_signal" or candidate.management_action in {
            "cancel_entry",
            "cancel",
        }:
            kind = "cancel_entry"
        else:
            continue
        facts.append(
            PriorMessageFact(
                raw_message_id=int(raw.id),
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind=kind,
                symbol=candidate.symbol,
                side=candidate.side,
            )
        )
    for raw_id in unresolved_ids:
        raw = raw_by_id[raw_id]
        facts.append(
            PriorMessageFact(
                raw_message_id=raw_id,
                message_id=int(raw.message_id),
                posted_at=raw.posted_at,
                kind="unresolved",
            )
        )
    for raw_id, raw in raw_by_id.items():
        evidence = current_evidence_by_raw_id.get(raw_id)
        durable_preamble = bool(preamble_evidence_ids_by_raw_id.get(raw_id))
        durable_candidate = raw_id in candidate_raw_ids
        needs_resolution = False
        if evidence is None:
            needs_resolution = not (durable_preamble or durable_candidate)
        else:
            normalized_valid = True
            try:
                normalized = json.loads(evidence.normalized_evidence_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                normalized = {}
                normalized_valid = False
            if not isinstance(normalized, dict):
                normalized = {}
                normalized_valid = False
            has_entry_context = isinstance(normalized, dict) and isinstance(
                normalized.get("entry_context"), dict
            )
            current_preamble_persisted = int(evidence.id) in (
                preamble_evidence_ids_by_raw_id.get(raw_id) or set()
            )
            if evidence.extraction_status != "completed":
                needs_resolution = True
            elif not normalized_valid:
                needs_resolution = True
            elif has_entry_context and not current_preamble_persisted:
                needs_resolution = True
            else:
                effective_status = str(
                    normalized.get("recognition_result") or ""
                )
                if effective_status not in {"是策略", "非策略"}:
                    strategy = normalized.get("strategy")
                    lifecycle = normalized.get("lifecycle_event")
                    effective_status = (
                        "是策略"
                        if bool(strategy)
                        or (
                            isinstance(lifecycle, dict)
                            and str(lifecycle.get("event_type") or "none")
                            != "none"
                        )
                        else "非策略"
                    )
                if effective_status == "是策略" and not durable_candidate:
                    needs_resolution = True
        if needs_resolution and raw_id not in unresolved_ids:
            facts.append(
                PriorMessageFact(
                    raw_message_id=raw_id,
                    message_id=int(raw.message_id),
                    posted_at=raw.posted_at,
                    kind="unresolved",
                )
            )
    return facts


def _result_from_assembly(
    assembly: EntryStrategyAssembly,
    preamble: EntryPreamble,
    *,
    mode: str,
) -> EntryAssemblyResult:
    multiplier = Decimal(str(assembly.risk_multiplier))
    evidence = json.loads(assembly.evidence_json or "{}")
    return EntryAssemblyResult(
        status="assembled",
        reason_code=None,
        mode=mode,
        proposed_risk_multiplier=multiplier,
        effective_risk_multiplier=multiplier,
        preamble_id=int(preamble.id),
        preamble_message_id=int(preamble.message_id),
        strategy_message_id=int(evidence.get("strategy_message_id")),
        assembly_id=int(assembly.id),
        assembly_fingerprint=str(assembly.fingerprint),
    )


def _proposed_evidence(
    *,
    preamble: EntryPreamble,
    strategy_message: RawMessage,
    candidate: SignalCandidate,
) -> tuple[dict[str, object], str]:
    evidence: dict[str, object] = {
        "chat_id": int(strategy_message.chat_id),
        "preamble_raw_message_id": int(preamble.raw_message_id),
        "preamble_message_id": int(preamble.message_id),
        "strategy_raw_message_id": int(strategy_message.id),
        "strategy_message_id": int(strategy_message.message_id),
        "symbol": str(candidate.symbol).upper(),
        "side": str(candidate.side).lower(),
        "risk_multiplier": str(preamble.risk_multiplier),
    }
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return evidence, hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()


def assemble_entry_strategy(
    session_factory: sessionmaker,
    *,
    strategy_raw_message_id: int,
    signal_candidate_id: int,
    strategy_instance_id: str,
    mode: str,
    assembled_at: datetime,
) -> EntryAssemblyResult:
    """Propose or atomically persist one strategy/preamble assembly."""

    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("entry preamble mode must be disabled, shadow, or live")
    with session_factory() as session:
        strategy_message = session.get(RawMessage, int(strategy_raw_message_id))
        candidate = session.get(SignalCandidate, int(signal_candidate_id))
        if strategy_message is None or candidate is None:
            raise LookupError("strategy message or candidate not found")
        if int(candidate.raw_message_id) != int(strategy_message.id):
            raise ValueError("candidate does not belong to strategy message")
        if candidate.event_type != "entry_signal" or not candidate.symbol or not candidate.side:
            raise ValueError("candidate is not a complete entry")
        existing = (
            session.query(EntryStrategyAssembly)
            .filter(
                (EntryStrategyAssembly.signal_candidate_id == int(candidate.id))
                | (EntryStrategyAssembly.strategy_instance_id == str(strategy_instance_id))
            )
            .one_or_none()
        )
        if existing is not None:
            if mode != "live":
                return EntryAssemblyResult(
                    status="blocked",
                    reason_code="existing_entry_assembly_not_live_authorized",
                    mode=mode,
                    proposed_risk_multiplier=Decimal("1"),
                    effective_risk_multiplier=Decimal("1"),
                    strategy_message_id=int(strategy_message.message_id),
                )
            preamble = session.get(EntryPreamble, int(existing.entry_preamble_id))
            if preamble is None:
                raise RuntimeError("entry strategy assembly lost its preamble")
            return _result_from_assembly(existing, preamble, mode=mode)
        if mode == "disabled":
            return EntryAssemblyResult(
                status="disabled",
                reason_code=None,
                mode=mode,
                proposed_risk_multiplier=Decimal("1"),
                effective_risk_multiplier=Decimal("1"),
                strategy_message_id=int(strategy_message.message_id),
            )
        decision = select_entry_preamble(
            strategy_posted_at=strategy_message.posted_at,
            strategy_message_id=int(strategy_message.message_id),
            strategy_raw_message_id=int(strategy_message.id),
            symbol=str(candidate.symbol),
            side=str(candidate.side),
            prior_facts=_load_prior_facts(
                session,
                strategy_message=strategy_message,
                assembled_at=assembled_at,
            ),
        )
        if decision.status != "ready" or decision.preamble_id is None:
            return EntryAssemblyResult(
                status=decision.status,
                reason_code=decision.reason_code,
                mode=mode,
                proposed_risk_multiplier=decision.risk_multiplier,
                effective_risk_multiplier=Decimal("1"),
                strategy_message_id=int(strategy_message.message_id),
            )
        preamble = session.get(EntryPreamble, int(decision.preamble_id))
        if preamble is None:
            raise RuntimeError("selected preamble disappeared")
        if mode == "shadow":
            _, fingerprint = _proposed_evidence(
                preamble=preamble,
                strategy_message=strategy_message,
                candidate=candidate,
            )
            return EntryAssemblyResult(
                status="proposed",
                reason_code=None,
                mode=mode,
                proposed_risk_multiplier=decision.risk_multiplier,
                effective_risk_multiplier=Decimal("1"),
                preamble_id=int(preamble.id),
                preamble_message_id=int(preamble.message_id),
                strategy_message_id=int(strategy_message.message_id),
                assembly_fingerprint=fingerprint,
            )

        evidence, fingerprint = _proposed_evidence(
            preamble=preamble,
            strategy_message=strategy_message,
            candidate=candidate,
        )
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assembly = EntryStrategyAssembly(
            entry_preamble_id=int(preamble.id),
            strategy_raw_message_id=int(strategy_message.id),
            signal_candidate_id=int(candidate.id),
            strategy_instance_id=str(strategy_instance_id),
            risk_multiplier=str(preamble.risk_multiplier),
            evidence_json=evidence_json,
            fingerprint=fingerprint,
            created_at=assembled_at,
        )
        try:
            session.add(assembly)
            session.flush()
            consumed = session.execute(
                update(EntryPreamble)
                .where(
                    EntryPreamble.id == int(preamble.id),
                    EntryPreamble.status == "pending",
                )
                .values(
                    status="consumed",
                    consumed_at=assembled_at,
                    updated_at=assembled_at,
                )
            )
            if int(consumed.rowcount or 0) != 1:
                raise RuntimeError("entry preamble was not consumed exactly once")
            session.commit()
            session.refresh(assembly)
            session.refresh(preamble)
            return _result_from_assembly(assembly, preamble, mode=mode)
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(EntryStrategyAssembly)
                .filter(
                    EntryStrategyAssembly.signal_candidate_id == int(candidate.id)
                )
                .one_or_none()
            )
            if existing is None:
                raise
            existing_preamble = session.get(
                EntryPreamble, int(existing.entry_preamble_id)
            )
            if existing_preamble is None:
                raise RuntimeError("entry strategy assembly lost its preamble")
            return _result_from_assembly(existing, existing_preamble, mode=mode)
