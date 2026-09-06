# Deepcoin REST + WebSocket 交易改造：设计总述

日期：2026-09-06。设计基础：`docs/2026-09-05-deepcoin-rest-ws-trading-handoff.md`
（下称"交接文档"）。进度真相：`docs/rest-ws-trading-status.md`。

本文件是设计总述，**不是执行清单**。执行会话只读状态文件指向的那一份阶段文件。

## 1. 要解决的问题

系统现在无法确定性地回答"这张止盈止损属于哪个仓位"。原因是入场限价腿走
`POST /deepcoin/trade/trigger-order`，多出"父触发单 → 子普通单 → posId"一层，
而 Deepcoin 公开 REST 不提供这一层的外键：

- 父 clOrdId 不传播到子单（411 条 trigger 提交回执 clOrdId 全为空）。
- `trigger-orders-history?ordId=<主单ID>` 返回空数组；该参数是条件单自身 ID 过滤器，
  不是主单反查键。
- `trigger-orders-pending` 的 TPSL 行没有 `posId` / `PositionID` / `parentOrdId`。

结果是保护归属只能靠候选筛选（方向、数量、价格、时间），fail-closed 一旦触发就
永久停滞：当前 183 条 intent 里 79 pending、19 failed，7 条
`trigger_protection_candidate_predates_fill` 全部落到 `manual_review`。

2026-09-05 的 ETH 实验证明存在一条**精确链路**，条件是入场改用普通 `order`
并在下单前订阅私有 WebSocket：

```text
REST /order 回执 main ordId
  -> WS Trade.OS == main ordId
  -> REST positions 唯一且方向正确的 split posId
  -> WS TriggerOrder.TU == 该 posId
  -> WS TriggerOrder.OS == TPSL 自己的 ordId
```

## 2. 两句判断（沿用交接文档，本仓库不改）

1. **WebSocket 解决时序，不解决身份。** 真正创造标识符的是"普通 order 取代
   trigger-order"。普通 order 是地基，WebSocket 是放大器。
2. **WebSocket 事件不得成为真相来源。** 推送到达 → 触发针对性 REST 核验 →
   核验通过才写账本。

## 3. 本仓库落地时与交接文档的差异

交接文档是在只读调研视角下写的，没有读本仓库的执行代码。以下七处必须按本仓库现状调整。

### 3.1 市价入场已经在走普通 order，迁移范围比交接文档小

交接文档"推荐迁移顺序"第 6 步写"只迁移普通市价/限价入场到 order"。
实际 `recovery_live_submit.py:1364` 的 `market` 分支早已用
`build_deepcoin_market_order_payload` + `place_order`，即
`POST /deepcoin/trade/order`。**只有 `limit` 分支走 trigger-order。**

因此阶段 5 的写入语义变更范围收窄为：限价入场腿从 trigger-order 改为 order；
市价腿不改接口，只改为由新绑定链驱动其保护归属。这降低了阶段 5 的风险，
但也意味着**市价腿今天就已经承受"普通 order 附带保护的归属问题"**，
不能把它当成"已经安全的对照组"。

### 3.2 clOrdId 的结论要收窄，否则会得出错误的迁移方案

交接文档说"去掉 clOrdId 后下单成功，因此当前设计不能依赖 clOrdId"。
这个结论正确，但它的**成因**被写得过宽了：

- 生产市价入场一直提交 clOrdId 并成功（链接调研：149 条成功市价入场均提交并回传同值）。
- 实验里被 `DuplicateAction` 拒绝的是 **`ordType=limit` + `clOrdId` + `tpTriggerPx`/`slTriggerPx`**
  的组合，单笔、去掉并发因素后仍然复现；只删 clOrdId 就被接受。

所以判重键与 `ordType` 或附带 TPSL 参数有关，不是"该账户拒绝一切 clOrdId"。
**直接影响：** 现成的 `build_deepcoin_place_order_payload` 无条件写入
`clOrdId`，把它原样用于限价迁移大概率复现 `DuplicateAction`。
阶段 5 必须先做单变量受控实验确定可接受的字段组合，不能凭现有 builder 直接切换。

### 3.3 不新建平行状态机，接到已有的四层账本上

