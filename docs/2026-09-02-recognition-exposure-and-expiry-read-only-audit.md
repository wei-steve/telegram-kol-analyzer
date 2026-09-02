# 识别异常、实盘敞口与静默过期只读核查

## 紧急结论

本次未发现用户指定的 lifecycle 存在“实盘仍有仓但缺少保护单”的敞口，因此没有触发“立即停止后续核查”门禁。

- Deepcoin 只读快照完整返回 1 个实盘仓位：`BTC-USDT-SWAP` 空头 4 张，`pos_id=1001125090990141`，唯一绑定 lifecycle 1054。它不在本次 11 条无候选消息指向的目标集合中。
- lifecycle 1054 当前有 4 条已验证保护：2 条止盈各覆盖 2 张，2 条止损均覆盖剩余 4 张。13:31 与 13:56 UTC 两次快照的持仓数、普通挂单数和 fingerprint 一致。
- lifecycle 1039、1040、1043、1044、1049、1051、1053 均无对应实盘仓位；因此这七个目标没有“实盘持仓缺保护”。其中 1043、1044、1051、1053 的本地 `lifecycle_status=entered`，但无 execution binding，交易所也没有对应仓位；这是本地状态残留，不是已确认实盘敞口。
- `raw_message_id=14378` 的无接纳原因为 NULL 已定位到具体路径：decision 停在 `comparison_status=execution_running`，作业五次重试均命中 `authoritative execution is already in progress`，最终为 `failed/processing_error:RuntimeError`；因为 automation 没有走到 finalize，`automation_reason` 保持 NULL，Web 的 `system_acceptance.reason_code` 也就是 NULL。
- 全历史中共有 408 条 `expired_stale_instruction`，其中 400 条（98.04%）是有效 text/image/text+image 输入。363/408（88.97%）在过期时没有任何 MiMo run 或 evidence；这部分是“根本没跑到识别”，不是“跑完但没显示”。

本轮只读、无部署、无重启、无设置或配置变更、无数据库写入，也没有任何交易所下单、改单或撤单。

## 范围、口径与只读边界

- 数据库固定快照范围：`raw_messages.id` 1–14482，共 14,482 条；`posted_at` 从 2025-10-06 09:25:46 UTC 到 2026-09-02 13:42:43 UTC。历史数量可承受，因此采用全部历史，而不是只取 30 天。
- SQLite 通过 URI `mode=ro` 打开，强制 `PRAGMA query_only=ON`、`temp_store=MEMORY`、1 秒 busy timeout；每轮使用短时只读事务，未建表、未建临时表、未 `VACUUM`。
- 交易所证据只从 worker 角色 `127.0.0.1:8002` 的 GET 只读接口取得，使用了 `/api/runtime-agent/read-only-exchange-snapshot`、`/api/runtime-incidents/live-position-sizes`、`/positions-panel` 和 `/positions-panel/tabs/open-orders`。
- 运行身份（2026-09-02 13:30:35 UTC）：Web=`b78f16098c591978fe764e15c9b793182fc97f5b`，ingest/worker=`0de19c1cbb2089fd58b8940d9b01a65096f9a063`；三者 `loaded_artifact_verified=true`。
- 证据输出只保留计数、比例、时间、raw message ID、lifecycle ID 和交易结构字段；未输出消息正文、群组/KOL 名称、发送者或图片内容。

## 1. lifecycle 1053 及同类目标的真实敞口

### 1.1 本地 lifecycle 与执行/保护投影

