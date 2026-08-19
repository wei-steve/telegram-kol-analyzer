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
current_phase: 1.5
phase_name: unblock-operator-maintenance
phase_status: completed        # planned | claimed | in_progress | completed
claimed_by: none               # phase 1b complete and deployed; NOTHING is claimed
current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-1b-unblock-operator-maintenance.md
last_completed_phase: 1
last_completed_commit: fd748d7aa7bf14acdf6c83d81fa137d1cdbab672
phase_0_code_commit: 816e296   # tasks 1-5; reviewed 2026-08-18. Task 6 (deploy + baseline) outstanding
last_deployed_commit: ee9c0d26041ef8b3e251fc392a79b5c586e76943
production_commit: ee9c0d26041ef8b3e251fc392a79b5c586e76943  # deployed 2026-08-19 03:22 UTC; was fd748d7 before phase 1b
production_commit_before_phase_0: 302c1ae467b98bc954ac2a25cc4a33a8d09f48f9  # rollback target
production_commit_before_phase_1: a00561bf7683091ae0a48471cbfc2af1e6b9fa8c  # phase 1 rollback target
phase_1_code_commit: fd748d7aa7bf14acdf6c83d81fa137d1cdbab672
phase_1_local_suite_before: 5644   # passed, 1 skipped, on the merged branch at e61dbf1
phase_1_local_suite_after: 5661    # passed, 1 skipped, 0 failed; delta 17 equals the 17 tests phase 1 adds
phase_1_worst_stall_criterion_met: false   # see the phase 1 unmet-criterion section below
phase_1b_code_commit: 06f916c5be8963b30dd87bc596c269c73dc641f7   # the code; deployed inside ee9c0d2
phase_1b_deployed_commit: ee9c0d26041ef8b3e251fc392a79b5c586e76943   # branch tip at deploy time = 06f916c plus two docs commits
phase_1b_local_suite_before: 5661   # passed, 1 skipped, at fd748d7
phase_1b_local_suite_after: 5664    # passed, 1 skipped, 0 failed; delta 3 equals the 3 tests added
deploy_branch_ahead_of_production: false  # production is ee9c0d2, the branch tip, as of 2026-08-19 03:22 UTC
loop_lag_after_phase1b_p99_ms: 7004.713   # 64.8 min, 2026-08-19 04:27 UTC. WORSE than phase 1's 6759.363
phase_1b_worst_stall_ms: 19687.274        # phase 1 was 15356.616. Worse, not better
phase_1b_stall_rate_unchanged: true       # 1 per 37.37 s vs phase 1's 1 per 37.36 s — identical
phase_1b_production_effect: none          # the code is correct; it changed nothing measurable
event_loop_still_blocked: true            # PHASE 2 REMAINS BLOCKED BY ITS OWN PREREQUISITE
next_step_needs_user_decision: true       # widening the blocking-call census has no phase and no owner
production_commit_before_phase_1b: fd748d7aa7bf14acdf6c83d81fa137d1cdbab672  # phase 1b rollback target
baseline_captured: true   # 64.9 minutes of real traffic, 2026-08-18 17:16 UTC
phase_0_blocking_call_census:   # Task 4 Step 3 — verbatim discovered set, 2026-08-18
  - "strategy_management_worker.run_strategy_management_worker_loop -> run_strategy_management_worker_tick"
  - "break_even_convergence_worker.run_break_even_convergence_worker_loop -> run_break_even_convergence_worker_tick"
  - "system_operator_bot.run_runtime_incident_notification_loop -> run_operator_maintenance_tick"
