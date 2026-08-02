# 第一止盈成交后自动成本保护 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在第一档止盈被交易所证明成交或消息驱动的部分平仓被确认后，撤销策略剩余入场腿，并把全部实时持仓腿收紧到各自实际成本价；成本价已被反穿时精确全平。

**Architecture:** 新增持久化的策略级成本保护收敛任务和逐腿状态，使用交易所完整快照与精确订单归属证明第一止盈成交。任务复用现有终止入场腿、`PositionMutationIntent`、市场成本保护判断、TPSL 替换和精确 `closePosId` 全平边界；权威消息识别与上下文策略解析保持不变。

**Tech Stack:** Python 3.12、SQLAlchemy、SQLite、pytest、Deepcoin REST、FastAPI/systemd。

---

### Task 1: 建立持久化任务、逐腿进度和交易所观察模型

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `tests/test_db_bootstrap.py`

**Step 1: 编写失败的 schema 测试**

在 `tests/test_db_bootstrap.py` 增加断言：

- `position_reconciliation_observations` 存在并保存 `venue`、`pos_id`、`size_text`、`avg_entry_price`、`pending_tpsl_json`、`snapshot_complete`、`snapshot_fingerprint` 和 `observed_at`；
- `strategy_break_even_convergences` 存在，且触发幂等键唯一；
- `strategy_break_even_convergence_legs` 存在，且 `(convergence_id, pos_id)` 唯一；
- 任务表状态、证据、目标快照、原因码和时间字段齐全；
- 现有数据库启动时能兼容增加新表和索引。

建议模型核心约束：

```python
UniqueConstraint(
    "venue",
    "strategy_instance_id",
    "trigger_type",
    "trigger_identity",
    name="uq_strategy_break_even_convergence_trigger",
)

UniqueConstraint(
    "convergence_id",
    "pos_id",
    name="uq_strategy_break_even_convergence_leg_position",
)

UniqueConstraint(
    "venue",
    "pos_id",
    "snapshot_fingerprint",
    name="uq_position_reconciliation_observation_fingerprint",
)
```

**Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_db_bootstrap.py -k 'break_even_convergence or reconciliation_observation' -v
```

Expected: FAIL，因为模型和表尚不存在。

**Step 3: 实现最小模型和 SQLite 兼容索引**

新增：

```python
class PositionReconciliationObservation(Base): ...
class StrategyBreakEvenConvergence(Base): ...
class StrategyBreakEvenConvergenceLeg(Base): ...
```

任务初始状态为 `planned`，逐腿初始状态为 `planned`。证据 JSON、目标快照 JSON 和决策 JSON 必须非空并使用稳定序列化；不要在 schema 阶段加入业务写入。

**Step 4: 运行 schema 测试**

Run:

```bash
uv run pytest tests/test_db_bootstrap.py -k 'break_even_convergence or reconciliation_observation' -v
```

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_db_bootstrap.py
git commit -m "feat: persist automatic break even convergence"
```

### Task 2: 持久化完整的仓位与 TPSL 对账观察

**Files:**
- Create: `src/telegram_kol_research/position_reconciliation_observations.py`
- Create: `tests/test_position_reconciliation_observations.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: 编写完整快照与不完整快照测试**

覆盖：

- 同一 `posId` 的数量、`avgPx` 和完整待执行 TPSL 集合被追加为不可变观察；
- 相同摘要重复写入只采用已有观察；
- 仓位接口、TPSL 接口、分页完整性或归属检查失败时记录 `snapshot_complete=False`；
- 不完整观察永远不能作为后续交易授权证据；
- 快照 JSON 不保存凭证或原始认证头。

**Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_position_reconciliation_observations.py tests/test_execution_bindings.py -k 'observation or complete_snapshot' -v
```

Expected: FAIL，因为尚无观察写入器。

**Step 3: 实现稳定观察记录器**

新增纯规范化函数和持久化函数：

