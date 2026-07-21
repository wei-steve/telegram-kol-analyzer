# Managed TPSL Position Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show and alert from a later managed TPSL order using its persisted exact position ownership, while leaving unowned exchange orders fail-closed.

**Architecture:** Add an optional exact order-ID-to-position-ID evidence map to the pure protection matcher. The live position loader obtains that map only from active, verified execution legs and their protection-ledger rows, then passes it into the matcher. The matcher merges those exact rows with inline position fields before considering its existing timestamp/size heuristic.

**Tech Stack:** Python 3, SQLAlchemy, pytest, FastAPI view model helpers.

---

### Task 1: Prove exact managed evidence supplements inline protection

**Files:**
- Modify: `tests/test_protection_attribution.py`
- Modify: `src/telegram_kol_research/protection_attribution.py`

**Step 1: Write the failing test**

Add a position with inline TP `63100` and a standalone SL order at `67200` created hours later. Supply exact ownership evidence mapping that order ID to the position ID. Assert the result is verified with TP `63100`, SL `67200`, and the stop order ID.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_protection_attribution.py -k managed -v`

Expected: FAIL because `match_position_protection` has no exact managed-evidence input.

**Step 3: Write minimal implementation**

Add an optional exact-evidence mapping argument. Add only pending TPSL rows whose IDs are mapped to the same live position into that position's exact row set, then preserve existing matching rules for every other row.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_protection_attribution.py -k managed -v`

Expected: PASS.

### Task 2: Source exact evidence from the durable ledger for the live UI

**Files:**
- Modify: `tests/test_web_app.py` (or the focused live-position test module)
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: Write the failing test**

Create a verified active execution leg and matching protection-ledger stop order for a split position whose direct Deepcoin position row has TP but no SL. Assert its rendered view model shows stop `67200` and does not label the position `无止损`.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_web_app.py -k managed_tpsl -v`

Expected: FAIL because the loader passes no ledger-backed exact evidence to the matcher.

**Step 3: Write minimal implementation**

Load only ledger rows for active live `pos_id`s whose execution leg is verified and belongs to an active binding. Pass a `{order_id: pos_id}` map only for exact, non-empty IDs to `match_position_protection`.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_web_app.py -k managed_tpsl -v`

Expected: PASS.

### Task 3: Regression suite and review

**Files:**
- Test: `tests/test_protection_attribution.py`
- Test: focused web-app test module

**Step 1: Run focused tests**

Run: `python3 -m pytest tests/test_protection_attribution.py tests/test_web_app.py -v`

Expected: PASS.

**Step 2: Run relevant broader tests**

Run: `python3 -m pytest tests/test_strategy_management_executor.py tests/test_reconcile.py -v`

Expected: PASS.

**Step 3: Commit**

```bash
git add src/telegram_kol_research/protection_attribution.py src/telegram_kol_research/web_app.py tests/
git commit -m "fix: attribute managed TPSL to exact position"
```
