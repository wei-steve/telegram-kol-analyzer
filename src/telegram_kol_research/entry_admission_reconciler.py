"""Bounded structured rechecks for deferred adjacent-entry admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.entry_assembly_admission import (
    _is_adjacent_entry_context_defer,
    _release_adjacent_entry_visibility_delay,
    assess_entry_assembly_admission,
)
from telegram_kol_research.instruction_execution_contracts import (
    InstructionExecutionConflictError,
    InstructionExecutionTransitionError,
    transition_instruction_execution_contract,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    InstructionExecutionContract,
    MessageInstructionItem,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


ENTRY_ADMISSION_RECHECK_DELAY = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class EntryAdmissionReconcileResult:
    released: int = 0
    expired: int = 0
    incidents: int = 0
    skipped: int = 0


def reconcile_due_entry_admissions(
    session_factory: sessionmaker,
    *,
    now: datetime,
    limit: int = 20,
) -> EntryAdmissionReconcileResult:
    """Release or expire due attempts without invoking any exchange writer."""

    bounded_limit = max(0, min(int(limit), 100))
    if bounded_limit == 0:
        return EntryAdmissionReconcileResult()
    with session_factory() as session:
        attempt_ids = [
            int(row_id)
            for (row_id,) in (
                session.query(EntryAssemblyAttempt.id)
                .filter(
                    EntryAssemblyAttempt.status == "pending",
                    EntryAssemblyAttempt.updated_at
                    <= now - ENTRY_ADMISSION_RECHECK_DELAY,
                )
                .order_by(EntryAssemblyAttempt.updated_at, EntryAssemblyAttempt.id)
                .limit(bounded_limit)
                .all()
            )
        ]

    counts = {"released": 0, "expired": 0, "incidents": 0, "skipped": 0}
    for attempt_id in attempt_ids:
        snapshot = _load_attempt_snapshot(session_factory, attempt_id=attempt_id)
        if snapshot is None:
            continue
        attempt, item, contract = snapshot
        if contract is not None and contract.state == "submit_unknown":
            counts["skipped"] += 1
            continue
        if item is None:
            counts["skipped"] += 1
            continue
        if item.status == "succeeded" and _is_adjacent_entry_context_defer(
            item.result_json
        ):
            if _expire_attempt_only(session_factory, attempt_id=attempt_id, now=now):
                _record_legacy_stale_incident(
                    session_factory,
                    attempt_id=attempt_id,
                    now=now,
                )
                counts["incidents"] += 1
            continue
        if item.status != "pending" or not _is_adjacent_entry_context_defer(
            item.result_json
        ):
            counts["skipped"] += 1
            continue
        deadline = _as_utc(item.execution_deadline_at)
        if deadline is not None and now >= deadline:
            contract_snapshot = (
                (contract.id, contract.state, contract.state_version)
                if contract is not None
                else None
            )
            if _expire_attempt_and_item(
                session_factory,
                attempt_id=attempt_id,
                item_id=int(item.id),
                now=now,
            ):
                counts["expired"] += 1
                _expire_contract_best_effort(
                    session_factory,
                    contract_snapshot=contract_snapshot,
                    now=now,
                )
            continue

        decision = assess_entry_assembly_admission(
            session_factory,
            strategy_raw_message_id=int(attempt.strategy_raw_message_id),
            signal_candidate_id=int(attempt.signal_candidate_id),
            mode="live",
            assessed_at=now,
        )
        if decision.status == "deferred":
            continue
        if decision.status == "blocked":
            contract_snapshot = (
                (contract.id, contract.state, contract.state_version)
                if contract is not None
                else None
            )
            if _expire_attempt_and_item(
                session_factory,
                attempt_id=attempt_id,
                item_id=int(item.id),
                now=now,
                reason="entry_admission_recheck_blocked",
            ):
                counts["expired"] += 1
                _expire_contract_best_effort(
                    session_factory,
                    contract_snapshot=contract_snapshot,
                    now=now,
                )
            continue
        with session_factory() as session:
            current_attempt = session.get(EntryAssemblyAttempt, attempt_id)
            if current_attempt is None or current_attempt.status != "pending":
                continue
            released = _release_adjacent_entry_visibility_delay(
                session,
                attempt=current_attempt,
                now=now,
            )
            if not released:
                session.rollback()
                continue
            current_attempt.status = "woken"
            current_attempt.woken_at = now
            current_attempt.updated_at = now
            session.commit()
            counts["released"] += 1

    return EntryAdmissionReconcileResult(**counts)


def _load_attempt_snapshot(session_factory, *, attempt_id: int):
    with session_factory() as session:
        attempt = session.get(EntryAssemblyAttempt, int(attempt_id))
        if attempt is None or attempt.status != "pending":
            return None
        item = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.raw_message_id
                == int(attempt.strategy_raw_message_id),
                MessageInstructionItem.signal_candidate_id
                == int(attempt.signal_candidate_id),
                MessageInstructionItem.instruction_kind == "entry",
                MessageInstructionItem.retired_at.is_(None),
            )
            .one_or_none()
        )
        contract = (
            session.query(InstructionExecutionContract)
            .filter(
                InstructionExecutionContract.message_instruction_item_id
                == int(item.id)
            )
            .one_or_none()
            if item is not None
            else None
        )
        session.expunge(attempt)
        if item is not None:
            session.expunge(item)
        if contract is not None:
            session.expunge(contract)
        return attempt, item, contract


def _expire_attempt_and_item(
    session_factory,
    *,
    attempt_id: int,
    item_id: int,
    now: datetime,
    reason: str = "entry_admission_deadline_expired",
) -> bool:
    error_json = json.dumps(
        {"status": "expired", "reason": reason},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with session_factory() as session:
        attempt = session.get(EntryAssemblyAttempt, int(attempt_id))
        item = session.get(MessageInstructionItem, int(item_id))
        if (
            attempt is None
            or attempt.status != "pending"
            or item is None
            or item.status != "pending"
        ):
            return False
        attempt.status = "expired"
        attempt.updated_at = now
        item.status = "failed"
        item.result_json = None
        item.error_json = error_json
        item.visibility_next_attempt_at = None
        item.updated_at = now
        session.commit()
        return True


def _expire_attempt_only(session_factory, *, attempt_id: int, now: datetime) -> bool:
    with session_factory() as session:
        attempt = session.get(EntryAssemblyAttempt, int(attempt_id))
        if attempt is None or attempt.status != "pending":
            return False
        attempt.status = "expired"
        attempt.updated_at = now
        session.commit()
        return True


def _expire_contract_best_effort(
    session_factory,
    *,
    contract_snapshot,
    now: datetime,
) -> None:
    if contract_snapshot is None:
        return
    contract_id, state, version = contract_snapshot
    if state not in {"pending", "deferred"}:
        return
    try:
        transition_instruction_execution_contract(
            session_factory,
            contract_id=int(contract_id),
            expected_state=str(state),
            expected_version=int(version),
            new_state="expired",
            reason_code="entry_admission_deadline_expired",
            evidence_refs=[{"kind": "entry_assembly_attempt"}],
            transitioned_at=now,
        )
    except (
        InstructionExecutionConflictError,
        InstructionExecutionTransitionError,
    ):
        return


def _record_legacy_stale_incident(
    session_factory,
    *,
    attempt_id: int,
    now: datetime,
) -> None:
    summary = json.dumps(
        {
            "component": "entry_admission_reconciler",
            "impact": "legacy_deferred_item_has_no_exchange_proof",
            "reason_code": "succeeded_deferred_contradiction",
            "source_status": "legacy_unproven",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(
        f"entry-admission-legacy:{attempt_id}".encode()
    ).hexdigest()
    record_runtime_incident(
        session_factory,
        source_kind="entry_assembly_attempt",
        source_record_id=str(int(attempt_id)),
        incident_type="unclassified_operation_failure",
        severity="high",
        fingerprint=fingerprint,
        redacted_summary=summary,
        occurred_at=now,
        feature_policy_version="execution-truth-v1",
        prompt_version="none",
        tool_policy_version="read-only-v1",
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
