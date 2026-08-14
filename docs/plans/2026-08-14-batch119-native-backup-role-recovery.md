# Batch 119 Native Backup-Stop Role Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the batch-119 exact-history loader recognize the existing exchange-native SL backup order only when four independent durable authorities prove its logical `backup_stop` role, then replace the invalid running-service dry-run with a separately approved stopped-service double-capture procedure.

**Architecture:** Keep generic protection-ledger semantics and production history unchanged. Add a batch-119-only role resolver that joins the verified ledger to the canonical primary or backup authorities, fingerprints the complete role proof, and reuses the fingerprint in planner/apply/resume CAS. Keep all server operations outside implementation; the runbook will require a later, separately approved stopped-service read-only window.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite, pytest, Deepcoin exact-ID read APIs, existing batch-119 snapshot authority and recovery CLI.

---

## Preconditions

- Work only in the dedicated `codex/mimo-v2-safe-rebuild` worktree.
- Start from design commit `d7360d82162b2d9016bc023a6613c88fc005c622` or a reviewed descendant.
- Use `@systematic-debugging` before changing behavior,
  `@test-driven-development` for every production change, and
  `@requesting-code-review` before completion.
- Do not push, deploy, stop/restart the service, run a production dry-run,
  bootstrap the production database, change settings, run `--apply`, or call an
  exchange writer while executing Tasks 1–6.
- Never serialize raw order IDs, position IDs, client IDs, provider JSON, or
  credentials into a plan, log, assertion message, or error.

### Task 1: Reproduce the production native-SL backup topology

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py:1-80`
- Modify: `tests/test_composite_management_batch_recovery.py:567-860`
- Modify: `tests/test_composite_management_batch_recovery.py:10640-10850`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Import the exact durable authority models**

Add these models to the existing test import block:

```python
from telegram_kol_research.models import (
    PositionBackupStopOrder,
    PositionProtectionLeg,
    TriggerProtectionIntent,
)
```

Reuse the existing `PositionMutationIntent` import.

**Step 2: Add one centralized production-shape fixture helper**

Add a helper beside `_seed_batch_119_false_submission` that converts the
existing canonical backup fixture into the production topology. It must:

```python
def _seed_native_sl_role_authority(session, *, binding, entry, strategy_id):
    primary = session.query(PositionProtectionLedger).filter_by(
        order_id=PRIMARY_ORDER_ID
    ).one()
    backup = session.query(PositionProtectionLedger).filter_by(
        order_id=BACKUP_ORDER_ID
    ).one()

    # Primary authority.
    trigger_intent = TriggerProtectionIntent(
        venue="deepcoin",
        execution_binding_id=binding.id,
        execution_order_leg_id=entry.id,
        request_fingerprint="1" * 64,
        pre_submit_tpsl_baseline_json="[]",
        correlation_id="batch119-primary-authority",
        parent_trigger_order_id="synthetic-parent-trigger",
        recovery_state="adopted",
        adopted_order_id=PRIMARY_ORDER_ID,
        retry_attempts=0,
        created_at=INCIDENT_STARTED,
        updated_at=NOW,
    )
    session.add(trigger_intent)
    session.flush()
    primary.evidence_source = "reconciliation_trigger_protection_intent"
    primary.evidence_json = json.dumps({"intent_id": trigger_intent.id})

    # Exchange-native backup authority. Build the request fingerprint from the
    # exact payload using the same canonical JSON SHA-256 algorithm as
    # position_mutation_intents._request_fingerprint.
    payload = {
        "instType": "SWAP",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "mrgPosition": "split",
        "tdMode": "cross",
        "posId": POS_ID,
        "slTriggerPx": "63000",
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
    }
    base_authority = "2" * 64
    request = {
        **payload,
        "_ledger_purpose": "stop_loss",
        "_base_authority_fingerprint": base_authority,
    }
    request_fingerprint = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    mutation = PositionMutationIntent(
        idempotency_key=(
            f"trigger-backup-stop:{binding.id}:{entry.id}:{POS_ID}:set"
        ),
        venue="deepcoin",
        operation="set_position_sltp",
        strategy_instance_id=strategy_id,
        execution_binding_id=binding.id,
        execution_order_leg_id=entry.id,
        pos_id=POS_ID,
        order_id=BACKUP_ORDER_ID,
        authority_fingerprint=base_authority,
        request_fingerprint=request_fingerprint,
        status="confirmed",
        request_json=json.dumps(request, sort_keys=True, separators=(",", ":")),
        response_json=json.dumps(
            {"code": "0", "data": [{"ordId": BACKUP_ORDER_ID}]},
            sort_keys=True,
            separators=(",", ":"),
        ),
        reserved_at=INCIDENT_STARTED,
        submitted_at=INCIDENT_STARTED,
        confirmed_at=NOW,
        created_at=INCIDENT_STARTED,
        updated_at=NOW,
    )
    session.add(mutation)
    session.flush()
    backup.purpose = "stop_loss"
    backup.size_text = None
    backup.evidence_source = "position_mutation_intent_readback"
    backup.evidence_json = json.dumps({"intent_id": mutation.id})

    session.add_all(
        [
            PositionBackupStopOrder(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry.id,
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                trigger_price="63000",
                order_id=BACKUP_ORDER_ID,
                client_order_id="synthetic-backup-client",
                status="active",
                request_json=json.dumps(payload, sort_keys=True),
                response_json=mutation.response_json,
                submitted_at=INCIDENT_STARTED,
                completed_at=NOW,
                created_at=INCIDENT_STARTED,
                updated_at=NOW,
            ),
            PositionProtectionLeg(
                protection_leg_id="batch119-primary-leg",
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry.id,
                role="primary_stop",
                leg_index=1,
                planned_trigger_price="64000",
                planned_size="38",
                pos_id=POS_ID,
                exchange_order_id=PRIMARY_ORDER_ID,
                status="verified",
                created_at=INCIDENT_STARTED,
                updated_at=NOW,
            ),
            PositionProtectionLeg(
                protection_leg_id="batch119-backup-leg",
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry.id,
                role="backup_stop",
                leg_index=1,
                planned_trigger_price="63000",
                planned_size="0",
                pos_id=POS_ID,
                exchange_order_id=BACKUP_ORDER_ID,
                status="verified",
                created_at=INCIDENT_STARTED,
                updated_at=NOW,
            ),
        ]
    )
