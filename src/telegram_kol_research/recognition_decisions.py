"""Persistence helpers for authoritative recognition audit decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, or_, update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RecognitionDecision, utc_now


@dataclass(frozen=True)
class RecognitionDecisionRecord:
    raw_message_id: int
    input_kind: str
    authoritative_model: str
    authoritative_status: str
    authoritative_payload: dict[str, Any]
    auxiliary_model: str | None
    auxiliary_status: str | None
    auxiliary_payload: dict[str, Any] | None
    agreement_status: str
    differences: list[str]
    prompt_versions: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticReviewClaim:
    raw_message_id: int
    token: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def save_recognition_decision(
    session_factory: sessionmaker,
    record: RecognitionDecisionRecord,
) -> RecognitionDecision:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == record.raw_message_id)
            .one_or_none()
        )
        now = utc_now()
        if row is None:
            row = RecognitionDecision(
                raw_message_id=record.raw_message_id,
                input_kind=record.input_kind,
                authoritative_model=record.authoritative_model,
                authoritative_status=record.authoritative_status,
                authoritative_payload_json=_json(record.authoritative_payload),
                agreement_status=record.agreement_status,
                differences_json=_json(record.differences),
                prompt_versions_json=_json(record.prompt_versions),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        row.input_kind = record.input_kind
        row.authoritative_model = record.authoritative_model
        row.authoritative_status = record.authoritative_status
        row.authoritative_payload_json = _json(record.authoritative_payload)
        row.auxiliary_model = record.auxiliary_model
        row.auxiliary_status = record.auxiliary_status
        row.auxiliary_payload_json = (
            _json(record.auxiliary_payload)
            if record.auxiliary_payload is not None
            else None
        )
        row.agreement_status = record.agreement_status
        row.differences_json = _json(record.differences)
        row.prompt_versions_json = _json(record.prompt_versions)
        # This compatibility save represents a terminal assessment (including
        # MiMo transport/schema failure), never work for the semantic worker.
        # Clear any older pending/running claim so stale candidates cannot win
        # a race with authoritative re-recognition.
        row.comparison_status = "completed"
        row.disagreement_severity = None
        row.comparison_model = None
        row.comparison_payload_json = None
        row.comparison_error = None
        row.comparison_next_attempt_at = None
        row.comparison_started_at = None
        row.comparison_claim_token = None
        row.compared_at = None
        row.updated_at = now
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def save_pending_authoritative_decision(
    session_factory: sessionmaker,
    record: RecognitionDecisionRecord,
) -> RecognitionDecision:
    """Persist MiMo authority before any non-authoritative comparison runs."""

    payload_json = _json(record.authoritative_payload)
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == record.raw_message_id)
            .one_or_none()
        )
        now = utc_now()
        changed = row is None or row.authoritative_payload_json != payload_json
        preserve_completed_review = (
            row is not None
            and not changed
            and row.comparison_status == "completed"
        )
        authoritative_generation = uuid4().hex
        if row is None:
            row = RecognitionDecision(
                raw_message_id=record.raw_message_id,
                input_kind=record.input_kind,
                authoritative_model=record.authoritative_model,
                authoritative_status=record.authoritative_status,
                authoritative_payload_json=payload_json,
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json=_json(record.prompt_versions),
                comparison_status="execution_pending",
                comparison_claim_token=authoritative_generation,
                comparison_attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)

        row.input_kind = record.input_kind
        row.authoritative_model = record.authoritative_model
        row.authoritative_status = record.authoritative_status
        row.authoritative_payload_json = payload_json
        if not preserve_completed_review:
            row.auxiliary_model = None
            row.auxiliary_status = None
            row.auxiliary_payload_json = None
            row.agreement_status = "pending"
            row.differences_json = "[]"
            row.prompt_versions_json = _json(record.prompt_versions)
            row.comparison_status = "execution_pending"
            row.disagreement_severity = None
            row.comparison_model = None
            row.comparison_payload_json = None
            row.comparison_error = None
            row.comparison_attempts = 0
            row.comparison_next_attempt_at = None
            row.comparison_started_at = None
            row.comparison_claim_token = authoritative_generation
            row.compared_at = None
        else:
            prompt_versions = json.loads(row.prompt_versions_json)
            prompt_versions.update(record.prompt_versions)
            row.prompt_versions_json = _json(prompt_versions)
            row.comparison_status = "execution_pending"
            row.comparison_started_at = None
            row.comparison_claim_token = authoritative_generation
            row.comparison_next_attempt_at = None
        # Automation belongs to this exact authoritative generation. Never let
        # a worker observe the outcome from an earlier execution attempt.
        row.automation_status = None
        row.automation_reason = None
        row.updated_at = now
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def finalize_authoritative_automation_outcome(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    authoritative_generation: str,
    automation_status: str,
    automation_reason: str | None,
) -> RecognitionDecision:
    """Atomically publish one generation's automation result for review."""

    with session_factory() as session:
        now = utc_now()
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "execution_pending",
                RecognitionDecision.comparison_claim_token
                == authoritative_generation,
            )
            .values(
                automation_status=automation_status,
                automation_reason=automation_reason,
                comparison_status=case(
                    (
                        RecognitionDecision.agreement_status == "pending",
                        "pending",
                    ),
                    else_="completed",
                ),
                comparison_claim_token=None,
                comparison_started_at=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeError(
                "authoritative generation is stale or no longer execution-pending"
            )
        session.commit()
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one()
        )
        session.expunge(row)
        return row


