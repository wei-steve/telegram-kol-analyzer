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
current_phase: 3
phase_name: compensation-window-repair
phase_status: planned          # planned | claimed | in_progress | completed
claimed_by: none               # phase 2f completed and released 2026-08-20; see phase_2f_* fields below before starting phase 3
current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-3-compensation-window-repair.md
last_completed_phase: 2f   # the sequence ran 0, 1, 1b, 1c, 1d, 1e, 2, 2f
last_completed_commit: 8122f15ba653e900ee88352b18f570d500bd65c4
phase_2f_reason: "user explicitly chose to close the gap in a dedicated phase before phase 3, 2026-08-19, when asked"
phase_2f_code_commit: 8122f15ba653e900ee88352b18f570d500bd65c4   # tasks 0-4; committed 2026-08-20 (session started 2026-08-19, deploy landed just past UTC midnight)
phase_2f_deployed_commit: 8122f15ba653e900ee88352b18f570d500bd65c4
phase_2f_local_suite_before: 5710   # passed, 1 skipped, at 3f5ed78
phase_2f_local_suite_after: 5713    # passed, 1 skipped, 0 failed; delta 3 equals the 3 tests phase 2f adds
phase_2f_gap_a_closed: true   # recovery_live_submit._submit_recovery_signal_direct now also holds position_authority_lock (stacked outside @serialized_source_message_execution)
phase_2f_gap_b_closed: true   # strategy_management_composite_executor.execute_composite_management_batch now holds position_authority_lock around its whole body
phase_2f_lock_ordering_precedent: "position_authority_lock outer, source_execution_lock inner - matches the pre-existing stacking order in deepcoin_execution_actions.recreate_trigger_entry_tpsl, so gap A introduces no new lock-ordering risk"
phase_2_per_chat_prerequisite_now_met: true   # tests/test_position_authority_boundary_coverage.py::test_per_chat_sharding_prerequisite_gap_is_closed passes; KNOWN_UNCOVERED_LEAVES is empty
phase_2_message_lock_mode_enabled_in_production: false   # STILL "global" - phase 2f closed the prerequisite gap but did NOT enable per_chat; that is a separate, explicit decision for a future session
phase_2_gap_a: "recovery_live_submit._submit_recovery_signal_direct (entry-signal order submission, strategy-revision-replacement submission) was serialized only by _source_execution_lock, not position_authority_lock - CLOSED by phase 2f"
phase_2_gap_b: "strategy_management_composite_executor.execute_composite_management_batch (composite management batch close/SLTP writes) was not serialized by anything - CLOSED by phase 2f"
phase_2_code_commit: 3f5ed78096f33d5dda59400a3a90dcf9bcb9c4cd   # tasks 1-4 code + task 5 suite; committed 2026-08-19
phase_2_deployed_commit: 3f5ed78096f33d5dda59400a3a90dcf9bcb9c4cd
phase_2_local_suite_before: 5684   # passed, 1 skipped, at 92e6e60
phase_2_local_suite_after: 5710    # passed, 1 skipped, 0 failed; delta 26 equals the 26 tests phase 2 adds
phase_0_code_commit: 816e296   # tasks 1-5; reviewed 2026-08-18. Task 6 (deploy + baseline) outstanding
last_deployed_commit: 8122f15ba653e900ee88352b18f570d500bd65c4
production_commit: 8122f15ba653e900ee88352b18f570d500bd65c4  # phase 2f, deployed 2026-08-20 00:39 UTC; verified over ssh 2026-08-20 00:39 UTC
production_commit_before_phase_2f_deploy: 3f5ed78096f33d5dda59400a3a90dcf9bcb9c4cd  # rollback target for phase 2f's code (no schema change)
production_commit_before_phase_2_deploy: 92e6e60a0985a81208064f785e2454bcafd99bfe  # rollback target for phase 2's code (no schema change)
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
phase_1c_deployed_commit: 93d1dfb483c34cea88ec9b7ca9adff7aa6bcaa2d   # stall attribution, observation only
phase_1c_blocker_found: "web_app.run_deepcoin_execution_reconcile_loop:7807 -> execution_bindings.reconcile_deepcoin_execution_bindings"
phase_1c_blocker_interval_seconds: 30     # 30 s sleep plus 6-10 s blocking = the measured 37.36 s stall period
phase_1c_captures: 20                     # distinct, over 25 min of steady state; 19 under the reconcile loop
phase_1d_deployed_commit: 1c8a7f29485429184584a3016b4264c9822fe6e3
phase_1d_local_suite_before: 5677
phase_1d_local_suite_after: 5680          # delta 3 equals the 3 tests added
loop_lag_after_phase1d_p99_ms: 232.928    # was 7464.866 before this phase — 32x better
phase_1d_worst_stall_ms: 6470.313         # was 12965.977. Halved, but still seconds, not tens of ms
phase_1d_stall_rate: "1 per 1250.15 s"     # was 1 per 37.30 s — the metronome is gone
phase_1d_loop_unavailable_pct: 2.0        # was 23.1
phase_1d_worst_stall_criterion_met: false # stalls approached zero; worst stall did not reach tens of ms
next_blocker_named: "lifecycle_monitor._run_one_cycle -> _context_resolution_scheduler -> context_resolution_worker.schedule_context_reanalysis:394 .all()"
census_third_blind_spot: "calls through an instance attribute or injected callback are ast.Attribute, not ast.Name, so the widened census still cannot see them"
phase_1e_deployed_commit: 92e6e60a0985a81208064f785e2454bcafd99bfe
phase_1e_local_suite_before: 5680
phase_1e_local_suite_after: 5684          # delta 4 equals the 4 tests added
loop_lag_after_phase1e_p99_ms: 79.914     # was 236.271 after phase 1d; 8777.887 at the phase 0 baseline
phase_1e_worst_stall_ms: 9297.432         # was 6470.313. WORSE, and the criterion is again not met
phase_1e_stall_count: 3                   # unchanged from phase 1d, but no captured stall is application code
phase_1e_loop_unavailable_pct: 1.2        # was 2.0 after phase 1d, 76 at the phase 0 baseline
phase_1e_worst_stall_criterion_met: false
residual_stalls_are_not_application_code: true  # both captures show the loop IDLE in selectors.select
residual_stall_leading_explanation: "host memory pressure and swap, not code: 477 MB of 1024 MB swap in use, the service itself 21.9 MB swapped out, vmstat si non-zero, while CPU steal is 0 and load is 0.60 on 2 cores. Correlational - measured after the window, not during a stall."
application_level_loop_blocking_believed_cleared: true  # with the caveat that 1 of 3 stalls was not captured
next_step_needs_user_decision: true       # fixing the named blocker has no phase and no owner
production_commit_before_phase_1b: fd748d7aa7bf14acdf6c83d81fa137d1cdbab672  # phase 1b rollback target
production_commit_before_phase_2: 92e6e60a0985a81208064f785e2454bcafd99bfe  # phase 2 rollback target (level 2; level 1 is the settings flag)
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

  - "phase-1c-local (2026-08-19, session-45794fed): LoopStallAttributor added to runtime_loop_health.py. It is a plain OS daemon thread, not a coroutine, because the loop cannot report on itself while blocked - the same reason the Phase 0 endpoint times out during a stall. LoopLagMonitor.run() records threading.get_ident() once and calls note_checkin() every iteration; the watchdog polls 4x a second and, when the check-in gap crosses the threshold, reads that threads frame from sys._current_frames() and formats its stack. One capture per episode, rate limited to the existing 60 s window, bounded to 5 captures and 25 frames. The frame table is only read when a capture is warranted, so steady-state cost is a clock comparison. Captures surface in the journal AND in GET /api/runtime/loop-health. 13 tests added, none of which sleep or need a real stall (injected clock and frame table): no capture before attach, none while checking in normally, capture once the gap crosses, exactly one per episode, rate limit across episodes, missing thread id and failing frame provider both degrade to a recorded reason, bounded captures and stack depth, daemon thread, idempotent start. Full suite 5677 passed, 1 skipped, 0 failed, 394s; before 5664, delta 13 exactly."
  - "phase-1c-ANSWER (2026-08-19 08:17-08:42 UTC): THE BLOCKER IS NAMED. Outcome 1 of the three the phase file listed - the stacks name a Python frame. 20 distinct captures over 25 minutes of steady state (uptime 97 s to 1481 s, so not a startup artifact). NINETEEN OF TWENTY are under src/telegram_kol_research/web_app.py:7807, run_deepcoin_execution_reconcile_loop, which calls execution_bindings.reconcile_deepcoin_execution_bindings SYNCHRONOUSLY on the event loop. blocked_ms across the captures ranges 3006 to 7513. The innermost frames are spread across the whole reconcile routine rather than one hot line - deepcoin_client.py:598 _request (the synchronous httpx exchange call, 3 captures), execution_bindings.py:2211 _exchange_row_matches_leg, :2287 _has_prior_authoritative_position_audit .all(), :2601 _post_entry_protection_mutated_binding_ids .all(), :4564 _apply_recorded_terminal_entry_events .all(), :1246 the leg-matching genexpr, position_take_profit_orders.py:229 .one_or_none(), and position_authority_lock.py:17. So the whole routine is seconds long: exchange HTTP plus many SQLite queries plus Python-level matching over legs, all on the loop."
  - "phase-1c-numbers-all-reconcile: the loops interval is `interval_seconds: int = 30` (web_app.py:7793). 30 s of sleep plus 6-10 s of blocking predicts a 36-40 s stall period; the measured rate was one per 37.36 s across a 6-hour window and one per 37.37 s across a 65-minute window. Each iteration also calls deepcoin_client_factory() and reaches list_open_orders, whose httpx timeout is 15 s - which accounts for the 15356.6 ms and 19687.3 ms outliers. And it is a timer, which is why the stall rate was a metronome while production handled only 1-16 messages an hour. Three previously unexplained observations, one cause."
  - "phase-1c-why-the-census-missed-it: this call hits BOTH blind spots at once. discover_blocking_calls() matches a call whose function name ends in _tick or _once AND which is defined in the same module. reconcile_deepcoin_execution_bindings ends in neither, and it is imported from execution_bindings into web_app. Either condition alone would have caught it. That is how KNOWN_BLOCKING_CALLS reached empty while the loop kept stalling, and it is why Phase 1b looked like a contradiction. The census needs widening before it can be trusted as a gate."
  - "phase-1c-secondary-findings: two things worth carrying forward, neither pursued here. (1) ONE capture of the twenty was not under the reconcile loop at all: the loop was idle in asyncio base_events _run_once at self._selector.select(timeout) while 7513 ms of lag accrued. An idle selector cannot be the loop being busy, so that episode is outcome 3 - a process-level pause such as GC, swap, or CPU starvation. One in twenty, mechanism unknown, recorded not chased. (2) TWO captures were blocked at position_authority_lock.py:17, `with _POSITION_AUTHORITY_LOCK:`, a threading.Lock acquired ON the event loop. Phase 1 moved the management ticks onto a worker thread, so a lock that used to serialize work already serialized by the loop can now be held by that worker while the loop waits on it. Whether Phase 1 made this reachable, or it was always reachable, is not established here."
  - "phase-1c-not-fixed-by-design: Phase 1c is observation only and fixed nothing. Production runs 93d1dfb, which adds the watchdog and changes no trading behavior. The named blocker is still on the event loop. Rollback target if the watchdog is ever unwanted: ee9c0d2."

  - "phase-1d-local (2026-08-19, session-45794fed): the reconcile loop body interleaves blocking calls with awaits, so it could not be wrapped as one unit like Phases 1 and 1b. All three blocking calls now submit individually to the EXISTING shared max_workers=1 executor - _build_deepcoin_reconcile_client (client construction plus the timestamp, kept together so the clients lifecycle is not split across threads), reconcile_deepcoin_execution_bindings, and sync_manual_closed_deepcoin_positions - while the three deliver_* awaits stay on the loop. The whole body remains inside ONE try, so a raise in an early segment still skips every later segment, and a test asserts exactly that. Census widened in the same commit: it had required the callee to be BOTH named *_tick/*_once AND defined in the same module, and the real offender was neither. The widened matcher accepts imported callees and any name. It surfaced 18 entries, hand-triaged: FOUR are real, previously invisible blocking calls in the bot command loops (process_system_operator_command, process_system_operator_callback_data, format_holding_positions_message, format_pending_positions_message - all take session_factory and query the database on the loop); the other 14 are pure helpers with no I/O, each reviewed by hand and annotated. The four real ones are recorded, NOT fixed, and have no owning phase - they are event driven, not timer driven, which is why they never produced the metronomic stalls. Full suite 5680 passed, 1 skipped, 0 failed, 387s; before 5677, delta 3 exactly."
  - "phase-1d-deploy (2026-08-19 10:07 UTC): DEPLOYED 1c8a7f2, updater exit 0 captured without a pipe. HEAD=1c8a7f2, telegram-kol.service active since 2026-08-19 18:07:23 CST, /api/trading-settings 200, watchdog_attached true."
  - "phase-1d-after (2026-08-19 11:09 UTC, uptime 3750 s = 62.5 min of real traffic): samples 7200 (ring buffer saturated), window_seconds 3674.064, p50_ms 0.89, p95_ms 6.168, p99_ms 232.928, max_ms 6470.313, stall_count 3, worst_stall_ms 6470.313, stall_captures 1. THE ATTRIBUTION WAS RIGHT. Against the pre-deploy reading (uptime 6565.6 s, stall_count 176, p99 7464.866, worst 12965.977, 23.1 percent unavailable): stall rate 1 per 37.30 s -> 1 per 1250.15 s, a 33.5x drop; at the old rate this uptime would have produced about 101 stalls and it produced 3. p99 7464.866 -> 232.928 ms, 32x better. Derived loop unavailability 23.1 -> 2.0 percent. The metronome is gone. This is the first phase whose falsifiable prediction held."
  - "phase-1d-criterion-partially-met: stalls approached zero as predicted, but worst_stall_ms is 6470.313, not the tens of milliseconds the phase file predicted. Down from 12965.977, so halved, but still seconds. Stated plainly: this criterion is NOT fully met. The difference from Phases 1 and 1b is that the residue is no longer a mystery - see the next entry."
  - "phase-1d-next-blocker-ALREADY-NAMED: the Phase 1c watchdog captured one of the three remaining stalls (3033.7 ms at 11:01:54 UTC) and named its cause with no guessing required, which is exactly what that watchdog was built for. The chain: lifecycle_monitor.py:307 run_loop -> await self._run_one_cycle() -> lifecycle_monitor.py:617 self._context_resolution_scheduler(...) -> web_app.py:4595 lambda -> web_app.py:3647 _schedule_context_resolution_for_app -> context_resolution_worker.py:394 schedule_context_reanalysis, which runs query.order_by(...).all() - a SQLAlchemy query executed on the event loop from inside an awaited coroutine. Recorded, NOT fixed; Phase 1d is scoped to the reconcile loop."
  - "phase-1d-census-THIRD-blind-spot: the widened census cannot see the call above either, and the reason is structural. self._context_resolution_scheduler(...) is an ast.Attribute call through an instance attribute holding an injected callback, while the matcher requires isinstance(func, ast.Name). Static analysis cannot resolve what that attribute holds at runtime. So the census now has a documented third blind spot on top of the two Phase 1d closed, and it must not be treated as proof that the loop is clean - only as a guard against regressions of the shapes it does match. The watchdog, not the census, is what finds these."
  - "phase-1d-behavior-unchanged: the strongest evidence of any phase so far. execution_bindings shows 155 rows touched in the 62 min after the restart, so the reconcile is genuinely running and genuinely doing its work from the worker thread. Zero 'Deepcoin execution reconcile failed', zero 'reconcile skipped', zero 'worker tick failed' in the journal since the restart. strategy_management_batches 1 row and message_instruction_items 1 row also touched."

  - "phase-1e-local (2026-08-19, session-45794fed): lifecycle_monitor._run_one_cycle collected the scheduler events its two loops would have fired and now submits them as ONE batch to the existing shared mgmt-worker executor via _run_context_resolution_scheduler_batch, preserving order and payloads. One submission rather than N+M because the originals ran back to back with nothing between them. The `if self._context_resolution_scheduler is not None` guard, the event payloads, the loop order, and the already-correct `await asyncio.to_thread(self._context_resolution_worker)` below are untouched. Two mistakes made and fixed during the work, recorded so they are not repeated: the helper was first inserted between a @dataclass decorator and its class, which broke module import outright, and an unused test helper was left behind and removed. 4 tests added (worker thread, payload fidelity, no submission when nothing is scheduled, responsiveness under a slow scheduler). Full suite 5684 passed, 1 skipped, 0 failed, 418s; before 5680, delta 4 exactly."
  - "phase-1e-deploy (2026-08-19 11:29 UTC): DEPLOYED 92e6e60, updater exit 0 captured without a pipe. HEAD=92e6e60, service active since 2026-08-19 19:29:07 CST, /api/trading-settings 200."
  - "phase-1e-after (2026-08-19 12:31 UTC, uptime 3744 s = 62.4 min): samples 7200, window 3643.605, p50_ms 0.916, p95_ms 6.128, p99_ms 79.914, max_ms 9297.432, stall_count 3, worst_stall_ms 9297.432, stall_captures 2. Against Phase 1d (uptime 4881 s, 3 stalls, p99 236.271, worst 6470.313, 2.0 percent unavailable): p99 236.271 -> 79.914 ms, 3x better; loop unavailability 2.0 -> 1.2 percent; stall_count unchanged at 3; worst_stall_ms 6470.313 -> 9297.432, WORSE. The numeric criterion - stalls near zero and worst stall under a second - is NOT met, for the fourth phase running. But what the stalls ARE changed completely; see the next entry."
  - "phase-1e-residual-stalls-are-NOT-code: this is the finding. BOTH captured stalls (4589.1 ms at 12:02:14Z and 4565.2 ms at 12:31:32Z) have byte-identical 18-frame stacks whose deepest frame is selectors.py:468 select, under base_events.py:1961 _run_once. The event loop was IDLE in the OS epoll wait, executing no Python at all, while thousands of milliseconds of lag accrued. That is not application code blocking the loop; it is the process not being scheduled or not being resident. The same signature appeared once in the Phase 1c captures (1 of 20) and was recorded then as an unexplained minority. It is now the whole of what remains. CAVEAT, stated because it matters: only 2 of the 3 stalls were captured - the one-per-60 s limiter dropped the third - so the correct claim is that every CAPTURED stall is non-code, not that no application blocking exists anywhere."
  - "phase-1e-host-evidence (measured 2026-08-19 12:35 UTC, after the window, NOT during a stall - so this is correlational, not proven causation): the server has 1965 MB of RAM with 689 MB free and 477 MB of its 1024 MB swap in use. The telegram-kol process itself reports VmRSS 209476 kB and VmSwap 21888 kB, so roughly 21.9 MB of it is on disk. vmstat showed non-zero swap-in (si 292 and 152 KB/s across consecutive samples). Meanwhile CPU steal is 0 and load average is 0.60 on 2 cores, which rules out host CPU oversubscription and local CPU contention. Memory pressure with active paging is therefore the leading explanation for a loop that stalls seconds while idle in select: page faults on a swapped-out process stall it in the kernel, where no Python frame can show it. Confirming this needs sampling during a stall, or simply more RAM."
  - "phase-1e-the-arc: against the Phase 0 production baseline, the whole of phases 1 through 1e moved the loop from p99 8777.887 ms to 79.914 ms (110x), from one stall per 10.7 s to one per 1248.1 s (117x), and from 76 percent unavailable to 1.2 percent (63x). Phase 2s prerequisite - that the loop can no longer be blocked - is now met by any reasonable reading."

  - "phase-2-task1-DECISION-GATE-FAILED (2026-08-19, session-phase2-lockshard-0819): traced every exchange-mutation leaf reachable from auto_trade_executor and checked each against position_authority_lock coverage, per the phase file's explicit Task 1 instruction. Verdict: FALSE, coverage is incomplete. Three leaves ARE covered - cancel_revision_entry_leg (deepcoin_execution_actions.py, @serialized_position_authority_mutation), execute_management_batch (strategy_management_executor.py, same decorator), reconcile_deepcoin_execution_bindings (execution_bindings.py, `with position_authority_lock():` directly, matching the Phase 1c stall captures at position_authority_lock.py:17). TWO are not: (gap A) recovery_live_submit._submit_recovery_signal_direct - reached from both process_trade_signal_live (entry-signal submission) and submit_strategy_revision_replacement_live (revision-replacement submission) - is decorated @serialized_source_message_execution, which acquires _source_execution_lock (source_message_deletion.py), a DIFFERENT RLock object, not position_authority_lock; its leaf writes (deepcoin_client.place_order/trigger_order, submit_exact_position_sltp via position_mutation_gateway.py, upsert_execution_binding/upsert_execution_order_leg) all run under that other lock only. (gap B) strategy_management_composite_executor.execute_composite_management_batch - the composite/'management v2' close-revise-partial-close path, reached whenever a management candidate carries management_contract_json - imports position_authority_lock nowhere in the file; its close_exact_position and submit_exact_position_sltp calls go through the same unlocked position_mutation_gateway.py, which provides only a DB-backed intent state machine (idempotency), not mutual exclusion. Today this is harmless because the single global message lock already prevents two messages of any kind from being in flight at once; per-chat sharding would remove that accidental protection and let e.g. chat A's entry submission (gap A, under _source_execution_lock) run truly concurrently with chat B's composite management close (gap B, under nothing) for the same or a different symbol. Per the phase file's Task 1 decision gate: 'if any exchange mutation path ... is not covered ... do not enable per-chat sharding in this phase. Finish Tasks 2 and 3, leave the flag disabled, record the gap, and stop.' That is what this session did. tests/test_position_authority_boundary_coverage.py encodes both the three covered leaves and the two gap leaves as parametrized regression guards, plus one explicit decision-gate assertion, so a future session that closes the gaps (or accidentally loses existing coverage) gets a failing test rather than silent drift."
  - "phase-2-tasks-2-4-local (2026-08-19, session-phase2-lockshard-0819): built the dormant infrastructure regardless of the Task 1 gate, per the phase file's explicit instruction. Task 2: src/telegram_kol_research/keyed_async_locks.py, KeyedAsyncLockRegistry - per-key asyncio.Lock created on first use via a refcounted guard (asyncio.Lock protecting the dict), dropped the instant refcount reaches zero and the lock is unlocked so a long uptime with many distinct chat_ids does not grow the registry without bound; lock_all() acquires every currently-known key's lock in a sorted(keys, key=repr) order, which is what makes two concurrent lock_all() calls deadlock-free (proven with a real two-key contest, not just asserted). 7 tests. Task 3: new src/telegram_kol_research/message_lock_provider.py, MessageLockProvider - mode() reads trading_settings.message_lock_mode fresh from the database on every call (no caching), so the flag is flippable without a restart; in 'global' mode it always returns the same shared asyncio.Lock regardless of chat_id (byte-for-byte the old single-lock behavior), in 'per_chat' mode it delegates to the registry. trading_settings.py gained message_lock_mode: Literal['global','per_chat'] = 'global' following the existing _mimo_contract_mode two-value-literal pattern exactly. telegram_live_listener.py: handle_new_message and handle_deleted_message now resolve their lock via resolve_lock_context(operation_lock, chat_id) instead of using operation_lock directly, preserving the None-fallback and raw-Lock-object contract that tests/test_reconcile_live_history.py and tests/test_telegram_live_listener.py already exercised, unmodified. run_periodic_reconcile now branches on resolve_message_lock_mode(operation_lock): 'none' and 'global' both wrap the ENTIRE run_reconcile_once call exactly as before (global is not merely 'the default', it is verified byte-for-byte identical); only 'per_chat' changes shape, passing a new chat_operation_lock parameter into run_reconcile_once, which the recovery-loop and per-dialog loop now acquire individually PER MESSAGE (extracted into _process_recovery_candidate and _process_dialog_raw_message closures so the lock scope is exactly one message's chain), never around dialog discovery or the Telegram fetch calls. web_app.py wires up app.state.message_lock_registry and app.state.message_lock_provider at the same point telegram_operation_lock is constructed, and both /api/refresh and the mimo-contract-mode-change guard in /api/trading-settings switch from `async with app.state.telegram_operation_lock:` to `async with app.state.message_lock_provider.lock_all():` (global mode: identical, since lock_all() returns that same object). Task 4: tests/test_live_listener_chat_isolation.py, 4 tests against the real wiring (run_live_listener + run_reconcile_once + a real MessageLockProvider/KeyedAsyncLockRegistry/session_factory, only persist_live_message_event and the authoritative processor faked) - a slow chat A does not delay chat B in per_chat mode; the same chat still serializes in arrival order in per_chat mode; global mode is provably byte-for-byte (chat B does not even start while chat A is in flight, opposite of per_chat); a per_chat reconcile pass processing chat A's recovery backlog does not block a live message in chat B. CAUGHT AND FIXED DURING THIS SESSION, not left in: the first implementation had resolve_message_lock_mode/resolve_lock_context called directly (synchronously) inside run_periodic_reconcile's `while True:` loop, which is itself a fresh, real synchronous database read (load_trading_settings) added to an unconditional loop - exactly the shape tests/test_runtime_event_loop_blocking_census.py exists to catch, and it did: it flagged both calls by name before this was committed. Fixed by wrapping both in asyncio.to_thread at the two call sites inside the while-loop only; handle_new_message/handle_deleted_message's per-message resolve call was deliberately left synchronous and direct, matching the codebase's own established precedent (e.g. web_app.py's _run_authoritative_processor already reads trading_settings synchronously per-message, off the loop via the existing asyncio.to_thread(authoritative_processor, ...) wrapper one level up; the four bot-command-loop blocking calls Phase 1d found and left unfixed are the same 'event-driven, not timer-driven' category) - the census does not flag it because it is not inside a while-loop, and a single indexed-key SQLite read per live message is the same order of cost as reads already happening elsewhere on this path."
  - "phase-2-task5-suite (2026-08-19): full local suite with .venv (Python 3.12.12): 5710 passed, 1 skipped, 0 failed, 390s. Before (at 92e6e60) was 5684 passed, 1 skipped; delta 26 equals exactly the tests this phase adds (7 keyed-lock-registry + 6 architecture/boundary-coverage + 4 chat-isolation + 9 trading-settings message_lock_mode round-trip and fails-closed cases). The blocking-call census (tests/test_runtime_event_loop_blocking_census.py) passes with KNOWN_BLOCKING_CALLS unchanged from before this phase - no new allowlist entry was needed once the to_thread fix above was applied."
  - "phase-2-deploy (2026-08-19 20:18 UTC): DEPLOYED DORMANT, per Task 6 Step 1 only - Steps 2 and 3 (prove the disable path, enable per_chat) were not attempted, because Task 1's decision gate failed and per_chat has no safe path to production this phase. Fast-forward pushed 92e6e60..3f5ed78 to codex/deepcoin-auto-trading-v1 (2 commits: the phase-2 claim, then the phase-2 code), confirmed origin/codex/deepcoin-auto-trading-v1 was an ancestor of HEAD first. Ran EXPECTED_COMMIT=3f5ed78096f33d5dda59400a3a90dcf9bcb9c4cd ./scripts/server_git_update.sh from this worktree, checked out at that exact commit; exit code 0, captured without a pipe. The curl connection-refused lines near the end of the updater's own output are verify_http_health's retry loop during the service restart, same as every prior phase's deploy - not a failure signal by themselves. Independently verified over ssh (not inferred from the updater's exit code): HEAD=3f5ed78 on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-20 04:18:38 CST, GET /api/trading-settings returns 200 with message_lock_mode: 'global' in the payload (confirms dormant, not merely 'default' - this is what the settings row actually contains in production right now), GET /api/runtime/loop-health answers (uptime 12.1 s at read time, so not a baseline reading, just a liveness check: samples 23, stall_count 0, watchdog_attached true)."
  - "phase-2-not-attempted: Task 6 Steps 2 and 3, and therefore the phase file's own completion criteria 'per_chat enabled in production, with the disable path proven working beforehand' and 'one full trading session observed with no new incident class', are NOT met and will not be attempted until a future phase closes phase_2_gap_a and phase_2_gap_b (see the decision-gate entry above and the 'Phase 2 -- Task 1 decision gate FAILED' section below). This is the outcome the phase file's own Task 1 anticipated and explicitly prescribed a stop for, not an incomplete session."
  - "phase-2f-task0-lock-ordering (2026-08-19/20): before writing any code, checked whether stacking position_authority_lock outside _submit_recovery_signal_direct's existing @serialized_source_message_execution could invert lock order anywhere else in the codebase (both are threading.RLock; reentrant on the same thread, but a fixed cross-lock order must hold everywhere or two threads acquiring them in opposite orders can deadlock). Found a pre-existing, already-live precedent: deepcoin_execution_actions.recreate_trigger_entry_tpsl already stacks @serialized_position_authority_mutation directly above @serialized_source_message_execution (position lock outer, source-execution lock inner) - grep-verified at deepcoin_execution_actions.py:1654-1656. Adopted the same order for gap A, so this introduces no NEW lock-ordering risk, only a second call site using an order the codebase already exercises safely. Gap B (execute_composite_management_batch) only ever needed to acquire position_authority_lock alone - it does not call into anything that acquires _source_execution_lock - so no ordering question arises there."
  - "phase-2f-tasks1-3-local (2026-08-19/20): Gap A closed by adding @serialized_position_authority_mutation as the outermost decorator on _submit_recovery_signal_direct (recovery_live_submit.py), stacked above the pre-existing @serialized_source_message_execution and @_report_entry_submission_progress - a 1-line functional change (plus the import). Gap B closed by adding the same decorator directly on execute_composite_management_batch (strategy_management_composite_executor.py), following the exact pattern execute_management_batch already used - also a 1-line functional change. Both proved with a real thread-based test, not a source-text check: a simulated mutation holds position_authority_lock on one thread while a second thread calls the now-covered function, and the test asserts the second thread cannot proceed (does not reach the exchange write, or does not even begin loading its batch) until the first thread releases the lock - tests/test_recovery_live_submit.py::test_submit_recovery_signal_direct_is_covered_by_position_authority_lock and tests/test_strategy_management_executor.py::test_execute_composite_management_batch_is_covered_by_position_authority_lock. tests/test_position_authority_boundary_coverage.py restructured: both leaves moved from KNOWN_UNCOVERED_LEAVES (now empty, kept as an empty list rather than deleted so a future regression has somewhere to be recorded) into COVERED_LEAVES; the old 'decision gate NOT met' assertion was replaced with test_per_chat_sharding_prerequisite_gap_is_closed, which asserts the opposite and is explicit that this closes the PREREQUISITE for per_chat, not per_chat itself."
  - "phase-2f-task4-suite: full local suite with .venv (Python 3.12.12), run independently twice (once by the executing session before it was interrupted by an API session-limit error immediately before its own commit step, and once more by the session that took over to finish Tasks 4-5 and verify the interrupted session's uncommitted work before trusting it): 5713 passed, 1 skipped, 0 failed both times, ~390-411s. Before (at 3f5ed78) was 5710 passed, 1 skipped; delta 3 equals exactly the tests this phase adds (1 in test_recovery_live_submit.py, 1 in test_strategy_management_executor.py, and a net +1 in test_position_authority_boundary_coverage.py after removing one test and adding two)."
  - "phase-2f-deploy (2026-08-20 00:39 UTC): DEPLOYED. Fast-forward pushed 3f5ed78..8122f15 to codex/deepcoin-auto-trading-v1 (2 commits: the phase-2f claim, then the phase-2f code), confirmed origin/codex/deepcoin-auto-trading-v1 was an ancestor of HEAD first. Ran EXPECTED_COMMIT=8122f15ba653e900ee88352b18f570d500bd65c4 ./scripts/server_git_update.sh from this worktree, checked out at that exact commit; exit code 0, captured without a pipe. The curl connection-refused lines near the end of the updater's own output are verify_http_health's retry loop during the service restart, consistent with every prior phase's deploy. Independently verified over ssh: HEAD=8122f15 on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-20 08:39:27 CST (=00:39:27 UTC), zero journal entries at priority err or above in the 2 minutes since restart, GET /api/trading-settings returns 200 with message_lock_mode: 'global' (unchanged by this phase, confirming it stayed dormant) and auto_trade_enabled: true, GET /api/runtime/loop-health answers with stall_count 0 at uptime 9.98s (a liveness check only, not a baseline - too early to compare against Phase 1e's p99). position_attribution_audits (3251 rows total) and position_protection_incidents (313 rows total) were read as a pre-deploy-activity snapshot only, most recent row 2026-08-19 22:47:50 predating this deploy by hours; not enough time had passed at verification time to observe any post-deploy row, so this does NOT yet demonstrate 'no new incident class' - that requires a longer observation window this session did not run."
  - "phase-2f-conclusion: Phase 2's own Task 1 decision gate now passes - tests/test_position_authority_boundary_coverage.py::test_per_chat_sharding_prerequisite_gap_is_closed passes, KNOWN_UNCOVERED_LEAVES is empty. This makes the PREREQUISITE for message_lock_mode=per_chat true. It does NOT enable per_chat, which stays 'global' in production and remains a separate, explicit decision - Phase 2's own Task 6 Steps 2 (prove the disable path) and 3 (enable and observe a full trading session) are still un-started and still require their own session with real multi-group traffic to observe, not something this phase or the next one may infer from a lock now being present in the code."

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
| 1c | `phase-1c-stall-attribution.md` | **completed** 2026-08-19, deployed `93d1dfb` — blocker NAMED: the deepcoin reconcile loop |
| 1d | `phase-1d-unblock-deepcoin-reconcile.md` | **completed** 2026-08-19, deployed `1c8a7f2` — stalls 1/37 s → 1/1250 s, loop unavailable 23.1% → 2.0% |
| 1e | `phase-1e-unblock-context-resolution-scheduler.md` | **completed** 2026-08-19, deployed `92e6e60` — p99 236 → 80 ms; residual stalls are NOT code |
| 2 | `phase-2-per-chat-lock-sharding.md` | **completed** 2026-08-19, deployed `3f5ed78` DORMANT — Task 1 decision gate FAILED, `per_chat` stays disabled, two gaps recorded |
| 2f | `phase-2f-close-position-authority-coverage-gaps.md` | **completed** 2026-08-20, deployed `8122f15` — both gaps closed, per_chat prerequisite now met, `per_chat` still NOT enabled |
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