```python
def build_position_observation_payload(*, position, pending_tpsl, complete): ...

def record_position_reconciliation_observation(
    session,
    *,
    venue,
    execution_binding_id,
    execution_order_leg_id,
    strategy_instance_id,
    position,
    pending_tpsl,
    snapshot_complete,
    observed_at,
): ...
```

在现有 Deepcoin 只读 reconciliation 成功取得完整仓位与 TPSL 快照后调用。摘要至少覆盖 `posId`、方向、数量、`avgPx` 和排序后的订单 ID/触发价/数量。

**Step 4: 运行测试**

Run:

```bash
uv run pytest tests/test_position_reconciliation_observations.py tests/test_execution_bindings.py -k 'observation or complete_snapshot' -v
```

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/position_reconciliation_observations.py src/telegram_kol_research/execution_bindings.py tests/test_position_reconciliation_observations.py tests/test_execution_bindings.py
git commit -m "feat: record exact position reconciliation observations"
```

### Task 3: 证明并收敛第一档止盈成交

**Files:**
- Create: `src/telegram_kol_research/take_profit_fill_evidence.py`
- Create: `tests/test_take_profit_fill_evidence.py`
- Modify: `src/telegram_kol_research/position_protection_legs.py`
- Modify: `src/telegram_kol_research/protection_reconciliation.py`
- Modify: `tests/test_protection_reconciliation.py`

**Step 1: 编写一级精确证据测试**

构造第一档 `PositionTakeProfitOrder`/`PositionProtectionLeg`，并提供相同 `ordId`、`posId`、方向和数量的成功成交或历史终态。断言返回：

```python
TakeProfitFillEvidence(
    proven=True,
    evidence_tier="exact_order_terminal",
    trigger_order_id="tp-1",
    filled_size="5",
)
```

订单 ID、持仓 ID、方向或数量不一致时返回 fail-closed 原因。

**Step 2: 编写二级结构化证据测试**

覆盖完整条件：

- 上次完整观察中 `tp-1 x5` 待执行、仓位 10；
- 当前完整观察中同一 `posId` 仓位 5、`tp-1` 消失、其余 TP 不变；
- 时间窗内没有人工/消息减仓 mutation intent。

断言证明 `exchange_position_delta`。以下任一情况必须拒绝：数量未变、变化不是 5、其余 TP 漂移、存在人工减仓、存在未知交易结果、当前快照不完整。

**Step 3: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_take_profit_fill_evidence.py tests/test_protection_reconciliation.py -k 'tp1 or position_delta' -v
```

Expected: FAIL，因为证据判定器尚不存在。

**Step 4: 实现纯证据判定器**

核心接口：

```python
def prove_first_take_profit_fill(
    *,
    tp_order,
    protection_leg,
    previous_observation,
    current_observation,
    trigger_history,
    order_history,
    trade_fills,
    conflicting_mutations,
) -> TakeProfitFillEvidence: ...
```

禁止读取行情价格。一级证据优先；一级证据缺失时才评估全部二级条件。

**Step 5: 原子更新止盈账本状态**

证据成立后，把精确第一档 `PositionTakeProfitOrder` 和 `PositionProtectionLeg` 更新为 `filled`，保存证据 JSON 和完成时间。证据不成立时不得把“订单缺失”写成成交。

**Step 6: 运行测试**

Run:

```bash
uv run pytest tests/test_take_profit_fill_evidence.py tests/test_protection_reconciliation.py -k 'tp1 or position_delta' -v
```

Expected: PASS。

**Step 7: 提交**

```bash
git add src/telegram_kol_research/take_profit_fill_evidence.py src/telegram_kol_research/position_protection_legs.py src/telegram_kol_research/protection_reconciliation.py tests/test_take_profit_fill_evidence.py tests/test_protection_reconciliation.py
git commit -m "feat: prove exact first take profit fills"
```

### Task 4: 规划和幂等采用策略级自动成本保护任务

**Files:**
- Create: `src/telegram_kol_research/break_even_convergence_planner.py`
- Create: `tests/test_break_even_convergence_planner.py`
- Modify: `src/telegram_kol_research/trading_settings.py`

**Step 1: 编写任务规划测试**

覆盖：

