import ast
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import telegram_kol_research.production_monitor_policy as policy_module
from telegram_kol_research.production_monitor_contract import (
    MONITOR_PROJECTION_V2_FIELDS,
    parse_monitor_projection,
)
from telegram_kol_research.production_monitor_policy import CandidateObservation
from telegram_kol_research.production_monitor_sentinel import (
    SentinelObservation,
    SentinelResult,
    SentinelRunOutcome,
    collect_sentinel_observation,
    evaluate_sentinel_observation,
    run_production_monitor_sentinel,
)
from telegram_kol_research.production_monitor_state import (
    LatestCompletedResult,
    ProductionMonitorState,
    ProductionMonitorStateStore,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _candidate(
    *,
    reason="audit_abnormal",
    fingerprint="a" * 64,
    anomaly_present=True,
    evidence_complete=True,
    generation=None,
    deadline=None,
    last_progress=None,
):
    return CandidateObservation(
        reason_code=reason,
        fingerprint=fingerprint,
        observed_at=NOW,
        anomaly_present=anomaly_present,
        evidence_complete=evidence_complete,
        snapshot_generation=generation,
        snapshot_started_at=(
            NOW - timedelta(seconds=10) if generation is not None else None
        ),
        snapshot_completed_at=(NOW if generation is not None else None),
        last_progress_at=last_progress,
        execution_deadline_at=deadline,
    )


def _observation(*candidates, adapter_failures=()):
    return SentinelObservation(
        checked_at=NOW,
        candidates=tuple(candidates),
        adapter_failures=tuple(adapter_failures),
    )


def _settling_candidate(*, generation=1, fingerprint="b" * 64):
    return _candidate(
        reason="composite_position_without_verified_stop",
        fingerprint=fingerprint,
        generation=generation,
        last_progress=NOW - timedelta(minutes=2),
        deadline=NOW + timedelta(minutes=3),
    )


def _confirmed_candidates(fingerprint="b" * 64):
    common = {
        "reason": "composite_position_without_verified_stop",
        "fingerprint": fingerprint,
        "last_progress": NOW - timedelta(minutes=10),
        "deadline": NOW - timedelta(minutes=5),
    }
    first = _candidate(generation=1, **common)
    second = replace(
        _candidate(generation=2, **common),
        observed_at=NOW + timedelta(seconds=1),
        snapshot_started_at=NOW + timedelta(milliseconds=100),
        snapshot_completed_at=NOW + timedelta(seconds=1),
    )
    return first, second


def test_sentinel_public_values_are_frozen():
    result = SentinelResult(
        observation_generation=1,
        checked_at=NOW,
        execution_status="COMPLETED",
        observed_health="HEALTHY",
        reason_codes=(),
        adapter_failures=(),
        evidence_complete=True,
        state_fingerprint="a" * 64,
        candidate_states=(),
        incident_projection=None,
    )
    outcome = SentinelRunOutcome(
        execution_status="COMPLETED",
        observed_health="HEALTHY",
        observation_generation=1,
        persisted=True,
        exit_code=0,
        failure_code=None,
        result=result,
    )

    for value, attribute, replacement in (
        (_observation(), "checked_at", NOW + timedelta(seconds=1)),
        (result, "execution_status", "FAILED"),
        (outcome, "execution_status", "FAILED"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, replacement)


@pytest.mark.parametrize(
    ("observation", "health"),
    [
        (_observation(), "HEALTHY"),
        (_observation(_candidate()), "UNHEALTHY"),
        (_observation(_settling_candidate()), "UNKNOWN"),
        (
            _observation(
                _candidate(reason="service_starting", fingerprint="c" * 64)
            ),
            "UNKNOWN",
        ),
        (
            _observation(
                _candidate(reason="future_reason", fingerprint="d" * 64)
            ),
            "UNKNOWN",
        ),
        (_observation(adapter_failures=("readiness",)), "UNKNOWN"),
        (
            _observation(
                _candidate(),
                _settling_candidate(),
                adapter_failures=("readiness",),
            ),
            "UNHEALTHY",
        ),
    ],
)
def test_pure_evaluation_uses_closed_health_priority(observation, health):
    result, _next_state = evaluate_sentinel_observation(
        observation=observation,
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert result.execution_status == "COMPLETED"
    assert result.observed_health == health


def test_immediate_confirmed_with_adapter_failure_never_projects():
    result, _state = evaluate_sentinel_observation(
        observation=_observation(
            _candidate(), adapter_failures=("readiness",)
        ),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.evidence_complete is False
    assert result.incident_projection is None


def test_settling_starting_unknown_and_adapter_failures_never_project():
    observations = (
        _observation(_settling_candidate()),
        _observation(
            _candidate(reason="service_starting", fingerprint="c" * 64)
        ),
        _observation(
            _candidate(reason="future_reason", fingerprint="d" * 64)
        ),
        _observation(adapter_failures=("readiness",)),
    )

    for observation in observations:
        result, _state = evaluate_sentinel_observation(
            observation=observation,
            previous_state=ProductionMonitorState(),
            observation_generation=1,
        )
        assert result.incident_projection is None
        assert result.observed_health == "UNKNOWN"


def test_multiple_confirmed_candidates_build_exactly_one_canonical_projection():
    first_a, second_a = _confirmed_candidates("a" * 64)
    first_b, second_b = _confirmed_candidates("b" * 64)
    first_result, first_state = evaluate_sentinel_observation(
        observation=_observation(first_a, first_b),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    result, _state = evaluate_sentinel_observation(
        observation=replace(
            _observation(second_a, second_b),
            checked_at=NOW + timedelta(seconds=1),
        ),
        previous_state=first_state,
        observation_generation=2,
    )

    assert first_result.incident_projection is None
    assert result.observed_health == "UNHEALTHY"
    assert result.incident_projection is not None
    projection = dict(result.incident_projection)
    assert set(projection) == MONITOR_PROJECTION_V2_FIELDS
    assert projection["observation_generation"] == 2
    assert projection["anomaly_fingerprint"] == result.anomaly_fingerprint
    assert parse_monitor_projection(projection) == projection
    assert projection["reason_codes"] == [
        "composite_position_without_verified_stop"
    ]


@pytest.mark.parametrize("current_evidence", ["incomplete", "repeated"])
def test_confirmed_health_stays_unhealthy_but_current_evidence_is_not_complete(
    current_evidence,
):
    first, second = _confirmed_candidates()
    _first_result, first_state = evaluate_sentinel_observation(
        observation=_observation(first),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    _confirmed_result, confirmed_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(second),
            checked_at=NOW + timedelta(seconds=1),
        ),
        previous_state=first_state,
        observation_generation=2,
    )
    current = replace(
        second,
        observed_at=NOW + timedelta(seconds=2),
        snapshot_started_at=NOW + timedelta(seconds=1, milliseconds=100),
        snapshot_completed_at=NOW + timedelta(seconds=2),
        evidence_complete=current_evidence != "incomplete",
        snapshot_generation=(3 if current_evidence == "incomplete" else 2),
    )

    result, _state = evaluate_sentinel_observation(
        observation=replace(
            _observation(current),
            checked_at=NOW + timedelta(seconds=2),
        ),
        previous_state=confirmed_state,
        observation_generation=3,
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.evidence_complete is False


def test_unobserved_confirmed_candidate_stays_unhealthy_without_new_projection():
    first, second = _confirmed_candidates()
    _first_result, first_state = evaluate_sentinel_observation(
        observation=_observation(first),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    _confirmed_result, confirmed_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(second), checked_at=NOW + timedelta(seconds=1)
        ),
        previous_state=first_state,
        observation_generation=2,
    )

    result, _next_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(), checked_at=NOW + timedelta(seconds=2)
        ),
        previous_state=confirmed_state,
        observation_generation=3,
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.evidence_complete is False
    assert result.incident_projection is None


def test_resolved_candidate_absence_does_not_make_complete_empty_run_incomplete():
    _confirmed, confirmed_state = evaluate_sentinel_observation(
        observation=_observation(_candidate()),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    terminal = replace(
        _candidate(anomaly_present=False),
        observed_at=NOW + timedelta(seconds=1),
        durable_terminal_fact=True,
    )
    _resolved, resolved_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(terminal),
            checked_at=NOW + timedelta(seconds=1),
        ),
        previous_state=confirmed_state,
        observation_generation=2,
    )

    result, next_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(), checked_at=NOW + timedelta(seconds=2)
        ),
        previous_state=resolved_state,
        observation_generation=3,
    )

    assert result.observed_health == "HEALTHY"
    assert result.evidence_complete is True
    assert next_state.candidates == ()