## Phase 1c — THE BLOCKER IS NAMED

Deployed `93d1dfb` on 2026-08-19 08:17 UTC. It captured a stall stack **twelve
seconds after startup** and 20 distinct stacks over the next 25 minutes.

**Nineteen of twenty point at the same place:**

```
web_app.py:7807   run_deepcoin_execution_reconcile_loop      <- async def, 30 s interval
  → execution_bindings.py:439    reconcile_deepcoin_execution_bindings   <- SYNCHRONOUS, on the loop
  → execution_bindings.py:1040   _apply_reconcile_snapshot
  → ... exchange HTTP, SQLite queries, leg matching ...
```

The innermost frames are spread across the whole routine, not concentrated on
one line — `deepcoin_client.py:598 _request` (the synchronous httpx call),
several `.all()` and `.one_or_none()` SQLAlchemy queries in
`execution_bindings.py`, the leg-matching generator at `:1246`, and
`position_authority_lock.py:17`. The entire reconcile is seconds long, and all
of it runs on the event loop.

**Every unexplained number now has one cause.** The loop's interval is
`interval_seconds: int = 30`.

| Observation | Explanation |
|---|---|
| stalls every 37.36 s | 30 s sleep + 6–10 s blocking |
| durations 6–10 s | exchange round trip + many SQLite queries + Python matching |
| outliers at 15.4 s and 19.7 s | `list_open_orders` on an `httpx` client with a **15 s** timeout |
| rate independent of traffic | it is a timer; production sees 1–16 messages/hour |

