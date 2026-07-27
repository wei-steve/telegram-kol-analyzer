# Account-Wide TPSL Ledger Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `position_protection_ledger` the sole TPSL ownership authority and remove runtime attribution based on symbol, side, size, price, or time.

**Architecture:** Introduce one account-wide ownership index keyed by `(venue, order_id)`, backfill every exact current mapping into it, and migrate readers and writers in controlled phases. Business-specific backup-stop and take-profit tables remain as workflow records, but no longer authorize display or exchange mutation. Production verification remains read-only and automatic trading stays frozen until separately authorized.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, Typer CLI, Deepcoin REST client, systemd

---

## Delivery rules

- Follow test-driven development for every behavior change: add one failing test, run it and confirm the expected failure, implement the minimum change, then rerun focused and broader tests.
- Do not change, submit, cancel, or replace a real Deepcoin order during implementation or verification.
- Do not write the production database until the reviewed backfill dry-run has zero refusals and its fingerprint is explicitly confirmed.
- Keep automatic trading frozen throughout deployment and production verification.
- Commit each task separately. Push reviewed commits only to `codex/deepcoin-auto-trading-v1`.
- Deploy only through the existing GitHub pull, editable reinstall, and `telegram-kol.service` restart workflow.
- Preserve unrelated local and server worktree files.

## Phase 0: Freeze a reproducible baseline

### Task 1: Add a read-only account-wide TPSL coverage report

**Files:**
- Create: `src/telegram_kol_research/tpsl_ownership_audit.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_tpsl_ownership_audit.py`
- Modify: `docs/runbook.md`

**Step 1: Write the failing coverage-report tests**

Add fixtures containing:

- two live BTC long positions;
- one live BTC short position;
- three pending `TPSL` rows with distinct `ordId` values and no exchange `posId`;
- three verified ledger rows mapping those IDs to exact positions;
- one stale ledger row whose order is no longer pending;
- one unowned pending order;
- one explicit exchange `posId` conflict.

Assert that the pure report returns stable, sorted counts and redacted identifiers:

```python
assert report.live_position_count == 3
assert report.pending_tpsl_count == 5
assert report.owned_pending_count == 3
assert report.unowned_pending_order_ids == ("manual-1",)
assert report.conflicts[0].reason == "exchange_position_conflicts_with_ledger"
assert report.stale_ledger_order_ids == ("old-1",)
assert report.exchange_write_count == 0
```

Also assert that no price, size, symbol, side, or timestamp changes ownership.

**Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/test_tpsl_ownership_audit.py -q
```

Expected: collection or import failure because `tpsl_ownership_audit` does not exist.

**Step 3: Implement the read-only report**

Implement immutable result types and a pure builder. Add a CLI command such as:

```bash
uv run telegram-kol-research audit-tpsl-ownership \
  --database-path data/research.db \
  --output-json
```

The command may call only `list_positions` and
`list_trigger_orders_pending`. Open SQLite with a read-only URI. It must not
call any Deepcoin write method or commit a session.

**Step 4: Run focused and CLI tests**

Run:

```bash
uv run pytest tests/test_tpsl_ownership_audit.py tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 5: Document the baseline procedure**

Add the exact server command, expected JSON fields, and the rule that a
nonzero write count invalidates the verification.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/tpsl_ownership_audit.py \
  src/telegram_kol_research/cli.py \
  tests/test_tpsl_ownership_audit.py docs/runbook.md
git commit -m "feat: audit account-wide TPSL ownership"
```

## Phase 1: Build the sole ownership authority

### Task 2: Add an account-wide ledger ownership index

**Files:**
- Modify: `src/telegram_kol_research/protection_ledger.py`
- Test: `tests/test_protection_ledger.py`

**Step 1: Write failing ownership-index tests**

Specify an API similar to:

```python
index = load_account_protection_ownership(
    session,
    venue="deepcoin",
    live_pos_ids={"pos-a", "pos-b"},
)
owner = index.owner_for_order("ord-a")
assert owner.pos_id == "pos-a"
assert index.orders_for_position("pos-a") == ("ord-a",)
```

Cover:

- lookup by exact `(venue, order_id)`;
- multiple orders for one position;
- terminal ledger rows retained in history but excluded from active ownership;
- a ledger owner no longer live classified as `stale`;
- deterministic conflict detection if corrupted input contains two owners;
- an unknown order classified `unowned`;
- no fallback using symbol, side, price, size, or time.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_protection_ledger.py -q
```

Expected: failure because the account ownership API is missing.

**Step 3: Implement the index**

