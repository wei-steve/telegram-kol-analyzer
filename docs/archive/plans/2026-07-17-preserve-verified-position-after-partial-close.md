# Preserve Verified Position After Partial Close Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve authoritative verified Deepcoin position ownership after a confirmed partial close changes the live position size.

**Architecture:** Keep `execution_order_legs` as the ownership authority. Update the attribution matcher so exact authoritative `posId` identity can survive mutable size drift, while weak legacy evidence remains conflict-prone. Do not loosen TPSL mutation gates or submit compensation trades.

**Tech Stack:** Python 3.12, SQLAlchemy 2, pytest, SQLite, Deepcoin read-only reconciliation evidence.

---

### Task 1: Add Position-Attribution Unit Coverage

**Files:**
- Modify: `tests/test_position_attribution.py`

**Step 1: Write the failing test**

Add a test where one `LegEvidence` has `pos_id="pos-partial"` plus policy-v2 authoritative evidence shape, requested size `9`, and one live `PositionEvidence` has the same `pos_id`, same symbol/side, but size `5`. Assert `match_entry_legs_to_positions` assigns the leg to the position with evidence type `direct_pos_id`.

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_position_attribution.py::test_authoritative_direct_pos_id_survives_partial_close_size_drift -v
```

Expected: FAIL because `_build_best_edge` rejects the pair before direct `posId` matching due to size mismatch.

**Step 3: Implement the minimal matcher change**

In `src/telegram_kol_research/position_attribution.py`, allow an exact direct `posId` edge before `_compatible_size(...)` only when the leg has authoritative persisted position evidence and instrument/side match.

**Step 4: Run focused attribution tests**

Run:

```bash
python -m pytest tests/test_position_attribution.py -q
```

Expected: PASS.

### Task 2: Add Reconciliation Regression Coverage

**Files:**
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write the failing reconciliation test**

Create a binding with one active verified authoritative entry leg where `order_id == pos_id`, request size `9`, and current Deepcoin position size `5`. Run `reconcile_deepcoin_execution_bindings` and assert the leg remains `verified`, binding remains `active`, and `last_exchange_status == "position_ownership_verified"`.

**Step 2: Run test to verify it fails before the matcher change**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_reconcile_preserves_verified_position_after_partial_close_size_drift -v
```

Expected before implementation: FAIL with `attribution_conflict`.

**Step 3: Verify after Task 1 implementation**

Run the same test again.

Expected: PASS.

**Step 4: Run nearby reconciliation tests**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_reconcile_does_not_grandfather_legacy_weak_verified_position tests/test_execution_bindings.py::test_reconcile_preserves_verified_position_after_partial_close_size_drift -q
```

Expected: PASS.

### Task 3: Recover Already-Demoted Authoritative Legs

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1: Write the failing recovery test**

Create a leg with `attribution_status="attribution_conflict"` and a live exact `posId`, plus a prior `position_attribution_audits` row proving `ownership_verified -> verified` for the same leg and `posId`. Assert reconciliation restores the leg and binding to verified ownership.

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_reconcile_recovers_prior_verified_position_after_old_conflict -v
```

Expected before implementation: FAIL with binding still `unknown`.

**Step 3: Implement the minimal recovery gate**

When building reconcile leg evidence, pass through the direct persisted `posId` for already-demoted legs only if `position_attribution_audits` contains a prior verified ownership audit for the exact same leg and `posId` with the current attribution policy.

**Step 4: Run focused recovery and safety tests**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_reconcile_recovers_prior_verified_position_after_old_conflict tests/test_execution_bindings.py::test_reconcile_does_not_grandfather_legacy_weak_verified_position -q
```

Expected: PASS.

### Task 4: Terminalize Missing Split Legs With History Proof

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1: Write the failing split-leg terminalization test**

Create a binding with two verified entry legs: one live position and one missing position. Make the fake Deepcoin history prove the missing exact `posId` is fully closed. Assert `sync_manual_closed_deepcoin_positions` marks only the missing leg as `manually_closed`, keeps the binding active, and leaves the lifecycle `entered`.

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_sync_manual_closed_positions_terminalizes_only_missing_verified_leg -v
```

Expected before implementation: FAIL because the sync skips the binding when any position remains live.

**Step 3: Implement exact missing-leg sync**

When a binding has some live positions, inspect missing verified entry legs. For each missing leg, require `list_position_history(inst_id, pos_id)` to prove same instrument, same side, positive `pos`, and `closePos == pos`. Terminalize only that leg and re-derive the binding from remaining live verified legs.

**Step 4: Run focused manual-close tests**

Run:

```bash
python -m pytest tests/test_execution_bindings.py::test_sync_manual_closed_positions_terminalizes_only_missing_verified_leg tests/test_execution_bindings.py::test_reconcile_then_sync_closes_a_previously_verified_missing_position -q
```

Expected: PASS.

### Task 5: Focused Verification And Deployment

**Files:**
- Modify only if needed: `docs/migration-handoff.md`

**Step 1: Run focused local tests**

Run:

```bash
python -m pytest tests/test_position_attribution.py tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 2: Review diff**

Run:

```bash
git diff -- src/telegram_kol_research/position_attribution.py tests/test_position_attribution.py tests/test_execution_bindings.py docs/plans/2026-07-17-preserve-verified-position-after-partial-close-design.md docs/plans/2026-07-17-preserve-verified-position-after-partial-close.md
```

Expected: No exchange mutation, no production DB write, and no TPSL safety loosening.

**Step 3: Commit and push**

Commit with:

```bash
git add docs/plans/2026-07-17-preserve-verified-position-after-partial-close-design.md docs/plans/2026-07-17-preserve-verified-position-after-partial-close.md src/telegram_kol_research/position_attribution.py tests/test_position_attribution.py tests/test_execution_bindings.py
git commit -m "fix: preserve verified position after partial close"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Deploy through the approved helper**

Run:

```bash
./scripts/server_git_update.sh
```

**Step 5: Read-only production verification**

Confirm server HEAD, active service, DB quick check, no new Deepcoin write caused by verification, and the previously conflicting positions regain verified ownership after reconciliation.
