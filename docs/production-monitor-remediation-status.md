# Production Monitor Remediation Status

This file is the canonical ownership and progress record for the production-monitor remediation approved on 2026-08-21.

```yaml
workflow: production-monitor-remediation
design_file: docs/plans/2026-08-21-production-monitor-remediation-design.md
implementation_plan_file: docs/plans/2026-08-21-production-monitor-remediation.md
phase_status: completed
claimed_by: null
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
candidate_commit: 556f53592436ab21f49281acda3448cf7037010e
focused_tests: "final monitor and web: 350 passed in 4.61s; final updater and installer: 108 passed, 1 skipped in 85.37s"
full_suite: "5888 passed, 1 skipped, 17 warnings in 442.65s (0:07:22)"
deployed_commit: 10160398630dc15472dc660fe13ca8721a19337d
production_window: "2026-08-22T05:10:17+00:00 to 2026-08-22T05:41:36+00:00 (at least 30 continuous minutes)"
real_messages_observed: 38
chats_observed: 9
monitor_cycles_observed: "2 (one non-notifying diagnostic and one scheduled cycle; both healthy)"
expected_head_verified: true
adapter_failure_absent: true
capture_writer_warning_absent: true
queue_backlog_verified: true
duplicate_processing_verified: true
loop_health_verified: true
evidence_path: /var/lib/telegram-kol-monitor/evidence/production-monitor-remediation-20260822T051016Z.jsonl
outstanding: null
```

Keep raw JSON, long logs, position rows, and detailed monitor output in a server-side evidence file. Do not place credentials, message contents, provider responses, or exchange payloads in this document.
