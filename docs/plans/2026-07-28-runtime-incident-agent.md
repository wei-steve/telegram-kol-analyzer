# Runtime Incident AI Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an event-driven runtime incident agent that initially diagnoses and reports production failures, then gains narrowly allowlisted recovery authority without entering the message-recognition or contextual strategy-resolution path.

**Architecture:** Existing production flows emit additive durable incidents without waiting for the agent. A separate worker uses bounded read-only tools, stores structured diagnosis, notifies through the system operator bot, and later selects deterministic recovery playbooks protected by feature flags, policy gates, idempotency, and post-action verification.

**Tech Stack:** Python 3.13, SQLAlchemy, SQLite, FastAPI service lifecycle, httpx/OpenAI-compatible model proxy, pytest, systemd, Telegram Bot API, existing Deepcoin read-only and mutation gateway layers.

---

## Execution Rules

1. Read the design, status, runbook, and `AGENTS.md` before every phase.
2. Implement or resume exactly `current_phase`; never execute two phases in one
   user turn.
3. Preserve unrelated dirty-worktree files.
4. Use TDD for runtime changes.
5. Commit phase changes intentionally and push
   `codex/deepcoin-auto-trading-v1`.
6. Documentation-only phases do not restart production.
7. Runtime phases remain `in_progress` until safe-window deployment and server
   verification complete.
8. Update `docs/runtime-incident-agent-status.md` last.

## Phase 0: Durable Design and Session Control

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/plans/2026-07-28-runtime-incident-agent-design.md`
- Create: `docs/plans/2026-07-28-runtime-incident-agent.md`
- Create: `docs/runtime-incident-agent-status.md`
- Create: `docs/runtime-incident-agent-runbook.md`

**Step 1: Record the invariant boundary**

Document that the agent cannot participate in first-pass recognition, strategy
targeting, contextual multi-information resolution, or unrestricted Deepcoin
writes.

**Step 2: Record the continuity gate**

Document dormant-by-default rollout, additive schema changes, safe deployment
windows, immediate disable paths, and the rule that an unverified server phase
cannot be marked complete.

**Step 3: Add the exact trigger phrase**

Add `请执行自定义ai agent的下一步实施` to `AGENTS.md` and point it at the
four canonical files.

**Step 4: Validate references**

Run:

```bash
for path in \
  docs/plans/2026-07-28-runtime-incident-agent-design.md \
  docs/plans/2026-07-28-runtime-incident-agent.md \
  docs/runtime-incident-agent-status.md \
  docs/runtime-incident-agent-runbook.md; do
  test -s "$path" || exit 1
done
```

Expected: exit code 0.

**Step 5: Commit**

```bash
git add AGENTS.md \
  docs/plans/2026-07-28-runtime-incident-agent-design.md \
  docs/plans/2026-07-28-runtime-incident-agent.md \
  docs/runtime-incident-agent-status.md \
  docs/runtime-incident-agent-runbook.md
git commit -m "docs: plan runtime incident agent rollout"
```

Production restart: not required because this phase changes documentation only.

## Phase 1: Durable Runtime Incident Ledger

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/runtime_incidents.py`
- Create: `tests/test_runtime_incidents.py`
- Modify: `tests/test_db_bootstrap.py`
- Modify: `tests/test_db_migrations.py`
- Create: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write failing model and bootstrap tests**

Cover additive table creation, required defaults, indexes, bounded fields, and
old-database bootstrap.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py -q
```

Expected: FAIL because `RuntimeIncident` and the ledger helpers do not exist.

**Step 2: Add the additive model**

Add `RuntimeIncident` with:

- source kind/source ID;
- incident type and severity;
- fingerprint and generation;
- status;
- repeat count and timestamps;
- claim token/timestamps;
- bounded redacted summary;
- diagnosis/notification/playbook/recovery fields;
- policy and prompt versions.

Do not add a migration that rewrites existing business rows.

**Step 3: Implement deterministic ledger helpers**

Implement:

- `record_runtime_incident`;
- `list_claimable_runtime_incidents`;
- `claim_runtime_incident`;
- `transition_runtime_incident`;
- `release_or_expire_runtime_incident_claim`.

Use compare-and-set transitions and same-fingerprint deduplication.

**Step 4: Add architectural boundary test**

Fail if runtime-agent modules import recognition application, contextual
strategy resolution, strategy targeting, management execution, or unchecked
Deepcoin write symbols. Permit typed IDs and read-only projections only.

**Step 5: Run focused and regression tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_strategy_management_executor.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/runtime_incidents.py \
  tests/test_runtime_incidents.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py \
  tests/test_runtime_agent_architecture_boundary.py
git commit -m "feat: add runtime incident ledger"
```

**Step 7: Deploy without enabling behavior**

Before restart, run the safe-window checks in the runbook. Deploy additive
schema code with all incident-agent flags off. Verify service health, listener
checkpoint continuity, reconcile backlog, and production monitor. If no safe
window exists, leave the phase `in_progress`.

## Phase 2: Deterministic Incident Adapters and Telegram Baseline

