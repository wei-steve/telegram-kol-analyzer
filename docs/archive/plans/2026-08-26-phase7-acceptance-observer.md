# Phase 7 Acceptance Observer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a versioned, tested, read-only Phase 7 observer that treats a same-chat successor waiting in `pending` as valid, detects actual durable claim overlap/order violations, and confirms rollback without re-running failed acceptance invariants.

**Architecture:** Keep the observer in one standalone standard-library script outside the production package import graph. Separate immutable observation data, pure invariant trackers, read-only SQLite/HTTP collectors, and the CLI loop so RED-to-GREEN tests can exercise behavior without production access. The observer emits JSON Lines and typed rollback guidance but contains no mutation path.

**Tech Stack:** Python 3.12 standard library (`argparse`, `dataclasses`, `json`, `sqlite3`, `time`, `urllib.request`), pytest, existing SQLite message-processing schema and runtime health endpoints.

---

### Task 1: Define immutable job observations and the same-chat invariant

**Files:**

- Create: `scripts/per_chat_phase7_observer.py`
- Create: `tests/test_per_chat_phase7_observer.py`

**Step 1: Write the failing pending-versus-overlap tests**

Load the standalone script with `importlib.util` and define a small job helper:

```python
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
```

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_per_chat_phase7_observer.py \
  -q
```

Expected: collection fails because `scripts/per_chat_phase7_observer.py` does not exist, or the new observation API is absent. Do not create production code before recording this expected failure.

**Step 3: Implement the minimal immutable model and evaluator**

Create the script with these core definitions:

```python
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
```

Do not compare `enqueued_at`, `claimed_at`, or `completed_at` across samples.

**Step 4: Run the focused tests and verify GREEN**

Run the Task 1 command again.

Expected: four tests pass.

**Step 5: Commit the RED-to-GREEN unit**

```bash
git add -- scripts/per_chat_phase7_observer.py tests/test_per_chat_phase7_observer.py
git diff --cached --name-only
git commit -m "test: define Phase 7 same-chat observer contract"
```

### Task 2: Add cutover and rollback convergence trackers

**Files:**

- Modify: `scripts/per_chat_phase7_observer.py`
- Modify: `tests/test_per_chat_phase7_observer.py`

**Step 1: Write failing convergence tests**

Add immutable `RuntimeObservation` fixtures and tests proving:

```python
def test_cutover_requires_three_consecutive_complete_samples():
    tracker = observer.ConvergenceTracker(
        target=observer.ExpectedRuntimeState("per_chat", 3, "queue", "queue"),
        expected_pids=observer.AuthorityPids(101, 102, 103),
        required_consecutive=3,
        deadline_seconds=5.0,
        previous_limit_applied_at="2026-08-26T08:18:25+00:00",
    )
    assert tracker.observe(runtime(elapsed=0.25, cap=3, new_limit=True)).passed is False
    assert tracker.observe(runtime(elapsed=0.50, cap=3, new_limit=True)).passed is False
    assert tracker.observe(runtime(elapsed=0.75, cap=3, new_limit=True)).passed is True


def test_cutover_mismatch_resets_streak_without_extending_deadline():
    tracker = convergence_tracker()
    tracker.observe(runtime(elapsed=0.25, cap=3, new_limit=True))
    result = tracker.observe(runtime(elapsed=0.50, cap=1, new_limit=False))
    assert result.consecutive == 0
    assert tracker.deadline_seconds == 5.0


def test_rollback_confirmation_ignores_prior_acceptance_failure():
    rollback = observer.RollbackConvergenceTracker(
        target=observer.ExpectedRuntimeState("global", 1, "queue", "queue"),
        expected_pids=observer.AuthorityPids(101, 102, 103),
        required_consecutive=1,
    )
    result = rollback.observe(runtime(lock_mode="global", cap=1))
    assert result.passed is True
```

The helper must create complete database/API tuples, worker role/cap/lane fields,
and unchanged PID evidence.

**Step 2: Run only the new tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_per_chat_phase7_observer.py \
  -q
```

Expected: failures identify the missing tracker and runtime-state types.

**Step 3: Implement a pure target-state matcher and separate trackers**

Add:

```python
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
```

`ConvergenceTracker.observe()` must:

1. fail on elapsed time strictly greater than its fixed deadline;
2. reset the streak on a complete non-target sample;
3. require DB and API target equality, unchanged PIDs, worker role `worker`, cap
   equality, and `0 <= active <= peak <= cap`;
4. additionally require a strictly newer `limit_applied_at` for cutover mode;
5. never change `deadline_seconds`.

`RollbackConvergenceTracker` uses the same structural matcher but does not
accept or invoke an acceptance tracker and does not require comparison with the
cutover timestamp.

