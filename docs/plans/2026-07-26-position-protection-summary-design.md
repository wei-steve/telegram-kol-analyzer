# Position Protection Summary Design

## Decision

The compact position summary must derive its stop-loss and take-profit fields
from the same verified exchange protection orders shown in the `止盈止损` list.
The legacy backup-stop ledger remains audit evidence only and must not override
the exchange snapshot in the current-position UI.

## Rules

- Only exact-position, verified protection orders participate.
- For a long position, sort stop prices descending: the first is `止损`, the
  second is `第二止损`; for a short position, sort ascending.
- For a long position, sort take-profit prices ascending; for a short position,
  sort descending.
- The summary shows every verified take-profit price in the chosen order.
- If no second verified stop exists, show `第二止损未设置` rather than a legacy
  submission state.
- The detailed list remains the authoritative per-order view and continues to
  display order id, size, and verification status.

## Example

For the current long position at entry `63894.1`, the verified orders are stops
at `61000` and `60878`, and a take profit at `67200`. The summary reads:
`止损 61000`, `第二止损 60878`, and `止盈 67200`.
