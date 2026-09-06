# Proactive Read-Only Runtime Incident Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add proactive, deterministic, read-only incident discovery that produces bounded AI diagnoses and Telegram Codex handoffs without changing trading state or interrupting production work.

**Architecture:** Keep the completed Phase 1–6 runtime incident ledger and Agent sidecar. Add an independently supervised, dormant-by-default scanner that confirms versioned invariants from bounded coherent snapshots, records only incident-observation metadata, and feeds confirmed incidents into the existing diagnosis and notification path. Phase 7 business recovery remains deferred, every action flag stays off, and every rollout step must pass the production continuity gate before enablement.

**Tech Stack:** Python 3.13, SQLAlchemy, SQLite, FastAPI loopback read-only projections, httpx/OpenAI-compatible MiMo provider, Typer, systemd, Telegram Bot API, pytest.

---

## Execution Rules

1. Read `AGENTS.md`, the runtime Agent design/status/runbook, and
   `docs/plans/2026-08-02-proactive-readonly-incident-agent-design.md` before
   every implementation turn.
2. Implement at most one rollout task that changes production runtime behavior
   per user turn. Documentation and local tests do not grant permission to
   continue into the next runtime task.
3. Preserve unrelated dirty-worktree files. Stage only the files named by the
   current task.
4. Use TDD. Write and run a focused failing test before implementation.
5. New configuration defaults off. Never enable a feature in the same step that
   first deploys its code.
6. The scanner and Agent may write only their additive observation, incident,
   diagnosis, notification, and verification ledgers. They may not write source
   business rows.
7. Keep `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED=false`, action and shadow
   allowlists empty, and Phase 7 unauthorized throughout this plan.
8. Do not deploy or restart during recognition, execution, management, exit,
   protection, reconciliation, or recovery work in flight.
9. Runtime work remains `in_progress` until server verification succeeds. If a
   safe window cannot be proven, stop after local commit/push and record the
   exact remaining verification.
10. Production tests are read-only. Do not create a Telegram strategy message,
    order, position, or exchange write as a canary.

## Task 0: Decouple Deferred Phase 7 from the Read-Only Roadmap

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Test: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write the failing roadmap-boundary test**

Add a test that loads the canonical status file and asserts:

```python
def test_phase_7_is_deferred_and_phase_8r_requires_no_action_authority():
    status = Path("docs/runtime-incident-agent-status.md").read_text()
    assert "phase_7_explicitly_approved: false" in status
    assert "phase_7_disposition: deferred_non_blocking" in status
    assert "current_phase: 8R.1" in status
    assert "phase_name: monitoring-observability-repair" in status
    assert "phase_status: planned" in status
```

Also assert the runbook states that Phase 8R cannot set an action flag or
populate any action/shadow allowlist.

**Step 2: Run the test and verify it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_architecture_boundary.py \
  -k 'phase_7_is_deferred' -q
```

Expected: FAIL because Phase 7 still blocks the linear roadmap and Phase 8R is
not recorded.

**Step 3: Update the canonical roadmap**

Record:

```yaml
current_phase: 8R.1
phase_name: monitoring-observability-repair
phase_status: planned
last_completed_phase: 6
phase_7_disposition: deferred_non_blocking
phase_7_explicitly_approved: false
```

Preserve the full Phase 1–6 evidence. State explicitly that Phase 8R contains
read-only work only and does not advance, approve, or partially implement
Phase 7.

Add a Phase 8R runbook section with the per-task dormant, shadow, canary,
disable, continuity, and notification rules from the approved design.

**Step 4: Run focused documentation-boundary tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_runtime_incident_phase5_config.py -q
```

Expected: PASS, with action authority still false by default.

**Step 5: Commit**

```bash
git add docs/runtime-incident-agent-status.md \
  docs/runtime-incident-agent-runbook.md \
  tests/test_runtime_agent_architecture_boundary.py
git commit -m "docs: decouple read-only agent roadmap"
```

Production restart: not required.

