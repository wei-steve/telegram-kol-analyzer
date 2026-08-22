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
current_phase: 6r
phase_name: semantic-review-control
phase_status: in_progress          # planned | claimed | in_progress | completed
claimed_by: null
current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-6r-semantic-review-control.md
last_completed_phase: 5   # the sequence ran 0, 1, 1b, 1c, 1d, 1e, 2, 2f, 3, 4, 5
last_completed_commit: 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8
phase_6a_planning_claim_commit: 7f266f43844b15648f6d013121f1efe3271a137d
phase_6a_implementation_claim_commit: 86f2328b9c89fcbdb5ee57fea289ffd773cb0784
phase_6a_design_commit: d04dc7d47cdb44fb6f36ada439c38e6c108b88c4
phase_6a_plan_commit: e93b13b253ff0191309b478182d11d4da38d0e9c
phase_6a_compat_candidate: f257a93121ba1d547955f0b4dd5a270dd347904d
phase_6a_deployed_commit: f257a93121ba1d547955f0b4dd5a270dd347904d
phase_6a_safe_live_mode: shadow
phase_6a_completed_tasks: "Tasks 0-11 complete; Task 12 completed dormant deploy and shadow enable, then stopped before the required real sync parity sample. Tasks 13-15 completion path, queue cutover, hardened Candidate B, restart drill, and L2 observation remain outstanding."
phase_6a_stop_reason: "FAILED CLOSED 2026-08-22: a real shadow sync sample is mandatory before queue cutover, but read-only production evidence found 2463 pending position-attribution notifications and 331 pending position-protection notifications. Calling the existing sync route would preserve its current notification semantics and could send extra Telegram messages during active work, which AGENTS.md forbids. No sync was manufactured, queue was not enabled, Candidate B was not built, and Phase 6 remains blocked."
phase_6a_local_focused_tests: "Candidate A focused acceptance: 123 passed, 451 deselected in 10.11s. Dedicated architecture baseline: 1 passed, 1 strict-xfailed in 2.32s as required for the compatibility candidate. The one asset-harness regression exposed by the first full suite was fixed and both the exact failed test and Task 7 focused slice passed."
phase_6a_local_full_suite: "Final Candidate A at f257a93121ba1d547955f0b4dd5a270dd347904d: 5995 passed, 1 skipped, 1 expected xfailed, 17 known warnings, 456.85s. The preceding invalid candidate run found one JS test-harness slice regression after 5994 passes; production JS was then changed only to move the idempotency helper declaration, affected focused tests passed, and this new final full suite was run exactly once."
phase_6a_l3_rehearsal: "/opt/telegram-kol-analyzer/data/backups/phase6a-f257a93121ba1d547955f0b4dd5a270dd347904d-20260822T080840Z/rehearsal-summary.json — passed=true; online backup 598310912 bytes; quick_check ok on backup, migration before/after repeated bootstrap, and rollback; only worker_command_jobs plus four expected indexes added; new table count 0; existing counts and 13 critical-table hashes unchanged; repeated bootstrap schema/count/hash delta false; physical rollback schema/count/hash exact."
phase_6a_deployment_status: "Candidate A fast-forward pushed eef0e53..f257a93 and gated-deployed with exact EXPECTED_COMMIT. Production HEAD exact f257a93121ba1d547955f0b4dd5a270dd347904d, telegram-kol.service active since 2026-08-22 16:17:07 CST, quick_check ok, worker table present with four indexes and zero rows. Updater backup: /opt/telegram-kol-analyzer/data/backups/research-deployment-f257a93121ba1d547955f0b4dd5a270dd347904d-20260822T081603Z.db."
phase_6a_runtime_modes: "message_lock_mode=global, message_pipeline_mode=queue, worker_command_mode=shadow; monolith topology unchanged; no Phase 6 systemd/process separation started."
phase_6a_worker_state_at_stop: "worker total=0, claimed=0, executing=0, uncertain=0; deployment-window journal grep found SQLITE_BUSY/database-is-locked/worker-command anomaly count 0. No shadow command sample exists because the mandatory real sync was not safe to invoke."
phase_6a_rollback_status: "Rollback preconditions are currently satisfied (claimed=0, executing=0). Safe operational rollback is the settings API switch shadow -> inline at exact Candidate A; no code rollback is required for the current migration-compatible candidate. The exact Candidate A SHA remains the future Candidate B rollback target. Schema is retained during operational rollback; physical removal was rehearsal-only and is not authorized on production."
phase_6a_outstanding: "Resolve or explicitly authorize handling of the pre-existing pending notification outboxes, then obtain one safe real sync shadow parity sample without manufacturing an exchange write. Re-run the quiet gate, prove shadow parity/no duplicate, cut to queue, run fake-adapter crash rehearsal and one zero-inflight production restart, then complete Candidate B authority hardening, its focused/final full suite, exact gated deploy, and L2 30-minute/5-message observation."
phase_6a_pause_status: "USER-AUTHORIZED PAUSE 2026-08-22: preserve exact deployed Candidate A f257a93121ba1d547955f0b4dd5a270dd347904d, worker_command_mode=shadow, and the Task 12 real-sync parity blocker. Do not advance, modify, roll back, or reinterpret Phase 6A while Phase 6R is current. After Phase 6R completes, restore this exact Phase 6A checkpoint; the 2463/331 pending-notification gate remains independent and unresolved."
phase_6r_authorization: "USER-APPROVED 2026-08-22: insert a prerequisite semantic-review-control phase, initially documentation-only. Preserve MiMo authority and all recognition, context resolution, strategy, position ownership, automation, and exchange-write semantics. Control only semantic_disagreement_review; do not disable other DeepSeek consumers. Add a runtime switch defaulting off, project disabled reviews as an explicit review_disabled state backed by compatibility terminal comparison_status=completed, and design a guarded L3 terminalization of historical pending/failed reviews."
phase_6r_planning_scope: "This claim authorizes only the design document, self-contained implementation plan, status bookkeeping, and their commits. No production/test code, database, runtime setting, provider configuration, key rotation, push, deployment, restart, notification-outbox handling, Telegram message, or exchange operation is authorized."
phase_6r_planning_claim_commit: 1c598528dcff68902c0433314840e859c6b595b2
phase_6r_design_commit: edce98caf0394ef0d1b23f147b79f25b47d6c535
phase_6r_plan_commit: 50a04b6e14324c882a9c0107ab5bf168b90c355b
phase_6r_planning_status: "PLANNED ONLY. The owner approved the default-off semantic-review control, compatibility completed/review_disabled projection, isolated DeepSeek-review scope, guarded historical pending/failed terminalization, runtime race policy, TDD sequence, L3 copy/apply/rollback rehearsal, exact deployment gates, and L2 observation. No production/test code, database, runtime setting, provider config, credentials, push, deployment, restart, Telegram outbox, notification, or exchange state was changed."
phase_6r_implementation_authorization: "USER-APPROVED 2026-08-22: implement Phase 6R according to its canonical plan. This did not itself confirm that the previously exposed DeepSeek, GLM, and MiMo keys were rotated, so the plan's explicit pre-push/deploy stop gate still applies."
phase_6r_implementation_claim_commit: 4860cb3ecc136ccfc5fb6556d523bbd345b4dc0e
phase_6r_rehearsal_reclaim_commit: b7bb12af1e342d5a08f927dc74458d0fd5ce72f5
phase_6r_fingerprint_repair_claim_commit: 1f4b81564e89e1b16f61b36dc2f2df1f215c66fc
phase_6r_key_rotation_confirmation: "OWNER-CONFIRMED 2026-08-22 in Codex after the explicit DeepSeek/GLM/MiMo rotation question. No secret or key material was requested or recorded."
phase_6r_local_candidate: 5e3cb630dd6c5e899c00ee8669fcd10b6ee3fbca
phase_6r_code_commits: "0569c416c7a81afdcc2e540129b44e48ec896da0 characterization; 7d8556929316c3ea920c036071473cc82f5ff355 default-off setting/UI; 9246a0a68c13a58cd7a5c608dd4a9fda8e3cb104 authority finalization; 402c05aff14481b7ad4694cbc2ed367e6061ebc9 worker gates/races; 98dca0a49679bcb61806733941baaa4592134fd8 disabled projection; 65ff1d06f42c956b30c335e6c23218df9d3bf6ef guarded transition/rollback; fcc8d56595e6031e3d51372563ac71668d357143 database-identity and repeated-apply hardening; 9df289d5bda4c44c1dbc135f8b3e4a4f45bd8bf3 read-only-default CLI; 5e3cb630dd6c5e899c00ee8669fcd10b6ee3fbca UTC-naive persisted timestamp fingerprint normalization after failed rehearsal."
phase_6r_scope: "Control only semantic_disagreement_review. Add semantic_review_enabled=false, preserve MiMo authority and all recognition/context/strategy/position/automation/exchange semantics, avoid provider config load/claim/retry/notification while disabled, and expose logical review_disabled using physical comparison_status=completed plus agreement_status=review_disabled. Other DeepSeek consumers remain unchanged."
phase_6r_verification_requirement: "Focused TDD for setting, authority finalization, worker gating/races, projection/downstream terminality, and dry-run/CAS/rollback CLI; one final full suite per final production candidate; L3 online-backup rehearsal on production copies with quick_check, counts, targeted hashes, repeated apply and exact targeted rollback; key rotation confirmation before deploy; exact-SHA gated deploy; then 30 continuous minutes and at least five real messages when available, with semantic provider/402/retry/notification deltas and unchanged runtime/trading semantics."
phase_6r_local_tests: "Original focused acceptance passed as previously recorded. Fingerprint repair TDD: the new UTC-aware apply / SQLite reload / unchanged repeated-apply test failed with unequal SHA before production code changed; after UTC-naive canonicalization, semantic control plus CLI focused tests passed 16/16. Final Candidate 5e3cb630dd6c5e899c00ee8669fcd10b6ee3fbca full suite ran exactly once: 6032 passed, 1 skipped, 1 expected xfailed, 17 known warnings, 461.15s; git diff --check passed."
phase_6r_l3_rehearsal: "PASSED on a fresh production online-backup set; the earlier failed b7bb12a evidence remains preserved separately. Canonical evidence: /opt/telegram-kol-analyzer/data/backups/phase6r-5e3cb63-20260822T115727Z/rehearsal-summary.json, passed=true. Initial four-copy SHA-256 aabacaed9962584b8b949bda91453e98c2ad4163bb00df34b4fc7a9e8787e062; 611553280 bytes; 82 table counts; 13 critical hashes; quick_check=ok on immutable/apply/repeat/rollback; 127 failed targets and running=0. Apply changed exactly 127; repeated original apply changed 0 with byte-identical post SHA a3ee0a01bbc631ab5ab0c4bf070ae253e02ac02bacc5a558380736270c3a331d; fresh replan target 0. Targeted rollback restored 127 exact preimages; all table counts/schema and all 13 critical hashes matched immutable. Only semantic_review_enabled missing->false and exact planned decision rows changed on apply; external counts stayed zero."
phase_6r_integration_status: "Fast-forward pushed through exact Candidate 5e3cb630dd6c5e899c00ee8669fcd10b6ee3fbca to origin/codex/deepcoin-auto-trading-v1 without force after ancestry verification. Server detached rehearsal source /tmp/phase6r-5e3cb63 matched the candidate; production was changed only later by the gated updater."
phase_6r_deployment_status: "Exact-SHA gated updater exit 0; production HEAD=5e3cb630dd6c5e899c00ee8669fcd10b6ee3fbca, telegram-kol.service active since 2026-08-22T12:08:57Z. semantic_review_enabled=false persisted through the normal settings API; message_lock_mode=global, message_pipeline_mode=queue, worker_command_mode=shadow; monolith plus existing read-only sidecars unchanged; quick_check=ok, active_write_count=0, loop stall_count=0. Deployment introduced no schema change; live preapply online backup is /opt/telegram-kol-analyzer/data/backups/phase6r-5e3cb63-20260822T115727Z/live-preapply.db, SHA-256 1eb19fa031aacc4297ddc8a1bc15bdccc8235db452cdbb1af6ecce7f8d6c1b22."
phase_6r_provider_stop_proof: "/opt/telegram-kol-analyzer/data/backups/phase6r-5e3cb63-20260822T115727Z/semantic-stop-proof.json passed over 2026-08-22T12:10:21Z..12:12:23Z: semantic feature invocation/max-id/402/attempt/running/notification deltas all zero; other message_recognition and strategy_alert 402 deltas also zero."
phase_6r_live_apply: "Fresh live plan SHA 39668891e85716fd4f180accbcf0113032ec983a7d4f47bf8a4cb370c8c6b9f6 targeted 128 failed rows with running=0 and quick_check=ok. Exact apply changed 128, used one timestamp, preserved every non-transition field, added/deleted no RecognitionDecision row, returned external counts 0/0/0 and post SHA 9a2c77bb55eeab236dbb67cb53064481441f16f9430fb68164b5be9577cc16fe. Immediate live state: pending/failed/running eligible=0, review_disabled=129, quick_check=ok. Target-row proof is in live-apply-verification.json."
phase_6r_live_evidence_substitution_authorization: "OWNER-APPROVED 2026-08-22: 接受归因后的并发漂移证据替代，批准继续 L2 观察. This accepts live-unrelated-drift.json plus exact target-row CAS proof as the substitute only for the impossible whole-critical-table unchanged-hash check under concurrent production writers. It does not waive traffic, review/provider, SQLite_BUSY, duplicate/backlog, topology, exchange, rollback, or any other Phase 6R/L2 gate."
phase_6r_l2_observation_claim_commit: 4573f837795ad7c7aa94b0e770f82a13ea75d1a7
phase_6r_observation_status: "FAILED CLOSED for insufficient natural traffic. The quiet monitor ran 2026-08-22T12:33:11.922124Z..13:03:11.937994Z for 1819.2 seconds, but received only four real Telegram messages from one chat; L2 requires at least five and tries for two chats, and the window was not extended or backfilled. Evidence: /opt/telegram-kol-analyzer/data/backups/phase6r-5e3cb63-20260822T115727Z/l2-observation.json, l2-journal-summary.json, l2-journal.log, and l2-stop-state.json. Therefore no Phase 6R completion claim is made."
phase_6r_observation_mimo: "All four in-window messages produced exactly one decision and one durable message job, with no duplicate or missing decision/job. MiMo remained the sole authoritative model (mimo-v2.5); all four outcomes were preserved authoritative_failed / automation skipped:mimo_authoritative_failed / comparison completed with zero comparison attempts, so this low-traffic window produced no new successful review_disabled decision. The ordinary message_recognition feature made 23 completed MiMo invocations with 402 delta zero; the four in-window jobs ended as three failed and one claimed snapshot before the last claim settled. These are observed existing recognition semantics, not changed or repaired by Phase 6R."
phase_6r_observation_runtime: "Candidate HEAD and active service stayed exact; semantic_review_enabled=false and global/queue/shadow stayed unchanged; monolith plus the same two read-only sidecars and their PIDs stayed unchanged. Semantic-review invocation/max-id/402/comparison-attempt/critical-notification deltas were all zero and running/eligible stayed zero. Total recognition notification-status rows increased by nine, including the four authoritative-failure notifications; semantic critical notification delta was zero. Message-job status moved expired 73->78, failed 193->197, pending 11->5, succeeded 433->433, and claimed 0->1 at the window endpoint; worker-command jobs remained zero. WAL and quick_check=ok held at both endpoints; journal SQLITE_BUSY=0, database-is-locked=0, queue-duplicate=0, semantic-review=0, HTTP 402=0, and loop stall_count stayed 1 with no new stall. active_write_count=0 at both endpoints, no execution event or trade signal occurred in-window, so direct exchange history was not applicable and no exchange query/write was manufactured."
phase_6r_rollback_status: "Rollback was not applied. Stop-state evidence at 2026-08-22T13:05:48.711803Z shows production Candidate 5e3cb63 active, semantic_review_enabled=false, global/queue/shadow, WAL, quick_check=ok, active_write_count=0, eligible/running review backlog zero, review_disabled=131, worker-command jobs zero, and semantic provider counters unchanged at 6931 invocations/max id 14389/402 count 360. The exact 128-row targeted rollback remains retained and evaluable at live rollback plan SHA 10c53b7e573f23795495701dc7c74edd03a69d70b9a9b0fa42fdf388412be035; applying it would recreate the failed review backlog and remains unauthorized."
phase_6r_outstanding: "Run a new, separately authorized fresh L2 window for 30 continuous minutes; it must receive at least five real messages and should cover two chats, without manufacturing traffic. The completed window also had four MiMo authoritative_failed outcomes and therefore supplied no new successful review_disabled projection; capture the normal successful MiMo path if natural traffic provides one, while preserving the failures as authoritative evidence rather than changing semantics. Until the traffic gate passes, Phase 6R remains in_progress at safe live Candidate 5e3cb63 and Phase 6A remains paused at its exact Task 12 plus 2463/331 blocker."
phase_6a_planning_status: "PLANNED ONLY. The owner approved the durable SQLite worker-command design and the complete TDD/L3 implementation plan. No production code, database, setting, GitHub branch, server, systemd unit, or exchange state was changed. A future implementation turn must run the full exclusive preflight, claim Phase 6A, and execute only its current phase file."
phase_6a_scope: "Move exactly four Web authority routes behind worker_command_jobs: /api/execution/sync-deepcoin, /api/execution/close-bound-position, /api/recovery-live-submit, and /api/trade-signals/process-next. Preserve existing HTTP success/error contracts and all recognition, strategy, position ownership, execution, and exchange-write semantics. Keep message_lock_mode=global, message_pipeline_mode=queue, and the monolith topology until Phase 6 resumes."
phase_6a_verification_requirement: "L3 schema/bootstrap and physical rollback rehearsal on production database copies, plus focused concurrency/recovery/uncertain/idempotency tests, one full suite per final production candidate, compatibility shadow/queue evidence, a hardened no-Web-authority candidate, gated deployment, one lifecycle restart, L2 30-minute/5-message observation, direct exchange history for observed write-capable commands, and an exact Candidate A rollback SHA. Missing or incomplete evidence fails closed."
phase_6_queue_soak_original_requirement_hours: 168
phase_6_queue_soak_user_accepted_minimum_hours: 37
phase_6_queue_soak_duration_override: "USER-APPROVED 2026-08-22: the owner accepts 37 hours of stable global/queue production operation as sufficient and waives only the phase file's one-week waiting duration so Phase 6 may be claimed after normal exclusive preflight. At the decision timestamp 2026-08-22T06:19:13Z, 37h08m44s had elapsed since the 2026-08-20T17:10:28.789382Z queue cutover."
phase_6_queue_soak_override_scope: "duration only. Do not waive Phase 6 Task 1 SQLite concurrency, process authority, or Telegram session decision gates; the safe deployment window, L2 verification, rollback, recognition/strategy/position/execution semantic invariants, message_lock_mode=global, and message_pipeline_mode=queue all remain mandatory."
phase_6_claim_commit: c50c264e102c567d792481eed29052760f7f51ef
phase_6_task_1_test_commit: 0a7ba6f73342f36c37d15b8c9b7bf75bd5fd1a73
phase_6_task_1_sqlite_gate: passed
phase_6_task_1_sqlite_evidence: "/opt/telegram-kol-analyzer/data/backups/phase6-task1-sqlite-load-c50c264.json"
phase_6_task_1_sqlite_result: "Production online-backup copy, 592166912 bytes, quick_check ok after the load. Three independent WAL writer processes with busy_timeout=30000 completed 2400/2400 sustained above-production transactions and 3000/3000 unthrottled burst transactions with SQLITE_BUSY=0 and no other errors. Sustained wait p50/p95/p99/max was 0.033/8.275/8.584/131.248 ms; burst wait p50/p95/p99/max was 0.007/0.015/0.025/1029.862 ms. The 30 second timeout is adequate for the measured profile. The production database itself was never written."
phase_6_task_1_authority_gate: failed
phase_6_task_1_authority_blockers: "Focused static architecture trace found four Web routes that still reach worker-only position authority: /api/execution/sync-deepcoin -> reconcile_deepcoin_execution_bindings; /api/execution/close-bound-position -> close_bound_position_market; /api/recovery-live-submit -> submit_recovery_order_live -> process_trade_signal_live -> execute_deepcoin_management_signal; /api/trade-signals/process-next -> process_next_trade_signal_live -> process_trade_signal_live -> execute_deepcoin_management_signal. These are not approved allowlist exceptions: they must move behind worker-owned jobs in a separately designed prerequisite before process separation can safely proceed."
phase_6_task_1_telegram_session_gate: "not run: Task 1 requires stopping on the first failed decision gate, so Step 3 was deliberately not advanced after the Web authority failure"
phase_6_local_full_suite: "not run: no production-code candidate exists and Task 1 failed before Task 2; running the final full suite here would violate the one-final-candidate verification rule"
phase_6_deployment_status: "not attempted; no push, gated updater run, unit installation, service restart, topology switch, or production observation window"
phase_6_topology_status: "unchanged: telegram-kol.service remains the single active production unit; no split units were installed or enabled"
phase_6_isolation_test: "not run because the authority gate failed before any split"
phase_6_rollback_status: "not applicable: production code, settings, units, and topology were unchanged"
phase_6_outstanding: "Phase 6A is implemented through Candidate A shadow deployment but remains in_progress at its mandatory real-sync parity gate. Complete Phase 6A first; then re-run the Phase 6 Task 1 authority gate and only if it passes continue with the Telegram session gate and Tasks 2-8. message_lock_mode must remain global and message_pipeline_mode must remain queue."
phase_5_claim_commit: ba975ff1e82ab4d1da2b45ecc09ccf48cb24f261
phase_5_code_commit: eaaa255f95f6c0889c86db5b674e458f3e2e5e56
phase_5_deployed_commit: 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8
phase_5_blocker_recovery_plan_commit: b77be48658ea3efffc7f568a9f2773283672cc04
phase_5_blocker_recovery_code_commit: a2ae02f4f9012e4901b3c06d2221917efeabb882
phase_5_blocker_recovery_deployed_commit: 81cb75ed1c1bcd718eada69dde189293ea2a921c
phase_5_queue_rollback_fix_commit: 77ec2149d4dbe6f54af7571a2c9756d2d3532fed
phase_5_queue_rollback_fix_deployed_commit: 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8
phase_5_queue_cutover_at_utc: "2026-08-20T17:10:28.789382Z"
phase_5_midtraffic_restart_at_utc: "2026-08-20T17:24:32Z"
phase_5_observation_end_at_utc: "2026-08-20T18:12:54.876237Z"
phase_5_observation_seconds: 3746.086855
phase_5_final_evidence: "/opt/telegram-kol-analyzer/data/backups/phase5-full-session-observation-7a54411.json"
phase_5_blocker_recovery_focused_tests: 126   # 126 passed, 0 failed in 9.99s
phase_5_blocker_recovery_local_suite: 5790   # 5789 passed, 1 skipped, 0 failed, 17 known warnings, 429.12s
phase_5_queue_rollback_fix_focused_tests: 51   # 51 passed, 0 failed in 4.65s
phase_5_queue_rollback_fix_local_suite: 5792   # 5791 passed, 1 skipped, 0 failed, 17 known warnings, 429.13s
phase_5_local_suite_after_task_1: 5763   # 5762 passed, 1 skipped, 0 failed; pure extraction gate before consumer work
phase_5_local_suite_after: 5779   # 5778 passed, 1 skipped, 0 failed, 17 known warnings, 440.50s
phase_5_message_lock_mode_unchanged: true   # production remains global
phase_5_pipeline_mode_final: queue   # enabled 2026-08-20 17:10:28 UTC after the repaired rollback-boundary proof
phase_5_rollout_status: "COMPLETED. Production is global/queue at exact deployed code commit 7a54411. The repaired real shadow -> queue -> shadow boundary drill passed with one authority and one decision at each in-flight transition. Queue was enabled through a double-read quiet-window gate. A deliberate gated restart while job 200/raw 11961 was claimed proved stale-claim resume: claimed_at advanced 17:24:13 -> 17:29:13 with stale_claim_reclaimed, then the job succeeded with one decision and zero execution events. The final 62m26s real-traffic window processed raw 11960-11967 from two chats as eight queue jobs, eight succeeded jobs, and eight decisions, with missing/orphan/stuck all zero, no pending backlog, loop stall_count zero, and no duplicate submission in internal events or directly queried exchange history."
phase_5_outstanding: "none. Phase 6 is only the next-phase pointer; it was not read or implemented in this session."
phase_5_blocker_diagnosis: "Resolved 2026-08-20 16:25:58 UTC. Read-only production checks first proved the historical race: batch 119 remained reconciling and rewrote updated_at every ~30 seconds; leg 103 had no client/exchange id, request, response, close intent, or close execution event. Deployed fix 81cb75e froze it recovery_required/management_close_submission_identity_missing. Recovery dry-run on the rehearsal copy and production matched evidence fingerprint 326f060ea379b57ce5d8be4eeeb572159e135af60611770fbac09f6c6d93a040 and source fingerprint 4dd05f60e53615bc1e32e988a87519f0b9c58aae82c9074ad6db9d2ddb466bdd. One production apply resolved the batch history_no_submission_confirmed, failed leg 103, and wrote audit event 3643; positions, regular orders, and all 12 pending trigger ids were unchanged."
phase_5_blocker_recovery_rehearsal: "Preserved /opt/telegram-kol-analyzer/data/backups/phase5-batch119-recovery-backup-81cb75e-20260820T162200Z.db and separate rehearsal copy phase5-batch119-recovery-rehearsal-81cb75e-20260820T162200Z.db. Evidence JSON before/after and CLI dry-run/apply outputs remain in the same directory. quick_check ok before/after; 81 table snapshots matched backup to rehearsal before apply. After rehearsal only strategy_management_batches and strategy_management_legs hashes changed and execution_events count changed 3642 -> 3643; all other table hashes/counts were identical."
phase_5_migration_rehearsal: "not applicable: Phase 5 changes no model, db bootstrap, or migration file; the Phase 4 message_processing_jobs schema was already rehearsed, backed up, migrated, and observed in production. The gated updater auto-detected no schema delta for eaaa255, so it performed no migration."
phase_4_claim_commit: cc2bf45e1799ec16b9e974ad78fd23e78309a0c9
phase_4_initial_code_commit: dae09efc699c94261c4245f88f4f7523d8ca667c
phase_4_history_enqueue_fix_commit: 6527c08fda70a5500cf747072288c499a1fa2f4d
phase_4_terminal_tracking_fix_commit: 3bd53553af51ba4619ed3703bade2028514af4b6
phase_4_deployed_commit: 3bd53553af51ba4619ed3703bade2028514af4b6
phase_4_local_suite_after: 5762   # 5761 passed, 1 skipped, 0 failed, 17 known warnings, 417.14s
phase_4_message_lock_mode_unchanged: true   # production remained global before, during, and after every Phase 4 deploy and shadow observation
phase_4_pipeline_mode_final: shadow   # default remains inline; the previously proven shadow -> inline off switch remains available without restart
phase_4_shadow_watermark: 11779
phase_4_shadow_window_utc: "2026-08-20T07:56:51Z through 2026-08-20T08:58:36Z (61m45s), including gated restart at 07:57 UTC"
phase_4_shadow_window_traffic: "36 raw messages across 10 distinct chats; 36 shadow jobs; 36 recognition decisions; execution_events 3629 -> 3632"
phase_4_final_parity: "missing_job_count=0, orphan_job_count=0, stuck_pending_count=0, pending=0, succeeded=28, failed=8"
phase_4_real_inline_finding: "All 8 failed jobs were terminalized as history_reconcile_error:MultipleResultsFound and all 8 already had RecognitionDecision rows. This is a real pre-existing late history-reconcile inline failure surfaced by shadow bookkeeping, not a shadow write failure; zero shadow enqueue/terminal-update failure log lines were observed. The final follow-up fix expands failure terminalization through candidate counting, trade merge, and strategy-alert delivery so these incidents no longer remain falsely stuck pending. Do not erase or backfill these evidence rows."
phase_4_trading_impact: "No deviation attributable to shadow bookkeeping. During the real window one pre-existing strategy-management close submitted and two pre-existing live-signal trigger entries submitted; shadow code issued no exchange reads or writes and no consumer exists. Exchange moved from 1 position/0 regular/5 BTC triggers/3 TPSL before the window to 0 position/0 regular/4 BTC triggers/0 TPSL after the close; the final BTC trigger query succeeded, and zero live positions means no position TPSL was required. active_write_count was 0 at every deploy gate and final verification."
phase_4_migration_rehearsal: "Production online-backup copy and separate dry-run copy at candidate dae09ef, evidence /opt/telegram-kol-analyzer/data/backups/phase4-rehearsal-evidence-dae09efc699c94261c4245f88f4f7523d8ca667c-20260820T062103Z.json: quick_check ok before/after, tables 80 -> 81 with only message_processing_jobs new, unique raw_message_id index present, job count 0, changed_existing_counts empty; raw_messages 11755, recognition_decisions 11754, execution_events 3620 unchanged. Backup and dry-run DB files remain beside the evidence JSON; updater also preserved research-deployment-dae09efc699c94261c4245f88f4f7523d8ca667c-20260820T062302Z.db."
phase_3_reason: "executed per the standing instruction to run exactly the phase named by current_phase_file"
phase_3_code_commit: 3eabde7c3c6e7e2edfc43c60c435c5a4da5975a3   # tasks 1-5; committed 2026-08-20
phase_3_deployed_commit: 3eabde7c3c6e7e2edfc43c60c435c5a4da5975a3
phase_3_local_suite_before: 5714   # 5713 passed, 1 skipped, at 3ed642a (the claim commit, no code yet) - collected in a throwaway worktree
phase_3_local_suite_after: 5732    # 5731 passed, 1 skipped, 0 failed, 400.6s; delta 18 equals exactly the tests phase 3 adds (11 in tests/test_authoritative_gap_recovery_loop.py, 5 in tests/test_trading_settings.py, 2 in tests/test_system_operator_bot.py)
phase_3_message_lock_mode_unchanged: true   # confirmed "global" in production both before and after this deploy; phase 3 does not touch the flag, only wires the new loop through the same resolve_message_lock_mode/resolve_lock_context convention run_periodic_reconcile already uses
phase_3_recovery_latency_observed: "MEASURED, not idealized. raw_message_id=11683 (posted 2026-08-20 01:48:30 UTC, arriving within seconds of the post-deploy restart at 01:48:58 UTC) had no decision; run_authoritative_gap_recovery_loop found and retried it repeatedly starting within its first pass, but it did not get a real decision until 01:56:00 UTC - a 7.5-minute recovery, not the sub-20s figure the phase's own docstrings describe for the uncontended case. A second, unrelated message (11693, posted 02:00:44 UTC) was picked up and resolved by the ordinary LIVE PATH (mimo-v2.5, not recovery_guard) in about 3 minutes with zero recovery-loop involvement - normal live-path latency, confirming the loop stays idle when it should."
phase_3_recovery_contention_finding: "The 7.5-minute delay for 11683 was caused by repeated collisions with a PRE-EXISTING, un-modified guard: authoritative_recognition.assess_message_authoritatively's claim_message_evidence_extraction (authoritative_recognition.py:775), which raises RuntimeError('message evidence extraction already in progress') when a claim is already held. 44 collisions were logged for this one raw_message_id between 01:50:06 and 01:52:07 UTC (all caught by _process_recovery_candidate's own except-block and logged, never propagating, never crashing anything), then zero further collisions - the claim eventually cleared and the next attempt succeeded. This is a genuine, previously-latent interaction: the OLD recovery path only ran every 300s via run_periodic_reconcile, so it rarely collided with an in-flight live-path claim; the NEW 20s-cadence loop collides with it far more often by design, since Task 1 explicitly requires the new loop to invoke the same authoritative_processor as the live path rather than skip it. Nothing in Phase 3's own code holds or races that claim - it is entirely inside authoritative_recognition.py, untouched by this phase. Recorded as a finding for a future session, not fixed here: it is out of Phase 3's scope (which is the recovery *loop and its lock/expiry semantics*, not the evidence-extraction claim mechanism it calls into), and it self-resolved without intervention, with no bad trade and no crash - message 11683 eventually got a correct, real (non-recovery_guard) decision, agreement_status=agreed, non-strategy."
phase_3_recovery_loop_idle_confirmed: true   # zero 'recovery failed' log lines and zero err-level journal entries in the 10 minutes preceding the final verification read (02:02-02:12 UTC window), service otherwise healthy and processing live traffic normally throughout
phase_3_no_discover_dialogs_confirmed: "grep of the deployed source at 3eabde7 for run_authoritative_gap_recovery_loop's body: zero references to discover_dialogs or a Telegram client object, matching the design and the test_recover_missing_authoritative_decisions_has_no_client_parameter regression test"
phase_3_followup_fix_commit: e30e209ab192bd962e47bc6505256e2b03f95ae9   # small correctness fix found during independent review of 3eabde7, committed and deployed separately by the reviewing session
phase_3_followup_fix_deployed_commit: e30e209ab192bd962e47bc6505256e2b03f95ae9
phase_3_followup_fix_local_suite_after: 5732   # passed, 1 skipped, 0 failed - same count as phase_3_local_suite_after, since this fix adds exactly 1 test and phase 3's own commit already included the other 18
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
last_deployed_commit: 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8
production_commit: 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8  # Phase 5 rollback-boundary fix; global/queue since 17:10:28 UTC, gated mid-traffic restart at 17:24:32 UTC
production_commit_before_phase_5: 3bd53553af51ba4619ed3703bade2028514af4b6
production_commit_before_phase_3_followup_fix: 3eabde7c3c6e7e2edfc43c60c435c5a4da5975a3  # rollback target for the follow-up fix alone (no schema change)
production_commit_before_phase_3_deploy: 8122f15ba653e900ee88352b18f570d500bd65c4  # rollback target for phase 3's code (no schema change)
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
deploy_branch_ahead_of_production: true  # completion/status docs are ahead; production code is exactly 7a54411 and needs no docs-only redeploy
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
  - "phase-6a-candidate-a-local-STOPPED-SAFE (2026-08-22, 01a02822-4cb1-7891-9808-7175deb8ab14): Tasks 1-9 followed focused RED/GREEN TDD and explicit-path commits. Final focused acceptance passed 123 with 451 deselected in 10.11s; compatibility authority baseline stayed 1 passed/1 strict-xfailed. The first full run found one asset-harness slice regression after 5994 passes; the helper declaration was moved without semantic change, the exact failed test and Task 7 slice passed, and the new final Candidate A f257a93121ba1d547955f0b4dd5a270dd347904d suite passed 5995, skipped 1, expected-xfailed 1, 17 warnings in 456.85s. git diff --check passed. No production code changed after that final suite."
  - "phase-6a-plan-only (2026-08-22, 01a02822-4cb1-7891-9808-7175deb8ab14): documentation-only L0 verification. git diff --check passed for the approved design and self-contained implementation plan; Ruby YAML.safe_load parsed the canonical fenced YAML and confirmed current_phase='6a', phase_name='durable-worker-command-boundary', phase_status='planned', claimed_by=null, the new current_phase_file, and last_completed_phase=5. No focused code test or full suite was run because this turn changed no production code, test code, schema, database, or runtime configuration. Design commit d04dc7d47cdb44fb6f36ada439c38e6c108b88c4; implementation-plan commit e93b13b253ff0191309b478182d11d4da38d0e9c."
  - "phase-6-task1-STOPPED (2026-08-22, 01a02822-4cb1-7891-9808-7175deb8ab14): tests/test_process_boundary_authority.py was written before any production-code change. Its empty-boundary assertion failed first exactly as intended and named four Web-route authority violations. The committed guard records the exact known blocker inventory and leaves the empty-boundary decision test strict-xfailed, so removal of the violations forces the test to be updated rather than silently XPASS. Focused result on Python 3.12.12: 1 passed, 1 xfailed in 2.30s. git diff --check passed. No full suite was run because Task 1 failed and no final production-code candidate exists."
  - "phase-5-queue-rollback-race-fix-local (2026-08-20, codex-phase5-20260820-root-55e520b): the required real queue -> shadow in-flight drill exposed that shadow gap recovery's UPSERT could rewrite a shadow=false claimed queue row to pending shadow, clear its claim, and invoke recognition concurrently. TDD regression test_shadow_rollback_does_not_adopt_or_reprocess_claimed_queue_job failed first because recovery_calls=[raw_id]. Fix 77ec2149d4dbe6f54af7571a2c9756d2d3532fed makes shadow adoption conditional: existing shadow rows may reset, and unclaimed pending queue rows may be safely adopted, but claimed or terminal queue-authority rows cannot be rewritten. The enqueue helper returns only ids actually admitted to shadow authority, and shadow gap recovery skips non-admitted current/expired candidates. A companion test proves unclaimed pending queue work is still adopted exactly once after rollback. Focused message-pipeline/worker/shadow/gap-recovery/blocking-census run: 51 passed in 4.65s. Full suite: 5791 passed, 1 skipped, 0 failed, 17 known warnings in 429.13s. git diff --check passed; only telegram_live_listener.py and test_message_pipeline_mode_exclusivity.py were staged; git add -A was not used. Production remains shadow and this fix is not yet deployed."
  - "phase-5-batch-119-blocker-fix-local (2026-08-20, codex-phase5-20260820-root-55e520b): design/implementation plan commit b77be48658ea3efffc7f568a9f2773283672cc04 and exact code commit a2ae02f4f9012e4901b3c06d2221917efeabb882. Strict TDD first reproduced the all-planned executing reconciliation race, then proved the minimal guard leaves the existing worker restart path authoritative. A second RED/GREEN regression freezes submitted/partial legs lacking both durable order identities as recovery_required/management_close_submission_identity_missing. Recovery planner tests refuse incomplete snapshots and each durable evidence conflict (client id, exchange id, request, response, PositionMutationIntent, ExecutionEvent); exact zero-submission evidence yields terminal_no_submission; apply is fingerprint-gated, idempotent, terminalizes only the proven leg/batch, and writes one audit event. Focused reconciliation/recovery/worker/blocking-census run: 126 passed in 9.99s. Full suite: 5789 passed, 1 skipped, 0 failed, 17 known warnings in 429.12s. git diff --check passed; only four explicit code/test paths were staged for a2ae02f; git add -A was not used. No recognition, strategy selection, sizing, order submission, message lock, or queue-mode semantics changed. Production deployment and recovery remain outstanding."
  - "phase-5-local (2026-08-20, codex-phase5-20260820-root-55e520b): strict TDD was used for the extraction, atomic claim, durable retry/max-attempt terminal failure, returned authoritative_failed retry, stale-claim recovery, expiry, per-chat ordering/cross-chat concurrency, dormant-shadow exclusion, queue/listener exclusivity, reply-recovery enqueue ordering, recovery/history exclusivity, dynamic queue worker lifecycle, rollback mode switch, and queue parity projection. The pure Task 1 extraction gate passed the entire unchanged suite plus its one new extraction test: 5762 passed, 1 skipped. Final focused production-facing files passed 244 tests; final full local suite on exact code eaaa255 passed 5778, skipped 1, failed 0, with 17 known warnings in 440.50s. The blocking-call census passed unchanged. Exact staged paths were checked before commit; git add -A was not used."
  - "phase-4-final (2026-08-20, codex-phase4-20260820-root-f509217): TDD reproduced the late history-reconcile terminalization gap before the final fix: tests/test_message_processing_shadow_enqueue.py::test_history_reconcile_shadow_marks_failed_when_later_inline_step_fails failed because the job stayed pending after persist_trade_ideas_from_candidates raised. Commit 3bd5355 expands the existing BaseException terminal guard without changing inline call order or exception propagation; focused file then passed 20 tests. Full suite on the exact final code commit passed 5761, skipped 1, failed 0, with 17 known warnings in 417.14s. Earlier Phase 4 full suite at 6527c08 passed 5760, skipped 1, failed 0; the one-test delta is exactly this regression test."
  - "phase-0-partial: commit 816e296 covers Tasks 1 and 2 only (LoopLagMonitor plus lifespan wiring). Written by an earlier session, not independently reviewed. Verified after the fact with .venv (Python 3.12.12) because .venv313b has no bin/python: 11 focused tests pass, tests/test_web_app.py passes 194, and the complete suite passes 5575 with 1 skipped and 17 known deprecation warnings. Task 3 (loop-health endpoint), Task 4 (census allowlist recorded in the status file), Task 5 (suite baseline recorded), and Task 6 (deploy plus 60-minute production baseline) are all still outstanding."
  - "phase-0-review-and-local-completion (2026-08-18, session-04451098): reviewed 816e296 against Tasks 1-3 rather than assuming it. Correction to the entry above: 816e296 in fact also contains Task 3 — GET /api/runtime/loop-health at src/telegram_kol_research/web_app.py:4770 plus three tests in tests/test_web_app.py. Review findings: LoopLagMonitor meets every Task 1 requirement (run/snapshot keys, deque(maxlen=7200) ring buffer, stall_threshold_ms=3000 with one warning per 60s via _last_stall_log_monotonic, injectable monotonic/now_provider/sleeper, no sleeping in tests); the lifespan wiring at web_app.py:3960 and the shutdown block at web_app.py:4201 match the existing contract_spec_refresh_task pattern byte-for-byte; the endpoint is declared async so it never depends on the shared threadpool. No defects found; no code changes were needed. Local runs with .venv (Python 3.12.12): tests/test_runtime_loop_health.py plus tests/test_runtime_event_loop_blocking_census.py 11 passed; tests/test_web_app.py 194 passed; full suite 5575 passed, 1 skipped, 17 known deprecation warnings, 352s. Suite baseline is exact, not approximate: collection at 816e296^ (0a61dfd, run in a throwaway worktree) is 5562 tests; collection at HEAD is 5576; delta 14 equals exactly the 14 tests 816e296 added (11 + 3), and the after run has zero failures. Task 6 (deploy plus 60-minute production baseline) was outstanding at the time of this entry; it was completed later the same day — see the server_verification entries and the 'how it was actually deployed' section."
  - "phase-1 (2026-08-18, session-45794fed): all seven local tasks done. New src/telegram_kol_research/runtime_worker_executor.py owns one lazily created ThreadPoolExecutor(max_workers=1, thread_name_prefix='mgmt-worker') with get_management_worker_executor, shutdown_management_worker_executor(wait) and run_on_management_worker. Both loops now submit to it: the strategy management loop submits _load_settings_and_run_strategy_management_tick so load_trading_settings and the tick stay atomic on one thread, and the break-even loop submits its tick directly. Both gained an explicit `except asyncio.CancelledError: raise` ahead of the broad except. The census allowlist lost both entries and now holds only the system_operator_bot offender. web_app.py calls shutdown_management_worker_executor(wait=False) in the lifespan after both worker tasks are cancelled; wait=False is deliberate so shutdown cannot hang, and an in-flight tick still finishes on its own thread exactly as it did when it ran on the loop. 17 tests added: 8 executor, 3 strategy-loop (cursor lane alternation still executable/recovery/executable/recovery on one cursor object, tick runs off the loop thread, loop survives a raising tick), 1 break-even (both loops observably share one thread with zero overlap - the guard against someone later splitting the pools), 3 responsiveness including one that reproduces the pre-Phase-1 shape and proves the guard fails on it, 2 web_app shutdown. Full suite with .venv (Python 3.12.12): 5661 passed, 1 skipped, 0 failed, 403s. Before was 5644 passed, 1 skipped; delta 17 equals exactly the 17 tests added. Import resolution was confirmed to point at the worktree, not the main checkout, before the run was trusted."
