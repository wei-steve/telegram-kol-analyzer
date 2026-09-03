"""Independent exact-token side-effect fence for entry-assembly wakeups."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from datetime import timedelta
import logging
from typing import Any

from sqlalchemy import update

from telegram_kol_research.authoritative_execution_schema import (
    require_recognition_execution_schema,
)
from telegram_kol_research.execution_boundary import ExecutionBoundaryOutcome
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    EntryAssemblyWakeupExecution,
)
from telegram_kol_research.recognition_execution_runtime import (
    periodic_lease_heartbeat,
)

logger = logging.getLogger(__name__)


def heartbeat_wakeup_execution(
    session_factory,
    *,
    child_execution_id: int,
    claim_token: str,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        result = session.execute(
            update(EntryAssemblyWakeupExecution)
            .where(
                EntryAssemblyWakeupExecution.id == int(child_execution_id),
                EntryAssemblyWakeupExecution.claim_token == str(claim_token),
                EntryAssemblyWakeupExecution.status.in_(
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


def mark_wakeup_side_effect_started(
    session_factory,
    *,
    child_execution_id: int,
    entry_assembly_attempt_id: int,
    wake_generation: int,
    strategy_raw_message_id: int,
    trigger_raw_message_id: int,
    claim_token: str,
    started_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        result = session.execute(
            update(EntryAssemblyWakeupExecution)
            .where(
                EntryAssemblyWakeupExecution.id == int(child_execution_id),
                EntryAssemblyWakeupExecution.entry_assembly_attempt_id
                == int(entry_assembly_attempt_id),
                EntryAssemblyWakeupExecution.wake_generation
                == int(wake_generation),
                EntryAssemblyWakeupExecution.strategy_raw_message_id
                == int(strategy_raw_message_id),
                EntryAssemblyWakeupExecution.trigger_raw_message_id
                == int(trigger_raw_message_id),
                EntryAssemblyWakeupExecution.claim_token == str(claim_token),
                EntryAssemblyWakeupExecution.status == "claimed",
                EntryAssemblyWakeupExecution.side_effect_started_at.is_(None),
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


def record_wakeup_outcome(
    session_factory,
    *,
    child_execution_id: int,
    claim_token: str,
    outcome: ExecutionBoundaryOutcome,
    recorded_at: datetime,
) -> bool:
    require_recognition_execution_schema(session_factory)
    if outcome.exchange_effect == "outcome_unknown":
        return False
    allowed_statuses = (
        ("claimed", "executing")
        if outcome.exchange_effect == "not_started"
        else ("executing",)
    )
    with session_factory() as session:
        statement = update(EntryAssemblyWakeupExecution).where(
            EntryAssemblyWakeupExecution.id == int(child_execution_id),
            EntryAssemblyWakeupExecution.claim_token == str(claim_token),
            EntryAssemblyWakeupExecution.status.in_(allowed_statuses),
        )
        if outcome.exchange_effect != "not_started":
            statement = statement.where(
                EntryAssemblyWakeupExecution.side_effect_started_at.is_not(None)
            )
        result = session.execute(
            statement.values(
                status="outcome_recorded",
                exchange_effect=outcome.exchange_effect,
                result_status=outcome.raw_status[:32],
                result_reason=(outcome.reason_code or "")[:256] or None,
                result_json=_safe_outcome(outcome),
                evidence_refs_json=_safe_evidence(outcome.evidence_refs),
                outcome_recorded_at=recorded_at,
                heartbeat_at=recorded_at,
                updated_at=recorded_at,
            )
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def finalize_recorded_wakeup(
    session_factory,
    *,
    child_execution_id: int,
    claim_token: str,
    finalized_at: datetime,
) -> bool:
    """Finalize only local state; this function has no adapter argument."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        child = session.get(EntryAssemblyWakeupExecution, int(child_execution_id))
        if (
            child is None
            or child.claim_token != str(claim_token)
            or child.status != "outcome_recorded"
        ):
            return False
        parent_result = session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.id == int(child.entry_assembly_attempt_id),
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claim_token == str(claim_token),
            )
            .values(
                status="woken",
                woken_at=finalized_at,
                wake_claim_token=None,
                wake_claimed_at=None,
                updated_at=finalized_at,
            )
        )
        if int(parent_result.rowcount or 0) != 1:
            session.rollback()
            return False
        child.status = "succeeded"
        child.completed_at = finalized_at
        child.updated_at = finalized_at
        session.commit()
        return True


