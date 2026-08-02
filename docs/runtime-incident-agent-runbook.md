# Runtime Incident AI Agent Rollout Runbook

## Purpose

Provide a fixed per-conversation and per-deployment procedure that prevents the
runtime incident agent rollout from interrupting normal Telegram intake,
strategy handling, management, exits, or reconciliation.

## Start of Every Implementation Conversation

1. Read `AGENTS.md`.
2. Read:
   - `docs/plans/2026-07-28-runtime-incident-agent-design.md`
   - `docs/plans/2026-07-28-runtime-incident-agent.md`
   - `docs/runtime-incident-agent-status.md`
   - this runbook
3. Run:

```bash
git status --short
git branch --show-current
git log -5 --oneline --decorate
```

4. Confirm the branch is `codex/deepcoin-auto-trading-v1`.
5. Preserve all unrelated worktree changes.
6. Set `phase_status: in_progress` when implementation begins.
7. Implement or resume only `current_phase`.

## Development Rules

- Add new behavior dormant by default.
- Prefer additive database changes.
- Do not make the normal listener or trading path wait for incident recording,
  the agent provider, Telegram notification, or recovery execution.
- Write focused failing tests before runtime code.
- Run contextual resolution and management regressions even though the agent
  must not own those flows.
- Never use live trading as a test fixture.

## Pre-Deployment Continuity Gate

Do not deploy merely because local work is finished.

Before any runtime restart:

1. Confirm the pushed commit is reviewed and matches the intended phase.
2. Confirm all new agent-related feature flags default off.
3. Use existing read-only production tools to check:
   - service and listener health;
   - latest Telegram checkpoint/freshness;
   - reconcile backlog;
   - active recognition ownership;
   - active strategy-management batches;
   - `submit_unknown`, `partial_failed`, and `recovery_required`;
   - protection incidents;
   - current production safety monitor result.
4. If a time-sensitive recognition, entry, management action, exit, or
   reconciliation is in flight, defer deployment.
5. If the read-only snapshot is incomplete, defer deployment.
6. Record the deferral in the status file and leave the phase `in_progress`.

The requirement is a proven safe window, not a guessed quiet period.

## Deployment

Use the project helper after push:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Documentation-only phases do not pull or restart production.

For runtime phases, the helper must pull the reviewed branch, reinstall the
editable package, and restart `telegram-kol.service`. Do not enable a new agent
flag in the same step as first deploying its code.

## Immediate Post-Deployment Verification

With all new flags off:

1. Confirm the server commit.
2. Confirm `telegram-kol.service` is active.
3. Confirm the listener resumes and the Telegram checkpoint advances.
4. Run or inspect reconciliation to prove no message gap.
5. Confirm existing first-pass recognition still runs.
6. Confirm contextual strategy resolution behavior is unchanged.
7. Confirm management/reconciliation workers are healthy.
8. Run the production safety monitor.
9. Confirm no unexpected incident-agent worker or notification is active.

Only then enable the phase's canary flag.

## Canary Rules

- Enable one incident type or playbook at a time.
- Start with capture, then deterministic notification, then read-only Agent,
  then shadow playbook, then low-risk action.
- Compare source records with incident records before widening scope.
- Any duplicate, unexpected business mutation, verification mismatch, or
  notification storm triggers immediate disablement.
- Disabling the incident agent must not disable normal production components.

### Phase 2 capture and notification flags

Phase 2 uses two independent, dormant-by-default settings:

- `TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES`: comma-separated exact
  incident types. Empty disables capture. Wildcards are not supported.
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED`: only `1`, `true`, `yes`,
  or `on` enables deterministic incident delivery. Empty or any other value
  disables delivery.
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`: optional comma-separated
  exact incident-type delivery allowlist. When absent, legacy global delivery
  behavior is preserved. When present but empty, no runtime incident is
  claimed for Telegram delivery.
- `TELEGRAM_KOL_RUNTIME_AGENT_TYPES`: optional comma-separated exact
  incident-type diagnosis allowlist. When absent, legacy Agent behavior is
  preserved. When present but empty, the Agent remains supervised but claims
  no runtime incident for AI diagnosis.

The first canary must set exactly one capture type and leave Telegram delivery
off. Compare source rows with `runtime_incidents` fingerprints and repeat
counts before adding another type. Telegram delivery may be enabled only after
the capture comparison is correct. Immediate Phase 2 rollback is to clear both
settings and restart only in a newly proven safe window.

Telegram delivery has at-least-once crash semantics because Telegram does not
accept an idempotency key. A crash after Telegram accepts a report but before
the local `delivered` commit may repeat it after lease expiry. The fixed
`事件ID` is the operator deduplication marker; committed successes are never
reclaimed.

### Phase 3 read-only Agent sidecar

