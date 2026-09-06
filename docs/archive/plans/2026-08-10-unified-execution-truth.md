# Unified Execution Truth Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every future executable Telegram instruction end with verified exchange evidence or a verified refusal, while preserving old records, multi-leg economics, exact-position ownership, and non-retryable unknown outcomes.

**Architecture:** Add an execution-contract state machine after authoritative candidate projection and before terminal instruction reporting. Keep MiMo, contextual targeting, `TradeSignal`, `ExecutionBinding`, and existing Deepcoin writers authoritative in their current domains; connect them through compatibility adapters, future-only shadow projection, evidence reconciliation, and staged live enforcement.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, Typer, pytest, existing Telegram/MiMo pipeline, existing Deepcoin REST writer/readback, Jinja templates, systemd deployment helpers.

---

## Implementation constraints

- Follow `@test-driven-development` for every behavior change and
  `@systematic-debugging` for every unexpected failure.
- Do not replay production Telegram history or place a synthetic live order.
- Do not let the new contract select strategies or targets.
- Do not retry `submit_unknown`, `unknown_exchange_outcome`, or any request that
  may already have reached Deepcoin.
- Keep every new runtime mode `disabled` by default and activate only above a
  fresh future watermark.
- Preserve the current local dirty worktree; stage only files named in the
  current task.
- Run syntax/unit checks locally, but perform final session, Deepcoin, service,
  and deployment verification on the server.
- Complete and review each task before starting the next one.  Do not combine
  the production promotions at the end of this plan.

### Task 1: Freeze the legacy impact inventory and characterization baseline

**Files:**
- Create: `docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md`
- Create: `tests/test_execution_truth_legacy_characterization.py`
- Inspect: `src/telegram_kol_research/authoritative_recognition.py`
- Inspect: `src/telegram_kol_research/message_instruction_items.py`
- Inspect: `src/telegram_kol_research/auto_trade_execution.py`
- Inspect: `src/telegram_kol_research/strategy_management_worker.py`
- Inspect: `src/telegram_kol_research/lifecycle_monitor.py`
- Inspect: `src/telegram_kol_research/recovery_live_submit.py`
- Inspect: `src/telegram_kol_research/source_message_deletion_worker.py`

**Step 1: Write the impact ledger**

Inventory every production reader/writer of these fields and result shapes:

```text
MessageInstructionItem.status
result["status"] / result["reason"] / result["submitted"]
TradeSignal.status
StrategyLifecycle.lifecycle_status / execution_binding_id
ExecutionBinding.status / last_exchange_status
ExecutionOrderLeg.status / attribution_status
```

For each call site, record one classification:

```text
authoritative_writer
compatibility_mirror
presentation_reader
monitor_only
retire_after_shadow
```

Include the introducing commits for adjacent-entry admission (`d168fdf`,
`b72f776`) and the P0 repair (`1269fa3`).  Record the two confirmed stale
admissions as anonymized regression shapes, not raw Telegram content.

**Step 2: Add characterization fixtures**

Build database fixtures with these exact state shapes:

```python
def test_deferred_item_can_currently_look_succeeded_without_exchange_proof(...):
    # item.status=succeeded, result.status=deferred,
    # pending assembly attempt, no TradeSignal, no binding
    ...

def test_price_entered_lifecycle_is_not_exchange_proof(...):
    # lifecycle entered from price monitoring, no execution_binding_id
    ...
```

The tests should describe the existing persisted shapes without asserting that
they are correct terminal outcomes.

**Step 3: Run the characterization tests**

Run:

```bash
uv run pytest -q tests/test_execution_truth_legacy_characterization.py
```

Expected: PASS and no exchange adapter calls.

**Step 4: Review the inventory against source search**

Run:

```bash
rg -n "MessageInstructionItem|lifecycle_status.*entered|TradeSignal.status|finish_message_instruction_item" src/telegram_kol_research
```

Expected: every match is classified in the impact ledger or explicitly marked
out of scope with a reason.

**Step 5: Commit**

```bash
git add docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md \
  tests/test_execution_truth_legacy_characterization.py
git commit -m "test: freeze execution truth legacy behavior"
```

### Task 2: Complete and harden the immediate adjacent-admission repair

**Files:**
- Modify: `src/telegram_kol_research/message_evidence.py`
- Modify: `src/telegram_kol_research/entry_assembly_admission.py`
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Test: `tests/test_message_evidence.py`
- Test: `tests/test_entry_assembly_admission.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_message_instruction_items.py`

**Step 1: Add failing evidence-contract tests**

Cover completed evidence with:

```python
@pytest.mark.parametrize(
    "strategy, expected_material",
    [
        (None, False),
        ({"symbol": None, "side": None, "entry": None}, False),
        ({"symbol": "", "side": "  "}, False),
        ({"symbol": "BTC", "side": None}, True),
    ],
)
def test_material_strategy_evidence(strategy, expected_material):
    assert has_material_strategy_evidence(strategy) is expected_material
```

