# DeepCoin 原生止盈止损核对记录（2026-07-25）

本记录对应服务器主账户的只读核对。它不包含 API 凭据、用户身份信息或可用于交易的密钥。

## 目的

确认项目当前应认定为“持仓附带止盈止损”的订单，是否与 DeepCoin 官方持仓页面一致；同时明确旧的通用条件单不得被误认为持仓保护单。

## 数据来源与方法

1. 从服务器使用项目当前主账户凭据调用 `GET /deepcoin/trade/trigger-orders-pending`，读取 BTC 与 ETH 的待触发订单。
2. 按交易所字段 `triggerOrderType` 分类，不以本地数据库状态或 API 写入成功回执替代交易所当前状态。
3. 在已登录的 DeepCoin 官方持仓页面逐笔读取“止盈止损(n)”。
4. 仅比较原生 `TPSL` 与官方持仓卡的保护单计数；`Conditional` 单独保留并不纳入该比较。

## 实盘快照结果

| 项目 | 数量 | 结论 |
| --- | ---: | --- |
| 待触发订单总数 | 73 | 包含两种不同的订单类型 |
| `triggerOrderType=TPSL` | 45 | 原生持仓止盈/止损 |
| `triggerOrderType=Conditional` | 28 | 独立通用条件委托，非持仓卡保护单 |
| 官方持仓卡“止盈止损(n)”合计 | 45 | 与原生 TPSL 精确一致 |
| pending 响应中返回 `posId` 的订单 | 0 | DeepCoin 当前回包未返回该字段 |

官方网页逐笔计数如下：

| 开仓均价 | 官方“止盈止损”数量 |
| ---: | ---: |
| BTC 63,894.1 | 3 |
| BTC 63,895.7 | 3 |
| BTC 63,748.9 | 4 |
| BTC 63,792.4 | 5 |
| BTC 63,890.7 | 5 |
| BTC 63,900.0 | 3 |
| BTC 64,000.0 | 4 |
| BTC 64,200.0 | 4 |
| BTC 64,300.0 | 4 |
| BTC 64,625.0 | 0（明确不操作） |
| BTC 64,797.0 | 4 |
| BTC 65,000.0 | 4 |
| ETH 1,886.72 | 2 |

## 类型边界

`TPSL` 由 `POST /deepcoin/trade/set-position-sltp` 为已有持仓设置。项目在分仓模式下提交 `instType`、`instId`、`posSide`、`mrgPosition=split`、`tdMode` 和目标 `posId`。它会显示在官方持仓卡的“止盈止损(n)”中。

`Conditional` 由 `POST /deepcoin/trade/trigger-order` 创建，是独立的条件委托。即使它带有触发价或关闭相关字段，也不会计入官方持仓卡的“止盈止损(n)”。现存的 28 条是历史遗留订单；它们不能被当作本项目的持仓保护，也不能因为本次核对而被批量撤销。

## 对当前系统实现的审查

审查日期：2026-07-25。相关离线回归：`140 passed`。

- `native_tpsl.normalize_native_tpsl()` 只接受 `triggerOrderType=TPSL`。
- `deepcoin_order_matching.extract_pending_protection_orders()` 优先使用该原生归一化；当前 `Conditional` 值不会落入兼容分支。
- Web 持仓保护读取 `_load_deepcoin_pending_tpsl_orders()` 同样显式过滤为 `TPSL`，所以旧条件单不会显示成“第二止损 active”。
- 第二止损执行器在写入前验证原主止损，并在写入后验证原主止损和新止损都仍存在；任一读回失败会落为 `pending_readback`，不会标记 `active`。
- 原生 pending 回包不返回 `posId`，而全仓位止损常返回 `sz=0`。因此系统不得仅凭币种、方向或价格猜测归属；没有已保存 `ordId` 的精确证据，或无法在完整持仓范围内唯一归属时，应停止并人工复核。

## 结论与操作规则

本次实盘状态证明：官方页面显示的 45 条持仓止盈止损，与 API 中 45 条原生 `TPSL` 完全一致。项目当前针对 `TPSL`/`Conditional` 的类型隔离是正确的。

但这不是 API 能逐条回传 `posId` 的证明：它当前没有回传。因此，未来的创建、调整或撤销必须持续保存并使用 `ordId ↔ posId` 的本地证据；对没有精确归属证据的原生 TPSL 或任何 `Conditional`，禁止批量撤销、替换或自动标记为已保护。

## 官方网页代码研究

研究日期：2026-07-25。研究对象是当时 DeepCoin 官方合约页面加载的构建文件
`4288.98c10ee1433f7b3c.js`。构建哈希可能随官网发布变化，因此本节记录的是
字段契约与算法，不把文件名当作长期 API。

### 官网使用的数据入口

官网不是使用公开的 `GET /deepcoin/trade/trigger-orders-pending` 构造持仓卡。
它向网页内部接口 `POST /v2/public/query/swap/send-batch` 提交 `Actions`，
一次读取：

- `account`
- `position`
- `order`
- `trigger_order`

