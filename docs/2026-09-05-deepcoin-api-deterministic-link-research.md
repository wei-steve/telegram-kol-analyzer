# Deepcoin 订单、成交、仓位确定性链接调研

调研日期：2026-09-05 UTC（本地 2026-09-04）。范围：官方文档、源码、生产 SQLite 只读查询、交易所 GET。风险级别 L0；没有下单、改单、撤单、保护设置、业务数据修改、配置修改、部署或重启。

## 结论：选择丙，但必须限定“没有提供”的范围

**对于当前“父触发单 → 成交子单 → 分仓 → 自动附带 TPSL”的完整归属链，本轮没有找到公开、受支持且可验证的确定性链接。按三选一，选择丙。** 这是当前公开契约和实际 REST 回包的工程结论，不是证明 Deepcoin 内部不存在关系，亦不是证明未实测的 WebSocket 永远不会增加字段。

核心判断拆成两部分：

- **“系统确实在推断”成立。** 当前父子候选匹配有价格、方向、数量和时间条件；TPSL 在没有明确仓位引用时也依赖候选筛选。fencing 不能全部删除，但也不能把筛选后的唯一候选叫作交易所直接返回的外键。
- **“现成公开接口中有一张表，只是我们没查”未获证实。** fills 已用于归属；GET 单笔详情确实没有接入，但实测并不增加父单或仓位字段。详情能够回读普通入场的 clOrdId，属于值得补齐的读取能力，不能解决自动附带止损归属。

特别排除两种误判：

1. `set-position-sltp` 的已知请求 `posId` 加成功响应 `ordId`，能建立本地确定性账本；系统已经使用这种方式。它不能回溯父触发单自动生成、没有经过这次本地请求的止损。
2. 官网内部数据存在 `PositionID` 的历史证据，见 [2026-07-25 核对记录](deepcoin-tpsl-live-verification-2026-07-25.md)。这是网页内部接口的旧研究，不是公开 REST/WS 已支持该关系的当前证明。本轮没有访问网页内部接口或建立 WS 会话。

**不建议直接取消附带止损。** 自定义 clOrdId 的原生 TPSL 创建契约本身尚不成立；即便改成保存服务端返回 ordId，也只解决第二段链接，触发成交到真实 posId 的发现仍可能失败。当前路径的无保护窗口没有可证明的最大值。

## 1. 证据范围与可复核性

生产 worker：`9501a5f39f0c5f196cc29f24f3e3b8786267126b`，PID `1525316`。`GET http://127.0.0.1:8002/api/runtime/deployment-identity` 于 `2026-09-05T05:47:47Z` 返回 `loaded_artifact_verified=true`。本地 HEAD：`f4c3b618d277e2881e4216f9dd13d4d8d85a87e9`。

已比对生产 release 与本地源码：deepcoin_client、execution_bindings、native_tpsl、trigger_protection_intents、trigger_protection_rescue_worker、web_app、production_safety_monitor、entry_protection_ledger_repair、deepcoin_order_matching、trigger_protection_assignment 内容一致。system_operator_bot 和 recovery_live_submit 整文件不同；本报告引用的通知配置、启用判断、两个投递函数及 `_record_submitted_order_legs` 已逐函数比对一致。因此不把本地 HEAD 当成生产版本。

官方网页直接访问出现 CloudFront 403；本机没有 `pwsh`，未安装工具或改环境，使用本地 curl 重试仍为 403。随后取得官方页面的搜索引擎索引正文；索引标注约两周至两个月前抓取，**文档不等同于今天服务端的全部实现**。交易所认证 API GET 从生产机器成功返回，补足实际字段证据。

服务端隔离证据目录：

```text
/var/lib/telegram-kol-cutover-evidence/9501a5f39f0c5f196cc29f24f3e3b8786267126b/api-link-readonly-20260905/
```

主要文件：`fills.json`、`history.json`、`pending.json`、`trigger-history.json`、`trigger-pending.json`、`positions.json`、`position-history.json`、`detail-*.json`、`history-*-id.json`、`database.json`、`runtime-notifications.json`、`latency-targets.json`、`latency-results.json`、`latency-fill-*.json`、`latency-stop-*.json`、`summary.json`。

