# Runtime Soak Findings Remediation Design

**Date:** 2026-08-20

**Scope:** Repair two defects found during the production queue soak without
claiming or implementing runtime-serialization Phase 6. Preserve recognition,
strategy resolution, position ownership, exchange execution, and
`message_lock_mode=global` semantics.

## Background

The read-only production review found that the durable message queue itself was
consistent: every observed raw message had one job, no backlog or duplicate
execution was found, and all bounded exchange identity checks completed. Two
operational defects were found around that queue:

1. The production safety monitor records `last_full_audit_date` only when a full
   audit has no findings. A complete audit that correctly reports historical
   anomalies is therefore rerun every timer interval. On the small production
   host, those repeated full audits correlate strongly with event-loop stall
   warnings.
2. Deterministic empty-input recognition failures (`message has no readable
   text or image`) are already treated as non-actionable for operator
   notification, but the durable consumer retries them exactly like transient
   provider or adapter failures. Five such jobs exhausted all five attempts even
   though the input cannot change.

Five historical management batches also remain `recovery_required`. Their last
progress predates the queue cutover, so they are evidence to investigate, not a
queue regression and not authorization for data repair.

## Goals

- Run at most one successfully completed full production audit per Shanghai
  calendar day while keeping real audit findings active and visible.
- Settle deterministic empty-input jobs once, fail closed, without entering the
  strategy or trading chain and without retrying them.
- Preserve retries for transient or unknown authoritative failures.
- Investigate the five historical recovery batches read-only and produce an
  evidence-backed recommendation.
- Keep Phase 6 unclaimed and leave its status file unchanged.

## Non-goals

- Changing recognition output, policy resolution, position attribution, order
  construction, exchange-write behavior, or lock scope.
- Silencing or clearing valid monitor anomalies.
- Repairing, replaying, cancelling, or mutating production data or exchange
  orders.
- Starting process separation or changing `message_lock_mode=global`.
- Adding CPU throttling before fixing the scheduling root cause.

## Design 1: Completed Audit Scheduling

The monitor must distinguish audit execution completeness from audit health.
These are separate questions:

- **Completed:** the adapter returned a complete, stable, parse-valid audit
  snapshot. This controls the daily schedule marker.
- **Healthy:** the completed snapshot contains no audit findings. This controls
  result severity, active reason codes, notification, and recovery semantics.

`last_full_audit_date` keeps its existing field name and state schema to avoid a
migration. Its meaning becomes the last Shanghai date on which a complete audit
was obtained. A complete abnormal audit advances the date and keeps
`audit_abnormal` and any composite reasons active. A command failure, adapter
failure, incomplete result, or unstable snapshot does not advance the date and
may retry on the next scheduled timer invocation.

The completion predicate must be narrow and derived from the audit result's
existing structural and snapshot-validity contract. It must not infer
completion from an empty reason list, and it must not treat a failed adapter's
fallback as completed. Audit health continues to use the existing evaluation
path so alert and recovery behavior is unchanged.

State persistence remains atomic. If writing the state fails, the date is not
durably advanced and the next run retries naturally.

### Monitor acceptance

- A complete abnormal audit advances the Shanghai date.
- A second normal invocation on the same date skips the full audit.
- The anomaly remains active and notification deduplication is unchanged.
- Incomplete, unstable, adapter-failed, or nonzero-command audits do not advance
  the date.
- A later complete healthy audit can still prove recovery under the existing
  recovery rules.

## Design 2: Terminal Empty-input Queue Outcome

The worker receives the authoritative processing result before deciding whether
to retry. Add an explicit terminal, non-retryable authoritative-failure outcome
for the exact persisted reason `message has no readable text or image`.
Classification must inspect the processing result itself, not notification text
or a reconstructed display payload.

The recognition decision remains `authoritative_failed`, automation remains
`skipped`, and `process_message_job` stops before strategy alerts, contextual
workers, or trading execution. A dedicated terminal exception or equivalent
typed outcome carries a stable reason code to the queue tick. The queue tick
catches it before the generic exception path and settles the job as
`succeeded` with an explicit terminal fail-closed reason. In queue terminology,
`succeeded` means the durable job has been fully handled; it does not relabel the
recognition decision as successful.

This terminal settlement:

- does not increment `attempt_count`;
- does not schedule a retry;
- does not invoke the terminal-failure notifier;
- clears the claim and permits the next job in the same chat lane;
- retains the already-persisted authoritative failure and skipped automation.

Every other `authoritative_failed` result continues through the existing
`AuthoritativeProcessingFailed` path, including durable backoff, five-attempt
limit, and terminal notification. Unknown shapes fail closed into the existing
retry path rather than being guessed to be empty input.

### Worker acceptance

- Exact deterministic empty input settles once with zero retry attempts and an
  explicit terminal reason.
- No strategy, context-resolution worker, or exchange-authorizing path runs for
  that result.
- The next message in the same chat lane can be claimed normally.
- Transient authoritative failures continue to retry and still fail terminally
  at the configured limit.
- Cancellation, stale-claim recovery, expiry, per-chat ordering, and cross-chat
  concurrency behavior remain unchanged.

## Historical Recovery Batch Audit

Audit the five known `recovery_required` batches individually using only
server-side read-only sources. For each batch, record local lifecycle evidence,
position identity, order identity, and bounded Deepcoin history. An incomplete
external response remains unknown; allow one reasoned retry and then fail
closed.

Store detailed rows and raw responses in a server-side evidence file. The
summary should contain only batch identity, evidence completeness,
classification, and recommendation. Do not modify the database, replay a
message, cancel an order, or send an exchange write. Any proposed recovery is a
separate L3 change with an exact rollback plan and fresh user approval.

## Delivery and Verification

Deliver the two code repairs as separate reviewed commits and production
checkpoints.

1. **Monitor repair (L1):** use focused TDD, then run one full local suite on the
   final candidate. Deploy in a proven safe window. Observe two natural monitor
   timer cycles: the first complete full audit records the date, the next skips
   the full audit, while existing anomalies remain active. Do not manually run
   the notifying monitor as a test.
2. **Worker repair (L2):** use focused concurrency/retry tests, then one full
   local suite on the final candidate. Deploy through the normal gated updater.
   Observe 30 continuous minutes and at least five real messages, trying to
   cover two chats. If five messages do not arrive, stop at 30 minutes and
   record limited traffic. Check queue parity, backlog, duplicate processing,
   and direct exchange history if real execution occurs. Do not generate test
   Telegram messages or exchange writes. No additional deliberate restart is
   required because restart recovery is not the repair's core claim.
3. **Historical audit:** run the bounded read-only per-batch investigation and
   stop for separate authorization if a mutation is recommended.

The worker deployment changes durable-consumer behavior and therefore resets
the one-week Phase 6 queue-stability observation start to that deployment. A
monitor-only deployment does not by itself reset that baseline, although its
deployment and service restart must be recorded. Phase 6 remains unclaimed
throughout this work.

## Rollback

- Monitor repair: revert only the monitor commit and redeploy; the unchanged
  state schema remains readable and the old schedule behavior resumes.
- Worker repair: revert only the worker commit and redeploy; empty-input jobs
  return to the existing retry behavior. Already settled jobs remain auditable
  through their stored recognition decisions and explicit queue reason.
- Neither rollback mutates exchange state or historical management batches.
