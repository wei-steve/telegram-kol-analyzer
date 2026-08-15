"""Cross-component temporal race matrix for production monitor v2.

The cases deliberately use the public policy, refresher, sentinel, readiness,
and routing boundaries.  The small helpers below only assemble typed inputs and
assert the three independent operator axes; they do not reproduce policy logic.
"""

from __future__ import annotations

import select
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.production_monitor_contract import (
    build_monitor_projection,
)
from telegram_kol_research.production_monitor_facts import (
    MonitorReadinessEvidence,
    build_readiness_candidates,
)
from telegram_kol_research.production_monitor_notifications import (
    MONITOR_NOTIFICATION_SLA,
    MonitorAcceptance,
    MonitorIntakeError,
    route_monitor_incident,
)
from telegram_kol_research.production_monitor_policy import (
    CandidateContext,
    CandidateObservation,
    classify_candidate,
)
from telegram_kol_research.production_monitor_refresher import (
    ReadOnlyDeepcoinMonitorClient,
    refresh_production_monitor_snapshot,
)
from telegram_kol_research.production_monitor_sentinel import (
    SentinelObservation,
    evaluate_sentinel_observation,
    run_production_monitor_sentinel,
)
from telegram_kol_research.production_monitor_snapshot import (
    ProductionMonitorSnapshotStore,
)
from telegram_kol_research.production_monitor_state import (
    ProductionMonitorState,
    ProductionMonitorStateStore,
)


BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    current: datetime = BASE

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta) -> datetime:
        self.current += timedelta(**delta)
        return self.current


def _candidate(
    clock: FakeClock,
    *,
    reason: str = "stalled_composite_component",
    fingerprint: str = "a" * 64,
    anomaly: bool = True,
    evidence_complete: bool = True,
    generation: int | None = 1,
    snapshot_started_at: datetime | None = None,
    snapshot_completed_at: datetime | None = None,
    progress_at: datetime | None = None,
    deadline_at: datetime | None = None,
    durable_terminal: bool = False,
) -> CandidateObservation:
    now = clock.now()
    if generation is not None:
        snapshot_started_at = snapshot_started_at or now - timedelta(seconds=2)
        snapshot_completed_at = snapshot_completed_at or now - timedelta(seconds=1)
    return CandidateObservation(
        reason_code=reason,
        fingerprint=fingerprint,
        observed_at=now,
        anomaly_present=anomaly,
        evidence_complete=evidence_complete,
        snapshot_generation=generation,
        snapshot_started_at=snapshot_started_at,
        snapshot_completed_at=snapshot_completed_at,
        last_progress_at=progress_at,
        execution_deadline_at=deadline_at,
        durable_terminal_fact=durable_terminal,
    )


def _settling_candidate(clock: FakeClock, **overrides) -> CandidateObservation:
    values = {
        "progress_at": clock.now() - timedelta(minutes=10),
        "deadline_at": clock.now() - timedelta(minutes=5),
    }
    values.update(overrides)
    return _candidate(clock, **values)


def _classify(candidate, *, clock, previous=None):
    return classify_candidate(
        candidate,
        CandidateContext(now=clock.now(), previous=previous),
    )


def _assert_candidate_axes(
    result,
    *,
    deployment_blocking: bool,
    incident_eligible: bool,
    notification_eligible: bool,
    agent_eligible: bool,
) -> None:
    assert result.deployment_blocking is deployment_blocking
    assert result.incident_eligible is incident_eligible
    projection = None
    state = result.candidate_state
    if result.incident_eligible and result.evidence_complete and state is not None:
        projection = build_monitor_projection(
            {
                "checked_at": state.last_observed_at,
                "observation_generation": 1,
                "anomaly_fingerprint": state.fingerprint,
                "execution_status": "COMPLETED",
                "observed_health": "UNHEALTHY",
                "reason_codes": [state.reason_code],
                "adapter_failures": [],
                "fallback_reason": None,
            }
        )
    notification_status, agent_status = _route_channel_statuses(projection)
    assert (notification_status is not None) is notification_eligible
    assert (agent_status is not None) is agent_eligible


