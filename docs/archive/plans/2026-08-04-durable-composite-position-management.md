# Durable Composite Position Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute authoritative partial-take-profit plus protection instructions completely and durably, consuming the first TP stage, converging the exact close delta, and replacing remaining protection without duplicate writes or an unprotected window.

**Architecture:** Persist a versioned immutable management contract on the authoritative candidate and batch, then execute three durable components under the existing strategy lock: consume the first TP stage, converge the partial close, and replace remaining protection. Reuse exact position ownership, mutation intents, and exchange reconciliation; add component-level state and evidence so restart and unknown outcomes resume safely instead of flattening or abandoning the instruction.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, Deepcoin REST client, systemd production service.

---

## Global constraints

- Keep first-pass recognition and contextual target resolution authoritative.
- MiMo remains the only recognition authority; DeepSeek remains advisory.
- Never infer position or TPSL ownership from symbol, side, price, size, or time.
- Never retry an exchange-unknown write until exact exchange evidence resolves it.
- Never add position to compensate for an over-reduced result.
- Never cancel the last verified stop before replacement stops are read back and owned.
- Do not replay historical Miya or Sanjie instructions automatically.
- Introduce the feature dormant, validate in shadow, and enable live only in a proven safe server window.

### Task 1: Persist versioned instruction contracts and component state

**Files:**
- Modify: `src/telegram_kol_research/models.py:512-575`
- Modify: `src/telegram_kol_research/models.py:1008-1135`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/strategy_management_contracts.py`
- Create: `src/telegram_kol_research/strategy_management_components.py`
- Test: `tests/test_db_migrations.py`
- Create: `tests/test_strategy_management_contracts.py`
- Create: `tests/test_strategy_management_components.py`

**Step 1: Write failing migration tests**

Assert that an existing database gains:

```text
signal_candidates.management_contract_json
signal_candidates.management_contract_fingerprint
strategy_management_batches.management_contract_json
strategy_management_batches.management_contract_fingerprint
strategy_management_batches.contract_version
strategy_management_components
```

The new component table must include:

```python
management_batch_id
strategy_management_leg_id  # nullable for batch-wide components
component_kind
sequence
status
idempotency_key
desired_json
evidence_json
reason_code
attempt_count
last_progress_at
execution_deadline_at
created_at
updated_at
completed_at
```

Add unique indexes on `idempotency_key` and on
`(management_batch_id, strategy_management_leg_id, component_kind)`.

**Step 2: Run the migration tests to verify RED**

Run:

```bash
uv run pytest -q tests/test_db_migrations.py -k composite_management
```

Expected: failure because the columns and table do not exist.

**Step 3: Add models, migrations, and immutable serializers**

Implement a frozen `ManagementInstructionContract` with canonical decimal strings and stable sorted JSON. Provide:

```python
def serialize_management_contract(contract: ManagementInstructionContract) -> str: ...
def management_contract_fingerprint(contract: ManagementInstructionContract) -> str: ...
def load_management_contract(value: str) -> ManagementInstructionContract: ...
```

Reject unknown versions, duplicate required components, fractions outside `(0, 1]`, and an explicit stop without `current_message_text` provenance.

**Step 4: Add component state-transition tests**

Cover compare-and-set transitions, immutable idempotency keys, one active component per sequence, restart claiming, and refusal to reclaim `awaiting_exchange`, `confirmed`, or `operator_required` as a new write.

**Step 5: Run focused tests**

```bash
uv run pytest -q tests/test_db_migrations.py tests/test_strategy_management_contracts.py tests/test_strategy_management_components.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/strategy_management_contracts.py \
  src/telegram_kol_research/strategy_management_components.py \
  tests/test_db_migrations.py tests/test_strategy_management_contracts.py \
  tests/test_strategy_management_components.py
git commit -m "feat: persist composite management contracts"
```

### Task 2: Extract all management clauses before choosing an action

**Files:**
- Modify: `src/telegram_kol_research/management_directives.py:85-275`
- Modify: `src/telegram_kol_research/message_recognition.py:1057-1345`
- Modify: `src/telegram_kol_research/message_recognition.py:1832-1993`
- Test: `tests/test_management_directives.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Add exact Miya and Sanjie failing unit tests**

