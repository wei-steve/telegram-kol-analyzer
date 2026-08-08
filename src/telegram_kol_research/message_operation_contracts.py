"""Dormant, deterministic persistence helpers for message-operation contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageOperationContract,
    MessageOperationItem,
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    MessageInstructionItem,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
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
INSTRUCTION_KINDS = INTENT_KINDS
DESCENDANT_KINDS = frozenset(
    {
        "signal_candidate",
        "strategy_lifecycle",
        "management_envelope",
        "management_target",
        "management_item",
        "execution_binding",
        "execution_order_leg",
        "execution_event",
        "position_mutation_intent",
        "protection_revision",
        "context_resolution_attempt",
        "safety_refusal",
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
_DEADLINE_SECONDS = {
    "new_entry": 600,
    "add_entry": 600,
    "take_profit": 180,
    "stop_loss": 60,
    "cancel": 60,
    "exit": 60,
    "manage": 300,
    "unresolved_executable": 60,
    "other_management": 300,
}
_TAKE_PROFIT_ACTIONS = frozenset(
    {"partial_take_profit", "take_profit", "reduce_position", "close_half"}
)
_STOP_LOSS_ACTIONS = frozenset(
    {
        "stop_loss",
        "move_stop_to_break_even",
        "move_stop_to_protect",
        "break_even",
        "modify_stop",
    }
)
_CANCEL_ACTIONS = frozenset(
    {"cancel", "cancel_entry", "cancel_order", "cancel_pending_entry"}
)
_EXIT_ACTIONS = frozenset(
    {"exit", "exit_position", "full_exit", "full_close", "close_position"}
)
_ADD_ENTRY_ACTIONS = frozenset({"add_entry", "add_position"})
_EXECUTABLE_EVENT_TYPES = frozenset(
    {"entry_signal", "add_entry", "position_update", "exit_position", "cancel_entry"}
)
_NON_EXECUTABLE_AUTHORITATIVE_STATUSES = frozenset(
    {"非策略", "non_strategy", "not_strategy"}
)


@dataclass(frozen=True, slots=True)
class MessageOperationItemProjection:
    sequence: int
    intent_kind: str
    instruction_key: str
    authoritative_instruction_id: str
    expected_descendant_kind: str
    expected_terminal_kind: str
    evidence_references: tuple[str, ...]
    target_lifecycle_id: int | None = None
    source_disposition: str = "active"


@dataclass(frozen=True, slots=True)
class MessageOperationProjection:
    raw_message_id: int
    executable_intent: bool
    intent_kind: str
    expected_terminal_kind: str
    deadline_at: datetime
    items: tuple[MessageOperationItemProjection, ...]
    evidence_references: tuple[str, ...]
    model_calls: int = 0


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
    evidence_refs: Sequence[str] | None = None,
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
    refs_json = _evidence_refs_json(evidence_refs)
    values = {
        "raw_message_id": raw_message_id,
        "intent_kind": intent_kind,
        "expected_terminal_kind": expected_terminal_kind,
        "status": "observing",
        "deadline_at": deadline_at,
        "evidence_refs_json": refs_json,
        "agent_requested": False,
        "policy_version": policy_version,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with session_factory() as session:
        if session.get(RawMessage, raw_message_id) is None:
            raise MessageOperationContractBoundsError(
                "raw_message_id does not identify an authoritative message"
            )
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
            or row.evidence_refs_json != refs_json
        ):
            raise MessageOperationContractBoundsError(
                "existing contract conflicts with authoritative expectation"
            )
        return _detach(session, row)


def project_message_operation_contract(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> MessageOperationProjection | None:
    """Project expectations from existing durable facts without model calls."""

    with session_factory() as session:
        raw = session.get(RawMessage, raw_message_id)
        if raw is None:
            raise MessageOperationContractBoundsError(
                "raw_message_id does not identify an authoritative message"
            )
        decision = session.execute(
            select(RecognitionDecision).where(
                RecognitionDecision.raw_message_id == raw_message_id
            )
        ).scalar_one_or_none()
        envelope = session.execute(
            select(ManagementMessageEnvelope)
            .where(ManagementMessageEnvelope.raw_message_id == raw_message_id)
            .order_by(ManagementMessageEnvelope.id.desc())
        ).scalars().first()
        targets = (
            session.execute(
                select(ManagementMessageTarget)
                .where(ManagementMessageTarget.envelope_id == envelope.id)
                .order_by(
                    ManagementMessageTarget.target_ordinal,
                    ManagementMessageTarget.id,
                )
            ).scalars().all()
            if envelope is not None
            else []
        )
        instruction_rows = session.execute(
            select(MessageInstructionItem)
            .where(MessageInstructionItem.raw_message_id == raw_message_id)
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
        ).scalars().all()
        candidates = session.execute(
            select(SignalCandidate)
            .where(SignalCandidate.raw_message_id == raw_message_id)
            .order_by(SignalCandidate.id)
        ).scalars().all()
        candidate_by_id = {row.id: row for row in candidates}
        item_by_id = {row.id: row for row in instruction_rows}
        context_attempt = session.execute(
            select(ContextResolutionAttempt)
            .where(ContextResolutionAttempt.raw_message_id == raw_message_id)
            .order_by(ContextResolutionAttempt.id.desc())
        ).scalars().first()

        decision_payload = _bounded_decision_payload(decision)
        context_resolution = decision_payload.get("_context_resolution")
        context_resolution = (
            context_resolution if isinstance(context_resolution, dict) else {}
        )
        context_attempt_payload = _bounded_json_object(
            context_attempt.decision_json if context_attempt is not None else None
        )
        resolution_status = str(
            context_resolution.get("decision") or ""
        ).strip().lower()
        attempt_resolution_status = str(
            context_attempt_payload.get("decision") or ""
        ).strip().lower()
        decision_event = decision_payload.get("lifecycle_event")
        decision_event = decision_event if isinstance(decision_event, dict) else {}
        event_type = str(decision_event.get("event_type") or "").strip().lower()
        decision_action = str(
            decision_event.get("management_action")
            or decision_payload.get("management_action")
            or ""
        ).strip().lower()
        unresolved = (
            context_attempt is not None
            and str(context_attempt.status).strip().lower() == "completed"
            and resolution_status in {"hold", "unresolved"}
            and attempt_resolution_status == resolution_status
        )

        projected_items: list[MessageOperationItemProjection] = []
        if unresolved:
            projected_items.append(
                _recognition_item_projection(
                    decision=decision,
                    raw_message_id=raw_message_id,
                    context_attempt_id=context_attempt.id,
                )
            )
        elif targets:
            for sequence, target in enumerate(targets, start=1):
                source_item = item_by_id.get(target.message_instruction_item_id)
                intent = _action_to_intent(
                    target.normalized_action,
                    event_type="position_update",
                    instruction_kind="management",
                )
                projected_items.append(
                    _item_projection(
                        sequence=sequence,
                        intent_kind=intent,
                        instruction_key=f"management_target:{target.id}",
                        authoritative_instruction_id=f"management_target:{target.id}",
                        evidence_references=tuple(
                            reference
                            for reference in (
                                f"management_target:{target.id}",
                                f"message_instruction:{source_item.id}"
                                if source_item is not None
                                else None,
                            )
                            if reference is not None
                        ),
                        target_lifecycle_id=target.target_lifecycle_id,
                        source_disposition=_source_disposition(source_item),
                    )
                )
        elif instruction_rows:
            for sequence, item in enumerate(instruction_rows, start=1):
                candidate = candidate_by_id.get(item.signal_candidate_id)
                intent = _action_to_intent(
                    candidate.management_action if candidate is not None else None,
                    event_type=candidate.event_type if candidate is not None else event_type,
                    instruction_kind=item.instruction_kind,
                )
                projected_items.append(
                    _item_projection(
                        sequence=sequence,
                        intent_kind=intent,
                        instruction_key=f"message_instruction:{item.id}",
                        authoritative_instruction_id=f"message_instruction:{item.id}",
                        evidence_references=tuple(
                            reference
                            for reference in (
                                f"message_instruction:{item.id}",
                                f"signal_candidate:{candidate.id}"
                                if candidate is not None
                                else None,
                            )
                            if reference is not None
                        ),
                        target_lifecycle_id=(
                            candidate.target_lifecycle_id
                            if candidate is not None
                            else None
                        ),
                        source_disposition=_source_disposition(item),
                    )
                )
        elif candidates:
            for sequence, candidate in enumerate(candidates, start=1):
                intent = _action_to_intent(
                    candidate.management_action,
                    event_type=candidate.event_type,
                    instruction_kind=(
                        "entry"
                        if candidate.event_type in {"entry_signal", "add_entry"}
                        else "management"
                    ),
                )
                projected_items.append(
                    _item_projection(
                        sequence=sequence,
                        intent_kind=intent,
                        instruction_key=f"signal_candidate:{candidate.id}",
                        authoritative_instruction_id=f"signal_candidate:{candidate.id}",
                        evidence_references=(f"signal_candidate:{candidate.id}",),
                        target_lifecycle_id=candidate.target_lifecycle_id,
                    )
                )
        elif (
            event_type in _EXECUTABLE_EVENT_TYPES
            and decision is not None
            and str(decision.authoritative_status).strip().lower()
            not in _NON_EXECUTABLE_AUTHORITATIVE_STATUSES
        ):
            intent = _action_to_intent(
                decision_action,
                event_type=event_type,
                instruction_kind=(
                    "entry" if event_type in {"entry_signal", "add_entry"}
                    else "management"
                ),
            )
            projected_items.append(
                _item_projection(
                    sequence=1,
                    intent_kind=intent,
                    instruction_key=f"recognition_decision:{decision.id}",
                    authoritative_instruction_id=f"recognition_decision:{decision.id}",
                    evidence_references=(f"recognition_decision:{decision.id}",),
                )
            )

        if not projected_items:
            return None

        item_tuple = tuple(projected_items)
        intent_kinds = {item.intent_kind for item in item_tuple}
        contract_intent = (
            next(iter(intent_kinds)) if len(intent_kinds) == 1 else "manage"
        )
        expected_terminal = (
            item_tuple[0].expected_terminal_kind
            if len({item.expected_terminal_kind for item in item_tuple}) == 1
            else "verified_management"
        )
        base_time = _aware_utc(raw.posted_at or raw.created_at)
        deadline_seconds = min(_DEADLINE_SECONDS[item.intent_kind] for item in item_tuple)
        evidence = [f"raw_message:{raw_message_id}"]
        if decision is not None:
            evidence.append(f"recognition_decision:{decision.id}")
        if envelope is not None:
            evidence.append(f"management_envelope:{envelope.id}")
        return MessageOperationProjection(
            raw_message_id=raw_message_id,
            executable_intent=True,
            intent_kind=contract_intent,
            expected_terminal_kind=expected_terminal,
            deadline_at=base_time + timedelta(seconds=deadline_seconds),
            items=item_tuple,
            evidence_references=tuple(evidence),
            model_calls=0,
        )


def persist_message_operation_projection(
    session_factory: sessionmaker,
    projection: MessageOperationProjection,
    *,
    now: datetime | None = None,
) -> MessageOperationContract:
    """Persist one deterministic projection into the dormant additive ledger."""

    if not projection.items:
        raise MessageOperationContractBoundsError("projection must contain items")
    sequences = [item.sequence for item in projection.items]
    instruction_keys = [item.instruction_key for item in projection.items]
    if len(sequences) != len(set(sequences)):
        raise MessageOperationContractBoundsError(
            "projection item sequences must be unique"
        )
    if len(instruction_keys) != len(set(instruction_keys)):
        raise MessageOperationContractBoundsError("projection item keys must be unique")

    intent_kind = _closed_value("intent_kind", projection.intent_kind, INTENT_KINDS)
    expected_terminal_kind = _closed_value(
        "expected_terminal_kind", projection.expected_terminal_kind, TERMINAL_KINDS
    )
    if not isinstance(projection.deadline_at, datetime):
        raise MessageOperationContractBoundsError("deadline_at must be a datetime")
    timestamp = now or utc_now()
    contract_status = _projection_contract_status(projection.items)
    contract_values = {
        "raw_message_id": projection.raw_message_id,
        "intent_kind": intent_kind,
        "expected_terminal_kind": expected_terminal_kind,
        "status": contract_status,
        "deadline_at": projection.deadline_at,
        "evidence_refs_json": _evidence_refs_json(projection.evidence_references),
        "agent_requested": False,
        "policy_version": POLICY_VERSION,
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    with session_factory() as session:
        if session.get(RawMessage, projection.raw_message_id) is None:
            raise MessageOperationContractBoundsError(
                "raw_message_id does not identify an authoritative message"
            )
        statement = sqlite_insert(MessageOperationContract).values(**contract_values)
        session.execute(
            statement.on_conflict_do_nothing(
                index_elements=["raw_message_id", "policy_version"]
            )
        )
        contract = session.execute(
            select(MessageOperationContract).where(
                MessageOperationContract.raw_message_id == projection.raw_message_id,
                MessageOperationContract.policy_version == POLICY_VERSION,
            )
        ).scalar_one()
        contract_fields = (
            "intent_kind",
            "expected_terminal_kind",
            "status",
            "evidence_refs_json",
        )
        if (
            any(
                getattr(contract, field) != contract_values[field]
                for field in contract_fields
            )
            or contract.deadline_at != projection.deadline_at.replace(tzinfo=None)
        ):
            raise MessageOperationContractBoundsError(
                "existing contract conflicts with authoritative projection"
            )

        for item in projection.items:
            item_status = _projection_item_status(item.source_disposition)
            item_values = {
                "contract_id": contract.id,
                "sequence": item.sequence,
                "instruction_key": _bounded_text(
                    "instruction_key", item.instruction_key, maximum=128
                ),
                "instruction_kind": _closed_value(
                    "instruction_kind", item.intent_kind, INSTRUCTION_KINDS
                ),
                "authoritative_instruction_id": _bounded_text(
                    "authoritative_instruction_id",
                    item.authoritative_instruction_id,
                    maximum=255,
                ),
                "expected_descendant_kind": _closed_value(
                    "expected_descendant_kind",
                    item.expected_descendant_kind,
                    DESCENDANT_KINDS,
                ),
                "expected_terminal_kind": _closed_value(
                    "expected_terminal_kind",
                    item.expected_terminal_kind,
                    TERMINAL_KINDS,
                ),
                "status": item_status,
                "evidence_refs_json": _evidence_refs_json(
                    item.evidence_references
                ),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            item_statement = sqlite_insert(MessageOperationItem).values(**item_values)
            session.execute(
                item_statement.on_conflict_do_nothing(
                    index_elements=["contract_id", "instruction_key"]
                )
            )
            stored_item = session.execute(
                select(MessageOperationItem).where(
                    MessageOperationItem.contract_id == contract.id,
                    MessageOperationItem.instruction_key == item.instruction_key,
                )
            ).scalar_one()
            comparable = (
                "sequence",
                "instruction_kind",
                "authoritative_instruction_id",
                "expected_descendant_kind",
                "expected_terminal_kind",
                "status",
                "evidence_refs_json",
            )
            if any(
                getattr(stored_item, field) != item_values[field]
                for field in comparable
            ):
                raise MessageOperationContractBoundsError(
                    "existing item conflicts with authoritative projection"
                )
        session.commit()
        return _detach(session, contract)


def run_message_operation_shadow_once(
    session_factory: sessionmaker,
    *,
    after_raw_message_id: int,
    limit: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Project a bounded future-only batch without incidents or notifications."""

    if not 0 <= after_raw_message_id <= 2**63 - 1:
        raise MessageOperationContractBoundsError(
            "after_raw_message_id must be a non-negative SQLite identifier"
        )
    if not 1 <= limit <= 100:
        raise MessageOperationContractBoundsError("limit must be between 1 and 100")
    with session_factory() as session:
        rows = session.execute(
            select(
                RawMessage.id,
                RecognitionDecision.comparison_status,
                MessageOperationContract.id,
            )
            .outerjoin(
                RecognitionDecision,
                RecognitionDecision.raw_message_id == RawMessage.id,
            )
            .outerjoin(
                MessageOperationContract,
                (MessageOperationContract.raw_message_id == RawMessage.id)
                & (MessageOperationContract.policy_version == POLICY_VERSION),
            )
            .where(
                RawMessage.id > after_raw_message_id,
            )
            .order_by(RawMessage.id)
            .limit(limit)
        ).all()

    result = {
        "contracts_created": 0,
        "errors": 0,
        "existing_skipped": 0,
        "last_scanned_raw_message_id": after_raw_message_id,
        "messages_scanned": 0,
        "model_calls": 0,
        "ordinary_skipped": 0,
        "pending_blocked": 0,
    }
    for raw_message_id, comparison_status, contract_id in rows:
        result["messages_scanned"] += 1
        if comparison_status not in {"completed", "failed"}:
            result["pending_blocked"] = 1
            break
        if contract_id is not None:
            result["existing_skipped"] += 1
            result["last_scanned_raw_message_id"] = raw_message_id
            continue
        try:
            projection = project_message_operation_contract(
                session_factory, raw_message_id=raw_message_id
            )
            if projection is None:
                result["ordinary_skipped"] += 1
                result["last_scanned_raw_message_id"] = raw_message_id
                continue
            persist_message_operation_projection(
                session_factory, projection, now=now
            )
            result["contracts_created"] += 1
            result["model_calls"] += projection.model_calls
            result["last_scanned_raw_message_id"] = raw_message_id
        except Exception:
            result["errors"] += 1
            break
    return result


