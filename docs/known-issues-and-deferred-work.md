# 已知问题与延期事项

本页只汇总指定文档中仍然成立、但尚未处理或尚未达到接管条件的事项。历史上曾列为 `Outstanding`、但后文已经完成或被明确取代的恢复步骤不再重复收录。下列内容不是实施授权；处理任何一项前仍需按其风险等级另行确定范围。

## 影响实际交易

| 事项 | 当前影响 | 发现来源 | 建议处理时机 | 处理前必须确认 |
|---|---|---|---|---|
| **平仓后的保护投影与本地 lifecycle 状态可能残留。** 文档分别记录了陈旧的 `position_protection_legs` 753–755、平仓后仍为 `verified` 的 protection-ledger 598–601，以及 lifecycle `1043`、`1044`、`1051`、`1053` 本地仍为 `entered`、但 binding/order leg/protection leg/protection ledger 均为 0 且交易所无对应仓位。准确目标行范围在修复前需重新确认。 | 这些记录不是已证实的裸仓，但本地状态与交易所事实不一致；陈旧投影曾误伤部署门禁，未来管理/归属逻辑还可能阻塞或错误选择目标。 | `docs/ai-context-resolution-optimization-status.md`：**Read-only live-position protection audit and P0 checkpoint / Exchange position and current protection**、**R1 activation stopped at the fresh protection-ledger gate**；`docs/2026-09-02-recognition-exposure-and-expiry-read-only-audit.md`。 | 下一次依赖本地 lifecycle/保护簿记的部署门禁之前；或下一次真实持仓进入管理/保护替换前。 | 新鲜且完整的交易所持仓/普通单/逐合约触发单快照；确认当前权威表和准确目标行；定义无实盘仓位时 lifecycle 与保护投影的既有终结语义；数据库备份、单事务和回滚边界。 |
| **Shadow 收紧判据仍不能接管权威。** 历史回放虽召回 25/25 个关键漏失和 474/474 次实质改变，生产也已有少量真实 shadow 行，但证据量仍不足。 | 现状不影响交易，因为旧判据仍是唯一权威；若过早切换，可能漏掉加仓、平仓、移动止损或保护利润等管理语义。 | `docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**18.7 推荐与证据门槛**；`docs/ai-context-resolution-optimization-status.md`：**Main-recognition observability deployed and live-verified**。 | 持续积累生产 shadow 样本后，再单独评审是否进入权威切换设计；在此之前不实施节省。 | 固定词表后进行未参与选词的留出回放；人工标注关键样本；生产 shadow 对动作族、目标 thread、适用性和风险收缩保持 100% 一致；实质改变召回必须为 100%。 |
| **47 条不同 raw message 尚待人工标注。** 它们对应联合反事实会漏失的 53 个 attempt，需要区分“纠正第一层漏识别”“仅改变目标”和“风险收缩”。现已有带标注时快照与 provenance 的所有者标注入口，但这 47 条积压本身仍未清理。 | 缺少领域人工标签会使词表覆盖与收紧规则的安全结论依赖同一批历史数据自证，不能据此改变交易判据。 | `docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**17.5 B 与联合规则的全部漏失样本**、**17.6 结论与上线证据门槛**。 | 在任何 shadow 判据权威化、词表收紧或 prompt 精简之前。 | 冻结这 47 条的确切清单与标注口径；由所有者/领域人工完成标注；保留原始消息、图片证据、第一层与上下文后结论，避免用模型输出代替人工真值。 |
| **29 条 recognition decision 永久停在 `comparison_status=execution_running` 且 automation 从未 finalize。** 早期对账的 28 条之后，截至 `2026-09-02T15:57:55Z` 又新增 raw message `14497`，说明该状态仍在自然增长。 | 这些行会阻止同一消息再次写入权威 decision：`recognition_decisions.py:90/202` 抛出 `authoritative execution is already in progress`；也会使积压过期在 `message_processing_backlog_expiry.py:149-154` 以 `BacklogExpiryRefused("execution_running_decision_present")` 拒绝。27/29 对应 processing job 已是 `succeeded`，因此主要表现为静默残留；其余 2 条为 `failed`。 | `docs/2026-09-02-recognition-exposure-and-expiry-read-only-audit.md`；`docs/2026-09-02-execution-running-read-only-audit.md`。 | 单独设计租约回收与历史修复任务时；本条只记录，不授权解锁、重跑或改写。 | 固定当时的全部清单、claim token 与 message-time lifecycle 证据；证明合法状态转换及幂等边界；排除任何已产生或可能影响实盘管理的执行。 |

