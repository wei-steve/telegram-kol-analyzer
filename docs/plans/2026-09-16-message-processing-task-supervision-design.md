# 消息处理任务：守护重启、锁异常容错、队列停摆告警

日期：2026-09-16
状态：已于 2026-09-16 部署生产 `6cce08b1`（回滚 `d8714c48`）
关联：`docs/plans/2026-09-15-context-reanalysis-loop-analysis.md`（同一晚的部署重启暴露了本问题）

## 1. 事故

2026-09-16（北京时间）：

| 时间 | 事件 |
| --- | --- |
| 00:07 | worker（pid 1849924，`ae43312a`）正常处理完 job 5242 |
| 00:18:03 | `runtime_loop_health` 记录事件循环卡顿 25.8 s（卡在 `system_operator_bot.run_strategy_management_notification_loop` 的同步 `session.flush()`） |
| 00:18:26 | `worker_command_worker_task` 因 `sqlalchemy.exc.OperationalError: database is locked` 退出（`worker_command_jobs.mark_expired_executing_commands_uncertain`） |
| 00:18:27 | `message_processing_worker_task` 因同一异常退出（`message_processing_worker.py:787 run_message_processing_worker_loop` → `:338 claim_message_processing_jobs` 的 `BEGIN IMMEDIATE`） |
| 00:35 / 01:02 / 01:03 | 三个群各一条消息入库排队（job 5243/5244/5245），无人认领 |
| 01:21:57 | 部署 `d8714c48` 重启 worker；启动时的 gap-recovery 把三条超过 15 分钟的消息标为 `expired_stale_instruction`，决策写 `识别失败 / authoritative_gap_recovery_expired` |

`busy_timeout` 已是 30 s（`db.py:886`），说明写锁被占超过 30 s。占锁者未定位（另议，见第 6 节）。
但无论谁占锁，**认领任务不该因一次瞬时异常永久退出**。

## 2. 根因（代码）

1. `run_message_processing_worker_loop`（`message_processing_worker.py:760-820`）的主循环里，
   `_load_trading_settings_with_observed_at`、`claim_message_processing_jobs` 都在 `asyncio.to_thread` 里直接调用，
   没有任何 try/except；`for task in done: task.result()` 也会把单个 tick 的未预期异常抛到循环顶层。任何一次异常 = 循环结束。
2. `ensure_message_processing_worker_mode`（`web_app.py:6639`）用 `asyncio.create_task(runner(...))` 直接起任务，
   只挂了 `_log_background_task_result` 记日志的回调。已有的守护包装 `_supervise_restartable_background_task`
   （`web_app.py:1386`，指数退避重启、连续 10 次失败才放弃并记 critical 事故）只包了
   `telegram_bot_command_task` / `runtime_incident_notification_task` / `system_operator_bot_command_task` 三个任务。
3. `worker_command_worker_task`（`web_app.py:5877`）同样裸起。
4. 任务死了之后进程仍是 `active`，systemd 与页面「监控中」都看不出来。`ensure_message_processing_worker_mode`
   只在启动和 `web_app.py:9277`（设置刷新）时被调用，不会自动补起。

历史：journal 保留期（08-24 起）内 `message_processing_worker_task` 退出 1 次（本次）；
`message_processing_jobs.last_reason='expired_stale_instruction'` 近 30 天出现 13 天，其中 08-31 有 281 条、09-12 有 45 条
（原因可能不同，但都是「队列有消息而无人处理」的表现）。

## 3. 方案

### 3.1 两个任务纳入守护包装（必做）

- `ensure_message_processing_worker_mode`：把 `asyncio.create_task(app.state.message_processing_worker_runner(...))`
  改为 `asyncio.create_task(_supervise_restartable_background_task("message_processing_worker_task", lambda: app.state.message_processing_worker_runner(...同样的参数...), session_factory=app.state.session_factory, runtime_config=app.state.runtime_incident_config, supervision=_task_supervision(app, "message_processing_worker_task")))`。
  现有的两个 done-callback（`_log_background_task_result`、`clear_completed_message_processing_worker`）保留。
