# Entry and Take-Profit Position Design

## Goal

Keep entry splitting and take-profit staging as separate concepts. A KOL entry range may create multiple entry legs, but multiple take-profit targets must not multiply entry positions.

## Current Problem

The Deepcoin live submission path expands each limit entry leg into one child order per take-profit target. For a hybrid entry with one market leg and one limit leg plus three take-profit targets, the system submits four entry orders and later tracks four positions. This makes ad-hoc KOL management ambiguous because a temporary partial take-profit or full exit is strategy-level intent, not an instruction to operate on a take-profit child position.

## Desired Behavior

- Single-price or market entry submits one entry leg.
- Range entry submits at most the entry legs produced by the order draft, normally two legs.
- Multi-stage take-profit prices remain a protection or exit plan attached to the position, not extra entry orders.
- Full-exit KOL messages close every active position bound to the matched strategy.
- Partial take-profit KOL messages reduce the matched strategy's active positions by the requested fraction, rather than selecting a take-profit child position.

## Implementation Notes

- Remove the live-submit expansion that splits limit entry legs by `take_profit_legs`.
- Keep existing trigger-order embedded TP/SL fields for limit entries by using the nearest take-profit target when Deepcoin accepts only one attached TP on a trigger order.
- Keep existing market-position protection behavior, including multi-TP protection payloads for already-open market positions.
- Preserve existing binding and order-leg persistence shape: one `ExecutionOrderLeg` row per submitted entry order.

## Verification

- Update recovery live submit tests so a two-leg limit draft submits two trigger orders, not four.
- Update auto-trade tests so hybrid entry submits one market order and one trigger order, not one market plus three triggers.
- Keep full-close and partial-close tests proving management messages can operate on all bound positions.