Also require malformed normalized JSON to remain unresolved and require a real
non-`none` lifecycle event or entry fragment to remain actionable.

**Step 2: Run the narrow tests and verify the shared classifier is missing**

Run:

```bash
uv run pytest -q \
  tests/test_message_evidence.py \
  tests/test_entry_assembly_admission.py
```

Expected: FAIL because the current P0 helper is private to admission instead of
being the shared evidence contract.

**Step 3: Move the classifier to the evidence boundary**

Add to `message_evidence.py`:

```python
def has_material_strategy_evidence(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        str(field).strip()
        for field in value.values()
        if field is not None
    )
```

Import it from `entry_assembly_admission.py`.  Update the prompt contract so a
non-strategy response prefers `strategy: null` while retaining compatibility
with fixed all-null schemas.

**Step 4: Prove deferred work remains pending**

Keep `adjacent_entry_context_pending` in the single defer-reason registry and
assert end to end:

```python
assert result["status"] == "in_progress"
assert item.status == "pending"
assert session.query(TradeSignal).count() == 0
```

**Step 5: Run the focused regression suite**

Run:

```bash
uv run pytest -q \
  tests/test_message_evidence.py \
  tests/test_entry_assembly_admission.py \
  tests/test_auto_trade_execution.py \
  tests/test_message_instruction_items.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/message_evidence.py \
  src/telegram_kol_research/entry_assembly_admission.py \
  src/telegram_kol_research/prompt_defaults.py \
  tests/test_message_evidence.py \
  tests/test_entry_assembly_admission.py \
  tests/test_auto_trade_execution.py \
  tests/test_message_instruction_items.py
git commit -m "fix: centralize adjacent entry evidence semantics"
```

### Task 3: Add the execution-contract schema and additive migration

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `tests/test_instruction_execution_contract_models.py`
- Modify: `tests/test_db_migrations.py`

**Step 1: Write failing model tests**

Require one contract per instruction item, monotonic state versions, bounded
reason/evidence fields, and ordered immutable transitions.

```python
def test_instruction_item_has_one_execution_contract(session_factory):
    first = InstructionExecutionContract(message_instruction_item_id=item_id, ...)
    second = InstructionExecutionContract(message_instruction_item_id=item_id, ...)
    ...
    with pytest.raises(IntegrityError):
        session.commit()
```

**Step 2: Run tests to verify the models do not exist**

Run:

```bash
uv run pytest -q tests/test_instruction_execution_contract_models.py
```

Expected: collection failure for missing model names.

**Step 3: Add the models**

Add constants and models to `models.py`:

```python
INSTRUCTION_EXECUTION_STATES = frozenset({
    "pending", "deferred", "submitting", "submit_unknown",
    "verified", "failed", "expired",
})

class InstructionExecutionContract(Base):
    __tablename__ = "instruction_execution_contracts"
    # unique message_instruction_item_id
    # indexed state/deadline and strategy_instance_id
    # state_version defaults to 0
    # attempted_exchange_write defaults to False
    # evidence_refs_json defaults to []

class InstructionExecutionTransition(Base):
    __tablename__ = "instruction_execution_transitions"
    # unique (contract_id, state_version)
```

Use SQLAlchemy check constraints for the bounded state and terminal-kind
vocabularies.  Use foreign keys to `message_instruction_items`,
`trade_signals`, and `execution_bindings`; do not cascade-delete audit rows.

**Step 4: Add SQLite compatibility indexes**

`Base.metadata.create_all()` creates new tables.  Add the required indexes to
`SQLITE_COMPAT_INDEXES` so an existing SQLite production database converges to
the same schema on startup.

**Step 5: Add migration idempotency tests**

Run database bootstrap twice against a pre-contract database and assert the
same tables, indexes, and row counts after both runs.

**Step 6: Run model and migration tests**

```bash
uv run pytest -q \
  tests/test_instruction_execution_contract_models.py \
  tests/test_db_migrations.py
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  tests/test_instruction_execution_contract_models.py \
  tests/test_db_migrations.py
git commit -m "feat: add instruction execution contract ledger"
```

### Task 4: Implement the state machine with compare-and-set transitions

**Files:**
- Create: `src/telegram_kol_research/instruction_execution_contracts.py`
- Create: `tests/test_instruction_execution_contracts.py`

**Step 1: Write the transition matrix tests**

Parameterize every legal edge and representative illegal edges:

```python
@pytest.mark.parametrize("before,after", LEGAL_EDGES)
def test_transition_accepts_legal_edge(...): ...

@pytest.mark.parametrize("before,after", ILLEGAL_EDGES)
def test_transition_rejects_illegal_edge(...): ...
```

Require:

- exactly one transition row per successful state version;
- stale versions fail without changing either table;
- `submit_unknown` cannot return to `pending` or `submitting`;
- `verified/failed/expired` are immutable;
- evidence JSON is structured and bounded;
- a transition to `submitting` sets `attempted_exchange_write=True` only at
  the actual writer boundary, not during planning.

