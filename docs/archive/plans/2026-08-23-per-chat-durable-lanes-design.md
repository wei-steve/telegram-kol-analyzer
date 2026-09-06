# Per-Chat Durable Lanes and Correct Cross-Chat Admission Design

**Date:** 2026-08-23

**Status:** Approved for planning. Implementation has not started.

**Baseline:** `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`

## 1. Goal

Fix `KeyedAsyncLockRegistry.lock_all()` so a cross-key operation excludes both
existing and future keys, then safely operate the existing durable message
queue with at most three concurrent chat lanes in production.

This work changes scheduling, process-local admission, and observability only.
It must not change recognition results, MiMo v2.5 context authority, contextual
strategy resolution, position attribution, trading decisions, management
instructions, retry/recovery meaning, exchange execution, or exchange-write
semantics.

## 2. Verified Current State

The planning investigation independently verified the following at the stated
baseline:

- `KeyedAsyncLockRegistry.lock_all()` snapshots only the key locks that exist
  when it starts. A key created while `lock_all()` waits can enter immediately.
- `message_processing_jobs` is the sole durable message-processing queue.
- `claim_message_processing_jobs()` considers the oldest non-terminal row in
  each chat lane. A live lease or a retry that is not yet due blocks later rows
  in the same chat.
- `run_message_processing_worker_tick()` processes claims from different chats
  concurrently and currently defaults to `limit=20`, regardless of
  `message_lock_mode`.
- The current worker loop waits for the entire claimed batch. If one member of
  the batch is slow, unused slots are not refilled until that slow member ends.
- all trading settings are stored in one `trading_settings` JSON row. A single
  database commit makes a two-field value visible atomically, but the current
  HTTP path has no expected-state transition guard and a Web-process lock does
  not protect the ingest or worker process.
- the split topology gives live listener and Telegram reconcile ownership to
  ingest, durable queue and exchange authority to worker, and no singleton
  processing authority to Web.

The existing focused lock, worker, and settings tests passed, but they did not
cover future-key admission. A read-only minimal reproduction showed the new key
entering while `lock_all()` was waiting.

## 3. Non-Goals

This work will not:

- create one SQLite table, process, thread, or permanent `asyncio.Queue` per
  chat;
- create a second actor, mailbox, queue, or ordering authority;
- use an in-memory lock as cross-process synchronization;
- change queue status values, schema, retry count, lease duration, or durable
  settlement rules;
- re-enable DeepSeek semantic review or add a DeepSeek context fallback;
- replay historical analysis data, manufacture Telegram traffic, submit test
  trades, or add exchange writes;
- reopen or modify the completed runtime-serialization Phase 0-6 canonical
  pointer.

## 4. Options Considered

### 4.1 Reuse durable lanes and add correct admission plus bounded scheduling

Keep `message_processing_jobs` as the source of truth. Add a writer-preference
shared/exclusive admission barrier to the keyed registry, add an explicit
parallel-chat setting, and make the worker refill free slots as tasks finish.

This is the selected option. It preserves existing durable ordering and limits
the new concepts to process-local synchronization, an execution cap, and
in-memory observation.

### 4.2 Physical queue or worker per chat

This would multiply lifecycle, cancellation, failure, and monitoring state by
the number of chats. It still needs a global-operation barrier and introduces
more places where durable order could diverge. It is rejected.

### 4.3 A second actor or mailbox layer

An in-memory mailbox would need to reconstruct its ordering from SQLite after
restart and remain consistent with retry and leases. That creates two ordering
authorities. It is rejected.

## 5. Process-Local Shared/Exclusive Admission

### 5.1 State

`KeyedAsyncLockRegistry` will own one `asyncio.Condition` and the following
condition-protected state:

- `active_readers`: admitted per-key/global operations;
- `waiting_writers`: `lock_all()` callers that have announced exclusive intent;
- `writer_active`: whether one cross-key operation holds exclusive admission.

The existing per-key lock map and reference counts remain responsible only for
same-key serialization and bounded cleanup.

### 5.2 Shared admission

A reader waits while either `writer_active` is true or `waiting_writers > 0`.
After admission it increments `active_readers` and holds that admission for the
entire business critical section, including any wait for its per-key lock.

On exit or cancellation it decrements `active_readers`. When the last reader
leaves, it notifies waiting writers.

Holding shared admission while waiting for the per-key lock is deliberate. A
writer must drain both currently executing and already admitted same-key work;
otherwise a queued same-key operation could enter after exclusive admission.

### 5.3 Exclusive admission

A writer increments `waiting_writers` before waiting. That immediately blocks
future readers and provides writer preference. It waits for
`active_readers == 0` and `writer_active == false`, then atomically decrements
`waiting_writers` and sets `writer_active=true`.

On normal exit, exception, or holder cancellation it clears `writer_active`
and notifies all waiters. If cancelled before acquisition it decrements
`waiting_writers` exactly once and notifies waiters when required.

