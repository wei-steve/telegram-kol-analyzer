# Trigger Protection Lineage Attribution 本地实现证据

## 结论

已按已确认设计完成本地实现、测试和独立 fail-open 评审。新路径只在完整证明“提交证言 + owner 前置基线 + parent → child → `posId` 成交血缘 + 账户级双向唯一”时才获得新归属权威。无法证明、证据冲突、别名冲突、多解、快照不完整或所有权冲突都保持拒绝。

独立评审结论为 **P0=0、P1=0、P2=0，未发现 fail-open 路径**。本地实现基线的全量测试为 **7148 passed、4 skipped、0 failed**；同步 monitor 身份收敛的 4 个远程提交后，合并态全量测试为 **7170 passed、4 skipped、0 failed**。

## 边界与候选身份

- 工作区：`/Users/steven/Documents/telegram获取消息`
- 分支：`codex/phase0-deploy-integration`
- 实现基线 HEAD：`8c8e88390c0248b8b0368905a5363a1432c9f17c`
- 全量测试时的集成同步基线 HEAD：`3e29de96a62fe92d1bf1cfc3919446ec6b2156c0`
- 最终提交基线 HEAD：`18409847a7c2ed2221e61e04206d19693775a7d3`；该后续提交只为 `docs/known-issues-and-deferred-work.md` 新增 monitor 迁移待办，未改变 `src/tests` 树。
- 最终 `src + tests` 未提交 diff SHA-256：`015071bd9a0cd321f036d9438b20f9e36abe271e27fc33ec297e7c8ad5d4fa00`
- 最终 `src` 未提交 diff SHA-256：`15aaa8773e4fb82137b2265d524b03d6c8384476dc372d1b6efc757c1babaf2c`
- 指纹口径说明：上述 `015071...` 由 `git diff -- src tests` 生成，因而不包含当时未跟踪的 `tests/fixtures/trigger_protection_lineage_cases.json`。暂存后包含该 fixture 的完整 `src + tests` patch SHA-256 为 `1109768ae474f807ecf8e732d0260c5c8df415e13acca425bc035a426c051589`；排除 fixture 后仍精确为 `015071...`，fixture 文件 SHA-256 为 `6b312273e08e25f83eb7ecb44bf269b2309cf81b73168f9d586b7367434bd123`。因此指纹差异来自新文件进入 Git diff 的口径变化，不是远程同步改写。
- 本地实现冻结时未提交、未暂存、未推送；所有者随后已单独授权将该冻结候选同步至 `3e29de96` 并提交、推送。
- 本轮未部署、未重启、未修改 schema、未修改生产数据、未调用交易所写接口、未处置 7 条历史记录。

## 实现对账

### 1. 提交证言与 owner 基线

- 保存并读取完整 raw pending-TPSL 响应；只有明确成功、无未翻页、无跨合约污染的快照才能作为基线或当前候选集。
- 验证 intent、entry leg、parent event、binding submitted order 的身份和请求指纹一致，且 parent 响应只有一个成功对象。
- `attached_on_trigger_order=true` 只是提交证言，不是 child order ID。保护请求必须为精确 stop-only 形状，任何 TP 字段或额外保护语义都拒绝。

### 2. parent → child → `posId` 成交血缘

- child 只来自 regular-order history 中自身唯一的 order-ID aliases；`triggerOrdId` / `triggerOrderId` 只作为 parent aliases。
- parent 与 child 必须在合约、方向、价格、精确交易所 `cTime` 和明确 filled/partially-filled 证据上一致；child 的 `posId` 必须唯一对应当前活仓。
- `filled` 与非零错误码同时出现时视为证据冲突，竞争 child 不得被过滤掉。
- 时间只作序列合理性门：候选不得早于 durable intent，不得晚于快照，且匿名候选的 `cTime` 必须与唯一 child 精确相等。不存在时间容差或“最近一张”选择。

### 3. 账户级双向唯一与 owner 模型修正

- 归属仍使用账户级二分图分配；一个 owner 只有一个 candidate，一个 candidate 也只有一个 owner 时才能 finalize。
- **owner 数据模型影响范围：** potential-owner universe 现在包含所有带活跃 `posId` 的 trigger-entry legs，即使其没有 intent、有重复 intent 或 attribution conflict，也必须作为阻断 owner；只有具备单一、身份一致 intent 的 owner 才能成为可分配 owner。
- 这项修正不改变既有正确归属：既有 direct/legacy 权威和 watermark 前 intent 保持原语义；过去被拒绝的对象只有同时命中完整新血缘证据且通过双向唯一时才改为接受。
- 即使 candidate 带直接 `posId`，仍必须通过当前活仓、合约、方向、保护价格/数量、快照完整性、已有账本 owner 冲突和 order-ID 全局唯一性检查。

### 4. finalize、保护收敛与管理解锁

