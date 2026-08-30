# Deepcoin Contract Cache Ownership Repair Status

```yaml
workflow: deepcoin-contract-cache-ownership-repair
design_status: approved
current_phase: manual_cleanup_production_cutover
phase_state: in_progress
claimed_by: codex-01a04f45-e0e5-7642-aeb7-0c398bd03375
candidate_sha: 89a7dc66ea0c788f48be2e9841cec010cd8feeb1
candidate_content_sha: 89a7dc66ea0c788f48be2e9841cec010cd8feeb1
handoff_sha: 89a7dc66ea0c788f48be2e9841cec010cd8feeb1
pushed_sha: 89a7dc66ea0c788f48be2e9841cec010cd8feeb1
review_findings_repair_base_sha: 49b8f40c9af0f38344724c84f39a7e065e5beabd
task12_findings_repair_base_sha: eb3dc0d0868d8131f003c869842bddba07aa5c29
production_sha: 0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f
auto_trade_frozen: false
freeze_raw_message_id: null
restore_raw_message_id: null
task12_gate: failed_closed
task12_gate_policy: version_aware_local
task12_health_classification: legacy_capability_absent
task12_observed_max_raw_message_id: 13534
task12_refusal_baseline_count: 16
task12_time_sensitive_pending_trigger_count: 7
pending_entry_cancel_candidate_status: superseded_by_simple_cancel_all
pending_entry_cancel_candidate_sha: 708a479f7e20aba74869d87acb3839f3fd91e96b
pending_entry_cancel_pushed_base_sha: 91bb257e2a1c808c25a54149a7c71c392c0952e4
pending_entry_cancel_revision_gate_plan_sha: 7a17c3a0818c9f674fc5afb6bafb163bc48639b1
pending_entry_cancel_revision_scope_fix_base_sha: be97f3233838e6e0867529cf04cd4e380d9c9625
pending_entry_cancel_quiescence_base_sha: 47ea0885d02532faf7a941694f6b19dcdb1af9a6
pending_entry_cancel_quiescence_plan_sha: 99ce6d9e3e52314b485ce9c7561a93e95a41a862
pending_entry_cancel_production_executed: false
pending_entry_cancel_live_order_count: 0
legacy_runtime_drain_bridge_status: rejected_deleted_local_task10_complete
legacy_runtime_drain_bridge_base_sha: be9d75cdab57ffe57daea03b9eb1cf862cae698b
legacy_runtime_drain_bridge_design_sha: 50aa78086f70286291a7161df64681c215957a38
legacy_runtime_drain_bridge_plan_sha: 1dd9868233670486ac8575f609e954fa221f6071
legacy_runtime_drain_bridge_content_sha: c13f207df762a725de428ce0657064df55c53443
legacy_runtime_drain_bridge_review_base_sha: 5024a59e97b4328acba101f9bc138d7bf3d47530
legacy_runtime_drain_bridge_review_design_sha: 4a2a2ac0793e3faddbbc69e4940e6391b6652795
legacy_runtime_drain_bridge_review_plan_sha: d53aadbe602f8397e29cb25216c6e131240f31fb
legacy_runtime_drain_bridge_production_executed: false
immutable_control_bootstrap_status: superseded_deleted_local
simple_cancel_all_cutover_status: production_preflight_failed_closed_history_query_incomplete
simple_cancel_all_cutover_design_sha: 71eb1d4b
simple_cancel_all_protocol_removal_sha: a3434ebb
simple_cancel_all_reconciliation_sha: ec0b9dee
simple_cancel_all_bytecode_fix_sha: d2c640e9
simple_cancel_all_review_repair_base_sha: a61325181c54a2d3aef85247fbaabcef93d7489a
simple_cancel_all_production_candidate_sha: c1c046a34c5125d7bfe6452d33e9a0ff1a1f0609
simple_cancel_all_final_focused: 314_passed_1_skipped
simple_cancel_all_final_suite: 6626_passed_3_skipped_32_warnings
simple_cancel_all_production_executed: false
manual_cleanup_read_only_verified_at: 2026-08-29T20:20:19Z
manual_cleanup_exchange_snapshot_status: zero_live_orders_historical_requires_fresh_cutover_recheck
manual_cleanup_target_fill_count: 0
manual_cleanup_local_eligible_count: 7
manual_cleanup_local_repair_status: complete_production_cutover_pending
manual_cleanup_local_repair_base_sha: bd73ceb15eb7228f8d9e52641891578cb1883253
manual_cleanup_local_repair_focused: 344_passed_1_skipped
manual_cleanup_local_repair_final_suite: 6644_passed_3_skipped_32_warnings
manual_cleanup_exact_history_repair_base_sha: eafe60cbadb69f52246c5e1c9cbb1d71850df506
manual_cleanup_exact_history_repair_code_sha: 5a3bb9383037d4e3e03b843352af947b46356cb6
manual_cleanup_exact_history_repair_focused: 190_passed_plus_2_targeted_passed
manual_cleanup_exact_history_repair_final_suite: 6663_passed_3_skipped_32_warnings
manual_cleanup_exact_history_repair_review: no_remaining_p0_p1
manual_cleanup_maintenance_pacing_status: complete_fresh_production_cutover_pending
manual_cleanup_maintenance_pacing_base_sha: 98120385974870420c2be0abb3f297df3e8855ff
manual_cleanup_maintenance_pacing_design_sha: 800b42659c6a2d199a49eec4998de750f8636064
manual_cleanup_maintenance_pacing_plan_sha: 8370d474864d5167377a6615d69d0f63f69df4c3
manual_cleanup_maintenance_pacing_red_sha: 7afcc9877c533a27a68c409f4d0eaecbe133e6c4
manual_cleanup_maintenance_pacing_code_sha: 63088ad03f8696a0734da2ec1996ff68a2395ae4
manual_cleanup_maintenance_pacing_focused: 270_passed
manual_cleanup_maintenance_pacing_final_suite: 6665_passed_3_skipped_32_warnings
manual_cleanup_maintenance_pacing_review: no_p0_p1
manual_cleanup_timer_pid_repair_status: complete_fresh_production_cutover_pending
manual_cleanup_timer_pid_repair_base_sha: ac196e14951f657aa12ed68750b3501f6c94a5e8
manual_cleanup_timer_pid_repair_code_sha: cce1f8654d94a572b0340a62f17226dbb93d2da0
manual_cleanup_timer_pid_repair_focused: 299_passed_1_skipped
manual_cleanup_timer_pid_repair_final_suite: 6667_passed_3_skipped_32_warnings
manual_cleanup_timer_pid_repair_review: no_p0_p1
manual_cleanup_production_cutover_status: maintenance_stopped_fresh_reconciliation_ready
manual_cleanup_production_stage_sha: 89a7dc66ea0c788f48be2e9841cec010cd8feeb1
manual_cleanup_production_stage_content_sha256: 8dee8c014b2be5fc2ae495b865d5d2f807b0da60d0b3f177793705961289c828
manual_cleanup_production_stage_manifest_sha256: 2b614bba3dc0ea8c1101363ff98d23c59caf9c6f6b8ab788a0ee694eff86a6de
manual_cleanup_production_evidence_path: /var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/evidence.jsonl
manual_cleanup_production_evidence_sha256: 9d81ee2f085cca2b9764950f36e467083a8f8e7c93361fea0b073bb897bfa608
manual_cleanup_production_preflight_at: 2026-08-30T01:56:18Z
manual_cleanup_production_preflight_blocker: null
manual_cleanup_production_target_fill_status: all_7_zero_in_both_stable_snapshots
manual_cleanup_production_preflight_attempt: 4
manual_cleanup_production_preflight_http_status: null
manual_cleanup_production_backup_created: false
manual_cleanup_production_authority_seeded: false
manual_cleanup_production_database_mutation_executed: false
manual_cleanup_production_service_control_executed: true
manual_cleanup_production_activation_executed: false
manual_cleanup_production_observation_started: false
manual_cleanup_production_runtime_terminal_state: maintenance_stopped
manual_cleanup_production_runtime_process_count: 0
manual_cleanup_production_persistent_inhibit_proven: true
manual_cleanup_production_inhibit_directory_mode: 0755
manual_cleanup_production_stopped_preflight_database_path: /var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/stopped-preflight.db
manual_cleanup_production_stopped_preflight_database_sha256: f76b28af4121760436424fc083e6b053cb9caa3565bf6aa2516b83bf4dc20243
manual_cleanup_production_stopped_preflight_fingerprint: 7ead66602f3d73244ce9fa50c177a9fa3e3a81a3102ee8b8ee1bb218d807eda7
rejected_release_sha: ffb06d19eabfd32dfdab2942b2152fd2809e3d17
rejected_release_active: false
task12_evidence_path: /run/deepcoin-cache-task12.wUO5Zp/evidence.jsonl
task12_latest_evidence_location: codex_task_transcript
historical_replay_allowed: false
```

## Ownership rule

If `phase_state` is `claimed` or `in_progress` and `claimed_by` does not match
the current task, stop immediately without modifying the repository. When the
phase completes or pauses, record both verified evidence and outstanding work.

## Execution scope policy

A user may approve one coherent phase covering all normal steps named in that
scope. Those steps do not require repeated per-action confirmation. Exact SHAs,
immutable manifests, fresh production evidence, backups, rollback boundaries,
and fail-closed unknown handling remain mandatory. Stop for a material scope
expansion or an irreversible action that the approved phase did not include.

## Verified

- The reviewed cancellation cross-process quiescence repair started from exact
  clean SHA `47ea0885d02532faf7a941694f6b19dcdb1af9a6` on branch
  `codex/phase0-deploy-integration`. Design and implementation-plan commits are
  `39221cc8dfd8a5681412aa075f97854e4bd79e41` and
  `99ce6d9e3e52314b485ce9c7561a93e95a41a862`; the durable authority primitive,
  v2 worker integration, cancellation integration and final review repair are
  `fbfc9f7989434365e493bbd503d90b1cc28da8f0`,
  `c8d0c0b673b94f428482644888256b1f2404c53c`,
  `9267755707108e312349cf9207c7b98c7a89492e` and
  `708a479f7e20aba74869d87acb3839f3fd91e96b`.
