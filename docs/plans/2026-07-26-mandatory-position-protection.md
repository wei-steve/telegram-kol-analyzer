# Mandatory Position Protection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the verified second stop and every planned take-profit target mandatory for all active automatic Deepcoin positions, using the existing persisted `ordId ↔ posId ↔ entry_leg` evidence as the authoritative TPSL association.

**Architecture:** Preserve the generic fail-closed matcher for orders without an identity, but treat a caller-supplied persisted/just-returned `ordId` as authoritative when all order fields agree and any exchange-supplied position ID does not conflict. Remove the backup-stop feature switch, reconcile missing protection in stop-first order, recover only provably missing TP targets, and use the existing logical leg, specialized execution, and unified protection ledgers for idempotency.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, Typer, Deepcoin REST client, systemd, Git/GitHub.

---

## Safety and execution rules

- Use `@test-driven-development` for every code task below.
- Use `@systematic-debugging` if a regression test fails for a reason other than the intended missing behavior.
- Use `@requesting-code-review` after the local suite passes and before pushing.
- Never submit, cancel, or replace a Deepcoin order from a local test.
- Run real exchange verification only on the server; its Telegram session, Deepcoin IP allowlist, and production credentials are authoritative.
- Preserve unrelated user changes (`uv.lock`, inspection directories, artifacts, and untracked planning files).
- Every exchange write must retain the existing account authority lock, write limiter, durable reservation-before-submit rule, and unknown-outcome freeze.
- Do not add a feature flag for second stops or staged take profits.
- Do not add a special case for strategy record `618` or position `1001124367311625`.

### Task 1: Restore authoritative exact-order TPSL matching

**Files:**

- Modify: `tests/test_deepcoin_order_matching.py`
- Modify: `src/telegram_kol_research/native_tpsl.py:124-175`

**Step 1: Write the failing exact-order regression test**

Replace the current expectation that an exact known zero-size order remains ambiguous in a multi-position snapshot. The test must prove that a persisted exact `ordId` verifies an unscoped Deepcoin row even when two same-contract, same-side split positions are open:

```python
def test_native_tpsl_exact_persisted_order_id_survives_missing_position_id():
    first = _native_tpsl_position(posId="pos-btc-1")
    second = _native_tpsl_position(posId="pos-btc-2")

    match = match_native_tpsl_order(
        position=first,
        open_positions=[first, second],
        orders=[{
            "ordId": "system-zero-stop-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPrice": "63000",
            "sz": "0",
        }],
        expected=NativeTpslExpectation(
            ord_id="system-zero-stop-1",
            purpose="stop_loss",
            trigger_price="63000",
            size="0",
        ),
    )

    assert match.status == "verified"
    assert match.order is not None
    assert match.order.ord_id == "system-zero-stop-1"
```

Also add:

- exact `ordId` plus exchange `PositionID` pointing to the other position returns `mismatch`;
- exact `ordId` with wrong trigger price, TPSL side, instrument, size, or purpose returns `mismatch`;
- no expected `ordId` with two same-side positions remains `ambiguous`.

**Step 2: Run the focused test and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_deepcoin_order_matching.py \
  -k "exact_persisted_order_id or exact_order_id_requires_open_position_scope" -q
```

Expected: the new persisted-order test fails with `ambiguous`.

**Step 3: Implement the minimal exact-ID rule**

In `match_native_tpsl_order()`, keep the unique-order-ID lookup, duplicate-ID rejection, and `_exact_order_matches()` validation. Remove the `_has_unique_open_position_scope()` requirement only from the `expected.ord_id` branch:

```python
if expected.ord_id:
    exact = [order for order in normalized if order.ord_id == expected.ord_id]
    if len(exact) > 1:
        return NativeTpslMatch(status="ambiguous", order=None)
    if not exact:
        return NativeTpslMatch(status="not_found", order=None)
    order = exact[0]
    return NativeTpslMatch(
        status="verified"
        if _exact_order_matches(position, order, expected)
        else "mismatch",
        order=order,
    )
```

Do not change the no-order-ID branch. `_exact_order_matches()` must continue rejecting a returned exchange position ID that conflicts with the target position.

Update the docstring to state that `expected.ord_id` must come from persisted local evidence or the response to the exact write being verified.

**Step 4: Run the native TPSL tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_deepcoin_order_matching.py -q
```

