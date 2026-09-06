# Recognition Execution Lease Recovery Design

> **2026-09-02 修订说明：** 本次根据 entry-assembly wakeup 重复执行只读核查，改写第 2、5、6、9、
> 11、12 节，并新增第 6.1–6.3 节。新增/改写处均以“2026-09-02 修订”标注。修订补入：adapter
> 返回契约、entry-assembly wakeup 独立持久 fence、process-wide SIGTERM drain、scanner durable
> cursor。本次只修订设计，不授权实现。

## 1. 目标与非目标

目标是在不制造重复执行可能的前提下，消除新的
`recognition_decisions.comparison_status=execution_running` 永久孤儿，并为现存 29 行提供
逐行、可审计、可回滚的处置边界。

本设计不改变识别 prompt、上下文判据、candidate 内容、交易参数或 Deepcoin 写语义；不以
“让 backlog expiry 跑通”为理由削弱现有保护；不授权本轮实施代码、schema 或数据修复。

核心安全次序是：**可以继续漏做，也不能重复做。** 任何不能证明处于 exchange-write
boundary 之前的尝试，一律冻结为 outcome unknown，不自动重放。

## 2. 调查结论对设计的约束

> **2026-09-02 修订：本节增加第二入口的只读核查结论。**

只读调查见 `docs/2026-09-02-recognition-execution-running-root-cause-read-only-audit.md`：

- 29 条均由 context reanalysis 时间链关联；27 条 outer job 的 `succeeded` 是 context worker
  吞异常、外层忽略返回状态所致，不是 finalize 成功的证据。
- claim 后至 finalize 前没有提前 return，也没有 assessment 从非 failed 变成 failed 的路径；
  真实缺口是 post-claim exception/process-death safety。
- 29 条当前 generation 均无 candidate、binding、order leg、execution event、management
  batch/envelope/target；未发现已执行但无 automation 记录的 (b) 类。
- 当前 shutdown 直接 cancel message-processing worker；其中运行的 `asyncio.to_thread()` 不会
  因 awaitable 被 cancel 而停止底层线程。因此 graceful shutdown 必须 drain，而不能把 task
  cancel 当成执行已经停止。
- 现有 `WorkerCommandJob` 已经证明一个可复用的安全模式：只允许回收尚未跨越
  `side_effect_started_at` 的过期 claim；跨越边界的过期执行转为 `uncertain`，永不自动重放。
- entry-assembly wakeup 的第二入口只读核查见
  `docs/2026-09-02-entry-assembly-wakeup-duplicate-execution-read-only-audit.md`。结论为甲：当前生产
  路径由 `MessageInstructionItem pending→executing` 和覆盖 legacy/no-item 路径的稳定
  `TradeSignal pending→processing` CAS 阻止第二次 exchange write；历史也未发现 wakeup 重复 entry
  写实例。该结论不依赖 Deepcoin 对 `client_order_id` 去重。
- 但是 `EntryAssemblyAttempt claimed→pending` 的五分钟 stale recovery 自身不检查副作用边界，且
  `authoritative_recognition.py` 的 wakeup adapter 调用位于主 authoritative attempt 之外。下游当前
  恰有保护不等于该入口设计正确；新状态机必须给它独立 durable fence，不能把主 lease fence
  生搬到另一条消息、也不能继续依赖下游实现细节。

## 3. 方案比较

### 方案 A：单纯 `try/finally` 清掉 `execution_running`

拒绝。它能覆盖普通 Python 异常，却覆盖不了 SIGKILL/掉电；更重要的是，如果 Deepcoin 已接收
写请求但本地尚未 finalize，盲目清锁会让同一消息再次执行。

### 方案 B：超过固定时间直接把 running 重置为 pending

拒绝。当前 token 没有 owner identity、heartbeat 或 side-effect phase；“时间久”只能证明状态
陈旧，不能证明没有进程仍在执行，也不能证明交易所没有接收写请求。