No key snapshot or repeated key enumeration is involved. Multiple writers are
exclusive and cannot admit a reader between queued writers while another
writer remains waiting. Strict FIFO ordering among writers is not required;
deadlock freedom and reader exclusion are required.

### 5.4 Registry cleanup

`lock(key)` acquires shared admission, takes a reference to the key lock, holds
the key lock for the user context, releases the reference, and finally releases
shared admission. Every partial-acquisition path is protected by `finally`.

The registry removes a key only after its reference count reaches zero and its
lock is unlocked. `lock_all()` does not manufacture key references, so it does
not impede cleanup.

## 6. MessageLockProvider Integration

Both configured modes must participate in the same admission boundary:

- `global`: shared admission followed by the existing global lock;
- `per_chat`: shared admission followed by the chat key lock;
- `lock_all()`: exclusive admission followed by the legacy global lock.

Holding the legacy global lock inside `lock_all()` preserves the observable
global-mode contract while the exclusive barrier is the actual future-key
admission gate.

Mode resolution must occur inside the provider context, not before admission.
This prevents a caller from resolving an old mode, waiting outside the
barrier, and entering after a transition with stale semantics.

Direct callers of `KeyedAsyncLockRegistry.lock(key)` retain the same public API
and automatically receive shared admission.

## 7. Durable Worker Scheduling

### 7.1 Source of truth

The worker will continue to claim exclusively through
`claim_message_processing_jobs()`. Its `BEGIN IMMEDIATE`, conditional update,
claim token, oldest-non-terminal lane ownership, retry date, and lease checks
remain unchanged.

No process-local key lock will be added to the durable worker. Same-chat safety
must not depend on process memory.

### 7.2 Explicit cap

Add `message_processing_max_parallel_chats` to `TradingSettings`:

- compatibility default: `20`;
- valid type: exact `int`, not `bool`;
- valid range: `1..20`;
- production cutover value: `3`.

The compatibility default prevents the code deployment from silently changing
the running queue before the cutover gate. The upper bound prevents a setting
from increasing concurrency beyond the previously possible default.

### 7.3 Work-conserving loop

The durable worker loop maintains only an ephemeral set of in-flight claim
tasks. On every refill cycle it:

1. reloads trading settings;
2. exits if `message_pipeline_mode != queue`;
3. computes `available = configured_cap - active_task_count`;
4. claims at most `available` chat lanes;
5. starts one task for each claim;
6. waits until one task completes or the normal poll interval expires;
7. settles completed tasks and refills free slots.

One slow chat consumes one of three slots. The other two slots continue to make
progress and can accept later chat lanes as they become free. The in-memory task
set is execution bookkeeping, not a queue or ordering authority.

### 7.4 Dynamic changes

Raising the cap permits additional claims on the next refill. Lowering the cap
does not cancel or preempt already executing claims; it stops new claims until
the active count falls below the new cap.

The production cutover requires zero claimed/in-flight message jobs before
changing `20 -> 3`, so the formal acceptance window cannot inherit an active
count above three.

### 7.5 Cancellation and restart

Loop cancellation cancels and awaits its asyncio wrappers but never rewrites a
durable claim to pending. A killed process leaves the claim for the existing
stale-lease recovery path. The oldest claimed row continues to block later rows
in the same chat until it is reclaimed and settled.

The change does not promise that Python can stop a synchronous function already
running in `asyncio.to_thread`. Production restart terminates the owning
process; local cancellation tests must verify durable claim behavior rather
than claim thread cancellation that Python does not provide.

## 8. Atomic Owner-Routed Settings Transition

### 8.1 Why the current update is insufficient

The two target values live in one JSON row and one commit is atomically visible,
but safety also requires:

- an expected-old-state check in the same write transaction;
- rejection of an incomplete global-to-per-chat request;
- process-local draining in the ingest process that owns the listener and
  reconcile operations;
- an unknown-outcome response that is verified rather than blindly retried.

A `lock_all()` acquired by the Web process cannot provide those ingest-process
properties.

### 8.2 Ownership routing

The existing `/api/trading-settings` route remains the external API. When a
request actually changes `message_lock_mode` or
`message_processing_max_parallel_chats`:

- Web proxies the bounded JSON request to the loopback ingest endpoint;
- ingest or monolith role executes the transition locally;
- worker role refuses the direct mutation;
- unrelated settings updates retain their existing path.

The loopback URL is fixed to HTTP localhost and the exact settings path, with a
bounded response. This is a control-plane routing decision, not a claim that an
HTTP request is a durable queue.

### 8.3 Transition request

The request accepts non-persisted expected-state fields:

- `message_lock_expected_mode`;
- `message_processing_expected_max_parallel_chats`.

For `global -> per_chat`, the payload must include both target fields and both
expected fields. A cap-only adjustment and `per_chat -> global` rollback are
allowed, but still require the relevant expected state.

