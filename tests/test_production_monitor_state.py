import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import telegram_kol_research.production_monitor_state as state_module
from telegram_kol_research.production_monitor_policy import (
    CandidateContext,
    CandidateObservation,
    CandidateState,
    classify_candidate,
)
from telegram_kol_research.production_monitor_state import (
    DEFAULT_SENTINEL_STATE_PATH,
    MONITOR_STATE_SCHEMA_VERSION,
    FallbackDeliveryState,
    IncidentAcceptanceState,
    LatestCompletedResult,
    ProductionMonitorState,
    ProductionMonitorStateStore,
)


NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)


def _save_with_lease(store, state):
    with store.single_flight() as lease:
        assert lease.acquired is True
        return lease.save(state)


def _candidate(*, fingerprint="a" * 64, generations=(7, 8)):
    return CandidateState(
        reason_code="composite_position_without_verified_stop",
        fingerprint=fingerprint,
        first_observed_at=NOW - timedelta(minutes=2),
        last_observed_at=NOW - timedelta(minutes=1),
        last_progress_at=NOW - timedelta(minutes=10),
        execution_deadline_at=NOW - timedelta(minutes=5),
        earliest_confirmation_at=NOW - timedelta(minutes=5),
        anomaly_generations=generations,
        healthy_generations=(),
        snapshot_generation_watermark=max(generations, default=None),
        consecutive_observations=2,
        last_observation_anomalous=True,
        lifecycle="CONFIRMED",
        confirmation_evidence_class=(
            "TWO_DISTINCT_COMPLETE_POST_PROGRESS_GENERATIONS"
        ),
        resolution_evidence_class=None,
    )


def _state():
    return ProductionMonitorState(
        candidates=(_candidate(),),
        incident_acceptances=(
            IncidentAcceptanceState(
                candidate_fingerprint="a" * 64,
                submission_id="b" * 64,
                accepted_at=NOW - timedelta(seconds=30),
            ),
        ),
        fallback=FallbackDeliveryState(
            fingerprint="c" * 64,
            status="PENDING",
            attempts=1,
            last_attempt_at=NOW - timedelta(seconds=20),
            next_attempt_at=NOW + timedelta(minutes=5),
        ),
        latest_completed_result=LatestCompletedResult(
            checked_at=NOW - timedelta(seconds=10),
            execution_status="COMPLETED",
            observed_health="UNHEALTHY",
            reason_codes=("composite_position_without_verified_stop",),
            adapter_failures=(),
            evidence_complete=True,
            state_fingerprint="d" * 64,
        ),
        audit_cursor="2026-08-14",
    )


def test_state_path_is_v2_and_not_legacy_compatible():
    assert DEFAULT_SENTINEL_STATE_PATH == (
        "/var/lib/telegram-kol-monitor/sentinel-v2.json"
    )
    assert DEFAULT_SENTINEL_STATE_PATH.endswith("sentinel-v2.json")
    assert MONITOR_STATE_SCHEMA_VERSION == 2


def test_state_store_round_trips_strict_bounded_state_with_mode_0600(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)

    saved = _save_with_lease(store, _state())

    assert saved == _state()
    assert store.load() == _state()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_missing_state_loads_empty_current_schema(tmp_path):
    state = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    ).load()

    assert state == ProductionMonitorState()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "legacy_field": None},
        lambda payload: {**payload, "schema_version": 1},
        lambda payload: {**payload, "schema_version": True},
    ],
)
def test_state_store_rejects_legacy_unknown_and_non_exact_schema(tmp_path, mutation):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    _save_with_lease(store, _state())
    payload = json.loads(path.read_text())
    path.write_text(json.dumps(mutation(payload)))
    path.chmod(0o600)

    with pytest.raises(ValueError, match="state"):
        store.load()