```

Use a single helper so later attack tests mutate one fact at a time. Do not
duplicate this setup across individual tests.

**Step 3: Write the failing production-shape test**

```python
def test_batch119_exact_scope_resolves_native_sl_backup_from_closed_authority(
    tmp_path,
):
    module = _recovery_module()
    factory, *_ = _seed_batch_119_false_submission(
        tmp_path, native_sl_backup=True
    )

    database_path = tmp_path / "batch-119.db"
    before = database_path.read_bytes()
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )

    assert snapshot.errors == {}
    assert snapshot.exact_scope.protection_orders == (
        ("backup_stop", BACKUP_ORDER_ID),
        ("stop_loss", PRIMARY_ORDER_ID),
    )
    assert database_path.read_bytes() == before
```

This uses the same private-copy byte comparison already used by the surrounding
recovery tests; do not add a second database-signature abstraction.

**Step 4: Run the RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py::test_batch119_exact_scope_resolves_native_sl_backup_from_closed_authority
```

Expected: FAIL because the old scope builder returns
`{"exact_scope": "exact_history_scope_invalid"}` before any fake-client GET.

**Step 5: Commit only after Task 2 turns the RED green**

Do not commit a knowingly failing branch. Task 1 and Task 2 form one RED/GREEN
commit.

### Task 2: Resolve primary and backup roles from exact durable authority

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:30-90`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:689-855`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:1300-1460`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Add model and strict-parser imports**

Import `PositionBackupStopOrder`, `PositionProtectionLeg`,
`TriggerProtectionIntent`, and `PositionMutationIntent`. Import
`load_validated_set_position_request` and `PositionMutationIntentError` from
`position_mutation_intents`.

Do not import or call an exchange client from the resolver.

**Step 2: Add an internal resolved-role record**

```python
@dataclass(frozen=True, slots=True)
class _Batch119ResolvedStopRole:
    logical_role: Literal["stop_loss", "backup_stop"]
    order_id: str = field(repr=False)
    evidence_fingerprint: str
