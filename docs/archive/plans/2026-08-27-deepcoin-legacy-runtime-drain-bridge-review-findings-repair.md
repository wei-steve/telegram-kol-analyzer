# Deepcoin Legacy Runtime Drain Bridge Review Findings Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair the five independent-review findings while replacing the impossible indefinite old-runtime freeze with a bounded, fail-closed compatibility cutover.

**Architecture:** Add an internal durable entry-only freeze and serialize it with the existing entry exchange authority. Keep the legacy global freeze only for the pre-cutover interval, hand bridge identity to the exact candidate worker, and then preserve entry freeze while management/protection remain live. Tighten pre-write intent classification, evidence time, worker identity, and revision sentinel scope without weakening true unknown handling.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite `BEGIN IMMEDIATE`, Typer, pytest, systemd and Linux `/proc` read-only identity evidence.

---

### Task 1: Separate entry submission from management and protection modes

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Write failing settings tests**

Add tests proving:

- `legacy_entry_submission_frozen` defaults to false and rejects non-booleans;
- `entry_submission_enabled` requires normal auto trade and no legacy freeze;
- with auto trade false plus legacy freeze true, management planning/execution,
  composite management, stop rescue and liveness retain their configured live
  modes;
- without the internal freeze, auto trade false keeps the existing disabled
  behavior.

**Step 2: Run RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trading_settings.py \
  -k "legacy_entry_submission or management_remains_live"
```

Expected: fail because the internal setting and derived property are absent.

**Step 3: Implement the minimal settings contract**

Add the internal boolean field and parse it through the existing strict boolean
parser. Add `entry_submission_enabled`. Change only the effective management and
protection properties to accept the compatibility-freeze state.

**Step 4: Write and run entry-path RED tests**

Prove both normal auto entry and direct recovery `open_position` refuse before
constructing or calling an exchange writer when the internal freeze is true,
while an unrelated management signal remains executable.

**Step 5: Implement the minimal entry checks and run GREEN**

Replace entry-only `auto_trade_enabled` decisions with
`entry_submission_enabled`. Do not change management, close, TPSL, rescue or
protection entrypoints.

Run the three focused files and require all green.

**Step 6: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_live_submit.py \
  tests/test_trading_settings.py tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py
git diff --cached --name-only
git commit -m "fix: separate legacy entry freeze from protection"
```

### Task 2: Serialize new-entry writes with bridge freeze

**Files:**
- Modify: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Test: `tests/test_entry_revision_exchange_authority.py`
- Test: `tests/test_recovery_live_submit.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`

**Step 1: Write failing authority tests**

Prove:

- `new_entry_worker` is a valid exact owner;
- an active new-entry authority blocks bridge freeze;
- a committed bridge freeze blocks later new-entry acquisition;
- the authority covers a whole multi-leg submission;
- known pre-write failure releases it;
- possible-write unknown retains it and blocks freeze/retry.

**Step 2: Run RED**

Run the named tests and confirm failures are caused by the missing owner and
freeze checks.

**Step 3: Implement minimal durable serialization**

Extend the existing authority schema without changing its storage key. Read the
internal freeze in the acquisition transaction. Acquire once for an
`open_position` signal before the first possible exchange call and release only
when the whole result is classified. Preserve the lease for unknown outcomes.

Bridge freeze rechecks the authority row in its own `BEGIN IMMEDIATE`
transaction before writing the internal freeze.

**Step 4: Run GREEN and the existing revision/cancellation authority tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_recovery_live_submit.py \
  tests/test_entry_revision_executor.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_legacy_runtime_drain_bridge.py
