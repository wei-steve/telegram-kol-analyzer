# Bound Close Legacy Monitor Transient-State Design

## Goal

Allow the bound-close live pre-quiescence wait to coexist with the production
legacy monitor timer without weakening any other unit, identity, writer, or
deployment safety boundary.

## Production Evidence

The reviewed read-only window at
`ef6e11b8e2a45b13f9d25d9ed39d51de7d085dd6` exited before stopping any
service. During the live wait, `telegram-kol-monitor.timer` triggered the
`Type=oneshot` `telegram-kol-monitor.service`. The service completed with
its known failure and changed its systemd runtime timestamps at
`2026-08-16 19:01:50 CST`.

The live verifier required every unit runtime state to remain identical to the
initial snapshot, so the expected timer-driven oneshot transition was treated
as an unsafe race. Post-window checks proved:

- core services were never stopped or restarted;
- the production SHA remained
  `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`;
- SQLite `PRAGMA quick_check` returned `ok`;
- no exchange capture or database apply occurred; and
- the private worktree and temporary directory were removed.

## Constraints

- No deployment-gate threshold, writer cutoff, or status set is weakened.
- No database or exchange write, history replay, notification, deployment, or
  MiMo v2 activation is introduced.
- Only the installed legacy `telegram-kol-monitor.service` paired with its
  installed and active timer receives a live-wait runtime-state exception.
- Unit installation identity, timer state, core service state, socket state,
  production SHA, database path/device/inode, process-scan integrity, and
  dynamic db-stage inventory remain exact.
- The stopped phase remains strict and retains its independent twelve-minute
  process-group hard deadline.
- Every new production attempt requires a new reviewed SHA and fresh exact
  approval tokens.

## Considered Approaches

### 1. Closed legacy-monitor-only exception

During the live wait, keep exact state equality for every unit except
`telegram-kol-monitor.service`. For that one unit, require the load state to
remain `loaded`, require its paired timer to remain installed and active, and
allow only the closed transient runtime states `failed`, `activating`, and
`inactive`.

This is the chosen approach. It matches the observed systemd behavior without
legitimizing unrelated drift.

### 2. Re-snapshot every unit after live readiness

This is rejected because an unexpected core service or socket failure could be
accepted as a new restoration baseline.

### 3. Stop the legacy timer before the live wait

This is rejected because it would interrupt production monitoring for the
entire writer-aging interval and expand the stop-window authorization.

## State Contract

During live pre-quiescence:

| Unit class | Required contract |
| --- | --- |
| All units | Initial installed/absent identity remains exact |
| Timers | Initial active/inactive state remains exact |
| Core resident services | Initial active/inactive state remains exact |
| Sockets | Initial active/inactive state remains exact |
| Dynamic db-stage units | Inventory and initial state remain exact |
| `telegram-kol-monitor.service` | Loaded and in exactly `failed`, `activating`, or `inactive` |
| Any other transient or unknown state | Refused |

The exception is valid only while
`telegram-kol-monitor.timer` is still `installed:active`. A missing,
inactive, failed, newly installed, or otherwise changed timer invalidates the
exception.

## Data Flow

```text
record exact initial unit/install identities
  -> live read-only writer polling
       -> exact identity checks for every unit
       -> exact runtime checks for timers/core services/sockets/db-stage
       -> closed transient-state check for legacy monitor only
  -> exact live ready
  -> final pre-stop identity/state check
  -> create independent stopped-phase deadline
  -> stop and prove all timers inactive
  -> converge legacy monitor to inactive
       failed      -> reset-failed
       activating  -> stop
       inactive    -> no-op
       other       -> refuse
  -> stop remaining audited services and sockets
  -> one stopped writer-helper invocation
       refused/error/drift -> restore, no exchange capture
       exact ready          -> two fresh read-only captures
  -> restore audited units
```

The existing legacy-monitor reset approval continues to authorize only the
timer-frozen convergence to `inactive`. It does not authorize starting the
legacy monitor or changing any other unit.

## Failure Semantics

- Any live install-state, timer, core service, socket, db-stage, SHA, database,
  or process-scan drift exits before `QUIESCE_ATTEMPTED=1`.
- A legacy monitor state outside the exact closed set exits before stopping.
- If the paired timer is not still installed and active, the monitor exception
  is unavailable and the window exits before stopping.
- After the timer freeze begins, all unit operations and verification remain
  under the existing stopped-phase absolute deadline.
- Failure to converge the legacy monitor to exact `inactive` triggers the
  parent restoration trap.
- The stopped helper remains single-shot. Live readiness never grants exchange
  capture or apply authority.
- Raw unit errors, provider facts, database identifiers, and credentials remain
  private.

## Verification

Tests must prove:

1. `failed -> activating -> failed` for the exact legacy monitor does not
   abort live polling when its timer remains installed and active.
2. `inactive` for the exact legacy monitor is accepted during live polling.
3. `active`, `deactivating`, unknown, and malformed legacy monitor states
   are refused before any stop.
4. Timer install/runtime drift makes the exception unavailable.
5. Any core service, socket, install identity, dynamic inventory, SHA, database
   identity, or process-scan drift remains refused.
6. After timers are inactive, legacy `failed`, `activating`, and
   `inactive` converge deterministically to `inactive` with no start.
7. The stopped writer helper remains single-shot and capture stays unreachable
   before exact stopped readiness.
8. Both twelve-minute process-group hard deadlines and the two independent
   180-second capture deadlines remain intact.
9. Deployment-preflight production source has no diff and all affected,
   adjacent, and full-repository tests pass.
