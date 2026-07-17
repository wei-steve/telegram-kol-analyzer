# Preserve Verified Position After Partial Close Design

## Goal

Keep a previously verified Deepcoin `posId` owned by the same entry leg after a confirmed partial close reduces the live position size.

## Background

The read-only production audit on 2026-07-17 found two live BTC positions for the same strategy still present on Deepcoin, but their `execution_order_legs` rows had moved from `verified` to `attribution_conflict` after a confirmed partial close. The reconciliation matcher currently requires the original entry requested size to equal the current live position size before it accepts any edge, including an exact persisted `posId` edge. That is too strict after a real partial close.

## Design

The reconciliation matcher should preserve direct authoritative identity before comparing mutable economics. If a leg already has an authoritative persisted position (`order_id == pos_id`, response `posId`, policy-v2 attribution evidence, reviewed equivalent assignment, or manual bind evidence) and the live exchange row has the same exact `posId`, matching instrument, and matching side, reconciliation should accept the edge even when the current size is smaller than the original entry size.

This rule does not create new ownership. It only preserves an already authoritative exact `posId` owner. Weak legacy verified rows that lack authoritative evidence remain unsafe and must still become `attribution_conflict`. A different `posId`, symbol, or side remains a conflict.

If an earlier buggy reconcile has already demoted a leg to `attribution_conflict`, a later reconcile may recover it only when the append-only `position_attribution_audits` table contains a prior `ownership_verified -> verified` audit for the same `execution_order_leg_id` and exact `posId` under the current attribution policy. This recovery path is still exact-position only and remains subject to the global one-owner check.

## Out Of Scope

This change does not loosen TPSL mutation safety. Deepcoin pending TPSL rows that lack `closePosId` or exact `posId` remain fail-closed when the planner cannot uniquely prove which position's protection would be cancelled or replaced. It also does not submit compensation trades or directly edit production SQLite.

## Verification

Add tests proving:

- A previously authoritative verified leg stays verified when the same live `posId` has a reduced size after partial close.
- A previously authoritative leg that was already demoted to conflict can recover only from matching prior verified audit evidence.
- A weak legacy verified leg with the same `posId` and changed size is not grandfathered.
- Existing exact-position and conflict tests still pass.

Production verification is read-only except for the reviewed deployment itself: deploy the code, allow or run reconciliation through the existing service path, then verify the affected positions regain verified ownership without any new Deepcoin write.
