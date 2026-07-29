# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 6
phase_name: low-risk-automatic-recovery
phase_status: in_progress
last_completed_phase: 5
last_completed_commit: 8ec6542
production_commit: 731318b236cca9609c964e4b4503a700459fc3fe
local_tests:
  - "phase-6-read-only-exchange-refresh-runtime-web-focused: 261 passed"
  - "phase-6-read-only-exchange-refresh-context-management-regressions: 396 passed"
  - "phase-6-read-only-exchange-refresh-listener-monitor-mutation-regressions: 178 passed, 1 skipped"
  - "phase-6-read-only-exchange-refresh-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-mimo-closed-final-correction-runtime-and-provider-focused: 136 passed"
  - "phase-6-mimo-closed-final-correction-context-management-regressions: 396 passed"
  - "phase-6-mimo-closed-final-correction-listener-monitor-mutation-regressions: 178 passed, 1 skipped"
  - "phase-6-mimo-closed-final-correction-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-mimo-json-final-and-bounds-focused: 215 passed"
  - "phase-6-mimo-json-final-and-bounds-context-management-regressions: 176 passed"
  - "phase-6-mimo-json-final-and-bounds-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-dedicated-mimo-provider-final-after-review-fixes: 184 passed"
  - "phase-6-dedicated-mimo-provider-final-regression: 183 passed"
  - "phase-6-dedicated-mimo-provider-post-deploy-focused: 31 passed"
  - "phase-6-dedicated-mimo-provider-final: 181 passed"
  - "phase-6-dedicated-mimo-provider-review-focused: 30 passed"
  - "phase-6-dedicated-mimo-provider-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-final-focused: 180 passed"
  - "phase-6-final-executor-race-suite: 15 passed"
  - "phase-6-final-review-focused: 52 passed"
  - "phase-6-executor-worker-ledger-notification-focused: 109 passed"
  - "phase-6-runtime-agent-database-focused: 174 passed"
  - "phase-6-context-resolution-regressions: 38 passed"
  - "phase-6-management-regressions: 421 passed"
  - "phase-6-listener-monitor-safety-regressions: 112 passed"
  - "phase-6-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-5-final-runtime-agent-monitor-and-safety-tests: 317 passed, 1 skipped"
  - "phase-5-final-context-resolution-and-management-regressions: 176 passed"
  - "phase-5-final-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-5-runtime-agent-and-safety-tests: 235 passed"
  - "phase-5-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "context-resolution-regressions: 38 passed"
  - "strategy-management-regressions: 358 passed"
  - "full-suite before final review fixes: 2611 passed, 1 skipped; every subsequently changed path passed the focused suites"
  - "phase-3-post-canary-focused: 127 passed"
