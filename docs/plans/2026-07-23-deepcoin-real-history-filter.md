# DeepCoin Real History Position Filter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restrict the history-position page to verified, closed DeepCoin positions with complete official metrics.

**Architecture:** Add a narrow eligibility predicate to the existing binding-to-history transformation. A row is eligible only when cached DeepCoin position evidence includes a real `posId` and all official metrics necessary for the page; the existing layout consumes only those verified rows and preserves strategy attribution.

**Tech Stack:** Python 3.12, SQLAlchemy, FastAPI/Jinja, pytest.

---

### Task 1: Specify history-position eligibility with tests

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_queries.py`

**Step 1: Write failing tests**

Add fixtures proving a binding with saved order payload only is excluded, while a binding with `history_metrics` containing `posId`, `avgPx`, `closeAvgPx`, `pnl`, `pos`, and `closePos` is retained.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_page_render.py -k 'verified_history or saved_order' -q`

Expected: FAIL because existing query includes saved-order fallbacks.

**Step 3: Implement minimal eligibility predicate**

Create a private helper that validates the cached DeepCoin metric schema and use it in the history-position query path. Do not alter data for other page tabs.

**Step 4: Run targeted tests**

Run: `uv run pytest tests/test_web_page_render.py -k 'verified_history or saved_order' -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py tests/test_web_page_render.py
git commit -m "fix: show only verified DeepCoin history positions"
```

### Task 2: Preserve DeepCoin time order and complete metrics

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_queries.py`

**Step 1: Write failing sort/metric test**

Create two valid DeepCoin history fixtures with opposite close times. Assert descending DeepCoin close time, stable position identifier tie-breaker, and all five metrics rendered as numbers.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py::test_verified_deepcoin_history_orders_by_close_time -q`

Expected: FAIL if the query still uses local lifecycle update time.

**Step 3: Implement minimal ordering and mapping changes**

Persist/consume the DeepCoin close time from cached history metrics and sort eligible rows by it. Expose close amount separately where necessary.

**Step 4: Run targeted test**

Run: `uv run pytest tests/test_web_page_render.py::test_verified_deepcoin_history_orders_by_close_time -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py tests/test_web_page_render.py
git commit -m "fix: order verified history by DeepCoin close time"
```

### Task 3: Verify the page contract and deploy

**Files:**
- Test: `tests/test_web_page_render.py`
- Deploy: `scripts/server_git_update.sh`

**Step 1: Run relevant regression suite**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_strategy_records.py -q`

Expected: PASS.

**Step 2: Push reviewed commits**

Run: `git push origin codex/deepcoin-auto-trading-v1`

**Step 3: Update production**

Run: `./scripts/server_git_update.sh`

Expected: server pulls the branch, reinstalls the editable package, and restarts `telegram-kol.service`.

**Step 4: Server verification**

Query the production database and page endpoint. Confirm displayed history bindings all include a non-empty `posId`, close price, PnL, and DeepCoin close timestamp; verify service health.

**Step 5: Commit only if verification changes code**

No code change is expected in this task.
