# Bound Position Close Design

## Goal

Allow an operator to close exactly one project-bound Deepcoin position from its exchange-position card.

## Scope

Only cards with a verified `bound` attribution expose the action. Unassigned or merely candidate-attributed positions have no close control.

## Safety Model

The browser sends only a `pos_id`. The server reloads the live Deepcoin position and verifies that the position ID is attached to one active execution binding. It derives the contract, direction, margin mode, position mode, and full live size from those trusted records; client-supplied quantity or trading parameters are never accepted.

The server submits a market close using the exact `closePosId`. Before submission, the client displays a confirmation dialog naming the symbol, direction, full position ID, current size, and market full-close action. The server records the request/result as an execution event and returns a clear result. A later refresh/reconciliation determines the final exchange fill state.

## Failure Handling

Missing/ambiguous binding, missing/zero live size, or an absent live position fail closed without an order. Exchange submission errors are returned to the card and do not update local lifecycle state to closed.
