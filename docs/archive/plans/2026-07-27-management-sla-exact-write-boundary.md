# Management SLA and Exact Write Boundary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure every risk-reduction message completes or escalates within a bounded SLA, and make it impossible for any service, CLI, or repair script to mutate a Deepcoin position/TPSL order without exact persisted ownership.

**Architecture:** Add a durable position-mutation authority and intent layer in front of every Deepcoin position/TPSL write. Extend management batches with deadlines and explicit escalation, route the normal executor and repair CLI through one gateway, and require a post-write account invariant audit before success. Keep verified `ExecutionOrderLeg.pos_id` plus the exact protection ledger authoritative; never use symbol/side/price or `positions.slTriggerPx` to authorize a write.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, existing Deepcoin REST client, management worker, system operator bot, and production safety monitor.

---

## Global Safety Constraints

- Use @systematic-debugging before changing behavior and @test-driven-development for every task.
- Make all code changes locally; use fake Deepcoin clients for local tests.
- Do not send a live order, cancel a live order, modify a live TPSL, or restore the real stop during implementation.
- Real verification runs only on the server because account identity and API allowlisting are server-bound.
- Preserve the current new-message and entry execution settings throughout deployment. Do not pause or downgrade new-message handling as part of this repair.
- Do not use a shadow phase. Roll out the management write boundary as a compatibility-preserving replacement, with focused server tests and a read-only account audit before the service restart.
- Preserve `ExecutionOrderLeg.pos_id` with `attribution_status="verified"` as the position authority.
- Preserve unknown or unattributed exchange orders; unknown is never permission to cancel.
- A write with an unknown result is reconciled by its persisted ID and never blindly retried.
- Apply current-position repair one exact action at a time with a fresh fingerprint; never add apply-all.
- Commit each task separately.
- Before pushing or deploying, use @requesting-code-review.
- Push reviewed commits to `codex/deepcoin-auto-trading-v1`.
- Deploy only through GitHub/server pull, editable reinstall, and `telegram-kol.service` restart.

## Fixed Incident Fixture

Use the real incident shape with synthetic IDs in unit tests:

```python
SISTER_POSITION = {
    "posId": "pos-sister",
    "instId": "BTC-USDT-SWAP",
    "posSide": "long",
    "pos": "10",
    "avgPx": "63895.725",
    "slTriggerPx": "",
}

OTHER_POSITION = {
    "posId": "pos-other",
    "instId": "BTC-USDT-SWAP",
    "posSide": "long",
    "pos": "2",
    "avgPx": "63900",
    # Deepcoin contaminated/aggregate field from the incident:
    "slTriggerPx": "63895.725",
}

SISTER_STOP = {
    "ordId": "ord-sister-stop",
    "instId": "BTC-USDT-SWAP",
    "posSide": "long",
    "sz": "0",
    "slTriggerPrice": "63895.725",
    # Pending readback omits posId/closePosId.
}
```

The permanent regression assertion is:

```python
with pytest.raises(PositionMutationAuthorityError, match="order_owner_mismatch"):
    gateway.cancel_position_sltp(
        authority=authority_for_other_position,
        order_id="ord-sister-stop",
    )
assert fake_client.cancel_position_sltp_calls == []
```

### Task 1: Add Pure Exact Mutation Authority

**Files:**
- Create: `src/telegram_kol_research/position_mutation_authority.py`
- Test: `tests/test_position_mutation_authority.py`
- Modify: `src/telegram_kol_research/position_attribution.py`

**Step 1: Write failing ownership tests**

Create table-driven tests covering:

```python
def test_foreign_order_is_rejected_even_when_price_matches_position_snapshot():
    authority = PositionMutationAuthority(
        venue="deepcoin",
        strategy_instance_id="strategy-other",
        execution_binding_id=2,
        execution_order_leg_id=22,
        pos_id="pos-other",
        instrument_id="BTC-USDT-SWAP",
        side="long",
        position_fingerprint="position-fp",
        protection_fingerprint="protection-fp",
    )
    owner = ProtectionOrderOwner(
        venue="deepcoin",
        order_id="ord-sister-stop",
        strategy_instance_id="strategy-sister",
        execution_binding_id=1,
        execution_order_leg_id=11,
        pos_id="pos-sister",
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )

    with pytest.raises(
        PositionMutationAuthorityError,
        match="order_owner_mismatch",
    ):
        require_order_owned_by_authority(authority=authority, owner=owner)
```

