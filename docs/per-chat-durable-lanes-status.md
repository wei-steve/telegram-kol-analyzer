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
claimed_by: unclaimed
claim_base_sha: null
current_task: phase-6-awaiting-claim
current_phase: phase_6_compatible_deployment
current_phase_file: docs/plans/2026-08-25-per-chat-activation-event-loop-optimization/phase-6-compatible-deployment.md
last_completed_phase: phase_5_trigger_intents_read_only_gate
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
phase_6_authorization: not_started_requires_new_claim
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
