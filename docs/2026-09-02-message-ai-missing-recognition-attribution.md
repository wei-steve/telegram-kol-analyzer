# 最近 500 条消息“结论未记录”只读归因

## 结论与门禁

本次 A 部分确认了真实漏识别证据，因此按任务门禁停止，没有执行 B 部分的展示层调整，也没有改代码、配置、schema、服务或生产数据。

- 上一份报告记录的 226 条 `recognition_result=NULL` 中，214 条（94.69%）的作业最终状态为 `expired`，原因均为 `expired_stale_instruction`；其中 213 条在本次快照时仍无最终识别结果，另 1 条在两次报告之间获得了结果。
- 另有 2 条作业失败：1 条为 `processing_error:ValueError`，虽存在 5 个 `completed/became_authoritative=true` 的 MiMo run，却没有 `recognition_decisions`；1 条为 `processing_error:AuthoritativeProcessingFailed`，5 个 MiMo run 全部失败。
- 只有 10 条（4.42%）是按现有代码明确处理的空输入终态 `terminal_authoritative_failure:empty_input`。其余缺结果不能归为“AI 未启用群组”的设计内空白。
- 11 条“策略类结论但无候选”均不是“消息到达时目标已处于终态”：10 条有明确目标 lifecycle，消息发生时 5 条为 `pending_entry`、5 条为 `entered`；另 1 条没有目标。10/11 记录为 `mimo_authoritative_not_safely_applied`，说明候选层在按 fail-closed 边界拒绝矛盾结果，而不是静默丢弃；剩余 1 条没有接纳原因，需进一步确认。
- 134 条 `context_unresolved` 全部已有最终 `recognition_result`，其中 133 条 context attempt 已 `completed`、1 条 `exhausted`。它们不是“至今仍无最终结论”的积压。

## 数据范围与只读边界

- 固定样本：`raw_messages.id` 13,949–14,448，共 500 条；`posted_at` 时间跨度为 2026-08-30 12:34:48 UTC 至 2026-09-02 11:19:47 UTC。
- 主快照观测时间：2026-09-02 12:37 UTC；主聚合耗时 1.392 秒。
- 访问方式：直接连接生产 SQLite URI `mode=ro`，并强制 `PRAGMA query_only=ON`、`temp_store=MEMORY`、1 秒 busy timeout；使用单个只读事务快照，未建临时表、未写入、未抓 Web 页面。
- 投影与分类：复用运行中 Web release 的 `web_queries._serialize_raw_messages()`，结论分类顺序与 `_messages.html` 完全一致。
- 查询时运行身份：Web=`b78f16098c591978fe764e15c9b793182fc97f5b`，ingest/worker=`0de19c1cbb2089fd58b8940d9b01a65096f9a063`，三个角色均 `loaded_artifact_verified=true`。生产设置为 `message_pipeline_mode=queue`，权威缺口恢复窗口为 15 分钟。
- 报告只包含计数、比例、raw message ID、配置序号和 lifecycle 等结构化字段；没有消息正文、群组/KOL 名称、发送者或图片内容。

### 226 与 225 的快照差异

上一份报告在 11:37 UTC 观测到 226 条 NULL。本次主快照只剩 225 条：`raw_message_id=14126` 的 decision 在 12:37:25 UTC 被更新，当前已出现 `recognition_result=是策略`。它仍保留 `job_status=expired`、`last_reason=expired_stale_instruction`；该消息属于配置群组 #3、输入类型为 `text`、无 media asset。

因此，下文群组表和“原 226 条”作业表按上一份报告的 226 条 cohort 复原；当前仍缺结果的细分则明确使用 225 条。该复原由“前后恰差 1 条”及唯一在两次观测间更新的样本行交叉验证，但上一轮没有保留数据库快照，所以它仍是基于两次只读快照的确定性对账，不是对旧库快照的直接重放。

## 1. 按来源群组与识别开关归因

为避免输出群组/KOL 名称，群组以 `config/groups.yaml` 中的顺序编号和 `chat_id` 标识。

