# 归属失败后的恢复闭环设计

状态：**设计候选，待评审与所有者批准；未实现。**

本轮只读取本地源码、Git 和已有调研文档，新增本文；未连接生产、未重新探测交易所、未处置存量。方案中的字段、状态、调度频率和验收均为提案，不代表现有能力或已获实施授权。

## 1. 目标与建议

**五次重试耗尽只结束一轮主动尝试，不结束观察。证据变化后，用同一归属判据重新评估；任何已可能发出的交易所动作永不由这个恢复循环重发。**

恢复成功有三个不同里程碑：

1. 找到可证明的归属：纯判定得到唯一 action。
2. 完成归属落库：事务写入 intent、logical primary leg、ledger、revision，未发生交易所写入。
3. 保护收敛：已授权的原有执行器确认备用止损/TP 现状，至多首次提交从未发送的具体动作。

第二步不能自动冒充第三步。本方案不取消附带止损、不改变 KOL 指令、不重放历史管理批次、不放宽时间/数量/身份条件。

### 方案比较

| 方案 | 优点 | 问题 | 选择 |
| --- | --- | --- | --- |
| 证据变化驱动重评 + 低频持续观察 | 无实质变化不反复判定，可追溯；失败仍有恢复机会 | 需要持久化最近输入、尝试记录和公平调度 | **推荐** |
| 周期性把 failed 改回 pending、清零次数 | 表面改动小 | 混淆人工接管与耗尽；旧执行器可能被重新解锁；历史丢失 | 不采用 |
| 永久人工复核 | 最少自动化变动 | 无法自动恢复时序故障，仍依赖人工发现和响应 | 仅保留显式人工接管路径 |

采用推荐方案，但不新增第二套归属算法。调度器只决定“何时重新取得完整输入”，无权决定“这单大概属于谁”。

## 2. 已核对的基础与证据边界

冻结判据基线：`877fbc33d783546ad2379b688c7648363a92c4a8`。本地 HEAD 为 `f4c3b618d277e2881e4216f9dd13d4d8d85a87e9`；后续实施须重新核对相关差异，不能把当前 checkout 等同于原始提交。

引用：

- [877fbc33 血缘设计](2026-09-04-trigger-protection-lineage-attribution-design.md)：完整基线、提交证言、唯一 child、精确 posId、候选与 child 原始时间精度等值、账户双向唯一、不可变所有权及存量授权边界。
- [API 调研](../2026-09-05-deepcoin-api-deterministic-link-research.md)：公开链接缺失、状态机停滞、通知已投递及时间戳口径限制。
- `execution_bindings.py:2104,2156,2596`：owner 筛选、pending/retrying 选择、五次上限；failed 不进入常规认领。
- `trigger_protection_rescue_worker.py:65`：manual_review 不在 rescue disposition 选择集合中。
- `entry_protection_ledger_repair.py:840`：`finalize_trigger_protection_adoption` 是数据库归属事务，不直接发单；但它会激活 revision，可能让同轮后续执行器满足前置条件。
- `position_mutation_gateway.py:331`：写前 CAS reserved→submitting；之后调用交易所；异常可能进入 recovery_required。
- `position_mutation_intents.py:158` 起：当前状态更新允许调用方指定前态，没有独立、不可清除的“曾进入发送边界”字段；submitted_at 在成功响应后的 submitted 转换才写入，NULL 不能证明未发出。
- `trigger_take_profit_convergence_executor.py:65`：名字带 plan 的公开函数也会更新状态和 commit，不能拿它直接做只读观察。

上一轮 `2026-09-05 05:47–05:59 UTC` 证据显示：七条 predates_fill 中六条 failed/manual_review、一条 retrying；七条 entry leg 均为本地 closed/manually_closed，Runtime Incident 均有 delivered 记录。**本轮未刷新这些生产事实。** 115 条 waiting_backup_stop 仅使用本轮用户提供的数量与会话2分工，不推断根因、对应活仓数量或写入状态。

## 3. 安全不变量

### 3.1 判据不可被重试次数影响

重新尝试必须复用基线的 attestation builder、`plan_trigger_protection_intent_assignments`、完整性检查、blocking owner 输入及 `finalize_trigger_protection_adoption`。实现可以抽取无副作用调用边界，但不能复制后简化算法。

每次仍要求：

