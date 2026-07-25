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