| 配置群组 | `chat_id` | 样本条数 | 原 226 条中的 NULL | 群内 NULL 占比 | `enabled` | `ai_strategy_enabled` |
|---:|---:|---:|---:|---:|:---:|:---:|
| #2 | -1002344190971 | 69 | 9 | 13.04% | true | false |
| #3 | -1002199068560 | 39 | 21 | 53.85% | true | true |
| #5 | -1003053031367 | 14 | 8 | 57.14% | true | true |
| #7 | -1002805019371 | 13 | 5 | 38.46% | true | true |
| #8 | -1003048800035 | 29 | 10 | 34.48% | true | true |
| #9 | -1002409877375 | 52 | 45 | 86.54% | true | true |
| #10 | -1002368892075 | 54 | 22 | 40.74% | true | true |
| #11 | -1002918719121 | 3 | 1 | 33.33% | true | false |
| #12 | -1003095914903 | 53 | 16 | 30.19% | true | true |
| #13 | -1002282384698 | 32 | 12 | 37.50% | true | true |
| #14 | -1002337721508 | 42 | 33 | 78.57% | true | true |
| #15 | -1002960443256 | 40 | 26 | 65.00% | true | true |
| #16 | -1003344714145 | 2 | 2 | 100.00% | true | true |
| #17 | -1002367395169 | 17 | 2 | 11.76% | true | true |
| #18 | -1003825498321 | 13 | 5 | 38.46% | true | true |
| #19 | -1002370796392 | 10 | 4 | 40.00% | true | true |
| #20 | -1002458558902 | 2 | 1 | 50.00% | true | true |
| #34 | -1003942765613 | 6 | 4 | 66.67% | true | true |
| **合计** |  | **500** | **226** |  |  |  |

按配置字段表面值拆分：`ai_strategy_enabled=true` 为 216/226（95.58%），`false` 为 10/226（4.42%）。但这不能解释为“216 条该识别、10 条设计内不识别”，原因如下：

1. 18 个有 NULL 的群组全部 `enabled=true`；生产 listener 的目标集合只检查 `group.enabled`。
2. `ai_strategy_enabled` 在当前调用链中用于 `strategy_alert_enabled_for_title`，也就是是否发策略提醒，不是权威识别入队开关。
3. queue 入队函数在 `message_pipeline_mode=queue` 时按新插入的 raw message 入队，不检查 `ai_strategy_enabled`。
4. 所有群组的 `tracked_senders` 均为空；当前权威识别链也不存在“非目标发送者则跳过识别”的过滤。

因此本样本中：

| 归因 | 条数 | 占原 226 条 | 判定 |
|---|---:|---:|---|
| 来自实际不在识别范围的禁用群组 | 0 | 0.00% | 设计内正常，但本样本未命中 |
| `ai_strategy_enabled=false`，但群组仍在权威识别范围 | 10 | 4.42% | 不能视为设计内跳过；字段只控制提醒 |
| `ai_strategy_enabled=true` | 216 | 95.58% | 需要按作业/run/decision 继续解释 |

## 2. 作业、run、decision 与实际前置过滤

### 原 226 条的作业状态

| 作业状态 | 条数 | 占原 226 条 | `last_reason` |
|---|---:|---:|---|
| `expired` | 214 | 94.69% | 全部 `expired_stale_instruction` |
| `failed` | 2 | 0.88% | `processing_error:ValueError` 1；`processing_error:AuthoritativeProcessingFailed` 1 |
| `succeeded` | 10 | 4.42% | 全部 `terminal_authoritative_failure:empty_input` |
| `pending` | 0 | 0.00% | — |
| `claimed` | 0 | 0.00% | — |
| 作业不存在 | 0 | 0.00% | — |
| **合计** | **226** | **100.00%** |  |

`ai_strategy_enabled=true` 的 216 条中，作业为 `expired` 205、`failed` 2、`succeeded/empty_input` 9；`false` 的 10 条中，作业为 `expired` 9、`succeeded/empty_input` 1。两组都进入了同一作业体系，再次证明该 flag 不是识别开关。

按任务原定义单列 (b) 类，即 `ai_strategy_enabled=true` 的原 216 条：

| 当前可观测状态 | 条数 | 占 216 条 |
|---|---:|---:|
| stale expiry，仍无最终结果 | 204 | 94.44% |
| failed，仍无最终结果 | 2 | 0.93% |
| succeeded/empty input，仍无结果 | 9 | 4.17% |
| stale expiry，但本轮已后续获得结果 | 1 | 0.46% |
| **合计** | **216** | **100.00%** |

该 216 条原始输入为 `text` 100、`image` 77、`text+image` 24、`empty` 15。当前唯一“有 MiMo run 但没有 decision”的 `raw_message_id=14388` 也属于此 (b) 类。

### 当前仍为 NULL 的 225 条

