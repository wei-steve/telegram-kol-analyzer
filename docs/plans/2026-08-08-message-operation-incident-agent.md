# Message Operation Incident Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Detect every executable Telegram message that does not reach a correct verified outcome, investigate only those violations with broad enforced read-only access, and send an immediate alert plus a durable copyable Codex handoff.

**Architecture:** Reuse the existing Runtime Incident Agent, ledger, sidecar, scanner, notification outbox, and evidence tools. Add a deterministic per-message expectation contract and an independent outcome supervisor that adds no model call to successful messages; contract violations enter the existing incident pipeline, where a structurally read-only Agent investigates and a separate dispatcher sends two-stage Telegram reports.

**Tech Stack:** Python 3.13, SQLAlchemy, SQLite, FastAPI, Typer, systemd, httpx, the existing OpenAI-compatible MiMo provider, Telegram Bot API, Deepcoin read-only APIs, pytest.

---

## Execution Rules

1. Read `AGENTS.md`, both Runtime Agent design documents, the original
   implementation plan, the canonical status, and the runbook before every
   implementation turn.
2. Resume exactly `current_phase`. Never implement more than one runtime phase
   in one user turn.
3. The original Agent is unfinished. Do not reset, skip, or mark Phase 8R.3
   complete merely because this plan exists.
4. Before adding code, map the task to existing code and reuse it. Do not create
   a parallel incident ledger, worker, notification loop, or policy system.
5. Use TDD. Write a focused failing test, prove the failure, implement the
   smallest behavior, then run focused and adjacent regressions.
6. New runtime behavior ships dormant or shadow-only, with a tested disable
   path. Production enablement requires a fresh safe-window proof and server
   verification.
7. Successful messages must create zero additional model calls.
8. Agent business actions, recovery actions, service control, database writes
   outside its own incident ledger, and exchange writes remain disabled.
9. Preserve unrelated dirty-worktree changes. Stage only files named by the
   current task.
10. Commit and push reviewed changes to
    `codex/deepcoin-auto-trading-v1`. Update the canonical status last.

## Phase Sequence

This plan extends the existing Phase 8R sequence:

- **8R.3:** finish the already in-progress proactive scanner and natural
  notification proof; no new authority;
- **8R.4:** gap inventory and dormant message-operation contract schema;
- **8R.5:** deterministic contract projection and zero-token shadow coverage;
- **8R.6:** outcome supervisor and Stage 1 notification;
- **8R.7:** broad enforced read-only investigation broker;
- **8R.8:** Agent eligibility for every message-operation incident;
- **8R.9:** durable handoff and Stage 2 notification;
- **8R.10:** independent coverage monitoring and final rollout audit.

Each phase is a separate implementation turn and a separate production gate.

### Task 0: Finish Existing Phase 8R.3 Without Reinterpreting It

**Files:**
- Modify only if evidence changes: `docs/runtime-incident-agent-status.md`
- Read: `docs/runtime-incident-agent-runbook.md`
- Read: `src/telegram_kol_research/runtime_incident_scanner.py`
- Test: `tests/test_runtime_incident_scanner.py`
- Test: `tests/test_runtime_incident_scanner_service.py`

**Step 1: Re-read the current checkpoint**

Run:

```bash
sed -n '1,240p' docs/runtime-incident-agent-status.md
sed -n '480,620p' docs/runtime-incident-agent-runbook.md
```

Expected: `current_phase: 8R.3`, `phase_status: in_progress`, and an exact
remaining-evidence statement. Do not replace it from chat memory.

**Step 2: Run the existing local Phase 8R.3 gate**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_scanner_service.py \
  tests/test_runtime_incident_observations.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: PASS. A failure blocks server review and is fixed within 8R.3 only.

**Step 3: Perform the runbook's read-only server review**

Verify the deployed commit, main service, scanner, Agent sidecar, monitor,
latest recognition, in-flight work, scanner observation counts, incident
claims, Telegram claims, and position-mutation source integrity. Do not send a
test trading message and do not replay a historical message.

