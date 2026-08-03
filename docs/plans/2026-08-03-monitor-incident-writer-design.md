# Monitor Incident Writer Design

## Goal

Complete Phase 8R.2 without granting the independent monitor write access to
the production SQLite database. The monitor remains read-only and submits a
bounded incident-capture projection to the already trusted main service.

## Chosen approach

Use a dedicated authenticated loopback endpoint in `telegram-kol.service`.
The monitor sends only the completed monitor timestamp, the closed monitor
failure codes, sanitized adapter labels, and the closed notification outcome.
The main service applies its own runtime capture policy and performs the
existing incident append and durable management/protection source scan through
its existing database writer.

This is preferred over making SQLite writable to the monitor, which would also
authorize every business table, and over a second SQLite outbox, which would
add an importer, recovery semantics, and another durable store to operate.

## Trust boundary

- The endpoint accepts only `127.0.0.1` or `::1` and rejects forwarded-client
  headers.
- A dedicated high-entropy monitor-capture token is required and compared in
  constant time. It is not a Telegram, provider, exchange, or Agent key.
- The installer copies only the capture allowlist and this dedicated token
  into the root-owned monitor environment.
- Request bodies are strictly parsed, size bounded, duplicate-key rejected,
  and limited to a versioned closed schema.
- Reason codes are limited to `adapter_failure` and `audit_incomplete`;
  notification errors are limited to the two monitor delivery failures.
- The endpoint cannot name an incident type, source record, fingerprint,
  strategy, position, order, or arbitrary summary.
- A non-blocking single-flight lock prevents concurrent capture requests.

## Data flow

1. The monitor reads and evaluates production state exactly as it does now.
2. It persists monitor state and optionally sends the existing independent
   operator alert.
3. It POSTs the bounded capture projection through a proxy-disabled loopback
   client with a short timeout.
4. The main service revalidates the projection and uses its already loaded
   `RuntimeIncidentConfig`.
5. The main service invokes `capture_monitor_state`, optionally
   `capture_notification_failure`, and
   `capture_uncaptured_runtime_incident_sources`.
6. The response exposes only accepted/captured counts. Capture transport
   failure is logged safely and never changes the monitor result, alert, or
   business path.

## Activation and rollback

The first deployment keeps capture at `management_partial_failed`. Production
activation requires a fresh safe window, a generated dedicated token, matching
main/monitor configuration, and a successful authenticated no-op probe. New
capture types are then enabled one at a time while Telegram and Agent selectors
remain pinned to `management_partial_failed`.

Rollback clears the new capture types first. The loopback writer may remain
dormant, or its token can be cleared after stopping the monitor timer. The
monitor database mounts remain read-only throughout.

## Verification

- Endpoint authorization, strict schema, size, concurrency, and fail-closed
  tests.
- Client fixed-loopback, no-proxy, timeout, and best-effort failure tests.
- Real source-parity tests proving three submissions produce one generation
  and never change management/protection source rows.
- Installer and systemd tests proving the production database is still
  read-only and only the dedicated token is shared.
- Production no-op probe, then one-type-at-a-time capture comparison with no
  Telegram or Agent claim for newly enabled types.