- intent/leg/event/binding 的请求、父单、成功提交证言一致；原始 owner-specific pre-submit baseline 完整。
- 当前精确 verified 活仓、唯一成交 child、原始时间精度规则、精确保护形状成立。
- 当前账户级候选与 owner 范围完整，双向唯一；已有 ledger、logical leg、intent owner 不冲突。
- 所有身份 alias 一致；未知、不完整、多个候选、多个 owner、数量变化、时间不符均拒绝。

不增加时间容差，不以“等得够久”“竞争者少了”“剩一张”替代证明，不从当前快照反造提交前基线。永久缺少历史证言可能永远无法自动恢复，这是允许的漏做。

新恢复通路不得在 lineage 判据拒绝后退回 legacy 时间/形状认领分支；原 lineage mode/watermark 不授权时只允许观察。策略版本改变是发布事件，不是交易所证据变好，须另行批准兼容性后才能重新判定。

### 3.2 交易所写入按具体动作防重

需要区分：父入场已提交是归属的前提；“禁止重发”针对待恢复的**同一个保护动作**，不是把整个策略所有未来动作全部视为已发送。

具体动作身份固定为：账户/venue + binding + entry leg + posId + logical protection leg（role/档位）+ 原始已授权业务操作。价格、数量、attempt、时间戳、重启次数不是产生新动作身份的理由。不能新建 convergence ID、改 payload_index 或换幂等键绕过旧动作。

已有键必须保留并绑定回原逻辑腿：

- backup：`trigger-backup-stop:<binding>:<leg>:<posId>:set`。
- TP：`tp-convergence:<convergence_id>:set:<payload_index>`；原 convergence 和档位映射冻结，恢复不重建或重排。

当前 gateway 的唯一 idempotency_key 有价值，但它不单独证明跨多个业务入口无重复。实现需对相同逻辑保护腿建立唯一 effect 关联，并在发送前原子写入不可回退的 `dispatch_consumed_at`，与 reserved→submitting 同一事务提交。

| 动作证据 | 恢复允许做什么 | 禁止做什么 |
| --- | --- | --- |
| 无发送历史，或可靠证据证明只在发送前 blocked/prewrite_refused；dispatch 标记未消耗 | 原有执行器在单独授权且全部新鲜门禁通过后首次发送；复用同一动作身份 | 换键、换腿或复制任务规避失败 |
| reserved，但旧路径/历史不足以证明从未到过发送边界 | 按 unknown 查回、等待人工核实 | 根据 submitted_at=NULL 自动发送 |
| submitting / submitted / confirmed / recovery_required，或任意响应/订单号/历史发送证据 | 仅精确 readback；已确认同一结果可幂等补齐本地记录 | 再次 POST |
| 已到发送边界后明确 rejected | 保存拒绝；此恢复任务不重发 | rejected→reserved 自动再试；重试库在底层重发 |
| 消耗标记已提交，但进程在真正发 HTTP 前崩溃 | 保守视为可能已发送，只查回 | 超时后释放标记再发 |

**承诺是 at-most-once dispatch，允许漏发；不是凭本地数据库宣称交易所 exactly-once。** 所有在范围内的发送入口、HTTP 自动重试都必须遵守同一个标记。读取租约可以过期重领，发送消耗标记不可以。一次不完整/空的 REST 回读绝不能证明“上次没下出去”。

“无发送历史”只适用于已纳入新机制、从创建起具备完整发送日志和入口约束的动作。对存量，查不到 mutation 行不等于没发过；必须核对 backup/TP/rescue、logical leg、execution event、request/response 和原业务动作身份。记录冲突、遗漏或无法唯一映射均归 unknown。

现有 gateway 的 cancel 路径存在 rejected 后有条件重试；本恢复循环不得调用该重新武装入口，也不自动 cancel/replace 旧保护。全局其他已授权业务是否保留该行为不在本设计中擅自改变。

## 4. 什么证据变化会触发重评

定义一次输入签名 `F`：canonical JSON 中包含必需源的完整性/覆盖范围、原始请求和 baseline 指纹、父/child 身份及原始 cTime、目标仓位身份/数量、相关候选语义行、全部 blocking owner/占用关系、相关 mutation 的身份与消耗状态。

数组排序；保留重复 ID 及冲突 alias；不能用 set 去重后把重复候选伪造成唯一。价格、数量按既有 Decimal 语义规范化，时间保留原值与精度。排除浮盈、行情价、采集时钟、无关行的更新时间；**新采集时间本身不是新证据**。新鲜性另行校验，过期输入只能触发重新读取，不能提交 action。

