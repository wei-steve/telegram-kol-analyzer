# DeepCoin 原生 TPSL 保护迁移 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将成交持仓的第二止损与分批止盈改为可在 DeepCoin 原生“止盈止损”中显示、可读回验证的 TPSL 订单，同时保留委托级主止损并安全迁移旧的通用触发单。

**Architecture:** 未成交限价或条件入场保持通过入场订单的 `slTriggerPx` / `slOrdPx=-1` 保护；不得以没有确切 `posId` 的仓位 TPSL 替代它。成交后的精确分仓使用 `set-position-sltp` 分别创建原生第二止损和每一档分批止盈；读取待挂订单时，以系统 `ordId` 优先、唯一的合约/方向/时间/数量特征回退匹配。迁移必须“创建、读回验证、再撤旧单”，手工 TPSL 或归属不唯一一律冻结。

**Tech Stack:** Python 3、SQLAlchemy、pytest、DeepCoin 私有 REST API、现有交易保护账本与执行队列。

---

## 已验证的交易所事实

- DeepCoin 手机端手工创建的 BTC-USDT-SWAP 全仓市价止损返回 `triggerOrderType=TPSL`、`slTriggerPrice=63000`、`sz=0`，且可能没有 `posId`。
- 同一分仓已存在系统主止损 `slTriggerPrice=63200`、`sz=6`；两个原生 TPSL 同时待挂，说明第二止损应为独立 TPSL，而非通用 `trigger-order`。
- 现有第二止损使用 `/deepcoin/trade/trigger-order`，虽可待挂但不会按原生“止盈止损”模型回读；现有止盈执行器提交后也没有以原生待挂订单回读确认。

### Task 1: 集中原生 TPSL 规范化与安全匹配

**Files:**
- Create: `src/telegram_kol_research/native_tpsl.py`
- Modify: `src/telegram_kol_research/deepcoin_order_matching.py`
- Test: `tests/test_deepcoin_order_matching.py`

**Step 1: 写出失败的匹配测试**

在 `tests/test_deepcoin_order_matching.py` 增加覆盖：系统 `ordId` 精确匹配；无 `posId` 的 `sz=0` 手工单只能在唯一候选时匹配；两个相同特征候选必须返回冲突而非任意选择；TP 与 SL 的触发价格和数量均必须相符。

```python
def test_native_tpsl_match_refuses_ambiguous_zero_size_stop():
    match = match_native_tpsl_order(position, [manual_a, manual_b], expected)
    assert match.status == "ambiguous"
    assert match.order is None
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_deepcoin_order_matching.py -q`

Expected: FAIL，因为原生 TPSL 标准化器与带状态的匹配结果尚不存在。

**Step 3: 实现最小的标准化与匹配器**

在 `native_tpsl.py` 实现不可变的 `NativeTpslExpectation`、`NativeTpslMatch`、`normalize_native_tpsl()` 和 `match_native_tpsl_order()`：

```python
def match_native_tpsl_order(position, orders, expected):
    exact = [o for o in orders if o.ord_id == expected.ord_id]
    if exact:
        return verify_native_tpsl(exact[0], expected)
    candidates = unique_identity_candidates(position, orders, expected)
    return verified_one_or_conflict(candidates, expected)
```

仅接受 `triggerOrderType == "TPSL"`；支持 `slTriggerPrice` / `slTriggerPx`、`tpTriggerPrice` / `tpTriggerPx` 和 `sz=0` 全仓语义。把 `deepcoin_order_matching.py` 中的分散判断改为调用此模块，保留现有公开函数兼容性。

**Step 4: 运行匹配与回归测试**

Run: `pytest tests/test_deepcoin_order_matching.py tests/test_deepcoin_client.py -q`

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/native_tpsl.py src/telegram_kol_research/deepcoin_order_matching.py tests/test_deepcoin_order_matching.py
git commit -m "feat: match deepcoin native tpsl orders safely"
```

### Task 2: 固化入场委托级主止损的边界

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_deepcoin_order_builder.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: 写出失败的入场保护测试**

新增断言：未成交 limit/trigger 入场载荷包含 `slTriggerPx`、`slTriggerPxType="last"` 和 `slOrdPx="-1"`，不含 `posId`，也不调用 `set_position_sltp`。已有且可精确识别的委托仅可经 `replace-order-sltp` 更新。

```python
def test_pending_limit_entry_embeds_primary_market_stop_without_position_id():
    payload = build_entry_order_payload(signal)
    assert payload["slTriggerPx"] == "63200"
    assert payload["slOrdPx"] == "-1"
    assert "posId" not in payload
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_deepcoin_order_builder.py tests/test_recovery_live_submit.py -q`

Expected: FAIL，直到测试明确限制仓位 TPSL 的使用范围。

**Step 3: 以最小改动实现边界**

让 `_deepcoin_embedded_sltp_fields()` 成为所有未成交入场建单的唯一主止损来源；为更新路径加入“必须有确切交易所 `ordId`”的守卫。禁止任何 pending-entry 代码将主止损转换成仓位级 `set_position_sltp`；市场入场仅在仓位归属已验证后才允许走仓位级主止损恢复。

**Step 4: 运行订单构建回归**

Run: `pytest tests/test_deepcoin_order_builder.py tests/test_recovery_live_submit.py tests/test_deepcoin_execution_actions.py -q`

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/recovery_live_submit.py tests/test_deepcoin_order_builder.py tests/test_recovery_live_submit.py
git commit -m "fix: keep primary stop attached to pending entry"
```

