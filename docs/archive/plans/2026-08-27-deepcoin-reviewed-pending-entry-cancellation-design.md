# Deepcoin Reviewed Pending Entry Cancellation Design

Date: 2026-08-27
Status: approved for local RED-to-GREEN implementation
Risk: local implementation L1; any production cancellation is a separately
authorized L3 exchange-write operation

## 1. Decision

Add a purpose-built, fail-closed operator tool for the seven reviewed Deepcoin
pending trigger entries that currently block the contract-cache deployment.
The tool is dry-run by default and may apply exactly one order per invocation.

The fixed reviewed set is:

| Order ID | Instrument | Lifecycle | Binding | Leg | Trigger | Size | Embedded stop |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `1001124718697641` | `ETH-USDT-SWAP` | 780 | 271 | 479 | 1827 | 3 | 1795 |
| `1001124718698413` | `ETH-USDT-SWAP` | 780 | 271 | 480 | 1812 | 3 | 1795 |
| `1001124760022605` | `BTC-USDT-SWAP` | 812 | 281 | 494 | 61890 | 13 | 60900 |
| `1001124760022650` | `BTC-USDT-SWAP` | 812 | 281 | 495 | 61390 | 14 | 60900 |
| `1001124898942178` | `ETH-USDT-SWAP` | 911 | 308 | 532 | 2250 | 2.3 | 2186 |
| `1001124905627977` | `BTC-USDT-SWAP` | 914 | 309 | 533 | 73690 | 8 | 72300 |
| `1001124905628046` | `BTC-USDT-SWAP` | 914 | 309 | 534 | 73390 | 8 | 72300 |

Production observations are evidence for the reviewed set, not runtime
authority. Every apply must rebuild a fresh plan from the current database and
exchange state.

## 2. Alternatives

### A. Reuse the generic binding-wide cancellation path

Rejected. It can submit more than one cancellation in one call and its older
path does not provide the exact one-order confirmation and dependent-state
terminalization contract required here.

### B. Use raw Deepcoin API calls and repair SQLite later

Rejected. Exchange and durable ownership could diverge, and a later repair
would have to infer an outcome after authority was lost.

### C. Add a reviewed, fingerprinted, one-order tool

Chosen. It uses fixed targets, fresh exchange truth, exact local ownership,
single-use confirmation, one write attempt, post-write readback, and one local
terminalization transaction.

## 3. Dry-run contract

The planner reads all configured target instruments once and fails closed
unless:

- all positions and regular pending-order reads are complete and empty;
- each reviewed order appears exactly once in pending trigger orders;
- no unreviewed pending trigger order exists in the governed BTC/ETH/SOL set;
- every row remains a long `Conditional` entry with the reviewed instrument,
  trigger price, size, and embedded exchange stop;
- the exact order maps to one reviewed binding, one reviewed entry leg, one
  pending lifecycle, one matching trigger-protection intent, and the expected
  local request fingerprint;
- no fill or terminal history indicates that the order already triggered;
- active exchange-write, queue claim, management, worker-command, and revision
  authority gates are zero.

The plan contains one action per order, conflicts, completed order IDs, and a
deterministic fingerprint. Dry-run never creates confirmation tokens, mutation
intents, events, notifications, or database updates.

## 4. Apply contract

Apply requires an exact order ID, action ID, expected plan fingerprint, and an
unused repair-confirmation token. It rebuilds the complete plan immediately
before the write and rejects any change.

The selected action reserves a durable mutation intent and consumes the token
before invoking exactly one `cancel_trigger_order` call. It never retries the
write. Transport failure, timeout, invalid response, missing exact returned
order identity, incomplete readback, unexpected fill, new position, or change
to any other reviewed order produces an unknown or blocked result and leaves
remaining orders untouched.

Success requires all of the following fresh evidence:

- the selected order is absent from pending trigger orders;
- the selected order is present in trigger history with an exact cancelled or
  expired terminal state;
- no selected-order fill exists;
- no new position or regular order exists;
- every other reviewed pending order is byte-semantically unchanged.

## 5. Durable terminalization

Only after confirmed exchange cancellation, one database transaction:

- marks the exact entry leg cancelled with an explicit terminal reason;
- resolves its trigger-protection intent as terminal and clears retry timing;
- terminalizes its planned protection legs and TP convergence without creating
  exchange actions;
- records one sanitized confirmed-cancellation event;
- when and only when every entry leg in the binding is terminal, marks the
  binding cancelled and the pending lifecycle expired/cancelled;
- keeps sibling pending legs and their lifecycle active after a partial
  reviewed-set cancellation;
- consumes no replay, backfill, worker-command, notification, or Telegram path.

Repeated dry-run recognizes a previously confirmed action only when exchange
absence and durable terminal state agree. Any disagreement is a conflict.

## 6. Redaction and output

CLI output is closed-schema JSON. It may include reviewed identifiers, prices,
sizes, statuses, hashes, and bounded reason codes. It must not print API keys,
signatures, passphrases, environment contents, raw exchange bodies, database
payloads, Telegram text, or exception detail.

## 7. Verification

Use focused TDD for planner conflicts, default dry-run, single-order selection,
single-use confirmation, no-retry unknown outcomes, post-write invariants,
sibling preservation, last-leg terminalization, idempotent completion, and
secret redaction. Run the adjacent cancellation/entry/protection tests and one
final full suite after all production-code changes are assembled.

No push, deployment, SSH, freeze, restart, production write, Deepcoin write,
historical replay, or Telegram send is authorized by this design.
