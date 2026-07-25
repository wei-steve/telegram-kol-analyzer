# Official-Style Position TPSL Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render every DeepCoin TPSL order on its exact position using exchange position IDs or durable local order-to-position evidence, while showing strategy attribution and keeping unverified orders separate.

**Architecture:** Normalize DeepCoin position and TPSL identities into one read-only display model. Resolve orders only by an exchange position ID or a verified persisted `ordId → posId` mapping, and make every TPSL write path record that mapping after exchange readback. Rebuild the position card around a variable-length TPSL list without weakening mutation safety.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, pytest, existing DeepCoin REST client and protection-ledger models.

---

### Task 1: Normalize the official DeepCoin position identity fields

**Files:**
- Modify: `src/telegram_kol_research/native_tpsl.py`
- Create: `tests/test_native_tpsl.py`

**Step 1: Write failing normalization tests**

Add cases proving that a native TPSL row accepts all verified exchange spellings:

```python
def test_normalize_native_tpsl_accepts_official_position_id():
    order = normalize_native_tpsl({
        "triggerOrderType": "TPSL",
        "OrderSysID": "order-1",
        "PositionID": "pos-1",
        "InstrumentID": "BTC-USDT-SWAP",
        "PosiDirection": "0",
        "Volume": "3",
        "SLTriggerPrice": "62000",
    })
    assert order is not None
    assert order.ord_id == "order-1"
    assert order.pos_id == "pos-1"
```

Also cover existing camelCase V1/V2 fields and confirm `Conditional` remains rejected.

**Step 2: Run the focused tests and verify failure**

Run:

```bash
pytest tests/test_native_tpsl.py -q
```

Expected: the PascalCase test fails because the current normalizer does not read `PositionID`, `OrderSysID`, or other official-web field names.

**Step 3: Implement the minimal field aliases**

Extend the current normalizer without changing its conservative matching policy:

```python
pos_id=_first_string(
    payload,
    "PositionID",
    "positionId",
    "posId",
    "pos_id",
    "closePosId",
)
```

Add corresponding aliases for order ID, instrument, side, size, timestamps and TP/SL trigger prices. Keep `triggerOrderType=TPSL` as the public API discriminator, while allowing the caller to explicitly identify official-web `BusinessType=X` rows before normalization.

**Step 4: Run tests**

Run:

```bash
pytest tests/test_native_tpsl.py tests/test_deepcoin_order_matching.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/native_tpsl.py tests/test_native_tpsl.py
git commit -m "fix: normalize DeepCoin position identities"
```

### Task 2: Build a pure official-style position/TPSL display join

**Files:**
- Create: `src/telegram_kol_research/position_tpsl_display.py`
- Create: `tests/test_position_tpsl_display.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: Write failing join tests**

Cover:

- direct `PositionID` match;
- local `ordId → posId` fallback;
- two same-symbol same-side split positions;
- `sz=0` order without identity remaining unattributed;
- conflicting local mappings failing closed;
- one combined TPSL row producing separate TP and SL display legs;
- stable ordering by association state, type, trigger price and order ID.

Example:

```python
result = build_position_tpsl_display(
    positions=[{"posId": "a", "instId": "BTC-USDT-SWAP"},
               {"posId": "b", "instId": "BTC-USDT-SWAP"}],
    pending_orders=[{"ordId": "sl-1", "triggerOrderType": "TPSL",
                     "instId": "BTC-USDT-SWAP", "sz": "0",
                     "slTriggerPx": "62000"}],
    exact_order_position_ids={},
)
assert result.by_pos_id["a"] == []
assert result.by_pos_id["b"] == []
assert [row.order_id for row in result.unattributed] == ["sl-1"]
```

**Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_position_tpsl_display.py -q
```

Expected: FAIL because the module does not exist.

**Step 3: Implement immutable display records**

Create dataclasses for the order leg and aggregate result. Move the display-only splitting rules out of `web_app.py`. Accept direct exchange position IDs first, then the verified local map. Never use price, size or time as an ownership key.

**Step 4: Replace the inline Web helpers**

