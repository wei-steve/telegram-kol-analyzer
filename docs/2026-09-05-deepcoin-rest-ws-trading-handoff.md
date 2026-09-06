# Deepcoin REST + WebSocket 交易逻辑重构交接

更新时间：2026-09-05

## 新会话的任务

基于本文件中的真实交易证据，重新设计系统的 Deepcoin 下单、成交确认、
仓位归属和止盈止损归属流程。先设计和评审，不直接修改或部署生产交易逻辑。

开始前先读取仓库根目录 `AGENTS.md`，检查共享工作区的 Git 状态。禁止
`git add -A`，不得覆盖其他会话的未提交文件。

## 已验证结论

推荐架构：

```text
REST：下单、改单、撤单、精确查询、启动快照、断线补账、最终核验
WebSocket：实时接收 Order、Trade、Position、TriggerOrder 变化
数据库：交易意图、幂等状态、原始事件、身份绑定和保护账本
```

普通市价单和普通限价单应使用 `POST /deepcoin/trade/order`。只有真正需要
“价格条件满足后再创建实际订单”的业务才使用
`POST /deepcoin/trade/trigger-order`。当前大量使用 trigger-order 会增加
“父触发单 -> 子普通单 -> posId”这一层，而 Deepcoin REST 没有稳定公开的
父子外键。

## 成功的真实实验

实验参数：

```text
instrument  ETH-USDT-SWAP
mode        cross + split
side        short
size        0.1 contract = 0.01 ETH
fill        2478.78
take profit 2468.78
stop loss   2488.78
```

身份结果：

```text
REST main ordId = 1001125145471184
REST posId      = 1001125145471184
TPSL ordId      = 1001125145471183
```

本样本中 main ordId 恰好等于 posId，但不得将这种数值相等写成交易所不变量。
权威关联来自 WebSocket 的字段引用和 REST 回读，而不是 ID 相等或相邻。

完整证据目录：

```text
/var/lib/telegram-kol-cutover-evidence/eth-rest-ws-tpsl-short-no-clordid-test-20260905/live-ab734b3900f6
```

主要文件：

```text
live-summary.json
correlation.json
ws-events.jsonl
ws-status.jsonl
raw.jsonl
submit-short-request.json
submit-short-response.json
```

## 关键 WebSocket 发现

下单后、入场成交前：

```text
Order.OS        = 1001125145471184
TriggerOrder.OS = 1001125145471183
TriggerOrder.TU = default
TriggerOrder.TS = 0
```

入场成交后，同一 TPSL 再次推送：

```text
Trade.OS        = 1001125145471184
TriggerOrder.OS = 1001125145471183
TriggerOrder.TU = 1001125145471184
TriggerOrder.TS = 1
REST posId      = 1001125145471184
```

因此，本次完整成交的普通限价空单建立了以下精确链路：

```text
REST 返回的 main ordId
-> WebSocket Trade.OS
-> REST 验证的 split posId
-> WebSocket TriggerOrder.TU
-> WebSocket TriggerOrder.OS，即 TPSL ordId
```

REST 的 trigger-orders-pending 能返回 TPSL ordId、方向、数量、价格和时间，
但没有 posId 或 parentOrdId。本次真正补齐 TPSL 到仓位关联的是成交后的
`TriggerOrder.TU == REST posId`。

事件时间线：

```text
19:19:22.182 UTC  REST /order 接受
19:19:22.242 UTC  WebSocket PushOrder
19:19:22.243 UTC  PushTriggerOrder，TU=default
19:24:00.086 UTC  主订单状态变化
19:24:00.087 UTC  同一 TPSL 更新，TU=真实 posId
19:24:00.089 UTC  PushTrade，OS=main ordId
19:24:04 UTC      REST 确认主订单 filled
19:24:05 UTC      REST positions 取得 posId
```

在该实验的轮询间隔下，WebSocket 比 REST 约早 4 至 5 秒发现成交及关联。

## clOrdId 结果

使用唯一字符串 `EO87a5994b49fcS` 调用普通 order 时，Deepcoin 返回：

```text
sCode = 14
sMsg  = DuplicateAction
```

该订单没有创建。失败证据：

```text
/var/lib/telegram-kol-cutover-evidence/eth-rest-ws-tpsl-short-test-20260905/live-87a5994b49fc
```

去掉 clOrdId 后下单成功。因此当前设计不能依赖 clOrdId 或 tag 作为交易所
所有权证明。本地必须生成并持久化自己的幂等键，REST 成功回包中的 ordId
作为主订单身份。REST 详情可能把 clOrdId 显示成系统 ordId，也不能把它误认
为调用方提交的自定义标识。

## 推荐状态机

```text
intent_persisted
-> submit_reserved
-> rest_accepted
-> order_live
-> partially_filled / filled
-> position_bound
-> protection_bound
-> active
-> closing
-> terminal
```

建议新增持久化 WebSocket 收件箱。回调只保存原始事件并唤醒协调器，不直接
下单、改保护或平仓。至少保存：频道、action、OS、TU、交易所时间、接收时间、
原始 payload、payload hash 和处理状态。

只有同时满足以下条件才能建立权威绑定：

```text
Trade.OS == REST main ordId
REST 得到唯一且方向正确的 split posId
TriggerOrder.TU == REST posId
TriggerOrder.OS 是 TPSL 自己的 ordId
合约、方向、数量、TP、SL 一致
```

禁止用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领。

## 失败关闭与恢复规则

