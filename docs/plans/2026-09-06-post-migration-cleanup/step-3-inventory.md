# Step 3 盘点 — inline / shadow 消息与命令路径

起点：`200e2bf7`（步骤 2 合并后）。分支：`cleanup/step-3-delete-inline-shadow`。

本文件是动手前的完整命中清单。每条标注 **删** / **保留并简化** / **不动**。
判定规则（贯穿全文，遇到分歧以它为准）：

1. 只删"在 `message_pipeline_mode == "queue"` / `worker_command_mode == "queue"` 下永远走不到的分支体"。
2. **不改任何公共函数签名**（`run_reconcile_once`、`recover_missing_authoritative_decisions`、
   `run_authoritative_gap_recovery_loop`、`run_live_listener`、`persist_live_message_event` 的形参一律保留）。
   这些函数的形参精简属于步骤 4 的"补偿循环去重"范围；本步删分支体后变成未使用的形参统一记在文末"留给以后"。
3. 只删除**只被已删分支引用的模块内私有辅助函数与局部变量**；被 worker / web_app / cli 复用的辅助函数一律保留。
4. 不改表结构，不改 `message_lock_mode` / `MessageLockProvider` / `KeyedAsyncLockRegistry`，
   不改 `run_periodic_reconcile` 与 gap recovery 循环的取舍。

---

## 1. `src/telegram_kol_research/telegram_live_listener.py`

`grep -n "pipeline_mode\|worker_command_mode\|shadow"` 全部命中（起点行号）：

