# Historical Management Terminalization L3 V2 Implementation Plan

> **Status (2026-08-21):** The separately authorized V2 implementation and
> production-copy rehearsal are complete. Sections 1, 6, and 10 retain the
> authorization boundary that existed before that checkpoint; section 11 is
> the authoritative execution record and current production boundary.

> **For Codex:** Execute this plan only after a new explicit implementation and
> production-copy rehearsal authorization. Do not use the superseded 45-action
> plan or its tool unchanged. Do not use subagents for this operation.

**Goal:** Terminalize the same six historical management batches and every
stale local projection now that binding 307's second verified position is also
proven historically fully closed.

**Architecture:** Replace the superseded live-sibling exception with an exact
terminal sibling proof. Generate a deterministic 47-action compare-and-set
plan from a fresh stable production snapshot, rehearse it on a fresh SQLite
online backup, and require a separate exact-fingerprint authorization before
any production mutation.

**Tech Stack:** Python 3.13, SQLite, canonical JSON SHA-256 fingerprints,
focused pytest coverage, existing read-only Deepcoin REST methods.

---

## 1. Authorization and supersession

This document is a read-only redesign. It authorizes no implementation, no
database-copy apply, and no production apply.

Forbidden until separately approved:

- modifying the production database or any database copy;
- invoking `--apply` or `--rollback`;
- replaying Telegram messages;
- creating, cancelling, replacing, or otherwise mutating exchange orders;
- modifying TPSL;
- deploying or pushing;
- changing recognition, strategy resolution, attribution, or execution
  semantics;
- cleaning or changing the existing dirty production checkout.

This plan supersedes the 45-action matrix in
`docs/plans/2026-08-21-historical-management-terminalization-l3.md`.
The existing implementation commit
`07064f5623ed4fd75f8f0808b6c133ad9839ff65` hard-codes the old live-sibling
exception and must not be used for production or copy apply without an
authorized V2 update.

## 2. Root-cause and exact sibling terminality evidence

### 2.1 Local identity chain

Binding 307 has exactly two execution legs:

| Leg | Purpose | Stored status | Attribution | Exact `posId` | Stored order ID |
|---:|---|---|---|---|---|
| 530 | entry | `filled` | `verified` | `1001124898122909` | `1001124898122909` |
| 531 | entry | `active` | `verified` | `1001124899621086` | `1001124898123056` |

Both legs belong to binding 307 and strategy instance
`deepcoin:-1003048800035:4331:BTC:short`. Lifecycle 910 is still `entered`.
There is no third leg under the binding.

The sibling has one confirmed mutation intent for owned backup stop
`1001124899626507`. Its two verified protection-ledger rows are primary stop
`1001124899621085` and backup stop `1001124899626507`. Every mutation intent
under binding 307 is `confirmed`; none is reserved, submitted, unknown, or
reconciling.

### 2.2 Exact Deepcoin closure chain

Private raw evidence:

`/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-rehearsal-20260821T180621Z/batch-144-sibling-terminality-20260821T183931Z.json`

SHA-256:
`2b0d981ae2fa6564402463b264c6e36d08f57d1dfff14836ac9b2fdb17907698`

The snapshot is complete, read-only, mode `0600 root:root`, and records
`exchange_write_count=0`. Exact sibling observations:

- live positions matching `posId=1001124899621086`: zero;
- regular open orders matching the exact position or proven local order IDs:
  zero;
- pending trigger/TPSL rows matching the exact position or proven local order
  IDs: zero;
- exact position-history rows: one;
- position history: `BTC-USDT-SWAP`, `short`, `pos=8`, `closePos=8`,
  `avgPx=72410.9`, `closeAvgPx=73200`, `uTime=1787268377000`;
- owned primary stop `1001124899621085` has trigger price `73200`, nonzero
  `triggerTime=1787268377`, and `uTime=1787268377000`;
- the primary-stop update time equals the exact position close time and its
  trigger price equals `closeAvgPx`;
- owned backup stop `1001124899626507` has `triggerTime=0` and is absent from
  current pending rows after the position closed.

This proves the sibling position closed through its owned stop. It does not
prove that management message #4332 executed its requested partial-close and
break-even sequence.

Derived classification evidence:

`/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-rehearsal-20260821T180621Z/batch-144-sibling-terminality-derived.json`

SHA-256:
`e8e9acbe43fc20fd4189905ba74ba84a089d6023eab032056d4ad2a448a8493b`

Classification: `historical_terminal/informational`.

### 2.3 Why the first derived predicate failed

