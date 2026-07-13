"""Coordinate MiMo authority with optional DeepSeek text validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.message_recognition import (
    MessageRecognitionResult,
    apply_authoritative_mimo_payload,
    infer_deepseek_auxiliary,
)
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_recognition_decision,
    update_recognition_execution_outcome,
)
from telegram_kol_research.recognition_experiments import (
    MimoAuthoritativeResult,
    run_mimo_authoritative_for_message,
)


@dataclass(frozen=True)
class AuthoritativeAssessment:
    raw_message_id: int
    mimo: MimoAuthoritativeResult
    deepseek_payload: dict[str, Any] | None
    agreement_status: str
    differences: list[str]


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
    mimo = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_recognition_config,
        media_root=media_root,
    )
    deepseek_payload = None
    if mimo.input_kind == "text":
        deepseek_payload = infer_deepseek_auxiliary(
            session_factory,
            raw_message_id=raw_message_id,
            config=ai_recognition_config,
        )
    if mimo.error_message or mimo.status == "识别失败":
        agreement_status, differences = "authoritative_failed", []
    else:
        agreement_status, differences = compare_assessments(mimo.payload, deepseek_payload)
    assessment = AuthoritativeAssessment(
        raw_message_id=raw_message_id,
        mimo=mimo,
        deepseek_payload=deepseek_payload,
        agreement_status=agreement_status,
        differences=differences,
    )
    save_recognition_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_message_id,
            input_kind=mimo.input_kind,
            authoritative_model=mimo.model,
            authoritative_status=mimo.status,
            authoritative_payload=mimo.payload,
            auxiliary_model=(
                ai_recognition_config.text_provider.model
                if deepseek_payload is not None
                else None
            ),
            auxiliary_status=(
                str(deepseek_payload.get("recognition_result") or "")
                if deepseek_payload is not None
                else None
            ),
            auxiliary_payload=deepseek_payload,
            agreement_status=agreement_status,
            differences=differences,
        ),
    )
    return assessment


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
    )


def process_authoritative_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    auto_trade_executor=None,
) -> AuthoritativeProcessingResult:
    """Apply MiMo first, then immediately hand its persisted result to automation."""

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_recognition_config,
        media_root=media_root,
    )
    recognition = apply_authoritative_assessment(session_factory, assessment)
    if assessment.agreement_status == "authoritative_failed":
        automation = {
            "status": "skipped",
            "reason": "mimo_authoritative_failed",
        }
    elif auto_trade_executor is None:
        automation = {"status": "skipped", "reason": "auto_trade_not_configured"}
    else:
        outcome = auto_trade_executor(raw_message_id)
        automation = outcome if isinstance(outcome, dict) else {
            "status": "completed",
            "reason": None,
        }
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_message_id,
        automation_status=str(automation.get("status") or "unknown"),
        automation_reason=(
            str(automation.get("reason"))
            if automation.get("reason") is not None
            else None
        ),
    )
    return AuthoritativeProcessingResult(
        assessment=assessment,
        recognition=recognition,
        automation=automation,
    )
