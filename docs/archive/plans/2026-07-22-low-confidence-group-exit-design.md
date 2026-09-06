# Low-Confidence Group Exit Design

## Goal

Treat a KOL's optional, low-confidence exit guidance as a protective partial
exit, rather than silently ignoring it.  A matching future message closes 50%
of every exact live leg belonging to each eligible strategy.

## Operator-approved semantics

- Scope is strictly one Telegram chat.  No strategy in another group may be
  selected.
- The message must state a direction, such as `多单` or `空单`.
- If it names BTC or ETH, it targets only that symbol and direction.  If it
  names no symbol, it may target both BTC and ETH active strategies in that
  chat with the stated direction.
- Every selected strategy must be entered/holding, have a verified Deepcoin
  binding, and have at least one exact live position.  Multiple strategies in
  the same chat and direction are intentional targets.
- A selected strategy can have multiple split-position legs.  Each exact live
  leg receives a 50% close based on its own live available size and contract
  step.

The policy is for cautious, discretionary exit phrases such as `求稳可走`,
`可以先平仓`, and `可以先平加仓或者平仓等新机会`.  It is not a broad match for
market commentary, teaching material, historical performance, or a future
conditional trade setup.

## Design

1. Add a deterministic cautious-exit classifier after authoritative message
   recognition.  It requires both an exit verb and a discretion/risk-reduction
   marker.  It extracts direction and, when stated, BTC/ETH symbols.
2. Resolve targets from the source chat only.  For an omitted symbol, fan out
   to BTC and ETH strategies with the extracted direction; for an explicit
   symbol, use that one symbol.  Revalidate every lifecycle, binding, and live
   position before persisting a target.
3. Persist one `position_update` / `partial_take_profit` candidate per target
   lifecycle with `management_fraction=0.5`.  Reuse the current multi-target
   instruction fan-out, durable management batches, exact-leg preflight,
   idempotency, and reconciliation pipeline.
4. The executor closes 50% of each verified live split leg independently.
   It preserves the remaining exposure and protection according to the
   existing partial-close reconciliation rules.  A leg that cannot safely be
   rounded or verified is blocked and recorded; it never causes a broad
   symbol-level close.
5. Persist an operator-visible decision explaining the matched phrase,
   extracted direction/symbol scope, selected lifecycle IDs, and all rejected
   candidates.  Repeated delivery of the same raw message remains idempotent.

## Safety boundaries

- A message with no explicit long/short direction remains non-actionable.
- No target is inferred outside the originating chat, and no unverified or
  stale lifecycle is included.
- Lack of an explicit coin is permitted only for BTC/ETH and only with an
  explicit direction; it does not extend to other instruments.
- Candidate, batch, and leg failures are isolated per strategy.  A safe BTC
  target may progress if an ETH target is blocked, but no strategy may submit a
  close with incomplete ownership or exchange evidence.
- Historical messages, including the previously ignored Andy message 4070,
  are never replayed after deployment.

## Verification

1. The phrase `空单解套的人就可以先平加仓或者平仓等新机会` in one chat fans out
   to every verified active BTC and ETH short strategy in that chat, with a
   0.5 fraction per strategy.
2. A strategy with two live legs submits one independently rounded 50% close
   per leg.
3. Explicit BTC wording excludes ETH; explicit ETH wording excludes BTC.
4. Cross-chat strategies, missing direction, no live verified position,
   commentary-only text, and duplicate delivery submit no close.
5. A blocked leg leaves its own batch auditable and does not create a broad
   close; unaffected target strategies continue independently.
6. Run focused local tests, then deploy through the prescribed server update
   path and verify a future natural message through durable decision, batch,
   leg, and exchange evidence.  Do not replay a historical live message.