- `worker_command_worker_task`（`web_app.py:5877-5889`）同样改法，任务名 `"worker_command_worker_task"`。
- 守护包装本身不改（退避 1 s → 60 s，健康运行 300 s 归零，连续 10 次放弃并记 critical 事故）。

### 3.2 循环内对锁异常做单次容错（必做）

`run_message_processing_worker_loop`：

- 把「读设置 + 认领」这一段包在 `try/except sqlalchemy.exc.OperationalError as exc`：
  记 `logger.warning("message processing claim skipped: %s (consecutive=%d)", type(exc).__name__, n)`，
  `await asyncio.sleep(min(interval * 2 ** n, 5.0))`，`continue`。连续成功一次后 `n` 归零。
  连续 `>= 20` 次（约 100 s 全部失败）仍抛出，让守护包装接手重启并计数。
- `for task in done: task.result()` 改为逐个 try/except `Exception`：
  `logger.exception("message processing tick failed")` 后继续；`asyncio.CancelledError` 原样抛。
  单个 tick 的异常不再终结整个循环。tick 内部已有的失败分类逻辑（`_run_claim_body` 等）不动。
- 只捕获这两类；其余异常（编程错误）照旧冒出，由 3.1 的守护重启并在 10 次后记事故。

### 3.3 队列停摆告警（必做）

新增独立协程 `run_message_processing_queue_stall_monitor(session_factory, *, activity, interval_seconds=60, stall_after=timedelta(minutes=3), cooldown=timedelta(minutes=15), capture, notify)`，
放在 `message_processing_worker.py`：

- 每 `interval_seconds` 查一次：`message_processing_jobs` 中 `status='pending'`（或该表表示「待认领」的状态）且
  `claim_token IS NULL` 且 `enqueued_at <= now - stall_after` 的行数 `stalled`。
- `stalled > 0` 且距离上次告警超过 `cooldown` → 调用 `capture(...)` 记运行事故
  `incident_type="message_processing_queue_stalled"`，severity `high`，摘要含 `stalled` 数、最老一条的入队时间、
  `activity` 最近一次认领时间；再调用 `notify(text)` 向系统操作机器人发一条
  「⚠️ 消息处理队列停摆：N 条消息超过 3 分钟无人认领，最老 …」。
- `stalled == 0` → 重置冷却。
- 事故类型注册：在 `runtime_incident_adapters.py` 按 `capture_context_worker_state` 的写法新增
  `capture_message_processing_queue_stalled(...)`；`RuntimeIncidentConfig.captures()` 若按白名单工作，
  把新类型加进默认白名单（找 `context_worker_exhausted` 出现的配置位置照抄）。
- 启动：在 `web_app.py` 起 `loop_lag_monitor_task` 的旁边（`web_app.py:5503`），
  仅当 `runtime_role_starts_singleton_task(app.state.runtime_role, "message_processing_worker")` 时起，
  任务名 `"message_processing_queue_stall_monitor_task"`，也用 3.1 的守护包装。
  `notify` 用 `send_system_operator_bot_message`（与 `notify_terminal_failure` 同一通道，`system_operator_bot_enabled` 为假时静默）。
- `MessageProcessingActivity` 增加 `last_claim_at: datetime | None`，在 `note_refill(n)` 且 `n > 0` 时更新，供摘要使用。

### 3.4 不做

- 不改 `busy_timeout`、不改 SQLite 模式、不改 `BEGIN IMMEDIATE`。
- 不定位 00:18 的占锁者（第 6 节另议）。
- 不改 gap-recovery 的 15 分钟过期规则：它是防止执行过期信号的安全设计。
- 不改 `_supervise_restartable_background_task` 的参数。

## 4. 测试

- `tests/test_message_processing_worker.py`：
  - 循环：`claim_message_processing_jobs` 抛一次 `OperationalError` → 记警告、循环继续、下一轮正常认领；
    连续 20 次 → 抛出；单个 tick 任务抛 `RuntimeError` → 记日志、循环继续处理下一条；`CancelledError` 仍能取消循环。
  - 停摆监视器：3 条 pending 且超过 3 分钟 → `capture` 与 `notify` 各调一次；冷却内再查不重复；
    队列清空后再停摆会再次告警；`stall_after` 内的新消息不算。
