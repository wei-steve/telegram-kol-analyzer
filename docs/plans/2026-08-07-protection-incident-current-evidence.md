# Protection Incident Current-Evidence Convergence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the read-only protection incident audit resolve legacy and transient incidents from exact complete current exchange evidence without changing historical rows or trading state.

**Architecture:** Preserve the strict newer-revision path, then reuse the existing protection snapshot matcher and account-wide ownership index as a fail-closed fallback for live exact-position scopes. Cache one result per scope and keep every output redacted and every operation read-only.

**Tech Stack:** Python, SQLAlchemy, SQLite, Typer, pytest, Deepcoin read-only reconciliation snapshots.

---

### Task 1: Reproduce the production-shaped convergence failure

**Files:**

- Modify: `tests/test_protection_incident_convergence.py`

**Step 1: Add a legacy current-state fixture**

Create one live exact position with:

- an incident newer than a legacy active revision whose payload has only
  `order_ids`;
- a verified primary `stop_loss` ledger row;
- an active specialized backup row whose ledger purpose is the historical
  `stop_loss` value;
- one verified take-profit row;
- complete pending TPSL rows that contain order IDs but no `posId`.

Add a second incident for the same exact scope to prove the result is reusable.

**Step 2: Add the expected classification assertion**

Require both rows to classify as
`resolved_by_current_exchange_evidence`, with unchanged redacted output.

**Step 3: Run the test and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_incident_convergence.py \
  -k legacy_current_exchange_evidence_without_pending_pos_id \
  -q
```

Expected: FAIL because the existing implementation requires a newer
role-aware revision and filters pending orders by `posId`.

**Step 4: Commit the failing regression test**

```bash
git add tests/test_protection_incident_convergence.py
git commit -m "test: reproduce protection audit convergence gap"
```

### Task 2: Add the exact current-state convergence path

**Files:**

- Modify: `src/telegram_kol_research/protection_incident_convergence.py`
- Test: `tests/test_protection_incident_convergence.py`

**Step 1: Load reusable current evidence once**

Import `PositionBackupStopOrder`, `PositionTakeProfitOrder`,
`build_position_protection_audit`, and
`load_account_protection_ownership`. Build the live-position map,
target-instrument completeness set, and account-wide ownership index once per
audit. Add a dictionary keyed by exact venue/binding/leg/position scope.

**Step 2: Implement one pure scope classifier**

For the exact incident scope:

- load only its current ledger, specialized backup, take-profit, and revision
  rows;
- identify active specialized backup order IDs;
- exclude only those exact IDs from legacy `stop_loss` primary candidates;
- call `build_position_protection_audit()` with the coherent pending rows,
  all open positions, exact rows, account ownership, and no freeze reasons;
- require target-instrument completeness, `protected=true`, at least one
  verified TP, and distinct nonempty primary/backup order IDs.

Return a boolean only; never flush or commit.

**Step 3: Compose it after the strict replacement path**

For a live position, classify as resolved when either the existing strict
newer-revision proof succeeds or the new exact current-state proof succeeds.
Keep terminal and evidence-insufficient behavior unchanged.

**Step 4: Run the focused test and verify GREEN**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_incident_convergence.py \
  -k legacy_current_exchange_evidence_without_pending_pos_id \
  -q
```

Expected: PASS.

**Step 5: Run the complete convergence test module**

```bash
.venv/bin/python -m pytest tests/test_protection_incident_convergence.py -q
```

Expected: all pass.

**Step 6: Commit the minimal implementation**

```bash
git add src/telegram_kol_research/protection_incident_convergence.py \
  tests/test_protection_incident_convergence.py
git commit -m "fix: converge protection incidents from exact current evidence"
```

### Task 3: Prove every fail-closed boundary

**Files:**

- Modify: `tests/test_protection_incident_convergence.py`
- Modify only if a failing boundary requires it: `src/telegram_kol_research/protection_incident_convergence.py`

**Step 1: Add parameterized failing-evidence tests**

Independently remove or corrupt:

- the target-instrument complete observation;
- the active specialized backup;
- the backup pending order;
- every take-profit order;
- exact binding or leg ownership;
- distinct primary/backup identity;
- canonical account ownership;
- the position's live exchange row.