Make `_load_deepcoin_live_position_rows()` call the new pure builder. Preserve the existing `match_position_protection()` call for mutation safety; do not reuse display association to authorize writes.

**Step 5: Run focused tests**

Run:

```bash
pytest tests/test_position_tpsl_display.py tests/test_web_app.py tests/test_web_page_render.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_tpsl_display.py src/telegram_kol_research/web_app.py tests/test_position_tpsl_display.py
git commit -m "refactor: join TPSL orders to exact positions"
```

### Task 3: Persist every verified position TPSL mapping

**Files:**
- Modify: `src/telegram_kol_research/protection_ledger.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Modify: `src/telegram_kol_research/native_tpsl_migration.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Test: `tests/test_protection_ledger.py`
- Test: `tests/test_position_take_profit_orders.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_backup_stop_repair.py`
- Test: `tests/test_native_tpsl_migration.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Add a failing ledger contract test**

Define one helper that records only a readback-verified native TPSL:

```python
record_verified_position_tpsl(
    session,
    binding=binding,
    leg=leg,
    pos_id="pos-1",
    request={"posId": "pos-1", "slTriggerPx": "62000", "sz": "3"},
    response={"data": [{"ordId": "sl-1"}]},
    pending_row={"ordId": "sl-1", "triggerOrderType": "TPSL",
                 "slTriggerPx": "62000", "sz": "3"},
    purpose="stop_loss",
)
```

Assert that the helper creates one verified `PositionProtectionLedger` row and rejects missing order IDs, mismatched `posId`, mismatched price/size, non-TPSL readback and a non-authoritative entry leg.

**Step 2: Run the ledger tests and verify failure**

Run:

```bash
pytest tests/test_protection_ledger.py -q
```

Expected: FAIL because the unified helper does not exist.

**Step 3: Implement the unified helper**

Keep `upsert_protection_ledger_row()` as the storage primitive. The new helper validates the request `posId`, response `ordId`, pending readback and exact leg ownership before calling it.

**Step 4: Route every `set_position_sltp` success path through the helper**

For each listed executor, add a test first that proves a successful verified submission produces the generic ledger mapping. Preserve specialized TP and backup-stop records. Unknown outcomes and pending-readback failures must not produce a verified ledger row.

**Step 5: Run all affected tests**

Run:

```bash
pytest \
  tests/test_protection_ledger.py \
  tests/test_position_take_profit_orders.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_backup_stop_repair.py \
  tests/test_native_tpsl_migration.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/protection_ledger.py \
  src/telegram_kol_research/position_take_profit_orders.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/backup_stop_repair.py \
  src/telegram_kol_research/native_tpsl_migration.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  tests/test_protection_ledger.py \
  tests/test_position_take_profit_orders.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_backup_stop_repair.py \
  tests/test_native_tpsl_migration.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py
git commit -m "fix: persist exact TPSL position ownership"
```

### Task 4: Add a fail-closed legacy evidence repair

**Files:**
- Create: `src/telegram_kol_research/tpsl_position_ledger_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_tpsl_position_ledger_repair.py`

**Step 1: Write failing dry-run planner tests**

Cover exact request JSON evidence, response/pending order ID agreement, current authoritative entry-leg ownership, conflicting order IDs, stale positions and evidence-free legacy orders.

The planner output must contain:

```python
{
    "actions": [...],
    "conflicts": [...],
    "fingerprint": "...",
}
```

**Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_tpsl_position_ledger_repair.py -q
```

Expected: FAIL because the planner does not exist.

**Step 3: Implement read-only planning**

Read existing request/response audit fields and current pending TPSL rows. Emit an action only when one exact `ordId`, one explicit request `posId` and one authoritative live entry leg agree.

**Step 4: Implement guarded apply**

Add a CLI command that defaults to dry-run. Apply requires the current fingerprint and only inserts/updates local ledger rows. It must never call a trading mutation endpoint.

**Step 5: Run tests**

Run:

```bash
pytest tests/test_tpsl_position_ledger_repair.py tests/test_web_cli.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/tpsl_position_ledger_repair.py src/telegram_kol_research/cli.py tests/test_tpsl_position_ledger_repair.py tests/test_web_cli.py
git commit -m "feat: repair exact TPSL position evidence"
```

