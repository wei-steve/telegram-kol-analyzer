# Phase 1c — Attribute the Stalls

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Added 2026-08-19, after Phase 1b removed the last call the AST census could
> see and production did not improve at all.

**Goal:** Stop guessing which call blocks the event loop. Capture the main
thread's stack at the moment it is blocked, so the next fix targets a known line
instead of a hypothesis.

**Nature:** Observation only. Same shape and same risk as Phase 0. No trading
path is touched, no behavior changes, nothing is fixed.

**Prerequisite:** Phase 1b is complete and deployed (`ee9c0d2`), and its null
result is recorded in `docs/runtime-serialization-remediation-status.md`.

## Why this phase exists

Three rounds of "guess a blocking call, fix it, deploy, measure" have now run:

| Round | Target | Production result |
|---|---|---|
| Phase 1 | both management worker ticks | p95 8311.9 ms -> 12.2 ms. Hit. |
| Phase 1b | the operator maintenance tick | stall rate unchanged to 4 s.f. Miss. |
| ad hoc | `message_operation_supervisor`, 30 s interval | already on `asyncio.to_thread`. Miss. |

`KNOWN_BLOCKING_CALLS` is empty and the loop still stalls **once every 37
seconds, for six to ten seconds**, with a 19.7 s outlier observed. Each further
guess costs a production deployment plus an hour of observation, and teaches
nothing when it is wrong.

The census is static analysis and it has been exhausted. It matches only a
same-module synchronous function named `*_tick` or `*_once` called directly
inside an `async while` loop. Anything else — a different name, a call reached
through an awaited coroutine, blocking work in a request handler or in the
Telethon path, or CPU-bound work with no I/O at all — is invisible to it.

What is missing is not more analysis. It is attribution: what is the loop
actually executing during those six seconds.

## Two facts that constrain the answer

- **The stalls are independent of message traffic.** Production is handling one
  to sixteen raw messages per hour, and the stall rate is a metronome at one per
  37 s across both a 65-minute and a 6-hour observation. Whatever this is, it is
  on a timer, not driven by load.
- **The distribution is bimodal.** p50 is 0.911 ms and p95 is 7.213 ms. The loop
  is healthy the overwhelming majority of the time, then blocked for seconds.

## Scope

Add stall attribution to the existing `LoopLagMonitor`. Report it. Change
nothing else.

### Task 1: A watchdog that captures the loop thread's stack

**Files:**
- Modify: `src/telegram_kol_research/runtime_loop_health.py`
- Create: `tests/test_runtime_stall_attribution.py`

The event loop cannot report on itself while it is blocked — that is the whole
problem, and it is also why the Phase 0 loop-health endpoint times out during a
stall. The observer must therefore be a plain OS thread, not a coroutine.

Shape:

- `LoopLagMonitor.run()` records `threading.get_ident()` once, then calls
  `note_checkin()` on every iteration.
- A daemon thread polls a few times a second. When
  `monotonic() - last_checkin` exceeds the stall threshold, it reads
  `sys._current_frames()[loop_thread_id]` and formats that frame's stack.
- One capture per stall episode, and at most one every
  `stall_log_interval_seconds`, so a continuously stalling loop cannot flood the
  journal.

Everything must be injectable — `monotonic`, the frame provider, the sleeper —
so the tests never sleep and never depend on a real stall.

Tests must assert: no capture while the loop checks in normally; a capture once
the check-in gap crosses the threshold; exactly one capture per episode, not one
per poll; the rate limiter holds across episodes; a missing or unknown thread id
degrades to a recorded reason rather than an exception; and the watchdog thread
is a daemon so it can never hold up shutdown.

[local]

```bash
.venv/bin/python -m pytest tests/test_runtime_stall_attribution.py -v
```

### Task 2: Expose the captures without needing the journal

**Files:**
- Modify: `src/telegram_kol_research/runtime_loop_health.py`
- Modify: `src/telegram_kol_research/web_app.py` only if the endpoint needs it
- Modify: `tests/test_web_app.py`

Add the most recent captures to `snapshot()` under a bounded key, so
`GET /api/runtime/loop-health` returns them directly. Bound the count and the
per-stack size; this payload must stay small.

Keep the journal warning too. The endpoint is unreliable during a stall — Phase
0 recorded that limitation and it is not fixed here — so the log is the durable
copy and the endpoint is the convenient one.

### Task 3: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Record both counts. **Never `git add -A`.** Stage the exact paths.

### Task 4: Deploy and read the answer

Follow `deployment-procedure.md`. There is no change class and no snapshot
argument. `EXPECTED_COMMIT` must be the **current branch tip**, and the script
must run from a checkout of that commit.

```bash
git push origin codex/phase0-deploy-integration:codex/deepcoin-auto-trading-v1
EXPECTED_COMMIT=<current tip> ./scripts/server_git_update.sh
```

Then wait at least one hour and read the captures:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s --max-time 40 http://127.0.0.1:8000/api/runtime/loop-health'
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 "journalctl -u telegram-kol.service --since '-90 min' --no-pager | grep -A 40 'stall stack'"
```

**Record the stacks verbatim in the status file.** They are the deliverable.

Three outcomes, all of them informative:

1. The stacks name a Python frame. That is the answer; assign the fix to a new
   phase and stop.
2. The stacks are shallow or sit in a C call with no useful Python frame — a
   native extension, a GIL-holding C library, or the SQLite driver. Record that,
   because it redirects the whole investigation.
3. No capture fires at all despite `stall_count` climbing. Then the lag is not
   the loop thread being busy — suspect process-level pauses such as GC, swap,
   or CPU starvation from a noisy neighbour. Record that too.

## Completion criteria

- The watchdog runs in production and captures at least one stall stack, or its
  failure to capture is itself recorded with the reason.
- The captures are readable from both the endpoint and the journal.
- The stacks, or the explicit absence of them, are recorded verbatim.
- No trading behavior changed. This phase fixes nothing by design.

## Rollback

Observation only, no settings flag, no schema change. Rollback is a redeploy of
the previous known good tip. The Phase 0 monitor and the Phase 1/1b executor are
untouched.

## Status file update

Set `phase_status: completed`, record the captured stacks verbatim, and state
plainly which of the three outcomes above occurred. Do not propose a fix in this
phase's entry — name the finding and leave the next phase to the user.
