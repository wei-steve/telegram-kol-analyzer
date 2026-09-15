# 消息处理任务：守护重启、锁异常容错、队列停摆告警

日期：2026-09-16
状态：用户 2026-09-16 批准，实施中（子代理）
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