Expected: evidence matches the current 8R.3 acceptance rule. If the canonical
status still requires a future natural event, keep the phase `in_progress` and
stop. This is not permission to waive the gate.

**Step 4: Update status only from proven evidence**

Record exact redacted server evidence. Set 8R.3 complete only when every
existing criterion is satisfied; otherwise record the remaining external
evidence verbatim.

**Step 5: Validate and commit any status update**

Run:

```bash
git diff --check -- docs/runtime-incident-agent-status.md
git add docs/runtime-incident-agent-status.md
git commit -m "docs: record phase 8r.3 verification"
```

Expected: one documentation-only commit, or no commit when evidence did not
change.

### Task 1: Inventory Reuse and Add the Dormant Contract Schema (Phase 8R.4)

**Files:**
- Create: `docs/runtime-incident-agent-gap-inventory.md`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/message_operation_contracts.py`
- Create: `tests/test_message_operation_contracts.py`
- Modify: `tests/test_db_bootstrap.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write the gap inventory**

Map each approved design requirement to `existing`, `partial`, `missing`, or
`conflicting`. At minimum inventory:

```text
runtime incident ledger
sidecar claim/diagnosis lifecycle
incident-type selectors
scanner observations
read-only evidence tools
Stage 1 notification
Stage 2 diagnosis notification
durable Codex handoff
per-message outcome coverage
read-only OS/database/exchange enforcement
coverage heartbeat and silent-loss monitoring
```

Expected: every new component below cites an existing component it reuses or a
specific gap it fills.

**Step 2: Write failing schema tests**

Add tests equivalent to:

```python
def test_message_operation_contract_schema_is_additive(session_factory):
    contract = create_message_operation_contract(
        session_factory,
        raw_message_id=42,
        intent_kind="manage",
        expected_terminal_kind="verified_management",
        deadline_at=NOW,
        policy_version="message-operation-contract-v1",
    )
    assert contract.status == "observing"
    assert contract.agent_requested is False


def test_one_contract_per_raw_message_and_policy(session_factory):
    first = _create_contract(session_factory, raw_message_id=42)
    second = _create_contract(session_factory, raw_message_id=42)
    assert second.id == first.id
```

Cover bounded intent/terminal/status fields, positive counters, indexes,
old-database bootstrap, and additive migration without rewriting business rows.

**Step 3: Prove the tests fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_message_operation_contracts.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py -q
```

Expected: FAIL because the contract models and helpers do not exist.

**Step 4: Add the minimal additive models**

Add `MessageOperationContract` and `MessageOperationItem` with fields matching
this shape:

```python
class MessageOperationContract(Base):
    __tablename__ = "message_operation_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    raw_message_id: Mapped[int] = mapped_column(index=True)
    intent_kind: Mapped[str] = mapped_column(String(32))
    expected_terminal_kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="observing")
    deadline_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    violation_code: Mapped[str | None] = mapped_column(String(64))
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    runtime_incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_incidents.id")
    )
    agent_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
```

Items store authoritative instruction identity, expected descendant kind,
expected terminal kind, observed terminal kind, status, and bounded evidence
references. Add uniqueness and check constraints; do not store unbounded raw
messages in these tables.

**Step 5: Add dormant helpers only**

Implement idempotent create/get and item append helpers. Do not call them from
the production message path in this phase. Every transition uses a bounded
closed enum and compare-and-set status update.

**Step 6: Extend architecture tests**

Assert `message_operation_contracts.py` cannot import provider clients,
strategy target selection, contextual decision application, management
execution, exchange mutations, or notification sending.

**Step 7: Run focused tests**

Run the Step 3 command plus:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: PASS.

**Step 8: Commit and deploy dormant**

```bash
git add docs/runtime-incident-agent-gap-inventory.md \
  src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/message_operation_contracts.py \
  tests/test_message_operation_contracts.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py \
  tests/test_runtime_agent_architecture_boundary.py \
  docs/runtime-incident-agent-status.md