def test_state_store_rejects_duplicate_json_fields(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    path.write_bytes(b'{"schema_version":2,"schema_version":2}')
    path.chmod(0o600)

    with pytest.raises(ValueError, match="state"):
        ProductionMonitorStateStore(path, now_factory=lambda: NOW).load()


def test_state_store_rejects_oversized_file_before_decoding(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    path.write_bytes(b"x" * (state_module.MONITOR_STATE_MAX_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(ValueError, match="size"):
        ProductionMonitorStateStore(path, now_factory=lambda: NOW).load()


def test_state_store_refuses_target_and_parent_symlinks(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    linked_state = tmp_path / "sentinel-v2.json"
    linked_state.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        ProductionMonitorStateStore(linked_state, now_factory=lambda: NOW).load()
    with pytest.raises(ValueError, match="symlink"):
        _save_with_lease(
            ProductionMonitorStateStore(linked_state, now_factory=lambda: NOW),
            _state(),
        )

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ProductionMonitorStateStore(
            linked_parent / "sentinel-v2.json", now_factory=lambda: NOW
        ).load()


def test_state_store_rejects_wrong_file_mode(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    _save_with_lease(store, _state())
    path.chmod(0o644)

    with pytest.raises(ValueError, match="0600"):
        store.load()


def test_state_store_bounds_candidate_count(tmp_path):
    candidates = tuple(
        _candidate(fingerprint=f"{ordinal:064x}")
        for ordinal in range(state_module.MONITOR_STATE_MAX_CANDIDATES + 1)
    )

    with pytest.raises(ValueError, match="candidate"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=candidates),
        )


@pytest.mark.parametrize("generations", [(8, 7), (7, 7), (7, 8, 9)])
def test_state_store_rejects_out_of_order_repeated_or_unbounded_generations(
    tmp_path, generations
):
    with pytest.raises(ValueError, match="generation"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(
                candidates=(_candidate(generations=generations),)
            ),
        )


@pytest.mark.parametrize("watermark", [True, -1, "8"])
def test_state_store_rejects_invalid_snapshot_generation_watermark(
    tmp_path, watermark
):
    with pytest.raises(ValueError, match="generation watermark"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(
                candidates=(
                    replace(
                        _candidate(),
                        snapshot_generation_watermark=watermark,
                    ),
                )
            ),
        )


def test_state_store_rejects_watermark_below_generation_evidence(tmp_path):
    with pytest.raises(ValueError, match="generation watermark"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(
                candidates=(
                    replace(
                        _candidate(),
                        snapshot_generation_watermark=7,
                    ),
                )
            ),
        )


def test_state_store_rejects_future_candidate_time(tmp_path):
    future = replace(_candidate(), last_observed_at=NOW + timedelta(seconds=1))

    with pytest.raises(ValueError, match="future"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(future,)),
        )


def test_atomic_replace_failure_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    original = _state()
    _save_with_lease(store, original)
    changed = replace(original, audit_cursor="2026-08-15")

    monkeypatch.setattr(
        state_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        _save_with_lease(store, changed)
    assert store.load() == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_single_flight_lease_covers_load_evaluate_persist_cycle(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    first = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    second = ProductionMonitorStateStore(path, now_factory=lambda: NOW)

    with first.single_flight() as active:
        assert active.acquired is True
        assert active.load() == ProductionMonitorState()
        with second.single_flight() as overlapping:
            assert overlapping.acquired is False
            with pytest.raises(RuntimeError, match="not acquired"):
                overlapping.load()
        active.save(_state())

    with second.single_flight() as later:
        assert later.acquired is True
        assert later.load() == _state()


def test_public_save_refuses_without_this_store_active_lease(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    first = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    second = ProductionMonitorStateStore(path, now_factory=lambda: NOW)

    with pytest.raises(RuntimeError, match="single-flight lease"):
        first.save(_state())
    with first.single_flight() as active:
        assert active.acquired is True
        with pytest.raises(RuntimeError, match="lease.save"):
            first.save(_state())
        with pytest.raises(RuntimeError, match="single-flight lease"):
            second.save(_state())
        active.save(_state())


def test_same_store_nested_lease_cannot_borrow_active_owner(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    with store.single_flight() as active:
        assert active.acquired is True
        with store.single_flight() as nested:
            assert nested.acquired is False
            with pytest.raises(RuntimeError, match="not acquired"):
                nested.save(_state())
        active.save(_state())


def test_other_thread_cannot_borrow_active_lease_capability(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    with store.single_flight() as active:
        assert active.acquired is True
        with ThreadPoolExecutor(max_workers=1) as executor:
            error = executor.submit(active.save, _state()).exception()
        assert isinstance(error, RuntimeError)
        assert "owner thread" in str(error)
        active.save(_state())


def test_private_authoritative_save_rejects_wrong_lease_capability(tmp_path):
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    with store.single_flight() as active:
        assert active.acquired is True
        with pytest.raises(RuntimeError, match="capability"):
            store._save_authoritative(_state(), capability=object())
        active.save(_state())


def test_single_flight_excludes_another_process_for_complete_transition(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    script = """
import sys
from telegram_kol_research.production_monitor_state import ProductionMonitorStateStore
with ProductionMonitorStateStore(sys.argv[1]).single_flight() as lease:
    print("acquired" if lease.acquired else "overlap")
"""

    with store.single_flight() as active:
        assert active.acquired is True
        child = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert child.stdout.strip() == "overlap"
        active.save(_state())


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            execution_deadline_at=None,
            earliest_confirmation_at=NOW - timedelta(minutes=5),
        ),
        replace(
            _candidate(),
            execution_deadline_at=NOW - timedelta(minutes=11),
            earliest_confirmation_at=NOW - timedelta(minutes=11),
        ),
    ],
)
def test_state_rejects_inconsistent_deadline_authority(tmp_path, candidate):
    with pytest.raises(ValueError, match="deadline"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            execution_deadline_at=None,
            earliest_confirmation_at=None,
        ),
        replace(_candidate(), last_progress_at=None),
        replace(
            _candidate(),
            execution_deadline_at=None,
            earliest_confirmation_at=None,
            last_progress_at=None,
        ),
    ],
)
def test_state_rejects_confirmed_settling_candidate_without_authority(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="authority"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            execution_deadline_at=None,
            earliest_confirmation_at=None,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            last_progress_at=None,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class="DURABLE_TERMINAL",
            execution_deadline_at=None,
            earliest_confirmation_at=None,
            last_progress_at=None,
        ),
    ],
)
def test_state_rejects_resolved_settling_candidate_without_authority(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="authority"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            anomaly_generations=(),
            healthy_generations=(9,),
            snapshot_generation_watermark=9,
            consecutive_observations=1,
            last_observation_anomalous=False,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class="DURABLE_TERMINAL",
            consecutive_observations=1,
            last_observation_anomalous=False,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class=(
                "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
            ),
            healthy_generations=(9, 10),
            snapshot_generation_watermark=10,
            last_observation_anomalous=False,
        ),
    ],
)
def test_state_round_trips_resolved_settling_candidate_with_authority(
    tmp_path, candidate
):
    state = ProductionMonitorState(candidates=(candidate,))
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    assert _save_with_lease(store, state) == state
    assert store.load() == state


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            anomaly_generations=(),
            healthy_generations=(),
            consecutive_observations=1,
            last_observation_anomalous=False,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            anomaly_generations=(),
            healthy_generations=(9, 10),
            snapshot_generation_watermark=10,
            consecutive_observations=2,
            last_observation_anomalous=False,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class=(
                "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
            ),
            healthy_generations=(),
            last_observation_anomalous=False,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class=(
                "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
            ),
            healthy_generations=(9,),
            snapshot_generation_watermark=9,
            consecutive_observations=1,
            last_observation_anomalous=False,
        ),
    ],
)
def test_state_rejects_resolution_evidence_without_exact_generation_facts(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="generation evidence"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            execution_deadline_at=NOW,
            earliest_confirmation_at=NOW,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
            execution_deadline_at=NOW,
            earliest_confirmation_at=NOW,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class=(
                "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
            ),
            execution_deadline_at=NOW,
            earliest_confirmation_at=NOW,
        ),
    ],
)
def test_state_rejects_settled_candidate_before_its_deadline(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="deadline"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


def test_classifier_settled_states_round_trip_deadline_boundary(tmp_path):
    def observation(
        *,
        at,
        generation,
        anomaly_present=True,
        durable_terminal_fact=False,
    ):
        return CandidateObservation(
            reason_code="composite_position_without_verified_stop",
            fingerprint="e" * 64,
            observed_at=at,
            anomaly_present=anomaly_present,
            evidence_complete=True,
            snapshot_generation=generation,
            snapshot_started_at=None if generation is None else at,
            snapshot_completed_at=None if generation is None else at,
            last_progress_at=NOW - timedelta(minutes=10),
            execution_deadline_at=NOW - timedelta(minutes=5),
            durable_terminal_fact=durable_terminal_fact,
        )

    first = classify_candidate(
        observation(at=NOW - timedelta(minutes=2), generation=7),
        CandidateContext(now=NOW - timedelta(minutes=2)),
    )
    confirmed = classify_candidate(
        observation(at=NOW - timedelta(minutes=1), generation=8),
        CandidateContext(
            now=NOW - timedelta(minutes=1),
            previous=first.candidate_state,
        ),
    )
    cleared = classify_candidate(
        observation(
            at=NOW - timedelta(minutes=2),
            generation=7,
            anomaly_present=False,
        ),
        CandidateContext(now=NOW - timedelta(minutes=2)),
    )
    terminal = classify_candidate(
        observation(
            at=NOW,
            generation=None,
            anomaly_present=False,
            durable_terminal_fact=True,
        ),
        CandidateContext(now=NOW, previous=confirmed.candidate_state),
    )
    one_healthy = classify_candidate(
        observation(
            at=NOW - timedelta(seconds=30),
            generation=9,
            anomaly_present=False,
        ),
        CandidateContext(
            now=NOW - timedelta(seconds=30),
            previous=confirmed.candidate_state,
        ),
    )
    recovered = classify_candidate(
        observation(at=NOW, generation=10, anomaly_present=False),
        CandidateContext(now=NOW, previous=one_healthy.candidate_state),
    )

    for ordinal, result in enumerate(
        (confirmed, cleared, terminal, recovered)
    ):
        assert result.candidate_state.lifecycle in {"CONFIRMED", "RESOLVED"}
        state = ProductionMonitorState(candidates=(result.candidate_state,))
        store = ProductionMonitorStateStore(
            tmp_path / f"sentinel-v2-{ordinal}.json",
            now_factory=lambda: NOW,
        )
        assert _save_with_lease(store, state) == state
        assert store.load() == state


def test_confirmed_bad_healthy_bad_flap_round_trips_sticky_state(tmp_path):
    def observe(previous, *, at, generation, anomaly_present=True):
        return classify_candidate(
            CandidateObservation(
                reason_code="composite_position_without_verified_stop",
                fingerprint="f" * 64,
                observed_at=at,
                anomaly_present=anomaly_present,
                evidence_complete=True,
                snapshot_generation=generation,
                snapshot_started_at=at,
                snapshot_completed_at=at,
                last_progress_at=NOW - timedelta(minutes=10),
                execution_deadline_at=NOW - timedelta(minutes=5),
            ),
            CandidateContext(now=at, previous=previous),
        )

    first = observe(None, at=NOW - timedelta(minutes=4), generation=7)
    confirmed = observe(
        first.candidate_state,
        at=NOW - timedelta(minutes=3),
        generation=8,
    )
    one_healthy = observe(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=9,
        anomaly_present=False,
    )
    bad_again = observe(
        one_healthy.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=10,
    )
    one_healthy_again = observe(
        bad_again.candidate_state,
        at=NOW - timedelta(seconds=30),
        generation=11,
        anomaly_present=False,
    )
    recovered = observe(
        one_healthy_again.candidate_state,
        at=NOW,
        generation=12,
        anomaly_present=False,
    )

    assert bad_again.candidate_state.lifecycle == "CONFIRMED"
    assert bad_again.candidate_state.anomaly_generations == (10,)
    assert one_healthy_again.candidate_state.lifecycle == "CONFIRMED"
    assert recovered.candidate_state.lifecycle == "RESOLVED"
    for ordinal, result in enumerate((bad_again, one_healthy_again, recovered)):
        state = ProductionMonitorState(candidates=(result.candidate_state,))
        store = ProductionMonitorStateStore(
            tmp_path / f"sentinel-v2-{ordinal}.json", now_factory=lambda: NOW
        )
        assert _save_with_lease(store, state) == state
        assert store.load() == state


def test_loaded_state_preserves_snapshot_generation_watermark_fence(tmp_path):
    def observe(previous, *, at, generation, anomaly_present=True):
        return classify_candidate(
            CandidateObservation(
                reason_code="composite_position_without_verified_stop",
                fingerprint="1" * 64,
                observed_at=at,
                anomaly_present=anomaly_present,
                evidence_complete=True,
                snapshot_generation=generation,
                snapshot_started_at=at,
                snapshot_completed_at=at,
                last_progress_at=NOW - timedelta(minutes=10),
                execution_deadline_at=NOW - timedelta(minutes=5),
            ),
            CandidateContext(now=at, previous=previous),
        )

    first = observe(None, at=NOW - timedelta(minutes=3), generation=7)
    confirmed = observe(
        first.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=8,
    )
    one_healthy = observe(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=9,
        anomaly_present=False,
    )
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )
    _save_with_lease(
        store,
        ProductionMonitorState(candidates=(one_healthy.candidate_state,)),
    )

    loaded = store.load().candidates[0]
    repeated = observe(loaded, at=NOW, generation=9, anomaly_present=False)

    assert loaded.snapshot_generation_watermark == 9
    assert repeated.observed_health == "UNHEALTHY"
    assert repeated.candidate_state == loaded
    assert repeated.decision_reason == "SNAPSHOT_GENERATION_REPEATED"


