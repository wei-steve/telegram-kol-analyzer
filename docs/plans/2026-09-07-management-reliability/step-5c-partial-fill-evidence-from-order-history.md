# A-5c：部分止盈成交的证据扩展到 orders-history，并重判活跃仓位的冻结收敛（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-5c-partial-fill-evidence`，工作树 `.worktrees/mgmt-step-5c`。

## 问题（A-5b 盘点发现）

A-5 任务 1 的判据三要求该止盈单在 `trigger-orders-history` 里 `triggerTime` 非 0。生产实测：2026-09-08 01:23Z 之后创建的
TPSL 单**一张都没进过** `trigger-orders-history`（BTC 最新 `1001125172997119`、ETH 最新 `1001125172457033`），而它们的成交
在 `orders-history` 里以"方向相反、数量恰等于该档、成交价恰等于触发价"的市价平仓单出现（conv 237 的 TP1/TP2：
`1001125181469680` buy 1 @2470.03 06:02:29Z、`1001125186263340` buy 0.6 @2450 13:39:12Z）。因此判据三在当前交易所行为下
拿不到证据，A-5 任务 1 基本不会触发。`list_trigger_order_history_by_order_id` 的 `ordId` 过滤实测无效（返回 0 行）。

## 任务

1. 判据三扩为二选一：(i) `trigger-orders-history` 该 ordId 已触发；或 (ii) `orders-history` 存在一笔已成交单：方向与仓位相反、
   `sz` 恰等于该档计划量、成交均价与该档触发价之差不超过该合约 tick 的 2 倍、`cTime` 晚于该档创建时间且早于观测到减仓的时刻、
   `reduceOnly`/平仓标记为真（若字段存在）。两条证据都记进解释结果的 `evidence_json`，注明用的是哪一条。
2. 多档同数量时仍判 `partial_reduction_take_profit_ambiguous`（沿用 A-5），除非 (ii) 的成交价能唯一对应一档。
3. 对 `conflicted / convergence_partial_position_unexplained` 且仓位仍活跃的收敛，每轮做一次重判（不再被 `continue` 跳过）；
   能解释的记止盈行 `completed`、收敛放回 `waiting_backup_stop`，交生产代码自行判断；不能解释的保持冻结。
4. 把 `list_trigger_order_history_by_order_id` 标记为不可靠（docstring + 调用点改用全量翻页过滤），或删除其调用。
5. B 线阶段 6 上线后，WS `TriggerOrder.TS` 变化作为第四条证据，本步只留接口。

## 禁止

- 不放宽数量与价格的精确匹配；不用时间接近单独认领；不用 `git add -A`；不碰 B 线锁定文件。

## 验证（L2）

- focused：(i)/(ii) 各自成立与不成立、tick 容差边界、多档同数量的歧义、conflicted 重判只作用于活跃仓位。
- 全量 0 failed；部署 tg-deploy；观察按 L2；conv 237 部署后应被重判并解释 TP1/TP2，若系统随后缩量止损或重建收敛，逐笔核对。

## 完成条件

更新状态文件到 `current_step: 6`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-6-rejection-semantics-and-frozen-bypass.md`；
`send_message` 给 `brain_session_id`。
