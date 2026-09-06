# Durable Composite Position Management Design

## Problem

Two live BTC management messages exposed the same structural defect through different symptoms:

- Miya said to take profit on 50% and move the remaining stop to `62700`. Recognition understood both clauses, but deterministic normalization returned early on the explicit stop and persisted only `adjust_stop_loss`. No close was planned or submitted.
- Sanjie said to take profit on 50% and move the stop to entry. The partial close was confirmed, but the protection clause was not preserved and the original first take-profit order remained live.

The current system stores one flattened management action. A successful action can therefore hide a dropped clause from the same source message. Model comparison does not catch this because it compares model semantics rather than the final normalized contract and exchange evidence.

## Goals

1. Preserve every independently actionable, risk-reducing clause from an authoritative management message.
2. Execute a composite instruction as one durable dependency graph, never as unrelated writes that can run out of order.
3. Prefer safety without abandoning safe, confirmed parts of an instruction.
4. Continue reconciliation until every component has exchange-confirmed truth, a definite rejection, or an explicit operator-required outcome.
5. Never retry an exchange-unknown mutation blindly.
6. Keep existing contextual target resolution and exact `posId` ownership authority unchanged.

## Non-goals

- Giving an auxiliary model trading authority.
- Inferring ownership from symbol, side, price, size, or message proximity.
- Automatically replaying old Miya or Sanjie messages after deployment.
- Generalizing every Telegram message into an unrestricted workflow language.
- Reversing an over-reduced position by automatically adding risk.

## Chosen architecture

Use the existing `StrategyManagementBatch` and exact per-position legs, but add an immutable instruction contract and durable component rows. A composite batch contains ordered components:

1. `consume_take_profit_stage`
2. `converge_partial_close`
3. `replace_remaining_protection`

The components share one target fingerprint and strategy lock. Each component has its own state, idempotency key, desired payload, evidence, error code, progress timestamp, and execution deadline. A component becomes eligible only when its dependency has a safe terminal result.

This is preferred over merely reordering conditionals because precedence fixes cannot prove that all message clauses survived. It is preferred over a general multi-action task graph because this three-phase composite is the smallest model that satisfies the observed safety and recovery requirements.

## Instruction contract

Recognition still uses authoritative MiMo and the established contextual target resolver. Deterministic normalization then extracts all components before choosing an action family. It must not return early when it sees one valid clause.

The immutable contract contains at least:

```json
{
  "version": 2,
  "target_lifecycle_id": 694,
  "strategy_instance_id": "deepcoin:...",
  "symbol": "BTC",
  "side": "long",
  "close_fraction": "0.5",
  "stop_mode": "explicit_price",
  "stop_price": "62700",
  "stop_price_source": "current_message_text",
  "take_profit_consumption": "consume_first_stage",
  "cancel_deferred_entries": true,
  "required_components": [
    "consume_take_profit_stage",
    "converge_partial_close",
    "replace_remaining_protection"
  ]
}
```

For cost protection without a numeric price, `stop_mode` is `actual_entry_price`. The execution price comes from each exact live position's fresh average entry, never from the original signal range.

The contract and its fingerprint are saved on the authoritative signal candidate and copied unchanged to the management batch. Before planning and again before execution, the system verifies that the source candidate, batch, and component set agree. A missing component produces `management_instruction_component_dropped`; it cannot be marked completed or sent to the exchange.

## First take-profit consumption policy

An explicit instruction to take profit immediately is treated as consuming the first outstanding take-profit stage:

- If the first TP is pending, cancel it and confirm absence before the partial market close.
- If exact order history proves it already filled, count that fill toward the requested reduction and do not close the same quantity again.
- If it is absent but its terminal state is unknown, enter `recovery_required`; do not submit a close until history, fills, and position observations resolve it.
- A single full-position TP is removed completely.
- With multiple stages, consume the earliest stage first. Preserve later stages where possible, but shrink or remove the earliest remaining stages until their total quantity is no greater than the final live position.

Price, quantity, and time may support display and validation but never establish TP ownership. Cancellation requires canonical ledger ownership for the exact `ordId` and `posId`.

## Partial-close convergence

The batch freezes a trusted pre-instruction position snapshot and computes:

```text
target_remaining = trusted_start_size * (1 - close_fraction)
remaining_close  = current_live_size - target_remaining
```

Every retry or recovery pass recomputes the remaining delta from fresh exchange truth:

- `remaining_close > 0`: submit only that exact quantity.
- `remaining_close == 0`: confirm the component without another write.
- `remaining_close < 0`: stop; never add position automatically. Record `position_below_target_remaining` for operator review.

Each close uses the existing position mutation gateway, a unique client order ID, a durable mutation intent, the exact position write gate, quantity-step and minimum-size validation, and a final fresh position read. A timeout or unknown response is never retried until order history, fills, or coherent position observations establish a definite outcome. A definite rejection with unchanged position may be retried within bounded policy after a fresh preflight.

