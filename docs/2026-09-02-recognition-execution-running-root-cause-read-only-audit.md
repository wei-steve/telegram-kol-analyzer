# `recognition_decisions.execution_running` 根因只读调查

## 结论摘要

- 截至本轮只读快照，生产库仍有 **29** 条
  `comparison_status=execution_running AND automation_status IS NULL`；27 条
  message-processing job 为 `succeeded`，2 条为 `failed`。
- **未发现“已实际执行但没有 automation 记录”的 (b) 类证据。** 29 条当前
  generation 的 signal candidate、execution binding、order leg、execution event、
  management batch/envelope/target 均为 0；硬性停止条件未触发。
- “job succeeded、decision 仍 running”的直接成因已确认：message job 在主识别之后会
  同步调用一次 context worker；context worker 捕获 `reanalyze()` 的异常并返回
  `retry_scheduled/exhausted`，外层 message job 不检查这个返回值，因此仍以
  `worker_completed` 结算。29/29 都有同 raw 的 context attempt；28 条 claim 与最近一条
  context attempt 的创建或更新时间相差小于 1 秒，剩余 raw `13308` 的 claim 落在该
  attempt 的执行区间内。
- claim 到 finalize 的生产代码没有提前 `return`，也不存在“claim 后 assessment 又变成
  `authoritative_failed`”的路径。`AuthoritativeAssessment` 是 frozen dataclass，claim 后到
  finalize 前没有 reassignment；能跳过 finalize 的只剩中间异常、`BaseException` 或进程
  终止。
- 最初的 post-claim 异常类型仍无法从现有证据恢复。context worker 只持久化异常类名且不
  记录首次 traceback；raw `14428` 的 journal 只保留了之后撞上旧锁的重试栈，raw
  `14497` 在对应窗口没有 raw ID、异常、重启或 OOM 日志。

## 范围、身份与只读边界

- 数据库：生产 `/opt/telegram-kol-analyzer/data/research.db`，使用 SQLite
  `mode=ro`/`-readonly`，每次先执行 `PRAGMA query_only=ON` 和 1 秒 busy timeout；未建表、
  未写行、未抓 Web 页面。
- 运行身份：本轮读取三个角色各自的 deployment identity，Web 为
  `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262`，ingest/worker 均为
  `0de19c1cbb2089fd58b8940d9b01a65096f9a063`，三者
  `loaded_artifact_verified=true`。核心四个文件在本地 HEAD 与 worker release 间零差异。
- 交易所：worker 的 `/api/runtime-incidents/live-position-sizes` 本轮返回 404；按一次有依据
  的重试读取 `/api/runtime-agent/read-only-exchange-snapshot`，得到
  `complete=true, position_count=1, open_order_count=0`，但该端点不返回逐仓归属字段，不能
  用它独立证明历史执行归属。前轮已确认这 29 条当前无对应实盘敞口，本报告不把当前仓位数
  倒推成历史执行证据。
- 所有远端 Python 调用均为标准库命令且带 `-B`；没有从 immutable release import 代码，
  没有生成 `__pycache__`/`.pyc`。
- 报告只保留计数、时间、raw message ID 与状态，不含正文、群组/KOL、发送者、标的或图片。

## 1. claim 到 finalize 的完整代码路径

生产等价代码位于 `authoritative_recognition.py:1410-1508`：

1. `assess_message_authoritatively()` 返回 frozen `AuthoritativeAssessment`。
2. 仅当 `agreement_status != authoritative_failed` 时，使用 generation token 执行
   `claim_authoritative_execution()`，把 `execution_pending` CAS 为
   `execution_running`。
3. claim 后依次执行：
   `apply_authoritative_assessment()`、`source_execution_barrier()`、automation 分支、
   `load_trading_settings()`、`finalize_authoritative_automation_outcome()`。
4. `authoritative_failed` 分支调用的是
   `update_recognition_execution_outcome()`，但它从不 claim；非 failed 分支才 claim，也始终
   进入 finalize 分支。

### 能跳过 finalize 的实际路径

claim 后至 finalize 前没有 `return`、`break` 或状态重算。以下任一调用抛出并越过当前函数，
都会遗留 `execution_running`：

