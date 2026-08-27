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

## Low-Perturbation Addendum

The first production use of the observer showed that coupling every durable-
queue sample to six role HTTP requests and their access-log writes can perturb
the event loops being measured. This addendum supersedes only the sampling and
cross-chat clauses above; the read-only boundary and every fail-closed safety
contract remain unchanged.

### Split cadence

The acceptance mode uses two independent monotonic schedules:

- a durable SQLite schedule, normally once per second, reads settings and job
  state through the existing `mode=ro` / `query_only` connection;
- a runtime HTTP schedule, normally once per 30 seconds and always at window
  start and end, reads settings and the ingest, worker, and Web health endpoints.

SQLite samples never trigger an HTTP request. Runtime samples cache the last
complete authority, role, cap, lane, and cumulative health state for diagnostic
output, but cached state cannot turn an incomplete due runtime read into a
healthy sample. Each due database or runtime read gets at most one bounded
retry; the second incomplete result fails closed immediately.

The convergence and rollback modes retain their bounded sub-second combined
sampling because each normally lasts about one second and directly proves an
expected-state transition. Their short request burst is outside the continuous
acceptance window and is not evidence of natural runtime health.

### Cross-chat attribution

The durable queue remains the concurrency authority. A complete SQLite snapshot
with `claimed` jobs from at least two distinct chats proves simultaneous durable
cross-chat ownership without an accompanying high-frequency HTTP read. The
worker's cumulative peak must independently reach at least the same lower bound
in a complete runtime sample from the same acceptance window. Neither signal is
sufficient by itself, and the worker peak must remain at most three.

Same-chat overlap and oldest-nonterminal ordering continue to be evaluated only
from one consistent SQLite snapshot. Cached runtime data is never used to infer
job order, and a pending successor is still valid backlog.

### Stall and acceptance contract

Role stall counters and attributed events are cumulative. A new event therefore
remains observable at the next low-frequency runtime sample even if its callback
has already recovered. Acceptance fails closed for any new ingest or worker
stall, any Web `captured_business_blocker`, authority or tuple drift, invalid
role evidence, or a second incomplete query.

A Web-only `loop_lag_confirmed_but_stack_unattributed` or
`idle_or_post_recovery_selector_capture` is recorded as Web evidence and cannot
be relabeled as a per-chat scheduler or worker defect. It may still fail the Web
health contract when the configured Phase 7 policy says so; the reason code and
rollback class must identify Web, never a fabricated business blocker.

The two-hour clock, five-natural-message minimum, two-chat attempt, ordering,
cross-chat, queue-drain, duplicate, authority, SQLite, session, and exchange
parity gates remain mandatory. The lower HTTP rate is a measurement correction,
not an acceptance waiver.

### Rejected cadence alternatives

Simply increasing the old single poll interval was rejected because it would
still couple database and HTTP work and could miss short simultaneous claims.
Ignoring selector-only captures was rejected because ingest and worker stalls
must remain fail closed even when the stack cannot be attributed to a business
function.

### Addendum verification

RED-to-GREEN tests must prove the database and runtime call budget, cross-chat
proof across the two independent signals, persistence of cumulative stall
deltas, and immediate failure after the one allowed retry. The approved final
candidate also receives the Phase 7 focused slice and one complete local suite
before the standalone observer is copied into a server evidence directory.
The observer does not enter the production checkout and no runtime deployment or
restart is required.

## R6 Web-Parity Isolation Addendum

R5 proved that cadence separation alone is insufficient when the external guard
still invokes the synchronous Web
`/api/runtime/message-pipeline-parity` projection. At
`2026-08-27T03:21:34Z` that request completed in the same log instant as a
`7369.438ms` Web loop lag and a post-recovery selector capture. The durable
worker path had already processed seven natural messages from three chats with
peak two, while queue, SQLite, session, management, execution, and exchange
guards remained clean. The failure is therefore attributed to the acceptance
measurement path, not to a proven per-chat scheduler or worker defect.

R6 removes every continuous call to the Web parity endpoint. The checked-in
observer's existing read-only SQLite snapshot remains authoritative for raw/job
identity, missing/orphan jobs, duplicates, ordering, claimed-chat concurrency,
and queue counts. It additionally computes stuck pending jobs directly in the
same `mode=ro`, `query_only=ON` transaction from `enqueued_at` and the fixed
300-second Phase 7 threshold. The external guard retains only read-only checks
that are not already present in the observer: active exchange writes,
management/revision/worker-command state, duplicate decisions, service/session
authority, and journal anomalies. None of those checks may call a production
Web diagnostic endpoint.

Role HTTP remains limited to the lightweight settings and loop-health reads at
window start, end, and every 30 seconds. A new role-attributed stall still fails
closed exactly as before. R6 changes only where parity evidence is collected;
it does not waive Web health, traffic, ordering, cross-chat, two-hour, exchange,
or rollback requirements.

The operational controller candidate must pass a source-boundary check proving
that `message-pipeline-parity` is absent before installation. RED-to-GREEN tests
must prove pending age is derived from SQLite, a 300-second pending row fails as
stuck, younger and terminal rows do not, and an external zero guard cannot mask
the database result.

## Authorization Boundary

The original design authorization covered only local artifacts. The owner has
now additionally authorized non-force push of the reviewed observer candidate,
read-only installation outside the production checkout, a fresh Phase 7
`global + 1` to `per_chat + 3` safe retry, and the already-tested atomic
rollback. Production runtime deployment, restart, schema or business-data
changes, replay, worker commands, manufactured Telegram traffic, test trades,
and exchange writes remain prohibited.
