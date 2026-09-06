# Phase 6 Ingest Refresh RPC Design

**Status:** Owner-approved on 2026-08-22 for implementation inside Phase 6

## Problem

Phase 6 Task 1 requires the Telethon session to be opened only by
`telegram-kol-ingest`. The current `telegram-kol-research web` command opens the
session during startup, and `POST /api/refresh` independently opens it whenever
the app has no shared Telegram client. A split Web process would therefore still
have a reachable session-opening path.

## Decision

Keep the public `POST /api/refresh` contract, but route it by runtime role:

- `all`: execute the existing refresh path unchanged.
- `ingest`: execute the same existing refresh path with its owned Telethon client.
- `web`: make exactly one bounded HTTP request to the ingest unit and return its
  status code and JSON body without reinterpretation.
- `worker`: fail closed; it neither opens the session nor proxies refresh.

The ingest RPC listens only on localhost. It is an authority transport, not a
second implementation: the existing reconcile function, global message lock,
timeout, response shape, and exception mapping remain authoritative in ingest.

## Failure and restart semantics

The Web proxy never retries automatically. A connection failure, timeout, or
invalid response becomes a bounded `503 ingest_refresh_unavailable`. Because a
lost response cannot prove whether ingest completed, the failure is explicitly
unknown and the request is not replayed. This matches the existing ambiguous
client-disconnect boundary without creating a duplicate refresh.

The RPC has no durable database state and adds no schema or migration. Phase 6
therefore remains L2. The later systemd split binds Web to port 8000, ingest to a
separate localhost port, and worker to a separate localhost port. Only ingest is
given Telegram configuration/session-file access; Web and worker receive
`InaccessiblePaths` for the session files.

## Role and route containment

The CLI role selector defaults to `all`, so the current monolith is unchanged.
Only `all` and `ingest` load Telegram auth, acquire the session lock, or construct
Telethon. The Web role receives the ingest refresh URL but no Telegram client.
Tests assert that the route is local only in `all`/`ingest`, proxied once in
`web`, and rejected in `worker`.

## Verification

Use RED/GREEN TDD for role validation, CLI session ownership, proxy success/error
passthrough, no-retry transport failure, and default-`all` compatibility. Then
rerun the full Phase 6 Task 1 authority and session gates before continuing the
canonical Phase 6 task order.
