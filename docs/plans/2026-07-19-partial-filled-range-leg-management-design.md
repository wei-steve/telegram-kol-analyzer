# Partial Filled Range-Leg Management Design

## Context

Range entries are commonly split into two Deepcoin entry legs: one market leg and one trigger/limit leg. A strategy can therefore be partially entered for hours while the second leg remains pending. The current management planner treats every entry leg under the binding as required ownership evidence. If one leg is still pending and has no `pos_id`, management actions such as "take profit 50%, move remaining stop to breakeven" are blocked before any close plan is created.

## Approved Approach

Use the verified live entry legs as the management target set. Pending, unassigned entry legs remain visible and untouched, but they no longer block management of already-owned live positions. Conflicted, unavailable, terminal, duplicated, or mismatched verified position ownership still blocks fail-closed.

For a `partial_then_break_even` message, the planner creates close legs only for verified live positions. After close reconciliation, the existing protection phase adjusts only those managed positions. Pending entry orders are not cancelled, recreated, or repriced automatically.

## Pending Entry Review

The existing pending-entry expiry review flow is reused and tightened to 3 hours. It must also cover an `entered` lifecycle whose binding has unresolved pending entry legs. When an entry leg remains pending longer than 3 hours, the system operator bot asks the user whether to keep waiting, request cancellation follow-up, or preserve the pending exchange order.

The notification is review-only. It does not cancel, replace, or submit any exchange order without an explicit operator callback or command.

For an already `entered` lifecycle, operator callbacks must never mark the whole
strategy `expired` because a verified live position exists. Continuing waiting
only updates the review state; cancel/keep decisions preserve the entered
lifecycle and require pending-leg-specific follow-up before exchange mutation.

## Safety Rules

- Never manage a position unless its entry leg is uniquely `verified` and has a live `pos_id`.
- Never infer ownership from symbol, side, group label, or pending order similarity.
- Do not mutate pending entry orders from a position-management message.
- If all entry legs are pending or no verified live leg exists, keep the existing blocked behavior.
- If any verified live leg has attribution conflict, unavailable evidence, terminal state, duplicate `pos_id`, or binding mismatch, block the batch.
- Only entry legs with no `pos_id` and nonterminal `open`/`pending`/`submitted`
  status may be treated as deferred pending legs. Partially filled legs or
  unverified legs with a `pos_id` are not safe to skip.

## Testing

Add focused tests for:

- A two-leg range strategy where one verified live leg and one pending unassigned leg receives `partial_then_break_even`; the planner creates a ready batch with one management leg for the verified `pos_id`.
- A strategy with only pending/unassigned legs remains blocked.
- The executor accepts a batch whose management-leg target set is a verified subset of the binding's entry legs.
- Lifecycle monitor sends a 3-hour review notification for an entered lifecycle with an unresolved pending entry leg.
