# Per-Chat Activation and Event-Loop Database Optimization Design

**Status:** Approved by the owner on 2026-08-25.

## Goal

Enable production `message_lock_mode=per_chat` with a bounded three-chat
durable-worker limit, while removing the currently proven synchronous database
work from asyncio event loops. Preserve recognition, strategy targeting,
position ownership, execution, and exchange-write semantics.

The implementation must remain small enough to support a later whole-project
slimming pass. It must not add a service, queue, actor/mailbox layer, database
schema, executor framework, or second status system.

## Verified Starting Point

- Production uses split `ingest`, `worker`, and `web` processes. The monolith is
  disabled.
- Production settings are currently `global + queue + queue` with compatibility
  parallel-chat cap `20`.
- The server has two CPUs. Python's default executor currently resolves to six
  workers. SQLAlchemy uses its file-SQLite `QueuePool` defaults of size five,
  overflow ten, and timeout thirty seconds per engine.
- The historical Web QueuePool exhaustion was caused by a Session-scope leak,
  not by the lane cap. The leak has already been repaired.
- The durable queue already makes the oldest non-terminal job the owner of a
  chat lane and permits distinct chats to progress independently.
- The current production observation has seen a worker lifecycle-monitor stall
  and an ingest reconcile stall with synchronous SQLAlchemy work on their event
  loops. Three event-driven Bot database calls also remain on the loop.
- The existing per-chat workstream is locally complete but not cut over. Its
  approved production target is `per_chat + 3`, with `global + 3` and
  `global + 1` rollback levels.
- A fresh read-only production check found management batch `150` already
  `resolved/historical_position_fully_closed`, with active management count
  zero. Trigger-protection intents `138`, `141`, and `147` are already
  `resolved/terminal` with reason
  `entry_leg_terminal_after_snapshot_wait`. `PRAGMA quick_check` returned
  `ok`. The older status header that still describes these rows as pending is
  stale and must not be used to justify another write.

All production, Git, database, service, and exchange facts are time-sensitive
and must be freshly verified by the phase that uses them.

## Options Considered

### One long implementation session

This minimizes handoff work but mixes local code, L3 data repair, deployment,
cutover, and observation in one growing context. It is rejected.

### One sequential session per risk boundary

Seven sessions share one exclusive worktree and one canonical status entry.
Each session reads only `AGENTS.md`, the status file, and its current phase
file. Exact commits, evidence paths, and the next phase pointer replace chat
history. This is the selected design.

### Per-file or parallel implementation

This appears faster but splits coupled SQLite, event-loop, lane-ordering, and
trading-authority boundaries across concurrent owners. It is rejected.

## Architecture Boundaries

Keep the existing three-process topology and task ownership. Do not add another
process, runtime role, durable queue, thread-pool manager, or database engine.

Do not explicitly enlarge the default executor or SQLAlchemy pool. The lane cap
is an asynchronous admission bound, not a required count of simultaneously
checked-out database connections. Increasing pool capacity does not increase
SQLite's single-writer throughput and may increase contention.

At cutover, set the durable-worker cap to three. Same-chat order continues to
come from `message_processing_jobs`, not from process-local locks. Ingest
per-chat locks protect per-message work inside one process. Global operations
continue to use exclusive `lock_all()` admission. The process-local registry
must never be represented as a cross-process authority.

## Event-Loop Database Offload

### Ingest reconcile

Keep Telethon fetches and asynchronous notification calls on the event loop.
Extract only the synchronous database slices in the production-active reconcile
path into small synchronous helpers and invoke them with `asyncio.to_thread()`.
The intended slices include:

- checkpoint repair and settings read;
- history-checkpoint projection;
- per-dialog media-row projection;
- normalized-message persistence;
- the existing non-queue compatibility database projections when they are
  exercised by tests.

Do not move the whole reconcile coroutine into a thread. Preserve the existing
operation ordering, global/per-chat lock scopes, retry behavior, and queue vs.
inline authority selection.

### Worker lifecycle expiry review

Extract the database portion of `_request_pending_expiry_reviews()` as one
synchronous unit: select candidates, evaluate pending-leg context, conditionally
claim rows, commit changes, and return bounded notification payloads.

Run that unit through the existing single-thread `mgmt-worker` executor. This
preserves the pre-existing non-overlap with strategy management and break-even
convergence. After it returns, send notifications asynchronously on the event
loop. Do not add a lifecycle-specific executor.

### Telegram Bot commands

Run read-only holding and pending-position formatting through
`asyncio.to_thread()`.

Run system-operator commands that may mutate the database or call Deepcoin on
the existing `mgmt-worker`. Reuse the callback path's queued-vs-started
cancellation boundary: queued cancellation prevents execution; started work is
drained before cancellation is propagated. Do not place these commands on the
shared default executor.

### Regression proof

Use focused dynamic heartbeat tests. When an injected database helper blocks,
the event loop must continue advancing. Remove the three corrected Bot calls
from the existing blocking-call allowlist.

Do not create a general-purpose AST framework. Add only the narrow static or
dynamic assertions needed to prevent these exact paths from moving back onto
the loop.

## Bounded Durable Claim Selection

Retain the existing `BEGIN IMMEDIATE`, conditional updates, claim tokens,
oldest-non-terminal ownership, retry times, live-lease blocking, stale-lease
recovery, and single-worker authority.

Replace the unbounded ORM `.all()` with one bounded SQL selection:

1. A CTE identifies the oldest non-terminal queue job for each chat.
2. SQL filters that oldest row to a due pending job or an expired claimed job.
3. SQL orders by chat and raw-message identity and returns no more than the
   current available-lane limit.
4. The existing conditional update claims only those returned rows.

An oldest job that is not due or has a live lease continues to block later work
in its chat. Other chats remain eligible. Python must receive at most `limit`
rows.

Do not add a database index or schema migration. If later production evidence
shows the bounded query itself is slow, index design is a separate evidence-led
change.

Required tests cover a large same-chat backlog, no overtaking, live and stale
claims, other-chat progress, two-worker competition, limit enforcement, and
restart recovery.

## Read-Only Production-State Prerequisites

### Management batch 150

Freshly verify that batch `150` remains terminal with reason
`historical_position_fully_closed`, its related management/component/leg/binding
state is internally consistent, active management count remains zero, and
`PRAGMA quick_check` is `ok`. This phase is read-only: no backup, CAS plan,
database apply, rollback rehearsal, exchange write, deployment, restart,
replay, or Telegram message.

If any required field has regressed or the query is incomplete, stop. Record
the discrepancy and design a separate L3 repair only after new owner approval;
do not repair it inside this workstream.

### Trigger-protection intents

In a separate session, freshly verify that intents `138`, `141`, and `147`
remain `resolved/terminal` with reason
`entry_leg_terminal_after_snapshot_wait`, retain their expected execution-leg
identity, and that `PRAGMA quick_check` is `ok`. Use only local database reads;
Deepcoin history is not needed to prove that the already-terminal rows have not
regressed.

This phase performs no database apply, rollback rehearsal, exchange call,
deployment, restart, replay, or Telegram message. Any regression or incomplete
query stops the workstream and requires a separately designed and authorized L3
repair.

## Atomic Cutover

Before cutover require an exact clean and authorized deployed SHA, exactly one
worker, ingest-only Telegram session ownership, no active write, no active
management, no claimed/inflight message job, no claimed/executing worker
command, WAL, `quick_check=ok`, clean loop/SQLite/session evidence, and a
complete worker-owned read-only exchange baseline.

Submit one ingest-owned expected-state transition:

```text
expected: global + 20 + queue
desired:  per_chat + 3 + queue
```

Do not split the fields into separate writes and do not restart. A conflict
must leave the old tuple unchanged. A timeout is unknown: read both fields and
retry once only if the read proves the write did not apply.

## Rollback

- Lock, admission, or ingest anomaly: atomically return to `global + 3`.
- Scheduler, duplicate, SQLite, execution, or concurrency anomaly: atomically
  return to `global + 1`.

Do not reset historical jobs or restart services. An unknown rollback response
requires an exact settings read before one reasoned retry.

## Production Acceptance

Cutover and observation stay in the same session. Observe one continuous
two-hour window without manufactured traffic, stitched windows, deployment,
restart, settings changes, worker-command invocation, or observer-triggered
exchange writes.

Require:

- at least five natural messages and an attempt to cover at least two chats;
- `peak_active_chat_lanes` at least two and no more than three;
- same-chat non-overlap and exact order;
- actual cross-chat progress and bounded backlog convergence;
- zero missing, orphan, stuck, or duplicate job/decision/execution identities;
- zero new SQLite lock, event-loop stall, session conflict, DeepSeek/402, or
  authority-drift evidence;
- complete worker-owned exchange baseline/end evidence with explained parity.

Insufficient traffic, no real overlap, an incomplete required query, or any
technical failure triggers rollback and leaves the workstream incomplete. No
automatic waiver is allowed.

## Seven Sequential Sessions

1. Event-loop database offload: local RED-to-GREEN code only.
2. Bounded claim selection: local RED-to-GREEN code only.
3. Candidate integration and review: focused verification, one final full
   suite, independent review, and frozen exact SHA.
4. Batch 150 read-only gate: verify the already-terminal production state or
   stop without repair.
5. Trigger-protection read-only gate: verify the three already-terminal intents
   or stop without repair.
6. Compatible deployment: reviewed exact-SHA push/deploy while retaining
   `global + 20`; prove no-op transition and conflict behavior; no cutover.
7. Cutover and acceptance: `per_chat + 3`, continuous observation, success or
   rollback before returning control.

The sessions are sequential. They use one dedicated exclusive worktree and no
subagents, background implementers, or parallel owners. Session 7 may not hand
off an enabled but unobserved cutover.

## Handoff and Documentation

Reuse `docs/per-chat-durable-lanes-status.md` as the only status entry. Do not
create another status system. At the start of execution, its compact header will
point to the current phase, phase file, claim owner, exact base/candidate/
deployed SHAs, and last completed phase.

Each session reads only:

1. `AGENTS.md`;
2. `docs/per-chat-durable-lanes-status.md`;
3. the single current phase file.

Each session claims before editing and ends with a concise ledger entry:
changed paths, exact SHA, RED/GREEN or final verification, whether a production
mutation occurred, evidence path/hash, next phase, and remaining authorization.
Long logs and raw exchange/database evidence stay outside the status document.

Create one design document, one short phase index, and seven self-contained
phase files. A later slimming phase may replace completed phase files with one
completion summary and remove superseded plans. Runtime code gains no framework
or persistent artifact solely for handoff.

## Authorization Boundary

Approval of this design authorizes only design and implementation-plan
documentation. It does not authorize production code changes, push, deployment,
restart, database mutation, Telegram traffic, replay, settings mutation,
`per_chat` cutover, or exchange writes.
