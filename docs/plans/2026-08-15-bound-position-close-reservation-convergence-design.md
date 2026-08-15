# Bound position close reservation convergence design

## Purpose

Phase One deployment remains paused because the approved Batch 119 recovery
window found 29 `bound_position_close_reservations` rows in the nonterminal
`submitted` state. The Batch 119 runbook correctly refuses to operate while any
durable exchange-writer marker remains nonterminal. This design adds one
closed-scope recovery path for those reservation rows so that proven historical
terminal facts can converge without weakening the deployment gate.

This is a Phase One gate-clearing subphase, not a new product feature. The
approved Phase One production target remains
`c50887b991712340d7d5606fb6916cdbb033926e`. Recovery code lives on the separate
`codex/bound-close-reservation-recovery` branch and is used only from a reviewed
detached candidate checkout. It is not a replacement production baseline.

## Production facts that motivate the design

The stopped-service Batch 119 apply window refused before backup or database
mutation because 29 bound close reservations were still `submitted`:

- 26 reference locally closed execution bindings;
- 2 reference locally unknown execution bindings;
- 1 references a locally active execution binding;
- all 29 have a historical `close_bound_position_market` execution event with
  an exchange order identifier;
- 10 already have a confirmed close mutation, while 19 do not.

Age, a closed binding, a missing current position, or a historical event by
itself is not terminal proof. In particular, the reservation associated with an
active binding might identify one closed split-position leg while another leg
remains live, or it might represent genuinely unresolved exchange work.

## Safety invariants

The recovery must preserve all of the following:

- Never ignore a reservation because it is old.
- Never treat local binding or lifecycle status as exchange truth.
- Never treat position absence alone as proof of a completed close.
- Never replay a Telegram message or a historical management instruction.
- Never submit, cancel, replace, close, or protect an exchange order.
- Never build or expose a writer-capable Deepcoin client.
- Never hand-edit the production database.
- Never change an execution binding, lifecycle, execution leg, management batch,
  position mutation, order identifier, request, or provider response.
- Never enable MiMo v2 or change trading settings.
- Any active, incomplete, ambiguous, conflicting, oversized, or drifting evidence
  is `UNKNOWN` or `ACTIVE`, never terminal.
- If one reservation remains `ACTIVE` or `UNKNOWN`, Batch 119 recovery and Phase
  One deployment remain blocked.

## Approaches considered

### Ignore old rows in deployment preflight

Rejected. A time cutoff would hide durable exchange outcomes and violate the
existing fail-closed gate.

### Expand the Batch 119 recovery allowlist

Rejected. Batch 119 has a fixed batch, leg, source, target quantity, and natural
stop proof. Adding 29 unrelated close reservations would destroy that closed
scope and couple two independent recovery authorities.

### Dedicated reservation convergence command

Selected. A separate read-only capture, deterministic classifier, fingerprinted
plan, and CAS apply keep the new authority narrow and independently removable.

## Components

### Closed-scope module

Add `bound_close_reservation_recovery.py`. It owns:

- strict loading of every reservation in one of the existing nonterminal states
  `reserved`, `submitted`, `submit_unknown`, `unknown_exchange_outcome`, or
  `recovery_required`;
- a hard population limit of 64 rows, with `LIMIT 65` overflow refusal;
- one coherent SQLite `mode=ro`, `PRAGMA query_only=ON`, explicit `BEGIN`
  snapshot for reservation, binding, exact execution event, exact position
  mutation, and exact entry-leg facts;
- a capability-limited Deepcoin reader exposing GET methods only;
- deterministic per-reservation classification;
- serialization containing only bounded counts, closed enums, timestamps,
  SHA-256 references, and fingerprints;
- a separately authorized CAS apply that can update only a proven reservation
  from its exact previous state to `confirmed` and append one immutable audit
  event.

The module must not import an exchange executor or accept an arbitrary batch,
table, status, SQL fragment, or update payload.

### CLI

Add one dormant command:

```text
recover-bound-position-close-reservations
```

Dry-run is the default. Apply requires all of:

- `--apply`;
- the fresh `--expected-fingerprint`;
- the exact expected action count;
- a generated confirmation token bound to that fingerprint;
- a fixed stopped-service capture authorization string defined by the final
  implementation plan.

There is no `--force`, row-id selector, ignore list, age threshold, notification,
or exchange-write option.

## Evidence capture

### Local authority

For each nonterminal reservation, load and bind:

- reservation id, exact `pos_id`, binding id, status, error, and timestamps;
- exactly one referenced execution binding and its instrument/side identity;
- exactly one matching `close_bound_position_market` execution event carrying a
  nonempty exchange order id and the same binding and position identity;
- any exact `close_position` mutation for the same binding, leg, and position;
- the exact owned entry leg when one exists.

Missing rows, duplicate matching close events, identity disagreement, malformed
JSON, invalid timestamps, unknown statuses, or an unexpected extra descendant
make that reservation `UNKNOWN`. Any source population change changes the source
fingerprint.

### Exchange authority

The read-only client collects:

- one complete current-position snapshot;
- one complete pending regular-order snapshot;
- for each distinct exact order id, bounded order history and trade fills using
  `ordId` and `limit=100`;
- for each distinct exact position id, bounded position history using `posId`
  and `limit=100`.

Every collection must have valid schema, identity, completion metadata, a
bounded response size, and a bounded wall-clock deadline. A response at the row
limit without trustworthy completion proof is ambiguous. The client rejects all
non-GET methods and does not inherit ambient proxy or custom CA environment.

