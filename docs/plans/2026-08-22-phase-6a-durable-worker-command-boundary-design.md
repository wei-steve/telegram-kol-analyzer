# Phase 6A Durable Worker Command Boundary Design

**Date:** 2026-08-22

**Status:** Approved for planning; implementation still requires a separate explicit user approval

## Context

Phase 6 process separation stopped fail closed at its first authority decision
gate. The SQLite three-process load gate passed, but the static authority trace
proved that four Web routes can still reach position or exchange authority:

- `POST /api/execution/sync-deepcoin`
- `POST /api/execution/close-bound-position`
- `POST /api/recovery-live-submit`
- `POST /api/trade-signals/process-next`

Splitting the current monolith while those paths remain callable in the Web
process would create two exchange-authority processes. Phase 6A is therefore a
prerequisite to Phase 6, not part of the process split itself.

## Goal and invariants

Move all four routes behind a worker-owned durable command boundary while
preserving their current HTTP success/error contracts and all trading meaning.

Phase 6A must not change:

- recognition output or first-pass recognition ownership;
- contextual strategy resolution;
- position ownership or attribution;
- order parameters, order ordering, exchange-write behavior, or existing
  domain-layer idempotency;
- `message_lock_mode=global`;
- `message_pipeline_mode=queue`;
- the Phase 6 process topology, which remains a later phase.

Phase 6A also must not absorb DeepSeek 402 handling, Monitor A work, historical
data repair, or any L3 repair unrelated to the new command table.

## Chosen architecture

Use a SQLite-backed durable command table. The Web route performs its existing
input validation, persists a command, and waits asynchronously for its durable
result. A worker-owned consumer exclusively claims the command and invokes the
same existing domain function with the same arguments and exception-to-HTTP
mapping. The Web route then returns the same status and JSON shape it returns
today.

This retains synchronous HTTP compatibility while moving exchange authority
out of the route call graph. It also gives restart recovery and an auditable
uncertainty boundary that an in-memory RPC cannot provide.

Rejected alternatives:

1. Returning `202 Accepted` immediately would simplify the worker but would
   change all four external interfaces and UI behavior.
2. A direct in-memory or socket RPC would separate call stacks but would lose
   commands during restart and could not safely classify an interrupted
   exchange submission.

## Command contract

The new `worker_command_jobs` table is append-oriented durable evidence with
these fields:

| Field | Purpose |
|---|---|
| `id`, `command_id` | Internal row id and public opaque command identity |
| `command_type` | Closed enum for the four approved adapters |
| `request_json` | Canonical bounded payload containing only domain inputs |
| `request_fingerprint` | SHA-256 of command type plus canonical payload |
| `idempotency_key` | Optional caller key, unique within command type |
| `status` | `pending`, `claimed`, `executing`, `succeeded`, `failed`, or `uncertain` |
| `claim_token`, `claimed_at`, `lease_expires_at` | Atomic worker ownership and pre-execution recovery lease |
| `attempt_count` | Claim/execution-attempt count |
| `side_effect_started_at` | Durable point after which replay is forbidden |
| `http_status`, `result_json` | Existing route response reconstructed for the waiter |
| `error_code`, `error_summary` | Stable class and bounded non-secret diagnostic |
| `uncertain_at`, `reconciled_at` | Uncertainty and later read-only resolution evidence |
| `created_at`, `completed_at` | Lifecycle timestamps |

Constraints and indexes must enforce:

- a closed status enum and closed command-type enum;
- unique `command_id`;
- unique non-null `(command_type, idempotency_key)`;
- a claim scan index over `(status, lease_expires_at, created_at)`;
- a request fingerprint index for diagnostics;
- bounded payload/result/error sizes in application validation;
- no API credentials, authentication headers, session values, or unbounded raw
  exchange responses in durable JSON.

Approved command mappings:

| Command type | Existing authority invoked only by worker |
|---|---|
| `sync_deepcoin_execution` | Existing reconcile/sync sequence, including current incident/cleanup delivery ordering |
| `close_bound_position` | `close_bound_position_market` plus current cleanup delivery |
| `recovery_live_submit` | `submit_recovery_order_live` |
| `process_next_trade_signal` | `process_next_trade_signal_live` |

