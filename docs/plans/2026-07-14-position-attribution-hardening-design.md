# Deterministic Position Attribution Hardening Design

## Goal

Make every Deepcoin position traceable to exactly one originating execution entry leg, preserve manual exchange actions as terminal facts, and fail closed whenever ownership or protection cannot be proven uniquely.

This design replaces symbol/side fallback attribution with an evidence-backed state machine. It also fixes Web stop-loss display when Deepcoin returns position and TPSL records with slightly different timestamps.

## Production Incident

On 2026-07-14, Deepcoin had two live ETH short positions. The Web console displayed one as belonging to 三马哥 and one as belonging to 智哥. Read-only production inspection established that both positions originated from 智哥:

- `pos_id=1001124083084014` came from 智哥's market entry leg.
- `pos_id=1001124083099498` came from 智哥's second trigger entry leg.

The second position was assigned to 三马哥 because:

1. A prior 三马哥 trigger order had been manually cancelled on the exchange.
2. Its local execution order leg remained `open`, and the binding remained eligible for recovery.
3. Reconciliation processed the older binding first.
4. Exact evidence recovery did not produce a unique result because both live ETH short positions had equal size, close prices, and timestamps within the same broad tolerance.
5. The fallback that selects the only remaining same-symbol, same-side position allowed the stale binding to claim 智哥's new position.

The same production snapshot proved that both live positions had stop loss `1820`, but one Web card omitted it. The position was created at `00:27:56`, while its full-position TPSL row was created at `00:27:57`. The current display matcher requires exact timestamp equality, so it did not associate the stop-loss row with the position.

## Confirmed Safety Rules

The user confirmed these rules:

1. A manual Deepcoin close or cancellation permanently terminates the corresponding local position or entry leg.
2. An old Telegram message must never recreate a manually terminated position or order.
3. Only a later, independent Telegram entry message may create a new strategy instance and new order legs.
4. If order IDs, entry legs, fills, and other evidence cannot establish unique ownership, the position remains unassigned or conflicted.
5. An unassigned or conflicted position must not be automatically closed, reduced, rebound, or have its TP/SL changed.
6. Symbol, side, price proximity, or a single remaining position are never sufficient ownership evidence.

## Ownership Model

`strategy_instance_id` identifies one independent Telegram entry strategy. One execution binding may contain multiple entry legs. Each entry leg owns at most one live Deepcoin position, and each Deepcoin `(venue, pos_id)` may be owned by at most one entry leg.

The execution order leg is the ownership boundary:

```text
Telegram entry message
  -> strategy_instance_id
  -> execution binding
  -> entry leg 1 -> order evidence -> pos_id A
  -> entry leg 2 -> order evidence -> pos_id B
```

Binding and lifecycle state are derived from their legs. A binding or lifecycle must never assign a position to a leg by inference in the opposite direction.

Add a database uniqueness rule for non-null `(venue, pos_id)` ownership. Because `venue` currently lives on the binding, implementation may either add `venue` to the leg or enforce the constraint through a dedicated position-ownership table. The implementation plan must choose the smallest migration that provides a real database constraint and an auditable ownership record.

## Entry-Leg State Machine

Canonical entry-leg states:

```text
planned
  -> submitted
  -> pending
  -> partially_filled
  -> filled
  -> closed
```

Terminal and exceptional transitions:

```text
pending -> exchange_cancelled
pending -> manually_cancelled
filled  -> manually_closed
any nonterminal state -> attribution_conflict
```

`exchange_cancelled`, `manually_cancelled`, `manually_closed`, and `closed` are terminal. Periodic recovery must not reopen them. If a new Telegram message creates another entry after a terminal state, it receives a new `strategy_instance_id` and new leg rows.

When a history row is cancelled but no project-generated cancellation event exists, reconciliation records `manually_cancelled`. When a matching project cancellation event exists, it records `exchange_cancelled`. The distinction is evidence-based and audit logged.

## Reconciliation Inputs

Each reconcile pass loads one coherent read-only evidence snapshot before changing local state:

- current positions;
- pending regular orders;
- regular order history;
- pending trigger and TPSL orders;
- trigger and TPSL history;
- fills;
- local execution bindings;
- local execution order legs;
- immutable execution events;
- prior verified ownership records.

