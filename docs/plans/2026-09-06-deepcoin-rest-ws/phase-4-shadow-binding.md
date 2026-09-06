# 阶段 4：影子构建绑定链并与现有保护账本逐笔比对

风险等级：**L3**（新增影子表）。行为面是纯影子：不取得任何权威、
不驱动任何决定、不做任何交易所写入。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

**领取前必须取得用户对本阶段的单独批准**（schema 变更）。

## 目标

用阶段 1–3 的事件流，在影子表里构建交接文档那条精确链路：

```text
REST 返回的 main ordId
  -> WS Trade.OS
  -> REST 验证的唯一 split posId
  -> WS TriggerOrder.TU
  -> WS TriggerOrder.OS（即 TPSL 自己的 ordId）
```

然后把影子链的结论与现有保护账本（`position_protection_ledger`、
`trigger_protection_intents`、`position_take_profit_orders`）**逐笔比对**，
产出差异报告。

这一阶段结束时，我们应该知道：新链路在真实生产流量下，
比现有推断式归属**多认出多少、少认出多少、认错多少**。
没有这个数字，阶段 5/6 的切换就是盲切。

## 前置

- 阶段 3 已 `completed`，唤醒式 reconcile 稳定，等价性已验证。
- 手上有阶段 2 的重复率/乱序基线与阶段 3 的 reconcile 次数分布。
- 准备好生产数据库副本用于 schema 演练（L3 要求）。
- 读 `docs/rest-ws-trading-status.md` 阶段 0 结论第 4、8 条
  （现有账本模块分工、`Position.PI` 与合约名格式差异）。

## 任务

### 1. 新表：影子绑定与差异

两张表，都随 `init_db` 建。

**`deepcoin_shadow_bindings`**：一行代表一次"入场 → 仓位 → 保护"的影子链尝试。

关键列：`venue`、`main_ord_id`、`instrument_rest`（归一化后）、`side`、
`stage`（用交接文档的状态名做标签：`rest_accepted` / `order_live` /
`partially_filled` / `filled` / `position_bound` / `protection_bound` /
`active` / `closing` / `terminal`）、`pos_id`、`protection_ord_id`、
`trade_os_seen_at`、`tu_matched_at`、`binding_confidence`
（`exact` / `unverified`）、`refusal_reason`、
`observed_execution_binding_id`（现有账本里对应的 binding，可为 NULL）、
`evidence_json`、`first_seen_at`、`last_seen_at`。

**`deepcoin_shadow_diffs`**：一行代表一处影子链与现有账本的差异。
关键列：`shadow_binding_id`、`diff_kind`、`subject`（binding/leg/posId/ordId）、
`shadow_value`、`ledger_value`、`detected_at`、`evidence_json`。

`diff_kind` 至少覆盖：
`shadow_only`（影子认出、账本没有）、`ledger_only`（账本有、影子没认出）、
`pos_id_mismatch`、`protection_ord_id_mismatch`、`side_mismatch`、
`size_mismatch`、`price_mismatch`、`timing_only`（结论相同但发现时间不同）。

`timing_only` 单独成类很重要：它是本改造的收益指标，不是缺陷。

### 2. 绑定判据：五个条件同时成立才算 `exact`

严格照交接文档，一个都不能少、一个都不能替：

```text
Trade.OS == REST main ordId
REST 得到唯一且方向正确的 split posId
TriggerOrder.TU == REST posId
TriggerOrder.OS 是 TPSL 自己的 ordId
合约、方向、数量、TP、SL 一致
```

不满足 → `binding_confidence='unverified'` + 具体 `refusal_reason`，**不猜**。

`Position.PI`（阶段 0 结论第 8 条发现的未文档化字段）可以作为**补充证据**
写进 `evidence_json`，用于在 `TU` 尚未变成真实 posId 时更早知道 posId 是什么。
但它**不能**替代上面五条中的任何一条，也不能单独用来认领。
解码层要做存在性检查，缺失时正常降级到 REST。

合约标识比对前必须用阶段 2 的显式映射表归一化（WS `ETHUSDT` ↔ REST `ETH-USDT-SWAP`）。

### 3. 差异报告

一个只读端点 + 一个可从 `cli.py` 调用的 dry-run 导出器：

`GET /api/runtime/deepcoin-shadow-binding-report`（localhost only），返回汇总计数：
按 `diff_kind` 的条数、`exact` 与 `unverified` 的比例、
`timing_only` 的中位提前量（秒）、`shadow_only` 与 `ledger_only` 的明细计数。

导出器把明细写到服务器证据文件，端点只返回计数。原始行不进 HTTP 响应。

### 4. 补测项 8、9、12 的观测装置

- **第 8 项（一仓多张部分 TPSL）**：生产现在用 `set-position-sltp` 会对同一 posId
  产生多张保护单。影子链要能同时挂多张 `protection_ord_id`（一对多），
  比对时按集合比对而不是按单值比对。
- **第 9 项（TP 或 SL 触发后另一侧的状态）**：捕捉 `TriggerOrder` 的
  `TS` 变化与随后的 `Position` 变化，记录"一侧触发后另一侧在 WS 与 REST 里
  各自变成什么"。这一项是纯观测，不需要构造。
