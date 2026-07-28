# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 1
phase_name: durable-runtime-incident-ledger
phase_status: in_progress
last_completed_phase: 0
last_completed_commit: 52a7eff
production_commit: 1488520b1674a736ba53f0a75fae018a57b6b645
local_tests:
  - "phase-1-required-regressions: 213 passed"
  - "full-suite: 2521 passed, 1 skipped"
server_verification:
  status: partial
  deployed_commit: 1488520b1674a736ba53f0a75fae018a57b6b645
  service: active
  additive_table: present-and-empty
  listener: post-restart-message-persisted
  recognition: post-restart-message-completed
  checkpoint: covers-post-restart-message
  reconciliation_backlog: unchanged-at-168
  incident_agent_behavior: dormant
  safety_audit: stable-complete-baseline-audit_abnormal
  remaining: "Repeat a controlled safe-window restart and prove the journal has no graceful-shutdown timeout, stop-sigterm timeout, or SIGKILL before completing Phase 1."
enabled_flags: []
known_issues:
  - "The deployment restart completed and message continuity was proven, but Uvicorn logged `timeout graceful shutdown exceeded`; the bounded-restart gate is not complete."
  - "The pre-existing production safety baseline remains `audit_abnormal` (31 blocked, 1 partial_failed, 5 recovery_required); the no-notify audit itself was stable and complete."
  - "The notification-enabled monitor service also reports missing notification configuration; Phase 1 did not alter monitor configuration."
phase_7_explicitly_approved: false
next_session_prompt: "请执行自定义ai agent的下一步实施"
```

## Permanent Boundary

- The existing first-pass AI and contextual multi-information strategy
  resolution remain authoritative.
- The runtime incident agent starts only from a durable runtime failure.
- It may diagnose a contextual worker failure but may not choose a strategy or
  replace the resolver's business decision.
- It may never bypass existing idempotency, exact ownership, reconciliation,
  or mutation gateways.

## Resume Protocol

When the user says `请执行自定义ai agent的下一步实施`:

1. Read `AGENTS.md` and all four runtime-agent documents.
2. Inspect the current branch, worktree, remote head, and this status.
3. If `phase_status` is `planned`, begin `current_phase`.
4. If `phase_status` is `in_progress`, resume the recorded remaining work.
5. Implement no later phase in the same turn.
6. Preserve the production continuity gates in the runbook.
7. Update this file after local tests and again after server verification.

## Phase History

### Phase 0 — Durable design and session control

- Status: completed
- Scope: documentation and `AGENTS.md` routing only
- Runtime behavior changed: no
- Production restart required: no
- Local verification: canonical documentation files present
- Commit: `52a7eff` (`docs: plan runtime incident agent rollout`)

## Current Phase Exit Checklist

Phase 1 is not complete until:

- [x] additive incident model and bootstrap path are tested;
- [x] same-fingerprint deduplication is tested;
- [x] claim transitions are concurrency-safe;
- [x] bounded/redacted storage is tested;
- [x] architecture-boundary test passes;
- [x] context-resolution and management regressions pass;
- [x] changes are committed and pushed;
- [x] a safe deployment window is proven;
- [x] production is updated with all agent flags off;
- [ ] service, listener, reconciliation, and safety monitor are verified;
- [ ] this file advances to Phase 2.

## Phase 1 Progress

- Local implementation commit: `1488520` (`feat: add runtime incident ledger`)
- GitHub push: completed on `codex/deepcoin-auto-trading-v1`
- Production deployment: completed with all incident-agent behavior dormant
- Additive schema: `runtime_incidents` exists and contains zero rows
- Continuity evidence: a message received after restart was persisted, covered
  by the checkpoint, and completed authoritative recognition; no post-restart
  recognition gap was found
- Exact remaining verification: perform one later controlled restart in a
  proven safe window and require a clean old-process shutdown journal with no
  graceful timeout, stop timeout, or `SIGKILL`; then rerun the stable no-notify
  safety audit and advance only to Phase 2