def _bounded_decision_payload(decision: RecognitionDecision | None) -> dict:
    if decision is None:
        return {}
    return _bounded_json_object(decision.authoritative_payload_json)


def _bounded_json_object(value: str | None) -> dict:
    if not isinstance(value, str) or len(value) > 65536:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source_disposition(item: MessageInstructionItem | None) -> str:
    if item is None:
        return "active"
    if item.retired_at is not None:
        return "superseded"
    if str(item.status).strip().lower() == "duplicate":
        return "duplicate"
    return "active"


def _recognition_item_projection(
    *,
    decision: RecognitionDecision | None,
    raw_message_id: int,
    context_attempt_id: int,
) -> MessageOperationItemProjection:
    decision_id = decision.id if decision is not None else raw_message_id
    return MessageOperationItemProjection(
        sequence=1,
        intent_kind="unresolved_executable",
        instruction_key=f"recognition_decision:{decision_id}",
        authoritative_instruction_id=f"recognition_decision:{decision_id}",
        expected_descendant_kind="context_resolution_attempt",
        expected_terminal_kind="verified_management",
        evidence_references=(
            f"recognition_decision:{decision_id}",
            f"context_resolution_attempt:{context_attempt_id}",
        ),
        target_lifecycle_id=None,
    )


def _projection_item_status(source_disposition: str) -> str:
    if source_disposition == "active":
        return "observing"
    if source_disposition in {"duplicate", "superseded"}:
        return source_disposition
    raise MessageOperationContractBoundsError("unsupported source disposition")