- **第 12 项（重连后是否重推 `TU=posId` 的 TPSL）**：在**确实存在带
  `TU=posId` 的活 TPSL** 时，主动断开 WS 再重连，记录重连后 60 秒内是否收到
  该 TPSL 的推送。
  - 只有 `binding_confidence='exact'` 的影子链才具备这个前提。
  - 若整个观察窗口都没有出现这样的对象，**如实记录"前提未出现，未能验证"**，
    不要为了凑这一项去下单。
  - 结论无论正负都要写进证据：这是决定阶段 2 的"暂停新入场"策略松紧的关键输入。

## 禁止

- 禁止让影子表驱动任何决定。现有账本、reconcile、保护路径的行为必须完全不变。
- 禁止用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领。
  尤其禁止利用"TPSL ordId = 入场 ordId − 1"这个已观测到的分配模式。
- 禁止把 `Position.PI` 当作五条判据的替代品。
- 禁止对不满足五条判据的对象产生 `exact` 结论。
- 禁止任何交易所写入。第 12 项的断连是断本地连接，不是对交易所做任何操作。
- 禁止修改现有账本表的任何行。影子表只写自己。
- 禁止为了凑补测项去下单。
- 禁止顺手修 `convergence_pending_alias_conflict` 或其他既有缺陷。
- 禁止用 `git add -A`。

## 验证等级与具体检查项

等级 **L3**（新表）。行为面 L1。

### schema（L3 强制）

- [ ] 生产数据库副本上跑 `init_db`，两张新表建成、既有表未变更。
- [ ] 副本 `PRAGMA quick_check` 通过。
- [ ] `execution_bindings`、`position_protection_ledger`、
      `trigger_protection_intents`、`position_take_profit_orders`、`raw_messages`
      的 before/after 行数完全相同。
- [ ] 回滚方案：`tg-deploy <pre-deploy-sha>`，影子表保留不删。

### 补测项（交接文档 12 项中的第 8、9、12 项）

- [ ] **第 8 项**：一对多保护单的影子链在离线用例与生产真实数据上都正确成集合。
- [ ] **第 9 项**：至少记录一次真实的 TP 或 SL 触发，写明另一侧在 WS 与 REST
      里的状态；若窗口内未触发，如实记录"未发生"。
- [ ] **第 12 项**：按上面的前提执行；结论三选一并如实记录
      （重推 / 不重推 / 前提未出现无法验证）。

### 测试

- [ ] focused：五条判据的每一条单独不满足时都必须落到 `unverified` +
      正确 `refusal_reason`（五个独立用例，不要合成一个）。
- [ ] 合约名归一化缺失时 fail-closed。
- [ ] 一对多保护单的集合比对。
- [ ] 差异分类：八种 `diff_kind` 各至少一个用例。
- [ ] 影子写入完全不触碰既有账本（用静态或运行时断言守护）。
- [ ] 最终候选跑一次全量套件（记录已知既有失败）。

### 生产观察

- [ ] 观察 30 分钟，覆盖至少 5 条真实消息、尽量 2 个群；
      不足则停止、留 `in_progress`、记录流量不足。
- [ ] **差异报告是本阶段的主产出**，必须给出具体数字：
      `exact` / `unverified` 各多少、八种 `diff_kind` 各多少、
      `timing_only` 的中位提前量。
- [ ] `shadow_only` 与 `ledger_only` 的每一条都要有归因，
      不能只给计数。这两类直接决定阶段 5/6 能不能开始。
- [ ] 直接查交易所历史：本阶段零新增写入。
- [ ] 既有账本行数在窗口前后的变化必须全部由正常交易解释。

## 完成条件

1. 上面全部检查项通过，或流量不足/前提未出现已如实记录且阶段留 `in_progress`。
2. 差异报告已产出并写进证据；`shadow_only` 与 `ledger_only` 每条都有归因。
3. 提交已推送并 `tg-deploy` 部署，回滚 SHA 已记录。
4. 更新 `docs/rest-ws-trading-status.md`：推进到阶段 5
   （`phase-5-order-entry-cutover.md`），证据区追加一行，
   并在证据区**明确写出差异报告的关键数字**——阶段 5 的批准会以它为依据。
5. 发消息给 `brain_session_id`，摘要必须包含差异报告数字与第 12 项结论。

## 汇报格式

```text
阶段 4 完成 / 阻塞 / 流量不足留 in_progress
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>
schema 演练：副本路径、quick_check、五张关键表 before/after 行数
差异报告：
  exact / unverified：N / N
  shadow_only：N（逐条归因）
  ledger_only：N（逐条归因）
  pos_id_mismatch / protection_ord_id_mismatch / side / size / price：N / N / N / N / N
  timing_only：N，中位提前量 N 秒
补测项 8：
补测项 9：
补测项 12：重推 / 不重推 / 前提未出现无法验证
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟），真实消息数、覆盖群数
交易所写入：零
对阶段 5 的建议：可以开始 / 需先补什么
异常与遗留：
证据路径（服务器）：
```
