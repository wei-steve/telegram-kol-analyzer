# Deepcoin Contract Cache Ownership Repair Status

```yaml
workflow: deepcoin-contract-cache-ownership-repair
design_status: approved
current_phase: task12_findings_local_red_green
phase_state: claimed
claimed_by: task12-findings-local-red-green-2026-08-27
candidate_sha: null
candidate_content_sha: null
handoff_sha: null
pushed_sha: d2a9c4c615a3fc25af5842f6209b3a080e763e5c
review_findings_repair_base_sha: 49b8f40c9af0f38344724c84f39a7e065e5beabd
task12_findings_repair_base_sha: eb3dc0d0868d8131f003c869842bddba07aa5c29
production_sha: 0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f
auto_trade_frozen: false
freeze_raw_message_id: null
restore_raw_message_id: null
task12_gate: failed_closed
task12_observed_max_raw_message_id: 13522
task12_evidence_path: /run/deepcoin-cache-task12.wUO5Zp/evidence.jsonl
historical_replay_allowed: false
```

## Ownership rule

If `phase_state` is `claimed` or `in_progress` and `claimed_by` does not match
the current task, stop immediately without modifying the repository. When the
phase completes or pauses, record both verified evidence and outstanding work.

## Verified

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
- The historical `contract_spec_sync_unavailable` terminal set is now 15 rather
  than the previously recorded 14. All 15 remain `verified_refusal` with
  `attempted_exchange_write=0`; the newest is raw message 13491. Four older
  instruction execution contracts remain pending/deferred with zero attempted
  exchange writes and require a separate read-only explanation before retrying
  Task 12.
- The candidate `--check` against the real cache was read-only. Type, link,
  group and mode passed; owner and Agent ACL failed. The production monitor env
  still pins the production HEAD but lacks the candidate auto-trade expectation
  field, so the current candidate updater cannot pass its monitor-env preflight.
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

- Repair and re-review the candidate's legacy monitor-env upgrade path before
  Task 13; the production env does not yet contain the required governed
  auto-trade expectation line. Any new candidate requires a new exact-SHA push.
- Make the Linux/root pytest test provide traversable ancestors to the dropped
  worker identity, then rerun the exact shipped test; the direct kernel behavior
  is proven but the prescribed automated acceptance remains red.
- Explain the four old zero-write nonterminal execution contracts and update the
  historical terminal refusal baseline from 14 to 15 without replay or data
  mutation.
- Rerun Task 12 under new authorization after the candidate is repaired.
  Deepcoin history completeness exhausted its single allowed retry in this
  session and remains unknown; no Task 13 authorization is currently eligible.
- Explicit freeze, exact-SHA deployment, Linux/root helper verification,
  worker refresh/health checks, bounded observation, and future-signal-only
  restore are separate Phase 3-4 authorizations. No push, deployment, SSH,
  restart, production write, Telegram replay, or Deepcoin write was performed.