| 变化 | 可用持久化锚点与精确条件 | 行为与边界 |
| --- | --- | --- |
| TPSL 不完整→完整 | 同 venue/instrument、较新 `pending_tpsl_snapshot_observations.id` 的 complete=true，前次 false/unknown；同时验证其余所需源 | 触发收集完整输入；仅 TPSL 变完整仍不能认领 |
| 候选集变化 | 两份完整、覆盖范围相同的 observation.order_ids_json 多重集不同，或已保存候选语义行发生字段变化 | 重评；列表分页/截断不同不可当“竞争消失” |
| 父/child 证据补齐 | 原 leg/event/binding/intent 身份未变，新证据出现原先缺失的成功父回执、唯一 child 或可解析 cTime，签名变化 | 重新运行原 builder；不补造 event/请求/baseline |
| owner 从未知变已验证 | 同 leg 的 attribution audit 与 posId 完整，且新鲜 positions 恰有一个同 ID 非零仓；binding/leg 无身份漂移 | 重评；leg 状态更新不能替代实盘活仓读取 |
| 竞争/所有权证据变化 | ledger、logical leg、其他 intent 的 immutable owner/候选关联或精确终态证据变化 | 重建全图；保留旧竞争证据，不因竞争者离开活仓集合而洗掉歧义 |
| 未知写入取得结果 | 原 mutation 与同一个 ordId 得到精确 readback，或原成功回执被找到 | 仅结果确认；不能借此产生第二次发送 |
| 仓位仍存活、输入不变 | last_observed 变新，但 F 未变 | 更新存活/健康观察，不反复调用同一判定；五次耗尽仍持续观察 |
| 仓位消失/人工操作线索 | 完整 positions 缺原 posId，或持久化人工处置事件 | 进入终态核验/人工保持，见第6节；不是归属放行 |

**现有持久化数据的局限：** pending observation 只有完整性、ID 集合和时间，不能还原所有候选字段；last_evidence_json 可能只有 candidate IDs；不存在可假定覆盖所有 REST 源的持久化完整快照。前两种变化可用现有行定位，其余需要复用已经保存的响应/audit，缺失则在未来正常读取时持久化最小判定输入。没有足够旧输入时记 `baseline_unknown`，第一次完整观察建立“比较基线”，不能伪称历史条件已经改善。

比较基线与 pre-submit baseline 是两个对象：前者可重新采集用于唤醒，后者不能重建，仍是归属硬门。

### 竞争消失的特别规则

“A 与 B 同形态，B 后来消失”只说明快照变了。剩余单可能仍是 B 的遗留保护。重新判定须保留历史未解决的 owner/candidate 竞争证据并通过原 blocking-owner 通路输入；终态记录不自动删除历史竞争边。只有原规则可用的证据排除该归属边，才可解除阻断。若原接口无法表达该未解决阻断，外层直接拒绝，不改成宽松唯一匹配。

同理，child 时间被交易所补齐/更正可以重评，但单纯等待不会让固定的精确时间不等变成相等；不承诺所有时序拒绝都能恢复。

## 5. 调度、状态与事务

### 5.1 将观察状态与旧业务状态分开

保留旧 recovery_state、retry_attempts、reason 作为历史，不批量清零。新增恢复观察状态（建议值）：

| 观察状态 | 含义 | 出路 |
| --- | --- | --- |
| watching | 等待新鲜、有变化的输入 | 变化→evaluating；无变化继续观察 |
| evaluating | 本轮只读判定被一个租约拥有 | 拒绝→watching；成功→ready；崩溃→租约回收并记录 abandoned |
| ready | 原判据已通过，但没有交易所授权含义 | 权威/版本/快照有效才可归属事务；失效重新读取 |
| readback_only | 某动作可能已经发出或结果未知 | 精确查回；不再发；确认后记录结果 |
| operator_hold | 有明确人工接管/结构性冲突需处理 | 保持观察和提醒，禁止自动认领或写入；需明确解除授权 |
| terminal | 原目标已确证退出或已明确交接终止自动管理 | 不再自动恢复原目标；历史证据保留 |

自动五次耗尽产生的旧 manual_review 与“所有者明确接管”必须区分。新单写明 hold_origin；旧记录来源不清楚，默认 legacy hold，不能按 NULL 或 reason 名称推断已获自动重启授权。

### 5.2 有界、公平、持续

