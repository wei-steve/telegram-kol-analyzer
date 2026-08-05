# Position Management Liveness Safety Gates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve exact `posId` and protection-order ownership while ensuring an unresolved native TPSL cannot permanently block independent, exact-position risk-reduction work.

**Architecture:** Replace per-intent, order-sensitive native-stop adoption with a pure account-wide mutual-unique assignment. Persist structured recovery state, compute operation-specific capabilities, and let an exact owned backup stop satisfy staged take-profit safety when native-stop ownership remains unresolved. Ship behind `position_management_liveness_v2_mode`, verify in shadow from a coherent production snapshot, and recover an existing position only through a reviewed fingerprinted plan.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, Typer, pytest, Deepcoin REST client, existing position authority lock and mutation gateway.

---

## Preconditions and invariants

- Work only on `codex/deepcoin-auto-trading-v1`.
- Preserve unrelated dirty-worktree files and stage only files named by each task.
- Never use symbol, side, price proximity, a lone remaining position, or `ordId = posId - 1` as standalone ownership proof.
- Never cancel or replace an order without exact ledger ownership.
- Every exchange write must use the existing account authority lock, durable mutation intent, exact-position final gate, idempotency key, and readback.
- Do not create a real test position, Telegram signal, or exchange order during verification.
- Deploy disabled, then shadow. Enable live only after a coherent snapshot proves the exact reviewed action set and no time-sensitive strategy work is active.
- A submit-unknown result is never retried blindly.

### Task 1: Add the pure account-wide native-protection assignment model

**Files:**
- Create: `src/telegram_kol_research/trigger_protection_assignment.py`
- Create: `tests/test_trigger_protection_assignment.py`

**Step 1: Write the failing production-shape assignment tests**

Create immutable inputs and outputs that contain only normalized evidence:

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProtectionOwner:
    leg_id: int
    binding_id: int
    pos_id: str
    instrument_id: str
    side: str
    size_text: str
    stop_price: str
    position_created_at: datetime


@dataclass(frozen=True, slots=True)
class ProtectionOrderCandidate:
    order_id: str
    instrument_id: str
    side: str
    size_text: str
    stop_price: str
    created_at: datetime
    explicit_pos_ids: tuple[str, ...] = ()


def test_assigns_identical_split_stops_after_excluding_existing_owner():
    result = assign_trigger_protection_orders(
        owners=(first_owner, second_owner),
        candidates=(first_stop, second_stop),
        existing_order_owners={first_stop.order_id: first_owner.pos_id},
        snapshot_complete=True,
    )
    assert result.assignments == {
        second_owner.leg_id: second_stop.order_id,
    }
    assert result.conflicts == ()


def test_assignment_is_independent_of_candidate_order():
    forward = assign_trigger_protection_orders(..., candidates=(first_stop, second_stop))
    reverse = assign_trigger_protection_orders(..., candidates=(second_stop, first_stop))
    assert forward == reverse


def test_prefill_candidate_is_excluded_without_blocking_newer_unique_candidate():
    result = assign_trigger_protection_orders(...)
    assert result.assignments[second_owner.leg_id] == second_stop.order_id
    assert result.exclusions[first_stop.order_id] == "candidate_predates_fill"


def test_true_many_to_many_shape_remains_unassigned():
    result = assign_trigger_protection_orders(...)
    assert result.assignments == {}
    assert result.conflicts[0].reason_code == "protection_assignment_not_mutual_unique"
```

Also cover explicit `posId`, conflicting position aliases, incomplete snapshots, missing timestamps, existing immutable-owner conflict, and candidates owned by another live position.

**Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_trigger_protection_assignment.py -q
```

Expected: collection failure because `trigger_protection_assignment` does not exist.

**Step 3: Implement normalized candidate construction and mutual-unique assignment**

Implement:

```python
@dataclass(frozen=True, slots=True)
class ProtectionAssignmentResult:
    assignments: dict[int, str]
    evidence_by_leg: dict[int, dict[str, object]]
    exclusions: dict[str, str]
    conflicts: tuple[ProtectionAssignmentConflict, ...]
    snapshot_fingerprint: str


def assign_trigger_protection_orders(
    *,
    owners: tuple[ProtectionOwner, ...],
    candidates: tuple[ProtectionOrderCandidate, ...],
    existing_order_owners: dict[str, str],
    snapshot_complete: bool,
) -> ProtectionAssignmentResult:
    ...
```

