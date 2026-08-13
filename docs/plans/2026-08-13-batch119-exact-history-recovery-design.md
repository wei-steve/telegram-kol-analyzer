# Batch 119 Exact-History Recovery Design

## Problem

The batch-119 recovery planner correctly refuses an incomplete Deepcoin
snapshot. Production exposes the limitation that several BTC and ETH history
collections contain exactly 100 rows. The existing client performs one
instrument-wide request, while the snapshot authority treats a full page with
no affirmative completion metadata as `snapshot_page_limit_ambiguous`.

The live position has since disappeared after an automatic stop, but current
position absence alone cannot prove which exchange action closed it. Treating a
truncated history response as complete would let a local false-submission repair
erase evidence of a real management close.

## Approved Safety Boundary

An absent batch-119 position may be classified as a natural stop only when the
close is bound to an existing `verified` batch-owned `stop_loss` or
`backup_stop` protection ledger row. A manual close, an unowned trigger order,
an incomplete identity, or conflicting close evidence remains a refusal.

The recovery remains allowlisted to batch `119`. It does not create a generic
historical-repair facility, relax ordinary snapshot completeness, replay a
message, alter automatic-trading authority, or authorize an exchange writer.

## Alternatives Considered

### General pagination

Regular order history and trade fills expose ID-based pagination, but position
history and trigger-order history expose only bounded exact filters. A general
cursor abstraction therefore cannot make every required collection complete.
It would also expand request volume and affect ordinary reconciliation.

### Time-window slicing

Position history and fills expose time filters, but trigger-order history does
not expose a complete time-pagination contract. Window boundaries could omit an
authoritative close and would require more requests.

### Exact-identity snapshot (selected)

Build one batch-specific snapshot from the target position ID and the small,
durably verified set of protection order IDs. This matches Deepcoin's official
query contract, minimizes requests, and leaves the generic automatic-trading
snapshot unchanged.

## Architecture

The batch-119 CLI continues to load the immutable incident profile and durable
database rows first. A new batch-specific read-only snapshot loader derives:

- the exact target `posId`, instrument, side, and entry ownership;
- the verified `stop_loss` and `backup_stop` ledger order references;
- any durable management close order or client-order references, which must
  remain absent for the false-submission repair; and
- a redacted SHA-256 digest of the exact query scope.

Inside one account-generation capture, the loader reads:

1. complete current account positions;
2. complete current open orders;
3. complete current pending TPSL rows for the target instrument;
4. position history filtered by the exact target `posId`;
5. trigger history filtered separately by each verified protection `ordId`;
6. regular order history and fills only for a durable exact order reference,
   never as an instrument-wide scan.

Every response still passes through the existing bounded collection authority.
A response with 100 rows and no affirmative completion proof remains
incomplete. All exact history collections, their scopes, row counts, canonical
fingerprints, and account generation contribute to the recovery snapshot and
final evidence fingerprint. Capture times are validated for chronology and
freshness but are not themselves fingerprint inputs, so two semantically
identical fresh captures can produce the same reviewed fingerprint.

The loader is called only by `recover-composite-management-batch` for the fixed
batch-119 profile. Generic binding reconciliation, protected-entry
reconciliation, Web background sync, and automatic-trading readers retain their
existing behavior.

## Natural-Stop Proof

`position_absent` is allowed only if all of the following are true:

- no current position matches the immutable `posId`, instrument, and side;
- exact position history contains the same `posId` and proves it closed;
- exactly one verified protection order has a successful terminal trigger fact;
- that trigger row matches the ledger order ID, instrument, side, purpose, and
  position identity available from durable evidence;
- the trigger time is no later than the position close/update time;
- any other original protection order is terminally cancelled, untriggered, or
  otherwise cannot claim the close;
- no durable management request, response, client order ID, exchange order ID,
  close mutation intent, close execution event, or owned management close
  history exists; and
- no unowned or conflicting exchange close row exists in the exact scope.

The planner records only the protection role, bounded state facts, counts, and
hashed references. It never serializes raw order IDs, position IDs, provider
messages, responses, or credentials.

If this proof succeeds, the existing `position_absent` apply path may only
terminalize local batch, leg, and component state and append its immutable audit
event. It constructs no exchange writer and reports `exchange_calls=0`.

## Failure Handling

The planner returns `refused` for any of these conditions:

- reader unavailable, transport failure, invalid JSON/schema, or incomplete
  pagination;
- account generation drift or an in-progress generation;
- missing, duplicate, non-verified, or wrong-owner protection ledger rows;
- exact-query response identity drift;
- 100-row ambiguity even under an exact filter;
- no triggered verified stop, multiple stops claiming the close, reversed or
  malformed timestamps, or an unowned/manual close;
- any newly discovered durable close submission evidence; or
- source, instruction population, lifecycle, binding, component, target, or
  evidence fingerprint drift.

Exceptions are reduced to closed safe reason codes. Retry is manual and always
starts with a new dry-run; there is no automatic apply or exchange retry.

## Test Strategy

Tests must first reproduce the current failure and then cover:

- instrument-wide histories at 100 rows while exact `posId`/`ordId` reads are
  complete and prove one verified natural stop;
- the same snapshot returning `ready` with `position_absent`, zero production
  writes, and zero exchange calls;
- exact response at 100 rows remaining refused;
- manual/unowned close, duplicate triggered stops, wrong ledger purpose,
  instrument/side/position mismatch, malformed/reversed time, and generation
  drift remaining refused;
- missing exact-history reader, pagination metadata, invalid/deep JSON, and
  hostile provider text remaining bounded and redacted;
- a fixed maximum GET population based only on the allowlisted exact identities;
- no call to any exchange writer in dry-run or position-absent apply;
- no behavior change in generic reconciliation and protected-entry snapshots;
  and
- CLI serialization, stale fingerprint/CAS, repeated apply, and deployment
  preflight regressions.

## Production Procedure

After local RED/GREEN work, broader regression, and independent review:

1. push the reviewed commit without deploying it;
2. run the candidate dry-run against a private consistent database copy with
   additive schema bootstrap only;
3. require `position_absent`, exact natural-stop ownership, zero writes/calls,
   and stable source/evidence fingerprints across two captures;
4. present the new redacted plan for a separate apply approval;
5. only after approval, follow the dedicated batch-119 stopped-service backup,
   final dry-run, exact-fingerprint apply, readback, and restart runbook; and
6. begin the dormant Stage-1 deployment in a later, separate quiet window.

No batch-119 apply or Stage-1 deployment is authorized by this design.