**Step 4: Verify GREEN**

Run the Task 2 test command. Expected: all observer tests pass.

**Step 5: Commit**

```bash
git add -- scripts/per_chat_phase7_observer.py tests/test_per_chat_phase7_observer.py
git diff --cached --name-only
git commit -m "feat: add independent Phase 7 convergence trackers"
```

### Task 3: Add the acceptance state machine and typed rollback guidance

**Files:**

- Modify: `scripts/per_chat_phase7_observer.py`
- Modify: `tests/test_per_chat_phase7_observer.py`

**Step 1: Write failing acceptance tests**

Add tests for these exact contracts:

```python
def test_pending_same_chat_successor_does_not_fail_acceptance():
    tracker = acceptance_tracker()
    result = tracker.observe(
        acceptance_snapshot(
            jobs=[job(1, 10, 7, "claimed"), job(2, 11, 7, "pending")],
            active_lanes=1,
            peak_lanes=1,
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
        )
    )
    assert result.failed is False
    assert tracker.cross_chat_progress is True


def test_acceptance_finalize_never_waives_traffic_or_peak():
    tracker = acceptance_tracker()
    tracker.observe(acceptance_snapshot(raw_count=3, chat_count=2, peak_lanes=1))
    result = tracker.finalize()
    assert result.failed is True
    assert result.reason == "acceptance_minimum_not_met"
```

Also cover tuple drift, PID drift, cap above three, duplicate raw-message job
identity, missing job identity, stuck non-terminal job, SQLite/loop/session
anomaly flags, and a second incomplete sample.

**Step 2: Verify RED**

Run the focused observer test file. Expected: missing acceptance types and methods.

**Step 3: Implement the minimal acceptance tracker**

Add `AcceptanceObservation`, `AcceptanceResult`, and `AcceptanceTracker`.
`observe()` must run checks in this order:

1. complete-query contract;
2. exact tuple/cap/PID/role/authority contract;
3. lane bounds and peak cap;
4. `evaluate_same_chat_jobs()`;
5. missing/orphan/duplicate/stuck and anomaly flags;
6. metrics update for raw messages, distinct chats, backlog, peak, and cross-chat
   claimed progress.

The tracker may retry one incomplete collection through the CLI loop but must
fail on the second incomplete collection. It must not turn an incomplete count
into zero. `finalize()` requires at least five natural messages, at least two
chats, peak between two and three, observed cross-chat progress, an empty final
non-shadow queue, and no prior failure.

Map lock/admission/ingest reason codes to `global + 3 + queue`; map ordering,
scheduler, duplicate, SQLite, execution, and concurrency reason codes to
`global + 1 + queue`.

**Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_per_chat_phase7_observer.py -q
```

Expected: all observer state-machine tests pass.

**Step 5: Commit**

```bash
git add -- scripts/per_chat_phase7_observer.py tests/test_per_chat_phase7_observer.py
git diff --cached --name-only
git commit -m "feat: add Phase 7 acceptance state machine"
```

### Task 4: Add read-only SQLite and HTTP collection

**Files:**

- Modify: `scripts/per_chat_phase7_observer.py`
- Modify: `tests/test_per_chat_phase7_observer.py`

**Step 1: Write failing real-SQLite collector tests**

Create a minimal SQLite fixture with `trading_settings`, `raw_messages`, and
`message_processing_jobs`. Test that the collector:

- opens through `file:<path>?mode=ro`;
- reports `PRAGMA query_only=1` and `journal_mode` without changing the database;
- parses only the `global` settings JSON fields needed for the tuple;
- returns new non-shadow jobs after the supplied baseline;
- excludes historical shadow rows;
- detects duplicate or missing raw/job identities;
- records `completed_at` only as diagnostic data.

Add an injected HTTP reader test returning settings and loop-health payloads for
ports 8001, 8002, and 8000. Verify role attribution and tuple completeness.

**Step 2: Verify RED**

Run the observer test file and confirm the collector functions are missing.

**Step 3: Implement collectors with no mutation surface**

Use:

```python
def open_read_only_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_json_url(url: str, *, timeout_seconds: float) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.load(response)
```

Do not construct `urllib.request.Request`, import `subprocess`, call a service
manager, or expose arbitrary SQL. One transaction must read the job set and
associated counts so the same-chat evaluator receives one consistent database
snapshot.

**Step 4: Verify GREEN and source boundary**

```bash
.venv/bin/python -m pytest tests/test_per_chat_phase7_observer.py -q
rg -n "urlopen|Request\(|method=|subprocess|systemctl|POST|worker.command" \
  scripts/per_chat_phase7_observer.py
