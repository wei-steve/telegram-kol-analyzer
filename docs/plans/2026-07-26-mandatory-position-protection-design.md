# 强制仓位保护与精确 TPSL 归属设计

## 目标

所有自动开仓的已成交 entry leg 必须拥有：

- 一条已验证主止损；
- 一条已验证第二止损；
- 策略计划中的全部分段止盈。

第二止损和多段止盈不是可选功能，不保留功能开关。系统同时扫描并补齐所有
active/open 自动仓位缺失的保护；手工绑定仓位和无法证明归属的交易所订单继续
fail closed。

## 已确认问题

生产系统当前存在两个独立问题：

1. `reconcile_deepcoin_execution_bindings()` 的
   `backup_stop_submission_enabled` 默认是 `False`，生产 worker 和周期
   reconciliation 没有显式启用，因此第二止损执行器没有运行。
2. 第二止损执行器验证主止损时调用通用原生 TPSL 匹配器。Deepcoin pending
   TPSL 回读不提供 `posId`，`sz=0` 全仓止损在存在多笔同合约同方向分仓时被判定
   为 ambiguous，即使本地已经存在精确的 `ordId ↔ posId` 保护账本映射。

第二个行为与项目已经选定并实现的官网式 TPSL 归属模型不一致。历史设计明确规定：
本系统创建 TPSL 时持久化请求中的精确 `posId` 和响应中的 `ordId`，后续按
`ordId` 回连本地账本；价格、数量、方向和时间只能核对已知关系，不能猜测归属。

## 权威关联模型

仓位和保护单的权威链为：

```text
StrategyLifecycle
  → ExecutionBinding
  → verified ExecutionOrderLeg
  → exact posId
  → PositionProtectionLeg
  → exchange ordId
  → PositionProtectionLedger(ordId ↔ posId)
```

各层职责：

- `ExecutionOrderLeg.pos_id` 是策略 entry leg 对真实分仓的权威归属。
- `PositionProtectionLeg` 表示每个主止损、第二止损和分段止盈的逻辑生命周期。
- `PositionBackupStopOrder` 和 `PositionTakeProfitOrder` 保存具体执行状态。
- `PositionProtectionLedger` 是取得交易所订单 ID 后的精确
  `ordId ↔ posId ↔ entry_leg` 映射。
- Deepcoin pending TPSL 是订单仍存在及字段仍一致的实时证据，但其缺失的
  `posId` 不能推翻已持久化的精确映射。

## 归属优先级

验证一条交易所 TPSL 与持仓的关系时依次使用：

1. 交易所行直接返回 `PositionID/positionId/posId/closePosId`，且与当前实时
   仓位和权威 entry leg 一致。
2. 交易所 `ordId` 命中 verified/active 的本地保护账本，账本的 `pos_id` 和
   `execution_order_leg_id` 仍指向当前权威 active entry leg。
3. 对系统刚提交的订单，精确请求 `posId`、响应 `ordId` 和按同一 `ordId`
   取得的 pending 回读共同建立新映射。
4. 没有精确键的旧订单或人工订单保持 unattributed；价格、数量、方向、时间不得
   单独提升为精确归属。

如果交易所直接返回的 position ID 与本地账本冲突，或同一 `ordId` 在本地映射到
多个 `posId`，该订单进入 conflict，不属于任何仓位，也不得被自动修改或撤销。

## 强制保护状态机

每条已成交自动 entry leg 按以下顺序推进：

```text
position_verified
  → primary_stop_verified
  → backup_stop_submitting
  → backup_stop_verified
  → take_profit_ready
  → take_profit_submitting
  → protection_complete
```

执行规则：

1. entry leg 必须是 active、attribution verified，并具有唯一真实 `posId`。
2. 实时仓位必须与 entry leg 的合约、方向、分仓模式和持久化经济数据一致。
3. 主止损必须通过精确账本映射和当前 pending `ordId` 回读验证。
4. 创建第二止损前先持久化 submitting 状态；请求必须携带精确 `posId`。
5. 第二止损响应必须返回唯一 `ordId`，并按该 ID 回读验证类型、价格、方向和
   市价止损语义。