**Files:**
- Create: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/semantic_disagreement_review.py`
- Modify: `src/telegram_kol_research/context_resolution_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/config.py`
- Create: `tests/test_runtime_incident_adapters.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: Write failing adapter tests**

Cover only technical/runtime states:

- provider/worker exhaustion;
- monitor adapter/incomplete audit;
- `submit_unknown`;
- `partial_failed`;
- `recovery_required`;
- severe protection incident;
- notification failure.

Assert that `unresolved`, `hold`, and ordinary contextual reanalysis do not
create incidents.

**Step 2: Implement best-effort adapters**

Adapters must preserve the original source transition even if incident
recording fails. Use stable source IDs and redacted fingerprints.

**Step 3: Write failing notification tests**

Cover fixed labels, at-most-once claims, bounded output, redaction, retry
behavior, and non-blocking delivery failure.

**Step 4: Implement Telegram incident notification**

Use the existing system operator bot. Send an initial deterministic report
without invoking AI.

**Step 5: Run focused and critical regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_adapters.py \
  tests/test_production_safety_monitor.py \
  tests/test_system_operator_bot.py \
  tests/test_context_resolution_worker.py \
  tests/test_strategy_management_executor.py -q
```

Expected: PASS.

**Step 6: Commit and deploy dormant**

Commit as `feat: record and notify runtime incidents`. Deploy with incident
capture and notifications disabled. Compare read-only source counts first,
then enable capture for one incident class. Enable Telegram delivery only after
dedupe evidence is correct. Do not restart during an active operation.

## Phase 3: Read-Only Incident Agent and Codex Handoff

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_contracts.py`
- Create: `src/telegram_kol_research/runtime_agent_tools.py`
- Create: `src/telegram_kol_research/runtime_agent_prompt.py`
- Create: `src/telegram_kol_research/runtime_agent_worker.py`
- Create: `src/telegram_kol_research/runtime_incident_handoff.py`
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_runtime_agent_contracts.py`
- Create: `tests/test_runtime_agent_tools.py`
- Create: `tests/test_runtime_agent_worker.py`
- Create: `tests/test_runtime_incident_handoff.py`

**Step 1: Define a closed structured contract**

Require incident ID, diagnosis hypothesis, confidence, evidence references,
missing evidence, recommended playbook name, auto-handle eligibility, and
Codex-handoff requirement. Reject extra actions and unknown tools.

**Step 2: Build bounded read-only tools**

Return compact redacted JSON. Do not expose arbitrary SQL, shell, secrets,
unbounded logs, raw provider bodies, or write clients.

**Step 3: Implement the bounded loop**

Enforce:

- maximum 4 tool steps initially;
- maximum wall time;
- maximum prompt/tool-output bytes;
- per-incident fingerprint reuse;
- repeated-tool refusal;
- no action execution;
- safe failure and claim recovery.

**Step 4: Build the Codex handoff bundle**

Store evidence and model hypotheses separately. Generate a ready-to-use prompt
that tells Codex to verify the hypothesis and follow `AGENTS.md`.

**Step 5: Add Telegram diagnosis report**

Report incident ID, evidence-based diagnosis, uncertainty, attempted queries,
remaining risk, and handoff availability.

**Step 6: Run tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_contracts.py \
  tests/test_runtime_agent_tools.py \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_incident_handoff.py \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 7: Commit and shadow deploy**

Commit as `feat: add read-only runtime incident agent`. Deploy disabled, then
enable for synthetic/read-only incidents only. Confirm no source business row
changes and compare Token usage before real incident enablement.

## Phase 4: Expand Read-Only Evidence and Build Evaluation Corpus

**Files:**
- Modify: `src/telegram_kol_research/runtime_agent_tools.py`
- Create: `src/telegram_kol_research/runtime_agent_evaluation.py`
- Create: `tests/fixtures/runtime_incidents/`
- Modify: `tests/test_runtime_agent_tools.py`
- Create: `tests/test_runtime_agent_evaluation.py`
- Modify: `docs/runtime-incident-agent-runbook.md`

**Step 1: Export reviewed, redacted historical incidents**

Include representative provider failures, worker exhaustion, `submit_unknown`,
`partial_failed`, `recovery_required`, protection incidents, and notification
failures. Do not include normal contextual ambiguity as an agent-owned case.

**Step 2: Add coherent evidence projections**

Add bounded local/exchange comparison, worker history, prior same-fingerprint
attempts, and protection summaries.

**Step 3: Add offline evaluations**

Measure:

- correct incident classification;
- correct evidence tool selection;
- unsafe recommendation refusal;
- unsupported certainty;
- Token/tool-step budgets;
- no contextual strategy targeting.

**Step 4: Run tests and commit**

Run all runtime-agent tests plus context-resolution and management regressions.
Commit as `test: expand runtime incident agent evaluations`.

**Step 5: Server validation**

Run read-only against reviewed incidents. Do not enable any action authority.

## Phase 5: Versioned Recovery Playbooks in Shadow Mode

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_playbooks.py`
- Create: `src/telegram_kol_research/runtime_agent_policy.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/config.py`
- Create: `tests/test_runtime_agent_playbooks.py`
- Create: `tests/test_runtime_agent_policy.py`
- Modify: `tests/test_runtime_agent_worker.py`