- `tests/test_web_app.py`（或现有测试守护包装的文件）：
  - `message_processing_worker_task` 与 `worker_command_worker_task` 的 runner 抛异常后被重启（restarts ≥ 1），
    `app.state.background_task_supervision` 里有这两个任务名。
  - 停摆监视器任务在 worker 角色启动、web 角色不启动。
- `tests/test_runtime_incident*.py`：新事故类型能被 `captures()` 接受并落库。
- 全量 `PYTHONPATH=. uv run pytest -q` 通过。

## 5. 实施要点

- 改动文件：`message_processing_worker.py`、`web_app.py`、`runtime_incident_adapters.py`（+ 配置默认白名单所在文件）、上述测试。
- 参数化优先用现有 `app.state.*` 值，不新增环境变量；`stall_after` / `cooldown` 作为函数默认值即可。
- 守护包装对「健康运行 300 s 后再失败」会把连续失败计数归零，因此偶发锁异常不会累计到 10 次放弃。

## 6. 另议

- 00:18 是谁占写锁 > 30 s：怀疑 worker 自身线程里的长写事务（lifecycle_monitor 00:17:32 刚跑完一轮）或
  `strategy_management_notification_loop` 在事件循环线程上做同步 DB 写（stall stack 指向它）。
  `runtime_loop_health` 记录的 >15 s 卡顿：09-13 两次、09-14 三次、09-15 九次，在上升。值得单独查。
- `expired_stale_instruction` 08-31 的 281 条与 09-12 的 45 条是否同一机制，未核。

## 7. 实施记录

日期：2026-09-15（子代理实施，worktree `.claude/worktrees/agent-afd774a5c9c55fd0d`，起点 `d81394be`）。
3.1 / 3.2 / 3.3 全部按第 3 节实施；3.4 一条未动。

### 7.1 改动文件

| 文件 | 改动 |
| --- | --- |
| `src/telegram_kol_research/message_processing_worker.py` | 3.2 循环容错；`MessageProcessingActivity.last_claim_at`；3.3 新增 `count_stalled_message_processing_jobs`、`run_message_processing_queue_stall_monitor`、`_queue_stall_instant_label`；新增常量 `MESSAGE_PROCESSING_CLAIM_LOCK_FAILURE_LIMIT=20`、`MESSAGE_PROCESSING_CLAIM_LOCK_MAX_BACKOFF_SECONDS=5.0`、`DEFAULT_QUEUE_STALL_MONITOR_INTERVAL_SECONDS=60.0`、`DEFAULT_QUEUE_STALL_AFTER=3min`、`DEFAULT_QUEUE_STALL_ALERT_COOLDOWN=15min` |
| `src/telegram_kol_research/web_app.py` | 3.1 两个任务纳入 `_supervise_restartable_background_task`；3.3 起 `message_processing_queue_stall_monitor_task`（同样守护、仅 worker 角色）、`app.state.message_processing_queue_stall_monitor_task` 初始化与关停取消；新增 `_capture_message_processing_queue_stall` / `_notify_message_processing_queue_stall` 两个 sink 工厂 |
| `src/telegram_kol_research/runtime_incident_adapters.py` | 新增 `capture_message_processing_queue_stalled`（照 `capture_context_worker_state` 写法，severity `high`） |
| `src/telegram_kol_research/config.py` | `ALWAYS_NOTIFIED_INCIDENT_TYPES` 增加 `message_processing_queue_stalled` |
| `src/telegram_kol_research/runtime_incidents.py` | `_SUMMARY_FIELDS` 增加 `stalled_jobs` / `oldest_enqueued_at` / `last_claim_at` |
| `tests/test_message_processing_worker.py` | 9 个新用例 + 日志采集辅助 |
| `tests/test_web_app.py` | 3 个新用例 |
| `tests/test_runtime_incident_adapters.py` | 3 个新用例 |
| `tests/test_runtime_event_loop_blocking_census.py` | 允许清单加 2 条（见 7.3） |

