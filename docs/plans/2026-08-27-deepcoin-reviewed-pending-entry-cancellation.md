# Deepcoin Reviewed Pending Entry Cancellation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local-only, dry-run-by-default operator tool that can cancel exactly one of seven reviewed Deepcoin pending trigger entries, never retry an unknown write, and terminalize its exact durable ownership state only after complete readback.

**Architecture:** Add a dedicated reviewed-target planner and one-action apply function alongside the existing legacy cancellation tooling, without reusing its position-close assumptions. The planner joins fresh Deepcoin GET results to exact SQLite ownership and returns a deterministic fingerprint; apply performs one cancellation attempt, verifies closed exchange evidence, then commits a narrow terminalization transaction. A Typer command exposes dry-run and guarded apply modes.

**Tech Stack:** Python 3.12, dataclasses, SQLAlchemy, Typer, pytest, existing Deepcoin client, repair-confirmation tokens, mutation intents, and execution-event persistence.

---

### Task 1: Add the reviewed pending-entry planner

**Files:**
- Create: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Create: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write the failing planner test**

Seed four pending lifecycles, four bindings, seven entry legs, their protection
intents/protection legs/TP convergence rows, and the fixed reviewed exchange
rows. Assert that the wished-for API:

```python
plan = build_reviewed_pending_entry_cancel_plan(
    session_factory,
    deepcoin_client=client,
    targets=REVIEWED_PENDING_ENTRY_TARGETS,
    now=NOW,
)
```

returns seven actions, zero conflicts, no completed IDs, exact reviewed local
identities, and a stable 64-character fingerprint without modifying SQLite.

**Step 2: Run RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py::test_plan_requires_exact_reviewed_exchange_and_local_ownership
```

Expected: FAIL because the module/API does not exist.

**Step 3: Implement the minimal planner**

Define immutable dataclasses for the fixed target, one action, and the plan.
Read positions, regular orders, pending triggers, trigger history, and fills for
BTC/ETH/SOL. Validate the closed reviewed set, exact row economics, empty
positions/regular orders, unique local ownership, matching pending lifecycle,
protection intent, request fingerprint, and no fill evidence. Use bounded reason
codes only.

**Step 4: Run GREEN**

Run the exact test from Step 2. Expected: PASS.

**Step 5: Add planner conflict RED cases**

Add parameterized tests for incomplete reads, unreviewed trigger rows, missing or
duplicate reviewed rows, changed economics/embedded stop, live positions or
regular orders, wrong binding/leg/lifecycle, missing protection intent, changed
request fingerprint, and fill evidence. Assert zero actions for unsafe targets
and explicit conflicts.

**Step 6: Implement conflict handling and run GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py -k plan
```

Expected: all planner tests pass.

**Step 7: Commit**

```bash
git add -- \
  src/telegram_kol_research/reviewed_pending_entry_cancel.py \
  tests/test_reviewed_pending_entry_cancel.py
git diff --cached --name-only
git commit -m "feat: plan reviewed pending entry cancellation"
```

### Task 2: Add one-order apply and unknown-outcome semantics

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write RED tests**

Cover exact plan fingerprint/action selection, unused confirmation token, fresh
plan equality, one `cancel_trigger_order` call, exact request fields, no retry on
transport exception or outcome-unknown exception, response code/order identity,
selected-order absence, cancelled history, no fills/new position/regular order,
and unchanged sibling reviewed rows.

**Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py -k apply
```

Expected: FAIL because apply is missing.

**Step 3: Implement minimal apply**

Add `apply_reviewed_pending_entry_cancel_plan(...)`. Reserve one mutation
intent, consume one confirmation token, submit exactly one cancel request, and
perform fresh closed readback. Catch no write exception for retry. Record only a
bounded unknown/blocked intent result until success is proven.

**Step 4: Run GREEN**

Run the apply selection. Expected: all apply tests pass.

**Step 5: Commit**

```bash
git add -- \
  src/telegram_kol_research/reviewed_pending_entry_cancel.py \
  tests/test_reviewed_pending_entry_cancel.py
git diff --cached --name-only
git commit -m "feat: cancel one reviewed pending entry"
```

### Task 3: Terminalize exact dependent state

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write RED tests**

After one confirmed cancellation, assert exact leg terminalization, resolved
terminal protection intent, terminal planned protection/TP state, one sanitized
confirmed event, and submitted mutation intent. Assert sibling leg/lifecycle/
binding remain live. After the binding's last reviewed leg, assert binding and
lifecycle terminalization. Assert no notification, worker-command, replay, or
unrelated row changes.

Add a completed-action planner test that accepts exchange absence only when the
durable terminal state and confirmed event agree; every mismatch must conflict.

**Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py -k "terminal or completed"
```

Expected: FAIL on missing terminalization.

**Step 3: Implement one transaction**

Use one SQLAlchemy transaction guarded by exact current IDs/statuses. Update only
the selected leg and its dependent rows. Terminalize binding/lifecycle only
after querying that no nonterminal entry leg remains. Store sanitized hashes and
reason codes in the execution event and mutation intent, never raw responses or
environment data.

**Step 4: Run GREEN and adjacent tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_legacy_conditional_cancel.py \
  tests/test_entry_revision_executor.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  src/telegram_kol_research/reviewed_pending_entry_cancel.py \
  tests/test_reviewed_pending_entry_cancel.py
git diff --cached --name-only
git commit -m "feat: terminalize reviewed pending entry state"
```

### Task 4: Expose a dry-run-by-default CLI

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli.py`

**Step 1: Write RED CLI tests**

Assert `cancel-reviewed-pending-entries` defaults to dry-run and emits closed
JSON. Apply must require `--order-id`, `--action-id`,
`--expected-fingerprint`, and `--confirmation-token`; conflicts exit nonzero.
Assert rendered output excludes seeded secret and raw-response sentinel values.

**Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_cli.py -k reviewed_pending_entries
```

Expected: FAIL because the command is not registered.

**Step 3: Implement minimal CLI wiring**

Import the fixed target set, planner, and apply function. Build the Deepcoin
client only through the existing environment loader. Print the plan before
apply, reject incomplete guards, print the bounded result, and return nonzero
unless status is confirmed cancelled.

**Step 4: Run GREEN**

Run the exact CLI selection. Expected: PASS.

**Step 5: Commit**

```bash
git add -- src/telegram_kol_research/cli.py tests/test_cli.py
git diff --cached --name-only
git commit -m "feat: expose reviewed pending entry cancellation"
```

### Task 5: Final verification and canonical handoff

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Test: `tests/test_server_update_scripts.py`

**Step 1: Run the final focused set**

Run the Task 3 adjacent tests plus the CLI selection. Expected: PASS.

**Step 2: Run static checks**

```bash
.venv/bin/python -m compileall -q src/telegram_kol_research
git diff --check
```

Expected: PASS.

**Step 3: Run one final full suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. If production code changes afterward, rerun affected focused
tests and this full suite once on the new final candidate.

**Step 4: Add a RED canonical-status assertion**

Extend the existing static status test to require the reviewed cancellation
candidate state and retain `task12_gate: failed_closed`,
`auto_trade_frozen: false`, and all null watermarks.

**Step 5: Update status and run GREEN**

Record the local candidate commits and test evidence. State explicitly that the
tool has not been pushed or run against production and that the seven live
orders remain untouched.

**Step 6: Commit status and verify clean tree**

Stage only the status/test files, commit, then run:

```bash
git status --short
git log --oneline --decorate -12
```

Expected: clean worktree. Do not push.
