# 阶段 2：去重、乱序保护、心跳、断线状态机与 REST 重同步

风险等级：**L2**（durable consumer 与恢复路径）。若本阶段引入新列或新表，
那一个提交按 **L3** 处理，必须补 schema 演练。仍然不做任何交易所写入。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

## 目标

把阶段 1 的"来什么存什么"升级成一条**可信的事件流**：知道哪些帧是重复的、
哪些是乱序的、连接现在处于什么状态、断线期间漏了什么、漏了之后怎么用 REST 补回来。

这一阶段结束时，系统应当能回答"到某个时刻为止，我对交易所状态的观测是完整的还是有缺口的"。
它仍然不依赖这些事件做任何决定。

## 前置

- 阶段 1 已 `completed`，`deepcoin_ws_events` 在生产里持续有数据。
- 手上有阶段 1 观察窗口内的真实事件样本（做重放测试要用真的，不要全用手编的）。
- 读 `docs/rest-ws-trading-status.md` 第 5、8 条（可提炼代码、字段格式差异）。

## 任务

### 1. 连接状态机

严格按交接文档的五态实现，不增不减：

```text
connecting -> healthy -> disconnected -> resyncing -> healthy
```

- 状态与转移时间落库或落进程内快照均可，但必须能通过只读端点观察到。
- `disconnected` 与 `resyncing` **只能产生"未知"，永远不能产生"零"或"无"**。
  任何读到这两个状态就返回空列表的代码都是错的。
- `healthy` 只有在"所有对象收敛"之后才能设置（见任务 5）。

### 2. 心跳与断线检测

- `websockets` 的 `ping_interval` / `ping_timeout` 设为实验里验证过的 10/10 秒。
- 另外维护一个**应用层静默计时器**：超过阈值没收到任何帧就主动判定为
  `disconnected` 并重连。协议层 ping 存活不等于业务流存活。
- 重连用指数退避（建议 1s 起、上限 60s、带抖动），不要固定间隔。
- listenkey 续期：官方为滑动一小时。实现定时续期，续期失败按 `disconnected` 处理并
  重新 `acquire_listen_key`。

**缺口检测的前提要写进代码注释**：Deepcoin 私有流**没有**公开的连续序号或
断线补播保证（公共行情的 `ResumeNo` 不适用于私有流）。因此本阶段的缺口检测只能是
"时间水位 + REST 重同步"，不能是"序号连续性"。不要发明一个假的序号。

### 3. 去重

去重键：`(channel, payload_hash)` + 同一 `exchange_time_ms`。

- 重复帧**不删除**，标记 `processed_state='duplicate'`，保留行。
  历史帧是判断交易所行为的唯一素材，删掉就再也拿不回来。
- 去重必须是幂等的：同一帧处理两次结果相同。
- 统计重复率并暴露在健康端点上。

### 4. 乱序保护

按实体身份分别维护"已知最新状态"，**旧状态不得覆盖新状态**：

| 实体 | 身份键 | 排序依据 |
|---|---|---|
| Order | `order_sys_id` | `exchange_time_ms`，相同则用 `received_ms` |
| Trade | `order_sys_id` + 成交标识 | 同上 |
| Position | `position_id`（短键 `PI`） | 同上 |
| TriggerOrder | `order_sys_id` | 同上 |

关键约束：**`TriggerOrder.TU` 从 `default` 变成真实 posId 是单向的**（实验中
`TS` 同时从 `0` 变 `1`）。一条 `TU=default` 的旧帧到达时，不得把已经是
`TU=<posId>` 的状态改回 `default`。这是本阶段最容易写错的一处。

跨频道**不假定任何顺序**。Trade 可能先于 Order 到达，Position 可能先于 Trade。
不要写"必须先看到 Order 才处理 Trade"的逻辑。

### 5. 断线后的 REST 重同步

进入 `resyncing` 后，按交接文档的重启序列执行（把它作为通用的"重新取得完整观测"流程）：

1. REST 查询活动普通订单、条件单、成交、当前仓位。
2. 重放本地未处理的持久化事件。
3. 重新建立 WS 并完成订阅。
4. **再做一次 REST 快照**，覆盖"第一次快照到订阅成功"之间的竞态窗口。
5. 比较事件状态与 REST 状态，**只允许状态前进**。
6. 全部收敛后才切回 `healthy`。

第 4 步不能省。省掉它就是把竞态窗口伪装成不存在。

REST 重同步的读全部走既有 `DeepcoinRestClient` 的 `list_*` / `read_*` 方法，
不新增读接口。合约标识归一化：WS 的 `ETHUSDT` ↔ REST 的 `ETH-USDT-SWAP`，
**用显式映射表**（从 `list_swap_instruments` 构建并缓存），不要字符串拼接推断。

### 6. 新入场暂停钩子（本阶段只建不启用）

