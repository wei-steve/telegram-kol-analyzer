# 仓位管理 SLA 与精确写入边界设计

## 目标

消除两类已经在生产发生的最高等级资金安全事故：

1. Telegram 明确减仓/止盈消息被识别后，因保护恢复阻断而长时间没有执行，也没有在可接受时限内升级人工接管。
2. 多个同合约、同方向拆分仓并存时，一次临时保护清理把其他策略的止损当成目标仓位旧止损并成功取消。

修复后，任何仓位管理消息必须在有界时间内进入“已完成”或“已明确升级人工接管”状态；任何持仓或 TPSL 写操作都必须在唯一、精确、最新的 `posId + execution binding + execution leg + ordId` 权威链上执行。

## 已确认事故证据

三姐消息 `chat_id=-1003000736304, message_id=1137` 要求：

- BTC 多单止盈 50%；
- 剩余仓位止损移动到开仓价。

消息入库约 1.5 秒，权威识别约 18 秒完成，但管理项立即以
`protection_recovery_required` 失败。该状态没有自动恢复时间、执行截止时间或立即升级告警。
后续生产修复才将持仓从 20 张减为 10 张，并创建保本止损
`ordId=1001124380144623, trigger=63895.725`。

随后事件 `execution_events.id=2894` 以另一个仓位
`posId=1001124331869718, binding=193` 的名义执行
`strategy_management_superseded_protection_cleanup`，实际取消的却是三姐的止损订单
`1001124380144623`。交易所返回成功。这个清理动作不在当前仓库正式代码中，属于一次性生产修复/清理路径。

触发错误的关键证据是：另一个同方向 BTC 拆分仓的 position 快照错误出现
`slTriggerPx=63895.725`。该值不是精确订单归属证据，却被清理路径用于寻找“旧保本止损”。

## 事故等级与安全原则

该问题定为 P0：

- 明确减风险指令静默延迟；
- 已创建的止损被跨仓取消；
- 交易所写入入口没有统一执行所有权校验；
- 修复结束前没有验证“目标改变正确、非目标保持不变”。

修复遵守以下不可破坏原则：

- `ExecutionOrderLeg.pos_id` 且 `attribution_status="verified"` 是持仓归属权威。
- TPSL 订单只有在持久化账本中存在唯一 `venue + ordId → posId + binding + leg` 关系时才可修改或取消。
- `positions.slTriggerPx/tpTriggerPx` 只用于展示和异常检测，永不授权写操作。
- 相同 symbol、side、price、size、时间和 position API 聚合字段都不是所有权证据。
- Deepcoin 返回结果不含 `posId/closePosId` 时，只能采用提交时已持久化的精确订单 ID 归属，不能反向猜测。
- 无法证明归属时冻结单个动作并立即升级；不得扩大到同币种、同方向其他仓位。
- 一次性 CLI、修复脚本和 Web 服务必须经过同一个交易所写入网关。
- 请求结果未知时只回读，不重复提交。

## 方案选择

### 方案 A：只修复错误清理条件

删除按价格寻找旧止损的代码，继续使用现有执行和恢复路径。

优点是改动最小。缺点是无法解决静默延迟、一次性脚本绕过、状态不一致和恢复无时限，不足以关闭本次 P0。

### 方案 B：增加重试和告警

保留分散的写入入口，为 `blocked` 增加重试和超时通知。

可以降低延迟，但仍允许某个新脚本直接调用 `cancel_position_sltp()`。跨仓误删仍可能再次发生。

### 方案 C：统一精确写入网关、持久化时限状态机和生产不变量审计

所有 position/TPSL 写入经过数据库权威校验；仓位管理状态机有明确 SLA、恢复和升级状态；生产修复前后验证目标与非目标持仓不变量。

这是批准采用的方案。

## 总体架构

```text
Telegram message
  → durable instruction item
  → authoritative recognition
  → exact management target
  → SLA/deadline-bearing management batch
  → exact mutation authority snapshot
  → Deepcoin position mutation gateway
  → exchange readback by exact IDs
  → post-write account invariant audit
  → completed | recovery_required | operator_required
```

