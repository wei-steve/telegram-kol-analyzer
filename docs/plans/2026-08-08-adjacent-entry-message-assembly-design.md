# Adjacent Entry Message Assembly Design

## Goal

Assemble entry instructions that a KOL sends across adjacent Telegram messages
before any exchange write, and safely revise an already submitted exact strategy
when a sizing or supplemental-entry instruction arrives later. The assembled
strategy must preserve the configured maximum-loss budget and must never treat
an entry-price range as implicit evidence of a half-sized strategy.

## Production Evidence

Recent production messages show three recurring shapes:

- 飞扬 `#4154` published a complete BTC long strategy and `#4155` followed
  41 seconds later with a supplemental entry near `63400`.
- 陈哥 `#9901` published `半仓操作` before complete strategy `#9902`, while
  `#9936` published `正常仓位操作` after complete strategy `#9935`.
- 米娅 `#558` published a complete BTC long strategy and `#559` followed
  three seconds later with `轻仓入场，50%仓位`; `#538/#539` used the same
  pattern earlier.

In each post-strategy case, the later raw message already existed before the
complete strategy reached automatic execution, but the current assembler only
loads messages earlier than the strategy. The strategy therefore used its full
configured budget and the later sizing or supplemental-entry message was
skipped as a non-action.

The samples also disprove any rule that infers total sizing from range width.
陈哥 has used a 300-point BTC range with both explicit half-sized and explicit
normal-sized instructions.

## Decisions

Use a two-stage architecture:

1. A source-order admission barrier and bidirectional adjacent-message
   assembler prevent premature execution when relevant later messages already
   exist in the database.
2. A durable exact-strategy revision pipeline handles the smaller set of cases
   where an order was already submitted before the later fragment arrived.

Do not use a fixed global delay. Do not submit first and rely exclusively on
later repair. Do not infer sizing from range width, leverage, or vague language.

## Canonical Semantics

Keep total strategy risk separate from entry-leg allocation.

### Total strategy risk

- `半仓操作` and an explicit `50%仓位` mean
  `strategy_risk_multiplier = 0.5`.
- An explicit percentage from zero through 100 percent maps to the same bounded
  multiplier.
- `全仓操作` and `正常仓位操作` mean
  `strategy_risk_multiplier = 1.0` relative to the configured symbol loss
  budget. They never mean committing the full account balance or changing the
  exchange margin mode.
- Vague terms such as `轻仓` without a numeric percentage remain
  non-authoritative. When one message contains both `轻仓` and an explicit
  `50%`, the numeric percentage is authoritative.
- Without an explicit accepted sizing fragment, use the configured risk budget.

### Entry-leg allocation

- `两个点位各半仓` means a full strategy budget with two entry allocations of
  approximately 50 percent each.
- A range can create multiple entry legs, but its width does not determine the
  total strategy multiplier.
- When a half-sized strategy also has a two-leg range, both legs share the
  half-sized total budget. The allocation is not applied twice.

### Supplemental entries

- `补仓：63400附近` is a supplemental entry price for the uniquely resolved
  strategy, not a new independent strategy budget.
- Existing filled risk plus all pending and supplemental legs must remain at or
  below `configured_risk_budget * strategy_risk_multiplier`.
- A supplemental entry without a unique strategy, valid stop, supported symbol,
  compatible side, or available risk headroom is blocked.

## Architecture

### Durable fragments

Add an append-oriented `entry_strategy_fragments` table. A fragment records:

- source raw-message and Telegram identity;
- normalized symbol and side;
- kind: `risk_multiplier`, `leg_allocation`, or `supplemental_entry`;
- bounded normalized payload;
- authoritative evidence version and recognition generation;
- optional exact target strategy raw message, candidate, thread, and lifecycle;
- source-order relationship: `before_strategy` or `after_strategy`;
- status: `pending`, `assembled`, `consumed`, `invalidated`, `expired`, or
  `blocked`;
- reason, fingerprint, and transition timestamps.

The current `entry_preambles` and existing assemblies remain readable for
historical compatibility. New recognition writes the general fragment model.
A one-time migration is unnecessary: existing historical rows are not replayed
into live execution.

Add an `entry_assembly_fragments` association table so one immutable assembly
can contain multiple fragments. Extend assembly evidence with the complete
bounded calculation snapshot rather than overwriting historical evidence.

