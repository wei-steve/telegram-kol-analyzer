# 入场候选方向与价格几何一致性门禁设计

状态：已实现并通过测试与独立评审，待集成，未部署

日期：2026-09-04

依据：`docs/2026-09-04-entry-candidate-direction-price-geometry-read-only-audit.md`

## 1. 目标与安全边界

在任何新入场或加仓请求到达交易所写边界前，确定性验证 side、入场价格域、止损和每一档止盈的方向关系。命中不一致或无法安全判定时必须 fail-closed，拒绝自动执行并转人工；系统不得猜测正确方向、交换止损止盈、重写模型结论或用市场常识补值。

该门禁只新增拒绝条件，不改变：

- MiMo、上下文识别与 lifecycle target 的权威关系；
- `exact_position_write_gate()`、position ownership verification、entry freeze、side-effect fence、client order lineage 等既有门禁；
- 已成交仓位的保护归属、补偿或管理逻辑。

本设计不包含任何存量数据修复、仓位处置、schema 变更或交易所写操作。

## 2. 方案比较与选择

| 方案 | 优点 | 不足 | 结论 |
| --- | --- | --- | --- |
| 只在 candidate 生成时检查 | 最早阻断，页面和通知容易解释 | recovery、legacy 或直接构造 execution draft 的路径可能绕过；生成后字段漂移无法覆盖 | 不足 |
| 只在执行准入时检查 | 离交易所边界最近，能覆盖最终值 | 坏 candidate 已进入后续状态，人工可见性较晚；非执行消费者仍可能误用 | 不足 |
| 共享纯校验器，在 candidate 准入与执行准入各调用一次 | 早发现，同时以最终执行值作第二道不可绕过的 fence | 需要固定两处相同语义和测试 | **采用** |

两处检查不是二选一：candidate 层负责拒绝自动接纳、保存可观测原因并告警；执行层是最终安全门，必须在任何副作用 claim/交易所 adapter 调用之前重新计算，不能信任早期“已通过”标志。

## 3. 统一数据模型与结果

新增一个无副作用的共享校验器，输入必须是已经确定来源的结构化值：

- `side`: `long | short`；
- `entry_prices`: 一至多个最终计划入场价格；
- `explicit_average_entry`: 仅在上游能证明文本明确标注“均价/average”时提供；
- `stop_loss`: 可空的单一绝对价格；
- `take_profit_prices`: 零至多档绝对价格；
- `symbol` 与执行时 contract tick size，用于既有价格归一化。

返回三态：

- `valid`：所有可用保护价与完整入场价格域一致；
- `invalid`：至少一个确定价格违反方向关系；
- `indeterminate`：side、入场或保护价格无法被确定性解析，或文本含无法消歧的额外数字。

`invalid` 与 `indeterminate` 对自动交易均 fail-closed。二者分开是为了可观测性，不是为了让后者放行。

建议原因码：

- `entry_price_geometry_stop_side_invalid`
- `entry_price_geometry_take_profit_side_invalid`
- `entry_price_geometry_equal_boundary`
- `entry_price_geometry_ambiguous`
- `entry_price_geometry_required_value_missing`

原因对象保留规范化后的 entry domain、offending field、offending value 与来源 ID；不得保存或通知整段消息正文。

## 4. 确切判据

### 4.1 价格归一化

1. 先使用既有 symbol-aware 价格归一化；结果必须是有限正数。
2. 执行层必须在 contract tick normalization 后检查，防止两个原值归一到同一 tick 后由严格关系变成相等。
3. 百分比、点数、分钟、小时、倍数等相对表达不能自动解释成绝对价格；没有已批准的确定性换算契约时返回 `indeterminate`。
4. stop_loss 必须解析为至多一个确定值；多个未标注数字返回 `indeterminate`。
5. 每一档 TP 独立校验；不能只看第一档或平均值。

### 4.2 入场价格域

- 单价入场：`entry_min = entry_max = price`。
- 区间入场：取全部计划 entry leg 的最小值与最大值，不只看中值。
- 明确均价：可用于诊断展示；若同时有区间，必须位于区间内，否则 `indeterminate`。安全门仍以整个价格域校验，因为任一 leg 都可能成交。
- market entry：candidate 阶段若没有可证明的绝对参考只能返回 `indeterminate`；执行阶段必须用将要提交时已经冻结的市场参考/订单 draft 价格域重验。
- hybrid、补仓或多 leg：把 market leg、limit leg 与 supplemental entry 全部纳入价格域，不能只看首单。

审计报告按用户指定采用中值作历史统计，执行门禁则使用完整价格域，后者更严格且直接覆盖每一个可能成交价。

### 4.3 方向关系

价格经 tick normalization 后使用严格不等式：

- long：`stop_loss < entry_min`，且每一档 `take_profit > entry_max`；
- short：`stop_loss > entry_max`，且每一档 `take_profit < entry_min`。

