# Multi-Target Partial Take-Profit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute an explicit multi-symbol partial-take-profit message as independent exact-strategy batches, closing every filled leg proportionally and cancelling each unfilled entry leg first.

**Architecture:** Extend lifecycle recognition from one management target to a validated list of targets, then persist one existing `SignalCandidate` per strategy. Reuse the existing durable message-instruction orchestration to execute each candidate independently. Expand the close executor's exact deferred-entry cancellation boundary from full exits to partial closes, so each strategy's pending range legs are cancelled before its per-position 50% close orders are submitted and reconciled.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite migrations already provided by `db.py`, pytest via `uv run pytest`, Deepcoin REST client protocol.

---

### Task 1: Specify and normalize multi-target lifecycle decisions

**Files:**
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Modify: `src/telegram_kol_research/message_recognition.py:997-1135,1502-1558`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_ai_recognition_config.py`

**Step 1: Write failing protocol and recognition tests**

Add a decision fixture whose `lifecycle_event` has `event_type="position_update"`, `management_action="partial_take_profit"`, `management_fraction=0.5`, and a `targets` list for entered BTC-short and ETH-short lifecycles. Assert both candidates persist from the same raw message:

```python
assert {(row.target_lifecycle_id, row.symbol, row.management_fraction) for row in rows} == {
    (btc_lifecycle.id, "BTC", 0.5),
    (eth_lifecycle.id, "ETH", 0.5),
}
```

Add negative cases for duplicate IDs, a target from another chat, a target whose symbol/side does not match the lifecycle, and a target below the confidence threshold. Each must create no candidate for that unsafe target.

**Step 2: Run the focused tests to verify RED**

Run: `uv run pytest -q tests/test_message_recognition.py -k multi_target tests/test_ai_recognition_config.py`

Expected: FAIL because the JSON contract and `_apply_lifecycle_event_decision` only accept one `target_lifecycle_id`.

**Step 3: Implement target-list validation and candidate fan-out**

Add an optional `targets` array to the lifecycle-event prompt/schema. Keep the legacy single target shape valid by normalizing it to a one-item list. Add an internal validated target value containing lifecycle ID, symbol, side, action, fraction, requested stop, and requested take-profit.

In `message_recognition.py`, resolve each target through `session.get(StrategyLifecycle, id)`, require the same chat, entered status, matching symbol/side, and a unique target ID. For each valid target, call the existing `_upsert_management_signal_candidate`; refactor its candidate lookup to filter by `target_lifecycle_id` so two management candidates from the same raw message never overwrite one another. Keep ambiguous text without explicit validated targets fail-closed.

**Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest -q tests/test_message_recognition.py -k multi_target tests/test_ai_recognition_config.py`

Expected: PASS.

**Step 5: Commit the protocol boundary**

```bash
git add src/telegram_kol_research/ai_recognition_config.py src/telegram_kol_research/message_recognition.py tests/test_message_recognition.py tests/test_ai_recognition_config.py
git commit -m "feat: recognize multi-target management instructions"
```

### Task 2: Project every management target into independent durable work

**Files:**
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:30-140`
- Test: `tests/test_message_instruction_items.py`
- Test: `tests/test_auto_trade_execution.py`

**Step 1: Write failing multi-management orchestration tests**

Persist one raw message with BTC and ETH management candidates and create instruction items. Assert two `management` items are produced with different candidate IDs and idempotency keys. Execute with a fake planner/executor where BTC returns `recovery_required` and ETH returns `submitted`:

```python
assert [(item["strategy_instance_id"], item["status"]) for item in result["items"]] == [
    (btc_strategy_id, "unknown"),
    (eth_strategy_id, "submitted"),
]
```

Run the processor twice and prove neither target gets a second submission.

**Step 2: Run the focused tests to verify RED**

Run: `uv run pytest -q tests/test_message_instruction_items.py tests/test_auto_trade_execution.py -k 'multi_management or independent_management'`

Expected: FAIL if item projection assumes one candidate or deduplicates management items only by raw message.

**Step 3: Preserve candidate-specific identity and independent outcomes**

Ensure item creation scans all actionable management candidates for the raw message and constructs one idempotency key from raw message, candidate ID, target lifecycle ID, intent, and recognition generation. Ensure `execute_message_instruction_items` continues after a definite failure or unknown result, and passes the claimed `candidate_id` into management planning.

Do not alter the existing one-candidate behavior. Do not retry an unknown exchange submission.

**Step 4: Run focused tests to verify GREEN and commit**

```bash
uv run pytest -q tests/test_message_instruction_items.py tests/test_auto_trade_execution.py -k 'multi_management or independent_management'
git add src/telegram_kol_research/message_instruction_items.py src/telegram_kol_research/auto_trade_execution.py tests/test_message_instruction_items.py tests/test_auto_trade_execution.py
git commit -m "feat: orchestrate independent management targets"
```

### Task 3: Cancel deferred range-entry legs before partial close

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py:273-360,1363-1490`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write failing executor tests for the reported shapes**

