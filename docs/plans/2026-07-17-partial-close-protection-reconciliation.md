# Partial-Close Protection Reconciliation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make an ordinary partial close report success only after each remaining exact position has current, attributable TPSL evidence, without blocking the close itself or unrelated strategies.

**Architecture:** Reuse the coherent reconciliation snapshot and existing
`match_position_protection` matcher after close-leg confirmation.  Keep the
close facts durable; downgrade only the affected batch to recovery-required
when the remaining positions' protection is absent or ambiguous.

**Tech Stack:** Python, SQLAlchemy, SQLite, pytest, Deepcoin read-only snapshot
adapters.

---

### Task 1: Reproduce stale protection after a confirmed partial close

**Files:**

- Modify: `tests/test_strategy_management_reconciliation.py`

1. Write a test with a `partial_close` batch whose close leg is confirmed and
   whose remaining exchange position carries ambiguous/stale TPSL evidence.
2. Run the one test; it should currently fail because reconciliation marks the
   batch `succeeded`.
3. Commit the test checkpoint if the worktree has no unrelated changes.

### Task 2: Reconcile remaining partial-close protection

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `tests/test_strategy_management_reconciliation.py`

1. Add a small helper that uses the already-loaded snapshot positions and
   pending trigger rows with `match_position_protection`.
2. After confirming all ordinary partial-close legs, require verified
   protection for every remaining exact `posId` before the batch transitions
   to `succeeded`.
3. Otherwise transition the batch to `recovery_required` with
   `partial_close_protection_unverified`; do not alter confirmed close-leg
   status or submit any exchange request.
4. Run focused reconciliation tests, then the full reconciliation and
   protection matcher suites.

### Task 3: Regression verification

**Files:**

- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_protection_attribution.py`

1. Confirm verified post-close protection remains successful.
2. Confirm full-close and composite protection workflows retain their existing
   behavior.
3. Run the relevant test suites and review the diff before committing.
