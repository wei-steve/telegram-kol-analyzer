# Runtime Serialization Remediation Status

This is the canonical cross-conversation checkpoint for the runtime
serialization remediation. Chat history must not be used to advance or
reinterpret the rollout. A new session reads this file, then opens only the one
phase file named by `current_phase_file`.

```yaml
project: runtime-serialization-remediation
design_doc: docs/plans/2026-08-18-runtime-serialization-remediation-design.md
index_doc: docs/plans/2026-08-18-runtime-serialization-remediation.md
deployment_doc: docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md
deploy_branch: codex/runtime-serialization-remediation   # NOT the updater default; pass -Branch every time
branch_base: origin/codex/deepcoin-auto-trading-v1 @ 302c1ae   # the branch the server actually fetches
design_version: 1
current_phase: 0
phase_name: loop-health-observability
phase_status: in_progress      # planned | claimed | in_progress | completed
claimed_by: none               # released; tasks 1-2 done, tasks 3-6 remain. # session id or a name you choose; see the claim protocol
current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-0-loop-health-observability.md
last_completed_phase: none
last_completed_commit: none
phase_0_code_commit: 816e296   # tasks 1-2 only; endpoint/census/baseline outstanding
last_deployed_commit: none
production_commit: unknown
baseline_captured: false
phase_0_partial_work_in_tree: false  # committed as 816e296 on 2026-08-18
loop_lag_baseline_p99_ms: null
loop_lag_after_phase1_p99_ms: null
local_tests:
  - "phase-0-partial: commit 816e296 covers Tasks 1 and 2 only (LoopLagMonitor plus lifespan wiring). Written by an earlier session, not independently reviewed. Verified after the fact with .venv (Python 3.12.12) because .venv313b has no bin/python: 11 focused tests pass, tests/test_web_app.py passes 194, and the complete suite passes 5575 with 1 skipped and 17 known deprecation warnings. Task 3 (loop-health endpoint), Task 4 (census allowlist recorded in the status file), Task 5 (suite baseline recorded), and Task 6 (deploy plus 60-minute production baseline) are all still outstanding."
server_verification: []
```

## Claim protocol — read this before starting any phase

**Only one session may work a phase at a time.** On 2026-08-18 two sessions both
read `phase_status: planned` for phase 0, both concluded the phase was theirs,
and both edited the same files in the same checkout. One of them then committed
everything in the tree, including another session's unrelated draft work. This
protocol exists so that cannot repeat.

Before touching any file:

1. Read `phase_status` and `claimed_by` above.
2. If `phase_status` is `claimed` or `in_progress` and `claimed_by` is not you,
   **stop**. Tell the user another session holds this phase; do not start.
3. Otherwise set `phase_status: claimed` and `claimed_by: <your session id>`,
   commit that one-line change immediately, and only then begin the work.
4. Release the claim when the phase completes or when you stop early.

A stale claim from a session that was killed is cleared by the user, not by the
next session helping itself to it. If a claim looks abandoned, ask.

**Never `git add -A` in this repository.** Sessions share one checkout. Stage the
exact paths your phase touched and verify with `git diff --cached --name-only`
before committing.

Parallel sessions should use separate git worktrees. `.worktrees/` already
exists in this repository. Sharing one checkout across sessions is what caused
the 2026-08-18 incident.

## Phase ledger

| Phase | File | Status |
|---|---|---|
| 0 | `phase-0-loop-health-observability.md` | planned |
| 1 | `phase-1-unblock-event-loop.md` | planned |
| 2 | `phase-2-per-chat-lock-sharding.md` | planned |
| 3 | `phase-3-compensation-window-repair.md` | planned |
| 4 | `phase-4-durable-job-shadow-enqueue.md` | planned |
| 5 | `phase-5-queue-consumer-takeover.md` | planned |
| 6 | `phase-6-process-separation.md` | planned |

All phase files live in
`docs/plans/2026-08-18-runtime-serialization-remediation/`, alongside
`deployment-procedure.md`, which every phase uses for its deploy step.

## Phase 0 partial work (resolved, kept for context)

A parallel session began Phase 0 and was stopped part way. Its work is now
committed as `816e296` and the working tree is clean. It contained:

- `src/telegram_kol_research/runtime_loop_health.py` (new, ~142 lines,
  `LoopLagMonitor` implemented)
- `tests/test_runtime_loop_health.py` (new)
- `tests/test_runtime_event_loop_blocking_census.py` (new)
- `src/telegram_kol_research/web_app.py` (modified, +20 lines: imports
  `LoopLagMonitor`, constructs `app.state.loop_lag_monitor`, starts
  `loop_lag_monitor_task` in the lifespan, cancels it on shutdown)

It covers Phase 0 Tasks 1 and 2. Its tests were run after the fact and pass, but
the code has **not been independently reviewed**.

The Phase 0 session must start by reviewing `816e296` against Tasks 1 and 2
rather than assuming it is correct, then continue from Task 3.

`src/telegram_kol_research/bound_close_writer_quiescence.py` is also untracked
but predates this remediation and is unrelated to it — leave it alone.

## Deployment reminder

Code is edited locally, pushed to GitHub, and pulled onto the server by the gated
updater `deploy/telegram-kol-update`, driven from the local machine by
`scripts/server_git_update.ps1` with an exact 40-hex commit. Nobody runs
`git pull` on the server by hand.

There are **no change classes** on this branch. The only required argument is the
commit; schema changes are detected automatically. An earlier draft of these
plans described an older updater with a `CHANGE_CLASS` parameter and a
`deployment-preflight` gate — that updater is not what the server runs. See
`deployment-procedure.md`.

## Why this branch exists

On 2026-08-18 the local `codex/deepcoin-auto-trading-v1` was found to have
diverged from the pushed branch of the same name a week earlier, at `2274d90`:

- pushed branch: 32 commits, simplifying the deployment gate — **this is what the
  server fetches**, and the user confirmed it is the version to keep
- local branch: 118 commits, of which 19 are an abandoned mimo recognition v2
  rebuild; the rest are unrelated work that has not been assessed

Rather than resolve that divergence first, this remediation was branched from the
pushed branch so it can proceed against the code the server actually runs. The
local branch's 118 commits are preserved on GitHub as
`origin/codex/protect-deepcoin-auto-trading-v1-442f6538-20260817` and are a
separate matter.

## How to update this file

At the end of a phase session, update `current_phase`, `phase_name`,
`phase_status`, `current_phase_file`, `last_completed_phase`,
`last_completed_commit`, the ledger row, and append one `local_tests` entry and
one `server_verification` entry describing exactly what was proven and what was
not. Record what remains outstanding rather than rounding it up to done.