### 统一写入网关

新增 `position_mutation_gateway.py`，成为以下操作的唯一生产入口：

- 精确拆分仓部分减仓/全平；
- 设置精确持仓 TPSL；
- 修改精确持仓 TPSL；
- 取消精确持仓 TPSL；
- 清理被同一持仓、同一执行腿的新保护明确取代的旧订单。

网关接收不可变 `PositionMutationAuthority`：

```python
@dataclass(frozen=True, slots=True)
class PositionMutationAuthority:
    venue: str
    strategy_instance_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    order_id: str | None
    expected_position_fingerprint: str
    expected_protection_fingerprint: str | None
```

权威对象只能由数据库 verified entry leg、最新交易所 position 快照和精确保护账本共同构造。网关在每次写入前重新验证：

- lifecycle、binding 和 leg 仍属于同一 strategy；
- leg 仍是 verified、非终态，并拥有目标 `posId`；
- 交易所仍唯一返回目标 `posId`；
- instrument、side、position mode、margin mode、size 和均价没有越过允许边界；
- 取消/修改订单时，账本中的 `ordId` 所有者与 authority 完全一致；
- 请求中的 `posId/closePosId` 与 authority 一致；
- 目标指纹仍有效；
- live execution gate 仍开启。

任何失败都在发 HTTP 请求之前拒绝，并记录无敏感信息的审计事件。

### 原始 Deepcoin 写方法收口

`DeepcoinRestClient` 保留低级 HTTP 能力，但 position/TPSL 写方法改为内部未校验方法，只允许网关所在模块调用。增加架构测试扫描 AST，禁止生产模块和脚本直接调用：

- `set_position_sltp`;
- `cancel_position_sltp`;
- `modify_position_sltp`（新增支持时）；
- position close 的原始 `place_order`;
- 任何保护清理专用低级方法。

entry 下单仍使用现有受控路径，但 position close 和 position TPSL 必须通过网关。

### 精确订单所有权

扩展保护账本约束：

- `venue + order_id` 唯一；
- 一个 active order 只能属于一个 `execution_order_leg_id` 和一个 `pos_id`；
- 所有权一旦写入不能被另一 binding 收养；
- replacement 记录必须保存 `previous_order_id` 和 `replacement_order_id`；
- “被替代”只允许在相同 binding、leg、posId 内成立；
- 未知订单保持 unattributed，不得用于清理。

不再从 `Position.slTriggerPx` 反查订单 ID。该字段只参与：

- UI 展示；
- 与精确账本的漂移告警；
- 触发只读重新审计。

### 有时限的管理状态机

每个管理 instruction item 和 batch 持久化：

- `received_at`;
- `recognized_at`;
- `execution_deadline_at`;
- `operator_escalation_at`;
- `next_attempt_at`;
- `attempt_count`;
- `last_progress_at`;
- `escalation_state`;
- `escalation_notified_at`.

默认 SLA：

| 阶段 | 目标 |
|---|---:|
| Telegram 入库 | 5 秒 |
| 权威识别 | 30 秒 |
| 计划生成 | 5 秒 |
| 正常交易所执行 | 60 秒 |
| 无进展升级 | 90 秒 |
| 强制人工接管 | 3 分钟 |

状态：

```text
pending
  → planning
  → ready
  → executing
  → reconciling
  → succeeded

planning/executing/reconciling
  → retry_wait
  → planning/reconciling

任何非终态超过 90 秒
  → operator_required + critical notification
```

`blocked` 不再允许成为没有 `next_attempt_at` 或人工升级记录的沉默终态。

错误分三类：

1. 临时可恢复：快照不完整、网络读取失败、交易所可见性延迟。按 5/15/30/40 秒退避，始终受 90 秒升级时限约束。
2. 写入结果未知：只回读原 client/order ID，不再次提交。
3. 不可安全自动恢复：所有权冲突、目标变化、保护账本冲突。立即进入 `operator_required`，不重试写入。

### 复合“减仓并推保本”事务

固定顺序：

