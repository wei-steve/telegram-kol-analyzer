"""Run MiMo authority and enqueue successful decisions for later review."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.message_recognition import (
    MessageRecognitionResult,
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.message_evidence import persist_mimo_message_evidence
from telegram_kol_research.models import MessageInstructionItem
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


@dataclass(frozen=True)
class AuthoritativeAssessment:
    raw_message_id: int
    mimo: MimoAuthoritativeResult
    deepseek_payload: dict[str, Any] | None
    agreement_status: str
    differences: list[str]
    semantic_review_status: str = "not_applicable"
    authoritative_generation: str | None = None


@dataclass(frozen=True)
class AuthoritativeProcessingResult:
    assessment: AuthoritativeAssessment
    recognition: MessageRecognitionResult
    automation: dict[str, Any]


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


def assess_message_authoritatively(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
) -> AuthoritativeAssessment:
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
    persist_mimo_message_evidence(
        session_factory,
        raw_message_id=raw_message_id,
        payload=mimo.payload,
        input_kind=mimo.input_kind,
        model=mimo.model,
        prompt_versions=mimo.prompt_versions,
        error_message=mimo.error_message,
        media_root=media_root,
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
    )


def apply_authoritative_assessment(
    session_factory: sessionmaker,
    assessment: AuthoritativeAssessment,
) -> MessageRecognitionResult:
    return apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=assessment.raw_message_id,
        payload=assessment.mimo.payload,
        model=assessment.mimo.model,
        error_message=assessment.mimo.error_message,
        authoritative_generation=assessment.authoritative_generation,
    )


def process_authoritative_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    auto_trade_executor=None,
) -> AuthoritativeProcessingResult:
    """Gate review until MiMo application and automation persistence finish."""

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_recognition_config,
        media_root=media_root,
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