交接文档要求："若新入场依赖 WebSocket 获取 TPSL 关联，WebSocket 未订阅或处于
gap 状态时应暂停新入场。"

阶段 5 之前入场并不依赖 WS，所以本阶段**只实现判定函数并暴露状态**，
不接到入场路径上。函数签名与语义现在定好，阶段 5 直接接：

```text
ws_observation_permits_new_entry() -> (bool, reason_code)
# healthy 且无未收敛缺口 -> (True, "")
# 其他一切情况（含 connecting）-> (False, <state>)
```

默认 fail-closed：状态未知时返回 `False`。

### 7. 健康端点扩展

`GET /api/runtime/deepcoin-ws-health` 增加：`state`、`state_since`、
`last_frame_at`、`reconnect_count`、`duplicate_rate_1h`、`out_of_order_count_1h`、
`last_resync_at`、`last_resync_outcome`、`permits_new_entry`。
仍然不返回任何 payload 内容。

## 禁止

- 禁止删除任何 `deepcoin_ws_events` 行。重复帧标记，不删除。
- 禁止让 `disconnected` / `resyncing` 状态返回空集合当作"没有"。
- 禁止用旧帧覆盖新状态，尤其禁止把 `TU=<posId>` 改回 `TU=default`。
- 禁止发明私有流的序号或假定断线补播。
- 禁止把 WS 观测写进任何既有账本（`execution_bindings`、
  `position_protection_ledger`、`trigger_protection_intents`、
  `position_take_profit_orders`）。本阶段仍然是只写自己的表。
- 禁止把暂停新入场的判定接到入场路径上（那是阶段 5）。
- 禁止任何交易所写入。REST 重同步只用 GET。
- 禁止用 `git add -A`。

## 验证等级与具体检查项

等级 **L2**（若加列则该提交补 L3 的 schema 演练：副本上 `init_db`、
`PRAGMA quick_check`、五张关键表 before/after 行数、回滚方案）。

### 补测项（交接文档 12 项中的第 4、5 项）

- [ ] **第 5 项：重复和乱序事件。** 用阶段 1 采集的真实帧构造离线重放：
      同一帧投递两次、三帧逆序投递、`TU=posId` 后再投 `TU=default` 的旧帧。
      断言：状态不回退、重复被标记、计数正确。这一项必须是确定性离线测试。
- [ ] **第 4 项：成交前断线、成交期间断线、成交后重连。**
      - 离线：注入连接异常，断言状态机走 `healthy → disconnected → resyncing → healthy`，
        且 `resyncing` 期间 `permits_new_entry=False`。
      - 生产：在观察窗口内至少主动触发一次重连（重启 worker 即可），
        确认走完五步重同步且 `last_resync_outcome` 为成功。
      - "成交期间断线"在本阶段无法用受控实盘复现（那要等阶段 5 的受控实验），
        用离线注入覆盖，并在汇报里明确标注这一项在本阶段只有离线证据。

### 测试

- [ ] focused：状态机全部转移、去重幂等、乱序保护、退避与抖动、
      合约名映射表缺失时的 fail-closed、`permits_new_entry` 的默认 False。
- [ ] 最终候选跑一次全量套件（记录已知既有失败）。

### 生产观察

- [ ] 观察 30 分钟，覆盖至少 5 条真实消息、尽量覆盖 2 个群。
      若 30 分钟内不足 5 条，**停止而不是延长**，把阶段留在 `in_progress`
      并记录流量不足（AGENTS.md L2 的明确要求）。
- [ ] 重启 worker 一次（本阶段核心主张就是恢复），确认重启后重同步走完且
      没有把重启期间的空观测写成"零"。
- [ ] 检查积压与重复处理：`processed_state` 各值的计数分布合理，
      没有大量卡在 `unprocessed`。
- [ ] 直接查交易所历史一次，确认本阶段零新增写入。
- [ ] `duplicate_rate_1h` 与 `out_of_order_count_1h` 记进证据，这是后续阶段的基线。

## 完成条件

1. 上面全部检查项通过，或"流量不足"已按 L2 规则记录且阶段留在 `in_progress`。
2. 提交已推送并 `tg-deploy` 部署，回滚 SHA 已记录。
3. 更新 `docs/rest-ws-trading-status.md`：推进到阶段 3
   （`phase-3-wake-reconciliation.md`），证据区追加一行。
4. 发消息给 `brain_session_id`。

## 汇报格式

```text
阶段 2 完成 / 阻塞 / 流量不足留 in_progress
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>
是否含 schema 变更：是/否（是则附副本演练结论）
补测项 4：离线证据 + 生产证据（分别列出）
补测项 5：离线重放用例与断言结果
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟），真实消息数、覆盖群数
状态机：reconnect_count、last_resync_outcome
重复率 / 乱序数（1h 基线）：
交易所写入：零
异常与遗留：
证据路径（服务器）：
```
