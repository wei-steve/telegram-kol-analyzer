# Bounded Stop-Loss Widening Design

## Objective

Allow an explicitly targeted, verified live BTC or ETH position to follow a
bounded KOL instruction that widens its stop loss, while preserving the
existing fail-closed behavior for ambiguous targets and every other symbol.

## Policy

- Tightening a stop remains allowed under the existing rules.
- For BTC, a long stop may move down and a short stop may move up by at most
  700 USDT, inclusive.
- For ETH, a long stop may move down and a short stop may move up by at most
  21 USDT, inclusive.
- Stop widening for every other symbol remains forbidden.
- A widening instruction is allowed only when the message resolves to one
  exact lifecycle and that lifecycle has a unique, verified live Deepcoin
  position binding.
- Unscoped or multi-target stop widening remains forbidden even when each
  individual price delta would be within the symbol threshold.
- Missing, invalid, or non-positive stop prices remain forbidden.

The delta is the absolute price difference between the lifecycle's currently
recorded stop and the requested stop. The BTC and ETH thresholds are price
units, not position PnL or percentage values.

## Architecture

Keep the policy in `management_scope.py`, at the boundary that already checks
source identity, target ownership, and stop direction. Split stop validation
into two decisions:

1. Existing risk-reducing stop movements remain valid for exact and group
   scopes.
2. Risk-increasing stop movements may pass only through the exact-target path,
   after verified live ownership is established, and only when the symbol and
   delta satisfy the fixed threshold table.

The group fan-out path continues to call the risk-reduction-only predicate, so
the new exception cannot broaden multiple positions at once. No new setting,
database column, or exchange API behavior is required.

## Data Flow

1. Authoritative recognition identifies `position_update`, the requested stop,
   and an exact lifecycle ID.
2. Deterministic management scope validates source identity and resolves the
   lifecycle.
3. The resolver verifies a unique live Deepcoin binding and verified entry leg.
4. The bounded widening policy compares current and requested stops.
5. An accepted instruction creates the existing management candidate and batch;
   the existing executor performs and verifies the exchange protection update.
6. A rejected instruction remains fail-closed with a specific reason code that
   can be surfaced by the existing incident/notification path.

## Failure Handling

- Reject BTC deltas greater than 700 and ETH deltas greater than 21.
- Reject widening for all other symbols.
- Reject widening when exact ownership cannot be verified.
- Reject widening when the current stop is unavailable; this exception must not
  infer an unknown delta.
- Preserve the original stop when any validation fails.
- Use bounded, non-sensitive reason codes in persisted diagnostics.

## Verification

Test first at the management-scope boundary:

- BTC long down 700 and BTC short up 700 are accepted.
- BTC widening above 700 is rejected.
- ETH long down 21 and ETH short up 21 are accepted.
- ETH widening above 21 is rejected.
- Other-symbol widening is rejected.
- Exact target without verified live ownership is rejected for widening.
- Group fan-out widening remains rejected.
- Existing stop-tightening tests continue to pass.

Then run the focused recognition/planner/worker suites to confirm an accepted
instruction still reaches the existing durable management pipeline. Production
verification must use read-only inspection and the next natural message; no
historical message replay or synthetic exchange mutation is permitted.
