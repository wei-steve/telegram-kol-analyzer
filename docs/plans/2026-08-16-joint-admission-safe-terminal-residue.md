# Joint Admission Safe Terminal Residue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Accept exactly 29 actionable reservations plus closed `confirmed` residue and exact-decimal-equivalent Batch 119 quantity steps without weakening another gate.

**Architecture:** Keep validation in the existing query-only transaction. Partition the bounded reservation population into actionable and exact-confirmed rows, fingerprint both, and grant descendant/apply authority only to the actionable 29. Reuse the positive Decimal parser for `quantity_step`; leave every other Batch 119 predicate unchanged.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite query-only transactions, Decimal, pytest.

---

### Task 1: Prove the production-shaped failures

**Files:**
- Modify: `tests/test_bound_close_batch119_joint_recovery.py`
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write confirmed-residue RED tests**

Extend the real joint fixture with nine additional valid
`BoundPositionCloseReservation` rows whose status is exact `confirmed`. Assert
the `joint_diagnostic` result is `ready`, reports 29 reservations and one Batch
119 incident, and has zero blocking writers. Change a non-status field on one
confirmed row and assert a new ready result has a different material fingerprint.

**Step 2: Write closed-status negative tests**

Parametrize one extra residue row as `NULL`, `future_state`, `submitted`, and
another known nonterminal state. Assert each returns `joint_material_invalid`.
Keep the existing proof that changing one of the 29 actionable rows to
`confirmed` refuses because the actionable population becomes 28.

**Step 3: Write quantity-step RED tests**

Using `_seed_batch_119_false_submission`, change only the snapshot
`quantity_step` from `"1"` to `"1.0"`, recompute its target fingerprint, and
assert the planner remains ready. Add negative cases for a different positive
number, zero, negative, and malformed text; each must remain refused.

**Step 4: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py \
  -k 'confirmed_residue or quantity_step_decimal'
```

Expected: confirmed residue is refused because the loader sees 38 rows, and
the equivalent Decimal representation is refused as an identity mismatch.

**Step 5: Commit RED tests**

```bash
git add tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "test: reproduce joint admission production shape"
```

### Task 2: Implement the closed partition and Decimal identity

**Files:**
- Modify: `src/telegram_kol_research/bound_close_batch119_joint_recovery.py`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Test: `tests/test_bound_close_batch119_joint_recovery.py`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Partition the bounded reservation population**

Keep exact schema validation and read the complete table under a reviewed finite
bound. Partition rows with a closed contract:

```python
actionable_rows = [row for row in rows if row["status"] in _TARGET_STATES]
confirmed_rows = [row for row in rows if row["status"] == "confirmed"]
if len(actionable_rows) != _EXPECTED_RESERVATION_COUNT:
    raise ValueError("joint_population_invalid")
if len(actionable_rows) + len(confirmed_rows) != len(rows):
    raise ValueError("joint_population_invalid")
```

Pass only actionable rows to `_load_source_descendants` and derive binding and
position IDs only from them. Include complete canonical confirmed-row material
under `confirmed_residue` in `reservation_source_material`, so it affects the
joint fingerprint but cannot grant raw/apply authority. Preserve the closed
`BOUND_APPLY_POST` contract: the exact target 29 must transition to confirmed;
pre-existing confirmed residue never becomes a target.

**Step 2: Compare quantity steps as bounded Decimals**

Validate the management-leg and snapshot forms with `_positive_decimal` and
compare the returned Decimal values:

```python
leg_step = _positive_decimal(leg.quantity_step, "quantity_step")
snapshot_step = _positive_decimal(row["quantity_step"], "quantity_step")
if leg_step != snapshot_step:
    return "target_snapshot_identity_mismatch"
```

Remove only raw-string equality for this field. Keep positivity, target
fingerprint, and every other identity predicate.

**Step 3: Run GREEN**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py
```

Expected: all pass.

**Step 4: Run adjacent safety suites**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_deployment_preflight.py \
  tests/test_cli_smoke.py
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

Expected: all pass and `src/telegram_kol_research/deployment_preflight.py` has
zero diff.

**Step 5: Commit implementation**

```bash
git add src/telegram_kol_research/bound_close_batch119_joint_recovery.py \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: admit safe joint recovery residue"
```

### Task 3: Full verification and approval boundary

**Files:**
- Verify only: `src/telegram_kol_research/deployment_preflight.py`
- Verify only: repository worktree

**Step 1: Run full verification**

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

Expected: all pass with only existing skips and warnings.

**Step 2: Review safety boundaries**

Request independent Critical/Important review. It must verify that only exact
`confirmed` residue is admitted; residue affects the fingerprint but receives
no apply/raw authority; the actionable population stays exactly 29; malformed
or different Decimal values refuse; ordinary recovery and deployment preflight
do not change; and no write, exchange mutation, replay, notification, deploy,
or MiMo v2 path is added. Resolve every finding with RED-GREEN evidence.

**Step 3: Stop before push and production retry**

Record the final clean commit SHA and request exact push approval. Do not push,
deploy, or reuse consumed production diagnosis/capture tokens.
