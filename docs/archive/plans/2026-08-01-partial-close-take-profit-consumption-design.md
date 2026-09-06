# Partial-Close Take-Profit Consumption Design

## Problem

When a live `partial_then_break_even` instruction closes part of a position, the
normal protection-replacement path currently recreates the old take-profit
sizes unchanged.  The production incident on Deepcoin position
`1001124499968070` reduced an eight-contract BTC short by four contracts, then
recreated take profits sized `4/2/2` against the four-contract remainder.  The
first target at 63800 therefore closed the entire remainder.

The lifecycle synchronizer subsequently labelled the position `manual` because
it observed that the exact position had disappeared without attributing the
exchange-generated reduce-only fill to a known take-profit order.

## Approved Behaviour

A message-driven partial close consumes the nearest outstanding take-profit
stages in order.  For the incident ladder `63800 x 4`, `63100 x 2`,
`62400 x 2`, closing four contracts consumes and removes the 63800 stage.  The
remaining position keeps `63100 x 2` and `62400 x 2`, plus the requested
break-even stop.

If a close crosses a stage boundary, complete stages are removed and the first
partially consumed stage is reduced.  The remaining take-profit order is
preserved.  A close that consumes every staged target leaves only the requested
stop protection.

## Architecture

The management executor will use one pure transformation for both the normal
and pre-cancel recovery paths.  It will normalize the authoritative protection
snapshot, consume `planned_close_size` from take-profit stages in their persisted
order, and resize full-position rows to the remaining exact position size.

Before any replacement write, a shared fail-closed validator will require:

- positive, finite, step-aligned take-profit sizes;
- no individual take-profit larger than the remaining position;
- aggregate take-profit size no larger than the remaining position;
- a valid persisted contract quantity step and minimum quantity.

Invalid or ambiguous protection will block the management batch before a new
TPSL is submitted.  Existing strategy recognition and contextual target
resolution remain unchanged.

For lifecycle attribution, exchange reconciliation will prefer exact evidence:
the vanished position's position history, reduce-only fills, and a known
take-profit ledger order/trigger.  When those facts prove a take-profit close,
the lifecycle records `take_profit`; otherwise it retains the existing
fail-closed manual classification.

## Data Flow

1. Resolve the management message to the existing entered strategy.
2. Snapshot the exact position and its verified protection rows.
3. Cancel deferred entries and old protection through the existing durable
   mutation boundaries.
4. Submit and confirm the requested partial close.
5. Consume the confirmed close quantity from the earliest take-profit stages.
6. Validate the replacement ladder against the exact remaining exchange size.
7. Submit the break-even stop and remaining take-profit stages.
8. On later position disappearance, reconcile exact exchange history and record
   a proven take-profit exit when available.

## Failure and Rollback Behaviour

The change is fail-closed.  Missing contract metadata, non-integral quantities,
an over-sized ladder, ambiguous exchange history, or an unconfirmed partial
close prevents replacement writes or attribution upgrades.  Disabling the
change is a code rollback to the preceding commit; no schema migration or data
rewrite is required.

Deployment must not occur during an active time-sensitive strategy operation.
Local work can complete without deployment if a safe window cannot be proven.

## Verification

Focused tests will cover the production `8 -> close 4 -> keep 2/2` case, partial
consumption across a stage, complete consumption, over-sized ladder rejection,
normal and recovery execution paths, and exact take-profit exit attribution.
The relevant management, reconciliation, and lifecycle suites will run locally.
After push, production verification will confirm the deployed SHA, active
service state, clean startup logs, and no active management batch before or
after restart.  No live trade will be submitted for testing.
