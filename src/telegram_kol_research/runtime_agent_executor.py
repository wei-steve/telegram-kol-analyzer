"""Closed, idempotent executor for Phase 6 low-risk recovery playbooks."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    RuntimeAgentRecoveryAttempt,
    RuntimeIncident,
)
from telegram_kol_research.runtime_agent_policy import (
    ExecutionPlaybookDecision,
)
from telegram_kol_research.runtime_agent_tools import RuntimeAgentToolRegistry


_PHASE_6_ACTIONS = frozenset(
    {
        "refresh_read_only_exchange_snapshot",
        "rerun_production_audit",
        "recover_stale_side_effect_free_claim",
        "reschedule_non_writing_ai_job",
        "fetch_missing_telegram_evidence",
        "build_read_only_reconciliation_plan",
    }
)
_CIRCUIT_FAILURE_STATUSES = frozenset(
    {"verification_failed", "failed", "action_outcome_unknown"}
)


class RuntimeAgentRecoveryDeferred(RuntimeError):
    """Raised by the worker integration for transient action serialization."""


@dataclass(frozen=True, slots=True)
class RuntimeAgentExecutorConfig:
    enabled: bool = False
    circuit_breaker_threshold: int = 3
    reservation_lease_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class RuntimeAgentExecutionResult:
    status: str
    incident_id: int
    attempt_id: int | None = None
    executed: bool = False
    verified: bool = False
    refusal_reasons: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()

    def to_ledger_mapping(
        self, *, decision: ExecutionPlaybookDecision
    ) -> dict[str, Any]:
        return {
            "mode": "execute",
            "policy_version": decision.policy_version,
            "nominated_playbook": decision.nominated_playbook,
            "playbook_version": decision.playbook_version,
            "accepted": decision.accepted,
            "refusal_reasons": list(
                self.refusal_reasons or decision.refusal_reasons
            ),
            "verification_query": decision.verification_query,
            "would_execute": decision.would_execute,
            "action_executed": (
                self.executed or self.status == "already_verified"
            ),
            "verification_status": self.status,
            "attempt_id": self.attempt_id,
            "evidence_references": list(self.evidence_references),
        }


def _json_payload(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > 4096:
        raise ValueError("recovery audit payload exceeds byte budget")
    return encoded


def _verification_passed(
    playbook_name: str,
    data: dict[str, Any],
    *,
    action_data: dict[str, Any],
) -> bool:
    if playbook_name == "refresh_read_only_exchange_snapshot":
        return (
            data.get("comparison_kind")
            == "local_vs_coherent_read_only_snapshot"
            and data.get("applicable") is True
            and data.get("coherent") is True
            and data.get("complete") is True
            and int(data.get("mismatches", 0) or 0) == 0
            and int(data.get("unknown", 0) or 0) == 0
        )
    if playbook_name == "rerun_production_audit":
        return (
            data.get("available") is True
            and data.get("audit_run_completed") is True
            and data.get("complete") is True
            and data.get("monitor_error") in (None, "")
        )
    if playbook_name == "recover_stale_side_effect_free_claim":
        return (
            data.get("safe_queue_restored") is True
            and data.get("claim_status") in {"pending", "ready"}
            and data.get("business_write_owned") is False
        )
    if playbook_name == "reschedule_non_writing_ai_job":
        return (
            data.get("job_rescheduled") is True
            and data.get("business_write_owned") is False
        )
    if playbook_name == "fetch_missing_telegram_evidence":
        return (
            data.get("evidence_fetched") is True
            and data.get("evidence_available") is True
            and data.get("probe_complete") is True
            and data.get("endpoint_reachable") is True
            and data.get("bot_identity_available") is True
            and data.get("target_chat_available") is True
        )
    if playbook_name == "build_read_only_reconciliation_plan":
        return (
            action_data.get("plan_recorded") is True
            and action_data.get("plan_id") not in (None, "")
            and action_data.get("action_executed") is False
            and data.get("comparison_kind")
            == "local_vs_durable_last_observed"
            and data.get("applicable") is True
        )
    return False


def _set_incident_recovery_status(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    status: str,
    now: datetime,
) -> None:
    with session_factory() as session:
        session.execute(
            update(RuntimeIncident)
            .where(RuntimeIncident.id == int(incident_id))
            .values(recovery_status=status, updated_at=now)
        )
        session.commit()


def _failure_count(session_factory: sessionmaker) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count(RuntimeAgentRecoveryAttempt.id)).where(
                    RuntimeAgentRecoveryAttempt.status.in_(
                        _CIRCUIT_FAILURE_STATUSES
                    )
                )
            ).scalar_one()
        )


def _claim_is_current(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    expected_claim_token: str,
    now: datetime,
) -> bool:
    with session_factory() as session:
        incident = session.get(RuntimeIncident, int(incident_id))
        if incident is None or incident.claim_expires_at is None:
            return False
        expiry = incident.claim_expires_at
        comparison_now = (
            now.replace(tzinfo=None)
            if expiry.tzinfo is None and now.tzinfo is not None
            else now
        )
        return (
            incident.status == "claimed"
            and incident.claim_token == str(expected_claim_token)
            and expiry > comparison_now
        )


def _reserve_attempt(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    expected_fingerprint: str,
    expected_claim_token: str,
    decision: ExecutionPlaybookDecision,
    reservation_lease_seconds: float,
    now: datetime,
) -> tuple[str, RuntimeAgentRecoveryAttempt | None]:
    with session_factory() as session:
        incident = session.get(RuntimeIncident, int(incident_id))
        if incident is None:
            return "incident_missing", None
        if incident.fingerprint != str(expected_fingerprint):
            return "fingerprint_mismatch", None
        expiry = incident.claim_expires_at
        comparison_now = (
            now.replace(tzinfo=None)
            if expiry is not None and expiry.tzinfo is None
            else now
        )
        if (
            incident.status != "claimed"
            or incident.claim_token != str(expected_claim_token)
            or expiry is None
            or expiry <= comparison_now
        ):
            return "claim_lost", None
        if incident.recovery_status == "action_frozen":
            return "incident_action_frozen", None
        existing = session.execute(
            select(RuntimeAgentRecoveryAttempt).where(
                RuntimeAgentRecoveryAttempt.idempotency_key
                == decision.idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            session.expunge(existing)
            if existing.status == "verified":
                return "already_verified", existing
            started_at = existing.started_at
            comparison_started = (
                started_at.replace(tzinfo=now.tzinfo)
                if started_at.tzinfo is None and now.tzinfo is not None
                else started_at
            )
            if (
                existing.status == "reserved"
                and comparison_started
                + timedelta(
                    seconds=max(
                        5.0, min(float(reservation_lease_seconds), 3600.0)
                    )
                )
                > now
            ):
                return "action_in_progress", existing
            return "action_outcome_unknown", existing
        active = session.execute(
            select(RuntimeAgentRecoveryAttempt).where(
                RuntimeAgentRecoveryAttempt.status == "reserved"
            )
        ).scalar_one_or_none()
        if active is not None:
            active_started = active.started_at
            comparison_started = (
                active_started.replace(tzinfo=now.tzinfo)
                if active_started.tzinfo is None and now.tzinfo is not None
                else active_started
            )
            active_deadline = comparison_started + timedelta(
                seconds=max(
                    5.0, min(float(reservation_lease_seconds), 3600.0)
                )
            )
            if active_deadline > now:
                session.expunge(active)
                return "circuit_busy", active
            stale_updated = session.execute(
                update(RuntimeAgentRecoveryAttempt)
                .where(
                    RuntimeAgentRecoveryAttempt.id == active.id,
                    RuntimeAgentRecoveryAttempt.status == "reserved",
                )
                .values(
                    status="action_outcome_unknown",
                    error_code="reservation_lease_expired",
                    completed_at=now,
                    updated_at=now,
                )
            ).rowcount
            if stale_updated == 1:
                session.execute(
                    update(RuntimeIncident)
                    .where(RuntimeIncident.id == active.incident_id)
                    .values(recovery_status="action_frozen", updated_at=now)
                )
                session.commit()
            else:
                session.rollback()
                current_active = session.get(
                    RuntimeAgentRecoveryAttempt, active.id
                )
                if (
                    current_active is not None
                    and current_active.status == "reserved"
                ):
                    session.expunge(current_active)
                    return "circuit_busy", current_active
        statement = (
            sqlite_insert(RuntimeAgentRecoveryAttempt)
            .values(
                incident_id=int(incident_id),
                incident_fingerprint=str(expected_fingerprint),
                playbook_name=str(decision.nominated_playbook),
                playbook_version=int(decision.playbook_version or 0),
                idempotency_key=str(decision.idempotency_key),
                status="reserved",
                attempt_number=1,
                policy_version=decision.policy_version,
                execution_slot=1,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
            .returning(RuntimeAgentRecoveryAttempt.id)
        )
        attempt_id = session.execute(statement).scalar_one_or_none()
        session.commit()
        if attempt_id is None:
            concurrent = session.execute(
                select(RuntimeAgentRecoveryAttempt).where(
                    RuntimeAgentRecoveryAttempt.idempotency_key
                    == decision.idempotency_key
                )
            ).scalar_one_or_none()
            if concurrent is not None:
                session.expunge(concurrent)
                return "action_in_progress", concurrent
            return "circuit_busy", None
        attempt = session.get(RuntimeAgentRecoveryAttempt, int(attempt_id))
        session.expunge(attempt)
        return "reserved", attempt


def _finish_attempt(
    session_factory: sessionmaker,
    *,
    attempt_id: int,
    status: str,
    action_result: dict[str, Any] | None,
    verification_result: dict[str, Any] | None,
    error_code: str | None,
    now: datetime,
) -> bool:
    with session_factory() as session:
        result = session.execute(
            update(RuntimeAgentRecoveryAttempt)
            .where(
                RuntimeAgentRecoveryAttempt.id == int(attempt_id),
                RuntimeAgentRecoveryAttempt.status == "reserved",
            )
            .values(
                status=status,
                action_result_json=(
                    _json_payload(action_result)
                    if action_result is not None
                    else None
                ),
                verification_result_json=(
                    _json_payload(verification_result)
                    if verification_result is not None
                    else None
                ),
                error_code=error_code,
                completed_at=now,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _finish_attempt_with_claim(
    session_factory: sessionmaker,
    *,
    attempt_id: int,
    incident_id: int,
    expected_claim_token: str,
    status: str,
    recovery_status: str,
    action_result: dict[str, Any] | None,
    verification_result: dict[str, Any] | None,
    error_code: str | None,
    now: datetime,
) -> bool:
    """Atomically fence finalization against the current incident claim."""

    claim_current = (
        select(RuntimeIncident.id)
        .where(
            RuntimeIncident.id == int(incident_id),
            RuntimeIncident.status == "claimed",
            RuntimeIncident.claim_token == str(expected_claim_token),
            RuntimeIncident.claim_expires_at.is_not(None),
            RuntimeIncident.claim_expires_at > now,
        )
        .exists()
    )
    with session_factory() as session:
        attempt_updated = session.execute(
            update(RuntimeAgentRecoveryAttempt)
            .where(
                RuntimeAgentRecoveryAttempt.id == int(attempt_id),
                RuntimeAgentRecoveryAttempt.status == "reserved",
                claim_current,
            )
            .values(
                status=status,
                action_result_json=(
                    _json_payload(action_result)
                    if action_result is not None
                    else None
                ),
                verification_result_json=(
                    _json_payload(verification_result)
                    if verification_result is not None
                    else None
                ),
                error_code=error_code,
                completed_at=now,
                updated_at=now,
            )
        ).rowcount
        if attempt_updated != 1:
            session.rollback()
            return False
        incident_updated = session.execute(
            update(RuntimeIncident)
            .where(
                RuntimeIncident.id == int(incident_id),
                RuntimeIncident.status == "claimed",
                RuntimeIncident.claim_token == str(expected_claim_token),
                RuntimeIncident.claim_expires_at.is_not(None),
                RuntimeIncident.claim_expires_at > now,
            )
            .values(recovery_status=recovery_status, updated_at=now)
        ).rowcount
        if incident_updated != 1:
            session.rollback()
            return False
        session.commit()
        return True


def execute_low_risk_recovery(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    expected_fingerprint: str,
    expected_claim_token: str,
    decision: ExecutionPlaybookDecision,
    config: RuntimeAgentExecutorConfig,
    tools: RuntimeAgentToolRegistry,
    action_handlers: Mapping[str, Callable[..., bool]] | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RuntimeAgentExecutionResult:
    """Execute one exact allowlisted read-only action and verify it once."""

    operation_now = now or datetime.now(UTC)
    fresh_now = clock or (lambda: operation_now)
    if not config.enabled or not decision.accepted:
        reasons = decision.refusal_reasons or ("action_authority_disabled",)
        return RuntimeAgentExecutionResult(
            status="refused",
            incident_id=int(incident_id),
            refusal_reasons=tuple(reasons),
        )
    threshold = max(1, min(int(config.circuit_breaker_threshold), 5))
    if _failure_count(session_factory) >= threshold:
        _set_incident_recovery_status(
            session_factory,
            incident_id=incident_id,
            status="action_frozen",
            now=operation_now,
        )
        return RuntimeAgentExecutionResult(
            status="circuit_open",
            incident_id=int(incident_id),
            refusal_reasons=("circuit_open",),
        )
    handlers = dict(action_handlers or {})
    if set(handlers) - _PHASE_6_ACTIONS:
        return RuntimeAgentExecutionResult(
            status="refused",
            incident_id=int(incident_id),
            refusal_reasons=("unknown_action_handler",),
        )
    action_handler = handlers.get(str(decision.nominated_playbook))
    if (
        action_handler is None
        or decision.idempotency_key is None
        or decision.playbook_version is None
    ):
        return RuntimeAgentExecutionResult(
            status="refused",
            incident_id=int(incident_id),
            refusal_reasons=("executor_not_configured",),
        )

    reservation_status, attempt = _reserve_attempt(
        session_factory,
        incident_id=incident_id,
        expected_fingerprint=expected_fingerprint,
        expected_claim_token=expected_claim_token,
        decision=decision,
        reservation_lease_seconds=config.reservation_lease_seconds,
        now=operation_now,
    )
    if reservation_status != "reserved":
        if reservation_status == "action_outcome_unknown":
            if attempt is not None and attempt.status == "reserved":
                _finish_attempt(
                    session_factory,
                    attempt_id=attempt.id,
                    status="action_outcome_unknown",
                    action_result=None,
                    verification_result=None,
                    error_code="action_outcome_unknown",
                    now=operation_now,
                )
            _set_incident_recovery_status(
                session_factory,
                incident_id=incident_id,
                status="action_frozen",
                now=operation_now,
            )
        return RuntimeAgentExecutionResult(
            status=reservation_status,
            incident_id=int(incident_id),
            attempt_id=attempt.id if attempt is not None else None,
            verified=reservation_status == "already_verified",
            refusal_reasons=(
                (reservation_status,)
                if reservation_status != "already_verified"
                else ()
            ),
    )

    try:
        handled = action_handler(
            incident_id=int(incident_id),
            idempotency_key=str(decision.idempotency_key),
            expected_fingerprint=str(expected_fingerprint),
        )
        if handled is not True:
            raise RuntimeError("action handler refused action")
        post_action_now = fresh_now()
        if not _claim_is_current(
            session_factory,
            incident_id=incident_id,
            expected_claim_token=expected_claim_token,
            now=post_action_now,
        ):
            _finish_attempt(
                session_factory,
                attempt_id=attempt.id,
                status="action_outcome_unknown",
                action_result=None,
                verification_result=None,
                error_code="claim_lost_after_action",
                now=post_action_now,
            )
            _set_incident_recovery_status(
                session_factory,
                incident_id=incident_id,
                status="action_frozen",
                now=post_action_now,
            )
            return RuntimeAgentExecutionResult(
                status="action_outcome_unknown",
                incident_id=int(incident_id),
                attempt_id=attempt.id,
                executed=True,
                refusal_reasons=("claim_lost_after_action",),
            )
        action_payload = {
            "data": {
                "handler_completed": True,
                "plan_recorded": (
                    decision.nominated_playbook
                    == "build_read_only_reconciliation_plan"
                ),
                "plan_id": (
                    f"runtime-recovery-attempt:{attempt.id}"
                    if decision.nominated_playbook
                    == "build_read_only_reconciliation_plan"
                    else None
                ),
                "action_executed": False,
            },
            "evidence_refs": [f"incident:{int(incident_id)}"],
        }
        action_references = (f"incident:{int(incident_id)}",)
        verification = tools.execute(
            str(decision.verification_query),
            {"incident_id": int(incident_id)},
            expected_incident_id=int(incident_id),
        )
        post_verification_now = fresh_now()
        if not _claim_is_current(
            session_factory,
            incident_id=incident_id,
            expected_claim_token=expected_claim_token,
            now=post_verification_now,
        ):
            _finish_attempt(
                session_factory,
                attempt_id=attempt.id,
                status="action_outcome_unknown",
                action_result=action_payload,
                verification_result=verification.as_model_payload(),
                error_code="claim_lost_before_commit",
                now=post_verification_now,
            )
            _set_incident_recovery_status(
                session_factory,
                incident_id=incident_id,
                status="action_frozen",
                now=post_verification_now,
            )
            return RuntimeAgentExecutionResult(
                status="action_outcome_unknown",
                incident_id=int(incident_id),
                attempt_id=attempt.id,
                executed=True,
                refusal_reasons=("claim_lost_before_commit",),
                evidence_references=tuple(verification.evidence_refs),
            )
        verified = _verification_passed(
            decision.nominated_playbook,
            verification.data,
            action_data=action_payload["data"],
        )
        status = "verified" if verified else "verification_failed"
        finished = _finish_attempt_with_claim(
            session_factory,
            attempt_id=attempt.id,
            incident_id=incident_id,
            expected_claim_token=expected_claim_token,
            status=status,
            recovery_status=(
                "action_verified" if verified else "action_frozen"
            ),
            action_result=action_payload,
            verification_result=verification.as_model_payload(),
            error_code=None if verified else "verification_mismatch",
            now=post_verification_now,
        )
        if not finished:
            _finish_attempt(
                session_factory,
                attempt_id=attempt.id,
                status="action_outcome_unknown",
                action_result=action_payload,
                verification_result=verification.as_model_payload(),
                error_code="claim_lost_during_finalization",
                now=post_verification_now,
            )
            _set_incident_recovery_status(
                session_factory,
                incident_id=incident_id,
                status="action_frozen",
                now=post_verification_now,
            )
            return RuntimeAgentExecutionResult(
                status="action_outcome_unknown",
                incident_id=int(incident_id),
                attempt_id=attempt.id,
                executed=True,
                refusal_reasons=("action_outcome_unknown",),
            )
        return RuntimeAgentExecutionResult(
            status=status,
            incident_id=int(incident_id),
            attempt_id=attempt.id,
            executed=True,
            verified=verified,
            refusal_reasons=(
                () if verified else ("verification_mismatch",)
            ),
            evidence_references=tuple(
                dict.fromkeys(
                    (*action_references, *verification.evidence_refs)
                )
            ),
        )
    except Exception as exc:
        failure_now = fresh_now()
        error_code = type(exc).__name__[:128]
        finished = _finish_attempt_with_claim(
            session_factory,
            attempt_id=attempt.id,
            incident_id=incident_id,
            expected_claim_token=expected_claim_token,
            status="failed",
            recovery_status="action_frozen",
            action_result=None,
            verification_result=None,
            error_code=error_code,
            now=failure_now,
        )
        if not finished:
            _finish_attempt(
                session_factory,
                attempt_id=attempt.id,
                status="action_outcome_unknown",
                action_result=None,
                verification_result=None,
                error_code="claim_lost_during_failure",
                now=failure_now,
            )
            _set_incident_recovery_status(
                session_factory,
                incident_id=incident_id,
                status="action_frozen",
                now=failure_now,
            )
            return RuntimeAgentExecutionResult(
                status="action_outcome_unknown",
                incident_id=int(incident_id),
                attempt_id=attempt.id,
                executed=True,
                refusal_reasons=("claim_lost_during_failure",),
            )
        return RuntimeAgentExecutionResult(
            status="failed",
            incident_id=int(incident_id),
            attempt_id=attempt.id,
            executed=True,
            refusal_reasons=(error_code,),
        )