```

**Step 5: Commit explicit paths**

Commit only the three implementation and three test files with message
`fix: serialize legacy entry freeze with writes`.

### Task 3: Add exact candidate worker handoff and rollback

**Files:**
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write failing state-machine tests**

Prove:

- handoff is accepted only from fenced/no-write state;
- legacy identity and candidate identity are distinct and exact;
- handoff retains internal entry freeze and revision sentinels;
- management/protection are live under candidate settings while entry remains
  disabled;
- cancellation after handoff binds to candidate identity;
- rollback before a reviewed write restores all original settings and exact
  sentinels;
- write-boundary or unknown state forbids rollback.

**Step 2: Run RED**

Run only the new bridge and CLI tests and observe the missing handoff transition.

**Step 3: Implement the schema and transition**

Bump the bridge document schema explicitly. Store immutable legacy identity and
mutable current authority identity. Add the explicit CLI action without making
it the default and without exposing tokens or process command lines.

Do not release entry freeze, sentinels or reviewed scope during handoff.

**Step 4: Run GREEN**

Run `tests/test_legacy_runtime_drain_bridge.py` and `tests/test_cli_smoke.py`.

**Step 5: Commit explicit paths**

Commit the two implementation and two test files with message
`fix: hand off legacy drain to candidate worker`.

### Task 4: Bind runtime identity to the exact split worker

**Files:**
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write failing identity tests**

Prove arbitrary service names, wrong proc cwd, malformed/oversized cmdline,
wrong runtime role, changed MainPID and changed start ticks all fail closed. Prove
the exact worker service, checkout, command and stable descriptor evidence pass.

**Step 2: Run RED**

Confirm current code incorrectly accepts a non-worker service and unbound proc
evidence.

**Step 3: Implement bounded descriptor-based evidence**

Hard-bind `telegram-kol-worker.service`, add service name to the identity, read
proc cwd/cmdline between the two PID/start-tick observations, and remove the
misleading CLI default/override.

**Step 4: Run GREEN and commit**

Run both focused files, then commit only their exact paths with message
`fix: bind bridge identity to worker process`.

### Task 5: Preserve known pre-write refusal without weakening unknown

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Test: `tests/test_reviewed_pending_entry_cancel.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`

**Step 1: Write failing tests**

For each allowlisted refusal, prove zero exchange calls, inner authority idle, a
fresh plan can be built with a new confirmation token, and pre-write rollback is
allowed. Also prove malformed `prewrite_refused`, submitting, recovery-required
and actual unknown intents still block plan, rollback and retry.

**Step 2: Run RED**

Confirm the current planner reports `prior_cancel_outcome_unknown` for the
proven pre-write case.

**Step 3: Implement minimal classification**

Persist `prewrite_refused` only at exact zero-write branches with bounded reason
and `submitted=false`. Centralize strict structural validation and use it in
both planning and bridge unknown scans.

**Step 4: Run GREEN and commit**

Run the two focused files and commit their exact paths with message
`fix: distinguish prewrite cancel refusals`.

### Task 6: Make exchange evidence freshness real

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_cli_smoke.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`

**Step 1: Write failing clock tests**

Use a controlled clock to prove a query taking more than 60 seconds is stale,
a completed query followed by an immediate transition is fresh, and future
timestamps fail closed.

**Step 2: Run RED**

Confirm the long query currently passes because one timestamp is reused.

**Step 3: Implement separate timestamps**

Timestamp evidence after all reads, recheck worker identity, then obtain a new
transition timestamp. Pass those exact values to evidence and transition APIs.

**Step 4: Run GREEN and commit**

Run both files and commit with message `fix: timestamp drain evidence after reads`.

### Task 7: Align revision sentinel claim scope

**Files:**
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Test: `tests/test_legacy_runtime_drain_bridge.py`
- Test: `tests/test_entry_revision_executor.py`
- Test: `tests/test_strategy_revision_planner.py`

**Step 1: Write failing scope tests**

Prove terminal unrelated claim residue is ignored consistently at fence,
cancellation and rollback. Prove every active foreign claim and every
target-related/orphan unknown child still blocks.

**Step 2: Run RED**

Observe `legacy_bridge_revision_claim_set_drift` after a fence that ignored the
same terminal row.

**Step 3: Implement one active-scope helper**

Use one query/helper for active IDs and active claimed IDs in fence and sentinel
validation. Do not change the separate target-related ambiguity scan.

**Step 4: Run GREEN and commit**

Run the three focused files and commit with message
`fix: align legacy revision sentinel scope`.

### Task 8: Final regression, review and canonical status

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Run the final focused authority/protection regression**

Run all directly affected tests, including trading settings, entry submission,
bridge, cancellation, entry revision, strategy management, protection rescue,
take-profit convergence and CLI smoke.

**Step 2: Run static checks**

```bash
.venv/bin/python -m compileall -q src/telegram_kol_research
git diff --check 5024a59e97b4328acba101f9bc138d7bf3d47530..HEAD
```

**Step 3: Run one final full suite**

```bash
.venv/bin/python -m pytest -q
```

If production code changes afterwards, rerun the affected focused tests and one
new final full suite.

**Step 4: Review the exact diff**

Recheck entry/protection isolation, lease retention, handoff/rollback,
single-order boundary, target-related unknown, evidence time, worker identity,
terminalization, secret redaction and history-replay absence.

**Step 5: Update status and commit explicit path**

Record each RED and GREEN result, focused/full-suite counts, exact content SHA,
review outcome, prohibited actions not taken, and the still-separate production
authorities. Commit only the canonical status file.

**Step 6: Final repository gate**

Require branch `codex/phase0-deploy-integration`, a clean worktree, and report the
new exact local HEAD. Do not push.