```

Expected: tests pass; only `urlopen` GET collection appears, and no mutation or
service-control symbol appears.

**Step 5: Commit**

```bash
git add -- scripts/per_chat_phase7_observer.py tests/test_per_chat_phase7_observer.py
git diff --cached --name-only
git commit -m "feat: collect Phase 7 state read only"
```

### Task 5: Add JSONL CLI orchestration without production writes

**Files:**

- Modify: `scripts/per_chat_phase7_observer.py`
- Modify: `tests/test_per_chat_phase7_observer.py`

**Step 1: Write failing CLI tests**

Inject a finite sample provider and fake monotonic clock. Test all modes:

- `convergence` exits zero only after three consecutive target samples within
  the unchanged five-second deadline;
- `acceptance` emits `acceptance_failed` with a stable reason and rollback target
  but performs no rollback;
- `rollback-convergence` can emit `rollback_converged` after an acceptance
  failure fixture;
- a second incomplete collection emits `observer_incomplete` and exits nonzero;
- every stdout line parses as one JSON object with `kind` and `observed_at`;
- no CLI argument accepts an HTTP method, SQL statement, shell command, or
  evidence output path.

**Step 2: Verify RED**

Run the focused observer tests and confirm CLI entry points are absent.

**Step 3: Implement the minimal CLI**

Use `argparse` subcommands with explicit required inputs:

- database path;
- ingest, worker, and Web localhost URLs;
- baseline raw-message and job IDs;
- expected authority PIDs;
- polling interval and fixed mode deadline/window;
- target tuple and pre-cutover `limit_applied_at` where required.

Keep collection, tracker observation, JSON serialization, and sleeping in
separate functions. Catch only known incomplete-read exceptions, retry once in
the same sample slot, and emit the final typed error. Write JSONL only to stdout.

End the script with:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: Verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_per_chat_phase7_observer.py -q
.venv/bin/python scripts/per_chat_phase7_observer.py --help
.venv/bin/python scripts/per_chat_phase7_observer.py convergence --help
.venv/bin/python scripts/per_chat_phase7_observer.py acceptance --help
.venv/bin/python scripts/per_chat_phase7_observer.py rollback-convergence --help
```

Expected: focused tests pass and help output exposes no mutation option.

**Step 5: Commit**

```bash
git add -- scripts/per_chat_phase7_observer.py tests/test_per_chat_phase7_observer.py
git diff --cached --name-only
git commit -m "feat: add read-only Phase 7 observer CLI"
```

### Task 6: Run focused regression verification and close the local remediation

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Run the complete observer test file**

```bash
.venv/bin/python -m pytest tests/test_per_chat_phase7_observer.py -q
```

Expected: all observer tests pass with no warning introduced by the new tool.

**Step 2: Run the existing durable-ordering regression slice**

```bash
.venv/bin/python -m pytest \
  tests/test_message_processing_worker.py::test_worker_activity_snapshot_tracks_three_active_chat_lanes \
  tests/test_message_processing_worker.py::test_worker_loop_never_exceeds_three_active_chat_lanes \
  tests/test_message_processing_worker.py::test_live_claim_blocks_later_same_chat_job_while_other_chats_progress \
  tests/test_message_processing_worker.py::test_retry_not_due_blocks_later_same_chat_job_while_other_chats_progress \
  tests/test_message_processing_worker.py::test_claim_is_atomic_and_only_claims_one_ordered_job_per_chat \
  tests/test_live_listener_chat_isolation.py::test_per_chat_mode_serializes_the_same_chat_in_arrival_order \
  -q
```

Expected: all six tests pass.

**Step 3: Run static and diff checks**

```bash
.venv/bin/python -m compileall -q scripts/per_chat_phase7_observer.py
git diff --check
git status --short
```

Expected: compile and diff checks succeed; only the explicit observer task paths
are present.

**Step 4: Update canonical status and release the claim**

Record:

- exact local observer candidate commit(s);
- RED failure and GREEN focused results;
- the six ordering regression results;
- source boundary proof that the tool contains no mutation path;
- `claimed_by: unclaimed`, `claim_base_sha: null`;
- `current_task: phase-7-observer-fixed-awaiting-push-and-safe-retry-authorization`;
- `phase_7_status: rolled_back_incomplete`;
- `deployment_authorized: false` and `cutover_authorized: false`.

Do not claim Phase 7 complete and do not enter Phase 8.

**Step 5: Commit only the canonical status**

```bash
git add -- docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record Phase 7 observer remediation"
git status --short
```

Expected: the final local worktree is clean and the last canonical-status commit
equals `HEAD`. No push, deployment, restart, production access, cutover,
rollback, replay, Telegram business traffic, test trade, or exchange write has
occurred.
