# Deepcoin Maintenance Read Pacing Design

## Problem

The manual-cleanup production preflight issued six exact
`/deepcoin/trade/fills` reads in about 0.7 seconds. Deepcoin documents this
private endpoint at 5 requests per second and 150 requests per minute, counted
by UID. The sixth exact fills read returned HTTP 401 while the preceding reads
using the same credentials and clock succeeded. The maintenance reconciliation
therefore violates the documented endpoint rate contract even though it remains
read-only and correctly fails closed after the response.

## Scope

Add pacing only to the exact fills and exact trigger-history reads performed by
`manual_pending_entry_reconciliation`. Preserve the canonical
`REVIEWED_PENDING_ENTRY_TARGETS`, all existing exchange and local evidence
requirements, the plan fingerprint, the one-transaction database boundary and
the stopped-legacy activation contract.

Do not add a global Deepcoin client limiter, retry, backoff, second target list,
persistent rate state, confirmation protocol, exchange write, replay or entry
thaw.

## Design

The reconciliation module will own a small invocation-local pacer. It uses a
monotonic clock and sleep function, both injectable for deterministic tests and
defaulting to `time.monotonic` and `time.sleep` in production.

The pacer keeps separate last-start timestamps for exact fills and exact trigger
history. Before every exact read, including the first read for each endpoint, it
waits until at least 0.41 seconds have elapsed from the endpoint's preceding
start. The initial wait also leaves a conservative boundary when separately
invoked CLI processes run consecutively. A 0.41-second interval is slightly
slower than the documented 150-per-minute average and also remains below five
requests per second.

`build_manual_pending_entry_reconciliation_plan` creates one pacer and passes it
through both exact-read evidence collectors. `apply_manual_pending_entry_reconciliation`
passes the same injectable clock and sleeper into its mandatory fresh re-plan.
No timing value is included in the evidence or plan fingerprint because pacing
changes request scheduling, not the accepted evidence.

## Failure Semantics

Pacing is not retry logic. Any HTTP status, transport failure, malformed result
or other incomplete exact query immediately returns the existing fail-closed
reason. The failed target is called exactly once, later targets are not queried,
and trigger history is not queried after an incomplete fills result. Unknown
remains permanently ineligible for automatic retry.

The invocation-local pacer does not claim to coordinate other processes using
the same Deepcoin UID. A future production attempt must still treat any
incomplete result as unknown and stop. During the database write boundary the
full runtime remains stopped and persistently masked as already required.

## Tests

1. A fake monotonic clock proves seven exact fills starts are separated by at
   least 0.41 seconds and no real sleeping occurs.
2. A sixth-call HTTP-style exception proves exactly six fills calls, no retry,
   no seventh fills call and no trigger-history call.
3. Exact fills and exact trigger history use independent pacing keys and both
   cover all canonical targets when evidence is healthy.
4. The apply path forwards the pacing dependencies into its fresh re-plan.
5. Existing reconciliation, Deepcoin client, CLI and maintenance-evidence tests
   remain green, followed by one final repository suite because production code
   changes.

## Operational Boundary

This local repair stage performs no push, SSH, stage, service control,
production database or settings mutation, Deepcoin request or write, activation,
observation or entry thaw. A later production cutover requires a newly reviewed
exact commit, new immutable stage and entirely fresh evidence.