每个 Action 都使用 `Method=GET`、`ExchangeID=DeepCoin`，并携带
`ProductGroup`、分页及账户范围。这个网页内部接口返回的仓位和触发单保留
PascalCase 原始字段，其中包括 `PositionID`。

### 字段标准化

官网将 `position` 行标准化为内部仓位对象，关键映射为：

| 交易所原字段 | 官网内部字段 |
| --- | --- |
| `PositionID` | `positionId` |
| `TradeUnitID` | `tradeUnitId` |
| `InstrumentID` | `instrumentId` |
| `AccountID` | `accountId` |
| `IsCrossMargin` | `isCrossMargin` |
| `PosiDirection` | `posiDirection` |

官网将 `trigger_order` 行标准化为内部订单对象时使用相同的
`PositionID → positionId` 映射，并另外保留 `OrderSysID → orderSysId`、
`BusinessType`、方向、触发价、数量及 TP/SL 字段。`BusinessType=X` 被归类为
`STOP_LOSS_TAKE_PROFIT`。

### 精确关联算法

官网构造每个仓位的 `tpslList` 时，不按价格、数量、创建时间或数组顺序猜测。
普通模式的核心条件是：

```text
order.positionId == position.positionId
and order.instrumentId == position.instrumentId
and order.pcType == STOP_LOSS_TAKE_PROFIT
```

双向持仓模式进一步要求：

```text
order.isCrossMargin == position.isCrossMargin
and order.posiDirection == position.posiDirection
and order.accountId == position.accountId
and order.tradeUnitId == position.tradeUnitId
and order.direction != position.direction
and order.shown
```

官网还会从仓位委托展示集合中排除“TPSL 但没有 `positionId`”的订单。实时更新
沿用同一模型：`TriggerOrder` 推送更新订单集合，`Position` 推送更新仓位集合，
随后重新执行 `positionId` 关联。

### 与公开 API 的差异

截至本次实盘检查：

- V1 `trigger-orders-pending` 不返回 `posId`；
- V2 `orders-algo-pending` 也不返回 `posId`；
- 官网网页内部 `send-batch` 的 `trigger_order` 数据包含 `PositionID`；
- 官网前端同时识别 `PositionID` 与 `TradeUnitID`，二者不是同一个字段；
- 私有 WebSocket 文档把 `TradeUnitID` 描述为 Position ID，但当前官网代码仍以
  独立的 `PositionID` 作为 TPSL 与仓位的主关联键。

因此不能把 `TradeUnitID` 直接替代 `PositionID`，也不能仅凭公开 REST 的缺字段
响应复现官网逐仓关联。

### 工程结论

官网代码证明交易所内部确实保留 `OrderSysID ↔ PositionID` 关系。生产系统的
可靠实现应在提交 `set-position-sltp` 时利用已知请求 `posId` 和响应 `ordId`
持久化该关系，再用公开 pending API 的 `ordId` 做实时存在性核对。

网页内部 `send-batch` 依赖官网登录态、网页签名和未公开契约，只能作为研究与
人工诊断证据，不应成为生产交易或持仓页面的依赖。没有交易所 `PositionID`
或本地持久化 `ordId ↔ posId` 的旧订单继续进入“未归属保护单”，不得自动猜测。

## 分阶段保护单与账本结论

补充审查日期：2026-07-25。

系统的触发入场已经具备正确的两阶段基础：

- 提交触发入场前创建 `TriggerProtectionIntent`；
- DeepCoin 接受入场委托后保存父委托 `ordId`；
- 入场成交并归属到唯一 entry leg 后取得真实 `posId`；
- 随入场委托附带的主止损在成交后从交易所快照中认领；
- 多段止盈通过 `TriggerTakeProfitConvergence` 等待真实仓位，再逐条提交；
- 第二止损通过专用执行器在主止损验证后创建。

因此，入场委托本身不存在 ID 缺失问题。成交前可使用父委托 `ordId/clOrdId`
关联保护意图；暂时不存在的是仓位 `posId` 和成交后生成的保护子单 `ordId`。
这两个值只能在成交和交易所回读后补齐，不能提前猜测。

现有审计记录仍有结构性缺口：

- `TriggerProtectionIntent` 是父委托级恢复记录，不是每个 TP/SL 一条记录；
- 多段止盈计划以集合 JSON 保存，提交前没有逐腿生命周期；
- `PositionProtectionLedger` 要求真实 `posId` 和 `order_id`，不能表达成交前计划；
- `PositionTakeProfitOrder` 与 `PositionBackupStopOrder` 是专用执行记录，不能单独
  充当所有保护单的统一审计账本。

后续采用两层账本：

1. 每一个主止损、第二止损和分段止盈都有独立逻辑保护腿，从 `planned` 开始记录；
2. 取得并回读验证真实保护单 `ordId` 后，再写入
   `PositionProtectionLedger(ordId ↔ posId)`。

第二止损和多段止盈只有在 entry leg 已成交、归属 verified、取得唯一 `posId`，
并且随入场委托附带的主止损已经认领和验证后才允许提交。任一步出现不确定结果，
对应逻辑保护腿进入等待或未知状态，不得标记为交易所已生效。
