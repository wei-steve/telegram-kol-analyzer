# Deepcoin Exact Stop Protection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve authoritative Deepcoin stop ownership when later exchange rows omit `posId`, create one exact-position market backup stop 20 bps beyond every eligible primary stop before take-profit submission, and provide a fingerprinted one-position-at-a-time repair path for existing live holdings.

**Architecture:** Treat the persisted exchange `ordId ↔ posId ↔ entry leg` association as durable ownership authority, while rejecting any explicit exchange position-ID conflict. Reconciliation restores or retains primary-stop health from exact order IDs, then creates and verifies an independent `closePosId` conditional market stop; take-profit convergence remains blocked until that backup is verified. Existing positions use a separate dry-run/apply planner with fresh exchange snapshots and a single-position fingerprint gate.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, Typer, pytest, existing Deepcoin REST client, systemd production service.

---

## Safety constraints

- Do not place, cancel, or modify a live order during local tests.
- Do not add a retry after `NotEnoughMoneyToClose`.
- Do not infer order ownership from symbol, side, quantity, price, or time.
- Do not make the repair CLI apply more than one `posId` per invocation.
- Treat an unknown exchange result as terminal operator recovery, not retryable failure.
- Keep existing user changes and untracked evidence files out of every commit.
- Production verification must run on the server after reviewed commits are pushed.

### Task 1: Preserve exact primary-stop ownership across unscoped Deepcoin rows

**Files:**
- Modify: `src/telegram_kol_research/protection_health.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing reconciliation tests**

Add focused tests covering:

```python
def test_reconcile_keeps_owned_stop_verified_when_pending_row_omits_position_id(tmp_path):
    # Persist a unique verified ledger row: order=tpsl-1, pos=pos-1.
    # Return a pending exchange row with ordId=tpsl-1 and no posId/closePosId.
    # Expect the ledger to remain verified.


def test_reconcile_restores_missing_owned_stop_when_same_order_is_pending(tmp_path):
    # Seed status=protection_missing with the same immutable order/position owner.
    # Return ordId=tpsl-1 without a position ID.
    # Expect status=verified and no duplicate ledger row.


def test_reconcile_refuses_owned_order_when_exchange_position_id_conflicts(tmp_path):
    # Return ordId=tpsl-1 with closePosId=other-pos.
    # Expect protection_missing plus one deduplicated conflict incident.


def test_reconcile_detects_failed_owned_stop_history_without_position_id(tmp_path):
    # Return history ordId=tpsl-1, no position ID, triggerTime>0,
    # errorCode=203, errorMsg=NotEnoughMoneyToClose.
    # Expect stop_trigger_failed and one incident.
```

**Step 2: Run the new tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_execution_bindings.py::test_reconcile_keeps_owned_stop_verified_when_pending_row_omits_position_id \
  tests/test_execution_bindings.py::test_reconcile_restores_missing_owned_stop_when_same_order_is_pending \
  tests/test_execution_bindings.py::test_reconcile_refuses_owned_order_when_exchange_position_id_conflicts \
  tests/test_execution_bindings.py::test_reconcile_detects_failed_owned_stop_history_without_position_id
```

Expected: failures showing the current matcher requires `posId` and cannot restore `protection_missing`.

**Step 3: Implement exact persisted-order matching**

Replace the current all-or-nothing `_matches` behavior with a helper shaped like:

```python
def _matches_owned_order(
    row: dict[str, Any],
    *,
    pos_id: str,
    order_id: str,
) -> bool:
    if _text(row, "ordId", "orderId", "order_id") != order_id:
        return False
    exchange_pos_id = _text(
        row,
        "closePosId",
        "posId",
        "pos_id",
        "positionId",
    )
    return not exchange_pos_id or exchange_pos_id == pos_id
```

Use this helper only for an existing unique `PositionProtectionLedger` row. Do not expose it as a general attribution matcher.

