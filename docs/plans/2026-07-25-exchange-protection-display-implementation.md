# Exchange Protection Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Display every relevant pending DeepCoin TP/SL order on each live position card while preserving the existing conservative verified-protection summary.

**Architecture:** Add a pure display-row normalizer in `web_app.py` that expands each pending TPSL row into separate TP and SL display entries. Directly position-bound rows are attached to the matching card; remaining same-instrument, same-side rows are shown as unverified candidates. The existing `match_position_protection` result remains unchanged and continues to drive only the verified summary fields.

**Tech Stack:** Python 3.11, FastAPI/Jinja2, SQLAlchemy, pytest.

---

### Task 1: Define the complete exchange-protection display contract

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py:810-1140`

**Step 1: Write the failing test**

Add a loader-level test with one active position and three pending rows: a direct TP at `65000`, a direct combined TP/SL at `66000`/`62000`, and an unscoped same-instrument/same-side SL at `61000`. Assert `exchange_protection_orders` has all four prices; direct entries are `已验证归属` and the unscoped entry is `无法归属`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_app.py -k exchange_protection_orders -v`

Expected: FAIL because the live position row does not contain `exchange_protection_orders`.

**Step 3: Write minimal implementation**

Add a pure helper near `_load_deepcoin_pending_tpsl_orders` that keeps TPSL rows with a non-zero trigger, emits one dictionary per active TP/SL side (`kind`, `trigger_price_text`, `size_text`, `order_id`, `ownership_state`), attaches exact `posId` rows as `已验证归属`, attaches unscoped same-instrument/same-side rows as `无法归属`, and sorts deterministically. Set the list on each position row. Do not alter `match_position_protection`, summary fields, or mutation preflight.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_app.py -k exchange_protection_orders -v`

Expected: PASS.

**Step 5: Commit**

Run: `git add tests/test_web_app.py src/telegram_kol_research/web_app.py && git commit -m "feat: expose complete exchange protection orders"`

### Task 2: Render individual exchange TP/SL orders on the position card

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:58-70`

**Step 1: Write the failing test**

Render a position containing `exchange_protection_orders` with verified TP `65000`, verified stops `62000` and `61000`, and unscoped TP `67000`. Assert that every price, `交易所保护单`, `已验证归属`, and `无法归属` appears.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k exchange_protection_orders -v`

Expected: FAIL because the template has no detailed protection-order section.

**Step 3: Write minimal implementation**

Below the metric grid, render a compact `交易所保护单` list only when `item.exchange_protection_orders` is non-empty. Each row renders its Chinese kind label, price, quantity when known, order ID, and ownership-state badge. Add no actions or mutation controls.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k exchange_protection_orders -v`

Expected: PASS.

**Step 5: Run focused regression tests**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py tests/test_protection_snapshot.py -q`

Expected: PASS.

**Step 6: Commit**

Run: `git add tests/test_web_page_render.py src/telegram_kol_research/templates/_exchange_positions_panel.html && git commit -m "feat: render exchange protection order details"`

### Task 3: Verify the working tree and deployment handoff

**Files:**
- Verify only: `src/telegram_kol_research/web_app.py`
- Verify only: `src/telegram_kol_research/templates/_exchange_positions_panel.html`

**Step 1: Run the full local relevant suite**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py tests/test_protection_snapshot.py tests/test_position_take_profit_orders.py -q`

Expected: PASS.

**Step 2: Inspect the final diff**

Run: `git diff HEAD~2..HEAD --check && git status --short`

Expected: no whitespace errors; preserve unrelated pre-existing worktree changes.

**Step 3: Push and deploy after review**

Run: `git push origin codex/deepcoin-auto-trading-v1`, then `powershell -ExecutionPolicy Bypass -File .\\scripts\\server_git_update.ps1` only after user authorization.
