# Trigger Protection Rescue Starvation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure a due trigger-protection rescue executes before adoption retry backoff can starve it, and safely recover the current evidence-backed terminal intent.

**Architecture:** Reorder the existing reconciliation/rescue orchestration without creating a new trading path. Extend terminal eligibility only for immutable deferred-refusal evidence, while retaining the existing exact-position rescue preflight and durable executor.

**Tech Stack:** Python, SQLAlchemy, pytest, systemd, Deepcoin REST integration.

---

### Task 1: Reproduce rescue starvation

**Files:**
- Modify: `tests/test_trigger_protection_stop_rescue.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1:** Add a test whose due retry would be rescheduled by reconciliation and assert the rescue executor is called before that reschedule.

**Step 2:** Run the focused tests and confirm RED because the current orchestration calls `_apply_reconcile_snapshot` first.

**Step 3:** Commit only the failing reproduction.

### Task 2: Reorder rescue before retry scheduling

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_execution_bindings.py`

**Step 1:** Move `run_trigger_protection_rescue_tick` before `_apply_reconcile_snapshot`, keeping both under the existing reconciliation authority boundary.

**Step 2:** Run the new test and the focused execution-binding suite; expect GREEN.

**Step 3:** Commit the minimal ordering fix.

### Task 3: Preserve refusal diagnostics without reopening exhausted intents

**Files:**
- Modify: `tests/test_trigger_protection_stop_rescue.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1:** Add a RED test proving the exact refusal reason and bounded evidence are persisted on a saved intent.

**Step 2:** Persist refusal reason and bounded evidence when scheduling future retries.

**Step 3:** Prove all terminal manual-review rows remain excluded from the bounded periodic worker, including rows with `predates_fill` evidence.

**Step 4:** Run focused tests and expect GREEN.

**Step 5:** Commit the terminal recovery fix.

### Task 4: Regression and review

**Files:**
- Test: `tests/test_trigger_protection_stop_rescue.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_position_management_liveness_recovery.py`
- Test: `tests/test_trigger_protection_intents.py`

**Step 1:** Run all focused and adjacent protection, ownership, mutation-gateway, and CLI tests.

**Step 2:** Run `git diff --check` and review for any path that weakens exact position identity, durable reservation, or idempotency.

**Step 3:** Commit any test-only boundary additions and push to `codex/deepcoin-auto-trading-v1`.

### Task 5: Deploy and converge the current position

**Files:**
- Modify after verification: `docs/runtime-incident-agent-status.md`

**Step 1:** On production, prove two quiet passes with no recognition, management, component, close, position mutation, rescue, Runtime Agent, notification claim, or recovery write in flight; require identical complete exchange snapshots and unchanged intent/position evidence.

**Step 2:** Deploy through `/usr/local/bin/telegram-kol-update` and verify the deployed SHA, four services, and HTTP monitor status.

**Step 3:** Revalidate the target.  If it remains live, require one durable rescue and exact exchange readback of the 66160 stop.  If it has closed, require zero rescue rows and zero exchange writes.

**Step 4:** For a live target, verify primary, backup, and take-profit convergence.  For a closed target, require the strict repair dry-run to return no action or conflict and classify its incident as historical terminal.

**Step 5:** Run the independent no-notify monitor diagnostic and server-focused tests.  Record exact bounded evidence in the status file, commit, and push the documentation checkpoint without redeploying it.
