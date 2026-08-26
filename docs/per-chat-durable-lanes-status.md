# Per-Chat Durable Lanes Status

This is the canonical checkpoint for the independent workstream that fixes
`KeyedAsyncLockRegistry.lock_all()` and safely enables bounded per-chat durable
message processing. It does not reopen, replace, or amend the completed runtime
serialization remediation status.

Before any implementation edit, a session must read this file and
`docs/plans/2026-08-23-per-chat-durable-lanes.md`, pass the claim gate, and commit
an exclusive claim to this file. The implementation base is the current commit
returned by:

```bash
git log -1 --format=%H -- docs/per-chat-durable-lanes-status.md
```

The working HEAD must equal that status commit before claiming. Upstream and the
remote deploy branch must separately equal the exact remote baseline stated by
the handoff; local planning-only commits may be ahead. A mismatch is a stop
condition, not permission to pull, reset, stash, clean, merge, or repair.

```yaml
project: per-chat-durable-lanes
design_doc: docs/plans/2026-08-25-per-chat-activation-event-loop-optimization-design.md
implementation_plan: docs/plans/2026-08-25-per-chat-activation-event-loop-optimization.md
canonical_status: docs/per-chat-durable-lanes-status.md
original_remediation_status: docs/runtime-serialization-remediation-status.md
deploy_branch: codex/deepcoin-auto-trading-v1
integration_branch: codex/phase0-deploy-integration
source_baseline: bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7
remote_baseline_at_planning: bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7
approved_design_commit: 9707109dfd1f0815dec6edbc8809fa3fb89a00a0
workstream_status: in_progress
claimed_by: codex-per-chat-phase7-safe-retry-20260826T214219Z-root
claim_base_sha: 56bebd363dd074cf3db1818978e8ebbdb74c94a7
current_task: phase-7-safe-retry-in-progress
current_phase: phase_7_cutover_acceptance
current_phase_file: docs/plans/2026-08-25-per-chat-activation-event-loop-optimization/phase-7-cutover-acceptance.md
last_completed_phase: phase_6_compatible_deployment
phase_1_status: local_complete
phase_1_authorization: local_code_and_tests_only
phase_1_candidate_commit: 3d5e05aeb4d439654ee9ed24b5bfa3158d0354bd
phase_1_invalidated_review_candidate: 0dc08425693b16a8c903d66b974380d86ff56b02
phase_1_commits:
  - bc52549e7ebf244eacd189efadc7ac7d57395d4f
  - 70107ad84cf133b5d1ee798a6532cf8fde2995c7
  - 1e403b3b7e8e371340c63874143b365979643aba
  - 0dc08425693b16a8c903d66b974380d86ff56b02
  - 3d5e05aeb4d439654ee9ed24b5bfa3158d0354bd
phase_1_independent_review_status: ready_zero_findings_after_red_green_hardening
phase_2_authorization: local_code_tests_status_and_commits_only
phase_2_remote_gate_baseline: d66afadda5e34db80851a0dae5986b622521ab3f
phase_2_status: local_complete
phase_2_candidate_commit: 592c0e9d6537c5e2f58c15cd495b6767a32b3da4
phase_2_commits:
  - 396bcb4606fe079a1a12e601bfa1a1f9c4db7f0b
  - 592c0e9d6537c5e2f58c15cd495b6767a32b3da4
phase_2_independent_review_status: ready_zero_findings_after_phase_3_review
phase_3_authorization: local_code_tests_status_and_commits_only
phase_3_remote_gate_baseline: d66afadda5e34db80851a0dae5986b622521ab3f
phase_3_status: local_complete
phase_3_candidate_commit: e37146eaea03befac6457fa224e9dad0cd6c7166
phase_3_invalidated_review_candidate: 77d570ee2187c7e7bbbaf53b6a55f1d0efb135de
phase_3_commits:
  - 77d570ee2187c7e7bbbaf53b6a55f1d0efb135de
  - e37146eaea03befac6457fa224e9dad0cd6c7166
phase_3_independent_review_status: ready_zero_findings_after_red_green_repair
phase_4_authorization: production_read_only_checks_local_evidence_status_and_local_commits_only
phase_4_remote_gate_baseline: d66afadda5e34db80851a0dae5986b622521ab3f
phase_4_status: completed
phase_4_production_sha: d66afadda5e34db80851a0dae5986b622521ab3f
phase_4_evidence_path: /Users/steven/.codex/evidence/per-chat-phase4-batch150-read-only-20260826T052707Z/production-read-only-gate.txt
phase_4_evidence_sha256: 4fb2a8e57f74e2f44f8bb2e29827f84c9a909480c48e0fc7800b49e116c399dd
phase_5_status: completed
phase_5_authorization: completed_production_read_only_checks_local_evidence_status_and_local_commits_only
phase_5_production_sha: d66afadda5e34db80851a0dae5986b622521ab3f
phase_5_production_tracked_dirty_count: 0
phase_5_production_untracked_historical_backup_count: 15
phase_5_evidence_path: /Users/steven/.codex/evidence/per-chat-phase5-trigger-intents-read-only-20260826T054000Z/production-read-only-gate.txt
phase_5_evidence_sha256: 0d9a31aced419dce9ebfc35d3a90e5368bd30127d0849e502e8c9ef4d738f344
phase_5_identity_investigation_evidence_path: /Users/steven/.codex/evidence/per-chat-phase5-trigger-intents-read-only-20260826T054000Z/production-checkout-diff-read-only.txt
phase_5_identity_investigation_evidence_sha256: de07b8b638a9699000131ab48819a8804db4c7a7f656a43dba467a89be16463f
phase_6_status: completed
phase_6_authorization: completed_exact_candidate_non_force_push_compatible_deployment_and_l2_read_only_verification_only
phase_6_candidate_commit: 8cccfbb1683894459368cec4ca64a0cf626a1e9a
phase_6_deployed_commit: 8cccfbb1683894459368cec4ca64a0cf626a1e9a
phase_6_window_start: 2026-08-26T06:06:06.946178+00:00
phase_6_window_end: 2026-08-26T06:36:07.108192+00:00
phase_6_natural_message_count: 8
phase_6_distinct_chat_count: 4
phase_6_peak_active_chat_lanes: 2
phase_6_focused_verification: "111 passed; 28 passed, 179 deselected; 5 passed, 260 deselected"
phase_6_noop_expected_state_status: passed_http_200_unchanged_global_20_queue
phase_6_stale_expected_state_status: passed_http_409_no_row_change
phase_6_pipeline_parity_status: passed_8_raw_8_succeeded_jobs_zero_missing_orphan_stuck_duplicates
phase_6_exchange_parity_status: passed_two_complete_worker_owned_read_only_snapshots_identical
phase_6_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase6-compatible-deploy-20260826T055717Z/phase6-evidence.log
phase_6_evidence_sha256: 7ed5d4baa4086f80586c4a27042f6158ac9a664c627b38d0040f891b79b36023
phase_7_status: rolled_back_incomplete
phase_7_latest_safe_retry_authorization: owner_authorized_exact_56bebd36_canonical_95a88371_production_claim_single_cutover_convergence_two_hour_acceptance_and_l2_rollback
phase_7_authorization: consumed_owner_authorized_new_safe_retry_global_1_to_per_chat_3
phase_7_ingest_stall_remediation_authorization: consumed_owner_authorized_local_red_green_root_cause_minimal_fix_tests_status_and_commits_only
phase_7_ingest_stall_remediation_status: deployed_verified_attribution_race_fixed_under_test_underlying_blocking_function_still_unknown
phase_7_ingest_stall_remediation_claim_commit: c633a9aff92c86a5c767a936d987b8675ce5fed0
phase_7_ingest_stall_remediation_plan_commit: 9a952b05b3da2679eb1cdf036cd38b621e47dd3b
phase_7_ingest_stall_remediation_candidate_commit: 37907849223a3e4b52086f3d162109fb8e7c5c3b
phase_7_ingest_stall_remediation_red_verification: one_failed_checkin_completed_before_frame_snapshot_and_stack_was_discarded
phase_7_ingest_stall_remediation_green_verification: one_passed_then_28_passed_259_deselected
phase_7_ingest_stall_remediation_full_suite_verification: 6342_passed_1_skipped_32_warnings_in_476_95_seconds
phase_7_telethon_entity_cache_authorization: owner_authorized_scheme_a_local_design_plan_red_green_tests_status_and_commits_only
phase_7_telethon_entity_cache_status: deployed_verified_zero_traffic_l1_awaiting_natural_message_evidence
phase_7_telethon_entity_cache_claim_commit: 9dd0706afed1e51bbaba6d751b92d6f72ba2d8fa
phase_7_telethon_entity_cache_design_commit: c32d3b64bb09890be2b14cd899d853f7fe37243d
phase_7_telethon_entity_cache_plan_commit: e5fdc5b5e512941a6f381e9a7a7870c4751a57c9
phase_7_telethon_entity_cache_candidate_commit: d79669b471bd88dd0404faee9485d472de358d00
phase_7_telethon_entity_cache_root_cause_status: local_real_sqlite_lock_reproduced_exact_process_entities_five_second_block_production_function_level_attribution_pending
phase_7_telethon_entity_cache_red_verification: process_entities_raised_database_locked_after_5_55_seconds_under_real_exclusive_entity_table_lock
phase_7_telethon_entity_cache_green_verification: 2_passed_in_0_15_seconds_entity_lock_avoided_and_update_state_survived_session_reopen
phase_7_telethon_entity_cache_focused_verification: 76_passed_in_4_04_seconds
phase_7_telethon_entity_cache_full_suite_verification: 6344_passed_1_skipped_32_warnings_in_533_71_seconds
phase_7_telethon_entity_cache_source_boundary: one_factory_assignment_disables_optional_entity_cache_writes_while_retaining_sqlite_auth_dc_and_update_state_persistence
phase_7_telethon_entity_cache_production_boundary: no_push_no_deploy_no_restart_no_cutover_no_telegram_or_exchange_calls
phase_7_telethon_entity_cache_deployment_authorization: consumed_owner_confirmed_exact_sha_deploy_one_updater_restart_and_bounded_natural_traffic_verification
phase_7_telethon_entity_cache_deployed_commit: 95a883715881b4fd393fbf5e745693cc78e066df
phase_7_telethon_entity_cache_server_focused_verification: 76_passed_22_warnings_in_28_01_seconds
phase_7_telethon_entity_cache_observation_start: 2026-08-26T20:21:41.444900+00:00
phase_7_telethon_entity_cache_observation_end: 2026-08-26T20:36:41.557076+00:00
phase_7_telethon_entity_cache_observation_stop_reason: fixed_fifteen_minute_deadline
phase_7_telethon_entity_cache_observation_samples: 181
phase_7_telethon_entity_cache_observation_natural_message_count: 0
phase_7_telethon_entity_cache_observation_distinct_chat_count: 0
phase_7_telethon_entity_cache_observation_stall_counts: ingest_0_worker_0_web_0
phase_7_telethon_entity_cache_observation_status: passed_stability_only_zero_traffic_does_not_prove_natural_message_path
phase_7_telethon_entity_cache_deployment_evidence_path: /opt/telegram-kol-analyzer/data/evidence/phase7-telethon-entity-cache-deploy-20260826T202141Z/observation-evidence.jsonl
phase_7_telethon_entity_cache_deployment_evidence_sha256: 7df2a84808235c833320af4538c76069a1f6f9f5505d9f669bb6abe4b949fd7a
phase_7_ingest_stall_watcher_status: active_read_only_baseline_updated_to_95a883715881b4fd393fbf5e745693cc78e066df
phase_7_ingest_stall_deployment_authorization: consumed_owner_continue_next_exact_fc8baaad_one_split_restart_global_1_l1_only
phase_7_ingest_stall_deployed_commit: fc8baaad2e677fe0536c0c7211e2ae9d0cc915d4
phase_7_ingest_stall_server_focused_verification: 28_passed_259_deselected_7_warnings_in_6_74_seconds
phase_7_ingest_stall_observation_start: 2026-08-26T18:29:57.989132+00:00
phase_7_ingest_stall_observation_end: 2026-08-26T18:45:24.951535+00:00
phase_7_ingest_stall_observation_stop_reason: fifteen_minute_deadline
phase_7_ingest_stall_observation_samples: 180
phase_7_ingest_stall_observation_natural_message_count: 0
phase_7_ingest_stall_observation_stall_count: 0
phase_7_ingest_stall_deployment_evidence_path: /opt/telegram-kol-analyzer/data/evidence/phase7-ingest-stall-attribution-deploy-20260826T182957Z/observation-evidence.jsonl
phase_7_ingest_stall_deployment_evidence_sha256: 1c9cdd16d3561b0b0db10be4758bada5dd1fdfc8c4e7927593f7022d868be807
phase_7_observer_fix_authorization: owner_authorized_local_design_plan_code_tests_status_and_commits_only
phase_7_observer_design_commit: c1edfb14b00730fc72eec225a93313f7e5ea67dd
phase_7_observer_plan_commit: 753c401c37e81a7620a02843c091fe5ade1727f9
phase_7_observer_claim_commit: d0a99946e8c4db2760db34c174d06c4544ff0821
phase_7_observer_candidate_commits:
  - 9fd5db290ced735786227f6ebc462979525a5753
  - 96a8d78e17ba45613580eedf5cbedee21fbd319f
  - 5212d96899f75fb07dbe6ad5944530b355c8c421
  - d2c1ee6c4b42948694836c112a6691183474a466
  - 513334ac5dbefb8942f2a823636a0307f9838902
phase_7_observer_red_verification: "RED confirmed missing contracts or entrypoints before each implementation batch; collector RED was 4 failed and 27 passed; CLI RED was 6 failed and 31 passed"
phase_7_observer_green_verification: "37 passed in focused observer suite; 6 passed in existing durable ordering regression slice; compileall and diff check passed"
phase_7_observer_source_boundary: "SQLite file URI mode=ro plus query_only=ON; HTTP collection uses urlopen GET only; JSONL writes only to stdout; no Request method override, POST, subprocess, service control, worker action, evidence-file output, rollback execution, Telegram send, replay, or exchange-write path"
phase_7_observer_status: pushed_exact_7a60aa2_stream_only_not_deployed
phase_7_blocker_remediation_authorization: completed_local_diagnosis_code_tests_status_and_commits_only
phase_7_blocker_fix_commit: a9545a1b16c5132b789c805d03680d203a9a0440
phase_7_blocker_fix_deployment_authorization: consumed_exact_sha_deploy_one_restart_l1_verification
phase_7_blocker_fix_deployed_commit: 7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9
phase_7_blocker_fix_deployment_status: passed_exact_sha_split_runtime_restart_and_l1_observation
phase_7_blocker_fix_deployment_focused_verification: 13_passed_then_13_passed_260_deselected_6_warnings
phase_7_blocker_fix_observation_start: 2026-08-26T08:22:01.604993+00:00
phase_7_blocker_fix_observation_end: 2026-08-26T08:37:18.191347+00:00
phase_7_blocker_fix_observation_stop_reason: fifteen_minutes
phase_7_blocker_fix_observation_samples: 177
phase_7_blocker_fix_observation_natural_message_count: 1
phase_7_blocker_fix_observation_distinct_chat_count: 1
phase_7_blocker_fix_observation_peak_active_chat_lanes: 1
phase_7_blocker_fix_deployment_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-blocker-fix-deploy-20260826T081637Z/deploy-evidence.log
phase_7_blocker_fix_deployment_evidence_sha256: ee0aa0b41929c2892439e70387965b9e6e1c31bd64f44f70eb7701d0418d4716
phase_7_blocker_red_verification: one_failed_expected_stale_stack_was_not_discarded
phase_7_blocker_focused_verification: 286_passed_2_warnings_then_21_passed
phase_7_blocker_full_suite_verification: 6304_passed_1_skipped_32_warnings_in_526_53_seconds
phase_7_blocker_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-blocker-diagnosis-20260826T075147Z/diagnosis-evidence.log
phase_7_blocker_evidence_sha256: 4dd3ad9cf3e91aed66ebd6b3d9b7660979d623379bf51ca9615b0f28b7bef0dc
phase_7_production_sha: 7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9
phase_7_before_tuple: global_1_queue
phase_7_cutover_tuple: per_chat_3_queue
phase_7_final_tuple: global_1_queue
phase_7_cutover_http_status: 200
phase_7_rollback_occurred: true
phase_7_rollback_http_status: 200
phase_7_failure_reason: actual_ingest_event_loop_stall_4473_963ms_caused_two_consecutive_ingest_settings_timeouts
phase_7_acceptance_window_status: failed_closed_at_2056_217122_seconds_then_l2_rolled_back_no_window_stitching
phase_7_retry_convergence_status: passed_three_consecutive_samples_by_sample_4_in_0_893838_seconds
phase_7_retry_convergence_samples:
  - "sample_1_elapsed_0.051409_db_api_per_chat_3_worker_cap_1_old_limit"
  - "sample_2_elapsed_0.332932_db_api_per_chat_3_worker_cap_3_new_limit_peak_0"
  - "sample_3_elapsed_0.613592_db_api_per_chat_3_worker_cap_3_new_limit_peak_0"
  - "sample_4_elapsed_0.893838_db_api_per_chat_3_worker_cap_3_new_limit_peak_0_third_consecutive_success"
phase_7_retry_remaining_gate_blockers: natural_message_evidence_under_deployed_entity_cache_fix_then_separate_safe_retry_authorization
phase_7_window_start: 2026-08-26T16:59:28.134926+00:00
phase_7_window_end: 2026-08-26T17:33:44.352048+00:00
phase_7_natural_message_count: 1
phase_7_distinct_chat_count: 1
phase_7_peak_active_chat_lanes: 1
phase_7_ordering_status: one_natural_job_succeeded_without_observed_overlap_but_insufficient_traffic_before_real_loop_stall
phase_7_backlog_status: max_1_final_pending_0_claimed_0_five_historical_shadow_pending_excluded
phase_7_duplicate_status: zero_missing_orphan_duplicate_job_duplicate_decision_or_bad_contract_before_failure
phase_7_sqlite_status: final_wal_quick_check_ok_query_only_1_total_changes_0_foreign_keys_0
phase_7_loop_status: failed_actual_ingest_stall_delta_1_to_2_at_2026_08_26T17_33_46_830341Z_4473_963ms_worker_web_unchanged_final_ingest_web_worker_2_1_0
phase_7_session_status: final_ingest_only_one_holder_pid_404790
phase_7_exchange_status: complete_worker_owned_baseline_end_identical_zero_positions_zero_open_orders
phase_7_previous_attempt_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-cutover-acceptance-20260826T065802Z/phase7-evidence.log
phase_7_previous_attempt_evidence_sha256: ad3d14aa04805a7187d5ca289e5a63ff9b681c269e9453b2137ace346bff127b
phase_7_postfix_attempt_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-postfix-safe-retry-20260826T085214Z/phase7-retry-evidence.log
phase_7_postfix_attempt_evidence_sha256: 566892624f51c82dcdb961a3577888915780f029d73a2a5d3e8445ae567ce1cf
phase_7_evidence_path: /opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-versioned-observer-retry-20260826T165900Z/phase7-evidence.log
phase_7_evidence_sha256: 26669603056989f68b477a438091ea3b5b69cb3f5f48506e0410ffaa06d408cb
phase_1_stop_conditions:
  - recognition_strategy_execution_or_exchange_semantics_change_required
  - schema_or_production_data_change_required
  - push_deploy_restart_or_production_action_required
verification_level: L2
local_candidate_commit: e37146eaea03befac6457fa224e9dad0cd6c7166
invalidated_local_candidate_commits:
  - c8f778201c123f0bbadddc06e718945307adf40b
  - c0e2471ed76b6d73bceb3be3d88304e57e44088d
  - 4490ec2c2e3adad3268a155376d5ba0da6c0b045
  - eb9ff4c261080190e3f6d360724aec05395197ed
  - de0ae43498dc5b330f3e61c70eb8ebb27d50b269
  - 78cc24dae7e2bf3b341c4f5ecdf28b9cf5de0284
  - e03622749c32ebb214af56cd118984268e72af56
local_focused_verification: "Phase 3 final consolidated focused set: 705 passed, 2 warnings in 57.33s"
local_full_suite_verification: "Phase 3 final candidate: 6303 passed, 1 skipped, 32 warnings in 506.37s"
local_compileall_verified: true
local_diff_check_verified: true
trigger_protection_candidate_commit: 130de7bbaff5abe28c912f60a554fe39be451ecd
trigger_protection_rehearsal_status: passed_copy_only_exact_three_rows
trigger_protection_rehearsal_evidence: /Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-20260825T025238Z/rehearsal-summary.json
trigger_protection_backup_sha256: de4926231f0c608028abd74ea9575b4448109ef307f0122477714baccfd27fe3
trigger_protection_production_apply_plan_status: not_built_not_authorized
trigger_protection_review_test_commit: 1992312cebfbf2496545aa582ff0786d372d4a1b
trigger_protection_independent_review_status: passed_no_critical_or_important
batch150_terminalization_tool_commit: 868bbf378d77960a05dd199b3c1df6b6cb78621b
batch150_rehearsal_status: passed_copy_only_volatile_cas
batch150_rehearsal_evidence: /opt/telegram-kol-analyzer/data/evidence/batch150-volatile-cas-rehearsal-20260824T224153Z/rehearsal-summary.json
batch150_production_apply_plan_status: not_built_not_authorized
production_lock_mode_at_planning: global
compatibility_parallel_chat_limit: 20
target_parallel_chat_limit: 3
fail_closed_parallel_chat_limit: 1
schema_change_planned: false
production_data_mutation_planned: false
exchange_write_semantics_change_planned: false
deployment_authorized: false
cutover_authorized: false
```