- 单腿 TP1 成交创建一个任务和一个逐腿目标；
- 两条 verified 实时腿中任一腿 TP1 成交，任务包含两条实时腿；
- 未成交入场腿写入策略目标快照但不作为成本止损腿；
- 相同 TP `ordId` 重复对账采用同一任务；
- 不同 worker 并发创建时唯一约束只保留一个任务；
- lifecycle/binding/entry leg 冲突、非 active 策略或非第一档 TP 时不规划；
- `move_stop_to_breakeven_after_tp1=False` 时仅返回 disabled 记录，零 live 任务；
- `management_execution_mode=shadow` 时创建 shadow 任务。

**Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_break_even_convergence_planner.py -v
```

Expected: FAIL，因为 planner 尚不存在。

**Step 3: 实现不可变策略快照和幂等采用**

核心接口：

```python
def plan_or_adopt_break_even_convergence(
    session_factory,
    *,
    trigger_type,
    trigger_identity,
    trigger_evidence,
    strategy_instance_id,
    planned_at,
    execution_mode,
) -> BreakEvenConvergenceRecord: ...
```

目标快照必须包含所有 verified live entry legs、所有 deferred entry legs、数量、`avgPx`、原保护摘要和快照时间。不要扫描或采用同标的其他策略的持仓。

**Step 4: 运行测试**

Run:

```bash
uv run pytest tests/test_break_even_convergence_planner.py -v
```

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/break_even_convergence_planner.py src/telegram_kol_research/trading_settings.py tests/test_break_even_convergence_planner.py
git commit -m "feat: plan strategy wide break even convergence"
```

### Task 5: 先撤销全部未成交入场腿并处理并发成交

**Files:**
- Create: `src/telegram_kol_research/break_even_convergence_executor.py`
- Create: `tests/test_break_even_convergence_executor.py`
- Modify: `src/telegram_kol_research/terminal_entry_cleanup.py`
- Modify: `tests/test_terminal_entry_cleanup.py`

**Step 1: 编写撤单先行测试**

覆盖一条 live 腿加一条 deferred trigger leg：

- 操作顺序必须为 `cancel deferred → readback terminal → protection write`；
- exact order/client ID 不匹配时零写入；
- 撤单响应未知时任务进入 `recovery_required`，不得改止损或全平；
- 已取消腿重复执行不再次发送撤单；
- 撤单期间 deferred leg 成交形成新 verified `posId` 时，旧目标快照失效，重新规划并把新持仓腿纳入策略级任务。

**Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_break_even_convergence_executor.py tests/test_terminal_entry_cleanup.py -k 'break_even or deferred' -v
```

Expected: FAIL，因为自动收敛执行器尚不存在。

**Step 3: 抽取并复用终止入场腿能力**

把现有人工全平/管理全平的精确撤单和回读逻辑暴露为受审计的共享函数。自动成本保护必须传入冻结快照中的精确腿集合；禁止按 symbol/side 扫描撤单。

**Step 4: 实现任务状态迁移**

实现：

```text
planned → preflight_verified → cancelling_deferred_entries
```

所有 exchange cancel 继续写 `PositionMutationIntent` 和 `ExecutionEvent`。

**Step 5: 运行测试**

Run:

```bash
uv run pytest tests/test_break_even_convergence_executor.py tests/test_terminal_entry_cleanup.py -k 'break_even or deferred' -v
```

Expected: PASS。

**Step 6: 提交**

```bash
git add src/telegram_kol_research/break_even_convergence_executor.py src/telegram_kol_research/terminal_entry_cleanup.py tests/test_break_even_convergence_executor.py tests/test_terminal_entry_cleanup.py
git commit -m "feat: cancel deferred entries before automatic break even"
```

### Task 6: 对全部实时腿执行成本保护、保留更优止损或精确全平

**Files:**
- Modify: `src/telegram_kol_research/break_even_convergence_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_market_policy.py`
- Modify: `src/telegram_kol_research/strategy_management_market_decisions.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `tests/test_break_even_convergence_executor.py`
- Modify: `tests/test_strategy_management_market_policy.py`
- Modify: `tests/test_strategy_management_executor.py`

