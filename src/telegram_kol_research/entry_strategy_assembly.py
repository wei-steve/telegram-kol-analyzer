"""Deterministically attach one prior sizing preamble to an entry strategy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.adjacent_entry_assembly import AdjacentEntryFact
from telegram_kol_research.models import (
    EntryAssemblyFragment,
    EntryPreamble,
    EntryStrategyAssembly,
    EntryStrategyFragment,
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
    fragment_ids: tuple[int, ...] = ()
    allocations: tuple[Decimal, ...] = ()
    supplemental_prices: tuple[Decimal, ...] = ()
    legacy_preamble_ids: tuple[int, ...] = ()
    configured_risk_budget_usdt: Decimal | None = None
    effective_risk_budget_usdt: Decimal | None = None
    planned_entry_leg_count: int | None = None


def adapt_prior_fact_to_adjacent_entry_fact(
    fact: PriorMessageFact,
) -> AdjacentEntryFact:
    """Expose legacy preamble facts through the v2 pure-selector contract."""

    if fact.kind != "entry_preamble" or fact.preamble_id is None:
        return AdjacentEntryFact(
            raw_message_id=fact.raw_message_id,
            message_id=fact.message_id,
            posted_at=fact.posted_at,
            kind=fact.kind,
            symbol=fact.symbol,
            side=fact.side,
        )
    return AdjacentEntryFact(
        raw_message_id=fact.raw_message_id,
        message_id=fact.message_id,
        posted_at=fact.posted_at,
        kind="fragment",
        symbol=fact.symbol,
        side=fact.side,
        fragment_id=-int(fact.preamble_id),
        fragment_kind="risk_multiplier",
        payload={"risk_multiplier": str(fact.risk_multiplier)},
    )


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


def _v2_result_from_assembly(
    assembly: EntryStrategyAssembly,
    *,
    mode: str,
) -> EntryAssemblyResult:
    evidence = json.loads(assembly.evidence_json or "{}")
    return EntryAssemblyResult(
        status="assembled",
        reason_code=None,
        mode=mode,
        proposed_risk_multiplier=Decimal(str(assembly.risk_multiplier)),
        effective_risk_multiplier=Decimal(str(assembly.risk_multiplier)),
        strategy_message_id=int(evidence["strategy_message_id"]),
        assembly_id=int(assembly.id),
        assembly_fingerprint=str(assembly.fingerprint),
        fragment_ids=tuple(int(value) for value in evidence.get("fragment_ids", [])),
        allocations=tuple(
            Decimal(str(value)) for value in evidence.get("allocations", [])
        ),
        supplemental_prices=tuple(
            Decimal(str(value))
            for value in evidence.get("supplemental_prices", [])
        ),
        legacy_preamble_ids=tuple(
            int(value) for value in evidence.get("legacy_preamble_ids", [])
        ),
        configured_risk_budget_usdt=(
            Decimal(str(evidence["configured_risk_budget_usdt"]))
            if evidence.get("configured_risk_budget_usdt") is not None
            else None
        ),
        effective_risk_budget_usdt=(
            Decimal(str(evidence["effective_risk_budget_usdt"]))
            if evidence.get("effective_risk_budget_usdt") is not None
            else None
        ),
        planned_entry_leg_count=(
            int(evidence["planned_entry_leg_count"])
            if evidence.get("planned_entry_leg_count") is not None
            else None
        ),
    )


def assemble_adjacent_entry_strategy(
    session_factory: sessionmaker,
    *,
    strategy_raw_message_id: int,
    signal_candidate_id: int,
    strategy_instance_id: str,
    mode: str,
    assembled_at: datetime,
    admission_decision=None,
    configured_risk_budget_usdt: Decimal | None = None,
    strategy_snapshot: Mapping[str, object] | None = None,
) -> EntryAssemblyResult:
    """Propose or atomically consume a source-ordered multi-fragment assembly."""

    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("entry message assembly v2 mode must be disabled, shadow, or live")
    with session_factory() as session:
        strategy_message = session.get(RawMessage, int(strategy_raw_message_id))
        candidate = session.get(SignalCandidate, int(signal_candidate_id))
        if strategy_message is None or candidate is None:
            raise LookupError("strategy message or candidate not found")
        existing = (
            session.query(EntryStrategyAssembly)
            .filter(
                (EntryStrategyAssembly.signal_candidate_id == int(candidate.id))
                | (
                    EntryStrategyAssembly.strategy_instance_id
                    == str(strategy_instance_id)
                )
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.entry_preamble_id is not None:
                if mode != "shadow":
                    return EntryAssemblyResult(
                        status="blocked",
                        reason_code="existing_legacy_entry_assembly",
                        mode=mode,
                        proposed_risk_multiplier=Decimal("1"),
                        effective_risk_multiplier=Decimal("1"),
                        strategy_message_id=int(strategy_message.message_id),
                    )
            elif mode != "live":
                return EntryAssemblyResult(
                    status="blocked",
                    reason_code="existing_entry_assembly_not_live_authorized",
                    mode=mode,
                    proposed_risk_multiplier=Decimal("1"),
                    effective_risk_multiplier=Decimal("1"),
                    strategy_message_id=int(strategy_message.message_id),
                )
            else:
                return _v2_result_from_assembly(existing, mode=mode)
    if mode == "disabled":
        return EntryAssemblyResult(
            status="disabled",
            reason_code=None,
            mode=mode,
            proposed_risk_multiplier=Decimal("1"),
            effective_risk_multiplier=Decimal("1"),
        )

    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    admission = admission_decision or assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=int(strategy_raw_message_id),
        signal_candidate_id=int(signal_candidate_id),
        mode=mode,
        assessed_at=assembled_at,
    )
    selection = admission.selection
    if admission.status in {"deferred", "blocked"}:
        return EntryAssemblyResult(
            status=("unresolved" if admission.status == "deferred" else "blocked"),
            reason_code=admission.reason_code,
            mode=mode,
            proposed_risk_multiplier=selection.risk_multiplier,
            effective_risk_multiplier=Decimal("1"),
            strategy_message_id=None,
        )
    with session_factory() as session:
        strategy_message = session.get(RawMessage, int(strategy_raw_message_id))
        candidate = session.get(SignalCandidate, int(signal_candidate_id))
        if strategy_message is None or candidate is None:
            raise LookupError("strategy message or candidate not found")
        evidence: dict[str, object] = {
            "chat_id": int(strategy_message.chat_id),
            "strategy_raw_message_id": int(strategy_message.id),
            "strategy_message_id": int(strategy_message.message_id),
            "signal_candidate_id": int(candidate.id),
            "symbol": str(candidate.symbol or "").upper(),
            "side": str(candidate.side or "").lower(),
            "fragment_ids": list(selection.fragment_ids),
            "legacy_preamble_ids": list(selection.legacy_preamble_ids),
            "risk_multiplier": str(selection.risk_multiplier),
            "allocations": [str(value) for value in selection.allocations],
            "supplemental_prices": [
                str(value) for value in selection.supplemental_prices
            ],
            "cutoff": [
                admission.cutoff[0].isoformat(),
                int(admission.cutoff[1]),
                int(admission.cutoff[2]),
            ],
        }
        if configured_risk_budget_usdt is not None:
            configured_budget = Decimal(str(configured_risk_budget_usdt))
            if not configured_budget.is_finite() or configured_budget <= 0:
                raise ValueError("configured risk budget must be positive and finite")
            evidence["configured_risk_budget_usdt"] = str(configured_budget)
            evidence["effective_risk_budget_usdt"] = str(
                configured_budget * selection.risk_multiplier
            )
        if strategy_snapshot is not None:
            bounded_snapshot = {
                key: strategy_snapshot.get(key)
                for key in (
                    "entry_prices",
                    "stop_loss",
                    "take_profit",
                    "entry_execution_type",
                )
            }
            evidence["strategy_snapshot"] = bounded_snapshot
            raw_entry_prices = bounded_snapshot.get("entry_prices")
            base_prices = (
                list(raw_entry_prices)
                if isinstance(raw_entry_prices, (list, tuple))
                else []
            )
            unique_prices: list[Decimal] = []
            for value in base_prices:
                parsed = Decimal(str(value))
                if parsed not in unique_prices:
                    unique_prices.append(parsed)
            for value in selection.supplemental_prices:
                if value not in unique_prices:
                    unique_prices.append(value)
            evidence["planned_entry_leg_count"] = len(unique_prices)
        fragment_rows = (
            session.query(EntryStrategyFragment)
            .filter(EntryStrategyFragment.id.in_(selection.fragment_ids))
            .order_by(
                EntryStrategyFragment.message_id.asc(),
                EntryStrategyFragment.raw_message_id.asc(),
                EntryStrategyFragment.id.asc(),
            )
            .all()
            if selection.fragment_ids
            else []
        )
        legacy_preambles = (
            session.query(EntryPreamble)
            .filter(EntryPreamble.id.in_(selection.legacy_preamble_ids))
            .order_by(EntryPreamble.message_id.asc(), EntryPreamble.id.asc())
            .all()
            if selection.legacy_preamble_ids
            else []
        )
        if len(fragment_rows) != len(selection.fragment_ids) or any(
            row.status != "pending" for row in fragment_rows
        ) or len(legacy_preambles) != len(selection.legacy_preamble_ids) or any(
            row.status != "pending" for row in legacy_preambles
        ):
            existing = (
                session.query(EntryStrategyAssembly)
                .filter(
                    EntryStrategyAssembly.signal_candidate_id == int(candidate.id)
                )
                .one_or_none()
            )
            if existing is not None and existing.entry_preamble_id is None:
                return _v2_result_from_assembly(existing, mode=mode)
            return EntryAssemblyResult(
                status="blocked",
                reason_code="entry_fragment_state_changed",
                mode=mode,
                proposed_risk_multiplier=selection.risk_multiplier,
                effective_risk_multiplier=Decimal("1"),
                strategy_message_id=int(strategy_message.message_id),
            )
        evidence["fragment_generations"] = [
            {
                "fragment_id": int(row.id),
                "evidence_version_id": int(row.evidence_version_id),
                "recognition_generation": str(row.recognition_generation),
                "fingerprint": str(row.fingerprint),
            }
            for row in fragment_rows
        ]
        evidence["legacy_preamble_generations"] = [
            {
                "preamble_id": int(row.id),
                "evidence_version_id": int(row.evidence_version_id),
                "recognition_generation": str(row.recognition_generation),
                "fingerprint": str(row.fingerprint),
            }
            for row in legacy_preambles
        ]
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fingerprint = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        if mode == "shadow" or not (fragment_rows or legacy_preambles):
            return EntryAssemblyResult(
                status="proposed" if (fragment_rows or legacy_preambles) else "none",
                reason_code=None,
                mode=mode,
                proposed_risk_multiplier=selection.risk_multiplier,
                effective_risk_multiplier=Decimal("1"),
                strategy_message_id=int(strategy_message.message_id),
                assembly_fingerprint=(
                    fingerprint if (fragment_rows or legacy_preambles) else None
                ),
                fragment_ids=selection.fragment_ids,
                allocations=selection.allocations,
                supplemental_prices=selection.supplemental_prices,
                legacy_preamble_ids=selection.legacy_preamble_ids,
                configured_risk_budget_usdt=(
                    Decimal(str(evidence["configured_risk_budget_usdt"]))
                    if evidence.get("configured_risk_budget_usdt") is not None
                    else None
                ),
                effective_risk_budget_usdt=(
                    Decimal(str(evidence["effective_risk_budget_usdt"]))
                    if evidence.get("effective_risk_budget_usdt") is not None
                    else None
                ),
                planned_entry_leg_count=(
                    int(evidence["planned_entry_leg_count"])
                    if evidence.get("planned_entry_leg_count") is not None
                    else None
                ),
            )
        assembly = EntryStrategyAssembly(
            entry_preamble_id=None,
            strategy_raw_message_id=int(strategy_message.id),
            signal_candidate_id=int(candidate.id),
            strategy_instance_id=str(strategy_instance_id),
            risk_multiplier=str(selection.risk_multiplier),
            evidence_json=evidence_json,
            fingerprint=fingerprint,
            created_at=assembled_at,
        )
        try:
            session.add(assembly)
            session.flush()
            for row in fragment_rows:
                fragment_message = session.get(RawMessage, int(row.raw_message_id))
                if fragment_message is None:
                    raise RuntimeError("entry fragment lost its source message")
                relationship = (
                    "before_strategy"
                    if _source_key(
                        fragment_message.posted_at,
                        row.message_id,
                        row.raw_message_id,
                    )
                    < _source_key(
                        strategy_message.posted_at,
                        strategy_message.message_id,
                        strategy_message.id,
                    )
                    else "after_strategy"
                )
                consumed = session.execute(
                    update(EntryStrategyFragment)
                    .where(
                        EntryStrategyFragment.id == int(row.id),
                        EntryStrategyFragment.status == "pending",
                    )
                    .values(
                        status="consumed",
                        source_relationship=relationship,
                        target_strategy_raw_message_id=int(strategy_message.id),
                        target_signal_candidate_id=int(candidate.id),
                        assembled_at=assembled_at,
                        consumed_at=assembled_at,
                        updated_at=assembled_at,
                    )
                )
                if int(consumed.rowcount or 0) != 1:
                    raise RuntimeError("entry fragment was not consumed exactly once")
                session.add(
                    EntryAssemblyFragment(
                        entry_strategy_assembly_id=int(assembly.id),
                        entry_strategy_fragment_id=int(row.id),
                        created_at=assembled_at,
                    )
                )
            for preamble in legacy_preambles:
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
                    raise RuntimeError("legacy entry preamble was not consumed exactly once")
            session.commit()
            session.refresh(assembly)
            return _v2_result_from_assembly(assembly, mode=mode)
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(EntryStrategyAssembly)
                .filter(
                    EntryStrategyAssembly.signal_candidate_id == int(candidate.id)
                )
                .one_or_none()
            )
            if existing is None or existing.entry_preamble_id is not None:
                raise
            return _v2_result_from_assembly(existing, mode=mode)


def finalize_adjacent_entry_assembly_draft(
    session_factory: sessionmaker,
    *,
    assembly_id: int,
    order_draft: Mapping[str, object],
) -> str:
    """Attach the exact bounded order economics once, before any exchange write."""

    bounded_legs = []
    for leg in list(order_draft.get("order_legs") or []):
        if not isinstance(leg, Mapping):
            continue
        bounded_legs.append(
            {
                key: leg.get(key)
                for key in (
                    "price",
                    "order_type",
                    "allocation_pct",
                    "risk_budget_usdt",
                    "quantity",
                    "quantity_unit",
                    "estimated_stop_loss_usdt",
                    "client_order_id",
                )
            }
        )
    if not 1 <= len(bounded_legs) <= 5:
        raise ValueError("entry assembly draft must contain one to five legs")
    draft_snapshot = {
        "strategy_instance_id": order_draft.get("strategy_instance_id"),
        "instrument_id": order_draft.get("instrument_id"),
        "stop_loss": order_draft.get("stop_loss"),
        "take_profit_legs": order_draft.get("take_profit_legs") or [],
        "risk_budget_usdt": order_draft.get("risk_budget_usdt"),
        "order_legs": bounded_legs,
    }
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, int(assembly_id))
        if assembly is None or assembly.entry_preamble_id is not None:
            raise LookupError("v2 entry assembly not found")
        original_evidence_json = assembly.evidence_json or "{}"
        original_fingerprint = str(assembly.fingerprint)
        evidence = json.loads(original_evidence_json)
        existing = evidence.get("order_draft_snapshot")
        if existing is not None:
            if existing != draft_snapshot:
                raise RuntimeError("entry_assembly_draft_conflict")
            return str(assembly.fingerprint)
        evidence["order_draft_snapshot"] = draft_snapshot
        evidence["final_entry_leg_count"] = len(bounded_legs)
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fingerprint = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        result = session.execute(
            update(EntryStrategyAssembly)
            .where(
                EntryStrategyAssembly.id == int(assembly_id),
                EntryStrategyAssembly.entry_preamble_id.is_(None),
                EntryStrategyAssembly.evidence_json == original_evidence_json,
                EntryStrategyAssembly.fingerprint == original_fingerprint,
            )
            .values(evidence_json=evidence_json, fingerprint=fingerprint)
        )
        session.commit()
        if int(result.rowcount or 0) == 1:
            return fingerprint
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, int(assembly_id))
        if assembly is None or assembly.entry_preamble_id is not None:
            raise LookupError("v2 entry assembly not found")
        evidence = json.loads(assembly.evidence_json or "{}")
        if evidence.get("order_draft_snapshot") != draft_snapshot:
            raise RuntimeError("entry_assembly_draft_conflict")
        return str(assembly.fingerprint)


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