git commit -m "feat: add dormant message operation contracts"
```

Push and deploy only after the runbook safe-window gate. Verify tables exist,
remain empty, and no model, Telegram, strategy, or exchange behavior changed.

### Task 2: Project Deterministic Expectations Without New AI (Phase 8R.5)

**Files:**
- Modify: `src/telegram_kol_research/message_operation_contracts.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/config.py`
- Create: `tests/test_message_operation_projection.py`
- Modify: `tests/test_message_operation_contracts.py`
- Modify: `tests/test_message_instruction_items.py`
- Modify: `tests/test_context_resolution_worker.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing projection tests**

Use durable fixtures for new entry, multi-target management, stop update, take
profit, cancel, exit, unresolved executable intent, ordinary chat, duplicate,
and superseded instruction. Assert:

```python
projection = project_message_operation_contract(session_factory, raw_message_id=42)
assert projection.executable_intent is True
assert projection.model_calls == 0
assert [item.intent_kind for item in projection.items] == ["take_profit", "stop_loss"]
```

For ordinary chat, assert `projection is None`. For unresolved executable
intent, assert a contract exists with no invented target ID.

**Step 2: Prove the tests fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_message_operation_projection.py \
  tests/test_message_instruction_items.py \
  tests/test_context_resolution_worker.py \
  tests/test_authoritative_recognition.py -q
```

Expected: FAIL because the deterministic projector does not exist.

**Step 3: Implement closed projection adapters**

Build expectations only from `RawMessage`, `RecognitionDecision`,
`ManagementMessageEnvelope`, `MessageInstructionItem`, authoritative strategy
links, management targets, and existing deterministic plans. The projector may
copy stable IDs and status enums; it may not call recognition, contextual
resolution, a provider, or any mutation planner.

Use an explicit result contract:

```python
@dataclass(frozen=True, slots=True)
class MessageOperationProjection:
    raw_message_id: int
    executable_intent: bool
    intent_kind: str
    expected_terminal_kind: str
    deadline_at: datetime
    items: tuple[MessageOperationItemProjection, ...]
    evidence_references: tuple[str, ...]
    model_calls: int = 0
```

Deadlines come from a reviewed mapping by instruction kind; exit, cancel, and
protection changes use the shortest windows.

**Step 4: Add a shadow-only CLI cycle**

Add `message-operation-supervisor --shadow --once` that projects new terminal
messages and persists contracts/items without producing incidents,
notifications, or Agent claims. Refuse startup unless the feature is enabled
and shadow-only in this phase.

**Step 5: Prove zero new model calls**

Patch every existing provider entry point to raise in focused integration
tests, run the projector, and assert projection still completes.

**Step 6: Run focused and adjacent regressions**

Run Step 2 plus:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_management_contracts.py \
  tests/test_strategy_management_planner.py \
  tests/test_message_recognition.py \
  tests/test_recognition_decisions.py -q
```

Expected: PASS.

**Step 7: Commit and run a shadow canary**

```bash
git add src/telegram_kol_research/message_operation_contracts.py \
  src/telegram_kol_research/cli.py src/telegram_kol_research/config.py \
  tests/test_message_operation_projection.py \
  tests/test_message_operation_contracts.py \
  tests/test_message_instruction_items.py \
  tests/test_context_resolution_worker.py \
  tests/test_authoritative_recognition.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: project message operation expectations"
```

Deploy dormant, then enable shadow-only in a proven safe window. Compare every
new contract with existing authoritative records. Record false positives,
missing instruction classes, model-call count, and rollback proof.

### Task 3: Evaluate Outcomes and Create Unified Incidents (Phase 8R.6A)