### Recognition contract

The authoritative first-pass recognizer remains responsible for literal
evidence extraction. It may emit fragments independently of
`recognition_result`, so a non-strategy message can carry a non-executable
fragment.

The contextual resolver remains responsible for target selection. It resolves
post-strategy fragments only when source order, symbol, side, reply evidence,
and active strategy context yield one exact target. It must not replace or
duplicate existing strategy targeting.

### Source-order admission barrier

Immediately before an entry strategy creates a recovery signal or trade signal,
load later same-chat raw messages that:

- are already persisted;
- are within the bounded adjacent-message segment;
- occur before the assembly cutoff captured for this execution attempt; and
- have not reached durable authoritative resolution.

If any such message could affect entry assembly, return
`adjacent_entry_context_pending`. Do not create a trade signal or exchange
write. When the later message reaches terminal recognition, schedule the
original strategy for assembly again.

Unrelated messages with completed evidence do not block. A new complete entry,
explicit cancellation, opposite-side entry, replacement, or expired adjacency
window forms a hard boundary. Selection uses Telegram `posted_at`, message ID,
and raw-message ID, never worker completion order.

This barrier adds no fixed delay when no later raw message exists. It closes the
observed race because the 米娅, 陈哥, and 飞扬 follow-ups were already persisted
when their strategies reached execution.

### Immutable assembly

The assembler consumes all compatible fragments at once and saves:

- configured and effective risk budgets;
- total risk multiplier and its source message;
- entry-leg allocation and its source message;
- original and supplemental entry prices;
- normalized stop and take-profit plan;
- per-leg quantity and estimated stop-loss risk;
- target strategy identities and source-order boundary;
- evidence versions and one idempotency fingerprint.

Fragment consumption and assembly creation occur in one database transaction.
The same source fragment cannot be consumed by two strategies, and the same
strategy generation cannot produce two current assemblies.

## Pre-submit Execution

If no exchange binding or trade submission exists, construct the order draft
only from the immutable assembly. Apply the total multiplier once, then divide
that effective risk across entry legs. Contract rounding may leave unused risk
but must never exceed the effective budget.

If a trade signal exists but has not crossed the exchange-write boundary,
supersede only the proven zero-submission signal and regenerate it from the
assembly. A signal with unknown submission state is not safe to supersede.

## Post-submit Revision

Recognition never writes to Deepcoin. It creates a durable entry revision intent
for the exact strategy instance and binding. Reuse the existing revision and
exact-cancellation infrastructure where its invariants match, while adding a
new revision kind for sizing and supplemental entries.

The revision state machine is:

```text
planned -> cancelling -> revalidating -> reducing_exposure/rebuilding
        -> reconciling -> succeeded
```

Any ambiguous write or ownership result transitions to `recovery_required`.

### All entry legs unfilled

Verify every pending leg by exact order ID/client order ID, cancel it, read back
terminal cancellation, and rebuild from the immutable assembly. Do not submit a
replacement until every old pending leg is proven terminal.

### Partially filled strategy

Re-read exact positions, pending regular orders, trigger orders, and protection
evidence under the serialized exchange-write boundary.

- If verified filled risk is below the new target, retain the filled exposure
  and allocate only the remaining headroom.
- If verified filled risk exceeds the new target, cancel all pending legs first,
  then use the existing exact risk-reducing management path to reduce the
  position to the target. Re-read the remaining position and protection before
  rebuilding any pending legs.
- If the stop is missing, unverified, or would be weakened, do not rebuild or
  increase exposure.

The position must retain verified stop protection throughout the revision.

### Cancellation/fill races

If an order disappears during cancellation, perform one bounded read-back. It
must prove either terminal cancellation or an exact verified fill. Otherwise
enter `recovery_required`. Unknown exchange responses are never converted into
automatic retries.

## Failure Handling and Idempotency

- Use fixed reason codes for unresolved adjacent evidence, ambiguous fragments,
  conflicting multipliers, target mismatch, exhausted risk budget, unknown
  submission state, cancellation uncertainty, late fill, and protection gaps.
- Persist request/response summaries, exact IDs, calculation inputs, and the
  assembly/revision fingerprint without secrets.
