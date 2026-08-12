# Batch 119 Instruction Disposition Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace batch 119 recovery's raw instruction-status veto with a batch-specific, fingerprinted durable disposition gate that performs no historical data writes.

**Architecture:** Add one private classifier to the existing dedicated recovery module. Its canonical population payload becomes part of the existing source fingerprint during read-only planning, locked apply, and resume authorization; every unclassified or drifting row still fails closed.

**Tech Stack:** Python 3.13, SQLAlchemy ORM, SQLite read-only sessions and `BEGIN IMMEDIATE`, pytest.

---

### Task 1: Characterize the audited production population

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Add real ORM fixtures for the four dispositions**

Add helpers that create:

- the exact batch 119 source instruction;
- one verified submitted entry mirror;
- one verified submitted management mirror;
- one pending residue with no execution authority; and
- one historical frozen unknown with a terminal descendant.

Use only synthetic IDs and payloads.

**Step 2: Write the failing planner test**

Assert the planner is `ready`, evidence contains only the four bounded counts and
a SHA-256 population digest, and every instruction row is unchanged.

**Step 3: Run the test and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_composite_management_batch_recovery.py \
  -k instruction_population_allows_audited_dispositions
```

Expected: FAIL with `additional_active_work_present`.

### Task 2: Implement the minimal classifier and source binding

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Add private canonical classification helpers**

Load every non-retired instruction outside `succeeded/failed`, validate one of
the four approved dispositions, build sorted redacted row evidence, and return a
bounded population payload. Raise a refusal for every other state.

**Step 2: Bind the payload to source evidence**

Pass the same payload to `_source_evidence_payload` from dry-run planning and
the locked source reread. Add only disposition counts, total count, and canonical
digest to public recovery evidence.

**Step 3: Run the focused test and verify GREEN**

Run the Task 1 command. Expected: PASS.

### Task 3: Fail closed on every proof boundary

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`

**Step 1: Write parameterized failing tests**

Cover:

- zero or duplicate target incident instructions;
- any `executing` instruction;
- submitted entry without the exact trade signal or binding;
- submitted management without its exact terminal batch;
- pending residue with result/error/contract/target/scheduled retry/deadline,
  escalation, trade signal, active lifecycle, active binding, target row, or
  active descendant;
- historical unknown with active lifecycle/binding or nonterminal descendant;
- malformed, oversized, deeply nested, or contradictory JSON; and
- unexpected duplicate durable links.

**Step 2: Verify RED**

Run only the new tests and confirm they fail for the intended missing proof.

**Step 3: Add minimal validations and verify GREEN**

Map all failures to the bounded recovery refusal. Do not catch database I/O or
arbitrary exceptions.

### Task 4: Prove fingerprint and locked CAS behavior

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`

**Step 1: Write failing drift tests**

After a ready dry-run, change one disposition fact while keeping row counts
constant. Assert source/evidence fingerprints change. Then change the population
between dry-run and apply and assert a bounded conflict with zero batch, leg,
component, audit, or exchange mutations.

**Step 2: Verify RED**

Run the drift and concurrency selections. Expected: old code either refuses the
initial plan or fails to bind the drift.

**Step 3: Recompute under the existing write lock**

Reuse the classifier in `_load_locked_recovery_source` and resume authorization.
Keep the lock acquisition before the first source read.

**Step 4: Verify GREEN**

Run the drift, apply, repeated-apply, resume, and CLI recovery selections.

### Task 5: Document and verify the amended production gate

**Files:**
- Modify: `docs/plans/2026-08-12-composite-management-batch-119-recovery.md`
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Amend the zero-active wording**

Require zero unclassified or genuinely active instruction work rather than zero
raw `submitted/unknown/pending` mirrors. State that historical rows remain
unchanged and the disposition digest must match twice around the fresh exchange
snapshot.

**Step 2: Run focused and broad tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_cli_smoke.py \
  tests/test_strategy_management_components.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py
```

Then run the full local suite and `git diff --check`.

### Task 6: Independent review, commit, and push without deployment

**Files:**
- Review every file changed since the design-doc commit.

**Step 1: Request independent code review**

Review the exact range against the approved design. Require explicit checks for
historical-row immutability, batch-only scope, payload redaction, false-safe
classification, lock ordering, CAS completeness, and zero exchange I/O.

**Step 2: Resolve all Critical and Important findings**

Use new RED-to-GREEN tests for every accepted finding, then rerun affected and
full suites.

**Step 3: Commit and push**

Create reviewed local commits and push the current HEAD to
`origin/codex/deepcoin-auto-trading-v1` without force. Confirm the remote SHA.
Do not deploy or restart production.