phase_0_deploy_delta: "302c1ae -> 6620613 is 4074 insertions and 0 deletions across 19 files. Production code touched: runtime_loop_health.py (new, 142 lines) and web_app.py (+36). The rest is docs (3341 lines) and tests (377). No existing line is modified or removed."
phase_0_merged_suite: "5644 passed, 1 skipped, 0 failed on codex/phase0-deploy-integration (385s). Deploy branch alone collects 5631; merged collects 5645; delta 14 equals exactly the tests Phase 0 adds."
server_verification:
  - "phase-6a-candidate-a-shadow-STOPPED-SAFE (2026-08-22 08:08-08:24 UTC): fast-forward push eef0e53..f257a93 succeeded. L3 rehearsal summary /opt/telegram-kol-analyzer/data/backups/phase6a-f257a93121ba1d547955f0b4dd5a270dd347904d-20260822T080840Z/rehearsal-summary.json passed with online backup 598310912 bytes, all quick checks ok, only the new table/four indexes added, zero new rows, unchanged existing counts and 13 critical hashes, repeat-bootstrap idempotence, and exact physical rollback schema/count/hash. Predeploy HEAD 1016039 was active global/queue/default-inline with active_write_count=0, no active management/mutation, WAL and quick_check ok. Gated updater deployed exact f257a93, service active at 16:17:07 CST, updater backup research-deployment-f257a93-20260822T081603Z.db, new table zero rows/four indexes, then a second quiet gate switched only worker_command_mode inline -> shadow; global/queue and monolith topology stayed unchanged. Stop evidence: worker total/claimed/executing/uncertain all zero, SQLite_BUSY/database-locked/worker-command journal anomaly count zero, quick_check ok. The mandatory real sync shadow sample was not invoked because 2463 pending attribution notifications and 331 pending protection notifications mean the existing route could send extra Telegram messages, forbidden during active work. Queue, crash drill, Candidate B, restart, direct write-history audit, and L2 observation were not started. Safe live state is exact f257a93 at global/queue/shadow; rollback preconditions claimed=0/executing=0 are satisfied."
  - "phase-6a-plan-only (2026-08-22): not run and not required for documentation planning. No production query, push, deploy, database/bootstrap operation, settings change, restart, systemd change, exchange call, or Telegram notification was performed during planning. The last recorded production and Phase 6 Task 1 evidence below remains historical context, not a fresh verification."
  - "phase-6-task1-STOPPED-SAFE (2026-08-22 06:24-06:29 UTC): read-only production baseline found HEAD 10160398630dc15472dc660fe13ca8721a19337d, telegram-kol.service active, settings message_lock_mode=global and message_pipeline_mode=queue, research.db WAL and quick_check ok. An online backup copy at /opt/telegram-kol-analyzer/data/backups/phase6-task1-concurrency-c50c264.db was load-tested by three separate writer processes only; evidence /opt/telegram-kol-analyzer/data/backups/phase6-task1-sqlite-load-c50c264.json records 5400/5400 committed, SQLITE_BUSY=0, all connections WAL/busy_timeout=30000, and final quick_check ok. The production DB was not written. The next authority gate failed locally, so no push, deploy, restart, unit install, topology switch, isolation load, or L2 observation was attempted; production remains the original monolith and rollback is not applicable."
  - "phase-5-production-COMPLETE (2026-08-20 16:57-18:14 UTC): pushed rollback-boundary fix and status as exact deploy-branch tip 7a54411ebb0907cd04c33fe3b474f4b5b54ecfb8, then gated-deployed it with EXPECTED_COMMIT; updater exit 0, production HEAD exact, service active, and settings global/shadow. Repaired real boundary proofs then passed: shadow job 194/raw 11955 stayed shadow and completed once across shadow -> queue; claimed queue job 195/raw 11956 stayed queue authority and completed once across queue -> shadow. Each had exactly one decision and zero execution events; evidence phase5-shadow-to-queue-boundary-7a54411.json and phase5-queue-to-shadow-boundary-7a54411.json. A double-read quiet-window gate enabled queue at 17:10:28.789382 UTC. First ordinary queue job 199/raw 11960 succeeded once. At 17:24:13 job 200/raw 11961 was captured genuinely claimed, then the gated updater deliberately restarted the service with the same exact EXPECTED_COMMIT=7a54411; updater exit 0, PID changed 593302 -> 648656, mode remained global/queue, and the stale lease was reclaimed at 17:29:13 before the job succeeded once with one decision and zero execution events. Exact boundary/cutover/restart evidence remains under /opt/telegram-kol-analyzer/data/backups/phase5-*-7a54411.json."
  - "phase-5-full-session-observation-COMPLETE (cutover 2026-08-20 17:10:28.789382 UTC; final capture 18:12:54.876237 UTC; 3746.086855 seconds): production remained exact 7a54411, service active with restart PID 648656, message_lock_mode global, and message_pipeline_mode queue. Real traffic raw 11960-11967 spanned two chats: 8 raw messages, 8 authoritative queue jobs, 8 succeeded, 8 decisions, zero failed/expired/pending, missing_job_count=0, orphan_job_count=0, stuck_pending_count=0, and oldest_pending_age_seconds null. active_write_count=0, no planned/executing/reconciling management batch, SQLite quick_check ok. Loop health after restart: p99 25.632 ms, max 197.738 ms, stall_count 0. Internal execution_events after cutover: zero. Direct Deepcoin order history, fill history, and trigger history all covered times earlier than cutover and had zero post-cutover rows, so the duplicate-order audit did not rely on internal counters. The first final exchange snapshot was explicitly rejected as incomplete after an ETH trigger query connection reset (9 returned triggers were not treated as healthy); a fresh complete read returned errors={}, the same two BTC position ids 1001124898122909 and 1001124899621086, zero regular open orders, and 12 pending trigger ids. Full evidence, including both exchange attempts and exact rows/ids, is /opt/telegram-kol-analyzer/data/backups/phase5-full-session-observation-7a54411.json. One non-error-level Telethon connection-cleanup warning/Traceback occurred at 17:38:50 UTC; PID and service stayed live, later real messages completed, parity stayed exact, and journal priority err..alert contained no entries. Recorded rather than hidden; it did not indicate a queue failure, lost message, exchange write, or duplicate order."
  - "phase-5-queue-to-shadow-boundary-FAILED-SAFE (2026-08-20 16:41:17-16:41:32 UTC): with production at 81cb75e and mode queue, real raw_message_id=11953 created authoritative queue job 192 and the worker claimed it at 16:41:17.875. The monitor proved active_write_count=0 and no active management batch, then switched queue -> shadow at 16:41:18.199 while the job was genuinely claimed. At 16:41:26 the pre-existing fast gap-recovery loop saw the still-missing decision and the shadow enqueue UPSERT rewrote that claimed queue row to shadow=true, cleared the claim, and direct recovery collided with the worker's evidence claim (RuntimeError message evidence extraction already in progress). The worker nevertheless persisted exactly one RecognitionDecision at 16:41:32, automation skipped/mimo_no_action, with zero execution_events for the source message; no order was submitted. The job evidence row remained failed/recovery_error:RuntimeError because its queue claim had been erased, so the rollback boundary is NOT proven and queue was not re-enabled. Production was already shadow by the rollback command and remains global/shadow. Exact evidence: /opt/telegram-kol-analyzer/data/backups/phase5-queue-to-shadow-boundary-81cb75e.json. Local fix 77ec214 prevents adoption of claimed queue rows; deploy and repeated boundary proof are outstanding."
  - "phase-5-shadow-to-queue-boundary-PASS (2026-08-20 16:34:13-16:34:55 UTC): starting global/shadow with active_write_count=0 and no active management batch, real raw_message_id=11952 created shadow job 191 pending at 16:34:13.218. The monitor switched shadow -> queue at 16:34:13.494 while that inline job was genuinely in flight. It completed once as shadow=true/succeeded/inline_completed at 16:34:55.552, with exactly one RecognitionDecision and zero execution_events for the source message; it was not consumed by the queue worker. Evidence: /opt/telegram-kol-analyzer/data/backups/phase5-shadow-to-queue-boundary-81cb75e.json. This half-boundary passed, but the full rollback drill failed later and therefore must be repeated after 77ec214 deploy."
  - "phase-5-batch-119-recovery-COMPLETE (2026-08-20 16:19-16:26 UTC): pushed and gated-deployed exact branch tip 81cb75ed1c1bcd718eada69dde189293ea2a921c with EXPECTED_COMMIT; updater exit 0, both active-write gates passed, service restarted active at 16:20:48 UTC, and settings remained global/shadow. Batch 119 froze recovery_required/management_close_submission_identity_missing at 16:20:55 without exchange changes. Preserved online backup, separate rehearsal DB, before/after table hashes, and CLI outputs under data/backups/phase5-batch119-recovery-*81cb75e-20260820T162200Z*. Rehearsal quick_check stayed ok and changed only batch/leg plus one audit event. Fresh production active_write_count=0, no active management batch, and dry-run fingerprint exactly matched rehearsal; one apply resolved batch 119/history_no_submission_confirmed and leg 103/failed and wrote execution_event 3643. Post-apply quick_check ok, service active, zero err journal entries since deploy, and complete direct Deepcoin reads showed the same two BTC short position ids, zero regular orders, and the same 12 pending trigger ids (BTC 9, ETH 3) before/deploy/after apply."
  - "phase-5-dormant-deploy (2026-08-20 15:14-15:19 UTC): before deploy, production HEAD was 3bd5355, service active, message_lock_mode=global, message_pipeline_mode=shadow, active_write_count=0. Direct read-only Deepcoin evidence showed 2 BTC short positions, 0 regular open orders, and pending trigger counts BTC=9, ETH=3, SOL=0; exact position and trigger IDs were retained for comparison. Pushed exact code eaaa255f95f6c0889c86db5b674e458f3e2e5e56, then ran EXPECTED_COMMIT=eaaa255f95f6c0889c86db5b674e458f3e2e5e56 ./scripts/server_git_update.sh from that checkout. The gated updater fast-forwarded 3bd5355..eaaa255, ran both active-write gates, stopped at 15:16:53 UTC, and restarted active at 15:17:00 UTC. Independent verification: server HEAD exactly eaaa255, service active, settings still global/shadow, active_write_count=0, endpoint healthy. The updater health probe needed its normal connection-refused retries during startup and then succeeded; no rollback fired. No schema file changed, so no migration ran."
  - "phase-5-dormant-safety-and-tests: deployed focused suite passed 38 tests with 0 failures in 26.88s. Direct post-deploy Deepcoin reads returned the same 2 position IDs, 0 regular orders, the same BTC/ETH/SOL trigger counts (9/3/0), and the same exact trigger IDs as pre-deploy, so the dormant deployment did not alter positions, orders, or TPSL/trigger protection. Journal showed a clean application shutdown/start and no message-processing-worker error."
  - "phase-5-cutover-NOT-started (evidence through 2026-08-20 15:19:40 UTC): watermark 11920 had zero post-deploy raw messages, zero jobs, and zero decisions; parity in shadow was missing=0, orphan=0, stuck=0 only because the observed window was empty. An empty window is not evidence that the extracted live path works and cannot prove exactly-once behavior at a shadow/queue transition. Therefore queue was not enabled, rollback-boundary proof was not claimed, no full real trading session was claimed, and no deliberate mid-traffic restart was attempted. Phase 5 remains in_progress and Phase 6 must not start."
  - "phase-5-real-shadow-path (2026-08-20 15:20:42-15:21:00 UTC): raw_message_id=11921 arrived from a configured real chat after the dormant deploy. The extracted path created exactly one shadow job at 15:20:42.784, completed it succeeded/inline_completed at 15:21:00.344, and persisted exactly one RecognitionDecision with automation_status=skipped, automation_reason=mimo_no_action. Parity from watermark 11920 was raw=1, jobs=1, missing=0, orphan=0, stuck=0, pending=0, succeeded=1; zero message-processing ERROR/Traceback lines. This positively proves the post-refactor shadow authority path, replacing the earlier empty-window limitation, but it does not prove queue authority."
  - "phase-5-queue-cutover-BLOCKED (2026-08-20 15:21:51 UTC): the mandatory read-only quiet-window check returned active_write_count=0 and no new pending message job, but also found strategy_management_batches id=119 in status=reconciling with updated_at=2026-08-20 15:21:51.035905 - actively updated in the same second as the check. The Phase 5 gate explicitly requires no in-flight management batch, so this is BLOCK even though the narrower deployment active-write counter is zero. message_pipeline_mode was not changed: production remains shadow, no blind retry was made, and no queue observation/restart/duplicate-order claim is made."
  - "phase-5-batch-119-read-only-diagnosis (2026-08-20 15:37-15:39 UTC): production HEAD remained exactly eaaa255f95f6c0889c86db5b674e458f3e2e5e56, service active, message_lock_mode=global, message_pipeline_mode=shadow, and active_write_count=0. Two checks 35 seconds apart proved this is not a transient cutover collision: batch 119 stayed reconciling/management_close_pending_exchange_confirmation while updated_at advanced 15:38:22.784171 -> 15:38:53.096219; it dates from 2026-08-12 and leg 103 remains submitted with no client/exchange order identity and management_close_order_not_found. The official recover-management-history command was run without --apply and refused management_batch_not_actionable (exit 2); no DB, settings, service, order, position, or TPSL mutation was made. Fresh direct Deepcoin reads were complete: the same two BTC short position IDs, zero regular open orders, and the same pending trigger counts BTC=9/ETH=3/SOL=0 with exact IDs unchanged from the prior Phase 5 snapshot. Shadow parity after watermark 11920 remained missing=0/orphan=0/stuck=0 with no pending jobs. Queue remains disabled because the phase's no-in-flight-management-batch gate is still BLOCKED."
  - "phase-4-production-complete (2026-08-20 06:21-08:59 UTC): production DB-copy migration rehearsal passed before the first deploy and preserved the exact backup/dry-run/evidence files named by phase_4_migration_rehearsal. Gated updater deployments with exact EXPECTED_COMMIT values succeeded for dae09ef, 6527c08, and final 3bd5355; production HEAD is exactly 3bd5355, service active since the required in-shadow restart at 2026-08-20 15:57:20 CST, message_lock_mode remained global, and message_pipeline_mode remains shadow. The disable path was proven before leaving shadow on: shadow created a job for raw 11756, switching inline suppressed a job for raw 11757, then a fresh watermark was used for the final window."
  - "phase-4-shadow-window (watermark 11779, 2026-08-20 07:56:51-08:58:36 UTC): 61m45s of real multi-group traffic spanning the gated in-shadow restart; 36 raw messages across 10 chats, exactly 36 jobs and 36 decisions. Final parity: missing 0, orphan 0, stuck pending 0, pending 0, succeeded 28, failed 8. All failures were explicit terminal history_reconcile_error:MultipleResultsFound findings and all had decisions; zero shadow enqueue/terminal-update failure log lines. Execution events changed 3629->3632 through existing trading paths (one strategy-management close, two live-signal trigger entries). Read-only exchange checks showed 1 position/0 regular/5 BTC triggers/3 TPSL before, then 0 position/0 regular/4 BTC triggers/0 TPSL after the legitimate close; final BTC trigger query succeeded, no position remained requiring TPSL, and active_write_count=0. No consumer exists and no behavior deviation was attributable to Phase 4."
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

  - "phase-3-local (2026-08-20, session-phase3-compwindow-0819): Task 1 (telegram_live_listener.py) extracted the inline recovery block that used to live inside run_reconcile_once into two pieces - a synchronous _load_gap_recovery_candidates(session_factory, chat_titles_by_id, now, message_limit) that reads TradingSettings.authoritative_gap_recovery_max_age_minutes fresh on every call and runs the same missing/expired queries as before, and an async recover_missing_authoritative_decisions(...) that offloads that read via asyncio.to_thread, performs no Telegram calls of any kind (no client parameter, no discover_dialogs_fn parameter - asserted directly by a signature-introspection test), and preserves the exact per-message lock-acquisition shape run_reconcile_once's own recovery loop already used. run_reconcile_once now calls this extracted function instead of running the block inline; a dedicated test proves its observable behavior (recovered_messages/expired_recovery_messages counts, the persisted automation_reason, notification suppression) is unchanged. A new run_authoritative_gap_recovery_loop (default interval 20s, DEFAULT_AUTHORITATIVE_GAP_RECOVERY_INTERVAL_SECONDS) resolves chat_titles_by_id every tick via an injected chat_titles_by_id_provider (wired in web_app.py to _group_label_by_chat_id(app.state.group_config) - local configuration, no Telegram), off the loop via asyncio.to_thread, and follows run_periodic_reconcile's own resolve_message_lock_mode/resolve_lock_context three-way branch verbatim (none/per_chat/global) rather than inventing new wiring - in 'global' mode (production's only enabled mode) the whole recovery call is wrapped in the single shared lock, exactly like run_periodic_reconcile wraps run_reconcile_once, so it cannot run concurrently with the live path or the old reconcile pass. Task 2: expiry is now classified via _classify_expired_authoritative_recovery_gap as EXPIRED_AFTER_SYSTEM_STALL (a recorded loop-lag-monitor stall overlapped the message's posted_at..now window) or EXPIRED_STALE_INSTRUCTION (no evidence of overlap, including when posted_at or the snapshot is missing - the quiet classification is the fail-safe default). The persisted automation_reason stays the literal string 'authoritative_gap_recovery_expired' for every classification, unchanged from before this phase, because tests/test_reconcile_live_history.py already asserts that exact string; only a new expiry_classification key inside the JSON payload and the Chinese summary text vary. Stall-induced expiries schedule at most one aggregate Telegram notification per StallExpiryNotificationRateLimiter window (default 300s, injectable monotonic clock, no sleeping in tests) via a new format_stall_induced_expiry_notification/send_stall_induced_expiry_notification pair in system_operator_bot.py, naming the whole burst ('N messages'), never one notification per message. Expired messages are still never auto-executed in either classification - expiry stays fail-safe, matching Task 2 Step 3 exactly. Task 3: trading_settings.py gained authoritative_gap_recovery_max_age_minutes: float = 15.0, parsed with the existing _positive_float helper (fails OPEN to the 15.0 default on any invalid value, matching every other float setting in this module, not raise), so the previously-hardcoded AUTHORITATIVE_GAP_RECOVERY_MAX_AGE constant is now a dead reference kept only as documentation - no code path reads it anymore. Default is numerically unchanged (15.0 minutes), so behavior does not change until an operator flips it."
  - "phase-3-task5-suite: full local suite with .venv (Python 3.12.12): 5731 passed, 1 skipped, 0 failed, 400.6s. Before (collected in a throwaway worktree at 3ed642a, the phase-3 claim commit with no code yet) was 5713 passed, 1 skipped, 5714 collected; after collects 5732; delta 18 equals exactly the tests this phase adds (11 in the new tests/test_authoritative_gap_recovery_loop.py covering all 6 of Task 4's required assertions plus rate-limiter unit tests, 5 in tests/test_trading_settings.py for the new setting's round-trip and fail-open behavior, 2 in tests/test_system_operator_bot.py for the new notification formatter). The blocking-call census (tests/test_runtime_event_loop_blocking_census.py) passes unchanged - no new allowlist entry needed; every new synchronous read this phase adds is wrapped in asyncio.to_thread at its call site."
  - "phase-3-pre-deploy-snapshot (2026-08-20 01:09:50 UTC): live position snapshot read from the server (data/web_cache/deepcoin_live_positions.json) as pre-deploy evidence: positions [], open_orders [] - zero open positions or working orders at deploy time, the safest possible window for a writer-adjacent deploy even though this phase touches no exchange-mutation code."
  - "phase-3-deploy (2026-08-20 01:48:xx UTC / 09:48:58 CST service restart): DEPLOYED. Fast-forward pushed 3ed642a..3eabde7 to codex/deepcoin-auto-trading-v1 (2 commits: the phase-3 claim, then the phase-3 code), confirmed origin/codex/deepcoin-auto-trading-v1 was an ancestor of HEAD first. Ran EXPECTED_COMMIT=3eabde7c3c6e7e2edfc43c60c435c5a4da5975a3 ./scripts/server_git_update.sh from this worktree, checked out at that exact commit; exit code 0, captured without a pipe. The curl connection-refused lines near the end of the updater's own output are verify_http_health's retry loop during the service restart, consistent with every prior phase's deploy. Independently verified over ssh (not inferred from the updater's exit code): HEAD=3eabde7 on codex/deepcoin-auto-trading-v1, telegram-kol.service active since 2026-08-20 09:48:58 CST, ActiveEnterTimestamp confirmed, zero journal entries at priority err or above in the first several minutes after restart. GET /api/trading-settings returns 200 with message_lock_mode: 'global' (unchanged, confirms this phase did not touch the flag) and the new authoritative_gap_recovery_max_age_minutes: 15.0 (confirms the setting round-trips through production with its unchanged default). GET /api/runtime/loop-health answers 200 throughout."
  - "phase-3-source-level-confirmation: grepped the DEPLOYED source directly over ssh (not the local worktree) for run_authoritative_gap_recovery_loop's body - zero references to discover_dialogs or a Telegram client, matching Task 1 Step 2's requirement and the local regression test. Also grepped the whole deployed src/ tree for the literal string 'authoritative recognition recovery failed' (the log line inside _process_recovery_candidate) and confirmed it exists in exactly one place - src/telegram_kol_research/telegram_live_listener.py:1011 - so every journal occurrence of that message is unambiguously attributable to recover_missing_authoritative_decisions (reached from either run_reconcile_once or the new fast loop), not some other code path."
  - "phase-3-real-gap-recovery-observed (2026-08-20 01:48-01:56 UTC): a REAL gap-recovery event happened during verification, not a synthetic one - no test data was inserted. raw_message_id=11683 (chat 米哥会员群-11分组, a genuinely configured, enabled group per config/groups.yaml) arrived at 01:48:30 UTC, seconds before the 01:48:58 UTC restart, and had no RecognitionDecision row. It was independently confirmed missing via direct sqlite3 queries against data/research.db on the server (not inferred from logs), then watched via a polling Monitor until it resolved. It DID resolve, at 01:56:00 UTC, with a real decision from authoritative_model='mimo-v2.5' (not recovery_guard - a genuine, successful recognition, agreement_status=agreed, non-strategy), NOT an expiry fallback. Measured recovery latency for this message: 7.5 minutes (01:48:30 posted to 01:56:00 decided). This IS the 'recovery latency improvement measured and recorded' the phase file's Task 6 Step 3 and completion criteria ask for, reported honestly rather than idealized - see the next entry for why it is 7.5 minutes and not the sub-20s figure the docstrings describe for the uncontended case."
  - "phase-3-recovery-contention-finding: see phase_3_recovery_contention_finding in the YAML block above for the full account. Summary: 44 collisions with a PRE-EXISTING (not modified by this phase), DB-backed claim/lease guard inside authoritative_recognition.assess_message_authoritatively (claim_message_evidence_extraction, authoritative_recognition.py:775) were logged for raw_message_id=11683 between 01:50:06 and 01:52:07 UTC, each one caught by _process_recovery_candidate's own except-block, logged as an ERROR, and never propagating or crashing anything - then zero further collisions, and a successful decision four minutes later. The 20s-cadence loop is far more likely to collide with an in-flight live-path claim than the old 300s reconcile ever was, precisely because Task 1 requires it to invoke the same authoritative_processor as the live path rather than skip contested messages. This is a genuine interaction effect worth a future session's attention (e.g., recognizing 'already in progress' as an expected, quiet-retry condition rather than an ERROR-level failure), but it is out of Phase 3's scope - the claim mechanism itself lives entirely in authoritative_recognition.py, which this phase does not touch - and it did not cause any incorrect trade, crash, or lasting production impact."
  - "phase-3-idle-and-live-path-confirmed (verified through 2026-08-20 02:12 UTC, about 24 minutes of production uptime): after the one contended message resolved, ZERO further 'recovery failed' log lines and ZERO err-level journal entries were observed in the 10 minutes preceding the final check. Direct DB query at that time found exactly 2 rows still missing a decision: raw_message_id=11693 (posted 02:00:44 UTC, 3 minutes old, evidently still in flight) and raw_message_id=8793 (posted 2026-06-13, a pre-existing historical outlier from over two months before this phase, unrelated to it and out of scope to backfill). 11693 was watched to resolution: it got a real decision from authoritative_model='mimo-v2.5' at 02:03:51 UTC - about 3 minutes after posting, via the ORDINARY LIVE PATH with zero recovery-loop involvement (no 'recovery failed' log for this id) - confirming Task 6 Step 4 directly: normal live messages are still processed by the live path, not by the recovery loop, and the recovery loop is idle during healthy operation. Also spot-checked the 7 other most recent messages (11685-11692): all show authoritative_model='mimo-v2.5' with posted-to-decided latency of roughly 1-2.5 minutes, consistent normal live-path behavior, none via recovery_guard."
  - "phase-3-stopping-point-decision: NOT stopping here - current_phase advanced to 4 per the phase file's own explicit instruction. Phase 3's own completion criteria are met: the loop runs independently of the Telegram fetch pass on a fast cadence off the event loop under the correct (global-mode-verified) lock; expiry is classified, recorded, and would notify (rate-limited) if stall-induced expiry actually occurred - none did during this observation window, both classifications were exercised only in local tests; the window is configurable with the default numerically unchanged; recovery latency was measured and recorded honestly, including the contention finding above rather than a rounded-up number; the loop was observed idle during healthy operation. Phases 4-6 (durable job queue, queue consumer takeover, process separation) address a different failure class - losing in-flight work on restart, and web traffic perturbing execution - not the silent-backlog-loss problem Phase 3 targets, so there is no dependency reason to stop, and the project's established pattern across every prior phase has been to advance immediately. The claim-contention finding above is recorded for whichever future session next touches authoritative_recognition.py or the recovery loop's retry behavior, not as a blocker."
  - "phase-3-followup-fix (2026-08-20, session that reviewed 3eabde7 before its own commit): while independently reviewing the phase-3 executing session's committed work - both sessions shared this worktree concurrently; the executing session's task-notification fired 'completed' prematurely (a known behavior when an agent briefly has no live children) and it kept working for another ~23 minutes and committed 3eabde7 and f27e959 during that window, while the reviewing session was mid-review of the same files. Diffs were confirmed non-overlapping (the reviewer never edited anything the executor's commits touched) and no work was lost. One real gap survived Phase 3 as committed: loop_lag_snapshot_provider reached the new fast run_authoritative_gap_recovery_loop but was never threaded into run_periodic_reconcile's own three calls to run_reconcile_once, so if that slower, 300s, Telegram-fetch-coupled fallback pass ever independently observed an expiry, it would always classify it expired_stale_instruction and skip the operator notification, even if the true cause was a stall - undermining Task 2's goal for that one, rare fallback path. Fixed by threading the same provider through run_periodic_reconcile's signature and its three run_reconcile_once call sites, and both web_app.py callers (the lifespan startup block and ensure_live_tasks_match_targets). Also added a web_app lifespan test (test_authoritative_gap_recovery_loop_lifespan_starts_and_is_cancelled) for the new loop's startup/shutdown wiring, which Phase 3 as committed did not have, mirroring the existing test_management_worker_lifespan_starts_once_and_is_cancelled pattern. Committed separately as e30e209 (3 files, 45 insertions, 1 new test) so the two independent units of work stay attributable. Full suite 5732 passed, 1 skipped, 0 failed - unchanged from phase_3_local_suite_after since this fix adds exactly 1 test to what 3eabde7 already had. Deployed 2026-08-20 02:16:55 UTC, verified over ssh: HEAD=e30e209, service active, zero err-level journal entries, message_lock_mode still 'global', authoritative_gap_recovery_max_age_minutes still 15.0."

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
| 3 | `phase-3-compensation-window-repair.md` | **completed** 2026-08-20, deployed `3eabde7` then follow-up fix `e30e209` — fast loop decoupled from Telegram fetch; a real gap (raw_message 11683) was observed and recovered end to end |
| 4 | `phase-4-durable-job-shadow-enqueue.md` | **completed** 2026-08-20, deployed `3bd5355` — 61m45s shadow, 36 messages/10 chats, parity missing/orphan/stuck all zero; 8 real late-inline failures recorded terminally |
| 5 | `phase-5-queue-consumer-takeover.md` | **completed** 2026-08-20, deployed `7a54411` — global/queue; repaired boundary rollback, claimed-job restart resume, 62m26s real traffic, parity and direct exchange duplicate audit passed |
| 6A | `phase-6a-durable-worker-command-boundary.md` | **in progress, stopped safe** 2026-08-22 — Candidate A `f257a93` deployed global/queue/shadow after passed L3 rehearsal; mandatory real sync parity blocked by pending notification outboxes, so queue/Candidate B/L2 observation not started |
| 6R | `phase-6r-semantic-review-control.md` | **in progress, deployed/live apply safe, observation blocked** 2026-08-22 — Candidate `5e3cb63` passed repaired L3 rehearsal and exact live 128-row transition, but concurrent runtime writes broke the literal unchanged-unrelated-hash gate; L2 observation not started pending owner decision |
| 6 | `phase-6-process-separation.md` | **blocked pending Phase 6A; stopped at Task 1 authority gate** — SQLite three-process gate passed with 0/5400 SQLITE_BUSY, but four Web routes still reach worker-only exchange authority; no split/deploy occurred |

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