Also assert rejection for missing owner, duplicate owners, terminal leg, unverified leg, changed position fingerprint, wrong `posId`, wrong binding, wrong strategy, wrong instrument, and wrong side. Assert that price equality never appears in the authority API.

**Step 2: Run RED**

```bash
.venv/bin/pytest tests/test_position_mutation_authority.py -q
```

Expected: FAIL because the module does not exist.

**Step 3: Implement immutable authority types**

Add:

```python
class PositionMutationAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PositionMutationAuthority:
    venue: str
    strategy_instance_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    position_fingerprint: str
    protection_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ProtectionOrderOwner:
    venue: str
    order_id: str
    strategy_instance_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    side: str


def require_order_owned_by_authority(
    *,
    authority: PositionMutationAuthority,
    owner: ProtectionOrderOwner | None,
) -> None:
    if owner is None:
        raise PositionMutationAuthorityError("order_owner_missing")
    expected = (
        authority.venue,
        authority.strategy_instance_id,
        authority.execution_binding_id,
        authority.execution_order_leg_id,
        authority.pos_id,
        authority.instrument_id,
        authority.side.lower(),
    )
    actual = (
        owner.venue,
        owner.strategy_instance_id,
        owner.execution_binding_id,
        owner.execution_order_leg_id,
        owner.pos_id,
        owner.instrument_id,
        owner.side.lower(),
    )
    if actual != expected:
        raise PositionMutationAuthorityError("order_owner_mismatch")
```

Add a builder that accepts only a verified live entry leg and the exact live position row. Reuse `canonical_live_position_economics()` and `require_verified_position_ownership()` from `position_attribution.py`; do not duplicate attribution rules.

**Step 4: Run GREEN**

```bash
.venv/bin/pytest tests/test_position_mutation_authority.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/position_mutation_authority.py \
  src/telegram_kol_research/position_attribution.py \
  tests/test_position_mutation_authority.py
git commit -m "feat: require exact authority for position mutations"
```

### Task 2: Persist Position Mutation Intents and Management Deadlines

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/position_mutation_intents.py`
- Test: `tests/test_position_mutation_intents.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Write failing schema tests**

Assert a new table `position_mutation_intents` contains:

```python
{
    "id",
    "idempotency_key",
    "operation",
    "strategy_instance_id",
    "execution_binding_id",
    "execution_order_leg_id",
    "pos_id",
    "order_id",
    "authority_fingerprint",
    "request_fingerprint",
    "status",
    "request_json",
    "response_json",
    "error_json",
    "reserved_at",
    "submitted_at",
    "confirmed_at",
    "created_at",
    "updated_at",
}
```

Assert `strategy_management_batches` and `message_instruction_items` include:

```python
{
    "execution_deadline_at",
    "operator_escalation_at",
    "last_progress_at",
    "escalation_state",
    "escalation_notified_at",
}
```

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_position_mutation_intents.py \
  tests/test_db_bootstrap.py -q