The function must:

1. validate exact existing owners first;
2. exclude already-owned orders from the unowned candidate pool;
3. exclude pre-fill orders per owner rather than returning from the whole plan;
4. accept explicit exact position identity before weaker evidence;
5. build all remaining owner/order edges;
6. accept only edges whose owner degree and order degree are both one;
7. sort every input before hashing and returning results;
8. return evidence, never write a database or call Deepcoin.

Do not implement maximum-weight guessing. Mutual uniqueness is the acceptance rule.

**Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest tests/test_trigger_protection_assignment.py -q
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trigger_protection_assignment.py tests/test_trigger_protection_assignment.py
git commit -m "fix: assign trigger protection account wide"
```

### Task 2: Persist structured protection recovery reasons and dispositions

**Files:**
- Modify: `src/telegram_kol_research/models.py:1507-1565`
- Modify: `src/telegram_kol_research/db.py:55-380`
- Modify: `src/telegram_kol_research/trigger_protection_intents.py:1-130`
- Modify: `tests/test_trigger_protection_intents.py`
- Modify: `tests/test_db_migrations.py`

**Step 1: Write failing model, transition, and migration tests**

Add assertions for three compatible columns:

```python
assert intent.last_reason_code == "candidate_visibility_pending"
assert intent.recovery_disposition == "retry"
assert json.loads(intent.last_evidence_json) == {"candidate_order_ids": ["stop-2"]}
```

Add a legacy SQLite database test proving `init_db()` adds:

```text
last_reason_code VARCHAR(128)
recovery_disposition VARCHAR(32)
last_evidence_json TEXT
```

without rewriting existing intent rows.

Add transition tests for:

```python
transition_trigger_protection_intent(
    session,
    intent,
    recovery_state="retrying",
    recovery_disposition="retry",
    last_reason_code="candidate_visibility_pending",
    last_evidence={"candidate_order_ids": ["stop-2"]},
)
```

Reject unknown recovery states, dispositions, oversized reason codes, and non-dictionary evidence.

**Step 2: Run the tests and verify they fail**

```bash
.venv/bin/pytest tests/test_trigger_protection_intents.py tests/test_db_migrations.py -q
```

Expected: missing attributes/columns.

**Step 3: Implement the compatible schema and transition API**

Add model fields and `SQLITE_COMPAT_COLUMNS["trigger_protection_intents"]` entries. Define stable dispositions:

```python
ALLOWED_TRIGGER_PROTECTION_RECOVERY_DISPOSITIONS = frozenset(
    {"wait", "retry", "exact_backup", "manual_review", "terminal"}
)
```

Serialize evidence with sorted keys and a bounded schema. Keep existing recovery-state values readable; new code maps legacy `failed` rows using the latest structured reason or a conservative `manual_review` fallback.

**Step 4: Run focused migration tests**

```bash
.venv/bin/pytest tests/test_trigger_protection_intents.py tests/test_db_migrations.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py src/telegram_kol_research/trigger_protection_intents.py tests/test_trigger_protection_intents.py tests/test_db_migrations.py
git commit -m "feat: persist protection recovery disposition"
```

### Task 3: Integrate global assignment into exchange reconciliation

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:1096-1525`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:1088-1332`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Add the failing two-filled-sibling integration regression**

Seed two trigger legs whose saved requests both contain:

```python
{"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "3.4", "slTriggerPx": "1935"}
```

Give them different parent trigger orders, exact verified position IDs, and position creation times. Return two anonymous stops in reverse order. Pre-own the first stop in the ledger.

Assert after reconciliation:

```python
assert intent_1.recovery_state == "adopted"
assert intent_2.recovery_state == "adopted"
assert intent_2.adopted_order_id == "second-stop"
assert ledger_by_order["second-stop"].pos_id == "second-pos"
assert result.protection_adopted == 1
```

Add a true ambiguous case that keeps both intents recoverable and writes no new ownership.

**Step 2: Run the regressions and verify the current failure**

```bash
.venv/bin/pytest \
  tests/test_execution_bindings.py::test_reconcile_assigns_second_identical_split_stop_globally \
  tests/test_execution_bindings.py::test_reconcile_keeps_true_anonymous_stop_ambiguity_recoverable \
  -q
```

Expected: the first test reproduces `candidate_predates_fill` or `candidate_not_unique`.

**Step 3: Replace per-intent candidate ownership with one snapshot-wide plan**

In `_reconcile_saved_trigger_protection_intents`:

1. normalize all eligible owners and pending/history candidates once;
2. load immutable ledger ownership once;
3. call `assign_trigger_protection_orders` once;
4. persist only assignments whose current recomputed fingerprint matches;
5. transition unmatched intents using structured disposition;
6. retain the existing `finalize_trigger_protection_adoption` immutable write boundary.

Keep `plan_trigger_protection_intent_adoption` as a compatibility wrapper for repair callers, but route it through the same pure assignment for a one-owner scope. Remove the loop-level immediate `candidate_predates_fill` refusal.

Use these reason mappings:

```python
REASON_DISPOSITIONS = {
    "candidate_not_yet_observable": "retry",
    "protection_assignment_not_mutual_unique": "exact_backup",
    "snapshot_incomplete": "wait",
    "immutable_owner_conflict": "manual_review",
}
```

**Step 4: Run reconciliation and legacy repair tests**

```bash
.venv/bin/pytest tests/test_execution_bindings.py tests/test_entry_protection_ledger_repair.py -q
```

Expected: pass, including API-order reversal and legacy fail-closed cases.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/entry_protection_ledger_repair.py tests/test_execution_bindings.py tests/test_entry_protection_ledger_repair.py
git commit -m "fix: reconcile anonymous stops without order dependence"
```

### Task 4: Add operation-specific position-management capabilities

**Files:**
- Create: `src/telegram_kol_research/position_management_capabilities.py`
- Create: `tests/test_position_management_capabilities.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `tests/test_strategy_management_planner.py`

**Step 1: Write failing capability tests**

Define a pure result:

```python
@dataclass(frozen=True, slots=True)
class PositionManagementCapabilities:
    may_cancel_owned_protection: bool
    may_replace_owned_protection: bool
    may_add_exact_backup_stop: bool
    may_add_exact_take_profit: bool
    may_reduce_exact_position: bool
    may_close_exact_position: bool
    reason_codes: tuple[str, ...]
```

Required scenarios:

```python
def test_unknown_native_stop_does_not_block_exact_backup_or_full_close():
    caps = evaluate_position_management_capabilities(
        exact_position_verified=True,
        native_stop_owned=False,
        exact_owned_stop=False,
        conflicting_unknown_take_profit=False,
        retained_take_profit_safe=True,
        snapshot_complete=True,
    )
    assert caps.may_cancel_owned_protection is False
    assert caps.may_add_exact_backup_stop is True
    assert caps.may_close_exact_position is True


def test_unknown_take_profit_blocks_add_tp_and_partial_not_full_close():
    ...
```

Also cover missing position ownership, stale/incomplete snapshots, submit-unknown mutation, retained TP overflow, and exact owned backup present.

**Step 2: Run and verify failure**

```bash
.venv/bin/pytest tests/test_position_management_capabilities.py -q
```

Expected: module missing.

**Step 3: Implement the pure capability evaluator**

The evaluator must not call the database or exchange. It must preserve these hard rules:

- no exact position ownership means no write capability;
- no complete current snapshot means no new write capability;
- unknown old protection blocks its cancellation/replacement only;
- conflicting unknown TP blocks additive TP and partial reduction that could over-exit;
- full exact close is independent of old protection ownership;
- any active/unknown mutation for the exact position suppresses overlapping writes.

**Step 4: Integrate capabilities into management planning**

Replace broad protection booleans in the relevant full-close, partial-close, protection-add, and replacement branches with explicit capability checks. Preserve existing exact target and fresh final-write gates.

Add assertions that a protection-order conflict on one `posId` does not block an independent component or sibling `posId`.

**Step 5: Run focused management tests**

```bash
.venv/bin/pytest tests/test_position_management_capabilities.py tests/test_strategy_management_planner.py -q
```

Expected: pass.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_management_capabilities.py src/telegram_kol_research/strategy_management_planner.py tests/test_position_management_capabilities.py tests/test_strategy_management_planner.py
git commit -m "refactor: authorize management by exact capability"
```

### Task 5: Permit an exact backup-stop fallback without adopting an unknown native stop

**Files:**
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py:250-420`
- Modify: `src/telegram_kol_research/trigger_protection_rescue_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py:2073-2331`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Create: `tests/test_trigger_backup_stop_executor.py`
- Modify: `tests/test_trigger_protection_stop_rescue.py`

**Step 1: Write failing fallback tests**

Seed:

- one exact verified live position;
- one active trigger entry leg with saved SL 1935;
- one intent with `recovery_disposition="exact_backup"`;
- no owned primary stop;
- an anonymous native stop that must remain untouched.

Assert the plan:

```python
assert plan.status == "ready"
assert plan.payload["posId"] == "second-pos"
assert plan.payload["slTriggerPx"] == "1935"  # or configured backup-buffer price
assert plan.cancel_order_ids == ()
```

Assert execution submits once, reads back the returned order ID, persists `PositionBackupStopOrder` and `PositionProtectionLedger`, and never calls a cancel API. Add submit-unknown idempotency and restart tests.

**Step 2: Run and verify the current block**

```bash
.venv/bin/pytest tests/test_trigger_backup_stop_executor.py tests/test_trigger_protection_stop_rescue.py -q
```

Expected: fallback case blocks with `primary_stop_not_verified` or rescue-ineligible.

**Step 3: Implement the fallback planner**

Add a separate branch rather than weakening `_pending_matches_primary`:

```python
if primary_stop is None and capabilities.may_add_exact_backup_stop:
    saved_stop = saved_trigger_stop(entry_leg.request_json)
    return build_exact_backup_fallback_plan(
        pos_id=pos_id,
        saved_stop=saved_stop,
        live_position=position,
        ...,
    )
```

The fallback must:

- require exact `ExecutionOrderLeg.pos_id`, current live position identity/size, saved stop, liquidation safety, and a complete snapshot;
- create no ownership for the anonymous native stop;
- submit through `submit_exact_position_sltp` with an idempotency key containing binding, leg, `posId`, and purpose;
- persist the returned/read-back order as an exact backup stop;
- stop on unknown exchange outcome.

Replace `_rescue_intent_is_deferred_or_ambiguous` string-token inspection with `recovery_disposition`.

**Step 4: Run the backup and rescue suites**

```bash
.venv/bin/pytest tests/test_trigger_backup_stop_executor.py tests/test_trigger_protection_stop_rescue.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trigger_backup_stop_executor.py src/telegram_kol_research/trigger_protection_rescue_worker.py src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_executor.py tests/test_trigger_backup_stop_executor.py tests/test_trigger_protection_stop_rescue.py
git commit -m "fix: add exact stop fallback for recoverable protection"
```

### Task 6: Decouple staged take profit from native-primary ownership

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:990-1076`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py:300-520`
- Modify: `tests/test_trigger_take_profit_convergence_executor.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write failing exact-backup-only convergence tests**

Create a verified trigger position with:

- no adopted native primary stop;
- one exact active backup stop whose request and pending readback identify the exact `posId`;
- desired TP targets 50/30/20;
- no unowned TP that could target this position.

Assert:

```python
plan = plan_trigger_take_profit_convergence(...)
assert plan.status == "ready"
assert [payload["tpTriggerPx"] for payload in plan.payloads] == ["1890", "1860", "1825"]
assert [payload["sz"] for payload in plan.payloads] == ["1.7", "1", "0.7"]
```

Add a counter-test where one unknown TP remains capable of targeting this exact position; expect `convergence_unowned_take_profit_present` and zero submission.

**Step 2: Run and verify the current block**

```bash
.venv/bin/pytest \
  tests/test_trigger_take_profit_convergence_executor.py::test_exact_backup_allows_tp_without_native_primary_ownership \
  tests/test_trigger_take_profit_convergence_executor.py::test_unknown_targeting_tp_still_blocks_additive_tp \
  -q
```

Expected: first case blocks with `convergence_verified_stop_missing`.

**Step 3: Implement `has_verified_exact_owned_stop`**

Replace the hard `primary AND backup` requirement with:

```python
owned_stop = verified_native_primary(...) or verified_exact_backup(...)
if not owned_stop:
    return "convergence_verified_stop_missing"
```

Keep exact backup verification strict: same binding, leg, `posId`, instrument, side, active local row, exact order ID, pending exchange readback, market-close semantics, and live position.

Update readiness so it records `convergence.pos_id` before moving to `ready`, including the exact stop evidence fingerprint. Do not relax `_unowned_pending_take_profit_present`.

**Step 4: Run TP convergence and reconciliation tests**

```bash
.venv/bin/pytest tests/test_trigger_take_profit_convergence_executor.py tests/test_execution_bindings.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/trigger_take_profit_convergence_executor.py tests/test_trigger_take_profit_convergence_executor.py tests/test_execution_bindings.py
git commit -m "fix: converge take profit from exact owned stop"
```

### Task 7: Add the disabled/shadow/live rollout gate

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `tests/test_trading_settings.py`
- Create: `tests/test_web_trading_settings.py`

**Step 1: Write failing settings tests**

Add:

```python
position_management_liveness_v2_mode: Literal["disabled", "shadow", "live"] = "disabled"
```

Test:

- absent legacy setting defaults to disabled;
- invalid values fail closed;
- shadow computes and persists evidence but invokes no exchange mutation;
- live is effective only when both global auto trade and live management execution are enabled;
- form/API round-trip preserves the setting.

**Step 2: Run and verify failure**

```bash
.venv/bin/pytest tests/test_trading_settings.py tests/test_web_trading_settings.py -q
```

Expected: setting missing.

**Step 3: Implement settings and wiring**

Add:

```python
@property
def effective_position_management_liveness_v2_mode(self) -> Literal["disabled", "shadow", "live"]:
    if self.position_management_liveness_v2_mode == "shadow":
        return "shadow"
    if (
        self.position_management_liveness_v2_mode == "live"
        and self.auto_trade_enabled
        and self.management_execution_mode == "live"
    ):
        return "live"
    return "disabled"
```

In disabled mode preserve the old runtime behavior exactly. In shadow mode run assignment/capability planning and store bounded evidence without exchange writes. In live mode enable exact fallback and TP convergence.

**Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_trading_settings.py tests/test_web_trading_settings.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py src/telegram_kol_research/web_app.py src/telegram_kol_research/web_queries.py tests/test_trading_settings.py tests/test_web_trading_settings.py
git commit -m "feat: gate position management liveness v2"
```

### Task 8: Make capability and recovery state observable

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_strategy_records.py`
- Modify: `tests/test_web_strategy_records.py`
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Write failing projection and monitor tests**

Require separate operator states:

```text
position_owner_verified
native_stop_assignment_pending
exact_backup_stop_verified
take_profit_convergence_waiting
take_profit_convergence_ready
risk_reduction_capability_available
manual_review_required
```

Add a regression proving a verified position with recoverable native-stop ambiguity is not displayed as “持仓归属未验证”. It should display “原生止损归属待恢复；精确仓位可继续风险降低操作”.

**Step 2: Run and verify failure**

```bash
.venv/bin/pytest tests/test_strategy_records.py tests/test_web_strategy_records.py tests/test_production_safety_monitor.py -q
```

Expected: projection lacks structured fields.

**Step 3: Implement bounded projections and alert classification**

Expose reason codes and booleans, not raw exchange payloads. Classify:

- missing exact position protection as critical;
- recoverable native-stop assignment with exact backup as warning;
- ready/completed TP convergence as healthy;
- submit-unknown or immutable ownership conflict as critical/manual review.

The monitor must alert on deadline/escalation transition, not every unchanged reconciliation tick.

**Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_strategy_records.py tests/test_web_strategy_records.py tests/test_production_safety_monitor.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py src/telegram_kol_research/web_app.py src/telegram_kol_research/production_safety_monitor.py tests/test_strategy_records.py tests/test_web_strategy_records.py tests/test_production_safety_monitor.py
git commit -m "feat: expose management liveness state"
```

### Task 9: Add a fingerprinted dry-run/apply recovery command

**Files:**
- Create: `src/telegram_kol_research/position_management_liveness_recovery.py`
- Create: `tests/test_position_management_liveness_recovery.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing dry-run tests**

Define a plan whose action kinds are restricted to:

```text
adopt_unique_native_stop
create_exact_backup_stop
converge_staged_take_profit
noop
```

Test that dry run:

- requires one explicit `posId`;
- loads one coherent exchange snapshot;
- prints the exact position, leg, proposed action, excluded candidates, target TP sizes, and fingerprint;
- never calls a mutation method;
- returns zero actions for stale, incomplete, conflicted, terminal, or already-converged state.

**Step 2: Run and verify failure**

```bash
.venv/bin/pytest tests/test_position_management_liveness_recovery.py tests/test_cli_smoke.py -q
```

Expected: command/module missing.

**Step 3: Implement the recovery plan**

Add CLI shape:

```bash
telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db \
  --pos-id <exact-pos-id>

telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db \
  --pos-id <exact-pos-id> \
  --apply \
  --expected-fingerprint <reviewed-fingerprint>
```

Apply must rebuild the snapshot and plan, compare the fingerprint, hold the authority lock, and execute at most one mutation component per invocation. Adoption-only apply may write the database after immutable revalidation. Exchange-write apply must reuse the normal exact backup or TP convergence executor; it must not contain a second submission implementation.

**Step 4: Add stale-plan, restart, and unknown-outcome tests**

Assert changed position size, changed pending order set, changed ledger, or changed intent state invalidates the fingerprint. Assert rerunning an already confirmed action is a no-op. Assert unknown outcome returns recovery-required and does not submit again.

**Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_position_management_liveness_recovery.py tests/test_cli_smoke.py -q
```

Expected: pass.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_management_liveness_recovery.py src/telegram_kol_research/cli.py tests/test_position_management_liveness_recovery.py tests/test_cli_smoke.py
git commit -m "feat: add fingerprinted management liveness recovery"
```

### Task 10: Run broad regression suites and review the implementation

**Files:**
- No source files unless a test exposes a defect.

**Step 1: Run focused protection and management suites**

```bash
.venv/bin/pytest \
  tests/test_trigger_protection_assignment.py \
  tests/test_trigger_protection_intents.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_backup_stop_executor.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_management_capabilities.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_position_management_liveness_recovery.py \
  -q
```

Expected: all pass.

**Step 2: Run listener, Web, monitor, and database regressions**

```bash
.venv/bin/pytest \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_trading_settings.py \
  tests/test_production_safety_monitor.py \
  tests/test_db_migrations.py \
  -q
```

Expected: all pass.

**Step 3: Run static and repository checks**

```bash
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: exit 0.

**Step 4: Review the complete diff**

Verify explicitly:

- no cancellation path accepts unowned orders;
- no position mutation accepts weak position identity;
- fallback submits no exchange write in disabled/shadow mode;
- all exchange writes have durable reservations and readback;
- submit-unknown cannot auto-retry;
- one leg cannot globally block unrelated positions;
- existing authoritative recognition/context targeting is unchanged.

Use the `requesting-code-review` skill before deployment and resolve every Critical or Important finding.

**Step 5: Commit any review-only corrections**

```bash
git add <reviewed-files-only>
git commit -m "fix: close management liveness review findings"
```

Skip this commit if no corrections are needed.

### Task 11: Document operations, audit SQL, and rollback

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`
- Modify: `README.md`

**Step 1: Add operator documentation**

Document:

- the difference between position ownership and protection-order ownership;
- the operation-specific capability table;
- disabled/shadow/live semantics;
- the dry-run/apply recovery command;
- reason/disposition meanings;
- how to inspect exact backup and TP convergence evidence;
- how to respond to submit-unknown;
- rollback without deleting confirmed orders or ledger history.

**Step 2: Add read-only audit SQL**

Include queries proving:

```sql
-- No verified position is missing every exact owned stop.
-- No active TP convergence exceeds current exact position size.
-- No order ID has multiple verified protection owners.
-- No recovery_required row is past its escalation deadline without notification.
-- No terminal position retains live convergence work.
```

**Step 3: Verify documentation references**

```bash
rg -n "position_management_liveness_v2_mode|recover-position-management-liveness|recovery_disposition" README.md docs/runbook.md docs/migration-handoff.md
git diff --check
```

Expected: all required terms are present and diff check passes.

**Step 4: Commit**

```bash
git add README.md docs/runbook.md docs/migration-handoff.md
git commit -m "docs: operate position management liveness recovery"
```

### Task 12: Push, deploy disabled, verify shadow, and recover one reviewed position

**Files:**
- No local source changes expected.

**Step 1: Confirm branch and push reviewed commits**

```bash
git status --short
git branch --show-current
git push origin codex/deepcoin-auto-trading-v1
```

Expected: only unrelated user-owned dirty files remain; push succeeds.

**Step 2: Prove a safe deployment window**

On the server, read only:

- recognition/context/management/position-mutation/recovery work in flight;
- current positions, pending entries, TPSL, and exact ownership;
- latest Telegram message continuity;
- service and production monitor state.

If any time-sensitive strategy operation is active or the snapshot is incomplete, do not restart. Record the exact remaining server verification.

**Step 3: Deploy with the new mode disabled**

Use the existing helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Verify server commit, editable package, `telegram-kol.service`, HTTP 200, listener continuity, and that `position_management_liveness_v2_mode` is disabled.

**Step 4: Run deployed focused tests and the production safety monitor**

Run the server equivalents of Tasks 10.1 and 10.2, then the no-notify production monitor. Expected: tests pass and no new active abnormal state appears.

**Step 5: Enable shadow only**

After another safe-window check, set the mode to `shadow` without authorizing any liveness-v2 exchange write. Capture the coherent shadow plan for the exact target position and verify:

- assignment is unique or correctly classified recoverable;
- proposed backup/TP actions affect only the exact reviewed `posId`;
- first-leg and other-position invariants are unchanged;
- no exchange mutation event was created.

**Step 6: Review a fingerprinted dry run for the current position**

```bash
telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db \
  --pos-id <current-exact-pos-id>
```

Do not reuse historical `posId` values from this plan. Use only the current fresh snapshot. If the position no longer exists, stop with a no-op and do not recreate anything.

**Step 7: Enable live and execute one bounded component only after approval**

Set live mode only after the reviewed fingerprint, safe window, and exact current position are all proven. Apply one component:

```bash
telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db \
  --pos-id <current-exact-pos-id> \
  --apply \
  --expected-fingerprint <reviewed-fingerprint>
```

Re-read positions, TPSL, ledger, mutation intent, events, and convergence state before planning the next component. Never batch multiple real writes under one stale fingerprint.

**Step 8: Complete post-deployment verification**

Prove:

- target position protection and TP state matches the saved strategy;
- no non-target position/order changed;
- no duplicate order IDs or owners exist;
- no active submit-unknown, recovery-required, or overdue component remains;
- listener and service remain healthy;
- the production safety monitor returns the reviewed baseline or better.

If verification fails, set the mode to disabled immediately. Do not cancel confirmed new protection as part of rollback; preserve exchange and ledger history for reconciliation.

---

## Completion criteria

The work is complete only when:

1. identical split-leg stops converge independent of API order;
2. true ambiguity never writes false ownership;
3. an exact owned backup stop can unblock safe staged TP without adopting an unknown native stop;
4. unowned-order cancellation/replacement remains impossible;
5. exact full close is not blocked solely by unrelated protection ownership gaps;
6. every recoverable block reaches a bounded retry, exact-backup path, or operator escalation;
7. disabled and shadow modes produce zero exchange writes;
8. local and deployed focused suites pass;
9. production verification uses current exact IDs and shows zero non-target mutation.