- `apply_authoritative_assessment()` 及其数据库投影；
- `source_execution_barrier()`；
- `_has_current_mimo_candidate()`；
- `auto_trade_executor()` 未被其适配器捕获的 `BaseException`，或进程在调用中终止；
- `load_trading_settings()`；
- `finalize_authoritative_automation_outcome()` 自身的数据库操作；
- SIGTERM 未完成 drain、SIGKILL、机器掉电等进程级终止。

`_run_auto_trade_executor()` 会把普通 `Exception` 转成
`{status: failed, reason: auto_trade_executor_error}`，所以普通 Deepcoin adapter 异常通常仍
应到达 finalize；不能把这 29 条笼统归因为 Deepcoin 网络异常。

### 已排除的假设

不存在“先 claim，随后 assessment 被重新赋值或重新计算成
`authoritative_failed`”的路径：

- `AuthoritativeAssessment` 使用 `@dataclass(frozen=True)`；
- claim 后至分支判断之间没有 `assessment = ...`；
- 唯一的 `replace(assessment, ...)` 在 finalize 成功之后。

## 2. 为什么 job 可以 succeeded

这是两套状态机的嵌套与错误传播边界不一致：

1. `process_message_job()` 先在 `asyncio.to_thread()` 中完成当前 raw 的主识别。
2. 函数末尾再调用一次 `context_resolution_worker()`；它可选择并重分析另一条待处理 raw。
3. `run_context_resolution_once()` 在 `reanalyze()` 外包了 `except Exception`，把异常转换为
   context attempt 的 retry/exhausted 状态并返回 dict，不再向 message job 抛出。
4. `process_message_job()` 忽略该 dict；`run_message_processing_worker_tick()` 因未收到异常，
   把当前 message job 结算为 `succeeded/worker_completed`。

因此目标 raw 自己的 job 状态只表示它原先的 message job 已返回；后续把它取出来重分析的
context worker 可能挂在同一个 tick，也可能挂在另一条 raw 的 message job 尾部。无论是哪种，
目标 raw 的 job 都不会因这次重分析失败而改成 failed；调用 context worker 的外层 job 也会因
异常被吞而继续成功。27 条 `succeeded` 与 orphan 并不矛盾。

另一个容易误读的字段是 `message_processing_jobs.completed_at`：worker 在 tick 开始时固定
`tick_time`，最终结算仍写这个起始时间，不是实际返回时间。所以下表中 claim 晚于
`job_completed_at`，既不能证明对应 job 真在该时刻完成，也不能证明该 job 持有后来的
reanalysis；后者可由任何随后 message job 尾部的 context worker 取走。

## 3. 29 条 job/decision 对账

`claim-job Δ` = `decision.updated_at - job.completed_at`。主表先标明 token 是否存在；其后单列
29 个精确值，便于未来逐行 CAS 计划绑定 preimage。