### 方案 C：持久化 execution attempt、显式副作用边界、fencing CAS、unknown 冻结

采用。每个 authoritative generation 新增一条持久化 attempt；所有持有者必须在进入任何
exchange-capable adapter 前，以 exact token 做 CAS 写入副作用边界。只有边界前的过期 claim
可安全终结；边界后的失联尝试只转为 `execution_uncertain`，不重放。

这不是依靠 PID 推断安全，而是依靠 durable phase + fencing：旧持有者若尚未跨界，在其 token
被 CAS 撤销后就无法跨界；旧持有者若已经跨界，持久化状态会明确禁止回收。

## 4. 新的持久化状态机

新增表 `authoritative_execution_attempts`，一条记录对应一个
`(raw_message_id, authoritative_generation)`，保留历史，不覆盖前次尝试。

| attempt status | 含义 | 自动处置 |
|---|---|---|
| `claimed` | 已取得 decision generation，但尚未进入 exchange-capable adapter | lease 过期后可用 exact CAS 终结为 `failed_safe` |
| `executing` | 已持久化 side-effect boundary，adapter 可能正在/已经写外部系统 | 绝不重放；失联后只转 `uncertain` |
| `outcome_recorded` | adapter 或纯 skip 分支的 canonical automation outcome 已持久化 | 可仅重做 decision finalize，不能重调 adapter |
| `succeeded` | decision finalize 成功 | terminal |
| `failed_safe` | 已证明未跨副作用边界，执行尝试安全失败 | terminal，可由新的显式识别产生新 generation |
| `uncertain` | 已跨边界但没有可确认 outcome | terminal fence；需独立 reconciliation/人工授权 |

`recognition_decisions` 对应状态：

- `claimed/executing/outcome_recorded`：保留 `execution_running`；
- `succeeded`：沿用当前 finalize 后状态；
- `failed_safe`：`comparison_status=completed`、`automation_status=failed`，reason 使用固定机器码
  `authoritative_execution_abandoned_before_side_effect`；
- `uncertain`：新增 `comparison_status=execution_uncertain`、
  `automation_status=uncertain`，reason 使用
  `authoritative_execution_outcome_unknown`。

所有保存新 authoritative decision 的入口必须同时把 `execution_running` 与
`execution_uncertain` 视为不可覆盖。后者只有独立 reconciliation 或逐行数据修复能转出。

## 5. 新表字段与 schema 边界

> **2026-09-02 改写：schema 范围增加 wakeup 独立 fence 与 scanner cursor；三者仍属于同一个
> additive schema 步骤，但 runtime 激活和存量数据处置继续分离。**

推荐字段：

| 字段 | 用途 |
|---|---|
| `id` | 主键 |
| `raw_message_id` | FK → `raw_messages.id`，索引 |
| `authoritative_generation` | 当前 decision generation；与 raw ID 组成唯一约束 |
| `status` | 上述六态 check constraint |
| `claim_token` | 每次 ownership CAS 的随机 token；不要复用 generation 本身 |
| `owner_runtime_role` | 必须为 worker/all；防止错误角色持有 |
| `owner_instance_id` | 进程启动时生成且生命周期内不变 |
| `owner_pid` | 诊断字段，不单独作为存活证明 |
| `owner_boot_id` | Linux boot ID，排除跨重启 PID 复用 |
| `owner_process_start_ticks` | `/proc/<pid>/stat` start time，排除同 boot PID 复用 |
| `owner_systemd_invocation_id` | systemd invocation identity；无 systemd 时可空 |
| `claimed_at` | 取得 claim 的时间 |
| `heartbeat_at` | owner 周期性续租时间 |
| `lease_expires_at` | 可见性/回收扫描边界 |
| `side_effect_started_at` | 调 adapter 前必须先 CAS 持久化；NULL 才允许安全回收 |
| `outcome_recorded_at` | canonical outcome 已落库时间 |
| `automation_status` / `automation_reason` | finalize 可复用的原始 outcome，不允许推断 |
| `error_class` / `error_summary` | 有界、无敏感数据的失败证据 |
| `uncertain_at` / `completed_at` / `updated_at` | terminal 与审计时间 |