交接文档给了一条推荐状态机
（`intent_persisted → submit_reserved → rest_accepted → order_live → partially_filled/filled
→ position_bound → protection_bound → active → closing → terminal`）。

本仓库已有四层账本承担这些语义：`execution_bindings`（binding/leg）、
`position_protection_ledger`、`trigger_protection_intents`、
`position_take_profit_orders`，外加 `position_mutation_gateway` 的意图与回读校验。

**决定：不新建平行状态机。** 交接文档的状态名只作为阶段 4 影子链的内部阶段标签，
写在影子表的一列里，用于和现有账本逐笔比对。阶段 5/6 才把新链接到既有账本上，
且是"新链驱动既有账本"，不是"新链替换既有账本"。

理由：并行两套权威状态机是本项目历史上代价最高的错误模式；
`docs/ARCHITECTURE.md` 第 6 节已明确"迁移只改变在哪里跑、怎么组织，从不改变决定什么"。

### 3.4 三进程拓扑与凭据隔离是硬约束，交接文档没有覆盖

交接文档只说"连接必须活在 worker 内"。本仓库的具体约束更强：

- worker / ingest / web 是三个 systemd 进程，Deepcoin 密钥只在 worker，
  Telegram session 只在 ingest。
- web 角色**没有执行权限**，任何交易所写入必须经 `worker_command_jobs` 的四条命令
  （`sync_deepcoin_execution` / `close_bound_position` / `recovery_live_submit` /
  `process_next_trade_signal`），其他 `command_type` fail-closed。
- 任何 `asyncio.Lock` 只在自己进程内有效，跨进程排他必须走数据库状态。

因此 WS 收件箱、协调器、reconciliation 全部落在 worker 进程内；
web 只能读收件箱做展示，不能消费。

### 3.5 `websockets` 不是项目依赖

`pyproject.toml` 没有它，本机与服务器的运行 venv 都没装。实验是在证据目录的
独立 venv（python3.11 + websockets 16.0）里跑的。阶段 1 的第一件事是加依赖，
并且要用 `websockets.asyncio.client` 而不是实验脚本里的 `websockets.sync.client`
——后者是阻塞式的，会卡住 worker 的事件循环。

### 3.6 私有 WS 的 Position 推送带未文档化的 `PI`（= split posId）

交接文档只提了 `TriggerOrder.TU`。复核证据发现 `PushPosition` 的
`data.PI = "1001125145471184"`，等于 REST 的 split posId。公开文档的 Position
字段表没有列出该字段。

这不改变"TU == posId 是绑定条件"的设计，但**多了一条独立证据源**：
`Position.PI` 可以在成交瞬间就给出 posId，不必等 REST positions 轮询。
按未文档化字段处理：解码层做存在性检查，缺失时降级到 REST，不得假定其永远存在。

### 3.7 WS 与 REST 的合约标识格式不同

WS 是 `ETHUSDT`，REST 是 `ETH-USDT-SWAP`。跨源比对前必须归一化。
现有 `deepcoin_normalization.py` 处理的是 REST 侧格式，阶段 2 要补 WS 侧映射，
且必须是**显式映射表**而非字符串拼接推断——推断在新合约上线时会静默出错。

### 3.8 交接文档的"历史仓位提示"已失效

实验空仓 `1001125145471184` 现已不在交易所（只读核实，见状态文件第 1 条）。
但交易所上有三张历史 pending 条件入场单，且当前正在全局否决止盈收敛
（状态文件第 2 条）。这不属于本改造范围，但**阶段 3 与阶段 4 的比对基线必须
把它们算进去**，否则会把"被否决"误读为"没有止盈需求"。

## 4. 目标架构与各部件关系

