# Versioned Protection Recovery Design

## Goal

Prevent a verified Telegram management instruction from being lost when a
Deepcoin TP/SL snapshot is temporarily incomplete, while preserving a complete,
immutable history from source message through strategy, exact position, and
every protection change.

## Scope

The design covers:

- durable source-message, AI-decision, strategy, `posId`, and management-batch
  lineage;
- immutable initial and replacement TP/SL revisions;
- five-minute recovery of a narrowly defined, temporary protection-snapshot
  failure; and
- evidence that distinguishes an exchange response problem from local matching
  or ownership refusal.

It does not infer ownership from symbol, side, group, message text, price, or
time proximity. It never auto-executes an instruction after its recovery window.

## Current Failure

An existing management batch becomes permanently `blocked` with
`protection_missing_cancellable_order_id` when the pending TPSL snapshot does
not include exact verified ledger order IDs. This is safe for the remaining
position, but it also prevents a valid partial take-profit. The batch currently
does not retain the observed TPSL evidence and is not eligible for re-planning
when a later snapshot recovers.

## Design

### Protection revisions

Treat `position_protection_ledger` rows as immutable order-level evidence and
add a revision-level history for every exact `(strategy_instance_id, pos_id)`.
A revision records the source message/management batch, predecessor revision,
requested TP/SL, old and new exact order IDs, exchange response evidence,
visibility snapshots, and lifecycle state:

`planned -> replacing -> visible -> active -> superseded`

or terminal `failed` / `conflicted`.

Initial entry protection creates the first revision. A stop or take-profit
change creates a successor rather than overwriting the initial record. Exactly
one revision may be `active` for an exact position. A replacement is not active
until the new IDs are confirmed in a fresh pending-TPSL snapshot.

### Temporary recovery

Only a blocked batch with all of these facts may recover:

1. exact active strategy binding and verified `posId`;
2. exact verified protection-ledger IDs for that position;
3. no ownership, position-size, lifecycle, or protection-version conflict; and
4. failure caused solely by missing current visibility of those exact IDs.

The batch saves a redacted snapshot observation: per-instrument response count,
sorted order-ID digest/set, parser/schema result, endpoint error, and timestamp.
It schedules retries at 5, 15, 30, 60, and 120 seconds, ending five minutes
after the first failed preflight. Each retry rebuilds the entire exact target
from a new exchange snapshot. A successful exact match replaces the empty
blocked plan in the same idempotency batch and executes once. Expiry or any
identity/protection drift is terminal and alerts the operator.

### Deepcoin response completeness

The current pending-TPSL response contains only `code`, `msg`, and `data`, with
no cursor or total metadata. The client must not silently assume completeness.
For a target action, it must prove visibility of every expected exact order ID;
unrelated missing orders do not matter. Unknown pagination metadata, invalid
schemas, endpoint errors, and a configurable apparent response cap are explicit
incomplete-response observations and cannot authorize a mutation. Missing
expected IDs enter the bounded recovery path rather than permanently discarding
the group instruction.

### Operator history

Strategy detail shows a chronological, append-only trail:

`source message -> AI decision -> entry -> initial protection -> each management
batch -> replacement/visibility observations -> current protection revision`.

Manual emergency actions are recorded as explicit manual events so later
reconciliation never assumes the original full size remains untouched.

## Safety Invariants

- Never cancel or replace a TP/SL without exact order-ID ownership.
- Never execute a recovered message outside its five-minute window.
- Never retry a submitted or outcome-unknown close.
- Never make two active protection revisions for one exact `posId`.
- Never treat an incomplete Deepcoin response as proof that protection is absent.

## Verification

Focused tests cover initial protection history, replacement lineage, incomplete
snapshots, recovered snapshots, expiry, API/schema errors, unknown pagination,
manual protection drift, duplicate workers/restarts, and exactly-once partial
close submission. Server verification uses only natural messages and read-only
snapshots until the reviewed feature is deployed.
