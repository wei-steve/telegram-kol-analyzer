# Trigger Stop-Only Staged Take-Profit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make DeepCoin trigger entries submit only stop loss and create all staged take profits once the exact filled split position is verified, with permanent per-leg TP audit history.

**Architecture:** Retain the durable convergence and TP order ledger already introduced locally, but remove embedded TP from trigger payloads and remove all old-TP cancellation from the first convergence path. Reconciliation releases an exact filled leg only when its stop is visible; the worker submits the complete TP allocation once and freezes any unknown outcome or later partial-position ambiguity.

**Tech Stack:** Python, SQLAlchemy, pytest, DeepCoin TPSL APIs, FastAPI strategy projections.

---

### Task 1: Make trigger entries stop-only

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1:** Write a failing trigger-payload test proving it contains SL fields but no `tp*` field even for a multi-TP draft.

**Step 2:** Run `.venv/bin/python -m pytest tests/test_recovery_live_submit.py::test_build_deepcoin_trigger_order_payload_is_stop_only_for_staged_take_profit -q`; expect FAIL.

**Step 3:** Change `build_deepcoin_trigger_order_payload` / embedded-field builder so trigger payloads receive only validated SL fields. Keep full TP plan in the durable convergence record.

**Step 4:** Re-run the targeted test; expect PASS.

**Step 5:** Commit `feat: submit trigger entries with stop only`.

### Task 2: Plan first TP creation without any TP cancellation

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`

**Step 1:** Write a failing test for a verified 10-contract trigger leg with visible SL and no pending TP. Assert plan payloads are `64500×5`, `63800×3`, `63100×2`, cancellation IDs are empty, and all payloads omit SL fields.

**Step 2:** Run the targeted pytest node; expect FAIL.

**Step 3:** Replace first-run preflight with a fail-closed condition: only `active` TP ledger/pending orders block creation. Do not cancel any TP in this path; require an exact verified live position and verified visible stop.

**Step 4:** Re-run the test; expect PASS.

**Step 5:** Commit `feat: create staged take profits after trigger fill`.

### Task 3: Submit TP orders once and retain history

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`

**Step 1:** Write a failing test that the executor makes only three `set_position_sltp` calls, records all returned TP IDs as active, never calls `cancel_trigger_order`, and marks response uncertainty `submit_unknown` with no retry.

**Step 2:** Run the relevant test nodes; expect FAIL.

**Step 3:** Implement reserve-before-submit and persist every response before continuing. Preserve already-created TP records if a later submission is unknown.

**Step 4:** Re-run; expect PASS.

**Step 5:** Commit `feat: submit staged trigger take profits once`.

### Task 4: Read-only reconciliation, worker, detail and alerts

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_records.py`

**Step 1:** Write failing tests for terminal TP history projection, unexplained partial-size conflict, worker execution only when enabled, and active/history grouping by entry leg.

**Step 2:** Run those focused suites; expect FAIL where behavior is missing.

**Step 3:** Reconcile known order IDs using pending/history snapshots; freeze unexplained partial exits. Run ready convergence tasks through the existing management worker only when live management is enabled. Project active/history rows and convergence state by leg.

**Step 4:** Re-run focused suites; expect PASS.

**Step 5:** Commit `feat: reconcile and expose trigger take profit history`.

### Task 5: Full verification and controlled rollout

**Files:**
- Modify: `docs/plans/2026-07-22-trigger-leg-staged-take-profit.md`

**Step 1:** Run `.venv/bin/python -m pytest tests/test_recovery_live_submit.py tests/test_position_take_profit_orders.py tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_execution_bindings.py tests/test_strategy_management_worker.py tests/test_strategy_records.py -q`.

**Step 2:** Run `.venv/bin/python -m pytest -q`.

**Step 3:** Commit plan/status documentation, push branch, and deploy code via `scripts/server_git_update.ps1` (or server helper). Verify service and read-only TP projections. Do not mutate the existing historical live position without a separate confirmation.
