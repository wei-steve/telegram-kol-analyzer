# Multi-Target Isolation and AI Agent Notification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute every safe target in a multi-target management message independently, persist target-level outcomes, and route every terminal error through deterministic `AI agent通知：（内容）` reporting plus read-only Agent diagnosis.

**Architecture:** Add an immutable message envelope and one target ledger row per declared lifecycle, then admit and execute each disjoint exact-position target through the existing candidate/item path. Derive message summaries from target rows, capture closed target/envelope incidents at durable boundaries, and widen deterministic notification and read-only Agent selectors only through dormant, shadow, and per-type canary stages.

**Tech Stack:** Python 3.11+, SQLAlchemy ORM, SQLite additive bootstrap migrations, pytest, asyncio workers, Telegram system-operator bot, existing Runtime Incident ledger and read-only Agent.

---

## Execution constraints

- Use @test-driven-development for every implementation task.
- Use @requesting-code-review before each runtime-enabling commit.
- Do not replay message 3465 or any historical Telegram message.
- Do not submit live test orders. Use isolated databases, fakes, read-only server
  inspection, and natural future messages.
- Keep first-pass recognition and contextual resolution authoritative.
- Keep Agent action authority false and both action playbook allowlists empty.
- Every feature starts dormant or shadow-only and has an independent disable
  switch.
- Runtime Incident phases must follow `docs/runtime-incident-agent-status.md`.
  Do not implement or enable more than one Runtime Incident phase in one user
  turn.
- Before any production restart, prove the safe deployment window required by
  `AGENTS.md` and the runtime-agent runbook.

### Task 1: Add the dormant envelope and target ledgers

**Files:**
- Modify: `src/telegram_kol_research/models.py:626`
- Modify: `src/telegram_kol_research/db.py:15`
- Test: `tests/test_db_migrations.py`
- Test: `tests/test_multi_target_management.py`

**Step 1: Write the failing model and migration tests**

Create `tests/test_multi_target_management.py` with an isolated database test:

```python
def test_bootstrap_creates_multi_target_ledgers(session_factory):
    with session_factory() as session:
        envelope = ManagementMessageEnvelope(
            raw_message_id=1,
            decision_fingerprint="d" * 64,
            normalized_action="partial_take_profit",
            shared_parameters_json="{}",
            projection_mode="shadow",
        )
        session.add(envelope)
        session.flush()
        target = ManagementMessageTarget(
            envelope_id=envelope.id,
            raw_message_id=1,
            target_lifecycle_id=10,
            target_ordinal=0,
            symbol="BTC",
            side="short",
            normalized_action="partial_take_profit",
            parameters_json='{"fraction":0.5}',
            parameter_fingerprint="p" * 64,
            collision_group_fingerprint="c" * 64,
            admission_state="identified",
            execution_state="not_started",
        )
        session.add(target)
        session.commit()
        assert target.id is not None
```

Extend `tests/test_db_migrations.py` to bootstrap twice and assert both new
tables and their unique indexes remain valid.

**Step 2: Run the tests and verify they fail**

Run:

```bash
pytest -q tests/test_db_migrations.py tests/test_multi_target_management.py
```

Expected: FAIL because `ManagementMessageEnvelope` and
`ManagementMessageTarget` do not exist.

**Step 3: Add minimal ORM models**

In `models.py`, add additive models with bounded constraints and these unique
identities:

```python
class ManagementMessageEnvelope(Base):
    __tablename__ = "management_message_envelopes"
    __table_args__ = (
        UniqueConstraint(
            "raw_message_id", "decision_fingerprint",
            name="uq_management_message_envelopes_decision",
        ),
    )
    # raw_message_id, decision_fingerprint, normalized_action,
    # shared_parameters_json, projection_mode, created_at, updated_at


class ManagementMessageTarget(Base):
    __tablename__ = "management_message_targets"
    __table_args__ = (
        UniqueConstraint(
            "raw_message_id", "target_lifecycle_id", "normalized_action",
            "parameter_fingerprint",
            name="uq_management_message_targets_idempotency",
        ),
    )
    # envelope/raw/lifecycle FKs, ordinal, symbol, side, action, bounded JSON,
    # fingerprints, admission/execution states, closed_reason_code,
    # candidate/item/incident nullable FKs, and progress timestamps
```

Let `Base.metadata.create_all()` create the new tables. Add only additive
compatibility entries in `db.py` if a column is changed during this task; never
drop or rewrite an existing table.

