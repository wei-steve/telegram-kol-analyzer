"""Run MiMo authority and enqueue successful decisions for later review."""

from __future__ import annotations

import json
import inspect
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.context_resolution import (
    ContextResolutionDecision,
    ContextResolutionError,
)
from telegram_kol_research.contextual_message_window import (
    ContextualMessageWindow,
    build_contextual_message_window,
)
from telegram_kol_research.entry_preambles import (
    persist_authoritative_entry_preamble,
)
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.entry_strategy_fragments import (
    persist_authoritative_entry_fragments,
)
from telegram_kol_research.message_recognition import (
    MessageRecognitionResult,
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.instruction_execution_projection import (
    project_instruction_execution_contracts,
)
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.message_evidence import (
    build_current_message_input_fingerprint,
    claim_message_evidence_extraction,
    finalize_claimed_mimo_message_evidence,
    finalize_claimed_mimo_v2_message_evidence,
    release_message_evidence_extraction_claim,
)
from telegram_kol_research.mimo_contract_circuit import (
    load_mimo_contract_circuit,
    record_mimo_v2_outcome,
)
from telegram_kol_research.mimo_recognition_runs import (
    canonical_json_fingerprint,
    complete_mimo_run,
    record_mimo_attempt,
    start_mimo_run,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    _execution_projection,
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    MessageInstructionItem,
    MimoRecognitionRun,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyThread,
    utc_now,
)
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    claim_authoritative_execution,
    finalize_authoritative_automation_outcome,
    save_pending_authoritative_decision,
    save_terminal_authoritative_decision,
    update_recognition_execution_outcome,
)
from telegram_kol_research.recognition_experiments import (
    MimoAuthoritativeResult,
    build_authoritative_context_for_message,
    infer_mimo_authoritative_v2,
    run_mimo_authoritative_for_message,
)
from telegram_kol_research.strategy_thread_candidates import (
    StrategyThreadCandidate,
    exact_single_current_risk_thread,
    generate_strategy_thread_candidates,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)
from telegram_kol_research.trading_settings import load_trading_settings


CONTEXT_TRIGGER_ORDER = (
    "revision_language",
    "cancellation_language",
    "entered_holder_language",
    "management_without_exact_target",
    "multiple_same_source_candidates",
    "reply_target_disagreement",
    "text_image_conflict",
    "apparent_entry_may_be_revision",
)
REVISION_LANGUAGE = ("更新", "修改", "改为", "调整", "replace", "update")
CANCELLATION_LANGUAGE = ("取消", "撤销", "撤单", "cancel")
ENTERED_HOLDER_LANGUAGE = (
    "有入场",
    "已入场",
    "持仓",
    "保护成本",
    "保本",
    "继续持有",
)
EXACT_CONTEXT_RISK_REDUCTION_MARKER = (
    "_exact_context_risk_reduction_authorized"
)
logger = logging.getLogger(__name__)
MIMO_V2_CONTRACT_VERSION = "mimo-authoritative-v2"
MIMO_V2_FALLBACK_ERROR_CODES = frozenset(
    {
        "provider_timeout",
        "provider_http_error",
        "invalid_json",
        "contract_validation_failed",
        "adapter_failure",
    }
)


@dataclass(frozen=True)
class AuthoritativeAssessment:
    raw_message_id: int
    mimo: MimoAuthoritativeResult
    deepseek_payload: dict[str, Any] | None
    agreement_status: str
    differences: list[str]
    semantic_review_status: str = "not_applicable"
    authoritative_generation: str | None = None
    context_resolution: ContextResolutionDecision | None = None
    context_resolution_triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthoritativeProcessingResult:
    assessment: AuthoritativeAssessment
    recognition: MessageRecognitionResult
    automation: dict[str, Any]


