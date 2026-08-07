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

Permit a failed `manual_review` intent to re-enter only this stop-only rescue
lane when its immutable `protection_adoption_refused` audit proves a deferred
candidate (`predates_fill`, ambiguous, not-unique, or explicitly deferred).
The query may discover such rows, but the full preflight remains authoritative
and blocks every ineligible row.  Persist the actual refusal reason and bounded
evidence on future retries so the terminal state is diagnosable.

For the current position, deploy the fix only in a proven quiet window.  The
already-due intent will then be planned and executed by the existing durable,
idempotent rescue path.  Verify the 66160 primary stop by exact exchange
readback, then allow the existing backup-stop and staged-take-profit workers to
converge the remaining roles.  Stop and request separate authority if any
snapshot, position size, ownership, liquidation, or order identity changes.

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
retry can defer the intent, prove only evidence-backed terminal manual-review
intents are reconsidered, and prove unrelated manual-review rows stay blocked.
Production verification requires exact current-position readback, primary,
backup, and take-profit role ownership, zero pending repair actions, unchanged
position economics, healthy services, and a no-notify monitor diagnostic.
