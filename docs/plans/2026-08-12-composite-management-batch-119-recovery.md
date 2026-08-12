# Composite Management Batch 119 Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent legacy reconciliation from mutating composite-management batches, then recover production batch 119 to its immutable remaining-size target through the existing durable composite executor.

**Architecture:** Enforce state-machine ownership inside the legacy reconciler, add a closed read-only planner for the single approved incident, and use a fingerprinted compare-and-swap repair to return only the false local state to the composite workflow. All exchange writes continue through `PositionMutationGateway`; MiMo remains on `v1`, unknown exchange outcomes never retry, and production recovery occurs only after a complete fresh snapshot and a controlled service-stop window.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite `BEGIN IMMEDIATE`, Typer, pytest, fake Deepcoin clients, systemd production service.

---

Design reference:
`docs/plans/2026-08-12-composite-management-batch-119-recovery-design.md`

## Execution Rules

- Work only in `/Users/steven/Documents/telegram获取消息-mimo-v2-safe-rebuild`
  on branch `codex/mimo-v2-safe-rebuild`.
- Use @test-driven-development for every behavior change: write one focused
  failing test, observe the intended failure, then write the minimal fix.
- Commit each task independently and request code review after every production
  behavior task. Resolve every Critical/Important finding before proceeding.
- Do not deploy, restart, mutate production, or call Deepcoin in Tasks 1–6.
- Use fake clients only in local tests.
- Do not modify the normal deployment preflight to ignore active work.
- Do not replay raw message 10532 through recognition or automatic trading.
- Do not create a new strategy-management batch for this recovery.
- Keep `mimo_contract_mode=v1` throughout the plan.
- Stop immediately if production facts differ from the reviewed recovery plan,
  if a complete exchange snapshot is unavailable, or if another real operation
  is in flight.
- A database backup may be restored only before an exchange request could have
  been sent. After any possible write, preserve the current database and
  reconcile from exchange truth.

### Task 1: Exclude composite batches from legacy reconciliation

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py:139-240`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Add a fixture that reproduces the production ownership defect**

In `tests/test_strategy_management_reconciliation.py`, seed a live
`partial_then_break_even` batch with:

- a valid `management_contract_json`, fingerprint, and version;
- one management leg still at `planned` with no request, response, client order
  ID, or exchange order ID;
- component 1 at `recovery_required` with reason
  `take_profit_exchange_snapshot_incomplete`;
- components 2 and 3 at `pending`; and
- a complete snapshot whose exact position remains `38` and contains no close
  order.

Add the focused regression:

```python
def test_legacy_reconciliation_never_mutates_composite_batch(tmp_path):
    factory, batch_id, leg_id, component_ids = seed_composite_snapshot_failure(
        tmp_path
    )
    before = composite_state(factory, batch_id)

    result = reconcile_strategy_management_batches(
        factory,
        snapshot=unchanged_position_snapshot(size="38"),
        reconciled_at=NOW,
    )

    assert result.checked == 0
    assert composite_state(factory, batch_id) == before
    assert load_leg(factory, leg_id).status == "planned"
    assert [load_component(factory, value).status for value in component_ids] == [
        "recovery_required",
        "pending",
        "pending",
    ]
```

**Step 2: Run the regression and verify RED**

Run:

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_strategy_management_reconciliation.py::test_legacy_reconciliation_never_mutates_composite_batch
```

Expected: FAIL because the legacy reconciler changes the leg to `submitted`
and the batch to `reconciling`.

**Step 3: Add malformed-hybrid and traditional-batch tests**

Add parameterized tests proving that any one composite marker excludes the
batch:

```python
@pytest.mark.parametrize(
    "marker",
    ["management_contract_json", "management_contract_fingerprint",
     "contract_version", "component_row"],
)
def test_legacy_reconciliation_fails_closed_for_any_composite_marker(
    tmp_path, marker
): ...
```

Also add:

```python
def test_legacy_reconciliation_still_reconciles_traditional_close_batch(
    tmp_path
): ...
```

Update `tests/test_execution_bindings.py` so the global binding reconciler is
exercised with one legacy and one composite batch. Assert only the legacy batch
changes.

**Step 4: Implement the query-level ownership boundary**

In `reconcile_strategy_management_batches`, constrain the query before loading
rows:

```python
query = session.query(StrategyManagementBatch).filter(
    StrategyManagementBatch.status.in_(_ACTIVE_RECONCILIATION_STATUSES),
    StrategyManagementBatch.management_contract_json.is_(None),
    StrategyManagementBatch.management_contract_fingerprint.is_(None),
    StrategyManagementBatch.contract_version.is_(None),
    ~StrategyManagementBatch.components.any(),
)
```

Do not add a per-loop `continue` as the primary boundary. Composite rows must
not enter the legacy reconciler's loaded working set.

**Step 5: Run focused reconciliation suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_strategy_management_reconciliation.py \
  tests/test_execution_bindings.py \
  tests/test_strategy_management_worker.py
git diff --check
```

Expected: PASS. Traditional management behavior remains unchanged.

**Step 6: Commit and review Task 1**

```bash
git add src/telegram_kol_research/strategy_management_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_execution_bindings.py
git commit -m "fix: isolate composite management reconciliation"
```

Request code review with special attention to every caller of
`reconcile_strategy_management_batches` and resolve all Critical/Important
findings before Task 2.

### Task 2: Build the closed, read-only recovery planner

**Files:**
- Create: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Create: `tests/test_composite_management_batch_recovery.py`
- Read: `src/telegram_kol_research/management_history_recovery.py`
- Read: `src/telegram_kol_research/deployment_preflight.py`

**Step 1: Write failing incident-profile and classification tests**

Create a closed profile and immutable result types:

```python
@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryProfile:
    batch_id: int
    raw_message_id: int
    lifecycle_id: int
    trusted_start_size: str
    target_remaining_size: str


BATCH_119_RECOVERY = CompositeBatchRecoveryProfile(
    batch_id=119,
    raw_message_id=10532,
    lifecycle_id=794,
    trusted_start_size="38",
    target_remaining_size="19",
)
```

The CLI-facing planner must reject any profile other than the allowlisted
incident. Unit helpers may accept an injected profile so fixtures remain small.

Add tests for the four position relations:

```python
@pytest.mark.parametrize(
    ("current", "disposition", "close_delta", "effective_remaining"),
    [
        ("38", "resume_to_target", "19", "19"),
        ("19", "protection_only_at_target", "0", "19"),
        ("12", "protection_only_below_target", "0", "12"),
        (None, "position_absent", "0", "0"),
    ],
)
def test_classify_recovery_position(...): ...
```

Also reject zero/negative, duplicate exact positions, an increased position
above the trusted start, a wrong side, and a wrong instrument.

**Step 2: Run the classification tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'profile or classify_recovery_position'
```

Expected: FAIL because the recovery module does not exist.

**Step 3: Implement immutable classifications**

Add:

```python
@dataclass(frozen=True, slots=True)
class CompositeRecoveryPosition:
    disposition: Literal[
        "resume_to_target",
        "protection_only_at_target",
        "protection_only_below_target",
        "position_absent",
    ]
    current_size: str | None
    close_delta: str
    effective_remaining_size: str
```

Use `Decimal` and the frozen quantity step/minimum quantity. Do not reuse a
percentage or parse the Telegram message.

**Step 4: Write failing exact-evidence planner tests**

Seed the production defect shape exactly:

```python
def test_batch_119_false_submission_is_ready_for_repair(tmp_path):
    factory, snapshot = seed_batch_119_false_submission(tmp_path)

    plan = build_composite_batch_recovery_plan(
        factory,
        profile=BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=NOW,
    )

    assert plan.status == "ready"
    assert plan.reason_code == "false_legacy_submission_proven"
    assert plan.position.disposition == "resume_to_target"
    assert plan.position.close_delta == "19"
    assert plan.production_writes == 0
    assert plan.exchange_calls == 0
    assert len(plan.evidence_fingerprint) == 64
```