- The closed-schema SQLite authority lease is acquired with `BEGIN IMMEDIATE`
  and has no timeout, steal or automatic recovery. Malformed state, unsupported
  schema and unknown owner remain fail-closed. Cancellation acquisition proves
  `auto_trade_enabled=false` and `entry_revision_v2_mode=disabled` in the same
  transaction. Protection, rescue and non-revision management authority is not
  routed through this lease.
- Both the v2 revision worker and the legacy revision orchestration acquire the
  same worker authority before planning/claiming or any exchange write. Legacy
  revision also refuses while global auto trade is frozen. Each implementation
  marks the real cancel, risk-reduction and replacement write boundaries; every
  non-success result after a write boundary, claim loss, inherited ambiguous
  progress or escaping exception retains authority. Only complete success or an
  explicit, proven pre-write result may release it.
- Reviewed cancellation remains dry-run by default and exact-single-order per
  apply. It rebuilds the reviewed plan while holding authority, preserves fresh
  fingerprint/token/intent gates, never retries unknown, releases only on an
  explicit pre-write refusal or complete local terminalization, and retains on
  every escaping exception or post-write incomplete outcome.
- RED reproduced v2 normal-return release after cancel unknown and replacement
  mismatch, legacy bypass of frozen/held authority, and apply exception cleanup.
  Later review RED covered post-cancel stop/evidence incompleteness, claim loss
  and legacy post-cancel size drift. GREEN passed 37 authority/v2 tests, 93
  legacy/auto-execution tests, 54 cancellation tests and the final 340-test
  adjacent set. Three independent review rounds ended ready with zero remaining
  Critical or Important findings.
- The first repository run after production code commit `708a479f` completed
  6513 passes and exposed only a stale canonical-status assertion. After that
  status/test-only correction, the final repository suite passed 6514 tests
  with 2 skipped and 32 warnings in 546.97 seconds. No production code changed
  after either full run.
- No push, deployment, SSH, settings freeze, restart, production/database write,
  Deepcoin write, historical replay or Telegram trading send occurred in this
  local repair.
- The historical-unrelated revision ambiguity scope repair started from exact
  clean SHA `be97f3233838e6e0867529cf04cd4e380d9c9625` on branch
  `codex/phase0-deploy-integration`. The approved design and implementation-plan
  update is `7a17c3a0818c9f674fc5afb6bafb163bc48639b1`; the production-code and test
  candidate is `afe5f50e6123182b34f5dc821521febc0120b851`.
- The active-authority gate still treats every non-terminal revision batch and
  any parent claim residue as global authority. Terminal, claim-free ambiguous
  children are ignored only when their parent binding, target lifecycle,
  execution leg and order are all unrelated to the fixed reviewed target set.
  Any target-related `cancel_submitting`/`submit_unknown` revision leg or
  `submit_reserved`/`submitted` replacement remains fail-closed. Missing parent
  rows and an empty reviewed target set also fail closed.
- RED first reproduced four unrelated terminal-child false blocks. Additional
  RED tests exposed replacement order-only overlap, orphan ambiguous children
  and the empty-target boundary. GREEN passed the complete reviewed-cancellation
  file at 39 tests and the adjacent six-file group at 213 tests. The one final
  repository suite passed 6475 tests with 2 skipped and 32 warnings in 626.28
  seconds. Independent review found no Critical, Important or Minor finding in
  the authorized scope.
- This local turn performed no push, deployment, SSH, freeze, restart,
  production/database/Deepcoin write, historical replay or Telegram send. The
  existing cross-process authority-check-to-exchange-write TOCTOU was neither
  widened nor claimed solved by this query-scope repair.
- The production read-only dry-run of pushed base
  `91bb257e2a1c808c25a54149a7c71c392c0952e4` failed closed before producing
  actions because its revision gate treated three old, unclaimed
  `recovery_required` batches as live authority. A separate read-only diagnostic
  confirmed all seven reviewed orders still appeared exactly once with unchanged
  economics, local ownership, pending state and request fingerprints; BTC/ETH/SOL
  counts were 4/3/0 with zero positions, regular orders, fills or unreviewed
  pending triggers.
- The approved gate-fix design and implementation plan are commit
  `1eb6b0c70f7e2dd648258fcce779c522a0095504`; the production-code candidate is
  `1b561ebc95292d45080c0a014e71d848cc86466f`. The planner now permits only a
  terminal, claim-free, ambiguous-child-free revision batch. It still blocks
  every non-terminal batch, any claim token/timestamp residue,
  `cancel_submitting`/`submit_unknown` revision legs, and
  `submit_reserved`/`submitted` replacements, including after recovery clears
  the parent claim.
- TDD captured the original false block, the claimed-before-child race and the
  no-claim unknown-outcome child states. The final reviewed-cancellation file
  passed 31 tests, the adjacent six-file group passed 204 tests, and the final
  repository suite passed 6467 tests with 2 skipped and 32 warnings in 694.98
  seconds. Independent review found no remaining Critical or Important findings;
  its sole Minor test-isolation finding was fixed before the final suite.
- The exact seven-entry cancellation helper is locally RED-to-GREEN complete at
  code candidate `d39fefa46f0c01b500059d9da77bbe0aa973f1df`. It is dry-run
  by default, opens only an existing database, selects exactly one reviewed
  order per apply, requires fresh plan and action fingerprints plus a globally
  single-use confirmation token, never retries an unknown write, verifies exact
  cancelled history/no fill/no new position or regular order/unchanged siblings,
  and terminalizes only the exact dependent local rows in one transaction after
  complete confirmation.
- The helper stores only closed-schema request/result evidence and bounded reason
  codes. Transport and response failures were tested without exception or raw
  response leakage. Focused and adjacent verification passed 236 tests; the
  final repository suite passed 6458 tests with 2 skipped and 32 warnings in
  679.07 seconds. `compileall` and `git diff --check` also passed. Independent
  code review found no remaining Critical or Important findings.
- Candidate `d39fefa46f0c01b500059d9da77bbe0aa973f1df` is local and
  unpushed. No SSH, freeze, deployment, restart, production/database/Deepcoin
  write, historical replay, or production Telegram action occurred during
  implementation. The live count of seven is retained from the latest separately
  authorized read-only Task 12 snapshot; it was not refreshed in this local-only
  turn and the seven orders remain untouched by this candidate.
- The version-aware gate candidate was non-force pushed and the remote
  `codex/deepcoin-auto-trading-v1` ref was independently verified at exact SHA
  `c3730ef6ea9406f490b44fab97847b556f946fb8` before the latest Task 12 rerun.
- The latest health classification closed the old-version ambiguity. The same
  token returned authenticated monitor-capture health HTTP 200 on the exact
  worker port, loop health proved `runtime_role=worker`, the exact production
  SHA route inventory contained no contract-spec route, and the contract-spec
  request returned HTTP 404. It is therefore `legacy_capability_absent`, not an
  authentication or runtime-role failure.
- Task 12 nevertheless remains fail-closed on a real safety condition: Deepcoin
  has seven unprotected pending trigger entries. All seven are uniquely owned by
  one local binding, but their lifecycle is `pending_entry`, order leg is
  `pending`, trigger-protection recovery is `pending`, and no row carries native
  attached protection. A worker-stop deployment window could overlap a fill
  before the protection path resumes, so Task 13 is not eligible.
- The latest read-only database watermark was 13534. SQLite remained WAL with
  `quick_check=ok`; active exchange writes, claims, active management, worker
  commands and revision claims remained zero. The refusal baseline remained 16
  terminal zero-write rows, with no invalid member or replay evidence.
- A second explicitly authorized Task 12 production-read-only preflight used
  candidate `a2bc1b4a42e7f9aeceadb2d1e5eb9006d707f3e6`. The local and remote
  candidate refs matched exactly. Production remained at
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`; its tracked tree was clean and
  its 15 untracked files were classified as historical configuration backups.
- Split ingest/worker/Web remained active with zero restarts, monolith remained
  inactive, and the Telegram session plus lock were owned only by ingest.
  SQLite was WAL, query-only and `quick_check=ok`; active exchange writes,
  non-shadow claims, active management, worker commands and revision claims
  were all zero. The observed maximum raw message ID was 13530.
- The isolated Linux/root sticky test passed against candidate code in a unique
  `/tmp` directory. Candidate `--check` against the real cache was read-only and
  found one recognized migratable legacy drift: type, link, runtime group and
  mode passed, while the real cache lacked the worker owner and Agent deny ACL
  required by the final candidate contract. No cache metadata or content changed.
- The refusal baseline is now 16 exact terminal
  `contract_spec_sync_unavailable` rows. Every row is `verified_refusal` with
  `attempted_exchange_write=0`; the newest is raw message 13529. This is an
  observed baseline only: historical replay, backfill and resubmission remain
  forbidden.
- The four old zero-write nonterminal contracts are now explained without
  mutation. Contracts 119 and 169 never entered submission and have no trade
  signal, binding or order leg. Contracts 148 and 151 are legacy shadow mirrors:
  their items terminalized through visibility-retry expiry while the shadow
  contracts remained deferred. All four have zero attempted exchange writes.
- Current Deepcoin account queries completed with zero positions, zero pending
  regular orders and pending BTC/ETH/SOL trigger counts of 4/3/0. Multiple
  schema-valid history and fills reads reached the venue's fixed boundary; this
  is bounded 100-row history coverage, not proof of complete account history.
- The approved version-aware gate design is commit `8ff0f692`; its implementation
  plan is `ac0c7399`. The local gate contract now separates immutable pre-deploy
  checks, recognized legacy migration, and strict post-deploy acceptance without
  changing runtime code, updater rollback, monitor redaction or trading authority.
- Task 12 findings were repaired locally from exact clean base
  `eb3dc0d0868d8131f003c869842bddba07aa5c29`. The claim commit is `f2ca84f9`,
  the approved narrow design/plan commit is `ca912d4f`, the legacy monitor-env
  upgrade repair is `f621164f`, the isolated Linux/root test repair is
  `b4cbef50`, the historical boundary update is `19891a47`, and the review P1
  repair is `9a0b883515de1af4e3785383bd059e62d8ea4bff`.
- A known legacy monitor env is accepted only when its metadata and unique
  expected-HEAD contract pass and the governed auto-trade line is absent. The
  updater backs up the original bytes, inserts the requested fixed option into
  a `0600` candidate, strictly validates it before atomic installation, and
  restores the byte-identical legacy env on later failure. Current-schema
  normalization remains idempotent; duplicate and invalid values fail closed;
  secret values are preserved without being printed.
- Read-only review found and repaired one P1 in the first local candidate:
  systemd `EnvironmentFile` whitespace/continuation semantics could hide a
  second managed key from line-anchored matching. The final updater accepts only
  the installer's closed, single-line assignment grammar and rejects leading
  whitespace, control characters, quotes, backslash continuations and other
  noncanonical lines before checkout. The focused re-review found no remaining
  P0-P2 findings.
- The shipped Linux/root sticky pytest now creates a unique traversable ancestor
  under `/tmp` instead of pytest's root-only temporary tree. The exact test
  passed as root in a local Linux container with no network, a read-only source
  mount and isolated tmpfs: 1 passed in 0.04s. It retained the real fork,
  UID/GID drop, root-owned replacement refusal, permission convergence and
  worker-owned replacement success; no real cache path was used.
- The earlier Task 12 repair used the then-observed count of 15 terminal
  zero-write refusals and left four old contracts for production explanation.
  That evidence is superseded by the current 16-row baseline and the completed
  read-only explanation above; replay and backfill remain forbidden.
- Final affected focused set: 920 passed, 2 skipped, 2 warnings in 169.17s.
  Bash syntax, Python compilation and `git diff --check` passed. The only valid
  final full suite after the last production-code edit: 6433 passed, 2 skipped,
  32 warnings in 546.36s. No production code changed after that run.
- During this Task 12 findings repair, no push, deployment, SSH, restart,
  production/settings/database mutation, Telegram send/replay, manufactured
  traffic or Deepcoin write was performed.
- `a61c2617430a44ab629bfa0de581aca1172a2b6e` is the evidence-bearing Task 12
  findings handoff commit. The following status-only commit records that SHA;
  any future exact-SHA action must use its read-only `git rev-parse HEAD`
  result as the final local integration target, avoiding self-reference.
- Phase 2 non-force push completed and the remote
  `codex/deepcoin-auto-trading-v1` ref was independently verified at exact SHA
  `d2a9c4c615a3fc25af5842f6209b3a080e763e5c`.
- Task 12 production preflight ran under explicit read-only authority. Production
  remained clean on branch `codex/deepcoin-auto-trading-v1` at
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`; monolith was inactive, the ingest,
  worker and Web services were active, and the monitor timer was active. The
  Telegram session lock owner matched the ingest PID.