## Fixed Boundaries

- `message_processing_jobs` remains the only durable queue and ordering
  authority.
- same-chat order remains the oldest non-terminal `raw_message_id` in the
  existing claim transaction.
- `KeyedAsyncLockRegistry` is process-local and must never be reported as a
  cross-process lock.
- the worker cap is per single worker process; production acceptance requires
  exactly one worker authority.
- production deployment requires separate authorization for an exact 40-hex
  SHA.
- production cutover requires a separately satisfied quiet-window gate.
- no historical replay, manufactured Telegram traffic, test trade, or extra
  exchange write is permitted.
- no DeepSeek semantic review or context fallback may be enabled.
- any schema/data-mutation requirement stops the L2 workstream.
- never run `git add -A`; stage and inspect exact paths.

## Approved Rollback

- lock/admission/ingest failure: atomically restore `message_lock_mode=global`
  while retaining cap 3;
- scheduler/duplicate/SQLite/execution/concurrency failure: atomically restore
  `message_lock_mode=global` and cap 1;
- neither rollback requires a restart;
- an unknown response requires an exact read before one reasoned retry.

## Acceptance Minimum

- final local production candidate: one complete suite after the last
  production-code edit;
- deployment remains global with compatibility cap 20 until cutover;
- cutover is one expected-state write to `per_chat + 3`;
- continuous two-hour natural-message observation;
- at least five natural messages, with at least two chats attempted;
- same-chat non-overlap/order, actual cross-chat progress, peak lanes at most 3;
- backlog convergence and zero duplicate job/decision/execution;
- zero SQLite lock, event-loop stall regression, session conflict,
  DeepSeek/402, and authority drift;
- complete read-only exchange history parity from the worker credential
  boundary;
- insufficient traffic or any failed gate rolls back and leaves the workstream
  incomplete without an automatic waiver.

## History

- `2026-08-26 Phase 7 Telethon entity-cache deployment verification`: after
  the owner confirmed the explicitly proposed next step, exact clean commit
  `95a883715881b4fd393fbf5e745693cc78e066df` was deployed through the repository
  updater with its single required split-runtime stop/start cycle. Pre-deploy
  production was exact clean commit
  `fc8baaad2e677fe0536c0c7211e2ae9d0cc915d4`, with one ingest, worker, and Web
  authority, inactive monolith, ingest-only Telegram session ownership,
  `global + 1 + queue`, semantic review disabled, zero claimed non-shadow jobs,
  and SQLite WAL `quick_check=ok` under `query_only=1`.

  Post-deploy production was exact clean commit
  `95a883715881b4fd393fbf5e745693cc78e066df`. Worker, Web, and ingest PIDs were
  `1898355`, `1898357`, and `1898359`, each with `NRestarts=0`; only ingest held
  the Telegram session. Server focused verification passed `76` tests with 22
  warnings in 28.01 seconds. The fixed L1 window ran from
  `2026-08-26T20:21:41.444900+00:00` to
  `2026-08-26T20:36:41.557076+00:00`, producing 181 complete five-second
  samples. All samples preserved the tuple, authorities, PIDs, zero backlog,
  and zero ingest/worker/Web stalls. No natural message arrived, so this proves
  deployment stability but not the real Telegram message path. Final SQLite
  remained WAL with `quick_check=ok`, no relevant stall/SQLite-lock journal
  line appeared, and the deployed factory contained
  `client.session.save_entities = False`.

  Evidence is
  `/opt/telegram-kol-analyzer/data/evidence/phase7-telethon-entity-cache-deploy-20260826T202141Z/observation-evidence.jsonl`
  with SHA-256
  `7df2a84808235c833320af4538c76069a1f6f9f5505d9f669bb6abe4b949fd7a`.
  The read-only stall watcher now targets this exact deployed SHA. No Phase 7
  cutover, rollback, production setting/data change, replay, worker command,
  manufactured Telegram traffic, test trade, or exchange call occurred. Phase
  7 remains rolled back and incomplete; a new safe retry still requires
  natural-message evidence and separate owner authorization.