**Step 2: Run tests to verify failure**

```bash
uv run pytest -q tests/test_instruction_execution_contracts.py
```

Expected: FAIL because the transition service does not exist.

**Step 3: Implement the API**

Expose narrow functions:

```python
def load_or_create_instruction_execution_contract(...): ...

def transition_instruction_execution_contract(
    session_factory,
    *,
    contract_id: int,
    expected_state: str,
    expected_version: int,
    new_state: str,
    reason_code: str,
    evidence_refs: list[dict[str, object]],
    transitioned_at: datetime,
    attempted_exchange_write: bool | None = None,
    trade_signal_id: int | None = None,
    execution_binding_id: int | None = None,
    terminal_kind: str | None = None,
    completion_scope: str | None = None,
): ...
```

Perform the contract update with one SQL compare-and-set and append the
transition in the same transaction.

**Step 4: Run the tests**

```bash
uv run pytest -q tests/test_instruction_execution_contracts.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/instruction_execution_contracts.py \
  tests/test_instruction_execution_contracts.py
git commit -m "feat: enforce instruction execution state transitions"
```

### Task 5: Add dormant settings and future-only projection

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Create: `src/telegram_kol_research/instruction_execution_projection.py`
- Modify: `tests/test_trading_settings.py`
- Create: `tests/test_instruction_execution_projection.py`
- Modify: `tests/test_authoritative_recognition.py`

**Step 1: Write fail-closed settings tests**

Add settings:

```text
instruction_execution_contract_mode = disabled | shadow | live
instruction_execution_entry_after_item_id = nonnegative integer
instruction_execution_management_after_item_id = nonnegative integer
```

Require defaults of `disabled`, reject unknown modes/negative watermarks, and
round-trip all unrelated current settings without loss.

**Step 2: Write projector tests**

Require the projector to:

- do nothing while disabled;
- create a `pending` contract in shadow mode only above the entry watermark;
- create no contract for retired or non-authoritative items;
- never create candidates or change target lifecycle IDs;
- remain idempotent under repeated authoritative processing.

**Step 3: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_trading_settings.py \
  tests/test_instruction_execution_projection.py \
  tests/test_authoritative_recognition.py
```

**Step 4: Implement the dormant projector**

Call the projector only after authoritative candidate/item persistence commits.
Use the item ID watermark, not message time.  Store the authoritative item and
candidate IDs as references; do not copy raw text.

**Step 5: Run tests**

Expected: PASS, with zero Deepcoin calls in every shadow test.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/models.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/instruction_execution_projection.py \
  tests/test_trading_settings.py \
  tests/test_instruction_execution_projection.py \
  tests/test_authoritative_recognition.py
git commit -m "feat: project future instruction contracts in shadow"
```

### Task 6: Centralize legacy result interpretation and fail closed

**Files:**
- Create: `src/telegram_kol_research/instruction_execution_outcomes.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Create: `tests/test_instruction_execution_outcomes.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_strategy_management_worker.py`
- Modify: `tests/test_message_instruction_items.py`

**Step 1: Write exhaustive outcome tests**

Define a typed adapter result:

```python
@dataclass(frozen=True, slots=True)
class InstructionOutcome:
    state: Literal[
        "deferred", "submitting", "submit_unknown",
        "verified", "failed", "expired",
    ]
    reason_code: str
    terminal_kind: str | None = None
    attempted_exchange_write: bool = False
```

Require explicit mappings for every currently emitted status/reason pair.
Unrecognized status, missing status, and contradictory combinations must raise
`InstructionOutcomeContractError`; they must never map to success.

**Step 2: Run tests to prove the default-success behavior fails**

```bash
uv run pytest -q tests/test_instruction_execution_outcomes.py
```

Expected: FAIL against `_instruction_finish_status()` returning `succeeded` for
unknown values.

**Step 3: Implement one adapter registry**

Move all defer reasons and terminal mappings into one module.  Keep a narrow
compatibility converter for `MessageInstructionItem.status`:

```text
deferred -> pending
submitting -> executing
submit_unknown -> unknown
verified -> submitted/succeeded according to terminal evidence
failed/expired -> failed
```

**Step 4: Replace direct dictionary interpretation**

Update auto-entry and management workers to call the adapter.  In shadow mode,
record divergence without changing legacy behavior.  In live mode, an unknown
adapter value is a deterministic failure plus incident, not `succeeded`.

**Step 5: Run focused tests**

```bash
uv run pytest -q \
  tests/test_instruction_execution_outcomes.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_worker.py \
  tests/test_message_instruction_items.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/instruction_execution_outcomes.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/message_instruction_items.py \
  tests/test_instruction_execution_outcomes.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_worker.py \
  tests/test_message_instruction_items.py