def fail_safe_wakeup(
    session_factory,
    *,
    child_execution_id: int,
    claim_token: str,
    failed_at: datetime,
    error: BaseException,
) -> bool:
    """Only a proven pre-boundary child may make its parent pending again."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        child = session.get(EntryAssemblyWakeupExecution, int(child_execution_id))
        if (
            child is None
            or child.claim_token != str(claim_token)
            or child.status != "claimed"
            or child.side_effect_started_at is not None
        ):
            return False
        parent_result = session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.id == int(child.entry_assembly_attempt_id),
                EntryAssemblyAttempt.status == "claimed",
                EntryAssemblyAttempt.wake_claim_token == str(claim_token),
            )
            .values(
                status="pending",
                blocking_raw_message_ids_json=f"[{int(child.trigger_raw_message_id)}]",
                wake_claim_token=None,
                wake_claimed_at=None,
                updated_at=failed_at,
            )
        )
        if int(parent_result.rowcount or 0) != 1:
            session.rollback()
            return False
        child.status = "failed_safe"
        child.error_class = type(error).__name__[:128]
        child.error_summary = str(error)[:512]
        child.completed_at = failed_at
        child.updated_at = failed_at
        session.commit()
        return True


def mark_wakeup_uncertain(
    session_factory,
    *,
    child_execution_id: int,
    claim_token: str,
    uncertain_at: datetime,
    error: BaseException,
) -> bool:
    """Freeze post-boundary wakeups; the parent deliberately remains claimed."""

    require_recognition_execution_schema(session_factory)
    with session_factory() as session:
        child = session.get(EntryAssemblyWakeupExecution, int(child_execution_id))
        if (
            child is None
            or child.claim_token != str(claim_token)
            or child.status != "executing"
            or child.side_effect_started_at is None
        ):
            return False
        child.status = "uncertain"
        child.exchange_effect = "outcome_unknown"
        child.error_class = type(error).__name__[:128]
        child.error_summary = str(error)[:512]
        child.uncertain_at = uncertain_at
        child.completed_at = uncertain_at
        child.updated_at = uncertain_at
        session.commit()
        return True


def run_claimed_entry_assembly_wakeup(
    session_factory,
    *,
    wake_claim,
    auto_trade_executor,
    execution_registry=None,
) -> None:
    if (
        wake_claim.child_execution_id is None
        or wake_claim.wake_generation is None
    ):
        raise RuntimeError("entry_assembly_child_fence_missing")
    token = str(wake_claim.claim_token)
    scope = (
        execution_registry.admitted(token)
        if execution_registry is not None
        else nullcontext()
    )
    try:
        scope.__enter__()
    except BaseException as exc:
        classified = False
        try:
            classified = fail_safe_wakeup(
                session_factory,
                child_execution_id=int(wake_claim.child_execution_id),
                claim_token=token,
                failed_at=datetime.now(UTC),
                error=exc,
            )
        except BaseException:
            logger.critical(
                "entry assembly wakeup drain-race persistence raised child_execution_id=%s",
                wake_claim.child_execution_id,
                exc_info=True,
            )
        if not classified:
            logger.critical(
                "entry assembly wakeup drain-race classification failed child_execution_id=%s",
                wake_claim.child_execution_id,
            )
            try:
                _capture_terminalization_failure(
                    session_factory,
                    wake_claim=wake_claim,
                    phase="claimed",
                    action="drain_race_terminalize_failed",
                )
            except BaseException:
                logger.critical(
                    "entry assembly wakeup drain-race incident capture raised child_execution_id=%s",
                    wake_claim.child_execution_id,
                    exc_info=True,
                )
        raise
    try:
        def renew_child_lease():
            heartbeat_at = datetime.now(UTC)
            heartbeat_wakeup_execution(
                session_factory,
                child_execution_id=int(wake_claim.child_execution_id),
                claim_token=token,
                heartbeat_at=heartbeat_at,
                lease_expires_at=heartbeat_at + timedelta(minutes=2),
            )

        try:
            heartbeat_scope = periodic_lease_heartbeat(renew_child_lease)
            heartbeat_scope.__enter__()
            now = datetime.now(UTC)
            if not mark_wakeup_side_effect_started(
                session_factory,
                child_execution_id=int(wake_claim.child_execution_id),
                entry_assembly_attempt_id=int(wake_claim.attempt_id),
                wake_generation=int(wake_claim.wake_generation),
                strategy_raw_message_id=int(wake_claim.strategy_raw_message_id),
                trigger_raw_message_id=int(wake_claim.trigger_raw_message_id),
                claim_token=token,
                started_at=now,
            ):
                raise RuntimeError("entry_assembly_wakeup_boundary_cas_failed")
            observed = auto_trade_executor(int(wake_claim.strategy_raw_message_id))
            if not isinstance(observed, ExecutionBoundaryOutcome):
                raise RuntimeError("entry_assembly_wakeup_boundary_outcome_missing")
            if observed.exchange_effect == "outcome_unknown":
                raise RuntimeError("entry_assembly_wakeup_outcome_unknown")
            if not record_wakeup_outcome(
                session_factory,
                child_execution_id=int(wake_claim.child_execution_id),
                claim_token=token,
                outcome=observed,
                recorded_at=datetime.now(UTC),
            ):
                raise RuntimeError("entry_assembly_wakeup_outcome_cas_failed")
            if not finalize_recorded_wakeup(
                session_factory,
                child_execution_id=int(wake_claim.child_execution_id),
                claim_token=token,
                finalized_at=datetime.now(UTC),
            ):
                raise RuntimeError("entry_assembly_wakeup_finalize_cas_failed")
        except BaseException as exc:
            classified = False
            try:
                with session_factory() as session:
                    child = session.get(
                        EntryAssemblyWakeupExecution,
                        int(wake_claim.child_execution_id),
                    )
                    status = child.status if child is not None else None
                classified = status in {
                    "failed_safe",
                    "uncertain",
                    "succeeded",
                }
                if status == "claimed":
                    classified = fail_safe_wakeup(
                        session_factory,
                        child_execution_id=int(wake_claim.child_execution_id),
                        claim_token=token,
                        failed_at=datetime.now(UTC),
                        error=exc,
                    )
                elif status == "executing":
                    classified = mark_wakeup_uncertain(
                        session_factory,
                        child_execution_id=int(wake_claim.child_execution_id),
                        claim_token=token,
                        uncertain_at=datetime.now(UTC),
                        error=exc,
                    )
                elif status == "outcome_recorded":
                    _capture_terminalization_failure(
                        session_factory,
                        wake_claim=wake_claim,
                        phase="outcome_recorded",
                        action="finalize_raised",
                    )
                    classified = True
            except Exception:
                logger.critical(
                    "entry assembly wakeup terminalization persistence raised child_execution_id=%s",
                    wake_claim.child_execution_id,
                    exc_info=True,
                )
            if not classified:
                logger.critical(
                    "entry assembly wakeup remained unclassified child_execution_id=%s",
                    wake_claim.child_execution_id,
                )
                try:
                    _capture_terminalization_failure(
                        session_factory,
                        wake_claim=wake_claim,
                        phase="unclassified",
                        action="exception_terminalize_failed",
                    )
                except BaseException:
                    logger.critical(
                        "entry assembly wakeup incident capture raised child_execution_id=%s",
                        wake_claim.child_execution_id,
                        exc_info=True,
                    )
            raise
        finally:
            if "heartbeat_scope" in locals():
                heartbeat_scope.__exit__(None, None, None)
    finally:
        scope.__exit__(None, None, None)


def _safe_evidence(refs: tuple[dict[str, Any], ...]) -> str:
    import json

    safe = []
    for ref in refs:
        safe.append(
            {
                str(key)[:64]: (
                    int(value)
                    if isinstance(value, int) and not isinstance(value, bool)
                    else str(value)[:128]
                )
                for key, value in ref.items()
                if str(key).lower() not in {"payload", "headers", "request", "response"}
            }
        )
    return json.dumps(safe, sort_keys=True, separators=(",", ":"))[:4000]


def _capture_terminalization_failure(
    session_factory,
    *,
    wake_claim,
    phase: str,
    action: str,
) -> None:
    from telegram_kol_research.runtime_incident_adapters import (
        capture_recognition_execution_state,
        capture_runtime_incident_best_effort,
    )

    capture_runtime_incident_best_effort(
        capture_recognition_execution_state,
        session_factory,
        family="active_wakeup_execution",
        row_id=int(wake_claim.child_execution_id),
        raw_message_id=int(wake_claim.strategy_raw_message_id),
        phase=phase,
        action=action,
        occurred_at=datetime.now(UTC),
    )


def _safe_outcome(outcome: ExecutionBoundaryOutcome) -> str:
    """Persist only the typed envelope, never an adapter response payload."""

    import json

    return json.dumps(
        {
            "status": str(outcome.status)[:32],
            "exchange_effect": str(outcome.exchange_effect)[:32],
            "raw_status": str(outcome.raw_status)[:32],
            "reason_code": (
                str(outcome.reason_code)[:256]
                if outcome.reason_code is not None
                else None
            ),
            "evidence_refs": json.loads(_safe_evidence(outcome.evidence_refs)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )[:8000]
