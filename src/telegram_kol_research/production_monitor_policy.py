"""Closed reason policies and pure temporal candidate transitions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from telegram_kol_research.production_monitor_contract import (
    SENTINEL_REASON_CODES,
)


IMMEDIATE = "IMMEDIATE"
SETTLING = "SETTLING"
EVIDENCE_UNKNOWN = "EVIDENCE_UNKNOWN"

_CONFIRMED = "CONFIRMED"
_RESOLVED = "RESOLVED"
_SETTLING = "SETTLING"
_MAX_TRACKED_GENERATIONS = 2
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ReasonPolicy:
    classification: str
    deadline_source: str | None
    minimum_bad_generations: int
    require_post_progress_snapshot: bool
    incident_eligible: bool
    maximum_decision_latency: timedelta | None
    resolution_healthy_generations: int
    confirmation_evidence_class: str
    resolution_rule: str


_IMMEDIATE_POLICY = ReasonPolicy(
    classification=IMMEDIATE,
    deadline_source=None,
    minimum_bad_generations=0,
    require_post_progress_snapshot=False,
    incident_eligible=True,
    maximum_decision_latency=timedelta(0),
    resolution_healthy_generations=0,
    confirmation_evidence_class="DURABLE_FACT",
    resolution_rule="DURABLE_TERMINAL",
)
_SETTLING_POLICY = ReasonPolicy(
    classification=SETTLING,
    deadline_source="execution_deadline_at",
    minimum_bad_generations=2,
    require_post_progress_snapshot=True,
    incident_eligible=True,
    maximum_decision_latency=timedelta(minutes=5),
    resolution_healthy_generations=2,
    confirmation_evidence_class=(
        "TWO_DISTINCT_COMPLETE_POST_PROGRESS_GENERATIONS"
    ),
    resolution_rule="DURABLE_TERMINAL_OR_TWO_HEALTHY_GENERATIONS",
)
_UNKNOWN_POLICY = ReasonPolicy(
    classification=EVIDENCE_UNKNOWN,
    deadline_source=None,
    minimum_bad_generations=0,
    require_post_progress_snapshot=False,
    incident_eligible=False,
    maximum_decision_latency=None,
    resolution_healthy_generations=0,
    confirmation_evidence_class="NONE",
    resolution_rule="COMPLETE_EVIDENCE_REQUIRED",
)


# Every reason is deliberately assigned here rather than falling through a
# default. High-level legacy reasons retain their closed meaning in phase one:
# audit_abnormal covers durable management submit_unknown/recovery_required
# rows, event_unknown_status covers durable unknown submission outcomes, and
# event_recovery_status covers durable execution recovery_required facts.
_REASON_POLICIES: dict[str, ReasonPolicy] = {
    "adapter_failure": _UNKNOWN_POLICY,
    "adjacent_entry_invariant_scan_incomplete": _UNKNOWN_POLICY,
    "authoritative_processor_required": _IMMEDIATE_POLICY,
    "audit_abnormal": _IMMEDIATE_POLICY,
    "audit_incomplete": _UNKNOWN_POLICY,
    "auto_trade_enabled_drift": _IMMEDIATE_POLICY,
    "completed_batch_missing_component_evidence": _SETTLING_POLICY,
    "composite_position_without_verified_stop": _SETTLING_POLICY,
    "consumed_entry_fragment_missing_assembly": _SETTLING_POLICY,
    "contract_violation_missing_stage1": _IMMEDIATE_POLICY,
    "duplicate_composite_close_submission": _IMMEDIATE_POLICY,
    "duplicate_manual_close": _IMMEDIATE_POLICY,
    "entry_message_assembly_v2_mode_drift": _IMMEDIATE_POLICY,
    "entry_preamble_ambiguous": _SETTLING_POLICY,
    "entry_preamble_mode_drift": _IMMEDIATE_POLICY,
    "entry_revision_replacement_before_old_terminal": _SETTLING_POLICY,
    "entry_revision_risk_budget_exceeded": _SETTLING_POLICY,
    "entry_revision_v2_mode_drift": _IMMEDIATE_POLICY,
    "exchange_snapshot_incomplete": _UNKNOWN_POLICY,
    "exchange_snapshot_stale": _UNKNOWN_POLICY,
    "exchange_snapshot_temporally_incoherent": _UNKNOWN_POLICY,
    "exchange_snapshot_unavailable": _UNKNOWN_POLICY,
    "event_recovery_status": _IMMEDIATE_POLICY,
    "event_unknown_status": _IMMEDIATE_POLICY,
    "executable_message_missing_contract": _IMMEDIATE_POLICY,
    "instruction_execution_contradiction": _IMMEDIATE_POLICY,
    "journal_errors": _UNKNOWN_POLICY,
    "live_entry_assembly_binding_evidence_missing": _SETTLING_POLICY,
    "live_entry_preamble_binding_evidence_missing": _SETTLING_POLICY,
    "live_entry_revision_protection_unverified": _SETTLING_POLICY,
    "live_position_retained_tp_oversized": _SETTLING_POLICY,
    "malformed_snapshot": _UNKNOWN_POLICY,
    "management_execution_mode_drift": _IMMEDIATE_POLICY,
    "max_concurrent_positions_drift": _IMMEDIATE_POLICY,
    "message_operation_coverage_incomplete": _UNKNOWN_POLICY,
    "message_operation_incident_missing_terminal": _IMMEDIATE_POLICY,
    "message_operation_supervisor_policy_invalid": _UNKNOWN_POLICY,
    "message_operation_supervisor_stale": _UNKNOWN_POLICY,
    "monitor_clock_rollback": _UNKNOWN_POLICY,
    "readiness_unavailable": _UNKNOWN_POLICY,
    "reviewed_sha_drift": _IMMEDIATE_POLICY,
    "sentinel_timer_late": _UNKNOWN_POLICY,
    "service_inactive": _IMMEDIATE_POLICY,
    "service_starting": _UNKNOWN_POLICY,
    "snapshot_refresh_overlap": _UNKNOWN_POLICY,
    "stale_adjacent_entry_admission": _SETTLING_POLICY,
    "stale_entry_preamble_unresolved": _SETTLING_POLICY,
    "stalled_composite_component": _SETTLING_POLICY,
    "state_invalid": _IMMEDIATE_POLICY,
}
REASON_POLICIES: Mapping[str, ReasonPolicy] = MappingProxyType(
    _REASON_POLICIES
)

if frozenset(REASON_POLICIES) != SENTINEL_REASON_CODES:  # pragma: no cover
    raise RuntimeError("production monitor reason policy registry is incomplete")


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    reason_code: str
    fingerprint: str
    observed_at: datetime
    anomaly_present: bool
    evidence_complete: bool
    snapshot_generation: int | None
    snapshot_started_at: datetime | None
    snapshot_completed_at: datetime | None
    last_progress_at: datetime | None
    execution_deadline_at: datetime | None
    durable_terminal_fact: bool = False


@dataclass(frozen=True, slots=True)
class CandidateState:
    reason_code: str
    fingerprint: str
    first_observed_at: datetime
    last_observed_at: datetime
    last_progress_at: datetime | None
    execution_deadline_at: datetime | None
    earliest_confirmation_at: datetime | None
    anomaly_generations: tuple[int, ...]
    healthy_generations: tuple[int, ...]
    snapshot_generation_watermark: int | None
    consecutive_observations: int
    last_observation_anomalous: bool
    lifecycle: str
    confirmation_evidence_class: str | None
    resolution_evidence_class: str | None


@dataclass(frozen=True, slots=True)
class CandidateContext:
    now: datetime
    previous: CandidateState | None = None


@dataclass(frozen=True, slots=True)
class CandidateClassification:
    observed_health: str
    incident_eligible: bool
    deployment_blocking: bool
    candidate_state: CandidateState | None
    decision_reason: str


def classify_candidate(
    candidate: CandidateObservation,
    context: CandidateContext,
) -> CandidateClassification:
    """Return a new state without sleeping, I/O, or guessed timeouts."""

    policy = REASON_POLICIES.get(candidate.reason_code)
    if policy is None:
        return _unknown(context.previous, "UNREGISTERED_REASON")
    if (
        not isinstance(candidate.fingerprint, str)
        or _SHA256.fullmatch(candidate.fingerprint) is None
    ):
        return _unknown(context.previous, "FINGERPRINT_INVALID")
    previous = _matching_previous(candidate, context.previous)
    snapshot_generation_watermark = (
        None
        if previous is None
        else previous.snapshot_generation_watermark
    )
    try:
        now = _aware_utc(context.now)
        observed_at = _aware_utc(candidate.observed_at)
        last_progress_at = _optional_aware_utc(candidate.last_progress_at)
        execution_deadline_at = _optional_aware_utc(
            candidate.execution_deadline_at
        )
    except ValueError:
        return _unknown(previous, "TIMESTAMP_INVALID")
    if observed_at > now:
        return _unknown(previous, "FUTURE_TIMESTAMP")
    if previous is not None and (
        now < previous.last_observed_at
        or observed_at < previous.last_observed_at
    ):
        return _unknown(previous, "CLOCK_ROLLBACK")
    if last_progress_at is not None and last_progress_at > now:
        return _unknown(previous, "FUTURE_TIMESTAMP")
    if type(candidate.anomaly_present) is not bool or type(
        candidate.evidence_complete
    ) is not bool:
        return _unknown(previous, "EVIDENCE_INVALID")
    if type(candidate.durable_terminal_fact) is not bool:
        return _unknown(previous, "DURABLE_TERMINAL_FACT_INVALID")
    if last_progress_at is not None and last_progress_at > observed_at:
        return _unknown(previous, "LAST_PROGRESS_AFTER_OBSERVATION")
    if not candidate.evidence_complete:
        return _unknown(previous, "EVIDENCE_INCOMPLETE")

    confirmed_terminal_resolution = (
        not candidate.anomaly_present
        and candidate.durable_terminal_fact
        and previous is not None
        and previous.lifecycle == _CONFIRMED
    )
    if (
        not confirmed_terminal_resolution
        and last_progress_at is not None
        and execution_deadline_at is not None
        and execution_deadline_at < last_progress_at
    ):
        return _unknown(previous, "TEMPORAL_ORDER_INVALID")

    if policy.classification == EVIDENCE_UNKNOWN:
        if not candidate.anomaly_present:
            state = _new_or_updated_state(
                candidate,
                previous,
                observed_at=observed_at,
                last_progress_at=last_progress_at,
                execution_deadline_at=execution_deadline_at,
                lifecycle=_RESOLVED,
                anomaly_generations=(),
                healthy_generations=(),
                snapshot_generation_watermark=snapshot_generation_watermark,
                consecutive_observations=1,
                confirmation_evidence_class=None,
                resolution_evidence_class="COMPLETE_EVIDENCE_NO_ANOMALY",
            )
            return _healthy(state, "COMPLETE_EVIDENCE_NO_ANOMALY")
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
            lifecycle=_SETTLING,
            anomaly_generations=(),
            healthy_generations=(),
            snapshot_generation_watermark=snapshot_generation_watermark,
            consecutive_observations=1,
            confirmation_evidence_class=None,
            resolution_evidence_class=None,
        )
        return CandidateClassification(
            observed_health="UNKNOWN",
            incident_eligible=False,
            deployment_blocking=True,
            candidate_state=state,
            decision_reason="EVIDENCE_POLICY_UNKNOWN",
        )

    if policy.classification == IMMEDIATE:
        return _classify_immediate(
            candidate,
            previous,
            policy=policy,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
        )

    if (
        confirmed_terminal_resolution
    ):
        assert previous is not None
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=previous.last_progress_at,
            execution_deadline_at=previous.execution_deadline_at,
            lifecycle=_RESOLVED,
            anomaly_generations=previous.anomaly_generations,
            healthy_generations=(),
            snapshot_generation_watermark=snapshot_generation_watermark,
            consecutive_observations=1,
            confirmation_evidence_class=previous.confirmation_evidence_class,
            resolution_evidence_class="DURABLE_TERMINAL",
        )
        return _healthy(state, "DURABLE_TERMINAL")

    sticky_confirmation_evidence: str | None = None
    authority_changed = previous is not None and (
        last_progress_at != previous.last_progress_at
        or execution_deadline_at != previous.execution_deadline_at
    )
    if authority_changed:
        if previous.lifecycle == _CONFIRMED:
            if last_progress_at is None or execution_deadline_at is None:
                return _unknown(previous, "DURABLE_AUTHORITY_CHANGED")
            sticky_confirmation_evidence = previous.confirmation_evidence_class
        previous = None
    if previous is not None and previous.lifecycle == _RESOLVED:
        previous = None
    if execution_deadline_at is None:
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=None,
            lifecycle=_SETTLING,
            anomaly_generations=(),
            healthy_generations=(),
            snapshot_generation_watermark=snapshot_generation_watermark,
            consecutive_observations=1,
            confirmation_evidence_class=None,
            resolution_evidence_class=None,
        )
        return CandidateClassification(
            observed_health="UNKNOWN",
            incident_eligible=False,
            deployment_blocking=True,
            candidate_state=state,
            decision_reason="DURABLE_DEADLINE_MISSING",
        )
    if observed_at < execution_deadline_at:
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
            lifecycle=(
                _CONFIRMED
                if sticky_confirmation_evidence is not None
                or (previous is not None and previous.lifecycle == _CONFIRMED)
                else _SETTLING
            ),
            anomaly_generations=(
                () if previous is None else previous.anomaly_generations
            ),
            healthy_generations=(
                () if previous is None else previous.healthy_generations
            ),
            snapshot_generation_watermark=snapshot_generation_watermark,
            consecutive_observations=(
                1 if previous is None else previous.consecutive_observations
            ),
            confirmation_evidence_class=(
                sticky_confirmation_evidence
                if sticky_confirmation_evidence is not None
                else (
                    None
                    if previous is None
                    else previous.confirmation_evidence_class
                )
            ),
            resolution_evidence_class=None,
        )
        if state.lifecycle == _CONFIRMED:
            return _confirmed(state, policy, "BEFORE_DURABLE_DEADLINE")
        return CandidateClassification(
            observed_health="UNKNOWN",
            incident_eligible=False,
            deployment_blocking=True,
            candidate_state=state,
            decision_reason="BEFORE_DURABLE_DEADLINE",
        )

    snapshot_validation = _validate_snapshot(
        candidate,
        now=now,
        observed_at=observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        snapshot_generation_watermark=snapshot_generation_watermark,
    )
    if snapshot_validation is not None:
        if previous is None:
            state = _new_or_updated_state(
                candidate,
                None,
                observed_at=observed_at,
                last_progress_at=last_progress_at,
                execution_deadline_at=execution_deadline_at,
                lifecycle=(
                    _CONFIRMED
                    if sticky_confirmation_evidence is not None
                    else _SETTLING
                ),
                anomaly_generations=(),
                healthy_generations=(),
                snapshot_generation_watermark=snapshot_generation_watermark,
                consecutive_observations=1,
                confirmation_evidence_class=sticky_confirmation_evidence,
                resolution_evidence_class=None,
            )
            if sticky_confirmation_evidence is not None:
                return _confirmed(state, policy, snapshot_validation)
            return CandidateClassification(
                observed_health="UNKNOWN",
                incident_eligible=False,
                deployment_blocking=True,
                candidate_state=state,
                decision_reason=snapshot_validation,
            )
        return _unknown(previous, snapshot_validation)
    generation = candidate.snapshot_generation
    assert generation is not None

    if candidate.anomaly_present:
        anomaly_generations = _next_generation_run(
            ()
            if previous is None or not previous.last_observation_anomalous
            else previous.anomaly_generations,
            generation,
        )
        if len(anomaly_generations) >= policy.minimum_bad_generations:
            state = _new_or_updated_state(
                candidate,
                previous,
                observed_at=observed_at,
                last_progress_at=last_progress_at,
                execution_deadline_at=execution_deadline_at,
                lifecycle=_CONFIRMED,
                anomaly_generations=anomaly_generations,
                healthy_generations=(),
                snapshot_generation_watermark=generation,
                consecutive_observations=len(anomaly_generations),
                confirmation_evidence_class=policy.confirmation_evidence_class,
                resolution_evidence_class=None,
            )
            return CandidateClassification(
                observed_health="UNHEALTHY",
                incident_eligible=policy.incident_eligible,
                deployment_blocking=True,
                candidate_state=state,
                decision_reason="CONFIRMED",
            )
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
            lifecycle=(
                _CONFIRMED
                if sticky_confirmation_evidence is not None
                or (previous is not None and previous.lifecycle == _CONFIRMED)
                else _SETTLING
            ),
            anomaly_generations=anomaly_generations,
            healthy_generations=(),
            snapshot_generation_watermark=generation,
            consecutive_observations=len(anomaly_generations),
            confirmation_evidence_class=(
                sticky_confirmation_evidence
                if sticky_confirmation_evidence is not None
                else (
                    previous.confirmation_evidence_class
                    if previous is not None and previous.lifecycle == _CONFIRMED
                    else None
                )
            ),
            resolution_evidence_class=None,
        )
        if state.lifecycle == _CONFIRMED:
            return _confirmed(state, policy, "CONFIRMED_STILL_PRESENT")
        return CandidateClassification(
            observed_health="UNKNOWN",
            incident_eligible=False,
            deployment_blocking=True,
            candidate_state=state,
            decision_reason="AWAITING_DISTINCT_BAD_GENERATIONS",
        )

    if sticky_confirmation_evidence is not None or (
        previous is not None and previous.lifecycle == _CONFIRMED
    ):
        healthy_generations = _next_generation_run(
            (
                ()
                if previous is None or previous.last_observation_anomalous
                else previous.healthy_generations
            ),
            generation,
        )
        if len(healthy_generations) >= policy.resolution_healthy_generations:
            state = _new_or_updated_state(
                candidate,
                previous,
                observed_at=observed_at,
                last_progress_at=last_progress_at,
                execution_deadline_at=execution_deadline_at,
                lifecycle=_RESOLVED,
                anomaly_generations=(
                    () if previous is None else previous.anomaly_generations
                ),
                healthy_generations=healthy_generations,
                snapshot_generation_watermark=generation,
                consecutive_observations=len(healthy_generations),
                confirmation_evidence_class=(
                    sticky_confirmation_evidence
                    if sticky_confirmation_evidence is not None
                    else previous.confirmation_evidence_class
                ),
                resolution_evidence_class=(
                    "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
                ),
            )
            return _healthy(state, "RECOVERY_CONFIRMED")
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
            lifecycle=_CONFIRMED,
            anomaly_generations=(
                () if previous is None else previous.anomaly_generations
            ),
            healthy_generations=healthy_generations,
            snapshot_generation_watermark=generation,
            consecutive_observations=len(healthy_generations),
            confirmation_evidence_class=(
                sticky_confirmation_evidence
                if sticky_confirmation_evidence is not None
                else previous.confirmation_evidence_class
            ),
            resolution_evidence_class=None,
        )
        return _confirmed(state, policy, "AWAITING_RECOVERY_HYSTERESIS")

    state = _new_or_updated_state(
        candidate,
        previous,
        observed_at=observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        lifecycle=_RESOLVED,
        anomaly_generations=(),
        healthy_generations=(generation,),
        snapshot_generation_watermark=generation,
        consecutive_observations=1,
        confirmation_evidence_class=None,
        resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
    )
    return _healthy(state, "CANDIDATE_CLEARED_BEFORE_CONFIRMATION")


def _classify_immediate(
    candidate: CandidateObservation,
    previous: CandidateState | None,
    *,
    policy: ReasonPolicy,
    observed_at: datetime,
    last_progress_at: datetime | None,
    execution_deadline_at: datetime | None,
) -> CandidateClassification:
    if candidate.anomaly_present:
        if previous is not None and previous.lifecycle == _RESOLVED:
            previous = None
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=last_progress_at,
            execution_deadline_at=execution_deadline_at,
            lifecycle=_CONFIRMED,
            anomaly_generations=(),
            healthy_generations=(),
            snapshot_generation_watermark=(
                None
                if previous is None
                else previous.snapshot_generation_watermark
            ),
            consecutive_observations=(
                1 if previous is None else previous.consecutive_observations + 1
            ),
            confirmation_evidence_class=policy.confirmation_evidence_class,
            resolution_evidence_class=None,
        )
        return _confirmed(state, policy, "CONFIRMED_IMMEDIATE")
    if (
        previous is not None
        and previous.lifecycle == _CONFIRMED
        and candidate.durable_terminal_fact
    ):
        state = _new_or_updated_state(
            candidate,
            previous,
            observed_at=observed_at,
            last_progress_at=previous.last_progress_at,
            execution_deadline_at=previous.execution_deadline_at,
            lifecycle=_RESOLVED,
            anomaly_generations=(),
            healthy_generations=(),
            snapshot_generation_watermark=previous.snapshot_generation_watermark,
            consecutive_observations=1,
            confirmation_evidence_class=previous.confirmation_evidence_class,
            resolution_evidence_class="DURABLE_TERMINAL",
        )
        return _healthy(state, "DURABLE_TERMINAL")
    if previous is not None and previous.lifecycle == _CONFIRMED:
        return _confirmed(previous, policy, "DURABLE_TERMINAL_REQUIRED")
    state = _new_or_updated_state(
        candidate,
        previous,
        observed_at=observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        lifecycle=_RESOLVED,
        anomaly_generations=(),
        healthy_generations=(),
        snapshot_generation_watermark=(
            None
            if previous is None
            else previous.snapshot_generation_watermark
        ),
        consecutive_observations=1,
        confirmation_evidence_class=None,
        resolution_evidence_class="COMPLETE_DURABLE_ABSENCE",
    )
    return _healthy(state, "NO_IMMEDIATE_ANOMALY")


def _validate_snapshot(
    candidate: CandidateObservation,
    *,
    now: datetime,
    observed_at: datetime,
    last_progress_at: datetime | None,
    execution_deadline_at: datetime,
    snapshot_generation_watermark: int | None,
) -> str | None:
    generation = candidate.snapshot_generation
    if type(generation) is not int or generation < 0:
        return "SNAPSHOT_GENERATION_INVALID"
    try:
        started_at = _aware_utc(candidate.snapshot_started_at)
        completed_at = _aware_utc(candidate.snapshot_completed_at)
    except ValueError:
        return "SNAPSHOT_TIMESTAMP_INVALID"
    if started_at > now or completed_at > now:
        return "FUTURE_TIMESTAMP"
    if started_at > completed_at:
        return "SNAPSHOT_TIMESTAMP_ORDER_INVALID"
    if completed_at > observed_at:
        return "SNAPSHOT_AFTER_OBSERVATION"
    if last_progress_at is None:
        return "LAST_PROGRESS_MISSING"
    if started_at <= last_progress_at:
        return "SNAPSHOT_NOT_AFTER_PROGRESS"
    if started_at < execution_deadline_at:
        return "SNAPSHOT_BEFORE_DURABLE_DEADLINE"
    if started_at == execution_deadline_at:
        return "SNAPSHOT_NOT_AFTER_DURABLE_DEADLINE"
    if snapshot_generation_watermark is None:
        return None
    if generation < snapshot_generation_watermark:
        return "SNAPSHOT_GENERATION_OUT_OF_ORDER"
    if generation == snapshot_generation_watermark:
        return "SNAPSHOT_GENERATION_REPEATED"
    return None


def _matching_previous(
    candidate: CandidateObservation,
    previous: CandidateState | None,
) -> CandidateState | None:
    if previous is None:
        return None
    if (
        previous.reason_code != candidate.reason_code
        or previous.fingerprint != candidate.fingerprint
    ):
        return None
    return previous


def _new_or_updated_state(
    candidate: CandidateObservation,
    previous: CandidateState | None,
    *,
    observed_at: datetime,
    last_progress_at: datetime | None,
    execution_deadline_at: datetime | None,
    lifecycle: str,
    anomaly_generations: tuple[int, ...],
    healthy_generations: tuple[int, ...],
    snapshot_generation_watermark: int | None,
    consecutive_observations: int,
    confirmation_evidence_class: str | None,
    resolution_evidence_class: str | None,
) -> CandidateState:
    return CandidateState(
        reason_code=candidate.reason_code,
        fingerprint=candidate.fingerprint,
        first_observed_at=(
            observed_at if previous is None else previous.first_observed_at
        ),
        last_observed_at=observed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        earliest_confirmation_at=execution_deadline_at,
        anomaly_generations=anomaly_generations[-_MAX_TRACKED_GENERATIONS:],
        healthy_generations=healthy_generations[-_MAX_TRACKED_GENERATIONS:],
        snapshot_generation_watermark=snapshot_generation_watermark,
        consecutive_observations=consecutive_observations,
        last_observation_anomalous=candidate.anomaly_present,
        lifecycle=lifecycle,
        confirmation_evidence_class=confirmation_evidence_class,
        resolution_evidence_class=resolution_evidence_class,
    )


def _next_generation_run(
    existing: tuple[int, ...],
    generation: int,
) -> tuple[int, ...]:
    return (*existing, generation)[-_MAX_TRACKED_GENERATIONS:]


def _confirmed(
    state: CandidateState,
    policy: ReasonPolicy,
    decision_reason: str,
) -> CandidateClassification:
    return CandidateClassification(
        observed_health="UNHEALTHY",
        incident_eligible=policy.incident_eligible,
        deployment_blocking=True,
        candidate_state=state,
        decision_reason=decision_reason,
    )


def _healthy(
    state: CandidateState,
    decision_reason: str,
) -> CandidateClassification:
    return CandidateClassification(
        observed_health="HEALTHY",
        incident_eligible=False,
        deployment_blocking=False,
        candidate_state=state,
        decision_reason=decision_reason,
    )


def _unknown(
    previous: CandidateState | None,
    decision_reason: str,
) -> CandidateClassification:
    if previous is not None and previous.lifecycle == _CONFIRMED:
        return CandidateClassification(
            observed_health="UNHEALTHY",
            incident_eligible=True,
            deployment_blocking=True,
            candidate_state=previous,
            decision_reason=decision_reason,
        )
    return CandidateClassification(
        observed_health="UNKNOWN",
        incident_eligible=False,
        deployment_blocking=True,
        candidate_state=previous,
        decision_reason=decision_reason,
    )


def _aware_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _optional_aware_utc(value: object) -> datetime | None:
    return None if value is None else _aware_utc(value)