1. 持久化 exact target 和当前保护快照。
2. 验证要取消的每个旧保护订单都属于相同 binding、leg、posId。
3. 持久化所有将使用的 client order ID。
4. 仅取消该 position 的已验证旧保护。
5. 使用精确 `closePosId` 提交减仓。
6. 按订单 ID 和成交回报确认实际剩余数量。
7. 按实际剩余数量重建止盈。
8. 按实际开仓均价建立保本止损。
9. 逐个读回新订单并立即写入精确账本。
10. 重新读取账户全部 position/TPSL。
11. 验证目标仓位达到计划状态，同时所有非目标仓位的精确订单集合没有变化。
12. 只有通过不变量审计后才标记 succeeded。

如果旧保护不能精确归属，不能自动取消；任务在 90 秒内升级人工处理。不能为了“及时”跨越所有权边界。

### 实际值与消息值分离

管理计划同时保存：

- `requested_stop_loss_text="63900"`：KOL 消息要求；
- `effective_stop_loss_text="63895.725"`：按真实均价得到的执行值；
- `confirmed_stop_loss_text`：交易所读回确认值。

生命周期的当前止损只由 `confirmed_stop_loss_text` 更新。UI 同时显示三者及确认时间，禁止用旧 lifecycle 值伪装当前交易所保护。

### 生产操作隔离

增加独立设置：

- `entry_execution_mode`;
- `management_execution_mode`;
- `position_repair_execution_mode`.

发生 P0 时可禁用新开仓和自动管理，同时保留只读监听、识别、审计和告警。修复 CLI 默认 dry-run；apply 必须提供：

- 单个 action ID；
- 单个精确 posId；
- 最新指纹；
- 明确的 operator confirmation token。

不提供 apply-all。

## 告警

以下情况立即发送最高优先级 Telegram 运维通知，不受普通重复抑制窗口影响：

- 明确减仓/全平/推保护消息 90 秒未完成；
- active live position 没有已验证主止损；
- 已验证止损从 pending 消失但持仓仍存在；
- 一个取消事件的 event binding/posId 与保护账本所有者不一致；
- 非目标持仓的保护订单集合在管理事务期间发生变化；
- 任一生产写入绕过统一网关；
- operator_required 超过 3 分钟未确认。

通知仅包含策略、posId 后四位、动作、原因和操作入口，不包含凭证。

## 测试策略

必须覆盖：

- 两个及以上 BTC long split positions；
- 另一个 position 的 `slTriggerPx` 返回目标止损价格；
- pending TPSL 不返回 `posId`；
- exact ledger owner 与价格推断冲突；
- 临时脚本试图跨 binding 取消订单；
- 管理任务在可见性失败后有界重试；
- 90 秒升级且通知只发送一次；
- 服务重启恢复原事务，不重复减仓；
- close 已成交但 TPSL 创建结果未知；
- 目标成功、非目标保护变化时整批不得成功；
- requested/effective/confirmed stop 正确落库；
- dry-run 指纹在任一持仓或订单变化后失效。

## 生产发布门禁

发布分四步，不使用 shadow，也不中断新消息操作：

1. 读取并记录现有 entry、management 配置；部署期间保持原值，repair 写入保持禁用。
2. 在服务器运行只读全账户审计，确认每个 live `posId` 的策略和保护所有权。
3. 使用 fake 回放三姐和另一 BTC long 的事故场景；确认跨仓取消请求在客户端调用前被拒绝。
4. 以兼容替换方式直接启用新的管理写入边界；单个异常只阻断对应 mutation intent，不全局关闭无关的新消息处理。

恢复 live 之前必须记录：

- 部署 commit；
- 服务状态；
- 全账户保护审计；
- 三姐精确止损恢复结果；
- 非目标不变量审计；
- SLA 告警测试结果；
- 生产监控状态。

## 非目标

- 不重新设计 AI 识别模型；
- 不允许系统从模糊消息自动增加风险；
- 不自动清理无法归属的历史人工订单；
- 不用数据库显示值替代交易所读回；
- 不在本次修复中增加批量生产修复按钮。
