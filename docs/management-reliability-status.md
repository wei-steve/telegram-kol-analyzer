# Management Reliability Status（A 线：管理指令可靠性修复）

2026-09-07 两起管理指令未执行事故的修复项目。本文件是跨会话唯一的进度真相；
新会话只读本文件，再打开 `current_step_file` 指向的那一份步骤文件，不要读其他步骤文件。
与 B 线（`docs/rest-ws-trading-status.md`，REST + WebSocket 改造）并行，互不阻塞。

```yaml
project: management-reliability
plan_index: docs/plans/2026-09-07-management-reliability/README.md
diagnosis: docs/2026-09-07-management-instruction-incident-read-only-diagnosis.md
server_notes: docs/2026-09-07-management-instruction-incident-server-notes.md
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，执行会话完成后必须 send_message 到这里
integration_branch: codex/deepcoin-auto-trading-v1               # 每步完成后由指挥会话本地合并并 push
deploy: tg-deploy <sha>（AGENTS.md 部署一节）
current_step: 1
current_step_file: docs/plans/2026-09-07-management-reliability/step-1-take-profit-veto-scope.md
step_status: claimed          # planned | claimed | in_progress | completed | blocked
claimed_by: local_912f7e68-19a2-43f3-bd62-62ff8d223a2a
last_completed_step: 0
last_completed_commit: null
user_decisions_2026_09_07:
  risk_reducing_bypasses_frozen: true      # 全平 / 保本类指令绕过“未了结减仓批次”冻结
  ambiguous_target_notifies_user: true     # 目标不唯一或无活跃仓位 → 通知确认，不自动改指向
  stale_pending_items_void_and_notify: true  # 29 条积压指令项全部作废并逐条通知，不补执行
  rest_ws_phase5_continues_in_parallel: true
```

## 步骤总览

| 步 | 名称 | 风险 | 需用户单独批准 |
|---|---|---|---|
| 1 | 止盈收敛否决只作用于保护单行 | L2 | 否 |
| 2 | 告警补全：白名单三类、后台任务自愈、两条投递通道、健康端点 | L1 | 否 |
| 3 | 被推迟指令的恢复与超时；29 条积压作废并通知 | L2（作废积压为 L3 数据变更） | 是（作废积压那一步） |
| 4 | 账本修复：binding 337、批次 158、幽灵 1081、已成交仍活跃的止盈单 | L3 | 是 |
| 5 | 部分止盈成交后自动收敛止损数量；待恢复批次超时告警，不永久冻结 | L2 | 否 |
| 6 | 确定性拒绝不升级为结果未知；降风险指令绕过冻结 | L2（改交易语义） | 是 |
| 7 | 目标不唯一/无活跃仓位时通知确认；入场失败不得模拟为已入场；只通知群跳过用独立状态 | L2 | 否 |

## 执行会话的领取协议

1. 新建会话先读 `AGENTS.md`，再读本文件，再只读 `current_step_file`。
2. 确认 `step_status` 为 `planned`；若本步在“需用户单独批准”列为“是”，证据区必须已有该步的批准记录，没有就停下。
3. `step_status` 改 `claimed`，`claimed_by` 填本会话 ID，单独提交；开始改代码前改 `in_progress` 并提交。
4. 在独立工作树 `.worktrees/mgmt-step-N` 与分支 `mgmt/step-N-<slug>` 上工作，工作树里软链接 `.venv`。
5. 完成后按步骤文件“完成条件”更新本文件（`completed`、`last_completed_step`、`last_completed_commit`、`current_step` 推进、`step_status` 回 `planned`、`claimed_by` 置空、证据区追加），单独提交并 push；`send_message` 给 `brain_session_id`。
6. 全程遵守 AGENTS.md：不用 `git add -A`；部署用 tg-deploy；观察按 L2 规则用后台监视器。

## 硬性禁止（所有步骤）

- 不得用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领归属（与 B 线同一条）。
- fail-closed 的结论必须留下可归因材料：每一次拒绝、冻结、跳过都要记下判定点的字段原文，不能只记结论。
- 任何一步不得顺手修另一步的问题；发现新问题写进证据区。
- 改交易语义的步骤（3 的作废、4、6）必须有用户对该步的单独批准记录。
- 不动 B 线的文件（`deepcoin_private_ws.py`、`deepcoin_ws_*.py`、影子表）。

## 证据记录

执行会话在此追加，格式：`- step-N (日期, 会话ID): 提交 SHA；做了什么；验证结果；遗留问题`。