phase_0_census_third_offender_note: "The third entry is beyond the two the phase file named. src/telegram_kol_research/system_operator_bot.py:2597 calls run_operator_maintenance_tick synchronously inside the `while True` of run_runtime_incident_notification_loop (async def at line 2564). The same iteration also calls load_trading_settings and may build a Deepcoin client via build_deepcoin_client_from_env, so a database read and an exchange HTTP call run on the event loop every 5 seconds. It is recorded, not fixed: Phase 0 is observation only. It is not yet assigned to a remediation phase. PHASE 1 UPDATE: this is now the prime suspect for the stalls that survived Phase 1 and it needs an owning phase. The census allowlist in tests/test_runtime_event_loop_blocking_census.py is down to this one entry."
phase_0_local_suite_before: 5562   # tests collected at 816e296^ (0a61dfd)
phase_0_local_suite_after: 5576    # 5575 passed, 1 skipped
phase_0_partial_work_in_tree: false  # committed as 816e296 on 2026-08-18
loop_lag_baseline_p99_ms: 8777.887
loop_lag_after_phase1_p99_ms: 6765.435   # 63.4 min, 2026-08-18 19:56 UTC; baseline was 8777.887
local_tests:
  - "phase-0-partial: commit 816e296 covers Tasks 1 and 2 only (LoopLagMonitor plus lifespan wiring). Written by an earlier session, not independently reviewed. Verified after the fact with .venv (Python 3.12.12) because .venv313b has no bin/python: 11 focused tests pass, tests/test_web_app.py passes 194, and the complete suite passes 5575 with 1 skipped and 17 known deprecation warnings. Task 3 (loop-health endpoint), Task 4 (census allowlist recorded in the status file), Task 5 (suite baseline recorded), and Task 6 (deploy plus 60-minute production baseline) are all still outstanding."
  - "phase-0-review-and-local-completion (2026-08-18, session-04451098): reviewed 816e296 against Tasks 1-3 rather than assuming it. Correction to the entry above: 816e296 in fact also contains Task 3 — GET /api/runtime/loop-health at src/telegram_kol_research/web_app.py:4770 plus three tests in tests/test_web_app.py. Review findings: LoopLagMonitor meets every Task 1 requirement (run/snapshot keys, deque(maxlen=7200) ring buffer, stall_threshold_ms=3000 with one warning per 60s via _last_stall_log_monotonic, injectable monotonic/now_provider/sleeper, no sleeping in tests); the lifespan wiring at web_app.py:3960 and the shutdown block at web_app.py:4201 match the existing contract_spec_refresh_task pattern byte-for-byte; the endpoint is declared async so it never depends on the shared threadpool. No defects found; no code changes were needed. Local runs with .venv (Python 3.12.12): tests/test_runtime_loop_health.py plus tests/test_runtime_event_loop_blocking_census.py 11 passed; tests/test_web_app.py 194 passed; full suite 5575 passed, 1 skipped, 17 known deprecation warnings, 352s. Suite baseline is exact, not approximate: collection at 816e296^ (0a61dfd, run in a throwaway worktree) is 5562 tests; collection at HEAD is 5576; delta 14 equals exactly the 14 tests 816e296 added (11 + 3), and the after run has zero failures. Task 6 (deploy plus 60-minute production baseline) was outstanding at the time of this entry; it was completed later the same day — see the server_verification entries and the 'how it was actually deployed' section."
  - "phase-1 (2026-08-18, session-45794fed): all seven local tasks done. New src/telegram_kol_research/runtime_worker_executor.py owns one lazily created ThreadPoolExecutor(max_workers=1, thread_name_prefix='mgmt-worker') with get_management_worker_executor, shutdown_management_worker_executor(wait) and run_on_management_worker. Both loops now submit to it: the strategy management loop submits _load_settings_and_run_strategy_management_tick so load_trading_settings and the tick stay atomic on one thread, and the break-even loop submits its tick directly. Both gained an explicit `except asyncio.CancelledError: raise` ahead of the broad except. The census allowlist lost both entries and now holds only the system_operator_bot offender. web_app.py calls shutdown_management_worker_executor(wait=False) in the lifespan after both worker tasks are cancelled; wait=False is deliberate so shutdown cannot hang, and an in-flight tick still finishes on its own thread exactly as it did when it ran on the loop. 17 tests added: 8 executor, 3 strategy-loop (cursor lane alternation still executable/recovery/executable/recovery on one cursor object, tick runs off the loop thread, loop survives a raising tick), 1 break-even (both loops observably share one thread with zero overlap - the guard against someone later splitting the pools), 3 responsiveness including one that reproduces the pre-Phase-1 shape and proves the guard fails on it, 2 web_app shutdown. Full suite with .venv (Python 3.12.12): 5661 passed, 1 skipped, 0 failed, 403s. Before was 5644 passed, 1 skipped; delta 17 equals exactly the 17 tests added. Import resolution was confirmed to point at the worktree, not the main checkout, before the run was trusted."