```python
def test_miya_partial_with_explicit_stop_preserves_all_components():
    contract = build_management_instruction_contract(
        text="BTC多单目前浮盈1100点，止盈50%，剩余仓位止损位移动至62700，做无风险持仓",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "stop_loss": "62700",
            "symbol": "BTC",
            "side": "long",
        },
    )
    assert contract.close_fraction == "0.5"
    assert contract.stop_mode == "explicit_price"
    assert contract.stop_price == "62700"
    assert contract.take_profit_consumption == "consume_first_stage"


def test_sanjie_partial_to_entry_preserves_all_components():
    contract = build_management_instruction_contract(
        text="比特币多单止盈50%，止损位移动至开仓价！",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "symbol": "BTC",
            "side": "long",
        },
    )
    assert contract.close_fraction == "0.5"
    assert contract.stop_mode == "actual_entry_price"
```

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_management_directives.py -k 'miya or sanjie'
```

Expected: Miya resolves to stop-only and Sanjie loses the protection component.

**Step 3: Replace early returns with component collection**

Collect full-exit, cancel-entry, partial fraction, explicit stop, cost protection, TP consumption, deferred-entry cancellation, and risk-increasing clauses first. Apply precedence only after collection:

- risk-increasing mixed with risk reduction: block;
- full exit: terminal action family, protection changes become irrelevant;
- partial plus stop: composite v2 contract;
- partial only or stop only: retain compatible legacy actions;
- ambiguous fractions or stop provenance: block before persistence.

Keep `resolve_management_directive()` as a compatibility adapter derived from the new contract. Do not duplicate text parsing.

**Step 4: Persist the immutable contract on the authoritative candidate**

Update candidate upserts so the exact contract JSON and fingerprint are written with the recognition generation. Refuse an in-place overwrite when an item or batch already references a different fingerprint.

**Step 5: Add integration assertions**

For both production messages, assert one authoritative candidate with:

```text
management_action = partial_then_break_even
management_fraction = 0.5
management_contract_fingerprint != NULL
required_components = all three v2 components
```

**Step 6: Run focused tests and commit**

```bash
uv run pytest -q tests/test_management_directives.py tests/test_message_recognition.py -k 'management or lifecycle'
git add src/telegram_kol_research/management_directives.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_management_directives.py tests/test_message_recognition.py
git commit -m "fix: preserve composite management clauses"
```

### Task 3: Add a composite-only disabled, shadow, and live gate

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing settings tests**

Assert:

```python
assert defaults.composite_management_v2_mode == "disabled"
```

Cover only `disabled`, `shadow`, and `live`; reject booleans and arbitrary strings. `live` must require the existing global automatic-trading and management-live gates.

**Step 2: Implement the setting and API/UI readback**

Semantics:

- `disabled`: persist the composite contract, block exchange writes with `composite_management_v2_disabled`, and never fall back to the lossy legacy path.
- `shadow`: plan components and snapshots with zero exchange writes.
- `live`: permit reviewed v2 execution.

Do not change single-action management behavior.

**Step 3: Verify and commit**

```bash
uv run pytest -q tests/test_trading_settings.py tests/test_web_app.py -k composite_management
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js src/telegram_kol_research/web_app.py \
  tests/test_trading_settings.py tests/test_web_app.py
git commit -m "feat: gate composite management v2"
```

### Task 4: Copy the contract into a fingerprinted batch and create components

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_batches.py`

**Step 1: Write failing planner tests**

For exact verified live positions, assert that planning copies the candidate contract byte-for-byte, verifies its fingerprint, and creates exactly:

```text
0 consume_take_profit_stage
1 converge_partial_close
2 replace_remaining_protection
```

For split positions, create per-leg components under the same batch and preserve deterministic `posId` order. Assert that a missing, extra, reordered, or fingerprint-mismatched component blocks with `management_instruction_component_dropped`.

**Step 2: Add trusted-start and target-remaining snapshots**

Persist per leg:

```json
{
  "trusted_start_size": "16",
  "target_remaining_size": "8",
  "avg_entry_price": "63000",
  "quantity_step": "1",
  "min_quantity": "1"
}
```

The target must never be recomputed from a later larger position. A later size increase is drift and requires operator review rather than granting a larger close.

