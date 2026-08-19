# Phase 1e — Unblock the Context Resolution Scheduler

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Added 2026-08-19, after the Phase 1c watchdog captured this call as the cause
> of the stalls that survived Phase 1d.

**Goal:** Clear the last named blocking call, so the worst-stall criterion that
Phases 1, 1b and 1d each left unmet is finally testable against a clean loop.

**Nature:** Behavior-preserving concurrency fix. Smallest of the series.

**Prerequisite:** Phase 1d is deployed (`1c8a7f2`). Its measurement is the
baseline: stalls one per 1250.15 s, p99 232.928 ms, worst 6470.313 ms, loop
unavailable 2.0%.

## Why this phase exists

Phase 1d cut the stall rate 33.5x, but three stalls remained in 62 minutes with
a worst of 6470 ms. The Phase 1c watchdog captured one of them and named the
cause with no guessing:

```
lifecycle_monitor.py:307          run_loop → await self._run_one_cycle()
lifecycle_monitor.py:617          self._context_resolution_scheduler(...)     ← sync callback
web_app.py:3647                   _schedule_context_resolution_for_app
context_resolution_worker.py:394  schedule_context_reanalysis
                                    → query.order_by(...).all()               ← SQLite, on the loop
```

`schedule_context_reanalysis` runs a `ContextResolutionAttempt` JOIN `RawMessage`
query and then writes. `_run_one_cycle` calls it **once per transition and once
per chat**, in two back-to-back loops at `lifecycle_monitor.py:611` and `:617`.
A cycle with N transitions and M chats does N+M such queries on the event loop.

**The fix is already sitting three lines below it.** The very next statement is
`await asyncio.to_thread(self._context_resolution_worker)` — the *worker* was
offloaded correctly and the *scheduler* was not.

## Scope

### Task 1: Batch the scheduler calls onto the shared executor

**Files:**
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `tests/test_lifecycle_monitor*.py` as needed (add, do not weaken)

Collect the events the two loops would have scheduled, then submit **one** call
that replays them in order on the shared `mgmt-worker` executor:

```python
def _run_context_resolution_scheduler_batch(scheduler, events) -> None:
    for event in events:
        scheduler(**event)
```

One submission rather than N+M, because the original made these calls
back-to-back with nothing between them; batching preserves that exactly while
paying a single hop.

**Use `get_management_worker_executor()`, not a new pool and not
`asyncio.to_thread`.** Same constraint as every phase in this series: these
calls were mutually exclusive with the other ticks only because everything ran
on the loop. The default executor is separately known to saturate.

Do not change the `if self._context_resolution_scheduler is not None:` guard at
`:597`, the event payloads, the ordering of the two loops, or the
`await asyncio.to_thread(self._context_resolution_worker)` that follows.

### Task 2: Tests

**Files:**
- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`

Assert the scheduler runs on the `mgmt-worker` thread, that events arrive in the
original order with identical payloads, that a cycle scheduling nothing makes no
submission, and that the scheduler still runs before the context resolution
worker.

### Task 3: Full local suite and commit

```bash
.venv/bin/python -m pytest -q
```

Record both counts. **Never `git add -A`.** Stage exact paths.

### Task 4: Deploy and measure

Per `deployment-procedure.md`. `EXPECTED_COMMIT` is the **current branch tip**,
run from a checkout of that commit, exit code captured without a pipe.

60 minutes minimum, then compare against Phase 1d: stalls one per 1250.15 s,
p99 232.928 ms, worst 6470.313 ms, unavailable 2.0%.

**Prediction:** `stall_count` reaches zero or near it and `worst_stall_ms` drops
below one second. If stalls persist, read `recent_stall_stacks` — the watchdog is
still deployed and has named the cause twice already. Do not guess.

## What this phase does NOT touch

Recorded so the next session does not assume they were handled:

- The four bot-command blocking calls in `KNOWN_BLOCKING_CALLS`. Real, but event
  driven, so they cannot produce periodic stalls.
- `lifecycle_monitor.backfill_from_trade_ideas()` at `:300`, called once before
  the `while` loop at startup. Outside the loop, so it is a startup cost only.
- `self._context_resolution_worker` running on the **default** executor via
  `asyncio.to_thread` rather than the shared one. Pre-existing, and it means that
  worker can run concurrently with the shared-executor ticks. Whether that is
  correct is a question this phase does not answer.

## Completion criteria

- The scheduler calls run on the shared `mgmt-worker` executor.
- Event order and payloads are unchanged, with a test proving it.
- The scheduler still runs before the context resolution worker.
- Production stall count and `worst_stall_ms` recorded against Phase 1d's.

## Rollback

No settings flag. Redeploy `1c8a7f2`. No schema change, no persisted state.

## Status file update

Set `phase_status: completed`, fill `loop_lag_after_phase1e_p99_ms` and
`phase_1e_worst_stall_ms`, add the ledger row, and append one `local_tests` and
one `server_verification` entry saying explicitly whether the worst-stall
criterion was met — it was not in Phase 1, 1b, or 1d.