## Task 1: Repair Independent Monitor Observability

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `scripts/install_server_monitor.sh`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_server_monitor_installation.py`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing notification-identity tests**

Add tests proving the monitor uses the same allowlisted system-operator bot
identity installed by `install_server_monitor.sh`:

```python
def test_monitor_loads_system_operator_bot_from_service_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_KOL_SYSTEM_BOT_TOKEN", "system-token")
    monkeypatch.setenv("TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID", "123")
    config = monitor_module._load_monitor_bot_config()
    assert config.bot_token == "system-token"
    assert config.chat_id == "123"
```

Assert checkout `.env` files are never read and notification-bot-only variables
do not silently satisfy the monitor contract.

**Step 2: Write failing state-file ownership tests**

Extend installer tests to require that an existing monitor state file is
preserved, then assigned to `telegram-kol-monitor:telegram-kol-monitor` with
mode `0600` before the timer can be enabled. Add a CLI regression proving an
invalid state file reaches the monitor's bounded state-integrity handling
instead of failing in Typer path validation before the monitor runs.

**Step 3: Run tests and verify they fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_system_operator_bot.py \
  -k 'monitor and (system_operator or state)' -q
```

Expected: FAIL because the monitor currently loads notification-bot fields and
the installer does not repair an existing state file's ownership.

**Step 4: Implement the minimum repair**

- Change `_load_monitor_bot_config()` to call
  `load_system_operator_bot_config(env_file_paths=[])`.
- Keep the monitor credential allowlist restricted to system-operator bot
  fields.
- Make the installer verify and repair only owner/group/mode on an existing
  state file; do not replace or truncate its content.
- Accept the CLI state path without pre-execution readability rejection, then
  let `_load_monitor_state()` produce the existing `state_invalid` evidence.
- Keep the monitor read-only with respect to the production database.

**Step 5: Run focused and installation tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_system_operator_bot.py \
  tests/test_codex_telegram_notify.py -q
```

Expected: PASS.

**Step 6: Run critical regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_agent_telegram_evidence.py \
  tests/test_web_live_listener_startup.py -q
```

Expected: PASS.

**Step 7: Commit and push**

```bash
git add src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/cli.py \
  scripts/install_server_monitor.sh \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_system_operator_bot.py \
  docs/runtime-incident-agent-status.md
git commit -m "fix: restore independent monitor alerting"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 8: Deploy only in a proven safe window**

Before deployment, run the canonical safe-window checks. If any time-sensitive
work is in flight, stop and leave Task 1 `in_progress`.

Deploy code with no new Agent or scanner flags. Reinstall the monitor only after
the main service passes post-deployment continuity checks. Keep the monitor
timer disabled during install, run the no-notify diagnostic, send exactly one
reviewed monitor test notification, then re-enable the timer.

Verify:

- main service active and HTTP 200;
- listener checkpoint continuity;
- latest recognition completes;
- management, exit, protection, and reconciliation work have no unexpected
  backlog;
- monitor state file is readable by its unprivileged identity;
- a bounded monitor run no longer reports `notification_config_missing`;
- runtime Agent stays active or idle as before with action authority off.

## Task 2: Expand Existing Incident Capture in Capture-Only Mode

**Files:**
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write a reviewed capture-profile test**

Define a named read-only profile containing only existing technical incident
types:

```python
READ_ONLY_CAPTURE_PROFILE = frozenset({
    "provider_retry_exhausted",
    "context_worker_exhausted",
    "management_submit_unknown",
    "management_partial_failed",
    "management_recovery_required",
    "severe_protection_incident",
    "monitor_adapter_failure",
    "monitor_audit_incomplete",
    "notification_delivery_failure",
})
```

Tests must assert that `unresolved`, `hold`, ambiguous strategy targeting, and
ordinary audit abnormalities are excluded.

**Step 2: Write source-to-incident parity tests**

For every type, create one authoritative durable failure and prove three scans
create one fingerprint generation with no source-row mutation. Include monitor
notification-config failure and the previously silent monitor adapter paths.

**Step 3: Run tests and verify the new profile fails**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_adapters.py \
  tests/test_production_safety_monitor.py \
  -k 'capture_profile or source_parity' -q
```

