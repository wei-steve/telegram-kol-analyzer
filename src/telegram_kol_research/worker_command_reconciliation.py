"""Read-only evidence evaluation for uncertain durable worker commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from telegram_kol_research.models import WorkerCommandJob, utc_now
from telegram_kol_research.worker_command_jobs import (
    WorkerCommandSnapshot,
    get_worker_command,
)


WORKER_COMMAND_RECONCILIATION_OUTCOMES = frozenset(
    {
        "confirmed_succeeded",
        "confirmed_no_submission",
        "conflict",
        "evidence_incomplete",
    }
)


class WorkerCommandReconciliationError(RuntimeError):
    pass


class WorkerCommandReconciliationDrift(WorkerCommandReconciliationError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerCommandEvidence:
    snapshot_complete: bool
    identity_chain_complete: bool
    operation_matches: bool
    submission_found: bool
    direct_exchange_proof: bool
    client_order_id_only: bool = False
    external_attempts: int = 1
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerCommandReconciliationReport:
    command_id: str
    outcome: str
    reason: str
    applied: bool = False


def collect_worker_command_evidence(
    command: WorkerCommandSnapshot,
    *,
    external_reader: Callable[[WorkerCommandSnapshot], dict[str, Any]],
) -> WorkerCommandEvidence:
    """Read external evidence once, with one bounded retry if incomplete."""

    payload: dict[str, Any] = {}
    attempts = 0
    for attempts in (1, 2):
        try:
            candidate = external_reader(command)
        except Exception as exc:
            candidate = {"complete": False, "reason": type(exc).__name__}
        payload = candidate if isinstance(candidate, dict) else {
            "complete": False,
            "reason": "invalid external evidence schema",
        }
        if payload.get("complete") is True:
            break
    return WorkerCommandEvidence(
        snapshot_complete=payload.get("complete") is True,
        identity_chain_complete=payload.get("identity_chain_complete") is True,
        operation_matches=payload.get("operation_matches") is not False,
        submission_found=payload.get("submission_found") is True,
        direct_exchange_proof=payload.get("direct_exchange_proof") is True,
        client_order_id_only=payload.get("client_order_id_only") is True,
        external_attempts=attempts,
        reason=str(payload.get("reason") or "") or None,
    )


def evaluate_worker_command_evidence(
    command: WorkerCommandSnapshot,
    evidence: WorkerCommandEvidence,
) -> WorkerCommandReconciliationReport:
    if command.status != "uncertain":
        raise WorkerCommandReconciliationDrift(
            f"command is no longer uncertain: {command.status}"
        )
    if not evidence.snapshot_complete:
        return _report(command, "evidence_incomplete", evidence.reason or "external snapshot incomplete")
    if not evidence.operation_matches:
        return _report(command, "conflict", "evidence conflicts with the requested operation")
    if evidence.submission_found:
        if evidence.client_order_id_only or not evidence.identity_chain_complete:
            return _report(
                command,
                "evidence_incomplete",
                "parent-child-posId identity chain is incomplete",
            )
        if not evidence.direct_exchange_proof:
            return _report(
                command,
                "evidence_incomplete",
                "direct exchange proof is incomplete",
            )
        return _report(command, "confirmed_succeeded", "exact local and exchange identity chain")
    if not evidence.direct_exchange_proof:
        # For absence, completeness of the external snapshot itself is the
        # direct negative proof; no heuristic identity is accepted.
        return _report(command, "confirmed_no_submission", "complete evidence proves no submission")
    return _report(command, "conflict", "exchange proof exists without a matching submission")


def reconcile_uncertain_worker_command(
    session_factory,
    *,
    command_id: str,
    evidence: WorkerCommandEvidence,
    apply_confirmed: bool = False,
    reconciled_at: datetime | None = None,
) -> WorkerCommandReconciliationReport:
    command = get_worker_command(session_factory, command_id=command_id)
    if command is None:
        raise WorkerCommandReconciliationError("worker command not found")
    report = evaluate_worker_command_evidence(command, evidence)
    if not apply_confirmed or report.outcome not in {
        "confirmed_succeeded",
        "confirmed_no_submission",
    }:
        return report

    timestamp = _naive_utc(reconciled_at or utc_now())
    if report.outcome == "confirmed_succeeded":
        status = "succeeded"
        http_status = 200
        result = {
            "command_id": command.command_id,
            "reconciled": "confirmed_succeeded",
        }
        error_code = None
        error_summary = None
    else:
        status = "failed"
        http_status = 409
        detail = "confirmed no submission; a new explicit action is required"
        result = {"detail": detail, "command_id": command.command_id}
        error_code = "confirmed_no_submission"
        error_summary = detail
    with session_factory() as session:
        updated = (
            session.query(WorkerCommandJob)
            .filter(
                WorkerCommandJob.command_id == command.command_id,
                WorkerCommandJob.status == "uncertain",
            )
            .update(
                {
                    WorkerCommandJob.status: status,
                    WorkerCommandJob.http_status: http_status,
                    WorkerCommandJob.result_json: json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    WorkerCommandJob.error_code: error_code,
                    WorkerCommandJob.error_summary: error_summary,
                    WorkerCommandJob.reconciled_at: timestamp,
                    WorkerCommandJob.completed_at: timestamp,
                },
                synchronize_session=False,
            )
        )
        session.commit()
    if updated != 1:
        raise WorkerCommandReconciliationDrift(
            "worker command state changed before guarded apply"
        )
    return WorkerCommandReconciliationReport(
        command_id=report.command_id,
        outcome=report.outcome,
        reason=report.reason,
        applied=True,
    )


def reconcile_worker_command_by_id(
    session_factory,
    *,
    command_id: str,
    deepcoin_client_factory,
    apply_confirmed: bool = False,
) -> WorkerCommandReconciliationReport:
    command = get_worker_command(session_factory, command_id=command_id)
    if command is None:
        raise WorkerCommandReconciliationError("worker command not found")

    def conservative_reader(_command):
        # Build the read-only client to verify availability, but refuse to
        # infer an exact outcome without a command-specific parent/child/posId
        # evidence projection. This path performs no exchange write.
        client = deepcoin_client_factory()
        readers = [
            getattr(client, name, None)
            for name in (
                "list_positions",
                "list_open_orders",
                "list_order_history",
                "list_position_history",
            )
        ]
        if not all(callable(reader) for reader in readers):
            return {"complete": False, "reason": "required read-only exchange reader unavailable"}
        for reader in readers:
            reader()
        return {
            "complete": False,
            "reason": "command-specific exact identity projection required",
        }

    evidence = collect_worker_command_evidence(
        command,
        external_reader=conservative_reader,
    )
    return reconcile_uncertain_worker_command(
        session_factory,
        command_id=command_id,
        evidence=evidence,
        apply_confirmed=apply_confirmed,
    )


def _report(command, outcome: str, reason: str) -> WorkerCommandReconciliationReport:
    return WorkerCommandReconciliationReport(
        command_id=command.command_id,
        outcome=outcome,
        reason=reason,
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
