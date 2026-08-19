# Phase 1d — Unblock the Deepcoin Execution Reconcile Loop

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Added 2026-08-19, after Phase 1c captured the loop's stack mid-stall and named
> this call as the cause of 19 of 20 observed stalls.

**Goal:** Move the last named blocking call off the asyncio event loop.

**Nature:** Behavior-preserving concurrency fix, same shape as Phase 1 but on a
loop body that interleaves blocking and awaited calls, so it needs segmented
offload rather than a single wrap.

**Prerequisite:** Phase 1c is deployed (`93d1dfb`) and its watchdog is running in
production. That matters: if stalls survive this phase, the watchdog names the
next offender immediately instead of costing another guess.

## Why this phase exists

Phase 1c captured 20 stall stacks over 25 minutes of steady state. **Nineteen**
are under `src/telegram_kol_research/web_app.py:7807`:

```python
async def run_deepcoin_execution_reconcile_loop(..., interval_seconds: int = 30, ...):
    while True:
        try:
            client = deepcoin_client_factory()                 # blocking
            synced_at = ...
            if hasattr(client, "list_open_orders"):
                reconcile_deepcoin_execution_bindings(...)     # blocking, the main offender
                if system_operator_bot_enabled(...):
                    await deliver_pending_position_attribution_incidents(...)
                    await deliver_pending_position_protection_incidents(...)
            sync_manual_closed_deepcoin_positions(...)         # blocking
            if system_operator_bot_enabled(...):
                await deliver_terminal_entry_cleanup_notifications(...)
        except DeepcoinClientError as exc:
            logger.warning(...)
        except Exception:
            logger.exception(...)
        await asyncio.sleep(interval_seconds)
```

Every previously unexplained number follows from this one call site:

| Observation | Cause |
|---|---|
| stalls every 37.36 s | 30 s sleep + 6–10 s blocking |
| durations 6–10 s | exchange HTTP + many SQLite queries + leg matching |
| outliers 15.4 s / 19.7 s | `list_open_orders`, `httpx` timeout is 15 s |
| rate ignores traffic | it is a timer; production sees 1–16 messages/hour |

## The critical design constraint — unchanged, and stronger here

Before Phase 1 all four ticks were mutually exclusive **as an accident of running
on the event loop**. Phases 1 and 1b preserved that by putting the management,
break-even, and operator ticks on one shared `max_workers=1` executor.

This loop reaches the same execution bindings, the same protection state, and the
same position audits the management ticks reach — `execution_bindings.py` is
literally the module both go through. Giving it its own executor would introduce
four-way concurrency on that shared state for the first time.

**Submit to the same `get_management_worker_executor()` singleton.** This phase
changes threading, never concurrency.

## The shape problem: do not wrap the whole body

Unlike Phase 1's loops, this body interleaves blocking calls with `await`s. The
whole body cannot be submitted as one unit. Offload the blocking segments
individually, in place, leaving the `await`s on the loop.

Two invariants that must survive:

1. **Ordering.** The blocking calls and the awaits must keep their exact
   relative order.
2. **Exception semantics.** In the original, a raise anywhere in the body skips
   everything after it and lands in the same `except` chain. Splitting into
   several submissions must not let a later segment run after an earlier one
   raised — keep one `try` around the whole body, exactly as now.

The `client` object is created in the first blocking segment and used by a later
one. With `max_workers=1` every segment runs on the same thread, so passing it
back through the loop and into the next submission is safe.

## Scope

### Task 1: Segment the loop body

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py` as needed (add, do not weaken)

Add module-level helpers for the blocking segments and submit each through
`run_on_management_worker`. Keep `except asyncio.CancelledError: raise` ahead of
the existing handlers, and keep `DeepcoinClientError` and `Exception` handling
byte-identical in behavior.

Do not change `interval_seconds`, the `hasattr(client, "list_open_orders")`
guard, the `system_operator_bot_enabled` guards, or any argument.

### Task 2: Widen the blocking-call census

**Files:**
- Modify: `tests/test_runtime_event_loop_blocking_census.py`

The census reported zero offenders while this call ran every 30 seconds. It
matched only a call that is **both** named `*_tick`/`*_once` **and** defined in
the same module. This one is neither — it is imported from `execution_bindings`.

Widen it to report any call inside an `async` while-loop that resolves to a
synchronous function, whether defined locally or imported, regardless of name.
Keep the existing exemptions: awaited calls, `asyncio.to_thread`, and
`run_in_executor` receive the function as a bare name rather than calling it.

Expect this to surface entries the narrow census never saw. **Record every new
entry in `KNOWN_BLOCKING_CALLS` with a comment; do not fix them here.** Only the
three calls this phase moves may be removed from the allowlist.

### Task 3: Responsiveness regression test

**Files:**
- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`

Add a case driving `run_deepcoin_execution_reconcile_loop` with a fake reconcile
that blocks its thread, asserting a concurrent coroutine keeps ticking. Assert
the reconcile runs on the shared `mgmt-worker` thread, and add a test that a
raise in the first blocking segment prevents the later segments from running.

### Task 4: Full local suite and commit

```bash
.venv/bin/python -m pytest -q
```

Record both counts. **Never `git add -A`.** Stage exact paths.

### Task 5: Deploy and measure

Per `deployment-procedure.md`. `EXPECTED_COMMIT` must be the **current branch
tip**, run from a checkout of that commit, exit code captured without a pipe.

Run at least 60 minutes across real traffic, then compare against Phase 1c's
production numbers: `worst_stall_ms` 19687.274, `stall_count` rate one per
37.37 s, `p99_ms` 7004.713.

**This phase finally has a falsifiable prediction.** If the reconcile loop was
the cause, `worst_stall_ms` should fall to tens of milliseconds and stalls should
approach zero. If multi-second stalls persist, the Phase 1c watchdog is still
deployed and will have captured the next offender's stack — read
`recent_stall_stacks` from `/api/runtime/loop-health` and record what it names.
Do not guess.

## Completion criteria

- All three blocking calls in the loop run on the shared `mgmt-worker` executor.
- Ordering and exception semantics are unchanged, with a test proving a raise in
  an early segment skips the later ones.
- The widened census passes, with any newly surfaced offenders recorded.
- Production `worst_stall_ms` and stall rate recorded against Phase 1c's.
- Execution binding reconciliation, manual-close sync, and incident delivery
  behave as before.

## Rollback

No settings flag. Rollback is a redeploy of `93d1dfb`, which keeps the Phase 1c
watchdog. No schema change and no persisted state.

## Status file update

Set `phase_status: completed`, fill `loop_lag_after_phase1d_p99_ms` and
`phase_1d_worst_stall_ms`, add the ledger row, and append one `local_tests` and
one `server_verification` entry stating explicitly whether the worst-stall
criterion was met this time — it was not in Phase 1, and not in Phase 1b.
