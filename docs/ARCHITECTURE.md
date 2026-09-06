# 当前架构（现状快照）

本文件只描述**当前生产运行的样子**，不记录历史演进。改动前先读这里的模式表。
历史方案在 `docs/archive/plans/`。

## 1. 进程拓扑

生产是三个独立进程，由三个 systemd unit 拉起，靠环境变量 `TELEGRAM_KOL_RUNTIME_ROLE`
区分角色（`src/telegram_kol_research/cli.py` 的 `--runtime-role` 也读这个 envvar）：

| unit | 角色 | 端口 |
|---|---|---|
| `deploy/systemd/telegram-kol-web.service` | `web` | 8000 |
| `deploy/systemd/telegram-kol-ingest.service` | `ingest` | 8001 |
| `deploy/systemd/telegram-kol-worker.service` | `worker` | 8002 |

合法角色集合 `RUNTIME_ROLES = {"all", "ingest", "worker", "web"}`
（`src/telegram_kol_research/web_app.py`）。只有 `all` 和 `ingest` 持有 Telegram 会话
（`runtime_role_owns_telegram_session`）。

## 2. 每个角色启动的后台单例任务

逐字来自 `src/telegram_kol_research/web_app.py` 的 `RUNTIME_ROLE_SINGLETON_TASKS`：

```python
RUNTIME_ROLE_SINGLETON_TASKS = {
    "ingest": frozenset({"live_listener", "reconcile"}),
    "worker": frozenset(
        {
            "authoritative_gap_recovery_loop",
            "break_even_convergence_worker",
            "contract_spec_refresh",
            "deepcoin_reconcile",
            "lifecycle_monitor",
            "message_operation_supervisor",
            "message_processing_worker",
            "position_snapshot_startup",
            "runtime_incident_notification",
            "semantic_review",
            "source_message_deletion_worker",
            "strategy_management_notification",
            "strategy_management_worker",
            "system_operator_bot_command",
            "telegram_bot_command",
            "worker_command_worker",
        }
    ),
    "web": frozenset(),
}
```

`web` 不启动任何后台单例任务，只服务 HTTP。
`all` 不是表里的键：`runtime_role_singleton_tasks("all")` 返回上表所有角色任务的并集，
这是**本地开发用的单进程模式**，生产不用。
`loop_lag_monitor` 是进程监控，不属于单例任务表，任何角色都会启动
（`runtime_role_starts_process_monitor`）。

## 3. 生产运行模式表

| 设置 | 生产值 | 代码默认值（`src/telegram_kol_research/trading_settings.py`） |
|---|---|---|
| `message_pipeline_mode` | `queue` | `queue`（`Literal["queue"]`） |
| `worker_command_mode` | `queue` | `queue`（`Literal["queue"]`） |

**代码默认值与生产一致**：两个设置的默认值都等于生产值，数据库里没有对应行时读到的就是生产模式。
两者现在都只有 `queue` 一种行为；生产数据库里遗留的 `inline` / `shadow` 值仍然读得回来——解析器记一条
warning 后按 `queue` 处理，从不抛错。

`message_lock_mode` 这个设置已经不存在了（`per_chat` 从未在生产启用）。生产数据库的设置行里可能还留着
`message_lock_mode` / `message_lock_expected_mode` 两个键，读的时候当作未知键静默忽略，下一次写设置时自然
消失；`/api/trading-settings` 收到这两个键也不会报错。锁的现状见第 4.5 节。

## 4. queue 模式下一条消息的路径

以 `telegram_live_listener.py`、`message_processing_worker.py`、`web_app.py` 为准：

1. **ingest 落库 + 入队，不做处理。**
   Telethon 回调进入 `telegram_live_listener.persist_live_message_event`。它把原始行写进
   `raw_messages`，然后通过 `enqueue_hook` → `_try_enqueue_processing_jobs` 幂等地在
   `message_processing_jobs` 里建一条 job（`last_reason="queue_enqueued"`）。
   ingest 回调里没有任何识别或执行调用。
   ingest 的 `reconcile`（拉取补齐）路径同样只补入队，
   reason 为 `history_reconcile_enqueued`。