Captured `blocked_ms` ranged 3006–7513 across the 20 samples.

## Why three rounds of census missed it

`discover_blocking_calls()` matches a call that is **(a)** named `*_tick` or
`*_once` **and (b)** defined in the same module. `reconcile_deepcoin_execution_bindings`
is neither — it is imported from `execution_bindings` into `web_app`.

**It hits both blind spots at once.** Either condition alone would have caught
it. That is how `KNOWN_BLOCKING_CALLS` reached empty while the loop kept
stalling, and it is why Phase 1b's null result looked like a contradiction
rather than a clue. The census cannot be trusted as a gate until it is widened.

## Two secondary findings, neither pursued

1. **One capture of twenty was not the loop being busy at all.** It showed the
   loop idle in `base_events._run_once` at `self._selector.select(timeout)`
   while 7513 ms of lag accrued. An idle selector cannot be blocking work, so
   that episode is a process-level pause — GC, swap, or CPU starvation.
   One in twenty. Mechanism unknown, recorded not chased.
2. **Two captures were blocked acquiring a `threading.Lock` on the event loop**
   (`position_authority_lock.py:17`). Phase 1 moved the management ticks onto a
   worker thread, so a lock that previously only serialized work the loop was
   already serializing can now be held by that worker while the loop waits.
   Whether Phase 1 made this reachable or it always was is not established here.

