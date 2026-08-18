# Phase 0 — Loop Health Observability

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Measure how badly the asyncio event loop is being blocked, and expose
that measurement, before changing any runtime behavior.

**Nature:** Additive and read-only. No trading behavior changes. No lock,
worker, or execution path is modified.

## Why this phase exists

The system intermittently stalls. The suspected cause is that synchronous
blocking calls run directly on the event loop, so Telethon cannot deliver
messages while an exchange HTTP call is in flight. Phase 1 fixes that, but
without a baseline there is no way to prove Phase 1 worked, and no way to find
blocking calls other than the two already identified.

`src/telegram_kol_research/web_app.py:279` already carries the comment
"outside the saturated shared executor", so the shared executor is known to
saturate. This phase turns that suspicion into numbers.

## Before you start: partial work may already exist

A parallel session completed Tasks 1 and 2 and was stopped. That work is
committed as `816e296`:

[local]

```bash
git show --stat 816e296
```

It adds `LoopLagMonitor` in `src/telegram_kol_research/runtime_loop_health.py`,
wires it into the Web lifespan, and adds the census test. Its tests pass — 11
focused, 194 in `tests/test_web_app.py`, and the full suite at 5575 passed with
1 skipped — but the code has **not been independently reviewed**.

Start by reviewing `816e296` against Tasks 1 and 2 below. Do not assume it is
right and do not assume it is wrong. Then continue from **Task 3**; Tasks 3, 4,
5, and 6 are all still outstanding.

`src/telegram_kol_research/bound_close_writer_quiescence.py` is unrelated to this
remediation — leave it alone.

## Scope

Create an event loop lag monitor, wire it into the Web lifespan, expose a
read-only diagnostic endpoint, and capture a production baseline.

Out of scope: fixing anything it finds. Record findings, do not act on them.

### Task 1: Add the loop health monitor module

**Files:**
- Create: `src/telegram_kol_research/runtime_loop_health.py`
- Create: `tests/test_runtime_loop_health.py`

**Step 1 — Write the failing tests first**

The module must provide a `LoopLagMonitor` with:

- `async def run(self) -> None` — loops forever, sleeping a fixed
  `interval_seconds` (default `0.5`) and recording the difference between the
  requested sleep and the observed elapsed time as one lag sample in
  milliseconds.
- `snapshot(self) -> dict[str, Any]` — returns at minimum
  `samples`, `max_ms`, `p50_ms`, `p95_ms`, `p99_ms`, `stall_count`,
  `last_stall_at`, `worst_stall_ms`, `window_seconds`.
- A bounded ring buffer. It must never grow without limit; cap at
  `max_samples` (default `7200`, one hour at 0.5s).
- A stall threshold (`stall_threshold_ms`, default `3000`). Crossing it logs one
  warning containing the observed lag and increments `stall_count`. Logging must
  be rate limited to at most one warning per `stall_log_interval_seconds`
  (default `60`) so a long outage cannot flood the journal.
- An injectable clock so tests are deterministic. Do not sleep in tests.

Tests must cover: empty snapshot is well formed, percentile computation, ring
buffer eviction at the cap, stall counting, and warning rate limiting.

**Step 2 — Implement until the tests pass**

[local]

```bash
.venv/bin/python -m pytest tests/test_runtime_loop_health.py -v
```

Expected: all pass.

