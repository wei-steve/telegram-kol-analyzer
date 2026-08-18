# Phase 6 — Process Separation

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Stop web traffic, recognition, and execution from sharing one process
and one event loop, so that load or a fault in one can no longer perturb the
timing of the others.

**Nature:** Deployment topology change. No application logic change.

**Prerequisite:** Phase 5 is complete and `queue` mode has been stable in
production for at least one week. Separating processes before the durable queue
exists would be unsafe — without it, two processes cannot coordinate who
processes a message.

## Why this phase exists

Everything runs in one process today. `telegram-kol.service` runs the Web app,
and the Web app's lifespan (`src/telegram_kol_research/web_app.py:3956`) starts
the Telegram listener, periodic reconcile, Deepcoin reconcile, strategy management
worker, break-even convergence worker, source message deletion worker, lifecycle
monitor, semantic review, contract spec refresh, message operation supervisor,
Telegram bot command loops, and notification loops — all as tasks on the same
event loop as every HTTP request handler.

Consequences that remain even after phases 1 through 5:

- Opening a workbench page competes with execution work for the same loop and the
  same thread pool.
- Any unhandled fault that kills the process takes down message intake, execution,
  and the UI together.
- Deploying a UI change requires restarting the trading path.

The project already demonstrates the better pattern: `telegram-kol-monitor` and
`telegram-kol-runtime-agent` run as separate hardened units under separate users
(`deploy/systemd/`). The main service is the remaining monolith.

## Scope

Split into three units sharing one database. Do not change application logic.

| Unit | Responsibility |
|---|---|
| `telegram-kol-ingest` | Telethon listener, media download, persist, enqueue, reconcile |
| `telegram-kol-worker` | Message processing worker, management worker, break-even worker, deletion worker, lifecycle monitor |
| `telegram-kol-web` | HTTP, SSE, workbench, read-only projections |

### Task 1: Establish the multi-process safety preconditions

**Do this before any split. If any check fails, stop and record it.**

**Step 1 — SQLite concurrency**

`src/telegram_kol_research/db.py:727` sets `journal_mode=WAL` and
`busy_timeout=30000`. WAL permits one writer and many readers across processes.
Three processes writing concurrently is a genuine change in contention profile.

Verify: enumerate the write paths in each proposed unit, estimate write frequency,
and confirm the 30 second busy timeout is adequate. Run a load test with three
concurrent writer processes against a copy of the production database and record
the observed `SQLITE_BUSY` rate.

**Decision gate:** if the busy rate is non-trivial, do not proceed with a three-way
split in this phase. A two-way split with all writers in one process, or moving to
a server database, is the correct alternative and needs its own phase. Record the
finding and stop.

**Step 2 — In-process locks that become meaningless across processes**

`position_authority_lock` (`src/telegram_kol_research/position_authority_lock.py`)
is a `threading.RLock`. Its own docstring says "in this service process". Across
processes it provides no exclusion at all.

Verify every path it guards ends up in exactly **one** unit. Under the proposed
split all exchange mutation lives in `telegram-kol-worker`, which satisfies this —
but it must be asserted, not assumed. The manual action endpoints in the Web app
are the likely violation: any HTTP endpoint that mutates a position would move the
mutation into the web process and break the boundary silently.

**Files:**
- Create: `tests/test_process_boundary_authority.py`

Write an architecture test enumerating every function reachable from a Web route
that calls a Deepcoin write method or a `position_authority_lock`-guarded path,
and assert the set is empty or explicitly allowlisted with a documented reason.

**Decision gate:** if a Web route mutates positions, either move it behind a job
enqueued for the worker, or do not split until it is moved.

**Step 3 — The Telegram session lock**

`src/telegram_kol_research/telegram_session_lock.py` exists because the Telethon
session file cannot be used by two processes. Confirm only `telegram-kol-ingest`
opens the session, and that the lock's owner-pid diagnostics
(`web_app.py:274`, `SESSION_LOCK_OWNER_PID_PATTERN`) still report usefully when
the holder is a different unit.