- SQLite evidence was complete through `query_only=1`, WAL mode and
  `PRAGMA quick_check=ok`. Active exchange writes, pending/claimed non-shadow
  queue jobs, active management batches, active worker commands and revision
  claims were all zero. Recent queue parity had zero missing, orphan and stuck
  jobs. The observed preflight maximum raw message ID was 13522.
- The bounded account snapshot completed with zero positions and zero pending
  regular orders. Pending trigger/TPSL reads completed for the configured
  BTC/ETH/SOL set with counts 4/3/0. Full history acceptance did not pass:
  BTC position history failed twice and multiple history/fill reads returned
  exactly the 100-row boundary, so completeness remains unknown.
- The prior production preflight observed 15 terminal zero-write refusals, with
  newest raw message 13491, and identified four contracts needing explanation.
  The current evidence above supersedes those counts and completes that
  explanation without mutation.
- The prior candidate `--check` against the real cache was read-only. Type, link,
  group and mode passed; the real cache did not satisfy the worker-owner or
  Agent-deny checks. The production monitor env still pins the production HEAD
  but lacked the candidate auto-trade expectation field, so that prior candidate
  updater could not pass its monitor-env preflight.
- The shipped Linux/root pytest case failed because the root-only pytest
  ancestors were mode `0700`, preventing the dropped worker identity from
  traversing to the inner sticky directory. A corrected direct kernel proof in
  a separate traversable isolated directory passed: replacement failed while
  root-owned, permission convergence produced worker/runtime `0660`, and the
  worker identity then replaced the target successfully. The real cache was not
  modified.
- Raw JSON, exact exchange rows and long logs are retained only in the
  root-owned `0700` directory with `0600` evidence file at
  `/run/deepcoin-cache-task12.wUO5Zp/evidence.jsonl`. Task 12 stopped fail-closed;
  no freeze, deployment, restart, settings/database mutation, replay, Telegram
  send or Deepcoin write was performed.
- Task 11 findings were repaired locally from exact clean base
  `49b8f40c9af0f38344724c84f39a7e065e5beabd`. The repair plan/claim commit is
  `f6e99937`; descriptor/current-entry binding and ACL persistence are in
  `34bd6289`; the remaining monitor/updater/runbook and descriptor-cleanup
  repairs are in candidate content commit
  `a6ac63cb57d633831196414f7a55bb1bd0f321f2`.
- Publication now restores the explicit `telegram-kol-agent:---` deny ACL on
  every Linux candidate inode, binds descriptor inspection to the current
  directory entry, and closes the pre-`fdopen` descriptor on ACL/setup failure.
  Unknown owner/group and replaced entries remain fail-closed.
- Main, diagnostic, and test-notification monitor units all consume the governed
  auto-trade expectation. The updater backs up, installs, and restores all three
  fixed unit paths with the monitor env in one rollback boundary; monitor output
  continues excluding secret values.
- `freeze_raw_message_id` is audit-only. A distinct
  `restore_raw_message_id = MAX(raw_messages.id)` is recorded after the enabled
  updater succeeds and immediately before settings recovery. All messages at or
  below it, including updater-window arrivals, remain terminal with zero replay
  and zero backfill.
- Final focused candidate: 915 passed, 2 skipped, 2 warnings in 157.04s.
- The only completed final full suite for this repaired candidate: 6428 passed,
  2 skipped, 32 warnings in 631.09s. No production code changed after this run.
- Bash syntax checks, Python compilation, and `git diff --check` passed. A
  read-only follow-up review found no remaining P0-P2 findings.
- During the local repair session, no push, deployment, SSH, restart,
  production/database/settings write, Telegram replay, manufactured traffic or
  Deepcoin write was performed.
- `2ab3e92458abcda65f4a5c46b11616eb820742ec` is the evidence-bearing repaired
  handoff commit. The following status-only commit records that SHA; any future
  exact-SHA action must use its read-only `git rev-parse HEAD` result as the
  final local integration target, avoiding an impossible self-reference.
- The legacy-runtime drain bridge was implemented locally from exact clean base
  `be9d75cdab57ffe57daea03b9eb1cf862cae698b`. Design and plan commits are
  `50aa78086f70286291a7161df64681c215957a38` and
  `1dd9868233670486ac8575f609e954fa221f6071`; planner, freeze/fence,
  cancellation binding, CLI/drain/release, and final race-review commits are
  `ce692e80aa92bd9c2967f473588c25a356c4205e`,
  `b8eeeecd7468bc7d96fadc49510334d01b4707e1`,
  `9b8a9ca852fce50c9c04978da37b0f692603a2b0`,
  `9b6292f1f10aad42dffc7ef3b336a26628b46e3b`, and
  `6e51eeed7ce23eabb691082198578421d5cc7c39`.
- Review RED proved four unsafe boundaries: worker identity could drift across
  the read/apply exchange window; drain accepted tampered event evidence;
  rollback restored settings after governed-setting drift; and drain accepted
  incomplete binding terminalization. GREEN now re-reads exact HEAD,
  systemd MainPID and descriptor-bound `/proc/<pid>/stat` identity at every
  mutation boundary, retains both authority layers on any post-boundary drift,
  rechecks target-related/unowned unknown mutations in the same SQLite write
  transaction, and validates exact event, intent, protection, binding and
  lifecycle terminal state before drain/release.
- The bridge keeps the unchanged legacy worker and its protection/rescue/
  management authority running. It fences only entry-revision authority with
  exact null-time sentinel claims, executes no bulk loop, accepts one reviewed
  order per explicit apply, and permanently stops on an unknown result.
  Historical terminal revision children remain ignored only when their parent,
  binding, lifecycle, leg and exact order are unrelated to the reviewed set;
  orphan or target-related ambiguity remains fail-closed.
- Final bridge verification passed the 12-file authority/protection regression
  at 359 tests with 3 existing warnings, Python compilation, and
  `git diff --check`. The single final repository suite passed 6563 tests with
  2 skipped and 32 existing deprecation warnings in 558.69 seconds. No
  production code changed after that full-suite run.
- No push, deployment, SSH, restart, production freeze, settings/database or
  Deepcoin write, order cancellation, historical replay, manufactured traffic,
  or Telegram send occurred while building and reviewing this local bridge.
- The independent-review findings repair started from exact clean SHA
  `5024a59e97b4328acba101f9bc138d7bf3d47530` on branch
  `codex/phase0-deploy-integration`. The bounded compatibility-cutover design
  and implementation plan are
  `4a2a2ac0793e3faddbbc69e4940e6391b6652795` and
  `d53aadbe602f8397e29cb25216c6e131240f31fb`.
- The P0 global-freeze defect is repaired by the durable internal
  `legacy_entry_submission_frozen` setting. New entry and entry-revision writes
  remain disabled while configured management, composite management, protection
  rescue and liveness authority stay live. New-entry multi-leg submission and
  bridge freeze now serialize through the same exact-owner SQLite authority;
  zero-write failure releases it, while any possible-write unknown retains it.
- Candidate handoff uses bridge schema v3 with immutable legacy identity and a
  distinct current authority identity. It preserves the entry-only freeze and
  exact revision sentinels, binds subsequent cancellation to the candidate, and
  allows rollback only before any reviewed write boundary. Runtime identity is
  hard-bound to `telegram-kol-worker.service`, exact checkout HEAD, stable
  MainPID/start ticks, exact proc cwd and the bounded
  `telegram-kol-research web --runtime-role worker` cmdline.
- A reviewed zero-write refusal is retryable only when its durable intent is a
  structurally exact `prewrite_refused` record with `submitted=false` and an
  allowlisted reason. A fresh plan and new confirmation token re-arm that exact
  intent under `BEGIN IMMEDIATE`; malformed, submitting, recovery-required or
  possible-write outcomes remain unknown and non-retryable. No refusal payload
  or CLI result exposes credentials or bridge/confirmation tokens.
