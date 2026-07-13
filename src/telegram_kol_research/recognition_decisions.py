"""Persistence helpers for authoritative recognition audit decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, update
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
                comparison_status="pending",
                comparison_attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)

        row.input_kind = record.input_kind
        row.authoritative_model = record.authoritative_model
        row.authoritative_status = record.authoritative_status
        row.authoritative_payload_json = payload_json
        if changed:
            row.auxiliary_model = None
            row.auxiliary_status = None
            row.auxiliary_payload_json = None
            row.agreement_status = "pending"
            row.differences_json = "[]"
            row.prompt_versions_json = _json(record.prompt_versions)
            row.comparison_status = "pending"
            row.disagreement_severity = None
            row.comparison_model = None
            row.comparison_payload_json = None
            row.comparison_error = None
            row.comparison_attempts = 0
            row.comparison_next_attempt_at = None
            row.comparison_started_at = None
            row.compared_at = None
        else:
            prompt_versions = json.loads(row.prompt_versions_json)
            prompt_versions.update(record.prompt_versions)
            row.prompt_versions_json = _json(prompt_versions)
        row.updated_at = now
        session.commit()
        session.refresh(row)
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
) -> int | None:
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
            result = session.execute(
                update(RecognitionDecision)
                .where(
                    RecognitionDecision.raw_message_id == raw_message_id,
                    _claimable_review(now, stale_before),
                )
                .values(
                    comparison_status="running",
                    comparison_started_at=now,
                    comparison_next_attempt_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount == 1:
                session.commit()
                return raw_message_id
            session.rollback()


def complete_semantic_review(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    model: str,
    auxiliary_payload: dict[str, Any],
    comparison_payload: dict[str, Any],
    agreement_status: str,
    severity: str,
    differences: list[str],
    prompt_versions: dict[str, int],
    compared_at: datetime,
) -> None:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter_by(raw_message_id=raw_message_id)
            .one_or_none()
        )
        if row is None:
            raise LookupError(f"Recognition decision not found for raw message {raw_message_id}")
        if row.comparison_status != "running":
            raise RuntimeError(
                f"Semantic review for raw message {raw_message_id} is not claimed"
            )
        row.auxiliary_model = model
        row.auxiliary_status = str(auxiliary_payload.get("recognition_result") or "") or None
        row.auxiliary_payload_json = _json(auxiliary_payload)
        row.agreement_status = agreement_status
        row.differences_json = _json(differences)
        merged_prompt_versions = json.loads(row.prompt_versions_json)
        merged_prompt_versions.update(prompt_versions)
        row.prompt_versions_json = _json(merged_prompt_versions)
        row.comparison_status = "completed"
        row.disagreement_severity = severity
        row.comparison_model = model
        row.comparison_payload_json = _json(comparison_payload)
        row.comparison_error = None
        row.comparison_next_attempt_at = None
        row.comparison_started_at = None
        row.compared_at = compared_at
        row.updated_at = compared_at
        session.commit()


def fail_semantic_review(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    error: str,
    next_attempt_at: datetime | None,
) -> None:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter_by(raw_message_id=raw_message_id)
            .one_or_none()
        )
        if row is None:
            raise LookupError(f"Recognition decision not found for raw message {raw_message_id}")
        if row.comparison_status != "running":
            raise RuntimeError(
                f"Semantic review for raw message {raw_message_id} is not claimed"
            )
        row.comparison_status = "pending" if next_attempt_at is not None else "failed"
        row.comparison_error = error
        row.comparison_attempts += 1
        row.comparison_next_attempt_at = next_attempt_at
        row.comparison_started_at = None
        row.updated_at = utc_now()
        session.commit()


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