Add a separate refusal test for each of:

- incomplete positions, trigger, order-history, fill, or pending-TP/SL snapshot;
- batch/lifecycle/binding/strategy/leg mismatch;
- contract fingerprint or component-topology mismatch;
- status other than the exact false state;
- non-null request, response, client order ID, or exchange order ID;
- a close execution event;
- a matching position mutation intent;
- a matching regular close order or fill;
- an additional active management batch/component/position mutation/instruction;
- current position above trusted start size;
- unexpected protection ownership; and
- source text or credentials accidentally appearing in serialized plan output.

**Step 5: Run planner tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'ready_for_repair or refuses or redacted'
```

Expected: FAIL because the planner is missing.

**Step 6: Implement a read-only, allowlisted planner**

Add:

```python
@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryPlan:
    batch_id: int
    status: Literal["ready", "refused"]
    reason_code: str
    position: CompositeRecoveryPosition | None
    source_fingerprint: str
    exchange_snapshot_fingerprint: str
    evidence_fingerprint: str
    evidence: Mapping[str, Any]
    production_writes: int = 0
    exchange_calls: int = 0
```

`build_composite_batch_recovery_plan` must:

1. inspect only the passed session factory and passed complete snapshot;
2. select one exact batch and exact components;
3. prove the production defect's source state;
4. search `ExecutionEvent` and `PositionMutationIntent` for durable submission
   evidence;
5. verify no other fresh active work using closed table/status queries;
6. compare exact exchange identity and order/fill evidence;
7. serialize only allowlisted stable facts; and
8. fingerprint canonical JSON with SHA-256.

Never include `pos_id`, order IDs, source message text, raw request/response
objects, provider error text, or credentials in retained CLI output. Use
redacted SHA-256 references where identity proof must be visible.

**Step 7: Prove the planner performs no database writes**

Open a SQLite database in `mode=ro`, capture main/WAL/SHM file signatures, run
the planner, and assert all signatures are unchanged. Patch every writer,
notifier, and executor entry point to raise if invoked.

**Step 8: Run Task 2 tests**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q tests/test_composite_management_batch_recovery.py
git diff --check
```

Expected: PASS with no network calls and no database writes.

**Step 9: Commit and review Task 2**

```bash
git add src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "feat: plan exact composite batch recovery"
```

Review for evidence completeness, redaction, exact identity, false-positive
submission proof, and any path that could generalize into an arbitrary editor.

