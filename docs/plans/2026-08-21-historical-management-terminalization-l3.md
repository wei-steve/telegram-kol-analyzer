# Historical Management Terminalization L3 Implementation Plan

> **For Codex:** Implement and execute this plan only in separately authorized
> steps. The present planning task authorizes no production mutation, no
> `--apply`, no replay, no deployment, and no exchange write.

**Goal:** Terminalize six proven historical management batches and their stale
local projections without claiming that the requested management actions
executed and without changing recognition, strategy resolution, position
ownership, or exchange-execution semantics.

**Architecture:** Build a deterministic dry-run plan from a stable SQLite
snapshot plus complete, exact Deepcoin evidence. Rehearse the same
compare-and-set action list on a production database copy, then—only after a
second exact-fingerprint authorization—apply all 45 row updates in one
`BEGIN IMMEDIATE` transaction while the service is stopped. No exchange client
write method is available to the repair path.

**Tech Stack:** Python 3.13, SQLite/SQLAlchemy, existing read-only Deepcoin
client, pytest, systemd, SHA-256 canonical JSON fingerprints.

---

## 1. Authorization boundary

This document is a plan, not authorization to execute it.

Forbidden in the planning turn and until separately approved:

- editing the production database;
- replaying any Telegram message;
- cancelling or creating any regular order, trigger order, or TPSL order;
- invoking any `--apply` option;
- deploying or pushing a commit;
- changing `message_lock_mode` or `message_pipeline_mode`;
- calling an exchange-write adapter, including as a “test”;
- expanding the target set beyond batches `123,127,129,133,144,146`.

The implementation must split authorization into two later checkpoints:

1. authorize implementation of the supervised tool, focused tests, and a
   rehearsal on a production database copy; this still authorizes no production
   database mutation;
2. after review of the exact tool SHA, dry-run plan fingerprint, expected
   action count `45`, backup path, and rollback file, authorize the exact
   production stop/apply/verify/start operation.

## 2. Verified planning baseline

The following was verified read-only on 2026-08-21. It is planning evidence,
not a substitute for the fresh apply gate.

- Local `HEAD`, `origin/codex/deepcoin-auto-trading-v1`, and production `HEAD`
  were all exactly
  `fdaff6b12d0aa4470e9bfcc63239c8541c01c5ff`.
- The local worktree was clean.
- `telegram-kol.service` was `active`/`running`.
- Production settings were `message_lock_mode=global` and
  `message_pipeline_mode=queue`.
- A production SQLite read-only transaction returned `PRAGMA quick_check=ok`.
- The complete `recovery_required` batch set was exactly
  `[123,127,129,133,144,146]`.
- The six bindings had no nonterminal position-mutation intent; every linked
  mutation intent was already `confirmed`.
- The evidence directory is private (`0700 root:root`), and all six evidence
  files are private (`0600 root:root`):
  `/opt/telegram-kol-analyzer/data/evidence/runtime-soak-remediation-six-20260821T171157Z/`.

Evidence file SHA-256 values:

| File | SHA-256 |
|---|---|
| `management-batches.json` | `b4e12cc76570f9a9c9f7c35ac05f09109a1989571e820e5ba92fbd83d291f841` |
| `protection-incidents.json` | `2bbf89c66e41beeb706dd91fdff9ac2792c3994dc8285f393773277707e1a6c9` |
| `six-batches-classification.json` | `93e78ecb1759d2c50ebcb5ffa3fb8ad85d0694886df857b51247860d2cda19f1` |
| `six-batches-exchange-chain.json` | `c6dc1d61b205a27ee0f4e6a8cd325b8ba47f67f523fe837a14326a9fd84d0b62` |
| `six-batches-local-chain.json` | `e18a4f4791f9fb6c057232d848f1a0caf2083000526d2fd6a56c5cae54db6220` |
| `tpsl-ownership.json` | `b51b7c1c86b8ad88b23c3327071f788943d207c32162034176c3744864ed7d56` |