不在 `recognition_decisions` 增加 owner/heartbeat 字段。decision 是当前投影，attempt 是执行历史；
混在同一行会在新 generation 覆盖时丢失审计链。

新增表 `entry_assembly_wakeup_executions`，一条记录对应一个
`(entry_assembly_attempt_id, wake_generation)`：

| 字段 | 用途 |
|---|---|
| `id` | 主键 |
| `entry_assembly_attempt_id` | FK → `entry_assembly_attempts.id`，索引 |
| `wake_generation` | 单调 generation；与 attempt ID 组成唯一约束 |
| `strategy_raw_message_id` | 被再次执行的策略消息 ID，作为 immutable evidence |
| `trigger_raw_message_id` | 触发本次 wake 的完成消息 ID |
| `status` | `claimed/executing/outcome_recorded/succeeded/failed_safe/uncertain` |
| `claim_token` | wake ownership 的随机 fencing token |
| owner identity/lease 字段 | 与 authoritative attempt 相同的 instance/PID/boot/start ticks/invocation/heartbeat/deadline 证据 |
| `side_effect_started_at` | 调用 wakeup `auto_trade_executor()` 前 exact-token CAS 写入 |
| `outcome_recorded_at`、`result_status`、`result_json` | adapter boundary envelope 的 canonical outcome；有界且无凭据 |
| `error_class/error_summary/uncertain_at/completed_at/updated_at` | terminal 与审计证据 |

`EntryAssemblyAttempt` 仍保留现有 parent workflow 状态；`uncertain` 属于 child wake execution，parent
不得通过改回 `pending` 绕过 child fence。这样避免把既有 attempt status 枚举扩展成兼具 assembly
与 exchange ownership 两套含义。

新增独立 `recognition_execution_scan_cursors` 表，不使用内存游标或通用 setting。最小字段为
`scan_family` 唯一键、`last_seen_id`、`pass_started_at`、`wrapped_at`、`updated_at`、`version`。
至少为 `succeeded_job_running_decision`、`legacy_running_decision`、active authoritative attempt、
active wake execution 分别保存游标，不能共享一个会互相跳过行的 cursor。

这是 additive schema 变更，只新增上述三类表/索引/约束，不修改任何现有表，必须先在生产库副本
演练，再作为独立 L3 schema 动作安装。旧 runtime 完全忽略这些表。runtime 激活前必须显式验证
全部表、约束和索引存在，不能依赖 activation 时的
`Base.metadata.create_all()` 偷渡建表。

## 6. claim/finalize 的保证释放结构

> **2026-09-02 改写：本节把 adapter 返回规范化放在 lease terminalization 之前，并将 wakeup
> 第二入口纳入独立 fence。**

改动边界集中在 `authoritative_recognition.process_authoritative_message()` 当前
1433–1508 行，以及 `recognition_decisions.py` 的 claim/finalize helpers：

1. `claim_authoritative_execution()` 在同一事务中：
   - 将 exact decision generation CAS 为 `execution_running`；
   - 插入 exact generation 的 attempt=`claimed`；
   - 保存 owner identity、token、lease；
   - 任一写入失败则整个事务回滚。
2. claim 后进入一个窄的 `try/except BaseException/finally` execution scope。
3. `apply_authoritative_assessment()`、source barrier 与所有无需 exchange adapter 的 skip 分支
   保持在 pre-side-effect 状态。
4. 若要调用 `auto_trade_executor()`，必须先用 raw ID + generation + claim token +
   `status=claimed` 做 CAS，写 `status=executing` 与 `side_effect_started_at`；CAS 失败不得调用
   adapter。
5. automation outcome 一旦得到，先以 exact token 持久化为 `outcome_recorded`，再 finalize
   decision；因此 finalize 自身失败或进程在两者之间退出时，只需重放 finalize。