建议调度参数（设计值，非生产现值）：有实质变化后的评估最短间隔 60 秒；读取错误按 1/2/5/10 分钟退避；无变化每 5 分钟复核健康；五次作为一轮资源预算，之后降频观察，不永久终止。新证据到达合并为一次待评估，不绕过最短间隔与全局额度。

复用正常 worker 收集的合格快照，避免每个 intent 独立拉整账户。按 due_at、last_observed_at、intent ID 公平轮转；每轮最多处理20个目标但**不截断它们所需的账户 owner/candidate 证据范围**。队列头不能长期占据固定前20。容量不足暴露调度延迟，不把旧快照标为新鲜。

扫描必须包含已纳入恢复范围的 failed/manual_review，以及缺 owner、next_attempt_at=NULL 的非终结记录；状态筛选不能先把它们排除。未成交父单进入 pending-entry 观察，不能报成已成交无止损。

### 5.3 提议的最小持久化增量

不重建通用事件总线。复用 intent、ledger、mutation 和 Runtime Incident；提出以下增量，全部留待单独 schema 评审：

- intent 的恢复控制字段：scope/enrollment、observe_state、hold_origin、next_observe_at、last_observed_at、last_progress_at、last_input_fingerprint、lease_token/expires_at、version；恢复来源和 followup 授权不能从旧状态猜测。
- 一张恢复尝试表：intent ID、attempt ID、触发原因、输入签名/有界证据引用、policy SHA、source coverage/freshness、开始/完成时间、前后状态、结果 reason、candidate/child/posId、相关 mutation IDs、write_consumed、授权引用、claim token；每次判定先持久化 started，结束追加/固定结果。唯一活跃 claim 与版本 CAS 防重复落库。
- mutation 的 logical_effect_key 唯一关联与不可清除 dispatch_consumed_at。旧动作无法唯一映射时不回填虚假“未发送”；进入 unknown。既有已消耗证据不得被清理或 rollback 重置。

完整原始 JSON 不进通知；证据引用必须持久可取且含必要原始字段，单个 hash 不足以复盘。无变化观察只更新心跳和计数，不产生成千上万重复 attempt；每一次真正调用判定器都必须有 attempt。

### 5.4 原子归属与后续动作隔离

1. 认领读取租约，取得有界且完整、新鲜的输入；使用纯 builder/planner。禁止观察模式调用会 commit 的公开 plan 或整个 reconcile/rescue 函数。
2. 通过后，先核实 scope、原 lineage live/watermark、人工保持、相关未知 mutation；不符合只记录 would_adopt/readback_only。
3. 归属事务内 CAS 原 intent/leg 版本，重查不可变 owner、mutation 消耗状态、授权、租约与输入的新鲜性；调用原 finalizer。任一变化回滚，重新读取。事务外到来的交易所变化仍靠原写前再读门阻断，不能宣称跨交易所原子快照。
4. **同一事务标记 recovery_origin 与 followup_policy=blocked**。所有可能被 adopted/active revision 解锁的 backup/TP/rescue/management 入口必须识别该阻断；不能先 commit adopted 再补阻断标记。
5. 只有后续单独批准的 scope 才可解除 followup 阻断；执行器重新检查每个动作的身份/消耗标记，已发送动作只查回，未发送档位才能首次执行。多个档位只按原始逻辑腿处理；遇到任意结果未知停止其依赖动作。

归属调度不拥有 POST 能力；只读 planner 客户端只暴露 GET。能写数据库的 adoption 阶段与可写交易所的执行器是不同能力。若尚未完成所有下游阻断检查，**只允许 observe，不允许开启 adopt**。

## 6. 真正终止与人工接管

| 情况 | 必须满足的证据 | 处理 |
| --- | --- | --- |
| 已经成功归属 | 原事务成功；当前精确回读满足原判据 | 结束归属重试，进入现有保护健康监控；不代表所有 TP/backup 已完成 |
| 原仓位已关闭 | 新鲜完整 positions 中无原 posId，精确 position history/平仓成交证明终态，关联未知写入已对清 | terminal(position_closed)，不挂任何保护，不重放消息 |
| 仓位未显示但历史不完整 | 只有缺席/空数组或本地 closed | terminal_check_pending/unknown，继续只读核验；不自动当作关仓 |
| 人工明确接管仍有仓位 | 持久化、可归属到原 target 的所有者交接指令及时间/范围 | operator_hold，停止自动 adoption/write；健康观察继续，直到明确交接终结或解除 |
| 人工已平仓/取消原任务 | 可审计人工指令加精确交易所终态，且未知结果已对清 | terminal(operator_resolved) |
| 无法恢复的基线/证言缺失或身份冲突 | 已记录具体缺失/冲突，无法按原规则重建 | operator_hold；不中断长期停滞监控，不能靠重试次数解除 |

