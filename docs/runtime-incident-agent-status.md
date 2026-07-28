# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 2
phase_name: deterministic-incident-adapters-and-telegram-baseline
phase_status: in_progress
last_completed_phase: 1
last_completed_commit: 9820d3b
production_commit: 9820d3be6e4ff426e60cdc9ee84c200dd0b63397
local_tests:
  - "phase-2-focused-regressions: 310 passed"
  - "full-suite: 2558 passed, 1 skipped; pytest cleanup hung after the completed summary and was interrupted"
server_verification:
  status: pending
  deployed_commit: 9820d3be6e4ff426e60cdc9ee84c200dd0b63397
  service: active
  incident_agent_behavior: phase-1-ledger-only
  remaining: "Prove a safe deployment window; deploy 22ac194 with capture and Telegram disabled; verify continuity; canary one capture class and its source-to-incident dedupe; then enable and verify Telegram delivery."
enabled_flags: []
known_issues:
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

- Status: in progress
- Local implementation commit: `22ac194`
- Review: no Critical, Important, or Minor findings
- Local verification: 310 focused/regression tests passed; the full suite
  reported 2558 passed and 1 skipped, then hung during pytest cleanup and was
  interrupted
- Runtime defaults: all capture classes disabled and Telegram delivery disabled
- Remaining: safe-window production deployment, disabled-state continuity
  verification, one-class capture/dedupe canary, and Telegram delivery canary

## Current Phase Exit Checklist

Phase 2 is not complete until:

- [x] technical/runtime-only adapter tests fail for the intended missing behavior;
- [x] `unresolved`, `hold`, and ordinary contextual reanalysis are excluded;
- [x] best-effort adapters cannot alter the original source transition;
- [x] deterministic Telegram reports are bounded, redacted, deduplicated, and
  non-blocking;
- [x] focused and critical regression tests pass;
- [x] changes are reviewed, committed, and pushed;
- [ ] production deploys with capture and notification flags disabled;
- [ ] one capture class is canaried only after source-to-incident dedupe evidence;
- [ ] Telegram delivery is enabled only after capture evidence is correct.
