# Management Drift And Multi-Leg Truth Design

## Problem

The 大漂亮 message `#1451` said to take profit on half of the position and
protect the remainder. The legacy recognition path changed lifecycle `466`
locally, but it neither created a management batch nor submitted a Deepcoin
partial close or stop update. Message `#1454` later failed safe application and
did not create a candidate. The live Deepcoin position therefore remained at
its full size with the original stop.

The current management-batch workflow already prevents new lifecycle mutation
before exchange confirmation. The remaining system defects are:

- legacy lifecycle state can disagree with the live position without a clear,
  durable operator-facing diagnosis;
- one binding can contain multiple comma-separated position IDs even though
  verified `execution_order_legs` are the persisted position authority;
- Deepcoin TPSL rows identify their target with `closePosId`, but the protection
  matcher does not treat that field as exact identity; and
- strategy list/detail rendering can consequently report a verified multi-leg
  strategy as missing or conflicting while also borrowing the wrong protection
  evidence.

## Safety Boundary

This change is diagnostic and reconciliation hardening. It must not replay old
Telegram messages, close a position, cancel an entry, change a TPSL order, or
repair production rows automatically.

Existing live positions remain untouched. A historical instruction may be
executed only through a separately reviewed current-state operator decision.

## Canonical Data Flow

The canonical chain remains:

`raw message -> authoritative recognition -> lifecycle -> binding -> verified entry legs -> exact exchange position/protection`

For Deepcoin live ownership, active verified entry legs are authoritative. A
comma-separated `execution_bindings.pos_id` is a compatibility projection only
and must not be compared as one exchange position ID.

For protection, an exchange TPSL row with `closePosId` is exact evidence for
that position. Instrument, side, time, and size matching remains a fail-closed
fallback only when no exact position identity exists.

## Components

### Protection attribution

Extend `match_position_protection` to recognize `closePosId` and normalized
variants as exact position identity. Exact rows must never be assigned to a
different same-symbol position by heuristic matching.

Tests will cover two same-side ETH positions with distinct `closePosId` values
and prove that one position cannot borrow the other's stop or take profit.

### Strategy-record multi-leg projection

Load the set of active verified entry-leg position IDs for each binding. Enrich
strategy records against every exact leg ID, not the aggregate binding string.
A multi-leg strategy is confirmed only when every expected live leg has one
matching exchange position and the ownership annotations agree.

The detail page will render each exact position separately. It will not claim
`not_found` merely because the compatibility binding contains multiple IDs.

### Management execution drift

Compare the lifecycle's expected stop/profit state with the exact protection
evidence on each verified live leg. When the lifecycle references a management
message but no confirmed management batch explains the difference, expose a
critical `management_execution_drift` result containing only safe identifiers,
expected values, actual values, and confirmation state.

The condition is observational. It does not mutate lifecycle, binding, batch,
leg, or exchange state. Exchange evidence that is unavailable or ambiguous is
reported as unknown rather than mismatch.

### Existing execution ordering

Add regression coverage proving the current management-batch contract:

1. plan against exact verified legs;
2. submit immutable management legs;
3. reconcile exchange confirmation; and
4. update lifecycle only after confirmation.

Submission failure, unknown response, or reconciliation ambiguity must leave
the lifecycle unchanged and route the batch to its existing blocked/recovery
state.

## Web And Alert Behavior

The strategy list and detail page will distinguish:

- verified multi-leg live position;
- exchange evidence unavailable;
- exact protection absent;
- exact protection mismatch; and
- management execution drift.

The recurring lifecycle monitor may continue to skip simulated exits for live
bindings, but the Web record must make the underlying drift visible instead of
repeating only an informational log line.

## Verification

Verification is staged:

1. focused unit tests fail before implementation;
2. focused protection, strategy-record, Web, and management tests pass;
3. the complete local test suite passes in the isolated worktree;
4. reviewed commits are pushed to `codex/deepcoin-auto-trading-v1`;
5. production is updated through the documented GitHub/server helper;
6. server tests, service state, database quick check, live positions, exact
   TPSL attribution, and the 大漂亮 lifecycle `466` record are checked read-only.

Production verification must prove that no exchange order, cancellation, or
database repair was produced by this change.