- `2026-08-26 Phase 7 ingest-stall attribution deployment verification`:
  after the owner replied `continue next step` to the explicitly bounded next
  action, exact canonical and implementation head
  `fc8baaad2e677fe0536c0c7211e2ae9d0cc915d4` was non-force pushed and then
  deployed through the repository updater. The authorization covered only this
  exact SHA, the updater-required single split-runtime stop/start cycle, and an
  L1 read-only natural-traffic observation while the tuple remained
  `global + 1 + queue`; it did not authorize Phase 7 cutover, rollback,
  production settings/data changes, replay, worker commands, manufactured
  Telegram traffic, test trades, or exchange writes.

  Pre-deploy local HEAD, canonical last commit, upstream, remote-tracking ref,
  and live remote all matched the exact candidate and the local tree was clean.
  Production was clean at
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`, with exactly one ingest, worker,
  and Web process, inactive monolith, ingest as the only Telegram-session
  holder, all three APIs at `global + 1 + queue`, semantic review disabled, and
  active exchange writes at zero. The query-only SQLite retry completed with
  WAL, `quick_check=ok`, `total_changes=0`, zero non-shadow pending/claimed
  jobs, zero active management, zero claimed/executing worker commands, and
  five historical shadow-pending jobs explicitly excluded.

  The updater completed successfully and production reached exact clean SHA
  `fc8baaad2e677fe0536c0c7211e2ae9d0cc915d4`. It started worker, Web, and
  ingest PIDs `1667524`, `1667526`, and `1667529`; all retained zero systemd
  restarts, monolith remained inactive, and only ingest PID `1667529` held the
  Telegram session. Post-deploy server verification passed `28` focused tests
  with `259` deselected and seven warnings in `6.74s`.

  The uninterrupted L1 observation started at
  `2026-08-26T18:29:57.989132+00:00`, sampled every five seconds until the
  fixed fifteen-minute deadline, and recorded `180` complete samples. No
  natural message arrived and no new stall occurred. Every sample preserved
  the exact PID/role/authority and `global + 1 + queue` tuple. Final SQLite was
  WAL, query-only, `quick_check=ok`, and `total_changes=0`; non-shadow queue,
  active management, active worker commands, service journals, and active
  exchange writes remained clear. Evidence is stored at
  `/opt/telegram-kol-analyzer/data/evidence/phase7-ingest-stall-attribution-deploy-20260826T182957Z/observation-evidence.jsonl`
  with SHA-256
  `1c9cdd16d3561b0b0db10be4758bada5dd1fdfc8c4e7927593f7022d868be807`.

  This proves the attribution-race candidate is deployed and stable under the
  bounded zero-traffic window. It does not identify the original blocking
  function or prove that the underlying ingest stall disappeared. Phase 7
  remains rolled back and incomplete; the next safe evidence step is to wait
  for a natural stall while remaining at `global + 1 + queue`, then use its
  frozen function-level stack to decide whether another RED-to-GREEN code fix
  is required. No new safe-retry or Phase 8 authorization exists.

- `2026-08-26 Phase 7 ingest-stall remediation claim`: session
  `codex-per-chat-phase7-ingest-stall-remediation-20260826T1755Z-root`
  exclusively claimed the owner-authorized local RED-to-GREEN investigation at
  exact clean canonical base
  `54dca4b57ee7da80b6566d42b048153d6467eb05`. The local upstream, local
  remote-tracking deploy ref, and live remote deploy branch all remained exact
  `7a60aa2ebe060dd90211c0fafb430044ce4ed30d`; the current integration branch
  was only two local canonical commits ahead. No Git lock, active Git mutator,
  or same-branch worktree owner was present.

  Authorization is limited to directly related ingest runtime and loop-health
  attribution source/tests, one minimal implementation plan, test-first root-
  cause reproduction, the smallest proven local production-code fix, focused
  verification, one final complete suite, canonical status updates, and
  explicit-path local commits. Push, deployment, restart, Phase 7 cutover or
  rollback, production configuration/schema/data changes, Telegram traffic,
  replay, worker commands, test trades, and every exchange write remain
  prohibited.

  Backward tracing confirmed a watchdog attribution race introduced by the
  earlier stale-stack safeguard: `poll_once()` claimed a stall and recorded the
  check-in generation under `_lock`, released that lock, and only then read the
  loop thread frame. A recovering loop could therefore complete
  `note_checkin()` in that gap and force the otherwise useful pre-recovery frame
  to be discarded. A no-file-write concurrent reproduction produced the exact
  production evidence shape: check-in completed before frame snapshot,
  `stack_size=0`, and `event loop recovered before stack capture`.

  The deterministic RED test
  `test_concurrent_recovery_cannot_erase_pre_recovery_blocking_function` first
  failed because the check-in completed before the paused frame provider was
  released and the synthetic ingest blocking frame was erased. Minimal GREEN
  replaces the attributor state lock with `threading.RLock` and keeps stall
  ownership, frame freezing, generation comparison, and capture append in one
  linearized critical section. Logging remains outside the lock. The existing
  same-thread provider test still proves that a recovery which truly happens
  before a frame is returned is discarded rather than misattributed.

  Exact candidate `37907849223a3e4b52086f3d162109fb8e7c5c3b` passed the RED
  target after repair, then the focused attribution/loop-health/Web slice passed
  `28` with `259` deselected in `1.21s`. `git diff --check` and compileall of
  both touched files passed. The one final complete suite on that frozen
  production-code candidate passed `6342`, skipped `1`, and emitted `32`
  existing warnings in `476.95s`. Claim commit is
  `c633a9aff92c86a5c767a936d987b8675ce5fed0`; implementation-plan commit is
  `9a952b05b3da2679eb1cdf036cd38b621e47dd3b`.

  This local candidate closes the recovery/frame attribution race only. It does
  not identify or claim to remove the production ingest function that caused
  the original `4473.963` ms stall. A separate exact-SHA non-force push and a
  separately authorized deployment/restart while production remains
  `global + 1 + queue` are required before a subsequent natural stall can
  produce authoritative function-level evidence. Phase 7 remains rolled back
  and incomplete; no new cutover authorization exists and Phase 8 remains
  prohibited.

- `2026-08-26 Phase 7 versioned-observer safe-retry claim`: session
  `codex-per-chat-phase7-safe-retry-20260826T1652Z-root` exclusively claimed one
  owner-authorized retry at exact clean canonical base
  `7a60aa2ebe060dd90211c0fafb430044ce4ed30d`. Local upstream, local remote-
  tracking, and the live remote deploy branch matched that exact SHA. Production
  remained clean at deployed runtime SHA
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`; the remote-only delta contains
  only the versioned observer, its tests, plans, and canonical status, with no
  deployed production-source change.

  The complete read-only preclaim snapshot found exactly one active/running
  ingest, worker, and Web authority, an inactive monolith, and ingest as the
  only Telegram-session holder. Database and all three APIs were exactly
  `global + 1 + queue + queue`; worker memory was cap `1` with zero active
  lanes. The non-shadow pending/claimed queue, active worker commands, active
  management, and active exchange-write count were all zero. SQLite was WAL,
  `query_only=1`, `quick_check=ok`, `total_changes=0`, with no foreign-key
  violations. The worker-owned exchange snapshot was complete with zero
  positions, zero open orders, and fingerprint
  `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`.

  Cumulative loop-stall baselines are ingest/Web/worker `1/1/0`; the last two
  events were approximately four and six hours before claim, and the current
  rolling one-hour health windows were quiet. They are retained as immutable
  baselines rather than cleared or treated as zero. Any increment during the
  final gate, convergence, or acceptance is a failure. Authorization permits
  one exact `global + 1` to `per_chat + 3` expected-state transition, the fixed
  five-second convergence gate, one continuous two-hour natural-traffic
  acceptance window, the approved necessary rollback, read-only production and
  exchange evidence, and canonical status updates with explicit-path local
  commits. Code/test changes, another push, deployment, restart, schema/data
  changes, replay, worker commands, manufactured traffic, test trades, and
  exchange writes remain prohibited.

  The final quiet gate passed at production SHA
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`. One atomic expected-state
  transition returned HTTP `200` at `2026-08-26T16:59:27.211940+00:00` and
  persisted `per_chat + 3 + queue + queue` immediately. Convergence sample 1 at
  `0.051409` seconds still reported worker cap `1`; samples 2, 3, and 4 at
  `0.332932`, `0.613592`, and `0.893838` seconds reported cap `3`, the new
  `limit_applied_at=2026-08-26T16:59:27.465981+00:00`, stable authority PIDs,
  and lane bounds at or below `3`. The third consecutive complete sample passed
  the fixed convergence gate. The full acceptance window then started from zero
  at `2026-08-26T16:59:28.134926+00:00`; convergence time was not counted.

  At `2026-08-26T17:33:44.352048+00:00`, `2056.217122` seconds into that
  uninterrupted window, both the initial sample and its sole allowed retry
  timed out on the first ingest `/api/trading-settings` GET. The ingest journal
  recorded a real `4473.963` ms event-loop stall at
  `2026-08-26T17:33:46.830341+00:00`, increasing its immutable stall baseline
  from `1` to `2`; worker and Web counts remained `0` and `1`. The ingest event
  loop recovered before stack capture, so the sampled stack was correctly
  discarded and the exact blocking function remains unknown. This is a real
  Phase 7 loop-health failure, not an observer false positive.

  The observer immediately submitted the approved L2 atomic rollback. It
  returned HTTP `200` at `2026-08-26T17:33:46.867054+00:00`; DB and all APIs
  were already `global + 1 + queue + queue` in rollback sample 1, and worker
  memory reached cap `1` by sample 2 at `0.337368` seconds. Three consecutive
  complete cap-`1` samples passed at `0.912043` seconds without a restart or PID
  change. No automatic recutover occurred.

  Post-rollback read-only verification found the same three authority PIDs,
  inactive monolith, the sole Telegram session held by ingest, zero non-shadow
  pending/claimed jobs, zero active worker commands, management, or exchange
  writes, and five excluded historical shadow-pending jobs. SQLite remained WAL
  with `query_only=1`, `quick_check=ok`, `total_changes=0`, and no foreign-key
  violations. The one natural message from one chat produced one succeeded job
  with zero missing, orphan, duplicate job/decision, or bad contract; there were
  no new execution events. The worker-owned exchange snapshot remained complete
  and identical to baseline with zero positions and zero open orders. Evidence
  is `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-versioned-observer-retry-20260826T165900Z/phase7-evidence.log`
  with SHA-256
  `26669603056989f68b477a438091ea3b5b69cb3f5f48506e0410ffaa06d408cb`.
  Phase 7 remains rolled back and incomplete. A separately authorized local
  RED-to-GREEN investigation/fix must first capture and remove the ingest stall,
  followed by separately authorized push/deployment and another owner-authorized
  safe retry. Phase 8 remains prohibited.

- `2026-08-26 Phase 7 acceptance-observer fix claim`: session
  `codex-per-chat-phase7-observer-fix-20260826-root` exclusively claimed the
  owner-authorized local observer remediation at exact clean canonical base
  `8e4b3a8ed8720495067dbb8d8e03ab15cc232a96`. The accepted design boundary is
  a versioned read-only observer whose state machine distinguishes a later
  same-chat pending enqueue from an actual claimed overlap and whose rollback
  convergence check is independent of failed acceptance invariants. The scope
  permits local design and implementation plans, RED-to-GREEN observer code and
  focused tests, canonical status updates, and explicit-path local commits. It
  does not authorize production access or mutation, push, deployment, restart,
  cutover, rollback, schema/data changes, replay, worker commands, manufactured
  Telegram traffic, test trades, or exchange writes.

- `2026-08-26 Phase 7 post-fix safe-retry rollback`: all preflight gates passed
  against exact production SHA
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`. The worker-owned exchange
  baseline was complete with zero positions, zero open orders, and fingerprint
  `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`.
  One ingest-owned expected-state request changed `global + 1 + queue` to
  `per_chat + 3 + queue` and returned HTTP `200` at
  `2026-08-26T08:55:47.828639+00:00`. Database and API persistence were
  immediate. Worker sample 1 still reported cap `1`; samples 2, 3, and 4
  reported cap `3`, zero active lanes, peak `0`, and new
  `limit_applied_at=2026-08-26T08:55:48.021057+00:00`. The third consecutive
  convergence sample completed after `0.914874` seconds. No convergence time
  was counted toward acceptance.

  The independent acceptance window began at
  `2026-08-26T08:59:52.921506+00:00`. At
  `2026-08-26T09:42:38.140941+00:00`, after `2565` seconds, the observer
  reported `same_chat_violations`; the window then contained three natural
  messages from two chats, peak active lanes `1`, and maximum backlog `2`.
  The approved scheduler/concurrency rollback immediately issued one atomic
  `per_chat + 3 + queue` to `global + 1 + queue` transition and received HTTP
  `200`. The observer labelled rollback confirmation incomplete because its
  confirmation samples reused the already-failed same-chat invariant. A fresh
  independent read proved the rollback itself had converged: database and all
  three APIs were `global + 1 + queue`, worker memory was cap `1`, active `0`,
  peak `1`, with new
  `limit_applied_at=2026-08-26T09:42:38.433385+00:00`. There was no second
  cutover and no restart.

  Read-only diagnosis classified the acceptance failure as an observer false
  positive, not a production scheduler violation. While the earlier job from
  chat `-1002805019371` was claimed, the next job from that chat remained
  `pending`; every runtime sample reported active and peak lanes at most `1`.
  The earlier recognition decision updated at
  `2026-08-26T09:42:37.798151+00:00`, before the later job's recorded terminal
  tick at `2026-08-26T09:42:37.882049+00:00`. Deployed claim SQL selects only
  the oldest pending-or-claimed lane owner per chat. It is valid for a later
  same-chat message to enqueue while the prior lane is active; the observer
  incorrectly treated that pending enqueue as processing overlap. The worker
  also writes a tick-start value to job `completed_at`, so that column is not a
  standalone wall-clock processing-end boundary for an external overlap query.

  The final queue had zero non-shadow pending or claimed jobs. Exactly one
  ingest, worker, and Web authority retained PIDs `404790`, `404786`, and
  `404788`; monolith remained inactive and only ingest held the Telegram
  session. All roles reported zero stalls. SQLite was `wal`, `query_only=1`,
  `quick_check=ok`, `total_changes=0`, with zero foreign-key violations;
  active management and worker commands were zero. The complete worker-owned
  exchange end snapshot was identical to baseline. Raw evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-postfix-safe-retry-20260826T085214Z/phase7-retry-evidence.log`,
  SHA-256
  `566892624f51c82dcdb961a3577888915780f029d73a2a5d3e8445ae567ce1cf`.
  Phase 7 remains rolled back and incomplete; Phase 8 is forbidden. A corrected
  acceptance observer and a new explicit safe-retry authorization are required.
  No code/test edit, push, deployment, restart, schema/data mutation, replay,
  worker command, manufactured Telegram traffic, test trade, or exchange write
  occurred.

- `2026-08-26 Phase 7 post-fix safe-retry claim`: session
  `codex-per-chat-phase7-safe-retry-20260826T084816Z-root` exclusively claimed
  one owner-authorized Phase 7 retry at exact clean canonical base
  `568fb78710ab0fb3e4b27c5589377854111353d8`. Local upstream, the local
  remote-tracking deploy ref, the live remote deploy branch, and production
  HEAD all resolved exactly to deployed fix commit
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`. Production tracked status was
  clean with no Git, updater, or deployment mutation; exactly one active/running
  ingest, worker, and Web authority had distinct PIDs, the monolith was inactive,
  and only ingest held the Telegram session. The API tuple was exactly
  `global + 1 + queue`, semantic review was disabled, all three roles reported
  zero stalls, and worker memory reported cap `1`.

  Authorization is limited to fresh complete read-only gates, one exact
  ingest-owned transition from `global + 1 + queue` to `per_chat + 3 + queue`,
  the non-stitchable five-second convergence gate, one complete continuous
  two-hour natural-traffic acceptance window after convergence, the approved
  atomic rollback, worker-owned read-only exchange baseline/end parity, raw
  evidence, canonical status updates, and explicit-path local commits. Code or
  test changes, push, deployment, restart, schema/data changes, replay, worker
  commands, manufactured Telegram traffic, test trades, and exchange writes
  remain unauthorized.