```

Expected: FAIL with missing table/columns.

**Step 3: Add the model and additive bootstrap migration**

Use statuses:

```python
POSITION_MUTATION_INTENT_STATUSES = frozenset({
    "reserved",
    "submitting",
    "submitted",
    "confirmed",
    "rejected",
    "recovery_required",
    "blocked",
})
```

Add unique indexes on `idempotency_key` and on non-null
`venue + operation + order_id + request_fingerprint`. Store redacted request/response JSON only.

Use additive SQLite migrations in `db.py`; do not rewrite existing rows. Existing management rows get null deadlines and are not automatically executed.

**Step 4: Add reservation and transition functions**

Implement compare-and-set transitions:

```python
def reserve_position_mutation_intent(...) -> PositionMutationIntent
def transition_position_mutation_intent(
    session_factory,
    intent_id: int,
    *,
    expected_statuses: set[str],
    new_status: str,
    transitioned_at: datetime,
    response: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> bool
```

Repeat reservation with the same idempotency key must return the existing row. A different fingerprint must raise `position_mutation_intent_conflict`.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_position_mutation_intents.py \
  tests/test_db_bootstrap.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/position_mutation_intents.py \
  tests/test_position_mutation_intents.py tests/test_db_bootstrap.py
git commit -m "feat: persist exact mutation intents and deadlines"
```

### Task 3: Create the Only Position/TPSL Write Gateway

**Files:**
- Create: `src/telegram_kol_research/position_mutation_gateway.py`
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Test: `tests/test_position_mutation_gateway.py`

**Step 1: Write the incident regression test**

Build two verified BTC long legs and two live positions. Persist the sister stop owner against `pos-sister`; expose the same price through `OTHER_POSITION["slTriggerPx"]`. Attempt cancellation with authority for `pos-other`.

Assert:

```python
assert result.reason == "order_owner_mismatch"
assert fake_client.cancel_position_sltp_calls == []
assert stored_intent.status == "blocked"
```

Also test a valid same-owner cancellation and stale authority fingerprint.

**Step 2: Run RED**

```bash
.venv/bin/pytest tests/test_position_mutation_gateway.py -q
```

Expected: FAIL because the gateway does not exist.

**Step 3: Implement the gateway**

Add:

```python
class PositionMutationGateway:
    def __init__(
        self,
        *,
        session_factory,
        deepcoin_client,
        live_execution_gate,
        now_provider,
    ) -> None: ...

    def cancel_owned_position_sltp(
        self,
        *,
        authority: PositionMutationAuthority,
        order_id: str,
        idempotency_key: str,
    ) -> PositionMutationResult: ...

    def set_exact_position_sltp(
        self,
        *,
        authority: PositionMutationAuthority,
        purpose: str,
        trigger_price: str,
        size: str,
        idempotency_key: str,
    ) -> PositionMutationResult: ...

    def close_exact_position(
        self,
        *,
        authority: PositionMutationAuthority,
        size: str,
        client_order_id: str,
        idempotency_key: str,
    ) -> PositionMutationResult: ...
```

Each method must:

1. reload DB ownership;
2. reload exact live position;
3. recompute fingerprints;
4. validate settings immediately before write;
5. reserve a durable intent;
6. transition to submitting;
7. call the low-level client once;
8. persist response or unknown outcome;
9. return without inferring success from an acknowledgement.

Cancellation payload is only:

```python
{
    "instType": "SWAP",
    "instId": authority.instrument_id,
    "ordId": order_id,
}
```

but it is emitted only after exact ownership validation. Setting TPSL must include:

```python
{
    "instType": "SWAP",
    "instId": authority.instrument_id,
    "posSide": authority.side,
    "mrgPosition": "split",
    "posId": authority.pos_id,
    "tdMode": verified_margin_mode,
    ...
}
```

Closing must include exact `closePosId=authority.pos_id`.

**Step 4: Make low-level methods explicitly unchecked**

Rename internal implementations in `DeepcoinRestClient`:

```python
_set_position_sltp_unchecked
_cancel_position_sltp_unchecked
_place_position_close_unchecked
```

Do not export them through the public protocol. The gateway owns a narrow internal adapter protocol used by fakes.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_position_mutation_gateway.py \
  tests/test_deepcoin_client.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_mutation_gateway.py \
  src/telegram_kol_research/deepcoin_client.py \
  tests/test_position_mutation_gateway.py tests/test_deepcoin_client.py
git commit -m "feat: centralize exact Deepcoin position writes"
```

### Task 4: Enforce the Boundary With an Architecture Test

**Files:**
- Create: `tests/test_position_mutation_architecture.py`
- Modify: `pyproject.toml`

**Step 1: Write an AST-based failing test**

Walk `src/telegram_kol_research` and `scripts`. Fail when code outside the allowlist directly accesses:

```python
FORBIDDEN_POSITION_WRITE_ATTRIBUTES = {
    "set_position_sltp",
    "cancel_position_sltp",
    "_set_position_sltp_unchecked",
    "_cancel_position_sltp_unchecked",
    "_place_position_close_unchecked",
}
```

Allow only:

```python
{
    "src/telegram_kol_research/position_mutation_gateway.py",
    "src/telegram_kol_research/deepcoin_client.py",
}
```

Also fail if a script constructs a `POST` request to the position SLTP endpoints.

**Step 2: Run RED**

```bash
.venv/bin/pytest tests/test_position_mutation_architecture.py -q
```

Expected: FAIL and list every current direct caller.

**Step 3: Add a dedicated pytest marker**

Register `architecture` in `pyproject.toml` and mark the test. Do not skip it in normal CI.

**Step 4: Keep the test failing until Tasks 5 and 6 migrate all callers**

Commit only after the caller list reaches zero.

### Task 5: Route Normal Management Through the Gateway

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write failing composite-action tests**

Test `partial_then_break_even` with:

- exact 20 → 10 close;
- exact old protection owner;
- exact remaining TP;
- effective stop from average entry;
- a second same-symbol/same-side position whose protection must remain byte-for-byte unchanged.

Assert all raw writes went through the fake gateway and no direct client write was called.

Add restart tests after each phase:

1. old protection cancelled;
2. close submitted outcome unknown;
3. close confirmed;
4. replacement TP submitted;
5. replacement stop submitted;
6. readback pending.

Every restart must resume/read back without duplicate reduction.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_reconciliation.py \
  -k 'partial_then_break_even or protection' -q
```

Expected: FAIL because the executor still writes through the raw client.

**Step 3: Inject the gateway**

Change executor entry points to receive `position_mutation_gateway`. Remove direct position/TPSL writes. Keep read-only client calls for snapshots.

Use deterministic idempotency keys:

```python
f"management:{batch.id}:{leg.id}:cancel:{order_id}"
f"management:{batch.id}:{leg.id}:close:{client_order_id}"
f"management:{batch.id}:{leg.id}:set:{purpose}:{row_index}"
```

Do not derive keys from mutable timestamps.

**Step 4: Reconcile mutation intents**

Reconciliation loads existing mutation intents first. For `submitted` or
`recovery_required`, query by persisted order/client ID. It must not create a successor intent until the prior outcome is terminal and the latest target fingerprint is valid.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_reconciliation.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_reconciliation.py
git commit -m "refactor: route position management through exact gateway"
```

### Task 6: Route Every Repair and Cleanup Path Through the Gateway

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Modify: `src/telegram_kol_research/current_protection_backfill.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/native_tpsl_migration.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: relevant files under `scripts/`
- Test: `tests/test_position_management_remediation.py`
- Test: `tests/test_backup_stop_repair.py`
- Test: `tests/test_current_protection_backfill.py`
- Test: `tests/test_recovery_live_submit.py`
- Test: `tests/test_trigger_backup_stop.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_position_mutation_architecture.py`

**Step 1: Add failing cross-owner remediation tests**

For every apply-capable repair module, attempt to pass an action whose displayed price matches but whose order owner belongs to another binding.

Expected:

```python
assert result.status == "blocked"
assert result.reason == "order_owner_mismatch"
assert fake_gateway.write_calls == []
```

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_position_management_remediation.py \
  tests/test_backup_stop_repair.py \
  tests/test_current_protection_backfill.py \
  tests/test_recovery_live_submit.py \
  tests/test_trigger_backup_stop.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_position_mutation_architecture.py -q
```

Expected: FAIL with direct writers and unsafe apply paths.

**Step 3: Require exact action identity**

Every apply command must require:

```text
--action-id <single action>
--pos-id <exact position>
--expected-fingerprint <fresh fingerprint>
--confirmation-token <single-use token>
```

Remove or reject bulk apply. Dry-run remains the default.

**Step 4: Replace raw client writes with gateway calls**

The repair planner may produce a suggestion from read-only evidence, but apply must rebuild authority from the database and live exchange snapshot. A plan generated from price/side matching is not eligible for apply.

**Step 5: Make architecture test GREEN**

```bash
.venv/bin/pytest tests/test_position_mutation_architecture.py -q
```

Expected: PASS with no forbidden callers.

**Step 6: Run all repair tests**

```bash
.venv/bin/pytest \
  tests/test_position_management_remediation.py \
  tests/test_backup_stop_repair.py \
  tests/test_current_protection_backfill.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_native_tpsl_migration.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/position_management_remediation.py \
  src/telegram_kol_research/backup_stop_repair.py \
  src/telegram_kol_research/current_protection_backfill.py \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  src/telegram_kol_research/native_tpsl_migration.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  scripts tests/test_position_mutation_architecture.py \
  tests/test_position_management_remediation.py \
  tests/test_backup_stop_repair.py \
  tests/test_current_protection_backfill.py
git commit -m "fix: enforce exact authority in all repair writes"
```

### Task 7: Add Bounded Management SLA and Escalation

**Files:**
- Create: `src/telegram_kol_research/management_sla.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Test: `tests/test_management_sla.py`
- Test: `tests/test_message_instruction_items.py`
- Test: `tests/test_strategy_management_worker.py`

**Step 1: Write failing SLA tests**

Cover:

```python
def test_risk_reduction_gets_deadline_from_message_receipt():
    sla = management_sla(received_at=NOW)
    assert sla.execution_deadline_at == NOW + timedelta(seconds=90)
    assert sla.operator_escalation_at == NOW + timedelta(minutes=3)


def test_protection_recovery_never_becomes_silent_terminal_failure():
    result = schedule_management_failure(
        reason="protection_recovery_required",
        now=NOW,
        execution_deadline_at=NOW + timedelta(seconds=90),
    )
    assert result.status == "retry_wait"
    assert result.next_attempt_at == NOW + timedelta(seconds=5)
```

Also test 5/15/30/40-second retry delays, unknown-write readback-only behavior, ownership conflict immediate escalation, restart recovery, and exactly-once escalation.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_management_sla.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_worker.py -q
```

Expected: FAIL.

**Step 3: Implement pure SLA policy**

Add:

```python
@dataclass(frozen=True, slots=True)
class ManagementSla:
    execution_deadline_at: datetime
    operator_escalation_at: datetime


RETRY_DELAYS_SECONDS = (5, 15, 30, 40)
IMMEDIATE_OPERATOR_REASONS = frozenset({
    "order_owner_missing",
    "order_owner_mismatch",
    "position_ownership_not_unique",
    "position_fingerprint_changed",
    "non_target_invariant_changed",
})
```

Base all deadlines on durable raw-message receipt time, not worker start time.

**Step 4: Replace silent failed/blocked transitions**

For a recognized risk-reduction instruction:

- temporary pre-write failures become `retry_wait` with `next_attempt_at`;
- unknown write outcomes become `recovery_required` and are polled;
- exact ownership failures become `operator_required`;
- crossing 90 seconds sets `operator_required` and queues a critical notification;
- crossing three minutes leaves writes frozen and repeats no notification unless the incident fingerprint changes.

Do not convert historical failed rows automatically. Provide a separate read-only audit for them.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_management_sla.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_worker.py \
  tests/test_auto_trade_execution.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/management_sla.py \
  src/telegram_kol_research/message_instruction_items.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/auto_trade_execution.py \
  tests/test_management_sla.py tests/test_message_instruction_items.py \
  tests/test_strategy_management_worker.py tests/test_auto_trade_execution.py
git commit -m "feat: bound risk reduction with management SLA"
```

### Task 8: Persist Requested, Effective, and Confirmed Stop Values

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_web_strategy_records.py`

**Step 1: Write failing value-flow tests**

For “移动止损至开仓价” with message price `63900` and actual exchange average `63895.725`, assert:

```python
assert leg.requested_stop_loss_text == "63900"
assert leg.effective_stop_loss_text == "63895.725"
assert leg.confirmed_stop_loss_text is None
```

After exact readback:

```python
assert leg.confirmed_stop_loss_text == "63895.725"
assert lifecycle.stop_loss == 63895.725
```

Acknowledgement without readback must leave confirmed null and lifecycle unchanged.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_web_strategy_records.py \
  -k 'stop_loss or break_even' -q
```

Expected: FAIL.

**Step 3: Add columns to `StrategyManagementLeg`**

Add:

```python
requested_stop_loss_text: Mapped[str | None]
effective_stop_loss_text: Mapped[str | None]
confirmed_stop_loss_text: Mapped[str | None]
confirmed_stop_loss_at: Mapped[datetime | None]
```

Keep `planned_tpsl_json` for backward compatibility but make new writes populate the explicit columns.

**Step 4: Update lifecycle only from confirmed value**

Replace `_confirm_protection_lifecycle()` inference from nullable
`planned_tpsl.stop_loss_text`. It must require one common non-null confirmed value across every required leg.

**Step 5: Render all three values**

Display:

- KOL request;
- effective execution target;
- currently confirmed exchange stop;
- confirmation time;
- drift warning when they differ unexpectedly.

**Step 6: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_web_strategy_records.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/templates/_strategy_detail.html \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_web_strategy_records.py
git commit -m "fix: persist confirmed management stop values"
```

### Task 9: Add Post-Write Account Invariant Auditing

**Files:**
- Create: `src/telegram_kol_research/position_mutation_invariants.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Test: `tests/test_position_mutation_invariants.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write failing non-target invariant tests**

Snapshot all live positions and exact owned order IDs before a management transaction. After target replacement, remove one non-target stop.

Assert:

```python
assert result.status == "operator_required"
assert result.reason == "non_target_invariant_changed"
assert result.changed_non_target_pos_ids == ("pos-sister",)
```

The batch must not be marked succeeded even if the target action itself succeeded.

**Step 2: Run RED**

```bash
.venv/bin/pytest tests/test_position_mutation_invariants.py -q
```

Expected: FAIL.

**Step 3: Implement bounded invariant snapshots**

Persist only:

- exact posId;
- instrument and side;
- size and average entry;
- exact owned pending order IDs and purposes;
- fingerprint.

Do not persist full account payloads or credentials.

Implement:

```python
def compare_post_write_invariants(
    *,
    before: AccountMutationInvariant,
    after: AccountMutationInvariant,
    allowed_target_pos_ids: set[str],
    expected_target_changes: Mapping[str, ExpectedTargetChange],
) -> InvariantComparison:
    ...
```

Any non-target position/order change is a critical incident. The comparison reports it but performs no compensating write automatically.

**Step 4: Require audit before success**

Every management or repair batch must pass target convergence and non-target invariants before `succeeded`.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_position_mutation_invariants.py \
  tests/test_strategy_management_executor.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_mutation_invariants.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  src/telegram_kol_research/strategy_management_executor.py \
  tests/test_position_mutation_invariants.py \
  tests/test_strategy_management_executor.py
git commit -m "feat: audit non-target state after position writes"
```

### Task 10: Add P0 Notifications and Safety Monitor Rules

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/strategy_alerts.py`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_production_safety_monitor.py`
- Test: `tests/test_strategy_alerts.py`

**Step 1: Write failing critical-notification tests**

Cover:

- risk reduction older than 90 seconds;
- active position loses verified primary stop;
- cancel event owner differs from event target;
- non-target invariant change;
- direct-writer architecture violation surfaced by deployment check;
- exactly-once notification by incident fingerprint.

P0 alerts must bypass the normal six-hour suppression window but deduplicate the same fingerprint.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_system_operator_bot.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_alerts.py \
  -k 'critical or management or protection' -q
```

Expected: FAIL.

**Step 3: Add stable reason codes**

Add:

```python
{
    "management_sla_breached",
    "active_position_primary_stop_missing",
    "position_mutation_owner_mismatch",
    "non_target_invariant_changed",
    "position_write_boundary_bypassed",
}
```

Notification payload includes strategy label, masked posId, action, age, reason, and read-only/operator URL. It must not include raw exchange payloads or credentials.

**Step 4: Add monitor queries**

The monitor checks:

- overdue nonterminal instruction items/batches;
- active live positions whose exact primary stop disappeared;
- mutation intents stuck in submitting/recovery_required;
- cancellation events whose stored target differs from the ledger owner;
- operator-required incidents lacking successful notification.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_system_operator_bot.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_alerts.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/strategy_alerts.py \
  tests/test_system_operator_bot.py \
  tests/test_production_safety_monitor.py tests/test_strategy_alerts.py
git commit -m "feat: alert on overdue and unsafe position mutations"
```

### Task 11: Separate Entry, Management, and Repair Kill Switches

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_position_mutation_gateway.py`

**Step 1: Write failing settings tests**

Add independent settings:

```python
entry_execution_mode: Literal["disabled", "shadow", "live"] = "disabled"
management_execution_mode: Literal["disabled", "shadow", "live"] = "disabled"
position_repair_execution_mode: Literal["disabled", "operator_confirmed"] = "disabled"
```

Test that disabling management blocks the gateway immediately before write even when the batch was planned while live. Repair mode never enables ordinary management.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  tests/test_position_mutation_gateway.py \
  -k 'execution_mode or live_gate' -q
```

Expected: FAIL.

**Step 3: Implement fail-safe settings parsing**

Missing or invalid values default to disabled. Keep legacy `auto_trade_enabled` as an additional master gate during migration; both the master gate and the specific mode must allow a live write.

**Step 4: Add visible state**

The UI shows three separate modes and a prominent banner whenever any write mode is live. Settings changes must create an audit event.

**Step 5: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_trading_settings.py tests/test_web_app.py \
  tests/test_position_mutation_gateway.py \
  tests/test_auto_trade_execution.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  tests/test_trading_settings.py tests/test_web_app.py \
  tests/test_position_mutation_gateway.py tests/test_auto_trade_execution.py
git commit -m "feat: separate entry management and repair gates"
```

### Task 12: Build a Read-Only Incident Audit and Single-Action Repair Plan

**Files:**
- Create: `src/telegram_kol_research/management_incident_audit.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_management_incident_audit.py`
- Test: `tests/test_cli_smoke.py`
- Modify: `docs/runbook.md`

**Step 1: Write failing dry-run tests**

The audit must report:

- all current live positions;
- verified strategy owner;
- current exact primary stop;
- backup stop;
- take profits;
- overdue management actions;
- cancellation owner mismatches;
- proposed single-position repair actions.

For the synthetic sister incident, propose restoring the confirmed break-even stop only when the position still exists, quantity and average entry match, TP remains expected, and no newer KOL exit instruction exists.

Changing any position/order invalidates the fingerprint.

**Step 2: Run RED**

```bash
.venv/bin/pytest \
  tests/test_management_incident_audit.py \
  tests/test_cli_smoke.py -q
```

Expected: FAIL.

**Step 3: Add CLI commands**

```text
telegram-kol-research audit-management-safety --database-path ...
telegram-kol-research repair-position-protection --database-path ... \
  --action-id ... --pos-id ... --expected-fingerprint ... \
  --confirmation-token ... --apply
```

The first command is read-only. The second is dry-run unless every apply argument is present and repair mode is `operator_confirmed`.

**Step 4: Route apply through the gateway**

The CLI never calls Deepcoin write methods directly. It rebuilds authority, verifies the fingerprint, reserves a mutation intent, applies one action, reads back, and runs the post-write invariant audit.

**Step 5: Document the runbook**

Document stop conditions:

- target fingerprint changed;
- ownership missing/conflicting;
- non-target invariant changed;
- exchange outcome unknown;
- notification unavailable;
- any unexpected pending order.

**Step 6: Run GREEN**

```bash
.venv/bin/pytest \
  tests/test_management_incident_audit.py \
  tests/test_cli_smoke.py \
  tests/test_readme_commands.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/management_incident_audit.py \
  src/telegram_kol_research/cli.py \
  tests/test_management_incident_audit.py tests/test_cli_smoke.py \
  docs/runbook.md
git commit -m "feat: audit and repair one exact position safely"
```

### Task 13: Run Local Regression, Review, and Static Safety Gates

**Files:**
- Modify only if tests reveal a defect.

**Step 1: Run focused P0 suite**

```bash
.venv/bin/pytest \
  tests/test_position_mutation_authority.py \
  tests/test_position_mutation_intents.py \
  tests/test_position_mutation_gateway.py \
  tests/test_position_mutation_architecture.py \
  tests/test_position_mutation_invariants.py \
  tests/test_management_sla.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_position_management_remediation.py \
  tests/test_management_incident_audit.py -q
```

Expected: PASS.

**Step 2: Run full local suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS.

**Step 3: Run compile and forbidden-call search**

```bash
.venv/bin/python -m compileall -q src tests
rg -n \
  'set_position_sltp|cancel_position_sltp|_set_position_sltp_unchecked|_cancel_position_sltp_unchecked' \
  src scripts
```

Expected: only the gateway/client allowlist plus deliberate test fixtures.

**Step 4: Request code review**

Use @requesting-code-review with special attention to:

- all write callers;
- owner equality checks;
- unknown-result idempotency;
- deadline transitions;
- critical notification dedupe;
- non-target invariant logic;
- no live API calls in tests.

**Step 5: Fix findings and rerun tests**

Expected: all review findings resolved and suite PASS.

**Step 6: Commit review fixes**

```bash
git add <reviewed-files>
git commit -m "fix: address exact mutation boundary review"
```

### Task 14: Push and Deploy Without Interrupting New Messages

**Files:**
- Modify: `docs/migration-handoff.md`

**Step 1: Confirm branch and scope**

```bash
git status --short --branch
git log --oneline origin/codex/deepcoin-auto-trading-v1..HEAD
```

Expected: only reviewed commits for this repair plus known user files.

**Step 2: Push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: success.

**Step 3: Read and preserve production execution settings**

Use the existing authenticated settings path or a reviewed server command to record:

```text
position_repair_execution_mode=disabled
auto_trade_enabled=<unchanged>
entry_execution_mode=<unchanged>
management_execution_mode=<unchanged>
```

Do not change the new-message, entry, or management settings during deployment. The
repair mode must remain disabled. Stop if the existing settings cannot be read back.

**Step 4: Deploy**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls the reviewed commit, editable install succeeds, service is active.

**Step 5: Run server tests without account writes**

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/pytest \
  tests/test_position_mutation_authority.py \
  tests/test_position_mutation_gateway.py \
  tests/test_position_mutation_architecture.py \
  tests/test_position_mutation_invariants.py \
  tests/test_management_sla.py -q
```

Expected: PASS.

**Step 6: Run read-only management safety audit**

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research audit-management-safety \
  --database-path data/research.db
```

Expected: complete account snapshot, no write, explicit actions and fingerprints.

**Step 7: Verify critical notification path**

Use the monitor's test-only notification command. Do not synthesize a live trade event.

Expected: exactly one test notification and successful monitor state.

**Step 8: Record evidence**

Update `docs/migration-handoff.md` with:

- deployed commit;
- before/after execution-setting readback proving new-message behavior was unchanged;
- focused server test output;
- read-only account audit timestamp;
- identified current protection gaps;
- notification test;
- deferred live steps.

**Step 9: Commit and push evidence**

```bash
git add docs/migration-handoff.md
git commit -m "docs: record exact write boundary deployment"
git push origin codex/deepcoin-auto-trading-v1
```

### Task 15: Restore the Sister Stop as One Operator-Confirmed Action

**Files:**
- No code changes unless the dry-run exposes a defect.
- Append evidence to `docs/migration-handoff.md`.

**Step 1: Keep all live modes disabled**

Only set:

```text
position_repair_execution_mode=operator_confirmed
```

Entry and management remain disabled.

**Step 2: Generate a fresh read-only repair plan**

The plan must confirm:

- exact `posId=1001124333585861`;
- remaining size is still 10;
- average entry is still `63895.725`;
- TP `66330` is still present;
- primary break-even stop is still absent;
- no newer full-exit instruction exists;
- backup trigger state is reported separately;
- every non-target position/order fingerprint is captured.

If any value differs, stop and replan.

**Step 3: Review the single action**

The action may propose one exact stop at the confirmed effective break-even price. It must not cancel or modify any other order.

Obtain explicit user approval for the exact action, position, price, and current fingerprint.

**Step 4: Apply one action**

```bash
.venv/bin/telegram-kol-research repair-position-protection \
  --database-path data/research.db \
  --action-id <reviewed-action-id> \
  --pos-id 1001124333585861 \
  --expected-fingerprint <fresh-fingerprint> \
  --confirmation-token <single-use-token> \
  --apply
```

Expected:

- gateway accepts exact authority;
- one Deepcoin write;
- returned order ID is persisted;
- pending readback verifies the same order ID;
- lifecycle confirmed stop becomes `63895.725`;
- non-target invariant audit passes.

**Step 5: Immediately run read-only audit again**

Expected:

- sister primary stop verified;
- TP unchanged;
- remaining size unchanged;
- every non-target owned order set unchanged;
- no new unattributed system order;
- no overdue mutation intent.

**Step 6: Disable repair mode**

Set `position_repair_execution_mode=disabled` and read it back.

**Step 7: Record evidence**

Append the action ID, fingerprint, returned order ID, exact readback result, non-target audit, and disabled repair mode to `docs/migration-handoff.md`. Do not record secrets.

### Task 16: Direct Compatibility Rollout and Live Verification

**Files:**
- Append evidence to `docs/migration-handoff.md`.

**Step 1: Replay the two-position incident with fakes on the server**

Expected:

- sister stop cannot be cancelled by other-position authority;
- no raw client write is called;
- overdue action reaches operator-required at 90 seconds;
- notification is exactly once.

**Step 2: Restart with the existing production modes unchanged**

Do not introduce a shadow interval and do not disable new entries. The gateway
replacement must be backward-compatible with ordinary new-message processing.

**Step 3: Verify every live management action**

For each action verify:

- message-to-recognition latency;
- recognition-to-plan latency;
- plan-to-completion latency;
- exact mutation intents;
- confirmed exchange readback;
- non-target invariants;
- notification state.

Any SLA breach or protection drift escalates immediately and blocks only the
affected exact mutation intent. It must not globally stop unrelated new-message
processing.

**Step 4: Final production gate**

Run:

```bash
systemctl is-active telegram-kol.service
.venv/bin/telegram-kol-research audit-management-safety \
  --database-path data/research.db
```

Expected:

- service active;
- no overdue risk-reduction item;
- no active position missing a verified primary stop;
- no owner-mismatch cancel event after deployment;
- no stuck mutation intent;
- monitor healthy.

## Completion Criteria

The incident is not considered fixed until all are true:

- every position/TPSL write path is behind the exact gateway;
- the architecture test prevents future bypass;
- the real cross-position incident fixture passes;
- risk-reduction work has a persisted 90-second SLA and three-minute operator escalation;
- unknown outcomes are readback-only and idempotent;
- requested/effective/confirmed stops are distinct and visible;
- post-write non-target invariants are required for success;
- P0 notification is proven;
- the sister stop is restored by one reviewed exact action;
- all other positions are proven unchanged;
- repair mode is disabled after use;
- server audit is healthy;
- controlled shadow/live burn-in completes without anomaly.