### 8.4 Transaction

The ingest-owned handler:

1. acquires `message_lock_provider.lock_all()`;
2. opens `BEGIN IMMEDIATE`;
3. loads and validates the persisted settings row;
4. compares persisted mode and cap with the expected fields;
5. validates the merged candidate;
6. writes the complete JSON row once;
7. commits;
8. releases process-local exclusive admission.

The expected fields are stripped before persistence. A mismatch returns a
conflict and changes nothing. A validation failure returns 422 and changes
nothing.

If the Web-to-ingest response is lost after commit, the outcome is `unknown`.
The operator must GET the exact settings and compare both fields. Repeating the
transition is permitted only after that read proves it did not apply.

## 9. Observability

The existing process-local `/api/runtime/loop-health` endpoint remains pure
in-memory and read-only.

The worker role adds:

- `configured_max_parallel_chats`;
- `active_chat_lanes`;
- `peak_active_chat_lanes_since_limit_change`;
- `last_refill_claimed`;
- `total_started`;
- `limit_applied_at`.

The ingest role adds:

- `active_shared_admissions`;
- `waiting_exclusive_admissions`;
- `exclusive_admission_active`;
- `known_key_count`.

Chat IDs are not exposed. The peak resets when the applied cap changes, so a
pre-cutover compatibility peak does not contaminate the cap-three observation.
The database remains the evidence source for job identity, ordering, backlog,
lease, retry, settlement, and duplicate counts.

The cap is per worker process, not cluster-wide. Production topology checks
must prove exactly one worker process. An accidental second worker is an
authority failure even though atomic claim still prevents the same job from
being claimed twice.

## 10. Rollback

Two rollback levels are required:

1. **Process-local lock rollback:** atomically set `message_lock_mode=global`
   through the ingest-owned transition. Keep cap at three.
2. **Concurrency fail-closed rollback:** for scheduler, duplicate, SQLite, or
   concurrency anomalies, atomically set `message_lock_mode=global` and cap to
   one.

Changing only `message_lock_mode` cannot serialize the durable worker because
the queue already runs independently of that setting. The cap-one rollback is
therefore the actual durable-lane fail-closed control.

Neither rollback requires a restart. Both use expected-state checks. Failure to
prove the rollback outcome leaves the workstream incomplete and requires a
read-only settings check before any retry.

## 11. Verification Strategy

This is L2. It changes durable-consumer scheduling and performs an authority
cutover, but changes no schema or production data and therefore must not be
upgraded to L3 unless implementation unexpectedly requires a schema or data
mutation. If that happens, stop rather than widening scope.

Every production-code change starts with a focused failing test. Development
uses focused tests. The final production candidate receives one full suite. A
later production-code change creates a new candidate and requires affected
focused tests plus a new single final full suite.

Required coverage includes:

- future-key exclusion while exclusive is waiting and while it is held;
- multiple exclusive callers, writer preference, cancellation, exception, and
  cleanup;
- two-chat concurrency, same-chat ordering, retry/live-lease blocking;
- cap-three peak and work-conserving refill around a slow lane;
- loop cancellation, process restart model, and stale-lease recovery without
  later-message overtaking or duplicate claim;
- default, round-trip, invalid, dynamic-raise, and dynamic-lower config cases;
- atomic old/new tuple visibility and expected-state conflicts;
- global and global-plus-cap-one rollback;
- Web/ingest/worker authority and proxy boundaries;
- pure in-memory observability with no database or exchange reads.

## 12. Production Acceptance

The implementation plan must require:

1. exact clean/tracking/remote/claim/SHA gates;
2. one final local full suite for the final candidate;
3. explicit-path staging and `git diff --cached --name-only` verification;
4. reviewed fast-forward push to `codex/deepcoin-auto-trading-v1`;
5. exact-SHA gated updater deployment;
6. post-deploy verification while still global and at compatibility cap 20;
7. a no-op global transition and rollback-path proof;
8. zero active writes, message inflight, management activity, and worker-command
   claimed/executing state before cutover;
9. WAL and `PRAGMA quick_check=ok`;
10. split worker/Web/ingest topology, monolith inactive, and ingest as the only
    Telegram session holder;
11. one atomic expected-state transition to `per_chat + 3`;
12. a continuous two-hour natural-traffic window with at least five messages
    and an attempt to cover at least two chats;
13. same-chat order, cross-chat overlap, peak at most three, backlog convergence,
    no duplicate job/decision/execution, no SQLite lock, no event-loop stall,
    no session conflict, no DeepSeek/402, and stable authority topology;
14. complete read-only exchange snapshots from the worker credential boundary,
    with one reasoned retry only for an incomplete query;
15. immediate rollback and incomplete status on any failed gate.

If the two-hour window ends below five natural messages, do not extend it,
manufacture traffic, or grant a waiver. Roll back to global, record the exact
traffic result and evidence path, and leave the workstream incomplete.
