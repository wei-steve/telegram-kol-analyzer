# Conditional TPSL Ownership Recovery Design

## Goal

Make Deepcoin condition-entry protection fully automatic for future orders:
every generated TP/SL order is durably attributable to the exact strategy,
entry leg, and split-position `posId`, so later partial exits and TP/SL updates
can use an exact cancellable `ordId` without manual lookup or symbol/side
guessing.

## Facts and Root Cause

Deepcoin has distinct order objects:

- A condition-entry (`trigger-order`) returns a condition parent `ordId` before
  it fires. Its later filled regular order has a different identity.
- Embedded `tpTriggerPx` and `slTriggerPx` cause Deepcoin to create TPSL
  trigger orders after the condition entry fires.
- `set-position-sltp` also creates a TPSL trigger order and returns its
  cancellable `ordId`.

Both embedded and independently created TP/SL therefore belong in
`trigger-orders-pending`, not the pending regular-order endpoint. The actual
failure is that a pending embedded TPSL row can expose `ordId`, instrument,
side, price, and size while omitting a reliable parent trigger ID or `posId`.
At management time that prevents safe cancellation of a particular TPSL row.

## Design Principles

1. **Persistent evidence wins.** Model protection ownership as a chain from
   local strategy intent through the condition parent, its execution, exact
   position, and TPSL order. Never derive ownership from group, instrument,
   side, price, or message text alone.
2. **Capture before submitting.** A post-fill snapshot alone cannot tell
   whether an otherwise matching TPSL existed before the entry submission.
3. **Fail closed for cancellation, fail safe for downside protection.** Never
   cancel an unverified TPSL row. If an exact verified position lacks a
   manageable stop after bounded recovery, create a position-specific stop
   with `posId` and record the returned order ID.
4. **Do not duplicate take profit as a fallback.** A second full-size TP may
   exit more than intended when an unknown embedded TP remains. Preserve an
   opaque TP until its ownership can be proved; retry recovery in the
   background.
5. **No replay.** Recovery only discovers and records ownership. It never
   replays a Telegram management instruction or blindly mutates historical
   protection.

## Data Model

### Protection intent

Create one durable protection-intent record per trigger-entry order leg before
submission. It contains:

- a locally generated immutable correlation ID;
- execution binding and order-leg IDs, strategy instance, group/message
  attribution, and deterministic `clOrdId`;
- request fingerprint: instrument, side, margin/position mode, entry size,
  entry trigger price, TP/SL prices, and full-versus-partial size semantics;
- the pre-submit pending-TPSL baseline for the instrument: all observed
  `ordId`s plus normalized fingerprints and snapshot time;
- submission time, parent condition `ordId` when returned, and a recovery
  state.

The existing `execution_order_legs`, `execution_events`, and
`position_protection_ledger` remain the authoritative strategy, execution, and
managed-protection records. The new intent only fills the missing temporal
correlation gap.

### States

```text
prepared -> submitted -> parent_pending -> parent_triggered
                                      -> position_verified
                                      -> tpsl_adopted
                                      -> recovery_deferred
                                      -> stop_rescued
```

All transitions are idempotent. A restart resumes from durable state and does
not submit a second entry or create duplicate rescue protection.

## Automated Correlation Workflow

```text
capture pending-TPSL baseline
        |
persist intent, submit trigger entry, store parent ordId
        |
watch parent history + regular order/fill history
        |
existing exact entry-leg attribution establishes posId
        |
read pending TPSL and TPSL history
        |
unique post-baseline candidate with exact intent fingerprint?
   | yes                                      | no
   v                                          v
write verified posId <-> TPSL ordId        defer and retry with
ledger evidence                            bounded diagnostics
```

A candidate is adopted only if it is a live or historically traceable TPSL
row that is absent from the pre-submit baseline, has a new unique `ordId`,
matches the intent's instrument, side, full size semantics, TP and SL prices,
and is in the validated parent-trigger/position time window. Any returned
`posId` must equal the already verified `posId`. The candidate may not already
belong to another execution leg or intent.

The worker serializes snapshot-and-submit operations per Deepcoin account,
instrument, and side. This makes two identical local entries non-overlapping;
external or manually placed competing TPSL rows still produce a refusal rather
than a guessed adoption.

## Management Workflow

1. Resolve one exact active `posId` through the existing entry-leg binding.
2. Resolve protection only through a verified ledger row and re-read the
   pending TPSL row by exact `ordId`.
3. Cancel that exact TPSL order, set the desired replacement with exact
   `posId`, and immediately persist the response `ordId` as the replacement
   ledger record.
4. If any revalidation fails, do not cancel or modify a TPSL row. Record a
   durable, bounded recovery diagnostic and let the correlation worker retry.

For a verified position whose attached protection remains opaque after the
recovery deadline, create only an exact-`posId` stop-loss rescue if no managed
stop is present. Persist the returned ID and use it for future stop updates.
Do not create a full duplicate TP/SL bracket. The original TP remains active
until correlation proves its order ID; requested TP changes remain pending
rather than risking double full-size exits.

## Safety and Observability

- A unique constraint prevents a TPSL `ordId` from being associated with more
  than one execution leg or protection intent.
- All snapshots, evidence fields, retry counts, state transitions, rescue
  writes, and conflict reasons are recorded without Telegram message text.
- UI/timeline shows: parent order ID, exact `posId`, adopted TPSL order IDs,
  pending recovery state, retry age, and whether a stop rescue is active.
- Automatic writes share the existing per-account Deepcoin rate limiter.
- No code path cancels a pending TPSL solely because it matches symbol, side,
  price, quantity, or an old timestamp.

## Rollout

1. Add models/migrations and deterministic unit tests for intent persistence,
   baseline capture, and recovery state transitions.
2. Capture intent and baseline during trigger-entry submission, without
   changing management mutations.
3. Add read-only reconciliation and audit telemetry; compare candidates with
   production snapshots before enabling ledger adoption.
4. Enable automatic adoption for strict unique post-baseline candidates.
5. Enable idempotent exact-`posId` stop rescue only after it has dedicated
   live-account verification and alerting.
6. Keep legacy ambiguous TPSL rows read-only; use history-based recovery and
   never auto-cancel them without later exact evidence.

## Test Matrix

- single entry: parent -> fill -> `posId` -> one new matching TPSL;
- identical price/size TPSL already present before submission;
- two competing local condition entries and account-scoped serialization;
- externally created competing TPSL after submission;
- delayed pending endpoint with TPSL visible only in history;
- restart between every workflow transition;
- duplicate worker deliveries and duplicate API snapshots;
- conflicting ledger/intent ownership;
- exact stop-rescue creation, response persistence, and no duplicate rescue;
- proof that ambiguous recovery performs no cancel, no TP write, and no
  Telegram-message replay.

## Non-Goals

- Do not claim that pending regular orders contain TPSL ownership.
- Do not turn a missing `ordId` into a symbol/side fallback.
- Do not use full duplicate take-profit as an automatic rescue.
- Do not automatically clean historical opaque protection unless new exact
  evidence proves ownership.
