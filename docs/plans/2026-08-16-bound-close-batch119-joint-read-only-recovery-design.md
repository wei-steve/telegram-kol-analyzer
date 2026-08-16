# Bound-Close and Batch 119 Joint Read-Only Recovery Design

**Date:** 2026-08-16

**Status:** Approved by the operator on 2026-08-16

## Objective

Break the recovery-order deadlock between the 29
`bound_position_close_reservations` and the exact Batch 119 false-submission
incident without weakening the ordinary deployment gate. Use one stopped-service
window to collect independent read-only evidence for both incidents, then retain
separate, freshly authorized apply windows for each recovery.

MiMo remains on v1 throughout this work. The design does not authorize a
production database write, an exchange write, a historical message replay, a
code deployment, or MiMo v2 activation.

## Confirmed Production Evidence

- Production remains at
  `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`.
- The reviewed bound-close candidate is
  `6c4fb31b4625ba706d23d35cda2034fbdfeeca8f`.
- The bound-close target population is exactly 29 reservations.
- Writer-quiescence inspection reports exactly two fresh management facts: one
  `strategy_management_batches` row in `reconciling` and one
  `strategy_management_legs` row in `submitted`.
- The batch is the exact known Batch 119 false-submission incident:
  `partial_then_break_even`, reason
  `management_close_pending_exchange_confirmation`, live mode, no execution
  deadline, and no durable progress watermark.
- Its fresh leg has no client order ID, exchange order ID, or response and has
  the known durable error evidence. The existing Batch 119 recovery already
  owns the complete exact-state and exchange-evidence classifier.
- Repeated aggregate reads show the batch and leg `updated_at` values changing
  every few seconds while the durable business state remains unchanged.
- The ordinary writer helper uses a ten-minute `updated_at` window. The retry
  loop therefore makes the incident permanently fresh and prevents the
  bound-close stopped-service window from starting.
- The failed production attempt stopped during live pre-quiescence. No service
  was stopped, no exchange capture ran, no database write occurred, the
  production SHA remained unchanged, and SQLite `quick_check` returned `ok`.

## Root Cause

Two independently correct recovery guards form a circular dependency:

1. bound-close recovery refuses while Batch 119 appears to be fresh management
   work;
2. Batch 119 recovery refuses while the 29 bound-close reservations appear to
   be active writer authority.

The immediate liveness signal is also semantically wrong for the known Batch
119 incident. Its retry loop updates `updated_at` without changing business
authority. Waiting longer cannot make it historical. Reducing the freshness
window, ignoring management rows globally, or treating every timestamp refresh
as harmless would all weaken safety and are rejected.

## Approaches Considered

### A. Joint read-only evidence, separate applies — selected

Treat the 29 reservations and the exact Batch 119 false-submission state as a
closed recovery incident set. Admit a stopped-service diagnostic only when two
live read-only snapshots prove that the set's material authority is unchanged
and every other writer is quiescent. After stopping all writer units, collect
two new bound-close captures and two new Batch 119 captures. Restore services
and stop. Apply the two recoveries later under separate permits and fresh
captures.

This resolves the circular dependency while preserving the ordinary deployment
gate and the separation between diagnostic and mutation authority.

### B. Recover Batch 119 first by exempting the 29 reservations

Rejected. The reservations represent possible close authority. Allowing Batch
119 to mutate management state while their exchange outcome remains unresolved
would make the dependency order unsafe.

### C. Wait, shorten the freshness window, or stop services until rows age

Rejected. The worker refreshes the rows continuously, so waiting while it runs
cannot succeed. Stopping merely to age the rows would hide unresolved authority,
not prove it. Time alone cannot establish exchange terminality.

## Architecture

### 1. Recovery-specific joint incident admission

Add a recovery-only read-only admission helper. It must not modify
`deployment_preflight.py` or change ordinary deployment decisions.

The helper accepts only this closed incident population:

- exactly the approved bound-close reservation target set and statuses;
- exactly Batch 119 with the existing false-submission topology and durable
  evidence contract;
- zero additional fresh, unknown, or age-independent writer facts.

The Batch 119 portion reuses the existing authoritative local snapshot loader
and state validator. It must not duplicate or weaken the Batch 119 classifier.
No other batch ID, topology, status, reason, leg, component population, or
durable error shape is accepted.

Admission is authority to attempt quiescence only. It is not a deployment
decision, terminal classification, exchange conclusion, apply capability, or
notification authority.

### 2. Material authority fingerprint

Create a bounded canonical fingerprint for the exact recovery incident set.
It contains every field that can change business meaning, including:

- reservation population and state;
- Batch 119 status, reason, mode, action, topology, and source identity;
- leg status and the presence and content fingerprints of request, response,
  client-order, exchange-order, snapshot, and error evidence;
- component state, attempt counts, evidence, deadlines, and completion facts;
- binding, lifecycle, entry-leg, mutation, and protection ownership facts used
  by the existing Batch 119 source loader.

The fingerprint excludes only the explicitly audited retry-heartbeat timestamps
whose mutation does not change authority. It must not exclude status-transition,
exchange-event, deadline, progress, completion, or evidence timestamps.

Two live coherent SQLite snapshots, separated by the existing bounded polling
interval, must have the same material fingerprint. The ordinary aggregate
counts must also remain identical. Any material change, unknown field, malformed
time, missing table, population drift, or new writer refuses before any service
stop.

### 3. Stopped-service joint diagnostic

After live admission, use the existing complete unit inventory, original-state
capture, process scan, production SHA check, database path/device/inode check,
legacy-monitor transient handling, and shared absolute deadline.

The sequence is:

1. establish the stopped-phase absolute deadline before any stop;
2. stop and verify all timers;
3. converge only the approved legacy monitor transient state;
4. stop and verify all database/writer services and sockets;
5. prove no relevant process remains;
6. reload the joint incident set from one coherent query-only transaction and
   require the same material fingerprint as live admission;
7. run one post-stop aggregate writer check and require no writer outside the
   closed incident set;
8. take Batch 119 capture 1 with a fresh dedicated reader;
9. take bound-close capture 1 with a different fresh dedicated reader;
10. take Batch 119 capture 2 with a third fresh dedicated reader;
11. take bound-close capture 2 with a fourth fresh dedicated reader;
12. compare each pair with its existing closed parser and semantic comparator;
13. require the joint material fingerprint and database identity to remain
    unchanged;
14. restore all original service states and remove all private artifacts.

The four captures are sequential and share the stopped-phase hard deadline.
Before capture 1, admission must reserve the complete worst-case budget for all
four capture limits plus local projection, comparison, identity verification,
and cleanup. Every subprocess runs under the same remaining absolute deadline
with direct process-group kill at expiry. A timeout, signal, malformed output,
or refused first capture makes all later captures unreachable.

The successful operator projection contains only fixed status, classification
counts, semantic fingerprints, zero-write counters, unchanged SHA, and service
restoration status. It contains no raw database IDs, reservation references,
position/order/message IDs, provider rows, timestamps, credentials, or
capabilities.

### 4. No authority crosses windows

The joint diagnostic always restores services and stops. Its permits, capture
documents, fingerprints, database copies, reader objects, and process-local
capabilities are destroyed and cannot authorize an apply.

#### Bound-close apply window

- obtains new bound-close apply authorization;
- repeats the complete unit stop and identity proof;
- reloads the same joint incident material authority;
- recaptures bound-close exchange evidence directly from the production
  database with a fresh reader;
- changes only the exact proven-terminal reservation rows and one redacted
  audit event under the existing transactional/CAS authority;
- proves Batch 119 material authority is unchanged before reporting success.

#### Batch 119 apply window

- obtains the existing separate Batch 119 apply authorization;
- requires the reservation target population to be confirmed and absent from
  active writer facts;
- performs a new Batch 119 source load and fresh exchange capture;
- applies only the existing allowlisted Batch 119 recovery;
- proves the confirmed reservations remain unchanged.

The two authorization contracts are distinct and cannot be combined, reordered,
or reused. Batch 119 apply remains second because unresolved reservations may
represent close authority.

### 5. Ordinary deployment remains unchanged

`deployment_preflight.py` receives no exception and no Batch 119 allowlist. It
continues to block until the reservations are confirmed, Batch 119 is terminal
or evidence-backed safe, all other writer facts satisfy the existing policy,
and normal snapshot/readiness requirements pass.

Only after both recovery applies and a fresh ordinary preflight may the reviewed
code deployment resume. MiMo remains v1, and the final deployed SHA becomes the
new baseline for later MiMo v2 work.

## Failure Handling

- Any incident-set mismatch refuses before service stop.
- Heartbeat-only timestamp changes are tolerated only when the complete material
  fingerprint remains identical across both live snapshots.
- Any material field change, new writer, unknown state, missing source row,
  schema drift, malformed JSON/time/decimal, or row-limit overflow fails closed.
- Any unit, process, SHA, database identity, or dynamic inventory drift refuses.
- Any capture timeout, response overflow, pagination overflow, provider schema
  conflict, incomplete evidence, or semantic instability restores services and
  consumes the diagnostic permit.
- A failed or refused diagnostic cannot be retried in the same stopped window.
- Apply commit ambiguity uses the existing close-writer/fresh-read-only exact
  verification rules. It never blindly retries or restores an older database
  after a request might have reached an external system.
- No path may hand-edit SQLite, replay a historical message, submit/cancel an
  exchange order, change TPSL, send an operator notification, deploy code, or
  enable MiMo v2.

## Verification

### Local tests

- heartbeat-only `updated_at` changes keep the material fingerprint stable;
- every other relevant Batch 119 and reservation field changes the fingerprint
  or refuses;
- another batch, reservation count/status drift, unknown/NULL state, new writer,
  missing table/column, malformed evidence, or excessive population refuses;
- live admission requires two distinct coherent snapshots and does not issue a
  capture or apply capability;
- post-stop reload must match the live material fingerprint;
- all four captures use distinct fresh readers and cannot reuse cached or
  serialized authority;
- first-capture refusal/timeout makes later captures unreachable;
- the single stopped-phase absolute deadline covers stop, verification, all
  four captures, comparison, and cleanup;
- diagnostic output proves database writes, exchange writes, and history replays
  are zero;
- bound-close apply cannot mutate Batch 119;
- Batch 119 apply cannot mutate confirmed reservations;
- authorization tokens and artifacts cannot cross windows;
- ordinary deployment remains blocked until both incidents converge;
- `deployment_preflight.py` has no production diff from the reviewed baseline.

Run focused recovery, Batch 119, writer-quiescence, runbook, CLI, Deepcoin
read-only authority, deployment-preflight, compileall, diff, Bash syntax, and
full repository tests. Resolve every Critical and Important review finding.

### Production checkpoints

1. push one exact reviewed candidate SHA;
2. obtain new joint stopped-read-only approval;
3. run the joint diagnostic and stop after service restoration;
4. obtain new bound-close apply approval and apply only reservations;
5. obtain new Batch 119 apply approval and apply only Batch 119;
6. capture stable normal monitor evidence;
7. run ordinary deployment preflight;
8. obtain ordinary deployment approval;
9. deploy and record the final production SHA as the later MiMo v2 baseline.

## Completion Boundary

This design authorizes documentation and local planning only. It does not
authorize implementation, push, production service stop, exchange capture,
database apply, Batch 119 recovery, ordinary deployment, or MiMo v2 activation.
