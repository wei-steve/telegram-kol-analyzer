# Deepcoin Reviewed Pending-Entry Cancellation Quiescence Design

## Goal

Eliminate the cross-process authority-check-to-exchange-write race between the
runtime entry-revision worker and the reviewed pending-entry cancellation CLI,
without stopping or borrowing authority from the protection worker.

## Root cause

The cancellation apply path currently performs a final database authority
check, closes that session, transitions its mutation intent and then calls
Deepcoin. The entry-revision executor is protected only by the module-level
`position_authority_lock`, which is a single-process `RLock`. The CLI and worker
run in separate processes, so a worker can claim a revision batch after the
CLI's last check and before its exchange request.

Checking `entry_revision_v2_mode=disabled` alone does not close the race. A
worker iteration can read `live` before the setting changes and claim later.
Repeated point-in-time queries have the same gap.

## Chosen architecture

Introduce one durable, database-backed exchange-authority lease stored under a
dedicated `TradingSetting` key. Acquisition uses SQLite `BEGIN IMMEDIATE`, so
selection and publication are atomic across processes without a schema change.

Only two writers participate:

- the live entry-revision executor, with owner kind `entry_revision_worker`;
- the reviewed pending-entry cancellation apply path, with owner kind
  `reviewed_pending_entry_cancel`.

Protection, rescue and position-management workers do not acquire this lease.
Their existing authority and process lifecycle remain unchanged.

The closed lease document contains only bounded operational fields: schema
version, state, owner kind, random token, optional revision batch/order identity
and acquisition timestamp. Unknown keys, malformed JSON, unsupported versions,
unknown state or unknown owner fail closed.

## Quiescence contract

The cancellation apply path may acquire authority only when the same immediate
transaction proves:

- `auto_trade_enabled` is exactly `false`;
- `entry_revision_v2_mode` is exactly `disabled`;
- the lease is absent or explicitly idle;
- no active or ambiguous exchange authority already exists.

The worker acquires the same lease immediately before entering the live
entry-revision executor. Therefore, after lease-aware code is deployed, an
iteration that read `live` before the settings freeze either already owns the
lease or must fail to acquire it. The CLI cannot overlap that iteration.

Dry-run planning never acquires or modifies the lease.

## Apply data flow

1. Validate the supplied plan, exact single order, action ID, fingerprint and
   unused confirmation token.
2. Build a fresh read-only exchange/database plan.
3. Atomically acquire the cancellation lease while checking the frozen settings.
4. Rebuild the fresh plan under the held lease. Any drift releases the lease
   because no exchange write has started.
5. Reserve the exact mutation intent, consume the confirmation token, run the
   last database gate and transition the intent to `submitting`.
6. Submit exactly one Deepcoin cancellation and perform the existing complete
   readback and local terminalization.
7. Release the lease only after confirmed cancellation and complete local
   terminalization, or after a proven pre-write refusal.

Every subsequent order requires a new dry-run, fingerprint, confirmation token
and lease acquisition.

## Worker data flow

Disabled and shadow entry-revision modes remain write-free and do not acquire
the lease. A live execution attempts to acquire the worker lease before its
batch claim or any exchange write. If the lease is busy or malformed, the
executor returns a bounded in-progress/fail-closed result and performs no batch
or exchange mutation. A normal, fully recorded return releases the lease. An
unhandled exception retains it.

## Failure semantics

The lease has no timeout, stale takeover or automatic cleanup. Process death
cannot silently reopen exchange authority.

- Pre-exchange drift or refusal: release is permitted.
- Confirmed cancellation plus complete local terminalization: release is
  permitted.
- Transport exception, unconfirmed response, incomplete readback, changed
  post-cancel evidence, database terminalization failure or unhandled exception:
  retain the lease and stop.
- Busy, malformed or unknown lease: no exchange write and no retry.
- Lease release token/owner mismatch: retain the lease and fail closed.

Existing `PositionMutationIntent` unknown-outcome evidence and ambiguous-child
gates remain authoritative; the lease adds cross-process exclusion and does not
replace those records.

## Security and observability

Lease output exposes no credentials or exchange responses. CLI results use
bounded reason codes. The confirmation token is not stored in the lease. The
production monitor may later add a read-only stuck-lease diagnostic, but no
monitor or deployment change is part of this local repair.

## Verification

TDD must prove:

- two independent session factories cannot acquire competing leases;
- malformed, unknown and held leases fail closed;
- cancellation acquisition requires both frozen settings;
- a live revision worker holding the lease blocks cancellation before the
  exchange call;
- cancellation holding the lease blocks the revision worker before batch claim;
- protection code is not routed through the new lease;
- pre-write refusals release authority;
- successful exact cancellation releases authority;
- every unknown or incomplete post-write outcome retains authority and remains
  non-retryable;
- dry-run remains read-only and one-order execution remains enforced.

Run the focused cancellation and entry-revision files, adjacent authority and
protection tests, then one final full repository suite. Independent review must
find no Critical or Important issue before the local candidate is committed.

## Explicit exclusions

This design does not authorize or perform push, deployment, SSH, settings
freeze, restart, production/database mutation, Deepcoin write, history replay or
Telegram trading notification. Deploying lease-aware worker code, freezing the
two settings and executing each production cancellation remain separate future
authorizations.