2. **worker 消费并做全部业务决定。**
   `message_processing_worker.run_message_processing_loop` 无条件消费，直到被取消。
   每轮按 `message_processing_max_parallel_chats` 上限
   `claim_message_processing_jobs` 认领作业，每条作业交给
   `run_message_processing_worker_tick` → `process_message_job`。
   `process_message_job` 就是**原来 ingest 回调里那条 post-persist 链**：上下文解析调度
   （`context_resolution_scheduler` / `context_resolution_worker`）、识别
   （`recognition_enabled`）、权威处理（`authoritative_processor`）、策略提醒
   （`strategy_alert_processor`）、系统 Bot 冲突通知。它只改变**在哪里调用**，
   不改变识别、策略解析、执行、提醒、通知的语义。
   过期作业由 `_classify_claim_expiry` 分类后走权威缺口恢复。

3. **web 发起的权威动作走 `worker_command_jobs`。**
   `web` 角色没有执行权限。`web_app.py` 用 `enqueue_worker_command` 把请求写进
   `worker_command_jobs`，`worker` 的 `worker_command_worker` 认领后由
   `worker_command_executor.execute_worker_command_adapter` 分发到恰好四种命令：

   | `command_type` | 适配器 |
   |---|---|
   | `sync_deepcoin_execution` | `_execute_sync` |
   | `close_bound_position` | `_execute_close` |
   | `recovery_live_submit` | `_execute_recovery` |
   | `process_next_trade_signal` | `_execute_process_next` |

   其他 `command_type` 一律 fail-closed（`unsupported_worker_command_type`）。

## 4.5 两个补偿循环，与锁在哪里

**两个补偿循环，分工不重叠。**

| 循环 | 角色 | 周期 | 对照谁 | 补什么 |
|---|---|---|---|---|
| `run_periodic_reconcile` → `run_reconcile_once` | `ingest` | 300s | **Telegram 历史** | 直播回调根本没收到的消息：拉回一小段最近历史，落库 + 入队（`history_reconcile_enqueued`） |
| `run_authoritative_gap_recovery_loop` → `recover_missing_authoritative_decisions` | `worker` | 20s | **数据库** | 已经落库、但至今没有权威决策行的消息：只入队（`recovery_enqueued`），不做任何 Telegram 调用 |

一句话：reconcile 补"没收到"，gap recovery 补"没处理"。只有 reconcile 碰 Telegram，所以 Telegram 会话
卡住不会连带拖住 gap recovery。过期分类（`authoritative_gap_recovery_max_age_minutes` 决定的
stall / stale）在 worker 的 `_classify_claim_expiry`，两个循环都不做。

**进程间不存在共享的进程内锁；跨进程互斥靠数据库状态。**

三个角色是三个操作系统进程，任何 `asyncio.Lock` 都只在自己进程内有效。所以：

- `ingest` 进程有且只有一把锁——`KeyedAsyncLockRegistry`（`keyed_async_locks.py`），
  按 `chat_id` 一把 `asyncio.Lock`，建在 `web_app.py` 的 `app.state.message_lock_registry`。
  取它的只有两处：直播回调（`run_live_listener` 的新消息与删除消息处理器）、以及 reconcile 每个
  会话的落库+入队那一小段。同群串行、跨群并行；Telegram 拉取本身**不**持锁，所以一次 reconcile
  再慢也不会冻住直播。它防的是同一个 chat 的两条写入路径撞在一起——`raw_messages` 上没有
  `(chat_id, message_id)` 唯一约束，靠这把锁保证不会插出重复行。
  `/api/runtime/loop-health` 在 ingest 角色下输出的就是这个 registry 的 `snapshot()`。