**Files:**
- Create: `src/telegram_kol_research/message_operation_supervisor.py`
- Modify: `src/telegram_kol_research/message_operation_contracts.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/runtime_incidents.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_message_operation_supervisor.py`
- Create: `tests/fixtures/message_operation_incidents/`
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Add redacted regression fixtures**

Create fixtures for recent real classes: contextual exhaustion, unresolved
actionable intent, missing management descendant, target refusal, partial
result, unknown result, false local success, exchange mismatch, restart skip,
and later reconciliation disproving success.

Each fixture contains expected status, violation code, bounded evidence refs,
and whether an incident must be created.

**Step 2: Write failing evaluator tests**

Cover the closed result:

```python
result = evaluate_message_operation_contract(
    contract=contract,
    evidence=evidence,
    observed_at=NOW,
)
assert result.status == "violated"
assert result.violation_code == "missing_management_descendant"
assert result.should_create_incident is True
```

Also cover verified success, actionable `hold`, `unresolved`, safety refusal,
proven duplicate, proven supersession, deadline behavior, and all-items-must-
verify aggregation.

**Step 3: Prove the tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_message_operation_supervisor.py \
  tests/test_runtime_incident_adapters.py -q
```

Expected: FAIL because the evaluator and adapter do not exist.

**Step 4: Implement deterministic evidence collection and evaluation**

Read only existing durable records. Return `observing`, `verified`,
`violated`, `duplicate_verified`, or `superseded_verified`. Never normalize
missing evidence into success.

Violation codes are closed and include:

```python
MESSAGE_OPERATION_VIOLATIONS = frozenset({
    "recognition_failed",
    "context_unresolved",
    "context_exhausted",
    "action_refused",
    "no_operation_created",
    "missing_management_descendant",
    "partial_operation",
    "unknown_operation_result",
    "operation_timeout",
    "local_success_unverified",
    "exchange_readback_mismatch",
    "restart_or_lease_skip",
    "reconciliation_disproved_success",
})
```

**Step 5: Reuse the existing incident ledger**

Record one `message_operation_failure` runtime incident per violation
fingerprint. Link the contract to that incident and append affected source
message IDs through a bounded additive relation. Do not create a second
incident lifecycle.

**Step 6: Keep production shadow-only**

The first deployment evaluates and records only contract observation state.
It creates no runtime incident, notification, or Agent claim until the shadow
comparison is reviewed.

**Step 7: Run focused and regression tests**

Run Step 3 plus:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_strategy_management_executor.py \
  tests/test_execution_events.py \
  tests/test_position_mutation_architecture.py -q
```

Expected: PASS.

**Step 8: Commit and shadow-verify**

```bash
git add src/telegram_kol_research/message_operation_supervisor.py \
  src/telegram_kol_research/message_operation_contracts.py \
  src/telegram_kol_research/runtime_incident_adapters.py \
  src/telegram_kol_research/runtime_incidents.py \
  src/telegram_kol_research/cli.py \
  tests/test_message_operation_supervisor.py \
  tests/fixtures/message_operation_incidents \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_agent_architecture_boundary.py \
  docs/runtime-incident-agent-status.md
git commit -m "feat: detect message operation violations"
```

Server verification must prove no business mutation, no notification, no
Agent claim, no new model call on success, and exact parity for reviewed
natural contracts.

### Task 4: Send Stage 1 for Every Violated Source Message (Phase 8R.6B)

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/message_operation_supervisor.py`
- Modify: `src/telegram_kol_research/config.py`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `tests/test_message_operation_supervisor.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing per-message outbox tests**

Assert two messages with one fingerprint each get Stage 1, while only one
Agent investigation is requested:

```python
assert stage1_rows_for(incident_id) == [message_1.id, message_2.id]
assert investigation_claims_for(incident_id) == 1
```

Cover durable claim tokens, stale-claim recovery, Telegram message ID,
attempts, bounded redaction, delivery failure, and watermark eligibility.