`_supervise_restartable_background_task`、`busy_timeout`、`BEGIN IMMEDIATE`、gap-recovery 15 分钟规则，一行未改。

### 7.2 新增用例

`tests/test_message_processing_worker.py`：

- `test_one_lock_error_only_skips_that_claim_and_the_loop_keeps_running`
- `test_twenty_consecutive_lock_errors_leave_the_loop_to_the_supervisor`
- `test_one_failing_tick_does_not_stop_the_next_message_from_running`
- `test_cancelling_the_loop_still_stops_it`
- `test_a_stalled_queue_is_captured_and_announced_once`
- `test_the_stall_alert_is_not_repeated_inside_its_cooldown`
- `test_a_queue_that_drains_and_stalls_again_alerts_again`
- `test_a_message_younger_than_the_threshold_is_not_a_stall`
- `test_a_deferred_retry_and_a_shadow_row_are_not_counted_as_stalled`（第 4 节之外补的，见 7.5 第 1 条）

`tests/test_web_app.py`：

- `test_the_message_processing_worker_task_is_restarted_after_it_fails`
- `test_the_worker_command_worker_task_is_restarted_after_it_fails`
- `test_the_queue_stall_monitor_runs_only_in_the_worker_role`

`tests/test_runtime_incident_adapters.py`：

- `test_a_stalled_message_processing_queue_is_recorded_with_its_numbers`
- `test_a_queue_stall_is_not_recorded_when_the_type_is_not_captured`
- `test_the_queue_stall_type_is_captured_without_an_environment_list`

### 7.3 既有测试调整

只有一处，且不是为了让测试变绿而改语义：

- `tests/test_runtime_event_loop_blocking_census.py` 的 `KNOWN_BLOCKING_CALLS` 增加
  `message_processing_queue_stall_monitor -> utc_now` 与 `-> _queue_stall_instant_label`。
  该清单是「async while 循环里直接调用同步函数」的静态普查白名单，按其文件头的规定，
  新增项必须写明为何可以留下。监视器每轮读一次时钟，只有在要告警时才用 `strftime`
  格式化两个时间；两者都不碰 session / client / 网络，与清单里已有的
  `run_semantic_review_loop -> utc_now` 同类。监视器唯一的数据库读走 `asyncio.to_thread`，
  普查未报，也没有进清单。

没有其他既有用例需要改：守护包装让 `app.state.message_processing_worker_task` /
`worker_command_worker_task` 从 runner 协程变成了 supervisor 协程，但既有断言只看
「是不是 None」「有没有被取消」「runner 收到了哪些 kwargs」，形态变化没有影响。
`tests/test_mimo_step2_authority_not_produced.py` 里按源码切片检查
`group_trading_mode_provider` 的那条断言，在 runner 调用被包进
`start_message_processing_worker_runner()` 之后仍然成立。

（实施过程中这两条曾一度失败：`tests/test_message_processing_worker.py` 新增的两个用例
用 `caplog` 断言日志，单跑通过、全量跑失败——全量跑时前面的用例已经调用过
`configure_application_logging`，worker logger 有了自己的 handler，记录不再落到
`caplog` 的 root handler 上。改为在测试内直接给 worker logger 挂一个采集 handler，
这是新代码自身的问题，不是既有测试的调整。）

### 7.4 全量测试

`PYTHONPATH=. uv run pytest -q`，最后三行原样：

```

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
8890 passed, 4 skipped, 107 warnings in 743.25s (0:12:23)
```

改动前基线同一命令为 8887 passed / 4 skipped（首轮 3 个失败，均由本次改动引入，已在 7.3 说明并修好）。

### 7.5 取舍与设计未覆盖之处