有未知写入时，即使原仓位关闭，也先进入 readback_only/结果核验；不能在终止时删除可能作用于账户的未知保护单。已关闭旧 posId 后出现同方向新 posId，不是旧任务复活。

告警 delivered/acknowledged、用户打开页面、普通手动减仓或修改订单都不等于“授权系统认领”或“人工已完成接管”。任何明确人工干预线索先阻断自动权限，再核实范围。

## 7. 存量7条与115条的接回边界

### 七条 predates_fill

按上一轮证据均为本地终态，因此**上线不自动重置、不自动接回活仓保护流程**。顺序是：另行批准只读逐条分类→新鲜完整 positions 与精确历史验证→核对所有关联 mutation/保护残留→生成 exact-ID manifest→单独 L3 数据终结授权。只记录终结原因，不虚构 adopted，不补历史 ledger，不下单。

若刷新发现一条仍对应真实活仓，也不能自动越过 877fbc33 的 lineage watermark、缺失原始 baseline 或旧 manual_review 来源不清问题。需要逐条授权纳入；用同一个 planner 得出可复核 action。归属写入与 backup/TP 交易所动作分别授权。无法证明就保留 hold。

### 115条 waiting_backup_stop

**待会话2确认：**哪些是活仓、哪些已终态；实际主/备用止损与账本交集；是否存在发送未知或同逻辑动作多条记录；实际 selector/watermark；115条是否对应115个不同目标。本文不把它们归因为“归属失败”。

新观察机制可在另行批准后监测它们，但不能直接把 waiting_backup_stop 改为 ready：它属于 TP convergence 层，归属重评只可能改变它的一项依赖。只有会话2证明根因已解除，且精确 primary/backup 均按原判据 verified、原 convergence/档位/posId 不变、全部发送历史已对账、未发送档位可证明、数量与权限仍成立时，才可在该批存量授权范围内接回原执行器。

若 backup 已可能发送，只读查回；若目标已关闭，走终态处理；若从未发送但原因仍不明，保持 waiting。新的归属恢复功能本身**不携带这115条的执行授权**。

交接给会话2所需最小字段是 intent/binding/leg/posId/convergence/logical leg/mutation ID 的关系、write state、实际 pending/ledger 交集、缺失证据与终态分类，不需要本轮重新调查其根因。

## 8. 长期停滞与审计

区分四个时钟：first_unresolved_at、last_observed_at、last_attempt_completed_at、last_progress_at。每轮循环开始时间不能作为实际完成时间；采集/判定/事务完成分别记录。普通轮询、重复失败、续租、告警投递都不能刷新 last_progress_at。

last_progress_at 只由可审计的里程碑推进：原缺失的必要证据首次补齐、原未知动作精确确认、归属事务成功、精确终态核实。新候选反复加入/消失、reason在不同拒绝间切换不算进展；另保留不可重置的 first_unresolved_at，持续暴露总等待时长。

停滞谓词（建议5分钟为活仓初始阈值，30分钟升级；这些是待批准运维参数，不影响归属判据）：

```text
unresolved = 未验证归属且未确认终态（包含 failed、manual_review 与无 owner）
anchor = 有证据的成交/首次未归属时间；否则 intent.created_at
unscheduled = unresolved AND next_attempt_at IS NULL AND next_observe_at IS NULL
stalled = unresolved AND now - COALESCE(last_progress_at, anchor) > threshold
observer_stale = unresolved AND (last_observed_at IS NULL 或观察间隔超限)
```

上述判断不要求 next_attempt_at 非空。对父单仍完整可见且未触发者，只报 pending-entry 等待/调度健康，不报活仓裸奔；对是否成交未知者明确报 evidence_unknown，不静默排除。manual hold 继续有交接/处理时限监控。

复用 Runtime Incident：首次停滞、严重度升级、证据变化后的再次拒绝、归属恢复与终态分别可追踪；同一问题按 target/阻塞原因去重，不能把 observed_at 加入事件身份造成每轮刷屏。delivered 只表示通知送出，incident resolution 必须绑定真实恢复/终结记录。

