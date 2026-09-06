# Deepcoin Strategy Management Batches Design

## Goal

Make Telegram-driven partial take-profit, full take-profit, and stop-loss
adjustment operate only on the exact Deepcoin positions owned by the selected
strategy. Support split positions, durable idempotency, exchange-confirmed
lifecycle transitions, and explicit recovery when only part of a multi-position
operation succeeds.

Production automatic trading remains disabled throughout implementation,
deployment, and shadow verification. Enabling management execution requires a
separate explicit operator approval.

## Confirmed Product Rules

### Partial Take-Profit

A strategy may own multiple verified Deepcoin split positions. A partial
take-profit applies proportionally to every live position in that strategy.

- An explicit percentage, such as `30%`, controls the first partial close.
- A partial instruction without a percentage defaults to 50%.
- The percentage is calculated from the exchange-reported size at batch
  preflight time.
- Contract step sizes are honored with deterministic remainder allocation so
  the aggregate close is as close as possible to the requested amount without
  exceeding the live position.
- The first exchange-confirmed partial close advances the strategy's partial
  take-profit round from zero to one.
- The second distinct partial take-profit instruction is promoted to a full
  close of every remaining position, regardless of the first instruction's
  percentage.
- A submitted, unknown, or partially confirmed first batch does not advance the
  round. A partially confirmed batch freezes further automatic management and
  requires review.
- A duplicate delivery of the same message is the same batch and must not count
  as a second instruction.
- Added positions do not reset the round. Only a new strategy lifecycle starts
  a new sequence.

For example, two positions of `0.02 BTC` each become `0.01 + 0.01` after a
default first partial take-profit. A second partial take-profit closes the full
remaining `0.02 BTC`.

### Full Take-Profit

A full exit closes every live, verified position owned by the selected strategy.
Each exchange request uses the exact position's `closePosId` and its current full
size. A failed leg does not prevent attempts on the other preflighted legs.

API acceptance is only submission evidence. The binding, entry legs, and
lifecycle remain active or exit-pending until exchange reconciliation confirms
that every target position is closed.

### Stop-Loss Adjustment

A stop-loss instruction applies to all live positions owned by the strategy.

- An explicit stop price is applied to every position.
- A move-to-break-even instruction uses each position's own exchange `avgPx`.
- Each position retains its own existing take-profit orders.
- Every position, ownership record, and protection relationship is preflighted
  before any cancellation.
- Any missing or ambiguous ownership or TPSL relationship blocks the whole
  batch without exchange mutation.
- Deepcoin cannot update all positions atomically. After preflight, positions
  are updated sequentially. If a new protection cannot be created, the system
  immediately attempts to restore that position's old protection, stops later
  legs, preserves already successful changes, and raises a high-priority alert.
- Failure to restore protection enters a recovery-required state and must never
  trigger a blind automatic retry.

## Identity Chain

The authoritative management identity chain is:

```text
Telegram raw message
-> MiMo recognition generation and target_lifecycle_id
-> SignalCandidate.target_lifecycle_id
-> StrategyLifecycle
-> strategy_instance_id
-> ExecutionBinding
-> verified, nonterminal ExecutionOrderLeg rows
-> exact Deepcoin posId set
-> immutable ManagementBatch target snapshot
```

Execution must not fall back to selecting an order from only chat, KOL, symbol,
and side. The lifecycle, strategy instance, and binding must agree. A target
lifecycle without its own binding cannot borrow the sole same-symbol,
same-direction binding belonging to another lifecycle.

Before batch creation, reconciliation must resolve any newly triggered
conditional order through the known trigger order, generated regular order,
and resulting position. Empty trigger-order `clOrdId` or `tag` values are not
ownership evidence. An indistinguishable assignment remains an attribution
conflict and freezes management.

## Persistent Model

### Management Batch

A durable management batch records:

- source raw-message ID, Telegram message ID, and recognition generation;
- target lifecycle ID, strategy instance ID, and execution binding ID;
- requested intent and effective action;
- requested fraction, effective fraction, and partial-take-profit round;
- immutable preflight timestamp and target fingerprint;
- idempotency fingerprint;
- batch status and structured block/failure reason;
- creation, execution, reconciliation, and completion timestamps.

Batch states are:

```text
ready -> executing -> reconciling -> succeeded
```

Exceptional terminal or paused states are:

- `blocked`: preflight failed before any exchange write;
- `partial_failed`: some legs were submitted or confirmed and others failed;
- `recovery_required`: an exchange result or protection state cannot be made
  safe automatically.

The source recognition generation, target strategy, and action form a unique
idempotency boundary. A batch's target set is immutable. If live state changes
before execution, the batch is blocked rather than silently targeting a new
position.

### Management Batch Leg

Each target `posId` has a durable leg containing:

- verified entry-leg and position identity;
- preflight size, entry price, side, margin mode, and position mode;
- contract size increment and planned close size;
- complete old TPSL snapshot and planned new TPSL snapshot;
- deterministic close client-order ID where applicable;
- exchange request, response, and returned order IDs;
- submission, confirmation, and recovery state;
- last exchange observation and structured error.

Leg states include:

```text
planned -> reserved -> submitted -> confirmed
```

Exceptional leg states include `submit_unknown`, `failed`, `restore_pending`,
`restored`, and `recovery_required`.

## Processing Flow

### Recognition and Planning

MiMo remains authoritative. Recognition persists the selected lifecycle on the
candidate instead of reducing the target to symbol and side. Management intent
is normalized into `partial_take_profit`, `full_exit`, `adjust_stop_loss`, or
`move_stop_to_break_even`.

