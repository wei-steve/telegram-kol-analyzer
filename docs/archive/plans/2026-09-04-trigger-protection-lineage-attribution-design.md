# 触发入场保护订单血缘归属修复设计

## 状态与安全目标

设计状态：**待所有者审批，未实现。**

目标是修正一个过于严格但语义错误的时间门：Deepcoin 在触发入场成交时可以先生成附带止损，
后生成/投影 live position。所以 `candidate.created_at < live_position.created_at` 不能否定它属于这笔入场。

安全目标不是“尽量认领”，而是：

> 只有当持久化提交证言、父触发单、唯一成交 child、精确 `posId`、提交前完整基线和
> 当前完整 TPSL 快照共同构成一条唯一、无冲突的血缘时，才可认领保护单。

无法证明就拒绝；不允许时间容差、最近一张、同价同量或单个 client ID 变成归属猜测。

## 方案比较

### 方案 A：给 `live_position.created_at` 加容差

例如允许候选早 10 或 30 秒。优点是改动小，但它没有证明所有权；同形状历史单只要落入窗口就可能被误认。
不采用。

### 方案 B：对所有匿名止损删除时间门

依靠合约、方向、数量、止损价和唯一候选认领。这些字段均可重复，且“当前只有一张”不能证明它不是其他时段的留存单。
这会变成 fail-open。不采用。

### 方案 C：提交证言 + 每个 owner 的前置基线 + 成交血缘 + 账户级双向唯一

将时间从“所有权判据”降为“序列合理性门”，用提交前基线和父单成交血缘证明候选来源，再用账户级双向唯一排除同形状竞争者。

优点：可精确覆盖已证实时序；不降低快照完整性、活仓、数量、账本不可变或唯一性门；不需要数据库 schema 迁移。

代价：必须修正现有账户级分配器的 owner 数据模型，且在任意证据缺失或多解时仍会拒绝。

**采用方案 C。**

## 归属证据分级

### 等级 1：交易所候选行直接身份

如果候选 TPSL 行提供了唯一、无冲突的显式 `posId`，且等于已验证 entry leg 的 `posId`，
这是直接身份证据。如果交易所还返回父 trigger order ID，它也必须等于持久化的
`parent_trigger_order_id`。任一 alias 冲突都拒绝。

直接 `posId` 仍不跳过当前活仓、合约、方向、保护价格/数量、快照完整性、账本所有权冲突和候选 order ID 唯一性检查。

### 等级 2：无 `posId` 的附带止损组合血缘

对 Deepcoin 当前这类无 `posId`/无父单号的 pending TPSL，必须同时满足下列全部条件。

#### A. 本地父单和提交证言唯一

1. intent、entry leg、execution event 和 binding 的 `execution_binding_id` / `execution_order_leg_id`
   一致，venue 是 Deepcoin，leg 是 `entry + trigger_limit + active + verified`。
2. intent 的 `parent_trigger_order_id` 同时等于 entry leg `order_id`、唯一
   `create_trigger_entry` event `order_id`和 binding `submitted_orders` 中的唯一 order ID。
3. event 与 binding submitted order 的父单回包均成功：`code=0`、`sCode=0`、
   `ordId` 等于父 trigger order ID。
4. leg request、event request、binding submitted-order request 和 intent `request_fingerprint` 完全一致。
5. 请求明确且只携带期望的原生止损：`slTriggerPx > 0`、`slOrdPx=-1`；
   本次的匿名分支不扩展到组合 TP+SL 或含糊保护集合。
6. binding submitted order 中唯一对应的 `protection_request` 与上述止损一致，
   `protection_response.code=0`、`data.attached_on_trigger_order=true`。

`attached_on_trigger_order` 的证明范围只是“该父单成功提交时带了这份保护请求”；
因为它不包含 child TPSL `ordId`，所以它不是单独的所有权证据。

#### B. 父触发单到实盘仓位的血缘唯一

1. 父 trigger order 只对应一个 child regular order；
2. child 必须是已成交终态，其直接 `posId` 唯一对应该 entry leg；
3. child regular order 的交易所 `cTime` 必须存在且可解析；对无 `posId`/无父单号的
   attached stop，候选 TPSL 的交易所 `cTime` 必须与这个 child `cTime` 在交易所原始精度上完全相等；
4. 当前完整 positions 快照中必须恰好有一个同 `posId` 的非零仓位；
5. 仓位的 `instId`、`posSide`、split mode 和当前 size 与 entry leg 的已验证身份及数量一致。

child `cTime` 与 candidate `cTime` 的精确等值不单独构成所有权，它是 attached submission、
owner baseline、精确保护形状和双向唯一之上的一条血缘边条件。两个 child 如果在同一交易所时间精度内成交且形状相同，
依然是多解，不得按 order ID 大小、返回顺序或腿序号强行配对。