| 行 | 内容 | 处置 |
|---|---|---|
| 124 | `def _enqueue_shadow_processing_jobs(` | **保留并简化**：改名 `_enqueue_processing_jobs`，删 `pipeline_mode_override` 形参与 mode 分派 |
| 130 | `pipeline_mode_override: str \| None = None` | **删** |
| 132 | docstring "Idempotently create shadow or authoritative queue jobs by mode." | **保留并简化**：改写为只描述 queue 入队 |
| 134-138 | 读 `message_pipeline_mode`、`if pipeline_mode not in {"shadow","queue"}: return []` | **删**（queue 恒真） |
| 140 | `is_shadow = pipeline_mode == "shadow"` | **删** |
| 171 | insert values `"shadow": is_shadow` | **保留并简化**：写死 `False`（停止写入非 queue 值，不改表结构） |
| 176-199 | `if is_shadow:` 整个 shadow 版 `on_conflict_do_update` | **删** |
| 201-204 | "Queue authority may adopt a terminal Phase-4 shadow row…" 迁移期注释 | **保留并简化**：改写为描述现状（queue 只接管终态或滞留 >5min 的历史 shadow 行） |
| 205-228 | queue 版 `on_conflict_do_update`（含 `MessageProcessingJob.shadow.is_(True)` 的 where） | **不动**（这是 queue 路径本身，历史 shadow 行的接管条件是语义，不能改） |
| 235-251 | `if is_shadow:` 的 admitted_ids 回查 | **删** |
| 253-275 | `def _mark_shadow_processing_jobs_terminal` | **删**（只 UPDATE `shadow.is_(True)` 的行；queue 行 `shadow=False`，在 queue 下恒为空操作） |
| 277-290 | `async def _try_enqueue_shadow_processing_jobs` | **保留并简化**：改名 `_try_enqueue_processing_jobs`，日志文案去掉 shadow |
| 292-314 | `async def _try_mark_shadow_processing_jobs_terminal` | **删**（只服务 shadow） |
| 340-342 | `_persist_live_message_event_inline` 的 `shadow_enqueue_hook` 形参 | **保留并简化**：改名 `enqueue_hook` |
| 343 | `run_post_persist_processing: bool = True` 形参 | **删**（私有函数的内部开关，非公共签名） |
| 444-445 | `if shadow_enqueue_hook is not None: await shadow_enqueue_hook(...)` | **保留并简化**：改名 |
| 455-471 | `if raw_message_id is not None and run_post_persist_processing:` → `process_message_job(...)` 整块 | **删**（inline 执行链，queue 下由 worker 承担） |
| 472-473 | `stats["recognition_status"] = "authoritative_processor_required"` | **删**（在上面那块内） |
| 481-527 | `persist_live_message_event` 包装：读 mode、`inline_enqueued` 文案、`run_post_persist_processing = mode != "queue"`、`if pipeline_mode == "shadow"` 的成功/失败终态标记 | **保留并简化**：只剩"落库 + 入队"，`last_reason` 恒为 `queue_enqueued`，去掉 try/except 里的 shadow 终态分支（`raise` 语义不变） |
| 1198-1214 | `recover_missing_authoritative_decisions` 里读 mode、`if pipeline_mode == "queue": 入队后 return` | **保留并简化**：去掉 `if`，无条件入队后 return |
| 1216-1332 | `_process_recovery_candidate` 闭包 + `for raw_message in missing_decision_messages` 循环 + `for raw_message in expired_messages` 过期循环 | **删**（queue 下 `return` 之后，恒不可达；过期分类改由 worker `_classify_claim_expiry` 承担） |
| 1334-1367 | stall 过期聚合通知块（`stall_expiry_notification_sender` / `_STALL_EXPIRY_NOTIFICATION_RATE_LIMITER`） | **删**（同上，恒不可达） |
| 1374-1376 | `def _load_reconcile_pipeline_mode`（内含 `repair_history_checkpoints` 副作用） | **保留并简化**：改名 `_repair_reconcile_history_checkpoints`，只保留 `repair_history_checkpoints` 副作用（tests/test_reconcile.py:41 按名字 monkeypatch `repair_history_checkpoints`，副作用必须保留） |
| 1558-1560 | `pipeline_mode = await _run_reconcile_database_slice(_load_reconcile_pipeline_mode, ...)` | **保留并简化**：改为调用重命名后的函数，不再接返回值 |
| 1606-1613 | `recognition_status = "queued" if pipeline_mode == "queue" else ...` | **保留并简化**：写死 `"queued"` |
| 1662-1669 | `history_shadow_raw_message_ids = await _try_enqueue_shadow_processing_jobs(..., pipeline_mode_override=pipeline_mode)` | **保留并简化**：改名调用，去掉 override 与返回值变量 |
| 1670-1737 | `if authoritative_processor is not None and pipeline_mode != "queue":` 的历史 inline 识别块（含 `_process_dialog_raw_message` 闭包、`_load_authoritative_reconcile_projection`、`_count_signal_candidates`、失败终态标记） | **删** |
| 1738-1742 | `elif authoritative_processor is None: logger.error("history recognition authority unavailable …")` | **保留并简化**：变成独立的 `if authoritative_processor is None:`，queue 下行为完全不变 |
| 1743-1748 | `if authoritative_processor is not None and pipeline_mode != "queue": persist_trade_ideas_from_candidates` | **删** |
| 1749-1767 | `if pipeline_mode != "queue" and strategy_alert_config is not None:` 的策略提醒块 | **删** |
| 1768-1782 | try/except 里的 `_try_mark_shadow_processing_jobs_terminal` 成功/失败调用 | **删**（辅助函数已删；try/except 随之消失，无异常语义改变——原 except 只做标记后 `raise`） |

**不动**：`handle_new_message` / `handle_deleted_message` 内没有任何 pipeline mode 分支，只有 `resolve_lock_context`
的锁分支（`message_lock_mode`，步骤 4）。`run_authoritative_gap_recovery_loop` 同样只有锁分支，本步零改动。

