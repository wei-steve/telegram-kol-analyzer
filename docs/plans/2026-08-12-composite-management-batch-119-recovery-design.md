# Composite Management Batch 119 Recovery Design

**Date:** 2026-08-12

**Status:** Approved for implementation planning

## Objective

Repair the state-machine defect that allowed legacy management reconciliation to
take ownership of a composite-management batch, then recover production batch
119 through its existing durable composite workflow. The recovery must converge
the original BTC long position from its frozen starting size of `38` to its
frozen target remaining size of `19`, and then establish verified break-even
protection for the actual remaining position.

The recovery is not a replay of the old Telegram message and is not a new
percentage calculation. It is convergence to one immutable target.

## Incident Summary

Batch 119 belongs to lifecycle 794 from the source
`🏧三姐精准策略群🏧11分组`. Its contract requires three ordered components:

1. consume the first take-profit stage;
2. converge the position to remaining size `19`;
3. replace the remaining protection at break-even.

The first component could not obtain a complete exchange snapshot and correctly
entered `recovery_required`. No close mutation intent, close request, provider
response, client order ID, or exchange order ID was created.

The legacy reconciliation path subsequently processed the composite batch. It
interpreted an unchanged position with no matching close order as a pending
legacy close, changed the management leg to `submitted`, and changed the batch
to `reconciling`. Every later readback found the same unchanged position and no
matching order, so the state remained permanently pending. This is a local
state-machine ownership defect, not a long-running Deepcoin order.

## Goals

- Enforce one owner for each management state machine.
- Preserve batch 119, its component identities, and all existing audit evidence.
- Prove that the incident batch has no durable close-submission evidence before
  allowing recovery.
- Calculate a close only as `current_size - 19` when that value is positive and
  valid under the frozen contract specification.
- Never add exposure or repeat a close because the current position changed
  after the original message.
- Use the existing position-mutation gateway and composite executor so durable
  reservation, idempotency, readback, and unknown-outcome behavior remain in
  force.
- Establish and verify new primary and backup protection before cancelling old
  protection.
- Keep production MiMo mode on `v1` throughout the incident recovery.

## Non-Goals

- Do not activate MiMo v2.
- Do not replay the historical Telegram message through recognition or automatic
  trade routing.
- Do not create a second compensation batch or a manually assembled exchange
  order.
- Do not provide a generic database state editor.
- Do not erase or rewrite the evidence that shows how batch 119 entered the
  incorrect state.
- Do not close existing positions merely to create a normal deployment window.

## Approaches Considered

### A. Repair and resume the durable composite workflow — selected

Add strict state-machine ownership, build a fingerprinted incident recovery
plan, repair only the proven false legacy state, and continue through the
existing composite executor. This retains exact position identity, audit,
idempotency, ordered components, and exchange readback behavior.

### B. Fix code but leave the real position for manual handling

This avoids an automated recovery write but separates the exchange action from
the durable management record. It leaves the batch unresolved and makes later
proof of what happened harder.

### C. Submit a direct exchange operation and patch the database afterward

This is faster but bypasses the established mutation gateway and creates the
highest duplicate-operation and audit risk. It is rejected.

## Architecture

### 1. Exclusive state-machine ownership

A batch with a non-empty `management_contract_json`, a contract fingerprint, a
contract version, or composite components is a composite batch. The legacy
`reconcile_strategy_management_batches` query must exclude composite batches
before it inspects or mutates any legs.

The composite reconciler and composite executor are the only normal paths that
may mutate composite component state. The global execution-binding reconciler
may still invoke legacy management reconciliation for traditional batches, but
the exclusion remains inside the legacy reconciler itself so every caller gets
the same safety rule.

Malformed hybrid batches fail closed. They are not silently routed into the
legacy state machine.

### 2. Dedicated recovery plan

Add a dedicated batch-recovery module and CLI entry point with dry-run as the
default. It accepts one exact batch ID and cannot edit arbitrary rows.

The read-only plan contains an allowlisted summary and a deterministic evidence
fingerprint over:

- batch, lifecycle, binding, strategy instance, and management-leg identity;
- frozen start size, target remaining size, quantity step, and minimum size;
- current exact position size and side;
- current regular orders, trigger orders, trade fills, and protection ownership;
- the batch, leg, component, and matching mutation-intent state;
- proof that no close request, response, client order ID, exchange order ID,
  execution event, or position mutation intent exists for the false submission;
- a complete exchange snapshot fingerprint; and
- the proposed database state transition required to return ownership to the
  composite workflow.

The retained output contains no credentials, raw provider payloads, or source
message text.

### 3. Apply boundary

Apply requires all of:

- batch ID `119`;
- the exact dry-run fingerprint;
- an explicit recovery authorization value; and
- a fresh exchange snapshot and database reread that reproduce the fingerprint.

The apply transaction uses an immediate SQLite write lock. It performs compare
and swap checks on the batch, leg, components, and absence of submission
evidence. Any mismatch aborts the entire operation before an exchange call.

The repair preserves the incident evidence in an immutable execution event. It
returns the batch and leg from their false legacy wait state to a composite-safe
state without marking any exchange action as completed. Component 1 remains the
first unconfirmed component and is retried only by the composite executor.

No recovery apply path calls Deepcoin while holding a database transaction.

## Recovery Data Flow

### Snapshot classification

The latest exact position is compared with the immutable target remaining size
`19`:

- `current > 19`: the partial-close component submits only the exact difference;
- `current == 19`: the partial-close component confirms with a zero delta and
  proceeds to protection;
