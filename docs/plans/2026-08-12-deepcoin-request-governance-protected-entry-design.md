# Deepcoin Request Governance and Protected Multi-Leg Entry Design

**Date:** 2026-08-12

**Status:** Approved

## Objective

Prevent a transient Deepcoin read failure, UID rate-limit collision, or ambiguous
write outcome from creating an unprotected or duplicated multi-leg entry. The
same durable execution state must drive automatic trading, reconciliation, and
the Web UI; human-readable error strings must never authorize a transition.

The design adds four cooperating boundaries:

1. one UID- and endpoint-aware request governor for every Deepcoin request;
2. bounded, category-aware retry for safe reads only;
3. a durable entry -> protection -> next-leg state machine; and
4. structured attempt, snapshot, and outcome evidence for recovery and display.

This work is separate from the dedicated batch 119 recovery. It does not replay,
migrate, or repair the known frozen two-leg entry incident.

## Incident Evidence

The production incident that motivated this design had the following durable
shape:

- the first market leg was submitted, verified, and created a live position;
- two position stop-loss writes were confirmed and remain visible by exact order
  identity;
- no second entry leg, trigger-protection intent, or second-leg exchange writer
  fact exists;
- the second-leg path failed with
  `trigger_protection_baseline_unavailable`; and
- the failure happened before the second-leg POST.

The current submission sequence performs two protection writes with immediate
readback and then makes another pending-TPSL read for the second-leg baseline.
That baseline GET has no endpoint-aware limiter or bounded retry. Its underlying
HTTP, transport, or response exception was collapsed into one generic error, so
the historical record cannot now distinguish a 429, transient network error,
or API response failure.

This was not evidence that a successful read returned an empty list. The error
means the read itself raised. Rapid sequential calls probably contributed, but
the missing structured cause prevents a stronger historical conclusion.

## Safety Constraints

- Unknown POST outcomes are never retried as POSTs.
- A later entry leg cannot be submitted until every required protection for the
  existing live exposure is exactly read back and confirmed.
- A failed or incomplete read is not equivalent to an empty exchange result.
- A retry belongs to the current operation and its original decision window.
  Entry pre-submit recovery has a total deadline of approximately ten seconds.
- A pre-submit read that is still unavailable after the deadline becomes
  `pre_submit_deferred`; no timer may submit that stale leg later.
- Supervised recovery creates a new operation only after fresh authority, price,
  risk, duplicate-order, contract-specification, position, and protection
  validation.
- Existing records are not automatically migrated to the new state machine.
- The known frozen two-leg incident stays unchanged unless a separate,
  explicitly approved recovery is designed.
- Batch 119 recovery and this generic client/execution refactor are never
  deployed or exercised as one combined mutation.
- API credentials, passphrases, signatures, raw authorization headers, and raw
  provider payloads are never written to durable attempt evidence or Web output.

## Approaches Considered

### A. Patch only the pending-TPSL baseline call

Add a sleep and retry around `_normalized_pending_tpsl_baseline`.

This is too narrow. It leaves every other GET ungoverned, preserves per-process
UID collisions, keeps unknown POST behavior underspecified, and does not fix the
current catch-and-continue exposure defect.

### B. Layered governor, state machine, and evidence model — selected

Route every request through one governor, distinguish read and write outcome
semantics, and drive both automation and the Web from one durable operation
state. Introduce the layers in independently gated stages.

This addresses the incident without replacing the entire trading executor and
allows each safety invariant to be proved before writer behavior changes.

### C. Central exchange queue service

Move every exchange request into a single service with a persistent queue.

This would provide the strongest cross-process serialization, but it is a much
larger operational change. It would alter deployment, ownership, reconciliation,
and failure recovery simultaneously and is not justified for the current scope.

## Architecture

```text
automatic trade / reconciliation / Web refresh
                  |
                  v
       durable operation state machine
                  |
                  v
         Deepcoin request governor
        /           |             \
 UID budget   endpoint budget   priority budget
        \           |             /
                  v
               Deepcoin
                  |
                  v
       condition-based readback
                  |
                  v
    durable attempt + snapshot evidence
```

The request governor is transport infrastructure. It does not decide strategy
identity, target a lifecycle, construct trade economics, or replace MiMo and the
existing contextual strategy authority.

The operation state machine owns transition safety. It receives an already
authorized, exact order draft and decides only whether the next exchange phase
is safe to execute.

## Request Governor

### Identity and scope

Every private Deepcoin request is keyed by:

- a one-way hash of the credential UID;
- normalized HTTP method; and
- normalized API path with the query string removed.

No secret is part of the key or persisted governor state. All client instances
in the service share the governor. A root-only runtime lock/state file, or an
equivalent small local coordination store, extends the same budget across the
service, CLI, and other local processes using that UID.