## 影响运维与部署

| 事项 | 当前影响 | 发现来源 | 建议处理时机 | 处理前必须确认 |
|---|---|---|---|---|
| **`ExecStartPre` 身份验证通过具有误导性。** 预检查和主 `ExecStart` 的环境/变量展开路径不同，曾出现预检查加载候选而主诊断加载旧 release。 | 单看绿色预检查可能错误放行旧代码；目前真正的兜底仍是主诊断的 `loaded_artifact_verified` fail-closed 结果及主进程实际导入路径。 | `docs/deepcoin-contract-cache-ownership-repair-status.md`：**Read-only diagnosis of the monitor code actually loaded**；`docs/ai-context-resolution-optimization-status.md`：**R1 activation identity root cause — read-only diagnosis**。 | 下一次重构 monitor unit/env 安装或身份门禁时；日常部署仍必须保留主诊断身份校验。 | 复现并固定 systemd `EnvironmentFile`、drop-in `Environment` 和 `ExecStart` 展开的优先级；验证主进程模块路径；不得以修复预检查为由削弱 `_loaded_release_evidence`。 |
| **Lifecycle 缺少直接 candidate 外键。** 已记录的 lifecycle 中 `signal_candidate_id` 为空，candidate → lifecycle 只能反查 `(chat_id, message_id)` 并结合 binding。 | 不改变既有交易结论，但增加事故追溯、订单来源证明和自动审计的复杂度；若 chat/message 不能唯一对应，影响尚需进一步确认。 | `docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**12.4 真实消息到执行的链路**；`docs/ai-context-resolution-optimization-status.md`：**Read-only live-position protection audit and P0 checkpoint / Source lineage and non-replay proof**。 | 新增自动化 lineage 审计、历史回填或调整 lifecycle schema 之前。 | 先统计 NULL 范围和 `(chat_id, message_id)` 唯一性；证明确定性 candidate 映射；确认历史回填不会覆盖已有非 NULL 关系，并设计冲突时 fail-closed。 |
| **日常管理审计仍受两张大表放大。** `context_resolution_attempts` 曾约 334 MB，`pending_tpsl_snapshot_observations` 曾约 282 MB；审计构造整库快照时出现约 1.2 GB 峰值。两表当前的增长率和 `pending_tpsl_snapshot_observations` 是否已有独立清理作业，文档不足，**需进一步确认**。 | 持续增长会增加备份、审计快照、页缓存和维护窗口成本；不能按表占比直接推算 RSS，但完整复制字节会被确定放大。 | `docs/deepcoin-contract-cache-ownership-repair-status.md`：**Monitor diagnostic timeout attribution and activation-gate subtraction**；`docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**8.3 与日常管理审计 1.2 GB 内存峰值的关系**、**10.2 需要代码/schema/运维数据政策的选项**。 | 在数据库继续显著增长、审计接近 cgroup 限额或规划下一次存储维护窗口前。 | 重新量测两表当前体积、时间分布、增长率和全部读取方；明确业务/审计保留要求；在生产副本演练归档、重写和峰值内存，不能把逻辑删除量直接当成物理回收量。 |
| **Queue 模式的 stale-expiry 缺少 stall 通知。** `message_processing_worker.py:571-593` 的 queue 过期分支记录并结算过期，但不调用 stall-expiry 通知发送器；非 queue 分支会调用。当前稳态 stale expiry 为 0，因此这是潜在可见性缺口，不是已确认的现行故障。 | 若 queue 模式再次因系统停顿产生过期，操作员可能只在数据库中看到结果而收不到对应告警。 | `docs/2026-09-02-recognition-exposure-and-expiry-read-only-audit.md`：**静默丢弃的范围与起点**。 | 下次改动消息管线告警路径时一并处理，不单独扩大当前展示层任务。 | 对齐 queue/非 queue 的 reason code、去重与通知失败语义；证明不会把用户消息自然过期误报为系统 stall。 |
| **显式过期记录存在历史口径断点。** `expired_stale_instruction` 等显式分类由 2026-08-19 的 commit `3eabde7c` 引入。 | 2026-08-21 之前没有过期记录，只能说明当时不会按该分类落库，不能据此断言此前不存在识别缺口。 | `docs/2026-09-02-recognition-exposure-and-expiry-read-only-audit.md`；commit `3eabde7c`。 | 任何跨 2026-08-19 的识别缺口、过期率或趋势分析。 | 报告必须显式分段，并用当时实际存在的状态/日志口径解释旧时期，不能把 NULL 或无记录当作零。 |

