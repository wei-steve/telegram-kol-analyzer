# Deepcoin Contract Cache Ownership Repair Status

```yaml
workflow: deepcoin-contract-cache-ownership-repair
design_status: approved
current_phase: candidate_integration
phase_state: planned
claimed_by: null
candidate_sha: 2ab3e92458abcda65f4a5c46b11616eb820742ec
candidate_content_sha: a6ac63cb57d633831196414f7a55bb1bd0f321f2
handoff_sha: 2ab3e92458abcda65f4a5c46b11616eb820742ec
review_findings_repair_base_sha: 49b8f40c9af0f38344724c84f39a7e065e5beabd
production_sha: null
auto_trade_frozen: false
freeze_raw_message_id: null
restore_raw_message_id: null
historical_replay_allowed: false
```

## Ownership rule

If `phase_state` is `claimed` or `in_progress` and `claimed_by` does not match
the current task, stop immediately without modifying the repository. When the
phase completes or pauses, record both verified evidence and outstanding work.

## Verified

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
- No push, deployment, SSH, restart, production/database/settings write,
  Telegram replay, manufactured traffic, or Deepcoin write was performed.
- `2ab3e92458abcda65f4a5c46b11616eb820742ec` is the evidence-bearing repaired
  handoff commit. The following status-only commit records that SHA; any future
  exact-SHA action must use its read-only `git rev-parse HEAD` result as the
  final local integration target, avoiding an impossible self-reference.
- Task 11 exact-SHA review rejected the prior candidate before push. The
  owner-authorized local RED→GREEN repair is claimed at exact clean base
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

- Linux/root sticky-directory integration test before production deployment.
- Any push or further Phase 2 integration action remains separately authorized.
- Production server preflight must still prove the exact checkout/topology,
  clean tracked tree, safe window, complete zero active-write evidence, healthy
  queues/listener/reconciliation/protection, and complete Deepcoin read-only
  attribution/history.
- Explicit freeze, exact-SHA deployment, Linux/root helper verification,
  worker refresh/health checks, bounded observation, and future-signal-only
  restore are separate Phase 3-4 authorizations. No push, deployment, SSH,
  restart, production write, Telegram replay, or Deepcoin write was performed.