| raw | posted_at (UTC) | job | attempts | job completed_at | last_reason | decision created_at | claim_at | token | claim-job Δs |
|---:|---|---|---:|---|---|---|---|---|---:|
| 12798 | 08-24 06:58:12 | succeeded | 0 | 08-24 06:59:49.807 | worker_completed | 08-24 07:01:24.723 | 08-24 07:19:04.488 | present | +1154.681 |
| 12849 | 08-24 10:45:18 | succeeded | 0 | 08-24 10:46:16.564 | worker_completed | 08-24 10:47:23.554 | 08-24 11:40:38.535 | present | +3261.971 |
| 12897 | 08-24 12:38:32 | succeeded | 0 | 08-24 12:47:30.687 | worker_completed | 08-24 12:48:45.636 | 08-24 13:59:38.770 | present | +4328.083 |
| 13022 | 08-25 03:06:18 | succeeded | 0 | 08-25 03:07:31.196 | worker_completed | 08-25 03:08:21.122 | 08-25 08:05:13.079 | present | +17861.883 |
| 13076 | 08-25 08:03:50 | succeeded | 0 | 08-25 08:03:50.348 | worker_completed | 08-25 08:07:46.513 | 08-25 08:09:16.967 | present | +326.619 |
| 13160 | 08-25 13:54:34 | succeeded | 0 | 08-25 13:56:03.047 | worker_completed | 08-25 13:57:18.194 | 08-26 00:33:25.231 | present | +38242.185 |
| 13166 | 08-25 14:33:54 | succeeded | 0 | 08-25 14:33:55.114 | worker_completed | 08-25 14:34:47.655 | 08-25 14:49:02.987 | present | +907.873 |
| 13198 | 08-26 00:01:27 | succeeded | 0 | 08-26 00:01:27.627 | worker_completed | 08-26 00:02:16.434 | 08-26 00:57:26.109 | present | +3358.482 |
| 13307 | 08-26 15:04:00 | succeeded | 1 | 08-26 15:07:47.371 | worker_completed | 08-26 15:07:47.183 | 08-26 15:10:26.708 | present | +159.337 |
| 13308 | 08-26 15:04:25 | succeeded | 1 | 08-26 15:10:28.527 | worker_completed | 08-26 15:10:28.416 | 08-26 15:13:57.401 | present | +208.874 |
| 13396 | 08-27 03:38:01 | succeeded | 0 | 08-27 03:38:25.086 | worker_completed | 08-27 03:39:45.023 | 08-27 03:40:57.357 | present | +152.271 |
| 13433 | 08-27 08:13:22 | succeeded | 0 | 08-27 08:14:54.397 | worker_completed | 08-27 08:17:14.979 | 08-27 08:20:20.976 | present | +326.579 |
| 13503 | 08-27 10:20:49 | succeeded | 0 | 08-27 10:20:51.349 | worker_completed | 08-27 10:21:57.167 | 08-27 10:27:49.069 | present | +417.720 |
| 13571 | 08-28 01:28:34 | succeeded | 0 | 08-28 01:28:36.022 | worker_completed | 08-28 01:30:09.817 | 08-28 01:31:22.870 | present | +166.848 |
| 13589 | 08-28 01:43:31 | succeeded | 0 | 08-28 01:43:31.533 | worker_completed | 08-28 01:44:24.495 | 08-28 01:46:05.691 | present | +154.158 |
| 13685 | 08-28 14:20:16 | succeeded | 0 | 08-28 14:20:16.423 | worker_completed | 08-28 14:21:59.889 | 08-28 16:05:03.170 | present | +6286.747 |
| 13723 | 08-28 16:16:06 | succeeded | 0 | 08-28 16:16:31.936 | worker_completed | 08-28 16:18:36.159 | 08-28 16:46:17.942 | present | +1786.006 |
| 13730 | 08-28 16:24:46 | succeeded | 0 | 08-28 16:24:47.961 | worker_completed | 08-28 16:26:02.840 | 08-28 16:54:04.195 | present | +1756.234 |
| 13835 | 08-29 14:41:13 | succeeded | 0 | 08-29 14:41:13.543 | worker_completed | 08-29 14:42:02.445 | 08-29 14:42:40.680 | present | +87.137 |
| 14193 | 09-01 02:17:41 | succeeded | 0 | 09-01 02:18:17.791 | worker_completed | 09-01 02:18:54.133 | 09-01 02:51:15.219 | present | +1977.428 |
| 14196 | 09-01 02:26:19 | succeeded | 0 | 09-01 02:26:19.885 | worker_completed | 09-01 02:27:27.916 | 09-01 02:55:25.568 | present | +1745.683 |
| 14214 | 09-01 05:37:46 | succeeded | 0 | 09-01 05:37:46.817 | worker_completed | 09-01 05:39:13.462 | 09-01 05:39:19.684 | present | +92.867 |
| 14220 | 09-01 05:40:15 | succeeded | 0 | 09-01 05:40:56.540 | worker_completed | 09-01 05:41:57.504 | 09-01 08:20:52.521 | present | +9595.981 |
| 14243 | 09-01 08:33:20 | succeeded | 0 | 09-01 08:33:21.277 | worker_completed | 09-01 08:34:44.444 | 09-02 03:48:26.633 | present | +69305.356 |
| 14289 | 09-01 14:03:50 | succeeded | 0 | 09-01 14:03:52.065 | worker_completed | 09-01 14:04:22.256 | 09-01 14:14:46.519 | present | +654.454 |
| 14374 | 09-02 03:12:58 | succeeded | 0 | 09-02 03:12:59.661 | worker_completed | 09-02 03:13:54.463 | 09-02 03:31:47.592 | present | +1127.931 |
| 14378 | 09-02 03:18:10 | failed | 5 | 09-02 03:27:23.134 | processing_error:RuntimeError | 09-02 03:22:00.272 | 09-02 03:22:00.278 | present | -322.856 |
| 14428 | 09-02 09:50:12 | failed | 5 | 09-02 09:59:07.429 | processing_error:RuntimeError | 09-02 09:52:19.221 | 09-02 09:54:30.734 | present | -276.695 |
| 14497 | 09-02 14:31:36 | succeeded | 0 | 09-02 14:37:25.967 | worker_completed | 09-02 14:38:34.491 | 09-02 14:47:22.092 | present | +596.125 |

