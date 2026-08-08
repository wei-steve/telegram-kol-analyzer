"""Dormant, deterministic persistence helpers for message-operation contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MessageOperationContract,
    MessageOperationItem,
    utc_now,
)


POLICY_VERSION = "message-operation-contract-v1"
MAX_EVIDENCE_REFS_JSON_LENGTH = 4096
INTENT_KINDS = frozenset(
    {
        "new_entry",
        "add_entry",
        "take_profit",
        "stop_loss",
        "cancel",
        "exit",
        "manage",
        "unresolved_executable",
        "other_management",
    }
)
TERMINAL_KINDS = frozenset(
    {
        "verified_entry",
        "verified_management",
        "verified_execution",
        "verified_cancel",
        "verified_exit",
        "verified_protection",
        "verified_refusal",
    }
)
CONTRACT_STATUSES = frozenset(
    {"observing", "verified", "violated", "superseded", "duplicate"}
)
ITEM_STATUSES = CONTRACT_STATUSES
_TERMINAL_CONTRACT_STATUSES = CONTRACT_STATUSES - {"observing"}
_EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._-]{1,128}"
)


class MessageOperationContractBoundsError(ValueError):
    """Raised before invalid or unbounded contract data reaches storage."""


def _bounded_text(name: str, value: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise MessageOperationContractBoundsError(
            f"{name} must contain 1 to {maximum} characters"
        )
    return value


def _closed_value(name: str, value: str, allowed: frozenset[str]) -> str:
    normalized = _bounded_text(name, value, maximum=max(map(len, allowed)))
    if normalized not in allowed:
        raise MessageOperationContractBoundsError(f"unsupported {name}")
    return normalized


def _evidence_refs_json(evidence_refs: Sequence[str] | None) -> str:
    refs = list(evidence_refs or ())
    if not all(
        isinstance(reference, str)
        and _EVIDENCE_REFERENCE_PATTERN.fullmatch(reference)
        for reference in refs
    ):
        raise MessageOperationContractBoundsError(
            "evidence references must be bounded stable references"
        )
    normalized = json.dumps(refs, separators=(",", ":"), ensure_ascii=True)
    if len(normalized) > MAX_EVIDENCE_REFS_JSON_LENGTH:
        raise MessageOperationContractBoundsError("evidence references are unbounded")
    return normalized


def _detach(session, row):
    session.refresh(row)
    session.expunge(row)
    return row


def create_message_operation_contract(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    intent_kind: str,
    expected_terminal_kind: str,
    deadline_at: datetime,
    policy_version: str = POLICY_VERSION,
    now: datetime | None = None,
) -> MessageOperationContract:
    """Idempotently persist a dormant contract without touching message processing."""

    if not isinstance(raw_message_id, int) or isinstance(raw_message_id, bool) or raw_message_id < 1:
        raise MessageOperationContractBoundsError("raw_message_id must be positive")
    intent_kind = _closed_value("intent_kind", intent_kind, INTENT_KINDS)
    expected_terminal_kind = _closed_value(
        "expected_terminal_kind", expected_terminal_kind, TERMINAL_KINDS
    )
    policy_version = _bounded_text("policy_version", policy_version, maximum=64)
    if not isinstance(deadline_at, datetime):
        raise MessageOperationContractBoundsError("deadline_at must be a datetime")
    timestamp = now or utc_now()
    values = {
        "raw_message_id": raw_message_id,
        "intent_kind": intent_kind,
        "expected_terminal_kind": expected_terminal_kind,
        "status": "observing",
        "deadline_at": deadline_at,
        "evidence_refs_json": "[]",
        "agent_requested": False,
        "policy_version": policy_version,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with session_factory() as session:
        statement = sqlite_insert(MessageOperationContract).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=["raw_message_id", "policy_version"]
        )
        session.execute(statement)
        session.commit()
        row = session.execute(
            select(MessageOperationContract).where(
                MessageOperationContract.raw_message_id == raw_message_id,
                MessageOperationContract.policy_version == policy_version,
            )
        ).scalar_one()
        if (
            row.intent_kind != intent_kind
            or row.expected_terminal_kind != expected_terminal_kind
            or row.deadline_at != deadline_at.replace(tzinfo=None)
        ):
            raise MessageOperationContractBoundsError(
                "existing contract conflicts with authoritative expectation"
            )
        return _detach(session, row)


def get_message_operation_contract(
    session_factory: sessionmaker,
    contract_id: int,
) -> MessageOperationContract | None:
    with session_factory() as session:
        row = session.get(MessageOperationContract, contract_id)
        return None if row is None else _detach(session, row)


def append_message_operation_item(
    session_factory: sessionmaker,
    *,
    contract_id: int,
    sequence: int,
    instruction_key: str,
    instruction_kind: str,
    authoritative_instruction_id: str,
    expected_descendant_kind: str,
    expected_terminal_kind: str,
    evidence_refs: Sequence[str] | None = None,
    now: datetime | None = None,
) -> MessageOperationItem:
    """Idempotently append one bounded authoritative item to a contract."""

    if not isinstance(contract_id, int) or isinstance(contract_id, bool) or contract_id < 1:
        raise MessageOperationContractBoundsError("contract_id must be positive")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise MessageOperationContractBoundsError("sequence must be positive")
    instruction_key = _bounded_text("instruction_key", instruction_key, maximum=128)
    instruction_kind = _bounded_text("instruction_kind", instruction_kind, maximum=32)
    authoritative_instruction_id = _bounded_text(
        "authoritative_instruction_id", authoritative_instruction_id, maximum=255
    )
    expected_descendant_kind = _bounded_text(
        "expected_descendant_kind", expected_descendant_kind, maximum=64
    )
    expected_terminal_kind = _bounded_text(
        "expected_terminal_kind", expected_terminal_kind, maximum=64
    )
    refs_json = _evidence_refs_json(evidence_refs)
    timestamp = now or utc_now()
    values = {
        "contract_id": contract_id,
        "sequence": sequence,
        "instruction_key": instruction_key,
        "instruction_kind": instruction_kind,
        "authoritative_instruction_id": authoritative_instruction_id,
        "expected_descendant_kind": expected_descendant_kind,
        "expected_terminal_kind": expected_terminal_kind,
        "status": "observing",
        "evidence_refs_json": refs_json,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with session_factory() as session:
        statement = sqlite_insert(MessageOperationItem).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=["contract_id", "instruction_key"]
        )
        session.execute(statement)
        session.commit()
        row = session.execute(
            select(MessageOperationItem).where(
                MessageOperationItem.contract_id == contract_id,
                MessageOperationItem.instruction_key == instruction_key,
            )
        ).scalar_one()
        comparable_fields = (
            "sequence",
            "instruction_kind",
            "authoritative_instruction_id",
            "expected_descendant_kind",
            "expected_terminal_kind",
            "evidence_refs_json",
        )
        if any(getattr(row, field) != values[field] for field in comparable_fields):
            raise MessageOperationContractBoundsError(
                "existing item conflicts with authoritative expectation"
            )
        return _detach(session, row)


def transition_message_operation_contract(
    session_factory: sessionmaker,
    *,
    contract_id: int,
    expected_status: str,
    new_status: str,
    violation_code: str | None = None,
    evidence_refs: Sequence[str] | None = None,
    runtime_incident_id: int | None = None,
    agent_requested: bool = False,
    now: datetime | None = None,
) -> bool:
    """Apply one terminal contract transition through compare-and-set."""

    expected_status = _closed_value(
        "expected_status", expected_status, CONTRACT_STATUSES
    )
    new_status = _closed_value("status", new_status, _TERMINAL_CONTRACT_STATUSES)
    if expected_status != "observing":
        raise MessageOperationContractBoundsError(
            "expected_status must be observing for a terminal transition"
        )
    if violation_code is not None:
        violation_code = _bounded_text("violation_code", violation_code, maximum=64)
    if new_status == "violated" and violation_code is None:
        raise MessageOperationContractBoundsError(
            "violated status requires violation_code"
        )
    if new_status != "violated" and violation_code is not None:
        raise MessageOperationContractBoundsError(
            "violation_code is only valid for violated status"
        )
    if runtime_incident_id is not None and (
        not isinstance(runtime_incident_id, int)
        or isinstance(runtime_incident_id, bool)
        or runtime_incident_id < 1
    ):
        raise MessageOperationContractBoundsError(
            "runtime_incident_id must be positive"
        )
    values = {
        "status": new_status,
        "violation_code": violation_code,
        "evidence_refs_json": _evidence_refs_json(evidence_refs),
        "runtime_incident_id": runtime_incident_id,
        "agent_requested": bool(agent_requested),
        "updated_at": now or utc_now(),
    }
    with session_factory() as session:
        result = session.execute(
            update(MessageOperationContract)
            .where(
                MessageOperationContract.id == contract_id,
                MessageOperationContract.status == expected_status,
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1
