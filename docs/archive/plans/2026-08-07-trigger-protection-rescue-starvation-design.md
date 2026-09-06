# Trigger Protection Rescue Starvation Design

## Problem

An exact BTC short position created from trigger-entry message 1153 has no
verified stop or take-profit order.  Its saved entry request contains the
intended stop at 66160 and take profit at 63250.  The exchange and ownership
preflight can prove that a stop-only rescue is safe, but intent 105 exhausted
five adoption retries and became `failed/manual_review` without ever creating a
`TriggerProtectionStopRescue` row.

The failure is deterministic.  Reconciliation processes a due adoption intent
first and moves `next_attempt_at` into the future.  The rescue worker runs only
afterward and selects only intents that are still due, so it cannot see the
same intent.  At the final retry, the intent becomes `manual_review`; the rescue
worker excludes that disposition even when immutable refusal evidence says the
only candidate predated the position fill.  The prior intent 104 followed the
same exhaustion path, and production has never created a rescue row.

## Selected approach

Run the exact-position rescue tick before reconciliation reschedules due
intents.  Preserve the existing strict rescue preflight: verified binding and
leg ownership, one exact live split position, unchanged size, no close,
management, or position mutation in flight, no current exchange stop or opaque
take profit, saved parent request stop, and liquidation-safe price.

Keep failed `manual_review` intents quarantined.  Reopening them in the periodic
worker would repeatedly rediscover closed historical positions and could starve
newer due work at the bounded query limit.  Persist the actual refusal reason
and bounded evidence on future retries so a terminal state remains diagnosable.

For the current position, deploy the fix only in a proven quiet window if it is
still live.  Before deployment it closed through its normal management path,
so no protection write is permitted or needed.  Its terminal intent remains
immutable evidence; the ordering fix applies to future due intents before they
can exhaust into manual review.

## Alternatives rejected

- Waiting for another retry cannot work: intent 105 is already terminal and
  the current worker excludes it.
- Direct exchange submission would bypass the durable reservation, exact
  position write gate, ownership ledger, and idempotency boundary.
- Editing the intent row by hand would erase the terminal evidence and still
  leave the ordering defect for future positions.

## Safety and rollback

The change does not alter strategy targeting or message recognition.  It can
only submit a stop through the existing stop-only executor and exact-position
write gateway.  Every planned rescue is unique per trigger intent and becomes
durably reserved before the exchange call.  Disabling
`position_management_liveness_v2_mode` remains the kill switch.  Rollback is
safe after there are no `ready`, `reserved`, `submit_unknown`, or
`recovery_required` rescue rows.

## Verification

Tests must reproduce the pre-fix starvation order, prove rescue runs before a
retry can defer the intent, prove terminal manual-review intents remain
quarantined, and prove refusal diagnostics are persisted.
Production verification requires exact current-position readback, primary,
backup, and take-profit role ownership, zero pending repair actions, unchanged
position economics, healthy services, and a no-notify monitor diagnostic.