`management-batches.json` contains all `146/146` batches, is stable and
parse-valid, and has no malformed rows or fields. The protection snapshot has
`exchange_snapshot_complete=true` and `current_risk=0`. The TPSL ownership
snapshot has two live positions, eight pending TPSL rows, all eight owned,
zero conflicts, zero unowned rows, and `exchange_write_count=0`.

`protection-incidents.json` is intentionally limited to 100 of 328 historical
incidents, so its top-level `output_complete=false` is not used to prove the
six-batch target set. Its complete current exchange snapshot and `current_risk`
classification are used only as corroborating current-risk evidence.

Planning-time table totals were:

| Table | Rows |
|---|---:|
| `strategy_management_batches` | 146 |
| `strategy_management_legs` | 127 |
| `strategy_management_components` | 21 |
| `execution_bindings` | 315 |
| `execution_order_legs` | 544 |
| `strategy_lifecycles` | 925 |
| `position_mutation_intents` | 530 |
| `execution_events` | 3675 |
| `raw_messages` | 12227 |
| `recognition_decisions` | 12226 |

These totals can grow before execution. The apply gate must capture a fresh
quiescent baseline and compare before/after equality; it must not require these
historical numbers verbatim.

## 3. Exact identity and immutable evidence matrix

Identity is proven only by the stored batch binding, the unique verified
execution leg, and the exact Deepcoin `posId`. Symbol, side, time proximity,
tag, and `clOrdId` are never sufficient.

| Batch | Raw / source message | Lifecycle | Binding | Target execution leg | Management leg / components | Exact `posId` | Deepcoin `pos == closePos` |
|---:|---|---:|---:|---:|---|---|---|
| 123 | `10696` / `4250` | 819 | 283 | 497 | 107 / 4,5,6 | `1001124765619311` | `2.3 == 2.3` |
| 127 | `10747` / `10009` | 816 | 282 | 496 | 110 / 7,8,9 | `1001124765261315` | `12 == 12` |
| 129 | `10839` / `4255` | 834 | 287 | 503 | 112 / 10,11,12 | `1001124787260932` | `2.3 == 2.3` |
| 133 | `11279` / `4275` | 859 | 292 | 511 | 117 / 13,14,15 | `1001124837556751` | `15 == 15` |
| 144 | `11892` / `4332` | 910 | 307 | 530 | 125 / 16,17,18 | `1001124898122909` | `8 == 8` |
| 146 | `12068` / `8823` | 921 | 313 | 540 | 127 / 19,20,21 | `1001124908211764` | `2.2 == 2.2` |

For every row above, the exact position is absent from the complete live
position snapshot, regular open orders, and pending TPSL snapshot, and is
present exactly once in position history with full-close proof. No incomplete
external response may be interpreted as absence.

The immutable stored fingerprints that must remain byte-for-byte unchanged are:

| Batch | Batch idempotency | Management contract | Target | Source text |
|---:|---|---|---|---|
| 123 | `d08672fd18fab476a9b7ed70d195d9ff2ccf27eb6d570a78c3b163ba58f6be7c` | `cbddc9b6dd2ec5fc26c49c5211ef0dcd73aaafdb2c1aea3faa796b10805b5192` | `1b46a80258e1ccfea24451b1c59cb1724328b4563e9c303ff61651c5b03dbf90` | `23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17` |
| 127 | `265f3c080298324bf0e2e1277a1205c5ddba0979e841e09549a69c604f89f95b` | `5329927c8a9a8ce17a4e912d64bac83c6d53f7162382ce16d009e1ab2aec8624` | `38df032b721c7b25585c8a6f23c384c4dd5ed1754ec2317bb14ca29fbbb80c04` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| 129 | `4502bcd6b647a17724beb9c4ab150005a402435da0ea366bbf9c175f08744c71` | `117bf9461ae417610429f4f341660e7325ddd7294945ad0e206e46115e423c54` | `90144990d71b1058815b0e9f116ee4c0f71fc0fd73e714a1456c4d10d7b1f640` | `23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17` |
| 133 | `d7ca719ce01bfb29923a2cdfdc7e08f086922386322480724768569c81d973c5` | `6680b2b5865195c909c086fc614da34e4eefdeae845fe6d738993145f80203e2` | `9b397543737394df0be4a9bb682bb330b872d1781cd768ced430e9df32c6e59e` | `23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17` |
| 144 | `d3caa0f8209c445f181dbf06725b9dd7f2e60a222297160baa98dd31eca45d25` | `e0b5a2a31c10792bb64d0654dfe055064e2d46477d1836dfdf710bc5454bf8bd` | `54241e51585e92532c62d92e15251ec49aee809e0b3f839a7a43c5f921222fa5` | `c3d414b80c2f6c90b6539bbe51b5001083f045a846e85de705d23dd76e02cfa3` |
| 146 | `c934ceffc43062fb4d63bd3d7210c2f15661121d60b51453973fa5dfb0f31d40` | `3e3ec28e10a34d6afbc487f0f6a463a4fd5d5aa2e39e1c4aafdbf6619c04d0e6` | `40a60800f3e56adf425d3b14b6bf7d64cfaf6d5b758ae82a4aa34da6509f64b4` | `46607cc0eeafc4a7d9bb9f696594a005b0c5675ebeb9ce5db3ce0d69c079fe6d` |