- `2026-08-26 Phase 7 blocker-fix exact deployment completion`: the owner
  separately authorized exact deployment commit
  `7ca03ac2839420b9d4b22ab13f16a52ebcbc0ef9`, one updater-required split-runtime
  restart, focused server verification, and an L1 observation. The clean local
  HEAD, canonical status commit, upstream, remote-tracking deploy ref, and live
  remote deploy branch all matched that exact SHA before deployment. Production
  was clean at prior commit `8cccfbb1683894459368cec4ca64a0cf626a1e9a`,
  exactly one ingest, worker, and Web authority were active, the monolith was
  inactive, only ingest held the Telegram session, the complete tuple remained
  `global + 1 + queue`, semantic review was disabled, and active exchange writes,
  active management, and active worker commands were zero. One natural non-shadow
  message job was initially claimed; a bounded read-only quiet gate observed it
  naturally clear on the first sample before the updater ran.

  The existing verified bootstrap updater fetched and verified the exact remote
  commit and updater hash, passed its topology, clean-checkout, and active-write
  gates, fast-forwarded production once, and completed one necessary managed
  stop/start cycle. Production HEAD, branch ref, remote-tracking ref, and monitor
  expected-HEAD pin then all matched the authorized SHA. New ingest, worker, and
  Web PIDs `404790`, `404786`, and `404788` remained stable through observation;
  the three roles stayed active/running, the monolith stayed inactive, and only
  ingest held the session. The database and API remained `global + 1 + queue`,
  semantic review stayed disabled, and the worker applied cap `1` at
  `2026-08-26T08:18:25.030388+00:00`.

  Exact deployed source inspection found the check-in generation and recovered-
  stack discard path. The focused stall-attribution test passed `13`; the focused
  runtime/Web loop-health slice passed `13` with `260` deselected and six existing
  warnings. The continuous L1 window ran from
  `2026-08-26T08:22:01.604993+00:00` through
  `2026-08-26T08:37:18.191347+00:00`, sampled `177` times, and ended on the fixed
  fifteen-minute criterion because only one natural message from one chat arrived.
  That job succeeded, final non-shadow pending and claimed counts were zero, and
  worker peak active lanes was `1`. There were zero incomplete-query retries,
  duplicate new jobs, loop stalls, SQLite locks, Telegram session conflicts,
  DeepSeek/402 events, authority drift, active exchange writes, active management,
  or active worker commands. Final SQLite evidence was `wal`, `query_only=1`,
  `quick_check=ok`, `total_changes=0`, with zero foreign-key violations. The five
  historical `shadow=1` pending rows were explicitly excluded by the worker queue
  contract.

  Raw evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-blocker-fix-deploy-20260826T081637Z/deploy-evidence.log`,
  SHA-256
  `ee0aa0b41929c2892439e70387965b9e6e1c31bd64f44f70eb7701d0418d4716`.
  No cutover, rollback, settings/schema/data change, replay, worker command,
  manufactured Telegram traffic, test trade, exchange write, code/test edit, or
  post-deployment push occurred. Phase 7 remains incomplete and requires a new,
  separately authorized safe retry; Phase 8 remains forbidden.

- `2026-08-26 Phase 7 blocker-remediation local completion`: production
  read-only diagnosis proved that the five apparent pending jobs were all
  historical `shadow=1` reconcile rows from 2026-08-20. The queue worker claim
  contract filters `shadow=0`; current queue pending and claimed counts were
  both zero. Therefore the prior unqualified pending count was a diagnostic
  scope error, not a worker scheduling or backlog defect, and it is not a
  remaining Phase 7 gate blocker.

  The Web loop stall was a real single event at
  `2026-08-26T07:30:33.765410+00:00`, with
  `worst_stall_ms=5653.293`. It did not reproduce: a bounded localhost
  positions-panel read completed in `0.060974` seconds and Web `stall_count`
  remained `1`; the journal contained exactly one stall warning since the
  event. The old `selector.poll()` stack is not authoritative attribution.
  `LoopStallAttributor.poll_once()` marked the episode under its lock but
  released that lock before reading the loop frame, allowing the loop to check
  in and advance before the watchdog formatted the stack. The observed warning
  and stack-log ordering matched this recovery race.

  RED-to-GREEN remediation added a check-in generation and discards a sampled
  stack with an explicit `event loop recovered before stack capture` reason if
  the loop recovered during capture. The focused regression first failed
  because the recovered stack remained populated, then passed after the
  minimal fix. Related loop-health and Web tests passed `286` with two existing
  warnings; the final focused set passed `21`; the one final complete suite on
  exact code candidate `a9545a1b16c5132b789c805d03680d203a9a0440` passed
  `6304`, skipped `1`, and emitted `32` existing warnings in `526.53s`.
  Production diagnosis evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-blocker-diagnosis-20260826T075147Z/diagnosis-evidence.log`,
  SHA-256
  `4dd3ad9cf3e91aed66ebd6b3d9b7660979d623379bf51ca9615b0f28b7bef0dc`.

  This local repair is not pushed or deployed. Production remains exact commit
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a` at
  `global + 1 + queue`; no service was restarted and no Phase 7 cutover was
  attempted. A push, exact-SHA deployment/restart with its own verification,
  and a separately authorized Phase 7 retry remain required. No production
  setting/schema/data change, worker command, replay, manufactured traffic,
  Telegram business message, test trade, or exchange write occurred.

- `2026-08-26 Phase 7 blocker-remediation claim`: session
  `codex-per-chat-phase7-blocker-repair-20260826-root` claimed the failed
  pre-cutover Web loop-stall and pending-job blockers at exact clean canonical
  base `61a5e9cfab14d4c3150c7ef7c2390ff4ec07874f`. Local upstream, the local
  remote-tracking deploy ref, the live remote deploy branch, and production
  HEAD remained exactly
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a`; production remained
  `global + 1 + queue`. Authorization is limited to read-only production root-
  cause evidence, minimal RED-to-GREEN local code/tests if a defect is proven,
  canonical status updates, and explicit-path local commits. Push, deployment,
  restart, cutover, rollback, production settings/schema/data changes, worker
  commands, replay, manufactured traffic, Telegram business messages, test
  trades, and exchange writes remain unauthorized.

- `2026-08-26 Phase 7 safe-retry pre-cutover stop`: the claimed retry repeated
  the immediate local identity check successfully at local canonical claim
  commit `0038675ff524a246ef36e5cff0f1ef6b27d81ac4`; local upstream, the local
  remote-tracking deploy ref, the live remote deploy branch, and production
  HEAD still resolved exactly to
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a`. Before any expected-state POST,
  the fresh production loop-health gate found that Web had recorded
  `stall_count=1`, `last_stall_at=2026-08-26T07:30:33.765410+00:00`, and
  `worst_stall_ms=5653.293`. The captured watchdog sample reported
  `blocked_ms=6151.834`. This violated the required zero-stall pre-cutover gate,
  so the monitor stopped fail closed at `2026-08-26T07:36:53.196418+00:00`.

  No settings POST was submitted, no cutover or rollback occurred, the
  five-second convergence gate did not start, and the two-hour acceptance
  window did not start. The post-stop database and ingest API both remained
  exactly `global + 1 + queue`; worker memory remained cap `1` with the same
  `limit_applied_at=2026-08-26T06:58:19.168909+00:00`. Ingest, worker, and Web
  retained PIDs `115505`, `115501`, and `115503`; all remained active/running,
  the monolith remained inactive, and only ingest held the Telegram session.
  Semantic review remained disabled, active exchange writes, active
  management, claimed message jobs, and active worker commands were zero.
  SQLite remained `wal`, `query_only=1`, `quick_check=ok`, `total_changes=0`,
  with zero foreign-key violations. Natural traffic created five pending jobs
  after the preclaim snapshot; a bounded 30-second read-only confirmation from
  `2026-08-26T07:39:19.430009+00:00` through
  `2026-08-26T07:39:49.468021+00:00` observed five pending and zero claimed in
  every sample, so the quiet/backlog gate is also not currently re-established.

  Phase 7 remains incomplete and Phase 8 is not permitted. Ownership is
  released and the single retry authorization is consumed; another attempt
  requires a new owner authorization and fresh complete pre-cutover gates. Raw
  evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-safe-retry-20260826T073652Z/phase7-retry-evidence.log`,
  SHA-256
  `d47e070667fa20e78faad02ffdd2f5c0aae7f5983c2bdcbbf2a680a25cd00031`.
  The prior rollback evidence remains retained separately at its canonical
  path and matching SHA-256. No code/test change, push, deployment, restart,
  schema/data edit, worker command, replay, manufactured traffic, Telegram
  business or operator/system Bot message, test trade, or observer-triggered
  exchange write occurred.

