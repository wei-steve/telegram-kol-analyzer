"""Compare-and-set state transitions for instruction execution contracts."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    INSTRUCTION_EXECUTION_COMPLETION_SCOPES,
    INSTRUCTION_EXECUTION_STATES,
    INSTRUCTION_EXECUTION_TERMINAL_KINDS,
    InstructionExecutionContract,
    InstructionExecutionTransition,
    MessageInstructionItem,
)


LEGAL_INSTRUCTION_EXECUTION_EDGES = frozenset(
    {
        ("pending", "deferred"),
        ("pending", "submitting"),
        ("pending", "verified"),
        ("pending", "failed"),
        ("pending", "expired"),
        ("deferred", "pending"),
        ("deferred", "failed"),
        ("deferred", "expired"),
        ("submitting", "verified"),
        ("submitting", "failed"),
        ("submitting", "submit_unknown"),
        ("submit_unknown", "verified"),
        ("submit_unknown", "failed"),
    }
)


class InstructionExecutionTransitionError(ValueError):
    """The requested state change violates the execution contract."""


class InstructionExecutionConflictError(RuntimeError):
    """The contract no longer matches the caller's expected state/version."""


def _detached(session, contract: InstructionExecutionContract):
    session.refresh(contract)
    session.expunge(contract)
    return contract


def _evidence_json(evidence_refs: list[dict[str, object]]) -> str:
    if not isinstance(evidence_refs, list) or any(
        not isinstance(reference, dict) for reference in evidence_refs
    ):
        raise ValueError("evidence_refs must be a list of mappings")
    try:
        serialized = json.dumps(
            evidence_refs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence_refs must be JSON serializable") from exc
    if len(serialized) > 4096:
        raise ValueError("evidence_refs exceeds 4096 characters")
    return serialized


def load_or_create_instruction_execution_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    projected_at: datetime,
    deadline_at: datetime | None = None,
) -> InstructionExecutionContract:
    """Create one contract from authoritative persisted item references."""

    with session_factory() as session:
        existing = (
            session.query(InstructionExecutionContract)
            .filter(
                InstructionExecutionContract.message_instruction_item_id
                == int(message_instruction_item_id)
            )
            .one_or_none()
        )
        if existing is not None:
            return _detached(session, existing)
        item = session.get(MessageInstructionItem, int(message_instruction_item_id))
        if item is None:
            raise LookupError("message instruction item not found")
        contract = InstructionExecutionContract(
            message_instruction_item_id=int(item.id),
            raw_message_id=int(item.raw_message_id),
            signal_candidate_id=int(item.signal_candidate_id),
            strategy_instance_id=item.strategy_instance_id,
            intent_kind=str(item.instruction_kind),
            state="pending",
            state_version=0,
            deadline_at=deadline_at,
            last_progress_at=projected_at,
            created_at=projected_at,
            updated_at=projected_at,
        )
        session.add(contract)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(InstructionExecutionContract)
                .filter(
                    InstructionExecutionContract.message_instruction_item_id
                    == int(message_instruction_item_id)
                )
                .one_or_none()
            )
            if existing is None:
                raise
            return _detached(session, existing)
        return _detached(session, contract)


