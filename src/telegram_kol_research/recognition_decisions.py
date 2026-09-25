"""Persistence helpers for authoritative recognition audit decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.models import RecognitionDecision, utc_now


class AuthoritativeExecutionInProgress(RuntimeError):
    """The message is already being executed, or is frozen as uncertain.

    A-6b. This is an expected state, not a fault: the guard exists so a
    reanalysis cannot overwrite a decision whose execution has crossed the
    side-effect boundary. It was a bare ``RuntimeError``, so the only way to
    tell it from a real failure was to match its message text -- and nobody
    did, which is why ``context_resolution_worker`` logged a full stack for
    every one of them, twelve in one thirty-minute window. It stays a
    ``RuntimeError`` so existing handlers behave exactly as they did.
    """

    def __init__(self, *, raw_message_id: int, comparison_status: str) -> None:
        super().__init__(
            "authoritative execution is already in progress or outcome is uncertain"
        )
        self.raw_message_id = int(raw_message_id)
        self.comparison_status = str(comparison_status)



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
    #: Outcome of the two gates that decide the contextual second pass, as
    #: ``{"outcome": ..., "triggers": [...]}``. ``None`` means the caller did
    #: not evaluate them, and the stored column stays NULL.
    context_resolution_gate: dict[str, Any] | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _context_resolution_gate_json(record: RecognitionDecisionRecord) -> str | None:
    """The gate column value for ``record``, or ``None`` when unevaluated."""

    if record.context_resolution_gate is None:
        return None
    return _json(record.context_resolution_gate)


def _save_terminal_authoritative_decision_in_session(
    session: Session,
    record: RecognitionDecisionRecord,
) -> RecognitionDecision:
    """Save fail-closed authority in the caller's transaction."""

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
            comparison_status="completed",
            context_resolution_gate_json=_context_resolution_gate_json(record),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row

    if row.comparison_status in {"execution_running", "execution_uncertain"}:
        raise AuthoritativeExecutionInProgress(
            raw_message_id=int(row.raw_message_id),
            comparison_status=str(row.comparison_status),
        )

    observed_status = row.comparison_status
    observed_token = row.comparison_claim_token
    expected_token = (
        RecognitionDecision.comparison_claim_token.is_(None)
        if observed_token is None
        else RecognitionDecision.comparison_claim_token == observed_token
    )
    terminal_values: dict[str, Any] = {
        "input_kind": record.input_kind,
        "authoritative_model": record.authoritative_model,
        "authoritative_status": record.authoritative_status,
        "authoritative_payload_json": _json(record.authoritative_payload),
        "auxiliary_model": record.auxiliary_model,
        "auxiliary_status": record.auxiliary_status,
        "auxiliary_payload_json": (
            _json(record.auxiliary_payload)
            if record.auxiliary_payload is not None
            else None
        ),
        "agreement_status": record.agreement_status,
        "differences_json": _json(record.differences),
        "prompt_versions_json": _json(record.prompt_versions),
        "comparison_status": "completed",
        "disagreement_severity": None,
        "comparison_model": None,
        "comparison_payload_json": None,
        "comparison_error": None,
        "comparison_next_attempt_at": None,
        "comparison_started_at": None,
        "comparison_claim_token": None,
        "compared_at": None,
        "updated_at": now,
    }
    # A caller that did not evaluate the gates (the recovery guard) must not
    # erase the gate an earlier recognition recorded for the same message.
    if record.context_resolution_gate is not None:
        terminal_values["context_resolution_gate_json"] = (
            _context_resolution_gate_json(record)
        )
    result = session.execute(
        update(RecognitionDecision)
        .where(
            RecognitionDecision.raw_message_id == record.raw_message_id,
            RecognitionDecision.comparison_status == observed_status,
            expected_token,
        )
        .values(**terminal_values)
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "authoritative decision changed or execution is already in progress"
        )
    return (
        session.query(RecognitionDecision)
        .filter(RecognitionDecision.raw_message_id == record.raw_message_id)
        .one()
    )


