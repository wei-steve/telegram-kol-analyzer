# 触发入场保护认领实时证据加固设计

## 背景

触发入场父单成交后，Deepcoin 可能生成不带 `posId` 和父单号的匿名止损子单。
现有实现已经能够依据已验证成交腿排除尚未成交的兄弟腿，但部署前复查发现仍有三个安全缺口：

1. pending TPSL 候选没有强制晚于目标仓位的权威成交时间；history 候选使用父单提交时间，而不是真实成交时间。
2. 正常对账和监督修复没有在最终认领前证明目标 `posId` 当前仍存在且数量未变化。
3. 通用保护账本 upsert 会覆盖已有 `venue + order_id` 的所有者字段，陈旧计划或并发写入可能造成保护单静默改绑。

这些缺口不会让现有测试失败，因为当前测试只覆盖候选形状、兄弟腿状态和普通幂等性，尚未覆盖成交前候选、已平仓/减仓快照以及计划后的所有权竞争。

## 目标

只有在同一份完整、只读交易所快照同时证明以下事实时，系统才允许自动或监督认领触发入场保护单：

- 目标仓位仍然在线；
- `posId`、合约、方向和数量与入场请求一致；
- 候选保护单不是提交前基线订单；
- 候选保护单创建时间不早于目标仓位的权威创建/成交时间；
- 候选 `ordId` 没有其他账本或逻辑保护腿所有者；
- 最终写入时上述数据库所有权仍未改变。

修复只建立现有交易所对象的身份关系，不创建、撤销、替换或修改任何 Deepcoin 订单。

## 非目标

- 不修改止损或止盈价格。
- 不修复 `1695 → 1795` 的消息解析问题。
- 不改变触发入场策略识别、目标策略解析或仓位管理策略。
- 不新增自动平仓政策。
- 不把 `ExecutionOrderLeg.last_verified_at` 重新解释为成交时间。

## 方案比较

### 方案 A：实时仓位证据 + 全局不可变账本（采用）

从当前交易所仓位快照取得 `posId`、合约、方向、数量和 `cTime`，把它们作为本次认领的权威活性证据。候选订单必须晚于该时间。保护账本的所有者字段改为写入后不可变，同一 `ordId` 只允许相同所有者刷新可变证据。

优点：不需要数据库迁移；正常对账和监督修复可以共享同一规则；同时封堵所有保护账本调用路径的静默改绑。

代价：若交易所缺少仓位创建时间或返回不完整快照，本次认领必须延后或拒绝。

### 方案 B：仅在触发保护 finalizer 增加检查

改动最小，但其他调用通用账本 upsert 的路径仍可能覆盖所有者，无法建立系统级不变量。

### 方案 C：新增永久 `verified_fill_at` 字段

长期语义最完整，但需要数据库迁移、历史回填以及旧仓位证据降级策略。本次问题可以从当前完整快照获得足够证据，不值得扩大迁移范围。

## 权威证据模型

新增不可变值对象 `TriggerProtectionLivePosition`，至少包含：

- `pos_id`
- `instrument_id`
- `side`
- `size_text`
- `created_at`
- `observed_at`

`created_at` 来自当前仓位的交易所 `cTime`，解析失败或缺失时视为证据不完整。`observed_at` 是本轮完整快照的读取时间，只用于证据和指纹，不替代成交时间。

目标仓位验证规则：

- 当前非零仓位中必须恰好存在一个相同 `posId`；
- 合约和方向必须与入场腿一致；
- 当前数量必须与触发入场请求数量数值相等；
- `cTime` 必须存在且可解析；
- 快照读取出现 positions 或 TPSL 错误时不得执行认领。

数量已经变化意味着保护单原始数量可能不再对应当前风险敞口，因此即使仓位仍存在也不得认领旧保护单。

## 候选时间规则

`plan_trigger_protection_intent_adoption()` 必须接收目标 `TriggerProtectionLivePosition`，并对 pending 与 history 候选统一执行：

```text
candidate_created_at >= live_position.created_at
```

候选时间从 `cTime`、`createdAt`、`created_at`、`createdTime` 中解析。不得使用 `uTime` 单独证明订单创建时间，因为订单后续更新会改变它。

处理结果：

- 候选创建时间缺失：`trigger_protection_candidate_time_unavailable`
- 候选早于仓位：`trigger_protection_candidate_predates_fill`
- history 候选除上述规则外，仍需满足现有父单引用和上界约束。

基线排除、保护形状、兄弟腿所有权和唯一候选规则保持不变。

## 正常对账数据流