## Phase 3 — compensation window repair, completed

Deployed `3eabde7` on 2026-08-20 (service restart 01:48:58 UTC). `message_lock_mode`
is untouched and still reads `"global"` in production, confirmed over ssh both
before and after this deploy.

**What shipped, in one paragraph each.** Task 1:
`recover_missing_authoritative_decisions` in `telegram_live_listener.py` is the
old inline recovery block from `run_reconcile_once`, extracted so it takes no
Telegram client and no `discover_dialogs_fn` — verified both by a signature
test and by grepping the *deployed* source directly. `run_reconcile_once` now
calls it, proven behaviorally unchanged by a dedicated regression test. A new
`run_authoritative_gap_recovery_loop` runs it independently every 20 seconds
(`DEFAULT_AUTHORITATIVE_GAP_RECOVERY_INTERVAL_SECONDS`), resolving
`chat_titles_by_id` from `_group_label_by_chat_id(app.state.group_config)` —
local configuration, never Telegram — and follows `run_periodic_reconcile`'s
own `resolve_message_lock_mode`/`resolve_lock_context` wiring exactly instead
of inventing a new convention. Task 2: expiry is now classified
`expired_after_system_stall` or `expired_stale_instruction` using the
loop-lag-monitor snapshot against the message's `posted_at`..`now` window; a
stall-induced burst gets at most one rate-limited aggregate notification
(`StallExpiryNotificationRateLimiter`, default 300s), never one per message;
expiry stays fail-safe in both classifications — nothing is ever auto-executed
from the expired path. The persisted `automation_reason` deliberately did NOT
change (`"authoritative_gap_recovery_expired"` for every classification),
because existing tests already depend on that exact string; only a new
`expiry_classification` payload key and the Chinese summary text vary by
classification. Task 3: the hardcoded 15-minute constant is now
`TradingSettings.authoritative_gap_recovery_max_age_minutes` (default `15.0`,
fails open to that default on any invalid value, matching every other float
setting in this module) — behavior is numerically unchanged until an operator
flips it.