- Drain evidence is timestamped after all exchange reads and compared with a
  separate post-identity transition time. Negative age and age over 60 seconds
  fail closed. Revision fence and later sentinel validation share one
  nonterminal-batch scope; unrelated terminal claim residue is ignored, while
  active foreign claims and target-related/orphan unknown children still block.
- RED reproduced every authorized review finding plus the later exact-intent
  re-arm boundary. GREEN checkpoints were 383, 226, 126, 133, 118, 138 and 99
  tests; the post-review re-arm regression passed in a 128-test affected set.
  The final wide authority/protection regression passed 1271 tests with 3
  existing warnings. Python compilation and exact base-to-candidate
  `git diff --check` passed.
- The only final repository suite after the last production-code edit passed
  6607 tests with 2 skipped and 32 existing deprecation warnings in 750.91
  seconds. Exact content candidate
  `c13f207df762a725de428ce0657064df55c53443` received a final base-diff review;
  no remaining P0-P2 finding was found in the authorized scope. No historical
  replay path or bulk cancellation loop was added.
- This review-findings repair performed no push, deployment, SSH, production
  freeze, restart, settings/database/Deepcoin write, order cancellation,
  historical replay, manufactured traffic or Telegram trading send. The seven
  production orders and production runtime were not queried or changed.

### Prior rejected candidate history

- Task 11 exact-SHA review rejected the prior candidate before push. The
  owner-authorized local RED→GREEN repair started at exact clean base
  `49b8f40c9af0f38344724c84f39a7e065e5beabd`. At that checkpoint no push, SSH,
  deployment, restart, production write, or trading write had occurred.
- The approved design and implementation plan were read in full.
- Initial gates passed at `bad13a7b56c833919536dfb7f028725201fc22cc` on
  `codex/phase0-deploy-integration` in the authoritative workspace.
- The implementation plan was validated and committed separately as
  `da56a7ede4965f42af173c6e5c98d1f5e4e9b2d6`.
- Tasks 2-3 implemented the descriptor-safe fixed-target ownership helper and
  permission contract in `bedc61d7`; focused result: 70 passed, 1 skipped.
  The skip is the Linux/root sticky-directory kernel integration test on macOS.
- Task 4 separated worker-owned cache handling from root/shared session files
  in `b496015a`; focused result: 61 + 5 passed.
- Task 5 transactionally installed and rolled back the worker helper/unit in
  `50925b44`; focused result: 166 passed.
- Task 6 added worker-owned closed-schema cache health projection in
  `5b92b424`; focused result: 96 + 6 passed.
- Task 7 added monitor cache health/refusal gates in `73516101`; focused result:
  689 passed, 1 skipped.
- Task 8 governed frozen monitor expectations and monitor unit/env rollback in
  `c22c17ca`; focused result: 133 passed, 1 skipped. Shell syntax checks passed.
- Task 9 added the controlled deployment/recovery runbook in `14819b30`;
  documentation/static result: 79 passed.
- Final focused candidate: 910 passed, 2 skipped, 2 warnings in 146.63s.
- The one and only final full suite: 6423 passed, 2 skipped, 32 warnings in
  558.75s. No production code changed after this run.
- The two macOS skips were the new Linux/root sticky-directory kernel test and
  the existing Linux/systemd sandbox probe.
- `git diff --check` passed and the pre-handoff worktree was clean at candidate
  content SHA `14819b309f27025d183f4bd27b8210ac74996e92`.
- `314f7c19628b8c49c15519fb3af405e704e718a4` is the evidence-bearing handoff
  commit. The following status-only commit records that SHA; the receiving
  phase must use its read-only `git rev-parse HEAD` result as the exact final
  integration target, avoiding an impossible self-referential status hash.

## Outstanding

- The repaired legacy-runtime drain bridge content commit
  `c13f207df762a725de428ce0657064df55c53443` is local only. It has not been
  pushed, installed, invoked against production, or used to cancel any of the
  seven reviewed orders. The following status-only commit records this evidence;
  any future exact-SHA review or push must resolve the then-current local HEAD
  rather than treating this content SHA as a self-referential handoff SHA.
- The cross-process quiescence reviewed cancellation candidate
  `708a479f7e20aba74869d87acb3839f3fd91e96b` has not been pushed, deployed, or
  executed in production. Its earlier cancellation-tool base was pushed at
  `91bb257e2a1c808c25a54149a7c71c392c0952e4`. An unknown result must stop the
  sequence and must not be retried; remaining exact orders must be freshly
  replanned one at a time.
- The local candidate closes the previously recorded separate-process TOCTOU in
  code, but production remains on the older unleased runtime. Therefore no live
  apply is safe until the exact candidate is deployed and verified, settings
  are frozen, and a fresh dry-run is reviewed.