Phase 3 adds a separately supervised worker and keeps it dormant by default:

- `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`: only `1`, `true`, `yes`, or `on`
  enables diagnosis. Empty or any other value leaves the worker disabled.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_STEPS`: bounded to 1–4.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_WALL_SECONDS`: bounded to 5–120 seconds.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_PROMPT_BYTES`: bounded to 4096–32768 bytes.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_OUTPUT_BYTES`: bounded to 512–32768
  bytes.
- `TELEGRAM_KOL_RUNTIME_AGENT_CLAIM_LEASE_SECONDS`: bounded to 5–3600 seconds.

The reviewed unit is
`deploy/systemd/telegram-kol-runtime-agent.service`. Install it only with:

```bash
sudo /opt/telegram-kol-analyzer/scripts/install_runtime_agent_sidecar.sh
```

The installer refuses an active or enabled unit and leaves the installed unit
disabled and inactive. Deployment of Phase 3 code and unit installation must
therefore create no Agent process. The worker may be started for a synthetic
canary only after the normal service passes the post-deployment continuity
checks and the environment file explicitly enables the Agent.

Each incident generation permits at most three model attempts. Failures use a
durable 5-second, then 10-second retry schedule; the third failure escalates
without another automatic model call. Tool transcripts, individual tool
outputs, model turns, wall time, and repeated calls are independently bounded.

Immediate Phase 3 rollback is:

1. stop and disable `telegram-kol-runtime-agent.service`;
2. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
3. leave capture and deterministic Phase 2 notification settings unchanged;
4. confirm `telegram-kol.service`, listener, recognition, contextual
   resolution, management, and reconciliation remain active.

### Phase 4 evidence projections and offline evaluation

Phase 4 remains read-only and adds no playbook execution or business-action
authority. Its evidence policy version is `runtime-agent-tools-v2`.

The reviewed corpus is stored under
`tests/fixtures/runtime_incidents/`. It contains bounded, redacted cases for
provider retry exhaustion, contextual-worker technical exhaustion,
`submit_unknown`, `partial_failed`, `recovery_required`, severe protection
incidents, and notification delivery failure. `unresolved`, `hold`, strategy
selection, and contextual-resolution replacement are prohibited corpus
outcomes.

Run the deterministic offline gate with:

```bash
PYTHONPATH=src .venv/bin/python -m telegram_kol_research.cli \
  runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents
```

The gate must report `all_passed: true` and full scores for classification,
evidence-tool selection, unsafe-recommendation refusal, supported certainty,
budget compliance, and contextual-targeting refusal.

The Phase 4 projections remain bounded and redacted:

- local versus durable last-observed exchange state reports explicit match,
  mismatch, and unknown counts;
- worker history is limited to ten related records and excludes prompts,
  provider bodies, and raw errors;
- prior same-fingerprint attempts are limited to ten generations;
- protection summaries expose presence/count facts but never order IDs or raw
  evidence payloads.

Server validation must first deploy with the Agent sidecar disabled. Run the
offline gate from the deployed checkout, then execute only the read-only tools
against reviewed incident IDs. Do not start the Agent sidecar or enable action
authority for Phase 4. If a projection is incomplete, records unbounded data,
or disagrees with its durable source row, leave Phase 4 `in_progress` and
disable the Agent flag.

### Phase 5 versioned recovery playbooks in shadow mode

Phase 5 adds the closed `runtime-playbooks-v1` catalog and deterministic
`runtime-shadow-policy-v1`. It adds no executor and authorizes no runtime
action. The model may nominate one catalog playbook; policy independently
records `shadow_accepted`, `shadow_refused`, or `not_requested`. Every
diagnosis report and Codex handoff must continue to state
`action_executed: false`.

`TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS` is a comma-separated exact
allowlist. Empty disables every shadow playbook. Wildcards and unknown names
never pass policy. The first deployment must keep both the Agent sidecar and
this allowlist disabled. After the normal service passes the continuity gate,
enable the Agent only in a separately proven safe window and canary exactly one
read-only playbook:

```text
refresh_read_only_exchange_snapshot
```

The Phase 5 catalog also contains `rerun_production_audit`,
`recover_stale_side_effect_free_claim`, `reschedule_non_writing_ai_job`,
`fetch_missing_telegram_evidence`, and
`build_read_only_reconciliation_plan`. Operational nominations require exact
durable proof that no business write is owned; absent proof is a deterministic
refusal. No Phase 5 canary may execute any of these functions or change a
source business row.

Before widening a shadow allowlist, compare the durable nomination, policy
version, refusal reasons, recovery status, notification text, and Codex
handoff with the reviewed corpus. Require zero accepted unknown playbooks,
zero accepted operational playbooks without exact prerequisite proof, and
zero `action_executed: true` values.