def _assert_sentinel_axes(
    result,
    *,
    deployment_blocking: bool,
    notification_eligible: bool,
    agent_eligible: bool,
) -> None:
    actual_deployment_blocking = not (
        result.execution_status == "COMPLETED"
        and result.observed_health == "HEALTHY"
        and result.evidence_complete
    )
    assert actual_deployment_blocking is deployment_blocking
    notification_status, agent_status = _route_channel_statuses(
        result.incident_projection
    )
    assert (notification_status is not None) is notification_eligible
    assert (agent_status is not None) is agent_eligible


def _assert_snapshot_axes(
    outcome,
    manifest,
    *,
    deployment_blocking: bool,
    notification_eligible: bool,
    agent_eligible: bool,
) -> None:
    exchange_evidence_blocks = not (
        outcome.execution_status == "COMPLETED"
        and outcome.snapshot_outcome == "SUCCESS"
        and manifest.last_success is not None
    )
    assert exchange_evidence_blocks is deployment_blocking
    # The public refresher outcome has no notification or Agent queue surface.
    assert hasattr(outcome, "notification_status") is notification_eligible
    assert hasattr(outcome, "agent_status") is agent_eligible


def _assert_routing_axes(
    projection,
    outcome,
    acceptance,
    *,
    deployment_blocking: bool,
    notification_eligible: bool,
    agent_eligible: bool,
) -> None:
    assert (projection["observed_health"] != "HEALTHY") is deployment_blocking
    notification_owned = bool(
        outcome.accepted
        and acceptance.notification_status
        in {"pending", "delivering", "delivered", "failed", "exhausted"}
    )
    agent_owned = bool(
        outcome.accepted
        and acceptance.agent_status
        in {
            "pending",
            "claimed",
            "diagnosed",
            "retry_pending",
            "escalated",
            "resolved",
            "closed",
            "timed_out",
        }
    )
    assert notification_owned is notification_eligible
    assert agent_owned is agent_eligible


def _route_channel_statuses(projection):
    if projection is None:
        return None, None
    checked_at = datetime.fromisoformat(projection["checked_at"])
    acceptance = MonitorAcceptance(
        submission_id=projection["submission_id"],
        accepted_at=checked_at,
        notification_status="pending",
        notification_claimed_at=None,
        notification_claim_expires_at=None,
        notification_failed_at=None,
        agent_status="pending",
    )
    fallback_calls = []
    outcome = route_monitor_incident(
        projection=projection,
        previous_state=ProductionMonitorState(),
        now=checked_at,
        submit=lambda _projection: acceptance,
        recheck=lambda _projection: acceptance,
        deliver_fallback=fallback_calls.append,
    )
    assert outcome.accepted is True
    assert fallback_calls == []
    return acceptance.notification_status, acceptance.agent_status


def _assert_run_axes(
    outcome,
    *,
    deployment_blocking: bool,
    notification_eligible: bool,
    agent_eligible: bool,
) -> None:
    actual_deployment_blocking = not (
        outcome.execution_status == "COMPLETED"
        and outcome.observed_health == "HEALTHY"
        and outcome.persisted
    )
    notification_status, agent_status = _route_channel_statuses(
        None if outcome.result is None else outcome.result.incident_projection
    )
    assert actual_deployment_blocking is deployment_blocking
    assert (notification_status is not None) is notification_eligible
    assert (agent_status is not None) is agent_eligible


