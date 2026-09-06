# Shadow Remediation Candidate Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow fingerprint-approved remediation to create a fresh preflight batch when the original canonical candidate already owns a blocked shadow batch.

**Architecture:** Keep the original recognition candidate and shadow batch immutable. Always project a distinct `approved_remediation` candidate for the reviewed action, then reuse the existing disabled-preflight, snapshot verification, promotion, and management execution path.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, Typer, systemd, Deepcoin REST client.

---

### Task 1: Reproduce canonical-candidate shadow reuse

**Files:**
- Modify: `tests/test_position_management_remediation.py`

**Step 1: Write the failing test**

Add a test that persists a canonical management candidate, its failed/succeeded
instruction item, and an existing `management_shadow_plan_only` batch. Enable
live management, build the remediation plan, and apply its action.

Assert that:

- the source candidate remains unchanged;
- one new candidate has `review_status == "approved_remediation"`;
- the new candidate ID differs from the source candidate ID;
- the apply result succeeds through the normal executor.

**Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py -k canonical_source_with_shadow
```

Expected: FAIL with
`remediation planning did not become ready:management_shadow_plan_only`.

### Task 2: Project a distinct remediation candidate

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Test: `tests/test_position_management_remediation.py`

**Step 1: Implement the minimal fix**

Remove the canonical-source early return from
`_project_canonical_remediation_candidate`. Always insert a candidate carrying
the normalized approved action, remediation recognition generation,
`approved_remediation` review status, and the source metadata.

**Step 2: Run the focused test**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py -k canonical_source_with_shadow
```

Expected: PASS.

**Step 3: Run regression suites**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py
```

Expected: PASS.

**Step 4: Commit**

```bash
git add \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_position_management_remediation.py \
  docs/plans/2026-07-27-shadow-remediation-candidate.md
git commit -m "fix: isolate approved shadow remediation candidates"
```

### Task 3: Deploy and execute reviewed actions

**Files:**
- Production checkout: `/opt/telegram-kol-analyzer`
- Production database: `/opt/telegram-kol-analyzer/data/research.db`

**Step 1: Push and deploy**

Push `codex/deepcoin-auto-trading-v1`, run the existing server Git update
helper, and verify the deployed SHA and active service.

**Step 2: Rebuild the production dry-run**

Run `repair-position-management` without `--apply`. Confirm the current live
snapshot still exposes the two Chen full exits and the Nick
`partial_then_break_even` action.

**Step 3: Apply sequentially**

For each action, use its current action ID and fingerprint. Rebuild the dry-run
after every successful action so the next approval uses current exchange state.
Stop immediately on any fingerprint, snapshot, ownership, or exchange
confirmation failure.

**Step 4: Verify**

Read back Deepcoin positions and protection orders, management batches,
execution events, and service status. Confirm the two Chen positions are gone
and Nick has the expected reduced quantity with break-even protection.
