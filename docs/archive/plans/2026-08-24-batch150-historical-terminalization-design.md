# Batch 150 Historical Terminalization Design

## Scope and authorization

Build and rehearse an exact, batch-specific L3 repair for management batch
`150`. The authorized checkpoint ends after a fresh read-only exchange capture,
an online production database backup, and apply/idempotency/rollback rehearsal
on a separate copy.

This checkpoint does not authorize production database apply or rollback,
service stop or restart, deployment, push, Telegram replay, trading-setting
changes, or any exchange write.

## Proven identity and terminality

Binding `320` and lifecycle `952` belong to strategy instance
`deepcoin:-1003048800035:4384:BTC:short` and contain exactly two verified entry
legs:

| Leg | Stored state | Exact `posId` | Entry identity |
|---:|---|---|---|
| `553` | `filled` | `1001124956792734` | market order `1001124956792734` |
| `554` | `active` | `1001124961572300` | parent trigger `1001124956792983` |

The parent trigger has a unique filled child regular order
`1001124961572300`; that child is also the exact sibling `posId`. Both exact
position-history calls returned one row with `pos=closePos=11`. The target
closed at average `77736.5`; the sibling closed at average `78603.9`. Both
position rows share close time `1787574676000`. The owned target stop
`1001124956792870` and owned sibling stop `1001124961572299` have the same
non-zero trigger time and update time. Current BTC live positions and regular
open orders are empty, and none of either leg's owned protection orders is
pending.

Every position mutation intent under binding `320` is `confirmed`; there are
no bound close reservations. These facts prove historical position closure.
They do not retroactively prove that batch `150` executed its requested
partial-close and break-even sequence.

## Selected architecture

Create an independent, standard-library-only module
`batch150_management_terminalization.py`. Do not modify the sealed six-batch
historical terminalization utility. The new module owns one exact target set,
one eight-action matrix, canonical JSON fingerprints, compare-and-set apply,
idempotent reapply, reverse compare-and-set rollback, and a small argparse CLI.

The planner opens its database with SQLite URI `mode=ro` and
`PRAGMA query_only=ON`. It validates the current database identity and the
normalized read-only exchange evidence before constructing any action. Apply
and rollback open a write transaction only after every supplied fingerprint,
timestamp, token, table count, target-set, and row-state gate passes.

## Exact eight-row action matrix

One repair timestamp is used for all new timestamps.

| Table | PK | Required before | Exact after |
|---|---:|---|---|
| `strategy_management_components` | `23` | `pending` | `safely_skipped`, reason `historical_position_fully_closed`, progress/completion/update timestamp |
| `strategy_management_components` | `24` | `pending` | same as component `23` |
| `strategy_management_legs` | `133` | `planned` | `failed`, update timestamp |
| `strategy_management_batches` | `150` | `recovery_required/take_profit_cancel_retry_exhausted` | `resolved/historical_position_fully_closed`, reconciled/completed/update timestamp |
| `execution_order_legs` | `553` | verified `filled` target leg | `closed/historical_exchange_position_closed`, verified/update timestamp |
| `execution_order_legs` | `554` | verified `active` sibling leg | same terminal state while retaining exact `pos_id` |
| `execution_bindings` | `320` | `active` | `closed`, `pos_id=NULL`, `historical_cleanup_terminal`, recovered/update timestamp |
| `strategy_lifecycles` | `952` | `entered` | `exited/exchange_closed`, exited/update timestamp |

Component `22` remains `operator_required`. Mutation intents, protection
ledger, position take-profit audit rows, execution events, raw messages,
recognition decisions, settings, and every other table remain unchanged.

## Fail-closed database gates

The planner requires all of the following:

- `PRAGMA quick_check=ok` and query-only mode;
- the complete nonterminal management-batch set is exactly `[150]`;
- no batch is in `executing`, `reserved`, `submitted`, `submit_unknown`, or
  `reconciling`;
- exact batch fingerprints and source IDs match the frozen constants;
- batch `150` has exactly management leg `133` and components `22,23,24` with
  the expected sequence, kinds, statuses, and attempt counts;
- binding `320` has exactly execution legs `553,554`, both with exact
  `strategy_instance_id`, order identities, verified attribution, and `posId`;
- lifecycle `952` remains `entered` and linked to binding `320`;
- every mutation intent under binding `320` is `confirmed`;
- no bound close reservation exists for binding `320`;
- all action before-values, including mutable timestamps, match byte-for-byte
  at apply time.

Any mismatch refuses before a writable connection is opened during planning,
or rolls back the transaction with zero committed writes during apply.

## Fail-closed exchange-evidence gates

Normalized evidence must state `snapshot_complete=true`, have no errors, and
record `exchange_write_count=0`. It must prove:

- zero exact live-position, regular-open-order, and pending-trigger matches;
- one unique full-close history row for each exact `posId`;
- exact instrument and side, positive `pos`, and `closePos == pos == 11`;
- target and sibling owned stop identities appear in trigger history with a
  non-zero trigger time matching the position close time;
- parent trigger `1001124956792983` has one unique child regular order
  `1001124961572300`, supported by matching trigger time, child creation time,
  filled quantity, instrument, side, and the exact sibling `posId`.

Symbol, side, timing, tag, or `clOrdId` alone never satisfy ownership.

## Artifacts and rehearsal

Create one root-only server evidence directory. Persist mode-`0600` normalized
exchange evidence, online backup, independent rehearsal database, canonical
plan JSON, rollback SQL, and summary JSON. Record SHA-256 for every artifact.

The rehearsal must prove:

1. pristine backup and rehearsal copy both pass `quick_check`;
2. first apply changes exactly eight rows;
3. identical reapply returns `already_applied` with zero changes;
4. all eight after rows match exactly and all table counts are unchanged;
5. reverse CAS changes exactly eight rows;
6. all eight before rows and the original logical digest are restored;
7. quick checks remain `ok` throughout;
8. production database writes and exchange writes remain zero.

The resulting plan is rehearsal evidence only. Production apply requires a
later authorization naming the exact tool commit, plan and rollback
fingerprints, repair timestamp, confirmation token, backup path and SHA, and
operation sequence. Any later drift invalidates the plan.