| 当前结果 | 条数 | 占 225 条 | 判定 |
|---|---:|---:|---|
| `expired_stale_instruction`，且输入可处理 | 206 | 91.56% | 真实权威识别缺口；需要进一步排查形成与恢复边界 |
| 作业失败，且输入可处理 | 2 | 0.89% | 真实失败；需要进一步排查 |
| `terminal_authoritative_failure:empty_input` | 10 | 4.44% | 设计内正常：代码明确拒绝无可读文字且无图片输入 |
| 输入为 `empty`，但作业先以 stale instruction 过期 | 7 | 3.11% | 内容本身最终也会被拒绝，但实际记录原因是未处理即过期，不能改写成已正常处理 |
| **合计** | **225** | **100.00%** |  |

输入类型按代码的真实 `_message_input_kind()` 判据统计；它把非空文字视为 `text`，把 `kind` 含 `photo/image` 或 MIME 为 `image/*` 的媒体视为图片：

| 输入类型 | 原 226 条 | 占比 | 是否会被前置拒绝 |
|---|---:|---:|---|
| `text` | 102 | 45.13% | 否 |
| `image` | 77 | 34.07% | 否；纯图片是有效输入 |
| `text+image` | 30 | 13.27% | 否 |
| `empty` | 17 | 7.52% | 是；无可读文字且无图片 |
| **合计** | **226** | **100.00%** |  |

也就是说，209/226（92.48%）不是因空内容被前置过滤。当前仍缺结果的 225 条中，208 条具有可处理输入；其中 206 条过期、2 条失败。

decision/run 关系：

- 当前 225 条 NULL 中，224 条有 `recognition_decisions`，状态均为 `authoritative_status=识别失败`、`agreement_status=authoritative_failed`。其中 213 条 payload 是 recovery guard 写入的 `expiry_classification/reason/summary`；另外 11 条 payload 没有 `recognition_result`。
- 1 条（`raw_message_id=14388`）有 5 个 `v1_authoritative/completed/became_authoritative=true` run，却没有 `recognition_decisions`；作业为 `failed/processing_error:ValueError`。这是明确的 run→decision 落地断点，需要进一步排查。
- `raw_message_id=14447` 有 decision，但 5 个 `v1_authoritative` run 全部 `failed/became_authoritative=false`，`final_error_code=v1_authoritative_failed`；作业为 `failed/processing_error:AuthoritativeProcessingFailed`。这是权威识别失败，不是前置过滤。
- 当前不存在 pending/claimed 作业，因此没有“此刻正在排队”的积压；但 213 条仍无结果的 stale expiry 是已经终止的历史识别缺口，不能因队列已清空而视为正常。

## 3. media asset 与识别结果交叉表

为与上一份报告的 255 条 media、226 条 NULL 完全对齐，本表使用上一轮 cohort；12:37 新获得结果的 `raw_message_id=14126` 无 media，因此可以由两次快照确定性对账。

|  | 有识别结果 | 无识别结果 | 合计 |
|---|---:|---:|---:|
| 有 media asset | 125 | 130 | 255 |
| 无 media asset | 149 | 96 | 245 |
| **合计** | **274** | **226** | **500** |

- `P(无识别 | 有媒体)=130/255=50.98%`。
- `P(无识别 | 无媒体)=96/245=39.18%`。
- 无结果消息中有媒体的比例为 `130/226=57.52%`，但仍有 96/226（42.48%）完全无媒体；同时 125/255（49.02%）的有媒体消息已有识别结果。

结论：媒体与无结果存在 11.80 个百分点的正关联，但不是高度重合，更不能把“有媒体”当作 226 条缺结果的主因。尤其 77 条纯图片被代码视为有效 `image` 输入，并非设计内过滤。

## 4. 11 条策略类结论但无候选

“消息当时状态”按 `signal_at/entered_at/exited_at` 与消息 `posted_at` 比较重建；“当前状态”直接读取 `strategy_lifecycles.lifecycle_status`。该表没有不可变的逐次状态快照，所以若需要审计终态中的 `invalidated/cancelled/expired` 精确转移时刻，仍需进一步确认；本批 10 个已找到的目标在消息时点只落在 `pending_entry/entered`，不存在该歧义。