git commit -m "fix: fail closed on unknown instruction outcomes"
```

### Task 7: Make adjacent deferrals deadline-bound and self-reconciling

**Files:**
- Modify: `src/telegram_kol_research/entry_assembly_admission.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Create: `src/telegram_kol_research/entry_admission_reconciler.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_entry_assembly_admission.py`
- Create: `tests/test_entry_admission_reconciler.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: Write typed-decision tests**

Extend `EntryAdmissionDecision` with exact blocker IDs, deadline, and recheck
fingerprint.  Require a deferred decision to contain all three.

**Step 2: Write lost-wakeup tests**

Persist a pending attempt whose blocker is already terminal, do not invoke the
completion callback, then run one reconciler tick.  Assert the exact item is
made claimable once and no unrelated item changes.

Also test:

- not-yet-due attempts remain untouched;
- expired attempts transition to expired/failed without an exchange call;
- `submit_unknown` contracts are excluded;
- repeated ticks are idempotent;
- the historical stale shape produces an incident but no order.

**Step 3: Implement the due reconciler**

The reconciler re-runs structured source-fact classification, not raw-text
inference.  It may release a pending item or expire a contract; it must never
call `auto_process_message_trade_signal` for a row older than its execution
deadline.

**Step 4: Wire the bounded periodic tick**

Run it from the existing operator loop with a small bounded batch.  Keep the
event-driven wake path for low latency.

**Step 5: Run tests**

```bash
uv run pytest -q \
  tests/test_entry_assembly_admission.py \
  tests/test_entry_admission_reconciler.py \
  tests/test_system_operator_bot.py
```

Expected: PASS and all fakes report zero exchange writes for stale/expired rows.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/entry_assembly_admission.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/entry_admission_reconciler.py \
  src/telegram_kol_research/system_operator_bot.py \
  tests/test_entry_assembly_admission.py \
  tests/test_entry_admission_reconciler.py \
  tests/test_system_operator_bot.py
git commit -m "feat: reconcile bounded deferred entries"
```

### Task 8: Shadow the complete entry submission and evidence chain

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/trade_signals.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Create: `src/telegram_kol_research/instruction_execution_entry_adapter.py`
- Create: `tests/test_instruction_execution_entry_adapter.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write end-to-end shadow tests**

Cover:

```text
pending -> deferred
pending -> submitting -> verified_entry
pending -> submitting -> failed with attempted_writes=0
pending -> submitting -> submit_unknown
two legs both verified
first leg verified, second confirmed absent -> verified partial + incident fact
```

Assert shadow mode never changes the number or payload of Deepcoin calls made by
the legacy path.

**Step 2: Implement pre-submit transition evidence**

Immediately before the existing writer call, persist the contract transition
and the selected draft fingerprint.  Link the resulting TradeSignal ID.

**Step 3: Implement post-submit evidence projection**

After existing durable writer logic finishes, load the exact TradeSignal,
binding, selected order legs, and protection rows.  Transition to `verified`
only if evidence satisfies the terminal kind.

**Step 4: Preserve unknown-outcome behavior**

Map `DeepcoinRequestOutcomeUnknown`, `unknown_exchange_outcome`, and ambiguous
partial results to `submit_unknown`.  Prove the adapter never calls the writer
again during reconciliation.

**Step 5: Run focused tests**

```bash
uv run pytest -q \
  tests/test_instruction_execution_entry_adapter.py \
  tests/test_recovery_live_submit.py \
  tests/test_execution_bindings.py \
  tests/test_auto_trade_execution.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/trade_signals.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/instruction_execution_entry_adapter.py \
  tests/test_instruction_execution_entry_adapter.py \
  tests/test_recovery_live_submit.py \
  tests/test_execution_bindings.py \
  tests/test_auto_trade_execution.py
git commit -m "feat: shadow verified entry execution contracts"
```

### Task 9: Preserve original multi-leg economics in operator recovery

**Files:**
- Create: `src/telegram_kol_research/entry_draft_revisions.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_entry_draft_revisions.py`
- Modify: `tests/test_deepcoin_order_builder.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write the Chen-style regression first**

Create an original BTC long range draft with two entry legs and a 20 USDT risk
budget.  Apply a `market_first_leg` revision and assert:

```python
assert len(revised["order_legs"]) == 2
assert revised["order_legs"][0]["order_type"] == "market"
assert revised["order_legs"][1] == original["order_legs"][1]
assert revised["risk_budget_usdt"] == original["risk_budget_usdt"]
assert sum(leg["risk_budget_usdt"] for leg in revised["order_legs"]) <= 20
```

Add negative tests proving a revision cannot:

- collapse two legs into one;
- duplicate a client order ID;
- increase aggregate risk;
- change stop-loss/take-profit without explicit authority;
- submit a second leg after the contract deadline;
- operate when any original leg has an unknown outcome.

**Step 2: Run tests and verify current single-market reconstruction fails**

```bash
uv run pytest -q tests/test_entry_draft_revisions.py
```

Expected: FAIL because current manual market recovery builds a new one-leg
100-percent draft.

**Step 3: Implement immutable draft revision**

Expose:

```python
def revise_entry_draft(
    original_draft: dict[str, object],
    *,
    operation: Literal["market_first_leg", "market_due_legs"],
    market_price: Decimal,
    authorized_leg_indices: tuple[int, ...],
) -> dict[str, object]: ...
```

Validate aggregate risk and preserve the original draft fingerprint as parent
evidence.  Assign new revision/client IDs only to explicitly changed legs.

**Step 4: Add a dry-run-first CLI path**

The command prints original vs revised leg mappings, risk, deadline, and exact
blocking reasons.  Apply requires an unchanged fingerprint and explicit leg
indices.  It must use the existing audited writer.

**Step 5: Run tests**

```bash
uv run pytest -q \
  tests/test_entry_draft_revisions.py \
  tests/test_deepcoin_order_builder.py \
  tests/test_recovery_live_submit.py \
  tests/test_cli_smoke.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/entry_draft_revisions.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/deepcoin_order_builder.py \
  src/telegram_kol_research/cli.py \
  tests/test_entry_draft_revisions.py \
  tests/test_deepcoin_order_builder.py \
  tests/test_recovery_live_submit.py \
  tests/test_cli_smoke.py
git commit -m "fix: preserve multi-leg recovery economics"
```

### Task 10: Add exact readback reconciliation and contradiction facts

**Files:**
- Create: `src/telegram_kol_research/instruction_execution_reconciliation.py`
- Create: `tests/test_instruction_execution_reconciliation.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: Write read-only reconciliation tests**

Use fake Deepcoin clients to cover:

- exact client order ID found in history;
- exact `posId` found and binding verified;
- order confirmed absent after no write;
- duplicate matching rows;
- incomplete positions/orders/TPSL snapshot;
- stale submitting lease;
- unknown outcome that later becomes verified;
- unknown outcome that later becomes confirmed absent.

Require duplicate/incomplete evidence to remain `submit_unknown`.

**Step 2: Implement one bounded reconciler tick**

Read candidate contracts by state/deadline, then query only the exact instrument
and IDs.  The reconciler may transition states but must never call a mutation
method.

**Step 3: Emit structured contradiction facts**

Return bounded facts such as:

```text
deferred_overdue
submitting_stale
verified_without_binding
binding_without_verified_contract
lifecycle_entered_without_binding
multi_leg_partial
exchange_snapshot_incomplete
```

**Step 4: Wire the tick in read-only mode**

Add it to the operator loop behind the execution-contract mode and a bounded
batch size.

**Step 5: Run tests**

```bash
uv run pytest -q \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_system_operator_bot.py
```

Expected: PASS and mutation-call counters remain zero.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/instruction_execution_reconciliation.py \
  src/telegram_kol_research/system_operator_bot.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_system_operator_bot.py
git commit -m "feat: reconcile instruction execution evidence"
```

### Task 11: Separate price lifecycle from execution presentation

**Files:**
- Create: `src/telegram_kol_research/execution_state_projection.py`
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/strategy_alerts.py`
- Modify: `src/telegram_kol_research/templates/_strategy_mid_panel.html`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`
- Create: `tests/test_execution_state_projection.py`
- Modify: `tests/test_lifecycle_monitor.py`
- Modify: `tests/test_web_queries.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_strategy_records.py`
- Modify: `tests/test_strategy_alerts.py`

**Step 1: Write projection tests**

Require these user-facing states:

```text
price touched, no binding -> 价格触发，未提交交易所订单
deferred contract -> 等待相邻消息确认
submitting -> 正在提交交易所
submit_unknown -> 交易所结果待核对，禁止重试
verified binding + live position -> 持仓中
verified refusal -> 已明确拒绝，未下单
contradiction -> 执行状态异常
```

**Step 2: Keep lifecycle writes analytical**

Do not remove historical `entered` transitions yet.  Add or expose a
`price_touched` projection and ensure no presentation reader treats `entered`
alone as exchange proof.

**Step 3: Implement one shared projection**

The web page, records API, and Telegram alerts must call the same projection
function.  It reads contract/binding/leg evidence; it does not write state.

**Step 4: Run tests**

```bash
uv run pytest -q \
  tests/test_execution_state_projection.py \
  tests/test_lifecycle_monitor.py \
  tests/test_web_queries.py \
  tests/test_web_app.py \
  tests/test_strategy_records.py \
  tests/test_strategy_alerts.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/execution_state_projection.py \
  src/telegram_kol_research/lifecycle_monitor.py \
  src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/strategy_alerts.py \
  src/telegram_kol_research/templates/_strategy_mid_panel.html \
  src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html \
  src/telegram_kol_research/templates/strategy_record_detail.html \
  tests/test_execution_state_projection.py \
  tests/test_lifecycle_monitor.py \
  tests/test_web_queries.py \
  tests/test_web_app.py \
  tests/test_strategy_records.py \
  tests/test_strategy_alerts.py
git commit -m "fix: present exchange execution truth"
```

