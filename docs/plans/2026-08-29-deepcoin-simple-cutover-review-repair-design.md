# Deepcoin Simple Cutover Review Repair Design

**Date:** 2026-08-29
**Status:** approved
**Risk:** L2 activation behavior and L3 local reconciliation; every production
action remains separately authorized

## Objective

Keep the short maintenance-window architecture while repairing only the four
action-level gaps found by the independent review. Do not restore the deleted
lease/bootstrap/drain protocol or introduce another persistent handoff state.

## Considered Approaches

1. **Add a stopped-legacy mode to the existing scoped activator (selected).**
   Reuse immutable release validation, authorization consumption, entry-frozen
   drop-ins, post-start runtime identity, and rollback. Replace only the
   impossible pre-start runtime identity proof with direct proof that every
   controlled unit is inactive and persistently masked.
2. Restore the deleted bootstrap protocol. This would recover the first-cutover
   path but also restore the state, leases, handoff ownership, and failure modes
   that the maintenance-window design intentionally removed.
3. Use an operator-only sequence of raw systemd commands. This is shorter in
   code but leaves no atomic authorization, immutable candidate validation, or
   tested rollback boundary and is therefore not acceptable for production.

## Selected Architecture

The existing activator gains one explicit `stopped_legacy` source mode. It is
valid only for the full authority scope (`web`, `monitor`, `ingest`, `worker`).
Before consuming authorization it proves:

- candidate and rollback releases are independently immutable and compatible;
- every controlled service and the monitor timer is inactive and persistently
  masked;
- active exchange-write count is exactly zero;
- entry admission will be frozen in every candidate and rollback drop-in.

The activator then consumes the ordinary one-shot activation authorization,
publishes candidate drop-ins, unmasks the declared units, and starts the
candidate. Candidate runtime identity and live management, protection, close,
TPSL, rescue, ingest, and worker capabilities are proved exactly as in ordinary
activation. There is no pre-start PID comparison because the source is proven
stopped. On candidate failure, the activator starts only the validated rollback
release, still entry-frozen. If rollback also fails, it stops and persistently
masks the entire scope and reports `rollback_failed`.

Ordinary release-to-release activation is unchanged and still requires the
currently running immutable rollback identity.

## Reconciliation Repairs

`finalize-cancelled-pending-entries` must prove the same inactive-and-masked
maintenance state before its first exchange read and again immediately before
the database write transaction. Evidence time is the snapshot completion time,
never a timestamp captured before network reads.

The exchange snapshot refuses any current position, regular order, or pending
trigger. It also refuses any target-related fill or trigger history that cannot
prove the target ended unfilled and cancelled. Unknown or ambiguous history is
permanently fail-closed; there is no automatic retry or recovery action.

For every member of canonical `REVIEWED_PENDING_ENTRY_TARGETS`, local proof
binds the exact lifecycle, binding, leg, venue, instrument, order ID, side,
entry purpose, stored request fingerprint and order economics. Protection
intent and protection/convergence rows must point to the same binding and leg.
The normal authority row is parsed with the canonical authority parser; a
malformed timestamp is not an idle authority.

The backup destination must be new, non-symlink, owner-only (`0600`), inside an
owner-controlled non-group/world-writable directory. SQLite `quick_check`,
`foreign_key_check`, and affected-table before counts are recorded before the
single terminalization transaction. The source database used for backup must
be the same resolved path used by the CLI session factory.

## Persistent State

No new database or protocol state is added:

```text
legacy_running -> maintenance_stopped -> candidate_entry_frozen
```

`stopped_legacy` is an invocation mode, not a stored lifecycle state.

## Acceptance

- A simulated legacy runtime with no identity endpoint can cut over only when
  the full unit scope is inactive and persistently masked.
- Any active, merely stopped-but-unmasked, or unknown unit blocks before
  authorization consumption and before database backup.
- Default-clock reconciliation produces fresh evidence; future or stale
  evidence still fails closed.
- Any target-related fill, ambiguous history, economic drift, identity drift,
  malformed authority timestamp, or noncanonical protection link blocks.
- Successful apply terminalizes all seven canonical targets and seeds authority
  in one transaction; failure leaves both the database and mask boundary safe.
- Ordinary release-to-release activation behavior and bytecode protections are
  unchanged.

## Authorization Boundary

This local repair authorizes no push, stage, SSH, production read, service
control, Deepcoin cancellation, database mutation, activation, rollback, or
entry thaw.