### comparison_claim_token 精确值

| raw | comparison_claim_token |
|---:|---|
| 12798 | `a6b3f9b23a1741f09959c59d703d394a` |
| 12849 | `f65e50b9d9de4c448ac5c7bcc82d4d05` |
| 12897 | `08c6dc7fae7248a3879a6585e8da23d2` |
| 13022 | `150e939f7dd0497ab6f9038fc17d3267` |
| 13076 | `f039c3e02cab4f4fb6e32ff12cd72fbd` |
| 13160 | `97f97bdf01fe4f48b8f17681dac58cd1` |
| 13166 | `90bba03ed50548318e9937c2a5691efc` |
| 13198 | `75fa567d46804264a576e741888b3ee8` |
| 13307 | `9ecf3c9abed7484b8acaab18ea7ca3ed` |
| 13308 | `3e39f4a7515a4f30bada0bc52b492c4e` |
| 13396 | `d34c0efd1dc64113921c438d29c7458d` |
| 13433 | `0692be8a529541038a991e54557acc44` |
| 13503 | `4efdeca813a942a88f21934b1baadd01` |
| 13571 | `2efa83f48a7f4263bf118465db9d8dc9` |
| 13589 | `d6bb8d569fc7489e985e98a3e2eb5656` |
| 13685 | `fba082669a274b139c28824450113408` |
| 13723 | `ff4ea7aad82d4205b01e3f3475208cf1` |
| 13730 | `6b0d83d6ecee4ed9bac30eb4e4c340f3` |
| 13835 | `2fb4749e66e746e1a284d9c5f2587261` |
| 14193 | `eeb96d6385e74ced9f814cd570feede7` |
| 14196 | `8e0d403fcee14ca284f9ad873f952c01` |
| 14214 | `ae90b0f26128493b9a5b7d3233b3cf09` |
| 14220 | `9880040c9718440284d8f04deef3c2bc` |
| 14243 | `5552eb1ee6134e7bb7a7c5ee4ca9e9bf` |
| 14289 | `1b1e6c82b5aa41ab936004be7a9c5fbb` |
| 14374 | `d4df4eaee43a4739b6927c8de9f62f51` |
| 14378 | `24e3e1e5500e4c1e8bfd5c541c38faa1` |
| 14428 | `4b90320e94b04e11a28945997b854575` |
| 14497 | `af01798853af41b8b314dfe36a96f2a3` |

## 4. context reanalysis 时间关联

- 29/29 都有同 raw 的 `context_resolution_attempts`。
- 28/29 的 claim 与最近 context attempt 的 `created_at` 或 `updated_at` 相差 `<1s`。
- raw `13308` 的相关 attempt `3896` 从 `15:13:04.338` 到 `15:16:02.182`；claim
  `15:13:57.401` 位于该区间内。
- raw `14428`：reanalysis attempt `4420` 于 `09:54:30.672` 完成，claim 于
  `09:54:30.734`，相差 0.062 秒。
- raw `14497`：reanalysis attempt `4478` 于 `14:47:22.058` 创建并完成，claim 于
  `14:47:22.092`，相差 0.034 秒。

这一关联与代码调用链共同确认卡死发生在 context reanalysis 路径；它不能恢复具体是哪一个
post-claim 调用抛错。

## 5. 执行副作用核查

对每条以当前 `comparison_claim_token` 为 generation，并同时核查 raw message 的直接
execution/management 关联：

| 证据 | 命中行数 |
|---|---:|
| 当前 generation signal candidate | 0 |
| 直接 execution binding | 0 |
| 直接 execution order leg | 0 |
| 直接 execution event | 0 |
| 当前 generation management batch | 0 |
| management envelope | 0 |
| management target / started target | 0 / 0 |