### Task 12: Feed contradictions to monitors without granting authority

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/runtime_incident_rules.py`
- Modify: `src/telegram_kol_research/message_operation_supervisor.py`
- Modify: `src/telegram_kol_research/runtime_incident_snapshot.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_message_operation_supervisor.py`
- Modify: `tests/test_runtime_incident_snapshot.py`

**Step 1: Write monitor tests for each contradiction fact**

Require high-signal incidents only for future/live contracts or exact historical
contradictions.  Stable legacy unproven rows should deduplicate, not alert every
timer tick.

**Step 2: Prove the Runtime Incident Agent remains read-only**

Tests must assert its registry still contains no order submit/cancel/position
mutation capability and that action authority remains false.

**Step 3: Add bounded adapters and Chinese notification templates**

Include contract/item/message IDs and reason codes only.  Exclude raw message
text, API payloads, `posId`, credentials, and unbounded errors.

**Step 4: Run tests**

```bash
uv run pytest -q \
  tests/test_production_safety_monitor.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_message_operation_supervisor.py \
  tests/test_runtime_incident_snapshot.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/runtime_incident_scanner.py \
  src/telegram_kol_research/runtime_incident_rules.py \
  src/telegram_kol_research/message_operation_supervisor.py \
  src/telegram_kol_research/runtime_incident_snapshot.py \
  tests/test_production_safety_monitor.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_message_operation_supervisor.py \
  tests/test_runtime_incident_snapshot.py
git commit -m "feat: detect unverified execution outcomes"
```

### Task 13: Enforce contracts for future entry messages

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Modify: `src/telegram_kol_research/instruction_execution_entry_adapter.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_message_instruction_items.py`
- Modify: `tests/test_instruction_execution_entry_adapter.py`

**Step 1: Write live-mode enforcement tests**

Above the watermark, assert:

- an item cannot finish `succeeded` without a verified terminal contract;
- a deferred contract mirrors to pending;
- a verified refusal mirrors to succeeded with refusal evidence;
- submit unknown mirrors to unknown and is not claimable again;
- items below the watermark preserve legacy behavior;
- shadow mode records divergence but does not change execution behavior.

**Step 2: Implement the live terminal gate**

At `finish_message_instruction_item`, load the active contract in live mode.
Reject a success mirror unless the contract is `verified` with a terminal kind.
Do not infer verification from the result dictionary.

**Step 3: Run focused tests**

```bash
uv run pytest -q \
  tests/test_auto_trade_execution.py \
  tests/test_message_instruction_items.py \
  tests/test_instruction_execution_entry_adapter.py
```

Expected: PASS.

**Step 4: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/message_instruction_items.py \
  src/telegram_kol_research/instruction_execution_entry_adapter.py \
  tests/test_auto_trade_execution.py \
  tests/test_message_instruction_items.py \
  tests/test_instruction_execution_entry_adapter.py
git commit -m "feat: require verified entry terminal evidence"
```

### Task 14: Adapt management, revision, cancellation, exit, and deletion paths

**Files:**
- Create: `src/telegram_kol_research/instruction_execution_management_adapter.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/entry_revision_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/source_message_deletion_worker.py`
- Create: `tests/test_instruction_execution_management_adapter.py`
- Modify: `tests/test_strategy_management_worker.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_entry_revision_executor.py`
- Modify: `tests/test_deepcoin_execution_actions.py`
- Modify: `tests/test_source_message_deletion_worker.py`

**Step 1: Write one adapter test per management terminal kind**

Cover verified management, cancel, exit, protection, refusal, partial,
recovery-required, and unknown exchange outcomes.  Preserve current exact
targeting and collision isolation.

**Step 2: Add shadow adapters first**

Map existing immutable management batches, components, mutations, and execution
events to the contract.  Do not change planner or writer authority.

**Step 3: Run shadow divergence fixtures**

Replay captured immutable inputs with fake writers for Chen, Miya, Sanjie, and
Feiyang scenarios.  Require no unexplained state divergence.

**Step 4: Add future-only live enforcement**

Apply the management watermark only after shadow tests pass.  Preserve all
existing `submit_unknown/recovery_required` non-retry behavior.

**Step 5: Run focused tests**

```bash
uv run pytest -q \
  tests/test_instruction_execution_management_adapter.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_executor.py \
  tests/test_entry_revision_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_source_message_deletion_worker.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/instruction_execution_management_adapter.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/entry_revision_executor.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/source_message_deletion_worker.py \
  tests/test_instruction_execution_management_adapter.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_executor.py \
  tests/test_entry_revision_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_source_message_deletion_worker.py
git commit -m "feat: verify management instruction outcomes"
```

### Task 15: Replace the informal deployment gate with a deterministic preflight

**Files:**
- Create: `src/telegram_kol_research/deployment_preflight.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `deploy/telegram-kol-update`
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Create: `tests/test_deployment_preflight.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `docs/server-deployment.md`
- Modify: `docs/runbook.md`

**Step 1: Write policy tests**

Model change classes:

```text
code
schema_compatible
execution_writer
live_promotion
```

Require deterministic classification:

```python
assert classify(fresh_active_exchange_write) == "BLOCK"
assert classify(protected_open_position) == "WARN"
assert classify(historical_unknown_row) == "WARN"
assert classify(incomplete_exchange_snapshot, change="execution_writer") == "BLOCK"
assert classify(incomplete_exchange_snapshot, change="code") == "WARN"
```

Also require additive migration backup/preflight for `schema_compatible` and
reviewed shadow evidence plus explicit authorization for `live_promotion`.

**Step 2: Define the JSON contract and exit codes**

```text
0 = PASS
2 = WARN (deployment allowed)
3 = BLOCK
4 = malformed/incomplete preflight (deployment refused)
```

Emit expected commit, change class, database watermark, checked facts, reasons,
creation/expiry times, and a SHA-256 fingerprint.  Emit no secrets, raw
messages, order payloads, or position IDs.

**Step 3: Implement read-only collection**

Inspect fresh active leases/claims across TradeSignal, execution contracts,
management batches/components, position mutations, closes, protection/rescue,
and source deletion.  Distinguish fresh active work from durable historical
residue.

**Step 4: Add the CLI**

```bash
.venv/bin/telegram-kol-research deployment-preflight \
  --database-path data/research.db \
  --expected-commit <sha> \
  --change-class code \
  --output /run/telegram-kol/deployment-preflight.json
```

Default to no exchange access.  `execution_writer` and `live_promotion` use the
existing read-only Deepcoin snapshot and require complete stable evidence.

**Step 5: Version the server helper**

Store the reviewed `/usr/local/bin/telegram-kol-update` source in
`deploy/telegram-kol-update`.  Before `git pull`/restart, require the expected
commit/change class, run preflight, and validate its unexpired fingerprint.
Abort on `BLOCK` or malformed output.  Print `WARN` reasons and continue only
when the preflight contract says warnings are deployable.

**Step 6: Update workstation helpers and docs**

Require `EXPECTED_COMMIT` and `CHANGE_CLASS` parameters rather than hiding the
deployment class in an operator judgment.

**Step 7: Run tests**

```bash
uv run pytest -q \
  tests/test_deployment_preflight.py \
  tests/test_cli_smoke.py \
  tests/test_server_update_scripts.py
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/deployment_preflight.py \
  src/telegram_kol_research/cli.py \
  deploy/telegram-kol-update \
  scripts/server_git_update.sh \
  scripts/server_git_update.ps1 \
  tests/test_deployment_preflight.py \
  tests/test_cli_smoke.py \
  tests/test_server_update_scripts.py \
  docs/server-deployment.md \
  docs/runbook.md
git commit -m "feat: make deployment preflight deterministic"
```

### Task 16: Add migration, crash, replay, and full regression gates

**Files:**
- Create: `tests/test_instruction_execution_fault_injection.py`
- Create: `tests/test_instruction_execution_replay.py`
- Create: `tests/fixtures/instruction_execution/`
- Modify: `tests/test_historical_state_repair.py`
- Modify: `docs/runbook.md`

**Step 1: Add crash-boundary fault injection**

Test crashes:

```text
before contract transition
after submitting transition, before HTTP
after HTTP send, before response
after accepted response, before local commit
between first and second entry legs
after position creation, before protection commit
after lost wakeup
during restart reconciliation
```

Require no duplicate client order IDs and no automatic retry after any possible
write.

**Step 2: Add replay corpus**

Use redacted immutable fixtures for:

- fixed all-null non-strategy neighbors;
- real half-size preamble before a strategy;
- following sizing fragment;
- two-leg eager limit;
- hybrid market plus limit;
- multi-instruction entry/management;
- old lifecycle without an instruction item;
- partial and unknown exchange outcomes.

Assert draft fingerprints, risk, target identity, contract outcome, and exchange
write count.

**Step 3: Test production-snapshot migration**

Use a sanitized copy with representative legacy states.  Bootstrap twice,
project read-only legacy truth, and verify no historical TradeSignal,
ExecutionBinding, execution event, or order draft is created.

**Step 4: Run focused and full suites**

```bash
uv run pytest -q tests/test_instruction_execution_fault_injection.py
uv run pytest -q tests/test_instruction_execution_replay.py
uv run pytest -q
```

Expected: all tests PASS.  Any unrelated baseline failure must be documented and
resolved before deployment; do not waive execution or migration failures.

**Step 5: Commit**

```bash
git add tests/test_instruction_execution_fault_injection.py \
  tests/test_instruction_execution_replay.py \
  tests/fixtures/instruction_execution \
  tests/test_historical_state_repair.py \
  docs/runbook.md
git commit -m "test: cover execution truth failures and legacy replay"
```

### Task 17: Review, push, and deploy P0 plus dormant schema only

**Files:**
- Review: all changes from Tasks 1-16
- Update: `docs/runbook.md`
- Update: `docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md`

**Step 1: Request code review**

