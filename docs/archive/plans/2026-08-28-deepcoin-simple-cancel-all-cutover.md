# Deepcoin Simple Cancel-All Cutover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the one-time bootstrap and per-order drain machinery, then add one read-only-exchange/local-write reconciliation action for operator-cancelled pending entries before ordinary entry-frozen activation.

**Architecture:** The operator cancels all entry triggers in Deepcoin while the legacy runtime is stopped. One local command rechecks a complete zero-position/zero-order snapshot, backs up SQLite, terminalizes the canonical reviewed targets and seeds the normal idle authority row in one transaction; ordinary scoped activation performs the runtime switch.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy/SQLite, pytest, systemd scoped activation.

---

### Task 1: Retire the one-time maintenance protocol

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Delete: `src/telegram_kol_research/immutable_control_bootstrap.py`
- Delete: `src/telegram_kol_research/deepcoin_maintenance_actions.py`
- Delete: `src/telegram_kol_research/deepcoin_maintenance_manifest.py`
- Delete: `src/telegram_kol_research/entry_authority_seed.py`
- Delete their dedicated tests
- Test: `tests/test_cli_smoke.py`

**Steps:**

1. Add a failing architecture test proving `seed-entry-authority`, `drain-one`,
   and `bootstrap-control` are absent from CLI help and their production modules
   do not exist.
2. Run the focused test and verify RED against the existing commands/files.
3. Remove the command blocks, imports, modules, dedicated tests, and obsolete
   `immutable_control_bootstrap`/`authority_self_test` owner kinds.
4. Run CLI and authority tests and verify GREEN.

### Task 2: Reconcile operator-cancelled entries in one transaction

**Files:**
- Create: `src/telegram_kol_research/reviewed_pending_entry_targets.py`
- Create: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify: `src/telegram_kol_research/cli.py`
- Replace: `tests/test_reviewed_pending_entry_cancel.py`
- Create: `tests/test_manual_pending_entry_reconciliation.py`
- Delete: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`

**Steps:**

1. Write failing tests for a read-only plan: canonical targets only, complete
   fresh evidence, zero positions, zero regular orders, zero pending triggers,
   exact local ownership, and absent-or-valid authority row.
2. Run the focused test and verify RED because the module is absent.
3. Implement the canonical target module and minimal plan builder.
4. Write failing apply tests for verified backup, one transaction, complete
   local terminalization, one event per target, authority-row seed, idempotent
   completed result, and rollback on injected failure.
5. Implement the local apply path. It must never call a Deepcoin write method.
6. Add the single Typer command and remove the old cancellation module/tests.
7. Run focused reconciliation, CLI, authority, and adjacent lifecycle tests.

### Task 3: Prevent staged releases from mutating on import

**Files:**
- Modify: `deploy/telegram-kol-activate`
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Write failing tests requiring activator Python `-B`,
   `PYTHONDONTWRITEBYTECODE=1`, and the same environment in every release
   drop-in.
2. Run focused tests and verify RED.
3. Add only those bytecode protections; retain external content validation and
   read-only release paths.
4. Run focused activation/stage tests and verify GREEN.

### Task 4: Documentation and final verification

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Modify: `docs/plans/2026-08-28-deepcoin-single-order-drain-immutable-control-bootstrap-design.md`

**Steps:**

1. Mark the prior bootstrap design superseded and record the simplified local
   candidate without claiming production evidence.
2. Run `git diff --check` and Python compilation.
3. Run all affected focused tests.
4. Run one final `.venv/bin/python -m pytest -q` suite after the last production
   code edit.
5. Review the exact diff for exchange-write absence, rollback boundaries,
   target-list uniqueness, entry freeze, secret redaction, and no replay.
6. Commit explicit paths only. Do not push or perform any production action.
