from datetime import UTC, datetime
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
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


def loop_stall_event(role, attribution):
    return observer.LoopStallEvidence(role=role, attribution=attribution)


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


@pytest.mark.parametrize("role", ["ingest", "worker"])
def test_ingest_and_worker_business_stalls_fail_closed_by_role(role):
    result = acceptance_tracker().observe(
        acceptance_snapshot(
            loop_stall_count=1,
            loop_stall_events=(
                loop_stall_event(role, "captured_business_blocker"),
            ),
        )
    )

    assert result.failed is True
    assert result.reason == f"{role}_event_loop_stall_captured_business_blocker"
    assert result.rollback_target == observer.ExpectedRuntimeState(
        "global", 1, "queue", "queue"
    )


def test_web_business_blocker_fails_closed_with_explicit_attribution():
    result = acceptance_tracker().observe(
        acceptance_snapshot(
            loop_stall_count=1,
            loop_stall_events=(
                loop_stall_event("web", "captured_business_blocker"),
            ),
        )
    )

    assert result.failed is True
    assert result.reason == "web_event_loop_stall_captured_business_blocker"


@pytest.mark.parametrize(
    ("attribution", "reason"),
    [
        (
            "loop_lag_confirmed_but_stack_unattributed",
            "web_event_loop_stall_loop_lag_confirmed_but_stack_unattributed",
        ),
        (
            "idle_or_post_recovery_selector_capture",
            "web_event_loop_stall_idle_or_post_recovery_selector_capture",
        ),
    ],
)
def test_web_unattributed_stall_fails_closed_without_scheduler_blame(
    attribution,
    reason,
):
    result = acceptance_tracker().observe(
        acceptance_snapshot(
            loop_stall_count=1,
            loop_stall_events=(loop_stall_event("web", attribution),),
        )
    )

    assert result.failed is True
    assert result.reason == reason
    assert "scheduler" not in result.reason
    assert "worker" not in result.reason
    assert result.rollback_target == observer.ExpectedRuntimeState(
        "global", 1, "queue", "queue"
    )


def test_loop_stall_count_without_role_attribution_fails_closed_as_incomplete():
    result = acceptance_tracker().observe(
        acceptance_snapshot(loop_stall_count=1)
    )

    assert result.failed is True
    assert result.reason == "loop_stall_evidence_incomplete"


def test_loop_stall_event_without_matching_count_fails_closed_as_incomplete():
    result = acceptance_tracker().observe(
        acceptance_snapshot(
            loop_stall_count=0,
            loop_stall_events=(
                loop_stall_event("web", "captured_business_blocker"),
            ),
        )
    )

    assert result.failed is True
    assert result.reason == "loop_stall_evidence_incomplete"


@pytest.mark.parametrize(
    "event",
    [
        ("unknown", "captured_business_blocker"),
        ("web", "guessed_business_function"),
    ],
)
def test_invalid_loop_stall_role_or_attribution_fails_closed(event):
    result = acceptance_tracker().observe(
        acceptance_snapshot(
            loop_stall_count=1,
            loop_stall_events=(loop_stall_event(*event),),
        )
    )

    assert result.failed is True
    assert result.reason == "loop_stall_evidence_invalid"


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


