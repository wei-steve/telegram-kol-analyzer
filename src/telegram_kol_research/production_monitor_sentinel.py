"""Pure sentinel evaluation and a dormant, injected collection runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from telegram_kol_research.production_monitor_contract import (
    MONITOR_ADAPTER_NAMES,
    SENTINEL_REASON_CODES,
    build_monitor_projection,
)
from telegram_kol_research.production_monitor_policy import (
    CandidateContext,
    CandidateObservation,
    CandidateState,
    classify_candidate,
)
from telegram_kol_research.production_monitor_state import (
    MONITOR_STATE_MAX_CANDIDATES,
    LatestCompletedResult,
    ProductionMonitorState,
    ProductionMonitorStateStore,
)


_MAX_OBSERVATION_GENERATION = 2**63 - 1


@dataclass(frozen=True, slots=True)
class SentinelObservation:
    checked_at: datetime
    candidates: tuple[CandidateObservation, ...]
    adapter_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SentinelResult:
    observation_generation: int
    checked_at: datetime
    execution_status: str
    observed_health: str
    reason_codes: tuple[str, ...]
    adapter_failures: tuple[str, ...]
    evidence_complete: bool
    state_fingerprint: str
    candidate_states: tuple[CandidateState, ...]
    incident_projection: Mapping[str, object] | None
    anomaly_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SentinelRunOutcome:
    execution_status: str
    observed_health: str
    observation_generation: int | None
    persisted: bool
    exit_code: int
    failure_code: str | None
    result: SentinelResult | None


class SentinelObservationCollector(Protocol):
    def __call__(self) -> SentinelObservation: ...


def collect_sentinel_observation(
    *,
    checked_at: datetime,
    expected_adapters: frozenset[str],
    adapter_collectors: Mapping[
        str, Callable[[], Sequence[CandidateObservation]]
    ],
) -> SentinelObservation:
    """Run injected adapters and retain only their closed failure labels."""

    timestamp = _aware_utc(checked_at, field="checked_at")
    if (
        not isinstance(expected_adapters, frozenset)
        or not expected_adapters
        or not expected_adapters <= MONITOR_ADAPTER_NAMES
    ):
        raise ValueError("expected adapters are invalid")
    if (
        not isinstance(adapter_collectors, Mapping)
        or not set(adapter_collectors) <= expected_adapters
    ):
        raise ValueError("adapter collectors must be a mapping")
    candidates: list[CandidateObservation] = []
    failures: list[str] = sorted(expected_adapters - set(adapter_collectors))
    for adapter_name in sorted(set(adapter_collectors) & expected_adapters):
        collector = adapter_collectors[adapter_name]
        if not callable(collector):
            raise ValueError("adapter collector is invalid")
        try:
            collected = collector()
            if not isinstance(collected, Sequence) or isinstance(
                collected, (str, bytes, bytearray)
            ):
                raise ValueError("adapter result is invalid")
            if (
                len(collected) > MONITOR_STATE_MAX_CANDIDATES
                or len(candidates) + len(collected)
                > MONITOR_STATE_MAX_CANDIDATES
            ):
                raise ValueError("adapter result exceeds safe bounds")
            rows = tuple(collected)
            if any(not isinstance(item, CandidateObservation) for item in rows):
                raise ValueError("adapter result is invalid")
            candidates.extend(rows)
        except Exception:
            failures.append(adapter_name)
    return SentinelObservation(
        checked_at=timestamp,
        candidates=tuple(candidates),
        adapter_failures=tuple(sorted(failures)),
    )


def evaluate_sentinel_observation(
    *,
    observation: SentinelObservation,
    previous_state: ProductionMonitorState,
    observation_generation: int,
) -> tuple[SentinelResult, ProductionMonitorState]:
    """Evaluate typed, bounded facts without performing I/O."""

    if not isinstance(observation, SentinelObservation):
        raise ValueError("sentinel observation is invalid")
    if not isinstance(previous_state, ProductionMonitorState):
        raise ValueError("sentinel previous state is invalid")
    if (
        type(observation_generation) is not int
        or not 1 <= observation_generation <= _MAX_OBSERVATION_GENERATION
    ):
        raise ValueError("sentinel observation generation is invalid")
    previous_generation = (
        None
        if previous_state.latest_completed_result is None
        else previous_state.latest_completed_result.observation_generation
    )
    expected_generation = (
        1 if previous_generation is None else previous_generation + 1
    )
    if observation_generation != expected_generation:
        raise ValueError("sentinel observation generation is not the exact next value")
    checked_at = _aware_utc(observation.checked_at, field="checked_at")
    adapter_failures = _closed_adapter_failures(observation.adapter_failures)
    if (
        not isinstance(observation.candidates, tuple)
        or len(observation.candidates) > MONITOR_STATE_MAX_CANDIDATES
        or any(
            not isinstance(item, CandidateObservation)
            for item in observation.candidates
        )
    ):
        raise ValueError("sentinel candidates are invalid")
    previous_result = previous_state.latest_completed_result
    if previous_result is not None and checked_at <= max(
        previous_result.checked_at,
        previous_result.checked_at_high_watermark,
    ):
        return _evaluate_clock_rollback(
            checked_at=checked_at,
            observation_generation=observation_generation,
            adapter_failures=adapter_failures,
            previous_state=previous_state,
        )

    previous_by_identity = {
        (item.reason_code, item.fingerprint): item
        for item in previous_state.candidates
    }
    if len(previous_by_identity) != len(previous_state.candidates):
        raise ValueError("sentinel previous candidates are duplicated")
    observation_fingerprints = [
        item.fingerprint for item in observation.candidates
    ]
    if len(set(observation_fingerprints)) != len(observation_fingerprints):
        raise ValueError("sentinel candidates are duplicated")

    next_candidates: list[CandidateState] = []
    active_reasons: set[str] = set()
    confirmed: list[CandidateState] = []
    incident_candidates: list[CandidateState] = []
    unknown_present = bool(adapter_failures)
    evidence_incomplete = bool(adapter_failures)
    for candidate in observation.candidates:
        classification = classify_candidate(
            candidate,
            CandidateContext(
                now=checked_at,
                previous=previous_by_identity.pop(
                    (candidate.reason_code, candidate.fingerprint), None
                ),
            ),
        )
        candidate_state = classification.candidate_state
        if not classification.evidence_complete:
            evidence_incomplete = True
        if candidate_state is not None:
            next_candidates.append(candidate_state)
        if classification.observed_health == "UNHEALTHY":
            if candidate.reason_code in SENTINEL_REASON_CODES:
                active_reasons.add(candidate.reason_code)
            if candidate_state is not None and candidate_state.lifecycle == "CONFIRMED":
                confirmed.append(candidate_state)
                if (
                    classification.incident_eligible
                    and classification.evidence_complete
                ):
                    incident_candidates.append(candidate_state)
        elif classification.observed_health == "UNKNOWN":
            unknown_present = True
            active_reasons.add(
                candidate.reason_code
                if candidate.reason_code in SENTINEL_REASON_CODES
                else "adapter_failure"
            )
    
    for candidate_state in previous_by_identity.values():
        if candidate_state.lifecycle == "CONFIRMED":
            evidence_incomplete = True
            next_candidates.append(candidate_state)
            active_reasons.add(candidate_state.reason_code)
            confirmed.append(candidate_state)
        elif candidate_state.lifecycle == "SETTLING":
            evidence_incomplete = True
            next_candidates.append(candidate_state)
            active_reasons.add(candidate_state.reason_code)
            unknown_present = True

    if adapter_failures:
        active_reasons.add("adapter_failure")
    candidate_states = tuple(
        sorted(
            next_candidates,
            key=lambda item: item.fingerprint,
        )
    )
    if len(candidate_states) > MONITOR_STATE_MAX_CANDIDATES:
        raise ValueError("sentinel candidate state exceeds safe bounds")
    reason_codes = tuple(sorted(active_reasons))
    observed_health = (
        "UNHEALTHY"
        if confirmed
        else "UNKNOWN"
        if unknown_present
        else "HEALTHY"
    )
    evidence_complete = not evidence_incomplete
    anomaly_fingerprint = (
        _confirmed_fingerprint(
            incident_candidates if incident_candidates else confirmed
        )
        if confirmed
        else None
    )
    incident_projection: Mapping[str, object] | None = None
    if incident_candidates and evidence_complete:
        confirmed_reasons = sorted(
            {item.reason_code for item in incident_candidates}
        )
        projection = build_monitor_projection(
            {
                "checked_at": checked_at,
                "observation_generation": observation_generation,
                "anomaly_fingerprint": anomaly_fingerprint,
                "execution_status": "COMPLETED",
                "observed_health": "UNHEALTHY",
                "reason_codes": confirmed_reasons,
                "adapter_failures": [],
                "fallback_reason": None,
            }
        )
        incident_projection = MappingProxyType(projection)

    state_fingerprint = _state_fingerprint(
        observation_generation=observation_generation,
        checked_at=checked_at,
        observed_health=observed_health,
        reason_codes=reason_codes,
        adapter_failures=adapter_failures,
        candidate_states=candidate_states,
    )
    result = SentinelResult(
        observation_generation=observation_generation,
        checked_at=checked_at,
        execution_status="COMPLETED",
        observed_health=observed_health,
        reason_codes=reason_codes,
        adapter_failures=adapter_failures,
        evidence_complete=evidence_complete,
        state_fingerprint=state_fingerprint,
        candidate_states=candidate_states,
        incident_projection=incident_projection,
        anomaly_fingerprint=anomaly_fingerprint,
    )
    next_state = replace(
        previous_state,
        candidates=candidate_states,
        latest_completed_result=LatestCompletedResult(
            observation_generation=observation_generation,
            checked_at=checked_at,
            checked_at_high_watermark=checked_at,
            execution_status="COMPLETED",
            observed_health=observed_health,
            reason_codes=reason_codes,
            adapter_failures=adapter_failures,
            evidence_complete=evidence_complete,
            state_fingerprint=state_fingerprint,
        ),
    )
    return result, next_state


def run_production_monitor_sentinel(
    *,
    state_store: ProductionMonitorStateStore,
    observation_collector: SentinelObservationCollector,
) -> SentinelRunOutcome:
    """Own one load-collect-evaluate-persist transition under one lease."""

    if not isinstance(state_store, ProductionMonitorStateStore) or not callable(
        observation_collector
    ):
        return _failed_outcome("sentinel_configuration_invalid")
    result: SentinelResult | None = None
    try:
        with state_store.single_flight() as lease:
            if not lease.acquired:
                return _failed_outcome("sentinel_overlap")
            previous_state = lease.load_for_sentinel()
            previous_result = previous_state.latest_completed_result
            generation = (
                1
                if previous_result is None
                else previous_result.observation_generation + 1
            )
            if generation > _MAX_OBSERVATION_GENERATION:
                return _failed_outcome("sentinel_generation_exhausted")
            observation = observation_collector()
            result, next_state = evaluate_sentinel_observation(
                observation=observation,
                previous_state=previous_state,
                observation_generation=generation,
            )
            try:
                lease.save(next_state)
            except Exception:
                return SentinelRunOutcome(
                    execution_status="FAILED",
                    observed_health=result.observed_health,
                    observation_generation=generation,
                    persisted=False,
                    exit_code=1,
                    failure_code="sentinel_persistence_failed",
                    result=result,
                )
            return SentinelRunOutcome(
                execution_status="COMPLETED",
                observed_health=result.observed_health,
                observation_generation=generation,
                persisted=True,
                exit_code=0,
                failure_code=None,
                result=result,
            )
    except ValueError:
        return _failed_outcome("sentinel_input_invalid", result=result)
    except Exception:
        return _failed_outcome("sentinel_unhandled_failure", result=result)


def _evaluate_clock_rollback(
    *,
    checked_at: datetime,
    observation_generation: int,
    adapter_failures: tuple[str, ...],
    previous_state: ProductionMonitorState,
) -> tuple[SentinelResult, ProductionMonitorState]:
    """Persist a typed aggregate temporal anomaly without inventing a fact row."""

    candidate_states = previous_state.candidates
    active_reasons = {
        item.reason_code
        for item in candidate_states
        if item.lifecycle in {"CONFIRMED", "SETTLING"}
    }
    previous_result = previous_state.latest_completed_result
    if previous_result is not None and previous_result.observed_health == "UNHEALTHY":
        active_reasons.update(previous_result.reason_codes)
    active_reasons.add("monitor_clock_rollback")
    if adapter_failures:
        active_reasons.add("adapter_failure")
    reason_codes = tuple(sorted(active_reasons))
    confirmed = tuple(
        item for item in candidate_states if item.lifecycle == "CONFIRMED"
    )
    observed_health = (
        "UNHEALTHY"
        if confirmed
        or (
            previous_result is not None
            and previous_result.observed_health == "UNHEALTHY"
        )
        else "UNKNOWN"
    )
    state_fingerprint = _state_fingerprint(
        observation_generation=observation_generation,
        checked_at=checked_at,
        observed_health=observed_health,
        reason_codes=reason_codes,
        adapter_failures=adapter_failures,
        candidate_states=candidate_states,
    )
    anomaly_fingerprint = (
        _confirmed_fingerprint(confirmed) if confirmed else None
    )
    result = SentinelResult(
        observation_generation=observation_generation,
        checked_at=checked_at,
        execution_status="COMPLETED",
        observed_health=observed_health,
        reason_codes=reason_codes,
        adapter_failures=adapter_failures,
        evidence_complete=False,
        state_fingerprint=state_fingerprint,
        candidate_states=candidate_states,
        incident_projection=None,
        anomaly_fingerprint=anomaly_fingerprint,
    )
    next_state = replace(
        previous_state,
        latest_completed_result=LatestCompletedResult(
            observation_generation=observation_generation,
            checked_at=checked_at,
            checked_at_high_watermark=(
                checked_at
                if previous_result is None
                else max(
                    checked_at,
                    previous_result.checked_at_high_watermark,
                )
            ),
            execution_status="COMPLETED",
            observed_health=observed_health,
            reason_codes=reason_codes,
            adapter_failures=adapter_failures,
            evidence_complete=False,
            state_fingerprint=state_fingerprint,
        ),
    )
    return result, next_state


def _failed_outcome(
    failure_code: str,
    *,
    result: SentinelResult | None = None,
) -> SentinelRunOutcome:
    return SentinelRunOutcome(
        execution_status="FAILED",
        observed_health=(
            "UNKNOWN" if result is None else result.observed_health
        ),
        observation_generation=(
            None if result is None else result.observation_generation
        ),
        persisted=False,
        exit_code=1,
        failure_code=failure_code,
        result=result,
    )


def _closed_adapter_failures(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > len(MONITOR_ADAPTER_NAMES):
        raise ValueError("sentinel adapter failures are invalid")
    if any(
        not isinstance(item, str) or item not in MONITOR_ADAPTER_NAMES
        for item in value
    ):
        raise ValueError("sentinel adapter failures are invalid")
    if tuple(sorted(set(value))) != value:
        raise ValueError("sentinel adapter failures are not canonical")
    return value


def _confirmed_fingerprint(candidates: Sequence[CandidateState]) -> str:
    return _sha256_json(
        {
            "confirmed": [
                {"fingerprint": item.fingerprint, "reason_code": item.reason_code}
                for item in sorted(
                    candidates,
                    key=lambda value: (value.reason_code, value.fingerprint),
                )
            ]
        }
    )


def _state_fingerprint(
    *,
    observation_generation: int,
    checked_at: datetime,
    observed_health: str,
    reason_codes: tuple[str, ...],
    adapter_failures: tuple[str, ...],
    candidate_states: tuple[CandidateState, ...],
) -> str:
    return _sha256_json(
        {
            "adapter_failures": list(adapter_failures),
            "candidate_states": [
                {
                    "fingerprint": item.fingerprint,
                    "lifecycle": item.lifecycle,
                    "reason_code": item.reason_code,
                }
                for item in candidate_states
            ],
            "checked_at": checked_at.isoformat(),
            "observation_generation": observation_generation,
            "observed_health": observed_health,
            "reason_codes": list(reason_codes),
        }
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_utc(value: object, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"sentinel {field} must be timezone-aware")
    return value.astimezone(UTC)