phase_0_deploy_delta: "302c1ae -> 6620613 is 4074 insertions and 0 deletions across 19 files. Production code touched: runtime_loop_health.py (new, 142 lines) and web_app.py (+36). The rest is docs (3341 lines) and tests (377). No existing line is modified or removed."
phase_0_merged_suite: "5644 passed, 1 skipped, 0 failed on codex/phase0-deploy-integration (385s). Deploy branch alone collects 5631; merged collects 5645; delta 14 equals exactly the tests Phase 0 adds."
server_verification:
  - "phase-0-deploy (2026-08-18 16:12 UTC): DEPLOYED. Pushed codex/phase0-deploy-integration to codex/deepcoin-auto-trading-v1 as a fast-forward (302c1ae..a00561b, no force). Ran scripts/server_git_update.sh with EXPECTED_COMMIT=a00561bf7683091ae0a48471cbfc2af1e6b9fa8c CHANGE_CLASS=code; exit 0. Verified over ssh: HEAD=a00561b on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-19 00:12:02 CST, GET /api/runtime/loop-health answers."
  - "phase-0-deploy-first-attempt-failed-safely: the first run was issued from the main checkout, which sits on codex/mimo-v1-baseline, so its deploy/telegram-kol-update hashed to e70aa550 while the server extracted a7e30187 from a00561b. The updater SHA256 guard refused and exited silently with production untouched at 302c1ae. Run the script from a checkout of the commit being deployed. The guard worked as designed."
  - "phase-0-baseline: CAPTURED 2026-08-18 17:16:57 UTC at uptime 3893 s, i.e. 64.9 minutes of real message traffic with no restart in between. samples 1886, window_seconds 3892.504, p50_ms 1.076, p95_ms 8311.911, p99_ms 8777.887, max_ms 15160.203, stall_count 365, worst_stall_ms 15160.203, last_stall_at 2026-08-18T17:16:53Z."
  - "phase-0-baseline-journal: 61 'event loop stalled' warnings over the same window. That 61 is NOT the episode count. Logging is rate limited to one warning per 60 s, and 61 warnings across 64.9 minutes is exactly that limiter saturating, so the journal proves stalls were near continuous rather than counting them. stall_count 365 is the real episode count: one stall of 3 s or more every 10.7 s. Logged durations alternate around 6.1 s and 8.0 s at a ~62 s cadence; treat that alternation as a hypothesis about two recurring blockers, not a finding, since the limiter samples only the first stall per window."
  - "phase-0-baseline-derived: the worst number is not a field in the payload. At interval 0.5 s an unblocked loop would have produced about 7785 samples in 3892.5 s; it produced 1886. Requested sleep totals 942.5 s, so 2950.0 s of the 3892.5 s window was lag: the event loop was unavailable roughly 76 percent of wall clock. The distribution is bimodal, not uniformly degraded, since p50 is 1.076 ms. That shape is the signature of discrete blocking calls rather than general overload, consistent with the census, and is what Phase 1 must move off the loop. Phase 1 is measured against loop_lag_baseline_p99_ms = 8777.887."
  - "phase-0-endpoint-limitation: GET /api/runtime/loop-health does NOT satisfy the phase file requirement that it stay answerable while the loop is degraded. It is declared async, so it runs on the loop it measures and is unavailable for as long as the loop is blocked. Observed directly: a capture using curl --max-time 10 failed against a 15 s stall. Declaring it async was deliberate and does avoid the saturated shared executor, but the tradeoff was never stated. Workaround in use: retry with --max-time 40. Recorded, not fixed, because Phase 0 is observation only. Anyone querying this endpoint during an incident must expect it to time out."
  - "phase-0-first-reading-after-deploy: superseded by the baseline above, kept for the record. At uptime 123 s, 2 minutes after the restart: samples 67, p50_ms 1.161, p95_ms 8473.065, p99_ms 15160.203, max_ms 15160.203, stall_count 10, worst_stall_ms 15160.203, window 122.885 s. Not usable as a baseline because it covers only service startup, which loads positions and contract specs and syncs the exchange."
  - "phase-1-deploy (2026-08-18 18:52 UTC): DEPLOYED. Pushed fd748d7 to codex/deepcoin-auto-trading-v1 as a fast-forward (e61dbf1..fd748d7, no force), then ran EXPECTED_COMMIT=fd748d7aa7bf14acdf6c83d81fa137d1cdbab672 ./scripts/server_git_update.sh from the worktree checked out at that commit, so the updater SHA256 guard matched. Verified over ssh afterwards: HEAD=fd748d7 on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-19 02:52:47 CST, /api/trading-settings returns 200. The curl connection refusals visible near the end of the updater output are its own verify_http_health retry loop during service startup; it succeeded, and no rollback fired."
  - "phase-1-deploy-procedure-correction: the phase file and deployment-procedure.md both specify -ChangeClass execution_writer plus -PreviousLiveSnapshotPath. NEITHER EXISTS in the updater on this branch. deploy/telegram-kol-update takes only EXPECTED_COMMIT and BRANCH, scripts/server_git_update.sh exports only SERVER/KEY_PATH/BRANCH/EXPECTED_COMMIT, and scripts/server_git_update.ps1 has no -ChangeClass parameter at all. Phase 0s recorded CHANGE_CLASS=code was therefore an inert environment variable, not a gate selection. The real safe-window gate is telegram_kol_research.deployment_active_write_check, which the updater runs itself immediately before and immediately after stopping the service and which exits 3 on an active exchange write; schema handling is auto-detected by diffing models.py, db.py and migrations rather than selected by a class. A prior live position snapshot was captured by hand before the deploy as evidence (14847 bytes, captured_at 2026-08-18T16:30:22Z) even though the updater has no argument to consume it. Phases 2, 3 and 5 name the same nonexistent arguments and must be corrected before they are executed."
  - "phase-1-baseline-reconfirmed-pre-deploy: read at 2026-08-18 18:51 UTC, minutes before the deploy, over a 9544.0 s window (2.65 h): samples 4757, p50_ms 0.953, p95_ms 8422.49, p99_ms 8922.257, max_ms 16667.007, stall_count 895, worst_stall_ms 16667.007. Derived loop unavailability 75.1 percent. This independently reconfirms the Phase 0 baseline over a window 2.5x longer, so the before/after comparison does not rest on a single reading."
  - "phase-1-after (2026-08-18 19:56 UTC, uptime 3803 s = 63.4 min of real traffic, no restart in the window): samples 5975, window_seconds 3802.336, p50_ms 0.919, p95_ms 12.183, p99_ms 6765.435, max_ms 15356.616, stall_count 103, worst_stall_ms 15356.616, last_stall_at 2026-08-18T19:56:12Z. Against the Phase 0 baseline: p95 8311.911 -> 12.183 ms, a 682x drop and the single largest change; p99 8777.887 -> 6765.435 ms, down 22.9 percent; stall rate one per 10.7 s -> one per 36.9 s, a 3.45x drop; derived loop unavailability ~76 percent -> 21.4 percent (requested sleep 2987.5 s of a 3802.3 s window leaves 814.8 s of lag). The loop is now healthy the large majority of the time instead of the small minority."
  - "phase-1-unmet-criterion: worst_stall_ms did NOT improve. 15356.616 ms after versus 15160.203 ms at baseline - marginally worse, and nowhere near the tens of milliseconds the phase file predicted; stall_count did not reach zero either. The phase file anticipated exactly this and prescribed the response: another blocking call is still on the loop, record it and do not guess further in this session. Journal evidence: stalls of 6975.6 ms and 6197.1 ms were logged at 02:53:01 and 02:54:16 CST, both after the 02:52:47 restart. The prime suspect is the one remaining census entry, system_operator_bot.run_runtime_incident_notification_loop, which calls run_operator_maintenance_tick synchronously every 5 s with a database read and a Deepcoin client build. Ruled out while gathering evidence: lifecycle_monitor candle fetches, which look alarming in the journal but run in an async def (_fetch_candles_full) and are ordinary Gate.io network timeouts predating the deploy. NOT fixed here - it is outside Phase 1 scope and has no owning phase yet."
  - "phase-1-behavior-unchanged: partially proven, and the gap is stated rather than rounded up. strategy_management_batches shows 1 batch touched (status reconciling) in the 63 min after the restart versus 0 rows in the comparable 63 min before it, so management work does still execute and did not regress. Break-even convergence could NOT be positively verified: strategy_break_even_convergences holds 2 rows whose last update is 2026-08-03, so there was no convergence work to do in either window. The loop is running and its tick raised nothing - zero 'worker tick failed' entries in the journal since the restart - but an idle path is not a proven path. First real convergence after this deploy should be checked."

  - "phase-1b-local (2026-08-18, session-45794fed): tasks 1-4 done. system_operator_bot._run_operator_maintenance_cycle now holds the settings read, the Deepcoin client construction, run_operator_maintenance_tick, and the client close as one unit, submitted to Phase 1s EXISTING shared max_workers=1 executor via run_on_management_worker. A separate pool was considered and rejected: all three ticks were mutually exclusive before Phase 1 only because all three ran on the loop, and a separate pool would introduce three-way concurrency on paths touching execution contracts, entry admissions and the exchange - the exact change Phase 1s design constraint exists to prevent. KNOWN_BLOCKING_CALLS is now EMPTY and the census passes with zero discovered offenders. One accepted divergence, recorded not hidden: close() now runs inside the submitted unit, so an exception it raises is swallowed by the loops pre-existing except Exception instead of escaping the while loop and killing the task; the alternative closes an httpx.Client back on the event loop. 3 tests added (operator loop responsiveness, operator and management ticks observably sharing one mgmt-worker thread with zero overlap, and client build/use/close all on the same worker thread in that order). Full suite 5664 passed, 1 skipped, 0 failed, 405s; before was 5661, delta 3 equals exactly the tests added."
  - "phase-1b-deploy-was-blocked-then-SUPERSEDED (2026-08-18): kept for the record only. The deploy was refused by the local agent permission layer at that time and production stayed at fd748d7. This was resolved on 2026-08-19 - see the phase-1b-deploy entry below. Do not read this entry as current state."

  - "phase-1b-deploy (2026-08-19 03:22 UTC): DEPLOYED. Ran EXPECTED_COMMIT=ee9c0d26041ef8b3e251fc392a79b5c586e76943 ./scripts/server_git_update.sh from the worktree checked out at that commit; updater exit code 0. Verified over ssh: HEAD=ee9c0d2 on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-19 11:22:53 CST, /api/trading-settings returns 200. ee9c0d2 is 06f916c (the Phase 1b code) plus two docs-only commits. TWO LESSONS. First, the initial attempt passed EXPECTED_COMMIT=06f916c and failed with exit 1 at the bootstrap FETCH_HEAD assertion, because two further commits had been pushed to the branch since: the updater compares FETCH_HEAD against EXPECTED_COMMIT, so the value must be the CURRENT BRANCH TIP, not the commit you care about. Production was untouched. Second, correcting the Phase 1 record: that deploy exit code was reported as 0 but was captured through a pipe, so it was tails exit code, not the updaters. Phase 1 did deploy successfully - HEAD, service state and endpoint were all verified independently - but the exit code cited for it was not evidence. This run captured the exit code without a pipe."
  - "phase-1b-first-reading-after-deploy: NOT a baseline, recorded so it is not mistaken for one. At uptime 152 s: samples 227, p50_ms 1.053, p95_ms 117.865, p99_ms 7469.818, max_ms 10067.971, stall_count 4, worst_stall_ms 10067.971, window 150.5 s. This covers service startup, which loads positions and contract specs and syncs the exchange, and Phase 0 already recorded that the startup window is unusable for comparison."

  - "phase-1b-after (2026-08-19 04:27 UTC, uptime 3886 s = 64.8 min of real traffic, no restart in the window): samples 6030, window_seconds 3884.374, p50_ms 0.911, p95_ms 7.213, p99_ms 7004.713, max_ms 19687.274, stall_count 104, worst_stall_ms 19687.274. THE CHANGE HAD NO EFFECT. Stall rate is the decisive number and it is unchanged to four significant figures: 104 stalls over 3886.1 s of uptime is one per 37.37 s, against Phase 1s 589 over 22006.0 s which is one per 37.36 s. p99 went from 6759.363 to 7004.713 (worse), worst_stall_ms from 15356.616 to 19687.274 (worse), derived loop unavailability from 21.2 to 22.4 percent (worse), p95 from 8.491 to 7.213 (better). Every one of those deltas is within run-to-run noise; the stall rate being identical is not noise, it is the signal. Note when reading the raw payload: stall_count is a cumulative counter over uptime while samples and window_seconds are limited by the 7200-sample ring buffer, so rates must be computed against uptime_seconds, not window_seconds - Phase 1s buffer was saturated at a 22006 s uptime."
  - "phase-1b-conclusion: the operator maintenance tick was NOT the cause of the stalls. That is a real finding, not a failure of the change. run_operator_maintenance_tick genuinely ran on the event loop every 5 seconds with a database read and a Deepcoin client, it genuinely no longer does, the census is genuinely empty, and production is genuinely no better. The AST census has therefore exhausted what it can see: KNOWN_BLOCKING_CALLS is empty and multi-second stalls continue at one per 37 seconds. Whatever is blocking the loop is invisible to a census that looks only for same-module sync functions named *_tick or *_once called inside an async while-loop. Journal evidence over the window: 50 rate-limited warnings (the limiter saturating again, so near continuous), logged durations clustering between 6.1 s and 10.7 s with a single 19.7 s outlier. Per the phase file this is recorded and NOT pursued: widening the census is its own task and has no owning phase."
  - "phase-1b-behavior-unchanged: strategy_management_batches shows 2 batches touched and message_instruction_items 2 rows touched in the 65 min after the restart, and there were zero 'worker tick failed' entries. The operator maintenance path reaches entry-admission and instruction-execution reconciliation, and message_instruction_items moving is consistent with it still running. runtime_incidents saw no new rows, so the notification delivery path was idle and is unproven, same gap as break-even convergence in Phase 1."
  - "phase-1b-keep-do-not-revert: Phase 1b stays deployed. It is correct code that removes a real database read and a real exchange client construction from the event loop on a 5-second interval, and it keeps the census at zero so the next regression is caught. Its null production result means that call was not the bottleneck, not that the change is wrong. Rollback target if ever needed is fd748d7."

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
| 0 | `phase-0-loop-health-observability.md` | **completed** 2026-08-18, deployed a00561b, baseline captured |
| 1 | `phase-1-unblock-event-loop.md` | **completed** 2026-08-18, deployed fd748d7, p95 8312 -> 12 ms; one criterion unmet |
| 1b | `phase-1b-unblock-operator-maintenance.md` | **completed** 2026-08-19, deployed `ee9c0d2` — census now empty, but **zero production effect** |
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

