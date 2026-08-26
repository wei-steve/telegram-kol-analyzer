from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys


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
