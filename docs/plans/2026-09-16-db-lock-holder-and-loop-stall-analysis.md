# 谁占了 30 秒写锁 · 事件循环卡顿为什么在上升

日期：2026-09-16
状态：4.1 + 4.2 + 4.3 已于 2026-09-16 部署生产 `5f26e721`（回滚 `6cce08b1`）
关联：`docs/plans/2026-09-16-message-processing-task-supervision-design.md` 第 6 节「另议」的两项。
数据来源：worker journal（保留期 08-24 起）、生产库 `data/research.db` 只读查询。

## 1. 结论先行

1. **占锁者找到了，而且两次都是同一段代码**：`management_target_confirmation.expire_stale_management_confirmations`
   在一个打开的写事务里先写审计事件（`execution_events`，`session.flush()` 已拿到写锁），
   然后在同一线程上调用 `notify` → `capture_runtime_incident_best_effort` → `record_runtime_incident` 用**另一个连接**写
   `runtime_incidents`。同一线程、两个连接、第二个要等第一个释放写锁——第一个要等 `notify` 返回才 commit。
   于是内层连接等满 `busy_timeout` 30 秒后失败（日志：`Runtime incident capture failed open: type=management_target_needs_confirmation error=OperationalError`），
   外层才 commit。这 30 秒内进程里所有其他写入者也在等同一把锁，到期的一起报 `database is locked`。
2. **两次事故的证据完全一致**：
   - 09-16 00:17:56.6 写入 `execution_events` #4262（`management_target_confirmation_reminder`，消息 16975）→ 00:18:26.665 内层捕获失败 →
     00:18:26.96/27 `worker_command_worker_task` / `message_processing_worker_task` 因 `BEGIN IMMEDIATE` 锁超时退出。
     该 3 分钟窗口里全库**只有这一条写入**。
   - 09-14 04:26:16 UTC（12:26 CST）写入 `execution_events` 一条 → 12:26:16 与 12:26:46 **连续两次**内层捕获失败（先提醒后超时，各 30 秒）→
     12:26:46 `telegram_live_listener._enqueue_processing_jobs` 锁超时、事件循环卡顿 25.5 秒。
3. **为什么一次锁会把整个事件循环冻住 25 秒**：`system_operator_bot.run_strategy_management_notification_loop`
   每 5 秒在**事件循环线程上同步**跑 `deliver_strategy_management_notifications`（`enqueue_… → session.flush()`），
   它撞上锁就把整个事件循环挂住直到 busy_timeout。`runtime_loop_health` 记录的 25.8 秒 / 25.5 秒卡顿就是它。
   它还 `except Exception: pass`，所以什么都不留。这是放大器，不是根因。
4. 频率：09-13 起每天 1–2 条待确认消息，每条产生提醒 + 超时两次自锁，各 30 秒。09-13 至 09-16 的 18 次 ≥15 秒事件循环冻结里，
   10 次就是它（逐条对上，见 3.2），其余 8 次是 09-15 晚指挥会话自己在生产库上跑分析扫描造成的 I/O 争用。
   在今天的守护修复之前，这个自锁每天都有机会杀死消息认领任务。

## 2. 证据链

### 2.1 00:18 事故的时间线（北京时间）

| 时间 | 证据 |
| --- | --- |
| 00:17:32 | lifecycle_monitor 一轮结束（无关，只是最后一条正常日志） |
| 00:17:52 | deepcoin_reconcile_round 记录完成（无关） |
| 00:17:56.6 | `execution_events` #4262 created_at：`action=management_target_confirmation_reminder, reason=confirmation_reminder, source_message_id=16975, chat=-1002337721508, after={"minutes_left":30}` |
| 00:18:03 | `runtime_loop_health` 栈采样：事件循环线程卡在 `run_strategy_management_notification_loop → enqueue_strategy_management_notifications → persist_… → session.flush()` |
| 00:18:26.665 | `Runtime incident capture failed open: type=management_target_needs_confirmation source=message_instruction_target error=OperationalError` |
| 00:18:26.96 | `runtime_loop_health`：事件循环卡顿 25 821.7 ms |
| 00:18:26.96 | `worker_command_worker_task` 退出：`worker_command_jobs.mark_expired_executing_commands_uncertain` `BEGIN IMMEDIATE` database is locked |
| 00:18:27 | `message_processing_worker_task` 退出：`claim_message_processing_jobs` `BEGIN IMMEDIATE` database is locked |