Dry-run uses two stopped-service captures. Capture times may differ, but source
population, exact identities, classifications, and semantic fingerprints must
match. Apply performs a new capture and rebuilds the plan inside the same
operation; a saved snapshot or dry-run capability cannot be reused.

## Classification

Each reservation has exactly one outcome.

### `PROVEN_TERMINAL`

This outcome requires one continuous identity chain from reservation to binding,
close event, exchange order, and exact position. It also requires:

- the exact close order has a successful terminal exchange state;
- fills and/or order quantities prove the intended close was not merely accepted;
- exact position history proves the same position reached its terminal closed
  quantity after the reservation and order event;
- the exact position is not currently live;
- the exact close order is not currently pending;
- no contradictory order, fill, mutation, or position fact exists.

A previously confirmed exact close mutation can participate in this proof but
does not replace the current and historical exchange checks.

An active parent binding does not automatically refuse terminalization: a
multi-position binding can retain another live sibling. Only the exact `pos_id`
and its complete evidence decide the reservation outcome, and the binding itself
is never changed by this tool.

### `ACTIVE`

The exact position or close order is currently active, or the exchange reports a
nonterminal close outcome. No action is proposed. The result blocks Phase One.

### `UNKNOWN`

Any missing, delayed, incomplete, conflicting, malformed, oversized, timed-out,
or identity-mismatched evidence produces `UNKNOWN`. No action is proposed. The
result blocks Phase One.

This avoids premature failure during delayed exchange callbacks: an unobserved
terminal result remains `UNKNOWN`; it is never converted to terminal merely
because a deadline or age threshold elapsed.

## Plan and fingerprint

The plan contains:

- exact schema version and mode;
- total, terminal, active, and unknown counts;
- one redacted SHA-256 reservation reference per item;
- each item's closed classification and closed reason code;
- source and exchange snapshot fingerprints;
- proposed action count;
- one overall evidence fingerprint and derived confirmation token;
- fixed `exchange_writes=0` and `history_replays=0` counters.

Raw database ids, `pos_id`, order ids, chat/message ids, prices, sizes, provider
payloads, credentials, and source text are never serialized or logged.

Any `ACTIVE` or `UNKNOWN` item makes the overall plan `refused`, even when other
items are proven terminal. This deliberately prevents a partial cleanup from
being misrepresented as a deployment-ready result. The dry-run may still report
the bounded counts needed to explain the refusal.

## Apply transaction

Apply runs only with all database-writing services stopped and no unlisted local
Telegram/Deepcoin process. Before the new capture, it requires a verified 0600
SQLite backup and `PRAGMA quick_check=ok`.

Inside `BEGIN IMMEDIATE`, apply:

1. reloads the complete source population;
2. rebuilds every local fingerprint;
3. verifies MiMo remains v1;
4. verifies the fresh exchange plan equals the approved fingerprint and action
   count;
5. CAS-updates each exact reservation from its planned nonterminal state to
   `confirmed`, clearing only `last_error` and updating `updated_at`;
6. appends one bounded aggregate
   `bound_close_reservation_history_converged` execution event containing only
   redacted reservation references and bound to the evidence fingerprint;
7. commits once, or rolls back everything.

Repeated apply with the same fully verified result is idempotent and creates no
additional events. A mixed partially applied state, unexpected confirmed row,
new reservation, changed source, changed exchange result, or event mismatch is a
conflict, not an invitation to resume.

## Production workflow and approval boundaries

1. Implement and fully test locally on the dedicated recovery branch.
2. Perform independent Critical/Important review.
3. Request a stopped-service, read-only double-capture approval.
4. Run two captures, restore all original service states, and report only the
   redacted stable/refused result.
5. If and only if every row is `PROVEN_TERMINAL`, request a separate apply
   approval with the fixed authorization token.
6. Enter a new stopped-service window, back up the database, recapture, apply,
   verify exact row/event deltas, and restore services.
7. Re-enter the already approved Batch 119 recovery workflow in a separate
   window.
8. Re-run ordinary deployment preflight and continue Phase One only after a
   fresh stable exchange snapshot and no blocking work.

The reservation recovery window, Batch 119 recovery window, and ordinary code
deployment window must remain three separate operations.

## Tests

Tests must cover:

- exact source population, overflow, malformed schema, duplicate events, and
  identity conflicts;
- successful terminal order plus exact closed-position history;
- current position, pending order, rejected/cancelled/partial order, missing
  history, delayed callback, page-limit ambiguity, timeout, and response-size
  refusal;
- active parent binding with one proven closed sibling;
- two semantically stable captures and every drift dimension;
- strict redaction and fixed output schema;
- inability to construct or call an exchange writer;
- apply authorization, fingerprint, action-count, confirmation-token, MiMo v1,
  service-stop, backup, and CAS boundaries;
- all-or-nothing rollback and idempotent repeat;
- proof that only reservation status/error/timestamp and the one aggregate audit event can
  change;
- unchanged Batch 119, batches 123/127/129, trading settings, messages,
  management rows, bindings, lifecycles, legs, mutations, and exchange state;
- deployment preflight remaining blocked for any `ACTIVE` or `UNKNOWN` result and
  becoming eligible only after all reservations and Batch 119 independently
  converge.

## Rollback and deletion

Before apply, rollback is simply no database change. During apply, any exception
rolls back the one transaction. After a successful apply, the verified pre-apply
backup is the emergency rollback artifact; it must not be restored while any
service is running and requires separate operator authorization.

The CLI is a recovery utility, not a daemon or timer. It is never installed as a
service and has no automatic execution path. After Phase One evidence is
recorded, its future removal can be reviewed independently; it must not be
folded into the old Monitor cleanup task.
