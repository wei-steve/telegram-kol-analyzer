# Phase 1b — Unblock the Operator Maintenance Tick

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Added 2026-08-18, after Phase 1 shipped and its production measurement showed
> the loop was still being blocked by a third caller.

**Goal:** Move the last known synchronous tick off the asyncio event loop, so
the loop is genuinely unblocked rather than mostly unblocked.

**Nature:** Behavior-preserving concurrency fix. Identical in shape to Phase 1,
smaller in size, and it reuses Phase 1's executor unchanged.

**Prerequisite:** Phase 1 is complete and deployed (`fd748d7`), and its
production numbers are recorded in
`docs/runtime-serialization-remediation-status.md`.

## Why this phase exists

Phase 1 moved both management worker ticks off the loop and the result was
large but incomplete:

| | Phase 0 baseline | after Phase 1 |
|---|---|---|
| p95 | 8311.911 ms | 12.183 ms |
| p99 | 8777.887 ms | 6765.435 ms |
| **worst stall** | **15160.203 ms** | **15356.616 ms** |
| stall episodes | 1 per 10.7 s | 1 per 36.9 s |

`worst_stall_ms` did not move. Phase 1's file predicted exactly this and named
the cause: another blocking call is still on the loop. It is the only remaining
entry in `KNOWN_BLOCKING_CALLS`:

`src/telegram_kol_research/system_operator_bot.py:2564`

```python
async def run_runtime_incident_notification_loop(...):
    while True:
        execution_client = None
        try:
            execution_settings = load_trading_settings(session_factory)   # database, on the loop
            ...
                execution_client = build_deepcoin_client_from_env()       # exchange client, on the loop
            run_operator_maintenance_tick(...)                            # blocking, on the loop
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            close_client()                                                # httpx close, on the loop
        ...
        await asyncio.sleep(max(0.1, float(interval_seconds)))
```

This runs **every 5 seconds**. `run_operator_maintenance_tick` reaches entry
admission reconciliation and execution-contract reconciliation, and when
`instruction_execution_contract_mode` is not `disabled` it is handed a
`DeepcoinClient` whose `httpx.Client` has a 15 second timeout. A 15 second worst
stall and a 15 second exchange timeout are not a coincidence worth ignoring.

## The critical design constraint — unchanged from Phase 1

Before Phase 1 all three ticks were mutually exclusive **as an accident of
running on the event loop**. Phase 1 preserved that between the management and
break-even ticks by giving them one shared `max_workers=1` executor.

The operator maintenance tick was the third member of that accidental
exclusion. Giving it its own executor would let it run concurrently with a
management batch for the first time, on paths that both touch execution
contracts, entry admissions, and the exchange. That is the exact behavior change
Phase 1's constraint exists to prevent.

**Therefore: submit to the same `get_management_worker_executor()` singleton.**
The three ticks keep queueing behind one another exactly as they did on the
loop — no regression, because that queueing is the pre-existing behavior — while
the event loop is freed. This phase changes threading, never concurrency.

Widening this to real parallelism is a separate, later decision that needs its
own evidence.

## Scope

Move the settings read, the client construction, the tick, and the client close
onto the shared executor as one unit. Do not change tick logic, cadence, limits,
ordering, or error handling.

Explicitly **out of scope:** `deliver_runtime_incident_notifications`, awaited
later in the same loop body. It is already an `async def`. It does perform
synchronous database reads inside, which is worth a look eventually, but it is
not a multi-second blocker and it is not this phase's target.

### Task 1: Offload the maintenance cycle

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_system_operator_bot*.py` as needed (add, do not weaken)

**Step 1 — Extract the whole cycle as one submitted unit**

Settings, client construction, tick, and close must stay together on one thread.
Splitting them would put the client's lifecycle on a different thread from its
use.

```python
def _run_operator_maintenance_cycle(session_factory, *, deepcoin_client_factory=None) -> None:
    execution_client = None
    try:
        execution_settings = load_trading_settings(session_factory)
        ...
        run_operator_maintenance_tick(...)
    finally:
        close_client = getattr(execution_client, "close", None)
        if callable(close_client):
            close_client()
```

**Step 2 — Submit it**

```python
        try:
            await run_on_management_worker(
                _run_operator_maintenance_cycle,
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
```

Keep the bare `except Exception: pass`. It is the pre-existing error handling
and this phase does not get to improve it.

**Step 3 — Record the one accepted divergence**

In the original, `close()` sits in a `finally` *outside* the `except Exception`,
so an exception raised by `close()` escapes the `while` loop and kills the task.
After this change `close()` runs inside the submitted unit, so such an exception
is swallowed by the loop's existing `except Exception: pass`.

This is a real, small behavior change and it is accepted deliberately: it is
strictly more robust, and the alternative — closing an `httpx.Client` back on
the event loop — reintroduces the blocking call this phase exists to remove.
Record it; do not hide it.

### Task 2: Empty the blocking-call census allowlist

**Files:**
- Modify: `tests/test_runtime_event_loop_blocking_census.py`

`KNOWN_BLOCKING_CALLS` becomes empty. The census asserts equality, so from this
commit onward *any* new synchronous tick called from an async loop fails the
suite. That is the point.

### Task 3: Tests

**Files:**
- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`

Add a case driving `run_runtime_incident_notification_loop` with a fake tick
that blocks its thread, asserting a concurrent coroutine keeps ticking — the
same shape as Phase 1's guard. Also assert the operator tick runs on the same
`mgmt-worker` thread as the management tick, which is the guard against someone
later giving it its own pool.

### Task 4: Full local suite and commit

```bash
.venv/bin/python -m pytest -q
```

Record both counts. **Never `git add -A`.** Stage the exact paths.

### Task 5: Deploy and measure

Follow `deployment-procedure.md`, **with the correction recorded in the status
file**: the updater takes no change class and no snapshot argument.

```bash
git push origin codex/phase0-deploy-integration:codex/deepcoin-auto-trading-v1
EXPECTED_COMMIT=<40-hex> ./scripts/server_git_update.sh
```

Run it from a checkout of the commit being deployed or the SHA256 guard refuses.

Then run at least 60 minutes across real traffic and compare against Phase 1:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s --max-time 40 http://127.0.0.1:8000/api/runtime/loop-health'
```

The expected result is the one Phase 1 did not deliver: `worst_stall_ms` in tens
of milliseconds and `stall_count` at or near zero.

If multi-second stalls survive this too, then the remaining blocker is something
the AST census cannot see — a synchronous call inside an `async def` that is not
a `_tick`/`_once` name, or blocking work inside an awaited coroutine. Record
that finding and stop; widening the census is its own task.

## Completion criteria

- The operator maintenance cycle runs on the shared `mgmt-worker` executor.
- `KNOWN_BLOCKING_CALLS` is empty and the census passes.
- The responsiveness guard covers this loop.
- Production `worst_stall_ms` and `stall_count` recorded against Phase 1's
  15356.616 ms and 103.
- Operator maintenance and incident notification behavior unchanged.

## Rollback

No settings flag. Rollback is a redeploy of `fd748d7`. No schema change and no
persisted state, so the revert is complete.

## Status file update

Set `phase_status: completed`, `current_phase: 2`, fill
`loop_lag_after_phase1b_p99_ms` and `phase_1b_worst_stall_ms`, add the ledger
row, and append one `local_tests` and one `server_verification` entry stating
explicitly whether the worst-stall criterion was met this time.