Add frozen data classes:

```python
@dataclass(frozen=True, slots=True)
class ProtectionOwnership:
    venue: str
    order_id: str
    pos_id: str
    status: str
    purpose: str

@dataclass(frozen=True, slots=True)
class AccountProtectionOwnership:
    by_order_id: Mapping[str, ProtectionOwnership]
    by_pos_id: Mapping[str, tuple[ProtectionOwnership, ...]]
```

Expose explicit methods for `owner_for_order`, `orders_for_position`, conflict
reporting, and stale reporting. Never accept heuristic matching arguments.

**Step 4: Run tests**

```bash
uv run pytest tests/test_protection_ledger.py tests/test_db_bootstrap.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protection_ledger.py \
  tests/test_protection_ledger.py
git commit -m "feat: index account-wide TPSL ownership"
```

### Task 3: Rebuild the audit report on the new ownership index

**Files:**
- Modify: `src/telegram_kol_research/tpsl_ownership_audit.py`
- Test: `tests/test_tpsl_ownership_audit.py`

**Step 1: Add a failing parity test**

Assert that every pending TPSL is classified exactly once as owned, unowned,
or conflict, and that:

```python
owned + unowned + conflicts == pending_tpsl_count
```

Assert that a ledger-owned order for `pos-b` is never reported as manual or
unowned while auditing `pos-a`.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_tpsl_ownership_audit.py -q
```

Expected: the cross-position case fails under the current per-position logic.

**Step 3: Use only the account-wide index**

Remove any union of backup-stop and take-profit business tables from the audit
reader. Those tables may be reported as migration sources, but not as current
ownership authority.

**Step 4: Run tests and commit**

```bash
uv run pytest tests/test_tpsl_ownership_audit.py tests/test_protection_ledger.py -q
git add src/telegram_kol_research/tpsl_ownership_audit.py \
  tests/test_tpsl_ownership_audit.py
git commit -m "refactor: audit TPSL through canonical ledger"
```

## Phase 2: Backfill exact business records into the ledger

### Task 4: Add a fingerprinted, database-only canonical ledger backfill

**Files:**
- Create: `src/telegram_kol_research/tpsl_ledger_backfill.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_tpsl_ledger_backfill.py`
- Modify: `docs/runbook.md`

**Step 1: Write failing dry-run tests**

Build a plan from:

- current live positions;
- current pending TPSL;
- `position_backup_stop_orders`;
- `position_take_profit_orders`;
- current protection-ledger rows;
- verified execution legs.

An action is allowed only when exact `order_id` and `pos_id` exist, the
pending row still exists, any explicit exchange `posId` agrees, the execution
leg is verified, and no competing owner exists.

Assert:

```python
assert plan.actions[0].order_id == "backup-1"
assert plan.actions[0].pos_id == "pos-a"
assert plan.refusals == ()
assert len(plan.fingerprint) == 64
```

Add refusal tests for missing order, closed position, explicit exchange
conflict, unverified leg, duplicate owner, and incomplete snapshot.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_tpsl_ledger_backfill.py -q
```

Expected: import failure because the planner is missing.

**Step 3: Implement dry-run planning**

The plan must be deterministic and fully serializable. Candidate attributes
such as time, price, size, and direction may appear in evidence but must not
select the target `posId`.

**Step 4: Add failing apply tests**

Test:

- exact fingerprint required;
- a fresh plan is rebuilt before apply;
- any changed pending snapshot refuses apply;
- all actions commit atomically;
- an injected error rolls back every ledger row;
- apply never invokes a Deepcoin write method;
- rerunning the same plan is idempotent.

**Step 5: Implement apply**

Use `upsert_protection_ledger_row` inside one transaction. Record
`evidence_source` values that identify the exact source table and source row.
Do not change the business records.

**Step 6: Run focused tests**

```bash
uv run pytest tests/test_tpsl_ledger_backfill.py \
  tests/test_protection_ledger.py tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 7: Document dry-run and apply**

Document separate commands. The apply command must require the reviewed
fingerprint and an existing repair-confirmation token. State explicitly that
zero actions authorizes no writes.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/tpsl_ledger_backfill.py \
  src/telegram_kol_research/cli.py \
  tests/test_tpsl_ledger_backfill.py docs/runbook.md
git commit -m "feat: backfill canonical TPSL ledger"
```

## Phase 3: Make every new TPSL write canonical

### Task 5: Enforce atomic business-record and ledger writes

