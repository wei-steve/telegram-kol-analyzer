"""Bounded structured rechecks for deferred adjacent-entry admission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.entry_assembly_admission import (
    _is_adjacent_entry_context_defer,
    _release_adjacent_entry_visibility_delay,
    assess_entry_assembly_admission,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    InstructionExecutionContract,
    InstructionExecutionTransition,
    MessageInstructionItem,
)


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
    execution_contract_mode: str = "disabled",
    entry_after_item_id: int = 0,
) -> EntryAdmissionReconcileResult:
    """Release or expire due attempts without invoking any exchange writer."""

    if execution_contract_mode != "live":
        return EntryAdmissionReconcileResult()
    bounded_limit = max(0, min(int(limit), 100))
    if bounded_limit == 0:
        return EntryAdmissionReconcileResult()
    with session_factory() as session:
        attempt_ids = [
            int(row_id)
            for (row_id,) in (
                session.query(EntryAssemblyAttempt.id)
                .join(
                    MessageInstructionItem,
                    and_(
                        MessageInstructionItem.raw_message_id
                        == EntryAssemblyAttempt.strategy_raw_message_id,
                        MessageInstructionItem.signal_candidate_id
                        == EntryAssemblyAttempt.signal_candidate_id,
                    ),
                )
                .join(
                    InstructionExecutionContract,
                    InstructionExecutionContract.message_instruction_item_id
                    == MessageInstructionItem.id,
                )
                .filter(
                    EntryAssemblyAttempt.status == "pending",
                    EntryAssemblyAttempt.updated_at
                    <= now - ENTRY_ADMISSION_RECHECK_DELAY,
                    MessageInstructionItem.id > int(entry_after_item_id),
                    MessageInstructionItem.instruction_kind == "entry",
                    MessageInstructionItem.status == "pending",
                    MessageInstructionItem.retired_at.is_(None),
                    InstructionExecutionContract.state == "deferred",
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
        if item is None:
            counts["skipped"] += 1
            continue
        if (
            int(item.id) <= int(entry_after_item_id)
            or contract is None
            or contract.state != "deferred"
        ):
            counts["skipped"] += 1
            continue
        if item.status != "pending":
            counts["skipped"] += 1
            continue
        if not _is_adjacent_entry_context_defer(item.result_json):
            if _expire_deferred_entry_truth(
                session_factory,
                attempt_id=attempt_id,
                item_id=int(item.id),
                contract_id=int(contract.id),
                contract_version=int(contract.state_version),
                now=now,
                reason="entry_admission_recheck_state_mismatch",
            ):
                counts["expired"] += 1
            continue
        deadline = _as_utc(item.execution_deadline_at)
        if deadline is not None and now >= deadline:
            if _expire_deferred_entry_truth(
                session_factory,
                attempt_id=attempt_id,
                item_id=int(item.id),
                contract_id=int(contract.id),
                contract_version=int(contract.state_version),
                now=now,
            ):
                counts["expired"] += 1
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
            if _expire_deferred_entry_truth(
                session_factory,
                attempt_id=attempt_id,
                item_id=int(item.id),
                contract_id=int(contract.id),
                contract_version=int(contract.state_version),
                now=now,
                reason="entry_admission_recheck_blocked",
            ):
                counts["expired"] += 1
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


def _expire_deferred_entry_truth(
    session_factory,
    *,
    attempt_id: int,
    item_id: int,
    contract_id: int,
    contract_version: int,
    now: datetime,
    reason: str = "entry_admission_deadline_expired",
) -> bool:
    evidence_json = '[{"kind":"entry_assembly_attempt"}]'
    error_json = json.dumps(
        {"status": "expired", "reason": reason},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with session_factory() as session:
        contract_result = session.execute(
            update(InstructionExecutionContract)
            .where(
                InstructionExecutionContract.id == int(contract_id),
                InstructionExecutionContract.message_instruction_item_id
                == int(item_id),
                InstructionExecutionContract.state == "deferred",
                InstructionExecutionContract.state_version
                == int(contract_version),
            )
            .values(
                state="expired",
                state_version=int(contract_version) + 1,
                reason_code=reason,
                evidence_refs_json=evidence_json,
                last_progress_at=now,
                terminal_at=now,
                updated_at=now,
            )
        )
        item_result = session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id == int(item_id),
                MessageInstructionItem.status == "pending",
            )
            .values(
                status="failed",
                result_json=None,
                error_json=error_json,
                visibility_next_attempt_at=None,
                updated_at=now,
            )
        )
        attempt_result = session.execute(
            update(EntryAssemblyAttempt)
            .where(
                EntryAssemblyAttempt.id == int(attempt_id),
                EntryAssemblyAttempt.status == "pending",
            )
            .values(status="expired", updated_at=now)
        )
        if (
            contract_result.rowcount != 1
            or item_result.rowcount != 1
            or attempt_result.rowcount != 1
        ):
            session.rollback()
            return False
        session.add(
            InstructionExecutionTransition(
                contract_id=int(contract_id),
                state_version=int(contract_version) + 1,
                previous_state="deferred",
                next_state="expired",
                reason_code=reason,
                evidence_refs_json=evidence_json,
                created_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
        return True


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