@pytest.mark.parametrize("offset", [-1, 0])
def test_checked_at_rollback_or_repeat_persists_unknown_next_generation(offset):
    previous = ProductionMonitorState(
        latest_completed_result=LatestCompletedResult(
            observation_generation=1,
            checked_at=NOW,
            checked_at_high_watermark=NOW,
            execution_status="COMPLETED",
            observed_health="UNKNOWN",
            reason_codes=("adapter_failure",),
            adapter_failures=("readiness",),
            evidence_complete=False,
            state_fingerprint="f" * 64,
        )
    )
    checked_at = NOW + timedelta(seconds=offset)

    result, next_state = evaluate_sentinel_observation(
        observation=SentinelObservation(
            checked_at=checked_at,
            candidates=(),
            adapter_failures=(),
        ),
        previous_state=previous,
        observation_generation=2,
    )

    assert result.execution_status == "COMPLETED"
    assert result.observed_health == "UNKNOWN"
    assert result.evidence_complete is False
    assert result.reason_codes == ("monitor_clock_rollback",)
    assert result.incident_projection is None
    assert next_state.latest_completed_result.observation_generation == 2
    assert next_state.latest_completed_result.checked_at == checked_at


def test_runner_atomically_persists_clock_rollback_as_completed_unknown(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: NOW + timedelta(seconds=1),
    )
    previous = ProductionMonitorState(
        latest_completed_result=LatestCompletedResult(
            observation_generation=1,
            checked_at=NOW,
            checked_at_high_watermark=NOW,
            execution_status="COMPLETED",
            observed_health="UNKNOWN",
            reason_codes=("adapter_failure",),
            adapter_failures=("readiness",),
            evidence_complete=False,
            state_fingerprint="f" * 64,
        )
    )
    with store.single_flight() as lease:
        assert lease.acquired is True
        lease.save(previous)

    outcome = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=NOW - timedelta(seconds=1),
            candidates=(),
            adapter_failures=(),
        ),
    )

    persisted = store.load().latest_completed_result
    assert outcome.exit_code == 0
    assert outcome.execution_status == "COMPLETED"
    assert outcome.observed_health == "UNKNOWN"
    assert outcome.observation_generation == 2
    assert persisted.observation_generation == 2
    assert persisted.reason_codes == ("monitor_clock_rollback",)