## Phase 1c fixed nothing, by design

Production runs `93d1dfb`, which adds the watchdog and changes no trading
behavior. The named blocker is still on the event loop. Rollback target if the
watchdog is ever unwanted: `ee9c0d2`.

## Phase 1d — THE STALLS ARE GONE

Deployed `1c8a7f2` on 2026-08-19 10:07 UTC, measured 62.5 minutes of real
traffic. **Phase 1c's attribution was correct**, and this is the first phase in
the rollout whose falsifiable prediction held.

| | before Phase 1d | after |
|---|---|---|
| **stall rate** | **1 per 37.30 s** | **1 per 1250.15 s** |
| stall count | 176 | 3 |
| p99 | 7464.866 ms | 232.928 ms |
| max | 11027.795 ms | 6470.313 ms |
| worst stall | 12965.977 ms | 6470.313 ms |
| **loop unavailable** | **23.1%** | **2.0%** |
| p50 | 0.872 ms | 0.89 ms |

At the old rate, 3750 s of uptime would have produced about **101** stalls. It
produced **3**. The metronome is gone.

**One criterion is still not fully met, stated plainly.** The phase file
predicted `worst_stall_ms` in tens of milliseconds. It is 6470.313 — halved from
12965.977, but still seconds. What is different from Phases 1 and 1b is that the
residue is no longer a mystery.

