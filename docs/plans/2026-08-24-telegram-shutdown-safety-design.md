# Telegram Shutdown Safety Design

## Context

The exact production deployment of
`cdfe1b73c40d34d92bf613e5bcf0c81bf1fc0007` completed successfully but exposed
two old-process shutdown defects:

- the ingest process performed two overlapping Telethon disconnects; one path
  closed the SQLite session while the keepalive task owned by the other path
  was still persisting update state;
- the worker system-operator Bot received a transient Telegram HTTP 502, its
  polling task terminated, and lifespan shutdown re-raised the already-failed
  task with an authenticated request URL in the traceback.

The server currently has ample disk and inode capacity, and the Telegram
session is writable by the ingest service. The ingest failure is therefore a
lifecycle ownership race, not a storage-capacity repair.

## Goals

- Give one task sole ownership of Telethon disconnect during normal shutdown.
- Keep Bot polling alive across transient Telegram transport, rate-limit, and
  server failures without hiding permanent configuration or authentication
  failures.
- Ensure an already-failed Bot task cannot fail FastAPI lifespan shutdown.
- Redact Telegram Bot credentials before application logs reach either the
  rotating file or stderr/systemd journal.
- Preserve all message recognition, durable queue, management, position,
  execution, exchange-write, and concurrency semantics.

## Non-goals

- No credential rotation.
- No Telegram, Deepcoin, production database, schema, settings, or cutover
  mutation.
- No deployment, restart, manufactured traffic, dependency patch, or broad
  background-task supervisor.
- No change to callback completion, cancellation draining, or management
  executor ordering introduced by the existing candidate.

## Selected Architecture

### Single-owner Telethon shutdown

`run_live_listener()` already awaits Telethon's `run_until_disconnected()`.
Telethon guarantees that this method calls `disconnect()` from its own
`finally` block. FastAPI must therefore cancel and await the listener task
instead of independently calling `client.disconnect()` first. This makes the
listener task the sole normal-shutdown owner.

The app keeps the existing bounded wait. If the listener does not terminate
within the bound, shutdown records a warning and continues; it must not start a
second disconnect path. A listener that already completed is inspected and its
result is consumed without another client close.

### Recoverable Bot polling

Both Bot command loops share one polling-recovery helper. Initialization
(`deleteWebhook`, command registration, and initial offset discovery) remains
fail-fast. Once polling begins:

- timeouts and `httpx.RequestError` transport failures are retried after the
  configured polling interval;
- HTTP 429 and 5xx responses are retried after the same bounded interval;
- other HTTP failures, including authentication and request-contract errors,
  remain terminal so invalid configuration is visible instead of looping
  forever;
- cancellation is never converted into a retry.

Retry logs contain only a stable Bot label, exception class, and optional HTTP
status. They never interpolate the exception, request, or authenticated URL.

### Task cleanup

The regular and system-operator Bot tasks use the same background-result
callback so failures are visible while the process is running. Lifespan uses a
Bot-specific stop helper that cancels and awaits each task, consumes both
`CancelledError` and an exception from a task that failed before cancellation,
and always clears app state. The background-result callback remains the single
failure report, avoiding a second shutdown traceback.

### Log redaction at emission

Application logging uses a formatter that applies the existing Telegram Bot
URL pattern to the fully formatted record. Redaction therefore covers normal
messages and rendered exception tracebacks before either configured handler
writes bytes. `read_log_page()` retains its existing second-layer redaction for
legacy log files.

This boundary intentionally does not alter global Python, Uvicorn, or Telethon
logging. Bot task exceptions remain inside the application task boundary, so
they do not escape into an unredacted Uvicorn lifespan traceback.

## Error Handling and Invariants

- A retryable poll failure cannot advance the Telegram update offset.
- A successfully returned update batch retains the existing offset and
  callback-processing behavior.
- Permanent HTTP failures terminate the task once and are emitted only through
  a redacting application handler.
- Normal ingest shutdown calls Telethon disconnect exactly once.
- A disconnect timeout remains bounded and never launches a competing close.
- No shutdown handler settles durable jobs, changes retry state, or calls an
  exchange client.

## RED-to-GREEN Verification

The implementation will add focused regressions that first fail against the
current code:

1. a Telethon-shaped listener proves normal lifespan shutdown invokes exactly
   one disconnect;
2. a 502 followed by a valid poll proves the Bot loop continues, while a 401
   remains terminal;
3. an already-failed Bot task proves lifespan exits successfully and clears the
   task state;
4. a synthetic authenticated HTTP exception proves the raw rotating log and
   captured stderr contain the redacted URL and not the test token;
5. the existing bounded listener-shutdown and callback cancellation suites
   remain green.

After all production-code edits are assembled, run the affected focused suites,
compile checks, diff checks, and one final complete pytest suite. Any later
production-code edit invalidates that complete-suite result.

## Delivery Boundary

The result is a reviewed local candidate only. Push, deployment, restart,
production observation, credential rotation, settings changes, database writes,
Telegram traffic, cutover, and exchange writes require separate authorization.