在 16:16:30–16:19:30 UTC 窗口内对全库所有带时间戳的表扫描，**唯一**的写入就是 `execution_events.created_at = 16:17:56.618600`。
web、ingest 两个进程该时段只有 5 秒一次的监控轮询，没有写入。

### 2.2 代码路径

`management_target_confirmation.py:372-465 expire_stale_management_confirmations`（调用方 `strategy_management_worker.py:250`，
运行在管理工作线程 `run_on_management_worker` 上）：

```
with session_factory() as session:            # 387
    for raw_message_id, items in by_message:
        ...
        _audit(session, ..., action=REMINDED)  # 447 → record_execution_event → session.add + session.flush()  ← 拿到写锁
        if notify is not None:
            notify(...)                        # 453 → _notify_confirmation_lapse
    session.commit()                           # 464  ← 到这里才放锁
```

`strategy_management_worker.py:236 _notify_confirmation_lapse` → `capture_runtime_incident_best_effort(capture_management_target_needs_confirmation, session_factory, ...)`
→ `runtime_incident_adapters._capture` → `runtime_incidents.record_runtime_incident`：`with session_factory() as session:` 新连接 `INSERT … RETURNING`。
超时（`CONFIRMATION_TIMEOUT`）分支（约 425 行）同样在事务内调 `notify`，09-14 那次就是提醒 + 超时连着两次各 30 秒。

对 `src/` 做了一次 AST 普查（在 `with session_factory() as session:` 块体内再调用会开新连接**写库**的函数）：
**只有这一处**。另有 4 处在事务内调 `load_trading_settings`（新连接只读，WAL 下不与写锁冲突），不构成同类问题。

### 2.3 为什么以前没被发现

- 内层写入是 `best_effort`，失败只记一条 WARNING；外层最终 commit 成功，业务结果正确。
- 事件循环卡顿有采样，但栈指向的是被卡住的通知循环（受害者），不是持锁的管理线程（肇事者）。
- 直到 09-16 认领任务被它杀死、三条消息过期，才顺藤摸到。

## 3. 事件循环卡顿

### 3.1 数量

| 日期 | ≥3 s 卡顿 | ≥15 s 卡顿 |
| --- | --- | --- |
| 09-10 | 4 | 0 |
| 09-11 | 9 | 0 |
| 09-12 | 8 | 0 |
| 09-13 | 7 | 2 |
| 09-14 | 11 | 3 |
| 09-15 | 51 | 9 |
| 09-16（至 08:00） | 6 | 4 |

### 3.2 ≥15 秒卡顿逐条归因（09-13 起共 18 次）

| 时间（CST） | 时长 | 归因 |
| --- | --- | --- |
| 09-13 16:42 / 17:12 | 27.9 s / 26.4 s | 消息 16485 的确认提醒（08:42 UTC）/ 确认超时（09:12 UTC），2.2 节的自锁 |
| 09-14 12:26 / 12:56 | 25.5 s / 27.1 s | 消息 16642 的提醒 / 超时 |
| 09-14 23:48 / 09-15 00:18 | 26.3 s / 26.7 s | 消息 16720 的提醒 / 超时 |
| 09-16 00:18 / 00:48 | 25.8 s / 30.1 s | 消息 16975 的提醒 / 超时（本次事故） |
| 09-16 04:21 / 04:51 | 29.9 s / 27.3 s | 消息 17010 的提醒 / 超时 |
| 09-15 20:22–20:48（8 次） | 15–30 s | **指挥会话自己的只读分析扫描**（见下） |