**Step 2: Prove the tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_system_operator_bot.py \
  tests/test_message_operation_supervisor.py \
  tests/test_db_migrations.py -q
```

Expected: FAIL because Stage 1 is currently incident-level rather than
affected-message-level.

**Step 3: Add an additive Stage 1 outbox**

Create a bounded row keyed by `(runtime_incident_id, raw_message_id,
notification_kind)`. Store status, claim token/time, attempts, next attempt,
Telegram message ID, delivered time, and bounded error code.

**Step 4: Format the immediate alert deterministically**

Include incident ID, source message ID/time, bounded original message,
authoritative intent kind, failed checkpoint, known impact, and the fixed text
that read-only AI investigation is in progress. Do not include a model
hypothesis.

**Step 5: Separate delivery from the Agent**

The existing main-service notification dispatcher claims Stage 1. The Agent
has no Bot Token and cannot choose arbitrary recipients or message text.

**Step 6: Run focused tests and notification regressions**

Run Step 2 and:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_runtime_agent_worker.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 7: Commit, deploy dormant, then activate above a watermark**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/message_operation_supervisor.py \
  src/telegram_kol_research/config.py \
  tests/test_system_operator_bot.py \
  tests/test_message_operation_supervisor.py \
  tests/test_db_migrations.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: notify every message operation violation"
```

Record the stopped-state maximum contract/incident ID before activation.
Enable Stage 1 for every new `message_operation_failure`, not a list of
violation subtypes. Historical rows remain unclaimed.

### Task 5: Add the Broad Enforced Read-Only Investigation Broker (Phase 8R.7)

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_investigation_broker.py`
- Modify: `src/telegram_kol_research/runtime_agent_tools.py`
- Modify: `src/telegram_kol_research/runtime_agent_production_audit.py`
- Modify: `src/telegram_kol_research/runtime_agent_telegram_evidence.py`
- Modify: `src/telegram_kol_research/runtime_agent_exchange_snapshot.py`
- Modify: `deploy/systemd/telegram-kol-runtime-agent.service`
- Modify: `scripts/install_runtime_agent_sidecar.sh`
- Create: `tests/test_runtime_agent_investigation_broker.py`
- Modify: `tests/test_runtime_agent_tools.py`
- Modify: `tests/test_runtime_agent_service.py`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing authority tests**

Cover allowed reads for message/reply evidence, database projections,
processing timeline, bounded journal, deployed code/Git state, non-secret
configuration state, exchange read-only snapshot/history, Telegram evidence,
and prior incidents.

Also assert hard refusal for:

```text
INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA writable_schema
file create/modify/delete outside private scratch
systemctl start/stop/restart/reload/enable/disable
Deepcoin place/amend/cancel/close methods
credential/key/token file reads
unapproved hosts and proxy forwarding
```

**Step 2: Prove authority tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_investigation_broker.py \
  tests/test_runtime_agent_tools.py \
  tests/test_runtime_agent_service.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: FAIL because the broad broker does not exist.

**Step 3: Implement declarative read requests**

Use a closed request envelope rather than arbitrary shell text:

```python
@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    incident_id: int
    evidence_kind: str
    object_ids: tuple[str, ...] = ()
    query: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    maximum_bytes: int = 8192
```

Evidence kinds are broad categories, not incident-type allowlists. Validate
incident binding, identifiers, row/byte/time limits, sensitive markers, and
returned evidence references.

**Step 4: Enforce database and filesystem read-only behavior**

Use immutable snapshots or SQLite read-only/query-only connections for
production facts. Mount source and logs read-only. Give the Agent only a
private temporary workspace. The broker process that can read privileged
evidence must not accept arbitrary commands or paths.

**Step 5: Enforce exchange and network boundaries**

Use a dedicated exchange key with trading disabled. Permit only reviewed
read-only endpoints. Keep the model provider as the only general outbound
destination; reject forwarded/proxied requests and all unapproved hosts.

**Step 6: Add an audit row for every request**

Persist incident ID, evidence kind, bounded arguments fingerprint, result
status, evidence reference, bytes, duration, and denial code. Never persist
credentials or raw provider responses.

**Step 7: Run focused security and regression tests**

Run Step 2 plus:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_exchange_snapshot.py \
  tests/test_runtime_agent_production_audit.py \
  tests/test_runtime_agent_telegram_evidence.py \
  tests/test_position_mutation_architecture.py \
  tests/test_tpsl_ownership_architecture.py -q
```