## Phase 0 — COMPLETE

Deployed `a00561b` on 2026-08-18 16:12 UTC and captured a 64.9-minute production
baseline. The completion criteria are met: the monitor runs in production, the
endpoint answers, the baseline is recorded, the census passes with an explicit
three-entry allowlist, and no trading behavior changed.

**The baseline, for Phase 1 to be measured against:**

| | |
|---|---|
| p50 | 1.076 ms |
| p95 | 8311.911 ms |
| **p99** | **8777.887 ms** |
| max | 15160.203 ms |
| stall episodes (>=3 s) | 365 in 64.9 min — one every 10.7 s |
| worst stall | 15160.203 ms |
| **loop unavailable** | **~76% of wall clock** |

The 76% is derived, not reported: 1886 samples where an unblocked loop would
have produced ~7785, leaving 2950.0 s of lag in a 3892.5 s window.

The distribution is bimodal — p50 is about 1 ms — so the loop is either healthy
or blocked for seconds, never mildly slow. That is the signature of discrete
blocking calls, matching the census, and it is what Phase 1 must fix.

Two things were found and deliberately **not** fixed, because Phase 0 is
observation only:

1. A third blocking call beyond the two the phase file named
   (`system_operator_bot`, see `phase_0_blocking_call_census`).
2. The loop-health endpoint cannot answer while the loop is stalled, which is
   the one completion criterion the phase file wrote that the implementation
   does not actually satisfy (see `phase-0-endpoint-limitation`).

