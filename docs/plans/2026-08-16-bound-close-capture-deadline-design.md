# Bound Close Capture Deadline Design

## Goal

Allow a bounded close-reservation exchange capture to finish its legitimate,
rate-limited read workload for up to 64 target reservations without weakening
the deployment gate or treating elapsed time as exchange truth.

The production diagnostic on 2026-08-16 classified all 29 target reservations
as `UNKNOWN / exchange_capture_timeout`. The capture had a fixed 30-second
absolute deadline, while one capture performs two global reads, two exact reads
per distinct close order, and one exact history read per distinct position.
Deepcoin position-history reads are also paced at a minimum interval of 1.05
seconds. The current deadline can therefore expire during valid bounded work
before exchange evidence is complete.

## Non-goals

This change does not:

- change deployment-gate severity or blocking facts;
- infer terminal state from record age, callbacks, or elapsed time;
- change the maximum target population of 64;
- change the 1 MiB response-size bound;
- add an exchange writer, database write, retry, history replay, or notification;
- modify Batch 119 or enable MiMo v2;
- deploy code or authorize a production capture/apply window.

## Selected Approach

Use a fixed 180-second absolute deadline for every fresh bound-close exchange
capture. A capture returns immediately when all reads and normalization finish;
180 seconds is only a hard upper bound.

This is preferred over a target-count formula because reservations, orders, and
positions have different deduplication ratios, so a count-based formula can
underestimate the real request graph. It is preferred over chunking because
chunks would observe different exchange moments and weaken the meaning of one
coherent capture.

The outer stopped-service window remains bounded by its existing 12-minute
absolute deadline.

## Deadline Architecture

The CLI creates a fresh deadline immediately before each exchange capture:

```text
deadline = monotonic_now + 180 seconds
```

The same absolute deadline covers the entire capture scope:

1. global current positions;
2. global pending orders;
3. exact order history for every distinct order;
4. exact fills for every distinct order;
5. exact position history for every distinct position;
6. streaming response reads;
7. JSON decoding and collection normalization.

Each dry-run capture receives a new reader and a new 180-second deadline. The
second capture remains unreachable unless the first capture is completely
`ready`. A post-apply recapture used only to resolve an ambiguous commit outcome
also receives a new reader and its own 180-second deadline.

The dedicated reader retains its existing per-request absolute-deadline scope,
read-only transport, `trust_env=False`, 1 MiB response bound, single-use
capability, and POSIX wall-clock interruption behavior.

## Fail-closed Behavior

Reaching the deadline interrupts the active read/parse operation, closes the
dedicated reader, and yields `UNKNOWN / exchange_capture_timeout`. Timeout never
becomes `ACTIVE` or `PROVEN_TERMINAL`, never creates an actionable plan, and
never grants apply authority.

Existing closed reasons remain authoritative for non-timeout failures,
including schema, identity, state, response-size, and evidence-availability
failures.

No automatic retry is added. A refused capture restores services and consumes
the production-window authorization. A new reviewed SHA and fresh exact tokens
are required before another stopped-service attempt.

Signals, configuration drift, database path/inode drift, unit inventory drift,
unknown processes, response overflow, and timer/handler conflicts remain
fail-closed. Service restoration order and original-state verification do not
change.

## Safe Output

The final operator output remains the existing redacted diagnostic projection:
status, action count, classification counts, closed reason counts, and the three
zero-write counters. It contains no reservation, position, order, message,
fingerprint, timestamp, provider row, raw error, path, token, or credential.

## Verification

TDD must first prove that the current 30-second deadline violates the new
contract. The implementation then makes the narrow constant change and proves:

- dry-run and post-apply recapture both use 180 seconds;
- independent captures obtain independent deadlines;
- successful work returns immediately rather than sleeping to the ceiling;
- blocking streams, slow-drip streams, JSON decoding, and collection parsing
  are interrupted at the absolute deadline;
- timeout remains `UNKNOWN / exchange_capture_timeout`;
- capture two, comparison, and apply remain unreachable after timeout;
- reader closure and POSIX signal/timer restoration remain correct;
- the 64-target, 1 MiB, GET-only, one-shot boundaries are unchanged;
- ordinary Deepcoin, Batch 119, and production-monitor timeout semantics are
  unchanged;
- deployment-preflight production code and gate severity have no diff;
- the runbook states the 180-second per-capture ceiling and unchanged 12-minute
  stopped-service ceiling.

Required final checks are the focused recovery/CLI/Deepcoin/runbook/gate suites,
independent review with zero Critical and zero Important findings, the complete
local test suite, compile checks, diff hygiene, and a clean worktree. Work stops
before push and before any new production window.