**Step 3: Preserve current strategy locks**

Only one unsafe batch per `strategy_instance_id` remains allowed. V2 component creation and the batch must commit in one transaction.

**Step 4: Run focused tests and commit**

```bash
uv run pytest -q tests/test_strategy_management_planner.py tests/test_strategy_management_batches.py -k 'composite or contract'
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_planner.py tests/test_strategy_management_batches.py
git commit -m "feat: plan durable composite components"
```

### Task 5: Plan first-TP consumption from exact exchange evidence

**Files:**
- Create: `src/telegram_kol_research/strategy_management_take_profit_consumption.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Test: `tests/test_strategy_management_take_profit_consumption.py`

**Step 1: Write pure failing tests for the consumption policy**

Cover:

- one full-position TP: cancel it completely;
- several stages: consume the earliest, preserve later stages within remaining size;
- first TP already filled: count exact fill toward reduction;
- pending first TP: produce an exact cancel action;
- absent TP with no terminal evidence: `take_profit_terminal_state_unknown`;
- duplicate order ID or conflicting ledger owner: block;
- retained total above target remaining: shrink/remove earliest retained stages deterministically;
- manual or unrelated partial close is not TP-fill proof.

**Step 2: Implement a side-effect-free planner**

```python
def plan_take_profit_consumption(
    *,
    contract,
    target_leg,
    pending_orders,
    trigger_history,
    order_history,
    trade_fills,
    protection_ledger,
    trusted_start_size,
    target_remaining_size,
) -> TakeProfitConsumptionPlan: ...
```

The result exposes exact order IDs to cancel, proven filled quantity, retained rows, evidence tier, and refusal code.

**Step 3: Verify and commit**

```bash
uv run pytest -q tests/test_strategy_management_take_profit_consumption.py
git add src/telegram_kol_research/strategy_management_take_profit_consumption.py \
  src/telegram_kol_research/position_take_profit_orders.py \
  tests/test_strategy_management_take_profit_consumption.py
git commit -m "feat: plan first take profit consumption"
```

### Task 6: Execute and reconcile TP consumption without a trigger race

**Files:**
- Create: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_position_mutation_gateway.py`

**Step 1: Write failing executor tests**

Prove that:

- a pending owned first TP is cancelled before close submission;
- TP cancellation unknown moves the component to `awaiting_exchange` and makes zero close calls;
- a TP that fills during cancellation is reconciled as fulfilled quantity;
- a definitely rejected cancel can be retried only after a fresh snapshot proves the order remains pending;
- restart never submits a second cancel for the same idempotency key.

**Step 2: Use the canonical mutation gateway**

Reserve one mutation intent per exact cancellation before the write. Persist the component's desired order IDs and intent IDs before submission. Confirm cancellation only from a complete pending-order snapshot or exact terminal history.

**Step 3: Fix readback-confirmed TPSL ledger convergence**

Add a regression for `submit_exact_position_sltp(..., require_readback=True)`: confirmation and canonical ledger upsert must occur in the same transaction. It must never produce a confirmed mutation intent whose live order remains unowned. Reuse this helper in v2 protection execution.

**Step 4: Verify and commit**

```bash
uv run pytest -q tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py -k 'consume or readback or ledger'
git add src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  tests/test_strategy_management_executor.py tests/test_position_mutation_gateway.py
git commit -m "feat: execute owned take profit consumption"
```

### Task 7: Converge the exact partial-close delta

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_sizing.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_sizing.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Add failing convergence tests**

For trusted start `16` and target remaining `8`, cover current sizes `16`, `12`, `8`, and `7`. Expected close deltas are `8`, `4`, `0`, and refusal `position_below_target_remaining`.

Add exchange scenarios:

- close response accepted and exact remaining size confirmed;
- response unknown but fill/history later confirms;
- response unknown with no evidence: no retry;
- definite rejection and unchanged position: bounded retry with a new attempt record but the same component target;
- partial fill: submit only the unresolved delta after exact reconciliation;
- size increase after trusted snapshot: operator-required drift, never a larger automatic close.

**Step 2: Submit through `close_exact_position()` only**

Use a deterministic client order ID and component idempotency key. Re-read exact position identity, size, average price, contract spec, active reservations, and global write gate immediately before every write.