6. 正常 finalize 与 attempt=`succeeded` 应在同一事务完成，避免一边 terminal、一边仍 active。
7. `except BaseException` 不吞原异常：
   - attempt 仍为 `claimed`：CAS 为 `failed_safe`，同时安全终结 decision，然后 re-raise；
   - attempt 为 `executing`：CAS 为 `uncertain`，同时把 decision 冻结为
     `execution_uncertain`，然后 re-raise；
   - attempt 为 `outcome_recorded`：保留 outcome，允许 scanner 只补 finalize，然后 re-raise。
8. `finally` 只保证“不留下没有分类的 active ownership”，绝不无条件清锁。若 terminalization
   本身失败，保留原 running 和 attempt 证据，并产生高优先级 incident；不得假报已释放。

### 6.1 Adapter 返回与异常契约（2026-09-02 新增）

当前 `auto_trade_executor()` 并不是一个足以直接驱动 lease 的类型化边界：

- multi-item 汇总返回 `completed`、`in_progress`、`unknown`、`partial_failed`；
- entry 成功返回 `submitted`；准入/配置/可见性分支返回 `blocked`、`skipped`、`deferred`；
- management 分支的当前顶层返回族为 `blocked`、`deferred`、`shadow_planned`、`submitted`、
  `succeeded`、`unresolved`、`recovery_required`；composite 内部还保存 `submitting`、
  `awaiting_exchange`、`submit_unknown`、`operator_required`、`partial_failed`、`reconciling`；
- revision 分支的当前顶层返回族为 `new_thread_required`、`blocked`、`planned`、`in_progress`、
  `cancelling_old_entries`、`submitted`、`succeeded`、`reconciling`、`recovery_required`；内部 batch/
  leg 还保存 `cancel_submitting`、`submitting_replacements`、`submit_unknown` 等状态；
- item wrapper 捕获 `DeepcoinRequestOutcomeUnknown` 后把 item terminal 为 `unknown`，捕获普通
  `Exception` 后把 item terminal 为 `failed`，最终只返回汇总字符串；
- 下游可抛 `DeepcoinRequestOutcomeUnknown`、`DeepcoinDefiniteRejection`、`DeepcoinClientError`、
  `RecoveryLiveSubmitError`、`EntrySubmissionProgressError`、instruction contract conflict/blocked/
  outcome-contract errors、management/revision executor errors，以及任意未预期 `Exception`；
  cancellation、SIGTERM 协作退出属于 `BaseException` 边界，SIGKILL/掉电没有 Python 返回。

这里的“全部”是指当前代码可观察的状态族；planner/executor 若增加新的原始状态，normalizer 必须
fail closed 拒绝未登记值，不能让它落入默认成功或 `else` 分支。

新边界必须把任何原始返回或异常先转换成类型化 `ExecutionBoundaryOutcome`，至少包含：

| 字段 | 枚举/含义 |
|---|---|
| `status` | `completed/failed_safe/outcome_unknown`，不得透传任意字符串作为 lease 结论 |
| `exchange_effect` | `not_started/confirmed_applied/confirmed_rejected/outcome_unknown` |
| `raw_status`、`reason_code` | 原始状态与机器理由，仅作追溯 |
| `evidence_refs` | trade signal、item、contract、batch/binding/event 等 durable evidence ID |

归类规则：

1. `DeepcoinRequestOutcomeUnknown` 必须直接成为 `exchange_effect=outcome_unknown`；不得像当前
   `auto_trade_execution.py:360-365` 一样仅压成普通 `unknown` 返回后让上层猜测。
2. `unknown`、`partial_failed`、`recovery_required`、`in_progress`、`reconciling`、
   `operator_required`、`awaiting_exchange`、`submit_unknown`、`unresolved`，以及无法证明 exchange
   未开始的 `failed`，一律冻结 lease 为
   `uncertain`。