前 10 次与 `execution_events` 里确认提醒/超时事件的时间**逐一对应**（提醒后 30 分钟必有超时，`REMINDER_LEAD_MINUTES=30`），
每次 25–30 秒＝`busy_timeout`。也就是说：**除了 09-15 晚上那一小时，所有 ≥15 秒的事件循环冻结都是 2.2 节这一段代码造成的。**

09-15 20:00–20:50 那一小时有 29 次 ≥3 秒卡顿、8 次 ≥15 秒。那正是指挥会话在生产库上跑上下文触发分析
（`ctx_audit.py` 等三个脚本，对 `context_resolution_attempts × recognition_decisions × raw_messages` 做 14 天全表关联，
1.1 GB 库反复整库读）的时段。采样栈显示当时事件循环线程卡在 `load_trading_settings`（10.7 s）、
`_management_payload_for_batch`（21.4 s）、`enqueue_strategy_management_notifications`（19.5 s）这类**普通小查询**上——
不是锁，是磁盘 I/O 被整库扫描占满，每个小查询都要等几秒到几十秒。
WAL 模式下只读不阻塞写入，但 I/O 争用一样会把在事件循环线程上同步跑的 DB 操作拖成秒级卡顿。
这一小时的 51 次里有 29 次是我造成的，「卡顿在上升」的印象主要来自这一天；扣掉它，09-13 到 09-16 的基线是每天 7–12 次 ≥3 秒，
其中 ≥15 秒的全部是 2.2 节的自锁。

**教训与规则**：以后在生产库上做分析，先 `VACUUM INTO` 一份快照到 `/tmp`（仓库里 `manual_pending_entry_reconciliation.py:930`
已有这个用法），对快照跑扫描；或至少 `nice -n 19 ionice -c3`。不要对线上 `research.db` 直接跑全表关联。

### 3.3 ≥3 秒卡顿的常态来源（采样栈，78 个样本）

| 最深业务帧 | 样本数 | 最长 | 说明 |
| --- | --- | --- | --- |
| `system_operator_bot` 通知循环（`enqueue_… / persist_… / _management_payload_for_batch`） | 32 | 21.4 s | 每 5 秒在事件循环线程上同步读写库，任何争用都直接冻结整个进程 |
| `cli.py web`（帧被截断，通常也是通知循环） | 17 | 18.7 s | |
| `message_processing_worker.process_message_job` | 4 | 3.2 s | tick 内同步段 |
| `trading_settings.load_trading_settings` | 3 | 10.7 s | 在事件循环线程上同步读设置 |
| `lifecycle_monitor._fetch_candles_full / _candle_from_payload` | 4 | 12.8 s | K 线解析在事件循环线程上 |
| `authoritative_recognition._run_leased_authoritative_execution` | 2 | 3.1 s | |
| `telegram_bot_commands._delete_webhook` | 2 | 6.4 s | 启动时同步网络调用 |

结论：卡顿的**常态来源**是通知循环把 DB 放在事件循环线程上（4.2）；**尖峰来源**是 2.2 的自锁（4.1）和 09-15 我自己的扫描。

## 4. 修复方案（待拍板）

### 4.1 根因：把「通知」挪到 commit 之后（必做，改动小）

`expire_stale_management_confirmations`：事务内只收集待通知项 `pending_notifies: list[tuple]`，
`session.commit()` 之后再逐个调用 `notify`。提醒与超时两个分支同改。
效果：写锁持有时间从「一次 Telegram/事故写入的时长」降到毫秒级；内层写入不再自锁。
测试：现有 `tests/test_management_target_confirmation*.py` 里对 `notify` 调用次数/参数的断言不变，
新增一条「`notify` 抛异常时审计事件仍已提交」和一条「`notify` 在 commit 之后被调用」（用 `session_factory` 包装记录顺序）。

### 4.2 放大器：通知循环的同步写库挪出事件循环线程（建议做）

