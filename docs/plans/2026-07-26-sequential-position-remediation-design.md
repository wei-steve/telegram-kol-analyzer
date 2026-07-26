# Sequential Position-Management Remediation Design

## Problem

The production dry run exposed seven missed management actions and 108
conflicts. The current remediation builder treats every failed instruction as
an independent action against one current exchange snapshot. That loses the
original message order and can propose mutually dependent actions at the same
time, such as break-even protection, then full exit, then another protection
change for the same strategy.

Historical conflicts from unrelated strategies also block every exact apply.
An entry cancellation that filled late can remain represented as unsupported
`cancel_entry` even though the safe current remediation is to close the exact
late-filled position.

## Required Semantics

Remediation replays missed instructions in original time order. It does not
collapse a strategy to its latest instruction.

Each `strategy_instance_id` owns an independent remediation chain. Chain order
is deterministic:

1. source message `posted_at`;
2. raw-message primary key;
3. instruction-item sequence;
4. signal-candidate primary key as a final tie-breaker.

Only the head action of a chain may be approved or applied. Later actions are
visible as waiting for their predecessor, but they are not executable and have
no reusable approval fingerprint.

After the head action executes, exchange reconciliation must reach a durable
outcome before the plan exposes the next action. The next plan is built from a
fresh database and exchange snapshot.

## Terminal Rules

A confirmed full exit terminates the old strategy lifecycle. Later messages
that still point to that lifecycle are recorded as terminally skipped and
cannot create exchange writes.

A later instruction may execute only when it belongs to a distinct, newly
entered lifecycle.

An entry-cancellation instruction that was missed and whose exact deferred
entry filled in the meantime becomes an exact full exit for the late-filled
position. This conversion requires verified binding, entry-leg, `posId`,
instrument, side, and live-position identity. It must never fan out by symbol
or side.

## Plan Shape

The dry run groups results by strategy chain and reports:

- chain identity and ordered source instructions;
- one `ready_for_approval` head action when safe;
- zero or more `waiting_for_predecessor` steps;
- chain-local conflicts;
- terminal/skipped steps with reasons;
- the coherent exchange snapshot fingerprint and deterministic chain
  fingerprint.

An action fingerprint covers the head step, its predecessor state, exact
binding and entry legs, exact live positions, relevant pending orders and
TPSL observations, and the shared instrument scope.

Waiting steps cannot be applied with a copied head fingerprint.

## Apply Flow

Apply rebuilds the complete plan from a fresh coherent snapshot, locates the
requested chain head, and verifies:

- the requested action is still the unique executable head;
- every predecessor is durably resolved;
- no earlier unresolved batch exists for the chain;
- source message and instruction ordering are unchanged;
- exact binding, entry legs, positions, economics, orders, and TPSL evidence
  match the confirmed fingerprint;
- the global live-management gate remains enabled at every exchange write.

Apply executes only that head through the normal planner, executor, and
reconciliation state machines. It never loops into the next historical step.
The operator must inspect the readback and approve a newly generated plan.

## Conflict Isolation

Conflicts freeze only their own strategy chain. They do not block an exact
head action belonging to another verified chain.

A conflict at or before a chain head prevents that chain from exposing an
executable action. A conflict in a later step remains visible but does not
invalidate an already safe predecessor.

Ambiguous target ownership, incomplete exchange evidence, risk-increasing
fan-out, unsupported protection direction, or an unresolved earlier batch
remain fail-closed.

## Production Rollout

1. Add production-derived regression fixtures for the observed ordered chains.
2. Verify a late-filled cancellation becomes an exact full exit.
3. Verify only the first unresolved step per chain is executable.
4. Verify confirmed full exit terminalizes all later old-lifecycle steps.
5. Verify a conflicted chain does not block an unrelated chain.
6. Run local focused and full tests plus independent review.
7. Push the reviewed commit.
8. On the server, deploy in shadow mode and replay the known production
   regressions.
9. Generate a production dry run and present one exact chain head at a time.
10. Apply only after explicit operator approval, reconcile, and regenerate the
    plan before considering the next step.

No production verification may use a synthetic live order.