| lifecycle ID | 当前本地状态 | 方向 | execution binding | execution order legs | protection legs | protection ledger | Deepcoin 当前对应仓位 | 当前保护判定 |
|---:|---|---|---:|---:|---:|---:|---|---|
| 1039 | `pending_entry` | ETH short | 0 | 0 | 0 | 0 | 无 | 无实盘仓位，不适用 |
| 1040 | `exited` | BTC long | 0 | 0 | 0 | 0 | 无 | 无实盘仓位，不适用 |
| 1043 | `entered` | BTC long | 0 | 0 | 0 | 0 | 无 | 本地 entered 不等于实盘有仓 |
| 1044 | `entered` | BTC short | 0 | 0 | 0 | 0 | 无 | 本地 entered 不等于实盘有仓 |
| 1049 | `exited` | BTC short | 1（closed） | 1（manually_closed） | 3（历史 verified） | 3（历史 verified） | 无 | 历史保护记录不代表当前挂单；当前无仓 |
| 1051 | `entered` | BTC short | 0 | 0 | 0 | 0 | 无 | 本地 entered 不等于实盘有仓 |
| 1053 | `entered` | BTC long | 0 | 0 | 0 | 0 | 无 | 本地 entered 不等于实盘有仓 |

lifecycle 1053 的完整当前结构是：`lifecycle_status=entered`，execution binding 0、execution order leg 0、position protection leg 0、position protection ledger 0。三条 `exit_position` 消息 14378、14384、14410 发生时，1053 的本地时序状态均为 `entered`；但本次 Deepcoin 实时只读证据确认它没有对应仓位。

lifecycle 1049 的历史 binding 为 `closed`，记录的 `pos_id=1001125090325378`、`last_exchange_status=manual_closed_or_not_found_on_exchange`；当前 Deepcoin 快照中无该仓位。

`raw_message_id=14244` 的 payload 未记录 target lifecycle；它也没有 reply target、`StrategyMessageLink` 或 `ContextResolutionAttempt` 可以只读反解出目标。因此其目标是“需进一步确认”，本报告不作推断。

### 1.2 Deepcoin 实盘快照

| 字段 | 只读结果 |
|---|---|
| 快照完整性 | `complete=true` |
| 实盘仓位数 | 1 |
| 普通未成交挂单数 | 0 |
| 唯一仓位 | BTC-USDT-SWAP short，4 contracts，`pos_id=1001125090990141` |
| 唯一归属 | lifecycle 1054，attribution `bound` |
| 止盈 | 2 条 verified：75400/2 张，76100/2 张 |
| 止损 | 2 条 verified：79300/4 张，79458.6/4 张 |
| 本次目标 lifecycle 实盘持仓 | 0 |
| 本次目标 lifecycle 实盘缺保护 | 0 |

`positions-panel` 在 list/group 投影中会重复显示同一卡片，本报告按 `pos_id` 去重；交易所仓位总数以完整的 worker 快照为准，不把 DOM 卡片数当仓位数。

**判定：已确认无本次目标实盘无保护敞口。** 本地 `entered` 残留与 Deepcoin 真实持仓已分开报告，没有将前者当成后者。

## 2. `非策略 + event_type!=none` 的真实范围

### 2.1 语义口径先校正

按数据组合本身统计，全历史中共 835 条 payload 同时满足 `recognition_result=非策略` 且 `lifecycle_event.event_type` 非 `none`，占全历史 14,482 条的 5.77%。该组合第一次出现于 2026-07-13 06:35:29 UTC；在它首次出现至快照终点的 8,768 条消息中，占 9.52%。

但按当前代码和 prompt 的确切语义，不能把这 835 条全部定性为“payload 自相矛盾”：

- `ai_recognition_config.py:189-191` 明确定义“新开仓识别”与“已有策略生命周期事件识别”是两个独立维度，并明确允许“非策略 + exit_position”。
- 835 条中，415 条（49.70%）最终为 `skipped/mimo_authoritative_not_safely_applied`；另 420 条（50.30%）走了 completed、submitted、in_progress、deferred 或其他非该 fail-closed 结果。

因此，“出现这个字段组合”与“候选未安全落地”是两个不同口径。前者是 prompt 明确允许的结构，后者才是需要在 835 条中单独跟踪的子集。

### 2.2 event type 分布