## The next blocker was named without a single guess

The Phase 1c watchdog captured one of the three remaining stalls. This is
precisely what it was built for, and it paid for itself here:

```
lifecycle_monitor.py:307          run_loop → await self._run_one_cycle()
lifecycle_monitor.py:617          self._context_resolution_scheduler(...)     ← sync callback
web_app.py:3647                   _schedule_context_resolution_for_app
context_resolution_worker.py:394  schedule_context_reanalysis
                                    → query.order_by(...).all()               ← SQLite, on the loop
```

A SQLAlchemy query running on the event loop, reached through an awaited
coroutine. Recorded, **not** fixed — Phase 1d is scoped to the reconcile loop.

## The census has a third blind spot, and it is structural

Phase 1d closed two blind spots (name-suffix, same-module). This call defeats the
widened matcher anyway: `self._context_resolution_scheduler(...)` is an
`ast.Attribute` call through an instance attribute holding an injected callback,
and the matcher requires `ast.Name`. Static analysis cannot resolve what that
attribute holds at runtime.

**Do not treat a passing census as proof the loop is clean.** It guards against
regressions of the shapes it matches, nothing more. The watchdog is what finds
these — twice now.

## Four real blocking calls surfaced, unowned

Widening the census exposed four calls that were invisible for the whole rollout,
all in the bot command loops, all taking a `session_factory` and querying the
database on the loop:

- `process_system_operator_command`
- `process_system_operator_callback_data`
- `format_holding_positions_message`
- `format_pending_positions_message`

They are **event driven, not timer driven** — they run only when someone sends a
bot command — which is why they never produced metronomic stalls. Recorded in
`KNOWN_BLOCKING_CALLS` with comments. Not fixed. No owning phase.

## Behavior unchanged — the strongest evidence yet

`execution_bindings` shows **155 rows touched** in the 62 minutes after the
restart, so the reconcile is genuinely running and genuinely doing its work from
the worker thread — not silently skipped. Zero `Deepcoin execution reconcile
failed`, zero `reconcile skipped`, zero `worker tick failed` since the restart.

Rollback target: `93d1dfb`.

## Phase 1e — the residual stalls are NOT application code

Deployed `92e6e60` on 2026-08-19 11:29 UTC, measured 62.4 minutes.

| | after Phase 1d | after Phase 1e |
|---|---|---|
| p99 | 236.271 ms | **79.914 ms** |
| loop unavailable | 2.0% | **1.2%** |
| stall count | 3 | 3 |
| worst stall | 6470.313 ms | 9297.432 ms |

**The numeric criterion is not met, for the fourth phase running.** Stalls did
not reach zero and the worst stall got worse. Said plainly, without dressing.

