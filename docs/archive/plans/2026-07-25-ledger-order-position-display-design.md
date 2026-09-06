# Ledger-backed order-position display design

## Goal

Display pending DeepCoin TPSL orders on the exact live position they protect,
even when the exchange pending-order response omits `posId`.

## Rule

For each pending TPSL row, resolve ownership in this order:

1. A live exchange `posId` is an exact position match.
2. Otherwise, a verified local protection ledger or active take-profit order
   with the same exchange order ID supplies an exact `posId` match.
3. If neither source supplies a live position, retain the row once in the
   unattributed summary.

The local ledger is accepted only for a verified active entry leg bound to the
same live position. This is a display-only mapping and does not change order
mutation or strategy-attribution decisions.

## Validation

Tests will show an unscoped TPSL order with a verified ledger order ID on its
correct card, absent from the unattributed summary; an unknown order remains
in that summary.
