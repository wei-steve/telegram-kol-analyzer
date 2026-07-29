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
