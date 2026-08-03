# 延迟入场退出与成交后保护救援设计

## 事故背景

大镖客 `#4167` BTC 多单拆成两条入场腿：8 张市价腿和 14 张、62490
触发限价腿。`#4168` 随后发出“63100没站稳，求稳就找机会出局”。上下文裁决
选择了 `#4167` 对应 thread 63，并给出 `decision=exit_thread`、
`management_action=exit_full`，但一个已经没有真实仓位、仍保持 entered/active 的
`#4139` 生命周期被保留为竞争候选，使置信度降到 0.62。该结果低于 0.70 的投影
门槛，没有形成管理候选、撤单批次或平仓批次，自动化结果是
`mimo_no_action`。`#4170` 再次建议出局时也因同一幽灵生命周期而 unresolved。

因此 14 张延迟腿继续留在 Deepcoin，并于 2026-08-03 16:11:14 CST 触发、
16:11:25 全部成交为仓位 `1001124565351337`，均价 62490。

原触发入场请求带有 62000 止损。Deepcoin 在父触发单触发时生成历史 TPSL
`1001124565351336`，其 `slTriggerPrice=62000`，但创建时间比真实仓位成交早
11 秒，且成交后不在 pending TPSL 中。现有实时证据加固正确拒绝认领该订单，
理由为 `trigger_protection_candidate_predates_fill`。随后系统确认新仓位在线、
归属唯一且 pending TPSL 为零，但只继续尝试认领已有保护单；第四次重试后仍为
`retrying`。备用止损因为 `primary_stop_not_verified` 被阻止。代码库虽然已有
`plan_trigger_protection_stop_rescue()` 与
`execute_trigger_protection_stop_rescue()`，生产循环没有调用它们，所以裸仓不会
自动获得新止损。

用户已授权并完成该 14 张延迟腿的精确平仓。订单 `1001124566729987` 全部成交，
均价 62621.7；交易所仓位及关联 pending TPSL 均已归零。该处置不替代系统修复。

## 目标

1. 明确退出的精确策略必须在一个管理批次中同时关闭已验证持仓腿、撤销所有未成交
   入场腿，防止退出后延迟成交。
2. 已没有真实交易所风险的幽灵生命周期不得继续制造目标歧义。
3. 保留“早于仓位成交的保护单不得认领”不变量。
4. 延迟腿成交且没有有效止损时，系统必须基于精确 `posId` 自动建立可恢复、幂等的
   止损救援，而不是只重试认领一个永远不会重新出现的历史订单。
5. 主止损确认后恢复备用止损和计划止盈，并使逻辑保护腿从 `waiting_fill` 收敛到真实
   状态。
6. 所有交易写入继续经过持久化意图、账户级串行锁、实时交易所预检和结果未知保护。

## 非目标

- 不降低新开仓或风险增加动作的置信度门槛。
- 不按币种、方向、价格或时间猜测策略或持仓归属。
- 不认领早于仓位创建时间的 TPSL。
- 不重放历史 `#4168` 或 `#4170` 消息。
- 不让告警代替止损救援。
- 不在结果未知时重复提交撤单、平仓或 TPSL 写请求。

## 方案比较

### 方案 A：精确退出收敛 + 自动止损救援 + 生命周期清理（采用）

先用实时交易所证据清理候选集合，再允许闭合的、唯一 thread 退出裁决进入现有精确
管理批次。批次同时处理持仓腿和延迟腿。若延迟腿在竞态中成交，则重新归入同一批次
的精确关闭集合。独立的保护恢复循环在确认新仓位无止损后建立持久化止损救援，确认
主止损后再恢复其余保护。

优点：同时消除本次事故的两个根因；沿用既有身份、幂等和结果未知边界；不会把安全
认领规则改松。

代价：需要协调上下文投影、管理批次、对账、保护救援和生命周期收敛，测试范围较大。

### 方案 B：只修复退出时撤销延迟腿

改动较小，可阻止与 `#4168` 完全相同的事故，但无法覆盖消息未及时到达、撤单与成交
竞态、服务停机期间成交或交易所附加止损失效。未来仍可能出现裸仓。

### 方案 C：只告警，由人工补止损