## 仅影响分析与存储

| 事项 | 当前影响 | 发现来源 | 建议处理时机 | 处理前必须确认 |
|---|---|---|---|---|
| **Context-resolution 全列归档后续尚未执行。** 待办包括 Step 1 触发器回填、Step 2 thread-ID 历史回填与零 fallback 证明、R2 reference-only 写入/移除旧读取回退，以及 Step 4 全量历史归档、marker 替换和物理压缩。 | R1 仍双写完整 `request_summary_json`，在线库继续保存可重复的大 payload；文档估算旧口径约 9.41 MB/日。当前不改变模型输入或交易行为。 | `docs/ai-context-resolution-optimization-status.md`：**Integrated full-column archive design — dependency removal, source reduction and complete history**（Step 1–4）。 | 严格按 Step 1 → Step 2 → R2/Step 3 → Step 4 的顺序分别授权；不能把数据回填、权威读取切换和物理压缩合并成一次变更。 | 固定谓词与 exact watermark；Step 1/2 全量确定性相等；运行时 fallback 计数为零；归档逐行 hash/manifest/恢复验证；停止写入后的备份、`quick_check`、外键和回滚窗口。 |
| **P0 正常流量基线仍未达到停止条件。** 指定文档最后固定口径只有 74 条消息、0.604 个正常日，距离 500 条还差 426，距离 7 日还差约 6.4 日；该数字只是文档快照，当前累计值需从后续观测日志读取。 | 触发率、分触发器 token 均值和日均成本仍可能受小样本/流量结构影响，不能当作最终基线。 | `docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**13.6 Cumulative distance to P0 target**（并继承 **12.7 距离 P0 目标** 的固定口径）。 | 继续每日固定窗口只读观测，直到先达到 7 个正常日或 500 条消息。 | 每次窗口终点查询前固定且不滑动；分母、8 触发器、真实 usage 和异常日口径保持一致；首次达标时单独标记，不用代理值覆盖真实 usage。 |
| **历史主识别真实 usage 不可补造。** 主识别可观测性上线前的 4,695 条历史 attempt 新列均为 NULL；context-resolution 的 legacy cohort 也必须区分重建触发器与真实采集 telemetry。 | 历史成本比较仍需代理或标为 unavailable，无法得到完整历史账单；把字节数写成 token 会污染累计分析。 | `docs/ai-context-resolution-optimization-status.md`：**Main-recognition nullable schema complete**、**Step 1 — exact trigger backfill**；`docs/plans/2026-09-01-main-recognition-observability-implementation.md`：**Task 1–3**；`docs/plans/2026-08-31-ai-context-resolution-analysis.md`：**14.7 Validation and limitations**。 | 随自然流量扩大直接测量样本；只对文档已证明确定性的触发器/thread-ID 做单独回填，不回填不存在的 provider usage。 | 查询必须区分 legacy NULL、确定性重建和 live-captured 三种 provenance；provider 未返回 usage 时保持 unavailable；任何代理估算都单独标注，不能写回生产 telemetry。 |

## 使用说明

- 本页是索引，不替代来源文档中的精确证据、ID、事务边界或验收条件。
- “建议处理时机”不构成生产授权；涉及交易语义、schema、数据回填、归档或压缩时仍需独立范围和回滚计划。
- 若来源文档后续证明某项已完成，应在本页更新其状态或移除；不要让历史已解决的 `Outstanding` 重新进入执行队列。
