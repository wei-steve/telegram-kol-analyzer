# Andy BTC Break-even Management Design

## Goal

Treat Andy's message “回成本了，时间太久，注意保护成本，如果有在上面补仓的一定要现在平加仓，甚至还有微弱利润” as a position-management instruction, not a non-strategy message.

## Confirmed Meaning

When it is attributed to the same KOL's active BTC short positions:

1. Reduce every matched split position by 50%.
2. After the individual reduction succeeds, move that individual position's stop loss to its own exchange-reported average entry price (`avgPx`).

The instruction does not mean closing only the newest add-on position. It applies to every matched active position. It must not cause a live action for the two currently losing BTC positions; this change is local code and offline verification only.

## Recognition

Recognition emits `event_type=position_update` with both management intents:

- `partial_take_profit` with a 0.5 fraction for the phrase `平加仓` in this management context;
- `move_stop_to_protect` for “回成本”, “保护成本”, or equivalent cost-protection language.

The recognition prompt and deterministic fallback will include this example so an LLM-only classification cannot silently return `非策略`.

## Targeting and Safety

Only target all active bindings that exactly match the source chat, resolved KOL id, symbol, and side. Every target must have an exact bound Deepcoin split-position id and a valid exchange average entry price. Never match an unbound exchange position or a position belonging to another KOL.

No target, an unavailable position id, an unavailable average entry price, or an ambiguous attribution must fail closed and create no guessed order.

## Execution

Use a composite, per-binding management action rather than independent unordered queue entries. For each target binding:

1. Submit a reduce-only market close for 50% of its current position size using the exact `closePosId`.
2. Only on a successful reduction, cancel its exact existing TP/SL rows and set the stop loss to that position's `avgPx`, preserving the prior take profit.
3. Write the close and protection events with the source Telegram message id and the target position id.

Targets are independent. Failure for one target must prevent its protection update but must not cause a guessed action for another target. The result must expose per-position success or failure for review.

## Verification

Offline tests must prove:

- the phrase is recognized as combined position management;
- two same-KOL BTC shorts are each reduced by 50%;
- each resulting stop loss equals its own `avgPx`, not a shared lifecycle price;
- an unrelated KOL's BTC short remains untouched;
- a failed reduction does not move that position's stop loss;
- missing binding, `posId`, or `avgPx` fails closed.

Production validation remains a reviewed server deployment followed by operational observation; no live probe or order is authorized by this feature work.