## Phase 0 Task 6 — how it was actually deployed

`codex/mimo-v1-baseline` could not be deployed directly: the updater
fast-forwards, and it was not a descendant of the deploy branch. Correcting the
framing that was written while that was still unresolved — the branches never
entangled 32 commits of unrelated work. `codex/mimo-v1-baseline` carried nothing
but this remediation (3 planning-doc commits plus Phase 0's code), and all mimo
v2 work predates the `2274d90` fork point, so both branches already had it. The
deploy branch's 32 commits are entirely the deployment-gate work and were never
touched.

`codex/phase0-deploy-integration` is `origin/codex/deepcoin-auto-trading-v1`
(302c1ae) with `codex/mimo-v1-baseline` merged in. **Zero conflicts.**

Verified on the merged branch, not on either branch alone:

- Full suite: **5644 passed, 1 skipped, 0 failed** (385 s, `.venv`, Python 3.12.12).
- Exact counts: the deploy branch alone collects 5631; merged collects 5645; the
  delta of 14 is exactly the 14 tests Phase 0 adds. No test was lost to the merge.
- Imports were proven to resolve to the merged worktree, not the main checkout,
  before the run was trusted (`pythonpath = ["src"]` wins over the editable `.pth`).
- The census re-run on the merged tree still returns exactly the 3 allowlisted
  offenders, so the deploy branch's 32 commits introduced no new blocking call.

Production before the deploy was verified read-only over ssh to be `302c1ae`,
exactly the tip of the remote deploy branch, which is what made the blast radius
knowable: 4074 insertions, 0 deletions, of which only 178 lines
(`runtime_loop_health.py` plus 36 lines in `web_app.py`) are production code.

Then, from a checkout of the commit being deployed:

```bash
git push origin codex/phase0-deploy-integration:codex/deepcoin-auto-trading-v1
EXPECTED_COMMIT=a00561bf7683091ae0a48471cbfc2af1e6b9fa8c CHANGE_CLASS=code ./scripts/server_git_update.sh
```

The push was a fast-forward (`302c1ae..a00561b`, no force). The updater exited 0.

**The first attempt failed, and the reason matters for every later phase.** It
was run from the main checkout, which sits on `codex/mimo-v1-baseline`, so the
local `deploy/telegram-kol-update` hashed to `e70aa550` while the server
extracted `a7e30187` from `a00561b`. The SHA256 guard refused and exited
silently, leaving production untouched at `302c1ae`. There is a bash equivalent
of the PowerShell command — `scripts/server_git_update.sh`, driven by
environment variables — which matters because the workstation in use has no
PowerShell. **Run it from a checkout of the commit being deployed, or the guard
will refuse.**

Rollback target if needed: `302c1ae467b98bc954ac2a25cc4a33a8d09f48f9`, same
change class. No schema migration and no persisted state, so nothing else has to
be reversed.
## Phase 1 — COMPLETE, with one completion criterion NOT met

Deployed `fd748d7` on 2026-08-18 18:52 UTC and measured 63.4 minutes of real
production traffic against the Phase 0 baseline.

**What changed.** Both worker loops now submit their blocking tick to one shared
`ThreadPoolExecutor(max_workers=1)` in
`src/telegram_kol_research/runtime_worker_executor.py`. The single worker is the
whole point: it frees the event loop while preserving, exactly, the mutual
exclusion the two ticks previously got only as an accident of running on the
loop. This phase changed threading, never concurrency.

**The measurement:**

| | Phase 0 baseline | after Phase 1 | |
|---|---|---|---|
| p50 | 1.076 ms | 0.919 ms | flat |
| **p95** | **8311.911 ms** | **12.183 ms** | **682x better** |
| p99 | 8777.887 ms | 6765.435 ms | 22.9% better |
| stall episodes | 1 per 10.7 s | 1 per 36.9 s | 3.45x better |
| **worst stall** | **15160.203 ms** | **15356.616 ms** | **no change** |
| loop unavailable | ~76% | ~21.4% | 3.5x better |

Windows: 3892.5 s baseline, 3802.3 s after. The baseline was independently
reconfirmed minutes before the deploy over a 2.65-hour window (p99 8922.257,
75.1% unavailable), so the comparison does not rest on one reading.

**The criterion that is not met.** The phase file required production
`worst_stall_ms` to be materially below the baseline, expecting "tens of
milliseconds" and `stall_count` at zero. It is 15356.616 ms — marginally worse
than baseline — and `stall_count` is 103, not 0. Stalls of 6975.6 ms and
6197.1 ms were logged after the restart.

This is the outcome the phase file anticipated: *another blocking call is still
on the loop*, and the prescribed response is to record it and not guess further.
The prime suspect is the one remaining census entry,
`system_operator_bot.run_runtime_incident_notification_loop`, which calls
`run_operator_maintenance_tick` synchronously every 5 seconds with a database
read and a Deepcoin client build. It has **no owning phase** and needs one.
Ruled out while gathering evidence: the `lifecycle_monitor` candle-fetch errors
that fill the journal are ordinary Gate.io network timeouts in an `async def`,
and predate the deploy.

Read the shape honestly: p95 collapsing from 8.3 s to 12 ms means the loop is
now healthy the large majority of the time instead of the small minority. But
the tail is untouched, so a stall of ~15 s can still happen, and Phase 2 must be
measured against 6765.435 ms rather than against a fixed loop.

**Behavior unchanged — partially proven.** Management batches were still touched
after the restart (1 in `reconciling`, versus 0 rows in the comparable window
before it). Break-even convergence could **not** be positively verified: its
table's last row dates from 2026-08-03, so there was no convergence work in
either window. Zero `worker tick failed` entries since the restart. An idle path
is not a proven path; check the first real convergence after this deploy.

Rollback target: `a00561bf7683091ae0a48471cbfc2af1e6b9fa8c`. No schema change
and no persisted state, so the revert is complete.

## Phase 1 deployment — the procedure docs are wrong about the updater

Phase 1's file, and `deployment-procedure.md`, both instruct
`-ChangeClass execution_writer` with `-PreviousLiveSnapshotPath`. **Neither
argument exists.** `deploy/telegram-kol-update` accepts only `EXPECTED_COMMIT`
and `BRANCH`; `scripts/server_git_update.sh` exports only
`SERVER`/`KEY_PATH`/`BRANCH`/`EXPECTED_COMMIT`; `scripts/server_git_update.ps1`
has no `-ChangeClass` parameter. Phase 0's recorded `CHANGE_CLASS=code` was an
inert environment variable, not a gate selection.

What the updater actually does:

- **Safe window:** `telegram_kol_research.deployment_active_write_check`, run by
  the updater immediately before *and* immediately after stopping the service.
  Exit 3 means "refused: active exchange write". This is enforced automatically;
  there is nothing for a phase to pass in.
- **Schema handling:** auto-detected by diffing `models.py`, `db.py` and
  `migrations` between the current and candidate commits, not selected by class.
- **Health:** `verify_http_health` polls `/api/trading-settings` up to 20 times
  after start, and the cleanup trap rolls back to the previous commit on any
  failure.

A prior live position snapshot was captured by hand before this deploy
(14847 bytes, `captured_at` 2026-08-18T16:30:22Z) to satisfy the substance of
the requirement, even though the updater has no argument to consume it.

**Phases 2, 3 and 5 name the same nonexistent arguments and must be corrected
before they are executed.**

The deploy itself was run from the worktree checked out at the commit being
deployed, so the updater's SHA256 self-check matched on the first attempt:

```bash
git push origin codex/phase0-deploy-integration:codex/deepcoin-auto-trading-v1
EXPECTED_COMMIT=fd748d7aa7bf14acdf6c83d81fa137d1cdbab672 ./scripts/server_git_update.sh
```

## Phase 1b — COMPLETE, and it changed nothing in production

Deployed `ee9c0d2` on 2026-08-19 03:22 UTC and measured 64.8 minutes of real
traffic. The code did exactly what it was written to do. Production did not
improve at all.

| | Phase 1 | after Phase 1b | |
|---|---|---|---|
| p50 | 0.918 ms | 0.911 ms | flat |
| p95 | 8.491 ms | 7.213 ms | noise |
| p99 | 6759.363 ms | 7004.713 ms | slightly worse |
| **stall rate** | **1 per 37.36 s** | **1 per 37.37 s** | **identical** |
| worst stall | 15356.616 ms | 19687.274 ms | worse |
| loop unavailable | 21.2% | 22.4% | slightly worse |

The stall rate matching to four significant figures is the finding. Everything
else in that table is run-to-run noise; that number is not.

**Read `stall_count` correctly.** It is a cumulative counter over process
uptime, while `samples` and `window_seconds` are capped by the 7200-sample ring
buffer. Phase 1's reading had a saturated buffer at 22006 s of uptime, so its
589 stalls span the whole uptime, not the 4569 s window shown. Rates must be
computed against `uptime_seconds`. Comparing raw counts, or counts against
`window_seconds`, gives a wrong answer here.

**The conclusion is a real finding, not a failed change.**
`run_operator_maintenance_tick` genuinely ran on the event loop every 5 seconds
with a database read and a Deepcoin client construction. It genuinely no longer
does. `KNOWN_BLOCKING_CALLS` is genuinely empty. And production is genuinely no
better. So that call was not the bottleneck.

**The AST census has now exhausted what it can see.** It looks for a same-module
synchronous function named `*_tick` or `*_once`, called directly inside an
`async while` loop. Every such call is gone, and the loop still stalls for six to
ten seconds, once every thirty-seven seconds, with a 19.7 s outlier in this
window. Whatever remains does not have that shape.

Per the phase file, this is recorded and **not** pursued here. Widening the
census is its own task, it has no owning phase, and choosing to open it is the
user's call.

**Phase 1b stays deployed.** It removes a real database read and a real exchange
client construction from the loop on a five-second interval, and it holds the
census at zero so the next regression is caught. Rollback target if ever needed:
`fd748d7`.

## What is actually known about the remaining blocker

Stated as evidence, not as a hypothesis to act on:

- The distribution is bimodal. p50 is 0.911 ms and p95 is 7.213 ms, so the loop
  is healthy the overwhelming majority of the time, then blocked for seconds.
- Stalls arrive at a strikingly regular rate — one per ~37 s across both a
  65-minute and a 6-hour observation.
- Logged durations cluster between 6.1 s and 10.7 s. The 3 s threshold and the
  one-warning-per-60 s limiter mean the journal shows only the first stall of
  each minute, so this is a sample, not the distribution.
- Ruled out by measurement: both management worker ticks (Phase 1), the operator
  maintenance tick (Phase 1b).
- Ruled out by inspection: `lifecycle_monitor` candle fetches — they are noisy in
  the journal but run in an `async def` and are ordinary Gate.io timeouts.

What the census cannot see, and what a widened investigation would have to cover:
synchronous work inside an `async def` that is not named `*_tick`/`*_once`;
blocking calls reached through an awaited coroutine; synchronous database or
HTTP work in request handlers and in the Telethon message path; and CPU-bound
work that blocks without any I/O call at all.

## Before Phase 2 starts — read this

Points 1 and 2 below replace the Phase 1 version of this section, which was
wrong about the updater's interface. See "the procedure docs are wrong about the
updater" above.

1. **There is no change class and no snapshot argument.** Do not try to pass
   `-ChangeClass`, `CHANGE_CLASS`, `-PreviousLiveSnapshotPath`, or
   `PREVIOUS_LIVE_SNAPSHOT_PATH`; the updater ignores all of them. Its safe
   window is enforced automatically by `deployment_active_write_check`, before
   and after the service stop. Phase 2's file names these arguments and is wrong
   on this point.
2. **This workstation has no PowerShell.** Use
   `EXPECTED_COMMIT=<40-hex> ./scripts/server_git_update.sh`.
3. **Run the deploy script from a checkout of the commit being deployed.** The
   updater compares the SHA256 of the local `deploy/telegram-kol-update` against
   the copy inside the deployed commit and exits silently on a mismatch. This
   cost one confusing failure in Phase 0; Phase 1 avoided it by deploying from
   the worktree.
4. **Phase 2's own prerequisite is still NOT met.** It says: "Do not attempt
   this phase while the event loop can still be blocked — the two effects would
   be indistinguishable in production." After Phase 1b the loop still stalls
   once every 37 seconds, for six to ten seconds at a time. Phase 2 introduces
   real parallelism; measuring it against this loop cannot separate its effect
   from the residual blocker. **Phase 2 should not start yet.**
5. **The next step is an investigation that has no phase and no owner.** The
   census is empty and the stalls remain, so finding the real blocker means
   widening how it is looked for — see "What is actually known about the
   remaining blocker" above. Opening that task is a decision for the user, not
   for the next session to help itself to.

The authoritative checkout for this rollout is the worktree
`.worktrees/runtime-serialization` on branch `codex/phase0-deploy-integration`,
which is what gets pushed to `deploy_branch`. `codex/mimo-v1-baseline` is
superseded — its copy of this file is frozen and says so.

The census in `tests/test_runtime_event_loop_blocking_census.py` asserts
equality, so any phase that removes a blocking call must shrink
`KNOWN_BLOCKING_CALLS` in the same commit or the suite fails.

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
