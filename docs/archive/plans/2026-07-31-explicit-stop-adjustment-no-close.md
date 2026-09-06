# Explicit Stop Adjustment Safety Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure an explicit stop-loss price can only produce a validated stop adjustment and can never fall through to break-even market close behavior.

**Architecture:** Normalize and prove explicit price provenance in the deterministic directive resolver, then give explicit stop adjustments precedence over generic protection labels. Preserve the existing planner and executor boundaries so `adjust_stop_loss` remains a protection-only mutation while price-less cost protection remains market-aware break-even handling.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, Deepcoin management planner/executor.

---

### Task 1: Reproduce the production directive failure

**Files:**
- Modify: `tests/test_management_directives.py`

**Step 1: Write the failing test**

Add a regression using the production wording, `management_action=move_stop_to_protect`, and numeric evidence `stop_loss=61900.0`. Assert that the resolved intent is `adjust_stop_loss`, the stop is retained, and its source is `current_message_text`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_management_directives.py -k explicit_stop -vv`

Expected: FAIL because the current resolver returns `move_stop_to_break_even` and discards the stop price.

### Task 2: Implement numeric source matching and precedence

**Files:**
- Modify: `src/telegram_kol_research/management_directives.py`
- Test: `tests/test_management_directives.py`

**Step 1: Implement the minimal fix**

Add a bounded numeric-token helper that compares finite positive decimal values from the current message with the structured stop value. Resolve a proven explicit stop as `adjust_stop_loss` before generic break-even classification.

**Step 2: Preserve price-less break-even behavior**

Add or retain a test showing that `做好成本保护` with no explicit price still resolves to `move_stop_to_break_even`.

**Step 3: Run focused tests**

Run: `pytest tests/test_management_directives.py -vv`

Expected: PASS.

### Task 3: Prove the recognition-to-candidate boundary

**Files:**
- Modify: `tests/test_message_recognition.py`

**Step 1: Write the failing integration regression if needed**

Exercise the lifecycle application path with the production wording and model action. Assert the persisted candidate uses `adjust_stop_loss`, carries `61900`, records `current_message_text`, and does not use `move_stop_to_break_even`.

**Step 2: Run the focused test**

Run: `pytest tests/test_message_recognition.py -k explicit_stop -vv`

Expected: PASS after Task 2; if it exposes a separate propagation defect, make the smallest source fix and repeat red/green.

### Task 4: Prove no close batch can be planned

**Files:**
- Modify: `tests/test_strategy_management_planner.py`
- Modify only if required: `src/telegram_kol_research/strategy_management_planner.py`

**Step 1: Add a planner regression**

Persist the production-shaped `adjust_stop_loss` candidate and assert the planner never selects `break_even_by_market` or `full_exit`. For the actual 61900 versus old 62400 long scenario, assert the request is blocked as non-risk-tightening rather than closed.

**Step 2: Run the planner regression**

Run: `pytest tests/test_strategy_management_planner.py -k "explicit_stop or adjust_stop" -vv`

Expected: PASS, with a blocked protection-only outcome for the production values.

### Task 5: Run regression suites and review

**Files:**
- No source files expected.

**Step 1: Run focused suites**

Run: `pytest tests/test_management_directives.py tests/test_message_recognition.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_strategy_management_market_decisions.py -q`

Expected: PASS.

**Step 2: Run code review and address all critical/important findings**

Review the diff against commit `a0660a0`, paying particular attention to numeric parsing, ambiguous numbers, long/short validation, and any route from `adjust_stop_loss` to a close action.

**Step 3: Commit**

Commit only the fix, tests, and plan document.

### Task 6: Push, deploy, and verify production

**Files:**
- No additional files expected.

**Step 1: Push**

Push `codex/deepcoin-auto-trading-v1` to GitHub.

**Step 2: Confirm a safe deployment window**

Read production status and recent messages/logs. Do not deploy during an active time-sensitive strategy operation.

**Step 3: Deploy with the existing helper**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1` when PowerShell is available; otherwise use the equivalent existing shell helper.

**Step 4: Verify**

Confirm the server commit, editable package, active `telegram-kol.service`, focused server tests, and recent logs. Perform read-only database/exchange checks only; do not submit a synthetic management instruction or trade.
