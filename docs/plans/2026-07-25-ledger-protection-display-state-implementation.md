# Ledger Protection Display State Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render locally verified DeepCoin TPSL ownership as `已验证归属` on the matched live-position card.

**Architecture:** The display splitter already has an exact verified `orderId -> posId` map. When it uses that fallback, it will make a display-only copy of the pending order containing the resolved position ID before calling the existing renderer. Existing native-`posId`, unknown-order, and conflict fail-closed paths remain unchanged.

**Tech Stack:** Python, SQLAlchemy, FastAPI/Jinja2, pytest.

---

### Task 1: Prove ledger fallback renders as verified

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: Write the failing assertion**

Extend `test_ledger_order_position_fallback_renders_tpsl_on_exact_position` so the card section containing `ledger-tp-1` and `recorded-tp-1` contains `已验证归属`, while the unattributed summary continues to contain only `unknown-tp-1` and `无法归属`.

**Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k ledger_order_position -v`

Expected: FAIL because the fallback-routed raw order lacks `posId` when `_exchange_protection_display_rows` computes `ownership_state`.

**Step 3: Implement the minimum display-only evidence propagation**

In `_split_exchange_protection_display_rows`, retain whether `order_pos_id` came from `exact_order_position_ids`. For that path only, append a copied order with the resolved `posId`; do not mutate the source exchange payload. Pass every other row through unchanged.

**Step 4: Re-run the focused test**

Run: `uv run pytest tests/test_web_page_render.py -k ledger_order_position -v`

Expected: PASS.

### Task 2: Regress and deploy

**Files:**
- Verify: `src/telegram_kol_research/web_app.py`
- Verify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`

**Step 1: Run relevant regression coverage**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py tests/test_protection_snapshot.py tests/test_position_take_profit_orders.py -q`

Expected: PASS.

**Step 2: Inspect change safety**

Run: `git diff --check`

Expected: no output.

**Step 3: Commit and deploy**

Commit only the implementation, tests, and this plan; exclude unrelated `uv.lock` and user artifacts. Push `codex/deepcoin-auto-trading-v1`, run `/usr/local/bin/telegram-kol-update` on the production server, then verify the deployed SHA, `telegram-kol.service`, and `/positions-panel` HTTP status.
