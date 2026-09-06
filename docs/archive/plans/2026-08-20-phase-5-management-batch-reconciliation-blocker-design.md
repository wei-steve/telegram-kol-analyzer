# Phase 5 Management Batch Reconciliation Blocker Design

## Context

Phase 5 queue cutover is blocked by production management batch 119. The batch
was planned on 2026-08-12 and remains `reconciling` with one `submitted` leg,
but the leg has no client order id, exchange order id, request, or response.
There is also no close execution event or position-mutation intent for the
managed position.

The production journal records the original failure:

```text
strategy management batch 119 failed
ValueError: composite_batch_not_executable:reconciling
```

The state is produced by a race. A worker claims a ready batch as `executing`,
but before it durably reserves an individual leg, the independent reconciler
can observe the unchanged position. The reconciler currently treats
`current == preflight` as `submitted` even when the leg is still `planned` and
has no submission identity, then changes the parent to `reconciling`. The
worker subsequently refuses the no-longer-executable parent. Reconciliation
then preserves the identityless submitted state forever.

## Decision

Use a fail-closed state-machine repair plus the existing fingerprint-gated
management-history recovery command. Do not edit production rows manually and
do not weaken the Phase 5 quiet-window gate.

## State-machine repair

An `executing` batch whose close legs are all still `planned` has durable proof
that no per-leg reservation completed. Reconciliation must leave that batch and
its legs unchanged so the management worker can use its existing all-planned
restart path.

A `submitted` or `partial` close leg with neither a client order id nor an
exchange order id is an impossible durable state under the current executor.
Reconciliation must freeze it as `recovery_required` with the explicit reason
`management_close_submission_identity_missing`. It must not retry, infer an
exchange result, or keep rewriting `updated_at`.

These changes submit no exchange request and do not change recognition,
strategy selection, sizing, or order execution semantics. They only prevent an
unsubmitted leg from being misclassified as submitted and make an impossible
legacy state visible and stable.

## Operator recovery

Extend `recover-management-history` to classify the new frozen state as
`terminal_no_submission` only when all of the following are true:

- the exchange snapshot is complete;
- durable binding and position ownership identity are exact;
- every affected leg lacks client and exchange order ids;
- every affected leg lacks request and response payloads;
- no close `PositionMutationIntent` exists for the legs;
- no management-close `ExecutionEvent` exists for the positions.

Any missing or conflicting evidence refuses the plan. A successful dry-run
produces the existing evidence fingerprint. Apply remains an explicit,
fingerprint-checked CAS operation. It marks the unsubmitted leg failed, marks
the batch resolved with `history_no_submission_confirmed`, and writes one
auditable execution event. It does not change execution bindings, lifecycle
state, positions, orders, or TPSL.

## Testing

Tests must first reproduce the current race and legacy state, then prove:

- all-planned `executing` batches remain executable and unchanged;
- identityless submitted legs freeze instead of reconciling forever;
- recovery refuses incomplete exchange snapshots;
- recovery refuses any client/order identity, request/response payload,
  mutation intent, or close execution event;
- exact zero-submission evidence yields a stable dry-run fingerprint;
- apply is CAS-protected, idempotent, and changes only the intended batch, leg,
  and audit event;
- existing exact-order and position-history recovery paths remain unchanged.

Run focused tests, the management test group, the event-loop blocking census,
and the full local suite.

## Production procedure

1. Keep `message_lock_mode=global` and `message_pipeline_mode=shadow`.
2. Capture fresh direct Deepcoin positions, regular orders, pending triggers,
   and TPSL ids plus the active-write and active-management-batch snapshot.
3. Push the exact reviewed commit and deploy only through the gated updater
   with `EXPECTED_COMMIT` set to the remote branch tip.
4. Verify batch 119 freezes with the new explicit reason and no exchange state
   changes.
5. Create a consistent SQLite online backup and a separate rehearsal copy.
6. Run recovery dry-run, then apply the exact fingerprint to the rehearsal copy.
   Preserve quick-check, table counts, and row-level before/after evidence.
7. Re-run production dry-run. Only if the evidence is still complete and exact,
   apply its fingerprint to production once.
8. Verify batch 119 is resolved, the audit event exists once, the service is
   healthy, and positions/orders/TPSL are unchanged.
9. Re-evaluate the Phase 5 quiet-window gate. Queue remains off unless every
   Phase 5 gate passes.

## Rollback and failure handling

Before apply, rollback is the normal code revert and gated redeploy. After a
successful evidence-gated apply, do not blindly restore the stale
`reconciling` state: the resolution is an audited fact that no submission
occurred. Preserve the pre-apply SQLite backup for forensic recovery.

If any exchange query, database check, fingerprint comparison, safe-window
gate, rehearsal, or post-apply verification is incomplete, stop with Phase 5
`in_progress`, leave queue disabled, and record the exact missing evidence.
