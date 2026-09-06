# 已验证仓位在接口中断后正常平仓的归属修复设计

## 背景

生产历史止盈收敛 ID 40 对应的入场腿被标记为 `attribution_conflict`，因此被历史状态修复工具安全排除。只读取证证明它并不是真实的归属冲突，而是“临时证据不可用”和“仓位正常平掉后从活跃快照消失”组合触发的状态机误判。

## 生产证据

- 执行绑定 208、入场腿 378、收敛 40 和三条止盈台账 11–13 的 `strategy_instance_id`、`pos_id`、绑定 ID 一致。
- 归属审计 1014 和 1015 均是政策版本 2 的 `ownership_verified/direct_order_position_id`，证据分别来自 `trade_fill` 和 `regular_order`。
- 三个止盈写入意图 16–18 均为 `confirmed`，返回的委托号与台账完全一致。
- 平仓写入意图 32 和平仓保留记录 26 均为 `confirmed`，平仓请求与回应指向同一精确仓位。
- Deepcoin 精确仓位历史返回唯一记录：`pos=5`、`closePos=5`，证明仓位完整关闭。
- 当前完整快照中不存在该仓位和三个止盈委托。
- 冲突审计 1147 的 `candidate_leg_ids` 为空，没有其他入场腿或仓位声明同一所有权。

## 根因

`reconcile_deepcoin_execution_bindings` 在任一交易所快照子读取失败时，会把非终态入场腿的当前归属状态改为 `evidence_unavailable`。下一次快照成功但仓位已经平掉时，“已验证仓位消失”分支只检查当前 `attribution_status == verified`，没有利用已持久化的权威 `ownership_verified` 审计。该入场腿因而落入过宽的“有 `pos_id` 但未匹配”分支，被错误转成 `attribution_conflict`。

代码已经有 `_has_prior_authoritative_position_audit`，并在重建入场腿经济证据时使用；缺口是它没有被用于“已验证仓位从活跃快照消失”的状态转移。

## 目标与不变量

- 临时快照错误不得永久销毁已经持久化的权威仓位所有权。
- 仓位从完整活跃快照消失不等于归属冲突；真实冲突必须有竞争入场腿、竞争仓位或精确身份矛盾。
- 恢复历史所有权不得把已关闭仓位重新标记为活跃。
- ID 40 的修复必须是默认干跑、本地数据库专用、无任何交易所写请求、带指纹和一次性令牌的受监督操作。
- 任一身份不一致、竞争所有者、快照不完整、精确仓位/委托仍活跃或平仓历史不完整，都必须阻断应用。
- 保留所有原始行和审计链，不物理删除数据。

## 方案选择

### 采用：通用状态机修复 + 现有历史修复工具的原子动作

这个方案在运行时使用已有的权威审计恢复所有权，并在现有 `repair-historical-state-convergence` 工具中增加一种受严格证据约束的止盈归属修复动作。对 ID 40 的归属恢复、收敛完成和三条台账过期在同一 SQLite 事务中完成。

### 不采用：ID 40 一次性 SQL/脚本

虽然快，但不能防止复发，绕过现有的干跑、快照、CAS、审计和一次性令牌安全边界。

### 不采用：只修运行时并永久排除 ID 40

它能防复发，但会留下已经可以由强证据证明的错误历史状态，不满足本次目标。

## 运行时防复发

在完整快照对账中，对未匹配到当前活跃仓位的非终态入场腿：

1. 仍优先使用当前快照产生的明确匹配和冲突。
2. 若存在真实 `conflict_leg_ids`、新候选仓位与已持久化 `pos_id` 不同，或其他入场腿声明同一仓位，继续进入 `attribution_conflict`。
3. 否则，如果 `_has_prior_authoritative_position_audit` 证明同 venue、同 leg、同 `pos_id`、政策版本 2 的权威所有权，就将当前 `evidence_unavailable` 恢复为 `verified`，并记录一条去重审计。
4. 这个分支只恢复归属身份，不把腿改为 `active`。后续由现有精确仓位历史/已确认平仓意图逻辑决定是否终态化。

## ID 40 受监督修复

历史修复快照在原有活跃仓位、普通委托、条件单和历史委托之外，只对“可恢复权威所有权”候选加载精确 `pos_id` 的 Deepcoin 仓位历史。该读取的错误、空白身份或返回其他 `pos_id` 都使计划冲突。

仅在以下条件同时满足时生成 `take_profit_attribution_repair` 动作：

- convergence、binding、leg 和全部 TP 台账的 venue、binding ID、leg ID、strategy instance 和 `pos_id` 一致。
- binding、lifecycle 和 leg 已终态；convergence 仍为带有完整 TP 台账的 `submitted`。
- 存在同 leg、同 `pos_id`、同 venue、政策版本 2 的权威 `ownership_verified` 审计。
- 所有后续 `attribution_conflict` 审计都没有非空竞争 leg/position 身份。
- 同一策略、binding、leg 和 `pos_id` 的 `close_position` 写入意图为 `confirmed`，且平仓保留记录为 `confirmed`。
- 精确仓位历史唯一指向该 `pos_id`，且按现有完整平仓判定规则证明已全部关闭。
- 完整当前快照证明精确仓位和全部 TP 委托都不存在。
- 数据库中没有其他腿声明同 venue/`pos_id`。

