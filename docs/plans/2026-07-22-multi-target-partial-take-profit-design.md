# Multi-Target Partial Take-Profit Design

## Problem

A single KOL message can explicitly manage several existing strategies, for
example: "BTC 和 ETH 的单子可以先止盈一半".  The current lifecycle event
contract carries one `target_lifecycle_id`, so it fails closed when more than
one target is named.  This avoids guessing, but it silently drops an otherwise
clear instruction.

An entry range may create several entry legs.  Legs are implementation details
of one strategy, not separate lifecycle targets.  A management instruction
therefore targets a strategy and then acts on all of its exact, verified legs.

## Scope and semantics

The system will support one explicit management instruction applying to several
uniquely identified strategies in the same Telegram message.

- Each named symbol must resolve within the source chat to exactly one entered
  lifecycle with a verified Deepcoin execution binding.
- A `partial_take_profit` of one half creates one independent management batch
  per resolved strategy, with an effective fraction of `0.5`.
- Every filled and exactly attributed entry leg in a target strategy receives a
  50% close based on its current verified position size.
- Every unfilled, still-live entry leg in that strategy is cancelled before the
  first close order.  This prevents a price retrace from reopening exposure
  after the KOL has begun taking profit.
- An already-filled range entry has no deferred legs to cancel.  Thus two
  filled ETH legs both close by 50%; a BTC strategy with one filled leg and one
  pending range leg closes the filled leg by 50% and cancels only its pending
  leg.
- A target that cannot be uniquely resolved, lacks verified ownership, has
  incomplete exchange evidence, or cannot prove cancellation is safe remains
  fail-closed and raises an operator-visible alert.
- Targets are independent.  A blocked BTC target does not suppress a safe ETH
  target, and vice versa.  Within each target, cancellation and close remain
  ordered and recovery-safe.

## Architecture

Recognition will emit a list of management targets rather than a single target
when the source message explicitly names multiple instruments.  Each target
contains the lifecycle ID, symbol, side, management action, and requested
fraction.  The deterministic application layer re-resolves every ID against
the message chat and current lifecycle state; it persists one `SignalCandidate`
per accepted target rather than assigning one candidate to multiple strategies.

The existing message-instruction orchestration will project each accepted
candidate into a separate durable management item.  Existing management
planning, execution, and reconciliation continue to own exchange writes.  A
batch snapshot already distinguishes manageable filled legs from deferred entry
legs; close-style partial-take-profit execution must use the exact deferred-leg
allowlist to cancel pending entries first, then submit the per-position partial
close orders.

Every batch retains the original raw-message ID and its own target lifecycle,
idempotency fingerprint, leg ledger, exchange request/response evidence, and
recovery status.  This gives independent retry and audit behavior even though
the source instruction was one message.

## Safety and failure handling

No target may be inferred from symbol/side alone when that combination maps to
multiple active strategies.  Such a target is not submitted.  Cancellation is
also exact-ID only: the executor must use the frozen `deferred_entry_leg_ids`
and match each stored exchange/client ID to one live order.  Missing, duplicate,
or mismatched order evidence blocks only that strategy before any close order is
sent.

The executor records cancellation separately from partial-close submissions.
The batch is successful only after all required cancellations and all close legs
are exchange-confirmed, followed by the existing remaining-protection checks.
Partial success becomes `recovery_required` with leg-level evidence; it must
not update the lifecycle as confirmed partial take profit.

## Verification

Tests must cover the reported case and its safety boundaries:

1. One BTC-and-ETH message produces two management candidates and two batches.
2. BTC has one verified filled leg and one deferred range leg: the deferred
   order is cancelled before the 50% close.
3. ETH has two verified filled legs: each receives an independently sized 50%
   close and no cancellation request.
4. A blocked target does not stop an unrelated safe target from being submitted.
5. An ambiguous symbol, unverified position, missing deferred order, duplicate
   order match, exchange failure, and repeated delivery submit no unsafe or
   duplicate orders.
6. Reconciliation only marks each strategy confirmed after all of that
   strategy's required legs are proven complete.

Production validation remains server-only: deploy the reviewed branch, restart
`telegram-kol.service`, and inspect the resulting per-target batches and
exchange audit events without replaying historical live orders.
