# Deepcoin Contract Cache Task 12 Findings Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair the local Task 12 candidate so a known legacy monitor env can be upgraded transactionally, the Linux/root sticky pytest is valid, and the refusal baseline is 15 without any production action or replay.

**Architecture:** Separate legacy source validation from strict destination validation. Normalize the missing governed monitor field inside the existing byte-preserving env transaction, retain strict fail-closed behavior for unknown states, and fix only the isolated Linux test ancestor permissions.

**Tech Stack:** Bash, Python 3.11, pytest, systemd artifact harness, POSIX sticky-directory semantics.

---

## Scope and authority

- Base: exact clean `eb3dc0d0868d8131f003c869842bddba07aa5c29` on
  `codex/phase0-deploy-integration` in the authoritative main workspace.
- Local files, tests, documentation, canonical status, and explicit-path local
  commits only.
- No push, SSH, deployment, restart, production/settings/database mutation,
  replay, manufactured traffic, Telegram send, or Deepcoin write.

### Task 1: Reproduce the legacy monitor-env upgrade loop

**Files:**

- Modify: `tests/test_minimal_server_updater.py`

**Step 1: Add a legacy fixture mode**

Generate an otherwise-valid monitor env with one expected-HEAD line, zero
auto-trade expectation lines, preserved secret/settings bytes, owner/mode
contract, and a complete installed timer.

**Step 2: Write RED tests**

Require enabled and disabled deployment requests to normalize that legacy env,
reach checkout, finish with exactly one governed expectation line, preserve all
other bytes, and never print the secret. Require a post-normalization failure to
restore the original legacy env byte-for-byte.

**Step 3: Verify RED**

Run only the new tests. Expected: exit 4 before checkout because current
classification uses the strict candidate schema.

### Task 2: Implement narrow source-schema normalization

**Files:**

- Modify: `deploy/telegram-kol-update`
- Test: `tests/test_minimal_server_updater.py`

**Step 1: Split validation modes**

Keep strict validation as the default. Add an explicit legacy-source mode that
accepts only zero expectation lines; one valid line is current, duplicates or
invalid values remain rejected.

**Step 2: Normalize in the existing transaction**

Allow classification and backup of the known legacy source. In the candidate
rewrite, replace the unique expected HEAD and insert the fixed requested option
after it if absent. Strictly validate the candidate before atomic installation
and strict-check the installed env afterward.

**Step 3: Verify GREEN and regressions**

Run legacy tests, current-schema transaction tests, malformed/duplicate tests,
all monitor rollback tests, and `bash -n deploy/telegram-kol-update`.

**Step 4: Commit explicitly**

Stage only `deploy/telegram-kol-update` and
`tests/test_minimal_server_updater.py`; verify the cached path list before
committing.

### Task 3: Repair the Linux/root sticky pytest fixture

**Files:**

- Modify: `tests/test_contract_cache_permissions.py`

**Step 1: Preserve the production test as RED evidence**

The Task 12 evidence already proves the shipped test exits through the generic
child failure because UID 65534 cannot traverse pytest's root-only ancestors.
Record this root cause in the test structure, not by weakening assertions.

**Step 2: Use a traversable isolated ancestor**

Create a unique directory under the platform temporary root, set only the
required traversal and sticky modes, run the real cross-UID replacement proof,
and remove only that exact directory in `finally`.

**Step 3: Verify**

Run the test locally for syntax/skip behavior and run the exact test in the
authorized isolated Linux/root environment. No real cache path is allowed.

**Step 4: Commit explicitly**

Stage only `tests/test_contract_cache_permissions.py` and verify the cached path
list before committing.

### Task 4: Update historical and replay boundaries

**Files:**

- Modify: `docs/runbooks/deepcoin-contract-cache-ownership-repair.md`
- Modify: `docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md`
- Modify: `tests/test_server_update_scripts.py`

**Step 1: Write RED**

Require prospective runbook acceptance to use the observed baseline of 15 and
to preserve terminal, zero-write, zero-replay, and zero-backfill semantics.

**Step 2: Update documentation**

Change only prospective baseline/acceptance language. Record that the four old
zero-write nonterminal contracts still require production-read-only
explanation; do not invent or mutate their state.

**Step 3: Verify GREEN**

Run the documentation contract tests and `git diff --check`.

### Task 5: Form the new local candidate

**Files:**

- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1:** Run the complete affected focused set and static syntax checks.

**Step 2:** After the last production-code edit, run one final complete suite.

**Step 3:** Commit implementation/test/docs paths explicitly; never use
`git add -A`.

**Step 4:** Record the new candidate content SHA, test evidence, Linux/root
result, and remaining production-only Task 12 gates in canonical status.

**Step 5:** Commit the status by explicit path, then verify exact HEAD, branch,
clean tree, and changed paths. Push and all production actions remain separately
unauthorized.

