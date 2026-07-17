# Partial-Close Protection Reconciliation Design

## Goal

Keep ordinary partial take-profit available when each target position is
uniquely owned, while preventing the batch from being reported as fully
successful until every remaining position has current, attributable protection
evidence.

## Problem

Deepcoin pending TPSL rows can omit `closePosId`.  A partial close can reduce a
split position while its pre-close TPSL rows retain their old quantities.  The
old path records the close as succeeded without a post-close protection check,
which leaves a live position observable but not safely mutable.

## Design

Partial close remains permitted after the existing exact entry-leg and
quantity preflight.  Its reconciliation performs a second, read-only protection
check against the same coherent exchange snapshot after every close leg is
confirmed:

1. Match each remaining `posId` by exact order position identity, position-row
   inline protection, persisted protection ledger, or the existing unique
   instrument/side/time/size matcher.
2. If all remaining positions have verified protection, preserve the normal
   `succeeded` result.
3. If any remaining position has absent or ambiguous protection, retain the
   confirmed close facts but place only that management batch in
   `recovery_required` with a fixed `partial_close_protection_unverified`
   reason.  It must not replay the close, cancel TPSL, add TPSL, or mutate a
   different position.

This is a local degradation: other strategies and independently verified
management actions continue.  The affected batch is visible for operator
review, rather than falsely claiming a fully reconciled partial take profit.

## Non-goals

- No automatic repair of already-live ambiguous TPSL rows.
- No order-ID adjacency, loose time-window, symbol-only, or cross-chat
  ownership inference.
- No production database edits or Deepcoin writes as part of verification.

## Verification

- A partial close with verified post-close protection stays `succeeded`.
- A partial close whose remaining position has stale-size/ambiguous protection
  becomes `recovery_required` after the close is confirmed.
- A full close and a `partial_then_break_even` composite retain their current
  behavior.