- `2026-08-26 Phase 7 safe-retry claim`: session
  `codex-per-chat-opt-phase7-retry-20260826-root` exclusively claimed only the
  owner-authorized Phase 7 retry at exact clean local canonical base
  `05609b79a385ebfb1e43a8a520826335e7017eb7`. Local upstream, the local
  remote-tracking deploy ref, the live remote deploy branch, and production
  HEAD all resolved exactly to
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a`. The previous Phase 7 evidence
  remained present with canonical SHA-256
  `ad3d14aa04805a7187d5ca289e5a63ff9b681c269e9453b2137ace346bff127b`.
  The worktree was clean with no Git lock or active Git mutation; other
  long-lived application processes holding this directory as cwd had no open
  repository files or Git child process.

  The production preclaim gate found exactly one active/running ingest,
  worker, and Web authority with distinct PIDs, an inactive monolith, and only
  ingest holding the Telegram session. Database and ingest API both reported
  exactly `global + 1 + queue`; worker in-memory health reported cap `1`, zero
  active lanes, peak `0`, and zero stalls. Semantic review was disabled.
  Active exchange writes, active management, pending/claimed message jobs, and
  pending/claimed/executing worker commands were all zero. SQLite was `wal`,
  `query_only=1`, `quick_check=ok`, `total_changes=0`, with zero foreign-key
  violations. The current service journals had zero SQLite locks, loop stalls,
  session conflicts, DeepSeek/402 errors, or authority drift. The worker-owned
  bounded read-only exchange baseline was complete at fingerprint
  `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`,
  with zero positions and zero open orders.

  This retry authorization explicitly supersedes only the Phase 7 plan's old
  `global + 20 + queue` retry baseline. It permits one exact ingest-owned
  expected-state transition from `global + 1 + queue` to
  `per_chat + 3 + queue`, one non-stitchable five-second worker convergence
  gate, the approved rollback, one complete two-hour natural-traffic
  acceptance window after convergence, read-only production evidence, and
  local canonical status updates with explicit-path commits. It does not
  authorize production-code or test changes, push, deployment, restart,
  schema/data changes, worker commands, replay, manufactured traffic, Telegram
  business or operator/system Bot messages, test trades, or exchange writes.

- `2026-08-26 Phase 7 immediate scheduler-gate rollback`: all final cutover
  gates passed again against exact production SHA
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a`. The ingest-owned expected-state
  request atomically changed `global + 20 + queue` to
  `per_chat + 3 + queue` once and returned HTTP `200`; both the database and
  ingest API confirmed the desired complete tuple. No service was restarted.
  The immediate worker runtime-health confirmation still reported
  `configured_max_parallel_chats=20`, `active_chat_lanes=0`, the prior
  `peak_active_chat_lanes_since_limit_change=2`, and the prior
  `limit_applied_at`, rather than the newly configured cap `3`. This failed the
  post-cutover scheduler/concurrency gate before the two-hour acceptance window
  began.

  Per the approved level-two rollback, the session read the exact current tuple
  and issued one atomic transition from `per_chat + 3 + queue` to
  `global + 1 + queue`. The rollback returned HTTP `200`; database and API
  confirmation was complete. Final worker runtime health then converged to cap
  `1`, zero active lanes, a reset peak of `0`, and a new limit-applied timestamp.
  Ingest, worker, and Web retained their original distinct PIDs and remained the
  only active/running split authorities; the monolith remained inactive and
  only ingest held the Telegram session. Final SQLite evidence was `wal`,
  `quick_check=ok`, `query_only=1`, `total_changes=0`, with zero foreign-key
  violations. Active exchange writes, active management, pending/claimed
  message jobs, and pending/claimed/executing worker commands were zero. Final
  journals and runtime health had zero SQLite locks, loop stalls, session
  conflicts, DeepSeek/402 errors, or authority drift. Complete worker-owned
  read-only exchange baseline and end snapshots were identical at fingerprint
  `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`,
  with zero positions and zero open orders.

  The uninterrupted two-hour acceptance window was not started; therefore its
  natural-message count is `0`, distinct-chat count is `0`, and Phase 7 peak,
  ordering, cross-chat progress, and convergence acceptance are not claimed.
  The workstream remains incomplete and `in_progress`, ownership is released,
  cutover authorization is consumed, and no automatic re-cutover is permitted.
  Raw evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase7-cutover-acceptance-20260826T065802Z/phase7-evidence.log`,
  SHA-256
  `ad3d14aa04805a7187d5ca289e5a63ff9b681c269e9453b2137ace346bff127b`.
  No push, deployment, restart, manufactured traffic, replay, worker command,
  Telegram business or operator/system Bot message, test trade, production
  code/schema/migration/data edit, or observer-triggered exchange write occurred.

- `2026-08-26 Phase 7 cutover-and-acceptance claim`: session
  `codex-per-chat-opt-phase7-20260826-root` exclusively claimed only Phase 7 at
  exact clean local canonical base
  `8c2159286309d9380622d0a3770c3d46592d11d7`. Phase 6 is completed and its
  deployed commit, local upstream, local remote-tracking deploy ref, live remote
  deploy branch, and production HEAD all resolve exactly to
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a`. The Phase 6 raw evidence remains
  present and matches canonical SHA-256
  `7ed5d4baa4086f80586c4a27042f6158ac9a664c627b38d0040f891b79b36023`.
  Canonical ownership was unclaimed, the local worktree was clean and exclusive,
  and no Git or deployment lock or updater process was active.

  The production preclaim gate found exactly one active/running ingest, worker,
  and Web authority at the deployed checkout, distinct PIDs, an inactive
  monolith, and only ingest holding the Telegram session. The complete tuple was
  exactly `global + 20 + queue`, semantic review was disabled, active exchange
  writes, active management, pending/claimed message jobs, and pending/claimed/
  executing worker commands were all zero. The worker had zero active lanes;
  SQLite was `wal`, `quick_check=ok`, `query_only=1`, `total_changes=0`, and had
  zero foreign-key violations. All three runtime roles reported zero loop stalls
  and the journals reported zero new SQLite locks, session conflicts,
  DeepSeek/402 errors, or authority drift. The worker-owned bounded read-only
  exchange baseline was complete with fingerprint
  `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`,
  zero positions, and zero open orders.

  Authorization is limited to one exact ingest-owned expected-state transition
  from `global + 20 + queue` to `per_chat + 3 + queue`, the approved atomic
  rollback to `global + 3 + queue` for lock/admission/ingest anomalies or
  `global + 1 + queue` for scheduler/duplicate/SQLite/execution/concurrency
  anomalies, one uninterrupted two-hour natural-traffic acceptance window,
  read-only production evidence, local canonical status updates, and local
  explicit-path commits. No push, deployment, restart, production code/schema/
  migration/data edit, worker command, replay, manufactured traffic, Telegram
  business or operator/system Bot message, test trade, or observer-triggered
  exchange write is authorized.

- `2026-08-26 Phase 6 compatible deployment completion`: exact candidate
  `8cccfbb1683894459368cec4ca64a0cf626a1e9a` was pushed without force and
  deployed once through the existing verified updater bootstrap. The local
  machine had no executable PowerShell runtime, so the `.ps1` wrapper itself
  could not start; no server action occurred on that failed attempt. The same
  wrapper bootstrap was then executed directly: it fetched the exact branch,
  verified `FETCH_HEAD`, extracted the candidate's updater, matched SHA-256
  `b24132a3204bebee29679530a19cc5c3e680f724b0200195d869865ed7adcb70`,
  verified the dual split-runtime contract, and invoked that updater. Production
  reached the exact candidate with one necessary restart; ingest, worker, and
  Web remained the only active authorities, the monolith stayed inactive, the
  monitor pin/timer were healthy, and all three service PIDs then remained
  unchanged through observation.

  Server focused verification passed `111`, then the settings expected-state
  slice passed `28` with `179` deselected, and the Web role/expected-state slice
  passed `5` with `260` deselected. The ingest-owned exact no-op
  `global + 20 -> global + 20` returned HTTP `200` with no tuple change; the
  deliberately stale expected cap `19` request returned HTTP `409` and left the
  complete settings row unchanged. No `per_chat` or cap `3` request was sent.
  The continuous L2 window ran from `2026-08-26T06:06:06.946178+00:00` through
  `2026-08-26T06:36:07.108192+00:00`, sampled `360` times, and observed `8`
  natural messages across `4` chats. All `8` queue jobs succeeded; ending
  pending and claimed counts were zero; peak active lanes were `2`; missing,
  orphan, stuck, duplicate job, duplicate recognition-decision, and duplicate
  execution-contract counts were zero. All runtime roles retained zero new
  loop stalls, SQLite locks, session conflicts, DeepSeek/402 errors, or
  authority drift. Opening and ending SQLite checks were `quick_check=ok`,
  `query_only=1`, and `total_changes=0` for each read-only snapshot. Two
  worker-owned bounded exchange reads were complete and had the identical
  fingerprint, with zero positions and zero open orders; no exchange write
  occurred. Production remained exactly `global + 20 + queue`, and semantic
  review remained disabled.

  Raw evidence is retained at
  `/opt/telegram-kol-analyzer/data/evidence/per-chat-phase6-compatible-deploy-20260826T055717Z/phase6-evidence.log`,
  SHA-256
  `7ed5d4baa4086f80586c4a27042f6158ac9a664c627b38d0040f891b79b36023`.
  Phase 6 is complete and the exclusive claim is released. The pointer advances
  only to Phase 7 awaiting claim; Phase 7 was not read, claimed, executed, or
  pushed, and cutover remains unauthorized.

- `2026-08-26 Phase 6 compatible-deployment claim`: session
  `codex-per-chat-opt-phase6-20260826-root` claimed only compatible deployment
  and L2 verification at exact clean local canonical base
  `4b2f004a226ac97c622331632e473ad3d1100ba0`. Phase 5 is completed; frozen
  production-code candidate `e37146eaea03befac6457fa224e9dad0cd6c7166`
  remains unchanged, is an ancestor, and only this canonical status changed
  afterward. Local upstream, local remote-tracking deploy ref, and live remote
  deploy branch all resolved to
  `d66afadda5e34db80851a0dae5986b622521ab3f`; the worktree was clean and
  exclusive, and no Git lock was present. Authorization is limited to one
  explicit-path claim commit, a verified exact 40-hex Phase 6 candidate,
  non-force push, the existing compatible deployment workflow, one necessary
  restart in a proven safe window, and Phase 6 L2 read-only/no-op/conflict
  evidence. Production must remain exactly `global + 20 + queue`. Cutover,
  `per_chat + 3`, schema or production-data changes, manufactured traffic,
  replay, test trades, exchange writes, Telegram business messages, and
  operator/system Bot messages are forbidden.

- `2026-08-26 Phase 5 completion after identity-check root-cause correction`:
  the earlier `deployed_dirty_count=15` stop was caused by a measurement-
  definition mismatch, not production code or managed-configuration drift.
  Phase 4 explicitly ran `git status --porcelain --untracked-files=no` and
  reported zero tracked changes. Phase 5 had changed the command to
  `--untracked-files=all`; a bounded read-only classification proved all 15
  entries are historical untracked configuration backup files dated from June
  through August 9. There were exactly zero tracked name-status or numstat
  differences against deployed HEAD
  `d66afadda5e34db80851a0dae5986b622521ab3f`, and the same 15 untracked paths
  were stable at the beginning and end of the classification capture. No file
  content was emitted; only paths, file metadata, and hashes were retained in
  local evidence. Therefore the production identity requirement is satisfied
  under the same tracked-worktree definition used by Phase 4, without deleting,
  moving, ignoring, or modifying any server file.

  The original Phase 5 database capture remains authoritative and complete:
  intents `138`, `141`, and `147` existed exactly once each and were stable as
  `resolved / terminal / entry_leg_terminal_after_snapshot_wait`; exact
  persisted binding/leg identities `301/522`, `306/528`, and `310/536` resolved
  to verified terminal legs with matching non-empty persisted `pos_id`, status,
  terminal reason, and intent evidence. Opening and closing `quick_check` were
  `ok`, `query_only=1`, and the SQLite connection reported `total_changes=0`.
  Phase 5 raw evidence remains only on the local machine at
  `/Users/steven/.codex/evidence/per-chat-phase5-trigger-intents-read-only-20260826T054000Z/production-read-only-gate.txt`,
  SHA-256
  `0d9a31aced419dce9ebfc35d3a90e5368bd30127d0849e502e8c9ef4d738f344`;
  identity root-cause evidence is
  `/Users/steven/.codex/evidence/per-chat-phase5-trigger-intents-read-only-20260826T054000Z/production-checkout-diff-read-only.txt`,
  SHA-256
  `de07b8b638a9699000131ab48819a8804db4c7a7f656a43dba467a89be16463f`.
  No server evidence file or directory, server/database write or repair,
  backup, CAS plan, historical attribution reconstruction, Deepcoin or exchange
  call, push, deployment, restart, configuration or data change, replay,
  Telegram business message, or operator/system Bot message occurred. Phase 5
  is complete; the pointer advances only to Phase 6 awaiting claim. Phase 6 was
  not read, claimed, or executed.

- `2026-08-26 Phase 5 production-identity investigation claim`: session
  `codex-per-chat-opt-phase5-identity-20260826-root` resumed only the unresolved
  `deployed_dirty_count=15` classification at exact clean local canonical base
  `20457cfb947b6c210b643191ffdccbb411169b27`. The local upstream, local remote-
  tracking deploy ref, and live remote deploy branch still resolved to
  `d66afadda5e34db80851a0dae5986b622521ab3f`; the worktree was clean and no Git
  lock was present. Authorization is limited to one bounded read-only listing
  and classification of the production checkout differences, comparison with
  exact deployed Git content where needed, local-only evidence, canonical
  status updates, and local commits. No production/server/database write or
  repair, backup, CAS plan, push, deployment, restart, configuration or data
  change, replay, Telegram traffic or business/operator/system Bot message,
  Deepcoin call, exchange action, Phase 6 read, claim, or execution is
  authorized.

- `2026-08-26 Phase 5 fail-closed stop`: the bounded read-only capture ran from
  `2026-08-26T05:40:02Z` through `2026-08-26T05:40:25Z`. The deployed HEAD and
  branch remained `d66afadda5e34db80851a0dae5986b622521ab3f` and
  `codex/deepcoin-auto-trading-v1`; split ingest, worker, and Web services were
  active/running from `/opt/telegram-kol-analyzer`, the monolith was
  inactive/dead, and all named service commands resolved `data/research.db`
  from that root. The database identity remained device `64257`, inode
  `75526029`. SQLite reported `query_only=1`, `total_changes=0`, and
  `quick_check=ok` at both required checkpoints. Intents `138`, `141`, and
  `147` existed exactly once each and remained byte-for-byte stable as
  `resolved / terminal / entry_leg_terminal_after_snapshot_wait`. Their exact
  audited persisted identities remained respectively binding/leg `301/522`,
  `306/528`, and `310/536`; every foreign-key leg resolved, every binding ID
  matched, each leg was verified with a non-empty persisted `pos_id` and a
  terminal status, and each intent's persisted terminal evidence exactly
  matched the leg ID, binding ID, `pos_id`, status, and terminal reason. No
  symbol, side, time, tag, or `clOrdId` inference was used. The target result
  was complete and stable, but the production checkout reported
  `deployed_dirty_count=15`, whereas the Phase 4 identity checkpoint recorded
  zero. Because the checkout can no longer be described as an exact clean
  deployed SHA, Phase 5 stopped fail closed without a retry, path inspection,
  repair, or Phase 6 advancement. The raw capture exists only on the local
  machine at
  `/Users/steven/.codex/evidence/per-chat-phase5-trigger-intents-read-only-20260826T054000Z/production-read-only-gate.txt`,
  SHA-256
  `0d9a31aced419dce9ebfc35d3a90e5368bd30127d0849e502e8c9ef4d738f344`.
  No server evidence file or directory, production database or server write,
  repair, backup, CAS plan, historical attribution reconstruction, Deepcoin or
  exchange call, push, deployment, restart, configuration or data change,
  replay, Telegram business message, or operator/system Bot message occurred.