结束身份回读时间 `2026-09-05T05:59:53Z`，release 与 loaded_artifact_verified 保持上述值。`manifest.json` 索引 45 个证据文件，自身 SHA-256：`3b4e1ab2b3e5de6f9088b3a35631acb339bb0930fa538715b751ae5e24da51d7`。`notification-config-presence.json` 仅记录配置存在性布尔值。

API 原始文件保存 GET 路径、观察时间、业务 code 和完整响应，不保存鉴权头。全部生产 SQLite 连接使用 `file:...research.db?mode=ro` 并执行 `PRAGMA query_only=ON`，实测为 1。远程诊断全部 `python3 -B`，仅用 stdlib，无生产模块导入、无 reconciler/adoption/rescue 调用。证据文件是唯一服务端文件产物；没有修改 release 或应用状态。

满 100 条的列表仅作为**字段样本**，不宣称是完整历史或完整账户快照；空数组只说明该次查询未命中。未重放任何历史消息。背景中的 6,392 次快照和无 binding 手动平仓事件未在本轮重新完整审计。

## 2. 问题一：clOrdId 是否能消除推断

| 路径 | 官方契约 | 实际证据 | 判断 |
| --- | --- | --- | --- |
| 普通 `POST /trade/order` | 请求支持 clOrdId，1–20 位；成功回执有 clOrdId | 149 条成功市价入场记录均提交并回传同值 | 可识别自己提交的普通单 |
| `POST /trade/trigger-order` 父单 | 文档回执列 clOrdId，但请求表未提供清晰的自定义 ID 支持承诺；附带 TP/SL 参数没有独立 ID | 411 条成功 trigger_limit 记录均提交 clOrdId，411 条回执均为空 | 提交字段不等于交易所接受并持久化 |
| 父 clOrdId 传播至成交子单 | 未声明传播 | 两个已知子单的详情 clOrdId 均等于各自 ordId；父 TKD… ID 查询未命中 | 实测反对“原值传播” |
| 附带 SL/TP 单独 clOrdId | 未声明 attachAlgoClOrdId、slClOrdId、tpClOrdId 等字段 | 原生 trigger pending/history 无 clOrdId 字段 | 不能作为生产方案 |
| `set-position-sltp` | 请求无 clOrdId；回执 ordId/sCode/sMsg | 已有成功请求 posId ↔ 响应 ordId | 支持自己的请求响应账本，不支持所设想的自定义 ID 查回 |
| `replace-order-sltp` | 请求仅 orderSysID、tpTriggerPx、slTriggerPx；成功 data 空对象 | 本轮不执行写调用；旧实测还存在接口适用对象限制 | 不能借此给已有保护单加 clOrdId |

