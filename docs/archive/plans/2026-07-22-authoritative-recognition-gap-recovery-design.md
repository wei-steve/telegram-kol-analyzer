# Authoritative Recognition Gap Recovery Design

## Context

The live and history paths persist a raw Telegram message before calling the
authoritative MiMo recognition and execution pipeline. If that later step is
interrupted, the raw message remains without a `recognition_decisions` row.
Periodic reconciliation advances its history checkpoint from persisted raw
messages and only processes newly inserted records, so it does not revisit the
gap. The result can be a missed close instruction even while the listener looks
healthy.

## Decision

At the beginning of every Telegram reconciliation pass, load a bounded,
chronological set of recent raw messages in the configured chat scope that have
no authoritative recognition decision. Process them through the same
`authoritative_processor` used for newly inserted messages, then retain the
existing notification and instruction-summary behavior.

The recovery scan is intentionally narrow:

- It runs only when an authoritative processor is configured.
- It auto-recovers only configured-chat messages whose Telegram `posted_at`
  timestamp is within the last 15 minutes, so a historic import or late
  delivery cannot replay a stale instruction.
- It ignores rows that already have a `recognition_decisions` record.
- It does not manufacture candidates, lifecycle changes, or exchange actions
  itself; `process_authoritative_message` remains the sole decision and
  execution boundary.
- Existing decision and execution idempotency guards remain authoritative, so
  a retry cannot submit an additional order for an already processed message.

Realtime ingestion and the recovery pass use the Web application's existing
Telegram operation lock. This makes the missing-decision query and processing
sequence mutually exclusive with new-message ingestion, preventing a live
processor from completing the same message after recovery has selected it.
An exception for one recovered message is logged and left without a decision
for a later retry; it does not prevent later eligible gaps from being handled.
An expired gap is never executed. Instead, the system writes a terminal
`recovery_guard` authoritative-failure decision and visible recognition result,
then sends the existing operator notification without scheduling an execution
retry. This notification intentionally bypasses low-value external-market
suppression: every expired gap needs operator visibility.

## Alternatives Considered

1. Make checkpoints wait for recognition completion. This couples independent
   transport progress to AI availability and leaves live-path interruptions
   difficult to recover safely.
2. Alert only on gaps. It surfaces the incident but still leaves time-sensitive
   close instructions unprocessed.
3. Scan and recover only missing decisions. Chosen because it repairs the
   durable gap with the smallest change and reuses the established execution
   guards.

## Verification

Add focused reconciliation tests that start with an already persisted,
unrecognized message and a history checkpoint at or beyond that message. The
next reconciliation must invoke the authoritative processor exactly once,
without needing a newly inserted Telegram record. A second reconciliation must
not invoke it again once the processor has persisted its decision. Verify old
records are excluded from automatic execution but recorded for operator review,
a failed recovered record does not block the next one, and realtime handling
waits for the same operation lock.

Production verification will inspect the service state, a fresh live message,
and the absence of recent configured-chat raw messages without an authoritative
decision. No live order will be used as a test fixture.
