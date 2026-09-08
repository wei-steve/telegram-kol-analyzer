"""Bounded structured rechecks for deferred adjacent-entry admission."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

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
    RawMessage,
)


logger = logging.getLogger(__name__)

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
    incident_reporter: Callable[..., object] | None = None,
) -> EntryAdmissionReconcileResult:
    """Release or expire due attempts without invoking any exchange writer.

    The gate is ``disabled``, not ``live``, and that is the whole of A-3d. This
    loop is the timer-driven half of a pair whose other half --
    ``instruction_execution_reconciliation``, which expires a deferred contract
    once its deadline passes -- gates on ``disabled`` and therefore runs under
    ``shadow``. Production has been on ``shadow`` since 2026-09-04, so it has
    been expiring deferred entries while never retrying them: seven auto-trade
    entries died that way between 2026-08-17 and 2026-09-04. Nothing here
    depends on the enforcement semantics that ``live`` switches on (the durable
    mirror convergence, fail-closed contract projection, the terminal-write
    compare-and-set); those stay gated on ``live`` exactly as they were.
    """

    if execution_contract_mode == "disabled":
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
                # An entry that was recognised, admitted, held and then timed
                # out is invisible everywhere else: the item just reads
                # ``failed``. This alert is the entire operator-facing outcome.
                if _report_entry_admission_expired(
                    session_factory,
                    item=item,
                    deadline_at=deadline,
                    now=now,
                    incident_reporter=incident_reporter,
                ):
                    counts["incidents"] += 1
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


def _report_entry_admission_expired(
    session_factory,
    *,
    item,
    deadline_at: datetime,
    now: datetime,
    incident_reporter: Callable[..., object] | None,
) -> bool:
    """Alert on one expired entry. Never lets a failed alert undo the expiry.

    ``item`` is the detached snapshot taken before the expiry wrote
    ``result_json`` away, so the defer reason it was holding is still readable
    here and nowhere else afterwards.
    """

    reason_code = _defer_reason_code(item.result_json)
    with session_factory() as session:
        chat_id = (
            session.query(RawMessage.chat_id)
            .filter(RawMessage.id == int(item.raw_message_id))
            .scalar()
        )
    if incident_reporter is None:
        from telegram_kol_research.runtime_incident_adapters import (
            capture_entry_admission_expired,
            capture_runtime_incident_best_effort,
        )

        def incident_reporter(**kwargs):
            return capture_runtime_incident_best_effort(
                capture_entry_admission_expired,
                session_factory,
                **kwargs,
            )

    try:
        recorded = incident_reporter(
            message_instruction_item_id=int(item.id),
            raw_message_id=int(item.raw_message_id),
            chat_id=int(chat_id or 0),
            defer_reason_code=reason_code,
            deadline_at=deadline_at,
            occurred_at=now,
        )
    except Exception:
        # The expiry is already committed and is the durable fact. An alert
        # that raises must not make the loop retry an expiry it already did.
        logger.warning(
            "entry admission expiry incident capture raised item_id=%s",
            int(item.id),
            exc_info=True,
        )
        return False
    return recorded is not None


def _defer_reason_code(result_json: str | None) -> str:
    try:
        result = json.loads(result_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(result, dict):
        return "unknown"
    return str(result.get("reason") or "unknown")


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