**Step 1: 编写逐腿市场决策测试**

覆盖：

- 多单市场价高于 `avgPx` → `set_break_even`；
- 空单市场价低于 `avgPx` → `set_break_even`；
- 多单当前止损高于 `avgPx`、空单当前止损低于 `avgPx` → `keep_tighter_stop`；
- 多单市场价不高于 `avgPx`、空单市场价不低于 `avgPx` → `full_exit`；
- 两条腿各自使用自己的 `avgPx`；
- 行情快照缺失、过期、字段不可信时零写入。

**Step 2: 编写保护替换测试**

以 `10 → TP1 x5 filled → remaining 5` 为例，断言：

- 已成交第一档不重建；
- 剩余 TP 数量合法且总和不超过 5；
- 止损精确绑定相同 `posId` 并设为 `avgPx`；
- 新止损和剩余 TP 都完成交易所回读后逐腿状态才完成。

**Step 3: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_break_even_convergence_executor.py tests/test_strategy_management_market_policy.py tests/test_strategy_management_executor.py -k 'break_even or tighter_stop or crossed_cost or consumed_tp1' -v
```

Expected: 至少新自动收敛场景 FAIL。

**Step 4: 扩展不可变市场决策**

复用 `assess_break_even_market`，增加共享的“现有止损是否更优”纯判断。每个任务只允许保存一次行情报价和逐腿决策摘要；重启后加载原决策，不能用新行情改变已经开始执行的动作。

**Step 5: 复用受审计写边界**

- `set_break_even`：走现有精确 TPSL 取消、替换、回读和账本收敛；
- `keep_tighter_stop`：零交易所写入，记录证据；
- `full_exit`：走精确 `closePosId` 市价全平和 reservation；
- 任一结果未知：只回读，不重试提交。

**Step 6: 运行测试**

Run:

```bash
uv run pytest tests/test_break_even_convergence_executor.py tests/test_strategy_management_market_policy.py tests/test_strategy_management_executor.py -k 'break_even or tighter_stop or crossed_cost or consumed_tp1' -v
```

Expected: PASS。

**Step 7: 提交**

```bash
git add src/telegram_kol_research/break_even_convergence_executor.py src/telegram_kol_research/strategy_management_market_policy.py src/telegram_kol_research/strategy_management_market_decisions.py src/telegram_kol_research/strategy_management_executor.py tests/test_break_even_convergence_executor.py tests/test_strategy_management_market_policy.py tests/test_strategy_management_executor.py
git commit -m "feat: converge all strategy legs to break even"
```

### Task 7: 让已确认的消息部分离场进入同一收敛流程

**Files:**
- Modify: `src/telegram_kol_research/management_directives.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `tests/test_management_directives.py`
- Modify: `tests/test_strategy_management_reconciliation.py`
- Modify: `tests/test_strategy_management_worker.py`

**Step 1: 编写消息部分平仓确认测试**

覆盖：

- `partial_take_profit` 管理批次仅提交但未确认时不创建成本保护任务；
- 部分平仓被交易所确认后，以 `management_batch_id` 创建任务；
- 同一批次重复 reconcile 采用同一任务；
- `partial_then_break_even` 也进入同一任务，不在旧路径重复改止损；
- 开关关闭时保持原部分平仓语义，零自动保护写入；
- 识别为行情回顾或 unresolved 的消息仍不生成管理批次。

**Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_management_directives.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_worker.py -k 'confirmed_partial or automatic_break_even' -v
```

Expected: FAIL，因为已确认部分平仓尚未桥接到 planner。

**Step 3: 实现成交后桥接**

在管理 reconcile 证明 `actual_closed_size > 0` 且结果唯一后调用：

```python
plan_or_adopt_break_even_convergence(
    trigger_type="confirmed_partial_close",
    trigger_identity=str(management_batch.id),
    ...,
)
```

删除或旁路旧的重复成本保护写入，确保一个消息只有一条受审计执行链。

**Step 4: 运行测试**

Run:

```bash
uv run pytest tests/test_management_directives.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_worker.py -k 'confirmed_partial or automatic_break_even' -v
```

Expected: PASS。

**Step 5: 提交**

```bash
git add src/telegram_kol_research/management_directives.py src/telegram_kol_research/strategy_management_reconciliation.py src/telegram_kol_research/strategy_management_worker.py tests/test_management_directives.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_worker.py
git commit -m "feat: protect confirmed message partial exits"
```

### Task 8: 增加自动收敛 worker、模式控制和操作员告警

**Files:**
- Create: `src/telegram_kol_research/break_even_convergence_worker.py`
- Create: `tests/test_break_even_convergence_worker.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: 编写 worker 恢复和模式测试**

覆盖：

- dormant/disabled：记录证据但不创建可执行任务；
- shadow：规划、决策和展示完整，客户端零写调用；
- live：有界领取一个任务并执行；
- 两个 worker 并发只能领取一次；
- `recovery_required` 只调用交易所读接口；
- 服务停止时任务安全取消，重启后恢复；
- 未完成任务不阻塞消息采集和只读对账。

**Step 2: 编写告警测试**

告警包含非敏感的策略、任务、`posId`、触发订单、原因码和人工动作提示。覆盖：

- TP1 消失但成交无法证明；
- deferred entry 撤销未知；
- 成本保护或全平未知；
- 归属冲突；
- 任务超时。

断言告警重试不执行任何交易所写入。

**Step 3: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_break_even_convergence_worker.py tests/test_web_app.py tests/test_system_operator_bot.py -k 'break_even_convergence' -v
```

Expected: FAIL，因为 worker 尚未接入应用生命周期。

**Step 4: 实现有界 worker 并接入 FastAPI lifespan**

参考现有 `strategy_management_worker` 的 lease、退避和 shutdown 方式。Deepcoin 对账只负责记录观察和规划；真实写入由单独 worker 执行，避免只读 reconciliation 隐式写交易所。

**Step 5: 实现告警 outbox**

优先使用现有 `ExecutionEvent`/系统操作员通知投递方式。告警状态只影响投递，不改变任务幂等状态，也不授权重复提交。

**Step 6: 运行测试**

Run:

```bash
uv run pytest tests/test_break_even_convergence_worker.py tests/test_web_app.py tests/test_system_operator_bot.py -k 'break_even_convergence' -v
```

Expected: PASS。

**Step 7: 提交**

```bash
git add src/telegram_kol_research/break_even_convergence_worker.py src/telegram_kol_research/web_app.py src/telegram_kol_research/system_operator_bot.py tests/test_break_even_convergence_worker.py tests/test_web_app.py tests/test_system_operator_bot.py
git commit -m "feat: run and alert automatic break even convergence"
```

### Task 9: 添加生产事故回归、设置展示和审计投影

**Files:**
- Create: `tests/test_dabiaoke_tp1_break_even_regression.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_detail_holding.html`
- Modify: `tests/test_strategy_records.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `docs/runbook.md`

**Step 1: 编写大镖客 `#4163` 回归测试**

固定场景：

```text
short market leg: 10 @ 63076.7
deferred short leg: 17 @ 63910
TP ladder: 62400 x5, 61700 x3, 61000 x2
current: same posId size 5, TP1 absent, TP2/TP3 present
```

断言在 shadow 模式生成：

- 已证明 `62400 x5` 成交；
- 撤销 deferred 17；
- 不重建第一档 TP；
- 剩余 5 张使用 `63076.7` 成本保护；
- 若当前价格为 `63461.2`，空单已反穿成本，决策为 exact full exit；
- 新大镖客多单本身不参与旧空单触发证明。

**Step 2: 编写详情和审计展示测试**

持仓/策略详情展示触发证据、任务状态、撤单、每腿成本价、市场决策和最终订单。不得把 shadow 决策显示成已执行。