**Files:**
- Modify: `src/telegram_kol_research/protection_ledger.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Modify: `src/telegram_kol_research/native_tpsl_migration.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_position_take_profit_orders.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_trigger_backup_stop.py`
- Test: `tests/test_backup_stop_repair.py`
- Test: `tests/test_native_tpsl_migration.py`
- Test: `tests/test_position_mutation_gateway.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Add failing invariant tests per writer**

For each TPSL creation or replacement path, assert that a successful,
exchange-read-back order creates exactly one ledger row with the same:

```text
venue, order_id, pos_id, binding_id, leg_id, purpose
```

Inject a ledger write failure and assert that the associated business-table
state does not commit. Assert that response-only or pending-readback outcomes
do not create a verified ledger row.

**Step 2: Run the focused tests and verify RED**

Run each affected test module separately. At least one current path should
demonstrate the missing atomic invariant before production code is changed.

**Step 3: Centralize verified ownership recording**

Add a single helper that validates exact leg ownership and calls
`upsert_protection_ledger_row`. Use it in the same SQLAlchemy transaction as
the business record update.

Remove duplicate post-commit ledger writes. Preserve idempotency on
`(venue, order_id)`.

**Step 4: Run focused writer tests**

```bash
uv run pytest \
  tests/test_position_take_profit_orders.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_trigger_backup_stop.py \
  tests/test_backup_stop_repair.py \
  tests/test_native_tpsl_migration.py \
  tests/test_position_mutation_gateway.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_recovery_live_submit.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protection_ledger.py \
  src/telegram_kol_research/position_take_profit_orders.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/backup_stop_repair.py \
  src/telegram_kol_research/native_tpsl_migration.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/recovery_live_submit.py \
  tests/test_position_take_profit_orders.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_trigger_backup_stop.py tests/test_backup_stop_repair.py \
  tests/test_native_tpsl_migration.py \
  tests/test_position_mutation_gateway.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_recovery_live_submit.py
git commit -m "refactor: write every TPSL owner to canonical ledger"
```

## Phase 4: Shadow the canonical read model

### Task 6: Add an exact ledger projection for pending TPSL

**Files:**
- Modify: `src/telegram_kol_research/position_tpsl_display.py`
- Modify: `src/telegram_kol_research/protection_snapshot.py`
- Modify: `src/telegram_kol_research/protection_health.py`
- Test: `tests/test_position_tpsl_display.py`
- Test: `tests/test_protection_snapshot.py`
- Test: `tests/test_strategy_alerts.py`

**Step 1: Write failing multi-position regression tests**

Use two same-symbol, same-side live positions. Give each position its own
ledger-owned `sz=0` stop. Assert:

```python
assert result.by_pos_id["pos-a"][0].order_id == "sl-a"
assert result.by_pos_id["pos-b"][0].order_id == "sl-b"
assert audit_a.manual_order_ids == ()
assert audit_b.manual_order_ids == ()
```

Add one genuinely unowned manual order and assert it appears only once in the
global unowned collection, never on every position.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_position_tpsl_display.py \
  tests/test_protection_snapshot.py tests/test_strategy_alerts.py -q
```

Expected: the current `sz=0` cross-position test fails.

**Step 3: Implement canonical projection**

Require `AccountProtectionOwnership` as an input. Associate pending rows only
through explicit exchange `posId` or canonical ledger order ID. A mismatch
becomes a conflict. Remove per-position `known_order_ids` construction and
`_unowned_native_order_can_affect_position`.

Split health fields into:

```text
has_verified_stop
has_verified_backup_stop
verified_take_profit_count
has_unowned_orders
ownership_conflict
readback_complete
automation_safe
```

**Step 4: Add shadow comparison**

Add a pure comparator that records old/new differences without changing
display or trade decisions. Do not include secrets or full order payloads.

**Step 5: Run focused tests**

```bash
uv run pytest tests/test_position_tpsl_display.py \
  tests/test_protection_snapshot.py tests/test_strategy_alerts.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_tpsl_display.py \
  src/telegram_kol_research/protection_snapshot.py \
  src/telegram_kol_research/protection_health.py \
  tests/test_position_tpsl_display.py \
  tests/test_protection_snapshot.py tests/test_strategy_alerts.py
git commit -m "feat: project TPSL ownership from account ledger"
```

## Phase 5: Switch Web, audit, and alerts

### Task 7: Make all read surfaces consume the same projection

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/strategy_alerts.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Test: `tests/test_web_app.py`
- Test: `tests/test_strategy_records.py`
- Test: `tests/test_strategy_alerts.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write failing read-surface consistency tests**

For one shared snapshot, assert that Web, strategy records, alerts, and the
operator bot report the same owned order IDs and the same health fields.

