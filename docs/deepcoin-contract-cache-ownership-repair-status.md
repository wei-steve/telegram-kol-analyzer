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
task12_evidence_path: /run/deepcoin-cache-task12.wUO5Zp/evidence.jsonl
task12_latest_evidence_location: codex_task_transcript
historical_replay_allowed: false
```

## Ownership rule

If `phase_state` is `claimed` or `in_progress` and `claimed_by` does not match
the current task, stop immediately without modifying the repository. When the
phase completes or pauses, record both verified evidence and outstanding work.

## Verified

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
