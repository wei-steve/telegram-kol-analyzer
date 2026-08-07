# Trigger Protection Rescue Starvation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure a due trigger-protection rescue executes before adoption retry backoff can starve it, and safely recover the current evidence-backed terminal intent.

**Architecture:** Reorder the existing reconciliation/rescue orchestration without creating a new trading path. Extend terminal eligibility only for immutable deferred-refusal evidence, while retaining the existing exact-position rescue preflight and durable executor.

**Tech Stack:** Python, SQLAlchemy, pytest, systemd, Deepcoin REST integration.

---

### Task 1: Reproduce rescue starvation

**Files:**
- Modify: `tests/test_trigger_protection_rescue_worker.py`
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

### Task 3: Recover evidence-backed exhausted intents

**Files:**
- Modify: `tests/test_trigger_protection_rescue_worker.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `src/telegram_kol_research/trigger_protection_rescue_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1:** Add RED tests proving a failed `manual_review` intent with a `trigger_protection_candidate_predates_fill` refusal is discovered and passes deferred-evidence eligibility, while a generic manual-review intent remains blocked.

**Step 2:** Persist refusal reason and bounded evidence when scheduling future retries.

**Step 3:** Allow the worker to discover terminal manual-review rows, and extend `_rescue_intent_is_deferred_or_ambiguous` to recognize `predates_fill`; leave `_prepare_trigger_protection_stop_rescue` as the final authority.

**Step 4:** Run focused tests and expect GREEN.

**Step 5:** Commit the terminal recovery fix.

### Task 4: Regression and review

**Files:**
- Test: `tests/test_trigger_protection_rescue_worker.py`
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

**Step 3:** Allow one natural reconciliation tick.  Require one durable rescue for intent 105 and exact exchange readback of the 66160 stop.  If the result is unknown or blocked, do not retry automatically.

**Step 4:** Verify the existing workers add one distinct backup stop and the staged 63250 take profit, all exact-owned by binding 264 / leg 469 / the unchanged position.  Require the strict repair dry-run to return no action or conflict.

**Step 5:** Run the independent no-notify monitor diagnostic and server-focused tests.  Record exact bounded evidence in the status file, commit, and push the documentation checkpoint without redeploying it.
