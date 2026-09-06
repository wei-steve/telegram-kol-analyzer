# DeepCoin History Positions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restyle only the exchange-position history tab as a DeepCoin App-inspired history list and guarantee deterministic newest-first close-time ordering.

**Architecture:** The backend continues to build one annotated exchange snapshot. `list_exited_strategies()` becomes the canonical stable ordering boundary before both ungrouped and grouped history views consume the rows. A dedicated Jinja macro renders a semantic, DeepCoin-shaped history row while scoped CSS activates the light treatment only when the history tab is active.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, Jinja2, vanilla JavaScript, CSS, pytest.

---

### Task 1: Make historical-position ordering explicit and stable

**Files:**

- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_queries.py:2120-2328`

**Step 1: Write the failing test**

Add a focused render test with three exited lifecycles: two distinct `exited_at` values and one `exited_at=None` row with an `entered_at` value. Render `/positions-panel`, then assert the inspectable history row IDs are ordered as newest close, earlier close, then missing-close fallback. Add two rows with equal `exited_at` and assert descending stable record ID order.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k history_position_time_order -v`

Expected: FAIL because the card lacks an inspectable ID and sorting has no closed-time-first stable-ID contract.

**Step 3: Write minimal implementation**

In `list_exited_strategies()`, add `history_sort_id` from each source record (`StrategyLifecycle.id`, `ExecutionBinding.id`, or `TradeIdea.id`). Retain source collection, but sort with this equivalent ordering:

```python
results.sort(
    key=lambda row: (
        row.get("exited_at") is not None,
        row.get("exited_at") or row.get("entered_at"),
        row.get("history_sort_id", 0),
    ),
    reverse=True,
)
```

Missing close times must never sort ahead of real close times. Do not change the limit, attribution, or DeepCoin API calls.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k history_position_time_order -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_page_render.py src/telegram_kol_research/web_queries.py
git commit -m "fix: stabilize historical position ordering"
```

### Task 2: Render a DeepCoin-shaped historical position row

**Files:**

- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:113-139`

**Step 1: Write the failing test**

Render an exited BTC short lifecycle with entry price, exit price, size, group attribution and timestamps. Assert `data-deepcoin-history-position`, `data-history-position-id`, `开仓均价`, `平仓均价`, `开仓时间`, `最后平仓时间`, `data-deepcoin-history-attribution`, and `data-exchange-history-panel` are present. Assert no markup changes are required in the other three panels.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k deepcoin_history_position_layout -v`

Expected: FAIL because special history layout hooks are absent.

**Step 3: Write minimal implementation**

Replace only `history_position_card()` with semantic sections in this approved order: header (symbol, direction, exit reason); available account/margin/leverage row; primary metrics (entry, realised PnL if supplied, maximum size); secondary metrics (exit, closed size); left-label/right-value time rows; and low-emphasis existing attribution. Use current `item` values only, preserve the exit-reason Chinese mapping and Jinja escaping, and do not invent absent DeepCoin fields. Add `data-exchange-history-panel` only to the position-history section.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k deepcoin_history_position_layout -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_page_render.py src/telegram_kol_research/templates/_exchange_positions_panel.html
git commit -m "feat: add DeepCoin historical position layout"
```

### Task 3: Add scoped DeepCoin visual treatment and regression coverage

**Files:**

- Modify: `tests/test_web_assets_smoke.py`
- Modify: `src/telegram_kol_research/static/app.css:1032-1210`
- Modify: `src/telegram_kol_research/static/app.css:3740-3855`

**Step 1: Write the failing test**

Add static asset assertions for the active-tab orange `#f97316`, `.deepcoin-history-position`, white history canvas, `.deepcoin-history-times dd`, and the continuing non-history `.exchange-position-card` selector.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_assets_smoke.py -k deepcoin_history -v`

Expected: FAIL because scoped DeepCoin history styles are absent.

**Step 3: Write minimal implementation**

Add CSS scoped to active `[data-exchange-history-panel]`: a white canvas with dark text and fine gray separators; orange `#f97316` active underline; unrounded history rows; three-column grids that collapse on narrow screens; muted labels, strong values, green realised PnL when supplied; right-aligned time values; subdued attribution; desktop reading-width cap; visible tab focus states. Do not change global base colors or the other panels.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_assets_smoke.py -k deepcoin_history -v`

Expected: PASS.

**Step 5: Run focused regression suite**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_strategy_records.py -q`

Expected: PASS.

**Step 6: Commit**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.css
git commit -m "style: match DeepCoin historical positions"
```

### Task 4: Visual verification and server rollout

**Files:**

- Create: `design-qa.md`

**Step 1: Capture and compare**

Run the local web application with representative records, open the historical-position tab in the user-selected Chrome browser, and capture the same viewport as the supplied DeepCoin screenshot. Compare source and result side by side.

**Step 2: Fix and record visual QA**

Record layout, typography, colors, spacing, responsive behavior, tab activation, and timestamp ordering in `design-qa.md`. Fix all P0/P1/P2 discrepancies and repeat until the document ends with `final result: passed`.

**Step 3: Commit, push, and deploy**

```bash
git add design-qa.md
git commit -m "docs: verify DeepCoin history positions"
git push origin codex/deepcoin-auto-trading-v1
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the branch is pushed, the server pulls the reviewed commit, reinstalls the editable package, and restarts `telegram-kol.service`.