def test_confirmed_sticky_state_round_trips_after_authority_evidence_reset(tmp_path):
    candidate = replace(
        _candidate(),
        first_observed_at=NOW - timedelta(minutes=1),
        last_observed_at=NOW - timedelta(minutes=1),
        last_progress_at=NOW - timedelta(minutes=2),
        execution_deadline_at=NOW + timedelta(minutes=5),
        earliest_confirmation_at=NOW + timedelta(minutes=5),
        anomaly_generations=(),
        healthy_generations=(),
        consecutive_observations=1,
        last_observation_anomalous=True,
    )
    state = ProductionMonitorState(candidates=(candidate,))
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    assert _save_with_lease(store, state) == state
    assert store.load() == state


def test_durable_terminal_after_future_authority_rebase_round_trips(tmp_path):
    future_deadline = NOW + timedelta(minutes=5)
    rebased = replace(
        _candidate(),
        first_observed_at=NOW - timedelta(minutes=1),
        last_observed_at=NOW - timedelta(minutes=1),
        last_progress_at=NOW - timedelta(minutes=2),
        execution_deadline_at=future_deadline,
        earliest_confirmation_at=future_deadline,
        anomaly_generations=(),
        healthy_generations=(),
        consecutive_observations=1,
        last_observation_anomalous=True,
    )
    terminal = classify_candidate(
        CandidateObservation(
            reason_code=rebased.reason_code,
            fingerprint=rebased.fingerprint,
            observed_at=NOW,
            anomaly_present=False,
            evidence_complete=True,
            snapshot_generation=None,
            snapshot_started_at=None,
            snapshot_completed_at=None,
            last_progress_at=rebased.last_progress_at,
            execution_deadline_at=future_deadline,
            durable_terminal_fact=True,
        ),
        CandidateContext(now=NOW, previous=rebased),
    )
    state = ProductionMonitorState(candidates=(terminal.candidate_state,))
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
    )

    assert terminal.observed_health == "HEALTHY"
    assert terminal.candidate_state.lifecycle == "RESOLVED"
    assert _save_with_lease(store, state) == state
    assert store.load() == state