Expected: FAIL until the profile and any missing adapter wiring exist.

**Step 4: Implement only missing capture wiring**

Do not add a new business decision path. Reuse `capture_runtime_incident_best_effort`
and existing source terminal states. Any capture exception must log a bounded
error class and fail open.

Keep Telegram delivery independently disabled for all newly added types during
capture comparison.

**Step 5: Run focused and critical regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_adapters.py \
  tests/test_production_safety_monitor.py \
  tests/test_context_resolution_worker.py \
  tests/test_strategy_management_executor.py \
  tests/test_protection_health.py \
  tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 6: Commit, push, deploy dormant, and compare**

Commit as:

```bash
git commit -m "feat: broaden read-only runtime incident capture"
```

Deploy with the production capture list unchanged. After continuity checks,
enable one new capture type at a time without enabling its Telegram delivery.
For each type compare stable source IDs, incident fingerprints, generations,
repeat counts, and source `updated_at` values. Record zero source-row changes.

Task 2 is complete only after every enabled capture type has source parity and
no duplicate incident generation.

## Task 3: Add the Dormant Observation Ledger and Scanner Contract

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/config.py`
- Create: `src/telegram_kol_research/runtime_incident_observations.py`
- Create: `src/telegram_kol_research/runtime_incident_scanner.py`
- Create: `tests/test_runtime_incident_observations.py`
- Create: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_db_bootstrap.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write failing additive-schema tests**

Add `RuntimeIncidentObservation` with bounded fields for rule ID/version,
fingerprint, object kind/ID, severity, state, consecutive count, first/last
observation, bounded evidence references, evidence fingerprint, confirmed
incident ID, and recovery time.

Tests must prove old databases gain the table additively and no existing table
is rewritten.

**Step 2: Write failing compare-and-set transition tests**

Cover:

- first observation -> `observing`;
- repeated coherent evidence increments the count;
- threshold confirmation creates exactly one runtime incident;
- changed material evidence updates evidence fingerprint without creating a
  duplicate generation;
- normal state before confirmation -> `resolved_without_incident`;
- confirmed recovery -> `resolved`;
- the same fingerprint may reopen only as a new incident generation;
- concurrent scanners have one transition winner.

**Step 3: Write the closed scanner result contract**

Use immutable result objects equivalent to:

```python
@dataclass(frozen=True, slots=True)
class InvariantObservation:
    rule_id: str
    rule_version: str
    object_kind: str
    object_id: str
    severity: str
    outcome: Literal["normal", "abnormal", "evidence_insufficient"]
    evidence_references: tuple[str, ...]
    evidence_fingerprint: str
    summary: Mapping[str, str | int | bool | None]
```

Reject unknown rule IDs, invalid evidence references, sensitive keys, unbounded
summaries, unsupported severities, and non-finite numbers.

**Step 4: Run tests and verify they fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_observations.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: FAIL because the observation model and scanner contract do not exist.

**Step 5: Implement the minimum dormant foundation**

Add configuration with safe defaults:

```text
TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED=false
TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY=true
TELEGRAM_KOL_RUNTIME_SCANNER_RULES=
TELEGRAM_KOL_RUNTIME_SCANNER_INTERVAL_SECONDS=60
```

Empty rule allowlist evaluates nothing. Wildcards and unknown rules are
refused. No scanner loop or systemd unit is enabled in this task.

**Step 6: Run focused and full architecture regressions**

Run the Step 4 suite plus runtime incident, Agent worker, context resolution,
management, listener, and protection tests.

Expected: PASS with all new defaults dormant.

**Step 7: Commit, push, and deploy schema dormant**

Commit as:

```bash
git commit -m "feat: add dormant incident observation ledger"
```

Deploy only after a safe window. Verify the additive table exists with zero
rows, no scanner process is installed, the normal service returns HTTP 200,
and listener/message continuity is complete.

## Task 4: Implement the First Deterministic Rule Catalog in Shadow Mode

**Files:**
- Create: `src/telegram_kol_research/runtime_incident_rules.py`
- Create: `src/telegram_kol_research/runtime_incident_snapshot.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/runtime_agent_exchange_snapshot.py`
- Create: `tests/test_runtime_incident_rules.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Create: `tests/fixtures/runtime_incident_observations/terminal_entry_exposure.json`
- Create: `tests/fixtures/runtime_incident_observations/unprotected_position.json`
- Create: `tests/fixtures/runtime_incident_observations/unknown_cancel.json`
- Create: `tests/fixtures/runtime_incident_observations/tp1_break_even_gap.json`
- Create: `tests/fixtures/runtime_incident_observations/silent_intake.json`

