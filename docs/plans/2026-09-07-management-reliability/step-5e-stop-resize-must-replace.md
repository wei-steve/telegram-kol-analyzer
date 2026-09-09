# A-5e：止损缩量必须"先挂新、确认后撤旧"，不能只叠加（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-5e-stop-resize-replace`，工作树 `.worktrees/mgmt-step-5e`。

## 问题（B 线 6-pre-3 实测）

`set-position-sltp` 是**叠加**语义：每次写入新增一张 TPSL（新 ordId），旧单原样留着；18 次生产写入得到 18 个不同 ordId，
同一 posId 上并存多张止损。A-5 任务 2 的止损缩量（`stop_loss_size_convergence.py`）只调用 `submit_exact_position_sltp`
挂新的小数量止损，**不撤旧的大数量止损**；回读按新 ordId 匹配成功后把账本 `size_text` 改成新数量，于是账本认为已缩量，
交易所上却同时挂着旧（如 sz=10）与新（sz=5）两张。同价并存时旧单仍可能先触发。该路径至今零触发，必须在首次触发前修好。
对照：`break_even_convergence_executor.py` 已有 `cancel-stop:<old_order_id>` 的撤旧步骤，是正确形状。

## 任务

1. 缩量流程改为：挂新止损（数量 = 在仓量）→ 回读确认新单在 `trigger-orders-pending`（按 `slTriggerPrice` / `posSide` / `sz`）
   → 用 `cancel_position_sltp` 按旧 ordId 撤旧单 → 回读确认旧单不在 → 才改账本 `size_text` 并把旧账本行标 `cancelled`。
   任何一步失败：新单已挂则不撤新单（仓位不裸奔），记 `stop_resize_replace_incomplete` 告警（ALWAYS_NOTIFIED）并冻结，
   由人处理；幂等键沿用 `sl-resize:<binding>:<pos_id>:<new_size>`，撤旧步骤用 `cancel-stop:<old_order_id>`。
2. 同样检查 A-5 任务 1 之后的其他"修改保护"路径（`position_take_profit_orders` 重建、`trigger_backup_stop`）是否存在
   "叠加当修改"的写法，列清单；有则纳入本步，无则记证据。
3. 测试：挂新成功撤旧成功 → 账本一致；挂新成功撤旧失败 → 告警冻结且新单保留；挂新失败 → 不撤旧、不改账本；幂等。

## 禁止

- 不撤全仓（`sz=0`）止损。不改止盈。不用 `git add -A`。

## 验证（L2）

- focused 与全量 0 failed；部署 tg-deploy；观察按 L2（该路径大概率无样本，如实记）。

## 完成条件

状态文件追加 step-5e 记录（A 线保持 done）；`send_message` 给 `brain_session_id`。