API failures are recorded per evidence source. An unavailable source does not mean that an order was cancelled or a position was manually closed.

## Reconciliation Phases

### Phase 1: Refresh Exact Order and Leg State

For every known leg, look up exact `ordId` and deterministic `clOrdId` in pending and historical data.

- Pending order found: leg remains `pending`.
- Fill evidence found: leg becomes `partially_filled` or `filled` as supported by the exchange data.
- Cancel history found: leg becomes `manually_cancelled` or `exchange_cancelled` based on the execution-event ledger.
- Evidence API unavailable: preserve the prior trusted state and mark the reconcile evidence source unavailable.
- Terminal leg: exclude it from all position-recovery candidates.

Cancelled trigger history must not count as fill evidence merely because it contains `sz`, price, or time fields. Fill evidence requires an explicit filled/partial state or a fill record from the fills endpoint.

### Phase 2: Compute Position Ownership Globally

Do not reconcile bindings sequentially. Build all eligible leg-to-position candidate edges first, then solve ownership as a global one-to-one assignment.

Evidence priority:

1. A position ID returned directly in the entry response.
2. An exact regular order ID or deterministic client order ID that identifies the resulting position.
3. A unique trigger-to-fill relationship established by the originating leg, exact symbol and direction, compatible quantity, and nearest unique fill time.
4. Price is diagnostic evidence after identity, quantity, and time; it cannot establish ownership alone.

Time comparison uses actual distance. Exact time outranks one second, and one second outranks sixty-nine seconds. A broad binary window must not give equal scores to materially different time distances.

If the highest-confidence result is tied, incomplete, or contradicted, do not write `pos_id`. Mark the leg and affected position as `attribution_conflict` and notify the operator.

Remove the current same-symbol, same-side, single-remaining-position fallback completely.

Existing verified ownership remains stable while its exact live position remains present. Stronger contradictory evidence creates a conflict; reconciliation must not silently move the position to another strategy. Historical correction uses the explicit audit-and-repair workflow described below.

### Phase 3: Derive Binding and Lifecycle State

- At least one leg has an active position: binding `active`, lifecycle `entered`.
- No active position, but at least one leg is pending: binding `open`, lifecycle `pending_entry`.
- All legs cancelled and no active position: binding `closed`, lifecycle `cancelled`.
- All positions manually closed and no pending legs: binding `closed`, lifecycle `exited` with manual reason.
- Active positions plus pending legs: binding remains `active`; closing one leg does not close the strategy.
- Any ownership conflict: preserve exchange visibility, set an abnormal state, and disable all automatic management for the affected position and strategy.

## Attribution States and Operation Gate

Web and backend use four explicit ownership states:

- `verified`: unique ownership evidence; exact-position automatic management may proceed.
- `unassigned`: no originating local entry leg, such as a manual exchange position.
- `attribution_conflict`: multiple candidates or contradictory evidence.
- `evidence_unavailable`: required Deepcoin evidence is temporarily unavailable.

Only `verified`, a live `pos_id`, and a nonterminal leg authorize automatic TP/SL adjustment, partial close, full close, or rebinding. The gate is enforced on the server for every mutation endpoint and automated management path.

For `evidence_unavailable`, retain the last verified display attribution but freeze mutations until fresh evidence succeeds. Never interpret an empty or failed API response as manual close.

Notifications include position ID, symbol, side, candidate strategies, missing or conflicting evidence, and the operation that was blocked. Notifications use durable deduplication so a 30-second reconcile loop does not repeatedly send the same incident.

## Web Position Display

Verified cards display:

- group/KOL;
- strategy instance ID;
- entry-leg index;
- position ID;
- ownership evidence type;
- evidence time distance or direct-ID marker;
- last verified time.

Conflict cards display:

- `归属待确认`;
- conflict status;
- candidate legs and evidence summaries;
- `自动管理已冻结`;
- position ID and current exchange risk data.

The Web attribution layer must consume persisted verified ownership or conflict state. It must not run an independent candidate-scoring algorithm that can disagree with reconciliation.

## TPSL and Stop-Loss Association

Protection association is separate from strategy ownership.

1. Prefer an exact TPSL `posId` when Deepcoin returns it.
2. Otherwise build all position-to-TPSL candidate edges for the same instrument and position side.
3. Use actual timestamp distance with a small justified tolerance instead of exact timestamp equality.
4. Treat `sz=0` as full-position protection.
5. Allow multiple partial take-profit sizes to sum to the live position size.
6. Resolve protection globally and require a unique result.
7. Never use an uncertain TPSL match to cancel or replace protection.