Expected: PASS.

**Step 8: Commit and deploy with Agent eligibility unchanged**

```bash
git add src/telegram_kol_research/runtime_agent_investigation_broker.py \
  src/telegram_kol_research/runtime_agent_tools.py \
  src/telegram_kol_research/runtime_agent_production_audit.py \
  src/telegram_kol_research/runtime_agent_telegram_evidence.py \
  src/telegram_kol_research/runtime_agent_exchange_snapshot.py \
  deploy/systemd/telegram-kol-runtime-agent.service \
  scripts/install_runtime_agent_sidecar.sh \
  tests/test_runtime_agent_investigation_broker.py \
  tests/test_runtime_agent_tools.py \
  tests/test_runtime_agent_service.py \
  tests/test_runtime_agent_architecture_boundary.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: add audited read-only investigation broker"
```

Deploy the broker dormant. Run isolated read canaries and mutation-refusal
canaries without changing the Agent incident selector.

### Task 6: Diagnose Every Message-Operation Incident (Phase 8R.8)

**Files:**
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/runtime_agent_prompt.py`
- Modify: `src/telegram_kol_research/runtime_agent_contracts.py`
- Modify: `src/telegram_kol_research/runtime_agent_tools.py`
- Modify: `src/telegram_kol_research/runtime_incidents.py`
- Modify: `tests/test_runtime_agent_worker.py`
- Modify: `tests/test_runtime_agent_prompt.py`
- Modify: `tests/test_runtime_agent_contracts.py`
- Modify: `tests/test_runtime_incident_phase5_config.py`
- Modify: `tests/test_runtime_agent_evaluation.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing eligibility tests**

Assert every new `message_operation_failure` is Agent-eligible regardless of
its violation code, while unrelated capture-only incident types retain their
current selector behavior. Assert success contracts never create Agent claims.

**Step 2: Write failing diagnosis-contract tests**

Require structured expected-versus-observed comparison, classification,
confidence, missing evidence, affected message IDs, likely code/test paths,
and `codex_handoff_required=True`. Preserve the rule that the Agent cannot
select a strategy or produce a replacement contextual decision.

**Step 3: Prove the tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_agent_prompt.py \
  tests/test_runtime_agent_contracts.py \
  tests/test_runtime_incident_phase5_config.py -q
```

Expected: FAIL on eligibility and the expanded closed diagnosis fields.

**Step 4: Implement class-level eligibility**

Replace final message-operation eligibility by subtype allowlist with one
reviewed class gate: every post-watermark `message_operation_failure` is
claimable. Keep the global Agent enable flag, watermark, claim lease, attempt
budget, and rollback flag.

**Step 5: Expand the bounded tool loop**

Expose the read-only broker categories. Preserve per-request and total tool
output bounds, repeated-call prevention, 120-second wall budget, and a forced
closed final response. No recovery playbook is nominated or executed for
message-operation incidents.

**Step 6: Reuse same-fingerprint diagnosis safely**

Reuse only when policy version, deployed code version, evidence fingerprint,
and violation class match. Append newly affected message IDs and force a fresh
investigation when material evidence or severity changes.

**Step 7: Run evaluation and regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_agent_prompt.py \
  tests/test_runtime_agent_contracts.py \
  tests/test_runtime_agent_tools.py \
  tests/test_runtime_agent_evaluation.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_strategy_management_executor.py -q
```