### Task 3: 将第二止损改为原生 TPSL，距离为 20 bps

**Files:**
- Modify: `src/telegram_kol_research/trigger_backup_stop.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Test: `tests/test_trigger_backup_stop.py`
- Test: `tests/test_backup_stop_repair.py`

**Step 1: 写出失败的第二止损载荷与验收测试**

测试主止损 63,200 的多仓生成第二止损 63,073.6（20 bps），并要求提交到 `set_position_sltp` 的载荷为 `slTriggerPx`、`slTriggerPxType="last"`、`slOrdPx="-1"`、精确 `posId`，而不是 `triggerPrice` 或 `closePosId`。测试提交成功不等于成功：只有原生待挂 TPSL 回读的订单 ID、方向、价格和数量均吻合才记录 active。

```python
def test_backup_stop_uses_native_tpsl_and_requires_readback():
    result = execute_backup_stop(intent, client, pending_orders)
    assert client.set_position_sltp_calls[0]["slTriggerPx"] == "63073.6"
    assert result.status == "pending_readback"  # before matching order appears
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_trigger_backup_stop.py tests/test_backup_stop_repair.py -q`

Expected: FAIL，因为当前代码构造并发送通用 `trigger-order`。

**Step 3: 实现原生第二止损与读回状态机**

保留 `build_backup_stop_trigger_payload` 作为兼容入口，但改为生成仓位级原生 TPSL 载荷；执行器调用 `deepcoin_client.set_position_sltp()`。读取 `/pending-tpsl-order` 结果后使用 Task 1 匹配器，将状态区分为 `active`、`pending_readback`、`ambiguous`、`rejected`；`pending_readback`/`ambiguous` 均不重试创建、不覆盖已有保护。用 Decimal 与合约步长处理 20 bps，禁止硬编码 `0.005`。

**Step 4: 运行第二止损回归**

Run: `pytest tests/test_trigger_backup_stop.py tests/test_backup_stop_repair.py tests/test_position_take_profit_orders.py -q`

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/trigger_backup_stop.py src/telegram_kol_research/trigger_backup_stop_executor.py src/telegram_kol_research/backup_stop_repair.py tests/test_trigger_backup_stop.py tests/test_backup_stop_repair.py
git commit -m "fix: submit backup stops as native tpsl"
```

### Task 4: 将分批止盈改为“提交后原生回读”

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Test: `tests/test_trigger_take_profit_convergence.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_position_take_profit_orders.py`

**Step 1: 写出失败的 TP 归属和回读测试**

覆盖每个分批 TP 都有自己的 `tpTriggerPx`、`tpOrdPx="-1"` 与精确 `sz`；原生待挂响应漏 `posId` 仍能由唯一身份归属；只收到 REST 提交 ID 而未在待挂 TPSL 中读回时，不写入 active `PositionTakeProfitOrder`。

```python
def test_take_profit_is_not_active_until_native_tpsl_readback_matches():
    execute_trigger_take_profit_convergence(..., pending_orders=[])
    assert session.query(PositionTakeProfitOrder).filter_by(status="active").count() == 0
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py -q`

Expected: FAIL，因为当前执行器在提交响应后即持久化 active 记录。

**Step 3: 实现逐档 TP 的验证与冻结**

在计划和执行器之间传递 `NativeTpslExpectation`；提交后刷新 pending TPSL，并经 Task 1 匹配器验证。仅验证成功的档位可标记 active；未读回或冲突档位写为待确认/冻结并带请求、响应、匹配原因。收敛门只在主止损与原生第二止损都已验证时开放，数量和合约步长不完整时不补单。

**Step 4: 运行 TP 与保护门回归**