`run_strategy_management_notification_loop`：`await asyncio.to_thread(deliver_strategy_management_notifications, ...)`。
该函数是 `async def` 但内部全是同步 DB + 同步 Telegram 发送（要核对 `claim/deliver` 里 HTTP 调用是否同步；若是 `await`，
只把 `enqueue_strategy_management_notifications` 与 `claim_next_…` 两个纯 DB 步骤 `to_thread`）。
同时把 `except Exception: pass` 改成 `logger.exception`，否则下次还是黑箱。
`tests/test_runtime_event_loop_blocking_census.py` 的 `KNOWN_BLOCKING_CALLS` 会相应减少一项或需要调整。

### 4.3 防御：写事务内禁止再开写连接（可选，后续）

在 `db.py` 的 session 工厂加一个线程局部计数：同一线程内已有 session 处于写事务时再开 session 并执行写语句 → 记 WARNING 带栈。
只记不拦，先看一周有没有漏网的同类路径。

### 4.4 不建议

- 调大 `busy_timeout`：只会把 30 秒冻结变成更长。
- 调小 `busy_timeout`：真正的短暂争用会更容易失败。

## 5. 另议（本文不处理）

- `lifecycle_monitor` 每轮对**每个活跃群**发一次 `exchange_snapshot_changed` 重分析事件（`lifecycle_monitor.py:663-668`），
  60 天里因此重排 85 次；在方案甲和封顶之后影响有限，先观察。
- `source_deletion_exit_timeout` 每 5 秒对 exit 310/311 记一次「卡住」告警并尝试捕获事故（被 bounds 拒绝），只刷日志不写库，
  但把 journal 灌成噪音。exit 310/311 本身为什么卡了两天，值得单独看。

## 6. 已批准的实施规格（4.1 + 4.2 + 4.3）

用户 2026-09-16 拍板「做 1 2 3」。以下是给子代理的精确规格；4.4 不做，第 5 节不做。

### 6.1 通知挪到 commit 之后（4.1）

- 文件 `src/telegram_kol_research/management_target_confirmation.py`，函数 `expire_stale_management_confirmations`（约 372-465 行）。
- 事务块内（`with session_factory() as session:`）**删除**两处 `notify(...)` 调用（超时分支约 425 行、提醒分支约 453 行），
  改为把参数追加到局部列表 `deferred_notifies: list[dict]`（键：`raw_message_id`、`kind`、`item_ids`，与现有调用参数一致）。
- `session.commit()` 之后、`return` 之前：`for call in deferred_notifies: notify(**call)`，每个调用单独 `try/except Exception`
  → `logger.exception("management confirmation notify failed raw_message_id=%s kind=%s", ...)`，一个失败不影响其余。
- 返回值形状不变。`_audit` 的写入位置不变（仍在事务内）。
- 注释写明原因：事务内调 `notify` 会经 `capture_runtime_incident_best_effort` 用第二个连接写库，同线程自锁 `busy_timeout` 30 s
  （引用本文档 2.2）。

### 6.2 通知循环的 DB 段挪出事件循环线程（4.2）

- 文件 `src/telegram_kol_research/system_operator_bot.py`。
- `deliver_strategy_management_notifications`（约 2840-2925 行）是 `async def`，内部 `resolve_delivery_after_id`、
  `enqueue_strategy_management_notifications`、`claim_next_strategy_management_notification`、以及发送后写回状态的
  `with session_factory() as session:` 块都是**同步 DB**；Telegram 发送是 `await send_system_operator_bot_message`（异步，不动）。
  把每一段同步 DB 调用改为 `await asyncio.to_thread(...)`：
  - `after_id = await asyncio.to_thread(resolve_delivery_after_id, session_factory, field_name=..., supplied=...)`
  - `await asyncio.to_thread(enqueue_strategy_management_notifications, session_factory, group_labels=group_labels)`
  - `claim = await asyncio.to_thread(claim_next_strategy_management_notification, session_factory, ...)`
  - 发送成功/失败后写回状态的两个 `with session_factory()` 块各抽成模块级同步函数
    （如 `_mark_strategy_management_notification_delivered(session_factory, *, notification_id, claim_token, now)` /
    `_mark_..._failed(...)`），用 `await asyncio.to_thread(...)` 调用。函数体逻辑逐字搬移，不改 SQL。
