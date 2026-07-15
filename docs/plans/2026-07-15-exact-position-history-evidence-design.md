# Exact Historical Position Evidence Design

## Problem

The historical attribution cleanup planner currently reads live positions,
pending orders, regular and trigger order history, trade fills, local lifecycle
rows, execution events, and exact-position close reservations. It does not read
Deepcoin's historical-position endpoint.

The production dry run therefore reported eight
`historical_terminal_evidence_missing` conflicts even though a read-only query
to `GET /deepcoin/account/positions-history` returned one exact fully closed
row for every affected `posId`. Each row had the requested `posId`, split
position mode, a positive original `pos`, and `closePos == pos`.

One component also exposes old attribution corruption. Position
`1001123808285345` belongs directly to binding 9 / leg 10, while binding 11 /
legs 13 and 14 incorrectly retain the same `posId`. Exact order and historical
position queries prove that binding 11's two entry orders created and fully
closed positions `1001123808728782` and `1001123808729017` instead.

The audit also exposed an apply-order defect. A leg can legitimately receive a
`clear_redundant_historical_position` action followed by a
`terminalize_historical_entry_leg` action. The first action clears `pos_id`, but
the second currently requires the original `pos_id`, so the transaction rejects
its own planned action chain and rolls back.

## Goals

- Add exact Deepcoin historical-position rows to the repair evidence snapshot
  and fingerprint.
- Treat only an exact, fully closed split-position row as terminal evidence.
- Resolve the eight evidence-missing conflicts without using position absence,
  price proximity, symbol/side similarity, or operator inference.
- Clear binding 11's stale shared ownership while preserving its real historical
  position IDs in immutable audit evidence.
- Allow an explicitly planned clear-then-terminalize chain for one leg without
  weakening stale-plan protection.
- Preserve the existing backup, fingerprint, conflict, transaction, and
  production dry-run gates.

## Non-Goals

- Do not mutate an exchange position or order.
- Do not modify current live-position ownership.
- Do not accept partial closes as terminal.
- Do not infer a close reason that the exchange evidence does not provide.
- Do not rewrite a stale historical leg to a different persisted `pos_id` merely
  because its order ID also identifies a historical position. The true ID is
  retained in audit evidence while the stale duplicate ownership is cleared.
- Do not apply a production cleanup without a separately reviewed fresh plan and
  explicit operator approval.

## Considered Approaches

### 1. Integrate exact historical-position evidence into the planner

This is the selected approach. It extends the existing coherent, fingerprinted
repair workflow and keeps all cleanup rules in one place. API failure or an
invalid response remains fail closed.

### 2. Insert operator-reviewed evidence rows manually

Rejected. It would mutate production evidence before the planner can verify it,
and it would make provenance depend on an ad hoc operator action.

### 3. Use a one-off SQL or cleanup script

Rejected. It would bypass the exchange snapshot, stale-plan fingerprint,
immutable attribution audits, and transaction rollback behavior.

## Evidence Acquisition

Add a read-only client method for:

```text
GET /deepcoin/account/positions-history
```

The method accepts `instType=SWAP`, `instId`, `mrgPosition=split`, `posId`, and
`limit=100`. Repair planning requests exact history for candidate identifiers
from nonterminal Deepcoin entry legs:

- the persisted `leg.pos_id`; and
- the leg's exact `order_id` when it differs from the persisted position ID.

The second identifier is required for corrupted legacy rows such as binding 11,
where each filled entry order is also the true split-position ID but the stored
`pos_id` was overwritten with another binding's position.

The loader deduplicates `(instrument, identifier)` requests. Any request error,
non-list response, or duplicate conflicting row is recorded as an evidence
source error and blocks all cleanup actions. Historical-position rows are added
to the exchange evidence fingerprint so a changed close record invalidates a
reviewed plan.

Normal reconciliation does not need to issue these per-ID history calls. The
new evidence is loaded only by `repair-position-attribution`, which is an
operator-invoked workflow.