Use `@requesting-code-review`.  Review specifically for:

- accidental target selection or Runtime Agent authority;
- unknown-outcome retries;
- aggregate risk drift;
- historical replay/backfill;
- old-reader compatibility;
- migration and rollback safety;
- deployment preflight false blockers and false passes.

**Step 2: Run the final local gate**

```bash
git diff --check
uv run pytest -q
git status --short
```

Expected: clean diff checks, full suite PASS, and only known unrelated user files
outside the staged implementation scope.

**Step 3: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Run server preflight for the reviewed change class**

First deploy only P0 and the additive dormant schema/settings.  Use
`schema_compatible`; do not enable shadow or live.

Require:

- expected server/database backup succeeds;
- no fresh active exchange write or position mutation;
- migration dry run succeeds;
- protected open positions are reported as warnings, not blockers;
- existing historical stale admissions remain unchanged;
- current execution-contract mode reads back `disabled`.

**Step 5: Deploy through the reviewed helper**

Use the project helper with exact expected commit and change class.  Confirm:

```text
deployed SHA matches
editable package points at the checkout
telegram-kol.service is active
HTTP returns 200
listener checkpoint advances on the next natural message
new contract tables exist and contain zero production rows while disabled
no new TradeSignal, binding, mutation, or exchange event was created at activation
```

Do not replay `#4171` or `#9974`.

**Step 6: Run deployed focused tests and no-notify diagnostics**

Run the model, state-machine, settings, adjacent-admission, preflight, and
monitor tests on the server.  Use fake/read-only adapters only.

**Step 7: Record deployment evidence and commit documentation**

Record bounded facts: commit, mode, watermarks, table counts, test counts,
service health, monitor result, and rollback command.  Do not record secrets or
exchange identifiers.

```bash
git add docs/runbook.md \
  docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md
git commit -m "docs: record dormant execution truth deployment"
git push origin codex/deepcoin-auto-trading-v1
```

### Task 18: Promote future-only shadow observation

**Files:**
- Update: `docs/runbook.md`
- Update: `docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md`

**Step 1: Establish a fresh future watermark**

In a separate approved operation, record the current maximum
`MessageInstructionItem.id`.  Verify all existing contracts remain absent or
historical read-only projections.

**Step 2: Enable shadow mode only above the watermark**

Change only:

```text
instruction_execution_contract_mode=shadow
instruction_execution_entry_after_item_id=<fresh max item id>
```

Keep the management watermark inactive.

**Step 3: Observe natural future messages**

For at least one complete observation period, compare:

- legacy item result;
- contract state;
- TradeSignal/binding/leg evidence;
- exchange write count;
- presentation state;
- contradiction facts.

Shadow mode must not change execution calls or payloads.

**Step 4: Review divergence**

Any unexplained divergence returns the mode to `disabled`.  Fix and repeat
shadow; do not promote with a waiver.

**Step 5: Record evidence**

Commit only bounded documentation.  Do not include raw messages, order payloads,
position IDs, or credentials.

### Task 19: Promote entry enforcement, then management enforcement separately

**Files:**
- Update: `docs/runbook.md`
- Update: `docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md`

**Step 1: Obtain explicit approval for entry live mode**

Require reviewed shadow evidence with zero unexplained divergence, healthy
monitoring, exact rollback, and a fresh deterministic `live_promotion`
preflight.

**Step 2: Enable entry enforcement above a new watermark**

Change only entry settings.  Observe the first natural eligible entries without
creating a synthetic production trade.

**Step 3: Verify each natural entry**

Confirm contract, TradeSignal, binding, each entry leg, `posId`, and protection
remain one-to-one.  A deferred entry must remain pending and either resume or
expire within its deadline.

**Step 4: Keep management in shadow**

Do not promote management in the same operation.  Collect a separate complete
management observation period covering modify stop, take profit, cancel, exit,
and deletion paths.

**Step 5: Obtain separate approval and promote management**

Use a new watermark and a fresh `live_promotion` preflight.  Preserve the exact
rollback switches and additive history.

**Step 6: Retire old readers only after stability**

Remove one duplicated reader/decision at a time, starting with UI fallbacks and
ending with legacy execution-status interpretation.  After each removal, run
the focused and full suites and observe another stable period.  Do not drop
legacy columns or tables in this plan.

## Completion criteria

- Production includes the P0 fix and cannot repeat the fixed-null admission
  stall for future messages.
- Every future executable instruction above the live watermark has exactly one
  contract.
- No deferred or unknown result can be mirrored as successful.
- Lost wakeups reconcile within the bounded deadline.
- Manual market recovery preserves original legs and aggregate risk.
- Price lifecycle alone is never presented as a real exchange holding.
- Monitor and Runtime Incident Agent detect contradictions without mutation
  authority.
- Deployment helpers use deterministic change-class-aware preflight results.
- Historical rows remain intact and are never automatically replayed.
- Entry and management promotions are separately reviewed, authorized, and
  reversible.