**`authoritative_processor` 形参：保留，不从 `run_live_listener` 签名移除。**
理由：删掉 `process_message_job` 之后它在 ingest 路径里仍被**引用**——
`_persist_live_message_event_inline` 第 401 行 `if authoritative_processor is not None and telegram_client is not None
and reply_to_message_id is not None:` 用它作为"识别权威已配置"的门，门内是 reply 目标补齐
（`fetch_missing_reply_target` / `reply_evidence_processor` / `context_resolution_scheduler`），
这段在 queue 模式下照常执行。移除形参会让这道门恒真，属于改语义，按"宁可少删"保留。
生产里 `app.state.authoritative_processor` 恒非 None（web_app.py:5827），所以保留它零成本。

## 2. `src/telegram_kol_research/message_processing_worker.py`

| 行 | 内容 | 处置 |
|---|---|---|
| 355 | 认领 SQL 的 `AND shadow = 0` | **不动**（防止消费历史 shadow 行，是安全语义不是模式分支） |
| 398 | `MessageProcessingJob.shadow.is_(False)` 认领条件 | **不动**（同上） |
| 720 | `if settings.message_pipeline_mode != "queue": <收干在飞任务后 return>` | **删**（worker 无条件消费） |
| 711 docstring | "Consume queue jobs only while the dynamic pipeline mode is `queue`." | **保留并简化**：改写为无条件消费 |

## 3. `src/telegram_kol_research/web_app.py`

| 行 | 内容 | 处置 |
|---|---|---|
| 277-278 | import `require_worker_command_mode_transition_safe`, `supervise_worker_command_mode` | **保留并简化**：前者随守卫一起删；后者保留（见下） |
| 817 | `mode in {"disabled","shadow","live"}` | **不动**（另一功能的 mode，与 pipeline/worker_command 无关） |
| 3837 | `mode not in {"static","shadow","live"}` | **不动**（同上） |
| 4602 / 4930 / 6443 | `config.shadow_only`（message operation supervisor） | **不动**（另一功能） |
| 4760-4765 | `mode = settings.worker_command_mode` + `if mode != "queue": 503 worker_command_mode_invalid` | **删**（Literal 收窄后恒不可达；连同上一行的 `load_trading_settings` 一起删，web 每条命令少一次 DB 读，无语义变化）。**web_app 里本就没有 worker command 的 inline 直跑分支或 shadow 分支**——它们在更早的阶段已经删干净，本步只清理这道恒真守卫 |
| 5790 | `worker_command_worker_runner or supervise_worker_command_mode` | **不动**（见下） |
| 5909 | `mode = load_trading_settings(...).message_pipeline_mode` + 5921 `if mode != "queue" or task is not None: return` | **保留并简化**：删 mode 读取与 `mode != "queue"` 分支，只留 `if task is not None: return` |
| 6193-6196 | `pipeline_mode = ...`、`observed_shadow = pipeline_mode != "queue"` | **保留并简化**：`observed_shadow` 恒 False，改为直接用常量；`pipeline_mode` 仍需读出来放进响应体 |
| 6219 | `MessageProcessingJob.shadow.is_(observed_shadow)` | **保留并简化**：`.is_(False)` |
| 6250-6254 | 响应体 `pipeline_mode` / `observed_job_kind` / `shadow_jobs` / `queue_jobs` | **不动**（只读诊断端点的响应契约，键名与形状全部保留；`observed_job_kind` 恒为 `"queue"`） |
| 8374-8378, 8392-8396 | `/api/trading-settings` 里两处 `require_worker_command_mode_transition_safe(...)` | **删**（模式切换守卫；只剩单一模式后 `current_mode == candidate_mode` 恒真、函数恒早返回） |

## 4. `src/telegram_kol_research/worker_command_*.py`