@pytest.mark.parametrize(
    "race_name",
    [
        "submit_readback_delay",
        "cancellation_visible_after_acceptance",
        "partial_fill_staggered_position_update",
        "partial_fill_staggered_protection_update",
    ],
)
def test_exchange_eventual_consistency_waits_for_durable_deadline(race_name):
    clock = FakeClock()
    scenario_reason = {
        "submit_readback_delay": "stalled_composite_component",
        "cancellation_visible_after_acceptance": (
            "completed_batch_missing_component_evidence"
        ),
        "partial_fill_staggered_position_update": (
            "composite_position_without_verified_stop"
        ),
        "partial_fill_staggered_protection_update": (
            "live_entry_revision_protection_unverified"
        ),
    }[race_name]
    candidate = _settling_candidate(
        clock,
        reason=scenario_reason,
        fingerprint={
            name: chr(ord("a") + index) * 64
            for index, name in enumerate(
                (
                    "submit_readback_delay",
                    "cancellation_visible_after_acceptance",
                    "partial_fill_staggered_position_update",
                    "partial_fill_staggered_protection_update",
                )
            )
        }[race_name],
        progress_at=clock.now() - timedelta(minutes=1),
        deadline_at=clock.now() + timedelta(minutes=4),
    )

    result = _classify(candidate, clock=clock)

    assert result.observed_health == "UNKNOWN"
    assert result.decision_reason == "BEFORE_DURABLE_DEADLINE"
    _assert_candidate_axes(
        result,
        deployment_blocking=True,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_snapshot_captured_before_local_progress_is_temporally_incoherent():
    clock = FakeClock()
    progress = clock.now() - timedelta(minutes=3)
    result = _classify(
        _settling_candidate(
            clock,
            progress_at=progress,
            deadline_at=clock.now() - timedelta(minutes=2),
            snapshot_started_at=progress - timedelta(seconds=2),
            snapshot_completed_at=progress - timedelta(seconds=1),
        ),
        clock=clock,
    )

    assert result.decision_reason == "SNAPSHOT_NOT_AFTER_PROGRESS"
    _assert_candidate_axes(
        result,
        deployment_blocking=True,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_one_generation_cannot_be_counted_twice_but_two_bad_generations_confirm():
    clock = FakeClock()
    progress = clock.now() - timedelta(minutes=10)
    deadline = clock.now() - timedelta(minutes=5)
    first = _classify(
        _settling_candidate(
            clock, generation=7, progress_at=progress, deadline_at=deadline
        ),
        clock=clock,
    )
    clock.advance(seconds=1)
    repeated = _classify(
        _settling_candidate(
            clock, generation=7, progress_at=progress, deadline_at=deadline
        ),
        clock=clock,
        previous=first.candidate_state,
    )
    _assert_candidate_axes(
        repeated,
        deployment_blocking=True,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )
    assert repeated.decision_reason == "SNAPSHOT_GENERATION_REPEATED"

    clock.advance(seconds=1)
    distinct = _classify(
        _settling_candidate(
            clock, generation=8, progress_at=progress, deadline_at=deadline
        ),
        clock=clock,
        previous=repeated.candidate_state,
    )
    assert distinct.candidate_state.anomaly_generations == (7, 8)
    _assert_candidate_axes(
        distinct,
        deployment_blocking=True,
        incident_eligible=True,
        notification_eligible=True,
        agent_eligible=True,
    )


def test_first_bad_then_good_converges_without_notification_or_agent_work():
    clock = FakeClock()
    bad = _classify(_settling_candidate(clock, generation=7), clock=clock)
    clock.advance(seconds=1)
    good = _classify(
        _settling_candidate(clock, generation=8, anomaly=False),
        clock=clock,
        previous=bad.candidate_state,
    )

    assert good.candidate_state.lifecycle == "RESOLVED"
    _assert_candidate_axes(
        good,
        deployment_blocking=False,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


class _ReadClient:
    uid_scope_hash = "d" * 64

    def __init__(self, *, response=None, failure=None):
        self.response = {"data": []} if response is None else response
        self.failure = failure
        self.calls = []

    @contextmanager
    def request_scope(self, scope):
        yield self

    def _read(self, name):
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        return self.response

    def read_positions(self, *, inst_id=None):
        return self._read("positions")

    def read_open_orders(self, *, inst_id=None):
        return self._read("open_orders")

    def read_trigger_orders_pending(self, *, inst_id):
        return self._read("pending_trigger_orders")


def _refresh(tmp_path, client, *, now=BASE, store=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = store or ProductionMonitorSnapshotStore(
        tmp_path / "manifest.json", now_factory=lambda: now
    )
    outcome = refresh_production_monitor_snapshot(
        client=ReadOnlyDeepcoinMonitorClient(client),
        store=store,
        now=now,
    )
    return outcome, store.load()


def test_incomplete_pagination_blocks_while_complete_empty_lists_are_authoritative(
    tmp_path,
):
    incomplete, incomplete_manifest = _refresh(
        tmp_path / "incomplete",
        _ReadClient(response={"data": [], "hasMore": True}),
    )
    assert incomplete.failure_code == "snapshot_pagination_incomplete"
    assert incomplete_manifest.last_success is None
    _assert_snapshot_axes(
        incomplete,
        incomplete_manifest,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )

    empty, empty_manifest = _refresh(tmp_path / "empty", _ReadClient())
    assert empty.snapshot_outcome == "SUCCESS"
    assert empty_manifest.last_success is not None
    assert all(
        collection.complete and collection.row_count == 0
        for collection in empty_manifest.last_success.collections
    )
    _assert_snapshot_axes(
        empty,
        empty_manifest,
        deployment_blocking=False,
        notification_eligible=False,
        agent_eligible=False,
    )


@pytest.mark.parametrize(
    ("failure", "failure_code"),
    [
        (TimeoutError("secret timeout"), "exchange_timeout"),
        (
            RuntimeError("rate limited"),
            "snapshot_read_unavailable",
        ),
    ],
)
def test_exchange_timeout_or_rate_limit_is_unknown_until_a_later_recovery(
    tmp_path, failure, failure_code
):
    store = ProductionMonitorSnapshotStore(
        tmp_path / failure_code / "manifest.json",
        now_factory=lambda: BASE + timedelta(seconds=1),
    )
    failed, failed_manifest = _refresh(
        tmp_path / failure_code,
        _ReadClient(failure=failure),
        store=store,
    )
    assert failed.failure_code == failure_code
    assert failed_manifest.last_success is None
    _assert_snapshot_axes(
        failed,
        failed_manifest,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )

    recovered, recovered_manifest = _refresh(
        tmp_path / failure_code,
        _ReadClient(),
        now=BASE + timedelta(seconds=1),
        store=store,
    )
    assert recovered.snapshot_outcome == "SUCCESS"
    assert recovered_manifest.last_success is not None
    assert recovered.generation == failed.generation + 1
    _assert_snapshot_axes(
        recovered,
        recovered_manifest,
        deployment_blocking=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_exchange_rate_limit_safe_code_is_closed_and_recoverable(tmp_path):
    rate_limited = RuntimeError("provider detail must not escape")
    rate_limited.fact = SimpleNamespace(safe_code="api_rate_limited")
    failed, manifest = _refresh(tmp_path / "rate", _ReadClient(failure=rate_limited))

    assert failed.failure_code == "exchange_rate_limited"
    _assert_snapshot_axes(
        failed,
        manifest,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_startup_without_first_successful_worker_cycles_stays_starting():
    clock = FakeClock()
    evidence = MonitorReadinessEvidence(
        service_generation="e" * 64,
        deepcoin_reconcile_first_success_at=None,
        deepcoin_reconcile_last_success_at=None,
        management_worker_last_success_at=None,
        message_supervisor_last_success_at=None,
        message_supervisor_policy_status="valid",
    )
    active = next(
        item
        for item in build_readiness_candidates(evidence, now=clock.now())
        if item.anomaly_present
    )
    result = _classify(active, clock=clock)

    assert active.reason_code == "service_starting"
    _assert_candidate_axes(
        result,
        deployment_blocking=True,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def _projection():
    return build_monitor_projection(
        {
            "checked_at": BASE,
            "observation_generation": 3,
            "anomaly_fingerprint": "f" * 64,
            "execution_status": "COMPLETED",
            "observed_health": "UNHEALTHY",
            "reason_codes": ["audit_abnormal"],
            "adapter_failures": [],
            "fallback_reason": None,
        }
    )


def _acceptance(*, notification="pending", agent="pending"):
    return MonitorAcceptance(
        submission_id=_projection()["submission_id"],
        accepted_at=BASE,
        notification_status=notification,
        notification_claimed_at=None,
        notification_claim_expires_at=None,
        notification_failed_at=(BASE if notification == "failed" else None),
        agent_status=agent,
    )


def test_incident_commit_with_lost_http_response_rechecks_without_fallback():
    calls = []
    acceptance = _acceptance()

    def lost_response(_projection):
        calls.append("commit_then_timeout")
        raise MonitorIntakeError("transport_unavailable")

    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=BASE,
        submit=lost_response,
        recheck=lambda _projection: calls.append("readback") or acceptance,
        deliver_fallback=lambda _message: pytest.fail("fallback is premature"),
    )

    assert calls == ["commit_then_timeout", "readback"]
    assert outcome.accepted is True
    assert outcome.fallback_status is None
    _assert_routing_axes(
        _projection(),
        outcome,
        acceptance,
        deployment_blocking=True,
        notification_eligible=True,
        agent_eligible=True,
    )


@pytest.mark.parametrize("agent_status", ["pending", "claimed", "retry_pending"])
def test_normal_agent_queueing_never_becomes_notification_failure(agent_status):
    delivered = []
    acceptance = _acceptance(notification="delivered", agent=agent_status)
    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=BASE + MONITOR_NOTIFICATION_SLA + timedelta(hours=1),
        submit=lambda _projection: acceptance,
        recheck=lambda _projection: acceptance,
        deliver_fallback=delivered.append,
    )

    assert outcome.accepted is True
    assert delivered == []
    assert outcome.fallback_status is None
    _assert_routing_axes(
        _projection(),
        outcome,
        acceptance,
        deployment_blocking=True,
        notification_eligible=True,
        agent_eligible=True,
    )
    assert acceptance.agent_status == agent_status


def test_expired_notification_sla_falls_back_but_agent_timeout_does_not():
    delivered = []
    pending = _acceptance(notification="pending", agent="timed_out")
    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=BASE,
        submit=lambda _projection: pending,
        recheck=lambda _projection: pending,
        deliver_fallback=delivered.append,
    )
    expired = route_monitor_incident(
        projection=_projection(),
        previous_state=accepted.state,
        now=BASE + MONITOR_NOTIFICATION_SLA,
        submit=lambda _projection: pending,
        recheck=lambda _projection: pending,
        deliver_fallback=delivered.append,
    )

    assert expired.accepted is True
    assert expired.fallback_reason == "deterministic_notification_unavailable"
    assert expired.fallback_status == "DELIVERED"
    assert len(delivered) == 1
    _assert_routing_axes(
        _projection(),
        expired,
        pending,
        deployment_blocking=True,
        notification_eligible=True,
        agent_eligible=True,
    )
    assert pending.agent_status == "timed_out"


def _healthy_observation(clock: FakeClock) -> SentinelObservation:
    return SentinelObservation(checked_at=clock.now(), candidates=())


def test_timer_overlap_blocks_deployment_without_notification_or_agent(tmp_path):
    clock = FakeClock()
    store = ProductionMonitorStateStore(
        tmp_path / "state.json", now_factory=clock.now
    )
    with store.single_flight() as held:
        assert held.acquired is True
        outcome = run_production_monitor_sentinel(
            state_store=store,
            observation_collector=lambda: _healthy_observation(clock),
            incident_router=lambda _projection: pytest.fail("must not route"),
        )

    assert outcome.failure_code == "sentinel_overlap"
    assert outcome.exit_code == 1
    _assert_run_axes(
        outcome,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_collector_crash_fails_execution_closed(tmp_path):
    clock = FakeClock()
    routed = []
    outcome = run_production_monitor_sentinel(
        state_store=ProductionMonitorStateStore(
            tmp_path / "state.json", now_factory=clock.now
        ),
        observation_collector=lambda: (_ for _ in ()).throw(
            ValueError("sanitized injected crash")
        ),
        incident_router=routed.append,
    )

    assert outcome.execution_status == "FAILED"
    assert outcome.observed_health == "UNKNOWN"
    assert outcome.failure_code == "sentinel_input_invalid"
    assert routed == []
    _assert_run_axes(
        outcome,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_sigkill_preserves_last_complete_state_and_next_run_recovers(tmp_path):
    clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
    state_path = tmp_path / "state.json"
    store = ProductionMonitorStateStore(state_path, now_factory=clock.now)
    first = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: _healthy_observation(clock),
        incident_router=None,
    )
    assert first.execution_status == "COMPLETED"
    assert first.observation_generation == 1
    original_bytes = state_path.read_bytes()

    child_code = """
import os
from pathlib import Path
import signal
import sys
from telegram_kol_research.production_monitor_state import ProductionMonitorStateStore

path = Path(sys.argv[1])
store = ProductionMonitorStateStore(path)
with store.single_flight() as lease:
    if not lease.acquired:
        raise SystemExit(2)
    temporary = path.with_name(f\".{path.name}.{os.getpid()}.interrupted.tmp\")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, \"wb\") as handle:
        handle.write(b'{\"schema_version\":2,\"candidates\":[')
        handle.flush()
        os.fsync(handle.fileno())
    print(temporary, flush=True)
    signal.pause()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(state_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "killed-process fixture did not acquire the lease"
        temporary_line = process.stdout.readline().strip()
        assert temporary_line
        interrupted_temporary = Path(temporary_line)
        assert interrupted_temporary.exists()
        process.kill()
        process.wait(timeout=5)
        assert process.returncode < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    # SIGKILL cannot clean up, but an incomplete temporary document must not
    # replace the last authoritative state or hold the lease forever.
    assert interrupted_temporary.read_bytes().endswith(b"[")
    assert state_path.read_bytes() == original_bytes
    preserved = store.load()
    assert preserved.latest_completed_result is not None
    assert preserved.latest_completed_result.observation_generation == 1

    clock.advance(seconds=1)
    recovered = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: _healthy_observation(clock),
        incident_router=None,
    )
    assert recovered.execution_status == "COMPLETED"
    assert recovered.observation_generation == 2
    assert recovered.persisted is True
    _assert_run_axes(
        recovered,
        deployment_blocking=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_state_write_failure_is_execution_failure_not_business_incident(
    tmp_path, monkeypatch
):
    import telegram_kol_research.production_monitor_state as state_module

    clock = FakeClock()
    store = ProductionMonitorStateStore(
        tmp_path / "state.json", now_factory=clock.now
    )
    monkeypatch.setattr(
        state_module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    routed = []
    outcome = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: _healthy_observation(clock),
        incident_router=routed.append,
    )

    assert outcome.failure_code == "sentinel_persistence_failed"
    assert outcome.observed_health == "HEALTHY"
    assert routed == []
    _assert_run_axes(
        outcome,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_clock_rollback_and_future_snapshot_remain_unknown():
    clock = FakeClock()
    first, state = evaluate_sentinel_observation(
        observation=_healthy_observation(clock),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    _assert_sentinel_axes(
        first,
        deployment_blocking=False,
        notification_eligible=False,
        agent_eligible=False,
    )

    clock.current -= timedelta(seconds=1)
    rolled_back, _ = evaluate_sentinel_observation(
        observation=_healthy_observation(clock),
        previous_state=state,
        observation_generation=2,
    )
    assert "monitor_clock_rollback" in rolled_back.reason_codes
    _assert_sentinel_axes(
        rolled_back,
        deployment_blocking=True,
        notification_eligible=False,
        agent_eligible=False,
    )

    clock.current = BASE
    future = _classify(
        _settling_candidate(
            clock,
            snapshot_started_at=BASE + timedelta(seconds=1),
            snapshot_completed_at=BASE + timedelta(seconds=2),
        ),
        clock=clock,
    )
    assert future.decision_reason == "FUTURE_TIMESTAMP"
    _assert_candidate_axes(
        future,
        deployment_blocking=True,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


def test_confirmed_anomaly_flap_needs_two_distinct_healthy_generations():
    clock = FakeClock()
    progress = clock.now() - timedelta(minutes=10)
    deadline = clock.now() - timedelta(minutes=5)

    def observation(*, generation, anomaly=True):
        return _settling_candidate(
            clock,
            generation=generation,
            anomaly=anomaly,
            progress_at=progress,
            deadline_at=deadline,
        )

    first = _classify(observation(generation=7), clock=clock)
    clock.advance(seconds=1)
    confirmed = _classify(
        observation(generation=8),
        clock=clock,
        previous=first.candidate_state,
    )
    _assert_candidate_axes(
        confirmed,
        deployment_blocking=True,
        incident_eligible=True,
        notification_eligible=True,
        agent_eligible=True,
    )

    clock.advance(seconds=1)
    one_good = _classify(
        observation(generation=9, anomaly=False),
        clock=clock,
        previous=confirmed.candidate_state,
    )
    _assert_candidate_axes(
        one_good,
        deployment_blocking=True,
        incident_eligible=True,
        notification_eligible=True,
        agent_eligible=True,
    )

    clock.advance(seconds=1)
    bad_again = _classify(
        observation(generation=10),
        clock=clock,
        previous=one_good.candidate_state,
    )
    assert bad_again.candidate_state.healthy_generations == ()

    clock.advance(seconds=1)
    good_again = _classify(
        observation(generation=11, anomaly=False),
        clock=clock,
        previous=bad_again.candidate_state,
    )
    clock.advance(seconds=1)
    recovered = _classify(
        observation(generation=12, anomaly=False),
        clock=clock,
        previous=good_again.candidate_state,
    )
    _assert_candidate_axes(
        recovered,
        deployment_blocking=False,
        incident_eligible=False,
        notification_eligible=False,
        agent_eligible=False,
    )


@pytest.mark.parametrize(
    "reason",
    ["event_unknown_status", "event_recovery_status"],
    ids=["durable_submit_unknown", "durable_recovery_required"],
)
def test_immediate_durable_unknown_or_recovery_required_routes_immediately(reason):
    clock = FakeClock()
    result = _classify(
        _candidate(
            clock,
            reason=reason,
            generation=None,
            progress_at=None,
            deadline_at=None,
        ),
        clock=clock,
    )

    assert result.candidate_state.lifecycle == "CONFIRMED"
    _assert_candidate_axes(
        result,
        deployment_blocking=True,
        incident_eligible=True,
        notification_eligible=True,
        agent_eligible=True,
    )


def test_no_notify_shadow_runner_never_calls_router_even_for_confirmed_fact(tmp_path):
    clock = FakeClock()
    outcome = run_production_monitor_sentinel(
        state_store=ProductionMonitorStateStore(
            tmp_path / "shadow-state.json", now_factory=clock.now
        ),
        observation_collector=lambda: SentinelObservation(
            checked_at=clock.now(),
            candidates=(
                _candidate(
                    clock,
                    reason="event_unknown_status",
                    generation=None,
                ),
            ),
        ),
        incident_router=None,
    )

    assert outcome.observed_health == "UNHEALTHY"
    assert outcome.result is not None
    _assert_sentinel_axes(
        outcome.result,
        deployment_blocking=True,
        notification_eligible=True,
        agent_eligible=True,
    )
    # Eligibility is recorded, but shadow mode performs no intake, notification,
    # fallback, or Agent queue operation because no router was injected.
    assert outcome.persisted is True


def test_shadow_only_cli_disables_intake_fallback_and_agent_queue(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app

    captured = []
    collector_modes = []
    real_runner = run_production_monitor_sentinel

    def recording_runner(**kwargs):
        captured.append(kwargs["incident_router"])
        return real_runner(**kwargs)

    def collect(**kwargs):
        collector_modes.append(kwargs["bridge_policy_mode"])
        return SentinelObservation(
            checked_at=BASE,
            candidates=(
                _candidate(
                    FakeClock(),
                    reason="event_unknown_status",
                    generation=None,
                ),
            ),
        )

    monkeypatch.setattr(
        cli_module,
        "collect_production_monitor_observation",
        collect,
    )
    monkeypatch.setattr(
        cli_module,
        "run_production_monitor_sentinel",
        recording_runner,
    )
    monkeypatch.setattr(
        cli_module,
        "recheck_due_monitor_notifications_persisted",
        lambda **_kwargs: pytest.fail("shadow must not recheck notification routing"),
    )
    args = [
        "run-production-monitor-sentinel",
        "--shadow-only",
        "--state-path",
        str(tmp_path / "sentinel-v2.json"),
        "--snapshot-path",
        str(tmp_path / "snapshot.json"),
        "--database-path",
        str(tmp_path / "research.db"),
        "--checkout-path",
        str(tmp_path / "checkout"),
        "--settings-url",
        "http://127.0.0.1:8000/api/trading-settings",
        "--coverage-path",
        str(tmp_path / "coverage.json"),
        "--journal-path",
        str(tmp_path / "journal.json"),
        "--expected-head",
        "a" * 40,
        "--expected-auto-trade",
        "true",
        "--expected-position-limit",
        "4",
        "--expected-management-mode",
        "live",
        "--expected-preamble-mode",
        "live",
        "--readiness-url",
        "http://127.0.0.1:8000/api/runtime-monitor-readiness",
        "--incident-loopback-url",
        "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",
    ]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert captured == [None]
    assert collector_modes == ["shadow"]
    payload = __import__("json").loads(result.stdout)
    assert payload["execution_status"] == "COMPLETED"
    assert payload["observed_health"] == "UNHEALTHY"
    assert payload["channel_failures"] == []


def test_runbook_stages_v2_bridge_policy_watermark_and_selectors_safely():
    runbook = (
        Path(__file__).parents[1] / "docs" / "production-monitor-v2-runbook.md"
    ).read_text(encoding="utf-8")

    stage_1 = runbook.index("### Policy stage 1")
    stage_2 = runbook.index("### Policy stage 2")
    stage_3 = runbook.index("### Policy stage 3")
    stage_4 = runbook.index("### Policy stage 4")
    assert stage_1 < stage_2 < stage_3 < stage_4
    for setting in (
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES",
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES",
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID",
        "TELEGRAM_KOL_RUNTIME_AGENT_TYPES",
    ):
        assert setting in runbook
    assert "SELECT COALESCE(MAX(id), 0)" in runbook
    assert "incident_type = 'production_monitor_incident'" in runbook
    assert "systemctl restart telegram-kol.service" in runbook
    assert "systemctl is-active telegram-kol-runtime-agent.service" in runbook
    assert "systemctl is-enabled telegram-kol-runtime-agent.service" in runbook
    assert "systemctl try-restart telegram-kol-runtime-agent.service" in runbook
    assert "sudo systemctl restart telegram-kol-runtime-agent.service" not in runbook
    assert "/api/runtime-incidents/monitor-v2-bridge-readiness" in runbook
    assert "x-monitor-capture-token" in runbook
    assert "schema_version == 2" in runbook
    assert "empty POST" in runbook
    assert "bridge channel health 不得写入 `adapter_failures`" in runbook
    assert "Agent channel 不参与 `available`" in runbook
    assert "不启用 live sentinel" not in runbook
    assert "--header @-" in runbook
    assert "builtin printf" in runbook
    assert (
        '--header "x-monitor-capture-token: '
        "${TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN}"
        not in runbook
    )
    assert runbook.index("record the exclusive notification watermark") < stage_4
    assert runbook.index("zero production_monitor_incident rows") < stage_4

    unit = (
        Path(__file__).parents[1] / "deploy/systemd/telegram-kol-sentinel.service"
    ).read_text(encoding="utf-8")
    live_exec = next(
        line.removeprefix("ExecStart=")
        for line in unit.splitlines()
        if line.startswith("ExecStart=")
    )
    shadow_exec = live_exec.replace(
        "/sentinel-v2.json",
        "/shadow-sentinel-v2.json",
    ) + " --shadow-only"
    assert f"ExecStart={shadow_exec}" in runbook
