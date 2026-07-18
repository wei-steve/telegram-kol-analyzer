# Entry Protection Ledger Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make initial market-entry TP/SL protection as tightly attributable as later TPSL management, and prevent the Web current-order view from presenting weak TPSL candidates as strategy ownership.

**Architecture:** The trade submission path records entry legs first, then writes verified `position_protection_ledger` rows for initial market-entry protection using exchange-returned order IDs that align one-to-one with protection rows, pending TPSL rows that carry exact `posId`/`closePosId`, or one exact-position combined TPSL row. The Web current-order projection reads that ledger before candidate matching and fails closed for TPSL rows without ledger evidence.

**Tech Stack:** Python, SQLAlchemy models, FastAPI/Jinja Web projection, pytest.

---

### Task 1: Cover Initial Protection Ledger Recording

**Files:**
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`

**Steps:**
1. Add failing tests for market entry with Deepcoin splitting one initial TP/SL request into separate exact-position pending TPSL orders, refusing price-only pending rows, and recording one exact-position combined TPSL row.
2. Verify the test fails with no `position_protection_ledger` rows.
3. After entry legs are persisted, record ledger rows for initial protection orders only when returned IDs align exactly, pending rows carry exact position identity, or a combined row carries both TP and SL for the exact position.
4. Verify the test passes and the rows include binding, entry leg, posId, purpose, trigger price, and `entry_protection_response`.

### Task 2: Tighten Web TPSL Attribution

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Steps:**
1. Add a failing test that a TPSL order with ledger evidence renders as `已验证保护`.
2. Add a failing test that a matching TPSL order without ledger evidence renders as `保护归属未验证` and not `可能归属`.
3. Query `position_protection_ledger` by current order ID before legacy binding/candidate matching.
4. Skip candidate attribution for TPSL rows without ledger evidence.

### Task 3: Verification

**Files:**
- Modify: `docs/migration-handoff.md`

**Steps:**
1. Document the durable rule: initial entry protection must write ledger evidence, and Web TPSL attribution must use ledger first.
2. Run focused tests:

```bash
.venv/bin/python -m pytest \
  tests/test_auto_trade_execution.py::test_auto_process_message_trade_signal_records_entry_protection_ledger \
  tests/test_web_page_render.py::test_exchange_current_tpsl_order_uses_protection_ledger_attribution \
  tests/test_web_page_render.py::test_exchange_current_tpsl_order_without_ledger_is_not_candidate_attributed -q
```

3. Run adjacent regression tests:

```bash
.venv/bin/python -m pytest tests/test_auto_trade_execution.py tests/test_web_page_render.py -q
```
