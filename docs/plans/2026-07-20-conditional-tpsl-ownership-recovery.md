# Conditional TPSL Ownership Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist a deterministic, restart-safe ownership chain from every Deepcoin trigger-entry request to its generated TPSL `ordId`, so later management adjusts exact protection automatically without guessing.

**Architecture:** Add a pre-submission `TriggerProtectionIntent` with the trigger-entry fingerprint and pending-TPSL baseline. After existing reconciliation has proved the entry leg's `posId`, an intent-aware planner adopts only one unique post-baseline TPSL candidate. Management continues to mutate exact ledger IDs. Ambiguity never triggers cancellation or duplicate take profit; a separately guarded, exact-position stop rescue protects downside only.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLite compatibility migrations, existing Deepcoin read client and reconciliation loop, pytest.

---

### Task 1: Durable trigger-protection intent state

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/trigger_protection_intents.py`
- Create: `tests/test_trigger_protection_intents.py`

**Step 1: Write failing tests**

Test one intent per `(venue, execution_order_leg_id)` with immutable request fingerprint/baseline, parent trigger ID, recovery state, retry metadata, and no more than one adopted TPSL ID. Test idempotent updates and old-SQLite `init_db` compatibility.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_trigger_protection_intents.py -q`.

Expected: FAIL because the model and helpers do not exist.

**Step 3: Implement minimal persistence**

Add `TriggerProtectionIntent`, unique indexes for venue/entry leg and nonempty venue/adopted order ID, and SQLite table/index compatibility. Persist only normalized request fingerprint and pre-submit baseline JSON. Implement `create_or_get`, `record_parent`, and idempotent state-transition helpers; reject any changed immutable evidence.

**Step 4: Confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py src/telegram_kol_research/trigger_protection_intents.py tests/test_trigger_protection_intents.py
git commit -m "feat: persist trigger protection intents"
```

### Task 2: Capture TPSL baseline before trigger-entry submission

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `src/telegram_kol_research/trigger_protection_intents.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_deepcoin_client.py`

**Step 1: Write failing tests**

Use a fake client with one pre-existing TPSL row and a returned parent `ordId`. Assert trigger-limit submission reads pending trigger orders before `trigger_order`, persists normalized baseline/request/parent ID, and creates nothing for market entries or trigger entries without both TP and SL. Assert an unavailable pre-submit snapshot defers submission rather than submitting without correlation evidence.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_auto_trade_execution.py -k trigger_protection_intent -q`.

Expected: FAIL because no baseline is saved.

**Step 3: Implement baseline-first submit**

Normalize TPSL rows to `ordId`, instrument, side, type, size, TP/SL price, and exchange timestamps. Within a narrow per-account/instrument/side lock: capture baseline, create intent, submit parent trigger order, and persist returned parent ID. Do not use regular pending orders, infer `posId`, or write/cancel TPSL.

**Step 4: Confirm GREEN**

Run `.venv/bin/python -m pytest tests/test_auto_trade_execution.py tests/test_deepcoin_client.py -q`.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/recovery_live_submit.py src/telegram_kol_research/deepcoin_client.py src/telegram_kol_research/trigger_protection_intents.py tests/test_auto_trade_execution.py tests/test_deepcoin_client.py
git commit -m "feat: capture trigger protection baseline"
```

### Task 3: Pure post-baseline TPSL adoption planner

**Files:**
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/trigger_protection_intents.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `tests/test_trigger_protection_intents.py`

**Step 1: Write failing tests**

Cover: one exact post-baseline candidate adopts; baseline candidate refuses; two candidates refuse; conflicting returned `posId` refuses; history-only candidate requires same fingerprint and calibrated time range; a ledger/intent-owned order ID refuses. Assert the planner performs no I/O or database writes.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py tests/test_trigger_protection_intents.py -k post_baseline -q`.

Expected: FAIL because baseline evidence is unused.

**Step 3: Implement minimal planner**

Return one immutable outcome: adopt, defer, or refuse. Require verified trigger leg/`posId`, parent ID, both expected TP/SL prices, full-size semantics, post-baseline ID, and uniqueness across pending/history rows. Timestamp evidence must be an explicit calibrated range. Reuse existing protection-ledger collision protections.

**Step 4: Confirm GREEN**

