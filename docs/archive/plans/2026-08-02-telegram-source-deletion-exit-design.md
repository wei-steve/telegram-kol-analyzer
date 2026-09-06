# Telegram Source-Deletion Exit Design

## Problem

The live Telegram listener currently registers only `events.NewMessage()`. It
does not receive or persist `MessageDeleted` events, and therefore a strategy
message can disappear from Telegram while its exchange orders remain active.

Shuqin messages `3428` and `3429` demonstrate the failure mode. Message `3428`
created two ETH trigger-entry orders with stop `1695`. The author then deleted
that source message and reposted the same strategy as message `3429` with stop
`1795`. The system retained both raw records, never learned that `3428` had
been deleted, and left its old entry orders live. One later filled and inherited
the withdrawn `1695` protection. Message `3429` separately failed contextual
resolution, so it never superseded the old execution.

The confirmed business rule is stronger than replacement detection:

> Deleting a source strategy message invalidates that strategy immediately.
> Cancel every unfilled entry order and market-close every filled position that
> belongs to the deleted message. A repost is a new strategy and may not execute
> until the deleted strategy has reached a proven terminal state.

## Goals

- Make Telegram deletion a durable, first-class source event.
- Stop all further automation for the deleted strategy before exchange writes.
- Cancel every exact unfilled entry leg belonging to the deleted message.
- Market-close every exact filled position belonging to the deleted message.
- Handle cancel/fill races, partial fills, unknown exchange outcomes and process
  restarts without duplicate cancellations or duplicate market closes.
- Keep the complete identity chain auditable:

  ```text
  chatId + messageId
  -> rawMessageId
  -> strategyInstanceId / lifecycleId
  -> executionBindingId
  -> executionOrderLegId
  -> ordId / posId
  -> mutation intent / management batch
  ```

- Prevent a repost from opening a replacement strategy while deletion cleanup
  for the withdrawn source remains in flight.

## Non-goals

- Do not infer deletion from a message disappearing during an ordinary history
  sync. Only an authenticated Telegram deletion event or a separate supervised
  repair may create the deletion action.
- Do not transfer orders or positions from the deleted message to a repost.
- Do not treat time proximity alone as evidence that two strategies are the
  same.
- Do not make Telegram edits follow deletion semantics. `MessageEdited` support
  will use the existing evidence-generation/re-recognition path and needs a
  separate execution-revision design.
- Do not delete historical raw messages, order records, events or ledgers.

## Considered approaches

### Direct event-handler writes

The deletion callback could immediately call the existing cancellation and
close APIs. This is small but unsafe: a crash between calls loses progress, an
unknown exchange result can be submitted again, and duplicate Telegram events
can duplicate market closes.

### Replacement grace window

The system could wait several minutes for a repost before touching the old
strategy. This reduces churn but contradicts the confirmed requirement to exit
immediately and leaves withdrawn orders exposed during the grace period.

### Durable deletion-exit state machine

This is the selected approach. The listener first writes an idempotent source
event and deletion-exit record. A worker then reuses exact order cancellation,
position-mutation intents, management reconciliation and the position authority
lock. Every exchange operation has durable before/after state and can recover
after restart.

## Data model

### Raw source state

Add nullable deletion metadata to `raw_messages`:

- `source_status`: `active` or `deleted`, default `active`;
- `deleted_at`: Telegram event time when available, otherwise receipt time;
- `deletion_event_fingerprint`: immutable SHA-256 identity of the accepted
  Telegram deletion event.

The raw payload and text remain intact for audit. Once set, deletion metadata is
immutable; a repost has a different Telegram `messageId` and therefore a new
raw row.

### Source-message event ledger

Add `telegram_source_message_events`:

- `id`;
- `chat_id`, `message_id`, optional `raw_message_id`;
- `event_type` (`deleted` initially);
- `event_fingerprint`, unique;
- `telegram_event_at`, `received_at`;
- `processing_status` (`recorded`, `action_created`, `ignored`,
  `recovery_required`, `completed`);
- bounded `reason_code` and `evidence_json`;
- `created_at`, `updated_at`, optional `completed_at`.

The fingerprint includes the authenticated account/session scope, chat ID,
message ID and event type. Duplicate delivery must return the existing row.

### Deletion-exit ledger

Add `source_message_deletion_exits`, one row per source event and exact strategy
lifecycle:

- source event, raw message, lifecycle, strategy instance and execution binding
  IDs;
- immutable target fingerprint containing all entry-leg identities known at
  planning time;
- state: `planned`, `cancelling_entries`, `closing_positions`, `reconciling`,
  `succeeded`, `blocked`, or `recovery_required`;
- exact cancellation trade-signal IDs and management-batch ID where present;
- last reason, retry/reconciliation timestamps and notification state;
- timestamps.

Unique constraints on source event plus lifecycle and on target fingerprint
make duplicate events idempotent.

The deletion-exit ledger is orchestration state, not an alternative order
ledger. Exchange ownership continues to come from execution legs, position
mutation intents, management legs and protection ledgers.

## Event ingestion

`run_live_listener()` registers a `events.MessageDeleted()` handler in addition
to `NewMessage()`. The handler:

1. extracts `event.chat_id` and every `deleted_id`;
2. accepts only configured target chats and locally known `chatId + messageId`
   pairs;
3. writes the source event and raw deletion marker in one transaction;
4. creates deletion-exit work for every exact lifecycle linked to the raw
   message;
5. signals the worker and returns quickly.

