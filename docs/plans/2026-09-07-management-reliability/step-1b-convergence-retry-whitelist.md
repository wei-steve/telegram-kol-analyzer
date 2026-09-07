# A-1b：被误判冻结的止盈收敛允许重试（L2，会在活跃仓位上挂出止盈单，需用户单独批准）

状态文件：`docs/management-reliability-status.md`。先领取；证据区须有本步批准记录。分支 `mgmt/step-1b-convergence-retry`，工作树 `.worktrees/mgmt-step-1b`。

## 问题

A-1 修正了 `convergence_pending_alias_conflict` 的误判，但 `execution_bindings.py:1330-1335` 只把
`conflicted` 收敛中 reason_code 属于 `convergence_verified_stop_missing` / `convergence_unowned_take_profit_present` /
`convergence_exchange_preflight_unavailable` 的行拉回重试。被旧缺陷冻死的收敛 227（binding 339 之后的仓位）与
230（binding 341，腿 586，BTC 多）是终态，当前持仓因此没有分档止盈。

## 任务

1. 把 `convergence_pending_alias_conflict` 加入该重试白名单。**不要**加入 `convergence_exact_leg_not_verified`
   或其他原因码，那些是真实的 fail-closed。
2. 重试时沿用现有流程：收敛重新 `ready` → 计划期用 A-1 修正后的判据复核 → `position_mutation_gateway`
   意图 → `set-position-sltp` → 回读确认。不新增任何写入方式，不改止盈档位与数量规则。
3. 加测试：conflicted + `convergence_pending_alias_conflict` 的行会被重新 ready；其他原因码仍被跳过。
4. 部署前只读列出当前会被拉回的收敛记录（预期 227/230，若 227 的仓位已平则应被现有 `leg` 校验跳过），
   写进证据并出示给用户。

## 禁止

- 不改止盈价格、档位、数量的计算。不动止损。
- 不用 `git add -A`。

## 验证（L2）

- 全量 0 failed。部署 tg-deploy，记录回滚 SHA，部署前确认无在途管理批次。
- 观察：部署后 15 分钟内确认收敛 230 是否重新 ready 并完成；逐笔核对新建止盈单（`position_take_profit_orders`
  新增行、`position_mutation_intents` 回执 sCode=0、交易所条件单直读）的方向 sell/posSide long、数量之和等于在仓量、
  价格与账本 desired 一致。任何不一致立即回滚并报告。之后仍按 L2 完成一个 ≥5 条真实消息的 30 分钟健康窗口
  （后台监视器持续观察直到凑够）。

## 完成条件

更新状态文件到 `current_step: 2`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-2-alerting.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、被拉回的收敛清单、每张新建止盈单的逐笔核对结果。
