# Step 6 — 集成部署与 L2 观察

状态文件：`docs/post-migration-cleanup-status.md`。
前置：步骤 1–5 全部 completed 并已合并进本地 `codex/deepcoin-auto-trading-v1`，本地与 `origin` 的差异只有清理提交。

本步走仓库既有的门控部署流程（AGENTS.md 的 stage / activate 两段式、精确 SHA、回滚 SHA），由用户决定交给 codex 或某个执行会话。方案里只规定验收，不重复部署命令。

## 部署前

- 在部署候选 SHA 上跑一次全量测试，0 failed。
- 确认 `GET /api/trading-settings` 在生产返回的两个模式字段为 `queue`（步骤 3 后已无其他取值）。
- 选择安静窗口：`active_write_count=0`、无 planned/executing/reconciling 的管理批次、队列无 pending。
- 回滚目标：部署前的生产 SHA，记入状态文件。

## 验收（L2）

- 三个 systemd 单元 active、`NRestarts=0`。
- 观察 30 分钟且至少 5 条真实消息，尽量覆盖 2 个群；不足则按 AGENTS.md 停止并记录，不无限延长。
- 队列：missing=0 / orphan=0 / stuck=0，无 duplicate 处理。
- `/api/runtime/loop-health`：stall_count 不增加。
- 交易所直读：观察窗内无非预期订单、无重复订单。
- `PRAGMA quick_check` ok。

## 完成条件

- 状态文件：`current_step: done`、`step_status: completed`，证据区记录部署 SHA、窗口、消息数、群数、异常。
- `send_message` 给 `brain_session_id`。