| raw message ID | 结论分类 | `event_type` | target lifecycle | 消息当时状态 | 当前状态 |
|---:|---|---|---:|---|---|
| 14204 | 仓位管理 | `position_update` | 1040 | `pending_entry` | `exited` |
| 14210 | 仓位管理 | `position_update` | 1040 | `entered` | `exited` |
| 14244 | 仓位管理 | `position_update` | 未记录 | 需进一步确认 | 需进一步确认 |
| 14259 | 仓位管理 | `position_update` | 1044 | `pending_entry` | `entered` |
| 14273 | 仓位管理 | `position_update` | 1039 | `pending_entry` | `pending_entry` |
| 14306 | 仓位管理 | `position_update` | 1043 | `pending_entry` | `entered` |
| 14371 | 仓位管理 | `position_update` | 1051 | `pending_entry` | `entered` |
| 14378 | 仓位管理 | `exit_position` | 1053 | `entered` | `entered` |
| 14384 | 仓位管理 | `exit_position` | 1053 | `entered` | `entered` |
| 14410 | 仓位管理 | `exit_position` | 1053 | `entered` | `entered` |
| 14418 | 仓位管理 | `position_update` | 1049 | `entered` | `exited` |

汇总：

- 11 条全部是“仓位管理”；`position_update` 8 条、`exit_position` 3 条。
- 10/11 有 target lifecycle；消息当时 `pending_entry` 5 条、`entered` 5 条，已处于终态 0 条。剩余 1 条 target 未记录。
- 当前状态为 `entered` 6 条、`pending_entry` 1 条、`exited` 3 条、目标未知 1 条。即使当前已退出的 3 条，在消息发生时也仍是活跃状态。
- 11 条的权威 payload 都同时呈现 `recognition_result=非策略` 与 management `event_type`。10 条的 `system_acceptance.reason_code=mimo_authoritative_not_safely_applied`，这支持“候选投影按安全边界主动拒绝矛盾结果”，不支持“因目标已经关闭而正常不生成”。
- `raw_message_id=14378` 的目标当时及当前均为 `entered`，但 `system_acceptance.reason_code=NULL`；仅凭现有数据无法判定为什么未生成候选，需进一步确认。

因此，“无候选 11 条主要是目标已平的正常终态”这一假设被数据否定。候选层的 fail-closed 行为本身有明确记录，但上游为什么产出“非策略 + management event”的矛盾权威 payload，以及 14378 为什么缺少拒绝原因，属于需要进一步排查的问题。

## 5. `context_unresolved` 的去向

`unresolved_reason` 实际不是稳定机器码，而是模型生成的自然语言原因。固定样本中的 134 个非空值互不相同，每个值只出现 1 次。逐值原样列出会等价于重放消息级内容，与本任务“不输出消息正文、KOL 名称或图片内容”的边界冲突，因此这里按精确值的基数/频次谱报告，不抄写文本：

| 每个 exact value 的出现次数 | 不同 value 数 | 覆盖消息数 |
|---:|---:|---:|
| 1 | 134 | 134 |

进一步使用同一条 context attempt 的结构化字段分组：

| 维度 | 取值 | 条数 | 占 134 条 |
|---|---|---:|---:|
| attempt status | `completed` | 133 | 99.25% |
| attempt status | `exhausted` | 1 | 0.75% |
| decision | `hold` | 103 | 76.87% |
| decision | `unresolved` | 31 | 23.13% |
| 当前最终结论 | 已有 `recognition_result` | 134 | 100.00% |
| 当前最终结论 | 仍为 NULL | 0 | 0.00% |

结论：`context_unresolved` 在这批数据中表示 context 层的终态 `hold/unresolved` 解释，不是仍在等待最终识别的长期滞留；134/134 已有最终 recognition result。唯一 `exhausted` 行也已有最终结论，因此不构成本轮 226 条缺结果的来源。

## 设计内正常与待排查清单

| 观察 | 结论 |
|---|---|
| 10 条 `terminal_authoritative_failure:empty_input` | 设计内正常；真实空输入按代码 fail-closed |
| `ai_strategy_enabled=false` 的 10 条 NULL | 不是设计内不识别；该字段当前只控制提醒 |
| 213 条当前仍 NULL 的 `expired_stale_instruction` | 真实历史识别缺口；需要进一步排查形成原因与为何未恢复 |
| 1 条 completed authoritative runs 但无 decision | 明确落地断点；需要进一步排查 |
| 1 条 authoritative runs 全失败 | 明确识别失败；需要进一步排查 |
| 11 条无候选 | 不是“目标当时已关闭”；10 条为安全拒绝矛盾 payload，1 条缺原因，均需进一步确认上游语义 |
| 134 条 `context_unresolved` | 已有最终结果，不是本轮漏识别积压 |

本轮因前三项异常证据触发 A→B 门禁，停止在只读归因。没有实施置信度阈值、`runtime_not_authoritative`、统计初值或 drift 容差调整；这些 B 部分改动仍保持未执行状态。