### Task 5: Render the official-style position card with strategy attribution

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing render tests**

Assert that one card renders:

- a dedicated strategy-attribution row;
- `止盈止损(4)`;
- four independently rendered order rows;
- type, trigger price, size, creation time, order ID and association badge;
- no scalar “第二止损未创建” warning when the complete list already shows the real state;
- one global unattributed section outside all cards.

**Step 2: Run the page tests and verify failure**

Run:

```bash
pytest tests/test_web_page_render.py -k "positions_panel" -q
```

Expected: new official-style assertions fail.

**Step 3: Update the Jinja model and markup**

Render the strategy attribution immediately below the card header. Replace the current unordered protection list with a structured, accessible detail grid and use the actual list length for `止盈止损(n)`.

**Step 4: Add responsive styling and disclosure behavior**

Desktop should resemble the official card hierarchy; narrow screens stack fields without horizontal clipping. The disclosure control must use native button/details semantics and retain keyboard access.

**Step 5: Run render tests**

Run:

```bash
pytest tests/test_web_page_render.py tests/test_web_app.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/templates/_exchange_positions_panel.html src/telegram_kol_research/static/app.css src/telegram_kol_research/static/app.js tests/test_web_page_render.py
git commit -m "feat: render official-style position protection"
```

### Task 6: Add optional WebSocket field verification without making it authoritative

**Files:**
- Create: `scripts/deepcoin_private_ws_field_probe.py`
- Create: `tests/test_deepcoin_private_ws_field_probe.py`
- Modify: `docs/deepcoin-tpsl-live-verification-2026-07-25.md`

**Step 1: Write parser tests with sanitized fixtures**

Cover `Position`, `TriggerOrder`, heartbeat, reconnect and payloads containing both `PositionID` and `TradeUnitID`. The probe output must include field names and equality counts but no listen key, API headers or account identity.

**Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_deepcoin_private_ws_field_probe.py -q
```

Expected: FAIL because the probe parser does not exist.

**Step 3: Implement a read-only bounded probe**

Acquire a listen key through the existing authenticated client, subscribe only to `Position` and `TriggerOrder`, collect a bounded number of messages, sanitize them and exit. Do not add it to the service loop.

**Step 4: Run local parser tests**

Run:

```bash
pytest tests/test_deepcoin_private_ws_field_probe.py -q
```

Expected: PASS.

**Step 5: Run the real probe on the server**

Run it from `/opt/telegram-kol-analyzer` using the server environment. Record whether live `TriggerOrder` rows include `PositionID`; do not enable any production data path from this result alone.

**Step 6: Commit**

```bash
git add scripts/deepcoin_private_ws_field_probe.py tests/test_deepcoin_private_ws_field_probe.py docs/deepcoin-tpsl-live-verification-2026-07-25.md
git commit -m "test: verify DeepCoin websocket position fields"
```

### Task 7: Complete local and production verification

**Files:**
- Modify: `docs/deepcoin-tpsl-live-verification-2026-07-25.md`

**Step 1: Run the complete local suite**

Run:

```bash
pytest -q
```

Expected: PASS.

**Step 2: Review the change**

Use the `requesting-code-review` skill. Confirm that display evidence is never reused to authorize a cancel, replace or close operation.

**Step 3: Push the reviewed commits**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: the remote branch advances to the reviewed local HEAD.

**Step 4: Update the production server**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the branch, reinstalls the editable package and restarts `telegram-kol.service`.

**Step 5: Verify service health**

Check:

```bash
systemctl is-active telegram-kol.service
curl -fsS http://127.0.0.1:8000/health
```

Expected: service is active and health succeeds.

**Step 6: Compare production with the official page**

For each current position, record:

- `posId`;
- official `止盈止损(n)`;
- project `止盈止损(n)`;
- every trigger price and size;
- strategy attribution;
- any unattributed orders.

Acceptance requires identical per-position counts and rows for all orders with exact evidence. Evidence-free orders must remain only in the unattributed section.

**Step 7: Record verification and commit**

Append the sanitized results to the live-verification document, then commit and push the evidence-only change.