**Step 3: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/test_dabiaoke_tp1_break_even_regression.py tests/test_strategy_records.py tests/test_web_page_render.py -k 'break_even or tp1' -v
```

Expected: FAIL，因为事故投影尚未实现。

**Step 4: 实现只读投影和 runbook**

在 runbook 写明：

- 如何查询证据、任务、逐腿状态和 mutation intent；
- 如何关闭开关或切回 shadow；
- 未知交易所结果时只能回读；
- 如何核对未成交腿已撤销、第一档 TP 未复活、成本止损或全平已回读。

**Step 5: 运行测试**

Run:

```bash
uv run pytest tests/test_dabiaoke_tp1_break_even_regression.py tests/test_strategy_records.py tests/test_web_page_render.py -k 'break_even or tp1' -v
```

Expected: PASS。

**Step 6: 提交**

```bash
git add tests/test_dabiaoke_tp1_break_even_regression.py src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py src/telegram_kol_research/templates/_detail_holding.html tests/test_strategy_records.py tests/test_web_page_render.py docs/runbook.md
git commit -m "test: cover tp1 automatic break even incident"
```

### Task 10: 全量验证、审查、推送和 shadow 部署

**Files:**
- Modify only files listed in Tasks 1-9 plus the approved design and this plan.

**Step 1: 运行聚焦测试**

Run:

```bash
uv run pytest \
  tests/test_db_bootstrap.py \
  tests/test_position_reconciliation_observations.py \
  tests/test_take_profit_fill_evidence.py \
  tests/test_break_even_convergence_planner.py \
  tests/test_break_even_convergence_executor.py \
  tests/test_break_even_convergence_worker.py \
  tests/test_strategy_management_market_policy.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_dabiaoke_tp1_break_even_regression.py -v
```

Expected: PASS。

**Step 2: 运行相关回归和语法检查**

Run:

```bash
uv run pytest \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_worker.py \
  tests/test_execution_bindings.py \
  tests/test_protection_reconciliation.py \
  tests/test_terminal_entry_cleanup.py \
  tests/test_management_directives.py \
  tests/test_web_app.py \
  tests/test_system_operator_bot.py -q
uv run python -m compileall -q src
```

Expected: PASS，compileall 零输出。

**Step 3: 审查变更**

使用 `requesting-code-review` skill，重点检查：

- 是否存在价格触碰授权交易；
- 是否有未经过精确 `posId` 归属的写入；
- 是否在撤入场腿回读前修改保护；
- 是否会放宽已有更优止损；
- 是否会重建已成交第一档 TP；
- 是否会在未知结果后重复提交；
- 是否会因新反向策略自动关闭旧策略；
- disabled/shadow 是否严格零交易所写入。

修复全部 Critical 和 Important 发现，并重新运行受影响测试。

**Step 4: 提交最终审查修复并推送**

```bash
git status --short
git push origin codex/deepcoin-auto-trading-v1
```

只推送已审查提交，不暂存用户现有无关文件或 `uv.lock`。

**Step 5: 证明安全部署窗口**

在服务器只读检查：

- 当前 Git SHA 和服务状态；
- 所有 active management/break-even convergence；
- 当前仓位、未成交入场腿和 TPSL；
- 最近大镖客及其他群组消息；
- 是否存在时间敏感的入场、减仓、撤单或未知交易所结果。

若无法证明安全窗口，停止部署，把阶段保留为本地完成并记录待验证项。

**Step 6: 以 dormant/shadow 部署**

Run:

```bash
./scripts/server_git_update.sh
```

部署时保持自动成本保护写入关闭或 shadow。确认：

- 服务器 SHA 正确；
- editable package 已重装；
- `telegram-kol.service` active；
- 启动日志无 schema、worker 或 Deepcoin 错误；
- live mutation intent 数量没有因部署增加。

**Step 7: 运行服务器只读回放**

回放大镖客 `#4163` 和近期多腿样本，确认计划、撤单目标、逐腿 `avgPx`、第一档消费及市场回退正确，且 exchange write count 为零。

**Step 8: 单独批准 live 启用**

shadow 证据通过后向用户报告结果。除非用户在后续回合明确批准，不把自动成本保护从 shadow 切换到 live。
