# DeepCoin 历史仓位指标完整性 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 固定展示历史仓位的五项 DeepCoin 成交指标，并以 `--` 明确标识缺失的真实数据。

**Architecture:** 历史仓位模板输出固定的五项指标，不再按字段是否存在而隐藏指标项。模板只使用数据查询层已经提供的实际成交价、盈亏和仓位数量；CSS 为缺失值及负盈亏提供局部样式，不影响其他标签页。

**Tech Stack:** Python、FastAPI、Jinja2、CSS、pytest。

---

### Task 1: 定义完整指标的渲染契约

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

新增一个含入场价、但没有平仓价、盈亏和数量的已退出生命周期记录；断言响应含全部五项标签，且每项缺失值渲染为 `--`。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k complete_history -q`

Expected: FAIL，因为当前模板会隐藏缺失指标。

**Step 3: Write minimal implementation**

暂不在本任务写实现；此失败测试定义模板输入输出契约。

**Step 4: Commit**

与 Task 2 的模板实现一同提交。

### Task 2: 固定输出五项历史仓位指标

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:136-145`
- Test: `tests/test_web_page_render.py`

**Step 1: Implement the minimal template change**

将两个条件指标网格改为固定五项：开仓均价使用 `entry_text` 或 `entry_price_actual`，平仓均价使用 `exit_price_actual`，盈亏使用 `realized_pnl`，两项数量使用 `position_size_text`。每个缺失值输出 `--`，不将策略计划价转换为实际成交价。

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k complete_history -q`

Expected: PASS。

**Step 3: Commit**

```bash
git add src/telegram_kol_research/templates/_exchange_positions_panel.html tests/test_web_page_render.py
git commit -m "feat: complete DeepCoin history metrics"
```

### Task 3: 补齐缺失值与盈亏色彩

**Files:**
- Modify: `src/telegram_kol_research/static/app.css:1273-1284`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

断言 CSS 包含只作用于历史仓位的缺失值中性颜色和负盈亏红色规则。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_assets_smoke.py -k history -q`

Expected: FAIL，因为当前仅有正盈亏绿色规则。

**Step 3: Write minimal implementation**

为 `deepcoin-history-metric-missing` 和 `deepcoin-history-pnl-negative` 添加局部样式；已有正盈亏使用绿色类。

**Step 4: Run focused and regression tests**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_strategy_records.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add src/telegram_kol_research/static/app.css tests/test_web_assets_smoke.py
git commit -m "style: clarify missing history metrics"
```