| event type | 条数 | 占 835 条 | 其中有消息时点活跃 target lifecycle |
|---|---:|---:|---:|
| `position_update` | 515 | 61.68% | 452 |
| `exit_position` | 262 | 31.38% | 82 |
| `cancel_entry` | 30 | 3.59% | 20 |
| `entry_confirm` | 28 | 3.35% | 26 |
| `close_signal` | 0 | 0.00% | 0 |
| **合计** | **835** | **100.00%** | **580** |

- 580/835（69.46%）至少指向一个在消息发生时处于 `pending_entry` 或 `entered` 的 lifecycle。
- 110/835（13.17%）没有记录 target；剩余 145 条只指向当时已终态或时序无法确认的 target。
- 按 target 引用数计，835 条中共有 778 个 lifecycle 引用：629 个（80.85%）在消息时点活跃，148 个当时已终态，1 个时序无法确认。
- 这些引用指向 378 个不同 lifecycle；当前状态为 `exited` 314、`expired` 45、`invalidated` 4、`pending_entry` 9、`entered` 6。这是本地 lifecycle 状态，不等同于交易所实盘持仓。

### 2.3 按周时间分布

| ISO 周（UTC） | 当周 raw messages | 该字段组合 | 当周占比 |
|---|---:|---:|---:|
| 2026-W29 | 1,056 | 157 | 14.87% |
| 2026-W30 | 1,043 | 158 | 15.15% |
| 2026-W31 | 1,221 | 103 | 8.44% |
| 2026-W32 | 1,049 | 102 | 9.72% |
| 2026-W33 | 1,074 | 98 | 9.12% |
| 2026-W34 | 1,577 | 77 | 4.88% |
| 2026-W35 | 1,252 | 117 | 9.35% |
| 2026-W36（至 09-02 13:42:43） | 496 | 23 | 4.64% |

**已确认：** 该组合自 2026-07-13 起持续存在，不是 2026-09-01/02 才出现的新现象。引入统一 MiMo 策略/生命周期 prompt 的 commit `af84cdb37fcf6869d1eecf1ad6cfc31e07cefec2` 提交时间是 2026-07-13 05:23:36 UTC，第一条数据在约 72 分钟后出现。仓库中没有保留当时可校验的 immutable 生产激活身份，因此只能说时间对齐，不能据此断言因果。

### 2.4 `raw_message_id=14378` 的 NULL reason 路径

| 结构字段 | 当前值 |
|---|---|
| lifecycle event | `exit_position -> lifecycle 1053` |
| decision | `authoritative_status=非策略`, `agreement_status=pending` |
| execution state | `comparison_status=execution_running` |
| automation | `automation_status=NULL`, `automation_reason=NULL` |
| processing job | `failed`, `attempt_count=5`, `last_reason=processing_error:RuntimeError` |
| MiMo runs | 5 个 `v1_authoritative/completed/became_authoritative=true` |
| candidate / management envelope | 0 / 0 |

worker journal 中五次失败都是 `save_pending_authoritative_decision()` 看到既有 decision 已为 `execution_running`，于是抛出 `RuntimeError("authoritative execution is already in progress")`。`_serialize_system_acceptance()` 的 reason 直接来自 `RecognitionDecision.automation_reason`；这条 decision 从未 finalize automation，所以 Web 显示 NULL 是对当前持久化状态的如实投影。

**已确认：** NULL reason 不是独立的候选拒绝分支，而是这条 decision 卡在 `execution_running` 导致 automation 结果未落库。

**需进一步确认：** 现有 journal 只保留了五次看到“已在执行”的失败栈，没有保留最初将该 decision 留在 `execution_running` 的异常栈；不能从当前数据反推最初异常。

## 3. `expired_stale_instruction` 的范围、起点与中间结果

### 3.1 总量和时间分布

全部 14,482 条历史消息中：

