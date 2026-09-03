"""Exact-token persistence for authoritative recognition execution attempts."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import case, update
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.authoritative_execution_schema import (
    require_recognition_execution_schema,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    RecognitionDecision,
)


_SECRET_PATTERN = re.compile(
    r"(?i)(dc-access-(?:key|sign|passphrase)|api[_-]?key|authorization|password|secret)"
    r"\s*[:=]\s*[^\s,;]+"
)


@dataclass(frozen=True)
class ExecutionOwnerIdentity:
    runtime_role: str
    instance_id: str
    pid: int
    boot_id: str
    process_start_ticks: str
    systemd_invocation_id: str | None = None


@dataclass(frozen=True)
class AuthoritativeExecutionClaim:
    attempt_id: int
    raw_message_id: int
    authoritative_generation: str
    claim_token: str


@dataclass(frozen=True)
class AuthoritativeExecutionAttemptSnapshot:
    attempt_id: int
    raw_message_id: int
    authoritative_generation: str
    status: str
    claim_token: str
    side_effect_started_at: datetime | None
    automation_status: str | None
    automation_reason: str | None
    exchange_effect: str | None
    lease_expires_at: datetime


def _bounded(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = _SECRET_PATTERN.sub(r"\1=[REDACTED]", str(value))
    return text[:limit]


def _evidence_json(value: list[dict[str, Any]] | None) -> str:
    safe: list[dict[str, Any]] = []
    for item in value or []:
        safe.append(
            {
                str(key)[:64]: (
                    int(raw)
                    if isinstance(raw, int) and not isinstance(raw, bool)
                    else _bounded(raw, 128)
                )
                for key, raw in item.items()
                if str(key).lower()
                not in {
                    "payload",
                    "request",
                    "response",
                    "headers",
                    "message_text",
                }
            }
        )
    return json.dumps(safe, sort_keys=True, separators=(",", ":"))[:4000]


def _snapshot(row: AuthoritativeExecutionAttempt) -> AuthoritativeExecutionAttemptSnapshot:
    return AuthoritativeExecutionAttemptSnapshot(
        attempt_id=int(row.id),
        raw_message_id=int(row.raw_message_id),
        authoritative_generation=str(row.authoritative_generation),
        status=str(row.status),
        claim_token=str(row.claim_token),
        side_effect_started_at=row.side_effect_started_at,
        automation_status=row.automation_status,
        automation_reason=row.automation_reason,
        exchange_effect=row.exchange_effect,
        lease_expires_at=row.lease_expires_at,
    )


def claim_authoritative_execution_attempt(
    session_factory,
    *,
    raw_message_id: int,
    authoritative_generation: str,
    owner: ExecutionOwnerIdentity,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> AuthoritativeExecutionClaim:
    """Atomically claim the decision generation and create its ownership row."""

    require_recognition_execution_schema(session_factory)
    if owner.runtime_role not in {"worker", "all"}:
        raise RuntimeError("authoritative_execution_not_owned_by_runtime_role")
    if lease_expires_at <= claimed_at:
        raise ValueError("lease expiry must be after claim time")
    token = uuid.uuid4().hex
    with session_factory() as session:
        try:
            result = session.execute(
                update(RecognitionDecision)
                .where(
                    RecognitionDecision.raw_message_id == int(raw_message_id),
                    RecognitionDecision.comparison_status == "execution_pending",
                    RecognitionDecision.comparison_claim_token
                    == str(authoritative_generation),
                )
                .values(
                    comparison_status="execution_running",
                    updated_at=claimed_at,
                )
            )
            if int(result.rowcount or 0) != 1:
                raise RuntimeError(
                    "authoritative execution claim failed for stale generation"
                )
            row = AuthoritativeExecutionAttempt(
                raw_message_id=int(raw_message_id),
                authoritative_generation=str(authoritative_generation),
                status="claimed",
                claim_token=token,
                owner_runtime_role=owner.runtime_role,
                owner_instance_id=_bounded(owner.instance_id, 64) or "missing",
                owner_pid=int(owner.pid),
                owner_boot_id=_bounded(owner.boot_id, 128) or "missing",
                owner_process_start_ticks=(
                    _bounded(owner.process_start_ticks, 64) or "missing"
                ),
                owner_systemd_invocation_id=_bounded(
                    owner.systemd_invocation_id, 128
                ),
                claimed_at=claimed_at,
                heartbeat_at=claimed_at,
                lease_expires_at=lease_expires_at,
                created_at=claimed_at,
                updated_at=claimed_at,
            )
            session.add(row)
            session.flush()
            attempt_id = int(row.id)
            session.commit()
        except (IntegrityError, RuntimeError):
            session.rollback()
            raise
    return AuthoritativeExecutionClaim(
        attempt_id=attempt_id,
        raw_message_id=int(raw_message_id),
        authoritative_generation=str(authoritative_generation),
        claim_token=token,
    )


def heartbeat_authoritative_execution_attempt(
    session_factory,
    *,
    attempt_id: int,
    claim_token: str,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    if lease_expires_at <= heartbeat_at:
        raise ValueError("lease expiry must be after heartbeat")
    with session_factory() as session:
        result = session.execute(
            update(AuthoritativeExecutionAttempt)
            .where(
                AuthoritativeExecutionAttempt.id == int(attempt_id),
                AuthoritativeExecutionAttempt.claim_token == str(claim_token),
                AuthoritativeExecutionAttempt.status.in_(
                    ("claimed", "executing", "outcome_recorded")
                ),
            )
            .values(
                heartbeat_at=heartbeat_at,
                lease_expires_at=lease_expires_at,
                updated_at=heartbeat_at,
            )
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def mark_authoritative_side_effect_started(
    session_factory,
    *,
    attempt_id: int,
    raw_message_id: int,
    authoritative_generation: str,
    claim_token: str,
    started_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        result = session.execute(
            update(AuthoritativeExecutionAttempt)
            .where(
                AuthoritativeExecutionAttempt.id == int(attempt_id),
                AuthoritativeExecutionAttempt.raw_message_id
                == int(raw_message_id),
                AuthoritativeExecutionAttempt.authoritative_generation
                == str(authoritative_generation),
                AuthoritativeExecutionAttempt.claim_token == str(claim_token),
                AuthoritativeExecutionAttempt.status == "claimed",
                AuthoritativeExecutionAttempt.side_effect_started_at.is_(None),
            )
            .values(
                status="executing",
                side_effect_started_at=started_at,
                heartbeat_at=started_at,
                updated_at=started_at,
            )
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def record_authoritative_automation_outcome(
    session_factory,
    *,
    attempt_id: int,
    claim_token: str,
    automation_status: str,
    automation_reason: str | None,
    exchange_effect: str,
    evidence_refs: list[dict[str, Any]] | None,
    recorded_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    if exchange_effect == "outcome_unknown":
        raise ValueError(
            "outcome_unknown must freeze the attempt instead of recording an outcome"
        )
    if exchange_effect not in {
        "not_started",
        "confirmed_applied",
        "confirmed_rejected",
    }:
        raise ValueError("invalid exchange effect")
    allowed_statuses = (
        ("claimed", "executing")
        if exchange_effect == "not_started"
        else ("executing",)
    )
    with session_factory() as session:
        statement = update(AuthoritativeExecutionAttempt).where(
            AuthoritativeExecutionAttempt.id == int(attempt_id),
            AuthoritativeExecutionAttempt.claim_token == str(claim_token),
            AuthoritativeExecutionAttempt.status.in_(allowed_statuses),
        )
        if exchange_effect != "not_started":
            statement = statement.where(
                AuthoritativeExecutionAttempt.side_effect_started_at.is_not(None)
            )
        result = session.execute(
            statement.values(
                status="outcome_recorded",
                exchange_effect=exchange_effect,
                automation_status=_bounded(automation_status, 32) or "unknown",
                automation_reason=_bounded(automation_reason, 256),
                evidence_refs_json=_evidence_json(evidence_refs),
                outcome_recorded_at=recorded_at,
                heartbeat_at=recorded_at,
                updated_at=recorded_at,
            )
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def fail_safe_authoritative_execution_attempt(
    session_factory,
    *,
    attempt_id: int,
    claim_token: str,
    failed_at: datetime,
    error_class: str | None,
    error_summary: str | None,
) -> bool:
    """Terminalize only an exact pre-side-effect owner and its decision."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, int(attempt_id))
        if (
            row is None
            or row.claim_token != str(claim_token)
            or row.status != "claimed"
            or row.side_effect_started_at is not None
        ):
            return False
        decision_result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == int(row.raw_message_id),
                RecognitionDecision.comparison_status == "execution_running",
                RecognitionDecision.comparison_claim_token
                == str(row.authoritative_generation),
            )
            .values(
                comparison_status="completed",
                agreement_status="review_disabled",
                comparison_claim_token=None,
                comparison_started_at=None,
                automation_status="failed",
                automation_reason=(
                    "authoritative_execution_abandoned_before_side_effect"
                ),
                updated_at=failed_at,
            )
        )
        if int(decision_result.rowcount or 0) != 1:
            session.rollback()
            return False
        row.status = "failed_safe"
        row.error_class = _bounded(error_class, 128)
        row.error_summary = _bounded(error_summary, 512)
        row.completed_at = failed_at
        row.updated_at = failed_at
        session.commit()
        return True