server_verification:
  status: phase-6-mimo-closed-final-verified
  deployed_commit: 731318b236cca9609c964e4b4503a700459fc3fe
  service: active-http-200
  bounded_restarts: "service restarted without SIGKILL; raw message 8309 crossed the restart with a live pre-restart evidence lease, logged one already-in-progress recovery error, then completed through normal lease expiry recovery"
  listener: monitoring-31-enabled-groups-and-continuing
  recognition: "latest raw message 8293 had a completed recognition before and after deployment"
  contextual_resolution_inflight: 0
  position_mutation_inflight: 0
  management_latest: "succeeded; six old partial_failed/recovery_required rows were historical, with no active claim or mutation"
  production_safety: "current diagnostic completed with monitor_error null and only the unchanged audit_abnormal baseline"
  sidecar: installed-disabled-inactive
  agent_flag: disabled
  production_incident_row: "incident 1 is diagnosed/delivered with one accepted shadow nomination, action_executed false, and the total runtime incident count remains 1"
  phase_4_offline_gate: "7 reviewed cases; all six metrics at 1.0"
  phase_4_readonly_tools: "all nine bounded tools executed against incident 1; only projection keys and evidence counts were inspected"
  incident_agent_behavior: "one bounded read-only projection plus deterministic shadow policy only; sidecar remained disabled and no playbook or business mutation executed"
  phase_5_predeployment:
    reviewed_commit: 8ec6542
    pushed: true
    production_checkout_before_deploy: 3de5bc7debe26e7b402622e4d0d62418d87d5d7d
    service: active-http-200
    listener: monitoring-31-enabled-groups
    latest_recognition: "raw message 8286 completed"
    contextual_resolution_claims: 0
    management_ready_or_running: 0
    management_recent_nonterminal_10m: 0
    position_mutation_inflight: 0
    position_mutation_recent_10m: 0
    management_audit: "stable/ok/complete on bounded retry; 80 rows, no malformed rows, submit_unknown=0, historical partial_failed=1 and recovery_required=5"
    sidecar: disabled-inactive
    agent_flag: disabled
    shadow_allowlist: empty
    production_checkout_after_deploy: 8ec6542de4ebe29eed8ab419109d654a9e12cad2
    deployment: "completed after a fresh safe-window check; bounded restart shut down cleanly"
    monitor_fix: "the monitor now opens the existing incident ledger without running bootstrap migrations; the diagnostic completed with monitor_error null and only the unchanged audit_abnormal baseline"
    post_deploy: "service active HTTP 200; listener monitoring 31 groups; latest raw message 8293 retained a completed recognition; contextual claims and position mutations zero"
    deployed_offline_gate: "7 reviewed cases; all nine Phase 5 metrics at 1.0"
    deployed_focused_tests: "95 passed"
    canary: "incident 1 used one bounded incident-summary tool call and nominated only refresh_read_only_exchange_snapshot; runtime-shadow-policy-v1 accepted it with would_execute=false and action_executed=false"
    source_integrity: "source management batch 28 remained historical partial_failed with updated_at 2026-07-21 15:20:33.518543; no business row or position mutation changed"
  phase_6_predeployment: "service active HTTP 200; latest raw message 8308 had a completed recognition; evidence, runtime-agent, management, and position-mutation work in flight were all zero"
  phase_6_deployment: "commit 1fe682980f97e17ac7dc54307036c5dff40a92a1 deployed with a bounded restart; service returned HTTP 200; sidecar remained disabled/inactive; agent and action authority were false with zero shadow/action allowlists"
  phase_6_ledger: "runtime_agent_recovery_attempts table present with zero rows"
  phase_6_continuity: "raw message 8309 arrived immediately before the restart and retained its pre-restart evidence lease; deterministic lease recovery completed recognition after expiry, while later messages 8310 and 8311 also completed; no management or position mutation was in flight"
  phase_6_deployed_tests: "91 focused tests passed; 7-case offline evaluation passed all nine metrics at 1.0"
  phase_6_monitor: "bounded no-notify diagnostic completed with monitor_error null and only the unchanged audit_abnormal baseline"
  phase_6_dedicated_provider: "root-owned mode 0600 configuration installed; sidecar identity loaded only the dedicated MiMo host/model/key presence through systemd-style environment injection; agent and action authority false with empty allowlists"
  phase_6_provider_probe: "one non-business request with no incident, Telegram, strategy, position, or exchange data returned HTTP 200 from mimo-v2.5 with the required tool-call shape; response content, headers, key, and usage were not printed or persisted"
  phase_6_provider_tests: "32 deployed focused tests passed; 7-case offline evaluation passed all nine metrics at 1.0; service HTTP 200, sidecar disabled/inactive, recovery-attempt ledger empty, latest three recognitions complete, and no evidence claim, management batch, or position mutation in flight"
  phase_6_provider_restart_continuity: "raw message 8314 arrived immediately before the final runtime restart, retained its pre-restart evidence lease, and completed through normal lease-expiry recovery; no message gap or business mutation was observed"
  phase_6_mimo_final_compatibility: "MiMo mimo-v2.5 rendered the forced final function as text markup, so commits 51b0fad and 5e96b55 switched the no-tool final turn to validated JSON-object output and explicitly published the existing diagnosis bounds in prompt v6. Raw provider responses and usage were not printed or persisted."
  phase_6_action_canary: "Incident 3 used a controlled deterministic final diagnosis after the live provider continued to fail closed on invalid evidence references. Exactly build_read_only_reconciliation_plan was allowlisted for the one-shot process; recovery attempt 1 reached verified, recorded plan_recorded true and business_action_executed false, and the diagnosis Telegram report was delivered."
  phase_6_action_canary_integrity: "Source management batch 22 remained historical recovery_required with unchanged updated_at 2026-07-20 15:17:48.203599; no context claim, management work, or position mutation was in flight; sidecar stayed disabled/inactive and no action flags were persisted."
  phase_6_post_canary: "service active HTTP 200 at 5e96b55; latest raw message 8318 completed recognition; focused deployed suite reported 59 passed; seven-case offline gate kept all nine metrics at 1.0; no-notify production monitor completed with monitor_error null and only the known audit_abnormal baseline"
  phase_6_final_correction_deployment: "commit 731318b deployed after a fresh safe-window check with zero evidence/context/management/position-mutation/recovery work in flight; the main service returned HTTP 200 and the sidecar remained disabled/inactive with Agent, shadow, action, and action-allowlist settings empty/off"
  phase_6_live_mimo_closed_final: "one isolated temporary-database notification-delivery incident used the deployed dedicated MiMo provider and prompt v7; it completed diagnosed on its first durable attempt after three bounded read-only queries, stored only the actual incident:1 evidence reference, and nominated fetch_missing_telegram_evidence while empty shadow/action allowlists refused all authority"
  phase_6_final_correction_postdeploy: "70 deployed focused tests passed; the seven-case offline gate kept all nine metrics at 1.0; production runtime incident count remained 3; service active HTTP 200; latest raw message 8341 completed recognition; no evidence/context/management/position-mutation/recovery work remained in flight; no-notify monitor had monitor_error null with only the known audit_abnormal baseline"
  remaining: "Keep all Agent/action flags empty/off. The live MiMo closed-final gate is now proven. Every remaining playbook still requires its own reviewed handler, positive verification proof, safe window, and canary before any enablement; refresh_read_only_exchange_snapshot is the next candidate."