- `worker` 进程的互斥边界是 `position_authority_lock.py`（按仓位/符号），那是真正会写交易所的地方。
- `web` 进程**没有**任何消息锁。它既不持有 Telegram 会话也不执行交易，需要跨进程排他的操作一律走
  数据库：设置写入用 `BEGIN IMMEDIATE` + 期望值比较交换（`transition_message_concurrency_settings`、
  `save_trading_settings`），权威动作走 `worker_command_jobs` 队列。

不要为了"保险"再引入一把进程内的全局锁：它在三进程拓扑下保护不了任何跨进程的东西，只会把同一个
进程里本可以并行的活动串起来。

## 5. 模块分类（粗分，待步骤 5 核实）

`src/telegram_kol_research/` 共 240 个 Python 模块。

**在线主路径（每条消息/每笔交易都可能走到）：**

- 摄入与生命周期：`telegram_live_listener.py`、`web_app.py`、`cli.py`
- 队列与执行边界：`message_processing_worker.py`、`worker_command_executor.py`、
  `worker_command_jobs.py`、`recovery_execution_queue.py`
- 识别与解析：`authoritative_recognition.py`、`context_resolution.py`、`semantic_review*.py`
- 交易与保护：`deepcoin_*.py`、`trading_settings.py`、`position_*.py`、
  `strategy_management_*.py`、`entry_*.py`、`protection_*.py`
- 通知与运维：`system_operator_bot.py`、`telegram_bot*.py`、
  `runtime_incident_*.py`、`runtime_deployment_identity.py`

**一次性修复 / 历史迁移候选**（按文件名含
`repair`/`recovery`/`reconcile`/`terminalization`/`legacy`/`migration`/`backfill`/`alignment`
粗筛，**尚未核实是否仍在线**，步骤 5 逐个判定）：

```
backfill.py                              legacy_backup_reconciliation.py
backup_stop_repair.py                    legacy_conditional_cancel.py
batch150_management_terminalization.py   management_history_recovery.py
context_analysis_backfill.py             native_tpsl_migration.py
current_protection_backfill.py           position_attribution_repair.py
entry_admission_reconciler.py            position_management_liveness_recovery.py
entry_assembly_fingerprint_repair.py     reconcile.py
entry_protection_ledger_repair.py        recovery_decisions.py
evidence_backfill.py                     recovery_execution_queue.py
frozen_exchange_empty_state_alignment.py recovery_live_submit.py
historical_management_terminalization.py recovery_live_submit_gate.py
historical_state_repair.py               recovery_order_confirmation.py
repair_confirmation.py                   recovery_order_confirmations.py
take_profit_protection_leg_repair.py     recovery_runner.py
tpsl_ledger_backfill.py                  recovery_scan.py
```

注意：这批里至少 `reconcile.py`（ingest 单例任务）、`recovery_execution_queue.py`、
`recovery_live_submit.py`（四条权威命令之一）**确实在线**，名字命中不等于可删。

## 6. AI 协作提示

- 改任何东西之前，先看上面第 3 节的模式表：**生产和代码默认值都跑 queue**。
  运行时真值仍以数据库里的设置行为准，不要只看默认值就下结论。
- 代码里已经没有 `inline` / `shadow` 分支了（清理方案步骤 3 删除）。消息与命令路径各只有一条，
  **不要重新引入模式开关**来做灰度或回滚。`message_processing_jobs.shadow` 列还在表上，
  新行恒为 `0`，worker 认领时用 `shadow = 0` 过滤掉历史行；删列是以后的 L3 工作。
- `web` 角色没有执行权限。任何需要写交易所或改仓位的动作，必须经 `worker_command_jobs`
  走那四条命令之一，不要在 web 进程里直接调交易所客户端。
- 锁只在自己进程里有效（第 4.5 节）。要跨进程排他就用数据库状态，不要新加进程内全局锁，
  也不要把 `KeyedAsyncLockRegistry` 当成跨进程的锁用。
- 迁移只改变"在哪里跑、怎么组织"，从不改变"决定什么"。任何看起来需要改交易语义的改动
  都是读错了需求，停下来问。
