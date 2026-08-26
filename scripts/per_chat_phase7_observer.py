#!/usr/bin/env python3
"""Read-only Phase 7 per-chat acceptance observer."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, Iterable, TextIO
from urllib import request as urllib_request


NONTERMINAL_STATUSES = frozenset({"pending", "claimed"})
EXIT_ACCEPTANCE_FAILED = 2
EXIT_OBSERVER_INCOMPLETE = 3


@dataclass(frozen=True)
class JobObservation:
    job_id: int
    raw_message_id: int
    chat_id: int
    status: str
    completed_at: datetime | None = None


@dataclass(frozen=True)
class InvariantViolation:
    code: str
    chat_id: int
    job_ids: tuple[int, ...]


@dataclass(frozen=True)
class SameChatEvaluation:
    violations: tuple[InvariantViolation, ...]
    claimed_chat_ids: frozenset[int]


@dataclass(frozen=True)
class ExpectedRuntimeState:
    lock_mode: str
    max_parallel_chats: int
    pipeline_mode: str
    worker_command_mode: str


@dataclass(frozen=True)
class AuthorityPids:
    ingest: int
    worker: int
    web: int


@dataclass(frozen=True)
class RuntimeObservation:
    elapsed_seconds: float
    complete: bool
    database_state: ExpectedRuntimeState | None
    api_state: ExpectedRuntimeState | None
    pids: AuthorityPids | None
    worker_role: str | None
    worker_cap: int | None
    active_lanes: int | None
    peak_lanes: int | None
    limit_applied_at: str | None


@dataclass(frozen=True)
class ConvergenceResult:
    passed: bool
    failed: bool
    consecutive: int
    reason: str | None


@dataclass(frozen=True)
class AcceptanceObservation:
    runtime: RuntimeObservation
    jobs: tuple[JobObservation, ...]
    raw_message_count: int
    distinct_chat_count: int
    pending_count: int
    claimed_count: int
    duplicate_job_count: int
    missing_job_count: int
    orphan_job_count: int
    stuck_job_count: int
    sqlite_lock_count: int
    loop_stall_count: int
    session_conflict_count: int
    deepseek_402_count: int
    ingest_anomaly_count: int
    execution_anomaly_count: int


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    failed: bool
    reason: str | None
    rollback_target: ExpectedRuntimeState | None


@dataclass(frozen=True)
class DatabaseObservation:
    state: ExpectedRuntimeState
    semantic_review_enabled: bool
    query_only: int
    journal_mode: str
    total_changes: int
    jobs: tuple[JobObservation, ...]
    raw_message_count: int
    distinct_chat_count: int
    pending_count: int
    claimed_count: int
    duplicate_job_count: int
    missing_job_count: int
    orphan_job_count: int


def evaluate_same_chat_jobs(jobs: list[JobObservation]) -> SameChatEvaluation:
    by_chat: dict[int, list[JobObservation]] = defaultdict(list)
    for row in jobs:
        if row.status in NONTERMINAL_STATUSES:
            by_chat[row.chat_id].append(row)

    violations: list[InvariantViolation] = []
    claimed_chat_ids: set[int] = set()
    for chat_id, rows in sorted(by_chat.items()):
        ordered = sorted(rows, key=lambda row: (row.raw_message_id, row.job_id))
        claimed = [row for row in ordered if row.status == "claimed"]
        if claimed:
            claimed_chat_ids.add(chat_id)
        if len(claimed) > 1:
            violations.append(
                InvariantViolation(
                    code="same_chat_multiple_claims",
                    chat_id=chat_id,
                    job_ids=tuple(row.job_id for row in claimed),
                )
            )
            continue
        if claimed and claimed[0].job_id != ordered[0].job_id:
            violations.append(
                InvariantViolation(
                    code="same_chat_out_of_order_claim",
                    chat_id=chat_id,
                    job_ids=(ordered[0].job_id, claimed[0].job_id),
                )
            )
    return SameChatEvaluation(
        violations=tuple(violations),
        claimed_chat_ids=frozenset(claimed_chat_ids),
    )


def _parse_iso_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _parse_database_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace(" ", "T"))


def open_read_only_database(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def collect_database_observation(
    path: Path,
    *,
    baseline_raw_message_id: int,
    baseline_job_id: int,
) -> DatabaseObservation:
    connection = open_read_only_database(path)
    try:
        connection.execute("BEGIN")
        settings_row = connection.execute(
            "SELECT value_json FROM trading_settings WHERE key = 'global'"
        ).fetchone()
        if settings_row is None:
            raise ValueError("global trading settings row is missing")
        settings = json.loads(settings_row["value_json"])
        state = ExpectedRuntimeState(
            lock_mode=str(settings["message_lock_mode"]),
            max_parallel_chats=int(
                settings["message_processing_max_parallel_chats"]
            ),
            pipeline_mode=str(settings["message_pipeline_mode"]),
            worker_command_mode=str(settings["worker_command_mode"]),
        )
        rows = connection.execute(
            """
            SELECT id, raw_message_id, chat_id, status, completed_at
            FROM message_processing_jobs
            WHERE id > ? AND shadow = 0
            ORDER BY id
            """,
            (baseline_job_id,),
        ).fetchall()
        jobs = tuple(
            JobObservation(
                job_id=int(row["id"]),
                raw_message_id=int(row["raw_message_id"]),
                chat_id=int(row["chat_id"]),
                status=str(row["status"]),
                completed_at=_parse_database_timestamp(row["completed_at"]),
            )
            for row in rows
        )
        raw_message_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE id > ?",
                (baseline_raw_message_id,),
            ).fetchone()[0]
        )
        distinct_chat_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM raw_messages WHERE id > ?",
                (baseline_raw_message_id,),
            ).fetchone()[0]
        )
        duplicate_job_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT raw_message_id
                    FROM message_processing_jobs
                    WHERE id > ? AND shadow = 0
                    GROUP BY raw_message_id
                    HAVING COUNT(*) > 1
                )
                """,
                (baseline_job_id,),
            ).fetchone()[0]
        )
        missing_job_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM raw_messages AS raw
                WHERE raw.id > ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM message_processing_jobs AS job
                      WHERE job.raw_message_id = raw.id
                        AND job.id > ?
                        AND job.shadow = 0
                  )
                """,
                (baseline_raw_message_id, baseline_job_id),
            ).fetchone()[0]
        )
        orphan_job_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM message_processing_jobs AS job
                LEFT JOIN raw_messages AS raw ON raw.id = job.raw_message_id
                WHERE job.id > ? AND job.shadow = 0 AND raw.id IS NULL
                """,
                (baseline_job_id,),
            ).fetchone()[0]
        )
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        pending_count = sum(row.status == "pending" for row in jobs)
        claimed_count = sum(row.status == "claimed" for row in jobs)
        total_changes = int(connection.total_changes)
        connection.rollback()
        return DatabaseObservation(
            state=state,
            semantic_review_enabled=bool(
                settings.get("semantic_review_enabled", False)
            ),
            query_only=query_only,
            journal_mode=journal_mode,
            total_changes=total_changes,
            jobs=jobs,
            raw_message_count=raw_message_count,
            distinct_chat_count=distinct_chat_count,
            pending_count=pending_count,
            claimed_count=claimed_count,
            duplicate_job_count=duplicate_job_count,
            missing_job_count=missing_job_count,
            orphan_job_count=orphan_job_count,
        )
    finally:
        connection.close()