**Step 3: Confirm from exchange truth only**

Neither HTTP success nor a submitted order completes the component. Require exact fills/history or coherent trusted-start/current-size evidence with no conflicting mutation.

**Step 4: Verify and commit**

```bash
uv run pytest -q tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_reconciliation.py -k 'target_remaining or composite'
git add src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/strategy_management_sizing.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  tests/test_strategy_management_sizing.py tests/test_strategy_management_reconciliation.py
git commit -m "feat: converge composite partial closes"
```

### Task 8: Replace remaining protection create-before-cancel

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/protection_ledger.py`
- Modify: `src/telegram_kol_research/protection_health.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_market_policy.py`
- Test: `tests/test_position_protection_ledger.py`

**Step 1: Write failing ordering and protection tests**

Assert the exact call order:

```text
set new primary stop
read back and own primary
set new backup stop
read back and own backup
cancel old primary/backup stops
verify retained TP total
```

Cover explicit `62700`, actual-entry `62841.6`, long/short geometry, tick rounding, market already through requested stop, an already tighter old stop, duplicate new-stop response IDs, readback failure, and old-stop cancellation failure.

**Step 2: Implement create-before-cancel**

Do not reuse the legacy pre-cancel protection path for v2. Persist canonical ledger ownership for both new stops before cancelling any old stop. If a new stop fails, retain old stops and keep the component recoverable.

**Step 3: Enforce retained TP bounds**

After protection replacement, require every retained TP to have exact ownership and total size no greater than the live position. A violation is `retained_take_profit_exceeds_position`, not success.

**Step 4: Verify and commit**

```bash
uv run pytest -q tests/test_strategy_management_executor.py \
  tests/test_strategy_management_market_policy.py \
  tests/test_position_protection_ledger.py -k 'create_before_cancel or retained_take_profit or explicit_stop'
git add src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/protection_ledger.py \
  src/telegram_kol_research/protection_health.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_market_policy.py \
  tests/test_position_protection_ledger.py
git commit -m "feat: replace composite protection safely"
```

### Task 9: Resume unfinished components and preserve progress across restart

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Create: `src/telegram_kol_research/strategy_management_composite_reconciliation.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Add restart tests at every boundary**

Restart after:

- TP cancel reservation;
- unknown TP cancel;
- close reservation;
- unknown close;
- close confirmation before component transition;
- primary stop creation;
- backup stop creation;
- new-stop verification before old-stop cancellation.

Assert no duplicate exchange writes and eventual convergence from the first nonterminal component.

**Step 2: Add bounded recovery scheduling**

Only exchange evidence or a valid transition updates `last_progress_at`. Use existing deadlines and escalation fields. A component with unknown exchange outcome remains read-only until resolved. A definitely rejected write may enter bounded retry only after fresh preconditions.

**Step 3: Keep recovery alive during admission rollback**

`composite_management_v2_mode=disabled` blocks new composite batches but must continue reconciling any component that already has a mutation intent or exchange submission.

**Step 4: Verify and commit**

```bash
uv run pytest -q tests/test_strategy_management_worker.py \
  tests/test_strategy_management_reconciliation.py -k 'composite or restart or recovery'
git add src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/strategy_management_composite_reconciliation.py \
  src/telegram_kol_research/web_app.py \
  tests/test_strategy_management_worker.py tests/test_strategy_management_reconciliation.py
git commit -m "feat: recover composite management components"
```

### Task 10: Verify completion at every boundary and report component outcomes

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/semantic_disagreement_review.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Test: `tests/test_semantic_disagreement_review.py`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_strategy_records.py`

**Step 1: Write failing completeness tests**

Reject:

- source says 50% but contract has no close;
- source says move stop but batch has no protection component;
- first TP was consumed but still reads pending;
- batch is `succeeded` while a required component lacks evidence;
- completion notification while overall state is recovering or operator-required.

**Step 2: Expand advisory comparison input**

Pass the authoritative payload, immutable contract, component plan, and actual outcomes to semantic review. Preserve MiMo authority. Material mismatch produces review/alert according to existing policy and never creates a write.

**Step 3: Add component-level operator summaries**

Report first TP, partial close, protection, remaining size, retained TP total, and overall state. Sanitize errors and keep Telegram chunking bounds.

**Step 4: Verify and commit**

```bash
uv run pytest -q tests/test_semantic_disagreement_review.py \
  tests/test_system_operator_bot.py tests/test_strategy_records.py -k composite
