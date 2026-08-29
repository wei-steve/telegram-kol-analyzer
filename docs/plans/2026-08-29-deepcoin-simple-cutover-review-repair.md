# Deepcoin Simple Cutover Review Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the short stopped-legacy maintenance cutover executable and fail-closed without restoring the deleted bootstrap/drain protocol.

**Architecture:** Add one invocation-only stopped-legacy mode to the existing scoped activator and one shared inactive-plus-persistently-masked proof used at reconciliation and activation boundaries. Tighten the existing one-transaction reconciliation around snapshot completion time, target history, canonical local identity, authority parsing, and backup creation; add no persistent protocol state.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy/SQLite, systemd, pytest.

---

### Task 1: Prove the stopped maintenance boundary

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Add a failing test whose fake legacy runtime has no identity endpoint but
   reports the complete authority scope inactive and persistently masked.
2. Verify RED: ordinary `activate_release` fails on pre-start runtime identity.
3. Add a `source_mode="stopped_legacy"` branch which is accepted only for
   `web,monitor,ingest,worker`, requires every controlled unit and monitor timer
   inactive plus masked, and requires zero active exchange writes before
   authorization consumption.
4. Add failing tests for active, unmasked, partial-scope, and unknown unit state;
   each must retain authorization and perform no unmask/start operation.
5. Implement minimal `RuntimeAdapter`/`SystemRuntimeAdapter` methods for exact
   systemd active and enabled-state proof.
6. Run:
   `.venv/bin/python -m pytest -q tests/test_scoped_release_activation.py`
   and verify GREEN.
7. Commit only the activation source and test paths.

### Task 2: Start candidate and preserve a stopped rollback boundary

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `deploy/telegram-kol-activate`
- Modify: `tests/test_server_update_scripts.py`

**Steps:**

1. Add failing tests proving stopped-legacy activation publishes entry-frozen
   drop-ins, unmasks only the declared full scope, starts the candidate, and
   validates post-start candidate runtime authority without a before-PID check.
2. Add failing rollback tests: candidate failure starts only the validated
   rollback entry-frozen; rollback failure stops and persistently masks the
   entire scope.
3. Add explicit `ACTIVATION_SOURCE_MODE=immutable|stopped_legacy` parsing in the
   activator entrypoint; reject every other value.
4. Implement the minimal start/rollback branches while leaving ordinary
   immutable-to-immutable activation unchanged.
5. Run focused activation and deploy-script tests and verify GREEN.
6. Commit explicit activation/deploy/test paths.

### Task 3: Repair evidence time and target history classification

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_maintenance_evidence.py`
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify: `tests/test_deepcoin_maintenance_evidence.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

**Steps:**

1. Add a failing default-clock test proving the plan can become `ready` after
   reads complete; verify it currently refuses its own future evidence.
2. Change the reconciliation freshness boundary to use a timestamp captured
   after evidence construction. Keep explicit clocks injectable in tests.
3. Add failing tests for a target-related trade fill and for trigger history
   whose terminal status is filled, ambiguous, or inconsistent with an
   unfilled cancellation.
4. Add one small public order-ID normalizer if needed and reject target-related
   fills or noncanonical history before local planning. Do not retry or write.
5. Run both focused test modules and verify GREEN.
6. Commit explicit evidence/reconciliation/test paths.

### Task 4: Prove exact canonical local ownership and authority syntax

**Files:**
- Modify: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify: `tests/test_entry_revision_exchange_authority.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

**Steps:**

1. Add a failing public-parser test showing an idle authority with an invalid
   `released_at` timestamp is rejected.
2. Expose a read-only canonical idle-document predicate backed by the existing
   authority parser; remove the reconciliation-local partial parser.
3. Add parameterized failing tests for drift in venue, symbol/instrument, side,
   strategy identity, request fingerprint, trigger price, size, embedded stop,
   intent binding/leg, protection parent/binding/leg, and convergence
   binding/leg.
4. Parse the stored leg request as a JSON object, normalize only documented key
   aliases, and compare exact canonical economics. Malformed or missing values
   block as `reviewed_local_state_changed`.
5. Run authority and reconciliation tests and verify GREEN.
6. Commit explicit source/test paths.

### Task 5: Revalidate maintenance stop and harden the SQLite backup

**Files:**
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`
- Modify: `tests/test_cli_smoke.py`

**Steps:**

1. Add failing tests requiring inactive-plus-masked proof before the first
   exchange read and again after fresh exchange planning but before backup or
   `BEGIN IMMEDIATE`.
2. Inject the runtime proof dependency into plan/apply; the CLI supplies the
   real system runtime adapter. Any active, unmasked, malformed, or changing
   state returns/raises a stable fail-closed reason.
3. Add failing backup tests for an existing path, symlink, unsafe parent mode,
   wrong source path, non-`0600` output, failed `quick_check`, and foreign-key
   violations. Assert the production database transaction is untouched.
4. Create the destination exclusively with no-follow semantics, validate parent
   ownership/mode, bind it to the same database path, enforce `0600`, and run
   integrity checks before terminalization.
5. Add a seven-canonical-target success fixture plus injected terminalization
   failure; assert one transaction, exact events, zero partial completion, and
   an unchanged verified backup.
6. Run reconciliation, CLI, DB, lifecycle, and authority focused tests and
   verify GREEN.
7. Commit explicit source/test paths.

### Task 6: Final verification and review

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Modify: `docs/plans/2026-08-28-deepcoin-simple-cancel-all-cutover-design.md`

**Steps:**

1. Update documentation with local evidence only and leave every production
   authorization explicitly outstanding.
2. Run `git diff --check` and Python compilation for changed modules.
3. Run all affected focused tests.
4. Run one final `.venv/bin/python -m pytest -q` after the last production-code
   edit.
5. Request an independent code review of the exact base-to-HEAD diff; repair all
   Critical/Important findings with new RED→GREEN cycles.
6. Re-run affected focused tests and the final full suite if production code
   changes after review.
7. Commit explicit documentation paths. Do not push, stage, SSH, deploy, freeze,
   restart, or perform production/Deepcoin writes.