```text
                      ┌──────────────── worker 进程（唯一持 Deepcoin 凭据）────────────────┐
                      │                                                                    │
  Deepcoin            │   ┌──────────────────┐                                             │
  私有 WS  ═══════════╪══▶│ WS 采集任务       │  只做三件事：                                │
  (Order/Trade/       │   │ deepcoin_private_ws│  1 落原始事件到收件箱                       │
   Position/          │   │ 单例任务，阶段 1   │  2 维护连接状态机                            │
   TriggerOrder)      │   └────────┬─────────┘  3 唤醒协调器                                │
                      │            │ 写                    ✗ 不下单 ✗ 不改保护 ✗ 不平仓      │
                      │            ▼                                                       │
                      │   ┌──────────────────────────────┐                                 │
                      │   │ WS 事件收件箱（新表，阶段 1）  │  channel/action/OS/TU/PI/       │
                      │   │ deepcoin_ws_events           │  exch_time/recv_time/raw/hash/   │
                      │   └────────┬─────────────────────┘  processed_state                 │
                      │            │ 读（去重、乱序保护、水位）阶段 2                          │
                      │            ▼                                                       │
                      │   ┌──────────────────┐    唤醒     ┌──────────────────────────────┐ │
                      │   │ 协调器            │────阶段 3──▶│ 现有 deepcoin_reconcile 循环  │ │
                      │   │ 只判断"该核验谁"   │            │ 30s 周期 → 事件到达时立刻跑一轮│ │
                      │   └────────┬─────────┘            └──────────┬───────────────────┘ │
                      │            │ 阶段 4                          │ REST 精确核验         │
                      │            ▼                                 ▼                     │
                      │   ┌──────────────────────────────┐  ┌──────────────────────────┐   │
                      │   │ 影子绑定链（新表，阶段 4）      │  │ 既有四层账本              │   │
                      │   │ ordId→Trade.OS→posId→TU/OS   │  │ execution_bindings        │   │
                      │   │ 只写不驱动，逐笔比对产出差异报告│──│ position_protection_ledger│   │
                      │   └────────┬─────────────────────┘  │ trigger_protection_intents│   │
                      │            │ 阶段 5/6 才反向驱动     │ position_take_profit_orders│  │
                      │            ▼                        └──────────┬───────────────┘   │
                      │   ┌──────────────────────────────┐             │                   │
                      │   │ recovery_live_submit          │◀────────────┘                   │
                      │   │ 入场：market→order（已是）      │  写交易所的唯一入口              │
                      │   │      limit→order（阶段 5）     │                                │
                      │   │ 保护：set-position-sltp（阶段6）│                               │
                      │   └──────────────┬───────────────┘                                 │
                      └──────────────────┼─────────────────────────────────────────────────┘
                                         │ REST 写（超时=unknown_exchange_outcome，绝不重发）
                                         ▼
                                    Deepcoin REST
```

三条不可越过的边界：

- **WS → 收件箱**：回调只写库，不做业务判断。
- **收件箱 → 协调器**：协调器只决定"去核验哪个对象"，不写账本。
- **REST 核验 → 账本**：只有 REST 精确核验通过才写账本。这是全系统唯一的真相入口。

## 5. 阶段划分与风险等级

风险等级按 `AGENTS.md` 的 L0–L3。

| 阶段 | 文件 | 内容 | 等级 | 定级理由 |
|---|---|---|---|---|
| 1 | `phase-1-ws-inbox.md` | worker 内私有 WS 采集，只落原始事件；新表 + 新依赖 | **L3** | schema 变更，需生产库副本演练；行为面本身是 L1 的附加只读采集 |
| 2 | `phase-2-dedup-and-resync.md` | 去重、乱序保护、心跳、断线状态机、REST 重同步 | **L2** | durable consumer 与恢复路径；若引入新列则该提交按 L3 处理 |
| 3 | `phase-3-wake-reconciliation.md` | WS 事件只唤醒既有 REST reconciliation | **L2** | 改变既有权威循环的调度节奏，可影响执行路径 |
| 4 | `phase-4-shadow-binding.md` | 影子构建绑定链并与保护账本逐笔比对 | **L3** | 新影子表；行为面是 L1 的纯影子，无权威接管、无交易所写入 |
| 5 | `phase-5-order-entry-cutover.md` | 限价入场 trigger-order → order，新绑定驱动，仅限新入场 | **L3** | 改变交易所写入语义 |
| 6 | `phase-6-protection-authority.md` | 新绑定驱动 TPSL 修改、撤销与平仓 | **L3** | 改变交易所写入语义，且直接决定止损是否挂上 |

阶段 7（交接文档第 7 步"真正的条件触发策略继续用 trigger-order 并保留独立父子归属流程"）
不单独立阶段：它是**保持现状**，写进阶段 5 的"禁止"里——阶段 5 不得把真正带突破/回落
触发条件的策略腿改成普通 order。

## 6. "生产改造前必须补测"12 项的分配