The governor waits before generating the exchange timestamp and signature so a
queued request does not carry a stale signature.

### Operational budgets

The system operates at approximately 80 percent of the documented UID limits:

| Documented limit | Operational budget | Representative reads/writes |
| --- | --- | --- |
| 5/second, 150/minute | 4/second, 120/minute | pending TPSL, trigger/order/fill history |
| 10/second, 300/minute | 8/second, 240/minute | positions, pending regular orders |
| 15/second, 450/minute | 12/second, 360/minute | entry and TPSL writers |
| 1/second, 60/minute | one per 1.25 seconds, 48/minute | stricter documented endpoints |

References:

- <https://www.deepcoin.com/docs/rateLimit>
- <https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrdersPending>
- <https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrder>

The existing TPSL writer limiter runs at the exact provider ceiling and covers
only set/cancel position-TPSL calls. It is replaced or wrapped by the shared
governor rather than being allowed to double-throttle independently.

### Priority isolation

The governor recognizes three execution priorities:

1. risk-reducing writes and post-write readback;
2. ordinary entry preflight and submission; and
3. periodic reconciliation, Web refresh, and audit traffic.

Background traffic receives a smaller sub-budget and may wait at most about two
seconds before deferring its cycle. It cannot consume the reserved capacity
needed for protection readback. Critical reads may wait only within their
operation deadline; priority does not bypass the UID or endpoint ceiling.

### Circuit behavior

Three retryable failures or rate-limit responses for the same UID/path inside
30 seconds pause background traffic for 15 seconds. Continued failures may grow
the pause to at most 60 seconds. Critical readback may still make a governed
attempt within its deadline. A circuit never authorizes a repeated writer.

## Request and Retry Semantics

### Safe reads

Transport connect/read timeouts and HTTP 408, 425, 429, 500, 502, 503, and 504
are retryable. A malformed JSON or schema response receives at most one extra
attempt and then becomes `schema_incompatible`.

Authentication failures, ordinary non-transient 4xx responses, explicit
business rejection, identity conflict, and contract conflict are not retried.

Critical reads use at most four attempts inside a ten-second total deadline.
Nominal delays are 0.5, 1, and 2 seconds with bounded jitter. `Retry-After` is
honored only within the remaining deadline. Each request timeout is bounded by
the remaining operation time. Background reads use at most two attempts inside
five seconds, then defer.

### Writers

The governor acquires budget before a writer is signed and submitted.

- A local pre-send failure known to occur before network submission can be
  retried inside the current operation deadline.
- An explicit exchange business rejection is terminal for that operation.
- Any transport, HTTP, close, or response-decoding ambiguity after submission
  produces an `unknown` outcome.
- An unknown writer is never sent again.
- A successful or ambiguous writer is reconciled only through safe GETs using
  stable client/order identity.

Closing an HTTP client must not convert an already parsed successful response
into an unknown outcome. Production request paths reuse a scoped persistent
HTTP client instead of opening and closing one connection per request.

## Snapshot Authority

An exchange read result has separate availability, schema-validity, and
completeness facts. An unavailable result is never represented as `[]`.

A snapshot records:

- capture start and end time;
- every endpoint and page used;
- endpoint availability and schema version;
- row count and pagination/completeness proof;
- canonical per-collection fingerprint; and
- the local exchange-write generation before and after capture.

If a local writer runs during capture, or an authority endpoint changes across
the capture boundary, the complete snapshot is discarded and retried within the
caller deadline. External exchange changes that make the rows internally
inconsistent also fail closed.

The pending trigger endpoint returns at most 100 rows. A 100-row result without
reliable pagination or another completeness proof is `incomplete`, not a
complete account absence proof.

Read-only loaders must propagate endpoint errors into snapshot evidence. The
current behavior that substitutes an empty trigger list after an exception is
removed from authoritative safety paths.

## Durable Entry State Machine

Web and automatic execution read the same canonical operation rows. The Web
does not reconstruct state by parsing display strings.

### First entry leg

Before POST, persist an immutable intent containing exact instrument, side,
quantity, order economics, stable client-order identity, request fingerprint,
contract version, and operation deadline.

The submit transition produces exactly one of:

- `entry_confirmed`: exact exchange readback proves the order/position;
- `entry_rejected`: the exchange explicitly rejected it;
- `entry_pending_readback`: submission was accepted but complete evidence is
  still pending; or
- `entry_unknown`: submission may have reached the exchange.

Both pending and unknown states forbid another POST with the same logical
intent.

### Protection gate

Once a live position exists, create one durable protection intent for every
required stop/protection leg before sending any protection writer. Each intent
advances independently through `prepared`, `submitted`, `pending_readback`, and
`confirmed` or a bounded failure/unknown state.

