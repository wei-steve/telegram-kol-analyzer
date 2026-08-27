# Deepcoin Reviewed Pending Entry Revision Gate Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop terminal, unclaimed `recovery_required` revision batches from falsely blocking the reviewed pending-entry cancellation dry-run while retaining every real revision write-authority gate.

**Architecture:** Reuse the exact batch write-boundary state already enforced by `deployment_active_write_check.py`: `submitting_replacements`. Keep the existing claimed revision-leg and replacement queries unchanged so an in-flight child write remains fail-closed.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, existing Deepcoin cancellation planner and deployment active-write gate.

---

### Task 1: Add revision authority regression coverage

**Files:**
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write the failing terminal-state test**

Seed the normal reviewed target set plus an unrelated `StrategyRevisionBatch`
with status `recovery_required` and no claim fields. Assert that the planner
still returns the complete clean action set with no conflicts.

**Step 2: Run RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py -k "recovery_required_revision_batch_without_claim"
```

Expected: FAIL because the current broad batch query returns
`active_exchange_authority_present`.

**Step 3: Add the active-boundary companion test**

Seed the same batch with status `submitting_replacements`. Assert zero actions
and the global `active_exchange_authority_present` conflict.

### Task 2: Implement the minimal gate correction

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`

**Step 1: Replace only the broad revision batch predicate**

Change the batch query from every status outside `succeeded`/`blocked` to:

```python
StrategyRevisionBatch.status == "submitting_replacements"
```

Do not alter the claimed revision-leg or replacement queries.

**Step 2: Run GREEN**

Run both new tests, then the complete reviewed-cancellation test file.

### Task 3: Verify, review, and commit the candidate

**Files:**
- Verify: `tests/test_deployment_active_write_check.py`
- Verify: reviewed cancellation and adjacent entry/protection tests
- Verify: full repository suite

**Step 1: Run focused and adjacent tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_deployment_active_write_check.py \
  tests/test_legacy_conditional_cancel.py \
  tests/test_entry_revision_executor.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py
```

**Step 2: Run one final full suite**

```bash
.venv/bin/python -m pytest -q
```

**Step 3: Request independent code review**

Review the exact diff for fail-open regressions and missing authority states.
Resolve all Critical and Important findings before committing.

**Step 4: Commit explicit paths**

Stage only the two plan documents, the reviewed cancellation module, and its
test file. Verify the staged path list before committing. Do not push.
