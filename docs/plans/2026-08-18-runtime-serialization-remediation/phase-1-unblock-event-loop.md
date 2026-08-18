# Phase 1 — Unblock the Event Loop

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Stop synchronous exchange I/O from blocking the asyncio event loop,
without changing which work runs, in what order, or with what result.

**Nature:** Behavior-preserving concurrency fix. Highest benefit-to-risk ratio in
the whole remediation.

**Prerequisite:** Phase 0 is complete and a production loop-lag baseline is
recorded in `docs/runtime-serialization-remediation-status.md`. Without the
baseline this phase cannot be proven to have worked.

## Why this phase exists

`run_strategy_management_worker_loop` calls its synchronous tick directly inside
an `async def`, with no thread offload:

`src/telegram_kol_research/strategy_management_worker.py:920`

```python
async def run_strategy_management_worker_loop(...):
    while True:
        try:
            settings = load_trading_settings(session_factory)
            run_strategy_management_worker_tick(...)   # blocking, on the loop
        except Exception:
            logger.exception("strategy management worker tick failed")
        await asyncio.sleep(max(0.01, float(interval_seconds)))
```

The identical pattern is at
`src/telegram_kol_research/break_even_convergence_worker.py:344`.

The tick reaches `load_deepcoin_execution_reconciliation_snapshot`, which calls
`list_positions` and `list_open_orders`. `DeepcoinClient` uses `httpx.Client` —
synchronous, 15 second default timeout
(`src/telegram_kol_research/deepcoin_client.py:301`). A tick handles up to 10
batches. `load_trading_settings` is also a synchronous database read on the loop.

While a tick runs, Telethon cannot deliver messages, SSE cannot push, HTTP
handlers cannot answer, and every other worker is frozen.

For contrast, the correct pattern already exists in this codebase at
`src/telegram_kol_research/source_message_deletion_worker.py:105`, which uses
`await asyncio.to_thread(...)`.

## The critical design constraint

Today these two ticks are mutually exclusive **as an accident of running on the
event loop**. Nothing else enforces it. A naive `asyncio.to_thread` on each loop
independently would let the strategy management tick and the break-even
convergence tick run concurrently for the first time, which is a real behavior
change on shared management batches and protection state.

Therefore: both loops must offload onto **one shared dedicated executor with
`max_workers=1`**. That frees the event loop while preserving the existing
mutual exclusion exactly. Do not give each loop its own executor. Do not use the
default executor — it is shared with every `asyncio.to_thread` in the process and
is already known to saturate (`src/telegram_kol_research/web_app.py:279`).

Widening this concurrency is a separate, later decision that needs its own
evidence. This phase changes threading only, never concurrency.

## Scope

Move both worker ticks off the event loop onto one shared single-worker
executor. Do not change tick logic, cadence, batch limits, ordering, or error
handling.

### Task 1: Add the shared worker executor

**Files:**
- Create: `src/telegram_kol_research/runtime_worker_executor.py`
- Create: `tests/test_runtime_worker_executor.py`

Provide a small module owning one lazily created
`concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mgmt-worker")`,
with:

- `get_management_worker_executor()` returning the singleton.
- `shutdown_management_worker_executor(wait: bool)` for lifespan shutdown.
- `async def run_on_management_worker(fn, /, *args, **kwargs)` that submits to
  the executor and awaits the result, propagating exceptions unchanged.

Tests must assert: exactly one worker thread, submissions execute serially in
submission order, exceptions propagate to the awaiting coroutine unchanged, and
shutdown is idempotent.

[local]

```bash
.venv/bin/python -m pytest tests/test_runtime_worker_executor.py -v
```

### Task 2: Offload the strategy management worker loop

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `tests/test_strategy_management_*.py` as needed (add, do not weaken)

**Step 1 — Change the loop body only**

In `run_strategy_management_worker_loop` (`:908`), move both `load_trading_settings`
and `run_strategy_management_worker_tick` onto the shared executor. Both are
blocking; offloading only the tick leaves a database read on the loop.

Preferred shape: one helper submitted as a single unit, so settings and tick stay
on the same thread and the pairing remains atomic:

```python
async def run_strategy_management_worker_loop(...):
    cursor = StrategyManagementWorkerCursor()
    while True:
        try:
            await run_on_management_worker(
                _load_settings_and_run_tick,
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
                max_batches=max_batches,
                cursor=cursor,
                now_provider=now_provider,
                contract_spec_provider=contract_spec_provider,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("strategy management worker tick failed")
        await asyncio.sleep(max(0.01, float(interval_seconds)))
```

**Step 2 — Preserve cancellation semantics exactly**

`asyncio.CancelledError` must propagate, not be swallowed by the broad
`except Exception`. The lifespan owns cancellation for this loop; verify that
cancelling the task still stops the loop and does not leave the executor holding
a half-finished tick. A tick already in flight completes on its thread — that is
correct and matches the current behavior, where an in-flight tick also completes
before cancellation is observed.