enabled_flags:
  - "capture:management_partial_failed"
  - "telegram:deterministic-runtime-incident-reports"
known_issues:
  - "The pre-existing production safety baseline remains `audit_abnormal` (32 blocked, 1 partial_failed, 5 recovery_required in the latest bounded audit); Phase 5 did not alter those historical rows."
  - "The notification-enabled monitor service also reports missing notification configuration; Phase 1 did not alter monitor configuration."
  - "Phase 6 production wiring currently implements only the read-only reconciliation-plan handler. Refresh, audit, claim recovery, AI-job reschedule, and Telegram-evidence playbooks fail closed as executor_not_configured until separately reviewed handlers exist."
  - "Historical MiMo v4/v6 attempts failed closed on text-form tool output and fabricated evidence. Prompt v7 now passed an isolated live closed-final validation with only actually gathered evidence, while production incident 2 remains preserved as escalated for audit history."
  - "A full-suite attempt reached 2237 passed and 1 skipped before a pre-existing 0.2-second lifespan timeout failed under aggregate load; the exact test passed in isolation."
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

- Status: completed
- Local implementation: closed diagnosis/tool contracts, eight bounded
  read-only projections, OpenAI-compatible tool loop, durable retry budget,
  reproducible Codex handoff, diagnosis Telegram report, and a separately
  supervised sidecar that remains disabled by default
- Provider compatibility hardening: serialized ignored parallel calls, reserved
  a forced closed final-diagnosis turn, versioned the exact prompt used, and
  validated the handoff before committing diagnosis
- Review: all Critical and Important findings resolved, including server-canary
  findings
- Local verification: 127 post-canary focused tests and 176
  context-resolution/management regressions passed
- Full-suite note: one pre-existing timing-sensitive Web lifespan test exceeded
  its 0.2-second deadline under aggregate load after 2237 passes and 1 skip;
  the exact test passed in isolation
- Deployment: `d5c5b70`; main service active with HTTP 200, sidecar installed
  disabled/inactive, and Agent flag absent
- Synthetic canary: an isolated temporary database was diagnosed with three
  bounded read-only tool calls and produced a reproducible Codex handoff; all
  source business tables remained empty
- Continuity: the listener continued after deployment; one expired
  pre-restart evidence claim was recovered to a completed recognition, the
  latest recognition completed, and no contextual-resolution or
  position-mutation work remained in flight

### Phase 4 — Expand read-only evidence and build evaluation corpus

