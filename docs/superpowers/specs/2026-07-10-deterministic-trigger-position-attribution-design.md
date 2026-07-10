# Deterministic Trigger Position Attribution Design

## Goal

Make every DeepCoin trigger-entry position traceable to its originating
strategy and entry order leg. Do not infer ownership from symbol and price
when direct order evidence exists.

## Root Cause

DeepCoin creates a new regular-order ID when a conditional entry fills and
does not preserve the client order ID in its regular-order history. The
current reconciliation code tries to connect the new position to a trigger
order using an exact price tolerance. Normal execution slippage can exceed
that tolerance, leaving a position unbound.

The same reconciliation also assigns a single recovered position ID to every
entry leg in a binding. Separately, closing one filled leg can close a binding
even when another trigger entry leg remains pending. That pending leg is then
excluded from future reconciliation.

## Design

Execution order legs are the ownership boundary. A binding may contain several
entry legs, but each leg owns at most one position ID. Reconciliation matches a
live position to an entry leg using the exact trigger order ID, direction,
quantity, and trigger/fill time. Price is diagnostic evidence only after those
identity fields match.

When a binding already has a live position, reconciliation evaluates each
unbound entry leg independently and appends only its recovered position ID.
It updates only that leg, never all legs at once.

A binding with an active or pending entry leg remains eligible for
reconciliation even if a different leg's position was manually closed. The
strategy lifecycle is closed only after no active position and no live entry
order remains.

## Historical Repair

The existing periodic reconciliation will repair legacy bindings from DeepCoin
order history. It must revive a prematurely closed binding when an exact
trigger-order leg has produced a currently open position, then restore the
lifecycle to entered. No manual attribution is used for this repair.

## Tests

Regression tests cover: two trigger legs filling with prices outside the old
tolerance; preserving distinct position IDs per leg; and recovering an entry
leg after its sibling binding was prematurely closed.