Expected: PASS; unknown unscoped zero-size orders remain ambiguous.

**Step 5: Commit**

```bash
git add \
  tests/test_deepcoin_order_matching.py \
  src/telegram_kol_research/native_tpsl.py
git commit -m "fix: trust persisted tpsl order identity"
```

### Task 2: Use exact ledger identity throughout backup-stop and TP verification

**Files:**

- Modify: `tests/test_backup_stop_repair.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py:367-430`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py:500-590`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py:410-575`

**Step 1: Write failing multi-position ledger tests**

Add a backup-stop executor test with:

- two active BTC long positions;
- target entry leg and `PositionProtectionLedger` mapping
  `primary-1 ↔ pos-1`;
- pending primary row `ordId=primary-1`, `sz=0`, no `posId`;
- another unrelated full-position stop for `pos-2`;
- exact live target position and a safe liquidation price.

Assert the plan submits exactly one backup request for `pos-1`; it must not report
`primary_stop_missing_on_exchange`.

Add convergence tests proving:

- persisted primary and backup order IDs verify when their pending rows omit `posId`;
- a pending row that directly returns a conflicting `PositionID` blocks;
- a newly submitted TP response order ID verifies when pending readback omits `posId`;
- an unknown unowned TP with no local order mapping still blocks/fails closed.

Add a repair-planner test proving unrelated unscoped zero-size TPSL rows at other prices do not produce `backup_similar_unscoped_order`; an unowned row at the exact planned backup price still does.

**Step 2: Run the focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_backup_stop_repair.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  -k "unscoped or persisted or multi_position or similar_unowned" -q
```

Expected: failures show the remaining `_native_tpsl_is_scoped_to_position()` and generic ambiguous-order gates.

**Step 3: Remove redundant heuristic ownership checks from exact-ID paths**

For `_pending_matches_primary()` and `_pending_matches_backup()`:

- require the persisted/saved order ID;
- require exact live target `posId`, instrument, side, and split mode;
- call `match_native_tpsl_order()` with that exact order ID;
- accept only `status == "verified"`;
- do not re-run unique timestamp/same-side-position inference afterward.

For `has_verified_exact_backup_stop()`:

- keep requiring an active `PositionBackupStopOrder`;
- keep validating its `request_json` contains the target `posId`, instrument, side, market stop semantics, and trigger price;
- verify the same persisted `row.order_id` in pending;
- treat a returned conflicting position ID as mismatch via the exact matcher.

For `_has_verified_native_primary_stop()`:

- continue loading only verified ledger rows for the exact binding, leg, and `posId`;
- verify each row by its exact `order_id`;
- remove `_native_tpsl_is_scoped_to_position()` after a successful exact-ID match.

For `_verified_native_take_profit()`:

- the order ID comes from the response to `set_position_sltp()` for an exact `posId`;
- verify by exact ID, target price, allocated size, instrument, side, and market TP semantics;
- remove the second heuristic scope check.

Keep `_native_pending_take_profit_ids()` conservative because it scans exchange rows before a local owner is known.

**Step 4: Correct similar-unowned backup detection**

In `_unowned_similar_backup()`, do not treat an `ambiguous` result caused by unrelated zero-size orders as a price match. Normalize pending rows first and block only rows that:

```python
order.pos_id is None
and order.size == Decimal("0")
and order.inst_id == payload["instId"]
and order.pos_side == payload["posSide"]
and order.stop_loss_trigger_price == Decimal(payload["slTriggerPx"])
```

If a row has a direct conflicting position ID, it is not a candidate for the target position.

**Step 5: Run focused and adjacent tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_deepcoin_order_matching.py \
  tests/test_backup_stop_repair.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_tpsl_display.py -q
```

Expected: PASS. The display remains ledger-driven and unknown orders remain unattributed.

**Step 6: Commit**

```bash
git add \
  tests/test_backup_stop_repair.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/backup_stop_repair.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py
git commit -m "fix: verify protection by exact ledger identity"
```

### Task 3: Make backup-stop submission mandatory

**Files:**

- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_strategy_management_worker.py`
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/execution_bindings.py:290-340`
- Modify: `src/telegram_kol_research/strategy_management_worker.py:148-170`
- Modify: `src/telegram_kol_research/web_app.py:4420-4440`
- Modify: `src/telegram_kol_research/web_app.py:5594-5630`

**Step 1: Replace the disabled-by-default test**

Delete `test_reconcile_keeps_second_stop_submission_disabled_by_default`.

Add:

```python
def test_reconcile_always_submits_missing_backup_stop_when_provider_is_available(tmp_path):
    ...
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=NOW,
        contract_spec_provider=_backup_stop_provider(),
    )
    assert len(client.sltp_payloads) == 1
```

Add a second call and assert the count remains one.

Update the existing explicit-enabled test to omit `backup_stop_submission_enabled=True`.

In the worker test, capture reconciler keyword arguments and assert the provider is present and the backup lane runs before the TP lane. In the web reconcile-loop test, assert each production tick passes the provider and invokes mandatory reconciliation.

**Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_strategy_management_worker.py \
  tests/test_web_app.py \
  -k "backup_stop or deepcoin_execution_reconcile_loop" -q
```

Expected: the mandatory default test fails because the current default flag is false.

**Step 3: Remove the feature switch**

Change the signature to:

```python
def reconcile_deepcoin_execution_bindings(
    session_factory,
    *,
    client,
    recovered_at=None,
    snapshot=None,
    contract_spec_provider=None,
):
```

Replace:

```python
if backup_stop_submission_enabled and contract_spec_provider is not None:
```

with:

```python
if contract_spec_provider is not None:
```

Do not add an environment setting, database setting, CLI toggle, or web toggle. Callers that only need read-only attribution may omit the provider; production callers already supply it.

Keep the order:

```text
apply position attribution
→ submit/verify missing second stops
→ refresh exchange snapshot if writes occurred
→ release eligible TP convergence
```

**Step 4: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_strategy_management_worker.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  tests/test_execution_bindings.py \
  tests/test_strategy_management_worker.py \
  tests/test_web_app.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/web_app.py
git commit -m "fix: require backup stops for automatic positions"
```

### Task 4: Materialize missing logical protection legs for active legacy entries

**Files:**

- Modify: `tests/test_position_protection_legs.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/position_protection_legs.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`

**Step 1: Write failing legacy-leg tests**

Seed an active verified entry leg with:

- exact `pos_id`;
- verified primary-stop ledger;
- a `TriggerTakeProfitConvergence` containing three targets;
- no `PositionProtectionLeg` rows.

Run reconciliation and assert the following logical rows exist before any new exchange write:

```python
[
    ("primary_stop", 1, "62500", None, "verified"),
    ("backup_stop", 1, "62375", "0", "planned"),
    ("take_profit", 1, "65100", "3", "planned"),
    ("take_profit", 2, "65800", "1", "planned"),
    ("take_profit", 3, "66400", "1", "planned"),
]
```

Use the real BTC contract step and current size `5`; expected TP quantities must come from `build_take_profit_plan()`, not hard-coded production logic.

Add conflict tests:

- an existing protection leg bound to another `posId` freezes;
- an existing exchange order ID cannot migrate to another leg;
- repeated reconciliation creates no duplicate logical legs.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_position_protection_legs.py \
  tests/test_execution_bindings.py \
  -k "legacy or materialize or protection_leg" -q
```

Expected: the active legacy entry has no logical protection rows.

**Step 3: Add one idempotent materialization helper**

In `position_protection_legs.py`, add a helper that receives the already-verified entry leg, exact position, primary ledger row, calculated backup price, and allocated TP plan. It must:

- create/get the primary logical leg;
- bind its exact `posId` and verified primary `ordId`;
- create/get the backup logical leg with size `0`;
- create/get one TP logical leg per desired target and allocated size;
- reject immutable-field conflicts;
- perform no exchange operation.

Use existing primitives:

```python
create_or_get_protection_leg(...)
bind_filled_position(...)
bind_verified_exchange_order(...)
```

Do not duplicate logical-leg construction across executors.

**Step 4: Invoke materialization after exact position and plan validation**

Call the helper only after:

- entry leg ownership is authoritative;
- the exact current position has been loaded;
- primary stop ledger identity is verified;
- contract spec and TP sizes are valid.

The backup and TP executors should then require the corresponding logical row and bind the returned exchange order ID after readback.

**Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_position_protection_legs.py \
  tests/test_execution_bindings.py \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py -q
```