@pytest.mark.parametrize(
    "candidate",
    [
        replace(_candidate(), confirmation_evidence_class="FAKE"),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class="FAKE",
        ),
    ],
)
def test_state_rejects_unregistered_evidence_classes(tmp_path, candidate):
    with pytest.raises(ValueError, match="evidence"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            reason_code="event_recovery_status",
            confirmation_evidence_class=(
                "TWO_DISTINCT_COMPLETE_POST_PROGRESS_GENERATIONS"
            ),
        ),
        replace(
            _candidate(),
            confirmation_evidence_class="DURABLE_FACT",
        ),
        replace(
            _candidate(),
            reason_code="adapter_failure",
            confirmation_evidence_class="DURABLE_FACT",
        ),
    ],
)
def test_state_rejects_confirmation_evidence_from_wrong_reason_policy(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="policy"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            _candidate(),
            reason_code="event_recovery_status",
            lifecycle="SETTLING",
            confirmation_evidence_class=None,
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="DURABLE_TERMINAL",
        ),
        replace(
            _candidate(),
            lifecycle="RESOLVED",
            resolution_evidence_class="COMPLETE_HEALTHY_GENERATION",
        ),
        replace(
            _candidate(),
            reason_code="adapter_failure",
            lifecycle="RESOLVED",
            confirmation_evidence_class=None,
            resolution_evidence_class="COMPLETE_DURABLE_ABSENCE",
        ),
    ],
)
def test_state_rejects_impossible_policy_lifecycle_evidence_combinations(
    tmp_path, candidate
):
    with pytest.raises(ValueError, match="policy"):
        _save_with_lease(
            ProductionMonitorStateStore(
                tmp_path / "sentinel-v2.json", now_factory=lambda: NOW
            ),
            ProductionMonitorState(candidates=(candidate,)),
        )


def test_single_flight_refuses_replaced_lock_inode(tmp_path, monkeypatch):
    path = tmp_path / "sentinel-v2.json"
    store = ProductionMonitorStateStore(path, now_factory=lambda: NOW)
    original_flock = state_module.fcntl.flock

    def replace_lock_after_flock(descriptor, operation):
        original_flock(descriptor, operation)
        lock_path = store.single_flight_path
        lock_path.unlink()
        lock_path.write_text("")
        lock_path.chmod(0o600)

    monkeypatch.setattr(state_module.fcntl, "flock", replace_lock_after_flock)

    with pytest.raises(ValueError, match="replaced"):
        with store.single_flight():
            pass


def test_state_file_contains_only_the_closed_top_level_schema(tmp_path):
    path = tmp_path / "sentinel-v2.json"
    _save_with_lease(
        ProductionMonitorStateStore(path, now_factory=lambda: NOW),
        _state(),
    )

    assert set(json.loads(path.read_text())) == {
        "schema_version",
        "candidates",
        "incident_acceptances",
        "fallback",
        "latest_completed_result",
        "audit_cursor",
    }