- finalize 在同一事务内重新验证 binding、intent、logical leg、ledger owner 及候选 order ID；冲突时整个归属不落库。
- 归属后的 backup stop 使用持久 reservation/status、稳定幂等键和精确写后回读；TP 每一档写前都重新读取完整 positions/TPSL/DB ownership，部分已存在时只补缺失档位。
- 已有未知 mutation、未归属可能保护单、数量漂移、alias 冲突或快照不完整时不发生新写入。
- `protection_missing_cancellable_order_id` 只在“verified ledger + 当前完整 TPSL 快照中唯一同 ID 订单 + 精确仓位/合约/方向/价量”同时成立时才解除。只有 ledger 或只有交易所 row 仍然阻断。

### 5. 激活与可观测性

- 新设置 `trigger_protection_lineage_attribution_mode=disabled|shadow|live`，默认 `disabled`。`live` 没有有限、已审核的 `trigger_protection_lineage_activation_after_intent_id` 时有效模式仍为 disabled。
- 只有 `mode=live` 且 `intent.id > watermark` 才可产生新归属权威；shadow 只记录规划，watermark 前不重放。
- 首次“交易所可见止损但所有权未验证”会立即产生有界、去重的 durable runtime incident，通过现有主动通知机制推送；不需要等到第 5 次恢复或 manual review。
- incident 明确区分 `native_stop_visible_ownership_unverified`、`live_position_stop_absent`、`ownership_conflict`、`ownership_recovered`，并与 Web 数据的阻断/恢复状态一致。不存储 raw exchange payload 或凭据。

## RED → GREEN 证据

- 两起生产形状 fixture 固化了“原生止损 `cTime` 与 child 成交单 `cTime` 精确相同，但早于 live-position 投影时间”的旧拒绝；新分配用例在实现前 RED，实现后 lifecycle `1050` / `1072` 通过新血缘证据而不再因早于 position projection 被拒绝，成功 control 保持原结论。
- 设计要求的负向用例已覆盖：同形状竞争订单、不完整快照、缺失 parent 证言、child → `posId` 非唯一、`cTime` 不精确相等、账户级双向唯一不成立、direct `posId` 证据冲突。
- 独立评审最后两个关键 RED/GREEN：
  - `posSide=short + side=sell` / `long + buy` 合法同义组合：修正前 2 failed；修正后连同真冲突用例 8 passed。
  - valid child + 同形状同时间 `{state=filled,errorCode=203}` competitor：修正前伪唯一 RED；修正后两条 child 均保留并阻断，相关 4 passed，`execution_bindings` 全文件 198 passed。

## 验证结果

- 两条直接受影响执行路径：`288 passed`。
- 归属/修复/backup/TP/管理/监控核心聚焦集：`1014 passed`。
- 其他改动模块回归：`674 passed`。
- 旧测试桩完整 raw TPSL 契约校正后：`331 passed`。该校正只为 fake client 增加 `code=0 + data` 的只读 raw response，没有放宽生产完整性门。
- 独立评审者在审核指纹上单独运行 8 个关键文件：`981 passed`。
- 实现基线全量 pytest：`7148 passed, 4 skipped, 32 warnings in 473.24s`。
- 同步 `30b0ece3`、`eaf542d1`、`6a493d15`、`3e29de96` 后的合并态全量 pytest：`7170 passed, 4 skipped, 32 warnings in 483.66s`。
- 全量测试后远程新增纯文档提交 `18409847`；自动合并无冲突，`src + tests` 指纹仍为 `015071bd9a0cd321f036d9438b20f9e36abe271e27fc33ec297e7c8ad5d4fa00`。按 `AGENTS.md` 的文档修改边界，不重复全量测试。
- 两次警告均为已有 YAML prompt 弃用提示和 Python 3.12 SQLite datetime adapter 弃用提示，无失败。
- `.venv/bin/python -m compileall -q src tests`：通过。
- `git diff --check`：通过。

## 独立 fail-open 评审对账

1. 他时段候选不能绕过 owner baseline：通过。
2. 一 candidate 多 owner、一 owner 多 candidate、同 `cTime` 双 child 均不采用：通过。
3. attached marker 不会被当作 child ID：通过。
4. 不完整 positions/TPSL/history 不会产生新归属：通过。
5. history-only 匿名单不会成为当前可撤单：通过。
6. 已有 ledger/logical-leg owner 不会被覆盖：通过。
7. 重启或重复 tick 不会重复创建 backup/TP：通过。
8. management 不能只凭 ledger 或只凭当前 exchange row 放行：通过。
9. watermark 前 intent 不会获得新血缘权威：通过。

评审中发现的每个 P1/P2 都先固化为失败用例再修正，最终复核时无未解决 P0/P1/P2。

## 后续授权边界

本文档记录本地候选实现、合并态验证和独立评审结果。所有者已单独授权本次同步、验证、提交和推送；仍不构成 stage、activate、重启、生产观测或存量修复的授权。上线必须按实施计划另行授权，并在交易所保护语义上再次确认不引入 fail-open。