Expected: PASS; new entries and legacy active entries use the same logical model.

**Step 6: Commit**

```bash
git add \
  tests/test_position_protection_legs.py \
  tests/test_execution_bindings.py \
  src/telegram_kol_research/position_protection_legs.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py
git commit -m "fix: materialize legacy position protection legs"
```

### Task 5: Reconcile and submit only missing take-profit targets

**Files:**

- Modify: `tests/test_trigger_take_profit_convergence_executor.py`
- Modify: `tests/test_position_take_profit_orders.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`

**Step 1: Write failing partial-convergence tests**

Cover:

1. Three desired targets, no TP orders: submit all three.
2. First target already active with exact local order ID and pending readback: submit only targets two and three.
3. All desired targets already verified: submit none and mark convergence complete.
4. A local active TP record missing from pending: freeze with
   `convergence_take_profit_missing_on_exchange`.
5. A pending TP without direct position ID or exact local ledger mapping: freeze with
   `convergence_unowned_take_profit_present`.
6. A returned exchange position ID conflicting with the local TP record: freeze.
7. A submit-unknown record for one target: do not retry that target.
8. Re-running after successful completion produces zero writes.

For a five-contract BTC position with `50/30/20`, assert the actual planner allocation and exact total, rather than implementing a second allocator inside the test.

**Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_take_profit_orders.py \
  -k "missing_target or partial or already_converged or unowned" -q
```

Expected: the current planner returns `convergence_take_profit_already_present` as soon as any TP exists.

**Step 3: Replace the any-TP blocker with exact desired-state reconciliation**

Build a desired map from the immutable convergence plan and current quantity allocation:

```python
desired_by_price = {
    price: {"price": price, "size": size}
    for price, size in allocated_targets
}
```

Load active `PositionTakeProfitOrder` rows for the exact binding, leg, and `posId`. For each row:

- require its exact `order_id`;
- verify the pending row by exact ID;
- require trigger price, size, market TP semantics, instrument, side, and non-conflicting position identity;
- mark the corresponding desired target satisfied.

Then:

- if an active row does not match a desired target, freeze;
- if a locally owned active row is missing from pending, freeze;
- if an unowned pending TP may affect this position, freeze;
- otherwise build payloads only for unsatisfied desired targets.

Do not cancel or replace a verified target merely to normalize ordering.

**Step 4: Make completion recoverable and idempotent**

Return an explicit `already_converged` plan when every target is satisfied. The executor should mark the convergence `submitted` without an exchange write.

Allow recoverable `waiting_backup_stop` and partial states to return to `ready` only after:

- exact position remains active;
- backup stop is verified;
- no unknown exchange outcome exists.

Keep `submit_unknown` terminal until readback or operator recovery resolves the uncertain order.

**Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trigger_take_profit_convergence.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_take_profit_orders.py \
  tests/test_strategy_management_worker.py -q
```

Expected: PASS; partial verified sets converge without duplicate orders.

**Step 6: Commit**

```bash
git add \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_take_profit_orders.py \
  src/telegram_kol_research/trigger_take_profit_convergence.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/position_take_profit_orders.py
git commit -m "fix: converge only missing take profit targets"
```

### Task 6: Add a read-only mandatory-protection audit

**Files:**

- Create: `src/telegram_kol_research/mandatory_protection_audit.py`
- Create: `tests/test_mandatory_protection_audit.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `docs/runbook.md`

**Step 1: Write failing audit tests**

Define a pure/read-only report with one row per active automatic entry leg:

```python
@dataclass(frozen=True, slots=True)
class MandatoryProtectionAuditRow:
    binding_id: int
    leg_id: int
    pos_id: str
    status: str
    missing_roles: tuple[str, ...]
    blocker: str | None
    planned_backup_stop: str | None
    desired_take_profits: tuple[tuple[str, str], ...]
```

Cover:

- complete position;
- missing backup and all TPs;
- backup verified but one TP missing;
- main stop ledger missing;
- main stop pending order missing;
- conflicting position ID;
- manual binding excluded;
- multiple positions audited independently;
- audit performs zero client write calls and zero database writes.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_mandatory_protection_audit.py -q
```