- `expired_stale_instruction` 408 条，占 2.82%；第一条发于 2026-08-21 08:47:41 UTC，最后一条发于 2026-09-01 13:33:53 UTC。
- `expired_after_system_stall` 3 条，占 0.02%；raw message ID 为 12321、12322、12544。
- 在 411 条全部过期中，408 条（99.27%）走 `expired_stale_instruction`，3 条（0.73%）走 `expired_after_system_stall`。
- 全历史在 2026-08-21 之前没有这两类过期记录；它不是从历史起点就一直按同一比例发生。

| 消息日（UTC） | 当日消息 | stale expiry | 占当日 | stall expiry | 占当日 |
|---|---:|---:|---:|---:|---:|
| 2026-08-21 | 270 | 48 | 17.78% | 0 | 0.00% |
| 2026-08-22 | 266 | 35 | 13.16% | 2 | 0.75% |
| 2026-08-23 | 221 | 35 | 15.84% | 1 | 0.45% |
| 2026-08-24 | 243 | 0 | 0.00% | 0 | 0.00% |
| 2026-08-25 | 220 | 0 | 0.00% | 0 | 0.00% |
| 2026-08-26 | 165 | 0 | 0.00% | 0 | 0.00% |
| 2026-08-27 | 203 | 0 | 0.00% | 0 | 0.00% |
| 2026-08-28 | 192 | 0 | 0.00% | 0 | 0.00% |
| 2026-08-29 | 107 | 1 | 0.93% | 0 | 0.00% |
| 2026-08-30 | 122 | 110 | 90.16% | 0 | 0.00% |
| 2026-08-31 | 186 | 171 | 91.94% | 0 | 0.00% |
| 2026-09-01 | 180 | 8 | 4.44% | 0 | 0.00% |
| 2026-09-02（至 13:42:43） | 130 | 0 | 0.00% | 0 | 0.00% |

**已确认：** stale expiry 集中在两段窗口：8 月 21–23 日共 118 条，8 月 30 日至 9 月 1 日共 289 条；其中 8 月 30–31 日分别覆盖当日 90.16% 和 91.94% 的消息。8 月 24–28 日连续五天为 0，因此不是稳定基线。

### 3.2 过期时是否已经跑过识别

口径使用 run/evidence 的 `created_at` 与 processing job `completed_at` 比较，防止把过期后的重分析误算成过期前证据。

| 过期时证据状态 | 消息数 | 占 408 条 | 判定 |
|---|---:|---:|---|
| 无 MiMo run、无 evidence | 363 | 88.97% | 完全没跑到识别 |
| 有已完成且成为权威的 MiMo run | 45 | 11.03% | 跑过识别，但作业仍被年龄门禁终结为过期 |
| 其中已有 evidence version | 43 | 10.54% | 有可见的中间证据 |
| 有 run 但无 evidence/decision 已创建证据 | 2 | 0.49% | 中间落地不完整 |

45 条消息在过期前合计留下 112 个 `v1_authoritative/completed/became_authoritative=true` run。其中 43 条的 decision `created_at` 也早于过期时刻；但 `RecognitionDecision` 是可更新单行，当前行不保留被 recovery guard 覆盖前的 payload 历史，不能从现库还原这 43 条当时的完整权威 payload。

按代码中与 `_message_input_kind()` 相同的判据，408 条的输入为 text 185、image 156、text+image 59、empty 8；400/408（98.04%）是可处理输入。“过期时没有 run/evidence”的 363 条中，355 条是可处理输入，8 条是 empty。NULL 没有被当成零或正常值。

当前状态方面：403/408（98.77%）仍是 `recovery_guard` payload；5 条当前已有 `recognition_result`。其中 4 条（13995、14020、14026、14126）在过期后出现了新 MiMo run；13764 在过期后使用既有 run 更新了 decision。这 5 条证明个别消息可被其他事件驱动的重分析覆盖，不能据此认定 408 条存在通用自动补跑。

### 3.3 现有恢复机制与未生效边界

只描述当前代码实际存在的路径：

