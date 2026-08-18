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
deploy_branch: codex/deepcoin-auto-trading-v1   # matches the updater default; no -Branch needed
integration_branch: codex/phase0-deploy-integration  # merged onto origin/deploy_branch (302c1ae); push this to deploy_branch
local_deploy_branch_is_poisoned: true  # the LOCAL codex/deepcoin-auto-trading-v1 is 118 commits diverged from origin and is checked out in /private/tmp/tg-risk-routing.wmF2Vj. Do not use it. origin/codex/deepcoin-auto-trading-v1 is authoritative.
design_version: 1
current_phase: 0
phase_name: loop-health-observability
phase_status: in_progress      # planned | claimed | in_progress | completed
claimed_by: none               # released 2026-08-18; tasks 1-5 done, task 6 (deploy + baseline) remains
current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-0-loop-health-observability.md
last_completed_phase: none
last_completed_commit: none
phase_0_code_commit: 816e296   # tasks 1-5; reviewed 2026-08-18. Task 6 (deploy + baseline) outstanding
last_deployed_commit: none
production_commit: 302c1ae467b98bc954ac2a25cc4a33a8d09f48f9  # verified read-only over ssh 2026-08-18; branch codex/deepcoin-auto-trading-v1, telegram-kol.service active. Production is exactly the tip of origin/deploy_branch, so deploying the integration branch adds Phase 0 and nothing else.
baseline_captured: false
phase_0_blocking_call_census:   # Task 4 Step 3 — verbatim discovered set, 2026-08-18
  - "strategy_management_worker.run_strategy_management_worker_loop -> run_strategy_management_worker_tick"
  - "break_even_convergence_worker.run_break_even_convergence_worker_loop -> run_break_even_convergence_worker_tick"
  - "system_operator_bot.run_runtime_incident_notification_loop -> run_operator_maintenance_tick"
phase_0_census_third_offender_note: "The third entry is beyond the two the phase file named. src/telegram_kol_research/system_operator_bot.py:2597 calls run_operator_maintenance_tick synchronously inside the `while True` of run_runtime_incident_notification_loop (async def at line 2564). The same iteration also calls load_trading_settings and may build a Deepcoin client via build_deepcoin_client_from_env, so a database read and an exchange HTTP call run on the event loop every 5 seconds. It is recorded, not fixed: Phase 0 is observation only. It is not yet assigned to a remediation phase."
phase_0_local_suite_before: 5562   # tests collected at 816e296^ (0a61dfd)
phase_0_local_suite_after: 5576    # 5575 passed, 1 skipped
phase_0_partial_work_in_tree: false  # committed as 816e296 on 2026-08-18
loop_lag_baseline_p99_ms: null
loop_lag_after_phase1_p99_ms: null
local_tests:
  - "phase-0-partial: commit 816e296 covers Tasks 1 and 2 only (LoopLagMonitor plus lifespan wiring). Written by an earlier session, not independently reviewed. Verified after the fact with .venv (Python 3.12.12) because .venv313b has no bin/python: 11 focused tests pass, tests/test_web_app.py passes 194, and the complete suite passes 5575 with 1 skipped and 17 known deprecation warnings. Task 3 (loop-health endpoint), Task 4 (census allowlist recorded in the status file), Task 5 (suite baseline recorded), and Task 6 (deploy plus 60-minute production baseline) are all still outstanding."
  - "phase-0-review-and-local-completion (2026-08-18, session-04451098): reviewed 816e296 against Tasks 1-3 rather than assuming it. Correction to the entry above: 816e296 in fact also contains Task 3 — GET /api/runtime/loop-health at src/telegram_kol_research/web_app.py:4770 plus three tests in tests/test_web_app.py. Review findings: LoopLagMonitor meets every Task 1 requirement (run/snapshot keys, deque(maxlen=7200) ring buffer, stall_threshold_ms=3000 with one warning per 60s via _last_stall_log_monotonic, injectable monotonic/now_provider/sleeper, no sleeping in tests); the lifespan wiring at web_app.py:3960 and the shutdown block at web_app.py:4201 match the existing contract_spec_refresh_task pattern byte-for-byte; the endpoint is declared async so it never depends on the shared threadpool. No defects found; no code changes were needed. Local runs with .venv (Python 3.12.12): tests/test_runtime_loop_health.py plus tests/test_runtime_event_loop_blocking_census.py 11 passed; tests/test_web_app.py 194 passed; full suite 5575 passed, 1 skipped, 17 known deprecation warnings, 352s. Suite baseline is exact, not approximate: collection at 816e296^ (0a61dfd, run in a throwaway worktree) is 5562 tests; collection at HEAD is 5576; delta 14 equals exactly the 14 tests 816e296 added (11 + 3), and the after run has zero failures. Task 6 (deploy plus 60-minute production baseline) is the only outstanding item and is blocked — see the Phase 0 Task 6 blocker section below."
phase_0_deploy_delta: "302c1ae -> 6620613 is 4074 insertions and 0 deletions across 19 files. Production code touched: runtime_loop_health.py (new, 142 lines) and web_app.py (+36). The rest is docs (3341 lines) and tests (377). No existing line is modified or removed."
phase_0_merged_suite: "5644 passed, 1 skipped, 0 failed on codex/phase0-deploy-integration (385s). Deploy branch alone collects 5631; merged collects 5645; delta 14 equals exactly the tests Phase 0 adds."
server_verification:
  - "phase-0: none. Nothing was pushed and nothing was deployed in this session. Task 6 is blocked on deploy-branch lineage (see the Phase 0 Task 6 blocker section); the updater fast-forwards, and 816e296 is not a descendant of origin/codex/deepcoin-auto-trading-v1 (302c1ae). No production baseline exists, so loop_lag_baseline_p99_ms stays null and baseline_captured stays false."
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
| 0 | `phase-0-loop-health-observability.md` | in_progress — tasks 1-5 done, task 6 blocked |
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

