# System Operator Callback Event-Loop Repair Design

## Problem

Task 13 stopped before `per_chat + 3` cutover because the worker recorded two
real event-loop stalls, with a worst delay of `3972.537 ms`. The captured stack
shows `run_system_operator_bot_command_loop()` synchronously executing
`process_system_operator_callback_data()`. The expiry-refresh branch constructs
a Deepcoin client, reconciles execution bindings, queries SQLite, and builds
economic evidence on the asyncio thread.

The existing blocking-call census already lists this callback path as a known,
unowned blocking call. The incident therefore confirms a documented gap rather
than a new per-chat scheduling defect.

## Approved Approach

Treat one callback as a single blocking management unit:

1. Determine whether the callback needs a Deepcoin client.
2. Construct that client inside the blocking unit.
3. Run `process_system_operator_callback_data()` in the same unit.
4. Submit the unit to the existing process-wide, single-thread management
   executor with `run_on_management_worker()`.
5. Await the result before sending the Telegram callback response.

The single-thread executor preserves the current mutual exclusion between
strategy management, reconciliation, operator maintenance, and callback work.
It also keeps Deepcoin client construction and use on one worker thread. The
default asyncio executor is deliberately not used because it is shared and can
introduce concurrency with management and exchange-sensitive paths.

## Rejected Alternatives

- `asyncio.to_thread()` around only the processor: simpler, but it uses the
  shared default executor and constructs the client on a different thread.
- Offload only expiry-refresh reconciliation: narrower than the observed stack,
  but leaves other callback database or exchange operations on the event loop.

## Behavior Boundaries

- Preserve callback ordering: the Bot still processes one update at a time and
  waits for processing before answering Telegram.
- Preserve callback return values and existing exception handling.
- Preserve the current client lifetime; client closing is outside this repair.
- Do not alter recognition, strategy choice, attribution, execution semantics,
  settings, queue ordering, or exchange-write semantics.
- Do not repair the separate command-message blocking allowlist entries in this
  change.

## RED-to-GREEN Verification

- A loop-responsiveness regression test injects a blocking callback processor
  and proves the Bot loop no longer starves a heartbeat coroutine.
- A thread-identity test proves Deepcoin client construction and callback
  processing both run on the same `mgmt-worker` thread.
- The blocking-call census removes exactly the repaired callback entry.
- Focused Bot, executor, census, and Web lifecycle tests must pass.
- Because production code changes, the rebuilt candidate receives one final
  complete suite after all production edits are frozen.

## Authorization Boundary

This design is authorized for local RED-to-GREEN implementation and candidate
rebuild only. It does not authorize push, deployment, restart, production
settings, cutover, manufactured Telegram traffic, database mutation, or
exchange writes.
