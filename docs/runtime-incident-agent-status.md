# Runtime Incident AI Agent Status

This is the canonical cross-conversation checkpoint. Chat history must not be
used to advance or reinterpret the rollout.

```yaml
project: runtime-incident-agent
design_version: 1
current_phase: 8R.2
phase_name: technical-incident-capture
phase_status: in_progress
last_completed_phase: "8R.1"
last_completed_commit: 2449382
production_commit: 2449382bd53195950b42ec6052fb4e6dee4fb9cd
local_tests:
  - "phase-8r.2-monitor-policy-contract: 314 passed, 1 Linux-only installation probe skipped"
  - "phase-8r.2-final-source-parity-and-gating-regression: 573 passed"
  - "phase-8r.2-capture-notification-agent-focused: 252 passed"
  - "phase-8r.2-source-adapter-and-production-path-focused: 496 passed"
  - "phase-8r.2-runtime-agent-and-notification-focused: 154 passed"
  - "phase-8r.2-plan replacement: tests/test_protection_health.py does not exist; protection coverage ran through tests/test_protection_ledger.py and tests/test_execution_bindings.py"
  - "phase-8r.1-monitor-observability-focused: 192 passed, 1 Linux-only systemd sandbox probe skipped"
  - "phase-8r.1-critical-runtime-regressions: 36 passed, 2 known sqlite deprecation warnings"
  - "phase-8r.1-review: no remaining Critical or Important findings; the final root-portable CLI regression test passed after the sole Minor review note was addressed"
  - "phase-8r-roadmap-boundary: 10 passed"
  - "phase-6-non-writing-ai-job-source-review-baseline: 115 passed"
  - "phase-6-non-writing-ai-job-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-telegram-evidence-runtime-web-focused: 355 passed, 2 sqlite deprecation warnings"
  - "phase-6-telegram-evidence-context-management-regressions: 417 passed"
  - "phase-6-telegram-evidence-listener-monitor-mutation-regressions: 136 passed, 2 known deprecation warnings"
  - "phase-6-telegram-evidence-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-stale-claim-rejected-runtime-baseline: 219 passed"
  - "phase-6-stale-claim-rejected-context-policy-baseline: 25 passed"
  - "phase-6-production-audit-runtime-web-focused: 333 passed"
  - "phase-6-production-audit-context-management-regressions: 396 passed"
  - "phase-6-production-audit-listener-monitor-mutation-regressions: 140 passed, 2 known deprecation warnings"
  - "phase-6-production-audit-offline-evaluation: 7 cases, all nine metrics at 1.0"
  - "phase-6-read-only-exchange-refresh-timeout-fix-runtime-web-focused: 262 passed"
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
  status: phase-8r.1-monitor-repair-complete
  deployed_commit: 2449382bd53195950b42ec6052fb4e6dee4fb9cd
  service: active-http-200
  bounded_restarts: "service restarted without SIGKILL; raw message 8309 crossed the restart with a live pre-restart evidence lease, logged one already-in-progress recovery error, then completed through normal lease expiry recovery"
  listener: monitoring-31-enabled-groups-and-continuing
  recognition: "latest raw message 8360 completed as non-strategy; diagnosis-sidecar activation did not restart or alter the main recognition service"
  contextual_resolution_inflight: 0
  position_mutation_inflight: 0
  management_latest: "succeeded; six old partial_failed/recovery_required rows were historical, with no active claim or mutation"
  production_safety: "current diagnostic completed with monitor_error null and only the unchanged audit_abnormal baseline"
  sidecar: installed-enabled-active
  agent_flag: enabled
  production_incident_row: "the production ledger has 3 incidents: 2 diagnosed and 1 escalated; none is claimable or actively claimed"
  phase_4_offline_gate: "7 reviewed cases; all six metrics at 1.0"
  phase_4_readonly_tools: "all nine bounded tools executed against incident 1; only projection keys and evidence counts were inspected"
  incident_agent_behavior: "the enabled sidecar is idle because no incident is claimable. On a future incident it may run bounded diagnosis tools and write only the incident diagnosis/notification ledger; empty shadow/action allowlists and disabled action authority prevent playbook or business mutation"
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
  phase_6_exchange_refresh_deployment: "commits 06a188a and 0b2aecc deployed through two separately proven safe windows; service active HTTP 200, sidecar disabled/inactive, all Agent/shadow/action flags empty/off, and the loopback endpoint returned one complete bounded snapshot while refusing a proxy-forwarded request with HTTP 404"
  phase_6_exchange_refresh_canary: "the first isolated temporary-database attempt failed closed because the five-second internal HTTP timeout was shorter than one observed provider response; no production ledger or business row changed. Commit 0b2aecc raised the bounded timeout to 20 seconds, after which one fresh isolated canary executed exactly two coherent read-only account snapshots and reached verified/action_verified with incident and exchange-snapshot evidence only"
  phase_6_exchange_refresh_postdeploy: "262 deployed focused tests passed; the seven-case offline gate kept all nine metrics at 1.0; production runtime incident count remained 3; latest raw message 8353 completed recognition; latest management batch 80 remained succeeded; no evidence/context/management/position-mutation/recovery work was in flight. The no-notify monitor had one transient adapter_failure and returned to monitor_error null with only the known audit_abnormal baseline on the bounded retry"
  phase_6_production_audit_deployment: "commit 25e8336 deployed after a fresh zero-in-flight safe-window check; service active HTTP 200, sidecar disabled/inactive, and every Agent/shadow/action flag and allowlist remained empty/off"
  phase_6_production_audit_boundary: "the root main service alone runs the fixed read-only audit command with a 20-second timeout, 1 MiB combined-output ceiling, dedicated automatically removed scratch root, and process-wide single-flight lock. The loopback endpoint refused X-Forwarded-For with HTTP 404; simultaneous requests returned one HTTP 200 and one immediate HTTP 409; a later request returned HTTP 200; scratch residue stayed zero. The unprivileged telegram-kol-agent identity consumed the bounded endpoint without database ownership or credential expansion"
  phase_6_production_audit_canary: "one isolated temporary-database management_partial_failed incident enabled exactly rerun_production_audit in-process; the deployed handler completed one production read-only audit and reached verified/action_verified with only incident:1 and audit-run:1 evidence. The production ledger remained at 3 incidents and 1 earlier recovery attempt; latest management batch 80 remained succeeded with unchanged updated_at 2026-07-29 06:39:13.358810"
  phase_6_production_audit_postdeploy: "333 deployed focused tests passed; the seven-case offline gate kept all nine metrics at 1.0; latest raw message 8354 completed as non-strategy; evidence/context/management/position-mutation/recovery work in flight was zero. The no-notify monitor returned monitor_error null with only the known audit_abnormal baseline. Before deployment it returned two transient adapter_failure results, then recovered to the same known baseline on the third bounded attempt"
  phase_6_stale_claim_review: "the proposed context-resolution handler was rejected before deployment. The authoritative adapter records context_worker_exhausted only after the source attempt becomes exhausted and its claim is cleared, while the proposed handler and policy required an unreachable stale-running row plus summary fields the adapter never emits. Adding a parallel stale-claim path would duplicate the authoritative context worker's existing compare-and-set reclaim. The synthetic implementation was removed and no production restart occurred"
  phase_6_stale_claim_no_deploy_continuity: "production remained at 25e8336 with the main service active HTTP 200 and sidecar inactive. Runtime incident count stayed 3; context, management, position-mutation, and recovery work in flight were zero; management batch 80 remained succeeded. Latest raw message 8357 had a recognition-failed result on the unchanged pre-existing production code after 8356 completed as non-strategy, so no deployment window was claimed and no restart was attempted"
  phase_6_telegram_evidence_deployment: "commit e3e784a deployed after raw messages 8358, 8359, and 8360 completed recognition following the unchanged-code failure at 8357, with zero evidence/context/management/position-mutation/recovery work in flight. The service restarted cleanly, returned HTTP 200, the sidecar stayed inactive, and every Agent/shadow/action flag and allowlist remained empty/off"
  phase_6_telegram_evidence_boundary: "the main service alone owns bot credentials and runs concurrent read-only getMe/getChat calls behind a true five-second wall-clock deadline, loopback/proxy refusal, and process-wide single-flight. The unprivileged sidecar received only four fixed booleans. X-Forwarded-For returned HTTP 404; simultaneous system-operator probes returned HTTP 200/409 and a later request returned HTTP 200. The notification-bot channel failed closed with HTTP 503 because its production config is absent"
  phase_6_telegram_evidence_canary: "one isolated temporary-database runtime_incident_notification failure enabled exactly fetch_missing_telegram_evidence in-process. The deployed system-operator probe reached verified/action_verified with only incident:2 and telegram-evidence:2 references; the synthetic failed source remained failed. Production stayed at 3 incidents, 1 historical recovery attempt, 70 strategy notifications with unchanged latest updated_at, and management batch 80 remained succeeded"
  phase_6_telegram_evidence_postdeploy: "221 deployed focused tests passed; the seven-case offline gate kept all nine metrics at 1.0; the listener reported monitoring 31 groups with raw message 8360 completed as non-strategy. Evidence/context/management/position-mutation/recovery work in flight was zero. The no-notify monitor returned monitor_error null with only the known audit_abnormal baseline"
  phase_6_non_writing_ai_job_review: "reschedule_non_writing_ai_job was rejected before implementation. context_worker_exhausted would re-enter the authoritative contextual path with the live auto-trade executor, while semantic_review provider exhaustion can be rescheduled only by mutating RecognitionDecision. Neither production adapter emits business_write_owned false, and get_worker_state has no semantic-review resolver. Production read-only inspection found zero related runtime incidents, zero exhausted context attempts, and two historical terminal semantic-review failures with three attempts, no claim, and no schedule. No runtime change, deployment, or restart occurred"
  phase_6_completion: "Phase 6 is complete with four reviewed deployed handlers and two documented fail-closed rejections. At completion every Agent/shadow/action flag and allowlist was empty/off. Optional Phase 7 is blocked because its separately required explicit user approval has not been given"
  phase_6_diagnosis_activation: "after explicit user instruction, the already deployed read-only diagnosis sidecar was enabled persistently without restarting the main service. Preflight proved a complete stable audit, only the known audit_abnormal baseline, no evidence/context/management/position-mutation/recovery work in flight, no claimable runtime incidents, a complete dedicated provider configuration, and empty shadow/action allowlists. The sidecar runs as telegram-kol-agent, is enabled/active, and repeatedly reports idle; the main service remains active HTTP 200. TELEGRAM_KOL_RUNTIME_AGENT_ENABLED is true while shadow/action allowlists remain empty and action authority remains false"
  phase_8r_1_deployment: "A fresh safe-window gate proved the latest raw message 8994 had completed recognition, with zero recognition claims, context work, management work, position mutations, recent execution events, runtime claims, or recovery attempts. Commit 2449382 was deployed with a bounded main-service restart; the service returned HTTP 200 and the Runtime Agent resumed active/idle."
  phase_8r_1_monitor_staging: "During staged deployment the monitor timer remained disabled and inactive. The installer preserved the existing state bytes, converged state.json to telegram-kol-monitor:telegram-kol-monitor mode 0600, installed the reviewed system-operator-only environment, and left the timer off until the separately approved activation. Obsolete notification-bot fields were removed from the monitor-only credential file after a root-owned 0600 backup was created."
  phase_8r_1_diagnostic: "The deployed no-notify diagnostic reached the monitor and returned monitor_error null with only the known audit_abnormal result. The deployed process loads a complete system-operator bot configuration from the service environment without reading checkout configuration."
  phase_8r_1_deployed_tests: "193 focused tests passed; 36 critical runtime regressions passed with 10 deprecation warnings. Main service and Runtime Agent are active; Agent action authority is false and both playbook allowlists are empty."
  phase_8r_1_activation: "A fresh activation gate again proved zero recognition, context, management, position-mutation, runtime-claim, or recovery work in flight. Exactly one reviewed monitor test-notification invocation returned sent. The timer is enabled/active with its next run scheduled; the first bounded notification-enabled run returned monitor_error null and notification_status sent for the known audit_abnormal baseline, with no notification_config_missing."
  phase_8r_1_continuity: "Main service and Runtime Agent remain active; raw message 8995 completed recognition; the Agent is idle, action authority is false, and both playbook allowlists are empty. The monitor state remains readable by its dedicated identity at mode 0600."
  remaining: "Phase 8R.2 local capture-only implementation is in progress. Before widening production capture, deploy the reviewed code dormant, atomically pin Telegram and Agent selectors to the currently approved legacy types, then enable and compare each additional capture type without Telegram or Agent claims. Do not implement the invariant scanner or any later Phase 8R stage."
enabled_flags:
  - "capture:management_partial_failed"
  - "telegram:deterministic-runtime-incident-reports"
  - "runtime-agent:read-only-diagnosis"
  - "monitor:independent-system-operator-alerting"
known_issues:
  - "The pre-existing production safety baseline remains `audit_abnormal` (32 blocked, 1 partial_failed, 5 recovery_required in the latest bounded audit); Phase 5 did not alter those historical rows."
  - "The production notification-bot configuration is absent, so the new read-only Telegram evidence endpoint returns HTTP 503 for strategy-management notification sources. Runtime-incident system-operator evidence is verified; the unsupported production channel fails closed and no credential was added in Phase 6."
  - "Phase 6 production wiring implements the read-only reconciliation-plan, exchange-snapshot-refresh, production-audit, and Telegram-evidence handlers. Claim recovery and AI-job reschedule remain executor_not_configured by reviewed design: the former duplicated the authoritative stale-claim path, while the latter had no source satisfying both non-writing proof and the recognition/contextual-resolution boundary."
  - "Historical MiMo v4/v6 attempts failed closed on text-form tool output and fabricated evidence. Prompt v7 now passed an isolated live closed-final validation with only actually gathered evidence, while production incident 2 remains preserved as escalated for audit history."
  - "A full-suite attempt reached 2237 passed and 1 skipped before a pre-existing 0.2-second lifespan timeout failed under aggregate load; the exact test passed in isolation."
phase_7_explicitly_approved: false
phase_7_disposition: deferred_non_blocking
phase_8r_roadmap_control:
  design_commit: 15c9d7a
  implementation_plan_commit: 3bd2ef1
  task_0_commit: 5323a47
  task_0_status: completed
  runtime_change: none
  production_restart: not_required
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

- Status: completed
- Local implementation: additive durable recovery-attempt ledger; exact
  execution policy; current fingerprint, claim token, lease, idempotency,
  attempt-budget, and one-active-action gates; per-incident freeze; global
  circuit breaker; playbook-specific fail-closed verification; bounded
  Telegram/Codex action evidence; and dormant action flags
- Production handler scope: `build_read_only_reconciliation_plan`,
  `refresh_read_only_exchange_snapshot`, `rerun_production_audit`, and
  `fetch_missing_telegram_evidence` are
  wired. They respectively record a bounded non-writing plan, refresh and
  compare coherent read-only account state, and run a bounded read-only
  production audit, or fetch fixed read-only bot/chat availability evidence.
  All other catalog actions refuse execution without an explicitly injected
  reviewed handler
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
  at that checkpoint Phase 6 remained `in_progress` for unreviewed playbook
  handlers. The later handler reviews and rejections completed the phase;
  at that checkpoint the persistent Agent/action flags remained off
- Final-correction hardening is locally complete in prompt v7: one malformed
  or contract-invalid final may receive one bounded correction turn with no
  tools and only actually gathered evidence references. A second invalid
  response, a corrected tool request, or an exhausted wall budget fails closed
  before recovery policy or action. Local review found no Critical, Important,
  or Minor defects. Commit `731318b` is deployed and the live MiMo closed-final
  validation passed without changing the three production incident rows.
- The second Phase 6 handler, `refresh_read_only_exchange_snapshot`, is
  deployed at `0b2aecc` and reviewed with no Critical, Important, or Minor
  findings. The sidecar receives no Deepcoin credential: a loopback-only,
  proxy-refusing main-service endpoint returns a bounded redacted state
  fingerprint, and the one-shot handler plus independent verification consume
  exactly two complete coherent reads. The first isolated canary failed closed
  on the original five-second internal HTTP timeout; after a bounded
  twenty-second timeout fix, a fresh isolated canary reached
  `action_verified`. Production incident and business rows remained unchanged.
- The third Phase 6 handler, `rerun_production_audit`, is deployed at
  `25e8336` and reviewed with no Critical, Important, or Minor findings. The
  root main service runs one fixed, bounded audit subprocess behind a
  loopback-only, proxy-refusing, single-flight endpoint; the unprivileged
  sidecar receives only the fixed proof projection. A temporary-database
  one-shot canary reached `action_verified` with `incident` and `audit-run`
  evidence only. Production incident, recovery-attempt, and management rows
  were unchanged.
- The fourth Phase 6 handler, `fetch_missing_telegram_evidence`, is deployed at
  `e3e784a` and reviewed with no Critical, Important, or Minor findings. The
  root main service keeps both bot credentials behind a loopback-only,
  proxy-refusing, single-flight endpoint and returns four booleans after a
  hard five-second read-only `getMe`/`getChat` probe. The unprivileged sidecar
  validates a real failed source and consumes one in-memory proof. An isolated
  runtime-notification canary reached `action_verified`; its source remained
  failed and production rows were unchanged. The absent notification-bot
  configuration causes that exact channel to fail closed with HTTP 503.
- `reschedule_non_writing_ai_job` was rejected before implementation. The
  contextual source can re-enter the live auto-trade path, while the only
  provider source is a failed semantic review that would require a prohibited
  `RecognitionDecision` mutation. Neither adapter proves
  `business_write_owned: false`. Review found no Critical, Important, or Minor
  source-analysis defect after the canonical status correction.
- Phase 6 is complete with four deployed positive canaries and two documented
  fail-closed candidate rejections. Production remains at `e3e784a`; the
  completion review is documentation-only and required no service restart.
- After Phase 6 completion, the user explicitly requested activation of the
  existing diagnosis worker. The sidecar is now enabled and active under the
  dedicated unprivileged identity. It had no claimable incident and remained
  idle during verification. Shadow/action allowlists are empty and action
  authority is still disabled, so activation adds diagnosis availability but
  no recovery or business-mutation authority.

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
- [x] all four implemented production handlers pass isolated reversible
      canaries;
- [x] each catalog playbook has either a reviewed handler with verification and
      an isolated canary, or a documented fail-closed source rejection.

### Phase 7 — Optional Bounded Business Recovery

- Status: deferred, unauthorized, and non-blocking
- Explicit approval recorded: no
- Runtime or production change: none
- Disposition: the user approved continuing read-only Agent improvements while
  leaving every bounded business-recovery action out of scope. Phase 7 no
  longer blocks those read-only phases.
- Permanent gate: the ordinary implementation trigger and every Phase 8R task
  grant no business-mutation authority. Phase 7 still requires a separate,
  fresh approval that explicitly accepts its business-mutation scope.

### Phase 8R — Proactive Read-Only Incident Detection

- Status: in progress
- Roadmap-control Task 0: completed locally with 10 focused tests passing;
  documentation and test changes only, so no production restart was required
- Current task: `8R.2 technical-incident-capture`
- Approved scope: deterministic proactive discovery, bounded read-only
  diagnosis, Telegram notification, Codex handoff, and read-only verification
- Prohibited scope: order, position, protection, strategy, recognition,
  contextual-resolution, source-business-row, service-control, and deployment
  mutations by the Agent
- Continuity requirement: every runtime task ships dormant or shadow-only,
  changes at most one runtime stage per user turn, and deploys only in a proven
  safe window with immediate independent rollback
- Authority state: Phase 6 diagnosis may remain active; shadow and action
  allowlists remain empty; action authority remains false
- Task 8R.1: completed at deployed commit `2449382`; the independent monitor
  timer is enabled, its dedicated state is readable, the one reviewed test
  notification was delivered, and the first notified run had no configuration
  error
- Next action: start only Task 8R.2 in capture-only mode; new incident-type
  Telegram delivery remains disabled during that first deployment
