# A-2：告警补全（L1）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-2-alerting`，工作树 `.worktrees/mgmt-step-2`。

## 问题（服务器笔记第三轮）

- `runtime_incidents` 投递白名单 `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` 不含
  `management_recovery_required`、`management_submit_unknown`、`context_worker_exhausted`，管理指令失败永不告警。
- 落成 `uncertain` 的权威执行尝试（raw 15006 / 15204）根本不生成任何事件。
- `system_operator_bot_command_task` 崩溃后只记录不重启（09-06 19:38Z 死了 6h48m）。
- `deployment-identity` 的 health 不含 `runtime_incident_notification` 与 `system_operator_bot_command`。
- `strategy_management_notifications` 自 07-21 零投递（95 行，delivered 28 / pending 67）。
- `position_protection_incidents` 383/384 `delivery_status=pending, notified_at=null`。
- `/etc/telegram-kol-worker.env` 里 `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空（token 已设）。

## 任务

1. 白名单：把上述三类加入默认白名单（代码默认值），并让 `context_worker_exhausted` 只在
   `operation` 以 `raw_message_` 开头时投递（实测现存积压全部带该前缀，过滤只对未来的 backfill/scanner 来源有效；挡住历史积压的唯一承重是 AFTER_ID 水位线）。**历史积压不补发**：投递器只处理
   本步部署之后新产生的事件（用现有 `AFTER_ID` 机制，部署时把门槛设为当前最大 id，记进证据）。
2. 权威执行落成 `uncertain` 时生成一条 `runtime_incidents`（类型 `authoritative_execution_uncertain`，
   severity high，summary 含 raw_message_id、error_summary、attempt id），并纳入白名单。
3. 后台任务自愈：`_log_background_task_result` 所在的启动逻辑对 `system_operator_bot_command`、
   `runtime_incident_notification`、`telegram_bot_command` 三个任务加指数退避重启（1s 起、上限 60s），
   每次重启记一条 warning 日志与计数；连续失败 10 次后停止重启并生成 critical 事件。
4. `deployment-identity` 的 health 补这两个任务的存活状态与上次投递时间（从 `runtime_incidents.notified_at` 取最大值）。
5. `strategy_management_notifications` 与 `position_protection_incidents` 两条通道：只读查明为何停投
   （配置、循环未启动、还是被异常吞掉），写进证据；若是代码缺陷且改动在 20 行以内就顺手修，
   否则记为遗留交步骤 5。
6. `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空：只报告，不改服务器配置；由用户决定。

## 禁止

- 不补发历史积压事件（会一次性发出上千条）。
- 不改任何交易逻辑。不用 `git add -A`。

## 验证（L1）

- focused：白名单默认值、`uncertain` 事件生成、任务重启退避与上限。
- 全量 0 failed。部署 tg-deploy，记录回滚 SHA。
- 部署后观察 15 分钟或 5 条消息：health 端点显示两个任务存活；人为 kill 一次 bot 长轮询连接（或注入异常）确认自愈；
  若窗口内产生新的白名单事件，确认送达。

## 完成条件

更新状态文件到 `current_step: 3`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-3-deferred-resume-and-backlog.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、AFTER_ID 门槛值、两条通道停投原因、自愈验证结果。
