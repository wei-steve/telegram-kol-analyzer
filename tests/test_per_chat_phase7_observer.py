from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "per_chat_phase7_observer.py"
)
SPEC = importlib.util.spec_from_file_location("per_chat_phase7_observer", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)


OLD_LIMIT_APPLIED_AT = "2026-08-26T08:18:25.030388+00:00"
NEW_LIMIT_APPLIED_AT = "2026-08-26T08:55:48.021057+00:00"


def job(job_id, raw_message_id, chat_id, status, completed_at=None):
    return observer.JobObservation(
        job_id=job_id,
        raw_message_id=raw_message_id,
        chat_id=chat_id,
        status=status,
        completed_at=completed_at,
    )


def test_later_same_chat_pending_behind_claim_is_valid():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "claimed"), job(2, 11, 7, "pending")]
    )

    assert result.violations == ()


def test_two_claimed_jobs_in_one_chat_are_overlap():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "claimed"), job(2, 11, 7, "claimed")]
    )

    assert [row.code for row in result.violations] == [
        "same_chat_multiple_claims"
    ]


def test_claimed_successor_behind_older_pending_job_is_out_of_order():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "pending"), job(2, 11, 7, "claimed")]
    )

    assert [row.code for row in result.violations] == [
        "same_chat_out_of_order_claim"
    ]


def test_completed_at_is_not_used_as_processing_overlap_boundary():
    misleading = datetime(2026, 8, 26, 9, 40, tzinfo=UTC)
    result = observer.evaluate_same_chat_jobs(
        [
            job(1, 10, 7, "succeeded", completed_at=misleading),
            job(2, 11, 7, "claimed", completed_at=misleading),
        ]
    )

    assert result.violations == ()


def runtime(
    *,
    elapsed=0.25,
    lock_mode="per_chat",
    cap=3,
    new_limit=True,
    complete=True,
    pids=None,
    active_lanes=0,
    peak_lanes=0,
):
    state = observer.ExpectedRuntimeState(lock_mode, cap, "queue", "queue")
    return observer.RuntimeObservation(
        elapsed_seconds=elapsed,
        complete=complete,
        database_state=state if complete else None,
        api_state=state if complete else None,
        pids=pids or observer.AuthorityPids(101, 102, 103),
        worker_role="worker" if complete else None,
        worker_cap=cap if complete else None,
        active_lanes=active_lanes if complete else None,
        peak_lanes=peak_lanes if complete else None,
        limit_applied_at=(
            NEW_LIMIT_APPLIED_AT if new_limit else OLD_LIMIT_APPLIED_AT
        ),
    )


def convergence_tracker():
    return observer.ConvergenceTracker(
        target=observer.ExpectedRuntimeState("per_chat", 3, "queue", "queue"),
        expected_pids=observer.AuthorityPids(101, 102, 103),
        required_consecutive=3,
        deadline_seconds=5.0,
        previous_limit_applied_at=OLD_LIMIT_APPLIED_AT,
    )


def test_cutover_requires_three_consecutive_complete_samples():
    tracker = convergence_tracker()

    assert tracker.observe(runtime(elapsed=0.25)).passed is False
    assert tracker.observe(runtime(elapsed=0.50)).passed is False
    assert tracker.observe(runtime(elapsed=0.75)).passed is True


def test_cutover_mismatch_resets_streak_without_extending_deadline():
    tracker = convergence_tracker()
    tracker.observe(runtime(elapsed=0.25))

    result = tracker.observe(runtime(elapsed=0.50, cap=1, new_limit=False))

    assert result.consecutive == 0
    assert tracker.deadline_seconds == 5.0


def test_cutover_deadline_is_fixed_and_cannot_be_extended():
    tracker = convergence_tracker()

    result = tracker.observe(runtime(elapsed=5.001))

    assert result.failed is True
    assert result.reason == "convergence_deadline_exceeded"
    assert tracker.deadline_seconds == 5.0


def test_rollback_confirmation_ignores_prior_acceptance_failure():
    rollback = observer.RollbackConvergenceTracker(
        target=observer.ExpectedRuntimeState("global", 1, "queue", "queue"),
        expected_pids=observer.AuthorityPids(101, 102, 103),
        required_consecutive=1,
    )

    result = rollback.observe(
        runtime(lock_mode="global", cap=1, new_limit=False)
    )

    assert result.passed is True


def acceptance_tracker():
    return observer.AcceptanceTracker(
        expected_state=observer.ExpectedRuntimeState(
            "per_chat", 3, "queue", "queue"
        ),
        expected_pids=observer.AuthorityPids(101, 102, 103),
    )


def acceptance_snapshot(
    *,
    jobs=None,
    raw_count=1,
    chat_count=1,
    active_lanes=0,
    peak_lanes=0,
    pending_count=0,
    claimed_count=0,
    complete=True,
    runtime_sample=None,
    **overrides,
):
    values = {
        "runtime": runtime_sample
        or runtime(
            active_lanes=active_lanes,
            peak_lanes=peak_lanes,
            complete=complete,
        ),
        "jobs": tuple(jobs or ()),
        "raw_message_count": raw_count,
        "distinct_chat_count": chat_count,
        "pending_count": pending_count,
        "claimed_count": claimed_count,
        "duplicate_job_count": 0,
        "missing_job_count": 0,
        "orphan_job_count": 0,
        "stuck_job_count": 0,
        "sqlite_lock_count": 0,
        "loop_stall_count": 0,
        "session_conflict_count": 0,
        "deepseek_402_count": 0,
        "ingest_anomaly_count": 0,
        "execution_anomaly_count": 0,
    }
    values.update(overrides)
    return observer.AcceptanceObservation(**values)