Assert that:

- `has_verified_stop=True` does not render “无止损”;
- missing second stop is distinct from missing primary stop;
- an unavailable snapshot is not treated as proof of absence;
- an unowned order is shown once in the global section;
- no read surface unions business tables to establish ownership.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_web_app.py tests/test_strategy_records.py \
  tests/test_strategy_alerts.py tests/test_system_operator_bot.py -q
```

Expected: at least the all-protected-false regression or wording assertions fail.

**Step 3: Switch the readers**

Load the account ledger once per coherent request/snapshot and pass the same
index to every projection. Remove local reconstructions of
`exact_order_position_ids`.

**Step 4: Run focused tests and commit**

```bash
uv run pytest tests/test_web_app.py tests/test_strategy_records.py \
  tests/test_strategy_alerts.py tests/test_system_operator_bot.py -q
git add src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/strategy_alerts.py \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  tests/test_web_app.py tests/test_strategy_records.py \
  tests/test_strategy_alerts.py tests/test_system_operator_bot.py
git commit -m "fix: align TPSL displays with canonical ownership"
```

## Phase 6: Switch every exchange mutation path

### Task 8: Require ledger-owned order IDs in management planning

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/protection_attribution.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_protection_attribution.py`

**Step 1: Write failing authorization tests**

Assert that the planner/executor:

- uses only ledger order IDs owned by the target `posId`;
- refuses an otherwise unique symbol/side/time/size candidate without ledger;
- refuses explicit exchange `posId` conflicting with ledger;
- refuses a stale ledger owner;
- never borrows another position's order;
- produces no cancel or set payload after refusal.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_protection_attribution.py -q
```

Expected: current heuristic-success cases fail the new assertions.

**Step 3: Replace heuristic preflight**

Load the account ownership index with the coherent snapshot. Build old TPSL
rows by exact ledger order IDs only. Re-read those IDs before every cancel or
replacement. Preserve existing snapshot/fingerprint drift checks.

`match_position_protection` must no longer construct time/size assignment
edges for runtime callers.

**Step 4: Run tests and commit**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_protection_attribution.py -q
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/protection_attribution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_protection_attribution.py
git commit -m "fix: authorize TPSL management through ledger"
```

### Task 9: Remove stop-adjustment and direct-action fallbacks

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_matching.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/native_tpsl.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Test: `tests/test_deepcoin_order_matching.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_native_tpsl.py`
- Test: `tests/test_backup_stop_repair.py`

**Step 1: Replace heuristic-success tests with refusal tests**

Delete expectations for:

- unique symbol-and-side fallback;
- position creation-time matching;
- size matching without an order ID;
- `sz=0` ownership inference.

Add assertions that these cases return a stable fail-closed reason and make
zero Deepcoin write calls.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_deepcoin_order_matching.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_native_tpsl.py tests/test_backup_stop_repair.py -q
```

Expected: current code still accepts at least one forbidden fallback.

**Step 3: Remove runtime fallbacks**

`resolve_stop_loss_adjustment_target` must accept an exact target `posId` and
ledger-owned order IDs. `match_native_tpsl_order` may validate an exact
`ordId`, but must not establish ownership without it. Keep candidate helpers
only in an explicitly named supervised-review module, if still required.

**Step 4: Run tests and commit**

```bash
uv run pytest tests/test_deepcoin_order_matching.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_native_tpsl.py tests/test_backup_stop_repair.py -q
git add src/telegram_kol_research/deepcoin_order_matching.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/native_tpsl.py \
  src/telegram_kol_research/backup_stop_repair.py \
  tests/test_deepcoin_order_matching.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_native_tpsl.py tests/test_backup_stop_repair.py
git commit -m "refactor: remove heuristic TPSL ownership"
```

## Phase 7: Enforce the architecture and remove dead paths

### Task 10: Add regression guards against guessed ownership

**Files:**
- Create: `tests/test_tpsl_ownership_architecture.py`
- Modify: `docs/deepcoin-order-management.md`
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`

**Step 1: Write architecture tests**

Use behavior-level tests plus a narrow source inspection guard to ensure
runtime modules do not reintroduce calls that authorize TPSL ownership by:

```text
unique symbol/side
creation-time tolerance
position-size equality
sz=0 fallback
```

Allow such fields only in explicitly supervised dry-run modules.

**Step 2: Run and verify the guard**

```bash
uv run pytest tests/test_tpsl_ownership_architecture.py -q
```

Expected: PASS only after Tasks 8 and 9 remove the old paths.

