# Unattributed Protection Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep position-card TPSL details exact while presenting unscoped exchange protection orders once in a separate summary panel.

**Architecture:** Split pending TPSL display rows into direct `posId` matches and unscoped rows. The live-position loader attaches only direct rows to a position. The positions-panel context receives a separately grouped unscoped collection, rendered once outside the card list. No strategy attribution or mutation preflight behavior changes.

**Tech Stack:** Python 3.11, FastAPI/Jinja2, pytest.

---

### Task 1: Split direct and unscoped exchange TPSL display rows

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py:860-1150,2213-2315`

**Step 1: Write the failing test**

Create two BTC-long positions, one direct `posId` stop, and one unscoped stop. Assert the first card has only the direct stop, the second card has none, and the unscoped stop appears in an independent collection exactly once.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_app.py -k unattributed_protection -v`

Expected: FAIL because unscoped rows are currently emitted for every matching instrument card.

**Step 3: Write minimal implementation**

Replace the per-card candidate expansion with a helper returning `(direct_rows_by_pos_id, unattributed_rows)`. Direct rows require an exact live `posId`; all other valid TPSL sides are retained once as `无法归属`, grouped by instrument and explicit position side when supplied. Keep the verified summary path unchanged.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_app.py -k unattributed_protection -v`

Expected: PASS.

### Task 2: Render the unscoped exchange-order summary once

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/web_app.py:1162-1310,3373-3415`

**Step 1: Write the failing test**

Render two BTC-long cards with one direct and one unscoped stop. Assert the direct order appears in exactly one position card and the unscoped order appears exactly once under `未归属交易所保护单`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k unattributed_protection -v`

Expected: FAIL because no separate summary panel exists.

**Step 3: Write minimal implementation**

Render a compact `未归属交易所保护单` panel in the positions tab, grouped by instrument and side. Each entry shows TP/SL kind, price, quantity, and order ID. Do not render actions or claim verified strategy ownership.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k unattributed_protection -v`

Expected: PASS.

### Task 3: Regression and deployment

**Files:**
- Verify only: `src/telegram_kol_research/web_app.py`
- Verify only: `src/telegram_kol_research/templates/_exchange_positions_panel.html`

**Step 1: Run focused regression tests**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py tests/test_protection_snapshot.py tests/test_position_take_profit_orders.py -q`

Expected: PASS.

**Step 2: Commit and deploy**

Commit only the implementation, tests, and this plan. Push `codex/deepcoin-auto-trading-v1`; run the existing server helper (or its SSH-equivalent using the configured key if PowerShell is unavailable), then verify `/opt/telegram-kol-analyzer` is at the pushed commit and `telegram-kol.service` is active.
