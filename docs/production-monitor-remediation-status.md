# Production Monitor Remediation Status

This file is the canonical ownership and progress record for the production-monitor remediation approved on 2026-08-21.

```yaml
workflow: production-monitor-remediation
design_file: docs/plans/2026-08-21-production-monitor-remediation-design.md
implementation_plan_file: docs/plans/2026-08-21-production-monitor-remediation.md
phase_status: in_progress
claimed_by: codex-monitor-remediation-20260822T020604Z
scope: live-position projection, capture adapter contract, expected-HEAD deployment sync
risk_level: L2
message_lock_mode: global
message_pipeline_mode: queue
deepseek_402_in_scope: false
```

## Claim protocol

The implementation session must operate exclusively in:

```text
/Users/steven/Documents/telegram获取消息/.worktrees/runtime-serialization
```

Before editing any implementation file, it must:

1. Read `AGENTS.md`, this status file, the design file, and the implementation plan file.
2. Verify the worktree is clean and `HEAD` is exactly the handoff commit supplied by the planning session.
3. Fetch `origin/codex/deepcoin-auto-trading-v1` read-only and record both local and remote SHAs. A known local-ahead state is not by itself permission to push unrelated commits.
4. Verify no other session has changed `phase_status` or `claimed_by`.
5. Change only `phase_status` to `claimed` and `claimed_by` to a unique Codex session identifier.
6. Stage this file explicitly, verify the staged path, and commit the claim before changing code.

If the worktree is dirty, HEAD differs from the handoff commit, another owner is present, or the canonical fields contradict one another, stop. Do not reset, clean, stash, overwrite, or infer completion.

## Allowed transitions

```text
planned -> claimed -> in_progress -> completed
```

- Set `in_progress` immediately after the claim commit and before the first production-code edit.
- Keep `in_progress` while any local, deployment, restart, traffic, or monitor evidence remains incomplete.
- Set `completed` only after every acceptance criterion in the implementation plan is proven.
- On a fail-closed stop, keep `in_progress` and record the exact gate, timestamp, candidate SHA, and evidence path below.

## Evidence record

The implementation session must append concise, non-secret facts here:

```yaml
candidate_commit: null
focused_tests: "adapter contract: 11 passed + 13 passed; live endpoint: 13 passed; fresh composite: 53 passed; updater and installer: 106 passed, 1 skipped in 82.05s"
full_suite: null
deployed_commit: null
production_window: null
real_messages_observed: null
chats_observed: null
monitor_cycles_observed: null
expected_head_verified: null
adapter_failure_absent: null
capture_writer_warning_absent: null
queue_backlog_verified: null
duplicate_processing_verified: null
loop_health_verified: null
evidence_path: null
outstanding: "final focused groups and one full suite, then exact-SHA push/deploy authorization"
```

Keep raw JSON, long logs, position rows, and detailed monitor output in a server-side evidence file. Do not place credentials, message contents, provider responses, or exchange payloads in this document.