Expand the health scan to include `protection_missing` rows so a still-pending exact `ordId` can restore them:

```python
.filter(
    PositionProtectionLedger.status.in_(
        ("verified", "protected", "protection_missing")
    )
)
```

Evaluation order must be:

1. matching failed history;
2. matching pending row;
3. successful close history;
4. missing.

If the pending row has the same order ID and no explicit position ID, retain or restore `verified`. If it has a different explicit position ID, create a bounded conflict incident and do not authorize mutation.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest -q tests/test_execution_bindings.py -k "protection or stop_trigger_failed"
```

Expected: all focused protection tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protection_health.py tests/test_execution_bindings.py
git commit -m "fix: preserve exact Deepcoin stop ownership"
```

### Task 2: Change the backup-stop distance to 20 bps

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop.py`
- Modify: `tests/test_trigger_backup_stop.py`
- Modify: `tests/test_trading_settings.py`

**Step 1: Update tests to the confirmed 20 bps policy**

Change the default assertions:

```python
assert TradingSettings().trigger_backup_stop_buffer_bps == 20
```

Add explicit long and short examples:

```python
@pytest.mark.parametrize(
    ("side", "expected"),
    [("long", "63872"), ("short", "64128")],
)
def test_calculate_backup_stop_price_applies_20_bps_default(side, expected):
    assert calculate_backup_stop_price(
        primary_stop="64000",
        side=side,
        price_tick="0.1",
    ) == expected
```

Retain tests proving long prices round down and short prices round up when the raw result falls between ticks.

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q tests/test_trigger_backup_stop.py tests/test_trading_settings.py
```

Expected: failures showing the existing default is 50 bps.

**Step 3: Implement the new default**

Change both defaults:

```python
trigger_backup_stop_buffer_bps: float = 20.0
```

and:

```python
buffer_bps: str | float | Decimal = 20
```

Keep validation behavior unchanged: absent or non-positive configured values fall back to the new 20 bps default.

**Step 4: Run tests**

Run:

```bash
uv run pytest -q tests/test_trigger_backup_stop.py tests/test_trading_settings.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/trigger_backup_stop.py \
  tests/test_trigger_backup_stop.py \
  tests/test_trading_settings.py
git commit -m "fix: set backup stops twenty basis points beyond primary"
```

### Task 3: Plan backup stops for every eligible automatic split-position entry

**Files:**
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/protection_health.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_trigger_backup_stop.py`
- Test: `tests/test_protection_ledger.py`

**Step 1: Write failing eligibility and idempotency tests**

Add tests for:

```python
def test_reconcile_creates_backup_for_verified_market_entry(tmp_path):
    # A system-created market entry with active verified exact ownership,
    # verified primary stop, split mode, contract spec and liquidation price
    # receives one backup stop.


def test_reconcile_excludes_manual_bound_position_from_automatic_backup(tmp_path):
    # order_kind=manual_bind / source=manual_operator_bind must not write.


def test_reconcile_records_bounded_backup_blocker_once(tmp_path):
    # A verified entry with primary_stop_not_verified creates one deduplicated
    # PositionProtectionIncident, not one row per reconcile.


def test_reconcile_does_not_duplicate_existing_active_backup(tmp_path):
    # Two reconciliation runs produce one exchange submission and one row.


def test_reconcile_stops_after_unknown_backup_submission(tmp_path):
    # The first request raises/returns no order ID; no later candidate is submitted.
```

**Step 2: Run focused tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_execution_bindings.py -k "backup_stop" \
  tests/test_trigger_backup_stop.py \
  tests/test_protection_ledger.py
```

Expected: market entry and blocker tests fail under the existing `trigger_limit` filter and silent `None` paths.

**Step 3: Introduce an explicit backup-stop planning result**

Add:

```python
@dataclass(frozen=True, slots=True)
class BackupStopPlan:
    status: str
    reason_code: str | None = None
    binding_id: int | None = None
    leg_id: int | None = None
    pos_id: str | None = None
    payload: dict[str, str] | None = None
```

