# Batch119 Empty Legacy Position Snapshot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow an exact empty Batch119 legacy position snapshot to reach fresh read-only exchange verification without weakening terminal proof or apply safety.

**Architecture:** Keep the durable legacy snapshot schema closed and continue requiring zero matching regular orders.  Accept either zero position rows or the existing one exact row; leave fresh position/natural-stop classification and all apply revalidation unchanged.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest.

---

### Task 1: Reproduce the empty legacy snapshot refusal

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `tests/test_bound_close_batch119_joint_recovery.py`

**Step 1: Write failing tests**

Add a helper that changes only `last_exchange_snapshot_json` to the exact closed
shape below:

```python
{
    "position_rows": [],
    "matching_regular_orders": [],
}
```

Assert that:

- a complete fresh natural-stop snapshot produces a ready
  `position_absent` plan;
- an incomplete fresh absent-position snapshot remains refused;
- a fresh live position continues through the normal live-position path;
- joint `joint_diagnostic` admission is ready with the production-shaped empty
  legacy snapshot.

Retain or add counterexamples for two legacy position rows, a matching regular
order, and extra provider fields.

**Step 2: Run RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  -k 'empty_legacy_position or nonexact_legacy_exchange_snapshot'
```

Expected: the new ready assertions fail with
`false_submission_state_mismatch`; the incomplete/counterexample assertions
already pass.

**Step 3: Commit RED evidence**

```bash
git add tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py
git commit -m "test: reproduce empty Batch119 legacy snapshot refusal"
```

### Task 2: Implement the minimal admission fix

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`

**Step 1: Implement the smallest branch**

In `_legacy_false_exchange_snapshot_refusal()`:

1. keep the exact top-level key check;
2. require `position_rows` to be a list with at most one item;
3. require `matching_regular_orders == []`;
4. return success immediately for the exact empty list;
5. run the existing closed row-shape and identity checks for the one-row form.

Do not modify fresh capture classification, natural-stop proof, apply, database
schema, joint writer policy, or deployment preflight.

**Step 2: Run GREEN**

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  -k 'empty_legacy_position or nonexact_legacy_exchange_snapshot'
```

Expected: all selected tests pass.

**Step 3: Run affected suites**

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_deployment_preflight.py \
  tests/test_cli_smoke.py
.venv/bin/python -m compileall -q src/telegram_kol_research tests
git diff --check
```

**Step 4: Commit implementation**

```bash
git add src/telegram_kol_research/composite_management_batch_recovery.py
git commit -m "fix: admit empty Batch119 legacy position snapshot"
```

### Task 3: Final verification and review

**Files:**
- Review the complete range from the design commit through Task 2.

**Step 1: Request independent review**

Require 0 Critical and 0 Important findings.  The reviewer must specifically
verify that empty legacy evidence cannot itself produce terminal proof or apply
authority and that all one-row mismatch checks remain intact.

**Step 2: Run full repository tests**

```bash
.venv/bin/pytest -q
git status --short
git diff --check
```

**Step 3: Stop at the push boundary**

Report the exact reviewed SHA and request explicit push approval.  Do not push,
deploy, run a new production capture, apply recovery, or enable MiMo v2.