Require incomplete snapshots to remain `evidence_insufficient`; other live
exact-position gaps remain `current_risk`.

**Step 2: Add the unowned-order and conflicting-owner tests**

An unowned native order that can affect the position and one order assigned to
multiple live positions must both prevent resolution.

**Step 3: Verify RED for any uncovered boundary**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_incident_convergence.py \
  -k "current_evidence and (missing or conflict or incomplete or duplicate or unowned)" \
  -q
```

Expected: each newly exposed implementation gap fails for its intended
classification, not because of fixture or schema errors.

**Step 4: Make the smallest implementation corrections**

Add only the guard required by each failing test. Do not add a database write,
new model, migration, feature flag, or exchange client call.

**Step 5: Run the focused module**

```bash
.venv/bin/python -m pytest tests/test_protection_incident_convergence.py -q
```

Expected: all pass.

**Step 6: Commit the boundary coverage**

```bash
git add src/telegram_kol_research/protection_incident_convergence.py \
  tests/test_protection_incident_convergence.py
git commit -m "test: enforce protection convergence boundaries"
```

### Task 4: Prove read-only behavior and adjacent compatibility

**Files:**

- Modify: `tests/test_protection_incident_convergence.py`
- Update: `docs/runtime-incident-agent-runbook.md`

**Step 1: Add an immutability regression test**

Snapshot every relevant incident, revision, ledger, backup, take-profit,
binding, and leg row before and after the audit. Require byte-for-field equality
and no new rows.

**Step 2: Run focused and adjacent tests**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_incident_convergence.py \
  tests/test_protection_snapshot.py \
  tests/test_protection_ledger.py \
  tests/test_backup_stop_repair.py \
  tests/test_runtime_incident_adapters.py \
  -q
```

Expected: all pass.

**Step 3: Document the current-evidence boundary**

In `docs/runtime-incident-agent-runbook.md`, state that legacy incident
convergence may use complete exact current evidence, list every fail-closed
condition, and state explicitly that the audit does not write the database or
call a Deepcoin mutation.

**Step 4: Run formatting and diff checks**

```bash
git diff --check
```

Expected: no output.

**Step 5: Commit the verification and runbook**

```bash
git add tests/test_protection_incident_convergence.py \
  docs/runtime-incident-agent-runbook.md
git commit -m "docs: define current protection convergence evidence"
```

### Task 5: Review, deploy, and verify production

**Files:**

- Update: `docs/runtime-incident-agent-status.md`

**Step 1: Review the complete diff**

Run the code-review workflow. Reject any database mutation, new exchange call,
raw identifier in audit output, loose symbol/side matching, or bypass of
account-wide ownership.

**Step 2: Push the reviewed commits**

Push to `codex/deepcoin-auto-trading-v1` only after all focused and adjacent
tests pass.

**Step 3: Prove a fresh production deployment window**

Require terminal latest recognition, matching live checkpoints, zero work in
flight, and two identical complete exchange snapshots. Deploy through the
standard server update helper and restart only during that proven window.

**Step 4: Run server tests before the production audit**

Run the focused and adjacent suite from Task 4 on the deployed commit.

**Step 5: Run one no-notify production audit**

Expected classification from the currently reviewed state:

- seven `resolved_by_current_exchange_evidence` rows for the two protected
  live positions;
- zero `current_risk` rows for those positions;
- one unchanged `evidence_insufficient` row;
- 222 unchanged `historical_terminal` rows.

Stop if the exchange snapshot is incomplete or any live row remains ambiguous.

**Step 6: Verify invariants**

Require:

- all four live positions and every reviewed TPSL order unchanged;
- relevant database rows and maximum IDs unchanged by the audit;
- no notification or Runtime Agent claim;
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID=272` unchanged;
- the Agent selector still exactly `management_partial_failed`;
- main, Agent, scanner, monitor timer, and HTTP health active;
- independent no-notify diagnostic healthy.

**Step 7: Record and push the production checkpoint**

Update `docs/runtime-incident-agent-status.md` with test totals, deployed SHA,
classification counts, exchange fingerprint, database invariants, service
health, and notification boundaries. Commit and push the documentation-only
checkpoint without redeploying it.