**Step 4: Run focused tests**

Run:

```bash
pytest -q tests/test_db_migrations.py tests/test_multi_target_management.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_db_migrations.py tests/test_multi_target_management.py
git commit -m "feat: add dormant multi-target management ledger"
```

### Task 2: Implement target-ledger idempotency and aggregate projection

**Files:**
- Create: `src/telegram_kol_research/management_message_targets.py`
- Modify: `tests/test_multi_target_management.py`

**Step 1: Write failing state-machine tests**

Add tests for:

```python
def test_upsert_target_is_idempotent_for_message_lifecycle_action_parameters(...):
    first = project_management_targets_in_session(session, decision=decision)
    second = project_management_targets_in_session(session, decision=decision)
    assert [row.id for row in first] == [row.id for row in second]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["confirmed", "confirmed"], "succeeded"),
        (["confirmed", "refused"], "partial_success"),
        (["confirmed", "failed"], "partial_success"),
        (["confirmed", "submit_unknown"], "attention_required"),
        (["refused", "failed"], "failed"),
    ],
)
def test_aggregate_status_is_derived_and_never_stored(states, expected):
    assert derive_envelope_status(states) == expected
```

Also test invalid state transitions and JSON/fingerprint bounds.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_multi_target_management.py
```

Expected: FAIL because projection helpers are missing.

**Step 3: Implement the minimal domain module**

Add closed constants and transition functions:

```python
ADMISSION_STATES = frozenset({"identified", "validating", "admitted", "refused"})
EXECUTION_STATES = frozenset({
    "not_started", "pending", "executing", "submitted", "confirmed",
    "failed", "submit_unknown", "recovery_required",
})

def target_idempotency_fingerprint(*, raw_message_id, lifecycle_id, action, parameters):
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{raw_message_id}\0{lifecycle_id}\0{action}\0{canonical}".encode()
    ).hexdigest()

def derive_envelope_status(states: Collection[str]) -> str:
    if any(state in {"submit_unknown", "recovery_required"} for state in states):
        return "attention_required"
    if states and all(state == "confirmed" for state in states):
        return "succeeded"
    if "confirmed" in states:
        return "partial_success"
    return "failed"
```

Use `session.begin_nested()` around each target insert. Resolve unique conflicts
by reading the existing row; do not roll back previously projected targets.

**Step 4: Run focused tests**

```bash
pytest -q tests/test_multi_target_management.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/management_message_targets.py tests/test_multi_target_management.py
git commit -m "feat: project independent management targets"
```

### Task 3: Unify the closed multi-target action policy

**Files:**
- Modify: `src/telegram_kol_research/management_directives.py`
- Modify: `src/telegram_kol_research/context_resolution.py:160`
- Modify: `src/telegram_kol_research/message_recognition.py:1794`
- Test: `tests/test_management_directives.py`
- Test: `tests/test_context_resolution.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write policy-parity tests**

Assert that both contextual parsing and persistence accept exactly the same
initial action set:

```python
@pytest.mark.parametrize("action", [
    "partial_take_profit", "exit_full", "exit_partial", "cancel_pending_entry",
])
def test_context_and_persistence_share_multi_target_risk_reduction_policy(action):
    assert multi_target_action_policy(action).fanout_allowed

@pytest.mark.parametrize("action", [
    "add_position", "reverse", "revise_entry", "replace_shared_stop",
])
def test_risk_increasing_or_shared_value_actions_never_fan_out(action):
    assert not multi_target_action_policy(action).fanout_allowed
```

Add a regression proving context-accepted full exit is not rejected by
`_validate_explicit_management_targets_in_session` merely because it is not
partial take profit.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_management_directives.py tests/test_context_resolution.py tests/test_message_recognition.py -k 'multi_target or fanout'
```

Expected: FAIL on full exit, partial exit, and cancel parity.

**Step 3: Implement one shared closed policy**

Add a frozen policy object in `management_directives.py`:

```python
@dataclass(frozen=True)
class MultiTargetActionPolicy:
    action: str
    risk_reducing: bool
    fanout_allowed: bool
    requires_fraction: bool = False

