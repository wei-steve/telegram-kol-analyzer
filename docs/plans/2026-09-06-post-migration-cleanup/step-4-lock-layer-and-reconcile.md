# Step 4 — 锁层收敛与补偿循环去重（L2，本地部分）

状态文件：`docs/post-migration-cleanup-status.md`。先领取。分支名：`cleanup/step-4-lock-and-reconcile`。
前置：步骤 3 已 completed 并合并；**用户已在指挥会话确认下方"待确认决策"的选项**，状态文件证据区有记录。若没有记录，停止并告知。

## 待确认决策（由指挥会话向用户提出）

现状：`message_lock_mode` 有 `global` / `per_chat` 两档，生产一直是 `global`，`per_chat` 从未启用。
步骤 3 之后 ingest 回调只做落库 + 入队，这把锁保护的只剩"同一 chat 内消息按到达顺序落库"这一件事。
web 进程里 `/api/refresh` 和设置切换用的 `lock_all()` 在三进程拓扑下根本锁不到 ingest 进程，属于失效的保护。

- **方案 A（指挥会话推荐）**：删除 `message_lock_mode` 设置与 `MessageLockProvider`；ingest 内固定用 `KeyedAsyncLockRegistry` 按 chat_id 加锁（落库 + 入队很快，同 chat 串行、跨 chat 并行）；删除 `lock_all()` 及 web 端调用，web 端需要的跨进程互斥改用已有的 DB 级机制（如 `active_write_count` / 队列状态检查），若没有等价机制则记录为遗留而不是假装有锁。
- **方案 B**：保留 `message_lock_mode`，把生产切到 `per_chat` 并按原 Phase 2 Task 6 做真实多群观察。这是启用而非清理，工作量和风险都更大。
- **方案 C**：全部保留不动，只删 `run_periodic_reconcile` 的重复部分。

## 任务 1（方案 A 时）：锁层

- `trading_settings.py` 删除 `message_lock_mode` 字段、`message_lock_expected_mode` 相关持久化键与切换守卫；解析器忽略 DB 中的历史键。
- 删除 `message_lock_provider.py` 与其测试；`keyed_async_locks.py` 保留。
- `telegram_live_listener.py`：`operation_lock` 参数改为直接接受 `KeyedAsyncLockRegistry`（或 `None`），`resolve_message_lock_mode` / `resolve_lock_context` 删除，调用点改为 `registry.lock(chat_id)`（以该类现有 API 为准）。
- `web_app.py`：删除 `telegram_operation_lock`、`message_lock_provider`、`lock_all()` 的构造和调用（第 5899-5904、8352-8419、9187 行附近，以实际为准）；`/api/runtime/...` 里的锁快照输出改为 registry 快照。
- 在 `docs/ARCHITECTURE.md` 增加一句：跨进程互斥不存在进程内锁，靠 DB 状态。

## 任务 2：补偿循环去重

- ingest 的 `run_periodic_reconcile`（300s）保留其唯一价值：对照 Telegram 历史发现漏收消息并落库 + 入队。删除其中已被 worker 的 `run_authoritative_gap_recovery_loop`（20s）覆盖的"找无决策消息并重放"部分。
- 明确两者分工写进 `docs/ARCHITECTURE.md`：reconcile = 对照 Telegram 补漏收；gap recovery = 对照 DB 补未处理。
- `authoritative_gap_recovery_max_age_minutes` 的过期分类（stall / stale）逻辑不动。

## 禁止

- 不改 worker 内按仓位/符号的锁（`position_authority_lock.py`），那是执行侧真正的互斥边界。
- 不改识别/执行语义。不改表结构。不 push，不部署，不用 `git add -A`。

## 验证（L2 本地部分）

- `tests/test_live_listener_chat_isolation.py` 改写为：同 chat 串行、跨 chat 并行、reconcile 与 live 不互相阻塞。
- `tests/test_runtime_event_loop_blocking_census.py` 通过。
- 全量 `python -m pytest -q` 0 failed。
- 本地 `TELEGRAM_KOL_RUNTIME_ROLE=ingest` 与 `=worker` 各能独立启动（不连外部服务）。

## 完成条件

1. 提交：锁层；补偿循环；文档。
2. 更新状态文件到 `current_step: 5`、`current_step_file: .../step-5-one-off-modules-and-flag-inventory.md`。
3. `send_message` 给 `brain_session_id`：采用的方案、分支、SHA、删除行数、web 端失去的互斥点及替代方式、全量末行。