The adapters must be thin. They may serialize inputs and outputs and preserve
the current exception mapping, but may not reinterpret the domain decision.

## State machine and crash boundary

```text
pending -> claimed -> executing -> succeeded
                              |-> failed
                              |-> uncertain
```

- A worker claims one eligible row atomically with a fresh token and lease.
- A stale `claimed` row with no `side_effect_started_at` may return to pending
  and be reclaimed.
- Immediately before entering an adapter that may read/write the exchange, the
  worker durably transitions the row to `executing` and sets
  `side_effect_started_at`.
- A worker that receives a normal domain result durably records `succeeded`.
- A known pre-submission/domain rejection with a complete error mapping records
  `failed` and the original HTTP status/detail.
- A crash, lost response, lease expiry, or incomplete evidence after
  `side_effect_started_at` records or is subsequently classified as
  `uncertain`. It is never automatically reclaimed or replayed.

The durable transaction that marks `executing` is deliberately before the
external call. This may conservatively classify a command uncertain even when
no order was sent, but it cannot silently repeat a possibly completed write.
Safety takes priority over availability at this boundary.

## Idempotency

`Idempotency-Key` is optional for compatibility. If supplied, the enqueue
operation atomically returns the existing command when both command type and
canonical request fingerprint match. Reusing the key with a different payload
returns `409` and creates no new command.

First-party UI actions must generate one stable random key per confirmed user
action and reuse it across a network retry. A new explicit confirmation creates
a new key. Existing callers that omit the header retain one-request-one-command
behavior.

This transport idempotency does not replace existing domain safeguards such as
position mutation intents, execution events, bindings, order legs, or exchange
order identities. Those remain authoritative for determining whether a trade
or position mutation happened.

## HTTP compatibility and waiting

Each route keeps its current payload validation before enqueueing. In queue
mode it then waits using non-blocking polling; synchronous SQLite calls must run
off the event loop. Terminal rows are mapped back to the exact current status
code and response body:

- `succeeded`: current `200` response body;
- `failed`: current `409`, `422`, `502`, or bounded `500` mapping;
- enqueue conflict: `409` for idempotency-key payload mismatch;
- wait deadline: explicit `504`, while the durable job continues;
- `uncertain`: explicit non-success response with `command_id`, no replay, and
  operator reconciliation required.

A retry with the same idempotency key attaches to the existing job and returns
its eventual terminal result. The durable result schema is versioned internally
so a worker result can be rejected rather than misrendered after incompatible
code drift.

## Uncertain reconciliation

Reconciliation is read-only and command-specific. It collects the minimum
evidence required to determine whether the original operation happened:

- exact `posId` and position state;
- `PositionMutationIntent`;
- `ExecutionEvent`;
- `ExecutionBinding` and `ExecutionOrderLeg`;
- client and exchange order identities;
- direct Deepcoin order, fill, trigger, and position history.

Allowed outcomes are:

- `confirmed_succeeded`: evidence proves the intended operation happened;
- `confirmed_no_submission`: complete evidence proves nothing was submitted;
- `conflict`: evidence disagrees or proves a different mutation;
- `evidence_incomplete`: an external query is partial, unavailable, or lacks a
  unique identity chain.

Only the first two outcomes may terminalize the command. `conflict` and
`evidence_incomplete` remain `uncertain`. An incomplete external response is
unknown, not zero; after one reasoned retry the system fails closed. Phase 6A
does not auto-resubmit even after `confirmed_no_submission`; any future retry
requires a new explicit user action and the existing domain gates.

## Rollout modes

Add an independent `worker_command_mode` setting:

- `inline` (default, migration-only): current route behavior; no worker command
  executes.
- `shadow` (migration-only): route executes inline and records a command plus a
  bounded response fingerprint for parity; the worker must not execute shadow
  rows.
- `queue`: route enqueues/waits and the worker is the only adapter authority.

The consumer initially runs inside the existing monolith lifespan so Phase 6A
can prove the boundary without introducing a second process. Phase 6 later
moves the already-exclusive consumer into the worker process. The authority
test is about Web route reachability, not merely which OS process currently
hosts the consumer.