MULTI_TARGET_ACTIONS = {
    "partial_take_profit": MultiTargetActionPolicy(..., True, True, True),
    "exit_full": MultiTargetActionPolicy(..., True, True),
    "exit_partial": MultiTargetActionPolicy(..., True, True, True),
    "cancel_pending_entry": MultiTargetActionPolicy(..., True, True),
}
```

Make `context_resolution.py` and `message_recognition.py` consume this function.
Retain explicit lifecycle, symbol, side, same-chat, message-time, live-binding,
and no-risk-increase checks.

**Step 4: Run focused tests**

Run the command from Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/management_directives.py src/telegram_kol_research/context_resolution.py src/telegram_kol_research/message_recognition.py tests/test_management_directives.py tests/test_context_resolution.py tests/test_message_recognition.py
git commit -m "fix: align multi-target action contracts"
```

### Task 4: Add dormant and shadow projection switches

**Files:**
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/web_app.py:3799`
- Modify: `src/telegram_kol_research/message_recognition.py:2448`
- Modify: `tests/test_runtime_incident_phase5_config.py`
- Modify: `tests/test_message_recognition.py`

**Step 1: Write failing configuration tests**

Test secure defaults:

```python
def test_multi_target_projection_defaults_dormant():
    config = load_multi_target_management_config({})
    assert config.projection_enabled is False
    assert config.shadow_only is True
    assert config.live_actions == frozenset()
```

Add a recognition test proving shadow projection writes envelope/target rows
but creates exactly the same candidates/items as the pre-feature path.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_runtime_incident_phase5_config.py tests/test_message_recognition.py -k multi_target
```

Expected: FAIL because the config and projection call do not exist.

**Step 3: Implement dormant configuration and shadow call**

Introduce:

```text
TELEGRAM_KOL_MULTI_TARGET_PROJECTION_ENABLED=false
TELEGRAM_KOL_MULTI_TARGET_SHADOW_ONLY=true
TELEGRAM_KOL_MULTI_TARGET_LIVE_ACTIONS=
```

Call `project_management_targets_in_session` only after the authoritative
decision is durable. In shadow mode, never branch candidate creation on the new
rows and never enqueue executable work from them.

**Step 4: Run focused tests**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/config.py src/telegram_kol_research/web_app.py src/telegram_kol_research/message_recognition.py tests/test_runtime_incident_phase5_config.py tests/test_message_recognition.py
git commit -m "feat: shadow multi-target projection"
```

### Task 5: Replace all-or-nothing admission with target isolation

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py:1062`
- Modify: `src/telegram_kol_research/management_scope.py`
- Modify: `src/telegram_kol_research/management_message_targets.py`
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_multi_target_management.py`
- Fixture: `tests/fixtures/context_resolution/shuqin_3465_multitarget.json`

**Step 1: Write the failing Shu Qin isolation regression**

Use the existing bounded fixture, invalidate only the ETH lifecycle, and assert:

```python
result = apply_fixture("shuqin_3465_multitarget.json", invalidate="ETH")
assert result.target("BTC").admission_state == "admitted"
assert result.target("ETH").admission_state == "refused"
assert result.target("ETH").closed_reason_code == "target_not_live"
assert result.candidates_for("BTC") == 1
assert result.items_for("BTC") == 1
assert result.candidates_for("ETH") == 0
```

Add the inverse case and a target-order permutation test. Assert the same valid
target IDs and outcomes for every input order.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_message_recognition.py tests/test_multi_target_management.py -k 'shuqin or target_isolation or permutation'
```

Expected: FAIL because the whole target list is currently rejected.

**Step 3: Implement per-target admission**

Replace the boolean whole-list validator with a result per target:

```python
@dataclass(frozen=True)
class TargetAdmission:
    decision: dict[str, Any]
    accepted: bool
    reason_code: str | None
    collision_group: str | None

def admit_explicit_management_targets_in_session(...) -> list[TargetAdmission]:
    results = []
    for decision in target_decisions:
        try:
            with session.begin_nested():
                results.append(_admit_one_target(...))
        except (ManagementScopeError, ValueError) as exc:
            results.append(refusal_from_closed_error(decision, exc))
    return results
```

Persist every result. Recurse into `_apply_lifecycle_event_decision` and
`_apply_deterministic_management_scope_if_matched` only for accepted targets.
Do not let a refused target undo candidates already created for another target.

