# Trigger Leg Staged Take-Profit Design

## Goal

Ensure every filled split-position leg receives the strategy's complete staged
take-profit plan, while never weakening its verified stop-loss or recreating a
take-profit tranche already consumed by a partial exit.

## Root Cause

Deepcoin trigger-limit entries accept only one embedded take-profit. When such
an entry fills, the existing recovery path protects the stop-loss but does not
converge the filled leg to its full staged take-profit plan. Later stop-loss
management preserves that incomplete live protection set.

## Desired Protection State

Each active filled leg owns its own protection plan. For every leg, allocate
the strategy's TP percentages over that leg's live exchange quantity, rounded
down to the contract step with the remainder assigned deterministically. A
10-contract leg with 50/30/20 targets receives 5/3/2; a 7-contract leg receives
3/2/2. The stop-loss protects the entire remaining position independently.

## Reconciliation and Mutation Rules

After a trigger entry is authoritatively matched to a live `posId`, queue a
durable protection-convergence job. It runs only when the binding and entry leg
are active, ownership is authoritative, live size equals the frozen preflight
quantity, and all current protection rows are uniquely attributable.

For a TP-only repair, retain the existing verified stop-loss. Replace only the
take-profit rows required to converge to the target set, then reread the
exchange and persist returned order IDs in the protection ledger. A failure or
unknown submission outcome freezes the job for recovery rather than retrying a
write blindly.

## Partial Take-Profit Interaction

Partial-close execution has priority over TP convergence. First reserve and
submit the exact close; wait for confirmed remaining position sizes. Only then
compute a new protection target for each remaining leg.

- An explicit target hit consumes that target; it must not be recreated.
- An explicit new TP plan replaces the prior plan.
- A generic partial take-profit with no target is ambiguous: do not infer a
  consumed target. Preserve the price ladder only after policy determines how
  to resize it, otherwise freeze TP convergence for review.

The stop-loss remains in place throughout partial-close handling. If ownership,
message semantics, or live quantities are ambiguous, the system makes no
automatic protection mutation and raises an actionable alert.

## Current Incident

The currently live 10-contract leg has a verified 67200 stop and an incomplete
64500×10 TP. A future authorized repair should leave its stop untouched and
converge TP orders to 64500×5, 63800×3, and 63100×2 after final preflight and
explicit operator confirmation.