The aggregate becomes `protected` only when every required protection is
confirmed by exact order identity and economics. One confirmed protection is
not sufficient. Any failure, unknown outcome, missing position identity, or
incomplete readback freezes all later exposure-increasing work.

The existing broad exception handler that records a warning and continues to a
later entry leg is removed from the new state-machine path. This design does not
introduce an unapproved automatic emergency close; it stops added exposure and
leaves risk-reducing recovery to an already authorized policy.

### Later entry leg

A later leg is considered only after the aggregate protection state is exactly
`protected`.

Persist a `pre_submit` operation before acquiring its pending-TPSL baseline.
The baseline must be available, schema-valid, and complete. A retryable read may
recover only within the current ten-second decision window.

If the window expires before POST, persist `pre_submit_deferred`. The operation
contains no writer attempt and no timer may resume it. If POST occurs and the
result is unknown, persist `entry_unknown` and reconcile by stable client/order
identity without resubmission.

The overall entry reaches `completed` only when every submitted leg and every
required protection is exactly confirmed.

## Structured Outcome Evidence

Do not encode control state in one error name. Persist three orthogonal facts:

| Dimension | Representative values |
| --- | --- |
| `phase` | `entry_submit`, `entry_readback`, `protection_submit`, `protection_readback`, `next_leg_preflight` |
| `outcome_certainty` | `not_sent`, `accepted`, `rejected`, `unknown`, `confirmed` |
| `error_category` | `rate_limited`, `transport_timeout`, `auth_failed`, `business_rejected`, `snapshot_incomplete`, `schema_invalid`, `state_conflict` |

Each immutable request-attempt record contains bounded, sanitized facts:

- operation and attempt ordinal;
- phase, endpoint, method, start/end time, and latency;
- limiter wait, retry delay, and bounded `Retry-After`;
- HTTP status and exchange business code when safely available;
- request fingerprint and hashed stable exchange identity references;
- snapshot fingerprint/completeness reference;
- safe exception type and bounded display message; and
- the resulting outcome certainty.

`error_message` is display-only. Retry eligibility is derived from the closed
structured categories, phase, certainty, attempt budget, and deadline, never
from punctuation or substring matching.

## Parent Signal and Lifecycle Semantics

The current `mark_trade_signal_failed` path invalidates an open-position
lifecycle and closes its TradeIdea whenever an open-position TradeSignal fails.
That is incorrect when a first leg or live position already exists.

Canonical execution state distinguishes:

- `active_protection_pending`: live exposure exists and protection is not fully
  confirmed;
- `active_protected_deferred`: current exposure is protected but a later leg was
  never sent;
- `recovery_required`: an unknown or conflicting live operation requires exact
  reconciliation; and
- `submission_failed_no_exposure`: complete evidence proves there is no live
  position, pending order, or unknown writer.

Only the last state may use the existing terminal failure behavior. A live or
unknown operation cannot invalidate the lifecycle or close its TradeIdea.

`TradeSignal.status` remains temporarily as a compatibility projection so old
queries do not break in one migration. New automation and the Web read the
canonical execution operation. The projection is written from canonical state;
it is not an independent authority and no component may reverse-engineer an
operation from that summary string.

## Recovery Rules

| Durable fact | Allowed automatic action |
| --- | --- |
| `not_sent` plus retryable local/read failure | retry inside the same ten-second operation window |
| GET unavailable or snapshot incomplete | retry GET only |
| explicit rejection | persist terminal operation rejection; no resend |
| POST unknown | exact GET reconciliation only; no resend |
| accepted but pending readback | exact GET polling only |
| `pre_submit_deferred` | no scheduled submission |
| state/identity conflict | stop and require supervised review |

A supervised attempt after `pre_submit_deferred` creates a new operation. It
must acquire the exact strategy/position lock and revalidate:

1. current strategy and lifecycle execution authority;
2. current price against the original entry conditions;
3. current risk budget, position side, and position size;
4. complete current protection;
5. complete pending, history, and fill evidence proving no duplicate leg;
6. current contract specifications; and
7. a new deadline, request fingerprint, and idempotency identity.

An unknown writer can advance when exact exchange evidence confirms it. It
cannot become safe to resend merely because one current snapshot does not show
the order. Any new recovery writer requires separately proven terminal absence
and an explicitly authorized new recovery operation.

## Background and Web Execution

Network-heavy synchronous reconciliation must not run directly on the async Web
event loop. Periodic reconciliation and manual sync execute in an isolated
worker/thread boundary and return bounded progress/status to the route.

The Web execution panel shows, from the canonical operation:

- phase and outcome certainty;
- attempt count and operation deadline;
- limiter wait and readback latency;
- latest complete snapshot time;
- blocking reason and allowed next action; and
- whether the operation is automatically retryable, read-only reconcilable, or
  supervision-only.