**Step 3: Update documentation**

Document:

- `position_protection_ledger` as sole authority;
- business tables as workflow state only;
- exact read and mutation flow;
- stable refusal codes;
- supervised historical mapping boundaries;
- no automatic attribution from price, quantity, side, or time.

**Step 4: Run all TPSL-related tests**

```bash
uv run pytest \
  tests/test_protection_ledger.py \
  tests/test_tpsl_ownership_audit.py \
  tests/test_tpsl_ledger_backfill.py \
  tests/test_protection_attribution.py \
  tests/test_protection_snapshot.py \
  tests/test_position_tpsl_display.py \
  tests/test_deepcoin_order_matching.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_native_tpsl.py \
  tests/test_backup_stop_repair.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_web_app.py \
  tests/test_strategy_alerts.py \
  tests/test_system_operator_bot.py \
  tests/test_tpsl_ownership_architecture.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_tpsl_ownership_architecture.py \
  docs/deepcoin-order-management.md docs/migration-handoff.md docs/runbook.md
git commit -m "test: enforce canonical TPSL ownership"
```

## Phase 8: Full review, push, deploy, and verify

### Task 11: Run local regression and review

**Files:**
- Review all files changed since the design commit.

**Step 1: Run formatting and static checks**

Use the repository's configured commands. At minimum:

```bash
git diff --check
uv run ruff check src tests
```

Expected: PASS.

**Step 2: Run the full local suite**

```bash
uv run pytest -q
```

Expected: PASS. Record any environment-only skips.

**Step 3: Review the complete diff**

Check specifically for:

- a read path that still unions the business tables as ownership authority;
- a write path that can act without an exact ledger order ID;
- any price/size/time/symbol fallback;
- database commits split across business record and ledger write;
- stale owners being re-attributed;
- incomplete snapshot treated as absence;
- tests that assert mocks rather than final write boundaries.

**Step 4: Commit any review fixes**

Use focused commits, rerunning the affected tests after each change.

### Task 12: Push and deploy through the approved workflow

**Files:**
- No new source files expected.

**Step 1: Confirm branch and clean intended diff**

```bash
git branch --show-current
git status --short
git log --oneline --decorate -12
```

Expected branch: `codex/deepcoin-auto-trading-v1`. Unrelated user files may
remain untracked or modified and must not be committed.

**Step 2: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds.

**Step 3: Update production**

```bash
./scripts/server_git_update.sh
```

Expected: the server fast-forwards to the pushed commit, reinstalls the
editable package, restarts `telegram-kol.service`, and reports it active.

### Task 13: Run server-only read-only verification

**Files:**
- No source changes unless verification finds a defect.

**Step 1: Confirm service and commit**

Over SSH, verify:

```bash
systemctl is-active telegram-kol.service
git -C /opt/telegram-kol-analyzer rev-parse HEAD
```

Expected: active and equal to the pushed SHA.

**Step 2: Confirm automatic trading remains frozen**

Use the existing read-only settings/status command. Do not change the setting.

**Step 3: Run the ownership audit before backfill**

Expected:

```text
live positions: current exchange count
pending TPSL: current exchange count
unowned/conflict: only genuinely absent canonical rows
exchange writes: 0
```

**Step 4: Run the canonical backfill dry-run**

Review every action and refusal. Apply only if:

- the current snapshot is complete;
- every action is an already-reviewed exact `ordId → posId` pair;
- there are zero conflicts and refusals;
- the fresh plan fingerprint matches;
- a separate repair-confirmation token has been issued.

The apply is database-only. It must report zero exchange writes.

**Step 5: Rerun the ownership audit**

For the baseline observed during design, the expected shape is:

```text
8 live positions
all current pending TPSL owned by the canonical ledger
0 unowned
0 conflicts
0 exchange writes
```

Use current counts if the exchange has naturally changed; reconcile every
difference by exact IDs.

**Step 6: Verify Web, alerts, and management dry-runs**

Confirm:

- no cross-position `manual_order_detected`;
- positions with verified stops are not labeled “无止损”;
- missing backup stop is separate from missing primary stop;
- Web, audit, and operator output show the same order ownership;
- management dry-runs reference only ledger-owned order IDs;
- no live management action is submitted.

**Step 7: Inspect service logs and write counters**

Confirm no new exception, ledger conflict, database migration failure, or
Deepcoin write call occurred during verification.

**Step 8: Stop with trading still frozen**

Report the exact verification counts and any remaining stale or unowned IDs.
Do not re-enable automatic trading without a new explicit user authorization.
