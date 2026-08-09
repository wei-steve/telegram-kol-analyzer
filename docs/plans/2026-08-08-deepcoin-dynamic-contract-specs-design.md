# Deepcoin 动态合约规格与平台能力交集设计

## 状态

2026-08-08 经操作者确认。本设计只规划修复，未启用任何新币种实盘交易。

## 问题

全局设置允许 `BTC`、`ETH` 和 `SOL`，而当前静态
`config/deepcoin_contract_specs.yaml` 只包含 BTC 和 ETH。SOL 信号虽能通过
全局 allowlist，但会在下单前因 `contract_size_unverified` 安全拒绝。
这防止了错误下单，但没有解决为所有允许币种提供合约规格的问题。

同时，全局允许列表是业务级策略，不能被某个交易平台的上币范围反向限制。
全局可以允许某个币种，但 Deepcoin 不支持时，Deepcoin 不得交易它。

## 已选方案

采用“Deepcoin 权威自动发现 + 严格验证的本地原子缓存”。

实际可新开仓币种必须是以下三个集合的交集：

```text
全局允许币种
∩ Deepcoin 当前支持且 state=live 的 USDT 永续合约
∩ 未过期且验证通过的 Deepcoin 合约规格
```

全局设置保存不会因 Deepcoin 不支持某币种而失败。不在交集中的币种
仅对 Deepcoin 标记为不可交易，不得创建交易所写入。

## 权威数据源

使用 Deepcoin 公开产品信息接口：

```text
GET /deepcoin/market/instruments?instType=SWAP
```

官方文档定义了执行所需的全部核心字段：`instId`、`ctVal`、`lotSz`、
`minSz`、`tickSz` 和 `state`。能力快照只接纳唯一的 `*-USDT-SWAP`
记录，但保留 live 和非 live 状态以便给出精确拒绝原因；只有
`state=live` 的合约才进入可开仓规格集合。规格数值必须可解析为有限正
Decimal，且 `minSz` 必须可按 `lotSz` 表示。重复合约、冲突数据、缺字段或非法值
使整次候选快照失败。

当前 `list_swap_symbols()` 使用行情接口，只能证明币种名和合约 ID，不能
提供数量及价格规格。实施后将由产品信息接口提供 Deepcoin 能力状态；
行情接口仍只用于价格。

## 缓存模型

默认运行时缓存路径为 `data/deepcoin_contract_specs_cache.json`。缓存是
非敏感的交易所元数据，但仍要遵循现有数据目录权限和部署边界。

快照包含：

- `schema_version`
- `venue`
- `source_path`
- `fetched_at`
- `expires_at`
- 规范化响应的 SHA-256 摘要
- 按 `instrument_id` 索引的合约状态和规格

刷新时先在内存中解析并完整验证，再写入同目录临时文件，`fsync`
后原子替换。刷新失败不得覆盖旧快照。默认 TTL 为 24 小时，可通过
显式配置调整。

静态 YAML 保留为人工对照和回滚资料，但不得在过期后隐式为新开仓提供
无时间界限的规格。既有仓位和订单使用它们创建时已冻结在 draft/binding
中的规格快照。

## 提供器与交集门禁

新的可刷新 provider 保持现有 `DeepcoinContractSpecProvider` 读取接口，并额外
暴露查询结果：

- `tradable`：全局允许，Deepcoin live，规格有效。
- `global_not_allowed`：Deepcoin 支持，但全局未允许。
- `venue_instrument_unsupported`：全局允许，Deepcoin 无该合约。
- `venue_instrument_not_live`：合约存在，但为 suspend/preopen/settlement。
- `contract_spec_missing`、`contract_spec_invalid`或 `contract_spec_stale`。
- `contract_spec_sync_unavailable`：刷新失败且没有未过期快照。

新开仓在任何草稿、确认、队列或交易所写入之前调用统一门禁。不可交易
结果必须终止在 pre-enqueue，不创建 `TradeSignal`、`ExecutionBinding` 或交易所
订单。原始信号和指令结果仍保留可审计的精确拒绝原因。

## 同步时机和设置语义

同步在三个时机运行：