### Task 2: Add a run-mode selector

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_runtime_role_selection.py`

Add a role selector — CLI flag or environment variable — controlling which
background tasks the lifespan starts: `all` (current behavior, default), `ingest`,
`worker`, `web`.

`all` must remain the default and must start exactly the set of tasks it starts
today. Assert that with a test comparing the task set before and after, so the
single-process deployment is provably unchanged.

Each role starts a strict subset. Assert that the union of `ingest`, `worker`, and
`web` equals `all` with no task started twice — a task started in two roles means
duplicate execution in production.

### Task 3: Add the systemd units

**Files:**
- Create: `deploy/systemd/telegram-kol-ingest.service`
- Create: `deploy/systemd/telegram-kol-worker.service`
- Create: `deploy/systemd/telegram-kol-web.service`
- Modify: `docs/server-deployment.md`, `docs/runbook.md`

Model the hardening on the existing units. `deploy/systemd/telegram-kol-runtime-agent.service`
is the reference for the strict pattern already proven on this server.

Constraints:

- Only `telegram-kol-ingest` gets access to the Telegram session file. The other
  two must have it in `InaccessiblePaths`.
- Only `telegram-kol-worker` gets Deepcoin write credentials. `telegram-kol-web`
  gets read-only credentials or none.
- All three need read-write access to the database directory, including the `-wal`
  and `-shm` files. Getting this wrong produces confusing read-only errors — the
  monitor unit's `BindReadOnlyPaths` block shows the WAL files that must be listed.
- Do not enable the new units in the same step that installs them.

### Task 4: Teach the updater about the new topology

**This is the task that makes Phase 6 possible at all. Do not skip it, and do not
reorder it after the split.**

**Files:**
- Modify: `deploy/telegram-kol-update`
- Modify: `scripts/server_git_update.ps1`
- Modify: `tests/test_server_update_scripts.py`

The gated updater hardcodes the single service in three places:

- `systemctl is-active --quiet telegram-kol.service` as a precondition, which
  **fails outright** once that unit is gone (`deploy/telegram-kol-update:48`)
- `systemctl stop telegram-kol.service` before the checkout moves
- `systemctl start telegram-kol.service` plus an active assertion afterwards

It also reinstalls itself from the commit being deployed
(`install -o root -g root -m 0755 deploy/telegram-kol-update /usr/local/bin/telegram-kol-update`).

Two consequences:

1. The updater must learn the three-unit topology **while the old topology is
   still running**, in an ordinary deployment. It cannot be taught afterwards,
   because after the split the very next deployment would fail its own
   precondition.
2. Because it self-installs, the topology-aware updater becomes the installed
   updater as soon as that deployment succeeds. So it must handle **both**
   topologies and detect which one is present, rather than switching outright.

**Step 1 — Make it topology-aware, not topology-switched**

Have it resolve the managed unit set at run time: if `telegram-kol.service` is
active, manage that single unit exactly as today; if the three new units are
installed and active, manage those instead. If neither or both are active, exit
non-zero with a clear message rather than guessing.

**Step 2 — Order the stop and start**

For the three-unit set, stop `ingest` first and start it last, so intake is the
first thing to quiesce and the last to resume. `worker` and `web` stop after and
start before it.

**Step 3 — Keep every existing gate intact**

The preflight gates, the flock, the fast-forward assertion, the change-class
rules, and the failure trap that restores the previous commit must all behave
identically. This task changes which units are cycled, nothing else.

**Step 4 — Deploy the topology-aware updater by itself, first**

Deploy this change alone, as `-ChangeClass code`, while the single unit is still
running. Confirm it deploys successfully and that the installed
`/usr/local/bin/telegram-kol-update` is the new version. Only then continue.

This is the one deployment in the whole remediation that must be verified twice:
once that it worked, and once that the **next** deployment still works after it.
Run a trivial follow-up deployment to prove the updater did not break itself.

### Task 5: Update the deployment helper

**Files:**
- Modify: `scripts/server_git_update.ps1`
- Modify: `deploy/telegram-kol-update`
- Modify: `tests/test_server_update_scripts.py`

Restart order matters: `worker` and `web` first, `ingest` last, so intake is the
last thing to drop and the last to come back. Stop order is the reverse.

The existing tests for these scripts must be extended, not replaced.

### Task 6: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Default role `all` means the entire existing suite must pass unchanged.

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/cli.py \
  deploy/systemd/ \
  deploy/telegram-kol-update \
  scripts/server_git_update.ps1 \
  tests/test_runtime_role_selection.py \
  tests/test_process_boundary_authority.py \
  tests/test_server_update_scripts.py
git diff --cached --name-only
git commit -m "feat: add runtime role selection and split systemd units"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 7: Deploy without splitting, then split

**Step 1 — Deploy with role `all`**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
This phase deploys with `-ChangeClass code`.

[local] Commit, push to the deploy branch recorded as `deploy_branch` in the
status file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and the change class above.

The updater enforces the safe window itself through `deployment-preflight`
before it stops the service. If it returns `BLOCK`, read the reason, wait, and
record it — do not retry blindly.

Confirm behavior is identical. The role selector is live but selecting the current
topology. Let it run one full trading session before splitting.

**Step 2 — Install the new units disabled**

Install all three units without enabling them. Confirm `telegram-kol.service` is
still the only thing running.

**Step 3 — Split during a proven safe window**

The split itself is **not** a `server_git_update.ps1` run. The code is already
deployed by then; this step only changes which units are enabled. It is a manual,
out-of-band maintenance action performed over ssh.

There is no in-place transition. The sequence is: stop and disable
`telegram-kol.service`, enable and start `telegram-kol-worker`, then
`telegram-kol-web`, then `telegram-kol-ingest` last.

Requires a genuine maintenance window with no active time-sensitive strategy
operation and no in-flight management batch. If one cannot be proven, stop with the
units installed and disabled, and record the outstanding step. There is no partial
version of this step.

**Step 4 — Verify after the split**

- All three units active; `telegram-kol.service` stopped and disabled.
- Only the ingest unit holds the Telegram session lock.
- Messages arrive, are enqueued, and are processed by the worker unit.
- Management batches execute from the worker unit.
- The workbench loads and SSE pushes from the web unit.
- Loop lag from the Phase 0 endpoint is healthy in every unit.
- `SQLITE_BUSY` errors are absent from all three journals.
- The production safety monitor and runtime agent still function — they reference
  the old topology in places and their expectations may need updating.

**Step 5 — Verify the isolation actually holds**

The claim of this phase is isolation, so test it: put sustained load on the web
unit and confirm execution timing in the worker unit is unaffected. Without this
the phase is unproven.

### Task 8: Restore the monitor's expectations

**Files:**
- Modify: `deploy/systemd/telegram-kol-monitor.service` and related config
- Modify: `src/telegram_kol_research/production_safety_monitor.py` as required

The monitor checks expected head, auto-trade enablement, and management mode
against a single service. Update it to understand three units, so a single dead
unit is detected rather than silently tolerated. A monitor that only watches one of
three processes is worse than no monitor, because it reports healthy while intake
is down.

## Completion criteria

- All three Task 1 decision gates passed, or the phase was deliberately stopped
  with the finding recorded.
- Role `all` proven behaviorally identical for one full trading session before the
  split.
- Three units running, each holding only the credentials and files it needs.
- Web load demonstrably does not perturb worker execution timing.
- The production safety monitor understands the new topology.

## Rollback

Stop the three units, start `telegram-kol.service`. The code supports role `all`
in the same build, so rollback needs no redeploy and no code change. Keep
`telegram-kol.service` installed but disabled until the split has been stable for
at least two weeks.

## Status file update

Set `phase_status: completed`, `current_phase: done`,
`phase_name: remediation-complete`. Record the split time, the `SQLITE_BUSY`
observations, the isolation test result, and any Task 1 gate that was waived and
why.

## After this phase

The runtime layer is now sound. The 39 repair, recovery, and reconciliation modules
identified in the design document were written to compensate for the defects fixed
in phases 1 through 5. Retiring the ones that are now redundant is worthwhile, but
it is separate work: each retirement needs evidence that the condition it repairs
no longer occurs. Do not begin it as part of this remediation.