def test_runner_tolerates_real_clock_rollback_before_state_load(tmp_path):
    wall_clock = [NOW]
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: wall_clock[0]
    )
    previous = ProductionMonitorState(
        latest_completed_result=LatestCompletedResult(
            observation_generation=1,
            checked_at=NOW,
            checked_at_high_watermark=NOW,
            execution_status="COMPLETED",
            observed_health="UNKNOWN",
            reason_codes=("adapter_failure",),
            adapter_failures=("readiness",),
            evidence_complete=False,
            state_fingerprint="f" * 64,
        )
    )
    with store.single_flight() as lease:
        assert lease.acquired is True
        lease.save(previous)
    wall_clock[0] = NOW - timedelta(seconds=2)

    outcome = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=wall_clock[0], candidates=(), adapter_failures=()
        ),
    )

    assert outcome.exit_code == 0
    assert outcome.execution_status == "COMPLETED"
    assert outcome.observed_health == "UNKNOWN"
    assert outcome.observation_generation == 2


def test_clock_high_watermark_blocks_partial_recovery_until_original_time(tmp_path):
    wall_clock = [NOW]
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: wall_clock[0]
    )
    first = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=wall_clock[0],
            candidates=(),
            adapter_failures=("readiness",),
        ),
    )
    assert first.observed_health == "UNKNOWN"

    wall_clock[0] = NOW - timedelta(seconds=2)
    rolled_back = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=wall_clock[0], candidates=(), adapter_failures=()
        ),
    )
    wall_clock[0] = NOW - timedelta(seconds=1)
    partial_recovery = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=wall_clock[0], candidates=(), adapter_failures=()
        ),
    )
    wall_clock[0] = NOW + timedelta(seconds=1)
    recovered = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: SentinelObservation(
            checked_at=wall_clock[0], candidates=(), adapter_failures=()
        ),
    )

    assert rolled_back.execution_status == "COMPLETED"
    assert partial_recovery.execution_status == "COMPLETED"
    assert partial_recovery.observed_health == "UNKNOWN"
    assert partial_recovery.result.reason_codes == ("monitor_clock_rollback",)
    assert recovered.execution_status == "COMPLETED"
    assert recovered.observed_health == "HEALTHY"
    assert recovered.observation_generation == 4


