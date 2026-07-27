"""Run MiMo authority and enqueue successful decisions for later review."""

from __future__ import annotations

import json
import inspect
import re
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
from telegram_kol_research.message_recognition import (
    MessageRecognitionResult,
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.message_evidence import persist_mimo_message_evidence
from telegram_kol_research.models import (
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyThread,
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
    run_mimo_authoritative_for_message,
)
from telegram_kol_research.strategy_thread_candidates import (
    StrategyThreadCandidate,
    generate_strategy_thread_candidates,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)


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
    evidence = {
        "version_id": int(evidence_row.id),
        "text": text_evidence,
        "images": image_evidence.get("images", []),
        "normalized": normalized_evidence,
        "conflicts": normalized_evidence.get("conflicts", []),
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
) -> MimoAuthoritativeResult:
    payload = dict(mimo.payload)
    original_strategy = mimo.payload.get("strategy")
    context_payload = decision.to_dict()
    payload["_context_resolution"] = context_payload
    if decision.decision == "new_thread":
        return replace(mimo, payload=payload)
    if decision.confidence < 0.7 or decision.decision in {"hold", "unresolved"}:
        payload.update(
            recognition_result="非策略",
            reason=decision.reason or "context resolution produced no executable action",
            strategy={},
            lifecycle_event={"event_type": "none", "confidence": 0.0},
            confidence=decision.confidence,
        )
        return replace(mimo, payload=payload, status="非策略")
    target_lifecycles = {
        candidate.thread_id: candidate.lifecycle_id for candidate in candidates
    }
    lifecycle_ids = [
        target_lifecycles[thread_id]
        for thread_id in decision.target_thread_ids
        if thread_id in target_lifecycles
    ]
    if len(lifecycle_ids) != len(decision.target_thread_ids):
        raise ContextResolutionError("resolved thread has no current lifecycle")
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
            {"target_lifecycle_id": lifecycle_id}
            for lifecycle_id in lifecycle_ids
        ]
    if decision.management_action is not None:
        lifecycle_event["management_action"] = decision.management_action
    payload.update(
        recognition_result="非策略",
        reason=decision.reason,
        strategy={},
        lifecycle_event=lifecycle_event,
        confidence=decision.confidence,
    )
    return replace(mimo, payload=payload, status="非策略")


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
        context_text = build_authoritative_context_for_message(
            session_factory,
            raw_message_id,
        )
        mimo = run_mimo_authoritative_for_message(
            session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=ai_recognition_config,
            media_root=media_root,
            context_text=context_text,
        )
        evidence_row = persist_mimo_message_evidence(
            session_factory,
            raw_message_id=raw_message_id,
            payload=mimo.payload,
            input_kind=mimo.input_kind,
            model=mimo.model,
            prompt_versions=mimo.prompt_versions,
            error_message=mimo.error_message,
            media_root=media_root,
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
                mimo = _resolved_mimo_result(mimo, context_decision, candidates)
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
    if assessment.agreement_status == "authoritative_failed":
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