git add src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/semantic_disagreement_review.py \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py \
  tests/test_semantic_disagreement_review.py tests/test_system_operator_bot.py \
  tests/test_strategy_records.py
git commit -m "feat: verify and report composite completion"
```

### Task 11: Add monitor invariants and fault-injection coverage

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Test: `tests/test_production_safety_monitor.py`
- Create: `tests/test_composite_management_fault_injection.py`

**Step 1: Add failing monitor tests**

Detect:

```text
completed_batch_missing_component_evidence
duplicate_composite_close_submission
live_position_retained_tp_oversized
composite_position_without_verified_stop
stalled_composite_component
```

Version drift alone remains deployment context, not a safety failure.

**Step 2: Build a failure matrix**

Inject definite rejection, unknown outcome, response persistence failure, stale readback, duplicate order ID, service restart, notification failure, auxiliary-model failure, and UI failure at each component boundary. Assert exact write counts and final durable states.

**Step 3: Verify and commit**

```bash
uv run pytest -q tests/test_production_safety_monitor.py \
  tests/test_composite_management_fault_injection.py
git add src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py tests/test_composite_management_fault_injection.py
git commit -m "test: harden composite management recovery"
```

### Task 12: Document operations, review, and deploy dormant

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`
- Create: `docs/composite-management-v2-live-verification.md`

**Step 1: Document the operator contract**

Include component states, stable reason codes, read-only SQL, exchange evidence order, admission disable versus recovery behavior, and the rule that historical Miya/Sanjie messages are never auto-replayed.

**Step 2: Run local focused suites**

```bash
uv run pytest -q \
  tests/test_management_directives.py \
  tests/test_message_recognition.py \
  tests/test_strategy_management_contracts.py \
  tests/test_strategy_management_components.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_position_mutation_gateway.py \
  tests/test_system_operator_bot.py \
  tests/test_production_safety_monitor.py \
  tests/test_composite_management_fault_injection.py
python3 -m compileall -q src
git diff --check
```

Expected: all tests pass and no whitespace errors.

**Step 3: Perform independent code review**

Use `requesting-code-review` and resolve every Critical and Important finding. Review particularly:

- no ownership fallback;
- no blind retry path;
- exact close-delta math;
- TP-fill/cancel race;
- create-before-cancel stops;
- rollback recovery compatibility;
- zero automatic historical replay.

**Step 4: Commit documentation and push**

```bash
git add docs/runbook.md docs/server-deployment.md docs/migration-handoff.md \
  docs/composite-management-v2-live-verification.md
git commit -m "docs: operate composite management v2"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 5: Prove a safe production window**

Before deployment, read only:

- active strategy-management batches and components;
- active/unknown mutation intents;
- live positions and exact ownership;
- pending TPSL ownership and protection incidents;
- listener/checkpoint/reconciliation health;
- production safety monitor result.

Defer deployment if management, close, protection, rescue, or a time-sensitive strategy operation is in flight.

**Step 6: Deploy dormant**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Verify server SHA, editable package, active `telegram-kol.service`, and `composite_management_v2_mode=disabled`. Run focused server tests. Do not submit a synthetic Telegram signal or exchange write.

**Step 7: Shadow replay Miya and Sanjie**

Use a read-only/sandboxed historical replay that cannot call Deepcoin writes. Require:

- Miya: consume TP, target remaining 50%, explicit stop `62700`;
- Sanjie: consume TP, target remaining 50%, actual-entry protection;
- exchange write count `0`;
- component and contract fingerprints stable across repeated replay.

**Step 8: Enable live separately**

Enabling `live` is a separate reviewed production decision after shadow evidence and a second safe-window check. Do not combine deployment and live enablement. After enablement, verify only naturally arriving messages; never create a real test trade.

**Step 9: Record production evidence**

Update `docs/composite-management-v2-live-verification.md` with non-sensitive SHA, service state, test counts, mode, shadow fingerprints, monitor result, and any remaining operator-required items.