def _claimable_review(now: datetime, stale_before: datetime):
    return or_(
        and_(
            RecognitionDecision.comparison_status == "pending",
            or_(
                RecognitionDecision.comparison_next_attempt_at.is_(None),
                RecognitionDecision.comparison_next_attempt_at <= now,
            ),
        ),
        and_(
            RecognitionDecision.comparison_status == "running",
            RecognitionDecision.comparison_started_at <= stale_before,
        ),
    )


def claim_next_semantic_review(
    session_factory: sessionmaker,
    *,
    now: datetime,
    stale_before: datetime,
) -> SemanticReviewClaim | None:
    """Atomically claim the oldest eligible semantic comparison."""

    with session_factory() as session:
        while True:
            raw_message_id = session.execute(
                session.query(RecognitionDecision.raw_message_id)
                .filter(_claimable_review(now, stale_before))
                .order_by(RecognitionDecision.updated_at, RecognitionDecision.id)
                .limit(1)
                .statement
            ).scalar_one_or_none()
            if raw_message_id is None:
                return None
            claim_token = uuid4().hex
            result = session.execute(
                update(RecognitionDecision)
                .where(
                    RecognitionDecision.raw_message_id == raw_message_id,
                    _claimable_review(now, stale_before),
                )
                .values(
                    comparison_status="running",
                    comparison_started_at=now,
                    comparison_claim_token=claim_token,
                    comparison_next_attempt_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount == 1:
                session.commit()
                return SemanticReviewClaim(
                    raw_message_id=raw_message_id,
                    token=claim_token,
                )
            session.rollback()


def complete_semantic_review(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    claim_token: str,
    model: str,
    auxiliary_payload: dict[str, Any],
    comparison_payload: dict[str, Any],
    agreement_status: str,
    severity: str,
    differences: list[str],
    prompt_versions: dict[str, int],
    compared_at: datetime,
) -> bool:
    with session_factory() as session:
        prompt_versions_json = session.execute(
            session.query(RecognitionDecision.prompt_versions_json)
            .filter_by(raw_message_id=raw_message_id)
            .statement
        ).scalar_one_or_none()
        if prompt_versions_json is None:
            raise LookupError(f"Recognition decision not found for raw message {raw_message_id}")
        merged_prompt_versions = json.loads(prompt_versions_json)
        merged_prompt_versions.update(prompt_versions)
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "running",
                RecognitionDecision.comparison_claim_token == claim_token,
            )
            .values(
                auxiliary_model=model,
                auxiliary_status=(
                    str(auxiliary_payload.get("recognition_result") or "") or None
                ),
                auxiliary_payload_json=_json(auxiliary_payload),
                agreement_status=agreement_status,
                differences_json=_json(differences),
                prompt_versions_json=_json(merged_prompt_versions),
                comparison_status="completed",
                disagreement_severity=severity,
                comparison_model=model,
                comparison_payload_json=_json(comparison_payload),
                comparison_error=None,
                comparison_next_attempt_at=None,
                comparison_started_at=None,
                comparison_claim_token=None,
                compared_at=compared_at,
                updated_at=compared_at,
            )
        )
        session.commit()
        return result.rowcount == 1


def fail_semantic_review(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    claim_token: str,
    error: str,
    next_attempt_at: datetime | None,
) -> bool:
    with session_factory() as session:
        exists = session.query(RecognitionDecision.id).filter_by(
            raw_message_id=raw_message_id
        ).first()
        if exists is None:
            raise LookupError(f"Recognition decision not found for raw message {raw_message_id}")
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "running",
                RecognitionDecision.comparison_claim_token == claim_token,
            )
            .values(
                comparison_status=(
                    "pending" if next_attempt_at is not None else "failed"
                ),
                comparison_error=error,
                comparison_attempts=RecognitionDecision.comparison_attempts + 1,
                comparison_next_attempt_at=next_attempt_at,
                comparison_started_at=None,
                comparison_claim_token=None,
                updated_at=utc_now(),
            )
        )
        session.commit()
        return result.rowcount == 1


def claim_critical_notification(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    fingerprint: str,
) -> bool:
    """Reserve the single critical-review notification allowed for a message."""

    with session_factory() as session:
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "completed",
                RecognitionDecision.disagreement_severity == "critical",
                RecognitionDecision.notification_fingerprint.is_(None),
                RecognitionDecision.notification_status.is_(None),
            )
            .values(
                notification_fingerprint=fingerprint,
                notification_status="scheduled",
                notification_error=None,
                updated_at=utc_now(),
            )
        )
        session.commit()
        return result.rowcount == 1


def update_recognition_execution_outcome(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    automation_status: str,
    automation_reason: str | None,
    notification_status: str | None = None,
    notification_error: str | None = None,
) -> None:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one_or_none()
        )
        if row is None:
            raise LookupError(f"Recognition decision not found for raw message {raw_message_id}")
        row.automation_status = automation_status
        row.automation_reason = automation_reason
        if notification_status is not None:
            row.notification_status = notification_status
            row.notification_error = notification_error
        row.updated_at = utc_now()
        session.commit()