## Protection replacement

Protection follows a create-before-cancel sequence after the close component is exchange-confirmed:

1. Read the exact remaining position and all canonically owned TPSL rows.
2. Calculate the requested explicit stop or actual-entry stop per position.
3. Validate price tick, side, current market safety, and risk tightening.
4. Submit and read back the new primary stop for the remaining size.
5. Submit and read back the backup stop.
6. Only after both new stops are owned and verified, cancel old stops.
7. Confirm that retained TPs are owned and total no more than the remaining position.

The old stop remains active while new protection is created. A failed new stop therefore does not create an unprotected window. If no verified stop exists despite this invariant, create a critical protection incident, freeze related new automatic entries, keep the recovery component active, and notify the operator. Notification failure never blocks recovery.

If a requested stop has become unsafe relative to the current market, the already confirmed risk reduction remains valid. Protection enters `operator_required` or keeps an already tighter verified stop; the system must not silently claim the requested protection succeeded and must not invent a full exit.

## Durable component states

Allowed states are:

```text
pending
preflighting
submitting
awaiting_exchange
confirmed
definitely_rejected
recovery_required
operator_required
safely_skipped
```

`confirmed`, `operator_required`, and a narrowly defined `safely_skipped` are terminal for a component. The whole batch succeeds only when every required component is `confirmed` or its contract explicitly permits the recorded safe alternative. `operator_required` keeps the message visibly incomplete.

Component transitions are compare-and-set and append evidence rather than overwriting exchange history. Service restart resumes the first nonterminal component. Unknown mutations retain their original idempotency key and are reconciled, not resubmitted.

## Safety versus completion policy

Only necessary dependencies block downstream work:

- Unknown first-TP terminal state blocks the partial close because duplicate reduction is possible.
- Unconfirmed partial close blocks remaining-size protection replacement.
- Notification, auxiliary-model, UI, and reporting failures do not block exchange work.
- A protection failure does not undo or repeat an already confirmed close.
- A safe component continues even when another independent, non-authoritative subsystem fails.

No batch may use a generic `failed` result to discard unfinished components. Recovery continues until confirmed exchange truth or explicit operator action is required.

## Completeness verification

Verify the instruction at three boundaries:

1. Source message to candidate contract.
2. Candidate contract to batch/components.
3. Components to exchange evidence and final live state.

Stable failures include:

- `management_instruction_component_dropped`
- `take_profit_terminal_state_unknown`
- `partial_close_target_overshot`
- `partial_close_exchange_outcome_unknown`
- `replacement_stop_market_unsafe`
- `replacement_stop_readback_unverified`
- `retained_take_profit_exceeds_position`
- `component_exchange_evidence_incomplete`

DeepSeek remains advisory. Its comparison input is expanded to include the immutable normalized contract and actual component outcomes. A disagreement can alert or block before a write according to existing reviewed policy, but never authorizes a trade.

## Notifications and observability

The operator summary reports each component separately:

```text
First take profit: cancelled / already filled / unresolved
Partial close: target, submitted delta, confirmed remaining size
Protection: primary stop, backup stop, retained TP total
Overall: complete / recovering / operator required
```

Progress timestamps advance only on new exchange evidence or a valid state transition. Visibility retries and escalation reuse the existing management incident infrastructure. The production safety monitor adds checks for missing components, duplicate close submissions, a completed batch with incomplete evidence, and live positions whose retained TP quantity exceeds their size.

## Rollout and rollback

Add `composite_management_v2_mode` with `disabled`, `shadow`, and `live`:

- `disabled`: single-action management remains unchanged; detected composite messages fail closed visibly instead of using the lossy legacy path.
- `shadow`: persist contract, components, snapshots, and simulated outcomes with zero exchange writes.
- `live`: execute only the reviewed composite path.

Deploy dormant, replay Miya and Sanjie historical messages in shadow, and run fault-injection tests. Enable live only during a proven safe window with no management, reconciliation, rescue, or time-sensitive strategy operation in flight. Existing batches continue under their original version. Historical messages are never automatically replayed.

Rollback disables admission of new v2 composites but does not disable recovery for an already submitted component. Reverting code is allowed only after all in-flight v2 components are terminal or the compatible recovery worker remains deployed.

## Acceptance criteria

- Miya's wording produces a 50% close contract, consumes the first TP, and sets the remaining stop to `62700`.
- Sanjie's wording produces a 50% close contract, consumes the first TP, and sets stops from the exact exchange average entry.
- No recognized clause disappears between message, candidate, batch, and components.
- A concurrent TP fill cannot cause double reduction.
- Unknown exchange results never cause duplicate writes.
- Remaining positions never lose all verified stop protection during normal replacement.
- Retained TP quantity never exceeds the live remaining position.
- Restart at every component boundary converges without duplication.
- A completed notification is impossible while a required component lacks exchange-confirmed evidence.