Refactor `_prepare_submission` into a read-only planner that returns one of:

- `ready`
- `blocked`
- `already_protected`
- `exchange_unavailable`

Use stable reason codes from the design document. Record blocked safety conditions through deduplicated `PositionProtectionIncident` rows; do not put raw exchange payloads in incident evidence.

**Step 4: Expand eligibility without admitting manual bindings**

Replace the hard-coded `order_kind == "trigger_limit"` condition with:

```python
ExecutionOrderLeg.order_kind != "manual_bind"
```

Then call `has_authoritative_persisted_position(leg, session=session)` and require:

- active verified entry leg;
- active binding;
- exact live split position;
- non-manual authoritative evidence;
- verified primary stop;
- valid liquidation price and contract spec.

This admits automatic `trigger_limit`, `limit`, and `market` entries but excludes manual operator binds.

**Step 5: Stop the batch after an unknown exchange outcome**

When submission raises or returns no order ID:

- persist `unknown_exchange_outcome`;
- record one `backup_exchange_outcome_unknown` incident;
- return immediately from `submit_verified_trigger_backup_stops`;
- do not continue to another position.

Keep duplicate prevention through `uq_position_backup_stop_orders_active_position`.

**Step 6: Run focused tests**

Run:

```bash
uv run pytest -q \
  tests/test_execution_bindings.py -k "backup_stop or protection" \
  tests/test_trigger_backup_stop.py \
  tests/test_protection_ledger.py
```

Expected: pass.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/protection_health.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_backup_stop.py \
  tests/test_protection_ledger.py
git commit -m "feat: create exact backup stops for automatic entries"
```

### Task 4: Require a verified backup stop before staged take profit

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_strategy_management_worker.py`

**Step 1: Write failing ordering tests**

Add:

```python
def test_take_profit_waits_until_exact_backup_stop_is_verified(tmp_path):
    # Primary is verified, backup absent: no set_position_sltp call.
    # The convergence remains retryable, not conflicted.


def test_take_profit_runs_after_backup_stop_is_active_and_pending(tmp_path):
    # Active persisted backup plus matching pending closePosId/order ID:
    # TP payloads may be submitted.


def test_take_profit_refuses_unscoped_similar_backup_order(tmp_path):
    # Same symbol/side/size/price but no exact durable owner is insufficient.


def test_management_worker_orders_reconcile_backup_before_take_profit(tmp_path):
    # One tick proves reconcile/backup verification precedes TP execution.
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_worker.py -k "take_profit or backup"
```

Expected: TP planning currently does not require a backup row.

**Step 3: Add the backup precondition**

In `_prepare_plan`, require one `PositionBackupStopOrder` for the exact leg and position with:

- `status == "active"`;
- non-empty exchange order ID;
- a fresh pending exchange row with the same order ID;
- a persisted exact submitted `closePosId == convergence.pos_id`;
- no explicit conflicting `closePosId`/`posId` in the pending row (an omitted
  position ID is acceptable only because the exact submitted ownership was
  durably persisted before read-back);
- matching market-order semantics and trigger price.

Return `convergence_waiting_backup_stop` when the backup does not yet exist. This is a transient state and must not be converted to terminal `conflicted`.

**Step 4: Make reconciliation wake waiting convergence rows**

After backup verification, move an exact matching convergence from `waiting_backup_stop` to `ready`. Do not wake:

- failed or missing primary stops;
- unknown backup submissions;
- explicit position-ID conflicts;
- terminal convergence rows.

**Step 5: Ensure worker ordering**

Before running the take-profit lane, execute a fresh binding reconciliation with the contract spec provider so the same authority-locked sequence is:

```text
primary health → backup plan/submit/read-back → convergence ready → TP submit
```

Do not reuse a snapshot captured before the backup submission as proof that the backup exists.