**The recovery latency claim is measured, not idealized — and the honest
number is worse than the phase file's own docstrings imply for the
uncontended case.** A real gap was observed during verification (not
synthetic test data): `raw_message_id=11683` arrived seconds before the
deploy's restart and had no decision. It took **7.5 minutes**
(01:48:30 → 01:56:00 UTC) to recover, not the sub-20s figure a single clean
pass would produce, because the new loop's 20s cadence collided 44 times with
a pre-existing, unmodified DB-backed claim guard inside
`authoritative_recognition.assess_message_authoritatively`
(`claim_message_evidence_extraction`) while the live path already held it.
Every collision was caught, logged, and harmless — no crash, no bad trade,
the message eventually got a real, correct decision. This is a genuine
interaction effect Phase 3's faster cadence surfaces more often than the old
300s cadence did, recorded for a future session rather than fixed here (the
claim mechanism lives entirely in `authoritative_recognition.py`, outside
this phase's files). A second, unrelated message (`11693`) was independently
observed resolving through the ordinary live path in ~3 minutes with zero
recovery-loop involvement, and the loop logged zero further activity in the
10 minutes preceding final verification — confirming Task 6 Step 4 (the loop
stays idle when the live path is healthy) directly, not by inference.

**Follow-up fix, deployed separately as `e30e209`.** A second session
reviewing this phase's committed work (the two sessions shared this worktree
briefly — see `phase-3-followup-fix` above for how that stayed conflict-free)
found `loop_lag_snapshot_provider` reached the new fast loop but never the
slow `run_periodic_reconcile` fallback path, so a stall-caused expiry
observed there instead of by the fast loop would have been silently
misclassified as ordinary staleness. Closed, tested (5732/1/0, same count —
this fix adds exactly one lifespan-wiring test), deployed, and verified over
ssh: `message_lock_mode` still `"global"`, `authoritative_gap_recovery_max_age_minutes`
still `15.0`, zero journal errors after restart.

