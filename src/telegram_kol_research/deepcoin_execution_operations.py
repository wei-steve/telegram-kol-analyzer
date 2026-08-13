"""Transactional repositories for durable Deepcoin execution evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    OutcomeCertainty,
    RequestPriority,
)
from telegram_kol_research.models import (
    DeepcoinAccountWriteGeneration,
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
    DeepcoinSnapshotEvidence,
    ExecutionBinding,
    ExecutionOrderLeg,
    TradeSignal,
)


_MAX_EVIDENCE_BYTES = 4096
_MAX_EVIDENCE_DEPTH = 8
_MAX_EVIDENCE_NODES = 256
_MAX_EVIDENCE_KEY_LENGTH = 128
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,127}$")
_HEX_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "authorization",
        "dc_access_key",
        "dc_access_passphrase",
        "dc_access_sign",
        "passphrase",
        "private_key",
        "raw_request",
        "raw_response",
        "request_body",
        "response_body",
        "secret",
        "token",
    }
)
_SECRET_COMPACT_KEYS = frozenset(key.replace("_", "") for key in _SECRET_KEYS)
_SECRET_TEXT_MARKERS = (
    "authorization: bearer",
    "api_key",
    "api_secret",
    "dc-access-key",
    "dc-access-passphrase",
    "dc-access-sign",
    "passphrase",
    "private_key",
    "secret=",
)
_PHASES = frozenset(
    {
        "entry_preflight",
        "entry_submit",
        "entry_readback",
        "protection_submit",
        "protection_readback",
        "next_leg_preflight",
        "reconciliation",
        "completed",
    }
)
_STATES = frozenset(
    {
        "planned",
        "entry_prepared",
        "entry_submitting",
        "entry_pending_readback",
        "entry_unknown",
        "entry_rejected",
        "entry_confirmed",
        "protection_prepared",
        "protection_pending_readback",
        "protection_unknown",
        "protected",
        "next_leg_preflight",
        "pre_submit_deferred",
        "completed",
        "recovery_required",
        "submission_failed_no_exposure",
    }
)
_SNAPSHOT_KINDS = frozenset(
    {
        "positions",
        "position_history",
        "open_orders",
        "order_history",
        "trade_fills",
        "trigger_orders_pending",
        "trigger_orders_history",
        "account_composite",
        "protection_pending",
        "market_ticker",
        "market_instruments",
    }
)


class DeepcoinOperationConflict(RuntimeError):
    """A bounded durable identity or compare-and-swap conflict."""


class DeepcoinEvidenceValidationError(ValueError):
    """Evidence is unsafe, malformed, or too large to persist."""


@dataclass(frozen=True, slots=True)
class ExecutionOperationRecord:
    id: int
    operation_key: str
    trade_signal_id: int
    parent_operation_id: int | None
    execution_binding_id: int | None
    execution_order_leg_id: int | None
    contract_version: str
    phase: str
    state: str
    outcome_certainty: str
    error_category: str | None
    reason_code: str | None
    request_fingerprint: str
    economics_fingerprint: str
    deadline_at: datetime
    writer_attempted_at: datetime | None
    completed_at: datetime | None
    attempt_count: int
    state_version: int
    evidence_json: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RequestAttemptRecord:
    id: int
    operation_id: int
    ordinal: int
    method: str
    normalized_path: str
    priority: str
    phase: str
    outcome_certainty: str
    error_category: str | None
    safe_code: str
    http_status: int | None
    business_code: str | None
    governor_wait_ms: int
    retry_delay_ms: int
    latency_ms: int
    uid_scope_hash: str
    request_fingerprint: str
    correlation_id_hash: str | None
    started_at: datetime
    completed_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceRecord:
    id: int
    operation_id: int
    ordinal: int
    snapshot_kind: str
    available: bool
    schema_valid: bool
    complete: bool
    row_count: int
    page_count: int
    collection_fingerprint: str | None
    start_write_generation: int
    end_write_generation: int
    capture_started_at: datetime
    capture_ended_at: datetime
    evidence_json: str
    error_category: str | None
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountWriteGenerationRecord:
    id: int
    uid_scope_hash: str
    generation: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationBundle:
    operation: ExecutionOperationRecord
    attempts: tuple[RequestAttemptRecord, ...]
    snapshots: tuple[SnapshotEvidenceRecord, ...]


def reserve_execution_operation(
    session_factory: sessionmaker,
    *,
    operation_key: str,
    trade_signal_id: int,
    contract_version: str,
    phase: str,
    state: str,
    outcome_certainty: str | OutcomeCertainty,
    request_fingerprint: str,
    economics_fingerprint: str,
    deadline_at: datetime,
    evidence: Mapping[str, Any],
    parent_operation_id: int | None = None,
    execution_binding_id: int | None = None,
    execution_order_leg_id: int | None = None,
    error_category: str | ErrorCategory | None = None,
    reason_code: str | None = None,
    writer_attempted_at: datetime | None = None,
    completed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> ExecutionOperationRecord:
    """Reserve one operation key or return its identical durable owner."""

    normalized = _validated_operation_reservation(
        operation_key=operation_key,
        trade_signal_id=trade_signal_id,
        parent_operation_id=parent_operation_id,
        execution_binding_id=execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        contract_version=contract_version,
        phase=phase,
        state=state,
        outcome_certainty=outcome_certainty,
        error_category=error_category,
        reason_code=reason_code,
        request_fingerprint=request_fingerprint,
        economics_fingerprint=economics_fingerprint,
        deadline_at=deadline_at,
        writer_attempted_at=writer_attempted_at,
        completed_at=completed_at,
        evidence=evidence,
        created_at=created_at,
    )
    with session_factory() as session:
        _begin_immediate(session)
        if session.get(TradeSignal, normalized["trade_signal_id"]) is None:
            raise DeepcoinOperationConflict("trade_signal_not_found")
        if normalized["parent_operation_id"] is not None and session.get(
            DeepcoinExecutionOperation, normalized["parent_operation_id"]
        ) is None:
            raise DeepcoinOperationConflict("parent_operation_not_found")
        if normalized["execution_binding_id"] is not None and session.get(
            ExecutionBinding, normalized["execution_binding_id"]
        ) is None:
            raise DeepcoinOperationConflict("execution_binding_not_found")
        if normalized["execution_order_leg_id"] is not None and session.get(
            ExecutionOrderLeg, normalized["execution_order_leg_id"]
        ) is None:
            raise DeepcoinOperationConflict("execution_order_leg_not_found")
        existing = (
            session.query(DeepcoinExecutionOperation)
            .filter_by(operation_key=normalized["operation_key"])
            .one_or_none()
        )
        if existing is not None:
            if not _reservation_identity_matches(existing, normalized):
                raise DeepcoinOperationConflict("operation_identity_conflict")
            session.commit()
            return _operation_record(existing)
        canonical_evidence_json = normalized.pop("canonical_evidence_json")
        row = DeepcoinExecutionOperation(
            **normalized,
            attempt_count=0,
            state_version=0,
            evidence_json=canonical_evidence_json,
        )
        session.add(row)
        session.flush()
        record = _operation_record(row)
        session.commit()
        return record


def transition_execution_operation(
    session_factory: sessionmaker,
    *,
    operation_id: int,
    expected_operation_key: str,
    expected_state: str,
    expected_state_version: int,
    phase: str,
    state: str,
    outcome_certainty: str | OutcomeCertainty,
    evidence: Mapping[str, Any],
    error_category: str | ErrorCategory | None = None,
    reason_code: str | None = None,
    writer_attempted_at: datetime | None = None,
    completed_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ExecutionOperationRecord:
    """Apply an exact state/version CAS transition to an operation."""

    operation_pk = _positive_int(operation_id, code="operation_id_invalid")
    key = _bounded_text(expected_operation_key, 255, "operation_key_invalid")
    prior_state = _bounded_text(expected_state, 32, "expected_state_invalid")
    prior_version = _nonnegative_int(
        expected_state_version, code="expected_state_version_invalid"
    )
    next_phase = _closed_text(phase, _PHASES, "phase_invalid")
    next_state = _closed_text(state, _STATES, "state_invalid")
    certainty = _enum_text(outcome_certainty, OutcomeCertainty, "certainty_invalid")
    category = _optional_enum_text(error_category, ErrorCategory, "error_category_invalid")
    safe_reason = _optional_safe_code(reason_code, code="reason_code_invalid")
    canonical_evidence = _canonical_evidence_json(evidence)
    writer_at = _optional_datetime(writer_attempted_at, "writer_attempted_at_invalid")
    done_at = _optional_datetime(completed_at, "completed_at_invalid")
    changed_at = _normalize_datetime(updated_at or datetime.now(UTC), "updated_at_invalid")

    with session_factory() as session:
        _begin_immediate(session)
        row = session.get(DeepcoinExecutionOperation, operation_pk)
        if row is None or row.operation_key != key:
            raise DeepcoinOperationConflict("operation_identity_conflict")
        if row.state != prior_state or int(row.state_version) != prior_version:
            raise DeepcoinOperationConflict("operation_state_conflict")
        if writer_at is not None and row.writer_attempted_at is not None:
            if writer_at != _normalize_datetime(
                row.writer_attempted_at, "writer_attempted_at_invalid"
            ):
                raise DeepcoinOperationConflict("writer_boundary_conflict")
        if done_at is not None and row.completed_at is not None:
            if done_at != _normalize_datetime(row.completed_at, "completed_at_invalid"):
                raise DeepcoinOperationConflict("completion_boundary_conflict")
        if changed_at < _normalize_datetime(row.updated_at, "updated_at_invalid"):
            raise DeepcoinOperationConflict("operation_time_conflict")
        effective_writer_at = writer_at or row.writer_attempted_at
        effective_done_at = done_at or row.completed_at
        if (
            effective_writer_at is not None
            and effective_done_at is not None
            and _normalize_datetime(effective_done_at, "completed_at_invalid")
            < _normalize_datetime(effective_writer_at, "writer_attempted_at_invalid")
        ):
            raise DeepcoinOperationConflict("operation_time_conflict")
        row.phase = next_phase
        row.state = next_state
        row.outcome_certainty = certainty
        row.error_category = category
        row.reason_code = safe_reason
        row.writer_attempted_at = effective_writer_at
        row.completed_at = effective_done_at
        row.evidence_json = canonical_evidence
        row.state_version = prior_version + 1
        row.updated_at = changed_at
        session.flush()
        record = _operation_record(row)
        session.commit()
        return record


def record_request_attempt(
    session_factory: sessionmaker,
    *,
    operation_id: int,
    expected_operation_key: str,
    expected_request_fingerprint: str,
    uid_scope_hash: str,
    fact: RequestAttemptFact,
    started_at: datetime,
    completed_at: datetime,
) -> RequestAttemptRecord:
    """Append one redacted transport attempt using an atomic aggregate ordinal."""

    operation_pk = _positive_int(operation_id, code="operation_id_invalid")
    key = _bounded_text(expected_operation_key, 255, "operation_key_invalid")
    request_fp = _fingerprint(expected_request_fingerprint, "request_fingerprint_invalid")
    uid_hash = _fingerprint(uid_scope_hash, "uid_scope_hash_invalid")
    values = _validated_attempt_fact(fact, started_at=started_at, completed_at=completed_at)
    with session_factory() as session:
        _begin_immediate(session)
        operation = session.get(DeepcoinExecutionOperation, operation_pk)
        if (
            operation is None
            or operation.operation_key != key
            or operation.request_fingerprint != request_fp
        ):
            raise DeepcoinOperationConflict("operation_identity_conflict")
        ordinal = int(
            session.query(func.max(DeepcoinRequestAttempt.ordinal))
            .filter_by(deepcoin_execution_operation_id=operation_pk)
            .scalar()
            or 0
        ) + 1
        row = DeepcoinRequestAttempt(
            deepcoin_execution_operation_id=operation_pk,
            ordinal=ordinal,
            uid_scope_hash=uid_hash,
            request_fingerprint=request_fp,
            **values,
        )
        session.add(row)
        operation.attempt_count = int(operation.attempt_count) + 1
        session.flush()
        record = _attempt_record(row)
        session.commit()
        return record


def record_snapshot_evidence(
    session_factory: sessionmaker,
    *,
    operation_id: int,
    expected_operation_key: str,
    snapshot_kind: str,
    available: bool,
    schema_valid: bool,
    complete: bool,
    row_count: int,
    page_count: int,
    collection_fingerprint: str | None,
    start_write_generation: int,
    end_write_generation: int,
    capture_started_at: datetime,
    capture_ended_at: datetime,
    evidence: Mapping[str, Any],
    error_category: str | ErrorCategory | None = None,
    error_code: str | None = None,
) -> SnapshotEvidenceRecord:
    """Append one immutable, bounded snapshot completeness proof."""

    operation_pk = _positive_int(operation_id, code="operation_id_invalid")
    key = _bounded_text(expected_operation_key, 255, "operation_key_invalid")
    values = _validated_snapshot(
        snapshot_kind=snapshot_kind,
        available=available,
        schema_valid=schema_valid,
        complete=complete,
        row_count=row_count,
        page_count=page_count,
        collection_fingerprint=collection_fingerprint,
        start_write_generation=start_write_generation,
        end_write_generation=end_write_generation,
        capture_started_at=capture_started_at,
        capture_ended_at=capture_ended_at,
        evidence=evidence,
        error_category=error_category,
        error_code=error_code,
    )
    with session_factory() as session:
        _begin_immediate(session)
        operation = session.get(DeepcoinExecutionOperation, operation_pk)
        if operation is None or operation.operation_key != key:
            raise DeepcoinOperationConflict("operation_identity_conflict")
        ordinal = int(
            session.query(func.max(DeepcoinSnapshotEvidence.ordinal))
            .filter_by(deepcoin_execution_operation_id=operation_pk)
            .scalar()
            or 0
        ) + 1
        row = DeepcoinSnapshotEvidence(
            deepcoin_execution_operation_id=operation_pk,
            ordinal=ordinal,
            **values,
        )
        session.add(row)
        session.flush()
        record = _snapshot_record(row)
        session.commit()
        return record


def advance_account_write_generation(
    session_factory: sessionmaker,
    *,
    uid_scope_hash: str,
    updated_at: datetime | None = None,
) -> AccountWriteGenerationRecord:
    """Atomically advance one hashed account's local writer boundary."""

    uid_hash = _fingerprint(uid_scope_hash, "uid_scope_hash_invalid")
    changed_at = _normalize_datetime(updated_at or datetime.now(UTC), "updated_at_invalid")
    with session_factory() as session:
        _begin_immediate(session)
        row = (
            session.query(DeepcoinAccountWriteGeneration)
            .filter_by(uid_scope_hash=uid_hash)
            .one_or_none()
        )
        if row is None:
            row = DeepcoinAccountWriteGeneration(
                uid_scope_hash=uid_hash,
                generation=1,
                updated_at=changed_at,
            )
            session.add(row)
        else:
            row.generation = int(row.generation) + 1
            row.updated_at = changed_at
        session.flush()
        record = _generation_record(row)
        session.commit()
        return record