1. `load_deepcoin_execution_reconciliation_snapshot()` 读取 positions、pending TPSL 和 history。
2. positions 读取失败时沿用现有 evidence-unavailable 路径，不消耗永久恢复次数。
3. 为每个待处理意图从 `snapshot.positions` 建立唯一 `TriggerProtectionLivePosition`。
4. 不在线、身份冲突、数量变化或时间缺失时，不调用 finalizer；写入有界拒绝证据。
5. planner 使用同一目标仓位证据筛选订单并生成 action。
6. finalizer 在当前数据库事务内重新验证保护账本及逻辑保护腿所有权，再原子写入逻辑腿、账本、意图和 revision。

已验证腿在交易所仓位消失时可以暂时保留历史归属，但不能因此获得新的保护单认领权限。

## 监督修复数据流

`build_entry_protection_ledger_repair_plan()` 在 `include_trigger_entries=True` 时：

1. 只调用 Deepcoin 只读接口，读取一次完整 positions 快照和按合约读取的 pending TPSL。
2. 对目标 `posId` 建立与正常对账相同的 `TriggerProtectionLivePosition`。
3. 使用同一个 planner 生成 action 或 refusal。
4. action evidence 必须包含有界的仓位身份、数量、仓位创建时间和快照时间。
5. 这些字段进入计划 fingerprint。

执行 `--apply` 前仍需重新生成计划。仓位消失、数量变化、时间变化、候选集合变化或所有权变化都会改变计划或产生 refusal，从而拒绝旧指纹。

## 账本不可变性

强化 `upsert_protection_ledger_row()` 的系统级不变量：

```text
(venue, order_id) ->
  execution_binding_id,
  execution_order_leg_id,
  strategy_instance_id,
  pos_id,
  instrument_id,
  side
```

若已存在同一 `venue + order_id`：

- 所有者字段完全相同：允许更新状态、触发价、数量、证据和最后观察时间；
- 任一所有者字段不同：抛出明确的 `protection_ledger_owner_conflict`，禁止覆盖。

finalizer 还必须检查：

- 目标 primary logical leg 未绑定其他 `exchange_order_id`；
- 其他 logical leg 未绑定该 `exchange_order_id`；
- 已存在的 ledger 行为空或属于完全相同的 binding、entry leg 和 `posId`。

任一冲突必须使当前事务整体回滚。数据库已有的 `venue + order_id` 唯一索引继续负责并发插入竞争；所有权验证负责禁止已有行被改写。

## 错误处理

- 快照不可用：保持意图 `retrying`，不消耗证据失败次数。
- 仓位不在线：返回 `trigger_protection_position_not_live`；自动路径不得写新账本。
- 数量变化：返回 `trigger_protection_position_size_changed`。
- 时间证据缺失或候选早于成交：拒绝该候选并记录有界证据。
- 所有权冲突：抛出不可恢复身份冲突，事务回滚并产生事故审计。
- 没有候选但快照完整：沿用有界 `not_yet_observable` 重试。

所有错误证据不得包含 API 凭据或完整交易所原始响应。

## 测试策略

### planner 单测

- pending 候选早于仓位 `cTime`，拒绝。
- history 候选早于仓位 `cTime`，拒绝。
- 候选缺少可证明创建时间，拒绝。
- 候选时间等于或晚于仓位时间，其他条件满足时允许。
- 仓位不存在、重复、合约/方向不一致、数量变化或缺少 `cTime`，拒绝。

### 正常对账测试

- 本地腿仍为 `active + verified`，但交易所仓位已消失，不认领遗留 TPSL。
- 当前仓位已部分减仓，不认领原数量保护单。
- 完整活仓位与唯一候选可以正常完成逻辑腿、账本、意图和 revision 原子写入。

### 监督修复测试

- dry-run evidence 和 fingerprint 包含有界 live-position 证据。
- 计划后仓位关闭或数量变化，旧 fingerprint apply 失败。
- 修复规划和执行不调用任何 Deepcoin 写接口。

### 不可变账本测试

- 相同所有者重复 upsert 幂等成功。
- 相同 `ordId` 的不同 binding、entry leg 或 `posId` 被拒绝且旧行不变。
- planner 生成 action 后，另一个所有者抢先写入该 `ordId`，finalizer 回滚逻辑腿、意图和 revision。
- 其他逻辑保护腿已经绑定该 `ordId` 时拒绝认领。

## 发布与回滚

- 所有改动先以现有安全路径的附加拒绝条件上线，不增加交易所写入。
- 本地运行聚焦测试、完整测试、compileall 和 `git diff --check`。
- 部署前必须重新证明没有时效性策略操作、in-flight management batch 或 unknown exchange outcome。
- 部署后只读检查服务状态、错误日志和保护认领审计。
- 代码回滚使用标准 Git 部署路径；已经建立的正确账本映射不删除。