Run `.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py tests/test_trigger_protection_intents.py tests/test_execution_bindings.py -q`.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_protection_ledger_repair.py src/telegram_kol_research/trigger_protection_intents.py tests/test_entry_protection_ledger_repair.py tests/test_trigger_protection_intents.py
git commit -m "feat: match post-baseline trigger protection"
```

### Task 4: Intent-aware read-only reconciliation

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/deepcoin_readonly.py`
- Modify: `src/telegram_kol_research/trigger_protection_intents.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write failing tests**

Start with a saved baseline/parent, let normal reconciliation verify `posId`, then expose one new matching TPSL. Assert one ledger row and `tpsl_adopted`. Add no-TPSL deferred retry, duplicate delivery/restart idempotency, conflict audit bound to exact `posId`, history-only recovery, and unavailable-snapshot retry tests.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_execution_bindings.py -k trigger_protection_recovery -q`.

Expected: FAIL because reconciliation has no intent recovery state.

**Step 3: Implement without exchange writes**

Extend the bounded snapshot with pending/history TPSL data. After exact position attribution, invoke the Task 3 planner, transactionally write ledger and intent state on adoption, or one fingerprinted audit plus bounded retry on defer/refusal. Add result counters for adopted/deferred/conflicting/unavailable.

**Step 4: Verify safety**

Run:

```bash
.venv/bin/python -m pytest tests/test_execution_bindings.py tests/test_entry_protection_ledger_repair.py tests/test_strategy_management_planner.py -q
rg -n "place_order|trigger_order|cancel_position_sltp|set_position_sltp|modify_position_sltp" src/telegram_kol_research/execution_bindings.py
```

Expected: tests pass and search finds no reconciliation exchange write.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/deepcoin_readonly.py src/telegram_kol_research/trigger_protection_intents.py tests/test_execution_bindings.py
git commit -m "feat: recover trigger protection ownership"
```

### Task 5: Exact-position stop-only rescue

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/trigger_protection_intents.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `tests/test_strategy_management_executor.py`

**Step 1: Write failing tests**

For an exact verified `posId` with deferred/ambiguous intent and no managed stop, assert a dedicated rescue request contains exact split `posId` and SL fields only—never TP fields. Test response-ID persistence, crash/retry idempotency, existing managed-stop no-op, opaque TP update stays blocked, and no opaque TPSL cancellation.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py -k "rescue or opaque" -q`.

Expected: FAIL because no rescue action exists.

**Step 3: Implement guarded rescue**

Add a separate action requiring exact verified position, live revalidation, durable rescue reservation, deferred/ambiguous intent, and no managed stop. Call `set-position-sltp` with `posId` plus SL fields only, then persist the response `ordId`, event, and ledger state. Never use rescue for unverified legacy positions or as a hidden normal-adjustment fallback.

**Step 4: Confirm GREEN**

Run `.venv/bin/python -m pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_auto_trade_execution.py -q`.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_executor.py src/telegram_kol_research/trigger_protection_intents.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py
git commit -m "feat: rescue untracked position stop"
```

### Task 6: Visibility, runbook, and staged production rollout

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `tests/test_strategy_records.py`
- Modify: `tests/test_web_queries_messages.py`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing tests**

Require timeline/read model output for parent order ID, exact position ID, recovery state/attempts, adopted TPSL IDs, bounded refusal code, and stop-rescue state—without Telegram text or secret payloads.

**Step 2: Confirm RED**

Run `.venv/bin/python -m pytest tests/test_strategy_records.py tests/test_web_queries_messages.py tests/test_web_page_render.py -k protection -q`.

Expected: FAIL because intent data is not exposed.

**Step 3: Implement read-only visibility and docs**

Expose identifiers, state, attempts, timestamps, and bounded refusal codes only. Document server read-only audit, strict adoption enablement, separate guarded live probe for stop rescue, and the prohibition on auto-cancelling legacy opaque TP rows.

**Step 4: Verify locally and on server**

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_records.py tests/test_web_queries_messages.py tests/test_web_page_render.py -q
.venv/bin/python -m pytest -q
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

Expected: local tests pass; server reports the pushed SHA and active service. Start production with read-only recovery audit, then strict adoption; enable stop rescue only after a separate tiny live-account probe proves response-ID persistence and restart idempotency.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html docs/runbook.md docs/server-deployment.md tests/test_strategy_records.py tests/test_web_queries_messages.py tests/test_web_page_render.py
git commit -m "docs: operate trigger protection recovery"
```
