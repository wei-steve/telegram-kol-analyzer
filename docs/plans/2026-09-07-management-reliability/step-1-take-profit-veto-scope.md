# A-1：止盈收敛的否决只作用于保护单行（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-1-tp-veto-scope`，工作树 `.worktrees/mgmt-step-1`。

## 问题

`src/telegram_kol_research/trigger_take_profit_convergence_executor.py` 第 506–511 与 746–751 行：
对 `read_complete_pending_tpsl_snapshot(instrument_id)` 返回的**全部** pending 行逐行调用
`_row_has_protection_fields(row) and not _native_tpsl_aliases_consistent(row)`，任一行不一致就返回
`convergence_pending_alias_conflict`（或 `_before_write`）全局否决。
`native_tpsl.protection_order_sides_consistent` 的 docstring 写明只能用于保护单（side 必须与 posSide 相反），
而带附带止损的**入场条件单**（side 与 posSide 同向）也带保护字段，于是被误判为冲突。

后果（服务器笔记第七轮）：最近 6 次止盈收敛全部失败，227/230 两次正是此码；当前持仓只有全仓止损，
没有任何分档止盈。

## 任务

1. 先写复现测试：构造一张入场条件单行（`side=buy, posSide=long, closeSLTriggerPrice` 存在）与一张正常
   TPSL 行并存的 pending 快照，断言修复前返回 `convergence_pending_alias_conflict`，修复后不再否决。
2. 把否决范围收窄为“保护单行”：判据必须是**结构性**的（例如 `ordType`/类型字段标识为 TPSL、
   或该行不含入场专有字段），不能靠价格或数量。先读 `native_tpsl.normalize_native_tpsl` 与
   `deepcoin_order_matching` 里已有的行分类函数，优先复用；没有就新增一个 `is_protection_order_row(row)`
   放在 `native_tpsl.py`，并写清判据来源（引用 `docs/2026-09-05-deepcoin-order-vs-trigger-order.md`）。
3. 真正的保护单行之间若仍不一致，否决保留原样，只是原因码要带上冲突行的 ordId 与字段原文
   （`refusal_detail`），写进 `trigger_take_profit_convergences.reason_detail` 或等价字段；没有这样的列就记进
   `evidence_json`。**不加列**；若必须加列，停下报告。
4. 同一判据应用到 `trigger_backup_stop_executor.py` 里 647/701/733/744 行的同类调用。
5. 不改任何其他收敛判据（`convergence_exact_leg_not_verified`、`convergence_partial_position_unexplained` 等本步不动）。

## 禁止

- 不改交易所写入的参数、顺序、幂等键。
- 不修改 `position_take_profit_orders` / `position_protection_ledger` 已有行。
- 不引入模式开关。不用 `git add -A`。

## 验证（L2）

- focused：复现测试 + 判据函数的正反用例（入场条件单、TPSL、市价 reduceOnly、缺字段行）。
- 全量 `.venv/bin/python -m pytest -q` 0 failed。
- 部署 tg-deploy，记录回滚 SHA；部署前确认 `active_write_count=0`、无 planned/executing/reconciling 批次。
- 观察：目标一个含 ≥5 条真实消息的 30 分钟健康窗口（AGENTS.md L2，后台监视器持续观察直到凑够）。
  本步的核心验收是：`trigger_take_profit_convergences` 新增记录中不再出现 `convergence_pending_alias_conflict`；
  若窗口内有活跃仓位，逐个确认分档止盈是否被重新建立（`position_take_profit_orders` 新增行 + 交易所条件单直读），
  并逐笔确认新建止盈的方向、数量、价格与账本一致。窗口内出现任何非预期交易所写入立即回滚。
- 若当前没有活跃仓位、无法验证止盈重建，如实记录“无样本”，阶段仍可 completed（判据修正本身已由测试证明），
  但证据区要写明待下一次持仓时复核。

## 完成条件

更新状态文件到 `current_step: 2`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-2-alerting.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、复现测试结论、观察窗内收敛记录的原因码分布、
止盈重建逐笔确认结果或“无样本”。
