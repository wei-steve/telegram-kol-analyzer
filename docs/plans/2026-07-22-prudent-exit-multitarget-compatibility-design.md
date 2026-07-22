# Prudent Exit and Multi-target Compatibility Design

## Goal

When a KOL says a uniquely attributable position may be exited for prudence
(for example, “求稳可走”), submit a full exit for every verified live leg of
that strategy.  Preserve fail-closed handling for ambiguous or malformed
multi-strategy requests.

## Cause

The multi-target lifecycle feature added `targets: []` to the model's default
JSON example.  Its parser treats any present empty `targets` value as invalid,
even when the same payload contains a valid single `target_lifecycle_id`.
That caused DBK message 4068 to be rejected before the existing full-exit
executor could run.

## Design

1. Teach the lifecycle prompt that “求稳可走”, “稳健者可走”, and equivalent
   prudential-exit phrases are `exit_position` / full-exit instructions when
   they uniquely refer to an entered strategy.
2. Make the JSON contract unambiguous: omit `targets` for a single target;
   include it only as a non-empty list for an explicit multi-strategy action.
3. Keep the runtime defensive: when `targets` is an empty list *and* a valid
   scalar `target_lifecycle_id` exists, normalize to the existing single-target
   flow.  Empty `targets` without a valid scalar target remains rejected.
4. Reuse the current full-exit candidate, management-batch, and exact-leg
   executor.  A successful decision will close all verified legs that belong
   to the one resolved strategy; no broad symbol-level close is introduced.

## Safety Boundaries

- A multi-target list remains valid only when it is non-empty and every target
  has a distinct immutable lifecycle ID.
- An ambiguous “求稳可走” message with no uniquely resolvable lifecycle remains
  non-actionable.
- No historical message is replayed and no current position is changed as part
  of this deployment.  The rule applies only to future natural messages.

## Verification

Cover the accepted single-target-empty-list regression, the rejected
targetless-empty-list case, preserved non-empty multi-target fan-out, and the
full-exit candidate produced for a prudent-exit phrase.  Run the focused tests
locally, then perform the project-required server-only verification after a
reviewed deployment.