Expected: PASS, including zero accepted strategy-targeting output and zero
reachable write path.

**Step 8: Commit and activate above a fresh watermark**

```bash
git add src/telegram_kol_research/config.py \
  src/telegram_kol_research/runtime_agent_worker.py \
  src/telegram_kol_research/runtime_agent_prompt.py \
  src/telegram_kol_research/runtime_agent_contracts.py \
  src/telegram_kol_research/runtime_agent_tools.py \
  src/telegram_kol_research/runtime_incidents.py \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_agent_prompt.py \
  tests/test_runtime_agent_contracts.py \
  tests/test_runtime_incident_phase5_config.py \
  tests/test_runtime_agent_evaluation.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: diagnose all message operation incidents"
```

Deploy with message-operation eligibility dormant, run one isolated canary,
then enable only after a fresh safe-window proof. Agent action flags and both
playbook allowlists remain off/empty.

### Task 7: Persist the Codex Handoff and Send Stage 2 (Phase 8R.9)

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/runtime_incident_handoff.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_runtime_incident_handoff.py`
- Modify: `tests/test_runtime_agent_worker.py`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `tests/test_runtime_agent_cli.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing durable-handoff tests**

Assert a diagnosis commit atomically stores a handoff artifact with incident,
affected messages, instruction items, original/reply evidence, expected versus
observed state, timeline, evidence refs, Agent hypothesis, missing evidence,
likely code/tests, prohibited actions, and a copyable Codex prompt.

Assert the artifact can be rebuilt after process restart without worker-memory
state.

**Step 2: Write failing Stage 2 tests**

Cover diagnosed, reused, provider-failed, tool-failed, evidence-incomplete, and
120-second timeout outcomes. Every outcome must queue an operator-visible
Stage 2 terminal notification.

Assert Stage 2 updates when affected count, severity, evidence, or diagnosis
changes, but not for an unchanged poll.

**Step 3: Prove the tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incident_handoff.py \
  tests/test_runtime_agent_worker.py \
  tests/test_system_operator_bot.py \
  tests/test_runtime_agent_cli.py \
  tests/test_db_migrations.py -q
```

Expected: FAIL because the current handoff is reconstructed/printed rather
than durably delivered as a Stage 2 artifact.

**Step 4: Add the additive handoff artifact and delivery revision**

Use a unique `(runtime_incident_id, diagnosis_revision)` row with bounded
redacted JSON, prompt text, evidence-document metadata, status, created time,
and content fingerprint. Keep Telegram claim/delivery fields in the existing
notification mechanism or its additive Stage 2 outbox.

**Step 5: Commit diagnosis and handoff atomically**

The Agent writes only its allowed incident diagnosis/handoff ledger. It never
sends Telegram directly. If handoff validation fails, the incident reaches an
explicit investigation-failed state and queues the deterministic failure
report.

**Step 6: Render the copyable Codex prompt**

The Telegram text must tell Codex to read `AGENTS.md`, independently verify the
hypothesis, inspect the stable evidence IDs, preserve strategy/context
authority, avoid unknown-write retry, add regression coverage, and follow the
server deployment gate. Do not require the operator to reconstruct IDs by
hand.

**Step 7: Handle oversized evidence**

Keep the bounded prompt in Telegram. Render larger redacted evidence as a JSON
document with stable handoff ID and content hash. The dispatcher, not the
Agent, owns the Bot Token and document upload.

**Step 8: Run focused and adjacent tests**

Run Step 3 plus:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_incidents.py \
  tests/test_runtime_agent_tools.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 9: Commit and canary Stage 2**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/runtime_incident_handoff.py \
  src/telegram_kol_research/runtime_agent_worker.py \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/cli.py \
  tests/test_runtime_incident_handoff.py \
  tests/test_runtime_agent_worker.py \
  tests/test_system_operator_bot.py \
  tests/test_runtime_agent_cli.py \
  tests/test_db_migrations.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: deliver durable runtime agent handoffs"
```

