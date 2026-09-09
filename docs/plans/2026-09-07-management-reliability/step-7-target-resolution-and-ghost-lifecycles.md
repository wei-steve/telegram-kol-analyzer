# A-7：目标不唯一或无活跃仓位时通知确认；入场失败不得模拟为已入场；只通知群跳过用独立状态（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-7-target-resolution`，工作树 `.worktrees/mgmt-step-7`。
用户决定 `ambiguous_target_notifies_user: true`：目标不唯一或无活跃仓位时**通知确认**，不自动改指向。

## 问题（排查报告 + 服务器笔记第二、六轮）

- 峰哥 raw 15155：`context_resolution_attempts` 4833 `unresolved / target_ambiguous`，两个候选里一个是幽灵
  lifecycle 1081（入场失败但被 `lifecycle_monitor` 模拟成 entered）。`strategy_alerts` 9621 `ignored_not_strategy`，
  `forwarded_at` 为空，无通知。
- 大镖客 raw 15201：解析到 lifecycle 1074（binding 337，仓位 09-04 已平），真实持仓是 lifecycle 1097；
  识别层自述“未通过 reply 明确指向，基于价格描述和策略活跃状态关联”。
- 只通知群（notify_only）的管理指令被记成 `message_recognitions.status='识别失败'` +
  `automation_reason='mimo_authoritative_not_safely_applied'`，与真正的识别失败共用字段。

## 任务

1. **候选集只认可验证的活跃仓位**：上下文解析在 auto_trade 群里给管理指令挑候选时，候选 lifecycle 必须
   `execution_binding_id` 非 NULL 且对应 binding 的 pos_id 在最近一次交易所持仓快照里存在（用现有
   `read-only-exchange-snapshot` 或 positions 直读，快照过期 > 5 分钟则不做判定、走通知）。
   幽灵 lifecycle 不进候选。
2. **目标不唯一 / 无活跃仓位 → 通知确认**：生成一条 `runtime_incidents`（类型 `management_target_needs_confirmation`，
   白名单内），Telegram 文案列出候选（群、KOL、合约、方向、入场价、开仓时间）与消息原文摘要，指令项状态置
   `awaiting_user_confirmation`；不执行、不猜。用户通过操作员 bot 现有的确认命令回复后再执行（若现有 bot
   没有“选择候选”的命令，本步只做通知与状态，把“回复选择”记为遗留）。
3. **入场失败不得模拟为已入场**：`lifecycle_monitor` 的模拟成交只允许作用于 notify_only 群的 lifecycle；
   auto_trade 群的 lifecycle 只能由真实 binding 的成交事件推进到 entered。对 raw 14843 那种
   `target_strategy_binding_visibility_retry_expired` 的入场失败，lifecycle 应进入 `entry_failed`（或项目等价终态）。
4. **只通知群跳过用独立状态**：notify_only 群的管理指令记 `automation_status=skipped`、
   `automation_reason=notify_only_group`，`message_recognitions.status` 保持识别结果（是策略/非策略），不再写“识别失败”。

### 5. 账本整理（step 4 遗留）

- lifecycle 1074：exit_reason 改回 `stop_loss`、exited_at 改为 `2026-09-04 13:07:38`（交易所 position-history 事实），审计记录引用 step 4 证据。
- 10 条 binding 为 NULL 的 entered lifecycle（1091、1102、1105、1108、1112、1114、1116、1119、1120、1121）：notify_only 群的按设计保留但标注
  `simulated_only`；auto_trade 群的按任务 3 的新规则改为 `entry_failed`（或项目等价终态），逐条交易所直读确认无仓位后再改。

### 6. 三周未被认领的消息作业（A-6 发现）

`message_processing_jobs` 有 5 条 2026-08-20 的 `pending` 作业（id 9/11/12/14/15，raw 11768/11770/11771/11773/11774，
`history_reconcile_enqueued`，`attempt_count=0`），worker 的认领查询把它们排除在外。只读查清认领条件为何排除它们
（shadow 列？chat 过滤？水位线？），写进证据；这 5 条消息本身已过时，按 A-3 作废工具的形状标为 `expired`（reason
`stale_job_voided_2026_09_09`），并修正认领条件或加告警，避免再出现"永远排不到"的作业。

## 禁止

- 不自动把指令改指向另一个仓位。
- 不改 notify_only 群的任何交易行为（它们本来就不交易）。
- 不用 `git add -A`。

## 验证（L2）

- focused：幽灵不进候选；两个真实候选 → 通知且不执行；候选唯一且仓位可验证 → 正常执行；快照过期 → 通知；
  auto_trade 群 lifecycle 不被模拟；notify_only 跳过的状态字段。
- 全量 0 failed。部署 tg-deploy，记录回滚 SHA。
- 观察：目标一个含 ≥5 条真实消息的 30 分钟健康窗口（后台监视器持续观察直到凑够）。
  验收：窗口内 notify_only 群的管理消息不再出现 `mimo_authoritative_not_safely_applied`；
  若出现目标不唯一的真实指令，确认通知送达且未执行。

## 完成条件

更新状态文件到 `current_step: 8`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-8-recognition-failure-attribution.md`；`send_message` 给 `brain_session_id`：
分支、SHA、部署与回滚 SHA、测试结论、窗口内的状态分布与通知记录、遗留（如 bot 选择命令）。