**Step 1: Write failing rule tests from redacted real incidents**

Implement the first five rules:

- `terminal_lifecycle_exchange_exposure_v1`;
- `active_position_missing_protection_v1`;
- `cancel_outcome_stale_unknown_v1`;
- `tp1_break_even_nonterminal_v1`;
- `monitor_incident_ledger_silence_v1`.

For each rule test normal state, abnormal state, allowed transition window,
incomplete exchange snapshot, stable fingerprint, and recovery.

The terminal-entry cleanup event 3158 becomes a redacted fixture proving that a
brief blocked cleanup followed by confirmed `exchange_cancelled` is a normal
transition and must not create a persistent incident.

**Step 2: Run tests and verify they fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py -q
```

Expected: FAIL because the catalog and coherent snapshot builder do not exist.

**Step 3: Implement pure rules over immutable projections**

Rules receive projections, not SQLAlchemy sessions or exchange clients. The
snapshot builder performs bounded reads and publishes completeness booleans,
observation time, and stable evidence references. No rule imports a write
client, executor, planner, or mutation gateway.

**Step 4: Add mutation-boundary tests**

Fail the test if scanner/rule modules import:

- recognition application or contextual targeting;
- strategy management executor;
- Deepcoin create/cancel/close methods;
- `position_mutation_gateway`;
- database helpers that update source business tables.

Run the scanner against a database copy and assert only the additive observation
and runtime incident tables may differ.

**Step 5: Run focused and domain regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_terminal_entry_cleanup.py \
  tests/test_break_even_convergence_executor.py \
  tests/test_protection_health.py \
  tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 6: Commit and run offline replay**

Commit as:

```bash
git commit -m "feat: detect critical runtime invariants read only"
```

Add a CLI replay command that accepts only fixture paths or an explicit
read-only database path and prints bounded counts. Run all reviewed fixtures
and require expected rule outcome, severity, fingerprint stability, and zero
source mutation.

## Task 5: Install the Scanner Dormant and Run a 48-Hour Shadow Canary

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Create: `deploy/systemd/telegram-kol-runtime-scanner.service`
- Create: `scripts/install_runtime_scanner_sidecar.sh`
- Create: `tests/test_runtime_incident_scanner_service.py`
- Modify: `tests/test_runtime_agent_cli.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing loop and service tests**

Require:

- disabled config exits without scanning;
- enabled config with empty rules performs no reads beyond health setup;
- bounded interval and one scan at a time;
- SIGTERM stops cleanly;
- uncaught scan errors are bounded and do not terminate the main service;
- installer refuses an active/enabled existing scanner;
- unit uses `telegram-kol-agent`, no credentials, no system bus, no write access
  outside the observation ledger path, and no access to checkout secrets;
- unit cannot start unless shadow-only is true on first installation.

**Step 2: Run tests and verify they fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_scanner_service.py \
  tests/test_runtime_agent_cli.py -k 'scanner' -q
