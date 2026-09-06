# Unattributed protection display design

## Problem

The first complete-order display treated every TPSL order without a `posId` as
a candidate for every same-instrument position. When several BTC long positions
are open, this duplicates the same orders across every card and incorrectly
suggests a per-position relationship.

## Chosen design

Each live position card displays only pending TPSL rows whose exchange `posId`
matches that card's `posId`. These rows are factual per-position exchange
orders and remain distinct from the conservative strategy-protection summary.

Every pending TPSL row without a matching live `posId` is retained once in a
separate `未归属交易所保护单` panel. The panel is grouped by instrument and
position side, lists each TP/SL side, price, size, and order ID, and labels the
entry `无法归属`. It contains no order-management actions.

## Safety and validation

No matching used by protection mutation or strategy attribution changes. Tests
will cover two same-side BTC positions plus one direct and one unscoped TPSL
row: the direct row appears on one card only, while the unscoped row appears
once in the separate panel.
