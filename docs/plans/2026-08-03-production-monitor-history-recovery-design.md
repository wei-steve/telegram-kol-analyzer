# Production Monitor History Recovery Design

## Problem

The production safety monitor audits all strategy-management history and treats
every `blocked`, `partial_failed`, `recovery_required`, and `submit_unknown`
batch as actionable. Production currently contains 32 non-informational
`blocked` batches, one `partial_failed` batch, and six `recovery_required`
batches. The newest predates the stop-rescue live enablement, but the all-history
count keeps the monitor in `audit_abnormal` indefinitely.

A separate reliability issue can make the audit incomplete: the private SQLite
snapshot copier refuses a component when the live WAL changes while that
component is being read. The monitor retries `source_snapshots_differ` once but
does not retry the equivalent transient reason
`source_component_changed_during_read`.

## Goals

- Keep terminal, fail-closed history visible without treating it as a current
  incident forever.
- Keep every genuinely unresolved or ambiguous exchange outcome actionable.
- Reconcile the seven existing unresolved batches using exact exchange and
  durable identity evidence only.
- Never replay an order, infer identity from symbol/side proximity, delete
  history, or weaken position-mutation authority.
- Preserve the running trading and stop-rescue paths during rollout.

## Chosen Approach

Use classification plus evidence-based convergence.

The management audit will split `blocked` history into two categories:

1. `terminal_blocked`: the batch is completed and neither the batch nor any leg
   contains an unknown, submitted-but-unconfirmed, partial-failure, or recovery
   state. These rows remain counted and inspectable but do not make the monitor
   unhealthy.
2. `actionable_blocked`: the row is incomplete, malformed, or retains an
   actionable leg/outcome. These rows continue to alert.

`partial_failed`, `recovery_required`, and `submit_unknown` remain actionable
regardless of age. The existing narrow informational-noop exclusion remains
compatible but is no longer the only safe terminal classification.

The audit output will expose both blocked counts so monitoring behavior is
explainable and tests can prove that no unresolved state was hidden.

## Historical Recovery

Add a bounded operator workflow for paused management history. Its default mode
is read-only and produces a redacted decision for explicitly selected batch
IDs. Apply mode requires the same immutable evidence fingerprint produced by
the dry run, so evidence changes abort the write.

Each selected batch is classified from exact durable and exchange evidence:

- A planned leg with no reservation, mutation intent, execution event, or
  exchange order evidence can converge only to a terminal no-submission result.
- A submitted close can converge only when its exact order/client-order ID,
  exact `posId`, binding, and exchange history agree. Position absence alone is
  insufficient when the submission outcome is unknown.
- A failed protection replacement can converge only when the exact restored
  protection is verified or the exact position is confirmed absent.
- Missing, conflicting, truncated, or unavailable evidence remains
  `recovery_required` or `partial_failed` and is reported for manual review.

The workflow updates existing batch/leg status through one transaction, records
the reason and reconciliation time, and emits no Deepcoin write. It never calls
an order-submission method.

## Snapshot Reliability

Treat `source_component_changed_during_read` and
`source_component_set_changed` as the same bounded transient snapshot family as
`source_snapshots_differ`: retry the complete private snapshot exactly once.
The second failure remains fail-closed as `audit_incomplete`. No unbounded loop
or pause is added to the trading service.

## Verification And Rollout

- Add failing tests for terminal/actionable blocked classification, unchanged
  treatment of unresolved statuses, and bounded snapshot retry.
- Add dry-run/apply tests proving fingerprint enforcement, exact identity,
  idempotence, zero submission calls, and fail-closed handling of incomplete
  evidence.
- Run focused tests, the complete local suite excluding only documented
  pre-existing failures, and code review.
- Push the reviewed branch, prove a fresh production safe window, deploy through
  the normal Git workflow, and verify the stop-rescue setting remains effective
  `live`.
- Run the recovery workflow in dry-run mode first. Apply only individually
  proven decisions, then rerun the production audit and safety monitor.
- If any selected batch cannot be proven terminal, leave it unchanged and
  report the exact missing evidence. Do not force the monitor green.

## Rollback

The monitor classification and snapshot retry are code-only and can be reverted
through the normal deployment workflow. Historical convergence writes are
allowed only for terminal states backed by a durable evidence fingerprint; they
are not rolled back into actionable states because doing so would misrepresent
confirmed exchange truth. No tables or history rows are deleted.