The raw collector initially reported one false check because it treated every
non-531 nonterminal verified leg as plan-external. That set contained leg 530,
which was already an explicit terminal target in the original six-batch plan.
The correct set predicate is:

```text
binding 307 execution legs - planned terminal leg IDs {530,531} == empty
```

The persisted derived evidence applies that predicate. No external query was
retried or weakened to reach the corrected classification.

## 3. New exact action matrix: 47 rows

| Table | Exact PKs | Action count |
|---|---|---:|
| `strategy_management_components` | `4,5,6,8,9,10,11,12,13,14,15,16,17,18,20,21` | 16 |
| `strategy_management_legs` | `107,110,112,117,125,127` | 6 |
| `strategy_management_batches` | `123,127,129,133,144,146` | 6 |
| `execution_order_legs` | `496,497,503,511,530,531,540` | 7 |
| `execution_bindings` | `282,283,287,292,307,313` | 6 |
| `strategy_lifecycles` | `816,819,834,859,910,921` | 6 |
| **Total** |  | **47** |

Relative to the superseded 45-action plan:

- add execution-leg action for PK 531;
- add lifecycle action for PK 910;
- retain the existing binding-307 action slot but change its proposed new
  value from active/sibling-summary to closed/NULL.

## 4. Exact per-table changes

Use one new `REPAIR_TS_UTC` generated by the future dry-run. Every current
before value, including mutable timestamps, must come from the future fresh
stable snapshot and appear in the CAS predicate. Do not reuse timestamps from
the 2026-08-21 evidence: production reconciliation has already updated at
least execution leg 530 after the old backup was created.

### 4.1 Management components: 16 actions

PKs `4,5,6,8,9,10,11,12,13,14,15,16,17,18,20,21`:

- old status: the exact current `pending` or `recovery_required` value recorded
  by the fresh dry-run;
- new status: `safely_skipped`;
- new reason: `historical_position_fully_closed`;
- `last_progress_at=completed_at=updated_at=REPAIR_TS_UTC`.

Components 7 and 19 remain unchanged as `operator_required`.

### 4.2 Management legs: 6 actions

| PK | Batch | Required old status | New status |
|---:|---:|---|---|
| 107 | 123 | `submitted` | `failed` |
| 110 | 127 | `planned` | `failed` |
| 112 | 129 | `planned` | `failed` |
| 117 | 133 | `submitted` | `failed` |
| 125 | 144 | `submitted` | `failed` |
| 127 | 146 | `planned` | `failed` |

Only `status` and `updated_at` change. Preserve `last_error`, requests,
responses, snapshots, order IDs, and position IDs.

### 4.3 Management batches: 6 actions

PKs `123,127,129,133,144,146` must still be exactly the complete
`recovery_required` set. Set:

- `status=resolved`;
- `reason_code=historical_position_fully_closed`;
- `reconciled_at=completed_at=updated_at=REPAIR_TS_UTC`.

Preserve every identity, contract, target, source, and idempotency field.

### 4.4 Execution legs: 7 actions

| PK | Binding | Exact retained `posId` | Required old status | New status |
|---:|---:|---|---|---|
| 496 | 282 | `1001124765261315` | `filled` | `closed` |
| 497 | 283 | `1001124765619311` | `filled` | `closed` |
| 503 | 287 | `1001124787260932` | `active` | `closed` |
| 511 | 292 | `1001124837556751` | `filled` | `closed` |
| 530 | 307 | `1001124898122909` | `filled` | `closed` |
| 531 | 307 | `1001124899621086` | `active` | `closed` |
| 540 | 313 | `1001124908211764` | `filled` | `closed` |

For all seven set:

- `terminal_reason=historical_exchange_position_closed`;
- `last_verified_at=updated_at=REPAIR_TS_UTC`.

Preserve `pos_id`, attribution, order identity, request/response payloads, and
strategy links. Legs 498 and 512 remain unchanged and cancelled.

### 4.5 Execution bindings: 6 actions

Bindings `282,283,287,292,307,313` all become:

- `status=closed`;
- `pos_id=NULL`;
- `last_exchange_status=historical_cleanup_terminal`;
- `recovered_at=updated_at=REPAIR_TS_UTC`.

Binding 307 is no longer an exception. Its exact before state must still be
`active`; its summary `pos_id` may remain the stale leg-530 value in the fresh
snapshot, but the future plan must use whatever exact byte value it reads and
must not substitute it silently.

### 4.6 Strategy lifecycles: 6 actions

Lifecycles `816,819,834,859,910,921` all become:

- `lifecycle_status=exited`;
- `exit_reason=exchange_closed`;
- `exited_at=updated_at=REPAIR_TS_UTC`.

Lifecycle 910 is no longer an exception. Preserve management fields,
`trade_idea_id`, source links, and all unrelated timestamps.

## 5. Explicit no-change scope

Do not insert, update, or delete rows in:

- `position_mutation_intents`;
- `position_protection_ledger`;
- `position_protection_incidents`;
- `execution_events`;
- `raw_messages`;
- `recognition_decisions`;
- exchange caches or history tables;
- strategy parsing, recognition, target, or contract tables;
- trading settings.

The verified protection-ledger rows for closed positions remain historical
evidence. Their absence from current pending exchange rows does not authorize
rewriting them in this repair.

## 6. Required V2 tool changes — separately authorized task

**Files:**

- Modify:
  `src/telegram_kol_research/historical_management_terminalization.py`
- Modify:
  `tests/test_historical_management_terminalization.py`
- Do not modify runtime workers or shared trading execution modules.

### Task 1: Write failing V2 matrix tests

Add focused tests requiring:

- exact action count 47;
- execution leg 531 closes with retained exact `pos_id`;
- binding 307 closes with `pos_id=NULL`;
- lifecycle 910 exits;
- only legs 530 and 531 exhaust binding 307's complete leg set;
- missing/non-unique/full-close-mismatched sibling history causes zero writes;
- any live/open/pending sibling match causes zero writes;
- a third binding-307 leg causes zero writes;
- changed mutable before values cause CAS refusal.

Run the new tests first and require an expected RED result against the current
45-action implementation.

### Task 2: Implement the minimum V2 planner change

Replace the live-sibling evidence contract with the exact terminal evidence
contract and immutable source hashes from section 2. Update only:

- expected action matrix from 45 to 47;
- sibling evidence validation;
- execution-leg action generation to include 531;
- binding-307 new state to the normal closed-binding rule;
- lifecycle generation to include 910;
- CLI `--expected-action-count` gate and help text.

Do not add an exchange client import to the tool.

### Task 3: Run focused validation

Run:

```text
pytest -q tests/test_historical_management_terminalization.py \
  tests/test_management_history_recovery.py
```

Also run `git diff --check`, module compilation, and CLI help parsing. A full
unrelated suite is not required unless shared runtime code is touched.

Commit only the explicit V2 tool, test, and plan paths. Do not push.

## 7. Fresh dry-run and rehearsal gates

The old online backup SHA
`c7322019ebf44afca43cea7f59d518ee37739ee9f133cca34363e662c972b246`
predates observed runtime changes and is evidence only. It must not be used for
the V2 rehearsal.

After separate authorization:

1. require the approved V2 tool commit and clean local worktree;
2. verify production remains at the expected deployed SHA, active, and
   `global/queue`;
3. require the recovery-required set to equal exactly
   `[123,127,129,133,144,146]`;
4. refresh complete read-only evidence for all seven exact positions;
5. require both binding-307 positions to have unique full-close history and no
   live/open/pending match;
6. require no unconfirmed mutation intent and no third binding-307 leg;
7. create a new private SQLite online backup, not `cp` of a live WAL database;
8. require `0700/0600` permissions and `PRAGMA quick_check=ok` on source/copy;
9. generate a new canonical 47-action dry-run plan and reverse SQL;
10. require exact plan, action, rollback, database, and exchange fingerprints;
11. rehearse apply once on the new copy, require 47 writes;
12. rehearse the same apply again, require `already_applied` and zero writes;
13. execute reverse CAS, require 47 writes and the exact original target-row
    fingerprint;
14. require unchanged table counts and `quick_check=ok` at every boundary.

No production checkout cleanup, service stop, production apply, deployment,
or exchange write belongs to this authorization checkpoint.

## 8. V2 expected state deltas

- management batches: `recovery_required -6`, `resolved +6`;
- management legs: `planned -3`, `submitted -3`, `failed +6`;
- management components: `pending -13`, `recovery_required -3`,
  `safely_skipped +16`, `operator_required` unchanged;
- target execution legs: `filled -5`, `active -2`, `closed +7`;
- bindings: `active -6`, `closed +6`;
- lifecycles: `entered -6`, `exited +6`;
- every affected and critical table total row count: unchanged;
- exchange writes: zero.

## 9. Rollback and production boundary

The V2 rollback reverses all 47 actions in reverse dependency order with exact
new-value CAS predicates. It may be rehearsed only on the new copy during the
next authorized checkpoint.

Production apply still requires a later, separate authorization naming:

- exact V2 tool commit SHA;
- exact 47-action plan fingerprint;
- exact repair timestamp;
- confirmation token;
- new backup path and SHA;
- reverse SQL path and SHA;
- exact service stop/apply/verify/start operation.

Any new state drift invalidates those values and performs zero writes.

## 10. Exact next authorization required

The next safe authorization is:

> Implement the V2 47-action planner/test changes in the isolated local
> worktree, create a fresh private SQLite online backup, and rehearse dry-run,
> 47-action apply, idempotent reapply, and reverse rollback on that fresh copy.
> Do not modify the production database, do not deploy or push, do not replay
> messages, and do not perform exchange writes. Return the exact tool commit,
> plan/rollback fingerprints and files, backup hash, and rehearsal result.

Do not authorize production apply until that checkpoint has completed and the
exact artifacts have been reviewed.

## 11. Authorized V2 implementation and rehearsal record

The user subsequently authorized the section-10 checkpoint, but did not
authorize a production database mutation, deployment, push, replay, or exchange
write. The completed local tool commit is:

`2f1affd01c0e50e728bb169e2cb3019a6790ed9a`

The focused L3 verification passed with 50 tests:

```text
pytest -q tests/test_historical_management_terminalization.py \
  tests/test_management_history_recovery.py
```

`git diff --check`, module compilation, and CLI help parsing also passed. No
shared runtime worker or exchange-write implementation was changed.

### 11.1 Fresh read-only evidence and private backup

Evidence directory, mode `0700` with files mode `0600`:

`/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-v2-rehearsal-20260821T192857Z/`

Key immutable artifacts:

| Artifact | SHA-256 |
|---|---|
| `fresh-seven-exchange-evidence.json` | `aa4a218a9c90f58b8f34b5fb8138e323cfe32078601ca31a30920901b5307aa8` |
| `fresh-seven-exchange-diagnostic.json` | `cd437133497354adc109edb838909942057c48fe24f3b59e235a0bd948c7b085` |
| `research-online-backup.db` | `10b20bb1dc83d6d50afd72646a9c71698b61cdb1a4c165b0eafc2b6825ca18cb` |
| `backup-metadata.json` | `e92789ff42fa72f96dc02e15f9f8ef094eb6a04922d2dc76fc9bb5a0c59afac3` |
| `terminalization-plan.json` | `456ecb2a38e6914c17006e5b6c06f821bf2be18d90dad4272af90a359196e2d2` |
| `rollback.sql` | `0b9e52110a592b9365d6b843339309c9c6953c12ff006f1a968af492cf80f86d` |
| `rehearsal-summary.json` | `87ee3ee8df54d41d28ed84e4e951d73dc2909d06b959c0b8d3e3717841cf6e2f` |

All seven exact positions had one unique full-close history row with
`closePos == pos` and zero exact matches in live positions, regular open
orders, and pending TPSL. The account TPSL audit observed eight pending TPSL,
all owned, with zero conflicts, zero unowned rows, and zero exchange writes.

The production source passed `PRAGMA quick_check`. The pristine backup and an
independent rehearsal copy were created with `sqlite3.Connection.backup`; all
critical table counts matched. The pre-existing production database mode was
recorded as `0666` but was not changed. Both newly created database artifacts
are mode `0600`.

### 11.2 Exact dry-run and copy-only result

The dry-run produced this exact 47-action matrix:

| Table | Actions |
|---|---:|
| `strategy_management_components` | 16 |
| `strategy_management_legs` | 6 |
| `strategy_management_batches` | 6 |
| `execution_order_legs` | 7 |
| `execution_bindings` | 6 |
| `strategy_lifecycles` | 6 |
| **Total** | **47** |

Exact rehearsal values:

- repair timestamp: `2026-08-21T19:33:03+00:00`;
- plan fingerprint:
  `347f0f54a4b60e9ce36e1e4b67b99e2c6672a8bf75c5702b1ed1d9aa172ba54d`;
- action fingerprint:
  `3b8fcc18318eeed732bcf78236a745ef63d549b5ba44b0cbb37f75cb5ee71494`;
- rollback fingerprint:
  `29504e76fc7d154b05f04864ce4afd4bdbf287f3e82e7f9eaef3c9f9f3fa0501`;
- database fingerprint:
  `606b4dffe099b5305f55917c683b8dc3f38a5354c76571648725bdfa9c04c944`.

The first copy apply attempt intentionally remained zero-write because the
operator supplied the original `Z` timestamp spelling while the plan stored
the canonical `+00:00` spelling. The exact gate returned
`repair_timestamp_mismatch`, `writes=0`. Using the value serialized in the
plan then proved:

- apply: `47` changed rows, `quick_check=ok`;
- identical reapply: `already_applied`, `0` changed rows;
- applied-state check: exact `47/47` after rows;
- rollback: `47` changed rows, `quick_check=ok`;
- rollback-state check: exact `47/47` before rows;
- pristine backup SHA unchanged and every affected/critical table count
  unchanged.

### 11.3 Current fail-closed production boundary

The final production read-only check found the same exact six-batch recovery
set and `47/47` semantic before-state matches. The active runtime had,
however, refreshed `updated_at` on execution legs `496,497,511,530,540` after
the online backup. Exact before-state matches were therefore only `42/47`.
The rehearsed plan is now evidence only and **must not** be used for production
apply. Its exact CAS gate correctly fails closed.

At the final checkpoint the service was active, modes were `global/queue`, all
three databases passed `PRAGMA quick_check`, and the operation counts remained:

- production database writes: zero;
- exchange writes: zero;
- deployments: zero;
- pushes: zero.

The next safe authorization is deliberately two-stage:

1. authorize a controlled production service stop plus a new read-only
   seven-position snapshot, new private online backup, and new stable dry-run;
   still do not apply;
2. review the newly generated exact plan/rollback fingerprints, repair
   timestamp, confirmation token, backup SHA, and paths, then separately
   authorize the exact 47-row production apply, verification, read-only
   Deepcoin recheck, and service start.

Any service state, target set, external state, row value, or artifact mismatch
must perform zero writes and leave the service stopped for operator review.

## 12. Stable production preparation checkpoint

On 2026-08-21 the user authorized only the first stage described in section
11.3: controlled service stop, fresh read-only exchange evidence, a new private
online backup, and a stable production-targeted dry-run. Production apply,
rollback, service start, deployment, push, replay, and exchange writes remained
unauthorized and were not performed.

The service was stopped from `active` to `inactive/dead` after the production
SHA, `global/queue` modes, exact six-batch recovery set, binding-307 leg set,
and mutation-intent gates passed. The service remains stopped pending review.

Private evidence directory, mode `0700` with files mode `0600`:

`/opt/telegram-kol-analyzer/data/evidence/historical-management-terminalization-v2-production-prep-20260821T194505Z/`

Stable artifact values:

| Artifact or gate | Exact value |
|---|---|
| Fresh exchange evidence SHA-256 | `ad61027cc248698e5c024bc278752dd9c82dcdf2371f40016020d61443874897` |
| Fresh diagnostic SHA-256 | `39a504737f03f8e39f9e403e881942057cd4a20e52057b0df00405410448e465` |
| Online backup SHA-256 | `7eac021570988ba96b7d823e3cc3b462e0ca38a26482988d8827c036f094016f` |
| Production plan SHA-256 | `bfa20d27b1aa7f50a61771bd0be968b4e6c6f89753d327747284166eb1d659bb` |
| Rollback SQL SHA-256 | `7777b8c81cee404d1f2ab14f0c8528f2e4da831f489db983a4df3ae2f6ce1959` |
| Plan fingerprint | `a6cae23f7c349b49638dc33e969d17e52eb48e1de266064dcf0d4b67485567dc` |
| Action fingerprint | `c2eb97e543e78b33e7dadc0b58ee8062b98ad60c2b664033f175de3a3fc5e9d9` |
| Rollback fingerprint | `9be189f86d0617dbae573934ba0dadbc0d55e6690c648c6a0ebe02b053eed575` |
| Database fingerprint | `2de264e1544ffb3e7df558cbde3cbc389f16db23beddc2ed26f627b03f994e42` |
| Exchange fingerprint | `8f66af80e22bbea5bf26066f143e2a666bd9607e5bf09427fa149b9bd0ebb91d` |
| Repair timestamp | `2026-08-21T19:48:56+00:00` |

All seven exact positions again had one unique full-close history row and zero
live/open/pending matches. TPSL conflicts, unowned pending rows, and exchange
writes remained zero. Production and backup both matched all 47 plan before
rows exactly, all affected and critical table counts matched, and both passed
`PRAGMA quick_check`.

The confirmation token is intentionally retained only in the private plan and
operator handoff, not this repository document. The next operation requires a
new explicit authorization naming the exact plan fingerprint, repair timestamp,
confirmation token, backup and rollback artifacts, production apply, post-apply
verification, read-only Deepcoin recheck, and service start. Any mismatch must
produce zero writes and leave the service stopped.
