# `execution_running` 租约卡死与 09-01 stale expiry 只读调查

## 结论摘要

- 截至 `2026-09-02T15:57:55Z`，生产库中符合 `comparison_status=execution_running AND automation_status IS NULL` 的行已由先前的 28 条增长为 **29 条**；新增的是 raw message `14497`。这不是固定的历史孤儿集，而是仍在自然增长的慢性状态。
- 29 条中 27 条 processing job 终态为 `succeeded`，2 条为 `failed`；29 条的 `comparison_started_at` 全为 NULL。因此它们不会被现有针对 semantic-review `running` 状态的超时回收选中。
- 权威 payload 全部是 `recognition_result=非策略`。25 条为 `event_type=none`；另有 3 条 `position_update` 和 1 条 `exit_position`。后 4 条合计指向 5 个 lifecycle，其中 4 个目标在消息时点为 `entered`。
- `2026-09-02T15:47:01Z` 的 worker 角色 GET 快照 `complete=true`，交易所当前有 2 个实盘仓位。两个 posId 都能对应到其他 execution binding；上述 5 个目标 lifecycle 均没有可归属的 execution binding。因此，**确认可归属到这 29 条目标的实盘仓位为 0**；本地 `entered` 不等于交易所实际有仓。
- 实际阻塞已发生：worker journal 记录了 raw `14378` 的 5 次、raw `14428` 的 4 次 `authoritative execution is already in progress`。未找到 `execution_running_decision_present` 的实际 backlog-expiry 拒绝记录。
- 09-01 的 8 条 stale expiry 时间上可归入 **13:22–13:49Z 的 `3205b074...` 激活/重启窗口**，不是 22:20–22:25Z 的后一次激活。其中 7 条为零尝试，1 条为一次尝试；不把这 1 条写成“worker 从未处理”。

## 口径与证据来源

- 数据库：生产 `/opt/telegram-kol-analyzer/data/research.db`，以 SQLite URI `mode=ro` 打开，设置 `PRAGMA query_only=ON`、`temp_store=MEMORY`、`busy_timeout=1000`，在显式只读事务后 rollback。未建表、未写行、未抓取 Web 页面。
- 交易所：worker 角色 `127.0.0.1:8002` 的 GET-only `/api/runtime-incidents/live-position-sizes`，快照时间 `2026-09-02T15:47:01.776884Z`，`complete=true`，2 个仓位。未下单、改单或撤单。
- 代码口径：本地对 `recognition_decisions.py`、`semantic_review_control.py`、`message_processing_backlog_expiry.py` 和 `authoritative_recognition.py` 与当前 worker release `0de19c1c...` 比较，四个文件零差异。
- 时间：数据库 naive datetime 按项目的 UTC 口径与文档中 `Z` 时间直接比较。
- 输出边界：本报告只保留计数、时间、raw message ID、lifecycle ID 和状态；不含消息正文、群组/KOL、发送者或图片内容。

## 1. 29 条的时间分布与窗口对齐

### 按 posted_at 日期

| UTC 日期 | 条数 | 占 29 条 |
|---|---:|---:|
| 2026-08-24 | 3 | 10.3% |
| 2026-08-25 | 4 | 13.8% |
| 2026-08-26 | 3 | 10.3% |
| 2026-08-27 | 3 | 10.3% |
| 2026-08-28 | 5 | 17.2% |
| 2026-08-29 | 1 | 3.4% |
| 2026-08-30 | 0 | 0.0% |
| 2026-08-31 | 0 | 0.0% |
| 2026-09-01 | 6 | 20.7% |
| 2026-09-02 | 4 | 13.8% |

### 与已记录窗口的对齐

- **明确同窗口，但不断言因果：** raw `14220` 于 `2026-09-01T08:20:52Z` 进入 `execution_running`，落在 R1 激活后 `08:08:41–08:23:42Z` 的 L1 窗口内；状态文档也单独记载该 raw 在窗口中做了 context reanalysis。时间重合不能单独证明是重启导致。
- **接近但不在窗口内：** raw `13396` 的 claim 在 R5 失败并回滚时点 `03:21:34Z` 后约 19 分钟；raw `14289` 的 claim 在 `3205b074...` 观测结束 `13:49:42Z` 后约 25 分钟。两者均不归为重启窗口内事件。
- **明确不对齐：** 29 条中没有任何 claim 落在 08-30–08-31 维护冻结期，也没有任何 claim 落在 09-01 `22:20:06–22:25:44Z` 的激活/解冻重启窗口。
- **其余未与文档中精确窗口对齐的 ID：** `12798, 12849, 12897, 13022, 13076, 13160, 13166, 13198, 13307, 13308, 13433, 13503, 13571, 13589, 13685, 13723, 13730, 13835, 14193, 14196, 14214, 14243, 14374, 14378, 14428, 14497`。仅从时间无法将它们归因于某次部署或重启。

