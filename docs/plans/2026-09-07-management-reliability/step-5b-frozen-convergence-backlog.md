# A-5b：追既往被 `convergence_partial_position_unexplained` 冻结的止盈收敛（L2；仓位活跃的行可能触发系统按既有路径挂止盈）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-5b-frozen-convergences`，工作树 `.worktrees/mgmt-step-5b`。

## 问题（A-5 部署前盘点）

`trigger_take_profit_convergences` 里带 `convergence_partial_position_unexplained` 的 conflicted 收敛 29 条。A-5 的
三判据解释只作用于 `submitted` 状态的收敛，`conflicted` 的直接 `continue`，因此既往冻结不会自动解开（含 222）。

## 任务

1. 只读盘点 29 条：binding、leg、pos_id、仓位当前是否活跃（positions 直读）、各止盈单在 trigger-orders-history 的触发状态、
   在仓量与账本计划量。清单 send_message 给指挥会话。
2. 仓位已平的：写 `completed / convergence_position_terminal`（照 A-4 对 230 的处理），审计记录 historical_cleanup。
3. 仓位仍活跃的：用 A-5 的三判据只读重判一次；能解释的（数量相等 + 止盈单在本 binding 账本 + 交易所显示已触发）把对应止盈单记
   `completed`、收敛放回 `waiting_backup_stop` 让生产代码自行重判并按既有路径处理；不能解释的保持冻结并把判定现场写进 `error_json`。
4. 先备份、副本演练、`PRAGMA quick_check`、before/after 逐行值，再改生产；每一行审计。

## 禁止

- 不手动建任何交易所单；止盈由系统既有收敛路径自己建。不改 A-5 的判据。不用 `git add -A`。

## 验证（L2）

- 副本演练与生产 quick_check；活跃仓位的收敛复位后若系统建出止盈，逐笔核对方向、数量之和等于在仓量、价格与账本 desired 一致。

## 完成条件

更新状态文件到 `current_step: 5c`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-5c-partial-fill-evidence-from-order-history.md`；
`send_message` 给 `brain_session_id`：备份路径、29 条逐行处置、复位后系统建单的逐笔核对。
