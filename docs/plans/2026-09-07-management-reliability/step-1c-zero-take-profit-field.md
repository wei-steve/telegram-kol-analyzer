# A-1c：止盈字段为字面量 `0` 不应被当作"存在未拥有的止盈单"（L2，会在活跃仓位上挂出止盈单，需用户单独批准）

状态文件：`docs/management-reliability-status.md`。先领取；证据区须有本步批准记录。
分支 `mgmt/step-1c-zero-take-profit-field`，工作树 `.worktrees/mgmt-step-1c`。

## 问题

A-1 修正了 `is_protection_order_row`（把入场条件单误当保护单），A-1b 让被那次误判冻结的收敛可以重试。
重试确实生效了：收敛 230 被拉回并重新 `ready`。但执行器计划期随后被**同一族的第二处误判**否决为
`convergence_unowned_take_profit_present`，230 因此在 `ready → conflicted → ready` 之间循环，
pos `1001125163581280`（8 张 BTC 多单）至今只有止损、没有分档止盈。

判定链（`trigger_take_profit_convergence_executor.py`）：

1. `_row_has_take_profit_fields`（1363 行）只把 `None` 与空串视为"无止盈字段"，
   于是把**只有止损、止盈字段是字面量 `"0"`** 的 TPSL 行判为"带止盈字段"。
2. 同一行交给 `normalize_native_tpsl` 时，`take_profit_trigger_price` 走
   `_first_positive_decimal`，`"0"` 正确地解析为 `None`。
3. `_unowned_pending_take_profit_present`（1128 行）因此走到
   `order is None or order.take_profit_trigger_price is None → return True`，
   判成"存在本地不拥有的止盈单"，整笔收敛 fail-closed。

即：步骤 1 与步骤 2 对"止盈字段是否存在"的判定不一致，`0` 在一处算"有"、在另一处算"无"。

BTC-USDT-SWAP 当前 3 行 TPSL 保护单全部命中（2026-09-07 只读快照，判定点读到的字段原文）：

- `ordId=1001125163581378` `TPSL` `side=sell` `posSide=long` `sz=0` `slTriggerPrice=78500` `closeSLTriggerPrice=78500` `tpTriggerPrice="0"` `closeTPTriggerPrice="0"` `tpPrice="0"`
- `ordId=1001125163582992` `TPSL` `side=sell` `posSide=long` `sz=0` `slTriggerPrice=78343` `closeSLTriggerPrice=78343` `tpTriggerPrice="0"` `closeTPTriggerPrice="0"`
- `ordId=1001125167675480` `TPSL` `side=sell` `posSide=long` `sz=10` `slTriggerPrice=78500` `closeSLTriggerPrice=""` `tpTriggerPrice="0"` `closeTPTriggerPrice=""`

## 任务

1. 让 `_row_has_take_profit_fields` 与 `normalize_native_tpsl` 的语义对齐：**只有能解析成正数的止盈字段
   才算"带止盈"**。具体三分支，缺一不可：
   - `None` / 空串 → 无止盈（与现状一致）；
   - 能解析且 `> 0` → 有止盈（与现状一致）；
   - 能解析但 `<= 0`（`"0"`、`"0.0"`、负数）→ **无止盈**（本步唯一的行为变化）；
   - **解析不了的垃圾值 → 仍算"有止盈"，保持 fail-closed**（不得顺手放宽这一支）。
2. 不动 `_unowned_pending_take_profit_present` 本身的其余分支，不动
   `_row_has_protection_fields`（它服务的是 A-1 的别名一致性判定，语义不同：那里 `0` 确实
   表示"这一行声明了该字段"，与"是否存在一个真实止盈价"是两回事）。
3. 加测试：只有止损、止盈字段为 `"0"` 的 TPSL 行不再触发
   `convergence_unowned_take_profit_present`；带真实正数止盈价且不在本地账本的行**仍然**触发；
   止盈字段为无法解析的垃圾值的行**仍然**触发。

## 禁止

- 不改止盈价格、档位、数量的计算。不动止损。不动 A-1 的 `is_protection_order_row`。
- 不放宽"解析不了 → fail closed"这一支。
- 不用 `git add -A`。不动 B 线的文件。

## 部署前只读预演（本步已先做过一次，部署前需按当时的实时快照重做）

在生产上用只读连接 + `no_autoflush` 调用 `_prepare_plan`，对比当前实现与候选修法。
2026-09-07 的预演结果（脚本 `/tmp/rehearse_1c.py`，证据目录 `mgmt-step-1b/`）：

- 当前生产实现 → `convergence_unowned_take_profit_present`（复现线上现象）
- 候选修法 → **通过**，产出 3 张止盈 payload：
  `tpTriggerPx=80700 sz=4` / `81400 sz=2` / `82100 sz=2`，
  `posId=1001125163581280` `posSide=long` `tpOrdPx=-1`，数量合计 8 = 在仓量。

## 验证（L2）

- 全量 0 failed。部署 tg-deploy，记录回滚 SHA，部署前确认 `active_write_count=0`、无在途管理批次。
- 部署后 15 分钟内逐笔核对新建止盈单：`position_take_profit_orders` 新增行、
  `position_mutation_intents` 回执 `sCode=0`、交易所条件单直读，三者一致；
  方向 `sell` / `posSide long`、数量之和等于在仓量、价格与账本 desired 一致。
  任何不一致立即 `tg-deploy` 回滚并报告。
- 之后按 L2 完成一个 ≥5 条真实消息的 30 分钟健康窗口，窗内零非预期写入。

## 完成条件

更新状态文件到 `current_step: 2`、
`current_step_file: docs/plans/2026-09-07-management-reliability/step-2-alerting.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、每张新建止盈单的逐笔核对结果。