3. `blocked/deferred/skipped/shadow_planned/pending/planned/new_thread_required` 只有在 adapter 同时
   提供 durable `exchange_effect=not_started` 证据时，才能以原 canonical automation status 进入
   `outcome_recorded`；不得把正常 block/skip 伪装成失败。`failed` 只有在 exact-token attempt 仍为
   pre-boundary 且证据明确时才能 `failed_safe`。状态名称本身不构成证明。
   `cancelling_old_entries`、`cancel_submitting`、`submitting_replacements` 已进入 revision 外部副作用
   流程，缺少逐 leg confirmed outcome 时不得视为 pre-boundary。
4. `submitted/completed/succeeded` 只有在明确给出 `confirmed_applied` 或可验证的 canonical
   outcome evidence 时，才能进入 `outcome_recorded` 后 finalize；缺证据同样 `uncertain`。
5. `DeepcoinDefiniteRejection` 只有在 adapter 明确证明请求被交易所确定拒绝且无任何其他 leg 已
   attempted 时，才是 `confirmed_rejected`；多 leg 中任一 leg 边界不清楚仍为 unknown。
6. 任意未登记状态、缺字段、normalizer 自身异常、普通异常、cancel/system exit 在
   `side_effect_started_at` 之后均为 `uncertain`；边界之前则只有 exact-token 证据仍为
   `claimed` 才可 `failed_safe`。
7. SIGKILL/掉电无返回，由 scanner 根据 durable side-effect phase 分类；绝不根据 job 的
   succeeded/failed 推断 exchange outcome。

`web_app.py`/worker 构造的 executor wrapper 不得再把异常压成无边界信息的普通
`{"status":"failed"}`。它必须 re-raise 原异常，或返回上述完整 envelope；lease 层只消费
envelope，不直接解释 legacy 状态字符串。

### 6.2 Entry-assembly wakeup 独立持久 fence（2026-09-02 新增）

`authoritative_recognition.py` 当前对 `wake_claim.strategy_raw_message_id` 的第二次 executor 调用，
与正在处理的 `raw_message_id` 不是同一个 authoritative generation，因此不得复用主 attempt 行。
新顺序为：

1. `claim_ready_entry_assembly_wakeups()` 在同一事务中 exact-CAS parent attempt，并插入新的 child
   wake execution=`claimed`；重复 `(attempt_id,wake_generation)` 由唯一约束拒绝。
2. 调 wake executor 前，必须按 attempt ID + generation + token + `status=claimed` exact CAS
   为 `executing` 并写 `side_effect_started_at`；CAS 失败不得调用 adapter。
3. adapter 返回先按 6.1 规范化并持久化 `outcome_recorded`，之后才把 parent 标为 `woken`；两者
   分事务时 scanner 只补本地 parent finalize，不重调 adapter。
4. pre-boundary 异常可以 exact CAS 为 `failed_safe`；post-boundary 异常/未知返回只能
   `uncertain`。所有 except 必须 re-raise，不能因 parent workflow 需要继续而吞掉 ownership 失败。

`entry_assembly_admission.py:653-675` 的五分钟回收改为：

- child=`claimed`、`side_effect_started_at IS NULL`、lease 过期，且 owner identity 已确认不在运行：
  exact CAS child→`failed_safe` 后，才允许 parent 回 `pending`；旧 token 已撤销，旧持有者无法跨界；
- child=`executing` 或 `side_effect_started_at IS NOT NULL`：child→`uncertain`，parent 保持
  `claimed`/不可 claim，产生 incident；**不得改回 pending**；
- child=`outcome_recorded`：只补 parent→`woken`，executor 调用次数必须为 0；
- legacy `claimed` parent 找不到 child fence：证据不足，保持不可 claim 并告警；不得沿用现有
  五分钟无条件 reset。

当前 TradeSignal CAS 已能阻止重复 entry 写，但新 fence 仍是必须项：它把第二入口本身变成可证明
的安全边界，覆盖未来 adapter 重构、非 entry 分支及下游状态变化，不再把安全性隐式委托给下游。

