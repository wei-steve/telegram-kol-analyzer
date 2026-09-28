"""Durable source-order admission barrier for adjacent entry evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, func, or_, update
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
from telegram_kol_research.entry_confirmation_candidates import (
    is_entry_confirmation_candidate,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    EntryAssemblyWakeupExecution,
    EntryPreamble,
    EntryStrategyFragment,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.message_evidence import (
    has_material_strategy_evidence,
    normalize_entry_strategy_fragments,
)
from telegram_kol_research.recognition_failure_attribution import (
    MIMO_AUTHORITATIVE_FAILED,
    MIMO_AUTHORITATIVE_FAILED_EXHAUSTED,
)


ADJACENT_ENTRY_MAX_AGE = timedelta(minutes=30)
ADJACENT_ENTRY_MAX_MESSAGES_PER_SIDE = 20
#: How long a deferred entry may wait for its adjacent context. The same six
#: hours as ``message_instruction_items.VISIBILITY_RETRY_DEADLINE``, and before
#: 2026-09-24 the two were in a race an entry could only lose: that sweep ran
#: over every instruction kind, so an entry was as likely to be expired by the
#: management deadline -- reported as "no matching position record was ever
#: found" -- as by its own. The sweep is now management-only and this deadline
#: is enforced solely by ``entry_admission_reconciler``, so the equal values no
#: longer meet. They are two independent numbers that happen to agree.
ENTRY_ADMISSION_EXECUTION_DEADLINE = timedelta(hours=6)

#: Terminal ``automation_status`` values of an adjacent message's authoritative
#: decision. ``blocked`` is the source-message deletion barrier, which has its
#: own closure path (``source_message_deletion_worker``) and leaves the row at
#: ``raw_messages.source_status='deleted'``; ``completed`` means the
#: authoritative processor already ran whatever the message asked for.
_TERMINAL_NO_ACTION_STATUSES = frozenset({"completed", "blocked"})
#: ``skipped`` is terminal for every reason except this one: an authoritative
#: failure is retried by the message processing job, so a candidate may still
#: arrive. See ``authoritative_recognition.py`` (the automation branch around
#: lines 2570-2590) for the full vocabulary.
#:
#: ``mimo_authoritative_failed_exhausted`` is deliberately absent, which makes
#: it terminal: the worker writes it when that retry has been spent, and no
#: candidate can follow. So is ``mimo_authoritative_failed`` itself once the
#: message's processing job is ``failed`` (``_JOB_EXHAUSTED_STATUSES``) -- the
#: rows written before the worker learned to rewrite the reason, and any path
#: that fails a job without doing so. 2026-09-28: 陈哥's raw 19490 held the
#: corrected BTC entry 19491 for 25 minutes on exactly such a row.
_NON_TERMINAL_SKIP_REASONS = frozenset({MIMO_AUTHORITATIVE_FAILED})
#: ``message_processing_jobs.status`` values after which no retry is coming.
_JOB_EXHAUSTED_STATUSES = frozenset({"failed"})

#: Lifecycle ``event_type`` values that take risk off: the first-pass prompt's
#: own vocabulary (``prompt_defaults.py``: ``cancel_entry``, ``exit_position``),
#: the candidate-level ``close_signal`` this module already maps to a
#: cancellation (``_load_source_facts``), and the full-exit spellings the
#: management readers accept as event types (``management_directives.py``,
#: ``message_recognition.py``).
_RISK_REDUCING_EVENT_TYPES = frozenset(
    {
        "cancel_entry",
        "exit_position",
        "close_signal",
        "exit_full",
        "full_exit",
        "close_position",
    }
)
#: ``management_action`` values that take risk off, whatever the event type
#: says: ``message_operation_contracts._CANCEL_ACTIONS`` / ``_EXIT_ACTIONS``
#: plus the partial/full aliases ``authoritative_instructions._canonical_kind``
#: folds together.
_RISK_REDUCING_ACTIONS = frozenset(
    {
        "cancel",
        "cancel_entry",
        "cancel_order",
        "cancel_pending_entry",
        "exit",
        "exit_position",
        "exit_full",
        "full_exit",
        "full_close",
        "close_position",
        "partial_exit",
        "exit_partial",
    }
)
_MANAGEMENT_CLASS_LABELS = frozenset({"策略管理", "仓位管理"})
_QUOTE_SUFFIXES = ("SWAP", "USDT", "USDC", "USD")


@dataclass(frozen=True, slots=True)
class EntryAdmissionDecision:
    status: str
    reason_code: str | None
    proposed_status: str
    cutoff: SourceOrderKey
    selection: AdjacentEntryDecision
    blocking_raw_message_ids: tuple[int, ...] = ()
    deadline_at: datetime | None = None
    recheck_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class EntryAssemblyWakeClaim:
    attempt_id: int
    strategy_raw_message_id: int
    trigger_raw_message_id: int
    claim_token: str
    child_execution_id: int | None = None
    wake_generation: int | None = None


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


def _decision_is_terminal_no_action(
    decision: RecognitionDecision | None,
    *,
    job_status: str | None = None,
) -> bool:
    """Whether an adjacent message's authoritative decision can still act.

    Evidence only says "this message looks like it wants something"; the
    authoritative decision is what says "and we decided not to do it". When the
    decision is terminal and produced no candidate, the candidate the admission
    barrier waits for will never arrive, and the entry stays deferred until its
    six-hour deadline elapses. See section 3 of
    ``docs/plans/2026-09-16-adjacent-entry-deadlock-and-market-entry-geometry-analysis.md``
    for the six production entries killed this way between 2026-09-04 and 09-16.

    ``job_status`` is the message's processing job status: a retryable
    failure stops being retryable once its job has failed for good.
    """

    if decision is None:
        return False
    status = str(decision.automation_status or "").strip().lower()
    reason = str(decision.automation_reason or "").strip().lower()
    if status in _TERMINAL_NO_ACTION_STATUSES:
        return True
    if status != "skipped":
        return False
    if reason not in _NON_TERMINAL_SKIP_REASONS:
        return True
    return str(job_status or "").strip().lower() in _JOB_EXHAUSTED_STATUSES


def _decision_is_exhausted_failure(
    decision: RecognitionDecision | None,
    *,
    job_status: str | None,
) -> bool:
    """Whether the decision is terminal only because its retries ran out."""

    if decision is None:
        return False
    if str(decision.automation_status or "").strip().lower() != "skipped":
        return False
    reason = str(decision.automation_reason or "").strip().lower()
    if reason == MIMO_AUTHORITATIVE_FAILED_EXHAUSTED:
        return True
    return (
        reason == MIMO_AUTHORITATIVE_FAILED
        and str(job_status or "").strip().lower() in _JOB_EXHAUSTED_STATUSES
    )


def _base_symbol(value: object) -> str | None:
    text = "".join(ch for ch in str(value or "").upper() if ch.isascii() and ch.isalnum())
    for suffix in _QUOTE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text or None


def _side(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"long", "buy"} or text in {"多", "做多", "开多"}:
        return "long"
    if text in {"short", "sell"} or text in {"空", "做空", "开空"}:
        return "short"
    return None


def _exhausted_blocker_may_cancel_entry(
    normalized: dict,
    *,
    blocker: RawMessage,
    strategy: RawMessage,
    candidate: SignalCandidate,
    own_lifecycle_ids: frozenset[int],
) -> bool:
    """Whether an unreadable neighbour could be the cancellation of this entry.

    4a/4b made an exhausted recognition failure terminal, which releases the
    entry behind it after about ten minutes. Before that it waited six hours
    and expired -- fail-closed. That is still the right answer for the one
    shape where releasing is dangerous: the KOL posts an entry, then a
    "取消 / 不进了 / 全部出局" for it, and the cancellation is the message we
    could not read. The first-pass evidence survives the failure, so it says
    enough to tell that shape apart; anything else is released.

    Released (``False``) when any of these holds:

    * the neighbour was posted before the strategy message. A cancellation
      cannot cancel an entry that did not exist yet -- 陈哥's 19490 preceded
      19491 by five seconds. Posting *after* is necessary, not sufficient: the
      adjacent window also holds later messages about other positions, which
      the three tests below tell apart;
    * its lifecycle event takes no risk off (``_RISK_REDUCING_EVENT_TYPES`` /
      ``_RISK_REDUCING_ACTIONS``). ``message_classes`` carries no action of its
      own, so it cannot make a message risk-reducing -- only targets come
      from it;
    * every target it names is an ``exact`` lifecycle that is not this
      entry's. Admission runs before this entry has a lifecycle in the normal
      order, so the entry's own set is whatever ``strategy_lifecycles`` row
      already carries its candidate or its (chat, message) -- usually none,
      in which case any exact target is another position's;
    * every target it names is for a different symbol or side. A missing or
      unreadable symbol or side matches, so ignorance keeps the block.
    """

    if source_order_key(
        blocker.posted_at, blocker.message_id, blocker.id
    ) < source_order_key(strategy.posted_at, strategy.message_id, strategy.id):
        return False
    lifecycle = normalized.get("lifecycle_event")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    event_type = str(lifecycle.get("event_type") or "").strip().lower()
    action = str(lifecycle.get("management_action") or "").strip().lower()
    if (
        event_type not in _RISK_REDUCING_EVENT_TYPES
        and action not in _RISK_REDUCING_ACTIONS
    ):
        return False

    targets: list[tuple[int | None, object, object]] = []
    try:
        lifecycle_target = int(lifecycle.get("target_lifecycle_id"))
    except (TypeError, ValueError):
        lifecycle_target = None
    targets.append((lifecycle_target, lifecycle.get("symbol"), lifecycle.get("side")))
    classes = normalized.get("message_classes")
    for element in classes if isinstance(classes, list) else []:
        if not isinstance(element, dict):
            continue
        if str(element.get("class") or "") not in _MANAGEMENT_CLASS_LABELS:
            continue
        target = element.get("target")
        target = target if isinstance(target, dict) else {}
        exact_id = None
        if str(target.get("resolution") or "") == "exact":
            try:
                exact_id = int(target.get("lifecycle_id"))
            except (TypeError, ValueError):
                exact_id = None
        targets.append((exact_id, target.get("symbol"), target.get("side")))

    entry_symbol = _base_symbol(candidate.symbol)
    entry_side = _side(candidate.side)
    for exact_id, symbol, side in targets:
        if exact_id is not None and exact_id not in own_lifecycle_ids:
            continue
        target_symbol = _base_symbol(symbol)
        target_side = _side(side)
        if target_symbol and entry_symbol and target_symbol != entry_symbol:
            continue
        if target_side and entry_side and target_side != entry_side:
            continue
        return True
    return False


def _is_adjacent_entry_context_defer(result_json: str | None) -> bool:
    try:
        result = json.loads(result_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(result, dict)
        and str(result.get("status") or "") == "deferred"
        and str(result.get("reason") or "") == "adjacent_entry_context_pending"
    )


def _release_adjacent_entry_visibility_delay(
    session,
    *,
    attempt: EntryAssemblyAttempt,
    now: datetime,
) -> bool | None:
    """Make the exact entry item immediately claimable after its final wakeup."""

    item = (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.raw_message_id
            == int(attempt.strategy_raw_message_id),
            MessageInstructionItem.signal_candidate_id
            == int(attempt.signal_candidate_id),
            MessageInstructionItem.instruction_kind == "entry",
            MessageInstructionItem.retired_at.is_(None),
        )
        .one_or_none()
    )
    if item is None:
        return None
    if item.status != "pending" or not _is_adjacent_entry_context_defer(item.result_json):
        return False
    item.visibility_next_attempt_at = None
    item.updated_at = now
    return True


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
    decisions_by_raw = {
        int(row.raw_message_id): row
        for row in session.query(RecognitionDecision)
        .filter(RecognitionDecision.raw_message_id.in_(raw_ids))
        .all()
    }
    job_status_by_raw = {
        int(raw_id): str(job_status)
        for raw_id, job_status in session.query(
            MessageProcessingJob.raw_message_id, MessageProcessingJob.status
        )
        .filter(MessageProcessingJob.raw_message_id.in_(raw_ids))
        .all()
    }
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
        if is_entry_confirmation_candidate(other_candidate):
            # 2026-09-23, design section 4.2.3. A confirmation candidate is an
            # ``entry_signal`` row, so it used to be read as ``complete_entry``
            # -- a hard boundary that cut off everything at and before its own
            # source key, including the sizing preamble hanging on that very
            # message. It represents no entry of its own (the execution gate
            # refuses it outright), so it is neither a boundary nor a fragment
            # here. It still produces a fact so the message counts as resolved
            # rather than falling through to ``unresolved``.
            kind = "entry_confirm"
        elif other_candidate.event_type == "entry_signal":
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
    own_lifecycle_ids: frozenset[int] | None = None
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
                    or has_material_strategy_evidence(normalized.get("strategy"))
                    or (
                        isinstance(lifecycle, dict)
                        and str(lifecycle.get("event_type") or "none") != "none"
                    )
                )
                # ``fragment_application_pending`` is decided by the evidence
                # alone (fragment rows are persisted straight from it), so it
                # deliberately stays outside the decision test.
                decision = decisions_by_raw.get(raw_id)
                job_status = job_status_by_raw.get(raw_id)
                decision_terminal = _decision_is_terminal_no_action(
                    decision, job_status=job_status
                )
                if (
                    decision_terminal
                    and action_expected
                    and raw_id not in candidate_raw_ids
                    and _decision_is_exhausted_failure(
                        decision, job_status=job_status
                    )
                ):
                    if own_lifecycle_ids is None:
                        own_lifecycle_ids = _own_lifecycle_ids(
                            session, strategy=strategy, candidate=candidate
                        )
                    decision_terminal = not _exhausted_blocker_may_cancel_entry(
                        normalized,
                        blocker=raw,
                        strategy=strategy,
                        candidate=candidate,
                        own_lifecycle_ids=own_lifecycle_ids,
                    )
                application_pending = fragment_application_pending or (
                    action_expected
                    and raw_id not in candidate_raw_ids
                    and not decision_terminal
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


def _own_lifecycle_ids(
    session,
    *,
    strategy: RawMessage,
    candidate: SignalCandidate,
) -> frozenset[int]:
    """Lifecycles that already belong to the entry being admitted, if any."""

    rows = (
        session.query(StrategyLifecycle.id)
        .filter(
            or_(
                StrategyLifecycle.signal_candidate_id == int(candidate.id),
                and_(
                    StrategyLifecycle.chat_id == int(strategy.chat_id),
                    StrategyLifecycle.message_id == int(strategy.message_id),
                ),
            )
        )
        .all()
    )
    return frozenset(int(row_id) for (row_id,) in rows)


def exhausted_blocker_wake_is_safe(
    session_factory: sessionmaker,
    *,
    blocker_raw_message_id: int,
) -> bool:
    """Whether naming this exhausted message as completed may release entries.

    The completed-message wakeup removes the message from every pending
    attempt's blocker list without re-assessing admission, which is right for
    a message that finished and wrong for an unreadable later cancellation of
    the entry (``_exhausted_blocker_may_cancel_entry``). When any attempt it
    blocks could be that, or its evidence cannot be read, the caller must not
    name it: the reconciler re-assesses each attempt on its own and releases
    the ones the guard allows.
    """

    with session_factory() as session:
        blocker = session.get(RawMessage, int(blocker_raw_message_id))
        if blocker is None:
            return False
        attempts = (
            session.query(EntryAssemblyAttempt)
            .filter(EntryAssemblyAttempt.status == "pending")
            .all()
        )
        blocked = []
        for attempt in attempts:
            try:
                blockers = {
                    int(value)
                    for value in json.loads(attempt.blocking_raw_message_ids_json or "[]")
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            if int(blocker.id) in blockers:
                blocked.append(attempt)
        if not blocked:
            return True
        evidence = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(blocker.id),
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
        if evidence is None or evidence.extraction_status != "completed":
            return False
        try:
            normalized = json.loads(evidence.normalized_evidence_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(normalized, dict):
            return False
        for attempt in blocked:
            strategy = session.get(RawMessage, int(attempt.strategy_raw_message_id))
            candidate = session.get(SignalCandidate, int(attempt.signal_candidate_id))
            if strategy is None or candidate is None:
                return False
            if _exhausted_blocker_may_cancel_entry(
                normalized,
                blocker=blocker,
                strategy=strategy,
                candidate=candidate,
                own_lifecycle_ids=_own_lifecycle_ids(
                    session, strategy=strategy, candidate=candidate
                ),
            ):
                return False
        return True


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
    item = (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.raw_message_id == int(strategy_raw_message_id),
            MessageInstructionItem.signal_candidate_id == int(signal_candidate_id),
            MessageInstructionItem.instruction_kind == "entry",
            MessageInstructionItem.retired_at.is_(None),
        )
        .one_or_none()
    )
    if item is not None and item.execution_deadline_at is None:
        item.execution_deadline_at = now + ENTRY_ADMISSION_EXECUTION_DEADLINE
    if existing is not None:
        if existing.status in {"shadow", "pending", "woken"}:
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
        if existing.status in {"shadow", "pending", "woken"}:
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
        item = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.raw_message_id == int(strategy.id),
                MessageInstructionItem.signal_candidate_id == int(candidate.id),
                MessageInstructionItem.instruction_kind == "entry",
                MessageInstructionItem.retired_at.is_(None),
            )
            .one_or_none()
        )
        deadline_at = item.execution_deadline_at if item is not None else None
        comparable_deadline = deadline_at
        comparable_assessed_at = assessed_at
        if comparable_deadline is not None:
            if comparable_deadline.tzinfo is None and comparable_assessed_at.tzinfo is not None:
                comparable_deadline = comparable_deadline.replace(
                    tzinfo=comparable_assessed_at.tzinfo
                )
            elif comparable_deadline.tzinfo is not None and comparable_assessed_at.tzinfo is None:
                comparable_assessed_at = comparable_assessed_at.replace(
                    tzinfo=comparable_deadline.tzinfo
                )
        if (
            mode == "live"
            and comparable_deadline is not None
            and comparable_assessed_at >= comparable_deadline
        ):
            return EntryAdmissionDecision(
                "blocked",
                "entry_admission_deadline_expired",
                "blocked",
                cutoff,
                selection,
                deadline_at=deadline_at,
            )
        proposed_status = "deferred" if selection.status == "pending" else selection.status
        attempt = None
        blocking_ids: list[int] = []
        if selection.status == "pending" and mode in {"shadow", "live"}:
            blocking_ids = list(selection.pending_raw_message_ids)
            attempt = _persist_attempt(
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
                "deferred",
                selection.reason_code,
                proposed_status,
                cutoff,
                selection,
                tuple(sorted(set(blocking_ids))),
                deadline_at or assessed_at + ENTRY_ADMISSION_EXECUTION_DEADLINE,
                attempt.fingerprint if attempt is not None else None,
            )
        if mode == "live" and selection.status == "blocked":
            return EntryAdmissionDecision(
                "blocked", selection.reason_code, proposed_status, cutoff, selection
            )
        return EntryAdmissionDecision(
            "ready", None, proposed_status, cutoff, selection
        )


def _finish_entry_assembly_wake_claim(
    session,
    *,
    attempt,
    claim_token: str,
    trigger_raw_message_id: int,
    now: datetime,
    execution_owner,
    revert_status: str,
) -> EntryAssemblyWakeClaim | None:
    """Turn a won compare-and-set into a durable child fence, or give it back.

    Shared by both triggers so the hand-off to
    ``run_claimed_entry_assembly_wakeup`` is identical whether the blocker
    finished or the reconciler admitted it. ``revert_status`` is the status the
    attempt came from, so one whose instruction item has moved on is put back
    exactly where it was rather than silently demoted.
    """

    released = _release_adjacent_entry_visibility_delay(
        session,
        attempt=attempt,
        now=now,
    )
    if released is False:
        session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.id == int(attempt.id),
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claim_token == claim_token,
            )
            .values(
                status=revert_status,
                wake_claim_token=None,
                wake_claimed_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return None
    wake_generation = int(
        session.query(
            func.coalesce(
                func.max(EntryAssemblyWakeupExecution.wake_generation),
                0,
            )
        )
        .filter(
            EntryAssemblyWakeupExecution.entry_assembly_attempt_id
            == int(attempt.id)
        )
        .scalar()
    ) + 1
    child = EntryAssemblyWakeupExecution(
        entry_assembly_attempt_id=int(attempt.id),
        wake_generation=wake_generation,
        strategy_raw_message_id=int(attempt.strategy_raw_message_id),
        trigger_raw_message_id=int(trigger_raw_message_id),
        status="claimed",
        claim_token=claim_token,
        owner_runtime_role=execution_owner.runtime_role,
        owner_instance_id=execution_owner.instance_id[:64],
        owner_pid=int(execution_owner.pid),
        owner_boot_id=execution_owner.boot_id[:128],
        owner_process_start_ticks=execution_owner.process_start_ticks[:64],
        owner_systemd_invocation_id=(
            execution_owner.systemd_invocation_id[:128]
            if execution_owner.systemd_invocation_id
            else None
        ),
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=2),
        created_at=now,
        updated_at=now,
    )
    session.add(child)
    session.flush()
    child_execution_id = int(child.id)
    session.commit()
    return EntryAssemblyWakeClaim(
        attempt_id=int(attempt.id),
        strategy_raw_message_id=int(attempt.strategy_raw_message_id),
        trigger_raw_message_id=int(trigger_raw_message_id),
        claim_token=claim_token,
        child_execution_id=child_execution_id,
        wake_generation=wake_generation,
    )


def claim_ready_entry_assembly_wakeups(
    session_factory: sessionmaker,
    *,
    completed_raw_message_id: int | None = None,
    now: datetime,
    limit: int = 20,
    execution_owner=None,
    execution_registry=None,
) -> tuple[EntryAssemblyWakeClaim, ...]:
    """Claim one deferred entry the wakeup path may now execute.

    Two independent triggers reach this function and only one of them can win,
    because both end in a compare-and-set on the same ``status`` column:

    * a blocker message finished -- ``pending``, and the completed message is
      the attempt's last blocker;
    * the reconciler re-assessed admission and it passed -- ``ready``, which
      needs no ``completed_raw_message_id`` because no blocker is left to name.
      The worker's periodic cycle calls it with none.

    A ``ready`` attempt carries the strategy message itself as its trigger. A
    real blocker is never the strategy message (``_load_source_facts`` excludes
    it), so that value also says which trigger produced the claim.
    """

    claimed: list[EntryAssemblyWakeClaim] = []
    if execution_owner is None:
        raise RuntimeError("entry_assembly_wakeup_execution_owner_required")
    from telegram_kol_research.authoritative_execution_schema import (
        require_recognition_execution_schema,
    )

    require_recognition_execution_schema(session_factory)
    if execution_owner.runtime_role not in {"worker", "all"}:
        raise RuntimeError("entry_assembly_wakeup_not_owned_by_runtime_role")
    if execution_registry is not None:
        execution_registry.require_accepting()
    with session_factory() as session:
        claimable_rows = (
            session.query(EntryAssemblyAttempt.id)
            .filter(EntryAssemblyAttempt.status.in_(("pending", "ready")))
            .order_by(EntryAssemblyAttempt.id.asc())
            .all()
        )
        attempt_ids = [int(row_id) for (row_id,) in claimable_rows]
        session.commit()
    # Claim exactly one child at a time. A batch of durable claims made before
    # adapter admission can strand the unvisited children when the first child
    # fails or process drain begins.
    claim_limit = 1
    for attempt_id in attempt_ids:
        if len(claimed) >= claim_limit:
            break
        for _ in range(3):
            with session_factory() as session:
                attempt = session.get(EntryAssemblyAttempt, int(attempt_id))
                if attempt is None or attempt.status not in {"pending", "ready"}:
                    break
                if attempt.status == "ready":
                    claim_token = uuid.uuid4().hex
                    result = session.execute(
                        update(EntryAssemblyAttempt)
                        .where(
                            EntryAssemblyAttempt.id == int(attempt.id),
                            EntryAssemblyAttempt.status == "ready",
                        )
                        .values(
                            status="claimed",
                            wake_claim_token=claim_token,
                            wake_claimed_at=now,
                            updated_at=now,
                        )
                    )
                    if int(result.rowcount or 0) != 1:
                        session.commit()
                        continue
                    claim = _finish_entry_assembly_wake_claim(
                        session,
                        attempt=attempt,
                        claim_token=claim_token,
                        trigger_raw_message_id=int(
                            attempt.strategy_raw_message_id
                        ),
                        now=now,
                        execution_owner=execution_owner,
                        revert_status="ready",
                    )
                    if claim is not None:
                        claimed.append(claim)
                    break
                if completed_raw_message_id is None:
                    # The periodic cycle names no completed message, so it has
                    # nothing to say about an attempt still waiting on one.
                    break
                old_blockers_json = attempt.blocking_raw_message_ids_json or "[]"
                try:
                    blockers = [int(value) for value in json.loads(old_blockers_json)]
                except (TypeError, ValueError, json.JSONDecodeError):
                    break
                if not blockers or int(completed_raw_message_id) not in blockers:
                    break
                remaining = [
                    value
                    for value in blockers
                    if value != int(completed_raw_message_id)
                ]
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
                        blocking_raw_message_ids_json=_canonical_json(remaining),
                        wake_claim_token=claim_token,
                        wake_claimed_at=now,
                        updated_at=now,
                    )
                )
                if int(result.rowcount or 0) == 1:
                    claim = _finish_entry_assembly_wake_claim(
                        session,
                        attempt=attempt,
                        claim_token=claim_token,
                        trigger_raw_message_id=int(completed_raw_message_id),
                        now=now,
                        execution_owner=execution_owner,
                        revert_status="pending",
                    )
                    if claim is not None:
                        claimed.append(claim)
                    break
                session.commit()
    return tuple(claimed)