- `2026-08-26 Phase 5 claim`: session
  `codex-per-chat-opt-phase5-20260826-root` claimed only the bounded trigger-
  protection intent `138`, `141`, and `147` production read-only gate at exact
  clean local base `858cd91d68f24436830012bcbe11bc830bc6a414`. Phase 4 is
  completed, the frozen local candidate
  `e37146eaea03befac6457fa224e9dad0cd6c7166` is unchanged, and the status
  pointer names only the Phase 5 plan. The local upstream, local remote-tracking
  deploy ref, and live remote deploy branch all resolved to
  `d66afadda5e34db80851a0dae5986b622521ab3f`; the worktree was clean and no Git
  lock was present. Authorization is limited to production read-only identity,
  `PRAGMA query_only=ON`, opening and closing `quick_check`, the three exact
  intent rows, their persisted execution-leg references and exact terminal leg
  identities, local-only raw evidence and hashing, canonical status updates,
  and local commits. Production or server writes, repairs, backups, CAS plans,
  historical attribution reconstruction, Deepcoin or exchange calls, push,
  deployment, restart, configuration or data changes, replay, manufactured
  Telegram traffic, Telegram business messages, and operator/system Bot
  messages are forbidden. Any incomplete query, missing or changed row, broken
  persisted leg reference, identity ambiguity, or state change is an immediate
  fail-closed stop condition without retry polling or repair.

- `2026-08-26 Phase 4 completion`: the bounded production read-only gate ran
  from `2026-08-26T05:27:07Z` through `2026-08-26T05:27:20Z` against exact
  clean deployed SHA `d66afadda5e34db80851a0dae5986b622521ab3f`. The ingest, worker,
  and Web services were loaded and active/running from
  `/opt/telegram-kol-analyzer`; the monolith was inactive/dead. Every service
  resolved `data/research.db` from that same working directory, whose exact
  read-only identity was device `64257`, inode `75526029`. SQLite reported
  `query_only=1`, `total_changes=0`, and `quick_check=ok` at both checkpoints.
  Batch `150` existed exactly once and remained byte-for-byte stable across its
  selected gate fields as `resolved / historical_position_fully_closed`.
  Its one management leg was `failed`; components `23` and `24` were
  `safely_skipped / historical_position_fully_closed`, while exhausted
  component `22` retained its historical `operator_required /
  take_profit_cancel_retry_exhausted` evidence under the terminal batch. Both
  execution legs were `closed / historical_exchange_position_closed`; binding
  `320` was `closed`, had null `pos_id`, and reported terminal marker
  `entry_legs_terminal`. The established active-management count was exactly
  zero, so none of these rows retained active management authority. Per the
  owner's prohibition on production writes, no server evidence directory was
  created; the exact raw output was saved only on the local machine at
  `/Users/steven/.codex/evidence/per-chat-phase4-batch150-read-only-20260826T052707Z/production-read-only-gate.txt`,
  SHA-256
  `4fb2a8e57f74e2f44f8bb2e29827f84c9a909480c48e0fc7800b49e116c399dd`.
  No production write or repair, backup, CAS plan, push, deployment, restart,
  configuration or data change, Telegram business or operator/system Bot
  message, replay, Deepcoin call, or exchange action occurred. Phase 4 is
  complete; Phase 5 is unclaimed and requires a new user turn.

- `2026-08-26 Phase 4 claim`: session
  `codex-per-chat-opt-phase4-20260826-root` claimed only the bounded batch `150`
  production read-only gate at exact clean local base
  `d6768b681ece8ed43aeced19c95da35afcfeb952`. Phase 3 frozen candidate
  `e37146eaea03befac6457fa224e9dad0cd6c7166` and approved design
  `9707109dfd1f0815dec6edbc8809fa3fb89a00a0` are ancestors. The local upstream,
  local remote-tracking deploy ref, and live remote deploy branch all resolved
  to `d66afadda5e34db80851a0dae5986b622521ab3f`; the status pointer named only
  the Phase 4 plan, the worktree was clean and exclusive, and no Git lock was
  present. Authorization is limited to the Phase 4 plan's bounded production
  read-only service, deployed-SHA, database-identity, and SQLite checks; local
  evidence; status updates; and local commits. Production writes or repairs,
  push, deployment, restart, configuration or data changes, Telegram business
  messages, operator/system Bot messages, replay, backup or CAS-plan creation,
  and exchange calls are forbidden. Any incomplete or mismatched result is an
  immediate stop condition.

- `2026-08-25 Phase 3 local completion`: exact production candidate
  `e37146eaea03befac6457fa224e9dad0cd6c7166` was reviewed over diff boundary
  `d88ecc99c1e7b95f253bfabe32e87fe2dc5391bc..e37146eaea03befac6457fa224e9dad0cd6c7166`.
  The initial consolidated focused run passed `700` tests, but independent
  review invalidated candidate
  `77d570ee2187c7e7bbbaf53b6a55f1d0efb135de` and its `6302 passed, 1 skipped`
  full-suite result: reconcile persistence now runs in a worker thread, while
  an active `LiveUpdateBroker` subscriber caused `publish_message()` to call
  `asyncio.get_event_loop()` from that thread after the database commit.
  The new RED test failed with the expected Python 3.12 `RuntimeError`; the
  minimal GREEN repair captures the subscriber's running loop and uses safe
  running-loop detection before thread-safe queue scheduling. The affected
  slice passed `50` tests, the final consolidated focused set passed `705`
  tests with `2` existing warnings in `57.33s`, `git diff --check` and full
  compileall passed, and the one valid complete suite after the last production
  edit passed `6303`, skipped `1`, and reported `32` existing deprecation
  warnings in `506.37s`. Same-session independent read-only re-review found
  zero Critical, Important, or Minor findings. The final diff adds no schema,
  migration, index, service, executor, queue, actor, recognition, strategy,
  execution, exchange-authority, configuration, or data change. No push,
  deployment, restart, production read/write, settings mutation, Telegram
  traffic, or exchange action occurred. Phase 3 is locally complete and frozen;
  Phase 4 is unclaimed and requires a new user turn and exclusive claim.

- `2026-08-25 Phase 3 claim`: session
  `codex-per-chat-opt-phase3-20260825-root` claimed only final local candidate
  review and freeze at exact clean local base
  `9e1d41400996881107ef15accff772173da4c280`. Phase 1 candidate
  `3d5e05aeb4d439654ee9ed24b5bfa3158d0354bd`, Phase 2 candidate
  `592c0e9d6537c5e2f58c15cd495b6767a32b3da4`, and approved design
  `9707109dfd1f0815dec6edbc8809fa3fb89a00a0` are ancestors. Upstream and the
  remote deploy branch both resolved to the authorized Phase 3 gate baseline
  `d66afadda5e34db80851a0dae5986b622521ab3f`; no Git lock was present.
  Authorization is limited to Phase 3 local review, RED-to-GREEN repairs,
  tests, status updates, and local commits. Push, deployment, restart,
  production reads/writes, settings changes, Telegram traffic, and exchange
  calls are forbidden. Any trading-semantic, schema, migration, index, service,
  executor, queue, actor, or authority change is a stop condition.

- `2026-08-25 Phase 2 local completion`: exact local candidate
  `592c0e9d6537c5e2f58c15cd495b6767a32b3da4` replaces only the unbounded
  durable-job candidate read inside the existing `BEGIN IMMEDIATE` claim
  transaction. One window CTE identifies the lowest nonterminal, non-shadow
  `raw_message_id` per chat, filters only due pending or stale claimed owners,
  orders by chat and raw-message ID, and applies `LIMIT :claim_limit` before
  Python receives rows. Existing conditional updates, claim tokens, attempt
  counts, timestamps, reasons, and the single commit boundary are unchanged.
  RED ran
  `.venv/bin/python -m pytest tests/test_message_processing_worker.py -k 'candidate_fetch_is_bounded or does_not_use_unbounded' -vv`
  and failed both selected tests: the bounded CTE was absent and the old
  `MessageProcessingJob.query.all()` path was reached. Initial GREEN passed
  both. The first complete worker-file run then exposed the exact-due datetime
  boundary (`1 failed, 23 passed`): untyped text parameters omitted ORM
  microseconds. That existing retry test remained RED until typed SQLAlchemy
  `DateTime` binds restored identical SQLite comparison format. Final worker
  verification passed `24`; shadow enqueue passed `21`; settings-cap passed
  `17`; process-role plus event-loop census passed `7` (`69` total). A
  representative temporary database with `48` jobs completed
  `EXPLAIN QUERY PLAN`, used the existing chat-id index, and selected exactly
  `3` rows at limit `3`. `git diff --check` and compileall of the touched
  source/test modules passed. No index, schema, migration, model, fallback
  query, pool/executor change, recognition/strategy/execution/exchange semantic
  change, full suite, push, deployment, restart, production query,
  configuration/data mutation, Telegram traffic, or exchange action occurred.
  Phase 2 is locally complete; Phase 3 final candidate review is unclaimed and
  requires a new user turn.

- `2026-08-25 Phase 2 claim`: session
  `codex-per-chat-opt-phase2-20260825-root` claimed only bounded durable-job
  candidate selection at exact clean local base
  `e49c8f3abc8e90c71da88b80bab3999fc0a3bd1d`. The owner explicitly authorized
  Phase 2 to use exact upstream and remote deploy baseline
  `d66afadda5e34db80851a0dae5986b622521ab3f`; both resolved to that SHA and no
  Git lock was present. Authorization is limited to Phase 2 local code, tests,
  status updates, and local commits. Push, deployment, restart, production
  queries, configuration/data mutation, Telegram traffic, and exchange actions
  are forbidden. Any schema, index, migration, model, fallback query,
  recognition, strategy, execution, exchange-write, pool-size, or executor-
  count change is a stop condition.

- `2026-08-25 Phase 1 local completion`: exact local candidate
  `3d5e05aeb4d439654ee9ed24b5bfa3158d0354bd` moves only the approved reconcile,
  lifecycle-expiry, and Bot database slices off their asyncio loops. Reconcile
  setup RED was
  `.venv/bin/python -m pytest tests/test_reconcile.py -k offloads_blocking_database_setup -vv`
  (`1 failed`, `0` heartbeats); GREEN plus the reconcile files passed `21`.
  Lifecycle heartbeat and mgmt-worker ordering REDs each failed as intended;
  GREEN plus lifecycle/worker files passed `45`. Bot heartbeat RED failed all
  three target paths, queued/started command cancellation RED failed `2`, and
  the narrowed census RED reported exactly the three target calls; GREEN plus
  the complete Phase 1 focused set initially passed `87`.
- `2026-08-25 Phase 1 review hardening`: independent review invalidated
  `0dc08425693b16a8c903d66b974380d86ff56b02` with three Important findings:
  cancellation could outlive newly offloaded reconcile/lifecycle work, and the
  tested non-queue reconcile projections remained on the loop. New RED ran
  `.venv/bin/python -m pytest tests/test_reconcile.py -k 'database_write_drains or non_queue_reconcile_database_projection' -vv`
  (`2 failed`) and the lifecycle cancellation slice (`1 failed`). GREEN adds
  queued-cancel/started-drain boundaries, drains expiry notifications before
  propagating a started cancellation, and offloads raw-message/candidate/trade-
  idea projections. The final focused command over Bot, worker executor,
  census, lifecycle, and reconcile files passed `90` in `15.04s`.
  `git diff --check` and compileall of every touched module/test passed.
  Independent re-review found zero Critical, Important, or Minor issues and
  returned Ready. No full suite was run because Phase 1 explicitly defers it to
  Phase 3. No push, deployment, restart, production setting/data, Telegram
  traffic, or exchange action occurred. Phase 2 is unclaimed and requires a new
  user turn.

- `2026-08-25 Phase 1 claim`: session
  `codex-per-chat-opt-phase1-20260825-root` claimed only the event-loop database
  offload phase at exact clean local base
  `d88ecc99c1e7b95f253bfabe32e87fe2dc5391bc`. Approved design correction
  `9707109dfd1f0815dec6edbc8809fa3fb89a00a0` is an ancestor. Authorization is
  limited to Phase 1 local code, tests, status updates, and local commits. Push,
  deployment, restart, production configuration/data, Telegram traffic, and
  exchange actions are forbidden. Any required recognition, strategy,
  execution, position-ownership, exchange-write, schema, pool-size, or executor-
  count change is a stop condition.