- Status: completed
- Commit: `356e844` (`test: expand runtime incident agent evaluations`)
- Local implementation: seven reviewed redacted incident fixtures, deterministic
  offline classification/tool/safety/certainty/budget/context-boundary metrics,
  coherent local/exchange comparisons, bounded related-worker history, bounded
  same-fingerprint attempt history, and an order-ID-free protection summary
- Authority change: none; all evidence remains read-only and the Agent sidecar
  remains dormant by default
- Local verification: runtime-agent and critical context-resolution/management
  regressions passed; all seven evaluation cases passed all six gates
- Review: no remaining Critical or Important findings
- Production verification: deployed with the Agent flag disabled and sidecar
  inactive; service returned HTTP 200; Telegram listener monitored 31 enabled
  groups; evidence/context claims and position mutations were zero; all nine
  read-only projections executed against incident `1`; the runtime incident
  count and source business state were unchanged

### Phase 5 — Versioned recovery playbooks in shadow mode

- Status: completed
- Implementation started: yes
- Local implementation: six versioned catalog playbooks, deterministic
  shadow-only policy, exact dormant allowlist, durable nomination/refusal
  audit, bounded Telegram/Codex reporting, and corpus selection gates
- Action authority change: none; Phase 5 contains no executor
- Review: no remaining Critical or Important findings
- Commits: `549f26f` (`feat: add shadow incident recovery playbooks`) and
  `8ec6542` (`fix: skip monitor database bootstrap`), pushed
- Local verification: final runtime-agent/monitor/safety selection reported
  317 passed and 1 skipped; context-resolution and management regressions
  reported 176 passed; all nine offline metrics scored 1.0 across seven cases
- Review: no remaining Critical or Important findings after verifying that
  incident-ledger writes remain best-effort and the monitor no longer runs
  compatibility migrations in its read-only startup path
- Production deployment: `8ec6542`; bounded restart completed cleanly with the
  Agent sidecar disabled and shadow allowlist empty
- Production verification: service HTTP 200, listener monitoring 31 groups,
  latest recognition complete, zero contextual claims and position mutations,
  current monitor result available with only the historical `audit_abnormal`
  baseline, deployed offline gate all green, and 95 focused tests passed
- Shadow canary: incident `1` nominated only
  `refresh_read_only_exchange_snapshot`; deterministic policy accepted it
  while recording `would_execute: false` and `action_executed: false`; source
  management batch `28` was unchanged

## Completed Phase 5 Exit Checklist

Phase 5 is not complete until:

- [x] all six playbooks declare versioned metadata, prerequisites, refusal
      reasons, side-effect class, idempotency, limits, and verification;
- [x] the prompt may nominate only closed catalog names;
- [x] deterministic policy independently accepts or refuses every nomination;
- [x] the durable ledger rejects execute mode, `would_execute: true`, and
      `action_executed: true`;
- [x] Telegram reports and Codex handoffs expose the shadow result and never
      contradict an invalid execution record;
- [x] the reviewed corpus has zero accepted unknown or unsafe actions and every
      decision records `action_executed: false`;
- [x] operational nominations require exact durable non-writing proof;
- [x] shadow playbooks are exact-allowlisted and dormant by default;
- [x] runtime-agent, architecture, notification, context-resolution, and
      management regressions pass locally;
- [x] changes receive review with no remaining Critical or Important findings;
- [x] changes are committed and pushed;
- [x] a safe production deployment window is proven;
- [x] production deploys with the Agent sidecar and shadow allowlist disabled;
- [x] service/listener/checkpoint/reconciliation continuity is verified;
- [x] the deployed checkout passes the offline corpus and focused Phase 5 gates;
- [x] exactly one read-only shadow playbook is canaried;
- [x] the canary records only nominations and policy results, with no business
      row mutation and no `action_executed: true`.

### Phase 6 — Low-risk automatic recovery

- Status: in_progress
- Local implementation: additive durable recovery-attempt ledger; exact
  execution policy; current fingerprint, claim token, lease, idempotency,
  attempt-budget, and one-active-action gates; per-incident freeze; global
  circuit breaker; playbook-specific fail-closed verification; bounded
  Telegram/Codex action evidence; and dormant action flags
- Production handler scope: only
  `build_read_only_reconciliation_plan` is wired, and it records a bounded
  read-only plan from durable last-observed state; all other catalog actions
  refuse execution without an explicitly injected reviewed handler
