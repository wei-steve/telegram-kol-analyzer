# Trigger Entry Protection Design

## Goal

Ensure every filled Deepcoin trigger-entry leg receives the source strategy's
take-profit protection after the exact split-position ID is verified, while
preserving fail-closed ownership checks. Update range-entry prices to use the
side-aware range endpoints adjusted by the configured market-deviation
percentage rather than the range midpoint.

## Confirmed behavior

- The production deviation is `0.15%`.
- For a range `low-high`, non-market long legs are `high * (1 + deviation)`
  then `low * (1 + deviation)`; short legs are `low * (1 - deviation)` then
  `high * (1 - deviation)`. Contract price ticks normalize both values.
- If the first range leg is eligible for the existing hybrid market path, it
  remains a market leg. The second limit leg uses the adjusted opposite range
  endpoint instead of the midpoint.
- Trigger submissions initially carry only the stop. A later, exact-position
  convergence creates TP-only protection: one target uses 100% of the leg;
  multiple targets use the configured allocation percentages.

## Root cause

The trigger payload deliberately omits take profit until a split `posId` is
known. Only multi-target triggers create a convergence row. Single-target
triggers consequently have no post-fill TP path. For multi-target triggers,
the executor requires the exchange pending-stop row to contain `posId`, while
the verified protection ledger can safely establish the exact ownership via
the trigger parent order when Deepcoin omits that field. This produces
`convergence_verified_stop_missing` despite a verified stop ledger row.

## Design

Use the existing `TriggerTakeProfitConvergence` as the one post-fill TP queue
for every trigger entry with at least one valid target. It remains immutable,
is released only after exact position reconciliation, and submits TP-only
orders. Its target normalization accepts one target only when its allocation
is exactly 100%; multi-target behavior remains unchanged.

The executor keeps the exact live-position, active verified entry-leg, side,
and no-existing-TP checks. It accepts a stop when either (a) the pending order
contains the exact position ID and agrees with the verified ledger, or (b) the
verified ledger's immutable adoption evidence links the stop to the same
trigger parent order. It does not adopt or cancel an opaque order and does
not relax any other ownership check.

Historical rows remain untouched. A later read-only audit may produce an
operator-reviewed exact repair plan; this change does not auto-reactivate
conflicted production convergences or send a TP for an existing position.

## Verification

Unit tests cover side-aware range prices and hybrid second legs, one-target
post-fill convergence, parent-intent backed stop verification without a
pending `posId`, and the existing duplicate/ambiguous TP refusals. Focused
tests and the full local suite run before commit. Production verification is
read-only first; no historical TP is submitted without a separate reviewed
plan and operator approval.