来源：[普通单及详情](https://www.deepcoin.com/docs/DeepCoinTrade/order)、[触发单](https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrder)、[持仓 TPSL](https://www.deepcoin.com/docs/DeepCoinTrade/setPositionSlTp)、[replace TPSL](https://www.deepcoin.com/docs/DeepCoinTrade/replaceTPSL)。统计范围是当前数据库中 `purpose=entry, venue=deepcoin` 且成功 dict 回执的腿，不是订单全历史完整性证明。

具体实测：

- leg 579：父 `1001125120426454` 请求 `TKDBK4495E1`，回执 clOrdId 空；已记录子 `1001125123045253` 的 GET 详情 clOrdId 为 `1001125123045253`。
- leg 580：子 `1001125126414222` 的 GET 详情 clOrdId 为 `1001125126414222`，没有父 `TKDBK4495E2`。
- 用 `TKDBK4495E1` GET 详情返回 `code=0,data=[]`；这与两个子单的非传播事实相符，不能单凭空响应推出订单不存在。
- leg 583 普通市价单：GET 详情回读 `TKDBK4501E1`；对**同一个 ordId**，orders-history 的 clOrdId 是空，fills 也为空。因此存在**同字段、不同接口值完整性**差异，不能只比较字段名。

即使未来支持传播，单个 clOrdId 也必须有账户范围、唯一性及多次触发/重建规则。官方 GET 详情对重复 clOrdId 只返回最新订单，不能用复用 ID 证明唯一血缘。

## 3. 问题二：完整字段、代码消费和差集

下文路径省略 `/deepcoin`。字段名集合列出可取得的官方响应表；除注明外类型均为 string。通用外层为 `code,msg,data`。**client 只校验 data 是 dict 列表，原样传递每个对象，未做字段裁剪**，见 `deepcoin_client.py:57`。因此已接入的 GET，官方字段与 client 保留字段的差集是空；“是否使用”还须看下游归属层。

### 3.1 fills

官方完整响应字段（15）：

```text
instType instId tradeId ordId clOrdId billId tag fillPx fillSz
side posSide execType feeCcy fee ts
```

请求支持 `instType,instId,ordId,after,before,begin,end,limit`。分页锚点是 billId，最大 100。来源：[fills](https://www.deepcoin.com/docs/DeepCoinTrade/tradeFills)。

代码：`deepcoin_client.py:454,464` 分别提供按合约与按 ordId 读取；`execution_bindings.py:570` 将 fills 装入快照，`:3243` 的 `_snapshot_fill_evidence` 使用它生成 FillEvidence，**不是没有用于归属**。

归属投影读取 `ordId/clOrdId/instId/posSide/side/fillSz/fillPx/ts`，还兼容可选 `posId/pos_id/positionId` 和数量、价格、时间别名。官方字段中不进入此 FillEvidence 投影的是 `instType,tradeId,billId,tag,execType,feeCcy,fee`；它们不包含目标仓位或父触发单外键。tradeId/billId 可用于成交去重、补页，不应冒充 posId。

实测最新 100 行完全是上述 15 字段；clOrdId 全空，没有 posId、parentOrdId、triggerOrdId、PositionID、closePosId。后续按精确入场 ordId 查询也回读为空 clOrdId。**fills 连接 regular order → trade，缺少 trade → position 及 trigger parent → child 两条明确引用。**

### 3.2 单笔详情与普通订单历史

GET `/trade/order` 官方完整响应字段（37，以响应表和示例合并核对）：

```text
instType instId tgtCcy ccy ordId clOrdId tag px sz pnl ordType
side posSide tdMode accFillSz fillPx tradeId fillSz fillTime avgPx state lever
tpTriggerPx tpTriggerPxType tpOrdPx slTriggerPx slTriggerPxType slOrdPx
feeCcy fee rebateCcy source reduceOnly rebate category uTime cTime
```

GET `/trade/orders-history` 官方表为上列去掉 `reduceOnly`（36）；实测列表含 reduceOnly，合计同为 37。`source=13` 的官方语义是策略触发产生的限价单，**是来源类别，不是父单 ID**。来源：[单笔详情](https://www.deepcoin.com/docs/DeepCoinTrade/order)、[普通历史](https://www.deepcoin.com/docs/DeepCoinTrade/ordersHistory)。

代码实际上：

- `place_order` 在 `deepcoin_client.py:329` 只 POST `/trade/order`。
- `get_order_history_by_id`（`:479`）调用 list_order_history 后本地筛选；没有 GET `/trade/order`，也没有调用 `orderByID` / `finishOrderByID`。
- history 原样保留，归属主要消费 ordId、clOrdId、状态、成交量/价、方向、合约、时间；如果出现 posId 或 parentOrdId/triggerOrdId，已有别名分支会读取。
- `execution_bindings.py:2021` 先看显式父引用或 clOrdId；匿名候选才走 `_trigger_child_order_potentially_matches`。`:3307` 起仍使用“唯一子普通单 ID 对应 split posId”的已建立应用规则。它不是 API 响应中多出了一列 posId。

差集：详情官方字段均尚未从详情端点消费；相对已有历史对象，实测新增**字段名**为空。相同 ordId 的 `clOrdId` 值却有差异：市价详情恢复自定义 ID，触发子单详情恢复的是自身 ordId。其他目标链接字段两者都没有。

GET 详情对父触发单 ID 和未成交附带止损 ID 均未命中；它是普通订单查询，不能当作通用 trigger/TPSL 详情接口。官方另有 [orderByID](https://www.deepcoin.com/docs/DeepCoinTrade/orderByID)、[finishOrderByID](https://www.deepcoin.com/docs/DeepCoinTrade/finishOrderByID)，文档未声明父引用/posId；本轮没有额外调用。V2 [orders-detail](https://www.deepcoin.com/docs/zh/v2/DeepCoinTrade/orders-detail) 的文档也未提供所需完整链，本轮未 POST 探测。

值得单独修复但本轮未实现的范围：client/protocol 增加 get_order，精确 ID 详情优先、空响应视为暂不可见并有界重试；执行恢复和归属调用点接入；增加同 ID 列表 clOrdId 空而详情非空的回归。估计 2–4 个生产模块和相应测试，约 1–2 工程日，属于经验估算。**这不能宣称已经解决附带止损归属。**

### 3.3 其他已使用端点

| 接口 | 官方完整响应字段或明确缺口 | 当前 client / 归属消费 | 差集与含义 |
| --- | --- | --- | --- |
| GET `/trade/orders-pending` | 本轮 V1 独立页面 403，索引反复落入 V2，未取得可核实的 V1 完整表；不拿 V2 冒充 V1 | `:425` 原样列表；按订单 ID、方向、状态、价格数量关联 | 本次 0 行，无法从空样本核定对象字段；差集 unknown |
| GET `/trade/trigger-orders-history` | instType instId ordId px sz triggerPx triggerPxType ordType side posSide tdMode lever triggerTime uTime cTime errorCode errorMsg | `:509–556` 原样列表；父 ordId、触发/错误状态、合约方向、价格数量、时间；兼容 clOrdId | 实测额外 slPrice slTriggerPrice tpPrice tpTriggerPrice closeSLPrice closeSLTriggerPrice closeTPPrice closeTPTriggerPrice；无父子引用、posId、clOrdId |
| GET `/trade/trigger-orders-pending` | instType instId ordId triggerPx ordPx sz ordType side posSide tdMode triggerOrderType triggerPxType lever slPrice slTriggerPrice tpPrice tpTriggerPrice closeSLTriggerPrice closeTPTriggerPrice cTime uTime | `:494–507` 原样列表；native_tpsl 读取 TPSL 类型、ordId、合约方向、数量、时间、SL/TP，兼容 PositionID/posId | 实测额外 closeSLPrice closeTPPrice；3 行无目标链接字段 |
| GET `/account/positions` | instType mgnMode instId posId posSide pos avgPx lever liqPx useMargin unrealizedProfit lastPx tpTriggerPx slTriggerPx mrgPosition isLeading isFollow ccy cTime uTime；isLeading/isFollow 为 bool | `:380` 原样列表；posId 已作仓位主键，合约方向、仓型、数量价格时间作校验 | 实测 20 字段与上述一致；没有保护 ordId 列表；SL/TP 价格不能反推保护 ID |
| GET `/account/positions-history` | instType instId mgnMode mrgPosition posId posSide avgPx closeAvgPx pos closePos pnl fee fundingFee lever ccy cTime uTime | `:390` 原样列表，指定 split/posId/limit；用于仓位终态和经济对账 | 实测 17 字段一致；posId 已消费，但无入场父触发单或保护单引用 |
| POST `/trade/order` | ordId clOrdId tag sCode sMsg | `:329` 原样回执；执行层取 ordId/clOrdId、检查业务结果 | 无原生 posId/保护 ID；数据库顶层 posId 可能是本地补入 |
| POST `/trade/trigger-order` | ordId clOrdId tag sCode sMsg | `:332` 原样回执；保存父 ordId | 没有子 ID/posId/附带保护 ID |
| POST `/trade/set-position-sltp` | ordId sCode sMsg | `:335` 原样回执；请求 posId 加回执 ordId 后进行精确回读和账本记录 | 没有未消费的身份列；本地请求响应链接已经使用 |
| POST `/trade/replace-order-sltp` | 无业务字段，成功 data={} | `:371` 原样回执 | 没有可保存或设置的 clOrdId |
| POST `/trade/cancel-position-sltp` | ordId sCode sMsg | `:345` 请求白名单 instType/instId/ordId；原样回执 | 无仓位/父子身份列；本轮未调用 |

官方来源：[trigger history](https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrdersHistory)、[trigger pending](https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrdersPending)、[positions](https://www.deepcoin.com/docs/zh/DeepCoinAccount/accountPositions)、[positions history](https://www.deepcoin.com/docs/zh/DeepCoinAccount/accountPositionsHistory)、[cancel TPSL](https://www.deepcoin.com/docs/DeepCoinTrade/cancelPositionSlTp)。写端点字段仅研究文档和已有记录。

TPSL 创建完整请求字段为 `instType,instId,posSide,mrgPosition,tdMode,posId,tpTriggerPx,tpTriggerPxType,tpOrdPx,slTriggerPx,slTriggerPxType,slOrdPx,sz`，没有 clOrdId。replace 完整请求为 `orderSysID,tpTriggerPx,slTriggerPx`。触发单附带部分为 `tpTriggerPx,tpTriggerPxType,tpOrdPx,slTriggerPx,slTriggerPxType,slOrdPx`，没有独立保护 ID。普通下单的 clOrdId 能力不能移植为这些端点的承诺。

**原始证据与本地富化必须分开：** `recovery_live_submit._record_submitted_order_legs` 会把已知 pos_id 写进 stored_response 顶层。leg 583 的数据库 response_json 顶层 posId 因此不是下单原回包证明。`readback_evidence_json` 中 `source=persisted_primary_ledger` 也是账本投影，不能作为交易所首次返回 posId 的证据。`native_tpsl.normalize_native_tpsl` 已兼容 `PositionID`，本轮不存在“REST 拿到 PositionID 却被 normalize 丢掉”的证据。

## 4. 问题三：WebSocket 是否补足链接

前两问没有完整解，因此审查公开 WS 文档。真实频道是 `Order,Position,Trade,TriggerOrder`，不是照搬其他交易所的 orders/positions/fills JSON schema。

| 频道 | 官方字段名（括号内短键） | 对链接的贡献 |
| --- | --- | --- |
| Order | LocalID(L), InstrumentID(I), OrderPriceType(OPT), Direction(D), OffsetFlag(o), Price(P), Volume(V), OrderType(OT), IsCrossMargin(i), OrderSysID(OS), Leverage(l), OrderStatus(Or), VolumeTraded(v), InsertTime(IT), UpdateTime(U), UpdateMillTime(UM), Turnover(T), PosiDirection(p), TradePrice(t) | OS 是普通单 ID；没有声明 PositionID、父触发引用或 clOrdId。LocalID 未承诺等于客户端 ID，不能自作映射 |
| Trade | TradeID(TI), Direction(D), OrderSysID(OS), MemberID(M), AccountID(A), InstrumentID(I), OffsetFlag(o), Price(P), Volume(V), TradeTime(TT), MatchRole(m), ClearCurrency(CC), Fee(F), FeeCurrency(f), CloseProfit(CP), Turnover(T), Leverage(l), InsertTime(IT) | OS → TI 有确定性关系；没有 PositionID 或触发来源 ID |
| Position | MemberID(M), InstrumentID(I), PosiDirection(p), Position(Po), UseMargin(u), CloseProfit(CP), OpenPrice(OP), Leverage(l), AccountID(A), IsCrossMargin(i), UpdateTime(U) | 文档未列分仓 PositionID；账户和方向不足以区分多个 split 仓 |
| TriggerOrder | MemberID(M), TradeUnitID(TU), AccountID(A), InstrumentID(I), OrderPriceType(OPT), Direction(D), OffsetFlag(o), OrderType(OT), OrderSysID(OS), Leverage(l), SLPrice(SL), SLTriggerPrice(SLT), TPPrice(TP), TPTriggerPrice(TPT), TriggerOrderType(TO), TriggerPriceType(Tr), TriggerStatus(TS), InsertTime(IT), UpdateTime(U) | TU 被描述为 Position ID，但文档示例 TU 与 M/A 同值，旧官网研究存在独立 PositionID；不能认定 TU 就是 REST split posId |

来源：[Order](https://www.deepcoin.com/docs/privateWS/order)、[Trade](https://www.deepcoin.com/docs/privateWS/Trade)、[Position](https://www.deepcoin.com/docs/privateWS/Position)、[TriggerOrder](https://www.deepcoin.com/docs/privateWS/TriggerOrder)。表与示例还存在短键差异：Order/TriggerOrder 示例出现 O，表列 OPT；Trade/Position 示例出现未解释的 c。必须保留原始消息并做版本化解码，不能凭缩写猜字段。

**不选择乙：**目前没有公开 WS 中所需完整血缘的可靠契约；尤其 TU 的说明不足以证明 split 仓归属。本轮未获取/续期 listenKey，这些 GET 会创建或延长服务器会话状态，与本轮严格只读边界不符。故未实测的额外 WS 字段明确为 unknown。

若未来核实有 PositionID/父引用，接入需要：

- 认证 REST 获取 listenKey，连接 `wss://stream.deepcoin.com/v1/private?listenKey=...`，续期滑动一小时；只订阅所需 tables。见 [私有订阅](https://www.deepcoin.com/docs/privateWS/subscribe)。鉴权材料只留服务器，不落原始日志。
- 心跳、超时、指数退避重连、重订阅、重复事件去重；不得把连接存活当成流完整。
- 文档未给出私有流跨频道严格顺序、连续序号缺口检测或断线补播保证。交易/订单/仓位可能先后到达，必须允许乱序，按实体身份保存证据并对账；公共行情的 ResumeNo 不能套用于私有流。
- WS 用于更快发现，REST 用于存续/终态核对。若关键链接仅 WS 有，断线期间漏掉的归属不能靠无该字段的 REST 自动补齐，仍需交易所提供可查历史链接。
- 预计 1–2 周完成可验收的持久化观察、断线/重复/乱序恢复与对账测试（经验估算，不含供应商确认）。应先 shadow；涉及 schema 和交易授权接管再按相应 L2/L3 单独实施。本轮没有实现。

## 5. 取消附带止损后的可行性与实测时延

### 5.1 “自己的 clOrdId 挂止损”尚无受支持接口

原生 TPSL 的 set-position-sltp 不声明 clOrdId；通用 trigger-order 的现有请求 clOrdId 没有可靠回传。普通 `/trade/order` 支持 clOrdId 不代表它能创建所需的分仓原生止损。把 Conditional 当 TPSL、或在收到成交后才发市价平仓，都改变了执行语义。

目前可讨论的替代是：取得准确 posId 后，调用原生 set-position-sltp，保存已知请求 posId 与成功回执 ordId，再精确读回。这能避免“认领自动附带 SL”，但无法消除前面的“父触发单找到子单/posId”。请求发出但响应丢失时，也不能靠一个未受支持的 clOrdId 安全查回和重发。

### 5.2 生产样本：市价成交到原生止损创建

选择最近 12 条具有 `entry_protection_response` 主止损账本的市价入场（按 leg ID 降序，2026-08-20 至 09-05）；按精确 entry ordId 读取 fills，按账本 stop ordId 查询原生 trigger history，当前未触发 stop 使用 pending。只使用双方精确 ID 命中的记录。

```text
t_fill_first = min(fills.ts of exact entry ordId)
t_stop_create = exact native TPSL.cTime
Δ = t_stop_create - t_fill_first
```

10 位秒转毫秒，13 位毫秒保留。本批可用回包都是整秒精度；不能把 0 秒差解释为没有裸奔窗口。

| 指标 | 结果 |
| --- | --- |
| 抽样 / 可用 / 缺证据 | 12 / 9 / 3 |
| 缺证据腿 | 545、540、530；不作为 0 延迟 |
| 可用范围 | 2026-08-21 至 2026-09-05 |
| 0 个整秒差 / 1 个整秒差 | 5 / 4 |
| 中位 / 均值 / 最大观测差 | 0 / 0.444 / 1 秒（整秒时间戳口径） |

这是**成功市价同步路径的创建时延**，有幸存者偏差，既不证明挂单回读已完成，也不是触发路径 P95/P99 或 SLA。若采用同一秒截断解释，1 秒差对应真实间隔约 0–2 秒；文档没有精度保证，不能承诺这个范围。

### 5.3 触发成交还多出发现、归属和调度

两笔最新成功触发样本，以子普通单 GET 详情 fillTime 到 intent 最后 adopted 更新时间计算：leg 579 为 **6.957 秒**，leg 580 为 **4.072 秒**。这是归属认领成功落库的端到端代理，**不是后挂止损实测**，两笔不构成延迟分布。后挂方案还需要新的提交和回读。

发现一个不能忽略的时间测量问题：ledger.first_seen_at/ownership audit.created_at 使用循环传入时间，可能早于该轮后续观察到的成交。leg 579 用它减 fillTime 得到 **-0.584 秒**；所以本报告没有把该字段当作真实保护完成时间。position_mutation_intents 的 reserved/submitted/confirmed 在样本中也复用同一时间，不能用这些列算网络耗时。

当前 reconcile 默认每轮完成后 sleep 30 秒（`web_app.py:9469`），另有管理路径触发 reconciliation。若仅等周期循环，成交发现可能增加一轮等待及执行耗时；不能承诺“最多 30 秒”。一般表达为：

```text
裸奔窗口 = 成交到被发现 + 父子/仓位归属耗时 + 调度等待 + SL 请求到交易所创建
```

任何一项归属失败、服务离线或请求结果未知都可能使窗口无界。**当前证据不足以接受直接去掉附带 SL 的风险。** 建议先修复恢复/告警闭环，并向交易所确认受支持的 TPSL→PositionID、parent→child 查询能力；若将来仍选择后挂，须先有可测的保护截止时间、部分成交处理、幂等未知恢复和所有者认可的超时处置，不能靠当前 9 个成功样本放行。

## 6. 为什么 fail-closed 变成永久停滞

### 6.1 实际现状与原问题有两处差异

当前 intent 总数 **183**：adopted 72、failed 19、pending 79、resolved 12、retrying 1。`last_reason_code=trigger_protection_candidate_predates_fill` 共 7 条：

| intent | entry leg | 当前恢复状态 | attempts | leg 当前状态 | 对应 Runtime Incident |
| --- | --- | --- | --- | --- | --- |
| 125 | 499 | failed / manual_review | 5 | manually_closed | 326，delivered |
| 131 | 509 | failed / manual_review | 5 | manually_closed | 358，delivered |
| 135 | 514 | failed / manual_review | 5 | closed | 365，delivered |
| 148 | 537 | failed / manual_review | 5 | manually_closed | 405，delivered |
| 163 | 559 | failed / manual_review | 5 | manually_closed | 2014，delivered |
| 166 | 563 | retrying / NULL | 4 | manually_closed | 2053，delivered |
| 177 | 577 | failed / manual_review | 5 | manually_closed | 2055，delivered |

因此不是“七条均五次终结”，也不是“系统从未告警”。七条都有 Runtime Incident `notification_status=delivered` 和 notified_at；这是系统记录的投递成功，不等于用户已阅读或处理。incident.status 仍为 pending，**告警投递与问题恢复没有闭环**。这些腿当前均为本地终态，不应为恢复旧 intent 而重新挂保护或重开仓；本轮未将本地终态扩大为全部历史交易所终态复核。

旧通道确实积压：七条各有一条 protection_adoption_refused audit，均 notification_status=pending；关联 8 条 backup_stop_blocked protection incident 也均 pending、notified_at=NULL。当前 split worker 的 `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空，token 非空；system bot 的 token/chat ID 均有值。

`web_app.py:5053` 将 notification_bot_config 传给 reconcile，`:9496` 按 `system_operator_bot_enabled` 判断；配置函数要求 token 与 chat_id 同时有值。因此当前旧告警通道被跳过，而另一条 Runtime Incident 通道已有 delivered 记录。不能用当前 env 反推全部历史时点的配置，但它能解释当前积压未消化。

### 6.2 状态机把“不准执行”与“不再观察”绑在一起

1. `execution_bindings.py:2596`：重试最多 5 次；未耗尽时按 5、10、20、40 分钟退避，达到上限进入 failed。
2. `trigger_protection_intents.py:130`：failed 且原 disposition 为空时自动变成 manual_review。
3. `execution_bindings.py:2156`：常规认领仅选择 pending/retrying。failed 即使 next_attempt_at 留着旧值也不再进入；“到期”不代表会被调度。
4. `trigger_protection_rescue_worker.py:65`：救援虽接受 failed，但 disposition 只允许 NULL/retry/exact_backup，**manual_review 明确被排除**。还存在按 ID 升序前 20 条、无轮转游标的饥饿风险。
5. owner 本身还必须是 verified、active 且有 posId。166 已 manually_closed，即使 retrying 也不再满足 owner 条件。原有终结收敛器只处理特定 retrying/wait/snapshot_incomplete，不能概括覆盖所有历史失败。
6. `entry_protection_ledger_repair.py:1587` 对候选创建时间早于仓位创建时间直接拒绝；若所依据的时间和对象不改变，重试五次也不会产生新信息。应核实时间精度和因果语义，不能直接放宽阈值消除报错。

这解释的是当前机制，不臆测作者动机：**安全门禁有必要，停止收集新证据不是安全门禁的必然要求。**

### 6.3 改进方向，本轮不实现

- 分离 execution authority 与 recovery observation：manual_review 继续禁止交易写入，但保留低频、只读、有截止/公平调度的复核；出现新的确定性证据再经相同身份门禁恢复。不能定时无条件重置 attempts。
- 区分临时不可见/网络失败、永久身份冲突、数据精度问题、结果未知。对同一不可变拒绝原因做退避与去重；证据变化后允许重新评估。结果未知先查回，禁止盲目重复挂单。
- 活仓和已闭仓分开收敛。已确认终态的腿将相关 intent 终结为明确原因；尚未查实交易所终态时保留 unknown。不要让 historical failed 冒充待救活仓。
- 将“未归属但交易所可见 SL”和“确认没有 SL”分开告警。备用 SL/TP 的执行前提继续保持，不拿候选推断绕过原生归属检查。
- 告警建立独立可重试 outbox：缺配置立即暴露健康异常；pending 监控年龄；failed 投递可重试；delivering 有租约恢复；收到确认/完成修复才结案。当前两个旧投递函数仅取 pending，发送异常转 failed 后不再选取，也没有此处的 delivering 租约回收。
- Runtime Incident 的 delivered 应能关联到具体 binding/leg/posId、保护是否有效、下一次复核时间、人工接管人；长期未处理需升级提醒。生产已有该通道，应复用并打通，不再另造一套孤立告警。
- 验收应覆盖：五次失败后新证据到达恢复、临时空历史、重启时 delivering 残留、通知缺配置、已闭仓清理、队尾饥饿；无任何测试可绕过精确仓位授权。

## 7. 本轮明确未能证明的内容

- V1 orders-pending 官方完整对象表未取得；本次空响应不能填补它。
- 私有 WS 是否含未文档化的 PositionID/父单字段未实测；不能给出乙的上线承诺。
- 不支持通过未经许可的写探测判断未知参数是否被服务器忽略；自定义 TPSL clOrdId 结论是“无可依赖契约”，不是实测服务器对该参数报错。
- 后挂触发止损的真实尾部时延和最大裸奔窗口未知；成功市价样本不能替代这一指标。

可向 Deepcoin 提交的最小问题是：提供受支持的 `trigger ordId → generated regular ordId(s)` 与 `TPSL ordId → split PositionID` 查询/历史补查契约；说明 trigger clOrdId 是否被忽略、生成子单 LocalID/clOrdId 的定义，以及 WS 的 TU 与 PositionID 是否不同。取得答复前，保留现有保护和身份门禁，优先解决失败之后仍能观察、告警、终结和恢复的闭环。
