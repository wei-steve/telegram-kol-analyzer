"""Shadow-safe projection of durable entry submission evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import load_entry_binding_evidence
from telegram_kol_research.instruction_execution_contracts import (
    InstructionExecutionConflictError,
    load_or_create_instruction_execution_contract,
    transition_instruction_execution_contract,
)
from telegram_kol_research.instruction_execution_outcomes import (
    interpret_instruction_outcome,
)
from telegram_kol_research.models import (
    InstructionExecutionContract,
    MessageInstructionItem,
)
from telegram_kol_research.trade_signals import (
    load_trade_signal_execution_evidence,
)


class EntryExecutionContractBlocked(RuntimeError):
    """The durable execution state forbids another entry writer call."""


@dataclass(frozen=True, slots=True)
class EntrySubmissionPreparation:
    state: str
    contract_id: int | None
    draft_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class EntrySubmissionProjection:
    state: str
    completion_scope: str | None = None
    verified_leg_indices: tuple[int, ...] = ()
    incident_facts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryInstructionMirror:
    effective_status: str
    contract_id: int | None
    contract_state: str | None
    contract_state_version: int | None
    divergence: bool
    evidence: Mapping[str, object]


def _enabled(mode: str) -> bool:
    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("execution contract mode must be disabled, shadow, or live")
    return mode != "disabled"


def _draft_fingerprint(draft: dict[str, Any]) -> str:
    serialized = json.dumps(
        draft,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _load_contract(session_factory, *, message_instruction_item_id: int):
    with session_factory() as session:
        row = (
            session.query(InstructionExecutionContract)
            .filter(
                InstructionExecutionContract.message_instruction_item_id
                == int(message_instruction_item_id)
            )
            .one_or_none()
        )
        if row is not None:
            session.expunge(row)
        return row


def project_entry_non_writer_result_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    result: dict[str, Any],
    projected_at: datetime,
    mode: str,
) -> InstructionExecutionContract | None:
    """Project deterministic non-writer outcomes before the item mirror gate."""

    if not _enabled(mode):
        return None
    initial_contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        projected_at=projected_at,
    )
    outcome = interpret_instruction_outcome(result, intent_kind="entry")
    contract = initial_contract
    for _attempt in range(8):
        if contract.intent_kind != "entry":
            return contract
        if contract.state in {"verified", "failed", "expired", "submit_unknown"}:
            return contract
        if outcome.attempted_exchange_write:
            if contract.state == "deferred":
                try:
                    contract = transition_instruction_execution_contract(
                        session_factory,
                        contract_id=int(contract.id),
                        expected_state="deferred",
                        expected_version=int(contract.state_version),
                        new_state="pending",
                        reason_code="entry_result_write_contradicts_deferral",
                        evidence_refs=[{"kind": "entry_result_contradiction"}],
                        transitioned_at=projected_at,
                    )
                except InstructionExecutionConflictError:
                    contract = _load_contract(
                        session_factory,
                        message_instruction_item_id=message_instruction_item_id,
                    )
                    if contract is None:
                        raise EntryExecutionContractBlocked(
                            "entry_contract_missing_after_conflict"
                        )
                continue
            if contract.state == "pending":
                try:
                    contract = transition_instruction_execution_contract(
                        session_factory,
                        contract_id=int(contract.id),
                        expected_state="pending",
                        expected_version=int(contract.state_version),
                        new_state="submitting",
                        reason_code="entry_result_reports_exchange_write",
                        evidence_refs=[
                            {
                                "kind": "entry_attempted_write_result",
                                "status": str(result.get("status") or "")[:64],
                            }
                        ],
                        transitioned_at=projected_at,
                        attempted_exchange_write=True,
                    )
                except InstructionExecutionConflictError:
                    contract = _load_contract(
                        session_factory,
                        message_instruction_item_id=message_instruction_item_id,
                    )
                    if contract is None:
                        raise EntryExecutionContractBlocked(
                            "entry_contract_missing_after_conflict"
                        )
                continue
            if contract.state == "submitting":
                try:
                    return transition_instruction_execution_contract(
                        session_factory,
                        contract_id=int(contract.id),
                        expected_state="submitting",
                        expected_version=int(contract.state_version),
                        new_state="submit_unknown",
                        reason_code="entry_result_attempted_write_unverified",
                        evidence_refs=[
                            {
                                "kind": "entry_attempted_write_result",
                                "status": str(result.get("status") or "")[:64],
                                "reason": str(result.get("reason") or "")[:128],
                            }
                        ],
                        transitioned_at=projected_at,
                    )
                except InstructionExecutionConflictError:
                    contract = _load_contract(
                        session_factory,
                        message_instruction_item_id=message_instruction_item_id,
                    )
                    if contract is None:
                        raise EntryExecutionContractBlocked(
                            "entry_contract_missing_after_conflict"
                        )
                    continue
            return contract

        target_state: str | None = None
        if outcome.state in {"failed", "expired"}:
            if contract.state in {"pending", "deferred"}:
                target_state = outcome.state
        elif outcome.state == "deferred" and contract.state == "pending":
            target_state = "deferred"
        if target_state is None:
            return contract
        try:
            return transition_instruction_execution_contract(
                session_factory,
                contract_id=int(contract.id),
                expected_state=str(contract.state),
                expected_version=int(contract.state_version),
                new_state=target_state,
                reason_code=str(outcome.reason_code)[:128],
                evidence_refs=[
                    {
                        "kind": "entry_non_writer_result",
                        "status": str(result.get("status") or "")[:64],
                        "reason": str(result.get("reason") or "")[:128],
                    }
                ],
                transitioned_at=projected_at,
            )
        except InstructionExecutionConflictError:
            contract = _load_contract(
                session_factory,
                message_instruction_item_id=message_instruction_item_id,
            )
            if contract is None:
                raise EntryExecutionContractBlocked(
                    "entry_contract_missing_after_conflict"
                )
    raise EntryExecutionContractBlocked("entry_contract_projection_conflict")


def project_entry_refusal_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    reason_code: str,
    evidence_refs: list[dict[str, object]],
    projected_at: datetime,
    mode: str,
) -> InstructionExecutionContract | None:
    """Persist one explicit pre-writer entry refusal from its decision point."""

    if not _enabled(mode):
        return None
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        projected_at=projected_at,
    )
    for _attempt in range(8):
        if contract.intent_kind != "entry":
            raise EntryExecutionContractBlocked("non_entry_instruction_contract")
        if contract.state in {
            "verified",
            "failed",
            "expired",
            "submit_unknown",
            "submitting",
        }:
            return contract
        if contract.state == "deferred":
            try:
                contract = transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state="deferred",
                    expected_version=int(contract.state_version),
                    new_state="pending",
                    reason_code="entry_admission_released_without_writer",
                    evidence_refs=[{"kind": "entry_refusal_admission_release"}],
                    transitioned_at=projected_at,
                )
            except InstructionExecutionConflictError:
                contract = _load_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
                if contract is None:
                    raise EntryExecutionContractBlocked(
                        "entry_contract_missing_after_conflict"
                    )
            continue
        try:
            return transition_instruction_execution_contract(
                session_factory,
                contract_id=int(contract.id),
                expected_state="pending",
                expected_version=int(contract.state_version),
                new_state="verified",
                reason_code=str(reason_code)[:128],
                evidence_refs=evidence_refs,
                transitioned_at=projected_at,
                terminal_kind="verified_refusal",
                completion_scope="full",
            )
        except InstructionExecutionConflictError:
            contract = _load_contract(
                session_factory,
                message_instruction_item_id=message_instruction_item_id,
            )
            if contract is None:
                raise EntryExecutionContractBlocked(
                    "entry_contract_missing_after_conflict"
                )
    raise EntryExecutionContractBlocked("entry_contract_projection_conflict")


def resolve_entry_instruction_mirror(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    requested_status: str,
    mode: str,
) -> EntryInstructionMirror:
    """Resolve the item mirror exclusively from durable entry-contract truth."""

    if mode not in {"disabled", "shadow", "live"}:
        raise ValueError("execution contract mode must be disabled, shadow, or live")
    requested = str(requested_status)
    if mode == "disabled":
        return EntryInstructionMirror(requested, None, None, None, False, {})
    with session_factory() as session:
        item = session.get(MessageInstructionItem, int(message_instruction_item_id))
        if item is None:
            raise LookupError("message instruction item not found")
        if str(item.instruction_kind) != "entry":
            return EntryInstructionMirror(requested, None, None, None, False, {})
    contract = _load_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
    )
    expected = "failed"
    reason_code = "execution_contract_missing"
    state = None
    terminal_kind = None
    completion_scope = None
    contract_id = None
    state_version = None
    if contract is not None:
        contract_id = int(contract.id)
        state = str(contract.state)
        state_version = int(contract.state_version)
        terminal_kind = contract.terminal_kind
        completion_scope = contract.completion_scope
        reason_code = str(contract.reason_code or "") or "execution_contract_pending"
        if state == "deferred":
            expected = "pending"
        elif state == "submitting":
            expected = "executing"
        elif state == "submit_unknown":
            expected = "unknown"
        elif state in {"failed", "expired"}:
            expected = "failed"
        elif (
            state == "verified"
            and contract.intent_kind == "entry"
            and terminal_kind == "verified_refusal"
            and completion_scope == "full"
        ):
            expected = "succeeded"
        elif (
            state == "verified"
            and contract.intent_kind == "entry"
            and terminal_kind == "verified_entry"
            and completion_scope in {"full", "partial"}
        ):
            expected = "submitted"
        elif state == "verified":
            expected = "failed"
            reason_code = "entry_terminal_contract_invalid"
        else:
            expected = "failed"
            reason_code = "verified_terminal_contract_required"
    divergence = expected != requested
    evidence: dict[str, object] = {
        "contract_id": contract_id,
        "state": state or "missing",
        "state_version": state_version,
        "terminal_kind": terminal_kind,
        "completion_scope": completion_scope,
        "reason_code": reason_code[:128],
        "expected_item_status": expected,
        "observed_item_status": requested,
        "divergence": divergence,
    }
    return EntryInstructionMirror(
        expected if mode == "live" else requested,
        contract_id,
        state,
        state_version,
        divergence,
        evidence,
    )


def project_entry_deferred_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    reason_code: str,
    blocker_ids: tuple[int, ...],
    deadline_at: datetime | None = None,
    recheck_fingerprint: str | None = None,
    projected_at: datetime,
    mode: str,
):
    """Project admission defer evidence without changing legacy item behavior."""

    if not _enabled(mode):
        return None
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        projected_at=projected_at,
        deadline_at=deadline_at,
    )
    if deadline_at is not None and contract.deadline_at is None:
        with session_factory() as session:
            session.execute(
                update(InstructionExecutionContract)
                .where(
                    InstructionExecutionContract.id == int(contract.id),
                    InstructionExecutionContract.deadline_at.is_(None),
                )
                .values(deadline_at=deadline_at, updated_at=projected_at)
            )
            session.commit()
            contract = session.get(InstructionExecutionContract, int(contract.id))
            session.expunge(contract)
    if contract.intent_kind != "entry":
        raise EntryExecutionContractBlocked("non_entry_instruction_contract")
    if contract.state == "deferred":
        return contract
    if contract.state != "pending":
        raise EntryExecutionContractBlocked(
            f"entry_contract_{contract.state}"
        )
    return transition_instruction_execution_contract(
        session_factory,
        contract_id=int(contract.id),
        expected_state="pending",
        expected_version=int(contract.state_version),
        new_state="deferred",
        reason_code=str(reason_code)[:128],
        evidence_refs=[
            {
                "kind": "entry_admission_blockers",
                "raw_message_ids": sorted(set(int(value) for value in blocker_ids)),
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "recheck_fingerprint": recheck_fingerprint,
            }
        ],
        transitioned_at=projected_at,
    )


def prepare_entry_submission_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    trade_signal_id: int,
    draft: dict[str, Any],
    prepared_at: datetime,
    mode: str,
) -> EntrySubmissionPreparation:
    """Persist submit intent immediately before the existing exchange writer."""

    if not _enabled(mode):
        return EntrySubmissionPreparation("disabled", None, None)
    fingerprint = _draft_fingerprint(draft)
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        projected_at=prepared_at,
    )
    if contract.intent_kind != "entry":
        raise EntryExecutionContractBlocked("non_entry_instruction_contract")
    if contract.state == "submit_unknown":
        raise EntryExecutionContractBlocked("entry_contract_submit_unknown")
    if contract.state in {"verified", "failed", "expired"}:
        raise EntryExecutionContractBlocked(f"entry_contract_{contract.state}")
    if contract.state == "submitting":
        raise EntryExecutionContractBlocked(
            "entry_contract_submitting_requires_reconciliation"
        )
    if contract.deadline_at is not None and _as_utc(prepared_at) >= _as_utc(
        contract.deadline_at
    ):
        _expire_entry_contract_deadline(
            session_factory,
            contract=contract,
            expired_at=prepared_at,
        )
        raise EntryExecutionContractBlocked("entry_contract_deadline_expired")
    if contract.state == "deferred":
        contract = transition_instruction_execution_contract(
            session_factory,
            contract_id=int(contract.id),
            expected_state="deferred",
            expected_version=int(contract.state_version),
            new_state="pending",
            reason_code="entry_admission_released",
            evidence_refs=[{"kind": "entry_admission_recheck"}],
            transitioned_at=prepared_at,
        )
    try:
        transitioned = transition_instruction_execution_contract(
            session_factory,
            contract_id=int(contract.id),
            expected_state="pending",
            expected_version=int(contract.state_version),
            new_state="submitting",
            reason_code="entry_writer_call_imminent",
            evidence_refs=[
                {
                    "kind": "entry_draft",
                    "fingerprint": fingerprint,
                    "trade_signal_id": int(trade_signal_id),
                }
            ],
            transitioned_at=prepared_at,
            attempted_exchange_write=True,
            trade_signal_id=int(trade_signal_id),
            require_unexpired_at=prepared_at,
        )
    except InstructionExecutionConflictError:
        latest = _load_contract(
            session_factory,
            message_instruction_item_id=message_instruction_item_id,
        )
        if (
            latest is not None
            and latest.deadline_at is not None
            and _as_utc(prepared_at) >= _as_utc(latest.deadline_at)
        ):
            _expire_entry_contract_deadline(
                session_factory,
                contract=latest,
                expired_at=prepared_at,
            )
            raise EntryExecutionContractBlocked("entry_contract_deadline_expired")
        raise
    return EntrySubmissionPreparation(
        transitioned.state,
        int(transitioned.id),
        fingerprint,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _expire_entry_contract_deadline(
    session_factory: sessionmaker,
    *,
    contract: InstructionExecutionContract,
    expired_at: datetime,
) -> InstructionExecutionContract:
    if contract.state == "expired":
        return contract
    if contract.state not in {"pending", "deferred"}:
        raise EntryExecutionContractBlocked(f"entry_contract_{contract.state}")
    try:
        return transition_instruction_execution_contract(
            session_factory,
            contract_id=int(contract.id),
            expected_state=str(contract.state),
            expected_version=int(contract.state_version),
            new_state="expired",
            reason_code="entry_execution_deadline_expired",
            evidence_refs=[{"kind": "entry_execution_deadline"}],
            transitioned_at=expired_at,
        )
    except InstructionExecutionConflictError:
        latest = _load_contract(
            session_factory,
            message_instruction_item_id=int(contract.message_instruction_item_id),
        )
        if latest is not None and latest.state == "expired":
            return latest
        raise EntryExecutionContractBlocked("entry_contract_deadline_expiry_conflict")


def project_entry_submission_result(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    trade_signal_id: int,
    attempted_writes: int,
    confirmed_legs: int,
    projected_at: datetime,
    mode: str,
    error: Exception | None = None,
    confirmed_absent_leg_indices: tuple[int, ...] = (),
) -> EntrySubmissionProjection:
    """Transition from submitting using exact durable signal/binding/leg evidence."""

    if not _enabled(mode):
        return EntrySubmissionProjection("disabled")
    contract = _load_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
    )
    if contract is None:
        raise EntryExecutionContractBlocked("entry_contract_missing")
    if contract.state in {"verified", "failed", "expired", "submit_unknown"}:
        return EntrySubmissionProjection(
            contract.state,
            completion_scope=contract.completion_scope,
        )
    if contract.state != "submitting":
        raise EntryExecutionContractBlocked(f"entry_contract_{contract.state}")
    if int(contract.trade_signal_id or 0) != int(trade_signal_id):
        raise EntryExecutionContractBlocked("entry_contract_signal_mismatch")

    signal = load_trade_signal_execution_evidence(
        session_factory,
        signal_id=int(trade_signal_id),
    )
    expected_indices = _selected_leg_indices(signal.draft)
    binding = load_entry_binding_evidence(
        session_factory,
        chat_id=signal.chat_id,
        message_id=signal.message_id,
        symbol=signal.symbol,
        side=signal.side,
        strategy_instance_id=signal.strategy_instance_id,
    )
    verified_indices = tuple(
        sorted(
            index
            for index, client_order_id in zip(
                binding.leg_indices,
                binding.client_order_ids,
                strict=True,
            )
            if index in expected_indices
            and client_order_id == _expected_client_order_id(signal.draft, index)
        )
    )
    if binding.draft_fingerprint != _draft_fingerprint(signal.draft):
        verified_indices = ()
    durable_absent_indices = tuple(
        sorted(
            index
            for index, client_order_id in zip(
                binding.confirmed_absent_leg_indices,
                binding.confirmed_absent_client_order_ids,
                strict=True,
            )
            if index in expected_indices
            and client_order_id == _expected_client_order_id(signal.draft, index)
        )
    )
    requested_absent = set(int(value) for value in confirmed_absent_leg_indices)
    absent_indices = tuple(
        index
        for index in durable_absent_indices
        if not requested_absent or index in requested_absent
    )
    exact_partition = (
        bool(expected_indices)
        and not set(verified_indices).intersection(absent_indices)
        and set(verified_indices).union(absent_indices) == set(expected_indices)
    )

    if error is not None and int(attempted_writes) == 0:
        transitioned = _finish(
            session_factory,
            contract=contract,
            new_state="failed",
            reason_code="entry_pre_submit_failed",
            projected_at=projected_at,
            evidence_refs=[{"kind": "trade_signal", "id": signal.id, "status": signal.status}],
        )
        return EntrySubmissionProjection(transitioned.state)

    if exact_partition and verified_indices:
        completion_scope = (
            "full" if set(verified_indices) == set(expected_indices) else "partial"
        )
        facts = () if completion_scope == "full" else ("multi_leg_partial",)
        evidence_refs: list[dict[str, object]] = [
            {
                "kind": "entry_binding",
                "id": int(binding.binding_id or 0),
                "verified_leg_indices": list(verified_indices),
                "confirmed_absent_leg_indices": list(absent_indices),
            }
        ]
        if facts:
            evidence_refs.append({"kind": "incident_fact", "code": facts[0]})
        transitioned = _finish(
            session_factory,
            contract=contract,
            new_state="verified",
            reason_code=(
                "entry_submission_verified"
                if completion_scope == "full"
                else "entry_submission_verified_partial"
            ),
            projected_at=projected_at,
            evidence_refs=evidence_refs,
            execution_binding_id=binding.binding_id,
            terminal_kind="verified_entry",
            completion_scope=completion_scope,
        )
        return EntrySubmissionProjection(
            transitioned.state,
            completion_scope,
            verified_indices,
            facts,
        )

    terminal_unknown = (
        int(attempted_writes) > 0
        or int(confirmed_legs) > 0
        or signal.status in {"unknown_exchange_outcome", "partial_submission_failed"}
        or type(error).__name__ == "DeepcoinRequestOutcomeUnknown"
    )
    transitioned = _finish(
        session_factory,
        contract=contract,
        new_state="submit_unknown" if terminal_unknown else "failed",
        reason_code=(
            "entry_submission_outcome_unknown"
            if terminal_unknown
            else "entry_submission_failed"
        ),
        projected_at=projected_at,
        evidence_refs=[
            {
                "kind": "trade_signal",
                "id": signal.id,
                "status": signal.status,
                "attempted_writes": max(0, int(attempted_writes)),
                "confirmed_legs": max(0, int(confirmed_legs)),
            }
        ],
    )
    return EntrySubmissionProjection(transitioned.state)


def _selected_leg_indices(draft: dict[str, Any]) -> tuple[int, ...]:
    legs = draft.get("order_legs")
    if not isinstance(legs, list) or not legs:
        return ()
    selected = draft.get("selected_entry_leg_indices")
    if not isinstance(selected, list):
        return tuple(range(1, len(legs) + 1))
    valid = {
        int(value)
        for value in selected
        if type(value) is int and 1 <= int(value) <= len(legs)
    }
    return tuple(sorted(valid))


def _expected_client_order_id(draft: dict[str, Any], leg_index: int) -> str:
    legs = draft.get("order_legs")
    if not isinstance(legs, list) or not (1 <= int(leg_index) <= len(legs)):
        return ""
    leg = legs[int(leg_index) - 1]
    return str(leg.get("client_order_id") or "") if isinstance(leg, dict) else ""


def _finish(
    session_factory,
    *,
    contract,
    new_state: str,
    reason_code: str,
    projected_at: datetime,
    evidence_refs: list[dict[str, object]],
    execution_binding_id: int | None = None,
    terminal_kind: str | None = None,
    completion_scope: str | None = None,
):
    return transition_instruction_execution_contract(
        session_factory,
        contract_id=int(contract.id),
        expected_state="submitting",
        expected_version=int(contract.state_version),
        new_state=new_state,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        transitioned_at=projected_at,
        execution_binding_id=execution_binding_id,
        terminal_kind=terminal_kind,
        completion_scope=completion_scope,
    )
