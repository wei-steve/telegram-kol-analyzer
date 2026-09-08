# 管理指令可靠性修复：方案索引

状态文件：`docs/management-reliability-status.md`。排查报告：
`docs/2026-09-07-management-instruction-incident-read-only-diagnosis.md`；
服务器侧笔记（1530 行）：`docs/2026-09-07-management-instruction-incident-server-notes.md`。

## 事故链（已证实）

大镖客 2026-09-07 03:01Z 保本离场未执行，六个环节：

1. 09-04 08:34Z 第一档止盈在交易所成交，仓位 10 → 5。
2. 止损单未缩量（交易所与账本都停在 10）。止盈收敛记录 222 按设计以
   `convergence_partial_position_unexplained` 冻结并发告警（已投递），无人工处置入口。
3. 48 分钟后减仓批次 158 终检发现保护量是在仓量两倍，正确拒绝；批次进入
   `recovery_required` 后无恢复路径，卡至今。
4. 规则“有未了结减仓批次即冻结该策略一切管理指令”；该仓位 13:07Z 被止损全平，账本仍 active，冻结持续三天。
5. 03:01Z 消息无回复引用，解析按“价格描述 + 策略活跃”关联到该已死策略（真实持仓是 lifecycle 1097）；
   被冻结拦下后，`partial_failed` 被执行边界升级为 `outcome_unknown`，五次重试全废。
6. 失败事件类型不在 Telegram 白名单，用户零告警。

峰哥 09-06 23:10Z 止盈未执行：09-04 一条入场实际失败，lifecycle_monitor 模拟成 entered 形成幽灵策略 1081，
目标不唯一 → 既不执行也不通知。

其他证实的缺陷：被 `waiting_source_deletion_exit` 推迟的指令永不恢复（29 条积压，含陈哥 09-07 00:37Z
的 auto_trade 入场）；最近 6 次止盈收敛全部失败，其中两次是 `convergence_pending_alias_conflict`
误判入场条件单为保护单；只通知群的跳过被记成“识别失败”；操作员 bot 接收任务崩溃不自愈；
`strategy_management_notifications` 自 07-21 零投递；`position_protection_incidents` 投递 pending。

## 步骤文件

1. `step-1-take-profit-veto-scope.md`
1b. `step-1b-convergence-retry-whitelist.md`
2. `step-2-alerting.md`
3. `step-3-deferred-resume-and-backlog.md`
3b. `step-3b-contact-digits-not-prices.md`
3c. `step-3c-unreadable-env-file.md`
3d. `step-3d-entry-admission-reconciler-disabled.md`（在 step 4 之后）
4. `step-4-ledger-repair.md`
5. `step-5-protection-resize-and-batch-timeout.md`
5b. `step-5b-frozen-convergence-backlog.md`
5c. `step-5c-partial-fill-evidence-from-order-history.md`
6. `step-6-rejection-semantics-and-frozen-bypass.md`
7. `step-7-target-resolution-and-ghost-lifecycles.md`

每份自包含，执行会话只读自己那一份。