Run: `pytest tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py tests/test_trigger_backup_stop.py -q`

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/trigger_take_profit_convergence.py src/telegram_kol_research/trigger_take_profit_convergence_executor.py src/telegram_kol_research/position_take_profit_orders.py tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py
git commit -m "fix: verify native take profit orders before activation"
```

### Task 5: 实现旧通用订单的冻结式迁移

**Files:**
- Create: `src/telegram_kol_research/native_tpsl_migration.py`
- Create: `scripts/migrate_native_tpsl_protection.py`
- Modify: `src/telegram_kol_research/backup_stop_repair.py`
- Test: `tests/test_native_tpsl_migration.py`
- Test: `tests/test_backup_stop_repair.py`

**Step 1: 写出失败的迁移决策测试**

新增测试覆盖：系统拥有的旧 generic backup 必须在原生单创建并读回后才取消；任何手工 63,000 止损、多个无 `posId` 候选、主止损缺失、仓位数量不一致或 API 不确定错误都产生 `frozen` 决策，且绝不撤单。

```python
def test_manual_native_stop_freezes_migration_without_cancelling_old_backup():
    decision = plan_native_tpsl_migration(position, pending_orders)
    assert decision.status == "frozen"
    assert decision.cancel_order_ids == ()
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_native_tpsl_migration.py tests/test_backup_stop_repair.py -q`

Expected: FAIL，因为迁移规划器和只读 CLI 均不存在。

**Step 3: 实现显式两阶段迁移**

实现 `plan_native_tpsl_migration()` 与 `execute_native_tpsl_migration()`：读取即时仓位、现有原生 TPSL、系统账本和旧通用 `trigger-order`；构造需要新增的 backup/TP；先提交并读回验证；仅验证的同一系统旧 `ordId` 可取消。把每个决策、请求、响应、读回证据和撤单结果写入既有执行事件/订单 JSON 审计字段，禁止将手工订单标为系统拥有。CLI 默认 `--dry-run`，真实模式必须显式 `--execute --position-id <id>`，一次只能迁移一个精确持仓。

**Step 4: 运行迁移单测与只读演练**

Run: `pytest tests/test_native_tpsl_migration.py tests/test_backup_stop_repair.py -q`

Run: `python scripts/migrate_native_tpsl_protection.py --help`

Expected: 测试 PASS；CLI 显示 `--dry-run`、`--execute` 与 `--position-id`。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/native_tpsl_migration.py src/telegram_kol_research/backup_stop_repair.py scripts/migrate_native_tpsl_protection.py tests/test_native_tpsl_migration.py tests/test_backup_stop_repair.py
git commit -m "feat: migrate generic protection to native tpsl safely"
```

### Task 6: 增加生产前保护审计输出

**Files:**
- Modify: `scripts/readonly_crosscheck_inspect.py`
- Modify: `src/telegram_kol_research/protection_snapshot.py`
- Test: `tests/test_protection_snapshot.py`

**Step 1: 写出失败的审计快照测试**

要求每个真实持仓输出主止损来源（entry/native）、第二止损协议（native/generic/none）、每档 TP 的原生验证状态、匹配策略、冻结原因和是否检测到人工订单；不得把单纯的提交响应显示为已保护。

**Step 2: 运行测试确认失败**

Run: `pytest tests/test_protection_snapshot.py -q`

Expected: FAIL，直到快照包含协议、读回和冻结字段。

**Step 3: 实现只读审计字段**

快照使用 Task 1 的标准化订单和匹配状态，将 `verified`、`pending_readback`、`ambiguous`、`manual`、`legacy_generic` 明确展示。脚本只读取数据库与交易所，不得提交、取消或修改订单。

**Step 4: 运行快照与全套本地测试**

Run: `pytest tests/test_protection_snapshot.py -q`

Run: `pytest -q`

Expected: 全部 PASS。

**Step 5: 提交**

```bash
git add scripts/readonly_crosscheck_inspect.py src/telegram_kol_research/protection_snapshot.py tests/test_protection_snapshot.py
git commit -m "feat: audit native tpsl protection state"
```

### Task 7: 服务器验证、受控迁移与上线

**Files:**
- Modify: `docs/plans/2026-07-25-native-tpsl-protection-design.md`
- Modify: `docs/plans/2026-07-25-native-tpsl-protection-migration.md`

**Step 1: 在服务器执行只读核对**

推送已审查分支后，在服务器先运行快照与迁移 dry-run，只记录当前 13 个持仓的订单 ID、原生 TPSL 数、旧 generic backup 数、TP 档位和冻结原因。不得在这一步运行 `--execute`。

```bash
python scripts/readonly_crosscheck_inspect.py
python scripts/migrate_native_tpsl_protection.py --dry-run --position-id 1001124330342183
```

Expected: 截图对应持仓因人工 63,000 原生止损进入 `frozen`，不会新增或撤销任何订单。

**Step 2: 先以一个无人工订单且归属唯一的仓位进行实盘迁移验证**

在用户明确确认目标仓位后，执行单一 `--position-id` 的真实迁移；立刻重新拉取 pending TPSL，确认新原生第二止损和每档 TP 均显示，旧系统 generic 订单才可撤销。

Expected: 新 TPSL 的订单 ID、方向、价格、数量与计划一致；无保护空窗；手工订单未变。

**Step 3: 审查并更新验收记录**

将实际请求/读回字段、冻结数量和迁移结果写回设计与本计划的“实施记录”小节，不能写入任何 API 密钥或会话信息。

**Step 4: 部署服务**

```bash
git push origin codex/deepcoin-auto-trading-v1
powershell -ExecutionPolicy Bypass -File .\\scripts\\server_git_update.ps1
```

Expected: 服务器从审查分支更新 editable package，并重启 `telegram-kol.service`；随后再次只读快照确认新逻辑没有自动接管人工 TPSL。

**Step 5: 提交验收记录**

```bash
git add docs/plans/2026-07-25-native-tpsl-protection-design.md docs/plans/2026-07-25-native-tpsl-protection-migration.md
git commit -m "docs: record native tpsl migration verification"
```