## Phase 0 Task 6 blocker — deploy branch lineage

Task 6 deploys `816e296` with `-ChangeClass code` and then captures a 60-minute
production baseline. It was **not** attempted, for a reason that no amount of
waiting fixes:

- `816e296` is on `codex/mimo-v1-baseline`.
- `deploy_branch` is `codex/deepcoin-auto-trading-v1`, currently `302c1ae` on the
  remote.
- The two diverged at `2274d90`. The deploy branch is **32 commits ahead** of
  that point; the remediation branch carries **4 commits** the deploy branch does
  not have (`72d726b`, `2fc0ad2`, `0a61dfd`, `816e296`).

Step 9 of the updater is `git merge --ff-only <ExpectedCommit>`, so deploying
`816e296` onto `codex/deepcoin-auto-trading-v1` fails: it is not a descendant of
`302c1ae`. This is not a preflight `BLOCK` and it is not a safe-window problem —
the deployment cannot even reach the preflight.

Resolving it means integrating the remediation work onto the deploy branch (merge
or rebase `codex/mimo-v1-baseline` onto `codex/deepcoin-auto-trading-v1`, retest,
push), which is a branch-integration decision affecting 32 commits of unrelated
production work. That is outside an observation-only phase and was left for the
user. Nothing was pushed and nothing was deployed.

Remaining Task 6 steps once the lineage is resolved:

1. Push the integrated commit to `codex/deepcoin-auto-trading-v1`; confirm with
   `git branch -r --contains HEAD`.
2. `powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 -ExpectedCommit <40-hex> -ChangeClass code`
3. Let it run at least 60 minutes across real message traffic, then
   `ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s http://127.0.0.1:8000/api/runtime/loop-health'`
   and record `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `stall_count`,
   `worst_stall_ms` into `loop_lag_baseline_p99_ms` and the ledger, plus the
   journal stall-episode count and worst duration.

Until that is done, `baseline_captured` stays `false`, Phase 0 stays
`in_progress`, and **Phase 1 must not start** — Phase 1's whole purpose is to be
measured against this baseline.

## Phase 0 Task 6 — lineage resolved, deploy still not run

The blocker above is resolved locally. Correcting its framing: the two branches
did **not** entangle 32 commits of unrelated work. `codex/mimo-v1-baseline`
carried nothing but this remediation — 3 planning-doc commits plus Phase 0's
code — and all mimo v2 work predates the `2274d90` fork point, so both branches
already had it. The deploy branch's 32 commits are entirely the deployment-gate
work and are untouched.

`codex/phase0-deploy-integration` is `origin/codex/deepcoin-auto-trading-v1`
(302c1ae) with `codex/mimo-v1-baseline` merged in. **Zero conflicts.**

Verified on the merged branch, not on either branch alone:

- Full suite: **5644 passed, 1 skipped, 0 failed** (385s, `.venv`, Python 3.12.12).
- Exact counts: deploy branch alone collects 5631; merged collects 5645; the
  delta of 14 is exactly the 14 tests Phase 0 adds. No test was lost to the merge.
- Imports were proven to resolve to the merged worktree, not the main checkout,
  before the run was trusted (`pythonpath = ["src"]` wins over the editable
  `.pth`).
- The census re-run on the merged tree still returns exactly the 3 allowlisted
  offenders, so the deploy branch's 32 commits introduced no new blocking call.

**Nothing has been pushed and nothing has been deployed.** Remaining steps:

```bash
git push origin codex/phase0-deploy-integration:codex/deepcoin-auto-trading-v1
git rev-parse codex/phase0-deploy-integration   # 40-hex for -ExpectedCommit
```

Then, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 -ExpectedCommit <40-hex> -ChangeClass code
```

The push is a fast-forward: the merge commit's first parent is 302c1ae, so the
updater's `git merge --ff-only` accepts it. If `deployment-preflight` returns
`BLOCK`, read the reason, wait, and record it — do not retry blindly.

After it runs at least 60 minutes across real message traffic:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s http://127.0.0.1:8000/api/runtime/loop-health'
```

Record `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `stall_count`, `worst_stall_ms`
into `loop_lag_baseline_p99_ms` and `server_verification`, plus the journal
stall-episode count and worst duration. Only then is Phase 0 complete and Phase 1
allowed to start.

## Deployment reminder

Code is edited locally, pushed to GitHub, and pulled onto the server by the gated
updater `deploy/telegram-kol-update`, driven from the local machine by
`scripts/server_git_update.ps1` with an exact 40-hex commit and a change class.
Nobody runs `git pull` on the server by hand. `deploy_branch` above is the branch
the server fetches; set it before the first deployment.

## How to update this file

At the end of a phase session, update `current_phase`, `phase_name`,
`phase_status`, `current_phase_file`, `last_completed_phase`,
`last_completed_commit`, the ledger row, and append one `local_tests` entry and
one `server_verification` entry describing exactly what was proven and what was
not. Record what remains outstanding rather than rounding it up to done.