def create_observer_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE trading_settings (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            value_json TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE raw_messages (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL
        );
        CREATE TABLE message_processing_jobs (
            id INTEGER PRIMARY KEY,
            raw_message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            completed_at TEXT,
            shadow INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    settings = {
        "message_lock_mode": "per_chat",
        "message_processing_max_parallel_chats": 3,
        "message_pipeline_mode": "queue",
        "worker_command_mode": "queue",
        "semantic_review_enabled": False,
    }
    connection.execute(
        "INSERT INTO trading_settings (key, value_json) VALUES ('global', ?)",
        (json.dumps(settings),),
    )
    connection.executemany(
        "INSERT INTO raw_messages (id, chat_id) VALUES (?, ?)",
        [(100, 1), (101, 7), (102, 8)],
    )
    connection.executemany(
        """
        INSERT INTO message_processing_jobs
            (id, raw_message_id, chat_id, status, completed_at, shadow)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (10, 100, 1, "succeeded", "2026-08-26 08:00:00", 0),
            (11, 101, 7, "claimed", None, 0),
            (12, 102, 8, "pending", None, 0),
            (13, 102, 8, "pending", None, 1),
        ],
    )
    connection.commit()
    connection.close()


def test_database_connection_is_read_only_and_query_only(tmp_path):
    path = tmp_path / "observer.db"
    create_observer_database(path)

    connection = observer.open_read_only_database(path)

    assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("CREATE TABLE forbidden_write (id INTEGER)")
    connection.close()


def test_database_collector_reads_nonshadow_jobs_and_tuple(tmp_path):
    path = tmp_path / "observer.db"
    create_observer_database(path)

    result = observer.collect_database_observation(
        path,
        baseline_raw_message_id=100,
        baseline_job_id=10,
    )

    assert result.state == observer.ExpectedRuntimeState(
        "per_chat", 3, "queue", "queue"
    )
    assert result.query_only == 1
    assert result.total_changes == 0
    assert [row.job_id for row in result.jobs] == [11, 12]
    assert result.raw_message_count == 2
    assert result.distinct_chat_count == 2
    assert result.pending_count == 1
    assert result.claimed_count == 1
    assert result.duplicate_job_count == 0
    assert result.missing_job_count == 0
    assert result.orphan_job_count == 0
    assert result.semantic_review_enabled is False


def test_database_collector_fails_metrics_closed_for_identity_anomalies(tmp_path):
    path = tmp_path / "observer.db"
    create_observer_database(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO raw_messages (id, chat_id) VALUES (103, 9)")
    connection.execute(
        """
        INSERT INTO message_processing_jobs
            (id, raw_message_id, chat_id, status, shadow)
        VALUES (14, 101, 7, 'succeeded', 0)
        """
    )
    connection.execute(
        """
        INSERT INTO message_processing_jobs
            (id, raw_message_id, chat_id, status, shadow)
        VALUES (15, 999, 10, 'succeeded', 0)
        """
    )
    connection.commit()
    connection.close()

    result = observer.collect_database_observation(
        path,
        baseline_raw_message_id=100,
        baseline_job_id=10,
    )

    assert result.duplicate_job_count == 1
    assert result.missing_job_count == 1
    assert result.orphan_job_count == 1


def test_guard_counter_reader_preserves_loop_stall_role_and_attribution(tmp_path):
    path = tmp_path / "guards.json"
    payload = {
        field: 0 for field in observer.GUARD_COUNTER_FIELDS
    }
    payload["loop_stall_count"] = 1
    payload["loop_stall_events"] = [
        {
            "role": "web",
            "attribution": "idle_or_post_recovery_selector_capture",
        }
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = observer._read_guard_counters(path)

    assert result["loop_stall_count"] == 1
    assert result["loop_stall_events"] == (
        loop_stall_event("web", "idle_or_post_recovery_selector_capture"),
    )


def test_runtime_collector_reads_three_roles_with_get_reader(tmp_path):
    path = tmp_path / "observer.db"
    create_observer_database(path)
    database = observer.collect_database_observation(
        path,
        baseline_raw_message_id=100,
        baseline_job_id=10,
    )
    requested = []

    def fake_reader(url, *, timeout_seconds):
        requested.append((url, timeout_seconds))
        if url.endswith("/api/trading-settings"):
            return {
                "message_lock_mode": "per_chat",
                "message_processing_max_parallel_chats": 3,
                "message_pipeline_mode": "queue",
                "worker_command_mode": "queue",
            }
        role = {"http://ingest": "ingest", "http://worker": "worker", "http://web": "web"}[
            url.removesuffix("/api/runtime/loop-health")
        ]
        payload = {"runtime_role": role, "stall_count": 0}
        if role == "worker":
            payload.update(
                {
                    "configured_max_parallel_chats": 3,
                    "active_chat_lanes": 1,
                    "peak_active_chat_lanes_since_limit_change": 2,
                    "limit_applied_at": NEW_LIMIT_APPLIED_AT,
                }
            )
        return payload

    result = observer.collect_runtime_observation(
        database,
        elapsed_seconds=0.5,
        expected_pids=observer.AuthorityPids(101, 102, 103),
        ingest_base_url="http://ingest",
        worker_base_url="http://worker",
        web_base_url="http://web",
        timeout_seconds=1.0,
        json_reader=fake_reader,
        pid_is_alive=lambda _pid: True,
    )

    assert result.complete is True
    assert result.api_state == database.state
    assert result.worker_role == "worker"
    assert result.worker_cap == 3
    assert result.active_lanes == 1
    assert result.peak_lanes == 2
    assert result.pids == observer.AuthorityPids(101, 102, 103)
    assert len(requested) == 6


def parse_jsonl(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_convergence_orchestration_exits_zero_after_three_samples():
    output = io.StringIO()

    exit_code = observer.run_convergence_samples(
        [runtime(elapsed=0.25), runtime(elapsed=0.50), runtime(elapsed=0.75)],
        convergence_tracker(),
        output=output,
        now_provider=lambda: "2026-08-26T10:00:00Z",
    )

    rows = parse_jsonl(output)
    assert exit_code == 0
    assert [row["kind"] for row in rows] == [
        "sample",
        "sample",
        "sample",
        "convergence_passed",
    ]
    assert rows[-1]["consecutive"] == 3


def test_acceptance_orchestration_reports_failure_without_rollback_write():
    output = io.StringIO()
    failing = acceptance_snapshot(
        jobs=[job(1, 10, 7, "claimed"), job(2, 11, 7, "claimed")],
        active_lanes=2,
        peak_lanes=2,
        claimed_count=2,
    )

    exit_code = observer.run_acceptance_samples(
        [failing],
        acceptance_tracker(),
        output=output,
        now_provider=lambda: "2026-08-26T10:00:00Z",
    )

    rows = parse_jsonl(output)
    assert exit_code == observer.EXIT_ACCEPTANCE_FAILED
    assert rows[-1]["kind"] == "acceptance_failed"
    assert rows[-1]["reason"] == "same_chat_multiple_claims"
    assert rows[-1]["rollback_target"] == {
        "lock_mode": "global",
        "max_parallel_chats": 1,
        "pipeline_mode": "queue",
        "worker_command_mode": "queue",
    }
    assert not hasattr(observer, "perform_rollback")


def test_rollback_orchestration_is_independent_after_acceptance_failure():
    failed_tracker = acceptance_tracker()
    failed_tracker.observe(
        acceptance_snapshot(duplicate_job_count=1)
    )
    output = io.StringIO()
    rollback_tracker = observer.RollbackConvergenceTracker(
        target=observer.ExpectedRuntimeState("global", 1, "queue", "queue"),
        expected_pids=observer.AuthorityPids(101, 102, 103),
        required_consecutive=1,
    )

    exit_code = observer.run_rollback_samples(
        [runtime(lock_mode="global", cap=1, new_limit=False)],
        rollback_tracker,
        output=output,
        now_provider=lambda: "2026-08-26T10:00:00Z",
    )

    rows = parse_jsonl(output)
    assert exit_code == 0
    assert rows[-1]["kind"] == "rollback_converged"


def test_second_incomplete_cli_sample_is_observer_incomplete():
    output = io.StringIO()

    exit_code = observer.run_acceptance_samples(
        [acceptance_snapshot(complete=False), acceptance_snapshot(complete=False)],
        acceptance_tracker(),
        output=output,
        now_provider=lambda: "2026-08-26T10:00:00Z",
    )

    rows = parse_jsonl(output)
    assert exit_code == observer.EXIT_OBSERVER_INCOMPLETE
    assert rows[-1]["kind"] == "observer_incomplete"
    assert rows[-1]["reason"] == "observer_incomplete"


def test_every_orchestration_line_has_kind_and_observed_at():
    output = io.StringIO()

    observer.run_convergence_samples(
        [runtime(elapsed=5.001)],
        convergence_tracker(),
        output=output,
        now_provider=lambda: "2026-08-26T10:00:00Z",
    )

    assert all(
        {"kind", "observed_at"} <= row.keys() for row in parse_jsonl(output)
    )


def test_cli_has_only_fixed_read_only_observation_inputs():
    parser = observer.build_parser()
    help_text = parser.format_help()
    subcommands = parser._subparsers._group_actions[0].choices
    all_help = help_text + "\n".join(
        command.format_help() for command in subcommands.values()
    )

    assert set(subcommands) == {
        "convergence",
        "acceptance",
        "rollback-convergence",
    }
    for forbidden in (
        "http-method",
        "sql",
        "shell",
        "command",
        "evidence-output",
        "post",
        "restart",
    ):
        assert forbidden not in all_help.lower()
    for required in (
        "--database",
        "--ingest-url",
        "--worker-url",
        "--web-url",
        "--baseline-raw-message-id",
        "--baseline-job-id",
        "--ingest-pid",
        "--worker-pid",
        "--web-pid",
        "--poll-interval",
    ):
        assert required in all_help
