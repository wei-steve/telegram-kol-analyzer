# Step 3 — 删除 inline 与 shadow 消息/命令路径（L2，本地部分）

状态文件：`docs/post-migration-cleanup-status.md`。先领取。分支名：`cleanup/step-3-delete-inline-shadow`。
前置：步骤 2 已 completed 并合并。

## 目标

`message_pipeline_mode` 和 `worker_command_mode` 只剩 `queue` 一种行为。ingest 角色的 Telethon 回调只做"落库 + 入队"，
不再持有任何可执行识别/执行的内联分支；shadow 双写路径整体删除。

这是"删路径"不是"改语义"：queue 路径上识别、上下文解析、策略、执行、交易所写入的代码一行都不能动。

## 任务 1：盘点（先提交盘点，再动手）

在 `docs/plans/2026-09-06-post-migration-cleanup/step-3-inventory.md` 写下：

- `grep -n "pipeline_mode\|worker_command_mode\|shadow" src/telegram_kol_research/telegram_live_listener.py src/telegram_kol_research/message_processing_worker.py src/telegram_kol_research/web_app.py src/telegram_kol_research/worker_command*.py` 的完整命中清单，每条标注"删 / 保留并简化 / 不动"。
- 与之对应的测试文件清单（`grep -rln "inline\|shadow" tests/ | xargs grep -ln "pipeline_mode\|worker_command_mode"`）。
- 已知重点位置（以实际为准）：`telegram_live_listener.py` 第 143 行附近的 `is_shadow`、494-526 的入队辅助、1208-1311 的 `run_reconcile_once` 内联恢复闭包、1618 的 handle 分支；`message_processing_worker.py` 720 行的 `!= "queue"` 检查；`web_app.py` 4784 行的 worker command mode 分发和 8398-8419 的模式切换守卫。

单独提交盘点文件。

## 任务 2：删除路径

- `trading_settings.py`：两个字段的 `Literal` 收窄为 `Literal["queue"]`；解析器对 DB 中的历史值 `inline`/`shadow` 记一条 warning 日志并按 `queue` 处理（不能抛错，生产 DB 行必须仍可读）。
- `telegram_live_listener.py`：`handle_new_message` / `handle_deleted_message` / `run_reconcile_once` / gap recovery 中所有非 queue 分支删除；`_try_enqueue_shadow_processing_jobs` 之类的 shadow 命名改为直白的 enqueue 命名；`_try_mark_shadow_processing_jobs_terminal` 若只服务 shadow 则删除。删除后 `authoritative_processor` 参数如果在 ingest 路径里已无人调用，从 listener 签名里移除并更新 `web_app.py` 的调用点。
- `message_processing_worker.py`：删除 mode 检查，worker 无条件消费。
- `web_app.py`：删除 worker command 的 inline 直跑分支和 shadow 分支；`require_worker_command_mode_transition_safe` / `supervise_worker_command_mode` 若只剩单一模式则删除或退化为空操作并删除调用；`/api/trading-settings` 里关于这两个模式切换的守卫删除。
- 数据库：`message_processing_jobs` 若有 `shadow` 列/标记，本步不改表结构（那是 L3），只停止写入非 queue 值；在盘点文件里记下列名，留给以后。

## 任务 3：测试

- 删除只为 inline/shadow 存在的测试（步骤 2 已用注释 `scheduled for removal in cleanup step 3` 标好）。
- 保留并改写的：queue 路径的入队幂等、崩溃后 pending 重领、gap recovery 只入队不执行、worker 消费一次且只一次。
- 新增一个测试：从 DB 读到历史值 `inline`/`shadow` 时得到 `queue` 且不抛错。
- `tests/test_runtime_event_loop_blocking_census.py` 必须仍通过（它守护事件循环不被同步 IO 阻塞）。

## 禁止

- 不动 `message_lock_mode`、`MessageLockProvider`、`KeyedAsyncLockRegistry`、`run_periodic_reconcile` 与 gap recovery 循环的取舍（步骤 4）。
- 不动 queue 路径内部的识别/执行调用顺序、参数、异常处理。
- 不改表结构。不 push，不部署，不用 `git add -A`。

## 验证（L2 本地部分）

- 聚焦：消息管道、worker、gap recovery、worker command、阻塞普查相关测试 `-x`。
- 最终候选一次全量 `python -m pytest -q`，0 failed。
- `python -c "import telegram_kol_research.web_app, telegram_kol_research.cli"` 通过。
- 本地以 `TELEGRAM_KOL_RUNTIME_ROLE=all` 启动 web（不连 Telegram、不连交易所也能起）并 `GET /api/trading-settings` 返回两个字段均为 `queue`。
- 生产 30 分钟 / 5 条真实消息观察放在步骤 6，本步不做。

## 完成条件

1. 提交：盘点；路径删除；测试整理。
2. 更新状态文件到 `current_step: 4`、`current_step_file: .../step-4-lock-layer-and-reconcile.md`。
3. `send_message` 给 `brain_session_id`：分支、SHA、删除行数（`git diff --shortstat` 相对起点）、删掉/保留/新增的测试数、全量末行、盘点文件里标为"以后"的项。
