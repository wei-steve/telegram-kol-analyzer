# Phase 7 Telethon Session Entity Cache Stall Remediation Design

## Context

The failed Phase 7 acceptance window observed a real ingest event-loop stall of
roughly 4.47 seconds. The deployed stack collector removed the attribution race,
but no post-fix natural stall has yet produced a function-level stack. Local
read-only diagnosis identified a directly relevant synchronous path in Telethon:
after an awaited request completes, Telethon calls
`SQLiteSession.process_entities()` on the event-loop thread. SQLite uses its
default bounded busy wait, so a locked entity table can block that thread for
approximately five seconds.

The earlier database offload work already moved application-owned synchronous
database slices away from the event loop. This remediation does not reopen that
boundary and does not change Telegram fetch, message recognition, strategy, or
exchange semantics.

## Scope

The production change is one client-construction invariant:

```python
client.session.save_entities = False
```

It applies to the existing Telethon `SQLiteSession` created by
`create_telegram_client()`. The session remains SQLite-backed. Authentication
key, data-center, and update-state persistence remain enabled; only Telethon's
optional entity lookup cache stops writing rows during request result handling.

No process, thread, event loop, service, queue, database schema, Telegram
authority, or exchange path is added or moved.

## Safety Contract

- `create_telegram_client()` must return a client whose session has
  `save_entities is False`.
- A real `SQLiteSession.process_entities()` call made through that configured
  session must return without waiting on an externally held entity-table write
  lock.
- The same session must still persist and reload Telethon update state.
- Existing connection, proxy, timeout, retry, login, runtime-role, and session
  ownership behavior must remain unchanged.
- Disabling the cache may require Telethon to resolve an entity from Telegram
  instead of a local username/phone cache. This is an accepted read-side cost;
  application logic must not depend on the optional Telethon entity cache as an
  authority.

## RED to GREEN Proof

The RED test constructs the client through the production factory and asserts
the factory-level session contract. It then uses a real Telethon
`SQLiteSession`, holds an exclusive SQLite transaction from a second
connection, and calls `process_entities()` with a real Telethon user object.
Before the production change, the factory leaves entity persistence enabled and
the contract fails. After the change, the call returns within a strict bounded
latency because no SQLite entity write is attempted.

A separate assertion writes, closes, reopens, and reads Telethon update state to
prove the durability retained by Scheme A. The test uses only temporary local
files and synthetic Telethon objects; it does not connect to Telegram.

## Verification

Run the new focused session test first, then the directly related Telegram
factory/fetch, runtime-role/session ownership, and event-loop census tests. Once
the production-code candidate is frozen, run the repository full suite exactly
once. Documentation-only status updates after that suite do not invalidate it.

## Rollback and Deployment Boundary

Local rollback is removal of the single `save_entities = False` assignment.
Production deployment is outside this authorization and requires a separate
exact-SHA authorization.

Scheme A is an evidence-backed minimal mitigation for synchronous entity-cache
writes, not proof that every possible Telethon session write is harmless. If a
future captured production stack identifies `set_update_state()`, `save()`, or
another required session-durability write as the blocking function, Scheme A
must not be represented as a complete root-cause fix; the design must escalate
to isolating the Telethon loop/session boundary instead.