def mark_authoritative_execution_uncertain(
    session_factory,
    *,
    attempt_id: int,
    claim_token: str,
    uncertain_at: datetime,
    error_class: str | None,
    error_summary: str | None,
) -> bool:
    """Freeze an exact post-boundary attempt; it is never made replayable."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, int(attempt_id))
        if (
            row is None
            or row.claim_token != str(claim_token)
            or row.status != "executing"
            or row.side_effect_started_at is None
        ):
            return False
        decision_result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == int(row.raw_message_id),
                RecognitionDecision.comparison_status == "execution_running",
                RecognitionDecision.comparison_claim_token
                == str(row.authoritative_generation),
            )
            .values(
                comparison_status="execution_uncertain",
                automation_status="uncertain",
                automation_reason="authoritative_execution_outcome_unknown",
                updated_at=uncertain_at,
            )
        )
        if int(decision_result.rowcount or 0) != 1:
            session.rollback()
            return False
        row.status = "uncertain"
        row.exchange_effect = "outcome_unknown"
        row.error_class = _bounded(error_class, 128)
        row.error_summary = _bounded(error_summary, 512)
        row.uncertain_at = uncertain_at
        row.completed_at = uncertain_at
        row.updated_at = uncertain_at
        session.commit()
        return True


def finalize_recorded_authoritative_execution(
    session_factory,
    *,
    attempt_id: int,
    claim_token: str,
    semantic_review_enabled: bool,
    finalized_at: datetime,
    adapter: Callable[[], Any] | None = None,
):
    """Finalize the stored outcome only; adapter is accepted to prove it is unused."""

    del adapter
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, int(attempt_id))
        if (
            row is None
            or row.claim_token != str(claim_token)
            or row.status != "outcome_recorded"
        ):
            raise RuntimeError("authoritative outcome is not finalizeable")
        review_values = (
            {
                "comparison_status": case(
                    (
                        RecognitionDecision.agreement_status == "pending",
                        "pending",
                    ),
                    else_="completed",
                ),
                "comparison_claim_token": None,
                "comparison_started_at": None,
            }
            if semantic_review_enabled
            else {
                "agreement_status": "review_disabled",
                "comparison_status": "completed",
                "comparison_next_attempt_at": None,
                "comparison_started_at": None,
                "comparison_claim_token": None,
            }
        )
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == int(row.raw_message_id),
                RecognitionDecision.comparison_status == "execution_running",
                RecognitionDecision.comparison_claim_token
                == str(row.authoritative_generation),
            )
            .values(
                automation_status=row.automation_status,
                automation_reason=row.automation_reason,
                updated_at=finalized_at,
                **review_values,
            )
        )
        if int(result.rowcount or 0) != 1:
            session.rollback()
            raise RuntimeError(
                "authoritative generation is stale or no longer execution-running"
            )
        row.status = "succeeded"
        row.completed_at = finalized_at
        row.updated_at = finalized_at
        session.commit()
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=int(row.raw_message_id)
        ).one()
        session.expunge(decision)
        return decision


def load_authoritative_execution_attempt(
    session_factory,
    *,
    attempt_id: int,
) -> AuthoritativeExecutionAttemptSnapshot:
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, int(attempt_id))
        if row is None:
            raise LookupError(f"authoritative attempt not found: {attempt_id}")
        return _snapshot(row)


def count_owned_active_authoritative_attempts(
    session_factory,
    *,
    owner_instance_id: str,
) -> int:
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        return int(
            session.query(AuthoritativeExecutionAttempt)
            .filter(
                AuthoritativeExecutionAttempt.owner_instance_id
                == str(owner_instance_id),
                AuthoritativeExecutionAttempt.status.in_(
                    ("claimed", "executing", "outcome_recorded")
                ),
            )
            .count()
        )
