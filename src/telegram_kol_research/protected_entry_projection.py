"""Closed compatibility projection for durable protected-entry operations."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol


ACTIVE_PROTECTION_PENDING = "active_protection_pending"
ACTIVE_PROTECTED_DEFERRED = "active_protected_deferred"
RECOVERY_REQUIRED = "recovery_required"
SUBMISSION_FAILED_NO_EXPOSURE = "submission_failed_no_exposure"
SUBMITTED = "submitted"

PROTECTED_ENTRY_PROJECTIONS = frozenset(
    {
        ACTIVE_PROTECTION_PENDING,
        ACTIVE_PROTECTED_DEFERRED,
        RECOVERY_REQUIRED,
        SUBMISSION_FAILED_NO_EXPOSURE,
        SUBMITTED,
    }
)

_LIVE_PROTECTION_PENDING_STATES = frozenset(
    {
        "entry_confirmed",
        "protection_prepared",
        "protection_pending_readback",
        "protection_unknown",
    }
)
_ENTRY_UNRESOLVED_STATES = frozenset(
    {
        "planned",
        "entry_prepared",
        "entry_submitting",
        "entry_pending_readback",
        "entry_unknown",
        "entry_rejected",
        "next_leg_preflight",
        "recovery_required",
    }
)
_SUCCESS_CHILD_STATES = frozenset({"protected", "completed"})
_ZERO_EXPOSURE_SNAPSHOT_KINDS = frozenset(
    {"positions", "open_orders", "trigger_orders_pending"}
)
_EMPTY_COLLECTION_FINGERPRINT = hashlib.sha256(b"[]").hexdigest()


class _Operation(Protocol):
    id: int
    operation_key: str
    trade_signal_id: int
    parent_operation_id: int | None
    contract_version: str
    phase: str
    state: str
    outcome_certainty: str
    reason_code: str | None
    request_fingerprint: str
    execution_binding_id: int | None
    execution_order_leg_id: int | None
    writer_attempted_at: datetime | None
    completed_at: datetime | None
    evidence_json: str


class _Snapshot(Protocol):
    deepcoin_execution_operation_id: int
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
    error_category: str | None
    error_code: str | None


class _Attempt(Protocol):
    deepcoin_execution_operation_id: int
    method: str
    phase: str
    outcome_certainty: str
    error_category: str | None
    request_fingerprint: str
    uid_scope_hash: str
    started_at: datetime
    completed_at: datetime


def project_protected_entry_operation(
    *,
    parent: _Operation,
    children: Sequence[_Operation],
    attempts: Sequence[_Attempt],
    snapshots: Sequence[_Snapshot],
    current_write_generation: int | None,
    verified_child_operation_ids: frozenset[int],
) -> str:
    """Project one locked v1 aggregate without interpreting display text."""

    try:
        return _project_protected_entry_operation(
            parent=parent,
            children=children,
            attempts=attempts,
            snapshots=snapshots,
            current_write_generation=current_write_generation,
            verified_child_operation_ids=verified_child_operation_ids,
        )
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        return RECOVERY_REQUIRED


def _project_protected_entry_operation(
    *,
    parent: _Operation,
    children: Sequence[_Operation],
    attempts: Sequence[_Attempt],
    snapshots: Sequence[_Snapshot],
    current_write_generation: int | None,
    verified_child_operation_ids: frozenset[int],
) -> str:
    """Internal projection with a bounded public fail-closed boundary."""

    if (
        str(parent.contract_version) != "1"
        or parent.parent_operation_id is not None
        or any(
            str(child.contract_version) != "1"
            or int(child.parent_operation_id or 0) != int(parent.id)
            or int(child.trade_signal_id) != int(parent.trade_signal_id)
            for child in children
        )
    ):
        return RECOVERY_REQUIRED

    if not _operation_keys_valid(parent, children):
        return RECOVERY_REQUIRED

    child_states = tuple(str(child.state) for child in children)
    if str(parent.state) == SUBMISSION_FAILED_NO_EXPOSURE:
        if not children and _has_complete_zero_exposure_proof(
            parent,
            attempts,
            snapshots,
            current_write_generation=current_write_generation,
        ):
            return SUBMISSION_FAILED_NO_EXPOSURE
        return RECOVERY_REQUIRED

    if str(parent.state) == "protected":
        if verified_child_operation_ids != frozenset(
            int(child.id) for child in children
        ):
            return RECOVERY_REQUIRED
        if not _protected_parent_evidence_valid(parent, children):
            return RECOVERY_REQUIRED
        if any(state == "pre_submit_deferred" for state in child_states):
            if all(
                state in _SUCCESS_CHILD_STATES
                or state == "pre_submit_deferred"
                for state in child_states
            ):
                return ACTIVE_PROTECTED_DEFERRED
            return RECOVERY_REQUIRED
        if all(state in _SUCCESS_CHILD_STATES for state in child_states):
            return SUBMITTED
        return RECOVERY_REQUIRED

    if (
        str(parent.state) in _LIVE_PROTECTION_PENDING_STATES
        and parent.writer_attempted_at is not None
        and str(parent.outcome_certainty) in {"accepted", "unknown", "confirmed"}
    ):
        return ACTIVE_PROTECTION_PENDING
    if str(parent.state) in _ENTRY_UNRESOLVED_STATES:
        return RECOVERY_REQUIRED
    return RECOVERY_REQUIRED


def _has_complete_zero_exposure_proof(
    parent: _Operation,
    attempts: Sequence[_Attempt],
    snapshots: Sequence[_Snapshot],
    *,
    current_write_generation: int | None,
) -> bool:
    writer_at = parent.writer_attempted_at
    if (
        writer_at is None
        or str(parent.outcome_certainty) != "confirmed"
        or str(parent.reason_code)
        != "submission_failed_no_exposure_confirmed"
        or parent.completed_at is None
        or type(current_write_generation) is not int
        or current_write_generation < 0
    ):
        return False
    normalized_writer_at = _utc_naive(writer_at)
    normalized_completed_at = _utc_naive(parent.completed_at)
    if normalized_completed_at < normalized_writer_at:
        return False
    writer_attempts = [
        attempt
        for attempt in attempts
        if int(attempt.deepcoin_execution_operation_id) == int(parent.id)
        and str(attempt.method) == "POST"
    ]
    if (
        len(writer_attempts) != 1
        or str(writer_attempts[0].phase) != "entry_submit"
        or str(writer_attempts[0].outcome_certainty) != "rejected"
        or str(writer_attempts[0].error_category) != "business_rejected"
        or str(writer_attempts[0].request_fingerprint)
        != str(parent.request_fingerprint)
        or not _fingerprint(_projection_uid_scope_hash(parent))
        or not isinstance(writer_attempts[0].uid_scope_hash, str)
        or len(writer_attempts[0].uid_scope_hash) != 64
        or str(writer_attempts[0].uid_scope_hash)
        != str(_projection_uid_scope_hash(parent))
        or _utc_naive(writer_attempts[0].started_at) < normalized_writer_at
        or _utc_naive(writer_attempts[0].completed_at)
        < _utc_naive(writer_attempts[0].started_at)
        or _utc_naive(writer_attempts[0].completed_at)
        > normalized_completed_at
    ):
        return False
    latest_by_kind: dict[str, _Snapshot] = {}
    for snapshot in snapshots:
        if int(snapshot.deepcoin_execution_operation_id) != int(parent.id):
            continue
        kind = str(snapshot.snapshot_kind)
        if kind not in _ZERO_EXPOSURE_SNAPSHOT_KINDS:
            continue
        current = latest_by_kind.get(kind)
        if current is None or int(snapshot.ordinal) > int(current.ordinal):
            latest_by_kind[kind] = snapshot
    if set(latest_by_kind) != set(_ZERO_EXPOSURE_SNAPSHOT_KINDS):
        return False
    generations = {
        (
            int(snapshot.start_write_generation),
            int(snapshot.end_write_generation),
        )
        for snapshot in latest_by_kind.values()
    }
    capture_windows = {
        (
            _utc_naive(snapshot.capture_started_at),
            _utc_naive(snapshot.capture_ended_at),
        )
        for snapshot in latest_by_kind.values()
    }
    if (
        generations != {(current_write_generation, current_write_generation)}
        or len(capture_windows) != 1
    ):
        return False
    return all(
        _snapshot_proves_empty(
            snapshot,
            writer_at=normalized_writer_at,
            rejection_at=_utc_naive(writer_attempts[0].completed_at),
            completed_at=normalized_completed_at,
        )
        for snapshot in latest_by_kind.values()
    )


def _snapshot_proves_empty(
    snapshot: _Snapshot,
    *,
    writer_at: datetime,
    rejection_at: datetime,
    completed_at: datetime,
) -> bool:
    fingerprint = snapshot.collection_fingerprint
    return (
        snapshot.available is True
        and snapshot.schema_valid is True
        and snapshot.complete is True
        and int(snapshot.row_count) == 0
        and int(snapshot.page_count) >= 1
        and isinstance(fingerprint, str)
        and fingerprint == _EMPTY_COLLECTION_FINGERPRINT
        and snapshot.error_category is None
        and snapshot.error_code is None
        and int(snapshot.start_write_generation)
        == int(snapshot.end_write_generation)
        and int(snapshot.end_write_generation) % 2 == 0
        and _utc_naive(snapshot.capture_started_at) >= writer_at
        and _utc_naive(snapshot.capture_started_at) >= rejection_at
        and _utc_naive(snapshot.capture_ended_at)
        >= _utc_naive(snapshot.capture_started_at)
        and _utc_naive(snapshot.capture_ended_at) <= completed_at
    )


def _protected_parent_evidence_valid(
    parent: _Operation,
    children: Sequence[_Operation],
) -> bool:
    evidence = _canonical_object(parent.evidence_json)
    if evidence is None:
        return False
    required = evidence.get("required_protection_count")
    confirmed = evidence.get("confirmed_protection_count")
    expected_entry_indices = evidence.get("expected_entry_leg_indices")
    parent_leg_index = evidence.get("leg_index")
    uid_scope_hash = evidence.get("uid_scope_hash")
    if (
        type(required) is not int
        or required <= 0
        or type(confirmed) is not int
        or confirmed != required
        or not isinstance(expected_entry_indices, list)
        or not expected_entry_indices
        or any(
            type(value) is not int or value <= 0
            for value in expected_entry_indices
        )
        or len(set(expected_entry_indices)) != len(expected_entry_indices)
        or type(parent_leg_index) is not int
        or parent_leg_index != expected_entry_indices[0]
        or not _fingerprint(uid_scope_hash)
    ):
        return False
    protection_children = [
        child for child in children if str(child.phase) == "protection_readback"
    ]
    entry_children = [
        child for child in children if str(child.phase) != "protection_readback"
    ]
    entry_child_indices: list[int] = []
    for child in entry_children:
        child_evidence = _canonical_object(child.evidence_json)
        if child_evidence is None:
            return False
        child_leg_index = child_evidence.get("leg_index")
        if type(child_leg_index) is not int or child_leg_index <= 0:
            return False
        if str(child.state) == "completed":
            if (
                str(child.outcome_certainty) != "confirmed"
                or child.writer_attempted_at is None
                or child.completed_at is None
                or str(child.reason_code) != "entry_sequence_completed"
                or not _fingerprint(child_evidence.get("order_ref"))
                or not _fingerprint(child_evidence.get("baseline_fingerprint"))
            ):
                return False
        elif str(child.state) == "pre_submit_deferred":
            if (
                str(child.outcome_certainty) != "not_sent"
                or child.writer_attempted_at is not None
                or child.completed_at is not None
                or str(child.reason_code) != "next_leg_preflight_deferred"
                or child_evidence.get("writer_attempted") is not False
            ):
                return False
        else:
            return False
        entry_child_indices.append(child_leg_index)
    protection_indices: list[int] = []
    protection_intent_ids: list[int] = []
    for child in protection_children:
        child_evidence = _canonical_object(child.evidence_json)
        if (
            child_evidence is None
            or str(child.state) != "protected"
            or str(child.outcome_certainty) != "confirmed"
            or child.writer_attempted_at is None
            or child.completed_at is None
            or str(child.reason_code) != "protection_fully_confirmed"
            or type(child_evidence.get("protection_index")) is not int
            or child_evidence.get("protection_index") < 0
            or type(child_evidence.get("position_mutation_intent_id")) is not int
            or child_evidence.get("position_mutation_intent_id") <= 0
        ):
            return False
        protection_indices.append(child_evidence["protection_index"])
        protection_intent_ids.append(
            child_evidence["position_mutation_intent_id"]
        )
    return (
        str(parent.outcome_certainty) == "confirmed"
        and parent.writer_attempted_at is not None
        and str(parent.reason_code) == "protection_fully_confirmed"
        and len(protection_children) == required
        and all(str(child.state) == "protected" for child in protection_children)
        and sorted(protection_indices) == list(range(required))
        and len(protection_intent_ids) == len(set(protection_intent_ids))
        and sorted(entry_child_indices) == sorted(expected_entry_indices[1:])
        and len(entry_child_indices) == len(set(entry_child_indices))
    )


def _operation_keys_valid(
    parent: _Operation,
    children: Sequence[_Operation],
) -> bool:
    parent_evidence = _canonical_object(parent.evidence_json)
    if parent_evidence is None:
        return False
    parent_leg_index = parent_evidence.get("leg_index")
    if type(parent_leg_index) is not int or parent_leg_index <= 0:
        return False
    prefix = f"protected-entry:v1:signal:{int(parent.trade_signal_id)}:leg:"
    if str(parent.operation_key) != f"{prefix}{parent_leg_index}:entry":
        return False
    for child in children:
        child_evidence = _canonical_object(child.evidence_json)
        if child_evidence is None:
            return False
        if str(child.phase) == "protection_readback":
            protection_index = child_evidence.get("protection_index")
            if type(protection_index) is not int or protection_index < 0:
                return False
            expected = (
                f"{prefix}{parent_leg_index}:protection:{protection_index}"
            )
        else:
            child_leg_index = child_evidence.get("leg_index")
            if type(child_leg_index) is not int or child_leg_index <= 0:
                return False
            expected = f"{prefix}{child_leg_index}:entry"
        if str(child.operation_key) != expected:
            return False
    return True


def _fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def projection_uid_scope_hash(parent: _Operation) -> str | None:
    """Return the parent's canonical account-scope authority, if valid."""

    return _projection_uid_scope_hash(parent)


def _projection_uid_scope_hash(parent: _Operation) -> str | None:
    evidence = _canonical_object(parent.evidence_json)
    if evidence is None:
        return None
    uid_scope_hash = evidence.get("uid_scope_hash")
    return str(uid_scope_hash) if _fingerprint(uid_scope_hash) else None


def _canonical_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        return None

    def reject_duplicate_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(constant)
            ),
        )
        if (
            not isinstance(parsed, dict)
            or json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != value
        ):
            return None
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    return parsed


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
