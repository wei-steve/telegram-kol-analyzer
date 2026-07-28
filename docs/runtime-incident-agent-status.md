# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 3
phase_name: read-only-incident-agent-and-codex-handoff
phase_status: in_progress
last_completed_phase: 2
last_completed_commit: 22ac194
production_commit: 711b41fd6a3ce6b6d1f710f445649c7b0f83f3fb
local_tests:
  - "phase-3-focused-ledger-agent-notification: 147 passed"
  - "context-resolution-and-management-regressions: 176 passed"
  - "full-suite attempt: 2237 passed, 1 skipped before a pre-existing 0.2-second lifespan timeout failed under aggregate load; the failing test passed in isolation"
server_verification:
  status: pending
  deployed_commit: 711b41fd6a3ce6b6d1f710f445649c7b0f83f3fb
  service: active
  bounded_restarts: clean
  listener: monitoring-31-enabled-groups
  recognition: latest-message-completed
  contextual_resolution_inflight: 0
  position_mutation_inflight: 0
  management_latest: succeeded
  production_safety: stable-complete-baseline-audit_abnormal
  capture_canary: "management_partial_failed source batch 28 -> runtime incident 1"
  capture_dedupe: "three scans retained generation 1 and repeat_count 1"
  telegram_canary: delivered
  incident_agent_behavior: deterministic-capture-and-telegram-only
  remaining: "Commit and push Phase 3, prove a safe deployment window, deploy with the Agent disabled, install the sidecar unit disabled/inactive, verify continuity, then run only a synthetic read-only canary."
enabled_flags:
  - "capture:management_partial_failed"
  - "telegram:deterministic-runtime-incident-reports"
known_issues:
  - "The pre-existing production safety baseline remains `audit_abnormal` (31 blocked, 1 partial_failed, 5 recovery_required); the no-notify audit itself was stable and complete."
  - "The notification-enabled monitor service also reports missing notification configuration; Phase 1 did not alter monitor configuration."
  - "The full local suite completed its test summary but pytest cleanup hung and required interruption."
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

### Phase 1 — Durable runtime incident ledger

- Status: completed
- Runtime behavior enabled: no
- Ledger commit: `1488520` (`feat: add runtime incident ledger`)
- Shutdown continuity fix: `9820d3b` (`fix: drain live streams before web shutdown`)
- Local verification: 325 phase/regression tests and 2522 full-suite tests passed
- Production verification: additive table present and empty, Telegram session
  owned by the restarted service, recognition/checkpoint continuity preserved,
  reconciliation backlog unchanged at 168, and incident behavior dormant
- Bounded restart: old PID exited cleanly without a Uvicorn graceful timeout,
  systemd stop timeout, or `SIGKILL`
- Safety audit: stable and complete with the unchanged pre-existing
  `audit_abnormal` baseline

### Phase 2 — Deterministic incident adapters and Telegram baseline

- Status: completed
- Local implementation commit: `22ac194`
- Review: no Critical, Important, or Minor findings
- Local verification: 310 focused/regression tests passed; the full suite
  reported 2558 passed and 1 skipped, then hung during pytest cleanup and was
  interrupted
- Runtime defaults: all capture classes disabled and Telegram delivery disabled
- Production deployment: `711b41f`, initially with all Phase 2 flags disabled
- Capture canary: the sole `management_partial_failed` source batch `28`
  produced runtime incident `1`; three scans retained generation `1` and
  `repeat_count=1`
- Telegram canary: runtime incident `1` reached `delivered`
- Enabled production scope: only `management_partial_failed` capture and
  deterministic runtime-incident Telegram delivery
- Continuity: service active, HTTP 200, listener monitoring 31 groups, no
  contextual-resolution or position-mutation work in flight, latest
  recognition complete, and no new service errors

### Phase 3 — Read-only incident agent and Codex handoff

- Status: in progress
- Local implementation: closed diagnosis/tool contracts, eight bounded
  read-only projections, OpenAI-compatible tool loop, durable retry budget,
  reproducible Codex handoff, diagnosis Telegram report, and a separately
  supervised sidecar that remains disabled by default
- Review: all Critical and Important findings resolved after three review
  passes
- Local verification: 147 focused ledger/Agent/notification tests and 176
  context-resolution/management regressions passed
- Full-suite note: one pre-existing timing-sensitive Web lifespan test exceeded
  its 0.2-second deadline under aggregate load after 2237 passes and 1 skip;
  the exact test passed in isolation
- Deployment: pending safe-window proof; no Phase 3 Agent process is enabled

## Current Phase Exit Checklist

Phase 3 is not complete until:

- [x] the closed structured contract rejects extra actions and unknown tools;
- [x] all tool and transcript outputs are bounded and redacted;
- [x] the worker is dormant by default and executes no recovery action;
- [x] claims, crash recovery, retry backoff, and attempt ceilings are durable;
- [x] identical fingerprints reuse diagnosis without another model call;
- [x] evidence and Agent hypotheses remain separate and rebuild a Codex handoff;
- [x] diagnosis Telegram reports state uncertainty and that no action executed;
- [x] architecture and contextual-resolution/management regressions pass;
- [x] changes receive review with no remaining Critical or Important findings;
- [ ] changes are committed and pushed;
- [ ] production deploys with the Agent flag off;
- [ ] the sidecar unit is installed disabled and inactive;
- [ ] service/listener/checkpoint/reconciliation continuity is verified;
- [ ] a synthetic read-only incident is diagnosed without a business-row change;
- [ ] the Agent is disabled again after the canary unless separately approved.
