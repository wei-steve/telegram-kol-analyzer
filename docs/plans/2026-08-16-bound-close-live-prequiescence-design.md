# Bound Close Live Pre-Quiescence Design

## Goal

Remove the impossible timing conflict in the bound-close reservation read-only
recovery window without weakening any deployment gate or increasing production
downtime. The existing flow waits up to ten minutes after stopping services for
ordinary writer markers to age beyond the deployment-preflight active window,
but it must also reserve seven minutes for two bounded exchange captures inside
a twelve-minute stopped-service deadline.

## Constraints

- The live pre-wait is read-only and lasts at most twelve minutes.
- The stopped-service phase retains its independent twelve-minute absolute
  wall-clock deadline.
- The ten-minute writer cutoff and the 180-second per-capture hard limit remain
  unchanged.
- No database or exchange writes, history replay, notification, apply,
  deployment, gate reduction, or MiMo v2 activation is added.
- Every reviewed SHA consumes fresh exact stop-window approval tokens.

## Chosen Approach

Poll the existing closed writer-quiescence helper while production services
remain active. Poll every fifteen seconds and return immediately when the
aggregate projection is exactly `ready`. This live result is only admission to
attempt a stopped-service window; it is not exchange evidence and grants no
capture or apply authority.

After the live poll succeeds, preserve the existing closed inventory and
identity checks, build the reviewed capture runner, and stop all audited units.
Once all units are proven inactive, run the writer-quiescence helper exactly
once. Only an immediate, strictly validated `ready` result may reach the first
exchange capture. A refused result after stopping indicates a writer race and
causes immediate restoration; the stopped phase never waits for markers to age.

The two exchange captures continue to use distinct fresh readers, independent
180-second absolute deadlines, the existing 420-second admission reserve, and
the stopped-phase process-group hard deadline.

## Data Flow

```text
normal production services
  -> live read-only writer poll (<= 12 minutes)
  -> exact ready projection
  -> recheck SHA / database identity / unit inventory
  -> build private reviewed runner
  -> stop audited units
  -> exact stopped-state and identity checks
  -> one writer helper invocation
       refused/error/drift -> restore, no exchange read
       ready               -> two fresh read-only captures
  -> stable comparison or closed refusal
  -> restore original unit states
```

## Failure Semantics

- Live poll timeout, helper error, malformed projection, or identity drift exits
  before `QUIESCE_ATTEMPTED=1`; no unit is stopped.
- A writer appearing between live readiness and stopped verification produces a
  closed `post_stop_writer_race` refusal and no exchange read.
- The stopped helper is invoked once. It has no sleep or aging loop.
- First-capture refusal keeps the second capture unreachable.
- Any blocked capture, projector, validator, identity check, or comparator is
  killed by the stopped-phase process-group deadline and the parent EXIT trap
  restores services.
- Raw errors and authorities remain private. Operator output is limited to a
  closed aggregate status such as `precheck_timeout`,
  `post_stop_writer_race`, `capture_refused`, or `stable`.

## Verification

Tests must prove:

1. Immediate live readiness does not sleep.
2. Refused-to-ready live polling leaves every service running.
3. A twelve-minute live timeout performs zero stop calls.
4. Malformed data, helper errors, unit/SHA/database drift, and nonzero process
   scan outcomes fail closed before stopping.
5. A post-stop writer race restores services and performs zero exchange reads.
6. The stopped helper is invoked exactly once and has no polling sleep.
7. Two captures retain distinct readers, independent 180-second deadlines, and
   the stopped-phase twelve-minute hard bound.
8. All existing recovery, deployment-preflight, CLI, and full-repository tests
   remain green, and production deployment-preflight source has no diff.

