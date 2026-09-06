# Batch 150 Historical Terminalization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce and rehearse an exact eight-row compare-and-set terminalization for historical management batch `150` without modifying production.

**Architecture:** Add a standalone, standard-library-only batch-specific planner and mutation engine. It validates frozen database identity plus normalized exchange evidence, emits canonical plan/rollback artifacts, and permits apply or rollback only when every exact authorization and row-state gate matches.

**Tech Stack:** Python 3.12+, SQLite URI read-only mode and online backup API, argparse, canonical JSON/SHA-256, pytest

---

## Task 1: RED — exact planner matrix and exchange proof

**Files:**

- Create: `tests/test_batch150_management_terminalization.py`
- Create later: `src/telegram_kol_research/batch150_management_terminalization.py`

### Step 1: Seed only the required production-shaped tables

Create a fixture database containing exact rows for batch `150`, management
leg `133`, components `22/23/24`, binding `320`, execution legs `553/554`,
lifecycle `952`, confirmed mutation intents, and the no-reservation invariant.
Include one unrelated resolved batch so target-set checks cannot be implemented
as a total-row-count shortcut.

### Step 2: Define normalized complete exchange evidence

The fixture must contain:

```python
{
    "snapshot_complete": True,
    "snapshot_errors": {},
    "exchange_write_count": 0,
    "positions": [],
    "open_orders": [],
    "pending_trigger_orders": [],
    "target_position_history": [{"posId": TARGET_POS_ID, "pos": "11", "closePos": "11", ...}],
    "sibling_position_history": [{"posId": SIBLING_POS_ID, "pos": "11", "closePos": "11", ...}],
    "target_stop": {"ordId": TARGET_STOP_ID, "triggerTime": CLOSE_SECONDS, ...},
    "sibling_stop": {"ordId": SIBLING_STOP_ID, "triggerTime": CLOSE_SECONDS, ...},
    "parent_child_chain": {
        "parent_trigger_order_id": PARENT_TRIGGER_ID,
        "unique_child_regular_order_id": SIBLING_POS_ID,
        "child_pos_id": SIBLING_POS_ID,
        "child_state": "filled",
        "child_size": "11",
        "child_created_at": PARENT_TRIGGER_SECONDS,
        "parent_trigger_time": PARENT_TRIGGER_SECONDS,
    },
}
```

### Step 3: Write the expected planner tests

Require:

- exactly eight actions with the approved per-table PK matrix;
- component `22` and every audit/evidence table remain outside the action set;
- retained exact `pos_id` on legs `553/554`;
- binding `320` closes with `pos_id=None`;
- canonical exchange/database/action/rollback/plan fingerprints;
- plan JSON round-trip and mode `0600`;
- rollback SQL contains exactly eight reverse CAS updates.

Parameterize fail-closed cases for incomplete snapshots, any live/open/pending
match, missing or duplicate position history, `closePos != pos`, wrong exact
identity, untriggered/wrong stop, non-unique parent-child chain, a third binding
leg, an unconfirmed intent, a close reservation, and a changed database
fingerprint.

### Step 4: Run RED

Run:

```bash
.venv/bin/pytest -q tests/test_batch150_management_terminalization.py
```

Expected: collection fails because the new module does not exist.

## Task 2: GREEN — read-only planner and artifact serialization

**Files:**

- Create: `src/telegram_kol_research/batch150_management_terminalization.py`
- Test: `tests/test_batch150_management_terminalization.py`

### Step 1: Add exact constants and data classes

Define `EXPECTED_ACTION_COUNT = 8`, exact IDs/fingerprints/strategy identity,
`REPAIR_REASON`, `TERMINAL_LEG_REASON`, and immutable action/plan/result data
classes. No exchange client import is allowed.

### Step 2: Implement `build_batch150_terminalization_plan`

Signature:

```python
def build_batch150_terminalization_plan(
    database_path: str | Path,
    *,
    exchange_evidence: Mapping[str, Any],
    repair_ts: datetime,
    code_sha: str,
) -> Batch150TerminalizationPlan:
    ...
```

Open with URI `mode=ro`, enable `query_only`, run `quick_check`, validate every
database and exchange invariant from the approved design, capture full before
rows, construct only the eight after rows, and derive canonical fingerprints
and a confirmation token.

### Step 3: Implement plan artifact helpers

Add:

```python
write_batch150_terminalization_plan(path, plan)
load_batch150_terminalization_plan(path)
render_batch150_rollback_sql(plan)
```

Plan and SQL files must be mode `0600`. Serialization must revalidate all
fingerprints and the exact action count.

### Step 4: Run GREEN planner tests

Run the exact Task 1 command. Expected: planner/evidence/serialization tests
pass; mutation tests are not added yet.

### Step 5: Commit Task 2

Stage only the new module and new test file, verify the staged paths, and
commit with `feat: add exact batch 150 terminalization planner`.

## Task 3: RED→GREEN — CAS apply, idempotency, and rollback

**Files:**

- Modify: `tests/test_batch150_management_terminalization.py`
- Modify: `src/telegram_kol_research/batch150_management_terminalization.py`

### Step 1: Write RED mutation tests