```

Expected: FAIL because the scanner command and unit do not exist.

**Step 3: Implement the bounded loop and hardened unit**

The initial loop records observations only. In shadow-only mode it must not
create claimable runtime incidents or send Telegram. Emit one bounded JSON
health line per scan cycle, not one idle line every five seconds.

**Step 4: Run service, scanner, and architecture tests**

Run the Step 2 suite plus database, runtime Agent service, architecture, and
server-installation tests.

Expected: PASS.

**Step 5: Commit, push, and deploy disabled**

Commit as:

```bash
git commit -m "feat: add dormant read-only incident scanner"
```

After a proven safe window, deploy code and install the scanner disabled and
inactive. Verify the main service and Agent remain unchanged.

**Step 6: Enable shadow-only in a separate safe window**

Enable exactly one rule first. Do not restart the main service if the scanner
environment can be installed and the sidecar started independently. Observe at
least 48 hours unless the critical-rule shortened gate in the design is fully
satisfied.

Compare every observation with authoritative local and exchange evidence.
Require zero source-row mutations, zero Telegram notifications, zero main
service latency regression, and no unbounded logs.

## Task 6: Canary Deterministic Telegram Notifications One Rule at a Time

**Files:**
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing confirmation and dedupe tests**

Cover first-snapshot critical confirmation, two-snapshot high confirmation,
transition-window expiry, material-evidence escalation, unchanged-evidence
suppression, recovery notification, Telegram retry, and stable incident IDs.

**Step 2: Write the fact-only message test**

Require fixed labels:

```text
【运行异常｜需要 Codex】
事件ID:
严重等级:
对象:
状态:
已确认事实:
Agent诊断: 尚未运行
自动操作: 无
Codex: 需要
```

Reject secrets, raw exchange responses, chat content, and unbounded identifiers.

**Step 3: Run tests and verify they fail**

Run scanner and system-operator bot focused tests. Expected: FAIL until shadow
confirmation can promote an observation and enqueue the fact-only outbox.

**Step 4: Implement promotion and delivery without AI dependency**

Confirmed observations create ordinary claimable runtime incidents. Reuse the
existing durable notification lease and at-most-once committed-success
behavior. AI provider availability must not gate deterministic delivery.

**Step 5: Run focused and critical regressions**

Run scanner, incidents, system bot, Agent worker, listener, context resolution,
management, protection, and reconciliation tests. Expected: PASS.

**Step 6: Commit and canary one rule**

Commit as:

```bash
git commit -m "feat: notify confirmed read-only incidents"
```

Deploy dormant. In a later safe window, enable Telegram delivery for exactly
one shadow-validated rule. Use a real naturally occurring observation or
reviewed historical replay outside production; do not manufacture a trading
incident. Verify one message, stable incident ID, no duplicate, and zero source
mutation before enabling another rule.

## Task 7: Extend Bounded AI Diagnosis and Codex Handoffs

**Files:**
- Modify: `src/telegram_kol_research/runtime_agent_tools.py`
- Modify: `src/telegram_kol_research/runtime_agent_prompt.py`
- Modify: `src/telegram_kol_research/runtime_agent_contracts.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/runtime_incident_handoff.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_runtime_agent_tools.py`
- Modify: `tests/test_runtime_agent_prompt.py`
- Modify: `tests/test_runtime_agent_contracts.py`
- Modify: `tests/test_runtime_agent_worker.py`
- Modify: `tests/test_runtime_incident_handoff.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: Write failing new-source projection tests**

For each scanner source kind, expose only stable object IDs, deterministic facts,
completeness booleans, bounded state summaries, and evidence references. Do not
expose arbitrary SQL, logs, raw provider bodies, or exchange credentials.

**Step 2: Write failing closed-diagnosis tests**

Require confirmed facts, hypothesis, confidence, missing evidence, impact,
containment, remaining risk, `codex_handoff_required`, recommended code areas,
and attempted read-only queries. Reject business actions, ownership guesses,
nonexistent references, and extra fields.

**Step 3: Write provider-failure fallback tests**

Prove a provider timeout, invalid tool call, fabricated evidence, or exhausted
retry leaves the deterministic notification intact and produces a fact-only
Codex handoff instead of suppressing the event.

**Step 4: Run tests and verify they fail**

Run the files listed above. Expected: FAIL until the new projections and closed
contract exist.

**Step 5: Implement minimal diagnosis support**

Reuse the existing bounded Agent loop, leases, MiMo provider isolation, prompt
budgets, and evidence validator. Do not add action handlers or enable Phase 7
policy evaluation.