- `2026-08-23 planning`: read-only investigation at source baseline
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`; clean HEAD/upstream/remote and
  unclaimed completed original remediation were verified. Existing focused
  lock/worker/settings tests passed `202 passed in 3.35s`. A minimal reproduction
  confirmed a future key enters while snapshot `lock_all()` waits. No production
  code, test, configuration, deployment, server, Telegram, database, or exchange
  mutation was performed.
- `2026-08-23 design`: owner approved the shared/exclusive admission,
  work-conserving durable lanes, compatibility default 20, production cap 3,
  ingest-owned atomic transition, pure in-memory observability, and two-level
  rollback design. Design commit:
  `1efd20cbd50be4e3c724d48874f6004fe6ad2c7c`.
- `2026-08-24T06:23:14Z implementation claim`: session
  `codex-per-chat-20260823-root-68b9e88` claimed the independent workstream at
  exact baseline `68b9e88bbb9dd76227056a08376b7a94b553c5f8`. The upstream and
  remote deploy baseline remained
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`; the working tree was clean and
  the original remediation pointer remained completed. Authorization is local
  Tasks 1-10 only: no push, deploy, restart, production configuration, Telegram
  traffic, database mutation, or exchange action.
- `2026-08-24 task 2 RED`: ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -k 'future_key or not_starved' -vv`;
  all 3 selected tests failed because a future key entered while snapshot
  `lock_all()` was held or waiting.
- `2026-08-24 task 2 GREEN`: replaced snapshot enumeration with a
  writer-preference shared/exclusive admission barrier. Ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv`; result:
  `9 passed in 0.19s`.
- `2026-08-24 task 3 RED`: ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -k 'cancel or exception or multiple_lock_all or mixed_reader' -vv`;
  all 6 selected tests failed because the required admission snapshot was not
  yet implemented.
- `2026-08-24 task 3 GREEN`: added the pure in-memory admission snapshot and
  exact registered/acquired cancellation bookkeeping. Ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv`; result:
  `15 passed in 0.19s`, including multiple writers, waiting/held cancellation,
  exception release, key-waiter cancellation, and mixed cleanup.
- `2026-08-24 task 4 RED`: ran the provider/rollback/mode-resolution slice;
  `2 failed, 2 passed`. The failures proved global work bypassed registry
  admission and a pre-created caller resolved `message_lock_mode` too early.
- `2026-08-24 task 4 GREEN`: provider operations now enter shared admission
  before resolving mode, while `lock_all()` always enters exclusive admission
  before the legacy global lock. Ran the registry, listener, reconcile, and
  position-authority compatibility slices; result: `49 passed in 2.61s`.
- `2026-08-24 task 5 RED`: ran the parallel-chat settings slice; all 13
  selected tests failed because the field was absent and invalid values were
  ignored.
- `2026-08-24 task 5 GREEN`: added compatibility default 20 and exact-integer
  bounds 1-20 for `message_processing_max_parallel_chats`, including storage
  preservation across unrelated saves. Full trading-settings result:
  `198 passed in 2.71s`.
- `2026-08-24 task 6 RED`: after replacing scheduler-sensitive start-order
  assertions with durable claimed-count evidence, the selected lane slice was
  `5 failed, 2 passed`: the old loop claimed 5-6 jobs at cap 3 and 3 jobs at
  cap 1; same-chat live/retry authority tests already passed.
- `2026-08-24 task 6 GREEN`: added pure in-memory lane activity and refactored
  the loop to dynamic, work-conserving, single-claim slot tasks. The focused
  lane slice passed `7 passed in 0.99s`; worker, pipeline-exclusivity, and
  shadow-enqueue compatibility passed `46 passed in 3.59s`.
- `2026-08-24 task 7 proof`: the Task 6 cancellation-finally implementation
  already satisfied the new restart/recovery assertions, so no additional
  production edit was required. The selected cancellation/stale/second-worker
  slice passed `3 passed in 0.41s`; the full worker file passed
  `21 passed in 1.88s`. Cancelled async slots retain their exact claim token
  and attempt count for stale-lease recovery, while later same-chat work stays
  blocked and a second worker cannot duplicate a live claim.
- `2026-08-24 task 8 RED`: the selected transaction/role slice failed all 6
  tests because the atomic helper, Web-to-ingest requester, ingest exclusive
  admission, and worker refusal were absent.
- `2026-08-24 task 8 GREEN`: added a `BEGIN IMMEDIATE` expected-state tuple
  transition and bounded localhost:8001 settings proxy. All 11 new transaction
  and role contracts passed; settings/Web selection passed
  `241 passed, 219 deselected in 7.19s`, and full settings/listener/pipeline
  compatibility passed `220 passed in 5.09s`. Unknown proxy outcomes are not
  retried, expected-state conflicts do not write, and unrelated saves avoid
  exclusive admission.
- `2026-08-24 task 9 RED`: the required observability/authority selection ran
  `5` tests: `2 failed, 3 passed`. The worker response had no shared lane
  activity object and the ingest response had no admission snapshot; the
  existing non-worker task partition and exchange-authority guard already
  passed.
- `2026-08-24 task 9 GREEN`: created one process-local
  `MessageProcessingActivity`, injected that exact instance into the worker
  loop, and exposed role-specific in-memory snapshots: worker/all reports only
  bounded lane counters and ingest/all reports only admission counters. No
  endpoint response contains chat IDs or reads settings, the database, or the
  exchange. Six new observability/process-boundary tests passed. The first
  affected run found one event-loop census violation from the Task 6
  `utc_now()` limit timestamp (`594 passed, 1 failed`); settings and observation
  time are now captured together in the existing worker thread. The census plus
  worker tests passed `24`, then the complete affected set passed
  `595 passed, 2 existing deprecation warnings in 46.21s`. Submission review
  strengthened the cross-process boundary assertion to prove the worker gets
  activity only, never the ingest registry/provider; that focused slice passed
  `3`. No schema, database data, recognition, strategy, execution, exchange
  semantics, production setting, deployment, restart, or external traffic
  changed.
- `2026-08-24 task 10 local candidate`: exact production-code candidate
  `c8f778201c123f0bbadddc06e718945307adf40b` passed the final local gate.
  The diff from implementation claim `0f40b7d` contains only the planned lock
  registry/provider, durable worker, trading-settings, Web wiring, tests, and
  this independent status document; it contains no schema, migration, model,
  recognition, strategy, execution, or original remediation pointer file.
  `git diff --check` passed and
  `$PROJECT_PYTHON -m compileall -q src/telegram_kol_research tests` exited zero
  in `0.68s`. The exact Task 9 focused command passed
  `589 passed, 2 warnings in 46.62s` (`48.24s` wall clock). The one authorized
  complete suite ran exactly once after the last production-code edit and
  passed `6215 passed, 1 skipped, 32 warnings in 524.52s` (`535.65s` wall
  clock). The warnings are existing deprecation warnings. Schema and production
  data are unchanged; recognition, strategy, position ownership, execution,
  exchange-write, and trading-decision semantics are unchanged. The workstream
  is now `local_complete`; Task 11 independent review, push, deployment
  authorization, and every production/cutover action remain unstarted and
  require their own gates.
- `2026-08-24 independent review`: the local Task 10 candidate was invalidated
  before further production edits. Read-only review at integration HEAD
  `4d53b83960d08598796308d1b244e468f5c57110` found three reproducible Important
  defects: explicit no-op expected-state requests bypassed concurrency-transition
  admission and role routing; an unrelated settings save could restore a stale
  concurrency tuple; and scheduler slots created under an old cap could claim
  after a lower cap was applied. The owner approved RED-to-GREEN remediation and
  rebuilding Task 10 under design A. Push, deployment, restart, production
  configuration, database mutation, and exchange action remain unauthorized.
- `2026-08-24 task 10 rebuilt candidate`: all three independent-review findings
  were repaired under RED-to-GREEN TDD. The new tests first proved `3` explicit
  no-op role/admission failures, `1` stale settings overwrite, and `1` lowered-cap
  oversubscription; paired expected fields without targets also failed `2`
  validation cases. The final implementation routes every explicit expected-state
  concurrency request through the owning role and exclusive admission, serializes
  all settings read-merge-write transactions with `BEGIN IMMEDIATE`, and performs
  scheduler-owned claim/refill only up to the current available capacity. The
  affected compatibility set passed `541 passed, 2 warnings in 36.65s`.
  `git diff --check` and compileall passed. Production candidate
  `c0e2471ed76b6d73bceb3be3d88304e57e44088d` then ran the one authorized final
  complete suite exactly once: `6222 passed, 1 skipped, 32 warnings in 503.19s`.
  The remote deploy branch remained exactly
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`. No push, deployment, restart,
  production configuration, database mutation, Telegram traffic, or exchange
  action occurred; Task 11 review/push and all production/cutover gates remain
  separately unauthorized.
- `2026-08-24 rebuilt-candidate review`: read-only re-review confirmed the
  original three findings were closed, then found one new Important interleaving:
  a normal complete settings payload carrying an initially unchanged concurrency
  tuple could restore that stale tuple if cutover committed after the endpoint's
  second comparison but before `save_trading_settings()` acquired its write
  transaction. Candidate `c0e2471ed76b6d73bceb3be3d88304e57e44088d`
  and its `6222`-test evidence were invalidated before any further production
  edit. The workstream returned to Task 10 RED-to-GREEN remediation; push and all
  production actions remain unauthorized.
- `2026-08-24 second rebuilt-candidate review`: read-only review invalidated
  candidate `4490ec2c2e3adad3268a155376d5ba0da6c0b045` before a final suite. It found
  two remaining variants of the same stale full-form defect: the MiMo-only branch
  still passed concurrency targets to an ordinary save, and a second settings
  read could reclassify an initially unrelated Web/worker request as a concurrency
  transition after role ownership had already been checked. The approved repair
  boundary is to classify request intent once from the first read and carry one
  concurrency-stripped payload through every non-explicit save path. No push or
  production action occurred.
- `2026-08-24 final rebuilt Task 10 candidate`: two deterministic RED tests
  proved the remaining MiMo-only stale full-form overwrite and Web second-read
  misclassification (`2 failed`), then stable first-read intent plus one shared
  concurrency-stripped local-save payload made all `11` critical Web paths GREEN.
  The complete affected set passed `544 passed, 2 warnings in 36.01s`; compileall
  and `git diff --check` passed. An independent read-only endpoint-level review
  found no Critical, Important, or Minor issues: `36/36` non-explicit role/timing
  interleavings, `10/10` explicit role/transition paths, and `16` focused
  regressions passed. Frozen production candidate
  `eb9ff4c261080190e3f6d360724aec05395197ed` then ran its one final complete
  suite exactly once: `6225 passed, 1 skipped, 32 warnings in 466.23s`.
  The workstream is `local_complete`; the remote deploy branch remains at
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`. Task 11 review/push, deployment,
  restart, production configuration, database mutation, Telegram traffic,
  exchange action, and cutover remain unstarted and separately unauthorized.
- `2026-08-24 task 16 repair claim`: production deployment of integration HEAD
  `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb` remained compatible at
  `global + 20`, but the Task 13 freeze gate stopped before cutover after two
  real worker event-loop stalls (`worst_stall_ms=3972.537`). The captured stack
  showed `run_system_operator_bot_command_loop()` calling synchronous expiry
  refresh reconciliation on the asyncio thread. The owner authorized local
  RED-to-GREEN repair and candidate rebuild only. Push, deployment, restart,
  production settings, cutover, manufactured traffic, database mutation, and
  exchange writes remain unauthorized.
- `2026-08-24 task 16 rebuilt candidate`: two dynamic RED tests proved the
  callback loop paused its heartbeat (`worst_gap=0.2551s`) and constructed and
  processed the callback on `MainThread`; the tightened static census also
  failed on the exact direct callback path. The minimal GREEN change submits
  optional Deepcoin client construction and synchronous callback processing as
  one unit to the existing single-thread management executor. Both dynamic
  tests passed, the census passed, and the separate command-message blockers
  remain explicitly out of scope. The focused Bot/census/Web compatibility set
  passed `398 passed, 2 warnings in 39.93s`; `git diff --check` and compileall
  passed. Frozen production-code candidate
  `de0ae43498dc5b330f3e61c70eb8ebb27d50b269` then ran its one final complete
  suite exactly once: `6227 passed, 1 skipped, 32 warnings in 514.56s`. No push,
  deployment, restart, production setting, cutover, manufactured Telegram
  traffic, database mutation, or exchange action occurred; all production
  actions remain separately unauthorized.
- `2026-08-24 task 16 rebuilt-candidate review`: independent read-only review
  invalidated candidate `de0ae43498dc5b330f3e61c70eb8ebb27d50b269` and its
  `6227`-test evidence before any push or production action. Cancelling the Bot
  task cancelled only the asyncio proxy while the already-started management
  callback continued, and lifespan shut down the management executor before
  stopping the newly added Bot producer. The approved repair remains local
  RED-to-GREEN work: cancellation must wait for the in-flight callback unit,
  and every management-executor producer must stop before executor shutdown.
  No deployment, restart, production setting, cutover, traffic, database, or
  exchange action is authorized.
