# Ledger-backed Order-Position Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bind unscoped pending DeepCoin TPSL rows to their exact live position when a verified local order ledger proves the relationship.

**Architecture:** Extend the existing exact order-ID map built from verified `PositionProtectionLedger` rows with active `PositionTakeProfitOrder` rows. Pass that map to the display-row splitter; it resolves the map before classifying an order as unattributed. Existing mutation and strategy-attribution paths remain unchanged.

**Tech Stack:** Python 3.11, SQLAlchemy, FastAPI/Jinja2, pytest.

---

### Task 1: Prove ledger-order fallback ownership

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: Write the failing test**

Create one live position and an unscoped pending TPSL row whose order ID maps to that position in `exact_order_position_ids`. Assert the row is in that position's direct rows and absent from unattributed rows; an unknown row remains unattributed.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_app.py -k ledger_order_position -v`

Expected: FAIL because the splitter currently ignores the exact order-ID map.

**Step 3: Write minimal implementation**

Add optional `exact_order_position_ids` input to the splitter. Use a mapped live `posId` only when it is still in the supplied live positions. Build the mapping from verified, active-leg `PositionProtectionLedger` and active `PositionTakeProfitOrder` records in the live-position loader.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_app.py -k ledger_order_position -v`

Expected: PASS.

### Task 2: Verify card rendering uses the ledger fallback

**Files:**
- Modify: `tests/test_web_page_render.py`
- Verify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`

**Step 1: Write the failing test**

Seed a verified active leg and ledger record for an unscoped TP order. Render the positions panel and assert the TP appears on that exact card, not in `未归属交易所保护单`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k ledger_order_position -v`

Expected: FAIL because the rendered loader has not passed ledger ownership into the splitter.

**Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k ledger_order_position -v`

Expected: PASS after Task 1 implementation.

### Task 3: Regression, review, and deploy

**Files:**
- Verify only: `src/telegram_kol_research/web_app.py`

**Step 1: Run regression suite**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py tests/test_protection_snapshot.py tests/test_position_take_profit_orders.py -q`

Expected: PASS.

**Step 2: Commit and deploy**

Commit only implementation, tests, and this plan; exclude pre-existing `uv.lock` changes. Push `codex/deepcoin-auto-trading-v1`, execute the server update helper or equivalent SSH command, and verify `/opt/telegram-kol-analyzer` commit and `telegram-kol.service` state.