**Step 3 — Confirm the mutable cursor is still safe**

`StrategyManagementWorkerCursor` is mutated by the tick. With a single-worker
executor there is still exactly one tick in flight at a time, so it remains safe.
Add a test that asserts cursor lane alternation is unchanged after the move.

### Task 3: Offload the break-even convergence worker loop

**Files:**
- Modify: `src/telegram_kol_research/break_even_convergence_worker.py`
- Modify: `tests/test_break_even_convergence_*.py` as needed

Apply the identical treatment at `:344`, using the **same shared executor**. Add
a test asserting both loops submit to the same executor instance — this is the
guard that prevents someone later "optimizing" them into separate pools and
silently introducing concurrency.

### Task 4: Shrink the blocking-call census allowlist

**Files:**
- Modify: `tests/test_runtime_event_loop_blocking_census.py`

Remove both entries from `KNOWN_BLOCKING_CALLS`. The census must now find zero
offenders for these two modules. If Phase 0 recorded additional offenders, leave
those in the allowlist untouched — they are not this phase's scope.

[local]

```bash
.venv/bin/python -m pytest tests/test_runtime_event_loop_blocking_census.py -v
```

### Task 5: Add a loop-responsiveness regression test

**Files:**
- Create: `tests/test_worker_loop_does_not_block_event_loop.py`

Drive each worker loop with a fake tick that sleeps synchronously for a
meaningful interval, and assert that a concurrently running coroutine keeps
ticking on schedule. Use injected fakes and a short sleep; the assertion is on
loop responsiveness, not on wall-clock duration, so keep it robust on a loaded
machine.

This test is what stops the defect from coming back.

### Task 6: Shutdown wiring

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

Call `shutdown_management_worker_executor` in the lifespan shutdown path,
after the worker tasks are cancelled. Assert in a test that shutdown does not
hang when a tick is in flight.

### Task 7: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Expected: no new failures relative to the pre-change baseline. Record both
counts.

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/runtime_worker_executor.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/break_even_convergence_worker.py \
  src/telegram_kol_research/web_app.py \
  tests/test_runtime_worker_executor.py \
  tests/test_worker_loop_does_not_block_event_loop.py \
  tests/test_runtime_event_loop_blocking_census.py
git diff --cached --name-only
git commit -m "fix: run management and break-even worker ticks off the event loop"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 8: Deploy and prove the improvement

**Step 1 — Deploy**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
This phase deploys with `-ChangeClass execution_writer`.

Because the class is `execution_writer`, capture a prior independent live
position snapshot first and pass `-PreviousLiveSnapshotPath`.

[local] Commit, push to the deploy branch recorded as `deploy_branch` in the
status file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and the change class above.

The updater enforces the safe window itself through `deployment-preflight`
before it stops the service. If it returns `BLOCK`, read the reason, wait, and
record it — do not retry blindly.

Also confirm no management batch is mid-submission before starting. If the
preflight blocks and no window opens in this session, stop, leave the phase
`in_progress`, and record the outstanding server step.

**Step 2 — Compare against the Phase 0 baseline**

Run at least 60 minutes across real traffic, then:

[server] — `127.0.0.1` is the server's loopback, not yours:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s http://127.0.0.1:8000/api/runtime/loop-health'
```

Record `p99_ms` and `worst_stall_ms` and compare to the Phase 0 baseline. The
expected result is that `worst_stall_ms` drops from seconds to tens of
milliseconds and `stall_count` goes to zero.

If `worst_stall_ms` is still in the seconds, the fix is incomplete: another
blocking call is on the loop. Record the finding, do not start guessing in this
session — that is a Phase 0 census follow-up.

**Step 3 — Confirm management work still executes**

Verify that management batches continue to be planned, claimed, and executed at
the same cadence as before, and that break-even convergence still runs. The
purpose of this phase is that nothing changes except responsiveness.

## Completion criteria

- Both worker loops offload to one shared `max_workers=1` executor.
- The census test finds zero offenders for these two modules.
- The loop-responsiveness regression test passes.
- Production `worst_stall_ms` is materially below the Phase 0 baseline, with
  both numbers recorded.
- Management and break-even behavior is unchanged in production.

## Rollback

No settings flag exists in this phase, so rollback is a redeploy of the previous
known good 40-hex SHA with `-ChangeClass execution_writer` and a fresh
`-PreviousLiveSnapshotPath`. See `deployment-procedure.md`, rollback level 2.

There is no database change and no persisted state, so the revert is complete.
The Phase 0 monitor stays in place and keeps reporting throughout.

## Status file update

Set `phase_status: completed`, `current_phase: 2`,
`phase_name: per-chat-lock-sharding`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-2-per-chat-lock-sharding.md`,
and fill `loop_lag_after_phase1_p99_ms`. Append one `local_tests` and one
`server_verification` entry stating explicitly whether the stall metric improved
and by how much.