Expected: import/module failure.

**Step 3: Implement the read-only report**

Reuse the same exact-ID verification and TP desired-state helpers as the execution path. Do not implement a second attribution policy.

Report statuses:

```text
complete
needs_backup_stop
needs_take_profit
needs_backup_and_take_profit
blocked
unknown_exchange_outcome
```

Include a deterministic database fingerprint, exchange fingerprint, and combined fingerprint. Exclude secrets and full authentication payloads.

**Step 4: Add the CLI command**

Add:

```text
telegram-kol-research audit-mandatory-protection
  --database-path data/research.db
  --deepcoin-contract-specs-path config/deepcoin_contract_specs.yaml
  [--pos-id ...]
```

The command must be read-only and emit JSON. Do not add `--apply` or an enable/disable option.

**Step 5: Document server usage**

In `docs/runbook.md`, document:

```bash
.venv/bin/telegram-kol-research audit-mandatory-protection \
  --database-path data/research.db \
  --deepcoin-contract-specs-path config/deepcoin_contract_specs.yaml
```

Explain that `blocked` and `unknown_exchange_outcome` positions remain protected by fail-closed behavior and require review; safe missing roles are filled by normal reconciliation after service start.

**Step 6: Run tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_mandatory_protection_audit.py \
  tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/mandatory_protection_audit.py \
  tests/test_mandatory_protection_audit.py \
  src/telegram_kol_research/cli.py \
  tests/test_cli_smoke.py \
  docs/runbook.md
git commit -m "feat: audit mandatory position protection"
```

### Task 7: Add an end-to-end multi-position recovery regression

**Files:**

- Create: `tests/test_mandatory_position_protection_flow.py`
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Write the end-to-end test**

Seed at least three simultaneous same-side split positions:

- position A: verified primary only, missing backup and three TPs;
- position B: verified primary and backup, one of three TPs already verified;
- position C: conflicting/unowned stop evidence.

Run normal reconciliation and TP worker ticks with a fake Deepcoin client whose native TPSL readback omits `posId`.

Assert:

- A gets one backup then all three TPs;
- B gets only its two missing TPs;
- C receives no writes and records one actionable incident;
- every request carries the exact target `posId`;
- every returned `ordId` maps to exactly one `posId` and entry leg in
  `PositionProtectionLedger`;
- every logical protection leg becomes verified only after readback;
- another reconciliation/worker tick makes zero writes.

**Step 2: Run test and verify initial failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_mandatory_position_protection_flow.py -q
```

Expected: fail until Tasks 1-6 are correctly integrated.

**Step 3: Add production safety assertions**

Extend the safety monitor coverage so an active automatic position reports unhealthy when:

- its main stop is verified but backup is absent;
- any planned TP target is missing;
- a protection write has unknown outcome;
- local order identity conflicts with exchange position identity.

Do not flag manual bindings as automatically repairable.

**Step 4: Run the complete local suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

Run static/syntax checks:

```bash
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: both exit zero.

**Step 5: Commit**

```bash
git add \
  tests/test_mandatory_position_protection_flow.py \
  tests/test_production_safety_monitor.py
git commit -m "test: cover mandatory protection recovery flow"
```

### Task 8: Review, push, deploy, and repair all safe missing protection

**Files:**

- No source changes expected.
- Server database: `/opt/telegram-kol-analyzer/data/research.db`
- Server service: `telegram-kol.service`

**Step 1: Review local scope**

Run:

```bash
git status --short
git log --oneline --decorate -10
git diff HEAD~7..HEAD --stat
```

Verify only intended source, tests, runbook, design, and plan files are committed. Do not stage unrelated workspace files.

Use `@requesting-code-review` and address all correctness findings before continuing.

**Step 2: Re-run the complete local suite after review**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: all pass.

**Step 3: Push the required branch**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: the reviewed commits are available on GitHub.

**Step 4: Capture a server baseline before installing**

Run read-only commands:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'cd /opt/telegram-kol-analyzer &&
   git rev-parse --short HEAD &&
   systemctl is-active telegram-kol.service &&
   sqlite3 -json data/research.db "
     SELECT execution_order_leg_id,pos_id,order_id,purpose,trigger_price,status
     FROM position_protection_ledger
     WHERE status='\''verified'\''
     ORDER BY pos_id,purpose,trigger_price;
   "'
```