逐行查询结果一致：
`12798, 12849, 12897, 13022, 13076, 13160, 13166, 13198, 13307, 13308, 13396,
13433, 13503, 13571, 13589, 13685, 13723, 13730, 13835, 14193, 14196, 14214,
14220, 14243, 14289, 14374, 14378, 14428, 14497` 的
`(current-generation candidate, direct binding, direct order leg, direct execution event,
current-generation management batch, envelope, target, started target)` 均为
`(0, 0, 0, 0, 0, 0, 0, 0)`。

只有 raw `14214` 有一条旧 generation candidate 与 terminal instruction item；item `909`
在 `05:39:13.550` 已为 `succeeded`，早于当前 generation claim
`05:39:19.684`。当前代码见到已有 instruction items 时只会 claim pending item；这里没有
可 claim item，因此本次 generation 即使进入 executor 适配器，也只会汇总既有 terminal
结果，不会再调用交易路径。该 raw 同样没有 batch、binding、order leg 或 event。

因此，29 条都归入 **(a) 下单前/无本次 generation 执行证据**；没有 (b) 类。这个结论是
当前生产持久化证据与实际分支条件的合并判断，不把缺失的交易所历史数据伪装成完整历史回放。

## 6. 时间归因

按 posted_at：08-24 为 3 条、08-25 为 4 条、08-26 为 3 条、08-27 为 3 条、08-28 为
5 条、08-29 为 1 条、09-01 为 6 条、09-02 为 4 条；08-30/31 为 0。

- raw `14220` 的 claim `09-01 08:20:52Z` 落在已记录 R1 L1 窗口
  `08:08:41–08:23:42Z`；只确认时间重合，不断言部署导致。
- raw `13396` 在 R5 rollback 后约 19 分钟，raw `14289` 在 `3205b074` 观察结束后约
  25 分钟，均不算窗口内。
- 没有 claim 落在 08-30/31 冻结期，也没有 claim 落在 09-01
  `22:20:06–22:25:44Z` 激活/解冻窗口。
- raw `14428` 与 `14497` 发生时 worker 始终为 PID `3315574`、`NRestarts=0`；当前
  systemd invocation 自 `2026-09-01T22:25:43Z` 起运行。对应时段 journal 无启停、OOM 或
  kill 记录。

所以这不是只在部署/重启时产生的孤儿，而是普通运行中的慢性异常路径。

## 7. 最新 journal 证据

### raw 14428

- `09:52:19Z`：主作业先以
  `AuthoritativeProcessingFailed: authoritative processor returned authoritative_failed`
  进入 retry。
- 当前 orphan claim 写于 `09:54:30.734Z`。
- `09:54:48Z`、`09:56:01Z`、`09:59:07Z` 的后续重试均在
  `save_pending_authoritative_decision()` 抛
  `RuntimeError("authoritative execution is already in progress")`；最终失败路径也在
  terminal save 时命中同一保护。
- journal 没有记录 `09:54:30Z` 那次 reanalysis 的首次 post-claim traceback；该异常被
  context worker 转成状态后吞掉。

### raw 14497

- orphan claim 写于 `14:47:22.092Z`，与 reanalysis attempt `4478` 相差 0.034 秒。
- worker journal `14:30–15:00Z` 及轮转应用日志均无 raw `14497`、traceback、ERROR、重启、
  OOM 或 kill 记录。
- context attempt 最终只留下 `last_error=RuntimeError`；后续重试撞旧锁也是 RuntimeError，
  因此不能把这个类名当作首次异常的精确归因。

## 已确认与需进一步确认

**已确认：**

- claim 后无 finally/兜底；任一中间异常或进程终止会遗留 running。
- assessment 不会在 claim 后变成 authoritative_failed。
- 27 条 job succeeded 是 context worker 吞异常并与外层 job 结算解耦导致。
- 当前 29 条无本次 generation 执行副作用证据，未发现 (b) 类。
- 该问题可在无部署、无重启的稳态运行中继续产生。

**需进一步确认：**

- 29 次最初 post-claim 异常分别发生在哪个调用点。现有持久化与日志不足以还原，不能猜成
  SQLite lock、网络异常或某个具体函数。
- 当前唯一交易所仓位的逐仓归属。本轮可用完整快照端点没有返回逐仓字段；这不改变“没有任何
  本次 generation 本地执行证据”的结论，但不能将其写成新的逐仓历史证明。

本轮没有修改生产代码、schema、decision/job 行、服务、配置或交易所状态。
