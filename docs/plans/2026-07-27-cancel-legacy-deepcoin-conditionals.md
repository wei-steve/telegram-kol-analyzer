# Legacy Deepcoin Conditional Cancellation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely cancel exactly six reviewed legacy Deepcoin conditional close orders while preserving their positions and native TPSL stops.

**Architecture:** Add a fail-closed planner that joins fresh exchange truth with durable ownership evidence and produces a fingerprinted action list. Add an apply path that handles one exact action per invocation, verifies a confirmation token, confirms the cancellation and readback, and records the database outcome. Expose both paths through a CLI so production execution can proceed sequentially with a fresh dry-run before every order.

**Tech Stack:** Python 3.12, SQLAlchemy, Typer, pytest, existing Deepcoin client and repair-confirmation infrastructure.

---

### Task 1: Define the cancellation planner

**Files:**
- Create: `src/telegram_kol_research/legacy_conditional_cancel.py`
- Test: `tests/test_legacy_conditional_cancel.py`

**Step 1: Write failing planner tests**

Cover:

- the five reviewed database-owned generic backup stops;
- the explicitly reviewed orphan order;
- exact order-field matching;
- exact live-position matching;
- independently verified native-stop presence;
- conflicts for missing or ambiguous exchange evidence;
- a stable fingerprint containing the complete reviewed action set.

**Step 2: Run the focused tests**

Run:

```bash
uv run pytest -q tests/test_legacy_conditional_cancel.py
```

Expected: failure because the planner module does not exist.

**Step 3: Implement the minimal planner**

Create immutable action and plan dataclasses. Accept an explicit reviewed-target
mapping. Read positions and pending trigger orders once per instrument. Require
one exact legacy `Conditional` row and one exact native TPSL stop for each
target. For database-owned targets, require one active
`PositionBackupStopOrder` whose request and exchange row agree. Return actions,
conflicts, and a deterministic SHA-256 fingerprint.

**Step 4: Run the focused tests**

Run:

```bash
uv run pytest -q tests/test_legacy_conditional_cancel.py
```

Expected: all planner tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/legacy_conditional_cancel.py tests/test_legacy_conditional_cancel.py
git commit -m "feat: plan exact legacy conditional cancellation"
```

### Task 2: Implement one-order apply semantics

**Files:**
- Modify: `src/telegram_kol_research/legacy_conditional_cancel.py`
- Modify: `tests/test_legacy_conditional_cancel.py`

**Step 1: Write failing apply tests**

Cover:

- required action ID, position ID, plan fingerprint, and unused confirmation
  token;
- fresh-plan equality immediately before the write;
- one `cancel_trigger_order` call with exact `instId` and `ordId`;
- explicit response code and exact returned order-ID validation;
- post-write readback proving only the target disappeared and native protection
  remains;
- stopping on outcome-unknown, rejection, or readback uncertainty;
- changing an owned `PositionBackupStopOrder` to `cancelled` only after confirmed
  readback;
- recording a sanitized execution event for both owned and orphan actions.

**Step 2: Run the focused tests**

Run:

```bash
uv run pytest -q tests/test_legacy_conditional_cancel.py
```

Expected: the new apply tests fail.

**Step 3: Implement the minimal apply path**

Reuse the existing repair-confirmation token helpers. Apply exactly one reviewed
action. Never retry an uncertain write. Re-read positions and pending trigger
orders after cancellation and fail closed unless the target is absent and its
native stop remains. Persist the confirmed result and execution event in one
database transaction.

**Step 4: Run the focused and adjacent tests**

Run:

```bash
uv run pytest -q \
  tests/test_legacy_conditional_cancel.py \
  tests/test_native_tpsl_migration.py \
  tests/test_backup_stop_repair.py
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/legacy_conditional_cancel.py tests/test_legacy_conditional_cancel.py
git commit -m "feat: apply confirmed legacy conditional cancellation"
```

### Task 3: Add the operator CLI

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing CLI tests**

Assert that `cancel-reviewed-legacy-conditionals` defaults to a dry-run and
prints actions, conflicts, and fingerprint. Apply mode must require one action
ID, position ID, expected fingerprint, and confirmation token.

**Step 2: Run the focused CLI tests**

Run:

```bash
uv run pytest -q tests/test_cli.py -k legacy_conditionals
```

Expected: failure because the command is not registered.

**Step 3: Implement the CLI**

Wire the fixed reviewed target set into the planner. Keep apply one-position at
a time and refuse any target conflict.

**Step 4: Run verification**

Run:

```bash
uv run pytest -q \
  tests/test_legacy_conditional_cancel.py \
  tests/test_cli.py -k "legacy_conditionals or repair_confirmation"
```

Expected: all selected tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli.py
git commit -m "feat: expose reviewed conditional cancellation"
```

### Task 4: Review, publish, and deploy

**Step 1: Review the complete diff and dirty worktree**

Run:

```bash
git status --short
git diff origin/codex/deepcoin-auto-trading-v1...HEAD
```

Expected: only the reviewed feature commits are in scope; unrelated existing
local changes remain unstaged.

**Step 2: Run the full relevant test set**

Run:

```bash
uv run pytest -q \
  tests/test_legacy_conditional_cancel.py \
  tests/test_native_tpsl_migration.py \
  tests/test_backup_stop_repair.py \
  tests/test_cli.py
```

Expected: all tests pass.

**Step 3: Push the branch**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub accepts the reviewed commits.

**Step 4: Update production**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the pushed commit, reinstalls the editable package,
restarts `telegram-kol.service`, and reports healthy status.

### Task 5: Execute sequential production cancellation

For each reviewed action:

1. Run a fresh production dry-run.
2. Confirm its exact immutable fields and native-stop evidence.
3. Create a single-use repair confirmation token.
4. Apply exactly that action using the current fingerprint.
5. Re-run the dry-run and live snapshot.
6. Stop immediately on any conflict or uncertainty.

Execute the orphan first, followed by the five database-owned targets. Do not
continue if a position or its native protection changed.

### Task 6: Final production verification

Run a fresh coherent Deepcoin snapshot and database audit. Require:

- all six legacy conditional IDs absent;
- all expected native stops still pending;
- all surviving positions unchanged;
- normal BTC and ETH entry triggers untouched;
- five owned backup rows marked `cancelled`;
- six confirmed execution events with no unknown outcomes.

No position close, TPSL replacement, or new order submission is part of this
operation.