def _context_value(value: Any, field: str, default=None):
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def requires_context_resolution(
    *,
    first_pass_payload: Mapping[str, Any],
    evidence: Mapping[str, Any],
    context_window: ContextualMessageWindow | Mapping[str, Any],
    candidates: Sequence[StrategyThreadCandidate | Mapping[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    """Return deterministic closed reasons for invoking the second resolver."""

    current = _context_value(context_window, "current", {})
    text = str(_context_value(current, "text", "") or "").lower()
    lifecycle_event = first_pass_payload.get("lifecycle_event")
    lifecycle_event = (
        lifecycle_event if isinstance(lifecycle_event, Mapping) else {}
    )
    event_type = str(lifecycle_event.get("event_type") or "none")
    target_lifecycle_id = lifecycle_event.get("target_lifecycle_id")
    recognition_result = str(
        first_pass_payload.get("recognition_result") or ""
    )
    reasons: set[str] = set()
    if any(term.lower() in text for term in REVISION_LANGUAGE):
        reasons.add("revision_language")
    if any(term.lower() in text for term in CANCELLATION_LANGUAGE):
        reasons.add("cancellation_language")
    if any(term.lower() in text for term in ENTERED_HOLDER_LANGUAGE):
        reasons.add("entered_holder_language")
    if event_type != "none" and target_lifecycle_id in (None, ""):
        reasons.add("management_without_exact_target")
    if len(candidates) > 1:
        reasons.add("multiple_same_source_candidates")

    reply_chain = _context_value(context_window, "reply_chain", ()) or ()
    reply_thread_ids: set[int] = set()
    if reply_chain:
        links = _context_value(reply_chain[0], "strategy_links", ()) or ()
        for link in links:
            thread_id = _context_value(link, "strategy_thread_id")
            if thread_id is not None:
                reply_thread_ids.add(int(thread_id))
    target_thread_ids = {
        int(_context_value(candidate, "thread_id"))
        for candidate in candidates
        if target_lifecycle_id not in (None, "")
        and int(_context_value(candidate, "lifecycle_id")) == int(target_lifecycle_id)
    }
    active_strategies = (
        _context_value(context_window, "active_strategies", ()) or ()
    )
    for strategy in active_strategies:
        lifecycle_id = _context_value(strategy, "lifecycle_id")
        thread_id = _context_value(strategy, "strategy_thread_id")
        if (
            target_lifecycle_id not in (None, "")
            and lifecycle_id is not None
            and int(lifecycle_id) == int(target_lifecycle_id)
            and thread_id is not None
        ):
            target_thread_ids.add(int(thread_id))
    if reply_thread_ids and target_thread_ids and reply_thread_ids != target_thread_ids:
        reasons.add("reply_target_disagreement")

    conflicts = evidence.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        reasons.add("text_image_conflict")
    if recognition_result == "是策略" and candidates and (
        "revision_language" in reasons
        or any(
            "overlapping_entry"
            in tuple(_context_value(candidate, "reasons", ()) or ())
            for candidate in candidates
        )
    ):
        reasons.add("apparent_entry_may_be_revision")
    ordered = tuple(reason for reason in CONTEXT_TRIGGER_ORDER if reason in reasons)
    return bool(ordered), ordered


def compare_assessments(
    mimo_payload: dict[str, Any],
    deepseek_payload: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    if deepseek_payload is None:
        return "not_applicable", []
    differences: list[str] = []
    if _value(mimo_payload, "recognition_result") != _value(
        deepseek_payload, "recognition_result"
    ):
        differences.append("recognition_result")
    for field in ("symbol", "side", "entry", "order_type", "stop_loss", "take_profit"):
        if _nested_value(mimo_payload, "strategy", field) != _nested_value(
            deepseek_payload, "strategy", field
        ):
            differences.append(f"strategy.{field}")
    for field in ("event_type", "target_lifecycle_id", "symbol", "side", "management_action"):
        if _nested_value(mimo_payload, "lifecycle_event", field) != _nested_value(
            deepseek_payload, "lifecycle_event", field
        ):
            differences.append(f"lifecycle_event.{field}")
    return ("disagreed" if differences else "agreed"), differences


def _value(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return str(value or "").strip().lower()


def _nested_value(payload: dict[str, Any], section: str, field: str) -> str:
    data = payload.get(section)
    if not isinstance(data, dict):
        return ""
    value = data.get(field)
    return str(value if value is not None else "").strip().lower()


def _numeric_range(value: Any) -> tuple[float | None, float | None]:
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    if not numbers:
        return None, None
    parsed = [float(number) for number in numbers[:2]]
    return (
        parsed[0],
        parsed[1] if len(parsed) > 1 else parsed[0],
    )


def _load_resolution_inputs(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    first_pass_payload: dict[str, Any],
    evidence_row,
) -> tuple[
    dict[str, Any],
    ContextualMessageWindow,
    tuple[StrategyThreadCandidate, ...],
]:
    normalized_evidence = json.loads(evidence_row.normalized_evidence_json or "{}")
    text_evidence = json.loads(evidence_row.text_evidence_json or "{}")
    image_evidence = json.loads(evidence_row.image_evidence_json or "{}")
    conflicts = image_evidence.get("conflicts")
    if not isinstance(conflicts, list):
        conflicts = normalized_evidence.get("conflicts", [])
    evidence = {
        "version_id": int(evidence_row.id),
        "text": text_evidence,
        "images": image_evidence.get("images", []),
        "normalized": normalized_evidence,
        "conflicts": conflicts,
    }
    strategy = first_pass_payload.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    entry_low, entry_high = _numeric_range(strategy.get("entry"))
    try:
        stop_loss = (
            float(strategy["stop_loss"])
            if strategy.get("stop_loss") not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        stop_loss = None
    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=int(raw_message_id),
        )
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=int(raw_message_id),
            symbol=strategy.get("symbol"),
            side=strategy.get("side"),
            entry_range_low=entry_low,
            entry_range_high=entry_high,
            stop_loss=stop_loss,
            take_profit=(
                str(strategy.get("take_profit"))
                if strategy.get("take_profit") not in (None, "")
                else None
            ),
        )
    return evidence, window, candidates


def _load_exchange_state(provider, raw_message_id: int, candidates) -> Any:
    if provider is None:
        return {}
    signature = inspect.signature(provider)
    if "candidate_thread_ids" in signature.parameters:
        return provider(
            raw_message_id,
            candidate_thread_ids={
                int(candidate.thread_id) for candidate in candidates
            },
        )
    return provider(raw_message_id)


def _load_current_mimo_evidence_result(
    session_factory: sessionmaker,
    raw_message_id: int,
) -> tuple[MimoAuthoritativeResult, MessageEvidenceVersion] | None:
    """Reconstruct the first pass from immutable evidence without calling MiMo."""

    with session_factory() as session:
        row = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
                MessageEvidenceVersion.extraction_status == "completed",
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
        if row is None:
            return None
        session.expunge(row)
    normalized = json.loads(row.normalized_evidence_json or "{}")
    text_evidence = json.loads(row.text_evidence_json or "{}")
    image_evidence = json.loads(row.image_evidence_json or "{}")
    images = image_evidence.get("images", [])
    if normalized.get("contract_version") == MIMO_V2_CONTRACT_VERSION:
        canonical_v2 = {
            "contract_version": MIMO_V2_CONTRACT_VERSION,
            "summary": normalized.get("summary"),
            "confidence": normalized.get("confidence", row.confidence),
            "intents": normalized.get("intents", []),
            "evidence": {
                "text": text_evidence,
                "images": images if isinstance(images, list) else [],
                "conflicts": image_evidence.get("conflicts", []),
            },
        }
        input_kind = (
            "text+image" if canonical_v2["evidence"]["images"] else "text"
        )
        try:
            parsed = parse_mimo_v2_payload(canonical_v2)
            adapted = adapt_mimo_v2_to_current_payload(parsed)
        except (KeyError, TypeError, ValueError) as exc:
            return (
                MimoAuthoritativeResult(
                    raw_message_id=int(raw_message_id),
                    payload={},
                    input_kind=input_kind,
                    model=row.model,
                    status="识别失败",
                    error_message=f"stored MiMo v2 evidence invalid: {exc}",
                    contract_version=MIMO_V2_CONTRACT_VERSION,
                    run_id=row.mimo_recognition_run_id,
                ),
                row,
            )
        try:
            prompt_versions = json.loads(row.prompt_versions_json or "{}")
        except (TypeError, json.JSONDecodeError):
            prompt_versions = {}
        projection_fingerprint = None
        if row.mimo_recognition_run_id is None:
            return (
                MimoAuthoritativeResult(
                    raw_message_id=int(raw_message_id),
                    payload={},
                    input_kind=input_kind,
                    model=row.model,
                    status="识别失败",
                    error_message="stored MiMo v2 evidence provenance invalid",
                    contract_version=MIMO_V2_CONTRACT_VERSION,
                ),
                row,
            )
        else:
            with session_factory() as session:
                run = session.get(
                    MimoRecognitionRun,
                    int(row.mimo_recognition_run_id),
                )
                if (
                    run is None
                    or int(run.raw_message_id) != int(raw_message_id)
                    or run.contract_version != MIMO_V2_CONTRACT_VERSION
                    or run.status != "completed"
                    or not run.became_authoritative
                    or run.model != row.model
                    or run.prompt_versions_json != row.prompt_versions_json
                    or run.canonical_payload_fingerprint
                    != adapted.canonical_v2_fingerprint
                    or run.projection_fingerprint
                    != canonical_json_fingerprint(
                        _execution_projection(adapted.payload)
                    )
                ):
                    return (
                        MimoAuthoritativeResult(
                            raw_message_id=int(raw_message_id),
                            payload={},
                            input_kind=input_kind,
                            model=row.model,
                            status="识别失败",
                            error_message=(
                                "stored MiMo v2 evidence provenance invalid"
                            ),
                            contract_version=MIMO_V2_CONTRACT_VERSION,
                            run_id=row.mimo_recognition_run_id,
                        ),
                        row,
                    )
                projection_fingerprint = run.projection_fingerprint
        return (
            MimoAuthoritativeResult(
                raw_message_id=int(raw_message_id),
                payload=adapted.payload,
                input_kind=input_kind,
                model=row.model,
                status=str(
                    adapted.payload.get("recognition_result") or "非策略"
                ),
                prompt_versions=(
                    prompt_versions
                    if isinstance(prompt_versions, dict)
                    else {}
                ),
                contract_version=MIMO_V2_CONTRACT_VERSION,
                run_id=row.mimo_recognition_run_id,
                projection_fingerprint=projection_fingerprint,
            ),
            row,
        )
    payload = {
        "recognition_result": normalized.get("recognition_result"),
        "reason": normalized.get("reason"),
        "summary": normalized.get("summary"),
        "confidence": normalized.get("confidence", row.confidence),
        "strategy": normalized.get("strategy", {}),
        "lifecycle_event": normalized.get("lifecycle_event", {}),
        "evidence": {
            "text": text_evidence,
            "images": images if isinstance(images, list) else [],
            "conflicts": normalized.get("conflicts", []),
        },
    }
    entry_context = normalized.get("entry_context")
    if isinstance(entry_context, dict):
        payload["entry_context"] = entry_context
    rejection_reason = normalized.get("entry_context_rejection_reason")
    if rejection_reason:
        payload["entry_context_rejection_reason"] = str(rejection_reason)
    entry_fragments = normalized.get("entry_fragments")
    if isinstance(entry_fragments, list):
        payload["entry_fragments"] = entry_fragments
    rejected_count = normalized.get("entry_fragments_rejected_count")
    if isinstance(rejected_count, int) and rejected_count > 0:
        payload["entry_fragments_rejected_count"] = rejected_count
    status = str(payload.get("recognition_result") or "")
    if status not in {"是策略", "非策略"}:
        lifecycle = payload["lifecycle_event"]
        status = (
            "是策略"
            if payload["strategy"]
            or (
                isinstance(lifecycle, dict)
                and str(lifecycle.get("event_type") or "none") != "none"
            )
            else "非策略"
        )
        payload["recognition_result"] = status
    try:
        prompt_versions = json.loads(row.prompt_versions_json or "{}")
    except (TypeError, json.JSONDecodeError):
        prompt_versions = {}
    return (
        MimoAuthoritativeResult(
            raw_message_id=int(raw_message_id),
            payload=payload,
            input_kind="text+image" if payload["evidence"]["images"] else "text",
            model=row.model,
            status=status,
            prompt_versions=(
                prompt_versions if isinstance(prompt_versions, dict) else {}
            ),
        ),
        row,
    )


def _resolved_mimo_result(
    mimo: MimoAuthoritativeResult,
    decision: ContextResolutionDecision,
    candidates: Sequence[StrategyThreadCandidate],
    *,
    current_message_id: int,
    exact_risk_reduction_authorized: bool | None = None,
) -> MimoAuthoritativeResult:
    payload = dict(mimo.payload)
    original_lifecycle_event = payload.get("lifecycle_event")
    if isinstance(original_lifecycle_event, Mapping):
        original_lifecycle_event = dict(original_lifecycle_event)
        original_lifecycle_event.pop(EXACT_CONTEXT_RISK_REDUCTION_MARKER, None)
        payload["lifecycle_event"] = original_lifecycle_event
    original_strategy = mimo.payload.get("strategy")
    context_payload = decision.to_dict()
    payload["_context_resolution"] = context_payload
    if decision.decision == "new_thread":
        return replace(mimo, payload=payload)
    if exact_risk_reduction_authorized is None:
        exact_risk_reduction_authorized = _authorizes_exact_context_risk_reduction(
            decision,
            candidates,
            current_message_id=current_message_id,
        )
    if (
        decision.confidence < 0.7
        and not exact_risk_reduction_authorized
    ) or decision.decision in {"hold", "unresolved"}:
        payload.update(
            recognition_result="非策略",
            reason=decision.reason or "context resolution produced no executable action",
            strategy={},
            lifecycle_event={"event_type": "none", "confidence": 0.0},
            confidence=decision.confidence,
        )
        return replace(mimo, payload=payload, status="非策略")
    candidates_by_thread = {
        candidate.thread_id: candidate for candidate in candidates
    }
    selected_candidates = [
        candidates_by_thread[thread_id]
        for thread_id in decision.target_thread_ids
        if thread_id in candidates_by_thread
    ]
    if len(selected_candidates) != len(decision.target_thread_ids):
        raise ContextResolutionError("resolved_lifecycle_missing")
    lifecycle_ids = [candidate.lifecycle_id for candidate in selected_candidates]
    payload["instructions"] = _resolved_instruction_payloads(
        payload.get("instructions"),
        decision=decision,
        selected_candidates=selected_candidates,
    )
    if decision.decision == "revise_thread":
        context_payload["replacement_strategy"] = dict(
            original_strategy if isinstance(original_strategy, dict) else {}
        )
        payload.update(
            recognition_result="非策略",
            reason=decision.reason or "strategy revision requires revision planner",
            strategy={},
            lifecycle_event={"event_type": "none", "confidence": 0.0},
            confidence=decision.confidence,
        )
        return replace(mimo, payload=payload, status="非策略")
    event_type = {
        "manage_thread": "position_update",
        "cancel_thread": "cancel_entry",
        "exit_thread": "exit_position",
    }[decision.decision]
    lifecycle_event: dict[str, Any] = {
        "event_type": event_type,
        "target_lifecycle_id": lifecycle_ids[0],
        "confidence": decision.confidence,
        "reason": decision.reason,
    }
    if len(lifecycle_ids) > 1:
        lifecycle_event["targets"] = [
            {
                "target_lifecycle_id": candidate.lifecycle_id,
                "symbol": candidate.symbol,
                "side": candidate.side,
            }
            for candidate in selected_candidates
        ]
    if decision.management_action is not None:
        lifecycle_event["management_action"] = decision.management_action
    if exact_risk_reduction_authorized:
        lifecycle_event[EXACT_CONTEXT_RISK_REDUCTION_MARKER] = True
    payload.update(
        recognition_result="非策略",
        reason=decision.reason,
        strategy={},
        lifecycle_event=lifecycle_event,
        confidence=decision.confidence,
    )
    return replace(mimo, payload=payload, status="非策略")


def _resolved_instruction_payloads(
    value: Any,
    *,
    decision: ContextResolutionDecision,
    selected_candidates: Sequence[StrategyThreadCandidate],
) -> list[dict[str, Any]]:
    """Attach exact targets only to the management instruction being resolved."""

    if not isinstance(value, list):
        return []
    rows = [dict(row) for row in value if isinstance(row, Mapping)]
    expected_kind = {
        "revise_thread": "replace_entry",
        "cancel_thread": "cancel_pending_entry",
        "exit_thread": (
            "partial_exit"
            if decision.management_action == "exit_partial"
            else "full_exit"
        ),
        "manage_thread": decision.management_action,
    }.get(decision.decision)
    if expected_kind is None:
        return rows
    aliases = {
        "exit_full": "full_exit",
        "exit_partial": "partial_exit",
        "cancel_entry": "cancel_pending_entry",
    }
    matches = [
        row
        for row in rows
        if aliases.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
        == aliases.get(str(expected_kind), str(expected_kind))
    ]
    if len(matches) != 1:
        return rows
    target_row = matches[0]
    if len(selected_candidates) == 1:
        candidate = selected_candidates[0]
        target_row["target"] = {
            "lifecycle_id": int(candidate.lifecycle_id),
            "thread_id": int(candidate.thread_id),
        }
    else:
        target_row["targets"] = [
            {
                "lifecycle_id": int(candidate.lifecycle_id),
                "thread_id": int(candidate.thread_id),
                "symbol": candidate.symbol,
                "side": candidate.side,
            }
            for candidate in selected_candidates
        ]
    return rows


def _authorizes_exact_context_risk_reduction(
    decision: ContextResolutionDecision,
    candidates: Sequence[StrategyThreadCandidate],
    *,
    current_message_id: int,
) -> bool:
    """Authorize only one fully evidenced, exact, risk-reducing exit."""

    if (
        decision.decision != "exit_thread"
        or decision.management_action != "exit_full"
        or not 0.60 <= float(decision.confidence) < 0.70
        or len(decision.target_thread_ids) != 1
    ):
        return False
    target_thread_id = int(decision.target_thread_ids[0])
    matching = [
        candidate
        for candidate in candidates
        if int(candidate.thread_id) == target_thread_id
    ]
    if len(matching) != 1 or matching[0].risk_state != "current_risk":
        return False
    if any(
        candidate.risk_state != "no_current_risk"
        for candidate in candidates
        if int(candidate.thread_id) != target_thread_id
    ):
        return False
    supporting = {int(message_id) for message_id in decision.supporting_message_ids}
    return {
        int(current_message_id),
        int(matching[0].root_message_id),
    }.issubset(supporting)


def _authorizes_exact_context_risk_reduction_from_db(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    decision: ContextResolutionDecision,
    current_message_id: int,
) -> bool:
    """Recheck the narrow exception against every active same-chat thread."""

    if (
        decision.decision != "exit_thread"
        or decision.management_action != "exit_full"
        or not 0.60 <= float(decision.confidence) < 0.70
        or len(decision.target_thread_ids) != 1
    ):
        return False
    with session_factory() as session:
        allowed, root_message_id = exact_single_current_risk_thread(
            session,
            raw_message_id=int(raw_message_id),
            target_thread_id=int(decision.target_thread_ids[0]),
        )
    if not allowed or root_message_id is None:
        return False
    supporting = {int(message_id) for message_id in decision.supporting_message_ids}
    return {int(current_message_id), int(root_message_id)}.issubset(supporting)


def _link_context_resolution(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    evidence_version_id: int,
    decision: ContextResolutionDecision,
) -> None:
    relation_kind = {
        "revise_thread": "revision",
        "manage_thread": "management",
        "cancel_thread": "cancellation",
        "exit_thread": "exit",
    }.get(decision.decision)
    if relation_kind is None:
        return
    for thread_id in decision.target_thread_ids:
        link_message_to_strategy_thread(
            session_factory,
            strategy_thread_id=thread_id,
            raw_message_id=raw_message_id,
            relation_kind=relation_kind,
            resolver="deepseek_context",
            confidence=decision.confidence,
            decision_version="context-resolution-v1",
            message_evidence_version_id=evidence_version_id,
            evidence={
                "supporting_message_ids": list(decision.supporting_message_ids),
                "opposing_message_ids": list(decision.opposing_message_ids),
                "conflict_types": list(decision.conflict_types),
                "reason": decision.reason,
            },
        )


def assess_message_authoritatively(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    context_resolver=None,
    exchange_state_provider=None,
    reuse_current_evidence: bool = False,
) -> AuthoritativeAssessment:
    saved = (
        _load_current_mimo_evidence_result(session_factory, raw_message_id)
        if reuse_current_evidence
        else None
    )
    if saved is not None:
        mimo, evidence_row = saved
    else:
        input_fingerprint = build_current_message_input_fingerprint(
            session_factory,
            raw_message_id,
            media_root=media_root,
        )
        claim_token = claim_message_evidence_extraction(
            raw_message_id=raw_message_id,
            session_factory=session_factory,
            input_fingerprint=input_fingerprint,
        )
        if claim_token is None:
            raise RuntimeError("message evidence extraction already in progress")
        try:
            context_text = build_authoritative_context_for_message(
                session_factory,
                raw_message_id,
            )
            settings = load_trading_settings(session_factory)
            use_v2 = _mimo_v2_is_eligible(
                session_factory,
                raw_message_id=raw_message_id,
                settings=settings,
            )
            selected_v2 = None
            terminal_v2_failure = False
            if use_v2:
                v2 = infer_mimo_authoritative_v2(
                    session_factory,
                    raw_message_id=raw_message_id,
                    config=ai_recognition_config,
                    media_root=media_root,
                    context_text=context_text,
                )
                if v2.succeeded:
                    if v2.parsed_result is None or v2.adapted_result is None:
                        raise RuntimeError(
                            "successful MiMo v2 result is incomplete"
                        )
                    record_mimo_v2_outcome(
                        session_factory,
                        outcome="success",
                    )
                    selected_v2 = v2
                    mimo = MimoAuthoritativeResult(
                        raw_message_id=int(raw_message_id),
                        payload=v2.adapted_result.payload,
                        input_kind=v2.input_kind,
                        model=v2.model,
                        status=str(
                            v2.adapted_result.payload.get(
                                "recognition_result"
                            )
                            or "非策略"
                        ),
                        prompt_versions=dict(v2.prompt_versions),
                        contract_version=MIMO_V2_CONTRACT_VERSION,
                        run_id=int(v2.run_id),
                        projection_fingerprint=(
                            v2.adapted_result.projection_fingerprint
                        ),
                    )
                else:
                    _record_v2_circuit_failure(
                        session_factory,
                        error_code=v2.error_code,
                    )
                    if v2.error_code in MIMO_V2_FALLBACK_ERROR_CODES:
                        mimo = _run_v1_authority_with_audit(
                            session_factory,
                            raw_message_id=raw_message_id,
                            ai_recognition_config=ai_recognition_config,
                            media_root=media_root,
                            context_text=context_text,
                            input_fingerprint=input_fingerprint,
                            run_kind="v1_fallback",
                            retry_of_run_id=int(v2.run_id),
                            fallback_from=MIMO_V2_CONTRACT_VERSION,
                        )
                    else:
                        terminal_v2_failure = True
                        mimo = MimoAuthoritativeResult(
                            raw_message_id=int(raw_message_id),
                            payload={},
                            input_kind=v2.input_kind,
                            model=v2.model,
                            status="识别失败",
                            error_message=(
                                v2.error_message
                                or "MiMo v2 analysis failed"
                            ),
                            prompt_versions=dict(v2.prompt_versions),
                            contract_version=MIMO_V2_CONTRACT_VERSION,
                            run_id=int(v2.run_id),
                        )
            else:
                mimo = _run_v1_authority_with_audit(
                    session_factory,
                    raw_message_id=raw_message_id,
                    ai_recognition_config=ai_recognition_config,
                    media_root=media_root,
                    context_text=context_text,
                    input_fingerprint=input_fingerprint,
                    run_kind="v1_authoritative",
                )
            if build_current_message_input_fingerprint(
                session_factory,
                raw_message_id,
                media_root=media_root,
            ) != input_fingerprint:
                raise RuntimeError(
                    "message input changed during evidence extraction"
                )
            if selected_v2 is not None:
                evidence_row = finalize_claimed_mimo_v2_message_evidence(
                    session_factory,
                    raw_message_id=raw_message_id,
                    claim_token=claim_token,
                    expected_input_fingerprint=input_fingerprint,
                    result=selected_v2.parsed_result,
                    run_id=int(selected_v2.run_id),
                    model=mimo.model,
                    prompt_versions=mimo.prompt_versions,
                    media_root=media_root,
                )
            elif terminal_v2_failure:
                evidence_row = None
            else:
                evidence_row = finalize_claimed_mimo_message_evidence(
                    session_factory,
                    raw_message_id=raw_message_id,
                    claim_token=claim_token,
                    expected_input_fingerprint=input_fingerprint,
                    payload=mimo.payload,
                    input_kind=mimo.input_kind,
                    model=mimo.model,
                    prompt_versions=mimo.prompt_versions,
                    error_message=mimo.error_message,
                    media_root=media_root,
                    mimo_recognition_run_id=mimo.run_id,
                )
            if evidence_row is None and not terminal_v2_failure:
                raise RuntimeError(
                    "message evidence finalize refused stale input or claim"
                )
        finally:
            release_message_evidence_extraction_claim(
                session_factory,
                raw_message_id=raw_message_id,
                claim_token=claim_token,
            )
    context_decision = None
    context_triggers: tuple[str, ...] = ()
    if not mimo.error_message and mimo.status != "识别失败":
        evidence, context_window, candidates = _load_resolution_inputs(
            session_factory,
            raw_message_id=raw_message_id,
            first_pass_payload=mimo.payload,
            evidence_row=evidence_row,
        )
        needs_resolution, context_triggers = requires_context_resolution(
            first_pass_payload=mimo.payload,
            evidence=evidence,
            context_window=context_window,
            candidates=candidates,
        )
        if needs_resolution and context_resolver is not None:
            try:
                context_decision = context_resolver(
                    session_factory=session_factory,
                    raw_message_id=raw_message_id,
                    ai_recognition_config=ai_recognition_config,
                    evidence=evidence,
                    context_window=asdict(context_window),
                    candidates=[asdict(candidate) for candidate in candidates],
                    first_pass_payload=mimo.payload,
                    exchange_state=(
                        _load_exchange_state(
                            exchange_state_provider,
                            raw_message_id,
                            candidates,
                        )
                    ),
                )
                mimo = _resolved_mimo_result(
                    mimo,
                    context_decision,
                    candidates,
                    current_message_id=int(context_window.current.message_id),
                    exact_risk_reduction_authorized=(
                        _authorizes_exact_context_risk_reduction_from_db(
                            session_factory,
                            raw_message_id=raw_message_id,
                            decision=context_decision,
                            current_message_id=int(
                                context_window.current.message_id
                            ),
                        )
                    ),
                )
                _link_context_resolution(
                    session_factory,
                    raw_message_id=raw_message_id,
                    evidence_version_id=int(evidence_row.id),
                    decision=context_decision,
                )
            except Exception:
                mimo = replace(
                    mimo,
                    payload={},
                    status="识别失败",
                    error_message="context resolution failed",
                )
    if mimo.error_message or mimo.status == "识别失败":
        agreement_status, differences = "authoritative_failed", []
    else:
        agreement_status, differences = "pending", []
    decision = RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind=mimo.input_kind,
        authoritative_model=mimo.model,
        authoritative_status=mimo.status,
        authoritative_payload=mimo.payload,
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status=agreement_status,
        differences=differences,
        prompt_versions={"mimo": mimo.prompt_versions},
    )
    if agreement_status == "authoritative_failed":
        # A failed authority is auditable and alertable, but there is no valid
        # MiMo decision for the semantic comparison worker to review.
        saved = save_terminal_authoritative_decision(session_factory, decision)
    else:
        saved = save_pending_authoritative_decision(session_factory, decision)
        if saved.comparison_claim_token:
            settings = load_trading_settings(session_factory)
            persist_authoritative_entry_preamble(
                session_factory,
                raw_message_id=int(raw_message_id),
                evidence_version_id=int(evidence_row.id),
                recognition_generation=str(saved.comparison_claim_token),
                payload=mimo.payload,
                mode=settings.entry_preamble_mode,
                now=utc_now(),
            )
            persist_authoritative_entry_fragments(
                session_factory,
                raw_message_id=int(raw_message_id),
                evidence_version_id=int(evidence_row.id),
                recognition_generation=str(saved.comparison_claim_token),
                payload=mimo.payload,
                mode=settings.entry_message_assembly_v2_mode,
                now=utc_now(),
            )
    return AuthoritativeAssessment(
        raw_message_id=raw_message_id,
        mimo=mimo,
        # A completed prior review remains in the audit row, but never enters
        # the synchronous return payload or execution decision.
        deepseek_payload=None,
        agreement_status=saved.agreement_status,
        differences=list(json.loads(saved.differences_json or "[]")),
        semantic_review_status=saved.comparison_status,
        authoritative_generation=(
            saved.comparison_claim_token
            if saved.comparison_status == "execution_pending"
            else None
        ),
        context_resolution=context_decision,
        context_resolution_triggers=context_triggers,
    )


def _mimo_v2_is_eligible(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    settings,
) -> bool:
    if settings.mimo_contract_mode != "v2_live_adapter":
        return False
    if int(raw_message_id) <= int(
        settings.mimo_v2_activation_after_raw_message_id
    ):
        return False
    return not load_mimo_contract_circuit(session_factory).is_open


def _record_v2_circuit_failure(
    session_factory: sessionmaker,
    *,
    error_code: str | None,
) -> None:
    if error_code in MIMO_V2_FALLBACK_ERROR_CODES:
        record_mimo_v2_outcome(
            session_factory,
            outcome=str(error_code),
        )


def _run_v1_authority_with_audit(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    context_text: str,
    input_fingerprint: str,
    run_kind: str,
    retry_of_run_id: int | None = None,
    fallback_from: str | None = None,
) -> MimoAuthoritativeResult:
    model = _configured_mimo_model_name(ai_recognition_config)
    input_kind = _message_input_kind(session_factory, raw_message_id)
    started_at = utc_now()
    started = time.perf_counter()
    try:
        mimo = run_mimo_authoritative_for_message(
            session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=ai_recognition_config,
            media_root=media_root,
            context_text=context_text,
        )
    except Exception as exc:
        completed_at = utc_now()
        run = start_mimo_run(
            session_factory,
            raw_message_id=int(raw_message_id),
            run_kind=run_kind,
            contract_version="v1",
            model=model,
            input_kind=input_kind,
            input_fingerprint=input_fingerprint,
            prompt_versions={},
            retry_of_run_id=retry_of_run_id,
            started_at=started_at,
        )
        record_mimo_attempt(
            session_factory,
            run_id=run.id,
            ordinal=1,
            status="http_error",
            error_code="v1_provider_error",
            error_message=str(exc),
            duration_ms=max(
                0,
                round((time.perf_counter() - started) * 1000),
            ),
            started_at=started_at,
            completed_at=completed_at,
        )
        complete_mimo_run(
            session_factory,
            run_id=run.id,
            status="failed",
            selected_ordinal=None,
            final_error_code="v1_provider_error",
            final_error_message=str(exc),
            completed_at=completed_at,
        )
        raise

    run = start_mimo_run(
        session_factory,
        raw_message_id=int(raw_message_id),
        run_kind=run_kind,
        contract_version="v1",
        model=mimo.model or model,
        input_kind=mimo.input_kind or input_kind,
        input_fingerprint=input_fingerprint,
        prompt_versions=mimo.prompt_versions,
        retry_of_run_id=retry_of_run_id,
        started_at=started_at,
    )
    failed = bool(mimo.error_message) or mimo.status == "识别失败"
    completed_at = utc_now()
    attempt = record_mimo_attempt(
        session_factory,
        run_id=run.id,
        ordinal=1,
        status="http_error" if failed else "completed",
        error_code="v1_authoritative_failed" if failed else None,
        error_message=(
            mimo.error_message
            or ("MiMo v1 recognition failed" if failed else None)
        ),
        response_payload=mimo.payload if not failed else None,
        duration_ms=max(
            0,
            round((time.perf_counter() - started) * 1000),
        ),
        started_at=started_at,
        completed_at=completed_at,
    )
    if failed:
        completed = complete_mimo_run(
            session_factory,
            run_id=run.id,
            status="failed",
            selected_ordinal=None,
            final_error_code="v1_authoritative_failed",
            final_error_message=(
                mimo.error_message or "MiMo v1 recognition failed"
            ),
            completed_at=completed_at,
        )
    else:
        completed = complete_mimo_run(
            session_factory,
            run_id=run.id,
            status="completed",
            selected_ordinal=attempt.ordinal,
            canonical_payload=mimo.payload,
            projection_payload=mimo.payload,
            became_authoritative=True,
            completed_at=completed_at,
        )
    return replace(
        mimo,
        contract_version="v1",
        run_id=int(completed.id),
        fallback_from=fallback_from,
        projection_fingerprint=completed.projection_fingerprint,
    )


def _configured_mimo_model_name(config: AiRecognitionConfig) -> str:
    for model in config.ai_models:
        if model.id == "mimo-v2.5" or model.model == "mimo-v2.5":
            return model.model or "mimo-v2.5"
    for provider in (config.image_provider, config.text_provider):
        if provider.model.strip():
            return provider.model.strip()
    return "mimo-v2.5"


def _message_input_kind(
    session_factory: sessionmaker,
    raw_message_id: int,
) -> str:
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .all()
        )
    has_text = bool((raw_message.text or "").strip())
    has_image = any(
        "photo" in str(asset.kind or "").lower()
        or "image" in str(asset.kind or "").lower()
        or str(asset.mime_type or "").lower().startswith("image/")
        for asset in assets
    )
    if has_text and has_image:
        return "text+image"
    if has_image:
        return "image"
    if has_text:
        return "text"
    return "empty"


def apply_authoritative_assessment(
    session_factory: sessionmaker,
    assessment: AuthoritativeAssessment,
) -> MessageRecognitionResult:
    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=assessment.raw_message_id,
        payload=assessment.mimo.payload,
        model=assessment.mimo.model,
        error_message=assessment.mimo.error_message,
        authoritative_generation=assessment.authoritative_generation,
        _exact_context_risk_reduction_authorized=(
            assessment.context_resolution is not None
            and assessment.context_resolution.decision == "exit_thread"
            and isinstance(assessment.mimo.payload.get("lifecycle_event"), dict)
            and assessment.mimo.payload["lifecycle_event"].get(
                EXACT_CONTEXT_RISK_REDUCTION_MARKER
            )
            is True
        ),
    )
    if result.status == "是策略":
        _ensure_entry_strategy_thread(
            session_factory,
            raw_message_id=assessment.raw_message_id,
        )
    elif (
        assessment.context_resolution is not None
        and assessment.context_resolution.decision == "revise_thread"
        and assessment.context_resolution.confidence >= 0.7
    ):
        _project_context_revision_instruction(
            session_factory,
            assessment=assessment,
        )
    execution_settings = None
    try:
        execution_settings = load_trading_settings(session_factory)
        project_instruction_execution_contracts(
            session_factory,
            raw_message_id=assessment.raw_message_id,
            settings=execution_settings,
            projected_at=utc_now(),
        )
    except Exception as exc:
        logger.warning(
            "Instruction execution shadow projection failed open: "
            "raw_message_id=%s error=%s",
            int(assessment.raw_message_id),
            type(exc).__name__,
        )
    if execution_settings is not None:
        try:
            reconcile_due_entry_admissions(
                session_factory,
                now=utc_now(),
                limit=10,
                execution_contract_mode=(
                    execution_settings.instruction_execution_contract_mode
                ),
                entry_after_item_id=(
                    execution_settings.instruction_execution_entry_after_item_id
                ),
            )
        except Exception as exc:
            logger.warning(
                "Entry admission reconciliation failed open: error=%s",
                type(exc).__name__,
            )
    return result


def _project_context_revision_instruction(
    session_factory: sessionmaker,
    *,
    assessment: AuthoritativeAssessment,
) -> None:
    decision = assessment.context_resolution
    if decision is None or len(decision.target_thread_ids) != 1:
        return
    context_payload = assessment.mimo.payload.get("_context_resolution")
    context_payload = context_payload if isinstance(context_payload, dict) else {}
    replacement = context_payload.get("replacement_strategy")
    replacement = replacement if isinstance(replacement, dict) else {}
    with session_factory() as session:
        thread = session.get(StrategyThread, int(decision.target_thread_ids[0]))
        if thread is None or thread.current_lifecycle_id is None:
            return
        lifecycle = session.get(
            StrategyLifecycle,
            int(thread.current_lifecycle_id),
        )
        if lifecycle is None:
            return
        candidate = (
            session.query(SignalCandidate)
            .filter(
                SignalCandidate.raw_message_id == assessment.raw_message_id,
                SignalCandidate.event_type == "strategy_revision",
                SignalCandidate.target_lifecycle_id == lifecycle.id,
                SignalCandidate.parse_source == "mimo_authoritative",
            )
            .one_or_none()
        )
        if candidate is None:
            candidate = SignalCandidate(
                raw_message_id=assessment.raw_message_id,
                event_type="strategy_revision",
                target_lifecycle_id=lifecycle.id,
                parse_source="mimo_authoritative",
            )
            session.add(candidate)
        candidate.symbol = lifecycle.symbol
        candidate.side = lifecycle.side
        candidate.management_action = "replace_entry"
        candidate.entry_text = _string_or_none(replacement.get("entry"))
        candidate.stop_loss_text = _string_or_none(replacement.get("stop_loss"))
        candidate.take_profit_text = _string_or_none(replacement.get("take_profit"))
        candidate.leverage_text = _string_or_none(replacement.get("leverage"))
        candidate.confidence = float(decision.confidence)
        candidate.recognition_generation = assessment.authoritative_generation
        session.flush()
        create_message_instruction_items_in_session(
            session,
            raw_message_id=assessment.raw_message_id,
            candidate_ids={int(candidate.id)},
        )
        session.commit()


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _ensure_entry_strategy_thread(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> None:
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            return
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.chat_id == int(raw_message.chat_id),
                StrategyLifecycle.message_id == int(raw_message.message_id),
            )
            .one_or_none()
        )
        if lifecycle is None:
            return
        lifecycle_id = int(lifecycle.id)
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=int(thread.id),
        raw_message_id=int(raw_message_id),
        relation_kind="root",
        resolver="authoritative_entry",
        confidence=1.0,
        decision_version="context-resolution-v1",
    )