The final dry-run must also hash every component `idempotency_key`, every
linked mutation intent `authority_fingerprint` and `request_fingerprint`, and
the normalized exact position-history row. Those values belong in the private
plan JSON, not repeated in operator output.

## 4. Repair semantics

The repair records only that the position is historically fully closed. It
must not assert that the original `partial_then_break_even` instruction
succeeded.

Use one transaction timestamp, `REPAIR_TS_UTC`, for every new timestamp below.
The dry-run prints its proposed value; the apply command must receive the exact
same value and plan fingerprint.

Canonical new values:

- management batch: `status=resolved`,
  `reason_code=historical_position_fully_closed`,
  `reconciled_at=completed_at=updated_at=REPAIR_TS_UTC`;
- management leg: `status=failed`, `updated_at=REPAIR_TS_UTC`; retain
  `last_error`, order identity, request, response, and snapshots exactly;
- nonterminal management component (`pending` or `recovery_required`):
  `status=safely_skipped`,
  `reason_code=historical_position_fully_closed`,
  `last_progress_at=completed_at=updated_at=REPAIR_TS_UTC`; retain desired and
  evidence JSON, attempts, and idempotency identity;
- already-terminal `operator_required` components 7 and 19 remain unchanged so
  the historical retry exhaustion is not erased;
- target execution leg: `status=closed`,
  `terminal_reason=historical_exchange_position_closed`,
  `last_verified_at=updated_at=REPAIR_TS_UTC`; retain `pos_id`, attribution,
  order identity, request, and response;
- a binding with no remaining nonterminal verified position leg becomes
  `status=closed`, `pos_id=NULL`,
  `last_exchange_status=historical_cleanup_terminal`,
  `recovered_at=updated_at=REPAIR_TS_UTC`;
- the corresponding lifecycle becomes `lifecycle_status=exited`,
  `exit_reason=exchange_closed`,
  `exited_at=updated_at=REPAIR_TS_UTC`; retain management fields and source
  links.

### Batch 144 live-sibling exception

Binding 307 also owns verified execution leg 531 at
`posId=1001124899621086`. Planning-time local state was:

- leg 530: `filled`, exact historical target `1001124898122909`;
- leg 531: `active`, exact verified sibling `1001124899621086`;
- binding 307: `active`, but its summary `pos_id` still pointed to the
  historical leg;
- lifecycle 910: `entered`.

Therefore binding 307 and lifecycle 910 are not stale terminal rows and must
not be closed. If a fresh complete snapshot proves leg 531 remains the unique
live verified sibling, update only the binding summary to
`status=active`, `pos_id=1001124899621086`,
`last_exchange_status=position_ownership_verified`, and set
`recovered_at=updated_at=REPAIR_TS_UTC`. Lifecycle 910 and leg 531 remain
unchanged.

