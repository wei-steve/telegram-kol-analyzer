# Trigger Entry Protection Adoption Design

## Goal

Allow later automated management of a Deepcoin split position that was opened
by a trigger entry carrying embedded TP/SL, without assigning or cancelling a
protection order by symbol, side, price, or message text alone.

## Problem

Deepcoin accepts TP/SL fields on a trigger entry before a split-position
`posId` exists.  Once that entry fills, the resulting pending TPSL row can
contain an `ordId`, instrument, side, quantity, prices, and timestamps but no
`posId`, `closePosId`, or parent-entry order ID.  The position response can
show the TP/SL price but not its cancellable order ID.  A later partial-close
and protection-change action therefore cannot prove which TPSL `ordId` it may
cancel, and must fail closed.

## Design

Retain embedded TP/SL on trigger entries so an entry never waits unprotected
for a polling cycle.  Add a post-fill *protection adoption* workflow that runs
only after the existing entry-leg attribution has verified an exact `posId`.
The workflow records a `position_protection_ledger` row only when it can match
the live pending TPSL order to that exact entry leg with all required evidence.
It does not submit, cancel, modify, or recreate an exchange order.

The adopted ledger row becomes the authority used by later management batches
to revalidate the current pending TPSL order by exact `ordId` before any
mutation.

## Required Evidence

An adoption candidate is eligible only when all of the following hold:

- The entry leg is an active, verified Deepcoin trigger-entry leg with one
  exact `posId`.
- The parent trigger-entry execution event and local request identify the
  same exact entry order/client order, instrument, side, margin mode, and
  split-position mode.
- The candidate is a live `TPSL` row with an `ordId`; any supplied position ID
  must equal the verified `posId`.
- The candidate's TP and SL set exactly matches the original entry request,
  including full-versus-partial protection size semantics.
- The candidate is globally unique among all current pending TPSL rows for the
  exact evidence set.  More than one candidate, an incomplete price set, an
  unavailable snapshot, or a conflicting verified ledger row is a refusal.
- Time evidence is calibrated from Deepcoin's observed fields before it can
  narrow candidates.  It must never be treated as proof merely because a
  timestamp is present.

The system must never adopt from symbol/side alone.  It must surface a
refusal for operator review instead.

## Workflow

```text
trigger entry with embedded TP/SL
        |
exact entry-leg / posId attribution
        |
read positions + pending TPSL snapshot
        |
strict unique match? -- no --> durable refusal + alert; no mutation
        |
       yes
        |
write verified posId <-> ordId ledger evidence
        |
later management batch rechecks exact ordId before cancel/replace
```

## Rollout

1. Add deterministic unit tests for a unique adoption, competing same-side
   positions, equal price/size duplicates, missing fields, stale/missing
   orders, and timestamp-calibration refusals.
2. Tighten the existing trigger-entry repair planner so time is either an
   explicit validated bound or excluded from the claimed evidence; do not call
   a merely non-empty timestamp a match.
3. Run the existing repair as a read-only production audit.  Applying a plan
   remains explicit and fingerprint-guarded until production evidence supports
   automatic adoption.
4. Add automatic adoption only for the proven strict case.  Keep all other
   cases fail-closed and alertable.

## Non-Goals

- Do not replay blocked Telegram management messages.
- Do not relax management preflight or cancel unverified TPSL rows.
- Do not replace embedded entry protection with post-fill `set-position-sltp`:
  it can create duplicate protection and introduces a post-fill protection
  window unless Deepcoin first documents and proves replacement semantics.
- Do not mutate current production ledger data as part of implementation
  rollout without a separately reviewed dry-run fingerprint.