- Production remains unchanged at
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`; automatic entry remains enabled.
  Do not enter Task 13 while any of the seven unprotected pending trigger entries
  remains live. A new read-only preflight must prove zero such orders, or a
  bounded drain/cancellation procedure must safely terminalize them before
  deployment. Unique ownership alone is not a safe worker-stop window.
- The legacy health classification remains closed: HTTP 404 alone is insufficient.
  It requires the same token, authenticated health on the exact worker port,
  `runtime_role=worker`, and exact previous SHA route absence in addition to the
  verified previous SHA and closed legacy monitor env.
- No push, deployment, SSH, restart, production write, Telegram replay, or
  Deepcoin write was performed during that phase.

## Immutable control bootstrap local replacement (Tasks 7-9)

- The rejected release `ffb06d19eabfd32dfdab2942b2152fd2809e3d17`
  remains inactive. It was not activated or used for any trading operation by
  this local batch.
- Local commit `e2f82058` introduces the one-time immutable-control bootstrap:
  the candidate-start boundary is entered only while bootstrap authority is
  held and the durable process-local entry freeze is installed. Local commit
  `05c71c6c` makes the monitor verify the loaded immutable release scope rather
  than checkout HEAD.
- The rejected legacy drain bridge, its CLI surface, its compatibility command,
  its dedicated tests, and the internal legacy settings freeze key have now
  been deleted locally. Future authority-changing ordinary activation requires
  the exact `web`, `monitor`, `ingest`, and `worker` component set. The existing
  action plan continues to prohibit exchange writes, historical/frozen-message
  replay, bulk order actions, production settings writes, and trading enablement.
- Exact final local test evidence and the final candidate commit remain pending
  Task 10. No push, production read, seed, order cancellation, activation,
  automatic thaw, SSH, restart, production/database/settings write, Deepcoin
  write, or Telegram replay occurred in this Tasks 7-9 local batch.
- Independent review rejected initial Tasks 7-9 head `3198fb80` with five
  Important findings: the bootstrap CLI had no concrete executor, evidence and
  authority times were stale across boundaries, unknown compensation was not
  proven, monitor scope omitted exact drop-ins/loaded identity, and ordinary
  activation did not actually execute the candidate monitor.
- The local follow-up now wires `bootstrap-control --apply` to the root guard,
  exact generation-CAS authority adapter, and a concrete systemd runtime
  adapter. It captures, atomically publishes, verifies, and can restore every
  governed base unit and activation drop-in. Candidate processes are started
  entry-frozen while bootstrap authority is held and are persistently
  re-inhibited while identity and no-write authority self-test proof runs.
  Compensation failures are explicit blocked terminal states rather than
  swallowed exceptions.
- Evidence freshness is rechecked at apply and candidate-start boundaries;
  release/block/self-test operations use fresh boundary timestamps. Monitor
  success now requires exact cwd, actual `/proc` command role, loaded artifact,
  unique PID/start tuples, all governed unit/drop-in bytes, and an actual
  no-notification diagnostic unit run from the candidate release. The checkout
  `--expected-head` monitor fallback is removed.
- The first follow-up review rejected exact head `1a0c0876` because manifest
  expiry was not rechecked at the mutation boundary, monitor proof followed
  authority release, compensation could stop at its first failed mask, release
  content/ownership was not fully revalidated, and authority freshness trusted
  self-reported booleans. A later review also caught and repaired one misplaced
  CLI reload that briefly broke the separate seed action.
- Exact content candidate `905c0993` closes those findings. Bootstrap reloads
  the same root-owned manifest after planning and rechecks its deadline before
  mutation and candidate start. Monitor proof occurs while bootstrap authority
  is held. Compensation attempts every target/unit disable, mask and stop before
  proving quiescence. Monitor recomputes the complete immutable release and
  ownership/mode contract. Shared authority evidence requires bounded numeric
  ages, effective management/rescue modes, and exactly one worker owner.
- Reboot takeover now has one persistent boot edge,
  `telegram-kol-runtime.target`, with `Requires+After` for worker, web, ingest
  and the monitor timer; every direct unit boot edge is disabled. Before the
  guard receipt is removed, target, all three services and the timer must be
  active, all three identities must again prove the exact release and complete
  role health, and their PID/start-tick tuples must match the pre-release proof.
  Any child failure or process replacement reinhibits and fails closed.
- The final Tasks 7-9 adjacent set passed 609 tests with 1 existing skip.
  Independent exact-archive review of `905c0993` passed 597 tests with 1 skip,
  `git diff --check` passed, and found no remaining Critical or Important
  finding. Task 10's one final repository suite and final local handoff remain
  pending; this was not an activation-ready candidate.

## Task 10 final local verification (failed closed)

- The production-code candidate is exact commit
  `905c099372d6ca26fc789443330ef657ccd39951` with tree
  `be0cafaf1189357202746aea758885f6357f7bdb`. Task 10 began from the
  status-only commit `fbbde09af98f2e4dee7ae8a111039f9187326a22` with tree
  `7e6cba8e62a1a21ecbc88ccd2cce2f94a2e7638c` and a clean worktree.
- Static verification passed `git diff --check`; production scope
  `src scripts deploy` contains no `legacy_runtime_drain_bridge` or
  `legacy_entry_submission_frozen` reference. The plan's broader command also
  searched `tests`, where expected negative compatibility assertions still
  mention the deleted names, so that command is not a valid zero-match gate as
  written. CLI help exposes exactly the three maintenance actions
  `seed-entry-authority`, `drain-one`, and `bootstrap-control`.
- The exact focused Task 10 set passed 974 tests with 1 skip in 36.37 seconds.
  The one and only final repository suite did not pass: 6585 tests passed, 14
  failed, 3 skipped, with 32 warnings in 478.53 seconds. An isolated rerun of
  only the failing files reproduced all 14 failures, with 4 adjacent tests
  passing, so the result is deterministic rather than suite-order pollution.
- Twelve failures use temporary databases without the now-required independently
  seeded entry-revision exchange-authority row and therefore correctly stop at
  `entry_revision_exchange_authority_missing` before reaching their older
  fault-injection or planner assertions. One freeze test reaches the safe
  pre-write refusal through `RecoveryLiveSubmitError` rather than its older
  `DeepcoinExecutionActionError` contract. One stage-helper test is rejected by
  the stricter closed action-manifest schema before reaching its older exact
  diagnostic string. Those compatibility decisions and fixture repairs remained
  outside that status-only Task 10 batch.
- The planned named cross-boundary first falsifier
  `test_cancel_timeout_then_crash_and_reboot_never_retries_or_restores` is not
  present in the implemented suite, so composed crash-after-unknown acceptance
  is missing. Its two component proofs pass: write-boundary unknown blocks and
  retains the token hash, and crash/reboot reconciliation keeps every governed
  unit persistently masked without starting one (2 tests passed in 0.15
  seconds). This is component evidence only, not a substitute for the missing
  composed falsifier.
- The rejected bridge implementation and its dedicated tests were deleted;
  the compatibility CLI and ordinary-settings freeze field were removed. They
  were replaced by the persistent runtime guard, immutable action manifests,
  independent authority seed, exact-single-order drain, immutable bootstrap,
  loaded-release monitor proof, and persistent target takeover described above.
- Task 10 therefore stops failed closed. No final handoff candidate is claimed,
  and no production-dependent acceptance is claimed. No push, stage, SSH,
  deployment, read-only production preflight, DB-copy rehearsal, production
  seed, order cancellation, bootstrap, entry thaw, freeze, restart, database or
  settings write, Deepcoin write, or Telegram replay occurred in this batch.
- Unknown remains permanently non-retryable and stops the sequence.

## Task 10 compatibility repair and final local evidence

- The local compatibility repair is exact commit
  `3a641e5f192128960a5fc980c6fe2dc57ad89f1f` with tree
  `34d7152d22ae48705d28d9adc52bac68c11cf1eb`. It changes six test files only;
  production code remains the independently reviewed `905c0993` content
  candidate described above.
- The twelve authority-dependent legacy tests now explicitly seed their
  temporary databases so they exercise the intended post-L3-seed boundary;
  the dedicated missing-row tests remain unseeded and continue to prove that
  production never auto-creates authority. The direct live-submit freeze test
  now expects its actual `RecoveryLiveSubmitError` API contract. The stage-only
  test constructs a valid four-role activate manifest before asserting the
  stage helper rejects the wrong action. The authority assertion uses closed
  schema v2 `action_id` and proves that ordinary `owner_id` is absent.
- The required composed first falsifier is now present and passes. It proves an
  accepted-then-timeout cancel calls Deepcoin exactly once, blocks durable
  authority, survives simulated process crash and host reboot with every unit
  still persistently masked, starts no unit, rejects an explicit retry before
  the exchange boundary, and never restores the legacy runtime.
- Verification was GREEN at every final checkpoint: the original failing file
  group passed 167 tests; the affected authority/guard group passed 229 tests;
  the exact Task 10 focused safety set passed 975 tests with 1 documented skip
  in 27.56 seconds; and the one final repository suite on exact commit
  `3a641e5f` passed 6600 tests with 3 documented skips and 32 warnings in
  414.94 seconds. No production code or tests changed after the final suite.
- Independent review found no Critical, Important, or Minor finding in the
  six-file repair and approved it for final verification. This closes the local
  Task 10 test gate only; production remained unchanged. Every reviewed order
  still requires a fresh plan and new confirmation token, and any unknown
  result permanently stops the sequence without automatic retry.

## Simplified cancel-all cutover replacement

- The operator explicitly rejected further growth of the one-time handoff
  mechanism and selected a single maintenance window: freshly prove zero
  positions, stop the legacy runtime, cancel every entry order through the
  Deepcoin operator interface, prove zero positions/orders, reconcile local
  state once, and use ordinary entry-frozen activation.
- Design and implementation plan commit `71eb1d4b` supersedes the nine-window
  seed/drain/bootstrap design. Commit `a3434ebb` deletes the three maintenance
  commands and their bootstrap, action coordinator, manifest and seed modules.
  Commit `ec0b9dee` deletes the per-order cancellation executor and persistent
  maintenance guard, extracts the sole canonical seven-target source, and adds
  one read-only-exchange/local-write reconciliation command.
- The new `finalize-cancelled-pending-entries` command has no Deepcoin write
  path. It requires a fresh complete zero-position, zero-regular-order and
  zero-pending-trigger snapshot, creates and verifies one SQLite backup, then
  terminalizes intent, leg, protection, convergence, binding, lifecycle and
  event state and creates the missing normal idle authority row in one SQLite
  transaction. A malformed or non-idle existing row fails closed.
- Commit `d2c640e9` directly repairs the staged-release mutation found during
  read-only activation preflight: the activator invokes Python with `-B`, and
  every release drop-in sets `PYTHONDONTWRITEBYTECODE=1`. Candidate code no
  longer needs to run a self-referential bootstrap proof.
- Focused verification passed 97 tests for protocol removal, 315 tests for the
  reconciliation/authority/runtime boundary, and 63 tests with one existing
  skip for activation and bytecode protection. Python compilation and
  `git diff --check` passed. The one final repository suite after the final
  production-code edit passed 6498 tests with 3 documented skips and 32
  existing warnings in 470.34 seconds. No push, stage, SSH, production read, service control,
  Deepcoin cancellation, database mutation, activation, restart or entry thaw
  occurred in this local implementation.
- The final review-repair batch started from exact clean commit
  `a61325181c54a2d3aef85247fbaabcef93d7489a` on
  `codex/phase0-deploy-integration`. Production/test commits `e56d721e`,
  `b0bc47a3`, `faa2d015`, `4f49e9e2`, and `44b99d82` bind the maintenance
  command to one shared service-control lock, exact stopped-runtime proof,
  fresh read-only exchange evidence, the sole canonical seven-target set, and
  one atomic local terminalization transaction.
- The canonical targets now include the reviewed chat/message/strategy and
  client-order identities plus exact entry and protection economics. Any local
  position identity, adopted protection order, protection exchange order, active
  noncanonical sibling, ambiguous fill, malformed/missing completed authority,
  or coordinated identity drift fails closed before a database write. The
  command never calls a Deepcoin write endpoint.
- The SQLite backup is created as an exclusive `0600` inode under a verified
  owner-only parent, captures uncheckpointed WAL commits, detects source
  replacement, remains bound to the exclusive destination inode, rejects final
  destination path/inode replacement, and validates the persisted bytes with
  `quick_check` and `foreign_key_check`, and is complete before
  `BEGIN IMMEDIATE`. A terminalization failure rolls back all seven targets and
  the authority seed together.
- Final static verification passed Python compilation and exact-base
  `git diff --check`. The final affected set passed 314 tests with 1 documented
  platform skip. The only final repository suite after the last production-code
  edit passed 6626 tests with 3 documented skips and 32 existing warnings in
  555.17 seconds. No production code changed after that suite.
- Independent exact-base review approved
  `a6132518..44b99d82` with no remaining P0/P1. It recorded two nonblocking P2
  audit-hardening opportunities: `desired_take_profits_json` is not separately
  compared after exact canonical protection-row validation, and the closed
  SQLite journal-header allowlist lacks a direct mixed-header injection test.
  Neither weakens the stopped-runtime, zero-position/order, no-attribution,
  backup, atomicity, or no-exchange-write safety claims; no additional runtime
  state or gate was added for them.
- This final local batch performed no push, stage, SSH, production read,
  service stop/mask/start/restart, Deepcoin UI cancellation, database/settings
  mutation, activation, rollback, entry thaw, historical replay, order retry,
  or Telegram trading send. Historical production SHA and order observations
  remain stale routing context, never current acceptance evidence.
- This documentation/test handoff reconciliation started from exact clean,
  pushed SHA `6c6c5f320b6c9d34a9c5ea4caafd15d06d74b79d`. It changes only the stale
  Task 12 status assertion to the already-recorded
  `superseded_by_simple_cancel_all` state. Production code and reviewed
  production candidate `44b99d82c662c264554dcb07b18ed11faa3222ff`
  remain unchanged; the resulting handoff is local, unpushed, unstaged, and
  inactive.

## Manual operator cleanup read-only verification

- The operator manually removed the seven Deepcoin pending triggers. A bounded
  production read-only verification ran from `2026-08-29T20:19:39Z` through
  `2026-08-29T20:20:19Z` against canonical targets loaded from staged, inactive
  release `2ce91a373bef5dc9878c54c4db5a23e0ace51d49`.
- Both the before and after account snapshots contained zero positions, zero
  regular open orders, zero pending triggers, and zero canonical targets still
  pending. Exact per-target fill reads returned zero rows for all seven. The
  completed run issued 32 GET requests with no query retry and no incomplete
  response.
- The current reconciliation history contract did not pass: five targets
  returned one exact history row whose status was not represented as the
  candidate's literal `cancelled|canceled`, and two exact `ordId` history reads
  returned no row. This is a local evidence-contract blocker, not evidence that
  an active order remains.
- SQLite was read with `query_only=1`; `total_changes=0`, `quick_check=ok`,
  foreign-key issues and active exchange writes were zero. All seven canonical
  local targets remain eligible, none is partially terminalized, binding scope
  is unchanged, and the normal authority row is still missing for the later L3
  transaction.
- Production remained on tracked-clean SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`; web, ingest, worker, and monitor
  timer remained active and the monolith inactive. The first SSH result channel
  was lost; one complete read-only rerun produced the evidence above. Neither
  run performed a production, database, settings, service-control, or Deepcoin
  write.
- The next local phase should simplify only the manual reconciliation evidence
  contract: require stable zero account snapshots, exact zero fills for every
  canonical target, intact local identity, and stopped-runtime proof at the
  eventual write boundary; explicit fill or identity conflict remains blocking.
  Do not restore the deleted bridge or add another persistent handoff state.