## Terminal Evidence Rules

A historical-position row is terminal proof only when all of the following are
true:

- response `posId` exactly equals the requested candidate;
- `mrgPosition` is `split`;
- instrument and position side match the entry binding;
- original `pos` parses as a positive number;
- `closePos` parses as the same quantity as `pos` within exact decimal
  normalization; and
- the ID is absent from the coherent live-position and pending-order snapshot.

`closePos < pos`, missing quantities, a mismatched side/instrument, duplicate
conflicting rows, or an API error produces no action and an unresolved conflict.

The evidence payload records `posId`, `pos`, `closePos`, `avgPx`, `closeAvgPx`,
`pnl`, `cTime`, and `uTime`. It never treats the row as proof of stop-loss,
take-profit, or manual close reason because the endpoint does not identify the
cause.

## Per-Leg Planning

Terminal evidence must be selected per leg, not once per binding.

1. Prefer exact history for the persisted `leg.pos_id` when that ownership is
   not known to be stale.
2. For a redundant competing leg whose persisted `pos_id` belongs to a unique
   authoritative owner, require exact fully closed history for that leg's own
   `order_id` before clearing and terminalizing it.
3. Record that order-derived historical position ID in both action evidence and
   the immutable audit, but persist `pos_id = NULL` for the stale competing leg.

This makes binding 9 / leg 10 the retained historical owner of
`1001123808285345`, while binding 11 / legs 13 and 14 lose that stale shared ID
only after their own positions are independently proven closed.

## Transaction Ordering

Keep the current deterministic action order: clear ownership before terminal
state changes. Before mutation, build an explicit map of planned
`clear_redundant_historical_position` actions by leg.

When a later terminalize action sees `leg.pos_id` equal to the planned clear
action's `new_pos_id`, it may proceed only if:

- the same plan contains that exact preceding clear action;
- the clear action's old value matches the terminalize action's recorded prior
  value; and
- all other expected leg state still matches.

No generic `NULL` tolerance is added. An external or unplanned clear still
fails as stale database evidence. The unique index remains the final action and
the whole transaction still rolls back on any error.

## Failure Handling

- Historical-position request fails: report evidence unavailable; no cleanup.
- Exact row missing: retain `historical_terminal_evidence_missing`.
- Partial close: report `historical_position_not_fully_closed`; no cleanup.
- Side, instrument, or mode mismatch: report invalid exact evidence; no cleanup.
- Competing leg lacks its own exact terminal row: keep the duplicate component
  unresolved.
- Exchange or database evidence changes after review: reject the fingerprint.
- Clear/terminalize chain differs from the planned dependency: roll back.

## Tests

Tests cover:

- client request path and exact query parameters;
- exact fully closed history producing terminal evidence;
- partial, zero, mismatched, duplicated, malformed, and unavailable history
  remaining fail closed;
- order-ID evidence resolving a stale competing leg without persisting the new
  historical ID;
- binding 11's two independently closed positions;
- historical-position rows participating in the exchange fingerprint;
- clear-then-terminalize applying atomically;
- an unplanned prior clear still failing stale-state validation;
- rollback, idempotency, unique-index ordering, and the existing current-live
  position exclusions.

## Production Rollout

1. Implement in the existing isolated worktree with test-driven development.
2. Run focused and full local tests.
3. Fast-forward the reviewed commits to `codex/deepcoin-auto-trading-v1` and
   push GitHub.
4. Confirm production automatic trading remains disabled and retain the
   existing timestamped backup.
5. Deploy through `scripts/server_git_update.sh`.
6. Run a fresh production `repair-position-attribution` without `--apply`.
7. Verify the eight evidence-missing conflicts are resolved only by exact fully
   closed rows, current live position actions remain empty, and no live `posId`
   appears in historical actions.
8. Stop and report the new action list and fingerprint. Production apply remains
   outside this design until separately approved.
