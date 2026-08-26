# Phase 7 Acceptance Observer Design

## Goal

Replace the unversioned Phase 7 acceptance script with a reusable, tested,
strictly read-only observer that distinguishes a queued same-chat successor from
an actual concurrent claim and confirms rollback convergence independently from
the acceptance invariant that triggered rollback.

## Chosen Approach

Add a standalone standard-library tool at
`scripts/per_chat_phase7_observer.py` and focused tests at
`tests/test_per_chat_phase7_observer.py`. The tool is not imported by any
production service. A future authorized retry can stream the checked-in script
to `python3 -` over SSH, so using the observer does not require a deployment or
write a tool into the production checkout.

The observer is read-only by construction:

- SQLite opens through a `mode=ro` URI and enables `PRAGMA query_only=ON`;
- runtime and settings observations use HTTP GET only;
- no expected-state POST, worker command, replay, Telegram send, exchange write,
  service control, or Git operation exists in the tool;
- structured JSON Lines are written to stdout; an outer, separately authorized
  session owns evidence-file persistence and any approved rollback.

## Alternatives Rejected

### Observer-owned rollback

Embedding the expected-state POST would react quickly but would give a
diagnostic tool production write authority and couple monitoring correctness to
mutation. This conflicts with the least-authority boundary and makes local tests
less representative of the production risk.

### Runbook-only SQL or prompt changes

Documented queries are smaller, but the previous failure came from an
unversioned query assembled during the retry. Without executable tests, a future
session could reintroduce the same pending-versus-active confusion.

## Observation Model

Each complete observation is an immutable snapshot containing:

- monotonic sample time and UTC timestamp;
- database and API settings tuples;
- worker PID, runtime role, configured cap, active lanes, peak lanes, and
  `limit_applied_at`;
- the fixed expected ingest, worker, and Web authority PIDs;
- every new non-shadow message-processing job after the supplied baseline,
  limited to identity, chat, status, and ordering fields;
- queue, duplicate, missing, and terminal counts needed by Phase 7.

`completed_at` may be retained as diagnostic output but is never used as an
active-processing boundary. The worker passes the tick-start timestamp into job
settlement, so the column does not prove the actual end of the asynchronous
processing body.

An incomplete database or HTTP read produces an incomplete sample. The CLI may
retry that sample once within its original sample deadline. A second incomplete
read is emitted as a fail-closed observer result; it is never converted to zero
or healthy.

## Same-Chat Contract

The durable queue is the cross-process ordering authority. For each complete
snapshot, the observer groups new non-shadow non-terminal jobs by `chat_id` and
orders them by `raw_message_id`.

A same-chat violation exists only when either condition is true in one
consistent database snapshot:

1. more than one job for the chat is `claimed`; or
2. a claimed job is not the oldest non-terminal job for that chat.

A later job that is only `pending` while the oldest job is `claimed` is valid
backlog, not overlap. Cross-sample guesses are forbidden: a previously observed
claim may have completed between samples. Exact order comes from the current
durable non-terminal rows, not from comparing enqueue time with `completed_at`.

Cross-chat progress is established when one complete snapshot contains claimed
jobs from at least two distinct chats and worker active lanes agrees with that
lower bound. Aggregate worker peak remains a separate cap guard and must never
exceed three.

## Phase State Checkers

The tool exposes three pure state checkers and corresponding CLI modes:

### Cutover convergence

Checks the fixed five-second contract without issuing the cutover: database and
API remain `per_chat + 3 + queue`, worker cap is 3, `limit_applied_at` is newer
than the supplied pre-cutover value, lane bounds are valid, and authority PIDs
and roles are unchanged. Success requires three consecutive complete samples.

### Acceptance

Tracks the continuous natural-traffic window and immediately emits a typed
failure for tuple, authority, lane-cap, same-chat, ordering, duplicate, queue,
SQLite, loop, session, or exchange-read anomalies. It records but does not waive
the minimum traffic, two-chat, cross-chat-progress, and peak requirements.

### Rollback convergence

Checks only rollback state: the complete database/API tuple equals the supplied
rollback target, worker memory has applied the target cap, and PID, role, and
authority remain unchanged. It does not call the acceptance checker and cannot
be blocked by the already-recorded acceptance failure. Success requires the
configured number of consecutive complete samples.

## Output and Exit Contract

Every line is a JSON object with a stable `kind` and UTC timestamp. Important
kinds are `sample`, `convergence_passed`, `acceptance_failed`,
`acceptance_summary`, `rollback_converged`, and `observer_incomplete`.

Failures include a stable reason code and the approved rollback class:

- lock, admission, or ingest failure -> `global + 3 + queue`;
- scheduler, ordering, duplicate, SQLite, execution, or concurrency failure ->
  `global + 1 + queue`.

Exit status is zero only for the requested mode's completed success condition.
Nonzero status distinguishes an acceptance failure from an incomplete observer
query and from invalid CLI input. The observer never performs the suggested
rollback itself.

## Testing Strategy

Focused tests construct real immutable snapshots and exercise the pure state
checkers without mocks where possible. RED-to-GREEN coverage must prove:

- a later same-chat pending job while the oldest job is claimed is accepted;
- a later job claimed only after the predecessor is terminal is accepted;
- two simultaneous same-chat claims fail;
- a claimed later job behind an older pending job fails ordering;
- simultaneous claims from different chats establish cross-chat progress;
- misleading `completed_at` values cannot create an overlap failure;
- cutover convergence requires three consecutive complete samples and resets on
  a non-matching complete sample without extending the deadline;
- rollback convergence succeeds from the rollback tuple, cap, and authority
  even after a recorded acceptance failure;
- one retry is allowed for an incomplete read and a second incomplete read fails
  closed;
- the source contains no HTTP mutation method or service-control path.

After focused tests pass, run the existing message-processing worker ordering
tests that cover durable same-chat ownership. Because the new tool is dormant
and does not alter production imports, no deployment, restart, production
observation, or full production suite is required for this local remediation.

## Authorization Boundary

This design authorizes only local design and implementation documents, observer
code, focused tests, canonical status updates, and explicit-path local commits.
Push, deployment, restart, production access or mutation, cutover, rollback,
schema/data changes, replay, worker commands, manufactured Telegram traffic,
test trades, and exchange writes remain separately prohibited.
