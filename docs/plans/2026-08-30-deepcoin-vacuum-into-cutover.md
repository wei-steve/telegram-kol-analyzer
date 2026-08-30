# Deepcoin `VACUUM INTO` Cutover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the failed incremental backup with a minimal verified `VACUUM INTO` backup and complete the existing production cutover.

**Architecture:** Preserve `_create_verified_backup()` and the existing cutover orchestration. Only its copy primitive changes; all source/path/integrity/count/hash checks and the post-backup transaction recheck remain.

**Tech Stack:** Python 3.11, SQLite, SQLAlchemy, pytest, git, existing exact-SHA deployment helpers.

---

### Task 1: Prove the copy primitive switch

**Files:**
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

1. Add a test whose source connection raises if `Connection.backup()` is used.
2. Run that test and verify RED on the current incremental implementation.

### Task 2: Implement the minimal backup

**Files:**
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

1. Replace destination connection and `source.backup(...)` state with a single
   parameterized `source.execute("VACUUM INTO ?", (str(backup_path),))`.
2. Preserve exclusive destination creation, inode binding, mode, verification,
   count, streaming hash and post-hash checks.
3. Remove tests coupled only to progress batches, BUSY callbacks and pinned
   incremental snapshots; retain behavior and safety tests.
4. Run the RED test, backup tests and adjacent reconciliation tests to GREEN.

### Task 3: Verify and commit the reviewed candidate

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

1. Run static checks and the focused reconciliation/deployment suite.
2. Run one final repository test suite.
3. Review the exact base-to-HEAD diff and fix any P0/P1 issue with focused tests.
4. Record concise evidence, explicitly stage only touched paths, commit, verify a
   clean tree and push exact HEAD to `codex/deepcoin-auto-trading-v1`.

### Task 4: Complete production cutover

**Files:**
- Use the existing reviewed action manifests and deployment helpers; do not add
  a new protocol.

1. Reprove `maintenance_stopped`, zero runtime processes and fresh complete
   read-only exchange/account evidence.
2. Create and verify the `VACUUM INTO` backup, recheck counts under
   `BEGIN IMMEDIATE`, terminalize the seven already-manually-cancelled local
   targets and seed the idle authority row.
3. Stage and activate the exact reviewed SHA with the existing rollback and
   authorization boundaries.
4. Verify cache ownership/ACL/freshness, runtime topology, backlog, duplicate
   processing, exchange state and future-only message handling; observe at the
   project-defined risk-adaptive level and record the evidence path.