- `current < 19`: no close and no add operation are allowed; the batch enters a
  reviewed protection-only recovery using the actual remaining size;
- position absent: no new position is created; the batch is terminalized from
  complete exchange evidence.

The `current < 19` and position-absent paths require explicit, tested component
classification. They must not be achieved by weakening the existing sizing
guard or treating an error as success.

### Ordered execution

For a live position at or above the target:

1. Obtain a complete snapshot for positions, pending trigger orders, trigger
   history, regular order history, and fills.
2. Reconcile or consume the first take-profit stage using its existing durable
   cancellation intents.
3. Re-read the exact position.
4. Calculate the close delta against target `19`.
5. Reserve a position mutation intent before the exchange call.
6. Submit one exact close through `PositionMutationGateway`.
7. Read back exchange truth. An unknown result becomes `awaiting_exchange` and
   is never automatically resubmitted.
8. Confirm the actual remaining quantity.
9. Create and read back a new primary break-even stop and backup stop sized to
   the actual remaining position.
10. Only after both new stops are verified may the old owned stops be cancelled.
11. Validate composite completion and persist the terminal batch state.

## Safety Invariants

- The original target is remaining size `19`; recovery never calculates 50% of
  the current size.
- No recovery path increases exposure.
- No exchange write occurs when the complete snapshot is missing or stale.
- No exchange write occurs when lifecycle, binding, leg, position, side, or
  contract specification identity has drifted.
- Any durable or exchange evidence of an earlier close submission blocks the
  false-submission repair.
- A possible exchange write disables database rollback to an earlier backup.
- Unknown exchange outcomes stop later components and enter reconciliation.
- The live execution gate is checked at every existing writer boundary.
- Existing protection is retained until replacement protection is verified.
- MiMo contract mode remains `v1` during deployment and recovery.

## Audit

Recovery writes one idempotent execution event keyed by the approved evidence
fingerprint. The event records only bounded structured facts:

- recovery action and batch ID;
- before/after batch, leg, and component statuses;
- source and exchange snapshot fingerprints;
- immutable target size and classified current-size relation; and
- whether any exchange call was possible when the state repair committed.

Repeated apply with the same fingerprint returns the existing result. A new
fingerprint requires a new review.

## Deployment and Production Recovery

Normal deployment preflight is expected to reject batch 119 because it appears
active. Recovery therefore uses a narrowly scoped incident window rather than
weakening normal preflight policy:

1. Complete local tests, review, commit, and push.
2. Fetch candidate code on the server without changing the running checkout.
3. Run the candidate dry-run against the live database in read-only mode and
   obtain a complete exchange snapshot.
4. Prove there is no other real in-flight instruction, recovery, reconciliation,
   or time-sensitive strategy operation.
5. Stop the service and create a verified database backup.
6. Revalidate that batch 119 is the only allowed false-active exception and that
   its evidence fingerprint is unchanged.
7. Install the reviewed candidate with MiMo still on `v1`.
8. Generate a new final dry-run fingerprint after installation.
9. Apply the state repair and invoke the original composite workflow.
10. Verify position size, order identity, primary stop, backup stop, retained
    take profit, mutation intents, component states, and audit event.
11. Restart and verify the Telegram listener, Web service, worker health, and
    MiMo `v1` setting.

This exception is not added to the general deployment helper. It is a dedicated
one-batch recovery command whose checks are stricter than the ordinary preflight.

## Rollback

Before any possible exchange write, a failed recovery may restore the verified
database backup and prior reviewed code.

After a request may have reached Deepcoin, do not restore the old database. The
new database contains the only durable reservation and idempotency evidence.
Keep the reviewed recovery code and current database, stop subsequent
components, and reconcile from exchange truth.

No rollback cancels a newly verified protective order unless another verified
protective order remains in place.

## Test Strategy

Testing follows red-green-refactor:

1. Reproduce the defect: a composite component enters `recovery_required`, then
   legacy reconciliation incorrectly marks its leg `submitted`.
2. Prove legacy reconciliation excludes valid and malformed composite batches
   while traditional batches behave unchanged.
3. Prove dry-run refuses incomplete snapshots, identity drift, any submission
   evidence, unexpected mutation intents, new matching orders, or stale
   fingerprints.
4. Prove apply uses an immediate-lock compare-and-swap boundary and is
   idempotent.
5. Use fake Deepcoin clients for current sizes above, equal to, and below `19`,
   plus a missing position.
6. Prove `38` produces one close of `19`, not another 50% calculation.
7. Prove unknown close results never resubmit and block protection replacement.
8. Prove replacement stops are verified before old stops are cancelled.
9. Run focused management, position-mutation, execution-binding, deployment
   preflight, CLI, and database migration suites.
10. Run the complete local suite and independent code review.

## Success Criteria

- The regression test demonstrates the original defect before the fix and
  passes afterward.
- No composite batch can be mutated by legacy reconciliation.
- Batch 119 dry-run proves exact identity and absence of prior close submission.
- Production recovery converges to the immutable target or safely classifies a
  smaller/absent position without adding exposure.
- Remaining live exposure has verified primary and backup protection.
- The batch and components reach an evidence-backed terminal or protected
  reconciliation state.
- No duplicate close is submitted.
- Service health and Telegram intake recover with MiMo still on `v1`.
- Only after batch 119 is terminal may the paused MiMo isolated-replay workflow
  resume; v2 activation remains separately approved.