**Step 6: Run focused tests**

Run:

```bash
uv run pytest -q \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_worker.py
```

Expected: pass.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/trigger_take_profit_convergence.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_worker.py
git commit -m "fix: require backup protection before take profit"
```

### Task 5: Preserve backup protection after a primary-stop failure

**Files:**
- Modify: `src/telegram_kol_research/protection_health.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1: Write failing failure-state tests**

Add:

```python
def test_primary_stop_failure_keeps_verified_backup_active(tmp_path):
    # Primary history has error 203; backup remains pending and exact.
    # Primary becomes stop_trigger_failed; backup remains active.


def test_primary_stop_failure_does_not_submit_market_retry(tmp_path):
    # Assert zero order/close/set-sltp write calls after the failure is observed.


def test_management_freezes_exact_position_after_primary_stop_failure(tmp_path):
    # Expect protection_recovery_required.


def test_monitor_reports_primary_failure_with_backup_state(tmp_path):
    # Alert payload includes bounded pos reference, primary state and backup state.
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_execution_bindings.py -k "stop_failure" \
  tests/test_strategy_management_planner.py -k "protection" \
  tests/test_production_safety_monitor.py -k "protection"
```

Expected: at least the explicit backup-retention and alert assertions fail.

**Step 3: Implement independent primary and backup health**

Do not let a primary failure mutate an active backup row. Evaluate each owned order separately and expose:

```python
{
    "primary_stop_status": "stop_trigger_failed",
    "backup_stop_status": "active",
}
```

The management planner must continue returning `protection_recovery_required`; an active backup is not permission to adjust TP/SL after a primary failure.

The monitor must deduplicate the incident and must not call any trading endpoint.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest -q \
  tests/test_execution_bindings.py -k "stop_failure or backup" \
  tests/test_strategy_management_planner.py \
  tests/test_production_safety_monitor.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/protection_health.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/production_safety_monitor.py \
  tests/test_execution_bindings.py \
  tests/test_strategy_management_planner.py \
  tests/test_production_safety_monitor.py
git commit -m "fix: retain backup stop after primary failure"
```

### Task 6: Add a dry-run-first repair planner for existing positions

**Files:**
- Create: `src/telegram_kol_research/backup_stop_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_backup_stop_repair.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing planner tests**

Define tests for:

```python
def test_repair_plan_is_read_only_and_fingerprinted(tmp_path):
    # Build a plan and prove zero trigger_order calls and zero DB writes.


def test_repair_plan_contains_exact_position_evidence_and_twenty_bps_price(tmp_path):
    # Assert binding/leg/pos IDs, primary order, live size, liquidation price,
    # contract step, backup price and snapshot fingerprints.


def test_repair_plan_blocks_similar_unscoped_order(tmp_path):
    # A conditional order that could be a backup but lacks exact ownership
    # produces backup_similar_unscoped_order, not an action.


def test_repair_apply_requires_one_position_and_exact_fingerprint(tmp_path):
    # Missing --pos-id, multiple actions or changed fingerprint refuses.


def test_repair_apply_stops_on_unknown_result(tmp_path):
    # Persist unknown outcome and submit no later position.


def test_repair_apply_is_idempotent(tmp_path):
    # Re-running after a verified active backup performs no write.
```

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q tests/test_backup_stop_repair.py tests/test_cli.py -k "backup_stop"
```

Expected: import/command failures because the planner does not exist.

**Step 3: Implement immutable plan types**

Create:

```python
@dataclass(frozen=True, slots=True)
class BackupStopRepairAction:
    binding_id: int
    leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    size: str
    primary_order_id: str
    primary_stop: str
    backup_stop: str
    liquidation_price: str
    request_payload: dict[str, str]


@dataclass(frozen=True, slots=True)
class BackupStopRepairPlan:
    created_at: datetime
    actions: tuple[BackupStopRepairAction, ...]
    conflicts: tuple[dict[str, str], ...]
    database_fingerprint: str
    exchange_fingerprint: str
    fingerprint: str