交易所写入最保守，但裸仓持续时间取决于人工响应，且已有救援规划器和执行器没有被
利用，不满足自动交易系统的基本保护目标。

## 设计一：用实时风险状态过滤退出候选

上下文候选仍由现有策略 thread 生成，但在风险降低动作投影前增加确定性实时状态
过滤。一个生命周期只有在至少满足下列一项时，才可作为“当前风险目标”：

- 拥有当前交易所快照中存在的、已验证 entry leg `posId`；
- 拥有仍在交易所 pending 的精确 regular/trigger 入场腿；
- 拥有需要 recovery 的未知结果写入，因而必须保留但禁止直接执行。

本地 `entered/holding`、active binding 或旧 `pos_id` 字符串本身不够。已经没有真实
持仓、没有 pending 入场、没有未知结果的生命周期进入终态收敛或人工复核，不再作为
其他消息的竞争候选。

当上下文裁决同时满足：

- `decision=exit_thread`；
- `management_action` 属于闭合的 full-exit 别名；
- 恰好一个 `target_thread_id`；
- 支持证据包含当前消息和目标根消息；
- 实时过滤后该 thread 是唯一精确风险目标；
- 动作只降低风险；

允许在 `confidence >= 0.60` 时进入确定性管理投影。这个窄门槛只适用于唯一 thread 的
全退出，不能用于新开仓、加仓、放宽止损、多目标扇出或 symbol/side fallback。任何
身份冲突、未知交易结果或不完整快照继续 fail closed。

## 设计二：完整策略退出必须覆盖混合状态入场腿

复用现有 exact-strategy management batch。批次快照冻结同一 strategy instance 的：

- 所有已验证 live position entry legs；
- 所有 pending regular/trigger entry legs；
- 每条腿的订单 ID、client order ID、`posId`、数量和证据版本。

执行顺序固定：

1. 重新读取完整 positions、pending regular 和 pending trigger 快照。
2. 先撤销精确 pending 入场腿。
3. 对“撤单时已不再 pending”的腿做一次有界回读。
4. 若回读证明该腿已经形成唯一、已验证 `posId`，把它加入同一批次的关闭集合。
5. 只有所有延迟腿都证明为 cancelled 或已纳入关闭集合后，才提交精确持仓市价关闭。
6. 交易结果未知时进入 `recovery_required`，不得自动重试。
7. positions 缺席和订单终态全部确认后才结束批次及生命周期。

这延续已有 cancel-entry composite exit 规则，并把 full-exit 的混合状态行为统一到同一
不变量。

## 设计三：成交后保护救援状态机

保留现有保护认领 planner。候选保护单早于仓位、缺少创建时间、所有者冲突或快照不
完整时，仍不得认领。

新增自动救援触发条件：

- entry leg 是 `trigger_limit`；
- 腿已经由交易所成交证据转换为 `active + verified`；
- `posId`、binding、合约、方向、position mode 和数量全部一致；
- 当前完整 pending TPSL 快照证明该 `posId` 没有有效主止损；
- 没有 active position mutation、close reservation 或 management batch 占用该仓位；
- 原始 `create_trigger_entry` 事件唯一并保存了止损价；
- 保护认领结果为确定性 deferred/refused（包括 `candidate_predates_fill`），而非快照
  不可用或未知交易结果。

满足条件后立即调用现有 stop-rescue planner，持久化唯一 rescue row。执行器通过现有
exact-position mutation gateway 提交 SL-only 请求。提交前再次验证仓位数量和无止损；
写请求返回后必须回读 pending TPSL，只有精确订单真实存在才将 rescue、逻辑腿和账本
标记 verified。

救援状态：

`eligible -> ready -> submitting -> submitted -> verified`

异常状态：

- 预检不可用：保持 ready/retryable，不提交；
- 结果未知：`recovery_required`，只允许只读对账；
- 仓位消失：`noop_position_closed`；
- 已有精确止损：认领或 `noop_already_protected`；
- 身份/数量冲突：blocked 并告警。

不能先等五轮认领失败才救援。第一份完整快照已经同时证明“仓位存在”和“有效止损不
存在”时即可规划救援；认领和救援在同一个账户级锁下二选一。