## Testing Strategy

Implementation follows TDD with deterministic clocks and no wall-clock sleeps.

### Governor and transport

- first and subsequent request pacing for every documented budget class;
- second/minute windows and 20-percent headroom;
- priority isolation and bounded background deferral;
- shared client, thread, and local-process UID coordination;
- timestamp/signature creation after governor wait;
- `Retry-After`, jitter, deadline, timeout, and circuit behavior;
- persistent connection reuse and close-error containment; and
- no credential or raw header persistence.

### Fault matrix

Inject connect/read timeout, HTTP 408/425/429/500/502/503/504, auth failure,
business rejection, malformed JSON, schema drift, truncated 100-row TPSL,
response-after-POST disconnect, and database failure at every pre/post-write
commit boundary.

### State-machine invariants

- protection not fully confirmed implies zero next-leg POSTs;
- an unknown writer produces at most one POST for its operation identity;
- unavailable reads never become empty results;
- a live position prevents lifecycle invalidation;
- pre-submit retry ends at the ten-second deadline;
- process restart resumes readback, not submission;
- exact duplicate/order drift fails closed;
- a state-machine version is pinned for the operation lifetime; and
- the known frozen incident and batch 119 rows remain byte-for-byte unchanged.

Use a local fake exchange integration server for concurrency and crash-boundary
tests. Real Telegram identity, credentials, and Deepcoin verification remain
server-only. Server verification begins read-only; no synthetic live order is
used as a shortcut.

## Metrics and Alerts

Record bounded metrics by normalized endpoint and operation phase:

- request count, rate-governor wait p50/p95/max, and retry count;
- 429 and retryable/non-retryable error counts;
- readback latency and snapshot incomplete/unavailable counts;
- circuit-open duration;
- unknown writer count;
- `pre_submit_deferred` count; and
- duration of live exposure without fully confirmed protection.

Any unknown writer, live exposure without complete protection, authentication
failure, or response-schema change is immediately operator-visible. Background
rate-limit pressure alerts before it consumes the critical reserve.

## Compatibility, Feature Gates, and Rollback

Changes are additive. New operation/attempt fields or tables are introduced
without rewriting historical TradeSignals.

Separate settings gate:

1. request-governor telemetry;
2. enforced background/read governance;
3. protected-entry state machine for newly created signals; and
4. enforced writer governance.

Telemetry mode observes real request facts but creates no simulated or shadow
orders. It does not delay execution. Each new operation pins its state-machine
contract version at creation.

Before a versioned operation sends any writer, its feature can be disabled and
the operation stopped normally. After a writer is attempted, that operation
remains owned by the same version's read-only reconciliation path; rollback
cannot hand it to legacy code or authorize a repeated POST. A feature gate may
stop new v2 operations without changing ownership of in-flight v2 operations.

## Staged Rollout

1. **Foundation:** deploy additive schema, structured evidence, connection
   reuse, governor, and tests with execution behavior unchanged.
2. **Read governance:** enforce background/Web GET governance, then critical
   automatic-trading GET governance. Writers remain unchanged.
3. **New-signal state machine:** enable only for TradeSignals created after a
   recorded watermark. Historical rows remain on their pinned contract.
4. **Writer governance:** route new-version writers through the governor and
   unknown-outcome boundary.
5. **Legacy retirement:** remove old string-based transition paths only after
   every old operation is terminal and audited.

Every stage receives independent review, focused/full local tests, server-side
read-only verification, and an explicit deployment approval. The stages do not
auto-advance.

## Deployment Safety Boundary

Before any production restart or setting change:

1. verify the exact reviewed Git commit and create a database backup;
2. stop the service and prove no executing/submitting/pending-readback writer is
   in flight;
3. take a fresh, complete exchange snapshot;
4. prove every current live position remains protected;
5. prove no unknown writer exists; and
6. compare database and exchange fingerprints before and after restart.

The known frozen two-leg incident may be excluded from the generic active-work
gate only by exact durable identity, unchanged fingerprint, verified current
position, and verified protection. The exception authorizes no replay,
submission, position change, or lifecycle change. Any identity or exchange drift
aborts deployment.

Batch 119 remains governed by its dedicated reviewed planner, locked apply, and
runbook. This rollout neither executes nor relaxes that recovery.

## Acceptance Criteria

The design is complete when tests and reviewed server evidence prove:

- all Deepcoin calls use the documented shared governor budget;
- safe read retry is bounded and observable;
- no unknown writer can be resent;
- no later leg can increase exposure before exact protection confirmation;
- no read failure is converted into exchange absence;
- live exposure cannot be terminalized as no-exposure failure;
- deferred stale entries cannot be submitted by a timer;
- Web and automation use the same canonical operation state; and
- existing frozen production incidents remain untouched.
