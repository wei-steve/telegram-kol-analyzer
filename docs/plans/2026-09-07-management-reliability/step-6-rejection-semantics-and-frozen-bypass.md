# A-6：确定性拒绝不升级为“结果未知”；降风险指令绕过冻结（L2，改交易语义，需用户单独批准）

状态文件：`docs/management-reliability-status.md`。先领取；证据区须有本步批准记录
（2026-09-07 用户决定 `risk_reducing_bypasses_frozen: true` 即为批准）。分支 `mgmt/step-6-rejection-semantics`，工作树 `.worktrees/mgmt-step-6`。

## 问题（服务器笔记第二、四轮）

- `auto_trade_execution._message_instruction_status` 见到 `failed` 项返回 `partial_failed`；
  `execution_boundary._KNOWN_UNKNOWN_STATUSES` 把 `partial_failed` / `in_progress` 归为 `outcome_unknown`；
  `_run_leased_authoritative_execution` 随即抛 `authoritative_execution_outcome_unknown`，attempt 落 `uncertain`，
  该消息永不自动重试。9 条 uncertain 全部如此（in_progress ×4、partial_failed ×3、completed 无写入 ×2），
  `evidence_refs_json` 全为 NULL，即**没有任何交易所写入却被判为结果未知**。
- `strategy_management_planner.py:570-578` + `_load_partial_policy_state`：lifecycle 有未了结的 PARTIAL_INTENTS 批次即
  `frozen=True`，所有管理指令一律 `blocked`，包括 `full_exit` 与 `move_stop_to_break_even`。

## 任务

1. **区分确定性拒绝与结果未知**：执行边界只有在 `tracker.writes` 非空且含 `outcome_unknown`/`started`，
   或 raw_status 本身表示已向交易所发出请求但无回执时，才归为 `outcome_unknown`。
   `blocked` / `partial_failed`（且 writes 为空、每个 item 的 error_json 是确定性原因）→ `failed_safe`，
   attempt 记 `failed_safe` 并保留 `error_summary` 与各 item 的 `error_json` 到 `evidence_refs_json`。
   `in_progress`（writes 为空、指令已交给异步批次）→ `completed / not_started`，automation_reason `handed_off_to_batch`，
   attempt 正常收尾，不冻结消息。
   **保留**：writes 非空时的一切现有 fail-closed 行为一字不改。
2. **降风险指令绕过冻结**：`full_exit`、`move_stop_to_break_even`、`adjust_stop_loss`（只允许收紧方向）在
   lifecycle `frozen=True` 时不再 `blocked`，改为：先把未了结的减仓批次标为 `superseded_by_risk_reduction`
   （不执行它），再按现有路径执行降风险指令。`partial_take_profit` / `partial_then_break_even` 仍受冻结约束。
3. 所有拒绝与绕过都写审计行：原批次 id、冻结原因、绕过的指令类型、raw_message_id。

## 禁止

- 不改 writes 非空时的任何判定。
- 不让 `partial_*` 类指令绕过冻结。
- 不用 `git add -A`。

## 验证（L2）

- focused：blocked → failed_safe 且可重试；in_progress → handed_off；writes 含 unknown 仍 outcome_unknown；
  full_exit 在 frozen 下执行且原批次被 superseded；partial 在 frozen 下仍 blocked。
- 全量 0 failed。部署 tg-deploy，记录回滚 SHA。
- 观察：目标一个含 ≥5 条真实消息的 30 分钟健康窗口（后台监视器持续观察直到凑够）。
  验收：窗口内 `authoritative_execution_attempts` 新增 `uncertain` 条数为 0 或每条都有非空 `evidence_refs_json`；
  若出现真实降风险指令，逐笔确认执行与审计行。交易所直读确认无非预期写入。

## 完成条件

更新状态文件到 `current_step: 7`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-7-target-resolution-and-ghost-lifecycles.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、测试结论、窗口内 attempt 状态分布、绕过冻结的记录。
