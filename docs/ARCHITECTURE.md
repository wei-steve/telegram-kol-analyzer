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
| `message_pipeline_mode` | `queue` | `inline`（`Literal["inline", "shadow", "queue"]`） |
| `worker_command_mode` | `queue` | `inline`（`Literal["inline", "shadow", "queue"]`） |
| `message_lock_mode` | `global` | `global`（`Literal["global", "per_chat"]`） |

**代码默认值目前与生产不一致**：`message_pipeline_mode` 和 `worker_command_mode`
的默认值仍是 `inline`，生产靠数据库里的动态设置跑在 `queue`。清理方案的步骤 2 会翻转这两个默认值。
`message_lock_mode=per_chat` 从未在生产启用。

## 4. queue 模式下一条消息的路径

以 `telegram_live_listener.py`、`message_processing_worker.py`、`web_app.py` 为准：

1. **ingest 落库 + 入队，不做处理。**
   Telethon 回调进入 `telegram_live_listener.persist_live_message_event`。它先读
   `message_pipeline_mode`，并设置 `run_post_persist_processing = (pipeline_mode != "queue")`。
   在 `queue` 模式下这个标志为 `False`，所以 ingest **不会**在回调里调用
   `process_message_job`；它只把原始行写进 `raw_messages`，然后通过
   `shadow_enqueue_hook` → `_try_enqueue_shadow_processing_jobs` 幂等地在
   `message_processing_jobs` 里建一条 job（`last_reason="queue_enqueued"`）。
   ingest 的 `reconcile`（拉取补齐）路径同样在 `queue` 模式下补入队，
   reason 为 `recovery_enqueued`。

2. **worker 消费并做全部业务决定。**
   `message_processing_worker.run_message_processing_loop` 只在
   `message_pipeline_mode == "queue"` 时消费；模式一旦不再是 `queue`，它把在飞任务收干净后返回。
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
   `worker_command_executor` 同样以 `worker_command_mode != "queue"` 作为不消费的条件。

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

- 改任何东西之前，先看上面第 3 节的模式表：**生产跑 queue，代码默认值还是 inline**。
  不要根据默认值推断运行时行为。
- 代码里的 `inline` 和 `shadow` 分支是迁移期的历史路径，正在被删除
  （清理方案步骤 3）。**不要在这些分支里加功能**，也不要为了让它们"对称"而扩展它们。
- `web` 角色没有执行权限。任何需要写交易所或改仓位的动作，必须经 `worker_command_jobs`
  走那四条命令之一，不要在 web 进程里直接调交易所客户端。
- 迁移只改变"在哪里跑、怎么组织"，从不改变"决定什么"。任何看起来需要改交易语义的改动
  都是读错了需求，停下来问。
