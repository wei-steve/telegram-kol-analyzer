# Batch119 Empty Legacy Position Snapshot Design

## Problem

The production Batch119 management leg contains a closed legacy snapshot with
zero `position_rows` and zero `matching_regular_orders`.  The recovery planner
currently requires exactly one legacy position row, so it refuses before the
fresh exchange capture can prove either a live position or an exact natural
stop.  This makes stale local evidence stronger than the fresh read-only
authority that is intended to decide recovery.

## Approved boundary

The legacy snapshot remains a closed, durable input.  It may contain either:

- one exact position row matching the existing position, instrument, side, and
  trusted-size checks; or
- an empty `position_rows` list.

Both forms still require an exact top-level schema and an empty
`matching_regular_orders` list.  Multiple rows, additional fields, an
unexpected regular order, and every identity or size mismatch remain refused.

An empty legacy snapshot is admission evidence only.  It does not prove that a
position is terminal and does not authorize apply.  The existing fresh
Deepcoin capture must still prove a live position or provide the complete
position-history and trigger-history authority required by the natural-stop
path.  Apply continues to revalidate the same durable source and fresh sealed
evidence inside its existing transaction boundary.

## Minimal implementation

Change only `_legacy_false_exchange_snapshot_refusal()` so that the exact empty
position list is accepted without trying to validate a position row.  Preserve
all other checks and do not change the ordinary deployment gate, joint writer
inventory, database schema, exchange client, or apply functions.

## Verification

Add regression tests proving:

- empty legacy positions plus zero regular orders reach the fresh planner;
- a fresh live position is still classified through the normal live path;
- a fresh absent position still needs complete natural-stop history;
- empty legacy positions with incomplete fresh absence evidence are refused;
- multiple legacy rows, unexpected regular orders, extra fields, and existing
  row identity/size drift remain refused;
- joint admission becomes ready only when all other joint material is valid.

