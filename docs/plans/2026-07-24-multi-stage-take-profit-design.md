# Four- and Five-Stage Take-Profit Design

## Goal

Execute every explicit take-profit target in a verified Deepcoin strategy message, up to five targets, while retaining exact position ownership, primary stop-loss protection, and the independent backup stop.

## Scope

- Support one through five take-profit targets in every entry path: trigger-limit, single market, and hybrid range entry.
- Preserve all source targets in the immutable execution draft and trigger take-profit convergence record.
- Build exact-position TP orders only after a position is verified by `posId`.
- Keep the existing three-target configuration behavior. Add deterministic defaults for four and five targets when no matching custom allocation list is configured.

Out of scope: inventing a target for source messages that say a target is pending or absent; rewriting a currently live position unless an explicit management/replacement operation is requested.

## Allocation Rules

Target prices are ordered nearest-to-farthest in the direction of the intended exit:

- Long: ascending prices.
- Short: descending prices.

When the configured allocation list has exactly the same number of positive entries as the target count, normalize and use it. Otherwise use:

| Target count | Allocation |
| --- | --- |
| 1 | 100% |
| 2 | 50% / 50% |
| 3 | Existing configured three-stage allocation |
| 4 | 40% / 20% / 20% / 20% |
| 5 | 40% / 15% / 15% / 15% / 15% |

Quantities are split with `Decimal` and the verified contract `quantity_step`. Every non-final leg rounds down to the step; the final leg receives the exact remaining quantity. The plan is invalid if any stage would be below the minimum quantity or if the step-rounded legs do not sum exactly to the live position quantity. The executor must fail closed rather than remove targets, merge targets, or submit a partial TP plan.

## Architecture

Introduce a single pure multi-stage TP planner used by the order draft builder and the exact-position convergence executor. The planner receives normalized prices, side, requested allocation configuration, contract step/minimum, and (when available) the exact live quantity. It returns all ordered target legs or a specific validation error.

`deepcoin_order_builder` uses the planner for the immutable `take_profit_legs` draft and must remove the current three-target truncation. Trigger entry and first market legs continue to submit only their primary stop embedded with entry; once an exact split `posId` exists, the convergence executor creates one TP order per planned target. This avoids attaching an ambiguous multi-target payload to an entry order.

The convergence executor uses the same quantity planner rather than its integer-only local splitter. It persists every submitted TP order in `PositionTakeProfitOrder`, then reconciliation verifies the complete plan by order ID, target price, and allocated quantity.

## Safe Replacement and Failure Handling

For a future explicit replacement of an existing TP set, the position-management path must:

1. read the exact position and pending protection orders;
2. verify an active primary stop and backup stop (or create and re-read the primary stop before proceeding);
3. reserve the replacement intent durably;
4. cancel only verified old TP rows, never entry orders or a stop-bearing combined order without first replacing its stop;
5. create each new TP row and persist its exchange order ID immediately;
6. re-read pending orders and mark the plan complete only if all target prices and quantities match.

An unknown exchange outcome, a changed position size, an ownership mismatch, a missing protection order, or a rejected TP freezes the plan and raises an actionable incident. It must not retry blindly or submit a second complete TP set.

## User Interface and Observability

The trading settings form accepts any comma-separated positive allocation sequence of one through five entries. The preview shows all parsed target prices, effective allocations, and quantity rounding. Strategy detail and position views display all planned and observed targets, including blocked/frozen reason codes.

## Verification

Unit coverage must include long and short ordering, four/five-target defaults, matching custom allocations, Decimal quantity allocation for BTC and ETH steps, undersized-position rejection, and no-three-target truncation. Integration coverage must assert that exact-position convergence creates and records four and five TP orders, verifies every order after a read-back, and freezes without duplicate writes when a submission response is uncertain.

Production rollout is read-only first: inspect draft/position/TP-plan parity, deploy, and observe new trigger entries only. Existing real positions are never changed by the rollout; they require a separately authorized exact-position management plan.