## 2. 逐条权威结论、目标与 job 终态

`message-time 状态 → 当前状态` 为——时表示 payload 没有 target lifecycle。

| raw ID | posted_at (UTC) | execution claim_at (UTC) | recognition_result | event_type | target lifecycle（时点→当前） | job 终态 |
|---:|---|---|---|---|---|---|
| 12798 | 08-24 06:58:12 | 08-24 07:19:04 | 非策略 | none | — | succeeded |
| 12849 | 08-24 10:45:18 | 08-24 11:40:38 | 非策略 | none | — | succeeded |
| 12897 | 08-24 12:38:32 | 08-24 13:59:38 | 非策略 | none | — | succeeded |
| 13022 | 08-25 03:06:18 | 08-25 08:05:13 | 非策略 | none | — | succeeded |
| 13076 | 08-25 08:03:50 | 08-25 08:09:16 | 非策略 | none | — | succeeded |
| 13160 | 08-25 13:54:34 | 08-26 00:33:25 | 非策略 | none | — | succeeded |
| 13166 | 08-25 14:33:54 | 08-25 14:49:02 | 非策略 | none | — | succeeded |
| 13198 | 08-26 00:01:27 | 08-26 00:57:26 | 非策略 | none | — | succeeded |
| 13307 | 08-26 15:04:00 | 08-26 15:10:26 | 非策略 | none | — | succeeded |
| 13308 | 08-26 15:04:25 | 08-26 15:13:57 | 非策略 | none | — | succeeded |
| 13396 | 08-27 03:38:01 | 08-27 03:40:57 | 非策略 | none | — | succeeded |
| 13433 | 08-27 08:13:22 | 08-27 08:20:20 | 非策略 | position_update | 1001（pending_entry→expired） | succeeded |
| 13503 | 08-27 10:20:49 | 08-27 10:27:49 | 非策略 | none | — | succeeded |
| 13571 | 08-28 01:28:34 | 08-28 01:31:22 | 非策略 | none | — | succeeded |
| 13589 | 08-28 01:43:31 | 08-28 01:46:05 | 非策略 | none | — | succeeded |
| 13685 | 08-28 14:20:16 | 08-28 16:05:03 | 非策略 | none | — | succeeded |
| 13723 | 08-28 16:16:06 | 08-28 16:46:17 | 非策略 | none | — | succeeded |
| 13730 | 08-28 16:24:46 | 08-28 16:54:04 | 非策略 | none | — | succeeded |
| 13835 | 08-29 14:41:13 | 08-29 14:42:40 | 非策略 | none | — | succeeded |
| 14193 | 09-01 02:17:41 | 09-01 02:51:15 | 非策略 | none | — | succeeded |
| 14196 | 09-01 02:26:19 | 09-01 02:55:25 | 非策略 | none | — | succeeded |
| 14214 | 09-01 05:37:46 | 09-01 05:39:19 | 非策略 | position_update | 1040（entered→exited） | succeeded |
| 14220 | 09-01 05:40:15 | 09-01 08:20:52 | 非策略 | none | — | succeeded |
| 14243 | 09-01 08:33:20 | 09-02 03:48:26 | 非策略 | none | — | succeeded |
| 14289 | 09-01 14:03:50 | 09-01 14:14:46 | 非策略 | none | — | succeeded |
| 14374 | 09-02 03:12:58 | 09-02 03:31:47 | 非策略 | none | — | succeeded |
| 14378 | 09-02 03:18:10 | 09-02 03:22:00 | 非策略 | **exit_position** | **1053（entered→entered）** | failed（5 attempts） |
| 14428 | 09-02 09:50:12 | 09-02 09:54:30 | 非策略 | **position_update** | **1044（entered→entered）、1051（entered→entered）** | failed（5 attempts） |
| 14497 | 09-02 14:31:36 | 09-02 14:47:22 | 非策略 | none | — | succeeded |

### 管理语义与实盘归属

- `exit_position / close_signal / position_update` 合计 4 条：`13433, 14214, 14378, 14428`；`close_signal` 为 0。
- 在消息时点指向已活跃 `entered` 目标的是 3 条：`14214, 14378, 14428`，共 4 个 lifecycle reference：`1040, 1053, 1044, 1051`。raw `13433` 指向 lifecycle `1001`，当时是 `pending_entry`，不记为已持仓。
- 当前 lifecycle 状态：`1001=expired`、`1040=exited`、`1044=entered`、`1051=entered`、`1053=entered`。
- 上述 5 个 lifecycle 的 `execution_binding_id` 均为 NULL，也没有 `(chat_id, message_id)` 匹配的 execution binding。worker 完整快照中的 2 个 posId 均映射到其他 binding（325 和 327）。因此确认“可归属的实盘仓位”为 0，同时保留本地 lifecycle 状态残留的独立问题。

