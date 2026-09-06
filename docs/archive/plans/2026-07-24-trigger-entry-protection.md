# Trigger Entry Protection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Apply side-aware endpoint pricing for range entries and guarantee exact-position take-profit convergence after trigger fills.

**Architecture:** `deepcoin_order_builder` derives both range-leg prices from the configured percentage and preserves the existing hybrid market first leg. The trigger TP convergence becomes the common post-fill path for one or more TP targets; its executor accepts only verified stop evidence tied either directly to the exact pending position or to the durable trigger-parent adoption record.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, Deepcoin REST client.

---

### Task 1: Change range-leg price derivation

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:102-183,380-421`
- Test: `tests/test_deepcoin_order_builder.py`

**Step 1: Write failing tests**

Add exact-price tests for a BTC long `65000-66000` and BTC short `65000-66000` with `max_market_entry_deviation_pct=0.15`, asserting long `[66099, 65097.5]` and short `[64902.5, 65901]` after tick normalization. Add a hybrid-range test proving its second limit leg is the adjusted opposite endpoint rather than the midpoint.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_deepcoin_order_builder.py`

Expected: FAIL because current prices are the unadjusted edge/midpoint values.

**Step 3: Write minimal implementation**

Pass the configured deviation into range-leg construction. Create a single side-aware helper that selects the long high/low or short low/high sequence and applies `1 +/- deviation / 100`; use it for ordinary range legs and the hybrid second leg. Preserve the existing market-first eligibility check and contract tick normalization.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_deepcoin_order_builder.py`

Expected: PASS.

### Task 2: Queue a post-fill convergence for every trigger target plan

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py:19-64`
- Modify: `src/telegram_kol_research/recovery_live_submit.py:674-681`
- Test: `tests/test_trigger_take_profit_convergence.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Write failing tests**

Add tests that a trigger entry with one `{price, allocation_pct=100}` target creates a waiting convergence and that a non-100 single target is rejected. Retain existing multi-target expectations.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence.py tests/test_recovery_live_submit.py`

Expected: FAIL because the constructor rejects a one-target plan and submission only creates rows for more than one target.

**Step 3: Write minimal implementation**

Allow exactly one normalized target only when its allocation totals 100. Create a convergence whenever the trigger draft has at least one TP target. Do not place TP inside the original trigger payload.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence.py tests/test_recovery_live_submit.py`

Expected: PASS.

### Task 3: Accept durable parent-intent stop proof during TP preflight

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py:231-265`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`

**Step 1: Write failing tests**

Make the fake pending stop omit `posId`, create a verified stop-ledger row with evidence source `reconciliation_trigger_protection_intent` and its matching parent trigger ID, then assert the plan is ready. Add a mismatch-parent test that remains `convergence_verified_stop_missing`.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py`

Expected: FAIL because the executor currently accepts only a pending stop containing `posId`.

**Step 3: Write minimal implementation**

Add a narrowly scoped proof helper: accept an otherwise verified stop row only when its JSON evidence contains the exact parent trigger ID from the same entry leg's durable trigger-protection intent and its trigger price agrees. Keep the exact live position, entry leg, side, ledger, and no-existing-TP gates unchanged.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py`

Expected: PASS.

### Task 4: Validate and review

**Files:**
- Modify: `docs/plans/2026-07-24-trigger-entry-protection-design.md`
- Test: `tests/test_deepcoin_order_builder.py`
- Test: `tests/test_trigger_take_profit_convergence.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Run the focused suite**

Run: `uv run pytest -q tests/test_deepcoin_order_builder.py tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_recovery_live_submit.py`

Expected: PASS.

**Step 2: Run the full local suite**

Run: `uv run pytest -q`

Expected: PASS.

**Step 3: Review the diff**

Run: `git diff --check && git diff -- src/telegram_kol_research/deepcoin_order_builder.py src/telegram_kol_research/trigger_take_profit_convergence.py src/telegram_kol_research/recovery_live_submit.py src/telegram_kol_research/trigger_take_profit_convergence_executor.py tests`

Expected: only the approved behavior, tests, and design documents are changed.

**Step 4: Commit**

Run: `git add docs/plans/2026-07-24-trigger-entry-protection-design.md docs/plans/2026-07-24-trigger-entry-protection.md src/telegram_kol_research/deepcoin_order_builder.py src/telegram_kol_research/trigger_take_profit_convergence.py src/telegram_kol_research/recovery_live_submit.py src/telegram_kol_research/trigger_take_profit_convergence_executor.py tests/test_deepcoin_order_builder.py tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_recovery_live_submit.py && git commit -m "fix: converge trigger entry take profits"`

Expected: a focused reviewed commit ready for server deployment.