### 6.3 SIGTERM process-wide drain（2026-09-02 新增）

drain 不是 queue worker 的局部计数器，而是 worker/all 进程共享的 execution registry：

- 在 `process_authoritative_message()` 的主 authoritative attempt 和每个 wake child attempt 外围注册
  exact token；registry 记录底层 execution future/线程是否真正结束；
- queue message-processing worker、inline listener、context reanalysis/reconcile，以及能直接进入
  同一 authoritative/executor 边界的 worker command/人工触发入口，必须共用同一 admission gate；
- SIGTERM 第一步关闭所有上述入口的新 claim/admission；已经入场的工作继续执行；
- 对 `asyncio.to_thread()` 保存并等待底层 callable 的 completion signal。取消 await task 只表示
  waiter 被取消，不表示线程或 exchange 调用结束，不能从 registry 移除；
- drain 同时等待 process-wide registry=0、durable attempts 不再由本 invocation 持有，以及现有
  `MessageProcessingActivity.active=0`；任一证据缺失即不宣称 drained；
- bounded timeout 到达时不清理 `executing`，也不把它变为可重放。进程随后被 systemd kill 的，
  重启 scanner 只能将 post-boundary attempt/wakeup 冻结为 `uncertain`。

### SIGTERM、SIGKILL 与掉电

> **2026-09-02 改写：以下为 6.3 的关停结果约束，不再只覆盖 queue worker。**

- SIGTERM：按 6.3 停止 queue、inline listener、reconcile/context 和 command 入口的新 claim，
  等待 process-wide registry、本 invocation durable attempts 与 MessageProcessingActivity 三类证据
  同时清零。当前 `web_app.py:5159` 的直接 cancel 改为 bounded process-wide drain。
- drain 内普通 cancellation/exception 仍走上述 BaseException 分类。
- drain 超时后不得把 `executing` 变成可重放；若 systemd 最终 SIGKILL，重启后的 scanner 按
  durable phase 处理。
- SIGKILL/掉电：Python finally 不会执行。新进程只允许回收 pre-side-effect `claimed`；
  `executing` 变 `uncertain`；`outcome_recorded` 只补 finalize。

## 7. 如何证明“孤儿”而不是“仍在执行”

不能靠 `comparison_claim_token`、年龄或 PID 单项判断。判据分两层：

### Owner/liveness 证据

要求 lease 已过期、heartbeat 已陈旧，并比较 owner instance、systemd invocation、boot ID、PID
start ticks。owner 明确仍活且 attempt 仍 active 时只告警，不回收。

### 防重复的决定性证据

- `status=claimed AND side_effect_started_at IS NULL` 才能进入 safe reclaim CAS。
- reclaim CAS 必须匹配 exact raw/generation/claim token/status，并撤销旧 token。
- 所有旧持有者在调用 adapter 前都必须执行同一 exact-token boundary CAS；token 被撤销后旧
  持有者不能跨界。即便 liveness 判断滞后，也不会形成两个执行者。
- `side_effect_started_at IS NOT NULL` 时，owner 死亡也不证明外部写未发生；只能
  `uncertain`，绝不自动重试。
- `outcome_recorded` 只重做本地 finalize，不调用 adapter，因此不会重复外部动作。

换言之，owner identity 用于减少误报和证明持有者已消失；真正保证不重复的是 durable
side-effect boundary 与 fencing CAS。

## 8. 现存 29 行的逐行处置设计

存量行没有新 attempt 表的 owner/phase 证据，scanner 不得自动接管。处置必须作为独立 L3
数据修复，逐行生成 immutable plan/preimage，并再次确认目标集合恰为 29。

每一行的必需证明：

1. exact raw ID、`execution_running`、automation NULL、exact generation token；
2. 对应 message job 与 context attempt 均已 terminal，且没有当前 in-flight owner；
3. 当前 generation candidate/batch 为 0；
4. direct binding/order leg/execution event、management envelope/target 为 0；
5. 不存在可 claim 的 active instruction item；
6. 管理语义行另做完整 worker GET 只读交易所归属与历史核对，外部证据不完整则不处置；
7. 每行 CAS 只更新 exact preimage；任一 predicate 漂移时该行零写并退出重新评审。