def load_account_write_generation(
    session_factory: sessionmaker,
    *,
    uid_scope_hash: str,
) -> AccountWriteGenerationRecord | None:
    """Return the detached current writer boundary without mutating it."""

    uid_hash = _fingerprint(uid_scope_hash, "uid_scope_hash_invalid")
    with session_factory() as session:
        row = (
            session.query(DeepcoinAccountWriteGeneration)
            .filter_by(uid_scope_hash=uid_hash)
            .one_or_none()
        )
        return None if row is None else _generation_record(row)


def load_operation_bundle(
    session_factory: sessionmaker,
    *,
    operation_id: int,
) -> OperationBundle:
    """Load one detached aggregate and its append-only evidence."""

    operation_pk = _positive_int(operation_id, code="operation_id_invalid")
    with session_factory() as session:
        operation = session.get(DeepcoinExecutionOperation, operation_pk)
        if operation is None:
            raise DeepcoinOperationConflict("operation_not_found")
        attempts = (
            session.query(DeepcoinRequestAttempt)
            .filter_by(deepcoin_execution_operation_id=operation_pk)
            .order_by(DeepcoinRequestAttempt.ordinal, DeepcoinRequestAttempt.id)
            .all()
        )
        snapshots = (
            session.query(DeepcoinSnapshotEvidence)
            .filter_by(deepcoin_execution_operation_id=operation_pk)
            .order_by(DeepcoinSnapshotEvidence.ordinal, DeepcoinSnapshotEvidence.id)
            .all()
        )
        return OperationBundle(
            operation=_operation_record(operation),
            attempts=tuple(_attempt_record(row) for row in attempts),
            snapshots=tuple(_snapshot_record(row) for row in snapshots),
        )