## 3. claim token 分布与现有转出路径

- 29/29 个 `comparison_claim_token` 非 NULL，29/29 互不相同，均为 32 位小写十六进制字符；无重复、无异常格式。为避免在报告中扩散内部租约值，不列出 token 全文。
- 正常流程的唯一转出是当次执行者持有相同 generation token 后调用 `finalize_authoritative_automation_outcome()`；它用 raw ID + `execution_running` + exact token 做 CAS，然后清除 token。
- semantic-review worker 的过期回收只覆盖 `comparison_status=running` 且 `comparison_started_at <= stale_before`的行；不覆盖 `execution_running`。
- `semantic-review-terminalize` CLI 的计划只选 `pending/failed`，也不选 `execution_running`。代码中未找到定时任务、恢复流程或 CLI 可在原执行者丢失后合法回收这类行。
- 同一 raw 的后续权威写入会在 `recognition_decisions.py:90/202` 直接抛 `authoritative execution is already in progress`。积压过期计划若把这些 raw 包含在目标集，会在 `message_processing_backlog_expiry.py:149-154` 拒绝整个计划。

## 4. 已经发生的阻塞证据

- worker journal 自 09-02 00:00Z 起实际记录 18 行 `authoritative execution is already in progress`；每次错误同时出现在错误日志与 traceback，去重后是 9 次处理失败：raw `14378` 5 次，raw `14428` 4 次。两者的 job 最终都是 `failed/processing_error:RuntimeError`。
- 27 个 `succeeded` job 没有把 decision 的孤儿租约暴露成 job 故障，这解释了其主要的静默形态。
- worker journal 中未找到 `execution_running_decision_present`；两个已知 cutover-evidence/backup 根目录对该精确字符串的只读检索也为 0 个文件。29 个 job 当前均已终结，没有属于当前 pending expiry target 的行。因此，“会拒绝 backlog expiry”是已证实的代码边界，但本轮没有发现它已实际拒绝过某次生产过期操作的证据。

## 5. 09-01 八条 stale expiry 对账

| raw ID | posted_at (UTC) | enqueued_at (UTC) | attempts | final claimed_at | completed_at (UTC) |
|---:|---|---|---:|---|---|
| 14278 | 13:19:09 | 13:19:09.546214 | 0 | NULL | 13:35:24.360875 |
| 14279 | 13:19:13 | 13:19:14.069482 | 0 | NULL | 13:35:24.391623 |
| 14281 | 13:28:39 | 13:28:41.452515 | 1 | NULL | 13:44:02.259635 |
| 14282 | 13:29:37 | 13:29:38.119584 | 0 | NULL | 13:49:02.933066 |
| 14283 | 13:33:52 | 13:33:55.834985 | 0 | NULL | 13:49:02.946987 |
| 14284 | 13:33:52 | 13:33:56.311851 | 0 | NULL | 13:49:02.958347 |
| 14285 | 13:33:53 | 13:33:56.663838 | 0 | NULL | 13:49:02.974013 |
| 14286 | 13:33:53 | 13:33:57.198034 | 0 | NULL | 13:49:02.985823 |

文档中 `3205b074...` 这次 schema/deploy 证据根时标为 `13:22:08Z`，最终交易所预检为 `13:31:33Z`，随后是激活、解冻、`worker -> web -> ingest` 重启，L1 窗口为 `13:34:00–13:49:11Z`。上表八条全部在该激活前/中入队，并全部在 L1 窗口中以 `expired_stale_instruction` 结算；最后四条在窗口结束前约 8 秒同批结算。

因此，这八条在时间和作业状态上能够归入 13:22–13:49Z 部署冻结/重启窗口。但需保留两个精确区分：

1. raw `14278/14279` 在证据根时标前约 3 分钟已入队，但窗口中仍未 claim 并过期；
2. raw `14281` 的 `attempt_count=1`，所以只能说它未在窗口内完成，不能说它从未被 worker 尝试。其余 7 条是零尝试。

这八条最后一条于 `13:49:02Z` 结算，比 `22:20:06Z` 的后一次 main-recognition 激活早 8 小时 31 分；不把它们归因于 22:20Z 窗口。

## 边界

本轮没有解锁、改写或删除任何 decision，没有重跑识别，没有更改仓位或保护单，也不在本报告中提出修复方案。