- Provider isolation implementation: dedicated
  `TELEGRAM_KOL_RUNTIME_AGENT_LLM_*` configuration with no fallback to shared
  credentials; direct MiMo `mimo-v2.5`; token accounting remains console-only
  and no usage ledger is added
- Authority change in production: none; all Phase 6 action flags remain off
- Review: all Critical and Important findings were fixed; final review found
  no remaining Critical or Important defects
- Dedicated provider review: no remaining Critical or Important defects;
  root-owned `0600` secrets load through systemd environment injection,
  shared credentials never act as fallback, and both Agent commands refuse
  invalid configuration before claiming an incident
- Commit: `1fe6829` (`feat: add dormant incident recovery boundary`), pushed
- Deployment: `1fe6829`, deployed after a fresh safe-window gate with all
  agent/action flags and allowlists disabled
- Production verification: 91 focused tests and the seven-case offline gate
  passed; service HTTP 200; recovery-attempt ledger empty; listener recognition
  continuity preserved through an expired pre-restart evidence claim; no
  management or position mutation was in flight; monitor retained only the
  known `audit_abnormal` baseline
- Provider verification: dedicated MiMo `mimo-v2.5` configuration is installed
  root-owned mode `0600`; a non-business forced-tool probe returned HTTP 200
  with the required response shape; no provider response or usage was
  persisted
- Provider compatibility hardening: `51b0fad` switched the final no-tool turn
  from a forced function call to validated JSON-object output after MiMo
  returned text-form tool markup; `5e96b55` published the existing closed
  output bounds in prompt v6 after MiMo produced an oversized hypothesis
- Canary: incident `3` passed a controlled deterministic one-shot canary with
  exactly `build_read_only_reconciliation_plan` allowlisted; recovery attempt
  `1` was verified, its recorded plan had `business_action_executed: false`,
  source management batch `22` was unchanged, and the Telegram diagnosis
  report was delivered
- End-to-end model status: prompt v7 completed one isolated live MiMo diagnosis
  with three bounded read-only queries and only the actually gathered
  `incident:1` reference. Empty shadow/action allowlists refused authority;
  Phase 6 remains `in_progress` for the unimplemented playbook handlers and
  persistent Agent/action flags remain off
- Final-correction hardening is locally complete in prompt v7: one malformed
  or contract-invalid final may receive one bounded correction turn with no
  tools and only actually gathered evidence references. A second invalid
  response, a corrected tool request, or an exhausted wall budget fails closed
  before recovery policy or action. Local review found no Critical, Important,
  or Minor defects. Commit `731318b` is deployed and the live MiMo closed-final
  validation passed without changing the three production incident rows.
- The next Phase 6 handler,
  `refresh_read_only_exchange_snapshot`, is locally complete and reviewed with
  no Critical, Important, or Minor findings. The sidecar receives no Deepcoin
  credential: a loopback-only, proxy-refusing main-service endpoint returns a
  bounded redacted state fingerprint, and the one-shot handler plus independent
  verification consume exactly two complete coherent reads. Deployment and
  canary remain pending.

## Current Phase 6 Exit Checklist

- [x] action authority and exact allowlist default off;
- [x] durable idempotency and one-active-action reservation exist;
- [x] current fingerprint, live claim token, and lease are checked before and
      after the action;
- [x] active duplicates defer, stale unknown outcomes freeze, and contention
      remains retryable;
- [x] unexpected exceptions, verification mismatch, and repeated failures
      freeze or open the circuit;
- [x] no order, position, protection, strategy, recognition, contextual
      resolution, or unknown-write mutation is reachable;
- [x] reports include action, exact verification status, and bounded evidence;
- [x] local focused, offline, context, management, listener, and monitor gates
      pass;
- [x] final review has no remaining Critical or Important findings;
- [x] changes are committed and pushed;
- [x] a safe production deployment window is proven;
- [x] production deploys with sidecar and every action flag disabled;
- [x] service/listener/checkpoint/reconciliation continuity is verified;
- [x] the reviewed production model provider is available;
- [x] the live provider completes a closed final using only gathered evidence;
- [x] exactly one production handler passes a reversible canary;
- [ ] each remaining playbook receives its own handler, verification proof,
      safe window, and canary before enablement.
