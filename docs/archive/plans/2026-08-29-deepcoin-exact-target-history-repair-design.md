# Deepcoin Exact-Target History Repair Design

**Date:** 2026-08-29
**Status:** approved by continuation of the recommended failed-preflight repair
**Risk:** L3 evidence-contract code; this local phase authorizes no production action

## Objective

Remove the manual-cleanup reconciliation workflow's dependency on bounded
full-account trigger history and fills. Keep the stable flat-account snapshots,
then obtain adverse history evidence and zero-fill evidence separately for each
canonical reviewed order. Preserve fail-closed handling, single-attempt external
reads, dry-run/apply drift detection, and every existing local ownership check.

## Considered Approaches

1. **Use a manual-cleanup evidence profile plus exact per-order reads
   (selected).** The shared account snapshot still proves positions, regular
   orders, and pending triggers are complete and empty. History and fills are
   read exactly once for each canonical `ordId`, so unrelated full-account
   pagination cannot block or dilute the target evidence.
2. Paginate full-account trigger history and fills. Deepcoin's stable cursor and
   ordering contract are not established, and the resulting evidence remains
   coupled to unrelated account activity.
3. Remove history evidence. Exact zero fills plus stable flat snapshots are
   strong negative evidence, but omitting history would discard affirmative
   filled, active, identity-conflict, and instrument-drift signals that must
   still block.

## Selected Evidence Contract

The default `build_deepcoin_maintenance_evidence` behavior remains unchanged.
The manual-cleanup caller selects only the complete account-flat queries:

- positions for every governed instrument;
- regular open orders for every governed instrument;
- pending triggers for every governed instrument.

It does not call the broad trigger-history or broad fills endpoints. For each
member of `REVIEWED_PENDING_ENTRY_TARGETS`, it then performs exactly one query
for trigger history by `instId + ordId` and exactly one fills query by
`instId + ordId`. There is no automatic retry.

Exact fills must be a well-formed list shorter than the endpoint boundary and
must contain zero rows. Any returned row, malformed result, exception, identity
conflict, or boundary-sized result blocks.

Exact history may contain zero rows or one well-formed row. Zero rows and a
unique row without literal `cancelled|canceled` are not blockers by themselves.
More than one row, malformed results, boundary-sized results, exceptions,
missing or conflicting order identity, conflicting client identity, instrument
drift, an active/executed/filled state, or a nonzero or malformed fill quantity
blocks.

The exact-history result is included in the reconciliation-plan fingerprint.
Therefore dry-run/apply cannot silently accept a changed history row after the
full-account history is removed from the shared evidence fingerprint.

## Scope and Safety Boundaries

- Canonical targets still come only from `REVIEWED_PENDING_ENTRY_TARGETS`.
- Local identity, economics, binding, leg, lifecycle, protection, convergence,
  authority, backup, transaction, and runtime-stop checks are unchanged.
- Unknown remains non-retryable in this workflow.
- No second order list, persistent state, lease, bridge, replay, cancellation,
  compensation order, or entry thaw is added.
- This phase performs no push, stage, SSH, service control, production database
  write, Deepcoin write, activation, or production observation.

## Acceptance

- A broad trigger-history or fills endpoint that would return 100 rows is never
  called by manual reconciliation.
- The default maintenance-evidence caller still reads and completeness-checks
  broad history and fills exactly as before.
- Every canonical target receives one exact history read and one exact fills
  read with the expected `instId` and `ordId`.
- Missing/nonliteral history plus exact zero fills can produce a ready plan.
- Explicit adverse history, duplicate/malformed/unknown history, or any fill
  blocks before database mutation and is not retried.
- Exact-history drift changes the plan fingerprint.