def _begin_immediate(session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _validated_operation_reservation(**values: Any) -> dict[str, Any]:
    created_at = _normalize_datetime(
        values["created_at"] or datetime.now(UTC), "created_at_invalid"
    )
    return {
        "operation_key": _bounded_text(values["operation_key"], 255, "operation_key_invalid"),
        "trade_signal_id": _positive_int(values["trade_signal_id"], code="trade_signal_id_invalid"),
        "parent_operation_id": _optional_positive_int(values["parent_operation_id"], "parent_operation_id_invalid"),
        "execution_binding_id": _optional_positive_int(values["execution_binding_id"], "execution_binding_id_invalid"),
        "execution_order_leg_id": _optional_positive_int(values["execution_order_leg_id"], "execution_order_leg_id_invalid"),
        "contract_version": _bounded_text(values["contract_version"], 64, "contract_version_invalid"),
        "phase": _closed_text(values["phase"], _PHASES, "phase_invalid"),
        "state": _closed_text(values["state"], _STATES, "state_invalid"),
        "outcome_certainty": _enum_text(values["outcome_certainty"], OutcomeCertainty, "certainty_invalid"),
        "error_category": _optional_enum_text(values["error_category"], ErrorCategory, "error_category_invalid"),
        "reason_code": _optional_safe_code(values["reason_code"], code="reason_code_invalid"),
        "request_fingerprint": _fingerprint(values["request_fingerprint"], "request_fingerprint_invalid"),
        "economics_fingerprint": _fingerprint(values["economics_fingerprint"], "economics_fingerprint_invalid"),
        "deadline_at": _normalize_datetime(values["deadline_at"], "deadline_at_invalid"),
        "writer_attempted_at": _optional_datetime(values["writer_attempted_at"], "writer_attempted_at_invalid"),
        "completed_at": _optional_datetime(values["completed_at"], "completed_at_invalid"),
        "canonical_evidence_json": _canonical_evidence_json(values["evidence"]),
        "created_at": created_at,
        "updated_at": created_at,
    }


def _reservation_identity_matches(
    row: DeepcoinExecutionOperation, values: Mapping[str, Any]
) -> bool:
    return all(
        (
            int(row.trade_signal_id) == values["trade_signal_id"],
            row.parent_operation_id == values["parent_operation_id"],
            row.execution_binding_id == values["execution_binding_id"],
            row.execution_order_leg_id == values["execution_order_leg_id"],
            row.contract_version == values["contract_version"],
            row.request_fingerprint == values["request_fingerprint"],
            row.economics_fingerprint == values["economics_fingerprint"],
            _normalize_datetime(row.deadline_at, "deadline_at_invalid")
            == values["deadline_at"],
        )
    )


def _validated_attempt_fact(
    fact: RequestAttemptFact,
    *,
    started_at: datetime,
    completed_at: datetime,
) -> dict[str, Any]:
    if not isinstance(fact, RequestAttemptFact):
        raise DeepcoinEvidenceValidationError("attempt_fact_invalid")
    method = _bounded_text(fact.method, 8, "attempt_method_invalid").upper()
    if method not in {"GET", "POST"}:
        raise DeepcoinEvidenceValidationError("attempt_method_invalid")
    path = _bounded_text(fact.normalized_path, 512, "attempt_path_invalid")
    if not path.startswith("/"):
        raise DeepcoinEvidenceValidationError("attempt_path_invalid")
    started = _normalize_datetime(started_at, "attempt_started_at_invalid")
    completed = _normalize_datetime(completed_at, "attempt_completed_at_invalid")
    if completed < started:
        raise DeepcoinEvidenceValidationError("attempt_time_order_invalid")
    status = None if fact.http_status is None else _nonnegative_int(
        fact.http_status, code="attempt_http_status_invalid"
    )
    if status is not None and not 100 <= status <= 599:
        raise DeepcoinEvidenceValidationError("attempt_http_status_invalid")
    business_code = _optional_safe_code(fact.business_code, code="business_code_invalid", max_length=64)
    correlation_hash = None
    if fact.correlation_id is not None:
        correlation = _bounded_text(fact.correlation_id, 255, "correlation_id_invalid")
        _reject_secret_text(correlation)
        correlation_hash = hashlib.sha256(correlation.encode("utf-8")).hexdigest()
    return {
        "method": method,
        "normalized_path": path,
        "priority": _enum_text(fact.priority, RequestPriority, "attempt_priority_invalid"),
        "phase": _bounded_text(fact.phase, 32, "attempt_phase_invalid"),
        "outcome_certainty": _enum_text(fact.outcome_certainty, OutcomeCertainty, "attempt_certainty_invalid"),
        "error_category": _optional_enum_text(fact.error_category, ErrorCategory, "attempt_error_category_invalid"),
        "safe_code": _safe_code(fact.safe_code, "attempt_safe_code_invalid"),
        "http_status": status,
        "business_code": business_code,
        "governor_wait_ms": _nonnegative_int(fact.governor_wait_ms, code="governor_wait_invalid"),
        "retry_delay_ms": _nonnegative_int(fact.retry_delay_ms, code="retry_delay_invalid"),
        "latency_ms": _nonnegative_int(fact.latency_ms, code="latency_invalid"),
        "correlation_id_hash": correlation_hash,
        "started_at": started,
        "completed_at": completed,
    }


def _validated_snapshot(**values: Any) -> dict[str, Any]:
    available = _strict_bool(values["available"], "snapshot_available_invalid")
    schema_valid = _strict_bool(values["schema_valid"], "snapshot_schema_valid_invalid")
    complete = _strict_bool(values["complete"], "snapshot_complete_invalid")
    start_generation = _nonnegative_int(values["start_write_generation"], code="snapshot_generation_invalid")
    end_generation = _nonnegative_int(values["end_write_generation"], code="snapshot_generation_invalid")
    fingerprint = values["collection_fingerprint"]
    collection_fp = None if fingerprint is None else _fingerprint(fingerprint, "snapshot_fingerprint_invalid")
    if schema_valid and not available:
        raise DeepcoinEvidenceValidationError("snapshot_schema_availability_invalid")
    if complete and (
        not available
        or not schema_valid
        or start_generation != end_generation
        or collection_fp is None
    ):
        raise DeepcoinEvidenceValidationError("snapshot_completeness_invalid")
    started = _normalize_datetime(values["capture_started_at"], "snapshot_started_at_invalid")
    ended = _normalize_datetime(values["capture_ended_at"], "snapshot_ended_at_invalid")
    if ended < started:
        raise DeepcoinEvidenceValidationError("snapshot_time_order_invalid")
    return {
        "snapshot_kind": _closed_text(
            values["snapshot_kind"], _SNAPSHOT_KINDS, "snapshot_kind_invalid"
        ),
        "available": available,
        "schema_valid": schema_valid,
        "complete": complete,
        "row_count": _nonnegative_int(values["row_count"], code="snapshot_row_count_invalid"),
        "page_count": _nonnegative_int(values["page_count"], code="snapshot_page_count_invalid"),
        "collection_fingerprint": collection_fp,
        "start_write_generation": start_generation,
        "end_write_generation": end_generation,
        "capture_started_at": started,
        "capture_ended_at": ended,
        "evidence_json": _canonical_evidence_json(values["evidence"]),
        "error_category": _optional_enum_text(values["error_category"], ErrorCategory, "snapshot_error_category_invalid"),
        "error_code": _optional_safe_code(values["error_code"], code="snapshot_error_code_invalid"),
    }


def _canonical_evidence_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise DeepcoinEvidenceValidationError("evidence_mapping_required")
    node_count = 0

    def inspect(item: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_EVIDENCE_NODES or depth > _MAX_EVIDENCE_DEPTH:
            raise DeepcoinEvidenceValidationError("evidence_complexity_exceeded")
        if item is None or isinstance(item, bool) or isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise DeepcoinEvidenceValidationError("evidence_number_invalid")
            return item
        if isinstance(item, str):
            _reject_secret_text(item)
            return item
        if isinstance(item, Mapping):
            normalized: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > _MAX_EVIDENCE_KEY_LENGTH:
                    raise DeepcoinEvidenceValidationError("evidence_key_invalid")
                normalized_key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
                if (
                    normalized_key in _SECRET_KEYS
                    or normalized_key.replace("_", "") in _SECRET_COMPACT_KEYS
                ):
                    raise DeepcoinEvidenceValidationError("evidence_secret_forbidden")
                normalized[key] = inspect(child, depth + 1)
            return normalized
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return [inspect(child, depth + 1) for child in item]
        raise DeepcoinEvidenceValidationError("evidence_type_invalid")

    normalized = inspect(value, 0)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise DeepcoinEvidenceValidationError("evidence_json_invalid") from None
    if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
        raise DeepcoinEvidenceValidationError("evidence_size_exceeded")
    return encoded


def _reject_secret_text(value: str) -> None:
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_TEXT_MARKERS):
        raise DeepcoinEvidenceValidationError("evidence_secret_forbidden")


def _fingerprint(value: Any, code: str) -> str:
    if not isinstance(value, str) or _HEX_FINGERPRINT.fullmatch(value) is None:
        raise DeepcoinEvidenceValidationError(code)
    return value


def _safe_code(value: Any, code: str, *, max_length: int = 128) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _SAFE_CODE.fullmatch(value) is None
    ):
        raise DeepcoinEvidenceValidationError(code)
    return value