- `run_strategy_management_notification_loop`（约 2930-2943 行）：`except Exception: pass` 改为
  `except Exception: logger.exception("strategy management notification loop tick failed")`。循环节奏不变。
- `tests/test_runtime_event_loop_blocking_census.py` 的 `KNOWN_BLOCKING_CALLS`：若普查对上述改动有新增/减少命中，按该文件头部的规则处理并在汇报里列出。

### 6.3 写事务内再开写连接的告警（4.3）

- 文件 `src/telegram_kol_research/db.py`。在 `create_session_factory` / `create_existing_session_factory` 返回的 engine 上
  （两个工厂共用一个安装函数，如 `_install_nested_write_guard(engine)`）挂 SQLAlchemy 事件：
  - `event.listens_for(engine, "begin")` 无法区分读写；改用 `before_cursor_execute`：对语句做前缀判断
    （`INSERT` / `UPDATE` / `DELETE` / `BEGIN IMMEDIATE` / `REPLACE`，大小写不敏感，去掉前导空白与 `WITH … ` CTE 时按首个非 CTE 关键字判断即可，
    做不到的情况按「非写」处理，宁漏勿误）。
  - 线程局部状态 `threading.local()`：`writing_connections: set[int]`（连接 id 集合）。
    `before_cursor_execute` 遇到写语句：若集合非空且**不含当前连接 id** → `logger.warning("nested write on a second connection while this thread holds a write transaction; statement=%s", 前 80 字符, stack_info=True)`；
    然后把当前连接 id 加入集合。
  - `event.listens_for(engine, "commit")` 与 `"rollback"`（`ConnectionEvents`）：从集合移除该连接 id。
    连接 `close`/`checkin` 也移除，防泄漏。
  - 每线程每分钟最多 1 条 WARNING（简单的 `last_warned_at` 线程局部节流），避免刷屏。
  - 只记不拦，不改任何 SQL 行为；`engine.dialect.name != "sqlite"` 时不安装。
- 部署后一周看 journal 里 `nested write on a second connection` 的出现次数与栈，确认除 6.1 之外没有漏网路径。

### 6.4 测试

- `tests/test_management_reliability_step5.py` / `step9.py` / `test_system_operator_bot.py` 里对 `expire_stale_management_confirmations`
  与 `notify` 的既有断言应保持不变（调用次数、参数）。新增：
  - `notify` 在 `commit` 之后被调用：用会记录顺序的 `session_factory` 包装（commit 时打点）+ 记录 `notify` 调用时刻，断言顺序。
  - `notify` 抛异常 → 审计事件已提交、函数正常返回、其余 `notify` 仍被调用。
  - 同线程自锁回归：用真实 SQLite 文件库（`busy_timeout` 设 1 s 以免测试慢），`notify` 内部用第二个 `session_factory()` 写一行；
    改动前会抛 `OperationalError`（或等 1 s），改动后 100 ms 内完成且两行都在库里。
- 通知循环：`deliver_strategy_management_notifications` 用假 `send` 跑一次，断言 DB 步骤发生在非事件循环线程
  （在假 `session_factory` 里记录 `threading.get_ident()` 与事件循环线程对比）；`run_strategy_management_notification_loop`
  tick 抛异常时有 `logger.exception` 记录且循环继续。
- 守卫：同线程两个连接嵌套写 → 恰好一条 WARNING 且 `stack_info`；同一连接连续写 → 无 WARNING；
  只读嵌套 → 无 WARNING；commit 后再写 → 无 WARNING；节流生效。
- 全量 `PYTHONPATH=. uv run pytest -q` 通过。

## 7. 实施记录

日期：2026-09-16。实施者：子代理（worktree `agent-aac3bd5f8cc17f435`，起点 `5eaaaedc`）。
第 1-6 节未改动；本节只记录按第 6 节实施的结果。

### 7.1 改动文件