`client_order_id` 只用来唯一选中本地 leg/event/submitted-order 组，不能代替
`父 trigger order ID -> 唯一 child regular order -> posId` 的交易所血缘。

#### C. 提交前基线证明候选不是旧单

1. 每个 intent 必须有在提交锁内取得、成功解析的完整 `pre_submit_tpsl_baseline_json`；
2. 候选 `ordId` 必须不在**这个 owner 自己的**基线中；
3. 不得再把所有 owner 的 baseline order ID 合并成一个全局排除集。候选出现在更晚
   owner 的基线中，只能证明它不属于更晚 owner，不能否定它属于更早 owner。

这是排除“别的时段的历史单”的关键证据，比仓位投影时间更接近真实提交边界。

#### D. 当前候选形状精确

候选必须来自同一轮完整 pending TPSL 快照，并且：

- `ordId` 非空且在账户快照中唯一；
- `triggerOrderType=TPSL`；
- `instId`、`posSide` 与 owner 一致；
- `sz` 数值等于入场 leg/当前仓位数量；
- `slTriggerPx` 数值等于提交时保存的止损价；
- `tpTriggerPx` 为 0/空，不将未规划的组合单当作 stop-only 保护；
- 候选没有冲突的 `posId`、parent ID 或其他 identity alias。

合约、方向、数量和价格每一项单独都不具备唯一性；它们只是组合血缘的形状约束。

#### E. 账户级双向唯一和不可变所有权

用 owner-specific baseline 和血缘证言构造 owner <-> candidate 二分图。只有当：

- 一个 owner 恰好只有一个候选；
- 该候选恰好只有这一个 owner；
- `venue + order_id` 没有既有账本所有者，或既有所有者就是完全相同的
  binding / leg / strategy instance / `posId` / instrument / side；
- 该 order ID 没有被其他 logical protection leg 或 intent 占用；

才生成 adoption action。任意 0 解、多解、别名冲突或已有所有者冲突均为零写入。

## `created_at` 时间判据的处置

### 对新的血缘证言分支

删除 `candidate.created_at >= live_position.created_at` 作为所有权条件。原因是 live position `cTime`
不是这类交易所附带止损的生成下界。

时间仍保留为序列合理性门：

- candidate `created_at` 必须可解析；
- candidate 不得早于交易所写入前已持久化的 intent `created_at`；父单成功 event
  是事后 ack，即时成交时候选可能早于 event 落库，因此不把 event `created_at` 当下界；
- 无显式位置/父单身份的 attached candidate `created_at` 必须与已验证唯一 child regular order
  的交易所 `cTime` 在原始精度上精确相等；
- candidate 不得晚于包含它的快照 `observed_at`；
- 不增加容差。时钟偏差导致该门无法通过时，继续 fail closed 并告警。

这些门可以防住提交前的旧单、未来时间异常和缺失时间的不可审计候选，但它们不再用来区分
“止损生成”与“仓位投影生成”的毫秒级先后。

### 对无血缘证言的旧路径

保留现有严格时间门和拒绝。尤其是：

- 不在当前 pending 快照的匿名 history 单，不能因为形状相同就成为当前可撤换保护；
- 组合 TP+SL、无完整基线、无成功提交证言、无唯一 child/`posId` 的候选不进入新分支；
- 旧的 legacy repair 和组合保护路径不因本次修复而放宽。

## 数据模型和纯函数边界

新增不可变值对象 `TriggerProtectionLineageAttestation`，只包含有界证据：

- binding / leg / execution event / intent ID；
- parent trigger order ID、client order ID、child regular order ID、child exchange `cTime`、`posId`；
- instrument、side、size、stop price；
- parent request fingerprint、owner baseline fingerprint、submission-attestation fingerprint；
- pre-submit intent created at、parent ack at、live snapshot observed at；
- `attached_submission_confirmed=true`。

builder 输入完整 binding、leg、intent、唯一 parent event、唯一 child/fill 证据和 live position。
输出要么是完整 attestation，要么是一个闭合 reason code，不返回部分可用对象。

`ProtectionOwner` 增加 owner-specific baseline、pre-submit intent time 和 attestation fingerprint。
`ProtectionOrderCandidate` 保留创建时间和显式 `posId` aliases。二分图的每条边必须同时经过
owner-specific baseline、直接 identity/附带血缘、形状和时序合理性门。

adoption evidence 至少保存：

```text
match=lineage_attested_attached_stop | explicit_pos_id
parent_trigger_order_id
child_regular_order_id
child_exchange_created_at
pos_id
intent_id
request_fingerprint
owner_baseline_fingerprint
lineage_attestation_fingerprint
snapshot_fingerprint
candidate_order_id
candidate_created_at
```

不保存完整交易所响应或凭据。

## 归属成功后的收敛

### 原子归属