| 文件:行 | 内容 | 处置 |
|---|---|---|
| `worker_command_executor.py:158` | `run_worker_command_tick` 的 `if settings.worker_command_mode != "queue": return WorkerCommandWorkerResult()` | **删** |
| `worker_command_executor.py:243` | `run_worker_command_loop` 的同款检查 | **删** |
| `worker_command_executor.py:253-278` | `def require_worker_command_mode_transition_safe` | **删**（唯一调用点是 web_app 的两处守卫，随之删除；它唯一抛出的 `WorkerCommandModeTransitionError`（同文件 115 行）一并删除） |
| `worker_command_executor.py:280-298` | `async def supervise_worker_command_mode` | **保留并简化**：退化为直接委派 `queue_runner`（不再轮询 mode），保留函数名与签名——它是 `app.state.worker_command_worker_runner` 的默认值，且 `tests/test_worker_command_mode_exclusivity.py` 依赖它 |
| `worker_command_jobs.py` / `worker_command_reconciliation.py` | grep 零命中 | **不动** |

## 5. `src/telegram_kol_research/trading_settings.py`

| 行 | 内容 | 处置 |
|---|---|---|
| 96-97 | `message_pipeline_mode: Literal["inline","shadow","queue"]` / `worker_command_mode: 同` | **保留并简化**：收窄为 `Literal["queue"]`，默认值仍是 `"queue"` |
| 811-817 | `_message_pipeline_mode` 解析器 | **保留并简化**：`inline`/`shadow` → 记 `logger.warning` 后返回 `"queue"`；其他非法值仍 `raise ValueError`（生产 DB 行必须仍可读，绝不抛错） |
| 820-826 | `_worker_command_mode` 解析器 | **保留并简化**：同上 |
| 94 | `message_lock_mode` | **不动**（步骤 4） |

模块目前没有 logger，需新增 `import logging` + `logger = logging.getLogger(__name__)`。

## 6. 测试文件清单

`grep -rln "inline\|shadow" tests/ | xargs grep -ln "pipeline_mode\|worker_command_mode"`：

| 文件 | 处置 |
|---|---|
| `tests/test_authoritative_gap_recovery_loop.py` | 9 处 `inline` 标记；inline 专属用例**删**，queue 专属（只入队不执行）用例**保留并改写** |
| `tests/test_live_listener_chat_isolation.py` | 1 处；改回默认 queue，断言语义不变 |
| `tests/test_message_pipeline_mode_exclusivity.py` | inline/shadow 互斥与 shadow 回滚用例**删**；queue 独占用例**保留** |
| `tests/test_message_processing_shadow_enqueue.py` | shadow 专属用例**删**；queue 入队幂等 / 恢复入队用例**保留并改写**；文件改名为 `test_message_processing_enqueue.py` |
| `tests/test_message_processing_worker.py` | 「mode 变为 inline 则 worker 停止消费」用例**删**；崩溃后重领、消费一次且只一次**保留**；`test_consumer_never_claims_dormant_shadow_rows` **保留**（守护 `shadow = 0` 认领过滤） |
| `tests/test_per_chat_phase7_observer.py` | 全部 `queue`/`shadow=0` 固定值，**不动** |
| `tests/test_reconcile.py` | 1 处 inline 标记，**改写**为 queue |
| `tests/test_reconcile_live_history.py` | 10 处 inline 标记；inline 识别/策略提醒用例**删**，落库与入队用例**保留并改写** |
| `tests/test_telegram_live_listener.py` | 10 处 inline 标记；inline post-persist 链用例**删**，落库/入队/reply 补齐用例**保留** |
| `tests/test_trading_settings.py` | 「显式 inline 覆盖默认」用例**删**；**新增**历史值 `inline`/`shadow` → `queue` 且不抛错的用例 |
| `tests/test_web_app.py` | 2 处 inline 标记 + `worker_command_mode_invalid` 用例**删** |
| `tests/test_worker_command_executor.py` | `@parametrize("mode", ["inline","shadow"])` 不消费用例**删**；queue 用例**保留** |
| `tests/test_worker_command_mode_exclusivity.py` | `require_worker_command_mode_transition_safe` 用例**删**；`supervise_worker_command_mode` 用例**保留并改写** |
| `tests/test_runtime_event_loop_blocking_census.py` | **不动**，必须仍通过 |

