# Phase 1 Event-Loop Database Offload Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move the proven reconcile, lifecycle-expiry, and Bot database operations off asyncio event loops without changing behavior.

**Architecture:** Use `asyncio.to_thread()` for ordinary synchronous read/database slices and the existing `mgmt-worker` for lifecycle and operator mutation paths. Keep network awaits, notifications, lock scope, ordering, and cancellation semantics unchanged.

**Tech Stack:** Python, asyncio, SQLAlchemy, pytest.

---

## Task 1: Claim the workstream

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

1. Verify a clean exclusive worktree, current branch/HEAD, no other active claim, and that the approved design correction is an ancestor.
2. Replace the stale plan pointers with the new design, master plan, `current_phase: phase_1_event_loop_db_offload`, and this file as `current_phase_file`.
3. Record the exact claim owner, base SHA, local-only authorization, and stop conditions.
4. Commit only the status file. Do not push.

## Task 2: Offload ingest reconcile database slices

**Files:**

- Modify: `tests/test_reconcile.py`
- Modify if needed: `tests/test_reconcile_live_history.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`

1. Add a heartbeat RED test that injects a blocking synchronous database slice into `run_reconcile_once()` and proves the loop currently stops advancing.
2. Extract small synchronous helpers for checkpoint/settings load, history-checkpoint projection, per-dialog media projection, and normalized-message persistence used by the production-active path.
3. Await those helpers with `asyncio.to_thread()`. Keep Telethon fetches and async notification delivery on the loop; do not thread the whole coroutine.
4. Run the focused reconcile tests and commit the exact source/test paths.

## Task 3: Serialize lifecycle expiry-review database work

**Files:**

- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`
- Modify: `tests/test_lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`

1. Add a RED heartbeat test for `_request_pending_expiry_reviews()` and a test proving it queues behind existing management work.
2. Extract one synchronous unit that selects candidates, evaluates pending-leg context, conditionally claims/commits rows, and returns bounded notification payloads.
3. Invoke that unit through `run_on_management_worker()`, then await notifier calls on the event loop.
4. Preserve the current state transitions, payloads, timestamps, commit boundary, and single-thread ordering. Run focused tests and commit exact paths.

## Task 4: Offload Bot database commands

**Files:**

- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`
- Modify: `tests/test_telegram_bot_commands.py`
- Modify: `tests/test_runtime_event_loop_blocking_census.py`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py`

1. Add RED heartbeat tests for `/positions`, `/pending`, and the system-operator text command path.
2. Await the two read-only formatters with `asyncio.to_thread()`.
3. Introduce the smallest command-unit wrapper needed to run `process_system_operator_command()` and any required Deepcoin client construction on `mgmt-worker`.
4. Reuse the callback path's cancellation contract: queued cancellation prevents execution; started work is drained before cancellation is propagated.
5. Remove only the three corrected Bot entries from `KNOWN_BLOCKING_CALLS`; fix the stale “four calls” comment. Keep reviewed pure helpers.
6. Run focused Bot, executor, census, lifecycle, and reconcile tests.

## Task 5: Close the phase

1. Run `git diff --check` and compile the touched modules.
2. Inspect the diff for any recognition, strategy, execution, position-ownership, exchange-write, schema, pool-size, or executor-count change; there must be none.
3. Update the canonical status with RED/GREEN commands/results, exact commits, and `current_phase_file` for Phase 2.
4. Stage explicit paths, verify `git diff --cached --name-only`, and commit locally. No full suite, push, deployment, restart, or production action in this phase.