| 文件 | 规格 | 内容 |
| --- | --- | --- |
| `src/telegram_kol_research/management_target_confirmation.py` | 6.1 | `expire_stale_management_confirmations` 事务内两处 `notify(...)` 改为收集进 `deferred_notifies: list[dict[str, Any]]`（键 `raw_message_id` / `kind` / `item_ids`，与原调用参数逐字一致），`with session_factory()` 块结束后逐个调用，每个单独 `try/except Exception` → `logger.exception("management confirmation notify failed raw_message_id=%s kind=%s", ...)`。新增 `import logging` 与模块级 `logger`。`_audit` 位置、`_fail_items`、返回值形状全部不变。函数 docstring 写明自锁机制并引用本文档 2.2。 |
| `src/telegram_kol_research/system_operator_bot.py` | 6.2 | `deliver_strategy_management_notifications` 的 `resolve_delivery_after_id`、`enqueue_strategy_management_notifications`、`claim_next_strategy_management_notification` 改为 `await asyncio.to_thread(...)`；发送成功/失败后写回状态的两个 `with session_factory()` 块抽成模块级同步函数 `_mark_strategy_management_notification_delivered` / `_mark_strategy_management_notification_failed`，同样经 `asyncio.to_thread` 调用。`await send_system_operator_bot_message` 不动。`run_strategy_management_notification_loop` 的 `except Exception: pass` 改为 `logger.exception("strategy management notification loop tick failed")`，循环节奏与 `CancelledError` 处理不变。 |
| `src/telegram_kol_research/db.py` | 6.3 | 新增 `statement_is_write`、`_strip_leading_sql_comments`、`_top_level_sql_tokens`、`_dbapi_connection_id`、`_install_nested_write_guard`；`create_session_factory`（在 `init_db` 之后）与 `create_existing_session_factory`（在 `create_engine` 之后）各调用一次安装函数。新增 `logging` / `threading` / `time` 导入与 `sqlalchemy.event`。 |
| `tests/test_db_lock_holder_and_loop_stall.py` | 6.4 | 新增测试文件，31 个用例，覆盖 6.1 / 6.2 / 6.3 三段。 |

### 7.2 守卫的实现细节（6.3）

- **写语句判断**（`statement_is_write`）：去掉前导 `--` / `/* */` 注释后，按括号深度 0 取词元。
  首词元属于 `{INSERT, UPDATE, DELETE, REPLACE}` → 写；`BEGIN` 且第二词元为 `IMMEDIATE` → 写；
  `WITH` 则走 CTE 列表（`名字 [列表] AS [NOT] [MATERIALIZED] (…)`，逗号可重复），判断 CTE 之后的第一个动词。
  其余一律按非写处理（含 DDL、`PRAGMA`、普通 `SELECT`、以及任何解析不出来的形状）。
  因为括号内整体跳过，`WITH update AS (SELECT 1) SELECT …`（CTE 名叫 `update`）不会误判为写。
- **连接身份**：`id(conn.connection.dbapi_connection)`（`_dbapi_connection_id` 对 `Connection` 与已经是
  DBAPI 连接的两种入参都能取到）。同一连接在池中被复用时 id 稳定，因此同一连接连续写不会误报——有专门用例。
- **移除时机**：`ConnectionEvents` 的 `commit` / `rollback`（engine 级，参数是 `conn`），加上 `PoolEvents`
  的 `checkin` / `close`（参数是 DBAPI 连接本身）。四个都往同一个线程局部集合里 `discard`。
- **节流**：线程局部 `last_warned_at` + `time.monotonic()`，每线程每 60 秒最多一条 WARNING。
  命中时机在「确认要告警之后、写日志之前」，所以被节流掉的那次仍然会把连接 id 加进集合，状态不会漂。
- **只记不拦**：监听器没有任何 `raise`，也不改语句。`engine.dialect.name != "sqlite"` 直接返回，不注册任何监听器。
- 安装点选在 `create_session_factory` 的 `init_db(engine)` **之后**，让引导期的建表/回填完全不经过守卫；
  `create_existing_session_factory` 没有引导期，紧跟 `create_engine`。

### 7.3 既有测试

**一处都没有改。** 具体核对：