def test_confirmed_future_deadline_is_incomplete_and_not_projected():
    first, second = _confirmed_candidates()
    _first_result, first_state = evaluate_sentinel_observation(
        observation=_observation(first),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )
    _confirmed_result, confirmed_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(second),
            checked_at=NOW + timedelta(seconds=1),
        ),
        previous_state=first_state,
        observation_generation=2,
    )
    changed_authority = replace(
        second,
        observed_at=NOW + timedelta(seconds=2),
        last_progress_at=NOW + timedelta(seconds=1, milliseconds=100),
        execution_deadline_at=NOW + timedelta(minutes=5),
        snapshot_generation=3,
        snapshot_started_at=NOW + timedelta(seconds=1, milliseconds=200),
        snapshot_completed_at=NOW + timedelta(seconds=2),
    )

    result, next_state = evaluate_sentinel_observation(
        observation=replace(
            _observation(changed_authority),
            checked_at=NOW + timedelta(seconds=2),
        ),
        previous_state=confirmed_state,
        observation_generation=3,
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.evidence_complete is False
    assert result.incident_projection is None
    assert next_state.candidates[0].lifecycle == "CONFIRMED"


@pytest.mark.parametrize(
    ("health", "persist_succeeds", "exit_code"),
    [
        ("HEALTHY", True, 0),
        ("UNHEALTHY", True, 0),
        ("UNKNOWN", True, 0),
        ("UNKNOWN", False, 1),
    ],
)
def test_exit_code_describes_execution_not_business_health(
    tmp_path, monkeypatch, health, persist_succeeds, exit_code
):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: NOW + timedelta(seconds=2),
    )
    observations = {
        "HEALTHY": _observation(),
        "UNHEALTHY": _observation(_candidate()),
        "UNKNOWN": _observation(adapter_failures=("readiness",)),
    }
    if not persist_succeeds:
        monkeypatch.setattr(
            store,
            "_save_authoritative",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("secret")),
        )

    outcome = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: observations[health],
    )

    assert outcome.exit_code == exit_code
    assert outcome.persisted is persist_succeeds
    assert outcome.execution_status == (
        "COMPLETED" if persist_succeeds else "FAILED"
    )
    assert outcome.observed_health == health
    assert "secret" not in repr(outcome)


def test_single_flight_covers_load_collect_evaluate_persist_and_overlap(
    tmp_path,
):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )
    calls = []

    with store.single_flight() as active:
        assert active.acquired is True
        outcome = run_production_monitor_sentinel(
            state_store=ProductionMonitorStateStore(
                store.path, now_factory=lambda: NOW
            ),
            observation_collector=lambda: calls.append("collect"),
        )

    assert outcome.exit_code == 1
    assert outcome.execution_status == "FAILED"
    assert outcome.failure_code == "sentinel_overlap"
    assert outcome.observation_generation is None
    assert calls == []
    assert store.load().latest_completed_result is None


def test_runner_advances_generation_only_after_completed_persisted_run(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: NOW + timedelta(seconds=2),
    )

    first = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: _observation(),
    )
    second = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: replace(
            _observation(), checked_at=NOW + timedelta(seconds=1)
        ),
    )

    assert first.observation_generation == 1
    assert second.observation_generation == 2
    assert store.load().latest_completed_result.observation_generation == 2


def test_runner_persists_multiple_candidates_in_state_canonical_order(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: NOW + timedelta(seconds=1),
    )

    outcome = run_production_monitor_sentinel(
        state_store=store,
        observation_collector=lambda: _observation(
            _candidate(reason="audit_abnormal", fingerprint="b" * 64),
            _candidate(reason="service_inactive", fingerprint="a" * 64),
        ),
    )

    assert outcome.exit_code == 0
    assert outcome.persisted is True
    assert [item.fingerprint for item in store.load().candidates] == [
        "a" * 64,
        "b" * 64,
    ]