## Manual cleanup local repair

- The local phase started from exact clean SHA
  `bd73ceb15eb7228f8d9e52641891578cb1883253` on
  `codex/phase0-deploy-integration`. It changes only the manual-cleanup evidence
  contract, the first `stopped_legacy` activation terminal state, their exact
  transport/quiescence interfaces, tests, and current documentation.
- Dry-run and apply remain the before/after stable account snapshots. Apply
  rebuilds the complete plan and requires its fingerprint to equal the reviewed
  dry-run fingerprint before backup or `BEGIN IMMEDIATE`. Every canonical
  `ordId` now receives one exact fills GET per snapshot; incomplete, full-page,
  identity-conflicting, or nonempty results fail closed without retry.
- Missing history or a unique history row without literal
  `cancelled|canceled` no longer blocks by itself. Explicit fill, partial fill,
  executed/success/completed/live states across the supported Deepcoin status
  aliases, nonzero or malformed fill quantities, duplicate rows, instrument
  mismatch, and order/client identity conflict remain blocking.
- The one-transaction reconciliation still uses only
  `REVIEWED_PENDING_ENTRY_TARGETS`, validates all local identity, economics,
  binding, leg, lifecycle, protection and convergence state, verifies an
  exclusive `0600` SQLite backup, terminalizes all targets, and creates the
  missing canonical v2 idle authority row atomically. The activation quiescence
  check now reuses that same canonical parser; missing, legacy-schema, held,
  blocked, or malformed documents remain unknown.
- First `stopped_legacy` activation validates and dispatches the candidate's own
  immutable activator without requiring a rollback release. It still requires
  the full scope inactive, persistently inhibited, `MainPID=0`, empty cgroups,
  no matching runtime process, and zero active exchange writes. Candidate
  post-start runtime/authority proof is single-attempt.
- The first falsifier passes: a protection-authority failure after partial
  candidate startup leaves every governed and legacy unit inactive and
  persistently inhibited with `MainPID=0` and empty cgroups, consumes no retry,
  starts no legacy runtime, and performs no database or exchange write. Failure
  ends `maintenance_stopped`; inability to prove that boundary reports
  `maintenance_stop_failed` rather than claiming safety.
- Ordinary immutable-to-immutable activation still requires and validates its
  rollback release. No new table, field, persistent lifecycle state, lease,
  bridge, confirmation protocol, replay, compensation order, or automatic retry
  was added.
- RED-to-GREEN checkpoints covered history relaxation/adverse evidence, exact
  fills, v2 authority/quiescence compatibility, no-retry activation and the
  partial-start falsifier. The affected final candidate passed 344 tests with
  one documented platform skip. Independent exact-base review found no P0/P1.
  Python compilation, Bash syntax and `git diff --check` passed. The one and only
  final repository suite passed 6644 tests with 3 skips and 32 warnings in
  592.53 seconds; no production code changed after that run.
- This local phase performed no push, SSH, stage, deployment, service control,
  production read/write, database/settings mutation, Deepcoin write, activate,
  entry thaw, historical replay, order retry, compensation order, or Telegram
  trading send. Historical production and account evidence above must be
  refreshed inside the later single production cutover phase.

## Manual cleanup production cutover attempt

- This task claimed `manual_cleanup_production_cutover` from exact clean local
  SHA `c1c046a34c5125d7bfe6452d33e9a0ff1a1f0609`. The exact reviewed commit was
  fast-forward pushed from remote `2ce91a373bef5dc9878c54c4db5a23e0ace51d49`
  and the remote `codex/deepcoin-auto-trading-v1` ref was independently verified
  at `c1c046a34c5125d7bfe6452d33e9a0ff1a1f0609`.
- The separate stage action created immutable inactive release `c1c046a3` with
  tree `2f7f7e081402b7f43093171eb45cc20cde7d85bc`, content SHA-256
  `ba99d1ec18a9252eff8aa3319260fc4f6687cff1a09a838e79324d27dcb8e7e4`,
  release mode `0555`, zero unsafe entries, and zero systemd references.
- Fresh production identity still matched tracked-clean legacy SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`. Web, ingest, worker and the
  monitor timer remained active; the monolith remained inactive. No service
  stop, mask, start, restart, drop-in publication or activation occurred.
- The production database preflight was query-only with `total_changes=0`,
  `quick_check=ok`, zero foreign-key issues and zero active exchange writes.
  Before counts were 24 management components, 133 management legs, 150
  management batches, 554 execution order legs, 320 bindings, 1034 lifecycles,
  3 trading settings and 3803 execution events. Its root-owned `0600` online
  copy also passed `query_only=1`, `quick_check=ok`, zero foreign-key issues and
  `total_changes=0`.
- The first attempted copy used `/run`, whose 394 MiB tmpfs is smaller than the
  approximately 813 MiB database. It failed during local SQLite backup before
  any Deepcoin query. The two incomplete files created by this task were
  explicitly removed, and evidence moved to the root filesystem's owner-only
  directory; no production database or service state changed.
- Both fresh account attempts returned zero positions, zero regular open orders
  and zero pending triggers for BTC, ETH and SOL. The BTC trigger-history reader
  returned exactly its 100-row completeness boundary on both of its bounded
  reads in each attempt. Both plans therefore stopped as
  `history_query_incomplete` before the general fills reader or any of the seven
  exact `ordId` fills queries could run.
- The incomplete history result is external unknown and permanently stops this
  production attempt. The task did not run the L3 copy rehearsal, enter the
  maintenance boundary, create the transaction backup, terminalize any target,
  seed authority, consume activation authorization, activate a candidate, or
  begin L2 observation. The seven-target fresh fill state is unknown; historical
  zero-fill evidence was not reused.
- Raw exchange rows, the verified production copy, database counts, call counts,
  unit state and the fail-closed terminal record are retained under
  `/var/lib/telegram-kol-cutover-evidence/c1c046a34c5125d7bfe6452d33e9a0ff1a1f0609/`.
  The `0600` evidence file is `evidence.jsonl`, SHA-256
  `ea563a1b2a9fbe2800662e9f6567a85206125841a34bd38083514cc1fcf62d92`.

## Exact-target history evidence repair

- The local repair started from exact clean SHA
  `eafe60cbadb69f52246c5e1c9cbb1d71850df506` and produced reviewed code commit
  `5a3bb9383037d4e3e03b843352af947b46356cb6`. It performed no push, stage, SSH,
  production read, service control, production database write, Deepcoin write,
  activation, observation, replay, retry, compensation order, bridge restore or
  entry thaw.
- Manual-cleanup reconciliation now builds its stable account snapshot from
  complete positions, regular-open-order and pending-trigger reads only. It no
  longer calls broad trigger history or broad fills, so unrelated 100-row
  account history cannot block this bounded seven-target workflow. The default
  maintenance-evidence profile remains unchanged for every other caller.
- Every canonical reviewed target receives one `instId + ordId` trigger-history
  read and one `instId + ordId` fills read per plan. There is no automatic retry.
  Exact fills must be a complete empty list. Exact history may be empty or have
  one nonliteral-cancel row, but malformed/boundary-sized/duplicate responses,
  exceptions, identity or instrument conflict, filled/live/executed states,
  nonzero or malformed fill quantities, a position identity, and a successful
  trigger timestamp remain fail-closed.
- Exact history content is bound into the plan fingerprint. A changed history
  result between dry-run and apply is rejected before backup or transaction.
  The seven-target success test proves all seven exact history/fills pairs are
  read once in dry-run and once in apply.
- RED first reproduced the broad-history dependency, absent exact trigger
  history API, missing fail-closed classifications and unbound history drift.
  Independent review then found one P1 involving `posId`, additional fill-size
  aliases and successful `triggerTime + errorCode` evidence; new RED tests
  reproduced it and the adverse detector was extended. Final independent review
  found no remaining P0/P1.
- The affected three modules passed 190 tests after the final production-code
  edit; two additional end-to-end test-only proofs also passed. Python
  compilation and `git diff --check` passed. The one final repository suite
  passed 6663 tests with 3 documented skips and 32 existing warnings in 635.98
  seconds. No production code changed after that suite.
- Production remains on legacy SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`. The old inactive stage
  `c1c046a34c5125d7bfe6452d33e9a0ff1a1f0609` is superseded for the next
  attempt. The phase stays `in_progress`: a later authorized production
  continuation must push the new exact handoff, create and verify a new
  immutable inactive stage, and restart the complete fresh preflight from the
  beginning. No prior zero-fill or account snapshot may be reused.

## Exact-target production cutover continuation

- The continuation began from exact clean local HEAD
  `8abaf2c6d6e361b7651fc41e11275e899bb6463a`. Its base-to-HEAD contained only
  reviewed exact-target evidence code/tests/design plus the status-only record.
  The explicit L2 full-scope push manifest prohibited service, database,
  settings, Telegram and exchange writes. The remote
  `codex/deepcoin-auto-trading-v1` ref was fast-forward pushed and independently
  verified at exact `8abaf2c6d6e361b7651fc41e11275e899bb6463a`.
- A separate stage action created immutable inactive release `8abaf2c6` with
  tree `40a9e085227359a1f00cebda563c9ccbb02c261b`, content SHA-256
  `68e373268885215a1bf0bd48492be8d7ce0d17d6c3a07bb744c7dc42fd14cad0`,
  manifest SHA-256
  `74c7abe4a3e76e99acedd9912edeffc2059afc56c6e5571d8643b12c58545395`
  and action-plan SHA-256
  `aa8f2bdc71c3be810f02562f7137902b889722a2e36ce3e27fee9a8f0708f48b`.
  The release and receipt matched the exact commit, the release was root-owned
  mode `0555`, and unsafe and group/other-writable entry counts were zero. It
  was not activated.
- The first preflight invocation used a superseded monitor unit name and stopped
  before creating a database copy or making a Deepcoin request. Read-only unit
  discovery identified the governed `telegram-kol-monitor.timer`; the corrected
  collector then ran once. This was an operator-script correction, not an
  external-query retry.
- Fresh runtime evidence at `2026-08-29T22:30:41Z` proved production still at
  legacy SHA `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`. Web, ingest and worker
  remained active from `/opt/telegram-kol-analyzer` with exact command roles and
  unchanged PID/start-tick identities; the monitor timer was active and the
  legacy monolith inactive. The candidate remained inactive.
