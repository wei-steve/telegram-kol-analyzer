# DeepCoin 历史仓位深色主题 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将历史仓位标签页恢复为交易执行台一致的深蓝黑主题。

**Architecture:** 只修改历史标签页作用域内的 CSS 颜色覆盖，继续复用现有布局、指标和标签状态。测试断言关键深色变量和历史作用域存在。

**Tech Stack:** CSS、FastAPI、pytest。

---

### Task 1: 为历史页建立深色主题测试

**Files:**
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

断言历史页作用域 CSS 使用 `var(--surface-panel)`、浅色正文及深色分割线。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_assets_smoke.py -k history -q`

Expected: FAIL，因为现有历史页使用白色。

### Task 2: 替换历史页的白色覆盖

**Files:**
- Modify: `src/telegram_kol_research/static/app.css:1167-1320`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Implement minimal CSS change**

把历史页容器、卡片和分组标题的白色背景替换为深色面板；将正文和时间改成浅色，保留橙色标签下划线和盈亏语义颜色。

**Step 2: Run verification**

Run: `uv run pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py -q`

Expected: PASS。

### Task 3: Commit and deploy

**Files:**
- Modify: files above

**Step 1: Run regression suite**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_strategy_records.py -q`

Expected: PASS。

**Step 2: Deploy**

Commit the CSS and tests, push `codex/deepcoin-auto-trading-v1`, then run `./scripts/server_git_update.sh`.