If leg 531 is absent, non-unique, conflicted, no longer `active`, or external
evidence is incomplete, the complete six-batch operation must perform zero
writes and exit for a new plan. It must not silently switch batch 144 to the
five-batch closure rule.

## 5. Exact row-change matrix

### 5.1 `strategy_management_batches` — six rows

Only `status`, `reason_code`, `reconciled_at`, `completed_at`, and `updated_at`
change.

| PK | Old status / reason | New status / reason |
|---:|---|---|
| 123 | `recovery_required` / `management_reconciliation_identity_mismatch` | `resolved` / `historical_position_fully_closed` |
| 127 | `recovery_required` / `take_profit_cancel_retry_exhausted` | `resolved` / `historical_position_fully_closed` |
| 129 | `recovery_required` / `take_profit_order_identity_conflict` | `resolved` / `historical_position_fully_closed` |
| 133 | `recovery_required` / `management_reconciliation_identity_mismatch` | `resolved` / `historical_position_fully_closed` |
| 144 | `recovery_required` / `management_reconciliation_identity_mismatch` | `resolved` / `historical_position_fully_closed` |
| 146 | `recovery_required` / `take_profit_cancel_retry_exhausted` | `resolved` / `historical_position_fully_closed` |

All six currently have `reconciled_at=NULL` and `completed_at=NULL`; both become
`REPAIR_TS_UTC`.

### 5.2 `strategy_management_legs` — six rows

Only `status` and `updated_at` change. `last_error` remains unchanged, including
the three `management_close_order_not_found` rows.

| PK / batch | Old | New |
|---|---|---|
| 107 / 123 | `submitted` | `failed` |
| 110 / 127 | `planned` | `failed` |
| 112 / 129 | `planned` | `failed` |
| 117 / 133 | `submitted` | `failed` |
| 125 / 144 | `submitted` | `failed` |
| 127 / 146 | `planned` | `failed` |

### 5.3 `strategy_management_components` — sixteen changed, two unchanged

For changed rows, update `status`, `reason_code`, `last_progress_at`,
`completed_at`, and `updated_at` as specified above.

| Batch | Changed component PKs and old states | New state | Explicitly unchanged |
|---:|---|---|---|
| 123 | `4 recovery_required`, `5 pending`, `6 pending` | all `safely_skipped` | none |
| 127 | `8 pending`, `9 pending` | both `safely_skipped` | `7 operator_required` |
| 129 | `10 pending`, `11 pending`, `12 pending` | all `safely_skipped` | none |
| 133 | `13 recovery_required`, `14 pending`, `15 pending` | all `safely_skipped` | none |
| 144 | `16 recovery_required`, `17 pending`, `18 pending` | all `safely_skipped` | none |
| 146 | `20 pending`, `21 pending` | both `safely_skipped` | `19 operator_required` |

### 5.4 `execution_order_legs` — six target rows

Only `status`, `terminal_reason`, `last_verified_at`, and `updated_at` change.