Telegram deletion events may contain several message IDs and may not provide a
message body or sender. The local raw-message key is therefore mandatory. An
unknown ID produces a durable `ignored/source_message_not_found` event and no
exchange action.

The listener's existing `operation_lock` also wraps deletion ingestion so a new
message callback cannot interleave with the initial deletion barrier in the
same service process.

## Immediate automation barrier

In the same database transaction that creates deletion-exit work:

- mark the raw message deleted;
- mark each linked strategy/lifecycle as source-deletion exit pending;
- prevent the deleted strategy from creating entry orders, protection orders,
  convergence work or ordinary management batches;
- prevent any candidate repost from auto-executing while an overlapping
  deletion exit from the same chat is nonterminal.

The barrier is based on exact source/lifecycle relationships for the deleted
strategy. The repost hold may use same chat plus normalized symbol and side to
avoid cross-strategy blocking, but it only delays execution; it never assigns
ownership or transfers positions.

## Exit orchestration

All planning and exchange mutation run under the existing position authority
lock.

### Snapshot and plan

The worker loads one coherent Deepcoin snapshot and the exact binding legs. It
classifies each entry leg as:

- unfilled and cancellable;
- filled with a verified `posId`;
- terminal/absent;
- partial, ambiguous or evidence unavailable.

Symbol/side proximity is never sufficient. A missing order identity or
ambiguous position blocks the action.

### Cancel unfilled entries first

Reuse `cancel_pending_entry_legs()` and its exact readback rules. Persist the
trade signal and execution events before the exchange call. Every leg must end
as confirmed cancelled/absent or be proven filled.

If a cancel races with a fill, refresh attribution and add the newly verified
`posId` to the same deletion exit. Do not report success after only the
original snapshot is flat.

### Close filled positions

After pending entries are terminally resolved, create one immutable full-exit
management batch for all exact live `posId`s owned by the binding. Reuse the
existing management executor and position-mutation gateway so each market
close has a deterministic client order ID, durable `reserved -> submitting ->
submitted/confirmed` state and readback recovery.

The close is always full size. It does not reuse the deleted message's stop or
take-profit values. Protection cleanup follows the existing full-exit path.

### Final reconciliation

The deletion exit becomes `succeeded` only when a fresh complete snapshot
proves:

- every entry leg is terminal;
- no entry order from the target fingerprint remains pending;
- every owned position is absent or zero-sized;
- no position mutation has an unknown outcome;
- the lifecycle is terminal with reason `source_message_deleted`.

Only then may a held repost proceed through normal recognition and entry
execution.

## Failure and recovery behavior

- Incomplete Telegram identity: ignore without exchange writes.
- Missing strategy link: complete as `ignored/non_strategy_or_unlinked`.
- Missing order identity or conflicting ownership: `blocked` and notify.
- Exchange snapshot unavailable: remain `reconciling`; availability retries do
  not consume evidence-conflict attempts.
- Cancel or close result unknown: `recovery_required`; reconcile by exact order
  and client-order IDs before any retry.
- Service restart: resume every nonterminal deletion exit from durable state.
- Duplicate deletion: return the existing event/exit and make no new exchange
  call.
- Multiple deleted messages: process each exact source independently under the
  authority lock.

No error path may restore the deleted strategy to an executable state.

## Repost behavior

A repost is always a new raw message and strategy identity. When it arrives
during deletion cleanup:

- recognition and display may complete;
- automation is held with reason `waiting_source_deletion_exit`;
- after old cleanup succeeds, normal safety and concurrency checks run again;
- the new strategy creates new order IDs and its own protection legs.

No semantic similarity decision is needed to exit the deleted strategy.
Similarity is used only to limit the repost execution hold to the same chat,
symbol and side.

## Observability

The Web strategy record and operator notification should show:

- source message deleted time;
- deletion-exit state;
- exact cancelled entry legs/order IDs;
- exact closed position IDs and management batch;
- late-fill detection;
- any blocked or recovery-required reason;
- replacement message held/released status.

All displayed evidence is bounded and excludes raw exchange payloads and
credentials.

## Testing

Tests must cover:

- listener registration and multi-ID deletion ingestion;
- duplicate deletion idempotency;
- unknown/non-strategy deletion with zero exchange writes;
- two unfilled legs cancelled exactly once;
- one filled plus one pending leg: cancel first, then market-close;
- a fill racing with cancellation and joining the same full exit;
- partial fill and missing identity fail closed;
- unknown cancellation and unknown market-close recovery without resubmission;
- restart from every nonterminal state;
- repost recognition allowed but auto execution held until cleanup succeeds;
- unrelated chat/symbol strategy remains executable;
- Shuqin `3428 deleted -> 3429 reposted` regression, proving the `3428`
  orders cannot survive to fill later.

Production verification must use a synthetic database and mocked Deepcoin
client locally. Deployment verification is read-only; no live Telegram message
may be deleted as a test and no live exchange order may be submitted.

## Rollout and rollback

Deploy schema and listener code dormant behind
`telegram_source_deletion_exit_enabled=false`. Verify deletion-event ingestion
with a synthetic/in-process test and confirm ordinary new-message processing is
unchanged. Enable only after a production safe-window audit proves no
recognition, management batch or position mutation is in flight.

Rollback disables the handler/worker flag and redeploys the prior commit.
Durable event and exit rows remain for audit. Rollback never reopens cancelled
orders or closed positions and never clears a deletion marker.
