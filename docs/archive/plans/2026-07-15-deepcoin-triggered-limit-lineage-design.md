# Deepcoin Triggered-Limit Lineage Design

## Purpose

Preserve exact strategy ownership when a Deepcoin Conditional entry triggers a
regular limit order that fills much later. The generated regular order may have
empty `clOrdId` and `tag`; attribution must not depend on either field and must
never cross Telegram chat or strategy-instance boundaries.

## Verified production behavior

Production evidence on 2026-07-15 established this chain:

1. Stored trigger order `1001124101153303` belonged to one exact entry leg and
   requested BTC short size 7 at 65100.
2. It triggered at 06:21:48 China time and generated regular limit order
   `1001124106836368` at that same time.
3. The regular order contained empty `clOrdId` and empty `tag` and remained
   unfilled until 14:33:09.
4. On fill, the split-position `posId` equaled the regular order `ordId`.

The current direct trigger-time-to-position-time rule rejected the resulting
position because the delayed fill put position creation roughly eight hours
after trigger time. Broadening that time window would introduce unsafe
same-symbol attribution.

## Safety boundary

Every Telegram chat and every `strategy_instance_id` is fully isolated. A
message in one chat cannot manage or provide ownership evidence for another
chat's strategy, even when publisher text, instrument, side, entry price, and
timing appear equivalent. Cross-chat strategy families are outside this design
and require a separate explicit reviewed configuration before they may exist.

No single proximity signal proves lineage. Missing, malformed, unavailable, or
non-unique evidence leaves the leg `unassigned` and blocks automatic management.

## Attribution chain

Add a globally mutual-unique multi-hop evidence path:

```text
persisted entry leg
  -> exact stored trigger ordId
  -> successful Deepcoin trigger-history row
  -> generated regular order candidate
  -> filled regular order
  -> ordId == split-position posId
```

The trigger-history row must match the stored trigger order ID and report a
successful trigger. A generated regular-order candidate must match the same
instrument, side, position side, margin/position mode when available, size, and
normalized limit price. Its creation time must match the trigger time within a
small API-precision tolerance. The regular order may fill later without a
fixed elapsed-time limit.

The final position must use the regular order ID as its exact `posId`, and its
size/economic identity must agree with the fill. Candidate selection runs
globally across all pending entry legs and all relevant regular orders. Both
directions must be unique: one leg selects one regular order and that regular
order selects only that leg. The final `posId` must also have one global owner.

## State transitions

- A successful trigger with a uniquely identified but unfilled regular order
  remains non-position-bearing. Persist auditable lineage evidence if the
  existing schema supports it without granting ownership; otherwise derive it
  again from exchange history.
- A uniquely identified filled regular order whose `ordId` equals a live
  position `posId` may move the entry leg to `verified` ownership.
- An already closed historical position may be reported as terminal evidence
  but ordinary reconciliation must not recreate live ownership for an absent
  position.
- Missing regular history, API errors, duplicate candidates, incompatible
  prices/sizes, or conflicting owners remain `unassigned`,
  `attribution_conflict`, or `evidence_unavailable` as appropriate.
- No attribution outcome submits, cancels, closes, or adjusts an exchange
  order.

## Integration

Extend the existing coherent Deepcoin reconciliation snapshot rather than
adding per-leg network calls. Regular order history must be loaded once per
instrument alongside trigger history and positions. Reuse the repository's
normalizers for timestamps, decimals, instruments, sides, and order IDs.

The new evidence path runs before strategy-management planning. Management
continues to require every target entry leg and every live `posId` to be
verified. Therefore delayed-fill attribution can make a safe leg manageable,
while ambiguity still blocks the entire strategy batch.

After this prerequisite is reviewed, resume Task 7 of the strategy-management
batch plan. Task 7 remains responsible for close-order and remaining-position
reconciliation, not entry attribution.

## Testing

Add regression coverage for:

- a triggered regular limit order that fills eight hours later;
- an unfilled generated regular order that must not create a position owner;
- empty regular-order `clOrdId` and `tag`;
- exact trigger-time/regular-create-time normalization across seconds and
  milliseconds;
- two same-price/same-size candidates that must remain ambiguous;
- two different chats or strategy instances with otherwise identical orders;
- a regular order claimed by two legs and a `posId` already owned elsewhere;
- history/read failure producing `evidence_unavailable` rather than absence;
- restart/repeated reconciliation idempotency;
- preservation of terminal/manual-close states;
- no exchange write calls.

## Rollout

Keep production automatic management fail closed. Deploy only after focused,
adjacent, and full tests plus an independent review. Verify with a read-only
production snapshot that the known delayed-fill chain is explainable, but do
not apply historical ownership or perform a live trade as a deployment test.