If account evidence proves that a stop loss exists but cannot identify one position uniquely, the card displays `止损存在，归属待确认`. It must not display the position as unprotected, and all automated protection mutation remains blocked.

## Audit and Historical Repair

Add immutable attribution audit records for:

- verified binding;
- terminal manual cancellation or close;
- conflict creation and resolution;
- evidence unavailable and restored;
- repair preview and application;
- old and new ownership IDs;
- evidence IDs and summarized fields used for the decision.

Provide a server command:

```text
telegram-kol-research repair-position-attribution --dry-run
telegram-kol-research repair-position-attribution --apply
```

Dry-run is the default. It reports current ownership, proposed ownership, evidence, confidence class, and records that cannot be repaired uniquely. Apply changes only uniquely proven ownership and terminal-state corrections. It never binds ambiguous positions.

## Test Matrix

Regression tests must cover:

- a cancelled stale 三马哥 leg cannot claim a later 智哥 ETH short position;
- two same-symbol, same-side, same-size fills sixty-nine seconds apart map to their exact legs;
- equal evidence produces `attribution_conflict`;
- cancelled trigger history is not fill evidence;
- manual cancellation prevents recovery from recreating an old order;
- manual close prevents the old strategy from reopening;
- a new Telegram entry creates a new strategy instance;
- API failure preserves last verified attribution and freezes mutations;
- crash after exchange acceptance but before database commit recovers only with deterministic evidence;
- multi-leg, partial-fill, partial-TP, and manual-reduction cases preserve distinct ownership;
- the database rejects two legs owning the same `(venue, pos_id)`;
- backend management rejects unassigned, conflicted, or evidence-unavailable positions;
- a one-second position/TPSL timestamp difference still displays the stop loss;
- `sz=0` full-position stop loss maps correctly;
- partial TP sizes may sum to position size;
- ambiguous protection displays `止损存在，归属待确认` and cannot be mutated.

## Production Rollout

1. Develop locally in an isolated worktree from the latest remote branch.
2. Use test-driven development for each state transition, matching rule, backend gate, and Web behavior.
3. Run focused attribution, execution, Web-render, syntax, and full tests. Record any pre-existing baseline separately and do not increase failures.
4. Commit and push reviewed changes to `codex/deepcoin-auto-trading-v1`.
5. Before production migration, disable global automatic trading. Do not automatically close positions or cancel orders.
6. Back up `data/research.db` on the server.
7. Pull from GitHub, reinstall the editable package, and apply additive database compatibility changes.
8. Run `repair-position-attribution --dry-run` and review every proposed change and conflict.
9. Apply only uniquely proven repairs.
10. Restart `telegram-kol.service` while automatic trading remains disabled.
11. Run several read-only reconciliation cycles and verify that ownership does not oscillate.
12. Confirm server HEAD, service health, HTTP routes, positions, orders, TPSL, audit records, and server tests.
13. Re-enable automatic trading only after explicit operator review.

## Incident-Specific Repair Targets

The repair dry-run should propose, but not silently apply, these corrections when current exchange history still proves them:

- Binding `112` (三马哥): remove incorrect `pos_id=1001124083099498`; confirm its old trigger legs were cancelled; mark the legs terminal and lifecycle `441` no longer entered.
- Binding `120` (智哥): retain market position `1001124083084014`; attach `1001124083099498` to the second trigger entry leg; keep lifecycle `457` entered with both live positions.
- Verify both 智哥 positions have stop loss `1820` and preserve their correct take-profit rows.
- Inspect the two live 舒琴 ETH long trigger orders, but do not cancel or change them as part of attribution repair.

## Acceptance Criteria

- Both current ETH short positions display as 智哥 with their correct entry legs.
- Both display stop loss `1820`.
- 三马哥 no longer displays a live position.
- Binding iteration order cannot change ownership.
- Repeated reconciliation and service restart produce stable ownership.
- Temporary API failure cannot close, reassign, or resurrect a strategy.
- No position is managed automatically without unique persisted ownership evidence.
- Manual exchange cancellation and close are terminal for the old strategy instance.