```

The record must never expose its order ID through `repr`.

**Step 3: Implement strict identity helpers**

Add small helpers with closed inputs:

```python
def _batch119_exact_owner(row, *, batch, binding, entry, leg) -> bool: ...
def _batch119_equal_decimal(left, right, *, allow_zero: bool) -> bool: ...
def _batch119_strict_json_object(raw, *, max_bytes: int = 16_384) -> dict: ...
def _batch119_unique_response_order_id(raw) -> str: ...
def _batch119_valid_timestamp_chain(*values) -> bool: ...
```

Requirements:

- booleans are never accepted as IDs or numbers;
- JSON rejects duplicate keys, non-finite values, excess bytes, depth, and
  node count;
- response parsing gathers every recognized order-ID field and requires one
  unique safe identity;
- trigger values are finite, bounded, and positive;
- size values are finite, bounded, and nonnegative;
- timestamps are real datetimes with
  `created/reserved <= submitted <= confirmed/completed <= updated` where the
  corresponding fields exist.

**Step 4: Implement the closed role resolver**

Add:

```python
def _resolve_batch119_stop_roles(
    session,
    *,
    rows,
    batch,
    binding,
    entry,
    leg,
) -> tuple[_Batch119ResolvedStopRole, _Batch119ResolvedStopRole]:
    ...
```

The implementation must:

1. Select only exact-owner verified native stop rows.
2. Require exactly two distinct safe exchange order IDs.
3. Resolve the primary by exact adopted `TriggerProtectionIntent` plus one
   verified `primary_stop` protection leg.
4. Resolve the backup by exact confirmed `PositionMutationIntent`, one active
   `PositionBackupStopOrder`, and one verified `backup_stop` protection leg.
5. Validate the mutation with `load_validated_set_position_request`; require
   operation `set_position_sltp`, canonical idempotency key, exact request and
   response order identity, SL-only payload, exact owner, and valid timestamp
   chain.
6. Require trigger equality across every backup source.
7. Accept omitted `sz` only when the backup request is the exact no-`sz`
   whole-position form and the protection leg has `planned_size == "0"`.
8. Require no extra primary/backup intent, leg, active backup row, or order
   owner.
9. Build one fingerprint per role from hashed IDs and canonical authority-row
   payloads. Include table role/state, economics markers, request/response
   fingerprints, and evidence fingerprints; never include raw IDs.
10. Return the roles sorted by `logical_role`.

Every validation failure raises
`CompositeBatchRecoveryRefusal("exact_history_scope_invalid")`.

**Step 5: Bind the resolver into exact-scope construction**

In `_build_batch119_exact_history_scope_in_session`, call the resolver before
constructing `_Batch119ExactHistoryScope`. Change
`_batch119_exact_history_scope_from_rows` to consume the two resolved role
records instead of interpreting `row.purpose` as the logical role.

Bump `_batch119_exact_scope_fingerprint` to schema version `4`. The fingerprint
must use each resolved record's logical role, hashed order identity, and full
role-authority fingerprint.

Keep the existing superseded-audit path closed: it may exempt an original
superseded order only through its already reviewed audit contract; it must not
manufacture a backup role.

**Step 6: Run focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'exact_scope or exact_loader or native_sl_backup'
```

Expected: PASS, including the new production-shape test and existing six-GET
call-order test.

**Step 7: Commit the first GREEN**

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: resolve batch119 native backup role"
```

### Task 3: Close malformed, conflicting, and partial authority paths

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`

**Step 1: Add a one-field attack matrix**

Create parametrized tests that start from `native_sl_backup=True`, mutate one
authority fact, and assert pre-network refusal. Cover at least:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_mutation_intent",
        "duplicate_mutation_intent",
        "mutation_wrong_owner",
        "mutation_wrong_operation",
        "mutation_wrong_status",
        "mutation_wrong_order",
        "mutation_bad_request_fingerprint",
        "mutation_multiple_response_orders",
        "backup_row_missing",
        "backup_row_not_active",
        "backup_row_wrong_owner",
        "backup_row_wrong_order",
        "backup_leg_missing",
        "backup_leg_duplicate",
        "backup_leg_not_verified",
        "backup_leg_wrong_owner",
        "backup_leg_wrong_order",
        "primary_intent_missing",
        "primary_intent_not_adopted",
        "primary_leg_missing",
        "primary_leg_wrong_order",
        "duplicate_role_order",
        "trigger_drift",
        "size_present_drift",
        "size_missing_without_whole_position",
    ],
)
def test_batch119_native_stop_role_authority_fails_closed_before_network(...):
    ...
    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0
