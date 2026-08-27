# Deepcoin Reviewed Pending Entry Revision Gate Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop terminal, claim-free ambiguous revision children outside the fixed reviewed bindings/orders from falsely blocking the reviewed pending-entry cancellation dry-run while retaining every active or target-related authority gate.

**Architecture:** Keep non-terminal revision batches and parent claim evidence as global blockers. Pass the fixed reviewed targets into the authority gate and scope terminal ambiguous child queries by parent binding, execution leg, or order identity so unrelated history cannot authorize or block a reviewed cancellation.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, existing Deepcoin cancellation planner and deployment active-write gate.

---

### Task 1: Add revision authority regression coverage

**Files:**
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write the failing terminal-state test**

Seed the normal reviewed target set plus an unrelated `StrategyRevisionBatch`
with status `recovery_required` and no claim fields. Assert that the planner
still returns the complete clean action set with no conflicts.

**Step 2: Run RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py -k "recovery_required_revision_batch_without_claim"
```

Expected: FAIL because the current broad batch query returns
`active_exchange_authority_present`.

**Step 3: Add active and ambiguous-boundary companion tests**

Seed the same batch with status `submitting_replacements`. Assert zero actions
and the global `active_exchange_authority_present` conflict. Also cover a
claimed `cancelling_old_entries` batch, half-present claim evidence,
`cancel_submitting`/`submit_unknown` revision legs, and
`submit_reserved`/`submitted` replacements without a parent claim.

### Task 2: Implement the minimal gate correction

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`

**Step 1: Distinguish terminal-clean state from live or ambiguous authority**

Allow only terminal batch states, then add independent fail-closed queries for
claim evidence and ambiguous children:

```python
StrategyRevisionBatch.status.not_in(
    {"succeeded", "blocked", "failed", "recovery_required"}
)
StrategyRevisionBatch.advance_claim_token.is_not(None)
StrategyRevisionBatch.advance_claimed_at.is_not(None)
StrategyRevisionLeg.status.in_({"cancel_submitting", "submit_unknown"})
EntryRevisionReplacement.status.in_({"submit_reserved", "submitted"})
```

The child queries must not depend on a surviving parent claim because recovery
clears that claim after an unknown exchange outcome.

**Step 2: Run GREEN**

Run both new tests, then the complete reviewed-cancellation test file.

### Task 3: Verify, review, and commit the candidate

**Files:**
- Verify: `tests/test_deployment_active_write_check.py`
- Verify: reviewed cancellation and adjacent entry/protection tests
- Verify: full repository suite

**Step 1: Run focused and adjacent tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_deployment_active_write_check.py \
  tests/test_legacy_conditional_cancel.py \
  tests/test_entry_revision_executor.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py
```

**Step 2: Run one final full suite**

```bash
.venv/bin/python -m pytest -q
```

**Step 3: Request independent code review**

Review the exact diff for fail-open regressions and missing authority states.
Resolve all Critical and Important findings before committing.

**Step 4: Commit explicit paths**

Stage only the two plan documents, the reviewed cancellation module, and its
test file. Verify the staged path list before committing. Do not push.

### Task 4: Scope terminal ambiguous children to the reviewed target set

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write RED tests**

Seed a terminal, claim-free `recovery_required` batch on a binding, execution
leg and order outside the reviewed set. Cover both a `submit_unknown` revision
leg and a `submit_reserved` replacement. Assert that the otherwise clean plan
still returns all reviewed actions. Retain the existing tests proving the same
states on the reviewed binding/leg/order fail closed.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py \
  -k "unrelated_terminal_ambiguous"
```

Expected: FAIL with `active_exchange_authority_present` under the current
global ambiguous-child queries.

**Step 3: Implement the minimal target-aware gate**

Pass the reviewed target tuple into `_active_exchange_authority_present` from
both planning and the last-moment apply gate. Leave global non-terminal batch
and claim queries unchanged. Join ambiguous children to their parent batch and
match only reviewed binding IDs, execution-leg IDs, or order IDs.

**Step 4: Run GREEN and regression**

Run the two new tests, the target-related ambiguity tests, the complete reviewed
cancellation file, the adjacent six-file group, and one final full suite.

**Step 5: Review and commit explicit paths**

Resolve all Critical and Important review findings. Stage only the design,
plan, production module, tests and canonical status; do not push.
