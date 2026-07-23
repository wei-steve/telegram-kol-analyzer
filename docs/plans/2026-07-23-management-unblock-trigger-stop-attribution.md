# Management Unblock and Trigger Stop Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Safely execute a later full-exit instruction after a fully restored failed protection update, and adopt uniquely evidenced stop-only trigger protection when Deepcoin omits `posId`.

**Architecture:** Keep the strategy lock for unresolved batches; resolve only a provably restored `partial_failed` predecessor before creating its successor. Add a narrow missing-`posId` adoption branch, then reuse the ledger and staged take-profit convergence paths.

**Tech Stack:** Python 3, SQLAlchemy, SQLite, pytest.

---

### Task 1: Safe predecessor resolution for full exits

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_planner.py`

**Step 1: Write the failing tests**

Add a test in which a `full_exit` follows a `partial_failed` protection batch whose management legs are all `restored`; expect a ready full-close batch and a resolved predecessor. Add a test with a `planned` or `submit_unknown` predecessor leg; expect `prior_partial_batch_unresolved` and no successor.

**Step 2: Run the test to verify RED**

Run: `pytest tests/test_strategy_management_planner.py -k 'restored or unresolved' -v`

Expected: the safe-successor case fails because every `partial_failed` predecessor freezes the planner.

**Step 3: Implement the minimum safe release**

Add a repository helper that resolves only the qualifying predecessor under the planner lock. Invoke it only for `full_exit`, after exact identity/idempotency validation. Keep the existing unique-index predicate and preserve locks for all other states.

**Step 4: Run the test to verify GREEN**

Run: `pytest tests/test_strategy_management_planner.py -k 'restored or unresolved' -v`

Expected: PASS.

**Step 5: Commit**

Run: `git add src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_batches.py tests/test_strategy_management_planner.py && git commit -m "fix: unblock full exit after restored management failure"`

### Task 2: Strict missing-position stop-only adoption

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Test: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write the failing tests**

Add a stop-only post-baseline pending TPSL candidate without `posId` whose instrument, side, full size, and stop price exactly match the immutable parent request; expect a `stop_loss` action. Retain refusal tests for two matching candidates and for a returned different `posId`.

**Step 2: Run the test to verify RED**

Run: `pytest tests/test_entry_protection_ledger_repair.py -k 'stop_only or explicit_returned_position_id' -v`

Expected: the acceptance case fails as `trigger_protection_candidate_position_invalid`.

**Step 3: Implement the minimum exact-evidence branch**

Accept an absent `posId` only for one post-baseline pending candidate that is stop-only and exact-matches the immutable request. Preserve baseline, history-proof, ownership, and ledger collision checks. Record evidence match `trigger_protection_intent_stop_only_missing_pos_id`.

**Step 4: Run the test to verify GREEN**

Run: `pytest tests/test_entry_protection_ledger_repair.py -k 'stop_only or explicit_returned_position_id' -v`

Expected: PASS.

**Step 5: Commit**

Run: `git add src/telegram_kol_research/entry_protection_ledger_repair.py tests/test_entry_protection_ledger_repair.py && git commit -m "fix: adopt exact stop-only trigger protection without pos id"`

### Task 3: Regression and production verification

**Files:**

- Modify: `docs/plans/2026-07-23-management-unblock-trigger-stop-attribution.md`

**Step 1: Run focused and full local verification**

Run: `pytest tests/test_strategy_management_planner.py tests/test_entry_protection_ledger_repair.py -v && pytest -q`

Expected: all tests pass.

**Step 2: Commit the plan status**

Run: `git add docs/plans/2026-07-23-management-unblock-trigger-stop-attribution.md && git commit -m "docs: record management and trigger protection verification"`

**Step 3: Push and update production**

Push the reviewed commits to `codex/deepcoin-auto-trading-v1`, then run `powershell -ExecutionPolicy Bypass -File .\\scripts\\server_git_update.ps1`.

**Step 4: Verify production read-only**

Confirm the deployed SHA and active service. Inspect message 9679's instruction item, predecessor/successor batches, both exact BTC positions, the BTC/ETH trigger intents, ledgers, pending TPSL orders, and ETH staged take-profit convergence. Do not submit, cancel, or directly edit production data during verification.