1. **「待认领」的判定范围**。第 3.3 节写的是 `status='pending'` 且 `claim_token IS NULL` 且
   `enqueued_at <= now - stall_after`。按指挥会话「以 `claim_message_processing_jobs` 的 SQL 为准」的
   要求，实现另加了该 SQL 自己的两个限定：`shadow = 0`（历史行，worker 本来就不认领）与
   `next_attempt_at IS NULL OR next_attempt_at <= now`（重试退避最长 300 s，超过 3 分钟阈值；
   不加这一条，一次正常退避就会被误报成停摆）。两者都只让告警更保守，不会漏掉真正无人认领的消息。
2. **事故类型白名单的落点**。`captures()` 只读环境变量 `TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES`
   加 `ALWAYS_NOTIFIED_INCIDENT_TYPES`。`READ_ONLY_CAPTURE_PROFILE` 是封闭的历史集合
   （有逐字断言的测试），不是运行时白名单，故未动；新类型加进 `ALWAYS_NOTIFIED_INCIDENT_TYPES`，
   这样服务器上手写的清单忘记它也照样落库。副作用：该事故也会被运行时事故通知任务发一条，
   于是一次停摆最多两条消息——监视器直发的那条（内容按第 3.3 节）与事故台账的通用渲染。
   保留，因为第 3.3 节明确要求 `capture` 与 `notify` 都调。
3. **摘要字段**。事故摘要有封闭词表，`stalled` 数、最老入队时间、最近认领时间在词表里都没有
   合适的现成字段（硬套 `retry_count` / `last_failure_at` 会让台账读起来是错的）。
   按该文件既有的做法（每次为新告警补字段并写明理由）增加了三个字段。
   这让改动多碰了 `runtime_incidents.py` 一个第 5 节没列的文件。
4. **监视器的数据库读放在 `asyncio.to_thread`**。第 3.3 节未规定。事故本身就是事件循环被
   同步 DB 写卡住，监视器在循环上做同步读会重复同一类问题。
5. **监视器的异常不自吞**。`capture` 已经是 fail-open 的（`capture_runtime_incident_best_effort`），
   `notify` 或数据库读若抛异常，交给 3.1 的同一个守护包装退避重启。代价是重启后冷却状态
   （进程内变量）归零，下一轮停摆会立刻再告一次；相对「监视器静默死掉」这是更安全的一侧。
6. **关停时取消新任务**。第 3.3 节未提。若不取消，lifespan 关停会被这个 `while True` 挂住，
   所以按 `loop_lag_monitor_task` 的写法加了取消块。
7. **守护包装的放弃计数是按任务名跨重启累积的**。`ensure_message_processing_worker_mode`
   在设置刷新时会重新起一次 supervisor，但 `_task_supervision` 返回的是同一条记录：
   若上一轮已经连续失败 10 次放弃，新 supervisor 起来后第一次失败就会再次放弃。
   未改——第 3.4 节禁止改动包装的参数与行为，且这条只在「已经彻底坏掉」之后才生效。
8. **未做**：没有为新事故类型写专门的 Telegram 渲染分支，通用渲染器
   （`format_runtime_incident_notification`）会输出组件、源状态、原因代码，足够定位；
   第 3.3 节也只要求监视器自己那条消息的文案。

## 8. 部署记录（指挥会话补记）

- 子代理提交 `6cce08b1`；指挥会话独立复跑全量 8890 passed / 0 failed / 4 skipped（783 s）。
- 2026-09-16 `tg-deploy 6cce08b1398a4a8de6357f22ece9bbe4c039feae`，回滚 SHA `d8714c48`。
  worker / web / ingest 均 active，启动无异常；共享分支 = 部署 SHA；部署后 2 小时内的 job 全部 succeeded，
  无超过 3 分钟未认领的 pending。
- 验证方式：journal 里 `Supervised background task message_processing_worker_task failed` /
  `message processing claim skipped` 出现即说明容错生效；系统操作机器人收到「消息处理队列停摆」即说明监视器生效；
  `message_processing_jobs.last_reason='expired_stale_instruction'` 应不再成批出现。
- 子代理审阅要点：一次停摆会产生两条 Telegram 消息（监视器直发 + 事故台账通用渲染），保留；
  守护包装的连续失败计数按任务名跨重启累积（3.4 禁止改包装），已记录为已知边角。
