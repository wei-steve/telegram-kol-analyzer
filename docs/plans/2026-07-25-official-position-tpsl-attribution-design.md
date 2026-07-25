# 官网式持仓与 TPSL 归属设计

## 目标

让 Web 持仓页采用 DeepCoin 官方页面的核心模型：一个真实仓位对象拥有一个
可变长度的 `tpsl_orders` 列表。页面展示全部止盈和止损明细，同时增加本项目的
群组、策略记录和归属证据。任何缺少精确证据的订单都不能被塞进某个仓位卡。

## 已验证事实

- 官网网页内部批量接口同时读取 `position` 与 `trigger_order`。
- 两类数据都包含 `PositionID`，前端统一转换为 `positionId`。
- 官网用 `positionId` 直接过滤 TPSL；双向持仓模式增加合约、账户、保证金模式、
  持仓方向、`tradeUnitId` 和反向平仓方向条件。
- 公开 V1/V2 pending API 的当前实盘响应不包含 `posId`。
- 本项目创建分仓 TPSL 时已经知道请求 `posId`，响应提供 `ordId`，因此未来订单
  可以在创建时保存精确关系。

## 方案比较

### 方案 A：调用官网网页内部接口

优点是能直接取得 `PositionID`，最接近官网当前实现。缺点是接口未公开，依赖
网页登录令牌、网页签名和可能频繁变化的前端契约；服务端 API IP 白名单与网页登录
身份也不是同一套权限。该方案只用于研究和人工诊断，不进入生产。

### 方案 B：依赖私有 WebSocket 补全 `PositionID`

官方前端能处理 `TriggerOrder.PositionID`，理论上实时通道可能提供该字段。当前公开
文档只说明 `TradeUnitID`，本次 API listen-key 只读订阅也未收到初始快照，尚未形成
可验证契约。该方案保留为实验性增强，必须在服务器实盘拿到字段样本并完成断线、
重连和快照一致性测试后才能启用，而且不能成为修改保护单的唯一证据。

### 方案 C：创建时持久化 `ordId ↔ posId`，读取时按订单 ID 回连

这是选定方案。所有本系统发起的 `set-position-sltp` 已知精确 `posId`。在交易所
回读确认订单存在后，把 `ordId`、`posId`、用途、触发价、数量、entry leg 和策略
归属写入现有保护账本。持仓页再把公开 pending 列表按 `ordId` 与账本连接。

该方案不依赖未公开接口，能覆盖未来所有系统订单；代价是旧人工订单或历史上未保存
回执的订单不能自动补齐，只能保持未归属或通过已有请求/响应审计证据做一次性回填。

## 数据模型

页面读取模型为：

```text
position
├── pos_id
├── instrument_id
├── account_id / margin_mode / position_side
├── strategy_attribution
│   ├── group
│   ├── strategy_record_id
│   ├── entry_leg_id
│   └── evidence_state
└── tpsl_orders[]
    ├── order_id
    ├── pos_id
    ├── kind: take_profit | stop_loss
    ├── trigger_price
    ├── size
    ├── created_time
    ├── association_source
    └── association_state
```

现有 `PositionProtectionLedger` 作为通用订单到仓位账本；
`PositionTakeProfitOrder` 继续保存分批止盈生命周期；
`PositionBackupStopOrder` 继续保存第二止损执行生命周期。无需新增数据库表。

## 关联优先级

1. 交易所行直接带 `PositionID`、`positionId`、`posId` 或 `closePosId`，且指向当前
   实时仓位：`exchange_position_id`。
2. pending 行的 `ordId/algoId` 命中状态为 verified/active 的本地保护账本，并且
   entry leg 与当前实时 `posId` 的权威归属仍有效：`persisted_order_position`。
3. 同一个订单 ID 出现互相冲突的本地归属：`conflict`，不进入任何仓位。
4. 没有精确键：`unattributed`，仅在全局“未归属交易所保护单”出现一次。

价格、数量、方向和时间只能用于核对已知关联是否一致，不能单独提升为精确归属。
`sz=0` 的全仓 TPSL 尤其不能在多个同币种、同方向分仓之间猜测。

## 页面设计

保留官网式“一个仓位一张卡”的阅读顺序：

1. 合约、方向、杠杆和持仓状态；
2. 独立的“策略归属”栏，显示群组、策略记录、entry leg、`posId` 和证据状态；
3. 数量、开仓均价、未实现盈亏等仓位指标；
4. “止盈止损(n)”可展开列表，每条显示类型、触发价、数量、委托时间、订单 ID 和
   关联来源；
5. 无精确归属的订单在所有仓位卡之后集中显示，不计入任何卡片的 `n`。

旧的“主止损、第二止损、止盈”摘要可以暂时保留给风险判断和兼容测试，但页面主视觉
改用完整列表，避免把任意数量的 TPSL 压缩成三个标量。

## 数据流

```text
DeepCoin positions ────────┐
                           ├─ exact position join ─ position cards
DeepCoin pending TPSL ─────┤
                           │
local protection ledgers ──┘

unresolved/conflicting TPSL ── global unattributed section
```

所有关联函数保持纯只读。展示层不得改变订单修改、撤销和保护健康检查的安全边界。

## 写入一致性

每条 `set-position-sltp` 路径必须遵循：

1. 提交前持久化意图或保留现有事务性预留；
2. 请求必须携带精确 `posId`；
3. 从响应提取 `ordId`；
4. 用 pending API 按 `ordId`、类型、触发价、数量和市场单语义回读；
5. 回读成功后统一 upsert `PositionProtectionLedger`；
6. 专用 TP/第二止损表按各自生命周期更新；
7. 任何提交结果不明、缺少订单 ID、回读失败或本地冲突都冻结，不标记 verified。

## 旧数据修复

只允许从以下证据回填：

- 交易所行直接携带的 position ID；
- 已保存的请求 JSON 中明确的 `posId`，同时响应或回读给出唯一 `ordId`；
- 已验证 entry leg 与专用 TP/第二止损记录共同给出的同一 `ordId/posId`。

回填工具默认 dry-run，输出计划、冲突和指纹。apply 阶段要求精确指纹，且只写本地账本，
不创建、撤销或修改交易所订单。无法证明的现存订单保留为未归属。

## 错误处理

- 某个合约 pending 读取失败：该合约显示“证据暂不可用”，不把旧缓存当成当前状态。
- 同一 `ordId` 对应多个 `posId`：从卡片移除并记录冲突。
- 账本指向已关闭仓位：不迁移到同币种新仓，显示为 stale evidence。
- 交易所 TPSL 数量与官网计数不同：阻止发布验收，先核查分页、类型过滤和快照完整性。
- WebSocket 后续增强断线：回退到 REST + 本地账本，不影响订单安全状态。

## 测试与验收

- 单仓多 TP、多 SL 和组合 TPSL 全部保留为独立显示行；
- 交易所直接 `PositionID` 与本地 `ordId ↔ posId` 得到相同关联结果；
- 两个同币种同方向分仓不会共享 `sz=0` TPSL；
- 未归属订单只出现一次；
- 策略归属与 TPSL 关联状态同时可见；
- 页面每张仓位卡的 `止盈止损(n)` 与官网对应卡片一致；
- 全部现有保护修改安全测试继续通过；
- 服务器实盘验证只读完成后才能部署。