def process_authoritative_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    auto_trade_executor=None,
    context_resolver=None,
    exchange_state_provider=None,
    reuse_current_evidence: bool = False,
) -> AuthoritativeProcessingResult:
    """Gate review until MiMo application and automation persistence finish."""

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_recognition_config,
        media_root=media_root,
        context_resolver=context_resolver,
        exchange_state_provider=exchange_state_provider,
        reuse_current_evidence=reuse_current_evidence,
    )
    if assessment.agreement_status != "authoritative_failed":
        if assessment.authoritative_generation is None:
            raise RuntimeError("authoritative execution generation is missing")
        if not claim_authoritative_execution(
            session_factory,
            raw_message_id=raw_message_id,
            authoritative_generation=assessment.authoritative_generation,
        ):
            raise RuntimeError(
                "authoritative execution claim failed for stale generation"
            )
    recognition = apply_authoritative_assessment(session_factory, assessment)
    from telegram_kol_research.source_message_deletion import (
        source_execution_barrier,
    )

    barrier = source_execution_barrier(
        session_factory,
        raw_message_id=raw_message_id,
    )
    if barrier.status == "block":
        automation = {"status": "blocked", "reason": barrier.reason}
    elif barrier.status == "hold":
        automation = {"status": "deferred", "reason": barrier.reason}
    elif assessment.agreement_status == "authoritative_failed":
        automation = {
            "status": "skipped",
            "reason": "mimo_authoritative_failed",
        }
    elif recognition.status == "识别失败":
        automation = {
            "status": "skipped",
            "reason": "mimo_authoritative_not_safely_applied",
        }
    elif not _has_current_mimo_candidate(session_factory, raw_message_id):
        automation = {"status": "skipped", "reason": "mimo_no_action"}
    elif auto_trade_executor is None:
        automation = {"status": "skipped", "reason": "auto_trade_not_configured"}
    else:
        outcome = auto_trade_executor(raw_message_id)
        automation = outcome if isinstance(outcome, dict) else {
            "status": "completed",
            "reason": None,
        }
    automation_status = str(automation.get("status") or "unknown")
    automation_reason = (
        str(automation.get("reason"))
        if automation.get("reason") is not None
        else None
    )
    if assessment.agreement_status == "authoritative_failed":
        update_recognition_execution_outcome(
            session_factory,
            raw_message_id=raw_message_id,
            automation_status=automation_status,
            automation_reason=automation_reason,
        )
    else:
        finalized = finalize_authoritative_automation_outcome(
            session_factory,
            raw_message_id=raw_message_id,
            authoritative_generation=assessment.authoritative_generation,
            automation_status=automation_status,
            automation_reason=automation_reason,
        )
        assessment = replace(
            assessment,
            agreement_status=finalized.agreement_status,
            differences=list(json.loads(finalized.differences_json or "[]")),
            semantic_review_status=finalized.comparison_status,
            authoritative_generation=None,
        )
    if auto_trade_executor is not None:
        from telegram_kol_research.entry_assembly_admission import (
            claim_ready_entry_assembly_wakeups,
            finish_entry_assembly_wakeup,
        )

        wake_now = utc_now()
        wake_strategy_ids = claim_ready_entry_assembly_wakeups(
            session_factory,
            completed_raw_message_id=int(raw_message_id),
            now=wake_now,
        )
        for wake_claim in wake_strategy_ids:
            try:
                auto_trade_executor(int(wake_claim.strategy_raw_message_id))
            except Exception:
                finish_entry_assembly_wakeup(
                    session_factory,
                    attempt_id=int(wake_claim.attempt_id),
                    claim_token=str(wake_claim.claim_token),
                    succeeded=False,
                    now=utc_now(),
                )
                raise
            finish_entry_assembly_wakeup(
                session_factory,
                attempt_id=int(wake_claim.attempt_id),
                claim_token=str(wake_claim.claim_token),
                succeeded=True,
                now=utc_now(),
            )
    return AuthoritativeProcessingResult(
        assessment=assessment,
        recognition=recognition,
        automation=automation,
    )


def _has_current_mimo_candidate(session_factory: sessionmaker, raw_message_id: int) -> bool:
    with session_factory() as session:
        return (
            session.query(MessageInstructionItem.id)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .filter(MessageInstructionItem.retired_at.is_(None))
            .first()
            is not None
        )