The planner:

1. Claims the authoritative recognition generation.
2. Resolves the exact lifecycle and strategy instance.
3. Reconciles exchange orders and positions.
4. Resolves one exact active binding for that strategy.
5. Requires every live target position to have verified, nonterminal ownership.
6. Preflights all live position economics and, for protection changes, all TPSL
   relationships.
7. Derives the effective partial action from the confirmed round.
8. Writes the immutable batch and all legs in one local transaction.

No exchange mutation occurs during planning or shadow mode.

### Close Execution

Before each close API call, the executor commits a durable per-position
reservation. It submits a Deepcoin market order with exact `closePosId` and a
deterministic `clOrdId` derived from the batch and leg. `tag` may carry a short
batch marker but is never treated as identity evidence.

If the API result is lost or times out, the leg becomes `submit_unknown`. The
recovery worker queries pending and historical regular orders by `clOrdId`
before deciding whether any retry is safe. It never blindly resubmits.

After all preflighted legs are attempted, reconciliation confirms actual
remaining sizes. A full-exit batch closes the binding and lifecycle only when
all target positions are confirmed absent or zero. A first partial batch
advances the partial round only when every planned reduction is confirmed.

### Stop-Loss Execution

The planner snapshots all old protection rows and derives complete replacement
protection per position. The executor cancels only exact old order IDs for the
current position, sets replacement protection, and records both operations.

On set failure it attempts to reconstruct the saved old protection immediately.
It then stops subsequent legs. Previously successful legs retain the new stop;
the batch exposes the mixed state for urgent review. Missing, ambiguous, or
changed protection at any preflight or pre-cancel gate blocks mutation.

## Concurrency, Recovery, and Ordering

Only one nonterminal management batch may own a strategy at a time. A full exit
in progress blocks a stop update or partial close for the same strategy. A
later natural message can be planned only after reconciliation makes the prior
batch terminal and safe.

Recovery resumes from durable batch and leg state. It never reparses the source
message, changes lifecycle target, or rebuilds the target set. Recovery first
queries exchange state for `reserved`, `submitted`, `submit_unknown`, and
`reconciling` legs.

Process crashes before an API call leave a safe reservation that can be
reconciled. Crashes after a call but before response persistence create an
unknown result that requires exchange lookup. TPSL restoration failure remains
paused for operator action.

## Notifications and Audit

Notifications include the source message, lifecycle, strategy instance,
binding, batch, and every target `posId` with planned and observed results.

- `blocked`: normal abnormal-case notification confirming no exchange write.
- `partial_failed`: high-priority notification listing successful, failed, and
  still-open positions.
- `submit_unknown` or `recovery_required`: highest-priority notification that
  explicitly prohibits blind retry and requests operator review.

Notifications are deduplicated by batch and state transition. Existing
execution events remain the immutable API audit trail; batch and leg rows hold
the resumable workflow state.

## Testing

Implementation follows test-driven development. Required coverage includes:

### Recognition and Targeting

- unqualified partial take-profit defaults to 50%; explicit percentages win;
- full exit and stop-loss intents remain distinct;
- target lifecycle persists through candidate, planning, and execution;
- a lifecycle without a binding cannot borrow another strategy's binding;
- multiple bindings, unverified legs, and ownership conflicts block planning;
- newly triggered conditional orders reconcile to generated regular orders and
  positions only with deterministic evidence.

### Partial and Full Close

- `6 + 4` contracts at 50% produce `3 + 2`;
- contract-step rounding meets the aggregate target without over-closing;
- `0.02 + 0.02` becomes `0.01 + 0.01` after the first default partial and zero
  after the second distinct partial instruction;
- a first explicit 30% close is followed by a full close on the second partial;
- submitted or partially confirmed first closes do not advance the round;
- duplicate messages and worker races do not create a second batch;
- every request has the exact `closePosId` and deterministic `clOrdId`;
- one failed close leg does not prevent attempts on other preflighted legs;
- API acceptance does not close the lifecycle before reconciliation;
- timeouts are resolved by lookup rather than blind resubmission.

### Stop-Loss

- all split positions receive an explicit stop price;
- break-even uses each position's own average entry;
- all existing per-position take profits are preserved;
- any ambiguous protection produces zero cancels and zero creates;
- replacement failure restores the old protection and stops later legs;
- restoration failure enters `recovery_required` and notifies urgently;
- successful earlier legs are not blindly rolled back.

### Recovery and Concurrency

- one strategy cannot run two management batches concurrently;
- full exit blocks concurrent protection mutation;
- crashes before, during, and after an API call recover safely;
- unknown submissions never auto-resubmit;
- notifications deduplicate until state changes.

## Rollout and Acceptance

1. Keep global automatic trading disabled.
2. Deploy schema, planner, state machine, and UI/audit visibility.
3. Enable shadow planning only; generate batches without Deepcoin writes.
4. Observe naturally arriving examples of partial take-profit, full exit, and
   stop adjustment.
5. Verify every plan's lifecycle, strategy, binding, complete `posId` set,
   quantities, protection snapshots, and block reasons against read-only
   exchange evidence.
6. Run focused and complete local tests, then server tests and service health
   checks at the identical Git commit.
7. Require a separate explicit operator approval before enabling management
   execution for BTC and ETH.

Acceptance requires no new full-suite failures, no guessed attribution, no
blind retry, exchange-confirmed lifecycle transitions, and correct shadow plans
for all three management actions.