def test_pending_same_chat_successor_does_not_fail_acceptance():
    tracker = acceptance_tracker()

    result = tracker.observe(
        acceptance_snapshot(
            jobs=[job(1, 10, 7, "claimed"), job(2, 11, 7, "pending")],
            active_lanes=1,
            peak_lanes=1,
            pending_count=1,
            claimed_count=1,
        )
    )

    assert result.failed is False


def test_same_chat_double_claim_fails_with_scheduler_l2_rollback():
    tracker = acceptance_tracker()

    result = tracker.observe(
        acceptance_snapshot(
            jobs=[job(1, 10, 7, "claimed"), job(2, 11, 7, "claimed")],
            active_lanes=2,
            peak_lanes=2,
            claimed_count=2,
        )
    )

    assert result.reason == "same_chat_multiple_claims"
    assert result.rollback_target == observer.ExpectedRuntimeState(
        "global", 1, "queue", "queue"
    )


def test_two_claimed_chats_establish_cross_chat_progress():
    tracker = acceptance_tracker()

    result = tracker.observe(
        acceptance_snapshot(
            jobs=[job(1, 10, 7, "claimed"), job(2, 11, 8, "claimed")],
            active_lanes=2,
            peak_lanes=2,
            claimed_count=2,
            chat_count=2,
        )
    )

    assert result.failed is False
    assert tracker.cross_chat_progress is True


def test_acceptance_finalize_never_waives_traffic_or_peak():
    tracker = acceptance_tracker()
    tracker.observe(
        acceptance_snapshot(raw_count=3, chat_count=2, peak_lanes=1)
    )

    result = tracker.finalize()

    assert result.failed is True
    assert result.reason == "acceptance_minimum_not_met"


def test_acceptance_finalize_passes_complete_minimums():
    tracker = acceptance_tracker()
    tracker.observe(
        acceptance_snapshot(
            jobs=[job(1, 10, 7, "claimed"), job(2, 11, 8, "claimed")],
            raw_count=5,
            chat_count=2,
            active_lanes=2,
            peak_lanes=2,
            claimed_count=2,
        )
    )
    tracker.observe(
        acceptance_snapshot(
            raw_count=5,
            chat_count=2,
            active_lanes=0,
            peak_lanes=2,
        )
    )

    result = tracker.finalize()

    assert result.passed is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"duplicate_job_count": 1}, "duplicate_job_identity"),
        ({"missing_job_count": 1}, "missing_job_identity"),
        ({"orphan_job_count": 1}, "orphan_job_identity"),
        ({"stuck_job_count": 1}, "stuck_message_job"),
        ({"sqlite_lock_count": 1}, "sqlite_lock"),
        ({"loop_stall_count": 1}, "event_loop_stall"),
        ({"session_conflict_count": 1}, "telegram_session_conflict"),
        ({"deepseek_402_count": 1}, "deepseek_402"),
        ({"execution_anomaly_count": 1}, "execution_anomaly"),
    ],
)
def test_acceptance_fails_closed_on_pipeline_anomaly(overrides, reason):
    result = acceptance_tracker().observe(acceptance_snapshot(**overrides))

    assert result.failed is True
    assert result.reason == reason
    assert result.rollback_target.max_parallel_chats == 1


def test_ingest_anomaly_uses_global_cap_three_rollback():
    result = acceptance_tracker().observe(
        acceptance_snapshot(ingest_anomaly_count=1)
    )

    assert result.reason == "ingest_anomaly"
    assert result.rollback_target == observer.ExpectedRuntimeState(
        "global", 3, "queue", "queue"
    )


def test_tuple_drift_fails_acceptance():
    drifted = runtime(lock_mode="global", cap=3)

    result = acceptance_tracker().observe(
        acceptance_snapshot(runtime_sample=drifted)
    )

    assert result.reason == "runtime_state_drift"


def test_pid_drift_fails_acceptance():
    drifted = runtime(pids=observer.AuthorityPids(101, 999, 103))

    result = acceptance_tracker().observe(
        acceptance_snapshot(runtime_sample=drifted)
    )

    assert result.reason == "authority_pid_drift"


def test_peak_above_three_fails_acceptance():
    excessive = runtime(cap=3, active_lanes=3, peak_lanes=4)

    result = acceptance_tracker().observe(
        acceptance_snapshot(runtime_sample=excessive)
    )

    assert result.reason == "worker_lane_bounds_invalid"


def test_second_incomplete_acceptance_sample_fails_closed():
    tracker = acceptance_tracker()
    incomplete = acceptance_snapshot(complete=False)

    first = tracker.observe(incomplete)
    second = tracker.observe(incomplete)

    assert first.failed is False
    assert first.reason == "observer_incomplete_retry"
    assert second.failed is True
    assert second.reason == "observer_incomplete"
