"""Strict atomic state for the dormant production monitor sentinel v2."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram_kol_research.production_monitor_contract import (
    MONITOR_ADAPTER_NAMES,
    MONITOR_EXECUTION_STATUSES,
    MONITOR_OBSERVED_HEALTH_STATUSES,
    SENTINEL_REASON_CODES,
)
from telegram_kol_research.production_monitor_policy import (
    EVIDENCE_UNKNOWN,
    IMMEDIATE,
    SETTLING,
    REASON_POLICIES,
    CandidateState,
)


MONITOR_STATE_SCHEMA_VERSION = 2
MONITOR_STATE_MAX_BYTES = 512 * 1024
MONITOR_STATE_MAX_CANDIDATES = 128
MONITOR_STATE_MAX_ACCEPTANCES = 128
DEFAULT_SENTINEL_STATE_PATH = (
    "/var/lib/telegram-kol-monitor/sentinel-v2.json"
)

_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidates",
        "incident_acceptances",
        "fallback",
        "latest_completed_result",
        "audit_cursor",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "reason_code",
        "fingerprint",
        "first_observed_at",
        "last_observed_at",
        "last_progress_at",
        "execution_deadline_at",
        "earliest_confirmation_at",
        "anomaly_generations",
        "healthy_generations",
        "snapshot_generation_watermark",
        "consecutive_observations",
        "last_observation_anomalous",
        "lifecycle",
        "confirmation_evidence_class",
        "resolution_evidence_class",
    }
)
_ACCEPTANCE_FIELDS = frozenset(
    {"candidate_fingerprint", "submission_id", "accepted_at"}
)
_FALLBACK_FIELDS = frozenset(
    {
        "fingerprint",
        "status",
        "attempts",
        "last_attempt_at",
        "next_attempt_at",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "checked_at",
        "execution_status",
        "observed_health",
        "reason_codes",
        "adapter_failures",
        "evidence_complete",
        "state_fingerprint",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CURSOR = re.compile(r"[A-Za-z0-9:._+-]{1,128}\Z")
_EVIDENCE_CLASS = re.compile(r"[A-Z0-9_]{1,96}\Z")
_CONFIRMATION_EVIDENCE_CLASSES = frozenset(
    {
        "DURABLE_FACT",
        "TWO_DISTINCT_COMPLETE_POST_PROGRESS_GENERATIONS",
    }
)
_RESOLUTION_EVIDENCE_CLASSES = frozenset(
    {
        "COMPLETE_DURABLE_ABSENCE",
        "COMPLETE_EVIDENCE_NO_ANOMALY",
        "COMPLETE_HEALTHY_GENERATION",
        "DURABLE_TERMINAL",
        "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS",
    }
)
_LIFECYCLES = frozenset({"SETTLING", "CONFIRMED", "RESOLVED"})
_FALLBACK_STATUSES = frozenset({"PENDING", "DELIVERED"})
_MAX_GENERATIONS_PER_CLASS = 2
_MAX_ATTEMPTS = 1_000_000


@dataclass(frozen=True, slots=True)
class IncidentAcceptanceState:
    candidate_fingerprint: str
    submission_id: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class FallbackDeliveryState:
    fingerprint: str
    status: str
    attempts: int
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class LatestCompletedResult:
    checked_at: datetime
    execution_status: str
    observed_health: str
    reason_codes: tuple[str, ...]
    adapter_failures: tuple[str, ...]
    evidence_complete: bool
    state_fingerprint: str


@dataclass(frozen=True, slots=True)
class ProductionMonitorState:
    schema_version: int = MONITOR_STATE_SCHEMA_VERSION
    candidates: tuple[CandidateState, ...] = ()
    incident_acceptances: tuple[IncidentAcceptanceState, ...] = ()
    fallback: FallbackDeliveryState | None = None
    latest_completed_result: LatestCompletedResult | None = None
    audit_cursor: str | None = None


class ProductionMonitorStateStore:
    """Load and atomically replace one strict sentinel-v2 state document."""

    def __init__(
        self,
        path: str | Path = DEFAULT_SENTINEL_STATE_PATH,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._active_capability: object | None = None
        self._active_owner_thread_id: int | None = None

    @property
    def single_flight_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.sentinel.lock")

    def single_flight(self) -> "SentinelStateLease":
        """Try one cross-process lease for the complete state transition."""

        return SentinelStateLease(self)

    def load(self) -> ProductionMonitorState:
        now = _aware_utc(self._now_factory(), field="current time")
        parent_fd = _open_safe_parent(self.path)
        descriptor: int | None = None
        try:
            _reject_existing_target(parent_fd, self.path.name)
            try:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | _no_follow_flag() | _close_on_exec_flag(),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return ProductionMonitorState()
            except OSError as exc:
                raise ValueError(
                    "production monitor state is unsafe or a symlink"
                ) from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("production monitor state must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("production monitor state mode must be 0600")
            if metadata.st_size > MONITOR_STATE_MAX_BYTES:
                raise ValueError("production monitor state exceeds safe size")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read(MONITOR_STATE_MAX_BYTES + 1)
            if len(raw) > MONITOR_STATE_MAX_BYTES:
                raise ValueError("production monitor state exceeds safe size")
            return _state_from_bytes(raw, now=now)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def save(self, state: ProductionMonitorState) -> ProductionMonitorState:
        del state
        raise RuntimeError(
            "public save is disabled; use lease.save under a single-flight lease"
        )

    def _save_authoritative(
        self,
        state: ProductionMonitorState,
        *,
        capability: object,
    ) -> ProductionMonitorState:
        if capability is not self._active_capability:
            raise RuntimeError(
                "production monitor state lease capability is not active"
            )
        if self._active_owner_thread_id != threading.get_ident():
            raise RuntimeError(
                "production monitor state lease belongs to another owner thread"
            )
        now = _aware_utc(self._now_factory(), field="current time")
        encoded = _state_to_bytes(state, now=now)
        if len(encoded) > MONITOR_STATE_MAX_BYTES:
            raise ValueError("production monitor state exceeds safe size")
        validated = _state_from_bytes(encoded, now=now)
        parent_fd = _open_safe_parent(self.path)
        temporary_name = (
            f".{self.path.name}.{os.getpid()}.{os.urandom(16).hex()}.tmp"
        )
        descriptor: int | None = None
        try:
            _reject_existing_target(parent_fd, self.path.name)
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _no_follow_flag()
                | _close_on_exec_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("production monitor state temporary is unsafe")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _reject_existing_target(parent_fd, self.path.name)
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = ""
            os.fsync(parent_fd)
            return validated
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)


class SentinelStateLease:
    """Nonblocking OS lease spanning load, evaluate, and persist."""

    def __init__(self, store: ProductionMonitorStateStore) -> None:
        self._store = store
        self._capability = object()
        self._descriptor: int | None = None
        self._owner_thread_id: int | None = None
        self.acquired = False

    def __enter__(self) -> "SentinelStateLease":
        if self._store._active_capability is not None:
            return self
        parent_fd = _open_safe_parent(self._store.single_flight_path)
        try:
            name = self._store.single_flight_path.name
            _reject_existing_lock_symlink(parent_fd, name)
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | _no_follow_flag()
                | _close_on_exec_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(descriptor, 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("production monitor state lease is unsafe")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    os.close(descriptor)
                    return self
                _verify_lock_identity(
                    parent_fd,
                    name,
                    os.fstat(descriptor),
                )
                self._descriptor = descriptor
                self._owner_thread_id = threading.get_ident()
                self.acquired = True
                self._store._active_capability = self._capability
                self._store._active_owner_thread_id = self._owner_thread_id
                return self
            except BaseException:
                if self._descriptor is None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise
        finally:
            os.close(parent_fd)

    def __exit__(self, *_args: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        self.acquired = False
        self._owner_thread_id = None
        if self._store._active_capability is self._capability:
            self._store._active_capability = None
            self._store._active_owner_thread_id = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load(self) -> ProductionMonitorState:
        self._require_acquired()
        return self._store.load()

    def save(self, state: ProductionMonitorState) -> ProductionMonitorState:
        self._require_acquired()
        return self._store._save_authoritative(
            state,
            capability=self._capability,
        )

    def _require_acquired(self) -> None:
        if not self.acquired or self._descriptor is None:
            raise RuntimeError("production monitor state lease is not acquired")
        if self._owner_thread_id != threading.get_ident():
            raise RuntimeError(
                "production monitor state lease belongs to another owner thread"
            )


def _state_to_bytes(state: ProductionMonitorState, *, now: datetime) -> bytes:
    if not isinstance(state, ProductionMonitorState):
        raise ValueError("invalid production monitor state")
    payload = {
        "schema_version": state.schema_version,
        "candidates": [_candidate_payload(item) for item in state.candidates],
        "incident_acceptances": [
            _acceptance_payload(item) for item in state.incident_acceptances
        ],
        "fallback": (
            None if state.fallback is None else _fallback_payload(state.fallback)
        ),
        "latest_completed_result": (
            None
            if state.latest_completed_result is None
            else _result_payload(state.latest_completed_result)
        ),
        "audit_cursor": state.audit_cursor,
    }
    encoded = _canonical_json(payload) + b"\n"
    _state_from_bytes(encoded, now=now)
    return encoded


def _state_from_bytes(raw: bytes, *, now: datetime) -> ProductionMonitorState:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(payload, dict) or frozenset(payload) != _STATE_FIELDS:
            raise ValueError("state fields are invalid")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != MONITOR_STATE_SCHEMA_VERSION
        ):
            raise ValueError("state schema_version is invalid")
        raw_candidates = payload["candidates"]
        if (
            not isinstance(raw_candidates, list)
            or len(raw_candidates) > MONITOR_STATE_MAX_CANDIDATES
        ):
            raise ValueError("state candidate count is invalid")
        candidates = tuple(
            _candidate_from_payload(item, now=now) for item in raw_candidates
        )
        if len({item.fingerprint for item in candidates}) != len(candidates):
            raise ValueError("state candidate fingerprints are duplicated")
        if tuple(sorted(item.fingerprint for item in candidates)) != tuple(
            item.fingerprint for item in candidates
        ):
            raise ValueError("state candidates are not canonical")
        raw_acceptances = payload["incident_acceptances"]
        if (
            not isinstance(raw_acceptances, list)
            or len(raw_acceptances) > MONITOR_STATE_MAX_ACCEPTANCES
        ):
            raise ValueError("state incident acceptance count is invalid")
        acceptances = tuple(
            _acceptance_from_payload(item, now=now)
            for item in raw_acceptances
        )
        if len({item.submission_id for item in acceptances}) != len(acceptances):
            raise ValueError("state incident acceptances are duplicated")
        if tuple(sorted(item.submission_id for item in acceptances)) != tuple(
            item.submission_id for item in acceptances
        ):
            raise ValueError("state incident acceptances are not canonical")
        raw_fallback = payload["fallback"]
        fallback = (
            None
            if raw_fallback is None
            else _fallback_from_payload(raw_fallback, now=now)
        )
        raw_result = payload["latest_completed_result"]
        latest_result = (
            None
            if raw_result is None
            else _result_from_payload(raw_result, now=now)
        )
        audit_cursor = payload["audit_cursor"]
        if audit_cursor is not None and (
            not isinstance(audit_cursor, str)
            or _SAFE_CURSOR.fullmatch(audit_cursor) is None
        ):
            raise ValueError("state audit cursor is invalid")
        return ProductionMonitorState(
            candidates=candidates,
            incident_acceptances=acceptances,
            fallback=fallback,
            latest_completed_result=latest_result,
            audit_cursor=audit_cursor,
        )
    except (
        KeyError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ValueError(f"invalid production monitor state: {exc}") from None


def _candidate_payload(value: CandidateState) -> dict[str, Any]:
    if not isinstance(value, CandidateState):
        raise ValueError("state candidate is invalid")
    return {
        "reason_code": value.reason_code,
        "fingerprint": value.fingerprint,
        "first_observed_at": _format_datetime(value.first_observed_at),
        "last_observed_at": _format_datetime(value.last_observed_at),
        "last_progress_at": _format_optional_datetime(value.last_progress_at),
        "execution_deadline_at": _format_optional_datetime(
            value.execution_deadline_at
        ),
        "earliest_confirmation_at": _format_optional_datetime(
            value.earliest_confirmation_at
        ),
        "anomaly_generations": list(value.anomaly_generations),
        "healthy_generations": list(value.healthy_generations),
        "snapshot_generation_watermark": value.snapshot_generation_watermark,
        "consecutive_observations": value.consecutive_observations,
        "last_observation_anomalous": value.last_observation_anomalous,
        "lifecycle": value.lifecycle,
        "confirmation_evidence_class": value.confirmation_evidence_class,
        "resolution_evidence_class": value.resolution_evidence_class,
    }


def _candidate_from_payload(payload: object, *, now: datetime) -> CandidateState:
    if not isinstance(payload, dict) or frozenset(payload) != _CANDIDATE_FIELDS:
        raise ValueError("state candidate fields are invalid")
    reason_code = payload["reason_code"]
    if reason_code not in SENTINEL_REASON_CODES:
        raise ValueError("state candidate reason is invalid")
    fingerprint = _fingerprint(payload["fingerprint"], field="candidate")
    first_observed_at = _parse_datetime(payload["first_observed_at"])
    last_observed_at = _parse_datetime(payload["last_observed_at"])
    if first_observed_at > last_observed_at:
        raise ValueError("state candidate time order is invalid")
    if last_observed_at > now:
        raise ValueError("state candidate time is in the future")
    last_progress_at = _parse_optional_datetime(payload["last_progress_at"])
    execution_deadline_at = _parse_optional_datetime(
        payload["execution_deadline_at"]
    )
    earliest_confirmation_at = _parse_optional_datetime(
        payload["earliest_confirmation_at"]
    )
    if last_progress_at is not None and last_progress_at > last_observed_at:
        raise ValueError("state candidate progress time is invalid")
    if earliest_confirmation_at != execution_deadline_at:
        raise ValueError("state candidate deadline authority is invalid")
    if (
        last_progress_at is not None
        and execution_deadline_at is not None
        and execution_deadline_at < last_progress_at
    ):
        raise ValueError("state candidate deadline precedes progress")
    anomaly_generations = _generation_tuple(
        payload["anomaly_generations"], field="anomaly"
    )
    healthy_generations = _generation_tuple(
        payload["healthy_generations"], field="healthy"
    )
    snapshot_generation_watermark = payload["snapshot_generation_watermark"]
    if snapshot_generation_watermark is not None and (
        type(snapshot_generation_watermark) is not int
        or snapshot_generation_watermark < 0
    ):
        raise ValueError("state candidate generation watermark is invalid")
    generations = (*anomaly_generations, *healthy_generations)
    if generations and (
        snapshot_generation_watermark is None
        or max(generations) > snapshot_generation_watermark
    ):
        raise ValueError("state candidate generation watermark is inconsistent")
    if set(anomaly_generations).intersection(healthy_generations):
        raise ValueError("state candidate generations overlap")
    consecutive = payload["consecutive_observations"]
    if type(consecutive) is not int or not 1 <= consecutive <= _MAX_ATTEMPTS:
        raise ValueError("state candidate observation count is invalid")
    last_anomalous = payload["last_observation_anomalous"]
    if type(last_anomalous) is not bool:
        raise ValueError("state candidate observation kind is invalid")
    lifecycle = payload["lifecycle"]
    if lifecycle not in _LIFECYCLES:
        raise ValueError("state candidate lifecycle is invalid")
    confirmation = _evidence_class(
        payload["confirmation_evidence_class"],
        allowed=_CONFIRMATION_EVIDENCE_CLASSES,
        field="confirmation",
    )
    resolution = _evidence_class(
        payload["resolution_evidence_class"],
        allowed=_RESOLUTION_EVIDENCE_CLASSES,
        field="resolution",
    )
    if lifecycle == "SETTLING" and (confirmation is not None or resolution is not None):
        raise ValueError("state settling candidate evidence is inconsistent")
    if lifecycle == "CONFIRMED" and (confirmation is None or resolution is not None):
        raise ValueError("state confirmed candidate evidence is inconsistent")
    if lifecycle == "RESOLVED" and resolution is None:
        raise ValueError("state resolved candidate evidence is inconsistent")
    _validate_policy_evidence(
        reason_code=reason_code,
        lifecycle=lifecycle,
        confirmation=confirmation,
        resolution=resolution,
    )
    _validate_policy_authority(
        reason_code=reason_code,
        lifecycle=lifecycle,
        last_observed_at=last_observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        earliest_confirmation_at=earliest_confirmation_at,
        anomaly_generations=anomaly_generations,
        healthy_generations=healthy_generations,
        resolution=resolution,
    )
    _validate_policy_generation_evidence(
        reason_code=reason_code,
        lifecycle=lifecycle,
        resolution=resolution,
        anomaly_generations=anomaly_generations,
        healthy_generations=healthy_generations,
        consecutive_observations=consecutive,
        last_observation_anomalous=last_anomalous,
    )
    return CandidateState(
        reason_code=reason_code,
        fingerprint=fingerprint,
        first_observed_at=first_observed_at,
        last_observed_at=last_observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        earliest_confirmation_at=earliest_confirmation_at,
        anomaly_generations=anomaly_generations,
        healthy_generations=healthy_generations,
        snapshot_generation_watermark=snapshot_generation_watermark,
        consecutive_observations=consecutive,
        last_observation_anomalous=last_anomalous,
        lifecycle=lifecycle,
        confirmation_evidence_class=confirmation,
        resolution_evidence_class=resolution,
    )


def _acceptance_payload(value: IncidentAcceptanceState) -> dict[str, Any]:
    if not isinstance(value, IncidentAcceptanceState):
        raise ValueError("state incident acceptance is invalid")
    return {
        "candidate_fingerprint": value.candidate_fingerprint,
        "submission_id": value.submission_id,
        "accepted_at": _format_datetime(value.accepted_at),
    }


def _acceptance_from_payload(
    payload: object,
    *,
    now: datetime,
) -> IncidentAcceptanceState:
    if not isinstance(payload, dict) or frozenset(payload) != _ACCEPTANCE_FIELDS:
        raise ValueError("state incident acceptance fields are invalid")
    accepted_at = _parse_datetime(payload["accepted_at"])
    if accepted_at > now:
        raise ValueError("state incident acceptance is in the future")
    return IncidentAcceptanceState(
        candidate_fingerprint=_fingerprint(
            payload["candidate_fingerprint"], field="candidate"
        ),
        submission_id=_fingerprint(payload["submission_id"], field="submission"),
        accepted_at=accepted_at,
    )


def _fallback_payload(value: FallbackDeliveryState) -> dict[str, Any]:
    if not isinstance(value, FallbackDeliveryState):
        raise ValueError("state fallback is invalid")
    return {
        "fingerprint": value.fingerprint,
        "status": value.status,
        "attempts": value.attempts,
        "last_attempt_at": _format_optional_datetime(value.last_attempt_at),
        "next_attempt_at": _format_optional_datetime(value.next_attempt_at),
    }


def _fallback_from_payload(
    payload: object,
    *,
    now: datetime,
) -> FallbackDeliveryState:
    if not isinstance(payload, dict) or frozenset(payload) != _FALLBACK_FIELDS:
        raise ValueError("state fallback fields are invalid")
    fingerprint = _fingerprint(payload["fingerprint"], field="fallback")
    status = payload["status"]
    if status not in _FALLBACK_STATUSES:
        raise ValueError("state fallback status is invalid")
    attempts = payload["attempts"]
    if type(attempts) is not int or not 0 <= attempts <= _MAX_ATTEMPTS:
        raise ValueError("state fallback attempts are invalid")
    last_attempt_at = _parse_optional_datetime(payload["last_attempt_at"])
    next_attempt_at = _parse_optional_datetime(payload["next_attempt_at"])
    if last_attempt_at is not None and last_attempt_at > now:
        raise ValueError("state fallback attempt is in the future")
    if (
        last_attempt_at is not None
        and next_attempt_at is not None
        and next_attempt_at < last_attempt_at
    ):
        raise ValueError("state fallback retry order is invalid")
    if attempts == 0 and last_attempt_at is not None:
        raise ValueError("state fallback attempts are inconsistent")
    return FallbackDeliveryState(
        fingerprint=fingerprint,
        status=status,
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        next_attempt_at=next_attempt_at,
    )


def _result_payload(value: LatestCompletedResult) -> dict[str, Any]:
    if not isinstance(value, LatestCompletedResult):
        raise ValueError("state completed result is invalid")
    return {
        "checked_at": _format_datetime(value.checked_at),
        "execution_status": value.execution_status,
        "observed_health": value.observed_health,
        "reason_codes": list(value.reason_codes),
        "adapter_failures": list(value.adapter_failures),
        "evidence_complete": value.evidence_complete,
        "state_fingerprint": value.state_fingerprint,
    }


def _result_from_payload(
    payload: object,
    *,
    now: datetime,
) -> LatestCompletedResult:
    if not isinstance(payload, dict) or frozenset(payload) != _RESULT_FIELDS:
        raise ValueError("state completed result fields are invalid")
    checked_at = _parse_datetime(payload["checked_at"])
    if checked_at > now:
        raise ValueError("state completed result is in the future")
    execution_status = payload["execution_status"]
    if execution_status not in MONITOR_EXECUTION_STATUSES:
        raise ValueError("state execution status is invalid")
    if execution_status != "COMPLETED":
        raise ValueError("state latest completed result did not complete")
    observed_health = payload["observed_health"]
    if observed_health not in MONITOR_OBSERVED_HEALTH_STATUSES:
        raise ValueError("state observed health is invalid")
    reason_codes = _closed_tuple(
        payload["reason_codes"],
        allowed=SENTINEL_REASON_CODES,
        field="reason",
    )
    adapter_failures = _closed_tuple(
        payload["adapter_failures"],
        allowed=MONITOR_ADAPTER_NAMES,
        field="adapter",
    )
    evidence_complete = payload["evidence_complete"]
    if type(evidence_complete) is not bool:
        raise ValueError("state evidence completeness is invalid")
    if observed_health == "HEALTHY" and (
        reason_codes or adapter_failures or not evidence_complete
    ):
        raise ValueError("state healthy result is inconsistent")
    return LatestCompletedResult(
        checked_at=checked_at,
        execution_status=execution_status,
        observed_health=observed_health,
        reason_codes=reason_codes,
        adapter_failures=adapter_failures,
        evidence_complete=evidence_complete,
        state_fingerprint=_fingerprint(
            payload["state_fingerprint"], field="state"
        ),
    )


def _generation_tuple(value: object, *, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > _MAX_GENERATIONS_PER_CLASS:
        raise ValueError(f"state candidate {field} generation count is invalid")
    if any(type(item) is not int or item < 0 for item in value):
        raise ValueError(f"state candidate {field} generation is invalid")
    result = tuple(value)
    if any(current <= previous for previous, current in zip(result, result[1:])):
        raise ValueError(f"state candidate {field} generation order is invalid")
    return result


def _closed_tuple(
    value: object,
    *,
    allowed: frozenset[str],
    field: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > len(allowed):
        raise ValueError(f"state {field} values are invalid")
    if any(not isinstance(item, str) or item not in allowed for item in value):
        raise ValueError(f"state {field} values are invalid")
    result = tuple(value)
    if tuple(sorted(set(result))) != result:
        raise ValueError(f"state {field} values are not canonical")
    return result


def _fingerprint(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"state {field} fingerprint is invalid")
    return value


def _evidence_class(
    value: object,
    *,
    allowed: frozenset[str],
    field: str,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or _EVIDENCE_CLASS.fullmatch(value) is None
        or value not in allowed
    ):
        raise ValueError(f"state {field} evidence class is invalid")
    return value


def _validate_policy_evidence(
    *,
    reason_code: str,
    lifecycle: str,
    confirmation: str | None,
    resolution: str | None,
) -> None:
    policy = REASON_POLICIES[reason_code]
    evidence = (lifecycle, confirmation, resolution)
    if policy.classification == IMMEDIATE:
        allowed = {
            ("CONFIRMED", "DURABLE_FACT", None),
            ("RESOLVED", None, "COMPLETE_DURABLE_ABSENCE"),
            ("RESOLVED", "DURABLE_FACT", "DURABLE_TERMINAL"),
        }
    elif policy.classification == SETTLING:
        allowed = {
            ("SETTLING", None, None),
            ("CONFIRMED", policy.confirmation_evidence_class, None),
            ("RESOLVED", None, "COMPLETE_HEALTHY_GENERATION"),
            (
                "RESOLVED",
                policy.confirmation_evidence_class,
                "DURABLE_TERMINAL",
            ),
            (
                "RESOLVED",
                policy.confirmation_evidence_class,
                "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS",
            ),
        }
    elif policy.classification == EVIDENCE_UNKNOWN:
        allowed = {
            ("SETTLING", None, None),
            ("RESOLVED", None, "COMPLETE_EVIDENCE_NO_ANOMALY"),
        }
    else:  # pragma: no cover - the policy registry is closed at import time
        raise RuntimeError("production monitor reason policy is invalid")
    if evidence not in allowed:
        raise ValueError(
            "state candidate policy lifecycle evidence is inconsistent"
        )


def _validate_policy_authority(
    *,
    reason_code: str,
    lifecycle: str,
    last_observed_at: datetime,
    last_progress_at: datetime | None,
    execution_deadline_at: datetime | None,
    earliest_confirmation_at: datetime | None,
    anomaly_generations: tuple[int, ...],
    healthy_generations: tuple[int, ...],
    resolution: str | None,
) -> None:
    policy = REASON_POLICIES[reason_code]
    if policy.classification != SETTLING or lifecycle == "SETTLING":
        return
    if (
        last_progress_at is None
        or execution_deadline_at is None
        or earliest_confirmation_at is None
        or earliest_confirmation_at != execution_deadline_at
        or (
            execution_deadline_at > last_observed_at
            and resolution != "DURABLE_TERMINAL"
            and (
                lifecycle == "RESOLVED"
                or bool(anomaly_generations)
                or bool(healthy_generations)
            )
        )
    ):
        raise ValueError(
            "state settled candidate deadline authority is invalid"
        )


def _validate_policy_generation_evidence(
    *,
    reason_code: str,
    lifecycle: str,
    resolution: str | None,
    anomaly_generations: tuple[int, ...],
    healthy_generations: tuple[int, ...],
    consecutive_observations: int,
    last_observation_anomalous: bool,
) -> None:
    policy = REASON_POLICIES[reason_code]
    if policy.classification != SETTLING:
        if anomaly_generations or healthy_generations:
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        if lifecycle == "CONFIRMED" and not last_observation_anomalous:
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        if lifecycle == "RESOLVED" and last_observation_anomalous:
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        return

    if (
        anomaly_generations
        and healthy_generations
        and min(healthy_generations) <= max(anomaly_generations)
    ):
        raise ValueError(
            "state candidate generation evidence is inconsistent"
        )

    if lifecycle == "SETTLING":
        if healthy_generations or len(anomaly_generations) >= policy.minimum_bad_generations:
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        if anomaly_generations and (
            not last_observation_anomalous
            or consecutive_observations != len(anomaly_generations)
        ):
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        return

    if lifecycle == "CONFIRMED":
        if last_observation_anomalous:
            valid = (
                not healthy_generations
                and len(anomaly_generations) <= policy.minimum_bad_generations
                and consecutive_observations
                == max(1, len(anomaly_generations))
            )
        else:
            valid = (
                len(healthy_generations) <= 1
                and consecutive_observations
                == max(1, len(healthy_generations))
            )
        if not valid:
            raise ValueError(
                "state candidate generation evidence is inconsistent"
            )
        return

    if last_observation_anomalous:
        raise ValueError("state candidate generation evidence is inconsistent")
    if resolution == "COMPLETE_HEALTHY_GENERATION":
        valid = (
            not anomaly_generations
            and len(healthy_generations) == 1
            and consecutive_observations == 1
        )
    elif resolution == "DURABLE_TERMINAL":
        valid = (
            len(anomaly_generations) <= policy.minimum_bad_generations
            and not healthy_generations
            and consecutive_observations == 1
        )
    elif resolution == "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS":
        valid = (
            len(anomaly_generations) <= policy.minimum_bad_generations
            and len(healthy_generations) == policy.resolution_healthy_generations
            and consecutive_observations == len(healthy_generations)
        )
    else:  # policy evidence validation rejects all other settled resolutions
        valid = False
    if not valid:
        raise ValueError("state candidate generation evidence is inconsistent")


def _format_datetime(value: object) -> str:
    return _aware_utc(value, field="state datetime").isoformat()


def _format_optional_datetime(value: object) -> str | None:
    return None if value is None else _format_datetime(value)


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("state datetime is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("state datetime is invalid") from None
    return _aware_utc(parsed, field="state datetime")


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _aware_utc(value: object, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate state field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite state number")


def _open_safe_parent(path: Path) -> int:
    absolute = path.absolute()
    if absolute.name in {"", ".", ".."}:
        raise ValueError("production monitor state path is invalid")
    current = Path(absolute.anchor)
    for component in absolute.parent.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ValueError("production monitor state parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("production monitor state path contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("production monitor state parent is not a directory")
    flags = os.O_RDONLY | _no_follow_flag() | _close_on_exec_flag()
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        return os.open(absolute.parent, flags)
    except OSError as exc:
        raise ValueError("production monitor state parent is unsafe") from exc


def _reject_existing_target(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("production monitor state symlink is refused")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("production monitor state must be a regular file")


def _reject_existing_lock_symlink(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("production monitor state lease symlink is refused")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("production monitor state lease is unsafe")


def _verify_lock_identity(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError("production monitor state lease was replaced") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
    ):
        raise ValueError("production monitor state lease was replaced")


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)
