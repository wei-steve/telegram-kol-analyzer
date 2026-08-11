# Take-Profit Protection-Leg Convergence Design

## Problem

An active, verified position has a verified exchange take-profit order in both
`position_take_profit_orders` and `position_protection_ledger`, while its
corresponding logical `position_protection_legs` row remains
`protection_recovery_pending`. The planned and submitted trigger prices are
numerically equal but use different text representations. The submit path uses
text equality to find the logical leg, so it records the exchange order in two
durable ledgers but misses the logical-leg binding. Reconciliation then calls an
otherwise idempotent position binder on every pass, which refreshes
`updated_at` even though no state changed. Deployment preflight consequently
sees permanently fresh protection work and fails closed.

## Chosen approach

Use exact decimal normalization for trigger-price identity and add a supervised,
exchange-read-only repair for already split durable truth. Do not weaken the
deployment gate and do not submit, cancel, or modify any exchange order.

The normal convergence writer will select a logical take-profit leg only when
the candidate is unique after exact `Decimal` normalization. Zero, malformed,
missing, or multiply matching prices fail closed. The position binder will
update `updated_at` only when `pos_id` or lifecycle state actually changes.

For historical repair, a dry-run planner will require one-to-one agreement
among all of the following:

- one pending logical take-profit leg;
- one active and verified entry leg with the same binding and `posId`;
- one active durable take-profit order with the same binding, leg, position,
  and numerically equal trigger price;
- one verified protection-ledger owner for that exact order and position;
- one exact pending exchange TPSL read-back for the order, instrument, side,
  position, price, and submitted size;
- no other logical protection leg owning the exchange order.

The planner emits a deterministic action fingerprint and confirmation token.
Apply mode rebuilds the live plan while holding the shared cross-process
position-authority lock, requires the selected action, fingerprint, and token
to match, then binds only the existing exchange order to the logical leg in one
database transaction. Re-running after success is a no-op.

## Alternatives rejected

1. Fix only the future string comparison and edit the current row manually.
   This leaves an unaudited production mutation and no reusable recovery path.
2. Ignore recently refreshed recovery rows in deployment preflight. This can
   hide a genuine in-flight protection write and weakens the safety boundary.

## Failure handling

Any ambiguity, missing ledger owner, stale exchange read-back, changed order,
ownership collision, or position state change produces a refusal and no local
or exchange mutation. The repair never treats price/side alone as ownership.
The existing deployment gate remains unchanged.

## Verification and rollout

Tests must first reproduce the formatting mismatch, the false timestamp churn,
and the current split-ledger shape. Focused tests cover exact repair, ambiguity,
stale exchange evidence, ownership collision, fingerprint drift, idempotence,
and absence of exchange writes. After full local review, push the exact commit,
run the repair dry-run and supervised apply on production, rerun the
`schema_compatible` deployment helper, and verify service health and dormant
execution-contract settings.
