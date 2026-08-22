# Authoritative Failure Notification Idempotency Design

## Problem and evidence

Production durable message processing retries a returned MiMo
`authoritative_failed` result up to five times. Each attempt currently calls
`_handle_authoritative_failure_notification`, which schedules the same Telegram
system notification without checking the persisted `notification_status`.

The 2026-08-22 Phase 6R observation exposed the failure mode directly: six
recent failed decisions accumulated 27 durable attempts, every decision ended
with `notification_status=sent`, and the user received repeated system-bot
messages. DeepSeek semantic review remained disabled and produced zero new
invocations or 402s, so changing that switch would not address this incident.

## Approved choice

The owner approved option A: retain the first authoritative-failure alert but
make delivery once-only per `raw_message_id` across durable recognition retries.

Rejected alternatives:

- notify only after the final recognition attempt, because it delays the first
  operational warning;
- disable all authoritative-failure notifications, because it hides distinct
  new failures and unrelated critical incidents.

## Design

Add one atomic persistence helper on `recognition_decisions`. It may transition
`notification_status` from `NULL` or `failed` to `scheduled` and returns true
only to the caller that owns that transition. `scheduled`, `sent`, and every
`suppressed_*` state are terminal for duplicate scheduling.

`_schedule_authoritative_notification` must claim this transition before it
creates the async sender task. A successful send writes `sent`; a send exception
writes `failed`, preserving the existing incident capture and allowing one later
durable attempt to retry delivery. Re-recognition already preserves notification
metadata, so later MiMo attempts cannot erase the once-only boundary.

This changes only duplicate delivery. It does not alter MiMo calls, retry count
or backoff, recognition payloads, authoritative result selection, automation,
context resolution, strategy ownership, exchange writes, queue ordering,
runtime modes, topology, or any Phase 6A notification outbox.

## Verification and rollout

TDD must first reproduce two schedules for one persisted sent decision. Focused
tests then cover the atomic claim, suppression after `scheduled`/`sent`, retry
after `failed`, and the existing durable-retry behavior. Run one final full suite
for the assembled production candidate.

Deploy only through the existing exact-SHA gated updater after a zero-active-
write quiet gate. Production verification must show unchanged
`global/queue/shadow` modes, semantic review still disabled, no new exchange
event caused by the deployment, and naturally occurring repeated MiMo attempts
producing no second notification for the same raw message. Never manufacture a
Telegram message, recognition failure, strategy, position, or order.

Operational rollback is a reviewed revert of the hotfix followed by the same
gated updater. No schema or data rollback is needed.
