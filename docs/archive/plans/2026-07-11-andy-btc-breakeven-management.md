# Andy BTC Break-even Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recognize Andy's “平加仓 / 保护成本” update as a 50% reduction plus per-position breakeven-stop management operation across all exactly matched BTC short positions.

**Architecture:** Keep lifecycle recognition responsible for identifying the combined management intent. The auto-trade bridge resolves every exact active binding for the current chat/KOL/symbol/side and queues one composite action. The Deepcoin action layer performs close-then-protect sequentially for each binding, using each live position's `avgPx` as its own stop price and returning per-target outcomes.

**Tech Stack:** Python 3, SQLAlchemy, pytest, existing Deepcoin REST client protocol and execution-event ledger.

---

### Task 1: Recognize cost-protection and add-position reduction language

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py:729-780,1125-1190`
- Modify: `src/telegram_kol_research/ai_recognition_config.py:91-110`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_ai_recognition_config.py`

**Step 1: Write the failing recognition tests**

Add a lifecycle-event test whose active BTC short context receives Andy's exact text and asserts `position_update`, `partial_take_profit, move_stop_to_protect`, and a confidence eligible for automated processing. Add a configuration assertion that the default prompt explicitly contains `平加仓` and `保护成本`.

```python
assert candidate.event_type == "position_update"
assert candidate.management_action == "partial_take_profit, move_stop_to_protect"
assert "平加仓" in DEFAULT_LIFECYCLE_EVENT_PROMPT
assert "保护成本" in DEFAULT_LIFECYCLE_EVENT_PROMPT
```

**Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_message_recognition.py -k andy -q && python -m pytest tests/test_ai_recognition_config.py -q`

Expected: FAIL because the exact wording is not yet a management example or fallback term.

**Step 3: Implement the minimal recognition change**

Add the exact message as a `position_update` example in both lifecycle prompts. Include `回成本` and `保护成本` in `_has_protective_stop_terms`; add a narrow `_has_add_on_reduction_terms` helper for `平加仓`, and make the deterministic management fallback produce the combined action when both it and cost-protection language appear. Do not classify standalone profit commentary as an executable management action.

**Step 4: Run the focused tests and verify they pass**

Run: `python -m pytest tests/test_message_recognition.py -k andy -q && python -m pytest tests/test_ai_recognition_config.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/message_recognition.py src/telegram_kol_research/ai_recognition_config.py tests/test_message_recognition.py tests/test_ai_recognition_config.py
git commit -m "feat: recognize add-on breakeven management"
```

### Task 2: Resolve all exact management bindings and build the composite queue signal

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py:330-470,657-710,1038-1070`
- Test: `tests/test_auto_trade_execution.py`

**Step 1: Write the failing bridge test**

Create two active BTC-short bindings for `chat_id=100, kol_id=alice`, each with a distinct `pos_id`; add a third BTC short for another KOL. Feed the exact Andy message and a `position_update` candidate. Assert one queued/submitted composite action contains only the first two binding ids and a 0.5 fraction.

```python
assert result["management_action"] == "partial_close_and_move_stop_to_entry"
assert [item["binding_id"] for item in result["result"]["targets"]] == [binding_one, binding_two]
assert all(item["fraction"] == 0.5 for item in result["result"]["targets"])
```

**Step 2: Run the focused test and verify it fails**

Run: `python -m pytest tests/test_auto_trade_execution.py -k andy_two_btc_shorts -q`

Expected: FAIL because the current singular `_load_active_execution_binding` returns no binding when multiple matches exist and `平加仓` has no fraction extraction.

**Step 3: Implement the minimal bridge change**

Add `_load_active_execution_bindings()` that returns all `open/active` bindings matching exact chat id, KOL id, symbol, and side, sorted by id. Add `平加仓` as a 0.5 result in `_extract_partial_close_fraction`, but only create the composite action when the message also expresses cost protection. Queue `partial_close_and_move_stop_to_entry` with `targets=[{"binding_id": id, "fraction": 0.5}]`; retain current single-binding behavior for existing close and TP/SL actions. Return `no_execution_binding` when no exact targets exist.

**Step 4: Run the focused test and verify it passes**

Run: `python -m pytest tests/test_auto_trade_execution.py -k andy_two_btc_shorts -q`

Expected: PASS, with no target belonging to the unrelated KOL.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py tests/test_auto_trade_execution.py
git commit -m "feat: queue multi-position breakeven management"
```

### Task 3: Execute each target as partial close followed by its own breakeven stop

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:38-80,115-310,514-630`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write the failing action-layer tests**

Create two BTC short bindings and live split positions with different sizes and `avgPx` values. Give each a distinct old TP/SL pair and submit one `partial_close_and_move_stop_to_entry` signal. Assert each close request is 50% of its position, each replacement stop uses its own `avgPx`, and both original take-profit values are preserved. Add a second test where the first close request raises a client error: it must produce a failed target result and no cancellation/protection payload for that position, while the other target is processed independently. Add fail-closed cases for missing `posId` and missing/nonpositive `avgPx`.

```python
assert [order["sz"] for order in client.order_payloads] == ["0.5", "1"]
assert [payload["slTriggerPx"] for payload in client.protection_payloads] == ["64000.0", "64500.0"]
assert failure["status"] == "failed"
assert failure["stage"] == "partial_close"
```

**Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_deepcoin_execution_actions.py -k breakeven_management -q`

Expected: FAIL with unsupported action.

**Step 3: Implement the minimal composite executor**

Dispatch `partial_close_and_move_stop_to_entry` from `execute_deepcoin_management_signal`. Validate every `targets` element before interacting with the exchange: binding id exists, belongs to the queued signal's KOL/chat/symbol/side, has a bound `posId`, and resolves to exactly one live position with positive `avgPx`. For each validated target, use existing close and TP/SL primitives with a target-specific signal payload: call the 50% close first; only after it succeeds, cancel its exact old protection orders and call `set_position_sltp` with preserved TP and `stop_loss=avgPx`. Capture exceptions per target as structured results; do not issue a stop update for a close failure. Keep every operation in the existing execution-event ledger.

**Step 4: Run the focused tests and verify they pass**

Run: `python -m pytest tests/test_deepcoin_execution_actions.py -k breakeven_management -q`

Expected: PASS, including target-scoped failure behavior.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py tests/test_deepcoin_execution_actions.py
git commit -m "feat: execute partial close with breakeven stop"
```

### Task 4: Run regression coverage and document the operational guardrail

**Files:**
- Modify: `docs/deepcoin-order-management.md:75-116`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write the failing documentation assertion only if the project maintains doc-content tests**

No new test is required if documentation has no test harness. Otherwise, add the smallest existing-style assertion for the new composite action name.

**Step 2: Update the operational documentation**

Document the new composite action, its exact attribution predicate, close-before-protect order, `avgPx` rule, target-scoped failure behavior, and the explicit rule that development work must never use it to act on pre-existing live positions.

**Step 3: Run focused and full relevant regressions**

Run: `python -m pytest tests/test_message_recognition.py tests/test_auto_trade_execution.py tests/test_deepcoin_execution_actions.py -q`

Expected: PASS.

**Step 4: Inspect the final diff**

Run: `git diff HEAD~3..HEAD --check && git status --short`

Expected: no whitespace errors and only expected files changed.

**Step 5: Commit**

```bash
git add docs/deepcoin-order-management.md
git commit -m "docs: describe breakeven management action"
```