Capture a non-secret Deepcoin snapshot of live `posId`, instrument, side, size, and pending TPSL `ordId`, type, trigger price, and size. Do not print credentials or signatures.

Create a recoverable database backup with an explicit timestamped target under
`/opt/telegram-kol-analyzer/data/backups/`; validate the backup file exists and is nonzero.

**Step 5: Install the new code without allowing an unreviewed service tick**

Because protection is mandatory and has no feature switch, do not use an update path that restarts the service before the new audit is reviewed.

Run:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'systemctl stop telegram-kol.service &&
   cd /opt/telegram-kol-analyzer &&
   git fetch origin &&
   git checkout codex/deepcoin-auto-trading-v1 &&
   git pull --ff-only origin codex/deepcoin-auto-trading-v1 &&
   .venv/bin/pip install -e .'
```

Expected: service is stopped, install succeeds, and server HEAD equals the pushed commit.

**Step 6: Run and review the mandatory-protection audit**

Run:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'cd /opt/telegram-kol-analyzer &&
   .venv/bin/telegram-kol-research audit-mandatory-protection \
     --database-path data/research.db \
     --deepcoin-contract-specs-path config/deepcoin_contract_specs.yaml'
```

Expected:

- every active automatic position appears exactly once;
- safe missing positions are `needs_backup_stop`, `needs_take_profit`, or both;
- conflicts are explicitly `blocked`;
- no exchange or database write occurs;
- position `1001124367311625` identifies main stop `1001124367311731` as exact persisted evidence and lists backup plus `65100/65800/66400` as missing.

If the audit shows a changed position, missing primary order, ownership conflict, malformed plan, or unknown outcome, do not start the service until the affected row is understood.

**Step 7: Start normal mandatory reconciliation**

Run:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'systemctl start telegram-kol.service &&
   systemctl is-active telegram-kol.service'
```

Expected: `active`.

Normal reconciliation must process all safe missing protection in bounded batches. Do not run a separate manual bulk-submit command.

**Step 8: Monitor until the bounded backlog converges**

Read logs without exposing secrets:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'journalctl -u telegram-kol.service --since "10 minutes ago" --no-pager |
   grep -E "create_backup_stop|take_profit|protection|blocked|unknown|ERROR|Traceback"'
```

Re-run `audit-mandatory-protection` after each bounded worker interval. Continue until every safe row is `complete` and only explicitly blocked rows remain.

**Step 9: Verify the 大镖客 position**

Require all of the following:

- position `1001124367311625` remains live and size is unchanged except for genuine market fills;
- primary stop `1001124367311731` still exists;
- exactly one verified backup stop exists at the calculated safe price;
- TP targets `65100`, `65800`, and `66400` all exist;
- TP allocated sizes sum to the then-current position size and respect BTC contract step/minimum;
- every protection order ID maps to entry leg `375` and the exact `posId`;
- no duplicate or unowned order was created.

**Step 10: Verify every other missing position**

For each audit row that was safe to repair:

- compare before/after order IDs;
- verify one backup stop at most;
- verify every desired TP target exactly once;
- verify request/ledger `posId` agrees with the live position;
- verify manual bindings and blocked positions received no writes.

**Step 11: Restart and prove idempotency**

Run:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'systemctl restart telegram-kol.service &&
   sleep 5 &&
   systemctl is-active telegram-kol.service'
```

After one full reconciliation interval:

- re-run the mandatory audit;
- compare pending TPSL order IDs with the pre-restart completed snapshot;
- confirm zero new backup/TP submissions;
- confirm no `unknown_exchange_outcome`, duplicate-order, or attribution-conflict incident was introduced.

**Step 12: Record production evidence**

Append a non-secret verification note under `docs/` containing:

- deployed commit;
- audit fingerprints before and after;
- counts of completed, blocked, and unknown rows;
- 大镖客 primary/backup/TP order IDs and prices;
- restart-idempotency result;
- any blocked position reason codes.

Commit and push the verification note only after reviewing that it contains no credentials, signatures, account identifiers beyond the already tracked exchange order/position IDs, or private Telegram content.