应用阶段在同一 `BEGIN IMMEDIATE`/SQLite 事务中重新校验全部本地 CAS 证据，然后：

1. 将 leg 378 的归属恢复为 `verified`，保持 `manually_closed`，写入 `historical_authority_restored` 审计及修复指纹。
2. 将 convergence 40 收敛为 `completed/convergence_position_terminal`。
3. 将 TP 台账 11–13 改为 `expired`，保留原写入证据并追加终态化证据。
4. 继续写入现有非通知型历史修复摘要审计。

## 并发、错误和审计

- 干跑计划指纹绑定本地行状态、身份字段、权威审计、平仓意图/保留记录、精确仓位历史和当前交易所快照。
- `--apply` 重新读取交易所并重建计划；指纹、动作数或一次性令牌不匹配就拒绝。
- 应用事务内对本地身份、状态和证据行做 CAS；任一并发变化使整个事务回滚。
- 修复不调用下单、撤单、改单或平仓 API。
- 重复干跑必须为零动作，一次性令牌不可重用。

## 测试设计

- 回归重现：`verified` + 权威审计 → 快照错误 → `evidence_unavailable` → 仓位完整关闭 → 快照恢复；结果不得为 `attribution_conflict`。
- 真冲突反例：有竞争 leg/position 时仍为 `attribution_conflict`。
- 弱证据反例：无政策版本 2 权威审计、audit `pos_id` 不同、空白身份、有后续真实冲突时不恢复。
- 历史计划正例：使用 ID 40 的最小生产形状 fixture，严格证据齐全时仅生成一个原子动作。
- 历史计划反例：仓位/委托仍活跃、快照不完整、历史仓位不完整、平仓未确认、身份不一致、真实竞争审计、其他 leg 声明同 `pos_id` 时全部阻断。
- 应用 CAS 测试：计划后修改 leg attribution、audit、mutation、reservation、convergence 或 TP 台账任一字段，整个修复必须拒绝。
- 交易所写边界测试：干跑和应用只允许读方法，写方法调用数必须为零。

## 生产实施与回滚

1. 本地测试、独立审查、推送到 `codex/deepcoin-auto-trading-v1`。
2. 只读证明当前仓位和委托，停止 `telegram-kol.service`，创建并验证 SQLite 备份。
3. 服务器拉取、重装、运行专项测试。
4. 干跑必须只包含 ID 40 的一个修复动作，当前活跃收敛仍为排除项，冲突为空。
5. 使用精确指纹、动作数 1 和一次性令牌应用，再次干跑必须为零动作。
6. 核对 leg 378、convergence 40、TP 11–13 和新审计，再次比对 Deepcoin 当前仓位/委托快照。
7. 启动服务并检查日志、网页、监控和当前保护。

如应用前验证失败，保持服务停止并不执行数据修复。如应用后但启动前验证失败，保留当前事故副本后从已验证备份恢复；代码验证失败时同时回退到上一生产 SHA。

## 验收标准

- 运行时回归证明临时 API 错误 + 正常平仓不再产生假归属冲突。
- 真实竞争所有权仍然 fail closed。
- 生产 ID 40 为 `completed`，TP 11–13 为 `expired`，leg 378 为 `verified/manually_closed`。
- 修复后干跑为零动作，数据库完整性正常，业务行数不减少。
- 当前 Deepcoin 仓位、普通委托和条件单在修复前后不变。
- 服务恢复 `active/monitoring`，交易执行和本次修复路径无新错误。

## 生产实施结果（2026-08-10）

- 部署提交：`46da2c74159429623c14151bc3564468b2bbfd63`。
- 停服前确认管理批次无在途状态、仓位 mutation 全部终态；SQLite 源库与备份均通过 `integrity_check`。可恢复备份：`data/backups/research.db.pre-id40-20260810T131457Z`。
- 首次生产干跑发现终态 binding 会按现有状态机清空 `pos_id` 并保留 `last_exchange_status=entry_legs_terminal`。已补防复发回归与 CAS 证据；空 `pos_id` 仅在该精确终态标记及其余完整强证据同时成立时可接受。
- 最终干跑仅生成一个 `take_profit_attribution_repair` 动作，目标 convergence 40、关联 TP 11–13，冲突为零；当前活跃 convergence 125 因交易所身份仍活跃而被排除。
- 应用后 leg 378 为 `verified/manually_closed`；convergence 40 为 `completed/convergence_position_terminal_prior_authority_restored`；TP 11–13 均为 `expired`；新增 restoration audit 2148 与 summary audit 3373，均为 `not_needed` 通知状态。
- 修复后二次干跑为 0 动作、0 冲突；数据库完整性正常，核心业务表无行数减少。
- Deepcoin 修复前后均为 2 个活跃仓位、0 个普通委托、7 个条件单，精确 ID 集合未变化。
- `telegram-kol.service` 已恢复 `active`，页面根路由、持仓面板和当前委托分页均返回 HTTP 200，页面显示“监控中”，启动后无 warning/error 日志。
