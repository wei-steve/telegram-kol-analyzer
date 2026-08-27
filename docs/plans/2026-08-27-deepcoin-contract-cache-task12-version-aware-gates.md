# Deepcoin Contract Cache Task 12 Version-Aware Gates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Task 12's candidate-before-deployment upgrade loop with a closed version-aware pre-deploy classification and strict post-deploy acceptance contract.

**Architecture:** Keep runtime code and the transactional updater unchanged. Encode the gate as an auditable documentation contract backed by static tests: immutable safety gates always pass, a narrowly recognized legacy cache/endpoint state may proceed to a separately authorized frozen deployment, and all candidate owner/ACL/health invariants become mandatory after deployment and before restoration.

**Tech Stack:** Markdown workflow/runbook/status documents, Python pytest static contract tests.

---

### Task 1: Write the version-aware gate RED tests

**Files:**
- Modify: `tests/test_server_update_scripts.py`
- Read: `docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md`
- Read: `docs/runbooks/deepcoin-contract-cache-ownership-repair.md`
- Read: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Extend the documentation contract test**

Load the main implementation plan and canonical status alongside the existing
runbook. Add assertions requiring all of these exact policy concepts:

- `recognized migratable legacy drift` / `已识别可迁移旧版漂移`;
- root owner plus absent Agent deny ACL is the complete allowed legacy set;
- unknown owner/type/link/group/mode/ACL errors remain fail-closed;
- HTTP 404 may be `legacy_capability_absent` only for the verified previous SHA
  and closed legacy monitor env;
- 401/403, timeout, malformed schema and non-404 HTTP errors remain blockers;
- a valid 100-row history window is bounded coverage rather than a cache
  migration blocker;
- post-deploy helper and health checks must satisfy the complete candidate
  contract;
- the refusal baseline is dynamically recorded as terminal zero-write rows and
  is never replayed or backfilled.

Also assert the obsolete phrases are absent from the prospective Task 12 and
restore contracts:

```python
assert "失败只因 owner 漂移" not in implementation_task12
assert "已知的 15 条历史" not in runbook
assert "历史 15 条拒绝" not in restore_contract
```

Do not weaken the existing assertions for fixed target, exact owner/group/mode,
ACL, regular file, single link, sticky semantics, freeze/restore watermarks, or
zero replay.

**Step 2: Run the focused test and verify RED**

Run:

```bash
pytest -q tests/test_server_update_scripts.py::test_contract_cache_repair_docs_define_closed_freeze_and_restore_contract
```

Expected: FAIL because the current runbook and implementation plan still require
owner-only drift, hard-code 15 refusals, and do not define the legacy health and
100-row classifications.

**Step 3: Commit only the RED test**

```bash
git add -- tests/test_server_update_scripts.py
git diff --cached --name-only
git commit -m "test: require version-aware Task 12 gates"
```

Expected staged path: only `tests/test_server_update_scripts.py`.

### Task 2: Make the Task 12 and restore contracts GREEN

**Files:**
- Modify: `docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md`
- Modify: `docs/runbooks/deepcoin-contract-cache-ownership-repair.md`

**Step 1: Replace the Task 12 owner-only gate**

Document the two allowed pre-deploy results:

1. the complete candidate owner/ACL contract already passes; or
2. the fixed target is a root-owned regular single-link file with the correct
   runtime group and `0660`, and worker owner plus an absent Agent deny ACL are
   the complete recognized migratable legacy drift.

Explicitly retain fail-closed behavior for every other owner, target type, link,
group, mode, ACL, parent, or directory-entry result.

**Step 2: Add the health capability split**

Before deployment, accept a missing endpoint only as HTTP 404
`legacy_capability_absent` when the previous production SHA and closed legacy
monitor env are verified. Block 401/403, timeout, non-404 HTTP failure and bad
schema. After deployment and before restore, require authenticated HTTP 200 and
the exact candidate schema.

**Step 3: Narrow the Deepcoin historical boundary**

Require complete current positions, pending regular orders, pending
trigger/TPSL rows and unique active ownership. Record a schema-valid 100-row
history/fills response as bounded historical coverage. It does not block the
cache migration unless an active row requires evidence outside that window.

**Step 4: Make refusal counts observational**

Replace hard-coded counts with an exact freeze-time set. Require every baseline
row to be terminal `verified_refusal` with
`attempted_exchange_write=0`, and preserve zero replay/zero backfill. Keep
`freeze_raw_message_id` and `restore_raw_message_id` future-only semantics.

**Step 5: Run the RED test and verify GREEN**

Run:

```bash
pytest -q tests/test_server_update_scripts.py::test_contract_cache_repair_docs_define_closed_freeze_and_restore_contract
```

Expected: PASS.

**Step 6: Commit the gate contract**

```bash
git add -- \
  docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md \
  docs/runbooks/deepcoin-contract-cache-ownership-repair.md
git diff --cached --name-only
git commit -m "docs: make Task 12 gates version aware"
```

### Task 3: Record the current evidence without declaring Task 12 passed

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Update canonical fields**

Record:

- approved/pushed candidate SHA
  `a2bc1b4a42e7f9aeceadb2d1e5eb9006d707f3e6`;
- production SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`;
- `task12_observed_max_raw_message_id: 13530`;
- the current 16-row terminal zero-write refusal baseline;
- the four old shadow contract explanations;
- the passed isolated Linux/root sticky test;
- the recognized owner-plus-ACL legacy drift;
- bounded 100-row history coverage;
- the unresolved health HTTP error, without guessing its status.

Keep `task12_gate: failed_closed`, `auto_trade_frozen: false`, and both freeze
and restore watermarks null. State that a new read-only Task 12 run must capture
the exact HTTP status and apply the version-aware classification before Task 13
can be considered.

**Step 2: Commit the status-only handoff**

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: record version-aware Task 12 handoff"
```

### Task 4: Run final L0 verification

**Files:**
- Verify: `tests/test_server_update_scripts.py`
- Verify: all files changed by Tasks 1-3

**Step 1: Run the full static updater/runbook test file**

Run:

```bash
pytest -q tests/test_server_update_scripts.py
```

Expected: all tests pass, including Bash syntax validation for the unchanged
updater scripts.

**Step 2: Run static Git checks**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Expected: no whitespace errors and a clean worktree. This is an L0
documentation/static-test change, so no full runtime suite is required.

**Step 3: Report the boundary**

Report the new local HEAD and focused test result. Explicitly state that no push,
SSH, freeze, deployment, restart, production/settings/database write, exchange
write, replay, or test Telegram message was performed, and that Task 12 remains
fail-closed until a separately authorized production-read-only rerun.

