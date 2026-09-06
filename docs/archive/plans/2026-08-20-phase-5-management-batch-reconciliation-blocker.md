# Phase 5 Management Batch Reconciliation Blocker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair the management reconciliation race that strands an unsubmitted batch and add a fail-closed, fingerprint-gated recovery path for batch 119 without touching exchange state.

**Architecture:** Preserve all-planned executing batches for the existing worker restart path, freeze impossible identityless submitted legs, and converge only exact zero-submission history through the existing operator CLI. Production recovery is rehearsed against a consistent database copy before a single CAS-protected apply.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, systemd, existing gated updater and Deepcoin read-only client.

---

### Task 1: Preserve all-planned executing batches

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write the failing race regression**

Create an `executing` close batch whose leg is still `planned`, reconcile an
unchanged exact position snapshot, and assert the parent remains `executing`
and the leg remains `planned`.

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_management_reconciliation.py::test_all_planned_executing_batch_remains_available_for_worker_restart -q
```

Expected: FAIL because the current reconciler changes the leg to `submitted`
and parent to `reconciling`.

**Step 3: Implement the minimum state guard**

Before per-leg reconciliation, detect an `executing` batch whose legs are all
`planned`. Count it as pending and leave its durable state unchanged.

**Step 4: Verify GREEN and nearby tests**

Run the new test and the complete reconciliation test file.

### Task 2: Freeze identityless submitted history

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write the failing impossible-state regression**

Seed a reconciling batch with a submitted leg that has no client id, exchange
id, request, or response. Assert reconciliation changes the leg to
`inconsistent` and freezes the batch as `recovery_required` with
`management_close_submission_identity_missing`.

**Step 2: Verify RED**

Expected: FAIL because the current row stays `submitted/reconciling` and its
timestamp advances forever.

**Step 3: Implement the minimum fail-closed classification**

Classify a submitted or partial leg with no durable client or exchange identity
as inconsistent. Propagate its explicit identity-missing reason to the parent
freeze result.

**Step 4: Verify GREEN and reconciliation coverage**

Run the two new tests and the complete reconciliation test file.

### Task 3: Plan exact zero-submission recovery

**Files:**
- Modify: `src/telegram_kol_research/management_history_recovery.py`
- Test: `tests/test_management_history_recovery.py`

**Step 1: Write failing planner tests**

Add a helper that seeds the new frozen state. Assert complete zero-submission
evidence returns `terminal_no_submission`. Add separate refusal tests for:

- incomplete exchange snapshot;
- any client or exchange order id;
- any request or response payload;
- a close position-mutation intent;
- a management-close execution event.

**Step 2: Verify RED**

Run only the new planner tests. Expected: the positive case refuses with
`exact_terminal_order_evidence_missing` or the first newly required reason.

**Step 3: Implement exact planner predicate**

Recognize only `recovery_required` plus
`management_close_submission_identity_missing`, require every durable absence
condition, and return the existing `terminal_no_submission` decision with
bounded redacted evidence.

**Step 4: Verify GREEN**

Run all management-history recovery tests.

### Task 4: Apply the recovery with CAS and audit

**Files:**
- Modify: `src/telegram_kol_research/management_history_recovery.py`
- Test: `tests/test_management_history_recovery.py`

**Step 1: Write failing apply tests**

Assert the exact decision marks the identityless leg `failed`, marks the batch
`resolved/history_no_submission_confirmed`, writes one audit event, remains
idempotent on repeat, and refuses a changed source fingerprint.

**Step 2: Verify RED**

Expected: FAIL because current terminal-no-submission apply only terminalizes
legs whose status is `planned`.

**Step 3: Implement minimum apply extension**

Allow the already-proven identityless inconsistent leg to transition to failed
inside the existing fingerprint-gated apply branch. Do not modify lifecycle or
binding rows.

**Step 4: Verify GREEN**

Run the apply tests, all recovery tests, and CLI smoke coverage for
`recover-management-history`.

### Task 5: Local verification and explicit commit

**Files:**
- Modify: `docs/runtime-serialization-remediation-status.md`

**Step 1: Run focused verification**

```bash
.venv/bin/python -m pytest -q \
  tests/test_strategy_management_reconciliation.py \
  tests/test_management_history_recovery.py \
  tests/test_strategy_management_worker.py \
  tests/test_runtime_event_loop_blocking_census.py
```

**Step 2: Run the full suite**

```bash
.venv/bin/python -m pytest -q
```

Record exact counts and warnings in the status file. Keep Phase 5
`in_progress` and queue disabled.

**Step 3: Review the diff**

Run `git diff --check`, inspect every changed hunk, and confirm there is no
recognition, sizing, policy, order-submission, message-lock, or queue-mode
semantic change.

**Step 4: Stage only explicit paths**

```bash
git add \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/management_history_recovery.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_management_history_recovery.py \
  docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
```

Commit without `git add -A`.

### Task 6: Push and deploy dormant

**Step 1: Confirm remote fast-forward**

Fetch `origin/codex/deepcoin-auto-trading-v1`, prove it is the exact parent of
the candidate, and push the integration HEAD to that deploy branch without
force.

**Step 2: Capture production safety baseline**

Read production HEAD, service state, `global/shadow` modes, active write count,
active management batches, parity, loop health, and complete direct Deepcoin
positions, regular orders, pending trigger/TPSL ids.

**Step 3: Gated deploy**

From the candidate checkout run:

```bash
EXPECTED_COMMIT=<40-hex-remote-tip> ./scripts/server_git_update.sh
```

If the updater returns BLOCK, stop and record it. Do not retry blindly.

**Step 4: Verify dormant behavior**

Confirm exact production HEAD, active service, `global/shadow`, no new journal
errors, unchanged exchange state, and batch 119 frozen with the explicit reason.

### Task 7: Rehearse and apply exact recovery

**Step 1: Create evidence artifacts**

Use SQLite online backup to create a preserved production backup plus a separate
rehearsal copy. Record `PRAGMA quick_check`, relevant table counts, and the exact
batch/leg/audit rows before rehearsal.

**Step 2: Dry-run and rehearse on the copy**

Run `recover-management-history` without `--apply` against the rehearsal copy,
capture the evidence fingerprint, then apply that exact fingerprint to the
copy. Verify quick-check and that only the expected batch, leg, and audit event
changed.

**Step 3: Re-check production safety**

Require fresh complete exchange reads, `active_write_count=0`, no new in-flight
management batch, unchanged batch source fingerprint, and production dry-run
matching the rehearsed decision.

**Step 4: Apply once to production**

Run the official CLI with `--apply --evidence-fingerprint <exact>`. Immediately
verify the batch is resolved, the audit event exists exactly once, and exchange
state is unchanged.

### Task 8: Resume Phase 5 gate

Re-run the quiet-window gate. Only if it passes, continue the Phase 5 file's
`shadow -> queue -> shadow` rollback-boundary proof and subsequent queue
observation. A full real trading session and deliberate mid-traffic restart are
still mandatory. Do not start Phase 6 in this turn.