| PK / binding | Old | New | Exact retained `pos_id` |
|---|---|---|---|
| 497 / 283 | `filled`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124765619311` |
| 496 / 282 | `filled`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124765261315` |
| 503 / 287 | `active`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124787260932` |
| 511 / 292 | `filled`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124837556751` |
| 530 / 307 | `filled`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124898122909` |
| 540 / 313 | `filled`, reason `NULL` | `closed`, `historical_exchange_position_closed` | `1001124908211764` |

Execution legs 498 and 512 are already cancelled and remain unchanged. Live
sibling leg 531 remains `active`, verified, and otherwise byte-for-byte
unchanged.

### 5.5 `execution_bindings` — six rows

| PK | Old status / `pos_id` / exchange status | New status / `pos_id` / exchange status |
|---:|---|---|
| 283 | `active` / `1001124765619311` / `position_attribution_evidence_unavailable` | `closed` / `NULL` / `historical_cleanup_terminal` |
| 282 | `active` / `1001124765261315` / `position_attribution_evidence_unavailable` | `closed` / `NULL` / `historical_cleanup_terminal` |
| 287 | `active` / `1001124787260932` / `position_attribution_evidence_unavailable` | `closed` / `NULL` / `historical_cleanup_terminal` |
| 292 | `active` / `1001124837556751` / `position_attribution_evidence_unavailable` | `closed` / `NULL` / `historical_cleanup_terminal` |
| 307 | `active` / `1001124898122909` / `position_attribution_evidence_unavailable` | `active` / `1001124899621086` / `position_ownership_verified` |
| 313 | `active` / `1001124908211764` / `position_attribution_evidence_unavailable` | `closed` / `NULL` / `historical_cleanup_terminal` |

Set `recovered_at=updated_at=REPAIR_TS_UTC` for all six.

### 5.6 `strategy_lifecycles` — five rows changed, one unchanged

Only `lifecycle_status`, `exit_reason`, `exited_at`, and `updated_at` change.

| PK | Old | New |
|---:|---|---|
| 819 | `entered`, reason/time `NULL` | `exited`, `exchange_closed`, `REPAIR_TS_UTC` |
| 816 | `entered`, reason/time `NULL` | `exited`, `exchange_closed`, `REPAIR_TS_UTC` |
| 834 | `entered`, reason/time `NULL` | `exited`, `exchange_closed`, `REPAIR_TS_UTC` |
| 859 | `entered`, reason/time `NULL` | `exited`, `exchange_closed`, `REPAIR_TS_UTC` |
| 921 | `entered`, reason/time `NULL` | `exited`, `exchange_closed`, `REPAIR_TS_UTC` |
| 910 | `entered`, reason/time `NULL` | unchanged because verified sibling leg 531 remains live |

All six have `trade_idea_id=NULL`, so no `trade_ideas` update is required.

### 5.7 Explicit no-change tables and columns

Do not update or insert rows in:

- `raw_messages`, `recognition_decisions`, or recognition payloads;
- `position_mutation_intents`;
- `position_protection_ledger`, `position_protection_incidents`, or TPSL
  ownership records;
- `execution_events`;
- strategy target/contract JSON or any strategy resolution table;
- exchange order/fill/history cache tables;
- trading settings.

Do not alter immutable identity columns on changed rows: batch/leg/component
foreign keys, strategy instance IDs, raw-message IDs, source message IDs,
`execution_binding_id`, `execution_order_leg_id`, order IDs, client order IDs,
`pos_id` on execution legs, attribution status/evidence, component desired or
evidence JSON, attempts, and all stored fingerprints.

## 6. Dry-run and exact fingerprint gate

The future tool must default to dry-run. Its production apply path must require
all of:

```text
--apply
--expected-plan-fingerprint <64 hex>
--expected-action-count 45
--expected-repair-ts-utc <exact ISO-8601 value>
--confirmation-token <derived one-time token>
```

Dry-run output must be canonical JSON and contain:

- `mode=dry_run` and `exchange_write_count=0`;
- exact code SHA and schema version;
- database path, read-only snapshot method, `quick_check`, exact target-set
  result, and fresh target/critical table counts;
- normalized per-batch local before values and immutable fingerprints;
- normalized per-batch exchange proof, including exact instrument, side,
  `posId`, `pos`, `closePos`, history update time, and absence from every
  current/open/pending source;
- batch 144 sibling proof for leg 531 and `posId=1001124899621086`;
- all 45 ordered compare-and-set actions and their exact before/after values;
- `database_fingerprint`, `exchange_fingerprint`, `action_fingerprint`, and a
  top-level `plan_fingerprint`, each SHA-256 of canonical JSON;
- the reverse compare-and-set action list and its hash;
- zero secret values and no raw credentials.

The exact target-set predicate is equality, not containment:

```text
current recovery_required batch IDs == [123,127,129,133,144,146]
```

The plan must refuse before opening a write transaction if any of these gates
fails:

- local, origin, or production SHA is not the separately approved exact SHA;
- the worktree or deployed checkout is dirty;
- service mode is not `global/queue`;
- active exchange-write count is nonzero;
- any `executing`, `reserved`, `submitted`, `submit_unknown`, or `reconciling`
  batch exists outside the six paused historical rows during the quiet gate;
- the target set, row topology, PK/FK identity, exact old values, immutable
  fingerprints, or expected action count differs;
- any target execution leg is no longer the unique verified owner of its
  exact `posId`;
- any linked mutation intent is unconfirmed;
- any target `posId` is live, open, pending TPSL, absent from exact history,
  non-unique in history, or has `pos != closePos`;
- any exchange call is incomplete, unstable, malformed, or returns an error;
- TPSL ownership has a conflict, unowned pending row, or nonzero exchange write
  count;
- batch 144 sibling leg 531 is not the unique verified current live leg with
  complete, conflict-free protection evidence;
- `PRAGMA quick_check` is not `ok`.

One reasoned retry is allowed for a transient incomplete external read. A
second incomplete response is `unknown` and forces zero writes. No retry loop
is allowed.

## 7. Backup, file permissions, and production-copy rehearsal

### Task 1: Implement only after tool/rehearsal authorization

**Files:**

- Create: a narrowly scoped supervised repair module and CLI entry point.
- Create: focused tests for dry-run, apply CAS, idempotency, rollback output,
  batch 144 sibling preservation, and every fail-closed gate.
- Do not modify production semantics or existing automatic workers.

The implementation must use a read-only exchange interface type. It must have
no import or callable path to create/cancel orders or mutate TPSL.

### Task 2: Create a private online backup for rehearsal

Use `umask 077` and a new explicit evidence directory beneath
`/opt/telegram-kol-analyzer/data/evidence/`. Require directory mode `0700` and
every plan, log, SQL, and database-copy file mode `0600`. Record, but do not
change, the live database metadata; planning-time metadata was
`0666 root:root`.

Create the rehearsal copy with SQLite's online backup API, not `cp` of a live
WAL database. Record source/backup sizes and backup SHA-256. Run
`PRAGMA quick_check` on both source and copy.

### Task 3: Rehearse on the production database copy

Run the exact dry-run and exact 45-action apply code against the private copy
using captured complete exchange evidence. Verify:

- all 45 compare-and-set actions apply once;
- the same apply is idempotently reported as already applied, with zero second
  writes;
- one changed old value, one changed immutable fingerprint, an extra target,
  a missing target, an incomplete exchange snapshot, a live target position,
  and a missing batch 144 sibling each cause a full rollback and zero changes;
- reverse SQL restores the copy to its exact pre-apply target-row fingerprint;
- `PRAGMA quick_check=ok` before apply, after apply, and after reverse rollback;
- only the six specified tables and 45 rows differ after apply;
- no exchange-write mock/counter is invoked.

Run focused tests only. This is a data repair tool, not a production-code
behavior change; do not run unrelated full suites unless implementation
touches shared runtime code.

After rehearsal, present the exact tool commit SHA, plan JSON path and hash,
backup path and hash, rollback SQL path and hash, test result, and expected
production action count. Stop for the second authorization.

## 8. Production transaction and rollback

### Pre-transaction sequence

After exact production authorization:

1. create a fresh private evidence directory;
2. run the fresh read-only dry-run while the service is active;
3. run the existing active-write check and require `active_write_count=0`;
4. prove a quiet management window and complete exchange evidence;
5. stop `telegram-kol.service`; verify it is inactive and no worker process
   remains;
6. rerun the active-write/target-row gates read-only;
7. create a quiescent SQLite backup with the backup API, mode `0600`, and
   record SHA-256 plus original DB mode/owner;
8. run `PRAGMA quick_check` on live DB and backup;
9. refresh the complete read-only Deepcoin snapshot once more and rebuild the
   plan; require its fingerprint, action count, and repair timestamp to match
   the approved inputs exactly.

Any mismatch starts no transaction and leaves the service stopped for operator
review; it does not substitute values or shrink the batch set.

### Transaction boundary

Use one connection and one `BEGIN IMMEDIATE` transaction. Before the first
update, reread and fingerprint every target row. Execute explicit PK-based CAS
updates with all planned old values in each `WHERE` clause. Require row count
one for every action.

Recommended dependency order:

1. sixteen management components;
2. six management legs;
3. six management batches;
4. six target execution legs;
5. six execution bindings;
6. five strategy lifecycles.

Before `COMMIT`, query all postconditions, exact unchanged sibling rows, target
set, and table-count deltas inside the same transaction. Any discrepancy calls
`ROLLBACK`; partial commit is forbidden.

### Rollback SQL

The dry-run must generate reverse CAS SQL containing every exact old value,
including old timestamps and `NULL`s. It may run only while the service remains
stopped and only if every row still equals the planned new value and
`REPAIR_TS_UTC`. Run all reverse actions in one `BEGIN IMMEDIATE` transaction,
verify their row counts and the original target-row fingerprint, then commit.

### Backup-file restore

If SQLite integrity fails or reverse CAS cannot run before service restart:

1. keep the service stopped;
2. preserve the failed database as a private evidence file rather than
   deleting it;
3. restore the verified quiescent backup to the exact production path;
4. restore the recorded original owner/group and mode;
5. run `PRAGMA quick_check` and compare critical table counts and target-row
   fingerprint;
6. restart only after the restored database passes every gate.

Never restore the whole backup after the service has resumed and accepted new
writes; that could discard later business data. A post-restart reversal would
require a new targeted L3 plan.

## 9. Minimum sufficient L3 verification

No schema/bootstrap/migration file changes are planned. No full-database hash,
unrelated full test suite, deployment, synthetic Telegram message, deliberate
exchange order, or long soak is required.

### Before/after counts

While the service is stopped, require total row-count equality before/after for:

- all six changed tables;
- `position_mutation_intents`;
- `execution_events`;
- `raw_messages`;
- `recognition_decisions`;
- protection ledger/incident tables.

Expected target-row state deltas are:

- management batches: `recovery_required -6`, `resolved +6`;
- management legs: `planned -3`, `submitted -3`, `failed +6`;
- management components: `pending -13`, `recovery_required -3`,
  `safely_skipped +16`, `operator_required` unchanged;
- target execution legs: `filled -5`, `active -1`, `closed +6`;
- bindings: `active -5`, `closed +5`; binding 307 remains active;
- lifecycles: `entered -5`, `exited +5`; lifecycle 910 remains entered.

Run `PRAGMA quick_check=ok` on the live database immediately after commit and
before service restart.

### Read-only semantic verification

After commit, run the complete management audit and require no actionable row
from batches 123, 127, 129, 133, 144, or 146. Confirm the exact six batch
statuses, all management-leg statuses, component terminality, target execution
leg terminality, five closed bindings/lifecycles, and the batch 144 live-sibling
exception.

Perform a fresh read-only Deepcoin review:

- all six historical `posId` values remain absent from live positions, open
  orders, and pending TPSL;
- exact position-history proof still has `closePos == pos`;
- batch 144 sibling leg 531 still maps uniquely to its current live `posId`
  and its pending TPSL ownership is complete and conflict-free;
- TPSL conflicts and unowned pending rows remain zero;
- `exchange_write_count=0`.

An incomplete post-read is unknown: retry once for a reasoned transient error,
then fail closed and do not claim verification success.

Restart the existing service without deploying code. Verify the deployed SHA
is unchanged, service is active, settings remain `global/queue`, the endpoint
is healthy, and no immediate SQLite or worker error appears. No 30-minute soak
is required because this operation changes historical rows only and introduces
no runtime or exchange-write semantics.

## 10. Exact next authorization required

The next safe operation is not production repair. It is:

> Implement the narrowly scoped dry-run/apply tool and focused tests described
> in this document, create a private SQLite online backup/copy, and rehearse the
> exact 45-row plan on that copy. Do not modify the production database, do not
> invoke production `--apply`, do not replay messages, do not deploy, and do not
> perform exchange writes. Return the exact tool SHA, dry-run plan fingerprint,
> backup and rollback hashes, and rehearsal result for a second authorization.

Only after that result is reviewed should a new task explicitly authorize the
exact production service stop, exact plan fingerprint, 45-action apply,
read-only verification, and restart.

## 11. Authorized implementation/rehearsal record (2026-08-21)

The first authorization checkpoint in section 1 was granted. The supervised
module and focused tests were implemented locally. The module defaults to
dry-run, has no exchange-client import, serializes all 45 ordered actions with
full-row before/after values, and exposes apply/rollback only behind exact
fingerprint, count, timestamp, and confirmation-token gates.

A private production online backup was created while the service remained
active. No production database content was changed:

- evidence directory:
  `/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-rehearsal-20260821T180621Z/`;
- directory mode/owner: `0700 root:root`; file mode/owner: `0600 root:root`;
- source database: `562814976` bytes, mode `0666 root:root`,
  `PRAGMA quick_check=ok`;
- backup database: `562819072` bytes, `PRAGMA quick_check=ok`;
- backup SHA-256:
  `c7322019ebf44afca43cea7f59d518ee37739ee9f133cca34363e662c972b246`;
- backup method: SQLite online backup API; production database writes: `0`.

The rehearsal then failed closed before plan generation. A fresh read-only
batch-144 sibling proof was attempted. The first attempt received the exchange
reads but failed in a local read-only SQL statement because shell quoting
removed a string literal. The single reasoned retry used bound SQL parameters,
but the resulting combined local/exchange/protection predicate was still
incomplete. In accordance with section 6, it was treated as unknown and no
further exchange retry was made.

Consequences of the failed gate:

- `batch-144-live-sibling.json` was not created;
- no dry-run plan or plan fingerprint was issued;
- neither apply nor rollback was invoked on the database copy;
- production DB writes and exchange writes remained zero;
- the production service remained active at deployed SHA
  `fdaff6b12d0aa4470e9bfcc63239c8541c01c5ff`.

This is not authorization to weaken or bypass the sibling gate. The next
operation requires a new explicit authorization to collect one new complete
read-only sibling snapshot with diagnostic output persisted even when a gate
fails, then resume the copy-only rehearsal. Production apply remains
unauthorized.

## 12. Re-authorized sibling diagnostic result (2026-08-21)

The single new read-only snapshot authorized after section 11 was collected.
The diagnostic was persisted regardless of outcome at:

`/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-rehearsal-20260821T180621Z/batch-144-live-sibling.json`

Its mode is `0600 root:root` and its SHA-256 is
`80504336d0271f8dc6fed8a841b81f642c447351efef61cada477897623e801a`.
It records only the read methods `list_positions` and
`list_trigger_orders_pending`, with `exchange_write_count=0`.

The gate failed with complete, affirmative evidence rather than an API error:

- the local sibling leg 531 remains the unique verified local owner of
  `posId=1001124899621086` and remains `active` under binding 307;
- the exact sibling `posId` has zero matches in the current live-position
  snapshot;
- neither current live position is the sibling; both are unrelated BTC longs;
- the account has eight pending TPSL rows, all eight owned, with zero ownership
  conflicts and zero unowned rows;
- the sibling has zero current owned pending-protection rows;
- the raw pending-trigger response had twelve rows, of which the ownership
  audit classified eight as TPSL.

Therefore the batch-144 live-sibling exception in sections 4 and 5 is no
longer proven current. The 45-action plan is invalid and must not be generated
or rehearsed. No dry-run plan, plan fingerprint, or rollback SQL was produced;
the production copy was not mutated.

After the failed gate, the production database still returned
`PRAGMA quick_check=ok`, its recovery-required set remained exactly
`[123,127,129,133,144,146]`, the backup SHA-256 remained
`c7322019ebf44afca43cea7f59d518ee37739ee9f133cca34363e662c972b246`,
and `telegram-kol.service` remained active at deployed SHA
`fdaff6b12d0aa4470e9bfcc63239c8541c01c5ff`.

The next safe step is a newly authorized read-only redesign: obtain exact
position-history and current-order proof for sibling
`1001124899621086`, determine whether binding 307 and lifecycle 910 may now be
terminalized, and produce a new exact action count and fingerprints. The
existing 45-action authorization cannot be reused. Production apply remains
unauthorized.