```

Use a strict fake whose every read and writer raises if called.

**Step 2: Add hostile JSON and identity cases**

Cover duplicate keys, excessive depth, excessive bytes, NaN/Infinity, booleans
as IDs, unsafe order identity, credential-shaped identity, wrong/missing
`posId`, and multiple different order IDs in response containers.

Assert that exception text, snapshot `repr`, and serialized refusal contain
none of the raw hostile values.

**Step 3: Add valid sparse-provider cases**

Prove the resolver still accepts:

- the production no-`sz` whole-position backup request;
- canonical-equivalent decimal spellings such as `63000`, `63000.0`, and
  `6.3e4` when every authority agrees economically; and
- provider history rows that omit optional `posId` only where the existing
  exact-history contract explicitly permits sparse identity.

Do not relax durable ORM identity fields.

**Step 4: Run RED, implement the smallest validation, rerun GREEN**

For each group, run its test before modifying production code and record the
expected failure. Then add only the validation required for that group.

Run the complete matrix:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'native_stop_role_authority or native_sl_backup'
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: close batch119 role authority conflicts"
```

### Task 4: Bind role authority to planner, apply, and resume CAS

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py:10700-10950`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:3000-3900`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:4400-4620`

**Step 1: Write post-capture drift REDs**

After a valid loader-issued snapshot, mutate or delete one of:

- primary trigger intent;
- primary protection leg;
- backup mutation intent;
- active backup row;
- backup protection leg;
- either ledger role source/economics/evidence; or
- insert an additional conflicting role authority.

For each mutation assert:

```python
assert plan.status == "refused"
assert plan.reason_code == "durable_snapshot_scope_mismatch"
```

Add equivalent apply and resume tests. Apply must raise
`CompositeBatchRecoveryConflict` before any ORM mutation or writer factory.
Resume must refuse before returning authorization. Compare database signatures
and writer call counts.

**Step 2: Rebuild role authority in every locked path**

Ensure `_load_locked_recovery_source`, position-absent snapshot validation,
apply repeat handling, and resume authorization all call the same exact-scope
builder in the same `BEGIN IMMEDIATE` transaction that checks the reviewed
scope fingerprint.

Do not copy role facts from the plan into current authority. Requery the ORM
rows and rebuild all fingerprints.

**Step 3: Run focused CAS tests**

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'native_sl_backup and (drift or apply or resume or repeat)'
```

Expected: PASS; all rejected cases show database byte/signature equality and
writer calls `0`.

**Step 4: Commit**

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: bind batch119 role authority to recovery CAS"
```

### Task 5: Prove generic isolation and exact read bounds

**Files:**
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_backup_stop_repair.py`
- Modify only if a regression is real: corresponding production file

**Step 1: Keep generic backup semantics unchanged**

Add or retain an assertion that the generic backup executor may persist the
exchange-native ledger purpose `stop_loss` while its dedicated
`PositionBackupStopOrder` and `PositionProtectionLeg` retain logical backup
authority. The new resolver must not be imported or called from the executor.

**Step 2: Reassert exact-loader reachability**

The production-shape success case must call exactly:

1. `read_positions(inst_id=...)`;
2. `read_open_orders(inst_id=...)`;
3. `read_trigger_orders_pending(inst_id=...)`;
4. `read_position_history(inst_id=..., pos_id=...)`;
5. one `read_trigger_order_history(..., order_id=..., limit=100)` per resolved
   stop.

Assert six total GETs, zero broad history calls, and zero POST/cancel/close/TPSL
writer reachability. Exact row-100 ambiguity remains refused.

**Step 3: Reassert protected-entry and generic snapshot isolation**

Use the existing AST/call-boundary tests to prove the batch119 loader and role
resolver remain reachable only from the allowlisted recovery CLI path. Do not
weaken generic pagination or snapshot authority.

**Step 4: Run adjacent suites**

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_execution_bindings.py \
  tests/test_backup_stop_repair.py \
  tests/test_protection_ledger.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protected_entry_reconciliation.py
