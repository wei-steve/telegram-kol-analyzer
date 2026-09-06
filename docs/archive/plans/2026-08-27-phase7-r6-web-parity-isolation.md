# Phase 7 R6 Web-Parity Isolation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the self-perturbing continuous Web parity request while
preserving complete, read-only, fail-closed Phase 7 parity and stuck-job
evidence.

**Architecture:** Extend the standalone observer's existing SQLite transaction
to count pending jobs whose `enqueued_at` age is at least 300 seconds. Merge that
database count with external guard counters so an external zero cannot mask a
stuck job. The production acceptance controller will stop calling
`/api/runtime/message-pipeline-parity`; it will retain direct SQLite, journal,
process/session, active-write, and lightweight 30-second role-health checks.

**Tech Stack:** Python standard library, SQLite read-only URI, pytest, systemd
transient monitoring unit.

---

### Task 1: Prove and implement SQLite stuck-job attribution

**Files:**
- Modify: `tests/test_per_chat_phase7_observer.py`
- Modify: `scripts/per_chat_phase7_observer.py`

**Step 1: Write the failing database test**

Add `enqueued_at` to the observer fixture schema. Insert one pending queue job
older than 300 seconds, one younger pending job, and one old terminal job. Assert
that `collect_database_observation(...).stuck_job_count == 1`.

**Step 2: Run the RED test**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_per_chat_phase7_observer.py \
  -k sqlite_stuck_pending
```

Expected: failure because `DatabaseObservation` has no `stuck_job_count`.

**Step 3: Implement the minimal read-only count**

Add `stuck_job_count` to `DatabaseObservation`. In the existing consistent
transaction, count new non-shadow `pending` rows whose `enqueued_at` is at least
300 seconds old. Do not add another connection or HTTP request.

**Step 4: Verify GREEN**

Run the RED command again. Expected: pass.

### Task 2: Prove database evidence cannot be masked

**Files:**
- Modify: `tests/test_per_chat_phase7_observer.py`
- Modify: `scripts/per_chat_phase7_observer.py`

**Step 1: Write the failing acceptance orchestration test**

Inject a database snapshot with `stuck_job_count=1` and an external guard with
`stuck_job_count=0`. Assert that the yielded acceptance observation contains
one stuck job and fails with `stuck_message_job`.

**Step 2: Run RED**

Run the single new test and confirm the current guard dictionary overwrites the
database evidence.

**Step 3: Implement additive evidence merging**

Before constructing `AcceptanceObservation`, add the database stuck count to
the external guard stuck count. Keep every existing incomplete-query and
rollback mapping unchanged.

**Step 4: Verify GREEN and focused regressions**

Run the complete observer module plus `tests/test_runtime_loop_health.py` and
the directly related durable-worker ordering slice.

### Task 3: Freeze and operate the R6 candidate

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Build the operational controller candidate**

Derive it from the verified R5 controller. Remove the Web parity HTTP call and
its parsing. Keep the guard's stuck counter at zero because the observer now
owns it. Reject the controller if its source contains
`message-pipeline-parity`; compile it and record its SHA-256.

**Step 2: Verify the final local code candidate**

Run compileall, the consolidated focused tests, independent review, and one
complete local pytest suite after the last code edit.

**Step 3: Commit and push explicitly**

Stage only the observer, observer test, this plan, and canonical status as
applicable. Inspect cached paths. Commit and non-force push to
`codex/deepcoin-auto-trading-v1`.

**Step 4: Install outside the production checkout**

Require exact production runtime SHA
`0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`, tracked-clean checkout,
`global + 1 + queue`, stable roles/PIDs, unique ingest session, zero queue,
management, worker command, revision claim and active exchange writes, SQLite
WAL/quick-check, and two identical complete exchange snapshots. Copy the exact
observer and controller only into a new evidence directory.

**Step 5: Run the final clean window**

Atomically cut over to `per_chat + 3`, prove three-sample convergence, and run
one uninterrupted two-hour natural-message window. Any real failed gate or
twice-incomplete query restores the mapped `global` target and independently
proves rollback. Do not manufacture traffic, deploy/restart runtime services,
replay, invoke worker commands, mutate business data/configuration, or make an
exchange write.

**Step 6: Close Phase 7**

On full acceptance, record traffic, chats, ordering, cross-chat proof, queue and
stuck evidence, role health, SQLite/session/authority, exchange parity, evidence
path/digest and final tuple. Mark Phase 7 complete, release the claim, commit and
push the exact status path. Only then is Phase 8 eligible.