- The new root-owned `0600` preflight copy at
  `/var/lib/telegram-kol-cutover-evidence/8abaf2c6d6e361b7651fc41e11275e899bb6463a/preflight.db`
  has SHA-256
  `e9990847d609619be3d9ff0e0dd9da79a0d9806858ffd869327480e26cc24445`.
  It passed `query_only=1`, `quick_check=ok`, zero foreign-key issues,
  `total_changes=0` and zero active exchange writes. Counts remained 24
  management components, 133 management legs, 150 management batches, 554
  execution order legs, 320 bindings, 1034 lifecycles, 3 trading settings and
  3803 execution events. The canonical authority row remained absent.
- The account-flat part of the first snapshot completed with zero positions,
  zero regular open orders and zero pending triggers across all three governed
  instruments. The first five canonical exact fills reads completed with zero
  rows. The sixth, for BTC, raised `DeepcoinClientError` on its sole permitted
  call at `2026-08-29T22:31:57Z`; the seventh fill and all exact history reads
  were therefore not attempted.
- This incomplete exact fill result is external unknown and permanently stops
  this cutover attempt. No automatic retry was performed. The task did not
  enter the maintenance boundary, stop or mask a unit, create the transaction
  backup, mutate the production database, terminalize a target, create the
  authority row, create or consume activation authorization, activate a
  candidate, observe L2 traffic or thaw entry.
- Raw rows, runtime/unit evidence, the read-only database copy and the terminal
  record are retained under
  `/var/lib/telegram-kol-cutover-evidence/8abaf2c6d6e361b7651fc41e11275e899bb6463a/`.
  The root-owned `0600` `evidence.jsonl` has SHA-256
  `890ba2452f55026a9ae231a725f59834dff881c85231713fd20ab0ad3a8af4b4`.
  The phase remains `in_progress`; another attempt requires explicit
  continuation and entirely fresh evidence. The failed exact query must never
  be retried automatically.

## Exact-target production cutover diagnostic continuation

- After explicit user continuation, one new read-only diagnostic preflight was
  run with fresh runtime, database-copy and Deepcoin evidence. A local synthetic
  classifier check first proved that the collector distinguishes HTTP status,
  transport and response-schema failures without recording credentials. No
  production code was changed and no automatic external-query retry was added.
- Fresh runtime evidence at `2026-08-29T22:39:31Z` again proved production at
  legacy SHA `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`. Web, ingest and worker
  retained the same PID/start-tick identities and exact legacy cwd and command
  roles; the monitor timer remained active and the legacy monolith inactive.
  A read-only check after the attempt confirmed those service states remained
  unchanged, and candidate `8abaf2c6d6e361b7651fc41e11275e899bb6463a`
  remained inactive.
- The fresh root-owned `0600` database copy at
  `/var/lib/telegram-kol-cutover-evidence/8abaf2c6d6e361b7651fc41e11275e899bb6463a/attempt-2/preflight.db`
  has SHA-256
  `4216d9828284f4885370f793525bc3402e79bb8651120050f0cce313c57b0e45`.
  It again proved `query_only=1`, `quick_check=ok`, zero foreign-key issues,
  `total_changes=0`, zero active exchange writes, the same eight critical table
  counts and an absent canonical authority row.
- The account-flat queries again completed with zero positions, zero regular
  open orders and zero pending triggers across all three instruments. The first
  five canonical exact fills queries again returned zero rows. The sole sixth
  exact fills call failed with `DeepcoinClientError` caused by HTTP status `401`;
  the seventh fills query, all history queries and the second stable snapshot
  were not attempted. The collector's summary then hit a local null-timestamp
  formatting defect after the reconciliation plan had already failed closed;
  this did not issue another external request or change the production result.
- HTTP `401` makes the required sixth-target fills evidence unknown. The second
  explicitly authorized attempt therefore also stopped before the maintenance
  boundary, with no third attempt. No unit was stopped or masked; no transaction
  backup or production database/settings mutation occurred; no Deepcoin write,
  activation authorization, activate, old-runtime restore, L2 observation or
  entry thaw occurred.
- Postmortem timing used only the already-recorded request timestamps: 15
  private GETs were issued in 2.207 seconds with approximately 0.2 ms gaps, and
  the HTTP `401` occurred at ordinal 15. The production host was NTP-synchronized
  with approximately 0.05 ms clock offset, so local clock drift is not supported.
  A burst-throttling or edge-authentication response is supported as a hypothesis
  but is not proven and must not be treated as authorization to retry.
- The second attempt's raw rows, sanitized failure category, runtime evidence,
  database copy and terminal record are retained under
  `/var/lib/telegram-kol-cutover-evidence/8abaf2c6d6e361b7651fc41e11275e899bb6463a/attempt-2/`.
  Its root-owned `0600` `evidence.jsonl` has final SHA-256
  `a834effc653d338eddf054c83d317d6df790ec49aff602d07c0b7843ae0cc3b3`.
  The phase remains `in_progress`; resolving or explaining the Deepcoin `401`
  read failure is required before a separately authorized fresh cutover attempt.

## Maintenance exact-read pacing repair

- The local repair began from exact clean HEAD
  `98120385974870420c2be0abb3f297df3e8855ff`. Design and implementation-plan
  commits are `800b42659c6a2d199a49eec4998de750f8636064` and
  `8370d474864d5167377a6615d69d0f63f69df4c3`. The first production falsifier
  tests were committed RED at `7afcc9877c533a27a68c409f4d0eaecbe133e6c4`:
  both failed because the reconciliation plan did not yet accept deterministic
  monotonic/sleep dependencies.
- Deepcoin's official rate rules limit both `/deepcoin/trade/fills` and
  `/deepcoin/trade/trigger-orders-history` to 5 requests per second and 150
  requests per minute per UID. The failed production attempt issued its first
  six exact fills reads in about 0.7 seconds, so the maintenance caller itself
  violated the documented fills endpoint contract. The observed HTTP `401`
  remains an external response rather than proof of Deepcoin's internal
  classification, but local clock drift and static credential failure were not
  supported by the evidence.
- Code commit `63088ad03f8696a0734da2ec1996ff68a2395ae4` adds only an
  invocation-local maintenance pacer. Exact fills and exact trigger-history
  reads use independent endpoint keys and a 0.41-second minimum start interval,
  including the first call. Deterministic arithmetic gives at most 3 calls in a
  sampled one-second window and 147 in a sampled 60-second window. The apply
  path forwards the same injectable monotonic clock and sleeper into its fresh
  re-plan. No pacing value enters the evidence fingerprint.
- The fail-closed contract is unchanged: a simulated sixth fills exception made
  exactly six calls, did not query the seventh target or any history row, made
  no exchange write and returned `target_fill_query_incomplete`. There is no
  retry, backoff, persistent rate state, second target list, replay, bridge,
  compensation order or trading thaw.
- GREEN verification passed the three direct pacing/no-retry/apply tests, then
  all 145 reconciliation tests in 12.21 seconds. The affected Deepcoin client,
  maintenance-evidence, reconciliation and CLI set passed 270 tests in 17.31
  seconds. Python compilation and exact-diff whitespace checks passed.
- The first full-suite invocation exposed a pre-existing status-contract drift:
  the status field did not match the repository's canonical
  `complete_production_cutover_pending` assertion. Restoring that truthful
  status-only value made the exact failed test pass. No production code changed.
  The final full repository suite then passed 6665 tests with 3 documented skips
  and 32 existing warnings in 578.55 seconds. No production code changed after
  that final suite.
- Independent local review of exact base
  `98120385974870420c2be0abb3f297df3e8855ff` through code candidate
  `63088ad03f8696a0734da2ec1996ff68a2395ae4` found no P0 or P1 issue. The
  invocation-local pacer intentionally does not claim cross-process UID quota
  coordination; any future incomplete production result remains unknown and
  stops the attempt without retry.
- This repair performed no push, SSH, production or Deepcoin query, production
  database/settings mutation, service control, stage, activation authorization,
  activate, observation, replay or entry thaw. The previously staged
  `8abaf2c6d6e361b7651fc41e11275e899bb6463a` does not contain this repair and
  must not be reused as the next candidate. A later explicitly authorized
  production cutover must push the new reviewed handoff, create a new immutable
  inactive stage and acquire entirely fresh evidence from the beginning.

## Paced production cutover attempt

- The explicitly authorized continuation started from exact clean local HEAD
  `287daacf8dbf2d44e56f311800ee85b83579e307`. The remote
  `codex/deepcoin-auto-trading-v1` ref was fast-forward pushed from
  `8abaf2c6d6e361b7651fc41e11275e899bb6463a` and independently verified at the
  exact candidate SHA.
- A separate stage action created new immutable inactive release `287daacf` with
  tree `36ff550769a6214c5402a7b0d238049275a9be5b`, content SHA-256
  `273a971025b706c3fb03f592b99c0a036f666fd07287d2d48323165de59383ef`,
  manifest SHA-256
  `afa900de77a73c360f8b0024ef0038c31515a52845b17f6fbc91a3c6b6a15d56`
  and action-plan SHA-256
  `aa8f2bdc71c3be810f02562f7137902b889722a2e36ce3e27fee9a8f0708f48`.
  The root-owned release is mode `0555`, has zero unsafe or group/world-writable
  entries and zero systemd or process references; it was not activated.