1. 服务启动时在不阻断主服务的前提下刷新；失败时保留旧快照并发出状态警告。
2. 管理员打开或保存交易设置时尝试刷新。
3. 单进程定时任务按 TTL 的一半周期刷新，用锁防止并发写入。

设置保存始终保留用户请求的全局 allowlist。API 响应同时返回每个币种在
Deepcoin 的能力状态，并在界面上显示“全局允许，但 Deepcoin 不可交易”。
同步失败不回滚全局设置，但会使没有有效快照的 Deepcoin 新开仓安全拒绝。

## 已有仓位的风险处理

平台下架、暂停或缓存过期只禁止新增风险，不能阻止已有仓位减仓、止损或
平仓。风险减少路径优先使用持久化 draft/binding 中的规格快照，并继续执行
精确 `posId`、instrument 和 side 校验。如果缺少可证明的冻结规格，必须转入现有
安全修复/人工处置路径，不得猜测数量。

## 可观测性

交易设置 API 和页面对每个币种显示：

- 全局是否允许
- Deepcoin 是否存在及是否 live
- 规格是否有效和新鲜
- 最后成功同步时间
- 下次过期时间
- 最后同步错误（经脱敏、有长度上限）
- 最终 `tradable` 和精确原因码

运行时拒绝写入既有审计链。不包含凭据、签名、完整原始响应或无界错误文本。

## 安全与故障策略

- Deepcoin 超时、403、429、5xx、JSON/schema 错误：保留旧快照；无未过期快照时禁止新开仓。
- 部分响应或任一重复/冲突合约：不发布整份候选快照。
- 合约从 live 变为非 live：新开仓立即失去资格；减仓路径保留。
- 合约规格变化：新订单使用新版；已有订单/仓位保留冻结版本。
- 时钟异常、未来时间或无法解析的时间戳：缓存无效。
- 不支持币种：正常能力结果，不是交易所故障，但仍需记录业务拒绝。

## 测试策略

本地测试覆盖：

- 产品信息接口的成功解析、非 live 合约、非 USDT 合约、缺字段、非法 Decimal、重复和冲突。
- 缓存原子发布、失败不覆盖、摘要校验、TTL 边界、未来时间和损坏文件。
- 全局 allowlist 与 Deepcoin 能力的全部交集组合。
- 不支持/暂停/缺规格/过期币种在 pre-enqueue 拒绝，且零 TradeSignal、binding 和客户端写调用。
- BTC/ETH 现有行为不回归；SOL 在验证规格后使用正确张数、最小量和价格步长。
- 全局允许但 Deepcoin 不支持时仍能保存设置，页面显示不可交易原因。
- 下架/过期不阻止已有仓位减仓和平仓。

服务器验证必须使用真实 Deepcoin 公开产品接口，对 BTC、ETH 和 SOL 生成非敏感
快照，并对每个币种核对 instrument、ctVal、lotSz、minSz、tickSz 和 state。
不下单的 dry-run 必须证明 SOL 草稿已使用验证规格。

## 部署和回滚

1. 先以完全 dormant 模式部署客户端、缓存和状态页，旧静态 provider 仍为执行权威。
2. 在服务器运行一次只读同步，人工核对 BTC、ETH、SOL 快照和与静态 BTC/ETH 的差异。
3. 启用 shadow 交集门禁，记录新旧决策差异，不改变交易结果。
4. 新增风险为零且两次独立结果一致后，使 live provider 成为新开仓权威。
5. 先对 BTC/ETH 开启，确认无回归；再在单独安全窗口允许 SOL 未来新信号。

回滚通过一个明确模式开关恢复静态 provider，并禁止任何只存在于动态快照的
新币种开仓。回滚不删除缓存、审计记录、信号或绑定，不重放历史消息，也不影响
已有仓位的降风险处理。

## 非目标

- 不自动将 Deepcoin 所有币种加入全局 allowlist。
- 不为 Deepcoin 不支持的币种寻找其他平台。
- 不猜测、插值或复用其他币种的合约规格。
- 不重放历史上因缺规格而失败的 SOL 信号。
- 不改变现有识别、上下文解析、策略定位和精确仓位所有权规则。