def _optional_safe_code(
    value: Any, *, code: str, max_length: int = 128
) -> str | None:
    if value is None:
        return None
    return _safe_code(value, code, max_length=max_length)


def _bounded_text(value: Any, max_length: int, code: str) -> str:
    if not isinstance(value, str):
        raise DeepcoinEvidenceValidationError(code)
    result = value.strip()
    if not result or len(result) > max_length:
        raise DeepcoinEvidenceValidationError(code)
    _reject_secret_text(result)
    return result


def _closed_text(value: Any, allowed: frozenset[str], code: str) -> str:
    result = _bounded_text(value, max(len(item) for item in allowed), code)
    if result not in allowed:
        raise DeepcoinEvidenceValidationError(code)
    return result


def _enum_text(value: Any, enum_type, code: str) -> str:
    try:
        return enum_type(value).value
    except (TypeError, ValueError):
        raise DeepcoinEvidenceValidationError(code) from None


def _optional_enum_text(value: Any, enum_type, code: str) -> str | None:
    if value is None:
        return None
    return _enum_text(value, enum_type, code)


def _positive_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeepcoinEvidenceValidationError(code)
    return value


def _optional_positive_int(value: Any, code: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, code=code)


def _nonnegative_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeepcoinEvidenceValidationError(code)
    return value


