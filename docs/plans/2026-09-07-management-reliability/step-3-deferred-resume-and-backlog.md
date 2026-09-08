# A-3：被推迟指令的恢复与超时；29 条积压作废并通知（L2 + L3 数据变更）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-3-deferred-resume`，工作树 `.worktrees/mgmt-step-3`。
**任务 3（作废积压）改生产数据，需证据区有用户对本步的批准记录**（2026-09-07 用户决定 `stale_pending_items_void_and_notify: true` 即为批准，执行前再向用户出示逐条清单确认一次）。

## 问题（服务器笔记第七轮）

`source_execution_barrier` 返回 `hold` 时消息被标 `deferred / waiting_source_deletion_exit`，
`recognition_decisions` 有决策行，因此不在权威缺口恢复的“无决策”候选里；
`source_message_deletion_worker.py` 完成退出后只 `_enqueue_source_deletion_notification`，从不重新入队被推迟的消息。
结果：`message_instruction_items` 29 条 `pending`（最早 07-22），`last_progress_at` 与 `escalation_state` 全为 null，
含陈哥群 auto_trade 入场 raw 15169（09-07 00:37Z）静默丢失，而 lifecycle 1096 显示 entered。

## 任务

1. 恢复：在 `source_message_deletion_worker` 每次退出流程到达终态（`succeeded` / 终止）后，查出因该退出而被
   `hold` 的 raw_message（按 barrier 判定所依据的关联键），把它们重新入队 `message_processing_jobs`
   （`last_reason="deferred_resume"`）。复用现有幂等入队函数，不重复建 job。
2. 超时：`deferred` 超过 `deferred_resume_timeout_minutes`（新 trading_settings 字段，默认 30）仍未恢复的，
   由 worker 的 gap recovery 循环生成 `runtime_incidents`（类型 `deferred_instruction_expired`，进白名单），
   并把该消息的 `automation_reason` 标为 `deferred_expired`，**不自动执行过期入场**。
3. 积压作废（L3，逐条出示后再做）：对 29 条 `pending` 指令项逐条列出 raw id、群、意图、posted_at，
   向用户出示清单；用户确认后把它们标为 `failed`，`error_json={"reason":"stale_pending_voided_2026_09_07"}`，
   对应 lifecycle 若为 `entered` 且 `execution_binding_id` 为 NULL，改为 `cancelled`（或项目已有的等价终态，
   先查 `strategy_lifecycles.lifecycle_status` 的取值集合），并为每条发一条 Telegram 通知说明已作废。
   生产库先 `sqlite3 .backup`，记录 before/after 行数与 `PRAGMA quick_check`。
4. 时间戳：指令项每次状态变化更新 `last_progress_at`；`escalation_state` 在超时时置 `expired`。

5. （step 2 遗留，指挥会话裁定）把 `management_stop_rejected` 加进 `config.ALWAYS_NOTIFIED_INCIDENT_TYPES`
   （生产首例 incident 2069，2026-09-08 06:26:24Z，high，累计零投递）。一行加一条测试，随本步一起部署。

## 禁止

- 不补执行任何过期入场或管理指令。
- 作废操作只允许在用户逐条确认后执行，且只改列出的那些行。
- 不用 `git add -A`。

## 验证

- focused：hold → 退出完成 → 自动重新入队且只入一次；超时事件生成；作废函数只改指定 id。
- 全量 0 failed。部署 tg-deploy，记录回滚 SHA。
- 观察：目标一个含 ≥5 条真实消息的 30 分钟健康窗口（后台监视器持续观察直到凑够）。核心验收：
  `message_instruction_items` 新增 `pending` 行的 `last_progress_at` 非空；若窗口内出现 `hold`，确认它在退出完成后被恢复。
- 作废：备份路径、quick_check、29 行 before/after 状态、通知送达 29/29。

## 完成条件

更新状态文件到 `current_step: 3b`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-3b-contact-digits-not-prices.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、恢复与超时的测试结论、作废清单与通知结果。