交接文档列了 12 项。分配如下，无遗漏、无重复：

| # | 补测项 | 阶段 | 怎么测 |
|---|---|---|---|
| 1 | 普通限价多单 | 5 | 前置受控实验（用户单独批准的最小实盘） |
| 2 | 部分成交和多次成交 | 5 | 前置受控实验 |
| 3 | 未成交撤销 | 5 | 前置受控实验 |
| 4 | 成交前断线、成交期间断线、成交后重连 | 2 | 注入式断线 + 生产真实流观察 |
| 5 | 重复和乱序事件 | 2 | 离线合成重放（确定性）+ 生产真实流去重指标 |
| 6 | 两张同方向、同数量、同价格订单并发 | 5 | 前置受控实验（DuplicateAction 根因） |
| 7 | 手工订单与系统订单并存 | 3 | 唤醒 reconciliation 后不得误判手工单；用现存三张 pending 条件单作真实并存基线 |
| 8 | 一仓多张部分 TPSL | 4 | 影子比对，用生产现有 `set-position-sltp` 产生的多张 TPSL |
| 9 | TP 或 SL 触发后另一侧的状态 | 4 | 影子比对生产真实触发事件 |
| 10 | 修改 TPSL 后 OS/TU 是否稳定 | 6 | 改写语义阶段的核心验证项 |
| 11 | REST 响应丢失但交易所实际接受时的恢复 | 5 | 前置受控实验（注入客户端侧超时） |
| 12 | 重连后 Deepcoin 是否重推当前 `TU=posId` 的 TPSL | 4 | 有活仓时主动断连重连，看是否重推；这是断线期漏事件能否自愈的判据 |

阶段 2 拿到的是"连接层性质"，阶段 4 拿到的是"绑定链性质"，阶段 5 拿到的是
"新写入路径性质"，阶段 6 拿到的是"改写稳定性"。第 12 项放阶段 4 而不是阶段 2，
是因为它需要一个带 `TU=posId` 的活 TPSL，而那要等影子链能识别出这类对象。

## 7. 三个最不确定的技术点

按对整体方案成立与否的影响排序。

1. **`DuplicateAction` 的判重键未知。** 生产市价单带 clOrdId 成功、实验限价单带
   clOrdId 被拒，同一账户同一时期。如果判重键是"同 instId + 同方向 + 同数量 +
   短时间窗"，那么阶段 5 的限价迁移会在正常交易中随机被拒，而且是
   **外层 code=0 的软拒绝**，最容易被误判为成功。阶段 5 的前置实验必须先把这个
   判清楚，否则整个迁移不成立。
2. **断线期间错过 `TU: default → posId` 那一次推送后能否自愈。** 交接文档明确
   "尚未证明 Deepcoin 断线重连后一定重放当前状态"，而 REST 侧没有 TPSL → posId 外键。
   如果不重放，那么每一次断线都会产生一批永久 `protection_binding_unverified`
   的仓位，只能人工处理。这决定了阶段 2 的"暂停新入场"策略要收得多紧。
3. **`Position.PI` 与 `TriggerOrder.TU` 是不是同一个 ID 空间。** 单个样本里两者
   都等于 `1001125145471184`，但公开文档把 `TU` 描述为 TradeUnitID、示例里 TU 与
   MemberID/AccountID 同值，而旧官网研究显示网页内部有独立 `PositionID`。
   如果在多仓、部分平仓或跨保证金模式下三者分叉，阶段 4 的绑定条件就要重写。
   一个样本证明不了 ID 空间等价。

## 8. 与既有生产缺陷的边界

以下三项**不属于**本改造范围，阶段文件里只作为观测基线出现，不得顺手修：

- 止盈收敛被 `convergence_pending_alias_conflict` 全局否决（状态文件第 2 条，已定位到
  `native_tpsl.protection_order_sides_consistent` 被用在入场条件单行上）。
- 5 条 `authoritative_execution_attempts.status='uncertain'` 无对账闭环
  （id 6/25/77/191/199）。
- binding 337 与腿 579/580 仍为 `active` 但其 posId 在交易所已不存在。

顺手修会把 L1/L2 的阶段偷偷升成 L3，破坏"一个阶段一个风险面"的划分。
需要修就单独立项、单独批准。