Add a BTC partial-close batch with one verified live position and one deferred trigger entry in `identity.deferred_entry_leg_ids`. Assert the exact trigger order is cancelled before the first close submission and the close size is 50%:

```python
assert client.cancel_trigger_calls == [{"instId": "BTC-USDT-SWAP", "ordId": "entry-2"}]
assert client.calls[0][0]["closePosId"] == "btc-pos-1"
assert client.calls[0][0]["sz"] == "0.5"
```

Add an ETH partial-close batch with two verified position legs and no deferred leg. Assert two 50% closes are submitted and no cancellation is called. Add missing and duplicate deferred-order match cases; both must return `recovery_required` before any close submission.

**Step 2: Run the executor tests to verify RED**

Run: `uv run pytest -q tests/test_strategy_management_executor.py -k 'partial_close and deferred_entry'`

Expected: FAIL because deferred-entry cancellation is currently limited to `full_close`/`full_exit`.

**Step 3: Extend the existing exact cancellation boundary**

For every `_CLOSE_ACTIONS` batch, call `_require_exact_entry_legs` and `_cancel_deferred_entry_legs` before `_require_fresh_close_write_boundary`. Reuse the immutable `deferred_entry_leg_ids` snapshot and its exact exchange order/client-order matching. Keep the existing fail-closed transition and audit events, but make reason text generic to close-style management rather than full exit.

Never cancel an order based only on symbol, side, or price. Do not cancel deferred legs belonging to another binding. A cancellation preflight failure must prevent every close order in that one batch, while other message items remain eligible.

**Step 4: Reconcile successful and failed states**

Add tests proving a partial-close batch becomes confirmed only after all planned close legs are exchange-confirmed and its deferred entry leg is recorded cancelled. Verify a cancellation failure does not change lifecycle management metadata or partial-round accounting.

**Step 5: Run GREEN verification and commit**

```bash
uv run pytest -q tests/test_strategy_management_executor.py -k 'partial_close and deferred_entry'
uv run pytest -q tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py
git add src/telegram_kol_research/strategy_management_executor.py tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py
git commit -m "fix: cancel deferred entries before partial take profit"
```

### Task 4: Render per-target outcomes and verify the complete flow

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_strategy_records.py` (or the existing strategy-record detail test module)

**Step 1: Write failing visibility tests**

Assert that operator output from the one source message names each target strategy, its independent batch state, the number of partial-close legs, and whether deferred entry cancellation completed. Assert a blocked BTC target does not make ETH's confirmed batch appear failed. Assert strategy records show the same raw-message ID against each target's batch.

**Step 2: Run the focused tests to verify RED**

Run: `uv run pytest -q tests/test_system_operator_bot.py tests/test_strategy_records.py -k 'multi_target or deferred_entry_cancel'`

Expected: FAIL until the renderer exposes per-target batch/cancellation outcomes.

**Step 3: Add bounded, evidence-based rendering**

Build summaries solely from persisted instruction items, management batches, and management legs. Include no live query or inferred ownership in display code. Reuse existing escaping/chunking and retain generic rendering for all other batches.

**Step 4: Run complete local verification**

```bash
uv run pytest -q tests/test_message_recognition.py tests/test_ai_recognition_config.py tests/test_message_instruction_items.py tests/test_auto_trade_execution.py tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py tests/test_system_operator_bot.py tests/test_strategy_records.py
python3 -m compileall -q src
git diff --check
```

Expected: exit code 0 and no whitespace errors.

**Step 5: Commit and prepare production rollout**

```bash
git add src/telegram_kol_research/system_operator_bot.py src/telegram_kol_research/strategy_records.py tests/test_system_operator_bot.py tests/test_strategy_records.py
git commit -m "feat: report multi-target management outcomes"
git push origin codex/deepcoin-auto-trading-v1
```

On the server, use the approved project helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Verify that `telegram-kol.service` is active, inspect startup logs for migration/worker exceptions, and validate a future safe test message or shadow-mode batch. Do not replay the historical live message #3366.