Deploy dormant, verify an isolated non-business canary, then enable for new
message-operation diagnoses only. Prove the Telegram copy block and server
artifact have matching incident/handoff IDs and hashes.

### Task 8: Monitor Coverage and Silent Loss (Phase 8R.10)

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/message_operation_supervisor.py`
- Modify: `scripts/install_server_monitor.sh`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_message_operation_supervisor.py`
- Modify: `tests/test_server_monitor_installation.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing metric and heartbeat tests**

Require bounded metrics for:

```text
executable_messages_total
contracts_created_total
contracts_verified_total
contracts_violated_total
stage1_pending/delivered/failed
agent_pending/diagnosed/failed/timed_out
handoffs_persisted_total
stage2_pending/delivered/failed
oldest_nonterminal_age_seconds
supervisor_last_success_at
```

Assert an executable message without a contract, a violation without Stage 1,
an incident without an operator-visible terminal state, or stale supervisor
heartbeat makes the independent monitor unhealthy.

**Step 2: Prove the tests fail**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py \
  tests/test_message_operation_supervisor.py \
  tests/test_server_monitor_installation.py -q
```

Expected: FAIL because end-to-end coverage is not monitored.

**Step 3: Add bounded read-only coverage projections**

Compute counts and oldest ages from durable ledgers. Expose only authenticated
loopback health data. Do not run a source scan from a public or unauthenticated
endpoint.

**Step 4: Add independent heartbeat checks**

The root-owned monitor reads the supervisor heartbeat but never receives Agent
provider credentials or database write access. A stale heartbeat or silent
coverage gap creates a monitor reason code and uses the existing independent
notification path.

**Step 5: Add end-to-end failure injection**

Test provider timeout, broker denial, missing DB evidence, journal failure,
exchange read failure, Telegram failure, process kill, stale claim recovery,
and restart. Every injected violation must end as diagnosed, failed, or timed
out with durable notification state.

**Step 6: Run the final local gate**

Run Step 2 plus all Runtime Agent, incident, message, context, management,
execution, position-mutation, notification, and architecture suites. Then run
the repository-wide suite and record any pre-existing unrelated failures
separately.

Expected: successful-message model-call delta 0; fixture violation coverage
100%; Stage 1 coverage 100%; operator-visible terminal coverage 100%; handoff
rebuild coverage 100%; unauthorized writes 0.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/message_operation_supervisor.py \
  scripts/install_server_monitor.sh \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py \
  tests/test_message_operation_supervisor.py \
  tests/test_server_monitor_installation.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: monitor runtime agent coverage"
```

**Step 8: Final staged production verification**

Deploy only in a proven safe window. Verify service/listener continuity,
latest recognition, zero in-flight mutation conflicts, complete read-only
exchange snapshot, supervisor heartbeat, coverage parity, Stage 1/Stage 2
outboxes, Agent claims, handoff rebuild, independent monitor health, and every
disable path.

Do not mark Phase 8R complete while any supported executable intent class lacks
a contract, any contract violation can be silent, or any Agent terminal state
lacks an operator-visible Stage 2 result.

## Final Definition of Done

- The original Phase 8R work is reconciled and completed rather than bypassed.
- Every recognized executable message has a durable deterministic contract.
- Successful messages add zero new model calls.
- Every contract violation produces one per-message Stage 1 alert.
- Every message-operation incident is Agent-eligible without subtype rollout
  selectors.
- The Agent can inspect all approved evidence categories through enforced
  read-only boundaries.
- Every investigation ends in a diagnosed, failed, or timed-out Stage 2 report.
- Every Stage 2 report has a durable, reproducible, copyable Codex handoff.
- Coverage gaps and supervisor failure are independently monitored.
- No Agent path can modify strategy decisions, contextual outcomes, services,
  production business data, orders, positions, or exchange state.