Immediate Phase 5 rollback is:

1. clear `TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS`;
2. stop and disable `telegram-kol-runtime-agent.service`;
3. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
4. leave Phase 2 capture and deterministic notification settings unchanged;
5. confirm the normal service and all business workers remain active.

### Phase 6 low-risk automatic recovery

Phase 6 adds a closed executor, but all action authority remains dormant until
both of these exact flags are enabled:

- `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`: only `1`, `true`, `yes`, or
  `on` enables action policy evaluation.
- `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS`: comma-separated exact
  playbook allowlist. Empty disables every action. Wildcards and unknown names
  are refused.
- `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_CIRCUIT_THRESHOLD`: bounded to 1–5 and
  defaults to 3.

The Runtime Agent provider is isolated from every shared or business-model
credential:

- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL` must be an HTTPS
  OpenAI-compatible endpoint;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY` is required and never falls back to
  `TELEGRAM_KOL_LLM_API_KEY`;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL` is required;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS` is bounded to 5–120
  seconds.

Production uses the direct MiMo endpoint and `mimo-v2.5`. The dedicated key
exists only in `config/runtime_incident_agent.env`, which must be root-owned
mode `0600`; never print, commit, persist, or include it in a handoff. Token
accounting is intentionally performed only in the MiMo console through this
dedicated key. The application does not store usage metadata.

Missing or invalid dedicated provider configuration must fail before an
incident claim or model-attempt increment. It must never fall back to the Web
workbench provider, authoritative recognition credentials, or another model.
The first provider verification must keep the sidecar and all action flags
disabled and use a non-business prompt with no incident, Telegram, strategy,
position, or exchange data. Report only configuration completeness, endpoint
host, model, HTTP status, and response-shape validity.

Enabling a name is not sufficient to execute it. Each playbook also requires a
reviewed, explicitly injected deterministic action handler. A missing handler
is a refusal, never a simulated success. The handler must perform the named
operation and return only a boolean completion signal; bounded read-only
verification independently proves the postcondition. Passive durable
projections cannot verify a live snapshot refresh, audit rerun, evidence
fetch, or recorded reconciliation plan.

Every execution requires the current incident fingerprint, the live worker
claim token and unexpired lease, one durable idempotency reservation, the
playbook attempt budget, and exact post-action evidence. Only one action may be
reserved globally at a time. An active duplicate reports `action_in_progress`;
an expired reservation is treated as an unknown outcome and freezes the
incident without replay. Verification mismatch, unexpected handler exception,
or repeated failures freeze further action and open the bounded circuit.

The first Phase 6 deployment must keep the Agent sidecar, action authority, and
action allowlist disabled. A canary is prohibited until:

1. a reviewed production model provider is available;
2. the exact playbook handler is wired and tested;
3. its verification projection returns playbook-specific positive proof;
4. a new safe window is proven;
5. the action flag and allowlist contain exactly one reversible playbook.

The canary report must show the incident ID, exact playbook, whether the action
executed, exact verification status, bounded evidence references, and remaining
risk. Never canary an order, position, protection, strategy, recognition,
contextual-resolution, or unknown-write mutation.

Immediate Phase 6 rollback is:

1. clear `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS`;
2. clear `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`;
3. stop and disable `telegram-kol-runtime-agent.service`;
4. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
5. leave Phase 2 capture and deterministic notification settings unchanged;
6. confirm the normal service and all business workers remain active.

### Phase 8R proactive read-only incident detection

Phase 7 bounded business recovery is deferred, unauthorized, and non-blocking.
Phase 8R extends only proactive discovery, bounded read-only diagnosis,
Telegram reporting, Codex handoff, and continued read-only verification.

Phase 8R never enables `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`.
Phase 8R never populates either the shadow or action playbook allowlist.
`TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS` and
`TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS` must remain empty throughout all
Phase 8R tasks.

Execute Phase 8R using these separately reviewed runtime stages:

1. repair independent monitoring observability and add health evidence;
2. widen existing technical incident capture in capture-only mode;
3. deploy the invariant scanner dormant, then run it shadow-only;
4. canary one deterministic notification rule at a time;
5. enable bounded AI diagnosis and Codex handoff only for a proven rule;
6. expand the rule catalog and continuous-quality metrics.

Task 8R.2 uses the closed `READ_ONLY_CAPTURE_PROFILE`: provider retry
exhaustion, contextual worker exhaustion, the three terminal management
failure states, severe protection incidents, monitor adapter or incomplete
audit failures, and notification delivery failures. It excludes unresolved or
held business outcomes, ambiguous strategy targeting, and ordinary audit
abnormalities. During capture comparison, every newly enabled type must be
absent from both the Telegram and Agent type allowlists. Three identical source
scans must retain one fingerprint generation, and the authoritative source
row plus its `updated_at` must remain unchanged.