**Step 1: Define playbook metadata and refusal contract**

Each playbook declares incident types, prerequisites, side-effect class,
idempotency, limits, verification, and refusal reasons.

**Step 2: Add first shadow-only playbooks**

- refresh read-only exchange snapshot;
- rerun production audit;
- recover a stale side-effect-free claim;
- reschedule an AI job proven not to own a business write;
- fetch missing Telegram evidence;
- build read-only reconciliation plan.

**Step 3: Enforce policy separation**

The model may nominate a playbook. Deterministic policy independently accepts
or refuses it. No playbook executes in this phase.

**Step 4: Evaluate and commit**

Compare agent selections with reviewed expected outcomes. Require zero accepted
unsafe actions in the corpus. Commit as `feat: add shadow incident recovery
playbooks`.

**Step 5: Shadow deploy**

Record nominations and policy results only. Telegram reports clearly state
that no action was executed.

## Phase 6: Low-Risk Automatic Recovery

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_executor.py`
- Modify: `src/telegram_kol_research/runtime_agent_playbooks.py`
- Modify: `src/telegram_kol_research/runtime_agent_policy.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/config.py`
- Create: `tests/test_runtime_agent_executor.py`
- Modify: `tests/test_runtime_agent_playbooks.py`
- Modify: `tests/test_runtime_agent_worker.py`

**Step 1: Write failing execution-boundary tests**

Require current fingerprint, idempotency record, one allowlisted action,
attempt budget, feature flag, and post-action verification.

**Step 2: Implement only low-risk actions**

Initially permit:

- refresh read-only snapshots;
- rerun audits;
- recover stale side-effect-free claims;
- reschedule AI jobs proven not to have business-write ownership;
- fetch missing evidence;
- freeze further action for an incident.

Do not add order, position, protection, strategy, recognition, or contextual
resolution mutations.

**Step 3: Add circuit breaker and disable path**

Automatically disable action authority after repeated verification mismatch,
unexpected exception, or budget breach.

**Step 4: Test and commit**

Run runtime-agent, database, listener, context-resolution, executor, and monitor
regressions. Commit as `feat: enable low-risk incident recovery`.

**Step 5: Canary deploy**

Enable one reversible playbook at a time. Verify before enabling the next.
Every action sends a Telegram report containing the incident, action, evidence,
and verification outcome.

## Phase 7: Optional Bounded Business Recovery

This phase requires a fresh explicit user approval after reviewing Phase 6
metrics. The trigger phrase alone does not authorize it until the status file
records that approval.

**Potential Files:**
- Modify: `src/telegram_kol_research/runtime_agent_playbooks.py`
- Modify: `src/telegram_kol_research/runtime_agent_policy.py`
- Modify: `src/telegram_kol_research/runtime_agent_executor.py`
- Integrate only through existing reviewed repair planners and
  `position_mutation_gateway.py`
- Add dedicated tests for every approved playbook

**Candidate actions:**

- apply an already fingerprinted deterministic database repair;
- cancel an exact owned unfilled entry order;
- restore an exact missing protection order;
- remove exact residual protection after confirmed close.

**Permanent refusals:**

- new entry or add-on;
- direction, leverage, or size selection;
- wider or removed protection;
- guessed ownership;
- retry of unknown write;
- replacement of contextual strategy resolution;
- direct unchecked Deepcoin write.

Every candidate action receives its own design review, test corpus, shadow
period, feature flag, canary, circuit breaker, and Telegram report.

## Phase 8: Cost, Quality, and Continuous Improvement

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_metrics.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify relevant Web templates only if an operator view is approved
- Create: `tests/test_runtime_agent_metrics.py`
- Modify: `docs/runtime-incident-agent-runbook.md`

**Step 1: Record bounded metrics**

Track incident counts, diagnosis outcome, escalation, auto-recovery,
verification mismatch, latency, tool steps, Token usage, and Codex-confirmed
hypothesis accuracy.

**Step 2: Add budget enforcement**

Set per-incident and daily model budgets, reuse unchanged fingerprints, and
escalate instead of looping.

**Step 3: Add reviewed regression workflow**

Convert confirmed incidents into redacted fixtures. Prompt, tool, policy, or
playbook changes must pass the fixture suite before deployment.

**Step 4: Final verification**

Run the complete project suite locally and the documented server verification.
Update status to `complete` only after all approved phases pass.

## Completion Definition

The rollout is complete when:

- the existing strategy-resolution pipeline is unchanged in authority;
- defined runtime incidents are durably deduplicated;
- the read-only agent produces bounded auditable diagnoses;
- Telegram reports include actions and remaining risk;
- Codex handoffs are reproducible from incident IDs;
- approved low-risk playbooks execute idempotently and verify results;
- every action can be disabled without stopping the normal system;
- production continuity and Token budgets meet the recorded targets;
- optional Phase 7 is either completed under separate approval or explicitly
  marked out of scope.
