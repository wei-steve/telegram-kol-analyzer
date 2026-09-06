# Partial-Close Take-Profit Consumption Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent message-driven partial closes from recreating take-profit quantities that can unexpectedly close the entire remaining position, and correctly attribute proven exchange take-profit exits.

**Architecture:** Add one pure take-profit consumption and validation boundary to the management executor and call it from both normal and recovery protection-replacement paths. Extend lifecycle reconciliation only where exact Deepcoin history and ledger evidence prove a take-profit close; retain manual classification for ambiguous disappearance.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, Deepcoin REST snapshots, systemd deployment.

---

### Task 1: Specify take-profit consumption

**Files:**
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`

**Step 1: Write the failing tests**

Add pure-function cases for `8 -> close 4` with take-profit sizes `4/2/2`, a
close that partially consumes the next stage, a close that consumes every
stage, and an aggregate ladder larger than the remaining position.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_strategy_management_executor.py -k 'consume or production_ladder or exceeds_remaining' -v`

Expected: FAIL because the normal transformation preserves or proportionally
reallocates the first stage instead of consuming it.

**Step 3: Implement the minimal pure transformation**

Replace the proportional take-profit redistribution with ordered consumption
of `planned_close_size`.  Preserve stop rows, remove fully consumed take-profit
rows, shrink the first partially consumed row, and validate every remaining
quantity against the contract step and remaining position.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_management_executor.py -k 'consume or production_ladder or exceeds_remaining' -v`

Expected: PASS.

### Task 2: Unify normal and recovery protection replacement

**Files:**
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`

**Step 1: Write the failing execution-path test**

Persist a normal `partial_then_break_even` batch with an eight-contract position
and `4/2/2` staged targets.  Confirm a four-contract close, execute protection
replacement, and assert the client receives only `2/2` targets at the second
and third prices.  Keep the existing pre-cancel recovery case and update it to
the same contract.

**Step 2: Run the focused tests and confirm RED**

Run: `uv run pytest tests/test_strategy_management_executor.py -k 'partial_then_break_even and (normal or precancelled or production)' -v`

Expected: the normal path submits the old `4/2/2` ladder.

**Step 3: Route both paths through the shared transformation**

Apply the transformation after exact protection preflight, regardless of
whether old orders remain pending or were already pre-cancelled.  Keep existing
cancel idempotency and exchange write boundaries unchanged.

**Step 4: Run the focused and full executor suites**

Run: `uv run pytest tests/test_strategy_management_executor.py -v`

Expected: PASS.

### Task 3: Attribute exact exchange take-profit exits

**Files:**
- Modify: `tests/test_execution_bindings.py` or the focused lifecycle synchronization test module selected from the existing implementation
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1: Write a failing evidence test**

Model a vanished exact position with position history showing the full closed
size, a reduce-only closing fill at a verified take-profit trigger, and ledger
ownership for that trigger.  Assert lifecycle exit reason `take_profit`.  Add an
ambiguous-history control that remains `manual`.

**Step 2: Run the exact test and confirm RED**

Run the selected test node with `uv run pytest <node> -v`.

Expected: the proven take-profit case is classified `manual`.

**Step 3: Implement exact evidence classification**

Pass bounded history evidence into manual-close synchronization, match exact
position identity and verified owned protection, and upgrade only proven
take-profit disappearance.  Do not infer from price alone.

**Step 4: Run focused and related suites**

Run: `uv run pytest tests/test_execution_bindings.py tests/test_strategy_management_reconciliation.py -v`

Expected: PASS.

### Task 4: Review, publish, and deploy safely

**Files:**
- Modify only files identified above and these two plan documents.

**Step 1: Run regression checks**

Run focused suites plus `uv run python -m compileall -q src`.

**Step 2: Review the scoped diff**

Check for accidental changes, write-before-validation paths, duplicate
protection, ambiguous attribution, and missing tests.  Resolve all critical and
important findings.

**Step 3: Commit and push**

Stage only the implementation, tests, and plan documents.  Preserve unrelated
working-tree files.  Push `codex/deepcoin-auto-trading-v1`.

**Step 4: Prove a safe deployment window**

Read production service state, active management batches, current positions,
and recent strategy messages without writing.  If any time-sensitive strategy
operation is active, stop before deployment and record the outstanding server
verification.

**Step 5: Deploy and verify**

Run `./scripts/server_git_update.sh`, then confirm the production SHA, editable
installation, `telegram-kol.service=active`, clean startup logs, and zero new
failed/recovery-required management batches.  Do not submit a test order or
Telegram message.