Management and protection sources are repeatable durable scans. Provider,
context-worker, and notification failures are one-shot callbacks emitted only
after their authoritative terminal-state commit; their parity check replays
the same reviewed callback projection three times and verifies that only the
runtime incident repeat counter changes. Monitor failures have no business
source row: parity is three identical monitor evaluations with zero management
or protection rows created or changed.

When Telegram delivery or the Runtime Agent is already enabled, migrate both
selectors before widening capture. In one proven safe window, keep the capture
list unchanged, set `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` and
`TELEGRAM_KOL_RUNTIME_AGENT_TYPES` to the exact currently approved legacy
types, reload the main service and sidecar, and verify that no unapproved type
is claimable. Only then add capture types one at a time while leaving both
selectors unchanged. An absent selector means legacy-all and is not a safe
capture-only production setting.

The independent monitor receives the same exact capture allowlist through its
root-owned `/etc/telegram-kol-monitor.env`, but no Agent/provider setting or
credential. `scripts/install_server_monitor.sh` extracts only
`TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES` from the root-owned runtime
policy file. Therefore each safe-window capture change must regenerate the
monitor environment with the timer stopped, verify the installed allowlist,
then restart/reload the main service and monitor schedule as one staged policy
change. The Telegram and Agent selectors remain confined to their existing
service configuration.

At most one runtime stage may be implemented per user turn. New code and
configuration default off. A first deployment never enables its feature. A
later enablement requires another complete safe-window check and the exact
rule or capture-type allowlist; wildcards are refused.

The proactive scanner must remain outside the listener, recognition,
contextual-resolution, execution, management, exit, protection, and
reconciliation critical paths. Scanner or Agent failure must not block or
change those paths. The scanner may write only additive observation and
incident metadata; it may not write a source business row.

Before every Phase 8R deployment or enablement:

1. prove the reviewed commit and dormant defaults;
2. prove no time-sensitive recognition, execution, management, exit,
   protection, reconciliation, or recovery work is in flight;
3. prove the read-only production snapshot is complete;
4. preserve durable Telegram intake and checkpoint recovery;
5. keep the scanner or new rule disabled during the first deployment;
6. verify the main service, listener, latest recognition, contextual resolver,
   management, protection, reconciliation, and independent monitor immediately
   after deployment;
7. compare source rows before and after any shadow scan and require zero source
   mutation.

If a safe window or complete snapshot cannot be proven, finish local work,
leave the current Phase 8R task `in_progress`, and record the exact server
verification still required. Do not restart or enable anything.

Immediate Phase 8R rollback is layered and independent:

1. disable the newest scanner rule or capture type;
2. disable proactive Telegram delivery while retaining durable evidence;
3. disable AI diagnosis while retaining deterministic incident capture;
4. stop and disable the scanner sidecar without touching the main service;
5. if necessary, stop the Runtime Agent sidecar while leaving normal Telegram
   intake and every business worker active;
6. confirm action authority is false and both playbook allowlists are empty;
7. run the independent monitor and verify message continuity.

## Rollback

1. Disable the newest phase feature flag.
2. Do not delete incident records or rewrite source business rows.
3. Confirm normal listener, recognition, contextual resolution, execution, and
   reconciliation remain active.
4. If a code rollback is required, use a reviewed forward fix or the project's
   safe deployment procedure; do not use destructive Git commands.
5. Run the production safety monitor and reconcile message continuity.
6. Leave the phase `in_progress` and record the exact failure evidence.

## Telegram Reports

During early phases, Telegram reports diagnosis only. Later reports must state:

- incident ID;
- evidence-based impact;
- Agent diagnosis as a hypothesis;
- exact playbook selected;
- whether any action executed;
- verification result;
- remaining risk;
- whether Codex repair is required.

No report may include credentials, raw provider responses, unbounded logs, or
direct personal/payment identifiers.

## Codex Repair Handoff

When an incident requires a code fix, use:

```text
请调查运行异常 incident_id=<ID>。
先读取 AGENTS.md 和该事件的 Codex 交接包。
独立验证 Agent 的诊断，不要把诊断当作事实。
不要扩大交易权限或绕过现有安全网关。
本地修复和测试后，按项目流程推送并在服务器安全窗口验证。
```

## End of Every Implementation Conversation

Before returning control:

1. Record local tests and results.
2. Record commit and push status.
3. Record deployment or the reason deployment was safely deferred.
4. Record server verification.
5. Update phase status and exact remaining work.
6. If complete, advance exactly one phase and set it to `planned`.
7. Preserve the exact next-session prompt:
   `请执行自定义ai agent的下一步实施`.
8. Send the single required project stop notification.
