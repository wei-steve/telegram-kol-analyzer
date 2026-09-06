# Trigger Entry Backup Stop Design

## Goal

Keep two independently observable exit protections for a filled Deepcoin
trigger-entry split position: the source strategy's attached stop and a
slightly more distant, exact-position backup stop. The backup limits exposure
when Deepcoin's automatically-created attached stop is missing or rejects at
trigger time.

## Incident

The ETH trigger-entry parent was accepted with `slTriggerPx=1919` and
`slOrdPx=-1`. Deepcoin created the stop child, but after it triggered the
child returned `errorCode=203` (`NotEnoughMoneyToClose`) and the live position
remained open. Reconciliation retained the historical child as `verified`, so
the lost protection was neither surfaced nor recovered.

## Constraints

- The attached stop remains the primary protection and is submitted with the
  trigger-entry parent to minimize the post-fill naked window.
- Do not create a second protection through `set-position-sltp`: Deepcoin
  documents that setting a new position TPSL can overwrite the existing TPSL.
- The backup must target only the exact verified split `posId`, never a
  symbol/side/time inferred position.
- A stop that has already been crossed must never be silently recreated; that
  would be an unapproved immediate exit.
- Opaque or non-owned exchange orders are never cancelled.

## Design

After trigger-entry reconciliation proves one exact active split position, the
system submits a separate Deepcoin trigger order that closes that exact
position (`closePosId`) at market. It is persisted as a `backup_stop` role and
is independent from the attached TPSL child.

The configurable default buffer is 50 basis points (`0.5%`). For primary stop
`S` and contract tick size `t`:

- long: `backup = floor_to_tick(S * (1 - buffer), t)`;
- short: `backup = ceil_to_tick(S * (1 + buffer), t)`.

The backup order uses the opposite close side, the strategy's `posSide`,
`mrgPosition=split`, `tdMode`, the exact `closePosId`, and a market execution
price. It receives its own client-order ID and durable leg record. It is not
represented as a TP/SL child or allowed to overwrite one.

Before submitting, the planner must prove that the entry leg is verified and
active, the live exchange position matches exact ID/instrument/side, the
primary stop exists or is otherwise recorded, the backup price is on the
correct risk side of the primary stop, and the backup is safely before the
liquidation boundary. Failure is a durable refusal and high-risk alert, not a
write.

## Lifecycle monitoring

For every owned primary and backup stop, reconciliation must compare the
pending and history trigger snapshots against the exact live position:

- pending exact expected order: `protected`;
- position remains live and a stop history row has `triggerTime > 0` with a
  non-zero error code: `stop_trigger_failed`;
- position remains live but neither pending protection nor successful close
  evidence is present: `protection_missing`;
- trigger reads fail: `protection_unknown`.

Any of the last three states creates one durable, high-priority incident,
freezes automatic management for that exact position, and exposes both order
IDs and exchange error evidence. If the primary stop fails but the backup
remains pending, the backup stays in force. If either close succeeds and the
position is demonstrably terminal, only the system-owned sibling order may be
cancelled.

## Recovery policy

When the primary stop is missing but market price has not crossed it, the
operator may approve a separate, exact-position recovery. When price has
crossed the primary stop or any stop has been rejected, the system does not
automatically recreate a stale stop or market-close the position. It alerts
and requires an explicit operator decision.

## Verification and rollout

Tests must cover buffer rounding for both sides, exact-ID-only planner gates,
the `errorCode=203` history case, duplicate/order-ownership refusals, and
terminal sibling-cancellation rules. A first production rollout is read-only
for incident detection. Backup submission is then enabled only for new
trigger entries, with server-side read-only confirmation of both orders and
their exact position ownership before broader use.