**But what the stalls *are* changed completely.** Both captures have
byte-identical 18-frame stacks whose deepest frame is:

```
base_events.py:1961  _run_once
selectors.py:468     select          ← the loop is IDLE, in the OS epoll wait
```

The event loop was executing **no Python at all** while thousands of
milliseconds of lag accrued. That is not code blocking the loop — it is the
process not being scheduled or not being resident. This signature appeared once
in Phase 1c (1 of 20 captures) and was recorded then as an unexplained minority.
It is now the whole of what remains.

**Caveat that matters:** only 2 of the 3 stalls were captured — the
one-per-60 s limiter dropped the third. The supportable claim is that *every
captured stall is non-code*, not that no application blocking exists anywhere.

## The mechanism is the host, not the code

Measured after the window, so this is correlational, not proven causation:

| | |
|---|---|
| RAM | 1965 MB total, 689 MB free |
| **swap** | **477 MB of 1024 MB in use** |
| **service swapped out** | **VmSwap 21888 kB** (RSS 209476 kB) |
| swap-in activity | `si` 292 and 152 KB/s across consecutive vmstat samples |
| CPU steal | **0** |
| load | 0.60 on 2 cores |

Zero steal and low load rule out host oversubscription and CPU contention.
Active paging on a process with 21.9 MB on disk does not: a page fault stalls
the process in the kernel, where no Python frame can show it — exactly matching
an idle-`select` stack.

**Confirming it needs sampling during a stall, or simply more RAM.** No code
change in this remediation will move it.

## The arc, phases 1 through 1e

Against the Phase 0 production baseline:

| | Phase 0 baseline | now | |
|---|---|---|---|
| p99 | 8777.887 ms | 79.914 ms | **110×** |
| stall rate | 1 per 10.7 s | 1 per 1248.1 s | **117×** |
| loop unavailable | 76% | 1.2% | **63×** |

## Phase 2 — Task 1's decision gate FAILED; per_chat sharding stays dormant

Deployed `3f5ed78` on 2026-08-19 20:18 UTC. This phase's own Task 1 required
answering one question before writing any sharding code: is every exchange
mutation path reachable from `auto_trade_executor` already serialized by
`position_authority_lock`, independent of the message lock this phase
replaces? The phase file was explicit about what a "no" means: *"do not
enable per-chat sharding in this phase. Finish Tasks 2 and 3, leave the flag
disabled, record the gap, and stop."* The answer is no, and this is that
stop.

**Two gaps, both live production code paths, neither hypothetical:**

1. Entry-signal order submission and strategy-revision-replacement
   submission (`recovery_live_submit._submit_recovery_signal_direct`) are
   serialized by `_source_execution_lock`, a *different* lock object
   (`source_message_deletion.py`), not `position_authority_lock`.
2. Composite management batch execution
   (`strategy_management_composite_executor.execute_composite_management_batch`
   — the "management v2" close/revise/partial-close path) is serialized by
   **nothing**. `position_mutation_gateway.py`, which both this path and the
   covered ones eventually call, provides a DB-backed intent state machine
   for idempotency, not mutual exclusion of its own.

Three other leaves genuinely are covered — `cancel_revision_entry_leg`,
`execute_management_batch`, `reconcile_deepcoin_execution_bindings` — which
is what makes this a real gap rather than a total absence of the pattern
this phase depends on.

**Why this is not an active incident.** The single process-wide message lock
this phase exists to replace is, today, an accidental second boundary: it
guarantees only one message is ever in flight anywhere, so gap 1 and gap 2
can never actually race each other in production right now. Enabling
`per_chat` would remove exactly that accidental protection — a chat A entry
submission (under `_source_execution_lock`) could then run concurrently with
a chat B composite management close (under nothing) — which is precisely the
scenario Task 1 exists to rule out before sharding ships live.

**What shipped anyway, per the phase file's own instruction:** Tasks 2, 3 and
4 — `KeyedAsyncLockRegistry`, `MessageLockProvider`, the `message_lock_mode`
flag (default and required value: `"global"`), and the chat-isolation tests
proving the mechanism works correctly in isolation. All of it is dormant.
`message_lock_mode` reads `"global"` in production, confirmed over ssh after
deploy, and every code path in `global` mode is proven byte-for-byte
identical to pre-Phase-2 behavior (see the chat-isolation test suite).
Deploying this dormant is safe regardless of the gap, because nothing it adds
can execute unless the flag is flipped — and flipping it is exactly what
Task 1 says not to do yet.

**Completion criteria, read honestly:** "Task 1's boundary verification
passed, or its gap is recorded and the flag was deliberately left disabled"
— met, via the second branch. "Chat isolation and same-chat ordering tests
pass" — met. "`per_chat` enabled in production, with the disable path proven
working beforehand" and "one full trading session observed with no new
incident class" — **not met, and not attempted**, because the phase file's
own Task 1 forbids attempting them under these findings.

**Rollback**, if ever needed even though nothing is enabled: redeploy
`92e6e60a0985a81208064f785e2454bcafd99bfe` (the pre-Phase-2 production
commit) with the same updater command. No schema change, no persisted state
beyond the new `message_lock_mode` settings key, which defaults to `"global"`
on any row that predates it.

**Opening a gap-closing phase is the user's call, not a session's to assume.**
Closing gap 1 means either routing `_submit_recovery_signal_direct` through
`position_authority_lock` in addition to `_source_execution_lock`, or making
a documented case that the two locks together provide equivalent exclusion —
which, per the Task 1 trace, they do not today. Closing gap 2 means adding
`position_authority_lock` coverage to
`execute_composite_management_batch`, following the same pattern
`execute_management_batch` already uses. Until one or both close,
`message_lock_mode` must stay `"global"` and Phase 2's remaining Task 6 steps
(prove the disable path, enable `per_chat`, observe a trading session) stay
un-started.

## Phase 2f — both position-authority coverage gaps closed

Deployed `8122f15` on 2026-08-20 00:39 UTC. The user was asked directly
whether to open a dedicated phase to close Phase 2's two gaps before
resuming the original phase sequence, or defer it; they chose to close it
first. This phase is that closing work, and nothing else — `message_lock_mode`
is untouched and still reads `"global"` in production.

**What changed, in one line each.** Gap A: `_submit_recovery_signal_direct`
(`recovery_live_submit.py`) gained `@serialized_position_authority_mutation`
as its outermost decorator, stacked above the pre-existing
`@serialized_source_message_execution`. Gap B:
`execute_composite_management_batch`
(`strategy_management_composite_executor.py`) gained the same decorator,
following the exact pattern `execute_management_batch` already used. Neither
function's logic changed — only when mutual exclusion is held.