### Task 2: Wire the monitor into the Web lifespan

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py` (add cases, do not weaken existing ones)

**Step 1 — Locate the lifespan**

The lifespan starts at `src/telegram_kol_research/web_app.py:3956`. Existing
background tasks follow a consistent shape: assign to `app.state.<name>_task`
via `asyncio.create_task`, then attach
`_log_background_task_result("<name>_task")` as a done callback. Match it
exactly.

**Step 2 — Start the monitor unconditionally**

Add `app.state.loop_lag_monitor` during app construction and start
`app.state.loop_lag_monitor_task` in the lifespan. This monitor is pure
observation, so it does not need a trading settings flag, but it must be
cancellable in shutdown alongside the other tasks. Follow the existing shutdown
handling for background tasks in the same lifespan.

**Step 3 — Verify shutdown cancels it**

Add a test asserting the task is created on startup and cancelled on shutdown.

### Task 3: Expose the read-only diagnostic endpoint

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

Add `GET /api/runtime/loop-health` returning `app.state.loop_lag_monitor.snapshot()`
plus `now` and `uptime_seconds`. It must be read-only, must not touch the
database, and must not call the exchange — it has to stay answerable while the
loop is degraded.

[local]

```bash
.venv/bin/python -m pytest tests/test_web_app.py -v
```

### Task 4: Add the blocking-call census

**Files:**
- Create: `tests/test_runtime_event_loop_blocking_census.py`

**Step 1 — Enumerate async loops that call a synchronous tick directly**

Write a test that parses the `src/telegram_kol_research` tree with `ast` and, for
every `async def` whose body contains a `while` loop, reports direct calls to
module-level synchronous functions whose names end in `_tick` or `_once` that are
not wrapped in `await`, `asyncio.to_thread`, or `run_in_executor`.

**Step 2 — Assert against an explicit allowlist**

Record the current offenders in an explicit `KNOWN_BLOCKING_CALLS` frozenset with
a comment naming the phase that will remove each one. The two already identified
are:

- `strategy_management_worker.run_strategy_management_worker_loop` calling
  `run_strategy_management_worker_tick` (`src/telegram_kol_research/strategy_management_worker.py:923`)
- `break_even_convergence_worker.run_break_even_convergence_worker_loop` calling
  `run_break_even_convergence_worker_tick` (`src/telegram_kol_research/break_even_convergence_worker.py:344`)

The test asserts the discovered set equals the allowlist. It therefore passes
today, fails if a new blocking call is introduced, and must be edited to shrink
the allowlist in Phase 1.

**Step 3 — Record anything else it finds**

If the census discovers offenders beyond the two above, add them to the
allowlist and record them verbatim in the status file. Do not fix them in this
phase.

[local]

```bash
.venv/bin/python -m pytest tests/test_runtime_event_loop_blocking_census.py -v
```

### Task 5: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Expected: no new failures relative to the pre-change baseline. Capture the
before and after counts; do not accept "roughly the same".

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/runtime_loop_health.py \
  src/telegram_kol_research/web_app.py \
  tests/test_runtime_loop_health.py \
  tests/test_web_app.py \
  tests/test_runtime_event_loop_blocking_census.py
git diff --cached --name-only
git commit -m "feat: add event loop lag monitor and blocking-call census"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 6: Deploy and capture the production baseline

Deployment is permitted in this phase because the change is purely additive
observation, but the safe-window rule still applies.

**Step 1 — Deploy**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
There are no change classes on this branch — the only required argument is the
commit, and schema changes are detected automatically.

[local] Commit, push to the branch recorded as `deploy_branch` in the status
file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and `-Branch <deploy_branch>`.

The updater enforces the safe window itself with an active-write check, before
and after it stops the service. Exit code 3 means an exchange write is genuinely
in flight — wait and retry later, do not work around it.

If the preflight blocks and no window opens in this session, stop, leave the
phase `in_progress`, and record the outstanding server step in the status file.

**Step 2 — Capture the baseline**

Let it run at least 60 minutes across a period that includes real message
traffic. Then capture:

[server] — `127.0.0.1` is the server's loopback, not yours:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s http://127.0.0.1:8000/api/runtime/loop-health'
```

Record `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `stall_count`, and
`worst_stall_ms` into the status file as the baseline. Also grep the journal for
the stall warnings and record how many distinct stall episodes occurred and their
worst duration.

## Completion criteria

- The monitor runs in production and the endpoint answers.
- A baseline with at least 60 minutes of real traffic is recorded in the status
  file, including `p99_ms` and `worst_stall_ms`.
- The census test passes with an explicit allowlist naming every current
  offender.
- No trading behavior changed.

## Rollback

No settings flag exists in this phase, so rollback is a redeploy: run
`server_git_update.ps1` with the previous known good 40-hex SHA. See `deployment-procedure.md`, rollback level 2.

The module and endpoint are inert without the lifespan task, and there is no
database migration and no persisted state, so nothing else has to be reversed.

## Status file update

Set `phase_status: completed`, `current_phase: 1`,
`phase_name: unblock-event-loop`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-1-unblock-event-loop.md`,
`baseline_captured: true`, and fill `loop_lag_baseline_p99_ms`. Append one
`local_tests` and one `server_verification` entry.