1. `_load_gap_recovery_candidates()` 只查找“还没有 `RecognitionDecision`”的 raw message，并按当前 15 分钟窗口分成可恢复和已过期。
2. 生产当前 `message_pipeline_mode=queue`。在 queue 模式下，gap recovery 只尝试 enqueue 后立即返回；worker 在实际处理前先用 15 分钟年龄门禁检查。
3. 一旦超龄，worker 先写入 fail-closed `recovery_guard` decision，再把 job 终结为 `expired`；下一轮 gap query 因为 decision 已经存在，不再选中该消息。
4. queue enqueue 的 UPSERT 只会接管符合条件的 shadow job，不会将已终态的 `shadow=false` 权威 job 重置为 pending。因此已过期权威 job 没有通用自动重放路径。
5. context resolution worker 确实可对已有 unresolved context attempt、命中声明 trigger 且仍符合资格的单条消息做 reanalysis。这是有条件的 context 重分析，不是对 `expired_stale_instruction` cohort 的通用补跑。

**已确认：** 408 条 stale expiry 中的 403 条至今仍保持 recovery guard；已过期 job 的终态和已存在 decision 共同使它们退出通用 gap recovery 选择集。

### 3.4 安静分类与告警路径的实际差异

`_classify_expired_authoritative_recovery_gap()` 的注释与实现一致：只有记录到的 event-loop stall 时间落在消息生命窗口内才分为 `expired_after_system_stall`；缺 posted time、缺 snapshot、缺 stall 记录或无法证明重叠时，都默认为 `expired_stale_instruction`。因此 408 条不是系统已证明它们是“过时业务指令”，而是无充分 stall 重叠证据时的兜底归类。

同时，当前 queue 模式存在一个需要如实记录的代码路径差异：

- 非 queue 的 `recover_missing_authoritative_decisions()` 分支会收集 `expired_after_system_stall` 并调用限流的 system-operator notification sender。
- queue 模式在 `telegram_live_listener.py:1209-1222` enqueue 后立即返回；过期由 `message_processing_worker.py:571-593` 处理。该 expiry 分支记录 decision 和终结 job 后直接 return，没有调用 stall-expiry notification sender。worker 的 `terminal_failure_notifier` 只在异常重试耗尽分支被调用，不覆盖 expiry 分支。

**已确认：** 同期并非长期为零，而是有 3 条 `expired_after_system_stall`。但就当前生产 queue 路径的代码而言，这 3 条的分类存在不等于已调用 system-operator notification sender；现有 journal 也没有可证明这 3 条已送达的记录。通知是否曾由代码外的其他观测路径触发，需进一步确认。

## 已确认与需进一步确认

| 事项 | 判定 |
|---|---|
| lifecycle 1053 本地 entered | 已确认 |
| lifecycle 1053 当前 Deepcoin 实盘仓位 | 已确认为无 |
| 11 条目标消息涉及的已知 lifecycle 实盘无保护敞口 | 已确认为 0 |
| raw 14244 的 target lifecycle | 需进一步确认；当前无可用结构化链接 |
| 835 条字段组合是否全部属于模型矛盾 | 已确认不能如此定性；prompt 明确允许该组合 |
| 415 条 `mimo_authoritative_not_safely_applied` | 已确认为该组合中的 fail-closed 子集 |
| raw 14378 的 NULL reason | 已确认由 `execution_running` 卡住、automation 未 finalize 导致；最初卡住原因需进一步确认 |
| 408 条 stale expiry 是否都没跑过 | 363 条过期时无 run/evidence；45 条已有 authoritative run |
| stale expiry 是否长期稳定发生 | 已确认为否；主要集中在两段窗口 |
| `expired_after_system_stall` 是否为零 | 已确认为 3，不是零 |
| queue 过期分支是否直接发送 stall-expiry 通知 | 已确认为否；是否有其他外部路径曾告警需进一步确认 |

本报告不提出修复方案，不对任何 lifecycle、仓位、保护单、识别结果或 processing job 做处置。