- Restart recovery resumes from durable state and fresh read-only evidence. It
  never repeats an exchange write whose result is unknown.
- Message edits supersede the current evidence generation. A consumed fragment
  cannot silently mutate an active assembly; it creates a new revision intent.
- Message deletion follows the existing source-deletion safety rules and cannot
  silently resurrect or resize a strategy.

## Operator Experience

The message and strategy views expose bounded summaries:

- waiting for adjacent entry context;
- configured budget, explicit multiplier, and effective budget;
- range-leg allocation versus total strategy sizing;
- supplemental prices and remaining risk headroom;
- source message pair or group;
- revision stage and fixed blocking/recovery reason.

Notifications distinguish a safe defer, a completed assembly, a completed
risk-reducing revision, and an operator-required recovery. They must not claim
that sizing or orders changed before exchange read-back proves it.

## Rollout

Use independent settings:

- `entry_message_assembly_v2_mode = disabled | shadow | live`
- `entry_revision_v2_mode = disabled | shadow | live`

Every phase is deployed disabled, has a tested disable path, and preserves the
current production path until explicitly activated.

1. Deploy fragment persistence and recognition dormant.
2. Run local and server read-only historical replay.
3. Enable assembly shadow mode and compare proposed versus actual risk.
4. Enable live pre-submit admission and assembly after invariant review.
5. Keep revision shadow-only while capturing natural submitted-order cases.
6. Enable exact unfilled cancellation/rebuild.
7. Enable partial-fill headroom and risk reduction only after exact ownership,
   protection, and restart recovery are proven.
8. Enable supplemental-entry rebuilding last.

Do not restart or deploy during an active time-sensitive strategy operation.
Historical Telegram messages are never resent to create real trades.

## Verification

### Historical read-only replay

- 飞扬 `#4154/#4155`: supplemental `63400` joins the original strategy and
  total risk remains at the configured cap.
- 陈哥 `#9901/#9902`: effective BTC risk is 10 USDT from explicit half sizing.
- 陈哥 `#9935/#9936`: effective BTC risk is 20 USDT from normal sizing.
- 米娅 `#558/#559` and `#538/#539`: effective BTC risk is 10 USDT from the
  post-strategy 50-percent fragment.
- `两个点位各半仓`: full total budget, two approximately equal allocations.
- Equal-width ranges with different explicit sizing remain different.

### Required race coverage

- later raw message persisted but recognition is active, failed, expired, or
  terminal;
- strategy has no signal, a zero-submission signal, submitted unfilled legs, a
  partial fill, or an unknown submission;
- cancellation races with a fill;
- filled risk below, equal to, or above the revised target;
- multiple or conflicting fragments;
- irrelevant chat, advertising, profit review, exit-percentage, and remaining
  position messages;
- edit, deletion, duplicate delivery, worker concurrency, and service restart;
- no duplicate order, no orphan pending leg, no risk-cap breach, and continuous
  verified stop protection.

## Success Criteria

- All approved historical samples produce the expected immutable assembly in
  read-only replay.
- No strategy executes while a persisted, potentially relevant adjacent message
  is still unresolved.
- Range width alone never changes total risk.
- The maximum estimated stop loss across filled and pending legs never exceeds
  the effective budget.
- A revision completes only from exact exchange read-back, and unknown outcomes
  stop in `recovery_required` without duplicate writes.
- Both feature switches disable new behavior without damaging existing durable
  evidence or the current production path.

## Operational gate and rollback clarification

Every restart and every mode transition requires a fresh read-only proof that
recognition, entry submission, cancellation, entry revision, management, and
position mutation have no in-flight or unknown-outcome work. A failed or stale
check means there is no safe window. Deployment begins with both v2 modes
disabled; server tests and the read-only historical replay precede assembly
shadow. Assembly live precedes revision shadow. Initial revision live is
restricted to exact unfilled legs; partial-fill risk reduction and supplemental
entry actions remain hard-closed by separate planner arguments (both default
false) until natural shadow evidence proves exact ownership and uninterrupted
verified protection and a later reviewed production wiring explicitly enables
each argument.

Rollback disables both modes to stop new admission and the automatic revision
worker. Operators continue bounded read-only reconciliation of every durable batch. It never deletes fragment, assembly,
replacement, intent, order, or read-back evidence and never retries an unknown
exchange result.
