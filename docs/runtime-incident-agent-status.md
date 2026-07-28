# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 1
phase_name: durable-runtime-incident-ledger
phase_status: planned
last_completed_phase: 0
last_completed_commit: pending-phase-0-commit
production_commit: not-required-for-documentation-only-phase
local_tests:
  - documentation-files-present
server_verification: not-required-for-documentation-only-phase
enabled_flags: []
known_issues: []
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
- Commit: recorded after commit in the next checkpoint update

## Current Phase Exit Checklist

Phase 1 is not complete until:

- [ ] additive incident model and bootstrap path are tested;
- [ ] same-fingerprint deduplication is tested;
- [ ] claim transitions are concurrency-safe;
- [ ] bounded/redacted storage is tested;
- [ ] architecture-boundary test passes;
- [ ] context-resolution and management regressions pass;
- [ ] changes are committed and pushed;
- [ ] a safe deployment window is proven;
- [ ] production is updated with all agent flags off;
- [ ] service, listener, reconciliation, and safety monitor are verified;
- [ ] this file advances to Phase 2.
