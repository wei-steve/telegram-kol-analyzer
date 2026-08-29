# Deepcoin Contract Cache Ownership Repair Status

```yaml
workflow: deepcoin-contract-cache-ownership-repair
design_status: approved
current_phase: candidate_integration
phase_state: planned
claimed_by: null
candidate_sha: c3730ef6ea9406f490b44fab97847b556f946fb8
candidate_content_sha: 9a0b883515de1af4e3785383bd059e62d8ea4bff
handoff_sha: c3730ef6ea9406f490b44fab97847b556f946fb8
pushed_sha: c3730ef6ea9406f490b44fab97847b556f946fb8
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
pending_entry_cancel_live_order_count: 7
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
simple_cancel_all_cutover_status: final_local_candidate_unpushed
simple_cancel_all_cutover_design_sha: 71eb1d4b
simple_cancel_all_protocol_removal_sha: a3434ebb
simple_cancel_all_reconciliation_sha: ec0b9dee
simple_cancel_all_bytecode_fix_sha: d2c640e9
simple_cancel_all_review_repair_base_sha: a61325181c54a2d3aef85247fbaabcef93d7489a
simple_cancel_all_production_candidate_sha: 44b99d82c662c264554dcb07b18ed11faa3222ff
simple_cancel_all_final_focused: 314_passed_1_skipped
simple_cancel_all_final_suite: 6626_passed_3_skipped_32_warnings
simple_cancel_all_production_executed: false
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
  `49b8f40c9af0f38344724c84f39a7e065e5beabd`; push, SSH, deployment, restart,
  production writes, and trading writes remain unauthorized.
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
- Production use remains separately gated: exact-SHA review/push, a fresh
  read-only preflight, each production freeze/fence transition, and each of the
  seven single-order cancellation applies require explicit authorization.
  Every order must be freshly replanned; an unknown result forbids retry and
  stops the sequence. Deployment, restart and future-signal-only restore remain
  later, separate authorities.

- The cross-process quiescence reviewed cancellation candidate
  `708a479f7e20aba74869d87acb3839f3fd91e96b` has not been pushed, deployed, or
  executed in production. Its earlier cancellation-tool base was pushed at
  `91bb257e2a1c808c25a54149a7c71c392c0952e4`. Any fresh production read-only
  plan, candidate push/deployment, and each individual Deepcoin cancellation
  require their own explicit authorization. An unknown result must stop the
  sequence and must not be retried; the remaining exact orders must be freshly
  replanned one at a time.
- The local candidate closes the previously recorded separate-process TOCTOU in
  code, but production remains on the older unleased runtime. Therefore no live
  apply is safe until this exact candidate is separately pushed, deployed and
  verified, then settings are separately frozen and a fresh dry-run is reviewed.
- Production remains unchanged at
  `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`; automatic entry remains enabled.
  Do not enter Task 13 while any of the seven unprotected pending trigger entries
  remains live. A new read-only preflight must prove zero such orders, or a
  separately designed and authorized drain/cancellation procedure must safely
  terminalize them before deployment. Unique ownership alone is not a safe
  worker-stop window.
- The legacy health classification remains closed: HTTP 404 alone is insufficient.
  It requires the same token, authenticated health on the exact worker port,
  `runtime_role=worker`, and exact previous SHA route absence in addition to the
  verified previous SHA and closed legacy monitor env.
- Explicit freeze, exact-SHA deployment, Linux/root helper verification,
  worker refresh/health checks, bounded observation, and future-signal-only
  restore are separate Phase 3-4 authorizations. No push, deployment, SSH,
  restart, production write, Telegram replay, or Deepcoin write was performed.

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
  separately pending; this is not an activation-ready, push-authorized or
  deployment-authorized claim.

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
  diagnostic string. These compatibility decisions and fixture repairs are not
  authorized in this status-only Task 10 batch.
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
- A separate local repair authorization is required before changing the stale
  fixtures/contracts and adding the missing composed falsifier. Even after a
  new green final candidate, the exact future authorizations remain separate:
  push; stage; SSH and the read-only production preflight; deployment; every
  service-control transition including freeze/mask/stop/start/restart; DB-copy
  rehearsal; the production L3 seed; seven independent Deepcoin single-order
  writes with a fresh plan and token for each; immutable bootstrap; and entry
  thaw. Any additional settings write, database write, Deepcoin write, or
  production mutation also requires its own explicit authorization. Unknown
  remains permanently non-retryable and stops the sequence.

## Task 10 compatibility repair and final local evidence

- The separately authorized local compatibility repair is exact commit
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
  Task 10 test gate only; it is not production-dependent acceptance and does
  not authorize handoff, push, stage, SSH, deployment, service control, seed,
  order cancellation, bootstrap, entry thaw, settings/database writes,
  Deepcoin writes, or any other production mutation.
- Future production work remains split across the exact independent
  authorizations recorded above. Every reviewed order still requires a fresh
  plan and new confirmation token, and any unknown result permanently stops the
  sequence without automatic retry.

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
  position identity, adopted child order, protection exchange order, active
  noncanonical sibling, ambiguous fill, malformed/missing completed authority,
  or coordinated identity drift fails closed before a database write. The
  command never calls a Deepcoin write endpoint.
- The SQLite backup is created as an exclusive `0600` inode under a verified
  owner-only parent, captures uncheckpointed WAL commits, detects source and
  destination path replacement, validates the persisted bytes with
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
  or Telegram trading send. Every such production phase remains outstanding
  and requires a new exact authorization. Historical production SHA and order
  observations remain stale routing context, never current acceptance evidence.