def _projection_contract_status(
    items: Sequence[MessageOperationItemProjection],
) -> str:
    statuses = {_projection_item_status(item.source_disposition) for item in items}
    if "observing" in statuses:
        return "observing"
    if "superseded" in statuses:
        return "superseded"
    return "duplicate"


def _item_projection(
    *,
    sequence: int,
    intent_kind: str,
    instruction_key: str,
    authoritative_instruction_id: str,
    evidence_references: tuple[str, ...],
    target_lifecycle_id: int | None = None,
    source_disposition: str = "active",
) -> MessageOperationItemProjection:
    descendant, terminal = _expectation_for_intent(intent_kind)
    return MessageOperationItemProjection(
        sequence=sequence,
        intent_kind=intent_kind,
        instruction_key=instruction_key,
        authoritative_instruction_id=authoritative_instruction_id,
        expected_descendant_kind=descendant,
        expected_terminal_kind=terminal,
        evidence_references=evidence_references,
        target_lifecycle_id=target_lifecycle_id,
        source_disposition=source_disposition,
    )


def _expectation_for_intent(intent_kind: str) -> tuple[str, str]:
    return {
        "new_entry": ("execution_binding", "verified_entry"),
        "add_entry": ("execution_binding", "verified_entry"),
        "take_profit": ("position_mutation_intent", "verified_execution"),
        "stop_loss": ("protection_revision", "verified_protection"),
        "cancel": ("position_mutation_intent", "verified_cancel"),
        "exit": ("position_mutation_intent", "verified_exit"),
        "manage": ("management_item", "verified_management"),
        "unresolved_executable": (
            "context_resolution_attempt",
            "verified_management",
        ),
        "other_management": ("management_item", "verified_management"),
    }[intent_kind]