- `2026-08-24 task 16 cancellation-shutdown review`: the first cancellation
  repair production commit `78cc24dae7e2bf3b341c4f5ecdf28b9cf5de0284`
  passed `400` focused tests and static checks, but was invalidated before its
  final suite. Read-only review proved that a callback still queued behind a
  saturated management executor would execute after Bot cancellation, and
  that a running callback failure during cancellation drain was retrieved but
  not logged. The next RED-to-GREEN boundary must cancel queued work without
  executing it, drain only work that actually started, preserve final Bot
  cancellation, and record a safe update-id exception log. Production actions
  remain unauthorized.
- `2026-08-24 final task 16 callback candidate`: deterministic RED proved both
  remaining review findings (`2 failed`): a callback queued behind a saturated
  management worker did not cancel promptly and later executed, while a
  running callback failure during cancellation produced no captured audit log.
  GREEN added one lock-linearized queued/started unit: queued cancellation wins
  without client construction or processor invocation; started work drains;
  running failure logs exactly one safe update ID with `exc_info`; repeated
  cancellation still ends as `CancelledError`. The five callback state tests
  passed, and a sentinel now proves the executor advanced past the cancelled
  queue position without timing assumptions. Final affected Bot/census/Web
  verification passed `402 passed, 2 warnings in 39.77s`; `git diff --check`
  and compileall passed. Independent read-only review found no Critical or
  Important issues, ran 2,000 queued/start races with zero cases where cancel
  won and the processor started, and returned Ready. The first complete suite
  reached `6230 passed, 1 skipped` but failed one test-only `caplog` assertion:
  the error and traceback were visibly emitted, while prior global logger state
  prevented capture. Replacing only that test fixture with a direct logger-call
  assertion passed the complete callback file (`18 passed`). Production code
  remained frozen at `e03622749c32ebb214af56cd118984268e72af56`; the final
  complete suite then passed `6231 passed, 1 skipped, 32 warnings in 492.58s`.
  The workstream is `local_complete`. No push, deployment, restart, production
  setting, cutover, manufactured traffic, database mutation, or exchange
  action occurred or is authorized.
- `2026-08-24 task 12 deployment blocker and rebuilt candidate`: the dormant
  deployment of exact remote candidate
  `a832ffb08972a3b74309c468274105b7014790fb` stopped at the predeploy gate
  because management batch `150` was `recovery_required`; production remained
  exact `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb` at `global + 20` with no
  deployment, restart, cutover, database write, or exchange write. Read-only
  evidence proved its first component exhausted three attempts before creating
  any cancel/reduce/protection intent. The production client exposes
  `list_trigger_order_history()`, while both composite snapshot call sites and
  their shared test double used the nonexistent plural spelling. RED changed
  only the fake to the production interface and reproduced
  `recovery_required`; GREEN changed both read-only snapshot call sites. The
  composite slice passed `33`, the complete management executor file passed
  `190`, and authority/backfill boundary tests passed `9`. Independent read-only
  review found no Critical or Important issue after two documentation-only
  corrections. Frozen production-code candidate
  `4d6950ad9919a9fb71f8f54a73c45d85912b9272` then passed the one final suite:
  `6231 passed, 1 skipped, 32 warnings in 475.29s`. One corrected, bounded
  read-only exchange snapshot then completed all six reads: the target posId was
  absent from live positions, had one exact position-history row, and none of
  its four owned protection order IDs remained pending. The binding's verified
  sibling leg remains `active` only in the database, while the complete account
  snapshot returned zero live BTC positions; sibling terminality and exact
  history therefore remain unknown. Batch `150` stays fail-closed until a
  separately authorized L3 plan proves the sibling's exact exchange history and
  derives one compare-and-set terminalization plus rollback from that evidence.
  The batch remains untouched; the rebuilt candidate is local only and has no
  push or deployment authorization.
- `2026-08-24 batch 150 exact L3 copy rehearsal`: owner-authorized work proved
  the previously unknown sibling history with one bounded, complete read-only
  Deepcoin snapshot. Target posId `1001124956792734` and sibling posId
  `1001124961572300` each had one exact full-close history row; parent trigger
  `1001124956792983` mapped to the unique filled child regular order and sibling
  posId, both owned stops had the exact close timestamp, and the current related
  position/open/pending sets were empty. Tool commit
  `13ad300e42cd7fc436b6ebb6aafeacac15df3317` passed its RED-to-GREEN tests and
  the focused compatibility set (`79 passed`), compile, CLI, diff check, and
  self-review. The server had space for only one 713 MB copy, so the verified
  immutable online backup was retained privately at
  `/Users/steven/.codex/evidence/batch150-terminalization-rehearsal-20260824T221044Z/research-online-backup.db`
  (SHA-256 `3a1d34bc4613f4753e5885d84b051a09cf2b0b6b3a25eafe33ab9297a160cfda`,
  mode `0600`, `quick_check=ok`) and an independent SQLite-backup copy was
  transferred to the private server evidence root
  `/opt/telegram-kol-analyzer/data/evidence/batch150-terminalization-rehearsal-20260824T221044Z/`.
  Copy-only execution passed `applied/8`, identical reapply
  `already_applied/0`, and `rolled_back/8`; every quick check was `ok`, the
  exact after rows matched, and the restored logical digest equalled the before
  digest `8265f9e80575eb2f1f4822fd38f35440b917c462a74058055667a066660b20c3`.
  Rehearsal plan/action/rollback fingerprints were respectively
  `271567c4572fa095f3e9ea6b9b2e92101848407951da8e40a9c355927f9ab4c6`,
  `48d8788f5563bcda95e74cbe2b16179bef171da8d2781af674d18997eeee73b8`,
  and `1f594d5102fd9e60e7fa65017f3dd648983aac6324362f36e7b416609704f1f0`.
  Production remained exact `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb`,
  split services active, monolith inactive, `global + 20`, batch set `[150]`,
  no unsafe management, no claimed job, no unconfirmed binding-320 intent,
  and `query_only=1` / `total_changes=0` / `quick_check=ok`; production and
  exchange write counts were zero. A read-only production-path plan was also
  generated, but execution leg `553.updated_at` was naturally refreshed before
  handoff, invalidating its full-row CAS immediately. It is retained only as
  evidence and is not an executable authorization candidate. Production apply,
  push, deployment, restart, cutover, replay, settings mutation, and exchange
  writes remain unauthorized; the next step requires an explicit choice between
  quiescing the relevant writer and approving a narrowly revised volatile-field
  CAS design.
- `2026-08-24 batch 150 controlled volatile-CAS rebuild`: owner approved the
  narrow design that treats only
  `execution_order_legs.id=553.updated_at` as volatile in starting-state
  classification and CAS predicates. It remains in before/after evidence, is
  still written by apply/rollback, and is checked exactly by the transactional
  postcondition. Design and execution-plan commits are
  `c402b124bc957b9bacc9e794d1223b846172c9fe` and
  `f67d71c5ab6d2e73e48d039c3fdf4464362189d2`. RED reproduced four exact
  failures while 33 prior/counterexample tests passed. GREEN plus the review
  correction binds the single CAS policy into schema-v2 plan and rollback
  fingerprints and the rendered SQL; the new file passed 38 tests and the
  final affected compatibility set passed `88 passed in 3.53s`, with compile,
  CLI, and diff checks clean. Exact tool commit
  `868bbf378d77960a05dd199b3c1df6b6cb78621b`, tool SHA-256
  `a8be99433fcc8ceebcfd002cddedcc7d38e144145abafe5d027e8b5ceec15d86`.
  Fresh immutable online backup
  `/Users/steven/.codex/evidence/batch150-volatile-cas-rehearsal-20260824T224153Z/research-online-backup.db`
  has SHA-256 `073e8a138a2b5ccab9447c90aeae506a6b465ddc9dddea36a5c4f5f0a0afc28a`,
  mode `0600`, and `quick_check=ok`; its independent SQLite rehearsal copy
  initially had SHA-256
  `7ea91b93f86210ec8ccf999cd0df2b8be3266991a96ed9590d6cf9f438c6df3d`.
  Complete fresh GET-only Deepcoin evidence again proved both exact full-close
  histories, parent-trigger to unique-child to sibling-posId lineage, both
  owned stops, and empty related live/open/pending sets with zero exchange
  writes. Private evidence root:
  `/opt/telegram-kol-analyzer/data/evidence/batch150-volatile-cas-rehearsal-20260824T224153Z/`.
  The schema-v2 plan contains exactly
  `execution_order_legs:553 -> ignored_before_fields=[updated_at]`; plan/action/
  rollback fingerprints are respectively
  `0ef630e968e8eb8e7aa22d2b85a68e0a3983bb3cbb00e8eaef4bda1597c63c01`,
  `8a1fdebcce8287479f0d006d279c59bad3318cd77a8ceac2e7a56d6cd040a0a6`,
  and `fe12e9702b9544e646d94c718f516a867ca454c9b44f3d2d3cd40ffca21ede32`.
  Three copy-only leg-553 timestamp drifts passed `applied/8`,
  `already_applied/0`, and `rolled_back/8`, restoring the exact complete before
  rows. A separate `leg553.last_verified_at` drift refused with
  `database_state_mixed`, zero repair writes, and exact cleanup. Production
  remained exact `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb`, split services active,
  monolith inactive, `global + 20`, and target rows equal the backup under the
  one-field policy. One natural claimed job appeared at the first stop snapshot
  and cleared on the single allowed read-only retry; final unsafe management and
  claimed jobs were zero, source `query_only=1` / `total_changes=0` /
  `quick_check=ok`, and production/exchange writes were zero. No production
  apply plan was built. Push, deployment, restart, cutover, replay, settings
  mutation, production DB apply, and exchange writes remain unauthorized.
- `2026-08-25 trigger-protection stale-wait local candidate`: exact
  production-code candidate
  `130de7bbaff5abe28c912f60a554fe39be451ecd` resolves only verified terminal
  trigger-protection intents in `retrying / wait / snapshot_incomplete` whose
  binding, execution leg, parent trigger order, nonempty `pos_id`, attribution,
  and instrument all match exactly. It also scopes pending-trigger/history
  snapshot errors to the owning instrument while retaining generic errors as
  account-wide fail-closed evidence. The terminalization RED witnessed the
  positive case remain stale (`1 failed, 8 passed`); GREEN passed `12`. The
  unrelated-instrument RED witnessed the BTC intent rewritten by an ETH error
  (`1 failed, 2 passed`); scoped-error GREEN passed `6`. The complete
  `tests/test_execution_bindings.py` passed `151`; adjacent protection/liveness
  compatibility passed `108` with `3` warnings; diff and compile checks passed.
  The one final complete suite passed `6281`, skipped `1`, with `32` warnings in
  `467.33s`; no production code changed afterward. A fresh immutable online
  production backup at
  `/Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-20260825T025238Z/research-online-backup.db`
  is mode `0600`, size `716435456`, SHA-256
  `de4926231f0c608028abd74ea9575b4448109ef307f0122477714baccfd27fe3`,
  with `quick_check=ok` and zero foreign-key violations. Invoking the shared
  helper on the independent copy matched exactly intents `138`, `141`, and
  `147`, changed `3` rows, changed `0` on identical reapply, preserved every
  other business row and all critical counts, and restored the exact starting
  rows and complete logical digest
  `3e8b3a39c520ae0d3466facae9975dd2dc11cb02f982337ff23f1e24dc23cc49`.
  Evidence manifest:
  `/Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-20260825T025238Z/rehearsal-summary.json`.
  Production remained exact
  `76e4c9486ff18d5ab1ea71eeb65f31f08072afbb`; the three production intents
  remained `retrying / wait / snapshot_incomplete`, unsafe management,
  claimed/executing worker commands, and claimed message jobs were zero, and
  the read-only postcheck had `query_only=1` / `total_changes=0`. No production
  apply plan was built. Production apply, push, deployment, restart, cutover,
  manufactured or natural Telegram observation, and exchange writes remain
  unauthorized. The next task is independent read-only review of the frozen
  candidate.
- `2026-08-25 trigger-protection independent review`: the first independent
  read-only review found no production implementation defect, zero Critical,
  two Important regression-test gaps, and one Minor counterexample gap. Test-only
  commit `1992312cebfbf2496545aa582ff0786d372d4a1b` now runs the terminal positive
  twice behind an unrelated ETH error barrier, exercises the public read-only
  loader with persisted BTC and ETH bindings and an ETH-only history failure,
  compares every intent column for all counterexamples, adds `failed` and
  `adopted` states, and covers scoped/generic/unrelated `trigger_history` and
  `pending_trigger_orders`. Removing the pre-barrier helper call produced the
  expected RED (`1 failed`); degrading the scoped loader error to generic also
  produced the expected RED (`1 failed`). Restored-code focused verification
  passed `21`, and the complete `tests/test_execution_bindings.py` passed `157`
  in `11.14s`; diff and compile checks passed. No production code changed, so
  the final complete-suite evidence remains attached to exact production-code
  candidate `130de7bbaff5abe28c912f60a554fe39be451ecd` and was not rerun. Independent
  re-review returned Ready `Yes`, zero Critical, zero Important, and only the
  now-corrected stale test names in this implementation plan as a Minor finding.
  Work remains local and unpushed. Production apply, push, deployment, restart,
  cutover, Telegram traffic, and exchange writes remain unauthorized; the next
  action requires explicit owner direction.