- REST 写入超时或响应不完整时记为 `unknown_exchange_outcome`，绝不自动重发。
- WebSocket 事件必须允许重复和乱序；旧状态不得覆盖新状态。
- WebSocket 断线不能解释为没有订单或没有仓位。
- 断线重连后通过 REST 重建订单、成交、持仓和 TPSL 快照。
- 无唯一 posId 或无 `TU == posId` 时，保护保持 unverified，禁止自动修改、
  撤销或认领。
- 若新入场依赖 WebSocket 获取 TPSL 关联，WebSocket 未订阅或处于 gap 状态时
  应暂停新入场。
- REST 是最终核验来源；WebSocket 是低延迟事件和 reconciliation 唤醒源。

## 系统停止、崩溃和 WebSocket 断线

WebSocket 断线不应被当作业务错误，也不能被解释成“没有变化”。运行状态至少
区分：

```text
connecting -> healthy -> disconnected -> resyncing -> healthy
```

计划停止时按以下顺序执行：

1. 停止接收新的入场意图。
2. 等待已经持有写租约的 REST 请求完成，或将其记为 unknown outcome。
3. 将已接收的 WebSocket 原始事件持久化完成。
4. 记录最后接收时间和本地处理水位。
5. 关闭 WebSocket 和进程。

重启时不能直接恢复交易，必须先：

1. 取得并验证单一 worker 写权威。
2. REST 查询活动普通订单、条件单、成交、当前仓位和必要的历史记录。
3. 重放本地未处理的持久化 WebSocket 事件。
4. 重新建立私有 WebSocket 并完成订阅。
5. 再做一次 REST 快照，覆盖“第一次快照到订阅成功”之间的竞态窗口。
6. 比较事件状态和 REST 状态，只允许状态前进。
7. 所有对象收敛后才把连接状态改回 healthy 并开放新入场。

不同停止时点的处理：

| 停止时点 | 恢复处理 |
| --- | --- |
| REST 下单前 | 本地 intent 可继续或由策略取消，没有交易所结果 |
| 请求发出但未收到响应 | 标记 unknown_exchange_outcome，禁止自动重发 |
| 已保存 main ordId，尚未收到 WS | REST 按 ordId 查询，再等待或重连 WS |
| 主单成交但尚未绑定 posId | REST 查成交和 split 仓位；绑定未完成前禁止自动管理 |
| TPSL 已推送但事件未处理 | 从持久化事件收件箱重放，不重新创建 TPSL |
| 停机期间成交 | 交易所附带 TPSL 继续在交易所生效；重启后 REST 补账并重新订阅 |

当前实验只证明正常连接下，成交后 `TriggerOrder.TU` 会从 `default` 变成真实
`posId`。尚未证明 Deepcoin 在断线重连后一定重放这条当前状态。因此如果停机
期间错过该事件，而 REST 又没有 TPSL 到 posId 的外键，系统必须保持
`protection_binding_unverified`，禁止按价格、时间或 ID 相邻认领，直到重新取得
精确证据或人工处理。

交易所侧附带止盈止损的价值之一，是 worker 停机时保护仍由 Deepcoin 执行。
系统恢复时不得因为本地没有收到事件而重新创建一套保护，否则可能产生重复
TPSL。先查询、核对和认领，确认确实缺失后才能走受控补挂流程。

## 推荐迁移顺序

1. worker 增加私有 WebSocket，只持久化原始事件，不取得交易权威。
2. 实现事件去重、乱序保护、心跳、断线状态和 REST 重同步。
3. WebSocket 事件只唤醒现有 REST reconciliation。
4. shadow 构建 `main ordId -> Trade.OS -> posId -> TriggerOrder.TU/OS`。
5. 与 REST 当前结果和保护账本逐笔比较。
6. 验证完成后，只迁移普通市价/限价入场到 order。
7. 真正的条件触发策略继续使用 trigger-order，并保留独立父子归属流程。
8. 最后才允许新绑定驱动 TPSL 修改、撤销和平仓。

## 生产改造前必须补测

- 普通限价多单。
- 部分成交和多次成交。
- 未成交撤销。
- 成交前断线、成交期间断线、成交后重连。
- 重复和乱序事件。
- 两张同方向、同数量、同价格订单并发。
- 手工订单与系统订单并存。
- 一仓多张部分 TPSL。
- TP 或 SL 触发后另一侧的状态。
- 修改 TPSL 后 OS/TU 是否稳定。
- REST 响应丢失但交易所实际接受时的恢复。
- 重连后 Deepcoin 是否重推当前 `TU=posId` 的 TPSL。

## 历史仓位提示

2026-09-05 19:26:43 UTC 的最后一次只读检查中，实验空仓仍存在且有 TP/SL：

```text
posId = 1001125145471184
size  = 0.1 contract
entry = 2478.78
TP    = 2468.78
SL    = 2488.78
```

这是历史时点证据。任何新会话开始工作前都必须重新查询实时状态，不能把它
当作当前持仓。

## 新会话建议开场指令

```text
请先阅读 AGENTS.md 和
docs/2026-09-05-deepcoin-rest-ws-trading-handoff.md。
基于其中真实实验结果，先检查当前 Git/运行时状态和实验仓位，再设计
Deepcoin REST+WebSocket 下单、成交、posId 和 TPSL 关联的生产改造方案。
不要直接部署或进行新的交易所写入；不要使用 symbol、方向、时间、ID相邻、
clOrdId 或 tag 作为归属证明。
```