def _action_to_intent(
    action: str | None,
    *,
    event_type: str | None,
    instruction_kind: str | None,
) -> str:
    normalized_action = str(action or "").strip().lower()
    normalized_event = str(event_type or "").strip().lower()
    if normalized_action in _TAKE_PROFIT_ACTIONS:
        return "take_profit"
    if normalized_action in _STOP_LOSS_ACTIONS:
        return "stop_loss"
    if normalized_action in _CANCEL_ACTIONS or normalized_event == "cancel_entry":
        return "cancel"
    if normalized_action in _EXIT_ACTIONS or normalized_event == "exit_position":
        return "exit"
    if normalized_action in _ADD_ENTRY_ACTIONS or normalized_event == "add_entry":
        return "add_entry"
    if normalized_event == "entry_signal" or instruction_kind == "entry":
        return "new_entry"
    return "other_management"


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
    instruction_kind = _closed_value(
        "instruction_kind", instruction_kind, INSTRUCTION_KINDS
    )
    authoritative_instruction_id = _bounded_text(
        "authoritative_instruction_id", authoritative_instruction_id, maximum=255
    )
    expected_descendant_kind = _closed_value(
        "expected_descendant_kind", expected_descendant_kind, DESCENDANT_KINDS
    )
    expected_terminal_kind = _closed_value(
        "expected_terminal_kind", expected_terminal_kind, TERMINAL_KINDS
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
        if session.get(MessageOperationContract, contract_id) is None:
            raise MessageOperationContractBoundsError(
                "contract_id does not identify a message operation contract"
            )
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
