# Chen Strategy Management Execution Consistency Design

## Goal

Prevent Telegram management intent from appearing as an executed Deepcoin
position change before exchange confirmation. Correctly interpret retained
position percentages and ensure every close quantity conforms to the Deepcoin
contract quantity step and minimum quantity.

This design is based on Chen group strategy message `#9519` and its management
messages `#9520`, `#9522`, `#9525`, and `#9527`.

## Confirmed Product Rules

### Percentage Meaning

Management percentages describe one of two different quantities:

- `止盈 60%`, `减仓 60%`, or `平仓 60%` means close 60%.
- `保留 40%`, `剩余 40%`, or `留 40% 底仓` means retain 40% and therefore
  close 60%.

An explicit close percentage takes precedence over a retained percentage when
both produce the same result. If the two meanings conflict, planning must fail
closed with a structured ambiguity reason. An unqualified partial-take-profit
instruction continues to use the existing first-round default of 50%.

### Deepcoin Quantity Compliance

The planner must use the current Deepcoin contract specification for every
target symbol. It must allocate the aggregate requested close across every
verified live `posId` in stable order while enforcing:

- `quantity_step` alignment for every planned leg;
- `min_quantity` for every nonzero planned leg;
- no leg larger than its current exchange-reported position size;
- no aggregate close larger than the requested amount after deterministic
  round-down; and
- exact full-position size for a full exit, rejecting a full close whose live
  size cannot be represented by the contract step.

The executor must revalidate the persisted step-aligned planned quantity before
each Deepcoin request. Invalid, stale, or non-representable quantities block the
batch before that leg is submitted. Deepcoin must never receive a fractional
contract quantity such as `2.4` when the step is `1`.

### Intent State Versus Exchange State

MiMo remains authoritative for interpreting a Telegram message, but recognition
must persist only the requested management intent. It must not write an
exchange-success statement or change effective lifecycle stop-loss, take-profit,
partial-round, or exit state.

The durable management batch owns execution state:

```text
recognized intent
-> planned batch
-> submitted per-position legs
-> exchange reconciliation
-> confirmed lifecycle state
```

Only reconciliation may promote the effective lifecycle state after observing
the exact target positions and protection orders. Disabled, blocked, failed,
partial-failed, submit-unknown, and recovery-required outcomes keep the prior
confirmed lifecycle values.

## Web Display

Strategy and message cards must distinguish:

- `已识别，未执行` for disabled or shadow-only management;
- `执行失败` with the structured batch or automation reason;
- `执行中/待交易所确认` after submission but before reconciliation; and
- `已执行` only after exchange confirmation.

The strategy card's effective stop-loss, take-profit, remaining position, and
exit status must come from confirmed lifecycle/exchange state. A recognized
requested value may be shown separately as an intent and must never replace the
confirmed value prematurely.

## Failure Handling

- Ambiguous lifecycle, binding, position ownership, protection ownership, or
  percentage semantics blocks planning with zero exchange writes.
- A contract-spec read failure or invalid quantity step blocks planning.
- A Deepcoin rejection remains failed and cannot mutate confirmed lifecycle
  fields.
- API timeout or unknown submission stays `submit_unknown`; it is reconciled by
  deterministic client/order identity and is never blindly retried.
- Partial multi-position success remains visible as `partial_failed` or
  `recovery_required`; it is not summarized as a fully executed strategy update.

## Test Plan

Regression coverage must use the production Chen messages:

- `#9520`: two verified positions receive per-position break-even planning;
- `#9522`: `止盈 60%` plans a 60% aggregate close and preserves remaining
  protection until reconciliation;
- `#9525`: `保留 40%` normalizes to a 60% close, not 40% or the default 50%;
- `#9527`: disabled execution records recognized intent without changing the
  confirmed stop-loss or claiming success.

Sizing tests must include `6 + 5` BTC contracts with `quantity_step=1`, proving
that a 60% aggregate request produces only integer per-position quantities and
never submits `2.4`, `3.6`, or another off-step value. Tests also cover minimum
quantity, step changes, conflicting percentages, full exits, and stale contract
specifications.

An end-to-end test must prove that recognition success followed by executor
failure leaves the confirmed lifecycle unchanged and renders a visible failure
state. A reconciliation-success test must prove that confirmed exchange state
updates the lifecycle and Web card exactly once.

## Rollout

1. Implement with automatic trading disabled in tests and no live Deepcoin
   calls.
2. Run focused recognition, planner, sizing, executor, reconciliation, and Web
   tests, then the full local suite.
3. Review the exact diff and push only reviewed commits to
   `codex/deepcoin-auto-trading-v1`.
4. Update the production server through the normal GitHub pull/restart path.
5. Verify the deployed SHA, service health, database migration state, and
   read-only management audit.
6. Do not place a test order. Validate the next natural management message from
   its batch plan, Deepcoin response, reconciliation, and Web state.