- `tests/test_management_reliability_step9.py::test_an_unanswered_question_expires_after_a_reminder`
  对 `notify` 的调用次数与 `kind` 顺序的断言原样通过（顺序仍是提醒在前、超时在后，只是每次都发生在各自的 commit 之后）。
- `tests/test_management_reliability_step5.py` 与 `tests/test_system_operator_bot.py` 里
  `deliver_strategy_management_notifications` 的 5 处调用（含 `asyncio.CancelledError` 那条）全部原样通过：
  `CancelledError` 是 `BaseException`，不被 `except Exception` 拦，租约语义不变。
- `tests/test_runtime_event_loop_blocking_census.py::KNOWN_BLOCKING_CALLS` **无需调整**。普查只看 `async def`
  里 `while` 循环内的直接同步调用；`deliver_strategy_management_notifications` 的同步段在 `for` 循环里，
  本来就不在普查范围内，而 `run_strategy_management_notification_loop` 的 `while` 里唯一的调用是
  `await deliver_...`（既是 `await` 又是 `async def`，双重豁免）。新抽出的两个模块级同步函数同理不在 `while` 里。
  改动前后 `discover_blocking_calls()` 的结果集合完全相同。

### 7.4 新增用例（`tests/test_db_lock_holder_and_loop_stall.py`，31 个）

6.1（5 个）：
- `test_the_confirmation_notification_is_sent_after_the_commit` —— 用包装 `session_factory` 在 `commit` 打点，
  断言 `trace == ["commit", "notify:confirmation_reminder"]`。
- `test_a_failing_notification_does_not_cost_the_committed_audit` —— `notify` 抛异常，审计事件仍有 1 条、
  `confirmation_reminded_at` 已落库、返回值正常、有 `logger.exception`。
- `test_one_failing_notification_does_not_silence_the_others` —— 两条待确认消息，第一个 `notify` 抛异常，第二个照发。
- `test_a_notifier_that_writes_on_its_own_connection_no_longer_self_locks` —— 真实 SQLite 文件库的自锁回归：
  `notify` 内部用第二个 `session_factory()` 写一行，断言整体耗时 < 0.9 s 且两边的行都在库里。
- `test_the_timeout_branch_also_notifies_after_the_commit` —— 超时分支同样在 commit 之后通知（`notify` 内部
  用新连接读到的 item 状态已经是 `failed`）。

6.2（3 个）：
- `test_management_delivery_runs_its_database_work_off_the_event_loop` —— 包装 `session_factory` 记录
  `threading.get_ident()`，断言事件循环线程 id 不在其中，且投递结果与行状态不变。
- `test_a_failed_management_delivery_also_writes_back_off_the_event_loop` —— 失败分支的写回同样不在事件循环线程；
  行落到 `failed`、`delivery_error="RuntimeError"`、租约释放。
- `test_a_failing_notification_loop_tick_is_logged_and_the_loop_continues` —— tick 抛异常时有 `logger.exception`
  （带 `exc_info`），且循环继续跑第二个 tick，`cancel()` 仍抛 `CancelledError`。

6.3（23 个）：
- `test_the_write_statement_test_errs_towards_silence` —— 16 条参数化语句（含 `BEGIN IMMEDIATE`、三种 CTE、
  名叫 `update` 的 CTE、注释前缀、`PRAGMA`、DDL、`None`）。
- `test_repeated_writes_on_one_connection_never_warn` —— 同一连接连续 5 次写：0 条 WARNING（防误报）。
- `test_a_nested_read_never_warns` / `test_a_write_after_the_commit_never_warns` /
  `test_a_write_after_a_rollback_never_warns` —— 三种不该告警的情形。
- `test_a_nested_write_on_a_second_connection_warns_once_with_a_stack` —— 恰好 1 条 WARNING，带 `stack_info`。
- `test_the_nested_write_warning_is_throttled_per_thread` —— 一分钟内 3 次嵌套写只出 1 条。
- `test_the_guard_is_only_installed_on_sqlite` —— 非 sqlite 方言不注册任何 `before_cursor_execute` 监听器。