### Task 3: Add the atomic false-state repair and immutable audit

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/strategy_management_components.py:15-39`
- Test: `tests/test_composite_management_batch_recovery.py`
- Test: `tests/test_strategy_management_components.py`

**Step 1: Write the failing apply compare-and-swap test**

```python
def test_apply_repairs_only_false_legacy_state_in_one_transaction(tmp_path):
    factory, snapshot = seed_batch_119_false_submission(tmp_path)
    plan = build_ready_plan(factory, snapshot)

    result = apply_composite_batch_false_state_repair(
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    assert result.status == "repaired"
    assert load_batch(factory, 119).status == "ready"
    assert load_leg(factory, 119).status == "planned"
    assert component_statuses(factory, 119) == [
        "recovery_required", "pending", "pending"
    ]
    assert close_submission_evidence(factory, 119) == []
    assert one_recovery_audit_event(factory, plan.evidence_fingerprint)
```

Assert the audit event preserves the old batch/leg/component status summary and
contains only fingerprints and bounded status facts.

**Step 2: Run the apply test and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_composite_management_batch_recovery.py::test_apply_repairs_only_false_legacy_state_in_one_transaction
```

Expected: FAIL because apply does not exist.

**Step 3: Add stale-plan, concurrent-change, and idempotency tests**

Cover each database field included in the source fingerprint. At minimum mutate
the batch status, leg status, one component status, request JSON, one order ID,
and add a matching mutation intent between planning and apply. Every case must
raise `CompositeBatchRecoveryConflict` and leave all rows unchanged.

Run two concurrent applies with the same plan. Exactly one creates the audit
event; the other returns `already_repaired`.

Patch the session factory to record SQL and assert `BEGIN IMMEDIATE` occurs
before the first apply read.

**Step 4: Implement the atomic repair**

Add:

```python
def apply_composite_batch_false_state_repair(
    session_factory,
    *,
    plan: CompositeBatchRecoveryPlan,
    expected_fingerprint: str,
    authorization: str,
    applied_at: datetime | None = None,
) -> CompositeBatchRecoveryApplyResult:
    if authorization != "I_AUTHORIZE_BATCH_119_TO_REMAINING_19":
        raise CompositeBatchRecoveryConflict("authorization_invalid")
    if plan.status != "ready" or plan.batch_id != 119:
        raise CompositeBatchRecoveryConflict("plan_not_actionable")
    if expected_fingerprint != plan.evidence_fingerprint:
        raise CompositeBatchRecoveryConflict("evidence_fingerprint_mismatch")
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        # Reread exact source rows, reconstruct source fingerprint, reject drift.
        # Insert the unique audit event before committing repaired local state.
        ...
```

For `resume_to_target` and `protection_only_at_target`:

- set the false legacy leg from `submitted` to `planned`;
- retain null request/response/order identity;
- set a bounded recovery reason in `last_error`;
- set the batch from `reconciling` to `ready`;
- preserve component 1 `recovery_required` and components 2/3 `pending`; and
- do not mark any exchange action successful.

Do not call the composite executor or Deepcoin inside this function.

**Step 5: Add under-target attestation persistence**

For `protection_only_below_target`, append the same bounded attestation to all
three component evidence histories inside the repair transaction:

```json
{
  "kind": "approved_under_target_recovery",
  "actual_remaining_size": "12",
  "original_target_remaining_size": "19",
  "recovery_evidence_fingerprint": "<sha256>"
}
```

The attestation does not change `desired_json`. Add tests proving component
identity remains immutable and the attestation cannot authorize an increased
position.

**Step 6: Add position-absent terminalization**

Permit `pending` and `recovery_required` to transition to `safely_skipped` only
for the dedicated recovery apply path. For `position_absent`:

- transition every nonterminal component to `safely_skipped` with the recovery
  fingerprint;
- set the false management leg to `failed` with
  `composite_recovery_exact_position_absent`;
- set the batch to `resolved` with the same reason;
- set `reconciled_at` and `completed_at`; and
- never create a close or protection intent.

Do not mark the batch `succeeded`, because the requested composite action was
not executed by this batch.

**Step 7: Run Task 3 tests**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_strategy_management_components.py \
  tests/test_management_history_recovery.py
git diff --check
```

Expected: PASS.

**Step 8: Commit and review Task 3**

```bash
git add src/telegram_kol_research/composite_management_batch_recovery.py \
  src/telegram_kol_research/strategy_management_components.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_strategy_management_components.py
git commit -m "feat: repair false composite submission atomically"
```

Review transaction ordering, unique audit behavior, evidence retention, and the
absence of any exchange call inside the apply transaction.

### Task 4: Resume the composite executor without recalculating 50 percent

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py:193-1300`
- Modify: `src/telegram_kol_research/strategy_management_composite_reconciliation.py:167-231`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write the failing original-target convergence test**

```python
def test_recovered_batch_closes_only_frozen_delta_38_to_19(tmp_path):
    factory, client = seed_repaired_batch(
        tmp_path, current_size="38", target_remaining="19"
    )

    result = execute_composite_management_batch(...)

    assert client.close_calls == [{"size": "19", "client_order_id": "CM119L103A1"}]
    assert all(call["size"] != "9" for call in client.close_calls)
    assert result.status in {"executing", "succeeded"}
```

The fixture must let component 1 confirm through the existing fake snapshot,
then exercise the real partial-close component and mutation gateway.

**Step 2: Run the test and verify existing behavior**

Run the test before production changes. It may already pass because the current
component uses `target_remaining_close_delta`. If it passes, retain it as a
characterization test and make the next under-target test the required RED.

**Step 3: Write failing at-target, under-target, and absent tests**

```python
def test_at_target_submits_no_close_and_replaces_protection_for_19(...): ...

def test_approved_under_target_submits_no_close_and_protects_actual_12(...):
    ...
    assert client.close_calls == []
    assert [row["sz"] for row in client.new_stop_calls] == ["12", "12"]

def test_under_target_without_exact_attestation_fails_closed(...): ...
def test_under_target_attestation_fails_if_position_changes(...): ...
def test_position_absent_terminal_plan_never_enters_executor(...): ...
```

**Step 4: Run the under-target tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q tests/test_strategy_management_executor.py \
  -k 'under_target or recovered_batch or at_target'
```

Expected: the approved under-target case fails with
`position_below_target_remaining`.

**Step 5: Implement a closed recovery-size helper**

Add a private helper that reads component evidence and returns an override only
when all of these are exact:

```python
def _approved_effective_remaining_size(
    *, desired: Mapping[str, Any], evidence: Any, current_size: object
) -> str | None:
    # Require one approved_under_target_recovery record.
    # Require its original target to equal desired["target_remaining_size"].
    # Require actual_remaining_size == current_size and 0 < actual < target.
    # Reject duplicates, malformed values, or any increase.
```

Use the helper in all three ordered components:

- take-profit consumption plans against the approved actual remaining size;
- partial close treats an exact approved actual size as a zero-delta confirmation
  and submits no writer call; and
- protection replacement sizes both new stops to the approved actual size.

Keep `desired_json` unchanged. Record the effective size in component evidence.

**Step 6: Preserve unknown-outcome and replacement ordering**

Add fault tests proving:

- an unknown close response yields `awaiting_exchange` and a second executor
  call makes zero new close requests;
- component 3 cannot run until component 2 is exchange-confirmed;
- both new stops are read back before either old stop is cancelled;
- failure to read back either new stop retains all old protection; and
- a position change after the approved fingerprint fails closed.

Do not alter `PositionMutationGateway` retry semantics.

**Step 7: Reconcile approved under-target evidence**

Update composite reconciliation so an exact approved under-target component can
be confirmed from current position truth without inventing a close intent. The
normal path still requires a durable mutation intent.

Add a negative test showing that missing or mismatched attestation remains
`awaiting`/`operator_required`, never `confirmed`.

**Step 8: Run composite and gateway suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py \
  tests/test_composite_management_batch_recovery.py
git diff --check
```

Expected: PASS with fake clients only.

**Step 9: Commit and review Task 4**

```bash
git add src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/strategy_management_composite_reconciliation.py \
  tests/test_strategy_management_executor.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: resume composite recovery to immutable target"
```

Review all writer boundaries, effective-size derivation, and no-retry behavior.

### Task 5: Expose the exact dry-run/apply CLI

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/cli.py:4490-4580`
- Modify: `tests/test_cli_smoke.py`
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write failing CLI boundary tests**

Add tests for:

- required `--batch-id` and existing `--database-path`;
- batch IDs other than 119 rejected before client construction;
- default dry-run opens the database read-only and produces compact allowlisted
  JSON;
- refused dry-run exits `2`;
- `--apply` requires both `--expected-fingerprint` and
  `--authorization I_AUTHORIZE_BATCH_119_TO_REMAINING_19`;
- stale fingerprint exits `2` before state repair;
- no notifier, listener, recognition, auto-trade, or generic management planner
  is invoked; and
- the position-absent result never constructs a writer.

Use command name `recover-composite-management-batch`.

**Step 2: Run CLI tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q tests/test_cli_smoke.py \
  -k 'recover_composite_management_batch'
```

Expected: FAIL because the command is missing.

**Step 3: Implement the lazy CLI command**

Add:

```python
@app.command("recover-composite-management-batch")
def recover_composite_management_batch(
    database_path: Path = typer.Option(..., "--database-path"),
    batch_id: int = typer.Option(..., "--batch-id", min=1),
    deepcoin_contract_specs_path: Path = typer.Option(
        ..., "--deepcoin-contract-specs-path"
    ),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    authorization: str | None = typer.Option(None, "--authorization"),
) -> None:
    ...
```

Validate `batch_id == 119`, the file path, and apply arguments before loading
environment configuration or constructing a client.

Dry-run uses an existing-file, read-only SQLite session and
`load_deepcoin_execution_reconciliation_snapshot_read_only`. It emits the
allowlisted plan and exits without a write.

Apply performs this order:

1. obtain a fresh complete snapshot;
2. rebuild the plan;
3. compare the fingerprint;
4. atomically repair only the false local state;
5. return immediately for `position_absent`; otherwise
6. load the contract-spec provider;
7. call `execute_composite_management_batch` once with the existing live gate;
8. emit repaired batch/component status and invariant counters.

The command must never loop a writer. A later invocation may reconcile/resume
through existing idempotent state if the first returns `awaiting_exchange`.

**Step 4: Add end-to-end fake-client tests**

Exercise dry-run then apply with the same test database and a stable fake
snapshot. Assert:

- the dry-run writes zero bytes;
- apply changes no unrelated row;
- size 38 generates one exact close of 19;
- the execution event and mutation intent are durable;
- a repeated command does not submit again; and
- MiMo/trading settings are unchanged.

**Step 5: Run Task 5 tests**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_cli_smoke.py -k 'recover_composite_management_batch or cli_help' \
  tests/test_composite_management_batch_recovery.py \
  tests/test_strategy_management_executor.py
git diff --check
```

Expected: PASS.

**Step 6: Commit and review Task 5**

```bash
git add src/telegram_kol_research/composite_management_batch_recovery.py \
  src/telegram_kol_research/cli.py \
  tests/test_cli_smoke.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "feat: add exact batch 119 recovery command"
```

Review CLI validation ordering, database read-only behavior, single-call writer
boundary, output redaction, and exit codes.

### Task 6: Document operations and complete regression review

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`
- Test: all affected suites

**Step 1: Document the exact incident procedure**

Add a runbook section named `Batch 119 composite-management recovery` covering:

- root cause and proof that no close was submitted;
- dry-run command and allowlisted output fields;
- exact authorization value;
- immutable target remaining size `19`;
- the four position dispositions;
- no-other-active-work requirement;
- service stop, database backup, apply, verification, and restart order;
- rollback boundary before versus after a possible exchange request; and
- the requirement that MiMo remains `v1`.

Do not include credentials, position IDs, order IDs, or source message text.

**Step 2: Run focused management safety suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_components.py \
  tests/test_execution_bindings.py \
  tests/test_position_mutation_gateway.py \
  tests/test_management_history_recovery.py \
  tests/test_cli_smoke.py -k 'management or recovery or cli_help'
```

Expected: PASS with no network calls.

**Step 3: Run execution-critical regressions**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q \
  tests/test_auto_trade_execution.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_instruction_execution_management_adapter.py \
  tests/test_message_operation_projection.py \
  tests/test_production_safety_monitor.py \
  tests/test_deployment_preflight.py
```

Expected: PASS.

**Step 4: Run the full local suite**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" \
  -m pytest -q
git diff --check
git status --short
```

Expected: PASS; record any platform-only skip exactly.

**Step 5: Perform independent review**

Use @requesting-code-review to review the complete recovery range against the
approved design. The review must explicitly verify:

- legacy/composite ownership is exclusive;
- no path calculates 50% of current size;
- no path adds exposure;
- no exchange writer runs before durable reservation;
- unknown results never retry;
- new protection precedes old-protection cancellation;
- the apply lock covers the first reread through commit;
- dry-run is genuinely read-only;
- normal deployment preflight remains strict; and
- MiMo settings are untouched.

Resolve all Critical/Important findings, rerun affected suites, then rerun the
full suite.

**Step 6: Commit documentation/review corrections**

```bash
git add docs/runbook.md docs/migration-handoff.md <reviewed-correction-files>
git commit -m "docs: add batch 119 recovery runbook"
```

Skip production-code paths in the commit command if no review correction
changed them.

**Step 7: Push without deploying**

Fetch the target branch, prove it has not moved unexpectedly, integrate with a
normal non-force update if required, and push the reviewed HEAD to:

```text
origin/codex/deepcoin-auto-trading-v1
```

Do not force push. Confirm the remote SHA equals local HEAD. Stop if history has
unexpectedly diverged.

### Task 7: Controlled production deployment and batch recovery

**Files:**
- No source edits expected.
- Write evidence only to a new root-owned server artifact directory.
- Update `docs/migration-handoff.md` afterward only with redacted results.

**Step 1: Confirm the running baseline read-only**

Using the project SSH key, record only:

- running commit and service state;
- effective `mimo_contract_mode`;
- batch 119/batch-leg/component bounded statuses;
- maximum terminal raw-message ID;
- counts of other active instructions, mutation intents, recoveries, management
  batches/components, and unknown outcomes; and
- the latest complete exchange snapshot timestamp.

Expected: MiMo is `v1`; batch 119 matches the reviewed false state; every other
active/unknown count is zero. If not, stop without deployment.

**Step 2: Fetch the candidate without changing the running checkout**

Fetch the exact reviewed SHA. Create a root-owned detached candidate worktree in
a new narrow path such as:

```text
/opt/telegram-kol-recovery-candidate-<short-sha>
```

Do not change `/opt/telegram-kol-analyzer`, its editable install, or the running
service in this step.

**Step 3: Run candidate dry-run against production read-only**

Run the candidate module with `PYTHONPATH` pointing at the detached candidate,
the existing production database, existing contract specs, and server
credentials loaded through the established root-only environment mechanism:

```bash
python -m telegram_kol_research.cli recover-composite-management-batch \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --batch-id 119 \
  --deepcoin-contract-specs-path \
    /opt/telegram-kol-analyzer/config/deepcoin_contract_specs.yaml
```

Expected: `status=ready`, `close_delta` derived from current minus 19, complete
snapshot, zero writes, and no unclassified or genuinely active instruction
work. The durable evidence must include the reviewed instruction-population
counts and SHA-256 digest without raw instruction, strategy, position, or order
identities. Save only the redacted JSON in a new `0700` artifact directory.

If the position relation is not the reviewed relation, stop and inspect the new
plan before continuing. Do not reuse an earlier fingerprint.

**Step 4: Prove the controlled recovery window twice**

Run the bounded read-only active-work check twice around a fresh exchange
snapshot. Both checks must prove:

- batch 119 is the only false-active management row;
- the instruction-population classifier returns exactly one
  `target_incident_frozen` row, all other nonterminal-looking rows are closed
  durable mirrors or frozen history, and no row is unclassified;
- the instruction-population counts and digest match across both checks;
- no mutation intent or unrelated durable descendant is executing, submitting,
  unknown, or recovery-required;
- no other management, revision, deletion, entry, recovery, or protection write
  is in flight; and
- every open position has verified protection.

Do not infer a disposition from elapsed time or raw instruction status. The
classifier must bind each row to exact durable evidence, and it never changes,
retires, replays, or backfills historical instruction rows.

**Step 5: Stop, back up, and revalidate before installing**

Stop `telegram-kol.service`. Immediately:

1. prove the service is inactive;
2. copy the SQLite database and sidecars with the established verified backup
   procedure;
3. validate the backup opens and contains the expected schema/counts;
4. rerun the candidate dry-run; and
5. require the same source fingerprint, instruction-population digest, and the
   reviewed current exchange relation.

If any check fails, restart the unchanged service and stop.

**Step 6: Install the reviewed code while keeping MiMo v1**

Update `/opt/telegram-kol-analyzer` to the exact reviewed SHA without force or a
destructive reset, reinstall the editable package, and run additive schema
bootstrap. Query settings before and after; `mimo_contract_mode` must remain
effectively `v1`.

Do not start the service yet.

**Step 7: Generate the final fingerprint and apply once**

Run the installed command once more in dry-run mode. If ready, invoke:

```bash
python -m telegram_kol_research.cli recover-composite-management-batch \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --batch-id 119 \
  --deepcoin-contract-specs-path \
    /opt/telegram-kol-analyzer/config/deepcoin_contract_specs.yaml \
  --apply \
  --expected-fingerprint '<FINAL_FINGERPRINT>' \
  --authorization I_AUTHORIZE_BATCH_119_TO_REMAINING_19
```

Expected by current known facts: one exact close delta from 38 to 19, followed
by verified break-even primary/backup protection. Treat the actual fresh plan as
authoritative if the position has safely moved.

Do not invoke apply a second time to “make sure.” Inspect durable result first.

**Step 8: Verify exchange and durable truth**

From complete read-only snapshots and bounded database queries, verify:

- actual position size equals the plan's effective remaining size;
- no duplicate regular close exists;
- any close mutation intent is terminal or protected awaiting reconciliation;
- new primary and backup stops are both verified and correctly sized;
- old stops were cancelled only after replacements existed;
- retained take-profit size does not exceed the position;
- component ordering and status are coherent;
- the batch is terminal or truthfully awaiting one known exchange result; and
- exactly one recovery audit event exists.

If a request outcome is unknown, do not restore the backup and do not resubmit.
Keep the service stopped only long enough to finish a bounded read-only
reconciliation check, then follow the documented recovery state.

**Step 9: Restart and verify continuity**

Start `telegram-kol.service` and verify:

- systemd reports `active`;
- HTTP health responds;
- Telegram intake advances naturally;
- the management worker no longer logs
  `composite_batch_not_executable:reconciling` for batch 119;
- no duplicate exchange write occurs;
- the production commit is exact; and
- MiMo remains `v1`.

**Step 10: Record redacted production evidence**

Record commit, artifact fingerprint, position disposition, close delta, final
batch/component states, protection verification, service health, MiMo mode, and
rollback boundary. Do not record credentials, raw source text, position IDs, or
order IDs.

Commit and push documentation-only evidence if required.

**Step 11: Stop before resuming MiMo rollout**

Batch 119 must be terminal and production stable before returning to the paused
MiMo Task 15 isolated replay. Do not enable `v2_live_adapter` in this plan. Ask
for a separate continuation before MiMo activation.

## Final Verification Checklist

- [ ] Composite batches never enter legacy reconciliation.
- [ ] Traditional management reconciliation remains unchanged.
- [ ] Dry-run is read-only and redacted.
- [ ] Only the exact production incident is actionable.
- [ ] Batch 119 has no prior close submission evidence.
- [ ] Recovery target is remaining `19`, never 50% of current size.
- [ ] No path increases exposure.
- [ ] Unknown results never retry.
- [ ] Replacement protection exists before old protection is cancelled.
- [ ] Full local suite and independent review pass.
- [ ] Production recovery uses one fresh final fingerprint.
- [ ] Service and Telegram intake recover on the reviewed commit.
- [ ] MiMo remains `v1`.