**Step 4: Run focused tests**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/message_recognition.py src/telegram_kol_research/management_scope.py src/telegram_kol_research/management_message_targets.py tests/test_message_recognition.py tests/test_multi_target_management.py tests/fixtures/context_resolution/shuqin_3465_multitarget.json
git commit -m "fix: isolate multi-target admission failures"
```

### Task 6: Track execution independently and freeze only collision groups

**Files:**
- Modify: `src/telegram_kol_research/message_instruction_items.py:150`
- Modify: `src/telegram_kol_research/strategy_management_worker.py:220`
- Modify: `src/telegram_kol_research/management_message_targets.py`
- Modify: `tests/test_message_instruction_items.py`
- Modify: `tests/test_strategy_management_worker.py`
- Modify: `tests/test_composite_management_fault_injection.py`

**Step 1: Write failing continuation and collision tests**

Cover three cases:

```python
def test_failed_first_target_does_not_block_second_disjoint_target(...): ...
def test_submit_unknown_freezes_only_same_pos_id_collision_group(...): ...
def test_overlapping_targets_do_not_claim_while_disjoint_target_continues(...): ...
```

Assert sequential exchange calls, target terminal states, and that the second
disjoint item is claimed after the first reaches `failed`, `unknown`, or
`recovery_required`.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_message_instruction_items.py tests/test_strategy_management_worker.py tests/test_composite_management_fault_injection.py -k 'target or collision or continue'
```

Expected: at least one continuation/collision assertion FAILS.

**Step 3: Implement target-state synchronization**

- Link admitted target rows to candidate and item IDs.
- On item claim, set only its target to `executing`.
- Map successful worker/batch results to `submitted` then `confirmed`.
- Map item `failed` to target `failed`, unknown submission to
  `submit_unknown`, and paused recovery to `recovery_required`.
- Change claim exclusion from whole-envelope uncertainty to exact
  `collision_group_fingerprint` uncertainty. Preserve the existing one-at-a-time
  account execution rule.
- Use compare-and-set updates so stale workers cannot overwrite a terminal
  state.

**Step 4: Run focused and regression tests**

```bash
pytest -q tests/test_message_instruction_items.py tests/test_strategy_management_worker.py tests/test_composite_management_fault_injection.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/message_instruction_items.py src/telegram_kol_research/strategy_management_worker.py src/telegram_kol_research/management_message_targets.py tests/test_message_instruction_items.py tests/test_strategy_management_worker.py tests/test_composite_management_fault_injection.py
git commit -m "fix: continue disjoint management targets after failure"
```

### Task 7: Enable actions one at a time behind the live allowlist

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_contracts.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/management_directives.py`
- Modify: `tests/test_strategy_management_contracts.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `tests/test_management_scope.py`

**Step 1: Write failing action matrix tests**

For each of `partial_take_profit`, `exit_full`, `exit_partial`, and
`cancel_pending_entry`, prove two disjoint targets get independent plans.
Prove add/reverse/revise and shared stop/TP values are refused target-by-target.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_strategy_management_contracts.py tests/test_strategy_management_planner.py tests/test_management_scope.py -k multi_target
```

Expected: FAIL for actions not yet represented by the closed contracts.

**Step 3: Implement the minimal action mappings**

Map only already supported single-target planner operations. Do not add a new
exchange operation. Require `0 < fraction <= 1` for partial actions and exact
owned pending entry legs for cancel. Gate live projection with the explicit
`TELEGRAM_KOL_MULTI_TARGET_LIVE_ACTIONS` allowlist.

Enable only `partial_take_profit` in the first live-capable commit. Add the
other action names in separate later commits after their shadow evidence passes.

**Step 4: Run focused tests**

Run the Step 2 command. Expected: PASS with the live allowlist empty by default.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_contracts.py src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/management_directives.py tests/test_strategy_management_contracts.py tests/test_strategy_management_planner.py tests/test_management_scope.py
git commit -m "feat: gate isolated multi-target actions"
```

### Task 8: Capture target- and envelope-scoped failures

**Files:**
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/config.py:13`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `tests/test_runtime_incidents.py`
- Modify: `tests/test_multi_target_management.py`

**Step 1: Write failing incident adapter tests**

Test closed types including:

```text
management_target_refused
management_target_orchestration_failed
management_target_visibility_exhausted
management_target_drift
management_target_collision
unclassified_operation_failure
```

Assert target incidents use `management_message_target:<id>` and global faults
use `management_message_envelope:<id>`. Assert redaction, deduplication, and
capture failure never rolls back another target's candidate/item.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_runtime_incident_adapters.py tests/test_runtime_incidents.py tests/test_multi_target_management.py -k 'target or envelope or unclassified'
```

Expected: FAIL because the adapters and explicit capture types are absent.

**Step 3: Implement bounded adapters capture-only**

Add one generic internal helper but expose explicit adapters:

```python
def capture_management_target_failure(
    session_factory, *, config, target_id, incident_type,
    reason_code, severity, occurred_at, recorder=None,
):
    if incident_type not in MANAGEMENT_TARGET_INCIDENT_TYPES:
        raise ValueError("unsupported management target incident type")
    return _capture(
        session_factory,
        config=config,
        source_kind="management_message_target",
        source_record_id=str(target_id),
        incident_type=incident_type,
        severity=severity,
        redacted_summary=_summary(reason_code=_safe_label(reason_code)),
        occurred_at=occurred_at,
        recorder=recorder,
    )
```

Call adapters only after the source transaction commits through
`capture_runtime_incident_best_effort`. Add explicit capture profiles; do not
use wildcard selectors. Keep Telegram and Agent selectors unchanged in this
task.

**Step 4: Run focused tests**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/runtime_incident_adapters.py src/telegram_kol_research/config.py src/telegram_kol_research/message_recognition.py src/telegram_kol_research/strategy_management_worker.py tests/test_runtime_incident_adapters.py tests/test_runtime_incidents.py tests/test_multi_target_management.py
git commit -m "feat: capture isolated target failures"
```

### Task 9: Standardize deterministic notification and grouped summaries

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py:150`
- Modify: `src/telegram_kol_research/message_instruction_items.py:455`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `tests/test_message_instruction_items.py`

**Step 1: Write failing title and grouping tests**

Assert both base and diagnosed reports begin with the exact prefix:

```python
assert text.startswith("AI agent通知：（")
```

Add a grouped-envelope fixture with BTC confirmed and ETH refused. Assert one
rendered notification lists both outcomes while both target incidents remain
separate durable rows. Test Telegram length splitting keeps the title on every
part and notification failure creates `notification_delivery_failure` without
changing target execution state.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_system_operator_bot.py tests/test_message_instruction_items.py -k 'ai_agent_title or grouped_target or notification_failure'
```

Expected: FAIL because current reports start with `【运行异常】`.

**Step 3: Implement one renderer boundary**

Add:

```python
AI_AGENT_NOTIFICATION_PREFIX = "AI agent通知：（"

def wrap_ai_agent_notification(content: str) -> str:
    bounded = _safe_runtime_incident_value(content, limit=3500)
    return f"{AI_AGENT_NOTIFICATION_PREFIX}{bounded}）"
```

Route deterministic incident reports, diagnosis addenda, and target aggregate
reports through this wrapper. Group only incidents sharing the same envelope
and notification window; never merge ledger rows or claims.

**Step 4: Run focused tests**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py src/telegram_kol_research/message_instruction_items.py tests/test_system_operator_bot.py tests/test_message_instruction_items.py
git commit -m "feat: standardize AI agent notifications"
```

### Task 10: Add read-only Agent diagnosis for target incidents

**Files:**
- Modify: `src/telegram_kol_research/runtime_incident_snapshot.py`
- Modify: `src/telegram_kol_research/runtime_agent_worker.py`
- Modify: `src/telegram_kol_research/runtime_agent_policy.py`
- Modify: `tests/test_runtime_agent_worker.py`
- Modify: `tests/test_runtime_agent_policy.py`
- Modify: `tests/test_runtime_agent_executor.py`
- Modify: `tests/test_runtime_agent_evaluation.py`

**Step 1: Write failing read-only diagnosis tests**

For each target incident type, assert the snapshot contains only stable refs for
the incident, target, lifecycle, binding, item, and bounded exchange/audit
evidence. Assert raw message text, provider output, credentials, and mutation
tools are absent. Simulate Agent provider failure and assert the deterministic
notification remains deliverable.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_runtime_agent_worker.py tests/test_runtime_agent_policy.py tests/test_runtime_agent_executor.py -k management_target
```

Expected: FAIL because target incidents are not selectable or resolvable.

**Step 3: Implement diagnosis-only support**

- Add exact target incident types to the snapshot resolver.
- Keep `actions_enabled=False` and both playbook allowlists empty.
- Add incident types to the Agent diagnosis selector only after Task 9's
  deterministic notification has been verified for that exact type.
- Return diagnosis, confidence, risk class, evidence refs, and one safe operator
  check; never nominate a target or business action.
- Capture Agent provider/tool/claim/diagnosis failures as explicit incidents.

**Step 4: Run focused tests and offline evaluation**

```bash
pytest -q tests/test_runtime_agent_worker.py tests/test_runtime_agent_policy.py tests/test_runtime_agent_executor.py
PYTHONPATH=src .venv/bin/python -m telegram_kol_research.cli \
  runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents
```

Expected: tests PASS; offline evaluation remains at its documented safety
thresholds with action authority false.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/runtime_incident_snapshot.py src/telegram_kol_research/runtime_agent_worker.py src/telegram_kol_research/runtime_agent_policy.py tests/test_runtime_agent_worker.py tests/test_runtime_agent_policy.py tests/test_runtime_agent_executor.py tests/test_runtime_agent_evaluation.py
git commit -m "feat: diagnose management target incidents read only"
```

### Task 11: Add dormant compliance scanner rules

**Files:**
- Modify: `src/telegram_kol_research/runtime_incident_rules.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `tests/test_runtime_incident_rules.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write failing dormant-rule tests**

Add rules for:

- terminal high-risk management target with no executable instruction item;
- admitted target whose item never reaches a terminal state by its deadline;
- target state inconsistent with the management batch;
- verified protection replacement missing required primary/backup roles.

Assert every new rule is `dormant_non_deployable`, absent from the production
allowlist, and produces no incident, Telegram claim, or Agent claim.

**Step 2: Run tests and verify failure**

```bash
pytest -q tests/test_runtime_incident_rules.py tests/test_runtime_incident_scanner.py -k 'management_target or position_compliance'
```

Expected: FAIL because the rules do not exist.

**Step 3: Implement only dormant catalog entries**

Add rule metadata and pure fact evaluators. Do not add production fact
projection, selector changes, or deployment wiring in the same turn. Update the
canonical status file with the exact phase, tests, and remaining shadow work.

**Step 4: Run focused scanner regression**

```bash
pytest -q tests/test_runtime_incident_rules.py tests/test_runtime_incident_scanner.py tests/test_runtime_incident_scanner_service.py
```

Expected: PASS with every new rule still non-deployable.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/runtime_incident_rules.py src/telegram_kol_research/runtime_incident_scanner.py tests/test_runtime_incident_rules.py tests/test_runtime_incident_scanner.py docs/runtime-incident-agent-status.md
git commit -m "feat: add dormant management target compliance rules"
```

### Task 12: Complete local regression and production-safe rollout records

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`
- Modify: `docs/runtime-incident-agent-runbook.md` only if a new operational
  command or rollback switch is introduced
- Test: all focused files changed above

**Step 1: Run the complete local focused suite**

```bash
pytest -q \
  tests/test_db_migrations.py \
  tests/test_multi_target_management.py \
  tests/test_management_directives.py \
  tests/test_context_resolution.py \
  tests/test_message_recognition.py \
  tests/test_message_instruction_items.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_contracts.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_worker.py \
  tests/test_composite_management_fault_injection.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_incidents.py \
  tests/test_system_operator_bot.py \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_agent_policy.py \
  tests/test_runtime_agent_executor.py \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py
```

Expected: PASS, except only previously documented unrelated skips/warnings.

**Step 2: Review scope and authority**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -12
```

Confirm unrelated dirty-worktree files remain untouched, no historical replay
path exists, Agent actions are false, and every selector is explicit.

**Step 3: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to the reviewed local HEAD.

**Step 4: Deploy only the currently approved dormant/shadow stage**

Use the existing helper from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Before restarting, prove no recognition/context/management/position-mutation or
runtime claim is in flight. If the safe window cannot be proven, stop after the
code update, leave the phase `in_progress`, and record the exact verification
still required.

**Step 5: Verify on the server without live orders**

Verify:

- deployed commit and editable package source parity;
- schema and focused tests;
- service HTTP health and listener continuity;
- projection switch state and live action allowlist;
- Agent read-only authority and empty action allowlists;
- target/incident/notification counts before and after the bounded check;
- no historical message replay and no new exchange mutation from verification;
- independent rollback by disabling only the new projection/action/selector.

**Step 6: Update canonical status and commit evidence**

Record exact deployed commit, flags, test counts, canary observations, rollback
result, and remaining work in `docs/runtime-incident-agent-status.md`.

```bash
git add docs/runtime-incident-agent-status.md docs/runtime-incident-agent-runbook.md
git commit -m "docs: record multi-target rollout verification"
git push origin codex/deepcoin-auto-trading-v1
```

Do not mark the broader rollout complete until every action and incident type
has passed its own dormant, shadow, deterministic-notification, and Agent
diagnosis gates.