任一值等于边界均为 `invalid`。这避免区间某一端成交后保护价落在错误方向或零距离。缺少 TP 不由本门禁自行补造；若现有策略允许没有 TP，则只校验已提供值。缺少现有强制字段仍由原门禁拒绝，并同时记录 `required_value_missing` 供统一观测。

## 5. 两道门禁的放置

### 5.1 Candidate 准入

在权威 payload 已投影出结构化 candidate、但 `trading_decision.evaluate_trading_decision()` 返回自动交易可接纳之前调用共享校验器：

- `valid`：继续现有流程；
- `invalid` / `indeterminate`：保持模型原始字段与 candidate 证据不变，决策改为人工复核，写入精确 reason code，不生成可自动消费的 entry instruction；
- 即使通知发送失败，candidate 仍保持 fail-closed；通知失败另记 incident 并走既有重试/运维可见路径。

不得通过把 side 改成相反方向、对调 SL/TP、删除 offending field 等方式使校验通过。

### 5.2 执行准入

在 `auto_trade_execution`/order draft 已得到最终 side、全部 entry legs、SL、TP 和 contract tick，且在任何 execution ownership claim、side-effect fence、client order 创建或 adapter 调用之前，再次调用同一校验器：

- 不读取 candidate 早期“passed”缓存作为证明；以当前将提交的最终 draft 为准。
- `invalid` / `indeterminate` 立即终结本次自动执行为安全拒绝，保证 adapter 调用计数为 0。
- recovery、entry-assembly wakeup、legacy/reconcile 等任何能构造新 entry 交易所写的入口必须汇合到该最终门禁；不能各自复制近似判断。
- 如果某条路径无法提供完整价格域，它必须得到 `indeterminate` 并停止，不能绕过。

该门禁发生在 `exact_position_write_gate()` 之前或与其串联；无论顺序如何，两者都必须通过。不得把几何门禁当成 ownership gate 的替代品。

## 6. 主动告警

每次首次命中向系统所有者发送主动通知，至少包含：raw_message_id、candidate_id、chat_id、symbol、side、规范化 entry domain、offending field/value、reason code、parse source、authoritative generation。

- 告警指纹：`candidate_id + authoritative_generation + normalized_geometry + reason_code`；同一代同一错误去重，权威结果或几何变化后允许新告警。
- 通知正文不含完整消息、凭据或图片。
- 通知投递失败不允许自动交易继续；失败另报 runtime incident。
- 页面将该 candidate 显示为“方向/价格关系待人工确认”，但 UI 不是安全门。

## 7. 与现有 fail-closed 机制的关系

- `exact_position_write_gate()` 继续证明精确仓位归属，新增门禁不改变其参数、成功条件或错误处理。
- side-effect fence、execution lease、entry freeze、duplicate position、client-order lineage 与交易所读回验证均原样保留。
- Deepcoin 返回的方向错误拒单不能作为本地校验替代。交易所行为可能变化，且错误 TP/SL 的接纳规则未被证明稳定。
- 任一外部读不完整仍按 unknown/fail-closed，不得把“查不到保护单”解释为“可以重试下单”。

## 8. 存量建议（本轮不执行）

1. 冻结本次严格命中的 33 条结构化清单，人工复核模型方向与价格语义；31 条没有 binding 的记录不得补跑或追单。
2. candidate 1895 与 2187 单独保留交易所请求、拒绝/成交、平仓和 PnL 证据；两者已平仓，不做任何自动重放。若本地 lifecycle 与交易所状态仍分叉，作为独立数据修复另行授权。
3. 334 条不可判断记录进入单独的“缺少确定性价格语义”队列，不得算作通过，也不得批量改写。
4. 对生产宽松数字抽取器额外报出的 23 条解析伪影单独评估解析契约；不要与 33 条已证实几何错误混合。

## 9. 后续实施与验证边界

本设计改变交易所写准入语义，风险等级为 L3。实施必须单独授权，代码、任何 schema 变更（若最终需要）与存量处置分步执行。

最小测试矩阵：

- long/short 的单价、区间、明确均价、多 leg、多档 TP；
- 边界相等、tick normalization 后相等、单个坏 TP；
- 相对表达、缺失值、歧义多数字均 fail-closed；
- candidate 层拒绝且产生去重告警；
- 构造绕过 candidate 层的 execution draft，最终门禁仍拒绝且 adapter 调用为 0；
- recovery/wakeup/legacy entry 入口均不能绕过；
- `exact_position_write_gate()` 等既有门禁回归；
- candidate 1895、2187 固化为回归夹具，预期均被拒绝。

回滚为恢复上一运行 release；本设计不要求修改既有 schema。回滚后新门禁消失，历史 candidate/incident/告警证据保留，不自动清理或重放。