def read_json_url(url: str, *, timeout_seconds: float) -> dict[str, object]:
    with urllib_request.urlopen(url, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("HTTP observation payload is not an object")
    return payload


def _state_from_settings(payload: dict[str, object]) -> ExpectedRuntimeState:
    return ExpectedRuntimeState(
        lock_mode=str(payload["message_lock_mode"]),
        max_parallel_chats=int(payload["message_processing_max_parallel_chats"]),
        pipeline_mode=str(payload["message_pipeline_mode"]),
        worker_command_mode=str(payload["worker_command_mode"]),
    )


def _pid_is_alive(pid: int) -> bool:
    return Path(f"/proc/{int(pid)}").exists()


def collect_runtime_observation(
    database: DatabaseObservation,
    *,
    elapsed_seconds: float,
    expected_pids: AuthorityPids,
    ingest_base_url: str,
    worker_base_url: str,
    web_base_url: str,
    timeout_seconds: float,
    json_reader: Callable[..., dict[str, object]] = read_json_url,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> RuntimeObservation:
    bases = {
        "ingest": ingest_base_url.rstrip("/"),
        "worker": worker_base_url.rstrip("/"),
        "web": web_base_url.rstrip("/"),
    }
    settings = {
        role: json_reader(
            f"{base}/api/trading-settings", timeout_seconds=timeout_seconds
        )
        for role, base in bases.items()
    }
    health = {
        role: json_reader(
            f"{base}/api/runtime/loop-health", timeout_seconds=timeout_seconds
        )
        for role, base in bases.items()
    }
    api_states = {role: _state_from_settings(row) for role, row in settings.items()}
    api_state = api_states["worker"]
    roles_match = all(
        str(health[role].get("runtime_role")) == role for role in bases
    )
    settings_match = all(row == api_state for row in api_states.values())
    pids_alive = all(
        pid_is_alive(pid)
        for pid in (expected_pids.ingest, expected_pids.worker, expected_pids.web)
    )
    worker = health["worker"]
    worker_cap = worker.get("configured_max_parallel_chats")
    active_lanes = worker.get("active_chat_lanes")
    peak_lanes = worker.get("peak_active_chat_lanes_since_limit_change")
    limit_applied_at = worker.get("limit_applied_at")
    complete = (
        settings_match
        and roles_match
        and pids_alive
        and worker_cap is not None
        and active_lanes is not None
        and peak_lanes is not None
        and limit_applied_at is not None
    )
    return RuntimeObservation(
        elapsed_seconds=float(elapsed_seconds),
        complete=complete,
        database_state=database.state,
        api_state=api_state if settings_match else None,
        pids=expected_pids if pids_alive else None,
        worker_role=str(worker.get("runtime_role")),
        worker_cap=int(worker_cap) if worker_cap is not None else None,
        active_lanes=int(active_lanes) if active_lanes is not None else None,
        peak_lanes=int(peak_lanes) if peak_lanes is not None else None,
        limit_applied_at=(
            str(limit_applied_at) if limit_applied_at is not None else None
        ),
    )


class ConvergenceTracker:
    def __init__(
        self,
        *,
        target: ExpectedRuntimeState,
        expected_pids: AuthorityPids,
        required_consecutive: int,
        deadline_seconds: float,
        previous_limit_applied_at: str | None,
    ) -> None:
        self.target = target
        self.expected_pids = expected_pids
        self.required_consecutive = max(1, int(required_consecutive))
        self.deadline_seconds = float(deadline_seconds)
        self.previous_limit_applied_at = previous_limit_applied_at
        self._consecutive = 0

    def observe(self, sample: RuntimeObservation) -> ConvergenceResult:
        if sample.elapsed_seconds > self.deadline_seconds:
            return ConvergenceResult(
                passed=False,
                failed=True,
                consecutive=self._consecutive,
                reason="convergence_deadline_exceeded",
            )
        if not sample.complete:
            return ConvergenceResult(
                passed=False,
                failed=False,
                consecutive=self._consecutive,
                reason="incomplete_observation",
            )
        if not self._matches_target(sample):
            self._consecutive = 0
            return ConvergenceResult(
                passed=False,
                failed=False,
                consecutive=0,
                reason="target_not_converged",
            )

        self._consecutive += 1
        return ConvergenceResult(
            passed=self._consecutive >= self.required_consecutive,
            failed=False,
            consecutive=self._consecutive,
            reason=None,
        )

    def _matches_target(self, sample: RuntimeObservation) -> bool:
        if sample.database_state != self.target or sample.api_state != self.target:
            return False
        if sample.pids != self.expected_pids or sample.worker_role != "worker":
            return False
        if sample.worker_cap != self.target.max_parallel_chats:
            return False
        if (
            sample.active_lanes is None
            or sample.peak_lanes is None
            or not 0
            <= sample.active_lanes
            <= sample.peak_lanes
            <= self.target.max_parallel_chats
        ):
            return False
        if self.previous_limit_applied_at is not None:
            if sample.limit_applied_at is None:
                return False
            if _parse_iso_timestamp(sample.limit_applied_at) <= _parse_iso_timestamp(
                self.previous_limit_applied_at
            ):
                return False
        return True


class RollbackConvergenceTracker(ConvergenceTracker):
    def __init__(
        self,
        *,
        target: ExpectedRuntimeState,
        expected_pids: AuthorityPids,
        required_consecutive: int,
        deadline_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            target=target,
            expected_pids=expected_pids,
            required_consecutive=required_consecutive,
            deadline_seconds=deadline_seconds,
            previous_limit_applied_at=None,
        )


class AcceptanceTracker:
    def __init__(
        self,
        *,
        expected_state: ExpectedRuntimeState,
        expected_pids: AuthorityPids,
    ) -> None:
        self.expected_state = expected_state
        self.expected_pids = expected_pids
        self.raw_message_count = 0
        self.distinct_chat_count = 0
        self.peak_lanes = 0
        self.max_backlog = 0
        self.cross_chat_progress = False
        self._pending_count = 0
        self._claimed_count = 0
        self._incomplete_attempts = 0
        self._failure: AcceptanceResult | None = None

    def observe(self, sample: AcceptanceObservation) -> AcceptanceResult:
        if self._failure is not None:
            return self._failure
        if not sample.runtime.complete:
            self._incomplete_attempts += 1
            if self._incomplete_attempts == 1:
                return AcceptanceResult(
                    passed=False,
                    failed=False,
                    reason="observer_incomplete_retry",
                    rollback_target=None,
                )
            return self._fail("observer_incomplete")
        self._incomplete_attempts = 0

        runtime_failure = self._runtime_failure(sample.runtime)
        if runtime_failure is not None:
            return self._fail(runtime_failure)

        same_chat = evaluate_same_chat_jobs(list(sample.jobs))
        if same_chat.violations:
            return self._fail(same_chat.violations[0].code)

        anomaly_fields = (
            (sample.duplicate_job_count, "duplicate_job_identity"),
            (sample.missing_job_count, "missing_job_identity"),
            (sample.orphan_job_count, "orphan_job_identity"),
            (sample.stuck_job_count, "stuck_message_job"),
            (sample.sqlite_lock_count, "sqlite_lock"),
            (sample.loop_stall_count, "event_loop_stall"),
            (sample.session_conflict_count, "telegram_session_conflict"),
            (sample.deepseek_402_count, "deepseek_402"),
            (sample.ingest_anomaly_count, "ingest_anomaly"),
            (sample.execution_anomaly_count, "execution_anomaly"),
        )
        for count, reason in anomaly_fields:
            if count:
                return self._fail(reason)

        self.raw_message_count = max(
            self.raw_message_count, sample.raw_message_count
        )
        self.distinct_chat_count = max(
            self.distinct_chat_count, sample.distinct_chat_count
        )
        self.peak_lanes = max(
            self.peak_lanes, int(sample.runtime.peak_lanes or 0)
        )
        self._pending_count = sample.pending_count
        self._claimed_count = sample.claimed_count
        self.max_backlog = max(
            self.max_backlog, sample.pending_count + sample.claimed_count
        )
        claimed_chat_count = len(same_chat.claimed_chat_ids)
        if (
            claimed_chat_count >= 2
            and sample.runtime.active_lanes is not None
            and sample.runtime.active_lanes >= claimed_chat_count
        ):
            self.cross_chat_progress = True
        return AcceptanceResult(
            passed=False,
            failed=False,
            reason=None,
            rollback_target=None,
        )

    def finalize(self) -> AcceptanceResult:
        if self._failure is not None:
            return self._failure
        if (
            self.raw_message_count < 5
            or self.distinct_chat_count < 2
            or not 2 <= self.peak_lanes <= 3
            or not self.cross_chat_progress
            or self._pending_count != 0
            or self._claimed_count != 0
        ):
            return self._fail("acceptance_minimum_not_met")
        return AcceptanceResult(
            passed=True,
            failed=False,
            reason=None,
            rollback_target=None,
        )

    def _runtime_failure(self, sample: RuntimeObservation) -> str | None:
        if (
            sample.database_state != self.expected_state
            or sample.api_state != self.expected_state
        ):
            return "runtime_state_drift"
        if sample.pids != self.expected_pids:
            return "authority_pid_drift"
        if sample.worker_role != "worker":
            return "authority_role_drift"
        if sample.worker_cap != self.expected_state.max_parallel_chats:
            return "worker_cap_drift"
        if (
            sample.active_lanes is None
            or sample.peak_lanes is None
            or not 0
            <= sample.active_lanes
            <= sample.peak_lanes
            <= self.expected_state.max_parallel_chats
        ):
            return "worker_lane_bounds_invalid"
        return None

    def _fail(self, reason: str) -> AcceptanceResult:
        cap = 3 if reason in {
            "lock_anomaly",
            "admission_anomaly",
            "ingest_anomaly",
        } else 1
        self._failure = AcceptanceResult(
            passed=False,
            failed=True,
            reason=reason,
            rollback_target=ExpectedRuntimeState(
                "global", cap, "queue", "queue"
            ),
        )
        return self._failure


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _emit_jsonl(
    output: TextIO,
    kind: str,
    *,
    now_provider: Callable[[], str],
    **fields: object,
) -> None:
    payload = {"kind": kind, "observed_at": now_provider(), **fields}
    output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    output.write("\n")
    output.flush()


def _runtime_sample_fields(sample: RuntimeObservation) -> dict[str, object]:
    return {
        "elapsed_seconds": sample.elapsed_seconds,
        "complete": sample.complete,
        "database_state": (
            asdict(sample.database_state) if sample.database_state else None
        ),
        "api_state": asdict(sample.api_state) if sample.api_state else None,
        "pids": asdict(sample.pids) if sample.pids else None,
        "worker_role": sample.worker_role,
        "worker_cap": sample.worker_cap,
        "active_lanes": sample.active_lanes,
        "peak_lanes": sample.peak_lanes,
        "limit_applied_at": sample.limit_applied_at,
    }


def run_convergence_samples(
    samples: Iterable[RuntimeObservation],
    tracker: ConvergenceTracker,
    *,
    output: TextIO = sys.stdout,
    now_provider: Callable[[], str] = _utc_now,
) -> int:
    incomplete_count = 0
    for sample in samples:
        _emit_jsonl(
            output,
            "sample",
            now_provider=now_provider,
            mode="convergence",
            **_runtime_sample_fields(sample),
        )
        if not sample.complete:
            incomplete_count += 1
            if incomplete_count >= 2:
                _emit_jsonl(
                    output,
                    "observer_incomplete",
                    now_provider=now_provider,
                    reason="observer_incomplete",
                )
                return EXIT_OBSERVER_INCOMPLETE
        else:
            incomplete_count = 0
        result = tracker.observe(sample)
        if result.passed:
            _emit_jsonl(
                output,
                "convergence_passed",
                now_provider=now_provider,
                consecutive=result.consecutive,
            )
            return 0
        if result.failed:
            _emit_jsonl(
                output,
                "convergence_failed",
                now_provider=now_provider,
                reason=result.reason,
                consecutive=result.consecutive,
            )
            return EXIT_ACCEPTANCE_FAILED
    _emit_jsonl(
        output,
        "observer_incomplete",
        now_provider=now_provider,
        reason="sample_stream_ended",
    )
    return EXIT_OBSERVER_INCOMPLETE


def _rollback_fields(
    target: ExpectedRuntimeState | None,
) -> dict[str, object] | None:
    return asdict(target) if target is not None else None


def run_acceptance_samples(
    samples: Iterable[AcceptanceObservation],
    tracker: AcceptanceTracker,
    *,
    output: TextIO = sys.stdout,
    now_provider: Callable[[], str] = _utc_now,
) -> int:
    observed_any = False
    for sample in samples:
        observed_any = True
        _emit_jsonl(
            output,
            "sample",
            now_provider=now_provider,
            mode="acceptance",
            **_runtime_sample_fields(sample.runtime),
            raw_message_count=sample.raw_message_count,
            distinct_chat_count=sample.distinct_chat_count,
            pending_count=sample.pending_count,
            claimed_count=sample.claimed_count,
        )
        result = tracker.observe(sample)
        if result.reason == "observer_incomplete_retry":
            continue
        if result.failed:
            kind = (
                "observer_incomplete"
                if result.reason == "observer_incomplete"
                else "acceptance_failed"
            )
            _emit_jsonl(
                output,
                kind,
                now_provider=now_provider,
                reason=result.reason,
                rollback_target=_rollback_fields(result.rollback_target),
            )
            return (
                EXIT_OBSERVER_INCOMPLETE
                if kind == "observer_incomplete"
                else EXIT_ACCEPTANCE_FAILED
            )
    if not observed_any:
        _emit_jsonl(
            output,
            "observer_incomplete",
            now_provider=now_provider,
            reason="sample_stream_ended",
        )
        return EXIT_OBSERVER_INCOMPLETE
    result = tracker.finalize()
    kind = "acceptance_summary" if result.passed else "acceptance_failed"
    _emit_jsonl(
        output,
        kind,
        now_provider=now_provider,
        passed=result.passed,
        reason=result.reason,
        rollback_target=_rollback_fields(result.rollback_target),
        raw_message_count=tracker.raw_message_count,
        distinct_chat_count=tracker.distinct_chat_count,
        peak_lanes=tracker.peak_lanes,
        max_backlog=tracker.max_backlog,
        cross_chat_progress=tracker.cross_chat_progress,
    )
    return 0 if result.passed else EXIT_ACCEPTANCE_FAILED


def run_rollback_samples(
    samples: Iterable[RuntimeObservation],
    tracker: RollbackConvergenceTracker,
    *,
    output: TextIO = sys.stdout,
    now_provider: Callable[[], str] = _utc_now,
) -> int:
    incomplete_count = 0
    for sample in samples:
        _emit_jsonl(
            output,
            "sample",
            now_provider=now_provider,
            mode="rollback-convergence",
            **_runtime_sample_fields(sample),
        )
        if not sample.complete:
            incomplete_count += 1
            if incomplete_count >= 2:
                _emit_jsonl(
                    output,
                    "observer_incomplete",
                    now_provider=now_provider,
                    reason="observer_incomplete",
                )
                return EXIT_OBSERVER_INCOMPLETE
        else:
            incomplete_count = 0
        result = tracker.observe(sample)
        if result.passed:
            _emit_jsonl(
                output,
                "rollback_converged",
                now_provider=now_provider,
                consecutive=result.consecutive,
            )
            return 0
        if result.failed:
            _emit_jsonl(
                output,
                "rollback_convergence_failed",
                now_provider=now_provider,
                reason=result.reason,
                consecutive=result.consecutive,
            )
            return EXIT_ACCEPTANCE_FAILED
    _emit_jsonl(
        output,
        "observer_incomplete",
        now_provider=now_provider,
        reason="sample_stream_ended",
    )
    return EXIT_OBSERVER_INCOMPLETE


def _add_common_observation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ingest-url", required=True)
    parser.add_argument("--worker-url", required=True)
    parser.add_argument("--web-url", required=True)
    parser.add_argument("--baseline-raw-message-id", type=int, required=True)
    parser.add_argument("--baseline-job-id", type=int, required=True)
    parser.add_argument("--ingest-pid", type=int, required=True)
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--web-pid", type=int, required=True)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--http-timeout", type=float, default=1.0)
    parser.add_argument(
        "--target-lock-mode", choices=("global", "per_chat"), required=True
    )
    parser.add_argument("--target-cap", type=int, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Phase 7 state observer; JSONL is written to stdout."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    convergence = subparsers.add_parser("convergence")
    _add_common_observation_arguments(convergence)
    convergence.add_argument("--deadline", type=float, default=5.0)
    convergence.add_argument("--required-consecutive", type=int, default=3)
    convergence.add_argument("--previous-limit-applied-at", required=True)

    acceptance = subparsers.add_parser("acceptance")
    _add_common_observation_arguments(acceptance)
    acceptance.add_argument("--window", type=float, default=7200.0)
    acceptance.add_argument(
        "--guard-counters-file",
        type=Path,
        required=True,
        help="Read-only JSON snapshot containing all external anomaly deltas.",
    )

    rollback = subparsers.add_parser("rollback-convergence")
    _add_common_observation_arguments(rollback)
    rollback.add_argument("--deadline", type=float, default=5.0)
    rollback.add_argument("--required-consecutive", type=int, default=3)
    return parser


def _expected_pids_from_args(args: argparse.Namespace) -> AuthorityPids:
    return AuthorityPids(args.ingest_pid, args.worker_pid, args.web_pid)


def _target_from_args(args: argparse.Namespace) -> ExpectedRuntimeState:
    return ExpectedRuntimeState(
        args.target_lock_mode,
        args.target_cap,
        "queue",
        "queue",
    )


def _incomplete_runtime(elapsed_seconds: float) -> RuntimeObservation:
    return RuntimeObservation(
        elapsed_seconds=elapsed_seconds,
        complete=False,
        database_state=None,
        api_state=None,
        pids=None,
        worker_role=None,
        worker_cap=None,
        active_lanes=None,
        peak_lanes=None,
        limit_applied_at=None,
    )


KNOWN_INCOMPLETE_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    sqlite3.Error,
)


def _collect_runtime_with_retry(
    args: argparse.Namespace,
    *,
    elapsed_seconds: float,
) -> tuple[DatabaseObservation | None, RuntimeObservation]:
    for _attempt in range(2):
        try:
            database = collect_database_observation(
                args.database,
                baseline_raw_message_id=args.baseline_raw_message_id,
                baseline_job_id=args.baseline_job_id,
            )
            runtime = collect_runtime_observation(
                database,
                elapsed_seconds=elapsed_seconds,
                expected_pids=_expected_pids_from_args(args),
                ingest_base_url=args.ingest_url,
                worker_base_url=args.worker_url,
                web_base_url=args.web_url,
                timeout_seconds=args.http_timeout,
            )
            return database, runtime
        except KNOWN_INCOMPLETE_ERRORS:
            continue
    return None, _incomplete_runtime(elapsed_seconds)


def _runtime_samples(args: argparse.Namespace) -> Iterable[RuntimeObservation]:
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        _database, runtime = _collect_runtime_with_retry(
            args, elapsed_seconds=elapsed
        )
        elapsed = time.monotonic() - started
        runtime = replace(runtime, elapsed_seconds=elapsed)
        yield runtime
        if elapsed > args.deadline:
            return
        time.sleep(args.poll_interval)


GUARD_COUNTER_FIELDS = (
    "stuck_job_count",
    "sqlite_lock_count",
    "loop_stall_count",
    "session_conflict_count",
    "deepseek_402_count",
    "ingest_anomaly_count",
    "execution_anomaly_count",
)


def _read_guard_counters(path: Path) -> dict[str, int]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("guard counters payload is not an object")
    return {field: int(payload[field]) for field in GUARD_COUNTER_FIELDS}


def _acceptance_samples(
    args: argparse.Namespace,
) -> Iterable[AcceptanceObservation]:
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        database, runtime = _collect_runtime_with_retry(
            args, elapsed_seconds=elapsed
        )
        guards: dict[str, int] | None = None
        if database is not None and runtime.complete:
            for _attempt in range(2):
                try:
                    guards = _read_guard_counters(args.guard_counters_file)
                    break
                except KNOWN_INCOMPLETE_ERRORS:
                    continue
        elapsed = time.monotonic() - started
        runtime = replace(runtime, elapsed_seconds=elapsed)
        if database is None or guards is None:
            runtime = _incomplete_runtime(elapsed)
            database = DatabaseObservation(
                state=_target_from_args(args),
                semantic_review_enabled=False,
                query_only=0,
                journal_mode="unknown",
                total_changes=0,
                jobs=(),
                raw_message_count=0,
                distinct_chat_count=0,
                pending_count=0,
                claimed_count=0,
                duplicate_job_count=0,
                missing_job_count=0,
                orphan_job_count=0,
            )
            guards = {field: 0 for field in GUARD_COUNTER_FIELDS}
        elif (
            database.query_only != 1
            or database.total_changes != 0
            or database.journal_mode.lower() != "wal"
        ):
            guards["sqlite_lock_count"] += 1
        if database.semantic_review_enabled:
            guards["ingest_anomaly_count"] += 1
        yield AcceptanceObservation(
            runtime=runtime,
            jobs=database.jobs,
            raw_message_count=database.raw_message_count,
            distinct_chat_count=database.distinct_chat_count,
            pending_count=database.pending_count,
            claimed_count=database.claimed_count,
            duplicate_job_count=database.duplicate_job_count,
            missing_job_count=database.missing_job_count,
            orphan_job_count=database.orphan_job_count,
            **guards,
        )
        if elapsed >= args.window:
            return
        time.sleep(args.poll_interval)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = _target_from_args(args)
    pids = _expected_pids_from_args(args)
    if args.mode == "convergence":
        tracker = ConvergenceTracker(
            target=target,
            expected_pids=pids,
            required_consecutive=args.required_consecutive,
            deadline_seconds=args.deadline,
            previous_limit_applied_at=args.previous_limit_applied_at,
        )
        return run_convergence_samples(_runtime_samples(args), tracker)
    if args.mode == "acceptance":
        tracker = AcceptanceTracker(expected_state=target, expected_pids=pids)
        return run_acceptance_samples(_acceptance_samples(args), tracker)
    tracker = RollbackConvergenceTracker(
        target=target,
        expected_pids=pids,
        required_consecutive=args.required_consecutive,
        deadline_seconds=args.deadline,
    )
    return run_rollback_samples(_runtime_samples(args), tracker)


if __name__ == "__main__":
    raise SystemExit(main())