6. 第二止损验证成功后写入逻辑保护腿、专用执行表和统一保护账本。
7. 只有第二止损 verified 后，才逐条创建缺失的分段止盈。
8. 每段止盈使用精确 `posId`，按响应 `ordId` 回读验证并写入三层记录。
9. 所有计划止盈均 verified，且止盈数量之和不超过当前仓位数量后，状态成为
   `protection_complete`。

任何步骤失败只冻结当前仓位，不阻塞其他仓位。

## 幂等和未知结果

- 第二止损以 `venue + pos_id` 保证只有一个活动执行记录。
- 分段止盈以 `venue + execution_order_leg_id + planned_trigger_price` 作为逻辑
  唯一身份。
- 提交前重新读取交易所和本地账本；完全匹配的系统订单采用而不是重复创建。
- 每次交易所写请求前先提交本地 `submitting` 记录。
- 网络超时、响应缺少订单 ID 或提交后回读不确定时标记
  `unknown_exchange_outcome/pending_readback`，禁止盲目重试。
- 已成功创建部分止盈时保留已验证订单，仅补齐可证明缺失的目标。
- 服务重启后从持久化状态恢复，不重放已经 verified 的写操作。

## 存量仓位补齐

部署后扫描全部 active/open 自动仓位，而不只处理大镖客：

- 保护完整：不修改。
- 主止损有精确账本且仍在 pending：补第二止损，再补全部缺失止盈。
- 第二止损已验证但止盈不完整：只补缺失止盈。
- 本地没有精确关联，但存在可从已保存请求 `posId`、响应 `ordId` 和权威 entry
  leg 证明的旧系统订单：先执行本地证据回填，再进入正常补齐流程。
- 存在无法归属的相似订单、人工订单、冲突映射、真实主止损缺失或仓位快照变化：
  不重复下单，记录事故并等待人工处理。

扫描使用有界批处理和 Deepcoin 写限速，但不提供关闭功能的业务开关。每个仓位在
实际写入前都必须重新获取最新仓位和 pending TPSL 快照。

大镖客持仓 `1001124367311625` 走通用流程。当前证据为：

- entry leg `375`，active/verified；
- 主止损订单 `1001124367311731`；
- 主止损价格 `62500`；
- `PositionProtectionLedger` 已保存
  `1001124367311731 ↔ 1001124367311625`；
- 计划止盈 `65100/65800/66400`。

实现不得增加该仓位专用分支。

## 异常分类

- `primary_stop_ledger_missing`：主止损没有精确本地映射。
- `primary_stop_order_missing`：账本订单 ID 不在当前 pending TPSL。
- `primary_stop_identity_conflict`：交易所 position ID 与本地账本冲突。
- `backup_stop_pending_readback`：第二止损提交结果尚未完成回读验证。
- `backup_stop_similar_unowned`：存在相似但无法精确归属的订单。
- `take_profit_partial`：只完成部分计划止盈。
- `position_snapshot_changed`：提交前后仓位身份或经济数据变化。
- `protection_plan_invalid`：价格方向、数量分配或安全边界不合法。
- `protection_order_unknown_outcome`：交易所写入结果不确定，禁止自动重试。

事故记录必须包含 binding、entry leg、posId、相关 ordId 和非敏感 reason code，
不得记录 API 密钥、签名或认证头。

## 部署与验收

本地以 TDD 增加精确账本优先、冲突 fail closed、无开关强制执行、批量存量补齐及
重启幂等测试。真实项目验证必须在服务器完成：

1. 部署前获取数据库和交易所只读基线及指纹。
2. 更新服务后先确认所有目标仓位的保护计划和冲突列表。
3. 运行正常 reconciliation，让系统按有界批处理自动补齐所有安全目标。
4. 逐仓核对主止损、第二止损、全部止盈的 `ordId ↔ posId` 账本。
5. 对大镖客仓位确认第二止损 verified，且
   `65100/65800/66400` 止盈全部存在、数量总和正确。
6. 对全部存量目标确认没有重复订单、没有修改手工仓位、没有
   unknown outcome 被自动重试。
7. 重启 `telegram-kol.service` 后再次 reconciliation，交易所写入计数应为零。

发布发现任何归属冲突、快照变化或回读不完整时停止该仓位，不通过降低证据标准完成
验收。