def transition_instruction_execution_contract(
    session_factory: sessionmaker,
    *,
    contract_id: int,
    expected_state: str,
    expected_version: int,
    new_state: str,
    reason_code: str,
    evidence_refs: list[dict[str, object]],
    transitioned_at: datetime,
    attempted_exchange_write: bool | None = None,
    trade_signal_id: int | None = None,
    execution_binding_id: int | None = None,
    terminal_kind: str | None = None,
    completion_scope: str | None = None,
    require_unexpired_at: datetime | None = None,
) -> InstructionExecutionContract:
    """Atomically update one expected version and append its immutable audit row."""

    expected_state = str(expected_state)
    new_state = str(new_state)
    if expected_state not in INSTRUCTION_EXECUTION_STATES:
        raise InstructionExecutionTransitionError("unknown expected state")
    if new_state not in INSTRUCTION_EXECUTION_STATES:
        raise InstructionExecutionTransitionError("unknown new state")
    if (expected_state, new_state) not in LEGAL_INSTRUCTION_EXECUTION_EDGES:
        raise InstructionExecutionTransitionError(
            f"illegal execution transition: {expected_state} -> {new_state}"
        )
    reason_code = str(reason_code).strip()
    if not reason_code or len(reason_code) > 128:
        raise ValueError("reason_code must contain 1 to 128 characters")
    serialized_evidence = _evidence_json(evidence_refs)
    if attempted_exchange_write not in {None, False, True}:
        raise ValueError("attempted_exchange_write must be boolean or None")
    if attempted_exchange_write is True and new_state != "submitting":
        raise InstructionExecutionTransitionError(
            "exchange-write intent may only be recorded when entering submitting"
        )
    if require_unexpired_at is not None and new_state != "submitting":
        raise InstructionExecutionTransitionError(
            "deadline guard may only be applied when entering submitting"
        )
    if new_state == "verified":
        if terminal_kind not in INSTRUCTION_EXECUTION_TERMINAL_KINDS:
            raise InstructionExecutionTransitionError(
                "verified transition requires a terminal kind"
            )
        if completion_scope not in INSTRUCTION_EXECUTION_COMPLETION_SCOPES:
            raise InstructionExecutionTransitionError(
                "verified transition requires a completion scope"
            )
    elif terminal_kind is not None or completion_scope is not None:
        raise InstructionExecutionTransitionError(
            "terminal kind and completion scope require verified state"
        )

    next_version = int(expected_version) + 1
    values: dict[str, object] = {
        "state": new_state,
        "state_version": next_version,
        "reason_code": reason_code,
        "evidence_refs_json": serialized_evidence,
        "last_progress_at": transitioned_at,
        "updated_at": transitioned_at,
    }
    if attempted_exchange_write is True:
        values["attempted_exchange_write"] = True
    if trade_signal_id is not None:
        values["trade_signal_id"] = int(trade_signal_id)
    if execution_binding_id is not None:
        values["execution_binding_id"] = int(execution_binding_id)
    if new_state == "verified":
        values.update(
            terminal_kind=terminal_kind,
            completion_scope=completion_scope,
            verified_at=transitioned_at,
            terminal_at=transitioned_at,
        )
    elif new_state in {"failed", "expired"}:
        values["terminal_at"] = transitioned_at

    with session_factory() as session:
        statement = update(InstructionExecutionContract).where(
            InstructionExecutionContract.id == int(contract_id),
            InstructionExecutionContract.state == expected_state,
            InstructionExecutionContract.state_version == int(expected_version),
        )
        if require_unexpired_at is not None:
            statement = statement.where(
                or_(
                    InstructionExecutionContract.deadline_at.is_(None),
                    InstructionExecutionContract.deadline_at
                    > require_unexpired_at,
                )
            )
        updated = session.execute(
            statement.values(**values).returning(InstructionExecutionContract.id)
        ).scalar_one_or_none()
        if updated is None:
            session.rollback()
            raise InstructionExecutionConflictError(
                "instruction execution contract state/version changed"
            )
        session.add(
            InstructionExecutionTransition(
                contract_id=int(contract_id),
                state_version=next_version,
                previous_state=expected_state,
                next_state=new_state,
                reason_code=reason_code,
                evidence_refs_json=serialized_evidence,
                created_at=transitioned_at,
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise InstructionExecutionConflictError(
                "instruction execution transition version already exists"
            ) from exc
        contract = session.get(InstructionExecutionContract, int(contract_id))
        if contract is None:
            raise InstructionExecutionConflictError(
                "instruction execution contract disappeared"
            )
        return _detached(session, contract)