```

The fingerprint must include the complete bounded action set, live-position economics, pending/history order IDs, contract spec and database ownership state.

**Step 4: Implement dry-run planning**

`build_backup_stop_repair_plan` must:

- load one coherent live snapshot;
- inspect exact active verified entry legs;
- exclude manual binds;
- require an authoritative primary stop;
- reject failed/unknown primary stops;
- reject existing active backups;
- reject similar unscoped conditional orders;
- calculate 20 bps backup prices;
- produce actions without committing or calling a write endpoint.

**Step 5: Implement single-position apply**

`apply_backup_stop_repair_plan` must:

1. require `pos_id` and expected fingerprint;
2. rebuild a fresh plan;
3. compare the complete fingerprint;
4. require exactly one matching action;
5. commit a `submitting` reservation;
6. submit one trigger order;
7. persist response or unknown outcome;
8. re-read the same order ID and verify it has no explicit position conflict,
   using the persisted exact `closePosId` request as the ownership authority;
9. return without touching any other position.

Do not accept a flag that applies every action.

**Step 6: Add the CLI**

Add:

```text
telegram-kol-research repair-backup-stops
telegram-kol-research repair-backup-stops --pos-id <id>
telegram-kol-research repair-backup-stops \
  --pos-id <id> \
  --apply \
  --expected-fingerprint <fingerprint>
```

Dry-run is the default. `--apply` without both `--pos-id` and fingerprint exits with code 2 before any write.

**Step 7: Run focused tests**

Run:

```bash
uv run pytest -q tests/test_backup_stop_repair.py tests/test_cli.py -k "backup_stop"
```

Expected: pass.

**Step 8: Commit**

```bash
git add \
  src/telegram_kol_research/backup_stop_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_backup_stop_repair.py \
  tests/test_cli.py
git commit -m "feat: add fingerprinted backup stop repair"
```

### Task 7: Expose distinct primary and backup protection states

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_strategy_records.py`
- Test: `tests/test_web_strategy_records.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing projection tests**

Add assertions that detail and position views show:

- primary stop price/status/order reference;
- backup stop price/status/order reference;
- explicit “主止损失败，第二止损有效”;
- explicit “第二止损未创建/证据未知”;
- no claim that a backup exists from a similar unscoped order;
- bounded blocker reason.

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_app.py -k "protection or backup"
```

Expected: missing projection/label assertions fail.

**Step 3: Implement separate projections**

Keep primary and backup arrays separate in the strategy record. Add bounded reader fields:

```python
{
    "primary_stop_state": ...,
    "backup_stop_state": ...,
    "backup_stop_blocker": ...,
}
```

Do not expose request/response JSON, API credentials, raw account calculations, or unbounded exchange messages.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest -q \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_app.py -k "protection or backup"
```

Expected: pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/templates/_strategy_detail.html \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/web_app.py \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_app.py
git commit -m "feat: show primary and backup stop health"
```

### Task 8: Run the local regression gate and update operations docs

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Document the dry-run/apply procedure**

Document:

- the 20 bps policy;
- exact ownership rules;
- dry-run command;
- one-position apply command;
- fingerprint invalidation conditions;
- explicit exclusion of known failed-primary positions from normal repair;
- no automatic retry after `NotEnoughMoneyToClose`;
- rollback steps that do not cancel live protection.

**Step 2: Run focused regression tests**

Run:

```bash
uv run pytest -q \
  tests/test_trigger_backup_stop.py \
  tests/test_protection_ledger.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_planner.py \
  tests/test_backup_stop_repair.py \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_app.py \
  tests/test_production_safety_monitor.py
```

Expected: pass.

**Step 3: Run the complete local suite**

Run:

```bash
uv run pytest -q
```

Expected: pass with no new warnings attributable to this change.

**Step 4: Run static checks**

Run:

```bash
uv run python -m compileall -q src tests
git diff --check
```

Expected: both commands succeed.

**Step 5: Request code review**

Use the `requesting-code-review` skill against the complete change set. Resolve all correctness or production-safety findings before deployment.

**Step 6: Commit docs**

```bash
git add docs/runbook.md docs/server-deployment.md docs/migration-handoff.md
git commit -m "docs: add exact backup stop operations"
```

### Task 9: Push and perform the server read-only gate

**Files:**
- No new source files.
- Update production evidence in `docs/migration-handoff.md` only after review.

**Step 1: Push reviewed commits**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: reviewed branch advances successfully.

**Step 2: Deploy through the documented helper**

Run:

```bash
./scripts/server_git_update.sh
```

Expected: server pulls the reviewed SHA, reinstalls the editable package and restarts `telegram-kol.service`.

**Step 3: Verify service and deployed SHA**

Run read-only server checks:

```bash
systemctl is-active telegram-kol.service
cd /opt/telegram-kol-analyzer
git rev-parse HEAD
.venv/bin/telegram-kol-research audit-management-batches \
  --database-path data/research.db \
  --limit 100 \
  --output-format json
```

Expected:

- service is active;
- SHA equals the pushed reviewed commit;
- snapshot is stable and complete;
- no malformed rows;
- no new unknown execution outcome.

**Step 4: Generate the production backup-stop dry run**

Run:

```bash
.venv/bin/telegram-kol-research repair-backup-stops \
  --database-path data/research.db
```

Expected:

- zero exchange writes;
- each live holding is either one safe action or one explicit blocker;
- `1001124330609705` is blocked as a failed-primary high-risk case;
- no action claims an unscoped similar order.

Stop if the snapshot is incomplete, any ownership conflict exists, or any action lacks exact `posId`.

**Step 5: Review one small-position candidate**

Choose one candidate only after operator review. Record:

- exact `posId`;
- current size;
- primary order ID and price;
- proposed 20 bps backup price;
- liquidation boundary;
- plan fingerprint.

Do not use a live Telegram message or create a test position.

### Task 10: Apply and verify one production backup stop

**Files:**
- Update: `docs/migration-handoff.md`

**Step 1: Apply exactly one reviewed action**

Run:

```bash
.venv/bin/telegram-kol-research repair-backup-stops \
  --database-path data/research.db \
  --pos-id <reviewed-pos-id> \
  --apply \
  --expected-fingerprint <reviewed-fingerprint>
```

Expected: one submitted and verified backup stop, or an explicit stop with no further writes.

**Step 2: Re-run dry-run and read-only audit**

Run:

```bash
.venv/bin/telegram-kol-research repair-backup-stops \
  --database-path data/research.db \
  --pos-id <reviewed-pos-id>

.venv/bin/telegram-kol-research audit-management-batches \
  --database-path data/research.db \
  --limit 100 \
  --output-format json
```

Expected:

- the position reports an active verified backup;
- no duplicate action remains;
- audit remains stable and complete.

**Step 3: Verify Web and exchange evidence**

Confirm:

- exact primary and backup order IDs are distinct;
- the persisted backup request has the exact `closePosId`, and the exchange
  read-back has the same order ID with no explicit position conflict;
- backup uses market execution;
- trigger distance is 20 bps after tick rounding;
- primary and backup appear separately in strategy detail and the position panel;
- no take-profit order was submitted before backup verification.

**Step 4: Record evidence and commit**

Update `docs/migration-handoff.md` with:

- deployed SHA;
- service state;
- dry-run fingerprint;
- reviewed `posId`;
- redacted primary/backup evidence;
- audit result;
- any deferred blockers.

Then run:

```bash
git add docs/migration-handoff.md
git commit -m "docs: record backup stop production probe"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 5: Stop for operator approval**

Do not apply the remaining actions automatically. Present the verified one-position result and request explicit approval for each subsequent production position.
