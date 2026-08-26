#!/usr/bin/env python3
"""Read-only Phase 7 per-chat acceptance observer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


NONTERMINAL_STATUSES = frozenset({"pending", "claimed"})


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