def test_adapter_collector_converts_raw_errors_to_sanitized_closed_facts():
    def failed():
        raise RuntimeError("credential=secret")

    observation = collect_sentinel_observation(
        checked_at=NOW,
        expected_adapters=frozenset({"service", "readiness"}),
        adapter_collectors={"service": lambda: (), "readiness": failed},
    )

    assert observation.adapter_failures == ("readiness",)
    assert observation.candidates == ()
    assert "secret" not in repr(observation)


def test_adapter_collector_marks_every_missing_expected_source_unavailable():
    observation = collect_sentinel_observation(
        checked_at=NOW,
        expected_adapters=frozenset({"service", "readiness"}),
        adapter_collectors={"service": lambda: ()},
    )

    result, _state = evaluate_sentinel_observation(
        observation=observation,
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert observation.adapter_failures == ("readiness",)
    assert result.observed_health == "UNKNOWN"
    assert result.evidence_complete is False


def test_adapter_collector_canonicalizes_mixed_missing_and_failed_sources():
    def failed():
        raise RuntimeError("private detail")

    observation = collect_sentinel_observation(
        checked_at=NOW,
        expected_adapters=frozenset({"readiness", "service"}),
        adapter_collectors={"readiness": failed},
    )

    result, _state = evaluate_sentinel_observation(
        observation=observation,
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert observation.adapter_failures == ("readiness", "service")
    assert result.observed_health == "UNKNOWN"


def test_oversized_adapter_output_is_discarded_as_one_closed_adapter_failure():
    oversized = tuple(
        _candidate(fingerprint=f"{ordinal:064x}") for ordinal in range(129)
    )
    observation = collect_sentinel_observation(
        checked_at=NOW,
        expected_adapters=frozenset({"service"}),
        adapter_collectors={"service": lambda: oversized},
    )
    result, _state = evaluate_sentinel_observation(
        observation=observation,
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert observation.candidates == ()
    assert observation.adapter_failures == ("service",)
    assert result.execution_status == "COMPLETED"
    assert result.observed_health == "UNKNOWN"


@pytest.mark.parametrize("generation", [2, 3, 5])
def test_pure_transition_requires_exact_next_observation_generation(generation):
    previous = ProductionMonitorState(
        latest_completed_result=LatestCompletedResult(
            observation_generation=3,
            checked_at=NOW - timedelta(seconds=1),
            checked_at_high_watermark=NOW - timedelta(seconds=1),
            execution_status="COMPLETED",
            observed_health="HEALTHY",
            reason_codes=(),
            adapter_failures=(),
            evidence_complete=True,
            state_fingerprint="f" * 64,
        )
    )

    with pytest.raises(ValueError, match="exact next"):
        evaluate_sentinel_observation(
            observation=_observation(),
            previous_state=previous,
            observation_generation=generation,
        )


def test_confirmed_but_policy_ineligible_candidate_never_projects(monkeypatch):
    monkeypatch.setitem(
        policy_module._REASON_POLICIES,
        "audit_abnormal",
        replace(
            policy_module.REASON_POLICIES["audit_abnormal"],
            incident_eligible=False,
        ),
    )

    result, _state = evaluate_sentinel_observation(
        observation=_observation(_candidate()),
        previous_state=ProductionMonitorState(),
        observation_generation=1,
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.incident_projection is None


def test_sentinel_module_has_no_legacy_or_dangerous_runtime_reachability():
    module_path = (
        Path(__file__).parents[1]
        / "src"
        / "telegram_kol_research"
        / "production_monitor_sentinel.py"
    )
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "telegram_kol_research.production_safety_monitor" not in imported_modules
    assert "telegram_kol_research.live_position_snapshot" not in imported_modules
    assert "telegram_kol_research.db" not in imported_modules
    assert "telegram_kol_research.runtime_incident_adapters" not in imported_modules
    for prohibited in (
        "notify",
        "telegram_bot",
        "send_message",
        "create_session_factory",
        "build_deepcoin_client",
        "fifteen",
        "15 * 60",
        "httpx",
    ):
        assert prohibited not in source.lower()