当前 28 行无 active instruction item。raw `14214` 的唯一 item 已在当前 claim 前 terminal
`succeeded`，因此没有可再次执行的 item；它仍需在 repair manifest 中单列，不与其余 28 条
用一条宽泛 UPDATE 混写。

修复目标值应与新状态机的 `failed_safe` 保持一致：保存“本 generation 未完成 automation、
已证明无本次执行副作用”，不得伪造业务成功。每行均保留原 payload/token/preimage 到 root-owned
证据文件。

若实施前任何行出现 current-generation execution 证据，或发现已实际执行但 automation 缺失，
立即从批次剔除并单独授权 reconciliation；不得和 pre-execution 行一起终结。

## 9. 可见性与 traceback 保留

> **2026-09-02 改写：所有 bounded scan 增加 durable keyset cursor，禁止固定 oldest-limit
> 饥饿。**

新增主动检测至少覆盖：

- `message_processing_jobs.status=succeeded` 且 decision 仍
  `execution_running/execution_uncertain`；
- active attempt 超过 lease；
- owner identity 不再存活；
- attempt=`executing` 失联；
- attempt=`outcome_recorded` 但 decision 未 finalize；
- exception terminalization/finalize CAS 失败。

scanner 输出计数、raw ID、generation fingerprint、phase、age、owner identity 摘要，不包含消息
正文或凭据；写入现有 runtime incident/系统操作者告警路径，并做 once-only fingerprint 防重复。

每个 scan family 使用第 5 节的独立 durable cursor：

1. 查询固定为 `id > last_seen_id ORDER BY id ASC LIMIT :limit`，不用 OFFSET，也不反复查询最旧的
   `LIMIT N`；
2. 每处理完一行（包括已经生成 incident 的 poison row）就以 cursor version exact CAS 推进，避免
   一条坏记录阻塞后续所有行；incident 写入与 cursor 推进要有可重入 fingerprint，崩溃重做不得
   重复通知或漏行；
3. 当前 pass 查不到更大 ID 时记录 pass complete，再把 cursor wrap 到 0；wrap 与 pass generation
   一起持久化，避免两个 scanner 一个从头、一个从尾交叉覆盖；
4. scan 期间插入的更大 ID 会在本 pass 后续页或下一 pass 被覆盖；老 ID 状态变化由周期性 wrap
   后重新检查；
5. `succeeded job + decision execution_running/execution_uncertain` 与 legacy running 必须使用
   各自 cursor。积压超过单次 limit 时，靠后的行必须在有限个扫描周期内被看到；验收测试以
   `limit+K` 数据证明最后一行可达。

同时修改 `run_context_resolution_once()`：在保存 retry 状态前记录包含 raw ID、attempt ID 与
完整 traceback 的 `logger.exception`；返回 retry 语义保持不变。外层 message job 不应因为它
代跑的另一条 context item 失败而被错误改成 failed，但必须由 orphan scanner 独立发现。

## 10. 与 backlog expiry 的关系

`BacklogExpiryRefused` 保护必须保留并扩展：

- 继续拒绝目标集中任何 `execution_running`；
- 同时拒绝任何 `execution_uncertain` 或 attempt=`executing/uncertain/outcome_recorded` 且尚未
  reconcile 的行；
- 只有经 exact evidence terminalize 为 `failed_safe/succeeded` 的行不再阻塞；
- 不允许 expiry 命令顺手清理、跳过或改写这些状态。

保护语义不变：维护动作不能越过可能正在执行或结果未知的权威操作。

## 11. 分步风险、激活与回滚

### 步骤 S：additive schema（L3）