继续使用现有 `finalize_trigger_protection_adoption()` 事务，在一个数据库事务内：

1. 重新验证 leg 仍是 exact binding / `posId` / active / verified；
2. 重新验证 logical primary leg 未绑定其他 order ID；
3. 重新验证 `venue + order_id` 不可变所有者；
4. 绑定 primary logical leg；
5. 写入 verified protection ledger；
6. 将 intent 终结为 adopted；
7. 激活 protection revision。

任一竞争冲突使整个事务回滚，不得用 planner 时的旧快照强制写入。

### 避免重复补单

归属成功不直接“重建整套保护”。它只解锁现有收敛机制，而收敛机制必须在每次写前重新读取：

- 精确实盘仓位和当前数量；
- 完整 pending TPSL 快照；
- 当前 verified ledger / logical protection legs；
- 现有 backup stop row、TP convergence row 和 mutation intent。

规则：

1. 主止损已在交易所且已认领，只保留，不重建。
2. 已有一张精确 verified 备用止损时，不再创建；否则使用现有
   `trigger-backup-stop:<binding>:<leg>:<posId>:set` 幂等键预留、提交、回读。
3. 对每一档止盈，先按当前 size 重新分配，只创建缺失档位；总量不得超过当前仓位。
4. 已有未归属止盈可能作用于该仓位、任一快照不完整、数量漂移或未知提交结果时，停止后续写入。
5. 已经存在的部分保护按 order ID + 账本所有者 + 当前快照交集计算，不按记录数猜测。

## 与管理路径的关系

对未来的管理消息，归属成功后 **有条件地** 解除
`protection_missing_cancellable_order_id`：

```text
verified ledger order ID
AND 当前完整 TPSL 快照中恰好一次出现该 ID
AND 快照行与 ledger 的 posId/instrument/side/price/size 不冲突
AND 该 ID 未被其他仓位/管理腿使用
```

如果只写入了 ledger，但订单在当前快照中已不可见，或价格/数量冲突，management planner
仍必须阻断。认领不是永久撤单授权。

已终结的历史 batch `152` / `157` 不会、也不应因归属修复而自动重放。
修复改变的是未来和明确授权的存量收敛路径，不是回放旧 KOL 指令。

## 存量六条 failed/manual_review 的处置边界

存量处置与代码修复、新单激活分开授权，不做批量自动重试。

### A. 仅剩历史记录

必须由 worker 8002 的当前完整 positions/pending TPSL GET 和精确 position history 共同证明：

- 目标 `posId` 当前不在实盘 positions；
- 已有精确历史关仓证据；
- 没有待收敛的未知交易所结果。

这类记录：

- 不补写保护 ledger；
- 不补挂止损/止盈；
- 不重放 management batch 或原消息；
- 不删除失败证据；
- 如需将未终结的历史 intent 收敛为“position already terminal”，应另做一个只修数据的 L3 计划和授权。

本次只读时点七条命中记录的 binding 均已 closed，当前 worker 唯一实盘 pos 不在其中；
因此现阶段没有一条可进入自动活仓修复。

### B. 仍有实盘仓位

如未来发现其他同类 active intent，必须为每一个 `posId` 生成单独的新鲜修复计划：

1. worker 8002 重读完整 positions / pending TPSL / relevant history；
2. 只读 planner 使用与自动路径相同的血缘判据；
3. 计划绑定 exact binding / leg / `posId` / candidate order ID / snapshot fingerprint / action fingerprint；
4. 所有者逐笔审核后，另行授权本地数据归属写入；
5. 归属后的备用止损/止盈补挂又是交易所写语义，需要再一次独立授权。

仓位已平、快照不完整、候选多解、数量漂移、任一 identity 冲突或已有未知 mutation 时，
计划必须为零 action。

## 主动可观测性

### 告警时机

不再等第五次重试失败。在第一份完整快照同时证明以下状态时立即创建可推送事故：

```text
live verified position
AND submitted attached-stop attestation
AND exact-shape native stop candidate visible
AND adoption not verified
```

消息必须明确说“交易所已看到止损，但系统未验证归属，改止损/止盈/部分平仓可能被阻断”，
不能误报为“交易所没有止损”。

如快照确认真的没有任何止损，使用独立的“实盘仓位缺失止损” critical 事故，不与归属失败混合。

### 去重和升级

事故 fingerprint 绑定 intent ID、binding/leg/`posId`、reason code、candidate order ID 集合和 snapshot fingerprint。
同一状态受现有抑制窗口去重；以下转换必须立即产生新事件：

- 首次血缘验证不通过；
- management batch 因缺少可撤换 ID 被阻断；
- intent 进入 failed/manual_review；
- 事故恢复：adoption verified，且当前快照仍唯一含有该 order ID。

