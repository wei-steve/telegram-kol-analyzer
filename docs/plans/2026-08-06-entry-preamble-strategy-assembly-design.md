# Entry Preamble Strategy Assembly Design

## Goal

Correctly apply an incomplete sizing instruction that is posted immediately
before a complete trading strategy. For example:

1. `BTC换手入场做空，半仓操作做个短线空单。`
2. `BTC，63900-64200附近，做空，止损64900，止盈62800。`

The first message is not independently executable, but its `半仓` instruction
must reduce the second message's configured BTC loss budget from 20 USDT to
10 USDT before order quantities are calculated.

## Decision

Introduce a durable `entry_preamble` stage. An incomplete message that contains
entry intent plus an explicit sizing modifier is persisted instead of being
discarded as an ordinary non-strategy message. The next complete entry strategy
may consume exactly one compatible preamble through deterministic rules.

This supplements the existing authoritative first-pass recognition and
contextual resolution. It does not replace either system, and it does not let
ordinary chat context silently alter an executable strategy.

## Alternatives Considered

### Prompt-only context merging

Continue passing recent messages to MiMo and ask it to mention sizing from the
previous message. This is insufficient because the current authoritative schema
has no entry-risk multiplier, there is no durable consumed state, and execution
cannot audit why the budget changed.

### Delayed message bundles

Delay every possible entry for a fixed interval and classify a bundle of nearby
messages. This can capture multi-message strategies, but it adds entry latency
and still needs deterministic ownership and consumption rules.

### Durable entry preambles

Persist incomplete but actionable entry context and consume it when a complete
strategy arrives. This has the smallest execution latency, provides explicit
evidence, and fails closed when matching is ambiguous. This is the selected
approach.

## Recognition Contract

Extend authoritative recognition with an optional structured entry-context
fragment. The fragment is independent of `recognition_result` so an incomplete
message may remain `非策略` while still contributing safe, non-executable context.

Conceptual shape:

```json
{
  "entry_context": {
    "kind": "entry_preamble",
    "symbol": "BTC",
    "side": "short",
    "risk_multiplier": "0.5",
    "confidence": 0.95,
    "reason": "明确要求半仓做空，但当前消息缺少完整入场和保护价格"
  }
}
```

Supported initial sizing language is intentionally narrow:

- `半仓` -> `risk_multiplier = 0.5`
- an explicit percentage such as `30%仓位` -> `risk_multiplier = 0.3`
- `满仓`, leverage-derived sizing, vague terms such as `轻仓`, and additive
  language such as `加仓` remain non-executable until separately designed.

The canonical meaning of `risk_multiplier` is a multiplier on the symbol's
configured maximum loss budget. It is never a direct contract-count multiplier.

## Persistence

Add an append-oriented table for entry preambles with these logical fields:

- source raw-message identity and chat identity;
- normalized symbol and side;
- canonical decimal risk multiplier;
- authoritative evidence version and recognition generation;
- status: `pending`, `consumed`, `expired`, or `invalidated`;
- consumed-by raw message and strategy instance, when applicable;
- creation, expiration, consumption, and invalidation timestamps;
- a fingerprint covering the source evidence and normalized fields.

Only one transition from `pending` is allowed. Consumption and strategy
creation occur in the same database transaction so a crash cannot apply one
preamble to two strategies.

## Matching Rules

Before executing a newly recognized complete entry strategy, load pending
preambles that satisfy all of the following:

1. Same Telegram chat.
2. Posted before the complete strategy.
3. Same normalized symbol and side.
4. Still pending and based on the current evidence version.
5. No intervening complete entry strategy for the same chat.
6. No intervening cancellation, opposite-side entry instruction, or explicit
   sizing replacement that invalidates the fragment.
7. Exactly one eligible preamble remains.

The first implementation should prioritize adjacency over a broad time window:
the matching preamble must be the latest relevant entry-intent message before
the complete strategy. Unrelated advertisement or conversation messages may be
ignored, but another entry-intent message forms a hard boundary.

If zero preambles match, use the configured risk budget unchanged. If more than
one preamble could match or any identity conflicts, block automatic execution
with a fixed reason code. Never choose by model confidence alone.

## Strategy Assembly

The assembled executable strategy records both the original and effective
budget:

```text
configured_risk_budget_usdt = 20
risk_multiplier = 0.5
effective_risk_budget_usdt = 10
preamble_message_id = 9901
strategy_message_id = 9902
```

`effective_risk_budget_usdt` is passed to the existing Deepcoin order builder.
The builder continues to allocate that budget across market and limit legs and
round quantities using the contract specification. It does not interpret
natural-language sizing.

The execution binding payload, instruction result, notification, and Web
strategy record must expose the multiplier, effective budget, and both source
message IDs. This makes it possible to explain every resulting contract size.

## Ordering and Concurrency

Messages are assembled by Telegram source order, not worker completion order.
If the complete strategy reaches execution while an immediately preceding raw
message is still awaiting authoritative recognition, execution is deferred
briefly with a fixed `preceding_entry_context_unresolved` state. It resumes only
after that message becomes terminal.

The consume operation uses a conditional update on `status = pending`, followed
by strategy creation in the same transaction. Concurrent workers therefore
cannot consume the same preamble twice.

## Failure Handling

- Invalid or unsupported sizing language does not create a preamble.
- Missing symbol or side may be saved only as unresolved evidence and cannot be
  consumed automatically.
- Conflicting symbol, side, or risk multipliers block assembly.
- Edited source messages supersede the prior evidence and invalidate an
  unconsumed preamble.
- Deleted source messages invalidate an unconsumed preamble.
- A consumed preamble remains immutable audit evidence even if the source is
  later edited or deleted; normal incident handling evaluates the already
  executed strategy.
- Failure to persist consumption prevents order submission.
- A preamble never mutates an already submitted strategy.

## Example

For messages 9901 and 9902:

1. Message 9901 is recognized as non-executable `entry_preamble`, BTC short,
   multiplier 0.5.
2. Message 9902 is recognized as the complete BTC short strategy.
3. The assembler finds 9901 as the sole latest compatible preamble.
4. The persisted strategy records evidence `[9901, 9902]` and effective risk
   budget 10 USDT.
5. The existing range-entry builder sizes both legs from the 10 USDT budget.
6. Message 9901 becomes consumed and cannot affect a later strategy.

## Testing

Unit and integration coverage must include:

- half-position preamble followed by a matching complete strategy;
- explicit percentage preamble;
- complete strategy without a preamble;
- symbol and side mismatch;
- multiple possible preambles;
- another complete strategy or opposite instruction between the two messages;
- unrelated chat messages between compatible messages;
- preamble edit and deletion before consumption;
- recognition workers finishing out of order;
- concurrent attempts to consume one preamble;
- crash before and after the atomic consume boundary;
- exact effective-risk and rounded-quantity assertions for single-price and
  range-entry strategies;
- audit and notification output containing both message IDs and both budgets;
- production shadow replay of historical messages 9901/9902 showing a 10 USDT
  effective budget and no exchange write.

## Rollout

1. Add the schema and recognition output in dormant mode.
2. Backfill or replay only curated fixtures; do not consume historical live
   messages.
3. Run shadow assembly and compare proposed budgets with existing executions.
4. Keep a configuration switch that disables preamble consumption while still
   retaining recognition evidence.
5. Enable consumption for selected chats only after shadow results contain no
   ambiguous or cross-strategy matches.
6. Preserve the existing default-budget path as the rollback path.