步骤 2 留下的 `# inline path: scheduled for removal in cleanup step 3` 注释共 **36 处**（状态文件写的是 32，
实际含步骤 2 自己新增的用例）：gap_recovery_loop 9、reconcile_live_history 10、telegram_live_listener 10、
reconcile 1、live_listener_chat_isolation 1、message_pipeline_mode_exclusivity 1、
message_processing_shadow_enqueue 1、trading_settings 1、web_app 2。逐一处理，处理完仓库内该注释归零。

---

## 留给以后（本步不做）

1. **`message_processing_jobs.shadow` 列**（`models.py:123`）：`nullable=False, default=True, server_default '1'`。
   本步只保证不再写入非 queue 值（插入恒 `False`），列、列默认值、以及 worker 认领时的 `shadow = 0` 过滤全部保留。
   删列或把默认值改成 `False` 是改表结构（L3），需要独立的迁移与回滚计划。
2. **删分支后变成未使用的形参**（按规则 2 一律保留，交给步骤 4 的补偿循环去重一起收）：
   - `recover_missing_authoritative_decisions`：`system_operator_bot_config`、`notification_bot_config`、
     `system_operator_conflict_sender`、`stall_expiry_notification_sender`、
     `authoritative_failure_retry_delay_seconds`、`chat_operation_lock`、`loop_lag_snapshot_provider`、
     `expiry_notification_rate_limiter`、`now_provider`（仍用于 `_load_gap_recovery_candidates`，实际仍在用）
   - `run_reconcile_once`：`strategy_alert_config`、`strategy_alert_enabled_for_title`、
     `strategy_alert_processor`、`system_operator_conflict_sender`、`stall_expiry_notification_sender`、
     `authoritative_failure_retry_delay_seconds`、`chat_operation_lock`、`loop_lag_snapshot_provider`
   - `run_authoritative_gap_recovery_loop`：向下透传上述形参的那一组
   - `run_live_listener` / `_persist_live_message_event_inline`：`chat_title`、`strategy_alert_config`、
     `strategy_alert_enabled_for_title`、`strategy_alert_processor`、`context_resolution_worker`、
     `authoritative_failure_retry_delay_seconds`、`system_operator_bot_config`、`notification_bot_config`、
     `system_operator_conflict_sender`。另有 `lifecycle_monitor` 与 `auto_trade_executor` —— 这两个**在本步之前
     就已经是死形参**（`_persist_live_message_event_inline` 函数体内从未引用过），与本步无关，一并记在这里。
3. **随分支删除而失去生产调用者的模块级对象**（本步保留，避免误伤）：
   - `telegram_live_listener.StallExpiryNotificationRateLimiter`、
     `_STALL_EXPIRY_NOTIFICATION_RATE_LIMITER`、
     `DEFAULT_STALL_EXPIRY_NOTIFICATION_MIN_INTERVAL_SECONDS`
   - `system_operator_bot.send_stall_induced_expiry_notification`
   删掉 stall 过期聚合通知后，"因系统停顿而过期"这件事在 queue 模式下已经改由 worker 的 `_classify_claim_expiry`
   记录，但**没有任何地方再发这条聚合通知**。这在本步之前就已经是生产现状（queue 模式下那段代码本就不可达），
   本步只是让它在代码上也变得显然。是否需要在 worker 侧补回这条通知，建议单独评估。
4. **步骤 2 遗留的 6 个 F841**（`entry_protection_ledger_repair.py:1263`、`message_recognition.py:1410` 与 `:3030`、
   `raw_ingest.py:118`、`strategy_management_batches.py:555`、`trigger_backup_stop.py:60`）：位于识别/策略/交易所写入
   代码，本步不碰。