Require:

- first apply changes exactly eight rows;
- identical reapply returns `already_applied` and zero changes;
- rollback changes exactly eight rows and restores the original logical digest;
- wrong plan fingerprint, timestamp spelling, token, action count, database
  path, table counts, or any before-row drift produces zero committed writes;
- rollback from a mixed or runtime-canonicalized state refuses with zero writes;
- quick check and table counts remain unchanged.

Run only the new mutation tests and observe the expected missing-function RED.

### Step 2: Implement CAS mutation functions

Add:

```python
def apply_batch150_terminalization_plan(...): ...
def rollback_batch150_terminalization_plan(...): ...
```

Use `BEGIN IMMEDIATE`, full-row `IS` predicates, a one-row-change guard, exact
start/end-state classification, postcondition reads, count gates, and
`quick_check` before commit. Roll back on every exception.

### Step 3: Implement argparse CLI

Commands:

- `plan`: read evidence JSON and emit plan JSON plus rollback SQL;
- `apply`: require exact plan fingerprint, action count, repair timestamp, and
  confirmation token;
- `rollback`: require exact rollback fingerprint, action count, and token.

CLI output is summary-only and contains no credentials or Telegram text.

### Step 4: Run GREEN

Run the complete new test file. Expected: all tests pass.

## Task 4: Local acceptance and reviewed tool commit

**Files:**

- `src/telegram_kol_research/batch150_management_terminalization.py`
- `tests/test_batch150_management_terminalization.py`
- `docs/plans/2026-08-24-batch150-historical-terminalization-design.md`
- `docs/plans/2026-08-24-batch150-historical-terminalization.md`

### Step 1: Run focused compatibility

```bash
.venv/bin/pytest -q \
  tests/test_batch150_management_terminalization.py \
  tests/test_historical_management_terminalization.py \
  tests/test_management_history_recovery.py
```

### Step 2: Run static checks

```bash
.venv/bin/python -m py_compile \
  src/telegram_kol_research/batch150_management_terminalization.py
.venv/bin/python -m telegram_kol_research.batch150_management_terminalization --help
git diff --check
```

### Step 3: Review exact diff

Confirm no runtime worker, trading settings, exchange-write client, schema, or
existing six-batch utility changed.

### Step 4: Commit explicit paths

Never use `git add -A`. Commit only the approved module, tests, and plan files.
Do not push.

## Task 5: Fresh server evidence and online production backup

**Server root:** `/opt/telegram-kol-analyzer`

### Step 1: Recheck immutable gates

Require exact production SHA `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb`,
split services active, monolith inactive, `global + 20`, batch set `[150]`, no
unsafe management status, no claimed message jobs, no unconfirmed binding-320
intent, and source `quick_check=ok`.

### Step 2: Create private evidence directory

Create one timestamped directory below
`data/evidence/batch150-terminalization-rehearsal-*`, mode `0700`. Every file
inside must be mode `0600`.

### Step 3: Capture one fresh normalized exchange snapshot

Use worker-only credentials and only GET/list methods. Persist the exact
target/sibling histories, parent-child lineage, owned-stop histories, current
absence sets, completeness fields, and `exchange_write_count=0`.

### Step 4: Create online backups

Use `sqlite3.Connection.backup` to create:

- immutable `research-online-backup.db`;
- independent `rehearsal.db` copied from the immutable backup through the
  SQLite backup API.

Require source/backup/rehearsal `quick_check=ok`, mode `0600`, SHA-256, and
matching critical table counts.

## Task 6: Copy-only plan/apply/idempotency/rollback rehearsal

### Step 1: Transfer the exact committed tool

Copy only the committed module into the evidence directory and record its
SHA-256. Do not install it into production or alter the production checkout.

### Step 2: Generate the plan against `rehearsal.db`

Use the committed tool SHA as `code_sha`. Persist plan JSON and rollback SQL.
Record plan, action, rollback, database, and exchange fingerprints; repair
timestamp; confirmation token; artifact hashes and modes.

### Step 3: Rehearse mutation boundaries

Apply once and require eight changes. Apply the same plan again and require
`already_applied/0`. Verify exact eight after rows. Roll back and require eight
changes. Verify exact eight before rows and the original logical digest.

### Step 4: Prove production remained untouched

Re-read production batch `150`, binding `320`, legs `553/554`, lifecycle `952`,
and source `total_changes=0/query_only=1/quick_check=ok`. Recheck service/mode
state and record production/exchange write counts as zero.

### Step 5: Write `rehearsal-summary.json`

The summary includes only commit, artifact paths/hashes, modes, fingerprints,
row counts, quick checks, exact state result, and authorization boundary. Raw
exchange rows remain in the private evidence directory.

## Task 7: Canonical status and handoff

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

Record the exact tool commit, focused tests, evidence root, backup/plan/rollback
hashes, all fingerprints, rehearsal results, and zero production/exchange
writes. Keep `deployment_authorized=false` and `cutover_authorized=false`.

Commit the status file by explicit path. Return the exact integration HEAD and
the exact later production-apply authorization parameters, but do not apply,
push, deploy, restart, or cut over.