- Fresh preflight proved production still ran tracked-clean legacy SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f` with the same web, ingest and
  worker PID/start-tick identities and exact legacy cwd/command roles. Two
  stable account snapshots returned zero positions, zero regular open orders
  and zero pending triggers. Each snapshot made exactly seven paced fills and
  seven paced trigger-history reads without retry or exception; all seven fills
  were empty, while history retained the accepted five-row/two-missing shape.
  Both plans were `ready` with fingerprint
  `7ead66602f3d73244ce9fa50c177a9fa3e3a81a3102ee8b8ee1bb218d807eda7`.
- The fresh root-owned `0600` database copy at
  `/var/lib/telegram-kol-cutover-evidence/287daacf8dbf2d44e56f311800ee85b83579e307/attempt-1/preflight.db`
  has SHA-256
  `6a87ab9579217c7f1d96aea6c01cb9598707eba2dea34196b86a726678175b37`.
  It passed `query_only=1`, `quick_check=ok`, zero foreign-key issues,
  `total_changes=0` and zero active exchange writes. Critical counts remained
  unchanged and the canonical authority row remained absent.
- The maintenance-entry program stopped before its first mask or stop operation.
  The real `telegram-kol-monitor.timer` returns an empty `MainPID` property, but
  the candidate's `SystemRuntimeAdapter.main_pid()` requires an integer for
  every controlled unit and the stopped-legacy boundary includes that timer.
  The same production code is used by reconciliation and activation, so this
  candidate cannot prove the required boundary without a reviewed code repair;
  bypassing or weakening the proof is prohibited.
- Post-attempt verification proved that no stop actually began: web, ingest and
  worker retained their original PIDs, the monitor timer remained active, no
  maintenance inhibit existed, production remained on the legacy SHA and the
  candidate still had zero systemd/process references. The production database
  remained query-only during verification with 3 trading settings, 3803
  execution events, no authority row, `total_changes=0` and zero active exchange
  writes.
- No transaction backup, database/settings mutation, target terminalization,
  authority seed, activation authorization, activation, old-runtime restore,
  automatic retry, historical replay, Deepcoin write, L2 observation or entry
  thaw occurred. The root-owned `0600` evidence file is
  `/var/lib/telegram-kol-cutover-evidence/287daacf8dbf2d44e56f311800ee85b83579e307/attempt-1/evidence.jsonl`,
  SHA-256
  `1aef8a38edb7d5d290fdbaf1b2101c2e6528cdd5bd2c969dec3ff20505678377`.
  The phase remains `in_progress` and now requires a minimal local timer PID
  compatibility repair, RED-to-GREEN coverage, final suite/review and a new
  exact production candidate before another fresh cutover attempt.

## Monitor timer MainPID compatibility repair

- The local repair began from exact clean HEAD
  `ac196e14951f657aa12ed68750b3501f6c94a5e8`, after the paced production
  attempt proved that a real `telegram-kol-monitor.timer` returns an empty
  `MainPID` property before any service-control operation. Root-cause tracing
  showed that the stopped-legacy boundary had applied a service-only numeric
  `MainPID` assumption to every controlled systemd unit.
- RED first reproduced the exact failure through
  `SystemRuntimeAdapter.main_pid("telegram-kol-monitor.timer")`. A separate
  service falsifier passed before implementation and continues to prove that an
  empty `MainPID` for `telegram-kol-web.service` is unknown and raises rather
  than being converted to zero.
- Code commit `cce1f8654d94a572b0340a62f17226dbb93d2da0` adds one closed
  processless-unit exception for the exact monitor timer. Only its successful
  empty-property response is normalized to zero. Unknown units, every service,
  malformed values, negative values, cgroup occupants and matching runtime
  processes retain the existing fail-closed behavior. No mask, activation,
  database, reconciliation, exchange or retry semantics changed.
- The two direct boundary tests passed, the complete scoped-activation module
  passed 39 tests, and the affected activation, reconciliation, CLI, updater
  and quiescence set passed 299 tests with one documented skip. Python
  compilation and exact-diff whitespace checks passed.
- Local review of the exact two-file diff from
  `ac196e14951f657aa12ed68750b3501f6c94a5e8` through code candidate
  `cce1f8654d94a572b0340a62f17226dbb93d2da0` found no P0/P1. The one final
  repository suite passed 6667 tests with 3 documented skips and 32 existing
  warnings in 520.36 seconds. No production code changed after that suite.
- This local phase performed no push, SSH, Deepcoin request or write, production
  database/settings mutation, service control, stage, authorization, activate,
  observation, replay, old-runtime restore or entry thaw. The inactive staged
  release `287daacf8dbf2d44e56f311800ee85b83579e307` lacks this repair and is
  superseded. A later production cutover must push the new exact handoff, create
  and verify another immutable inactive stage and acquire all account, fills,
  history, runtime, database and no-write evidence fresh from the beginning.

## Timer-fixed production cutover attempt

- The continuation began from exact clean local HEAD
  `89a7dc66ea0c788f48be2e9841cec010cd8feeb1`. The remote
  `codex/deepcoin-auto-trading-v1` ref was fast-forward pushed from
  `287daacf8dbf2d44e56f311800ee85b83579e307` and independently verified at the
  exact candidate.
- A separate stage action created immutable inactive release `89a7dc66` with
  tree `ba35fc4ba7dd4cc59e277a5b91f645ee90a1b379`, content SHA-256
  `8dee8c014b2be5fc2ae495b865d5d2f807b0da60d0b3f177793705961289c828`,
  manifest SHA-256
  `2b614bba3dc0ea8c1101363ff98d23c59caf9c6f6b8ab788a0ee694eff86a6de`
  and action-plan SHA-256
  `aa8f2bdc71c3be810f02562f7137902b889722a2e36ce3e27fee9a8f0708f48`.
  It was root-owned mode `0555`, had zero unsafe or group/world-writable entries
  and zero systemd/process references, and was not activated.
- Fresh production preflight again proved tracked-clean legacy SHA
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f` with unchanged web, ingest and
  worker PID/start-tick identities. Both stable account snapshots returned zero
  positions, zero regular open orders and zero pending triggers. Each made
  exactly seven paced fills and seven paced trigger-history GETs with no retry
  or exception; all fills were empty and history retained the accepted
  five-row/two-missing shape. Both plans were `ready` with fingerprint
  `7ead66602f3d73244ce9fa50c177a9fa3e3a81a3102ee8b8ee1bb218d807eda7`.
- The fresh root-owned `0600` preflight copy at
  `/var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/preflight.db`
  has SHA-256
  `6120bbefcba0753254e82602ebfdf8bc605fdfa1df7cecc6cc34b4b41ff60211`.
  It passed `query_only=1`, `quick_check=ok`, zero foreign-key issues,
  `total_changes=0` and zero active exchange writes; critical counts were
  unchanged and the canonical authority row remained absent.
- Maintenance entry then failed before writing any inhibit file. The operator
  collector set `umask 077`; `SystemRuntimeAdapter.mask_unit()` created each new
  unit drop-in directory with requested mode `0755`, but the umask reduced the
  actual mode to `0700`. Its exact owner/mode guard rejected all eight
  directories before publishing the persistent inhibit. The subsequent stop
  loop completed for the running web, ingest, worker and timer units, while the
  pre-existing failed monitor oneshot remained `failed` rather than `inactive`.
- Post-failure proof found no exact matching runtime process, zero candidate
  systemd/process references and zero active exchange writes. All web, ingest,
  worker, timer and legacy `MainPID`/cgroup populations were zero, but no
  persistent inhibit existed and the empty drop-in directories were mode
  `0700`. The only valid terminal classification is therefore
  `maintenance_stop_failed`, not `maintenance_stopped`.
- No automatic retry, directory-mode repair, transaction backup, production
  database/settings mutation, target terminalization, authority seed,
  activation authorization, activation, old-runtime restore, historical replay,
  Deepcoin write, L2 observation or entry thaw occurred. Production remained on
  the legacy checkout SHA, but its split runtime is stopped and not persistently
  inhibited. The root-owned `0600` evidence file is
  `/var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/evidence.jsonl`,
  SHA-256
  `93d2f23c8ca3fc3092e45880298199810793b7aa2195df16c1b2ba5083d88786`.
  Further service-control requires explicit continuation; it must first repair
  only the eight empty directory modes, publish and verify the exact persistent
  inhibits, reset the failed monitor oneshot to inactive without starting any
  runtime, and prove the complete stopped boundary before any fresh exchange
  evidence or database transaction.

## Maintenance stop convergence continuation

- The explicitly authorized continuation started from local status HEAD
  `3a0dab7e1bd729683b110d0b280c5bed22f9cfe0` and the exact recorded
  `maintenance_stop_failed` production state. Read-only validation proved that
  all eight target drop-in directories were root-owned, non-symlink, mode
  `0700` and completely empty; no inhibit file existed. Every runtime MainPID
  and cgroup population was zero, exact runtime-process matching returned none,
  active exchange writes were zero, and only the monitor oneshot retained a
  `failed` state.
- One explicit convergence operation changed only those eight empty directory
  modes to `0755`, published the candidate's exact root-owned `0444`
  maintenance-inhibit document in each directory, reloaded systemd and cleared
  the failed monitor oneshot without starting any unit. The resulting complete
  scope proved inactive, inhibited, `MainPID=0`, empty cgroups, zero matching
  runtime processes and zero active exchange writes. The valid terminal state
  is now `maintenance_stopped`.
- From that continuously inhibited boundary, a new root-owned `0600` read-only
  database copy was created at
  `/var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/stopped-preflight.db`,
  SHA-256
  `f76b28af4121760436424fc083e6b053cb9caa3565bf6aa2516b83bf4dc20243`.
  It passed `quick_check=ok`, zero foreign-key issues, `total_changes=0`, zero
  active exchange writes, unchanged critical counts and an absent canonical
  authority row.
- Two further stopped-boundary account snapshots at
  `2026-08-30T01:56:05Z` and `2026-08-30T01:56:18Z` each returned zero
  positions, zero regular open orders, zero pending triggers, seven empty exact
  fills reads and the accepted five-row/two-missing exact history shape. All
  calls completed once with no exception or retry. Both plans were `ready` with
  fingerprint
  `7ead66602f3d73244ce9fa50c177a9fa3e3a81a3102ee8b8ee1bb218d807eda7`.
- This continuation did not start any runtime, create the transaction backup,
  mutate production database/settings, terminalize a target, seed authority,
  create or consume activation authorization, activate, restore the old
  runtime, replay history, write Deepcoin, observe L2 traffic or thaw entry.
  Evidence remains in the same root-owned `0600` file at
  `/var/lib/telegram-kol-cutover-evidence/89a7dc66ea0c788f48be2e9841cec010cd8feeb1/attempt-1/evidence.jsonl`,
  now SHA-256
  `9d81ee2f085cca2b9764950f36e467083a8f8e7c93361fea0b073bb897bfa608`.
  The next authorized action may begin only from this proven persistent stop
  boundary and must reacquire fresh evidence inside the transaction executor
  before creating the exclusive `0600` transaction backup and atomically
  terminalizing all seven targets plus the idle authority row.