Mode transitions must use a quiet-window gate. At most one of inline execution
or worker execution may own a command. Shadow rows can never be claimed. Queue
rows already `claimed` or `executing` cannot be adopted by inline rollback.

`inline` and `shadow` are deliberately transition-only. They cannot remain as
reachable Web-to-exchange branches in the final Phase 6A candidate because the
static Phase 6 authority gate examines possible call paths, not only the live
setting. Rollout therefore uses two reviewed production candidates:

1. a compatibility candidate supplies `inline`, `shadow`, and `queue` so shadow
   parity and the queue cutover can be proven;
2. after queue evidence passes, a hardening candidate removes the legacy direct
   route branch while retaining queue execution and durable evidence.

Each candidate receives its own one-time final full-suite run. The second run
is required because removing the legacy production path changes production
code after the first run. Phase 6A cannot complete, and Phase 6 cannot resume,
until the hardening candidate is deployed in queue mode and the authority test
passes without an allowlist or xfail.

## Migration and rollback

Because Phase 6A adds a table and bootstrap/index definitions, it is L3.

Before any production migration:

1. Create a SQLite online backup and a separate rehearsal copy.
2. Record `PRAGMA quick_check`, the schema, critical business-table counts, and
   targeted hashes.
3. Apply bootstrap to the rehearsal copy twice to prove idempotence.
4. Verify only `worker_command_jobs` and its indexes/constraints were added;
   existing row counts and targeted hashes must be unchanged.
5. Rehearse physical rollback on a second copy by dropping only the new table
   and its indexes, then repeat quick check/count/hash verification.

Operational rollback after deployment retains the table and evidence:

1. Block new queue submissions.
2. Stop new worker claims.
3. Require zero `claimed` and zero `executing` commands.
4. Leave all `uncertain` rows untouched.
5. During the compatibility-candidate rollout, switch
   `worker_command_mode` to `inline`.
6. After the hardening candidate is deployed, use the gated updater to restore
   the exact reviewed compatibility candidate, verify its exact SHA, and only
   then switch to `inline`. Do not reintroduce the branch by an ad hoc edit.

Physical schema removal is allowed only before queue cutover or under a new,
separately approved migration plan. It is not part of routine rollback.

## Verification gates

Implementation follows strict TDD: each behavior starts with one focused
failing test, then the minimum code to pass. Required focused coverage includes
schema/bootstrap idempotence, atomic claims, stale pre-execution reclaim,
post-boundary uncertainty, idempotency conflicts, result compatibility,
restart recovery, event-loop non-blocking behavior, shadow/queue exclusivity,
all four adapters, and read-only uncertain reconciliation.

The final production-code candidate gets one complete local suite. If
production code changes afterward, rerun affected focused tests and one new
final complete suite.

Production gates are:

1. L3 migration and rollback rehearsal on production database copies.
2. Dormant `inline` deployment with schema verification.
3. Shadow parity for all observed command types without worker execution.
4. Proven safe window, then queue cutover.
5. One deliberate restart that proves pending/pre-execution recovery and no
   replay of an executing/uncertain command.
6. Direct exchange-history checks for commands that can write.
7. The Phase 6 authority test passes with no known-blocker allowlist or xfail.
8. Required production observation and rollback checks are recorded before
   Phase 6A can be completed.

Any incomplete external evidence, unsafe deployment window, duplicate
authority, unexplained result drift, SQLite busy anomaly, or unresolved
`uncertain` command fails closed. Phase 6A remains `in_progress`, production
stays at the last safe mode, and Phase 6 cannot resume.

## Completion boundary

Phase 6A is complete only when all four Web routes have preserved their public
contracts while their call graphs no longer reach exchange or position
authority, the worker queue is the single proven authority, restart and
uncertain-state behavior are demonstrated, the L3 rehearsal and rollback gates
pass, and production evidence is recorded.

Completion authorizes returning to Phase 6 Task 1. It does not itself authorize
the process split, a later cleanup phase, or any exchange-write semantic
change.
