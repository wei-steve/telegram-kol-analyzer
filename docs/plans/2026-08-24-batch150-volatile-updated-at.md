# Batch 150 Controlled Volatile `updated_at` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the batch `150` copy-rehearsal tool so only leg `553.updated_at` may drift before CAS while every other field remains exact.

**Architecture:** Advance the plan contract with one fingerprinted CAS-policy entry. Normalize only starting-state comparisons and SQL predicates for that exact coordinate; keep full destination writes and exact postconditions. Rehearse the committed tool on a new independent production backup without mutating production.

**Tech Stack:** Python 3.12+, SQLite, canonical JSON/SHA-256, argparse, pytest

---

### Task 1: RED — exact volatile-coordinate behavior

**Files:**

- Modify: `tests/test_batch150_management_terminalization.py`

**Step 1: Add an apply drift test**

Build a plan, change only `execution_order_legs.id=553.updated_at`, and require
apply to return `applied/8`. Verify the complete after row, including the repair
timestamp.

**Step 2: Add idempotent and rollback drift tests**

After apply, refresh only leg `553.updated_at`. Require identical apply to return
`already_applied/0`, then refresh it again and require rollback to return
`rolled_back/8`. Verify every nonvolatile field equals the plan before row and
the rollback writes the captured before timestamp.

**Step 3: Add fail-closed counterexamples**

Parameterize these mutations and require `database_state_mixed` with an
unchanged database digest:

```python
(
    ("execution_order_legs", 553, "status", "cancelled"),
    ("execution_order_legs", 553, "last_verified_at", "drift"),
    ("execution_order_legs", 554, "updated_at", "drift"),
    ("execution_bindings", 320, "updated_at", "drift"),
)
```

**Step 4: Add plan-policy and SQL tests**

Require the serialized plan to contain exactly one ignored-before coordinate.
Tampering, removing, or adding a coordinate must fail plan integrity. Require
rendered rollback SQL to omit leg `553.updated_at` only from that row's `WHERE`
clause while retaining it in `SET`; execution against an approved timestamp
drift succeeds, while another-field drift fails atomically.

**Step 5: Run RED**

Run:

```bash
.venv/bin/pytest -q tests/test_batch150_management_terminalization.py
```

Expected: the new approved-drift tests fail because current full-row state
classification still returns `database_state_mixed`.

### Task 2: GREEN — fingerprinted single-field policy

**Files:**

- Modify: `src/telegram_kol_research/batch150_management_terminalization.py`
- Test: `tests/test_batch150_management_terminalization.py`

**Step 1: Add the exact policy to the plan**

Add a canonical constant equivalent to:

```python
{
    "execution_order_legs:553": {
        "ignored_before_fields": ["updated_at"],
    }
}
```

Store it in the plan, advance the schema, and include it in plan material,
serialization, integrity validation, fingerprints, and CLI plan summary.

**Step 2: Normalize only starting-state comparisons**

Add a helper that removes the approved ignored field from an action row for
state classification. Use it only when determining all-before/all-after. Keep
post-update row reads exact and complete.

**Step 3: Narrow the CAS predicate**

For leg `553`, omit only `updated_at` from the SQL `WHERE`; retain it in the
changed-column assignments. Keep all other predicates unchanged and retain the
one-row guard.

**Step 4: Narrow rendered rollback SQL identically**

Use the same policy helper for rendered SQL predicates. Do not create a second
policy implementation.

**Step 5: Run GREEN**

Run the Task 1 command. Expected: every test passes.

**Step 6: Commit exact paths**

```bash
git add -- \
  src/telegram_kol_research/batch150_management_terminalization.py \
  tests/test_batch150_management_terminalization.py
git diff --cached --name-only
git commit -m "fix: tolerate exact batch 150 timestamp drift"
```

### Task 3: Local acceptance and review

**Files:**

- Review: `src/telegram_kol_research/batch150_management_terminalization.py`
- Review: `tests/test_batch150_management_terminalization.py`

**Step 1: Run focused compatibility**

```bash
.venv/bin/pytest -q \
  tests/test_batch150_management_terminalization.py \
  tests/test_historical_management_terminalization.py \
  tests/test_management_history_recovery.py
```

**Step 2: Run static checks**

```bash
.venv/bin/python -m py_compile \
  src/telegram_kol_research/batch150_management_terminalization.py
.venv/bin/python -m telegram_kol_research.batch150_management_terminalization --help
git diff --check
```

**Step 3: Review the exact diff**

Confirm there is one policy coordinate, no production runtime import, no
exchange client import, no settings/schema change, and no generalized volatile
field behavior.

### Task 4: Fresh production-copy candidate

**Server root:** `/opt/telegram-kol-analyzer`

**Step 1: Recheck read-only production gates**

Require production SHA `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb`,
split services active, monolith inactive, `global + 20`, recovery set `[150]`,
no unsafe management, no claimed job, no unconfirmed binding-320 intent, and
source `quick_check=ok` under `query_only=1`.

**Step 2: Capture fresh GET-only exchange evidence**

Repeat the bounded exact target/sibling history, parent-child lineage, owned
stop, and current absence reads. One reasoned retry is permitted for an
incomplete external response. Require `snapshot_complete=true`, empty errors,
and `exchange_write_count=0`.

**Step 3: Create a new private backup chain**

Create a new mode-0700 evidence root. Because current server free space holds
only one full database copy, create an online backup, verify its SHA/mode/quick
check, transfer it to the existing private local evidence area, create an
independent rehearsal through SQLite backup, and retain only that independent
copy on the server. Delete only incomplete or successfully transferred files
inside the newly created evidence root.

**Step 4: Transfer the exact committed tool**

Copy only the committed module into the new evidence root and record its
SHA-256. Do not install it into the production checkout.

### Task 5: Copy-only volatile-drift rehearsal

**Step 1: Build a plan against the new rehearsal**

Persist plan and rollback SQL, requiring the exact one-coordinate policy and
all canonical fingerprints.

**Step 2: Reproduce the production race on the copy**

After planning, update only leg `553.updated_at` on `rehearsal.db`. This is an
authorized mutation of the copy, not production.

**Step 3: Apply and reapply**

Require `applied/8`, exact after rows, then refresh only leg `553.updated_at`
again and require `already_applied/0`.

**Step 4: Roll back through another timestamp drift**

Refresh only leg `553.updated_at` once more, require `rolled_back/8`, and verify
the complete planned before rows plus exact nonvolatile logical digest.

**Step 5: Prove counterexample refusal**

On a separate fresh rehearsal copy, change one nonvolatile leg-553 field and
require zero committed repair rows. Restore only that copy from its pristine
local source if another run is needed; never repair production.

**Step 6: Prove production stayed untouched**

Re-read production exact target rows, source quick check/query-only/total
changes, services, modes, and gates. Record production and exchange write
counts as zero.

### Task 6: Status and handoff

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Write a private rehearsal summary**

Record tool commit/hash, backup paths/hashes/modes, plan/action/rollback/policy
fingerprints, drift injections, apply/reapply/rollback results, refusal result,
quick checks, counts, and authorization boundary.

**Step 2: Update canonical status**

Record the new local candidate and evidence root. Keep deployment and cutover
authorization false. Do not present a production apply fingerprint unless a
fresh production-path plan remains exact at handoff.

**Step 3: Commit the status file explicitly**

```bash
git add -- docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git commit -m "docs: record volatile CAS copy rehearsal"
```

**Step 4: Final boundaries**

Return the integration HEAD and evidence. Do not push, deploy, restart, apply
production data, cut over, replay traffic, mutate settings, or write to the
exchange.