指标至少有：未归属活仓数、未知仓位状态数、NULL调度数、最长无进展时间、观察滞后、各原因重评结果、租约过期数、readback_only 数、被防重拦截次数、adopted但followup仍阻断数。attempt 中 request dispatch count 必须为0；被许可的具体 effect 生命周期 dispatch count必须≤1。

## 9. 授权、风险分级与回滚

每行是独立批准对象；设计接受不代表下一行获批。

| 阶段 | 交付与边界 | 风险/必须证据 |
| --- | --- | --- |
| D 本轮设计 | 本文、依赖与未决项 | L0 文档检查；无生产操作 |
| C 本地代码 | 默认关闭；抽取纯判定边界、观察/审计、下游阻断、发送消耗标记；不迁移生产 | 单独代码授权；schema与发送路径按L3设计，不能因默认关闭忽略语义风险 |
| T 测试验证 | 另行批准的测试阶段，冻结最终候选；本地 focused/完整回归，必要的数据库副本迁移演练 | 每次开发编辑保持针对性检查；最终候选一轮全套；不夹带部署或真实发单 |
| R 独立评审 | exact SHA 检查判据不变、无重复发送、rollback兼容、无隐藏自动解锁 | 单独评审授权；未通过不集成/启用；本轮没有启动评审代理 |
| O 生产观察 | 单独 stage/activate/migration 与 observe-only 授权；无归属权威、无交易写入 | schema迁移L3备份/quick_check/关键计数；观察按新增shadow L1，15分钟或5条消息先到；不足不扩大结论 |
| A 新范围归属 | exact SHA、scope/watermark；仅adopt，followup保持blocked | L2权威切换：30分钟、至少5条消息，尽量2群；未达流量留in_progress；不包含存量 |
| W 后续保护执行 | 单独认可对具体新范围的既有动作首次执行、防重与未知结果行为 | L3交易所写语义授权；本轮不请求也不执行 |
| H 存量 | 7条、115条分别形成快照与manifest，分别审批归属/终结/交易所动作 | 数据写入L3；不能与新单功能激活捆绑 |

关闭观察/归属功能时停止新 claim；旧 claim 无权越过新的关闭门。保持全部 ledger、动作消耗标记和未知结果证据。回滚到不认识 dispatch 标记/恢复来源的旧版本，必须先关闭受影响执行权限并验证旧代码不会重新发送；不能仅切换 release 就宣称安全回滚。不能用旧数据库备份覆盖上线后的发送事实。

## 10. 验收矩阵与未决项

| 场景 | 必须证明 |
| --- | --- |
| 五次失败后快照变完整 | 调度再次评估，完整原判据通过才可认领；次数不清零、不影响判据 |
| 同输入重复/只有时间变化 | 不触发重复判定风暴；持续健康观察 |
| 两候选/两owner后来消失一个 | 不丢失历史竞争证明；仍可能拒绝，绝不能仅凭剩一个通过 |
| child时间不等、基线缺失、alias冲突 | 与877fbc33相同拒绝；不因重试放宽 |
| 原判据所有负面fixtures | 原拒绝语义保持；同等完整输入不得产生更多可认领集合 |
| NULL next_attempt_at、无owner | 被监控和观察；不会误作未成交活仓；不静默跳过 |
| 两worker/租约过期/事务冲突 | 至多一个归属提交；旧claim不能落库；重启后有abandoned审计 |
| 发送消耗落库前后各崩溃点、超时、回执丢失、明确拒绝 | 同effect自动dispatch至多一次；不换键、改convergence/index或清标记 |
| adopted事务后同轮运行backup/TP/rescue | 未有followup授权时零交易所写入 |
| 3档TP第一档成功、第二档未知 | 第一档不重发；第二档仅查回；依赖的第三档不得盲目继续 |
| 关仓/人工接管与重评竞争 | 原目标不被复活；无精确终态证据保持unknown |
| 100+等待对象且队首一直无变化 | 后排仍在预算内被公平观察；证据范围不被工作批量截断 |
| 关闭/回滚与发送事实 | 不清除消耗记录；不产生第二次dispatch |

待批准/确认项：调度与停滞阈值；schema字段最小集合及所有发送入口覆盖证明；会话2的115条根因与逐条分类；存量人工保持来源；新范围lineage激活权限。它们不阻碍本设计候选交付，但阻止直接上线或批量接回。