def save_terminal_authoritative_decision(
    session_factory: sessionmaker,
    record: RecognitionDecisionRecord,
) -> RecognitionDecision:
    """Atomically save fail-closed authority without displacing an executor."""

    with session_factory() as session:
        saved = _save_terminal_authoritative_decision_in_session(session, record)
        session.commit()
        session.refresh(saved)
        session.expunge(saved)
        return saved


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
                context_resolution_gate_json=_context_resolution_gate_json(record),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

        if row.comparison_status in {"execution_running", "execution_uncertain"}:
            raise AuthoritativeExecutionInProgress(
                raw_message_id=int(row.raw_message_id),
                comparison_status=str(row.comparison_status),
            )

        observed_status = row.comparison_status
        observed_token = row.comparison_claim_token
        values: dict[str, Any] = {
            "input_kind": record.input_kind,
            "authoritative_model": record.authoritative_model,
            "authoritative_status": record.authoritative_status,
            "authoritative_payload_json": payload_json,
            "comparison_status": "execution_pending",
            "comparison_started_at": None,
            "comparison_claim_token": authoritative_generation,
            "comparison_next_attempt_at": None,
            "automation_status": None,
            "automation_reason": None,
            "updated_at": now,
        }
        # As above: only a caller that actually evaluated the gates writes them.
        if record.context_resolution_gate is not None:
            values["context_resolution_gate_json"] = (
                _context_resolution_gate_json(record)
            )
        if preserve_completed_review:
            prompt_versions = json.loads(row.prompt_versions_json)
            prompt_versions.update(record.prompt_versions)
            values["prompt_versions_json"] = _json(prompt_versions)
        else:
            values.update(
                auxiliary_model=None,
                auxiliary_status=None,
                auxiliary_payload_json=None,
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json=_json(record.prompt_versions),
                disagreement_severity=None,
                comparison_model=None,
                comparison_payload_json=None,
                comparison_error=None,
                comparison_attempts=0,
                compared_at=None,
            )
        expected_token = (
            RecognitionDecision.comparison_claim_token.is_(None)
            if observed_token is None
            else RecognitionDecision.comparison_claim_token == observed_token
        )
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == record.raw_message_id,
                RecognitionDecision.comparison_status == observed_status,
                expected_token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeError(
                "authoritative decision changed or execution is already in progress"
            )
        session.commit()
        saved = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == record.raw_message_id)
            .one()
        )
        session.expunge(saved)
        return saved


def claim_authoritative_execution(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    authoritative_generation: str,
) -> bool:
    """Claim the exact persisted generation before any trading-state mutation."""

    with session_factory() as session:
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "execution_pending",
                RecognitionDecision.comparison_claim_token
                == authoritative_generation,
            )
            .values(
                comparison_status="execution_running",
                updated_at=utc_now(),
            )
        )
        session.commit()
        return result.rowcount == 1


def finalize_authoritative_automation_outcome(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    authoritative_generation: str,
    automation_status: str,
    automation_reason: str | None,
) -> RecognitionDecision:
    """Atomically publish one generation's automation result.

    The semantic-disagreement review was retired on 2026-09-25 and production
    had it switched off for its whole life, so the only branch this ever took
    is the one kept here: no review is pending, the claim is released, and the
    row lands on ``completed``. ``agreement_status`` keeps the literal
    ``"review_disabled"`` production already writes -- the message operation
    supervisor and the contract scan read that value when deciding a row is
    terminal.
    """

    with session_factory() as session:
        now = utc_now()
        lease_release_values = {
            "agreement_status": "review_disabled",
            "comparison_status": "completed",
            "comparison_next_attempt_at": None,
            "comparison_started_at": None,
            "comparison_claim_token": None,
        }
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.comparison_status == "execution_running",
                RecognitionDecision.comparison_claim_token
                == authoritative_generation,
            )
            .values(
                automation_status=automation_status,
                automation_reason=automation_reason,
                updated_at=now,
                **lease_release_values,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeError(
                "authoritative generation is stale or no longer execution-running"
            )
        session.commit()
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one()
        )
        session.expunge(row)
        return row


def _update_recognition_execution_outcome_in_session(
    session: Session,
    *,
    raw_message_id: int,
    automation_status: str,
    automation_reason: str | None,
    notification_status: str | None = None,
    notification_error: str | None = None,
) -> None:
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
        _update_recognition_execution_outcome_in_session(
            session,
            raw_message_id=raw_message_id,
            automation_status=automation_status,
            automation_reason=automation_reason,
            notification_status=notification_status,
            notification_error=notification_error,
        )
        session.commit()


def claim_authoritative_failure_notification(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    automation_status: str,
    automation_reason: str | None,
) -> bool:
    """Atomically reserve one authoritative-failure notification delivery."""

    with session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one_or_none()
        )
        if row is None:
            session.rollback()
            raise LookupError(
                f"Recognition decision not found for raw message {raw_message_id}"
            )
        observed_status = row.notification_status
        if observed_status not in {None, "failed"}:
            session.rollback()
            return False
        expected_status = (
            RecognitionDecision.notification_status.is_(None)
            if observed_status is None
            else RecognitionDecision.notification_status == observed_status
        )
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                expected_status,
            )
            .values(
                automation_status=automation_status,
                automation_reason=automation_reason,
                notification_status="scheduled",
                notification_error=None,
                updated_at=utc_now(),
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return False
        session.commit()
        return True