有 verified exact backup stop 时可降为 warning；无 exact backup 或已阻断管理时为 critical/high。

推送内容仅包含群组标签、lifecycle/binding/leg、可展示的有界 `posId`、原计划止损、
候选 order ID、首次暴露时间、reason code 和建议查看的策略记录链接；不包含 KOL 个人信息、
凭据或完整交易所响应。

## 新的闭合 reason codes

为避免再次只看到笼统 `candidate_predates_fill`，新分支至少区分：

- `trigger_protection_submission_attestation_missing`
- `trigger_protection_submission_attestation_conflict`
- `trigger_protection_parent_response_unconfirmed`
- `trigger_protection_child_lineage_not_unique`
- `trigger_protection_child_time_unavailable`
- `trigger_protection_candidate_child_time_mismatch`
- `trigger_protection_owner_baseline_invalid`
- `trigger_protection_candidate_in_owner_baseline`
- `trigger_protection_candidate_predates_submission_intent`
- `trigger_protection_candidate_after_snapshot`
- `trigger_protection_candidate_shape_conflict`
- `trigger_protection_assignment_not_mutual_unique`
- `trigger_protection_order_owned`

对原有非血缘分支保留 `trigger_protection_candidate_predates_fill`，用于表示“没有足够血缘证言，且旧时间门也不通过”。

## 独立 rollout 门和 future-only 水位

不复用当前已为 `live` 的 `position_management_liveness_v2_mode` 作为新归属分支的唯一开关。
新增两个独立设置，存入现有 `trading_settings.value_json`，不需要 schema 迁移：

- `trigger_protection_lineage_attribution_mode = disabled | shadow | live`，默认 `disabled`；
- `trigger_protection_lineage_activation_after_intent_id`，默认 `NULL`；只有经审核的非负整数才是有效 live 水位。

有效模式还要与现有总门相与：只有现有 management/liveness 权威允许、新门为 `live`
且水位是经审核整数，
新分支才可写 adoption。`shadow` 只产生有界差异证据；`disabled` 完全沿用旧判定。

激活时将当时生产 `MAX(trigger_protection_intents.id)` 审核为 watermark，仅
`intent.id > watermark` 可进入 live 血缘分支。历史 failed/manual_review 和激活前 pending/retrying 不得因切换自动变成 adoption。

## 风险等级与分步授权

本设计是 **L3（交易所保护写语义的前置权威变更）**。即使 adoption 本身只写本地账本，
它会解锁已启用的备用止损和止盈 executor，因此不能按一般数据归因改动处理。

授权边界必须分开：

1. **设计审批**：仅本文和实施计划；不改代码。
2. **本地代码与测试**：只改 pure matcher / reconciliation / monitor 和测试；不连接交易所写接口，不部署。
3. **独立安全评审**：评审者专项确认不存在 fail-open，尤其审查基线所属、多 owner/candidate、已有所有者冲突、历史单和不完整快照。
4. **immutable stage，功能 disabled**：另行批准 exact commit 和 manifest；独立新门默认 disabled，不激活新权威。
5. **shadow 验证**：只记录 old/new 决策、血缘证据和差异，零 ledger 写入、零交易所写入。
6. **future-only live 激活**：再次独立授权；只处理激活 watermark 之后新建的 intent，不触发历史重放。
7. **存量只读盘点**：对 failed/manual_review 逐笔区分 live 与 historical-only；不写。
8. **存量 DB 归属写入**：若真有 live 目标，每个 `posId` 单独指纹和授权，需备份、`PRAGMA quick_check`和 before/after 关键计数。
9. **存量交易所收敛写入**：与 DB 认领再次分开授权，逐 `posId` 提交、回读和核对，不批量补单。

若最终实现不改 schema，代码测试不需要生产 DB copy 迁移演练；任何真实生产 DB 行修改仍必须按 L3 备份和核对。

## 上线验收与回滚不变量

激活前必须证明：

- 已知 lifecycle `1050` 和 `1072` 回放在新 planner 中只各产生一个精确归属；
- binding `336` 两条同价同量腿的候选集合在交换顺序后结果不变；
- 两 owner 两 candidate 真多解、两 child 同一 `cTime` 的多解、不完整基线、缺失 attached attestation、已有所有者冲突、候选早于持久化提交意图或与 child `cTime` 不等时零 action；
- 重启、重复 worker tick、候选返回顺序变化都不会重复创建 ledger、backup stop 或 TP；
- management planner 只在 ledger + 当前唯一快照双重成立时解除 blocker；
- 全套测试通过，且有独立评审者明确给出“未发现 fail-open 路径”结论。

回滚只禁用新的血缘认领分支并恢复已审核 release。已经通过完整证据认领的 ledger 映射不删除，
已经回读确认的交易所保护单不撤销、不重建。未知提交结果只能继续只读对账，不因回滚自动重试。