**The lock-ordering question was checked, not assumed.** Stacking two
different `RLock`-backed decorators only avoids deadlock if every caller
that holds one never acquires the other in the opposite order somewhere
else. Before writing gap A's fix, the codebase was searched for existing
precedent: `deepcoin_execution_actions.recreate_trigger_entry_tpsl` already
stacks `@serialized_position_authority_mutation` directly above
`@serialized_source_message_execution` — position lock outer, source-execution
lock inner — and has run in production through every prior phase with no
reported deadlock. Gap A's fix uses the identical order, so it adds a second
call site to an already-safe pattern rather than inventing a new one. Gap B
never touches `_source_execution_lock` at all, so no ordering question
applies there.

**Coverage is proven, not asserted.** Each fix has a thread-based test that
holds `position_authority_lock` from a separate thread and shows the
now-covered function genuinely cannot proceed — cannot reach its exchange
write (gap A), cannot even load its own batch (gap B) — until that lock is
released. `tests/test_position_authority_boundary_coverage.py`, the
architecture test Phase 2 wrote for exactly this purpose, now has an empty
`KNOWN_UNCOVERED_LEAVES` and an explicit
`test_per_chat_sharding_prerequisite_gap_is_closed` in place of the old
"gate not met" assertion.

**One session, one interruption, independently re-verified.** The session
executing this phase committed its claim, then Tasks 0-3, then ran the full
suite (5713 passed, 1 skipped) — and was cut off by an API session-limit
error immediately before its own commit for Tasks 1-3. The next session that
picked this up did not trust that state on faith: it re-read every diff,
independently re-ran the full suite (same result, 5713/1/0), then completed
the commit and deploy. Both runs agree, and the numbers match what the
interrupted session reported before it stopped.

**What this phase does NOT do.** It does not enable `per_chat`. It does not
run Phase 2's Task 6 Steps 2 (prove the disable path) or 3 (enable and
observe one full trading session with real multi-group traffic) — those
still require their own session, deliberately, per the original instruction
that enabling the flag must be asked about separately from shipping the
infrastructure that makes it safe to ask about. What this phase does
establish is that the question can now honestly be asked: Phase 2's Task 1
decision gate, re-run, passes.

**Rollback**, if ever needed: redeploy `3f5ed78096f33d5dda59400a3a90dcf9bcb9c4cd`
(the pre-Phase-2f production commit) with the same updater command. No
schema change, no persisted state.

## Handoff note for the Phase 2 session (superseded — kept for context)

Phases 0, 1, 1b, 1c, 1d and 1e are complete and deployed. Production runs
`92e6e60`, verified over ssh, not inferred from git.

**Phase 2 is a different kind of change from everything before it.** Phases 1
through 1e were strictly threading moves — "changes threading, never
concurrency" was the sentence that made each of them low risk. Phase 2 genuinely
introduces parallelism that did not exist, on a path that reaches order
submission. Its safety comes from a different place: it ships behind the
`message_lock_mode` trading setting, deploys dormant, and rolls back with a
settings flip that needs no deploy and no restart.

Do not enable the flag in the same step as the deploy. Deploy dormant, confirm
the service is healthy, then enable deliberately.

Phase 2's Task 1 asks whether the position authority boundary really covers
exchange mutation. There is already evidence for that question: two Phase 1c
stall captures were blocked at `position_authority_lock.py:17`, a
`threading.Lock` taken on the event loop. Start from that, not from scratch.

## Before Phase 2 starts — read this (superseded — kept for context)

Points 1 and 2 below replace the Phase 1 version of this section, which was
wrong about the updater's interface. See "the procedure docs are wrong about the
updater" above. Point 5 below (the reconcile-loop blocker) was resolved by
Phases 1c/1d before Phase 2 started; it is stale, kept for the historical
record only. For the current state, see "Before Phase 3 starts" below.

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
4. **Phase 2's prerequisite is met.** It says: "Do not attempt this phase while
   the event loop can still be blocked." The loop is now unavailable 1.2% of
   wall clock with p99 at 79.9 ms, and every captured residual stall is the
   process idle in `select`, not code. There is no longer an application-level
   blocker that could be confused with Phase 2's effect.
   One thing to carry in: the residual stalls look like host memory pressure,
   so Phase 2's measurement should expect a small non-zero floor that its own
   changes cannot move.
5. **The blocker is now named, and fixing it has no phase and no owner.**
   `run_deepcoin_execution_reconcile_loop` at `web_app.py:7807` calls
   `reconcile_deepcoin_execution_bindings` synchronously on the loop every 30
   seconds. Opening the phase that fixes it is the user's decision, not
   something the next session may help itself to. Note the shape is identical to
   Phase 1's, and the same design constraint applies: this loop reaches the same
   execution-binding and protection state as the management ticks, so it belongs
   on the **existing shared `mgmt-worker` executor**, not a new one.
6. **Widen the census before trusting it again.** It matched only same-module
   `*_tick`/`*_once` calls and therefore reported zero offenders while this one
   ran every 30 seconds.

The authoritative checkout for this rollout is the worktree
`.worktrees/runtime-serialization` on branch `codex/phase0-deploy-integration`,
which is what gets pushed to `deploy_branch`. `codex/mimo-v1-baseline` is
superseded — its copy of this file is frozen and says so.

The census in `tests/test_runtime_event_loop_blocking_census.py` asserts
equality, so any phase that removes a blocking call must shrink
`KNOWN_BLOCKING_CALLS` in the same commit or the suite fails.

## Before Phase 3 starts — read this

1. **`message_lock_mode` stays `"global"`. Do not flip it as part of Phase 3
   or any unrelated work.** Phase 2's Task 1 found two exchange-mutation
   paths not covered by `position_authority_lock`; Phase 2f
   (`phase-2f-close-position-authority-coverage-gaps.md`, deployed `8122f15`)
   closed both, so the *prerequisite* for `per_chat` is now met — but
   enabling it (Phase 2's own Task 6 Steps 2-3: prove the disable path,
   enable and observe a full trading session with real multi-group traffic)
   was deliberately not attempted and has no owning phase yet. Opening that
   is the user's decision — ask, do not assume either that they want it
   opened next or that it should wait.
2. **Phase 3 is unrelated to Phase 2/2f** (compensation-window-repair, per
   the index doc) and can proceed independently regardless of whether
   `per_chat` is ever enabled.
3. **Deployment mechanics are unchanged from Phase 2** — see "There is no
   change class" above and "Before Phase 2 starts" for the still-accurate
   points (1-3: no `-ChangeClass`, no PowerShell on this workstation, run
   the deploy script from a checkout of the exact commit).
4. **The authoritative checkout is still**
   `.worktrees/runtime-serialization` on branch `codex/phase0-deploy-integration`.
   `codex/mimo-v1-baseline`'s copy of this file remains frozen and superseded.

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