**Step 6: Run offline evaluation and regressions**

Extend the reviewed fixture corpus. Require full scores for evidence reference
validity, fact/hypothesis separation, unsafe-action refusal, contextual-target
refusal, budget compliance, Codex decision, and provider-failure fallback.

Run runtime Agent, scanner, system bot, architecture, context resolution,
management, listener, and protection regressions. Expected: PASS.

**Step 7: Commit and enable after deterministic notification is proven**

Commit as:

```bash
git commit -m "feat: diagnose proactive incidents read only"
```

Deploy with diagnosis disabled for scanner-created incidents. After safe-window
and deterministic-notification evidence, enable one rule's AI diagnosis. Verify
all cited evidence exists and Telegram states `自动操作: 无`.

## Task 8: Add Quality Metrics, Self-Health, and Completion Gates

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_metrics.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Create: `tests/test_runtime_agent_metrics.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_runtime_agent_worker.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing bounded-metric tests**

Track counts and bounded latency summaries for observations, confirmations,
diagnoses, fact-only fallbacks, notifications, escalations, recoveries, duplicate
suppressions, evidence-insufficient outcomes, tool steps, token use, and
Codex-confirmed hypothesis accuracy. Do not store prompts or raw responses.

**Step 2: Write independent self-health tests**

The monitor must identify:

- stale scanner heartbeat;
- stale Agent heartbeat;
- monitor abnormality with no corresponding incident observation;
- notification failure not represented in the incident ledger;
- active scanner with an empty or unknown rule set.

Avoid recursion: the Agent cannot be the only component that decides the Agent
is unhealthy.

**Step 3: Run tests and verify they fail**

Run metric, scanner, Agent worker, and production monitor focused tests.
Expected: FAIL until bounded metrics and independent health projections exist.

**Step 4: Implement bounded metrics and health projections**

Add daily model budgets and unchanged-fingerprint diagnosis reuse. Budget
exhaustion escalates to fact-only notification; it never loops indefinitely.

**Step 5: Run the complete verification set**

Run the full local test suite. If a pre-existing timing test fails only under
aggregate load, rerun it in isolation and record both results; do not hide or
reinterpret a new failure.

Run every reviewed offline fixture and require:

- known recent incidents detected;
- zero critical false positives in the reviewed normal corpus;
- complete evidence references;
- no duplicate notification for unchanged evidence;
- provider outage still produces a fact-only alert;
- zero source business-row mutations.

**Step 6: Commit, push, and complete server verification**

Commit as:

```bash
git commit -m "feat: measure proactive incident detection quality"
```

Deploy dormant, then enable metrics and health checks without restarting the
main service when possible. Verify the acceptance criteria in the approved
design and record exact server evidence.

Phase 8R is complete only when all enabled rules meet their shadow/canary gates,
the main service and listener show continuity, the independent monitor alerts
successfully, the Agent has no business-action authority, and every rollback
path has been exercised without affecting normal trading operation.

## Final Acceptance Audit

Before marking Phase 8R complete:

1. Confirm production is on the reviewed pushed commit.
2. Confirm `telegram-kol.service`, listener, recognition, contextual resolution,
   management, exit, protection, and reconciliation are healthy.
3. Confirm scanner and Agent services can be disabled independently.
4. Confirm all Phase 7 flags are false and all action/shadow allowlists empty.
5. Compare production source tables before and after one scanner cycle; only
   observation/incident metadata may change.
6. Confirm critical detection latency is at most two minutes and high detection
   confirmation is at most two observations or ten minutes.
7. Confirm every sent message has a stable incident ID and valid evidence.
8. Confirm provider failure yields a fact-only Telegram report.
9. Confirm unchanged evidence does not create duplicate notification.
10. Confirm a resolved fixture produces exactly one recovery transition.
11. Run the production safety monitor and require no notification configuration,
    state-integrity, adapter, or incomplete-audit error beyond explicitly
    reviewed business abnormalities.
12. Update the status and runbook with commits, tests, deployment evidence,
    enabled rules, rollback proof, and the exact next-session prompt.