**Rollback**, if ever needed: redeploy `8122f15ba653e900ee88352b18f570d500bd65c4`
(the pre-Phase-3 production commit) with the same updater command. No schema
change, no persisted state beyond the new
`authoritative_gap_recovery_max_age_minutes` settings key, which defaults to
`15.0` on any row that predates it — numerically identical to the constant it
replaces.

## Before Phase 4 starts — read this

1. **`message_lock_mode` still stays `"global"`.** Phase 3 did not touch it,
   did not need to, and confirmed it unchanged over ssh. Enabling `per_chat`
   remains Phase 2's own un-started Task 6 Steps 2-3, a separate decision the
   user has not yet been asked to make in this context.
2. **A live interaction finding from Phase 3, worth reading before touching
   the recovery/recognition paths again**: `authoritative_recognition.py`'s
   `claim_message_evidence_extraction` claim/lease guard (raises
   `RuntimeError("message evidence extraction already in progress")`) collides
   far more often now that gap recovery runs every 20s instead of every 300s.
   Not a Phase 3 defect and not fixed by Phase 3 — see
   `phase_3_recovery_contention_finding` in the YAML block and the "Phase 3"
   section above for the full account. A future session touching either the
   recovery loop or that claim mechanism should know this collision is
   expected and currently logged at ERROR level even though it is harmless
   and self-resolving.
3. **Phase 4 (durable-job-shadow-enqueue) is a different failure class** than
   Phases 0-3: it addresses losing in-flight work on restart, not silent
   backlog loss from a stall (which Phase 3 now compensates for) or the event
   loop being blocked (which Phases 1-1e fixed). Read
   `phase-4-durable-job-shadow-enqueue.md` fresh; do not assume anything about
   its scope from this section.
4. **Deployment mechanics are unchanged** — see "There is no change class"
   and "Before Phase 2 starts" above for the still-accurate points (no
   `-ChangeClass`, no PowerShell on this workstation, run the deploy script
   from a checkout of the exact commit being deployed).
5. **The authoritative checkout is still**
   `.worktrees/runtime-serialization` on branch `codex/phase0-deploy-integration`.

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