**一个实现上的坑，记在这里免得下一个人再踩**：断言日志不能用 `caplog`。
`app_logging.configure_application_logging` 会把 `telegram_kol_research` 这个 logger 的 `propagate` 置为 `False`，
只要全量跑里先有任何一个用例调过它，后面 `caplog` 装在 root 上的 handler 就再也收不到东西。
两条日志断言因此单文件跑通、全量跑挂（日志明明出现在 captured stderr 里）。
现在改成直接往具体 logger 上挂 handler（文件里的 `_captured` 上下文管理器），与谁先跑无关。

### 7.5 取舍与判断

1. **`notify` 循环放在 `with` 块之外而不是块内 `commit()` 之后。** 6.1 的字面要求是「`session.commit()` 之后、
   `return` 之前」，两种写法都满足。选块外是因为那时连接已经归还连接池，内层通知连到的可能就是同一个连接，
   自锁的可能性被彻底消掉而不是只缩短。`_audit` 仍在事务内，返回值形状不变。
2. **`if notify is not None` 的守卫保留在收集处**，而不是改成无条件收集、调用前再判断。语义等价，改动更小。
3. **`notified_at` 与 `updated_at` 现在用同一个时间戳。** 原代码是 `notified_at=datetime.now(UTC),
   updated_at=datetime.now(UTC)` 两次取时钟，会得到相差微秒的两个值。6.2 给出的函数签名本身就只带一个
   时间参数（`now=`），抽函数后统一为一个 `delivered_at`。这是唯一一处行为上的（微秒级）差异。
4. **失败分支里的 `capture_runtime_incident_best_effort` 没有挪进线程。** 它也是事件循环线程上的同步写库调用，
   但 6.2 的清单没有列它（只列了 `resolve_delivery_after_id`、`enqueue_…`、`claim_next_…`、两个写回块），
   按「不扩大」处理，保持原样。**这是已知的遗留项**：Telegram 发送失败时，这一次 `runtime_incidents` 写入
   仍然会阻塞事件循环。要不要补，留给指挥会话决定——补的话是一行 `await asyncio.to_thread(...)` 包装。
5. **守卫的写语句判断宁漏勿误。** CTE 解析只在能确定形状时才判写，任何一步对不上就返回「非写」。
   代价是某些 `WITH … DELETE` 变体可能漏报；收益是日志里不会出现假警报——守卫的全部价值就在于它的告警可信。
6. **守卫用 `id(dbapi_connection)` 作连接身份。** 理论上对象被回收后 id 可复用，但四个移除事件
   （commit / rollback / checkin / close）覆盖了连接离开线程的所有路径，集合不会长期持有失效 id。
7. **没有碰 `busy_timeout`、`BEGIN IMMEDIATE`、守护包装，以及 `6cce08b1` 的任何内容**；4.4 与第 5 节未实施。

### 7.6 全量测试

`PYTHONPATH=. uv run pytest -q`：

最后 3 行：

```

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
8921 passed, 4 skipped, 107 warnings in 1359.82s (0:22:39)
```

## 8. 部署记录（指挥会话补记）

- 子代理提交 `9467fc4e`；指挥会话审阅后追加 `5f26e721`：把投递失败分支里的 `capture_runtime_incident_best_effort`
  也包进 `asyncio.to_thread`（子代理按「不扩大」原则留在了事件循环线程，与 6.2 的意图不符）。
- 指挥会话独立复跑全量 8921 passed / 0 failed / 4 skipped（1005 s）。
- 2026-09-16 `tg-deploy 5f26e721b12205b9a11c764a7ee64fd30896729b`，回滚 SHA `6cce08b1`。
  worker / web / ingest 均 active，启动无异常；共享分支 = 部署 SHA；启动后 3 分钟内无 `nested write on a second connection` 告警。
- 验证方式（一周）：journal 里 `nested write on a second connection` 的出现次数与栈（期望 0，若有即漏网路径）；
  `runtime_loop_health` ≥15 s 卡顿应不再出现在确认提醒/超时的时刻；`Runtime incident capture failed open: type=management_target_needs_confirmation`
  应不再出现。
