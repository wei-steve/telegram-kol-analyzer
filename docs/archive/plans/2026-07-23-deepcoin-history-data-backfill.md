# DeepCoin 历史仓位数据回填 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让历史仓位优先展示已保存订单载荷和 DeepCoin 真实成交历史中的数字，而不是大面积显示 `--`。

**Architecture:** `list_exited_strategies` 解析已关闭 `ExecutionBinding.payload_json` 的 `draft.order_legs`，用张数加权的提交价和合约面值回填开仓价及数量。另建只读历史成交缓存，按订单/仓位身份拉取 DeepCoin 成交和仓位历史，避免在页面渲染时逐卡调用交易所。

**Tech Stack:** Python、SQLAlchemy、DeepCoin REST client、FastAPI、Jinja2、pytest。

---

### Task 1: 解析旧绑定的可验证下单载荷

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_queries.py:2242-2268`

**Step 1: Write the failing test**

创建一个闭合绑定，载荷有价格 59100/58900、数量 7/9、合约面值 0.001 的两条订单腿；断言开仓价为数量加权 `58987.5`，最大/已平仓量为 `0.016 BTC`。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k binding_payload_backfill -q`

Expected: FAIL，因为旧绑定结果尚未解析 `payload_json`。

**Step 3: Implement the minimal parser**

安全解析 JSON，忽略不完整、非数值或非正数量腿。根据 `contract_value` 写入 `entry_price_actual`、`position_size_text` 和 `history_metric_source`；不能写入平仓价或盈亏。

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_page_render.py -k binding_payload_backfill -q`

Expected: PASS。

### Task 2: 将回填数值显示在历史仓位卡

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:118-154`
- Test: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

断言闭合绑定的卡片显示加权开仓价和两项数量，并为平仓价及盈亏保留 `--`。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_page_render.py -k binding_payload_backfill -q`

Expected: FAIL，直到查询层与模板契约接通。

**Step 3: Implement minimal display source hint**

在归属信息中显示「已保存下单数据回填」提示，仅对回填字段生效。

**Step 4: Run relevant tests**

Run: `uv run pytest tests/test_web_page_render.py -k 'history or binding_payload_backfill' -q`

Expected: PASS。

### Task 3: 缓存真实平仓成交数据

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Create: `src/telegram_kol_research/deepcoin_history_cache.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_deepcoin_history_cache.py`

**Step 1: Write failing aggregation tests**

以只读成交明细/仓位历史样本验证订单匹配、数量加权开平仓价格、已实现盈亏和 TTL 缓存；缺少闭合成交时返回空值。

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deepcoin_history_cache.py -q`

Expected: FAIL，因为缓存模块尚不存在。

**Step 3: Implement read-only cache and refresh path**

只调用 `list_trade_fills` 和 `list_position_history`；使用绑定订单/仓位身份精确匹配，限速并缓存。页面渲染不逐卡同步请求交易所。

**Step 4: Verify**

Run: `uv run pytest tests/test_deepcoin_history_cache.py tests/test_web_page_render.py -q`

Expected: PASS。

### Task 4: 全量验证、提交并部署

**Files:**
- Modify: relevant files above

**Step 1: Run regression suite**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_strategy_records.py tests/test_deepcoin_history_cache.py -q`

Expected: PASS。

**Step 2: Commit and deploy**

Stage the listed source and test files, commit `feat: backfill DeepCoin history metrics`, push `codex/deepcoin-auto-trading-v1`, then run `./scripts/server_git_update.sh`.