## 设计四：恢复完整保护

主止损 verified 后，使用已持久化的 `position_protection_legs` 恢复其余保护：

1. 备用止损按现有 buffer 规则规划和提交；
2. 止盈数量从该腿的实际当前 size 及原始比例计算，进行 contract step 向下取整；
3. 余数给最后一档，所有止盈数量总和不得超过当前仓位；
4. 每档使用独立逻辑 leg、mutation intent 和订单回读；
5. 若仓位已部分关闭或数量变化，重新规划，不复用旧数量；
6. 任一结果未知时停止后续写入。

若源策略已经收到已接受的 full exit、close reservation 正在执行或生命周期进入终态，
保护恢复不得与关闭竞争；关闭优先，救援改为 noop/recovery observation。

## 设计五：状态收敛与告警

对账确认触发腿成交时，逻辑保护腿不能继续显示为普通 `waiting_fill`。应转换为：

- `protection_recovery_pending`：仓位已成交，保护待认领或救援；
- `verified`：交易所订单已回读并写入账本；
- `blocked`：身份冲突或不可恢复拒绝；
- `terminal`：仓位已关闭，无需保护。

建立最高等级保护不变量：

`live verified position + no verified/pending stop + no close in progress`

告警内容只包含有界标识：strategy instance、binding/leg ID、哈希化或允许展示的
`posId`、原计划止损、裸露开始时间、rescue 状态和下一次动作。不得输出 API 凭据或
完整交易所响应。若同群存在未解决裸仓，默认阻止该群新的自动开仓，但不阻止精确平仓
或提高保护。

## 数据与迁移

优先复用：

- `trigger_protection_intents`
- `trigger_protection_stop_rescues`
- `position_protection_legs`
- `position_protection_ledger`
- `position_mutation_intents`
- `strategy_management_batches/legs`
- `position_attribution_audits`

若现有 rescue 状态列足够，本次不新增表。仅在无法表达
`recovery_required/noop_position_closed/verified` 时扩展闭合状态值，不保存任意异常文本。

## 测试策略

### 退出回放

- 回放 `#4167 -> #4168`，同时放入无真实仓位的 `#4139` 生命周期；实时过滤后只保留
  thread 63，生成一个 full-exit batch。
- 批次包含8张已成交腿和14张 pending trigger 腿，先撤14张再关8张。
- 模拟撤单边界14张刚成交：一次有界回读后把新 `posId` 纳入关闭集合。
- 任何身份不唯一、结果未知或快照不完整都产生零额外交易写入。

### 保护回放

- 父触发单带62000止损；Deepcoin先产生历史 TPSL，11秒后仓位成交；pending TPSL
  为空。
- 认领 planner 必须继续返回 `candidate_predates_fill`。
- 同一轮必须规划一个精确 SL-only rescue，而不是只增加认领重试次数。
- 执行、回读、重启恢复和重复 worker tick 均只能产生一张主止损。
- 主止损 verified 后，备用止损与三档止盈按当前数量恢复。

### 负面测试

- 位置数量变化、position mode 冲突、`posId` 不唯一、父事件不唯一、已有未知 mutation、
  close reservation、已有 opaque TP、交易所快照不可用。
- 每个负面场景都必须证明零未授权交易所写入。
- 生命周期终态或全退出进行中不得启动保护救援。

## 发布与回滚

1. 本地 TDD 完成退出、救援、状态和告警测试。
2. 服务端运行相同聚焦测试；交易所写调用全部使用 fake client。
3. 先部署候选过滤、状态观测和 rescue shadow planner，只记录“本应救援”的结果。
4. 审核自然发生的延迟成交样本，确认每个计划都具有唯一 `posId`、完整快照和正确止损。
5. 经用户再次明确批准后才启用 rescue live executor；full-exit 仍沿用当前 live 管理总开关。
6. 启用后被动观察首个自然样本，不创建真实仓位测试。
7. 回滚通过 Git revert 和标准部署；已提交但结果未知的 mutation 只能继续只读对账，
   不因回滚重试。

历史 `#4167` 已由人工授权精确平仓，不做消息重放或历史订单修造。