def _strict_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise DeepcoinEvidenceValidationError(code)
    return value


def _normalize_datetime(value: Any, code: str) -> datetime:
    if not isinstance(value, datetime):
        raise DeepcoinEvidenceValidationError(code)
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _optional_datetime(value: Any, code: str) -> datetime | None:
    if value is None:
        return None
    return _normalize_datetime(value, code)


def _operation_record(row: DeepcoinExecutionOperation) -> ExecutionOperationRecord:
    return ExecutionOperationRecord(
        id=int(row.id),
        operation_key=str(row.operation_key),
        trade_signal_id=int(row.trade_signal_id),
        parent_operation_id=row.parent_operation_id,
        execution_binding_id=row.execution_binding_id,
        execution_order_leg_id=row.execution_order_leg_id,
        contract_version=str(row.contract_version),
        phase=str(row.phase),
        state=str(row.state),
        outcome_certainty=str(row.outcome_certainty),
        error_category=row.error_category,
        reason_code=row.reason_code,
        request_fingerprint=str(row.request_fingerprint),
        economics_fingerprint=str(row.economics_fingerprint),
        deadline_at=row.deadline_at,
        writer_attempted_at=row.writer_attempted_at,
        completed_at=row.completed_at,
        attempt_count=int(row.attempt_count),
        state_version=int(row.state_version),
        evidence_json=str(row.evidence_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attempt_record(row: DeepcoinRequestAttempt) -> RequestAttemptRecord:
    return RequestAttemptRecord(
        id=int(row.id),
        operation_id=int(row.deepcoin_execution_operation_id),
        ordinal=int(row.ordinal),
        method=str(row.method),
        normalized_path=str(row.normalized_path),
        priority=str(row.priority),
        phase=str(row.phase),
        outcome_certainty=str(row.outcome_certainty),
        error_category=row.error_category,
        safe_code=str(row.safe_code),
        http_status=row.http_status,
        business_code=row.business_code,
        governor_wait_ms=int(row.governor_wait_ms),
        retry_delay_ms=int(row.retry_delay_ms),
        latency_ms=int(row.latency_ms),
        uid_scope_hash=str(row.uid_scope_hash),
        request_fingerprint=str(row.request_fingerprint),
        correlation_id_hash=row.correlation_id_hash,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _snapshot_record(row: DeepcoinSnapshotEvidence) -> SnapshotEvidenceRecord:
    return SnapshotEvidenceRecord(
        id=int(row.id),
        operation_id=int(row.deepcoin_execution_operation_id),
        ordinal=int(row.ordinal),
        snapshot_kind=str(row.snapshot_kind),
        available=bool(row.available),
        schema_valid=bool(row.schema_valid),
        complete=bool(row.complete),
        row_count=int(row.row_count),
        page_count=int(row.page_count),
        collection_fingerprint=row.collection_fingerprint,
        start_write_generation=int(row.start_write_generation),
        end_write_generation=int(row.end_write_generation),
        capture_started_at=row.capture_started_at,
        capture_ended_at=row.capture_ended_at,
        evidence_json=str(row.evidence_json),
        error_category=row.error_category,
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _generation_record(
    row: DeepcoinAccountWriteGeneration,
) -> AccountWriteGenerationRecord:
    return AccountWriteGenerationRecord(
        id=int(row.id),
        uid_scope_hash=str(row.uid_scope_hash),
        generation=int(row.generation),
        updated_at=row.updated_at,
    )