- 只新增 `authoritative_execution_attempts`、`entry_assembly_wakeup_executions`、
  `recognition_execution_scan_cursors` 表及其索引/约束，不改现有表与业务数据。
- 生产库副本演练，保留 backup SHA-256、`PRAGMA quick_check`、foreign-key check、受影响表和
  关键业务表前后计数。
- 生产 schema 动作与 runtime activation 分开；schema 完成后旧 runtime 继续运行。
- 回滚：在任何新 runtime 使用这些表前可直接 drop；一旦已有 attempt/wakeup/cursor，先导出并
  校验，再先回滚 runtime、后 drop 新表，避免 `create_all()` 重建。drop 顺序为 child wake execution
  与 cursor、authoritative attempt；不触碰现有业务表。

### 步骤 C：新状态机 runtime（L3）

这会改变 exchange-capable 执行的 failure/cancellation 语义，按最高风险 L3，而不是把它降为
普通异常处理。

- schema 已验收后才允许 worker/all activation；Web/ingest 无需持有或扫描 attempt。
- 初次激活时 scanner 只观测新 attempt/wakeup；legacy running scanner 只告警，不自动接管现存
  29 行。
- 完整测试必须覆盖每个异常注入点、CAS 竞态、SIGTERM drain、模拟 SIGKILL、pre/post boundary
  lease、outcome-recorded finalize 和 backlog guard。
- 回滚：停止新 claim，等待/分类本 candidate 产生的 active attempts；任何 executing/uncertain
  未 reconcile 时禁止回滚。安全后激活原 worker runtime；新表保留只读，不删，旧的 29 行仍按
  原 fail-closed 状态存在。

### 步骤 D：存量 29 行数据处置（L3，单独授权）

- 只在步骤 C 稳定观测通过后进行；每行 exact CAS，不用批量无谓改写。
- 保留生产 backup、29 行 preimage、action manifest/fingerprint、外部只读证据、前后计数与
  quick check。
- 回滚：使用 exact repair receipt 恢复每行原 preimage；恢复后这些行重新成为
  `execution_running` 并继续阻塞重识别/expiry，这是已知且预期的 fail-closed 回滚状态。

三个步骤必须分别授权、分别留证、分别提交/执行，不得把 schema、runtime activation 与数据
repair 合并为一个动作。

## 12. 验收不变量

> **2026-09-02 改写：增加 adapter envelope、wakeup fence、process-wide drain 与 cursor
> 不变量。**

- 任一 generation 最多有一个 active attempt/token。
- 未成功写 durable side-effect boundary，不得调用 exchange-capable adapter。
- 已写 side-effect boundary 的失联尝试永不自动重放。
- 原始 adapter 字符串状态不得直接决定 lease 终态；未登记状态、缺失
  `exchange_effect`、`DeepcoinRequestOutcomeUnknown` 和一切 post-boundary 不确定结果均变为
  `uncertain`。
- `outcome_recorded` recovery 只能本地 finalize，exchange adapter 调用次数保持 0。
- 任一 `(entry_assembly_attempt_id, wake_generation)` 最多一个 active wake token；wake executor
  调用前必须持久化 child side-effect boundary；post-boundary child 不得被五分钟逻辑 reset 为
  pending。
- SIGTERM 对 queue、inline listener、reconcile/context、command 入口停止新 admission，并等待
  `to_thread` 底层工作而非只等待 asyncio task；SIGKILL 模拟只产生 safe pre-boundary failure 或
  post-boundary uncertain，不产生第二次 adapter 调用。
- `execution_uncertain` 永久阻止新 authoritative overwrite 与 backlog expiry，直到独立
  reconciliation。
- 自动检测能在一个完整 keyset scan pass 内报告“job succeeded + decision running/uncertain”；
  构造超过单页 limit 的数据时，最后一页不会饥饿；每个 scan family 的 cursor 互不干扰。
- 标准成功、authoritative_failed、block/hold、无 candidate、executor 未配置、executor
  failed/unknown 的既有业务结果保持不变。

本设计文档不授权实施。