```

Expected: PASS.

**Step 5: Commit only if Task 5 changes files**

```bash
git add tests
git commit -m "test: isolate batch119 native role resolution"
```

Do not create an empty commit if existing tests already prove the boundary.

### Task 6: Replace the invalid running-service dry-run procedure

**Files:**
- Modify: `docs/runbook.md:1540-1665`
- Modify: `docs/plans/2026-08-13-batch119-exact-history-recovery.md:500-570`
- Test/inspect: `tests/test_cli_smoke.py:2870-4150`

**Step 1: Update the runbook precondition**

State explicitly that the old deployed service does not maintain candidate
write generation and that an empty production generation table is not a valid
fence. Forbid the running-service procedure for this recovery.

**Step 2: Document a separately approved stopped-service window**

The shell procedure must:

- verify the exact reviewed remote SHA and detached candidate worktree;
- record the original production SHA and service active state;
- require a separate operator approval before `systemctl stop`;
- stop the service and prove it is inactive;
- reject any durable active/unknown writer operation or another local process
  holding the Deepcoin environment;
- create a mode-0700 temporary directory and a fresh mode-0600 SQLite `.backup`
  after the stop;
- bootstrap only that copy;
- use the copy for both `--database-path` and
  `--generation-database-path` during dry-run;
- run two separate fresh copies/captures;
- compare only the documented stable semantic fingerprints and role evidence;
- run no `--apply`, deployment, setting write, production bootstrap, or exchange
  mutation; and
- restore the unchanged original service before returning, including a bounded
  cleanup path for a diagnostic refusal.

Do not place credentials, raw IDs, or complete provider output in the operator
record.

**Step 3: Keep future apply separate**

Document that a later apply approval must repeat the stopped-service final
snapshot and use the production database for both database arguments. It must
not reuse the diagnostic copy, reviewed fingerprint, or stopped-service permit.

**Step 4: Verify CLI and documentation consistency**

Run:

```bash
.venv/bin/python -m telegram_kol_research.cli \
  recover-composite-management-batch --help
rg -n \
  'generation-database-path|systemctl stop|systemctl start|--apply|batch 119' \
  docs/runbook.md \
  docs/plans/2026-08-13-batch119-exact-history-recovery.md
git diff --check
```

Expected: help still lists the required generation path; docs require stopped
service and forbid same-turn apply/deploy.

**Step 5: Commit**

```bash
git add \
  docs/runbook.md \
  docs/plans/2026-08-13-batch119-exact-history-recovery.md
git commit -m "docs: require stopped batch119 exact capture"
```

### Task 7: Full verification and review checkpoint

**Files:**
- Review: all changes since `d7360d82162b2d9016bc023a6613c88fc005c622`
- No production/server changes

**Step 1: Run focused gates**

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'native_sl_backup or native_stop_role_authority or exact_scope or exact_loader'
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_cli_smoke.py \
  tests/test_deepcoin_client.py \
  tests/test_deepcoin_snapshot_authority.py \
  tests/test_execution_bindings.py \
  tests/test_backup_stop_repair.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protected_entry_reconciliation.py
```

Expected: PASS.

**Step 2: Run the complete local gate**

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check d7360d82162b2d9016bc023a6613c88fc005c622..HEAD
git status --short
```

Expected: full suite PASS, compile/diff checks exit `0`, and worktree clean.

**Step 3: Perform the required review**

Use `@requesting-code-review` and attack:

- primary/backup order and owner substitution;
- intent request/response and evidence forgery;
- omitted-size whole-position ambiguity;
- duplicate/extra authorities;
- post-capture and concurrent CAS drift;
- row-100, malformed JSON/SQLite, hostile IDs, and redaction;
- generic writer/reconciler reachability; and
- stopped-service runbook cleanup and no-apply boundary.

Do not proceed until the review reports zero Critical and zero Important
findings. Fix each finding through a new RED/GREEN commit and rerun the affected
and full gates.

**Step 4: Return control without server action**

Report reviewed commit SHAs, test counts, retained nonblocking findings, and the
fact that no push, server stop, production dry-run, apply, deployment, restart,
setting change, database bootstrap, or exchange mutation occurred.

The separately approved stopped-service double capture is the next operational
task. It is not part of this implementation plan execution.
