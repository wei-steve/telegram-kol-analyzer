"""Persistence helpers for authoritative recognition audit decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
        row.automation_status = None
        row.automation_reason = None
        row.notification_status = None
        row.notification_error = None
        row.updated_at = now
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


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
