# Bounded Stop-Loss Widening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Permit exact, verified BTC and ETH stop-loss widening within fixed price limits while every other widening path remains fail-closed.

**Architecture:** Extend the management-scope safety boundary with a fixed BTC/ETH widening limit table. Keep group fan-out risk-reduction-only, and evaluate the bounded exception only after exact live position ownership has been verified.

**Tech Stack:** Python, SQLAlchemy, pytest

---

### Task 1: Add exact-target widening policy tests

**Files:**
- Modify: `tests/test_management_scope.py`

**Step 1: Write failing tests**

Add parameterized exact-target tests proving:

- BTC long down 700 and short up 700 pass.
- ETH long down 21 and short up 21 pass.
- BTC and ETH moves just beyond the limits fail.
- SOL widening fails.
- An unverified exact target cannot use the exception.
- Existing unscoped/group widening still fails.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/test_management_scope.py -q`

Expected: the four in-limit exact-target cases fail with `stop_adjustment_direction_not_verified`.

### Task 2: Implement the bounded exact-target exception

**Files:**
- Modify: `src/telegram_kol_research/management_scope.py`
- Test: `tests/test_management_scope.py`

**Step 1: Add fixed limits**

Add an immutable mapping with `BTC: 700` and `ETH: 21` price units.

**Step 2: Separate tightening from bounded widening**

Keep `_directive_reduces_lifecycle_risk` unchanged for the group path. Add a helper that accepts an exact target when either the existing risk-reduction check passes or a verified live BTC/ETH target has a finite, positive, in-direction widening delta within its inclusive limit.

**Step 3: Verify ownership before applying the exception**

In the exact-target branch, resolve `_verified_live_target` before considering bounded widening. Preserve deferred exact risk-reducing work when position visibility is temporarily unavailable, but reject widening without verified ownership.

**Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest tests/test_management_scope.py -q`

Expected: PASS.

### Task 3: Verify recognition-to-candidate behavior

**Files:**
- Modify: `tests/test_message_recognition.py`

**Step 1: Add a regression test**

Use an exact verified BTC lifecycle with stop 63800 and an authoritative message requesting 63300. Assert deterministic management scope creates one `adjust_stop_loss` candidate with stop `63300`.

**Step 2: Run the test before implementation changes**

Run the exact node with `uv run pytest ... -q` and confirm it fails under the old safety boundary.

**Step 3: Run after Task 2**

Expected: the candidate is created and the existing planner path remains unchanged.

### Task 4: Review, deploy, and verify

**Files:**
- Modify only if review finds an issue.

**Step 1: Run related suites**

Run: `uv run pytest tests/test_management_scope.py tests/test_management_directives.py tests/test_message_recognition.py tests/test_strategy_management_planner.py tests/test_strategy_management_worker.py -q`

**Step 2: Review the diff**

Confirm there is no widening fan-out, no setting/database migration, and no behavior change for non-BTC/ETH symbols.

**Step 3: Commit and push**

Commit only the plan, implementation, and tests. Push `codex/deepcoin-auto-trading-v1`.

**Step 4: Deploy in a proven safe window**

Use `scripts/server_git_update.ps1`, then verify the deployed SHA, editable package, `telegram-kol.service`, and clean startup logs.

**Step 5: Production verification**

Run focused server tests and read-only checks. Do not replay the historical Mia message or submit a synthetic exchange mutation. Observe the next natural exact-target adjustment.
