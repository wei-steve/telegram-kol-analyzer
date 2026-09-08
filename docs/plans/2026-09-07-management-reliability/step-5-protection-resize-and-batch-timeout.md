# A-5：部分止盈成交后自动收敛止损数量；待恢复批次超时告警（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-5-protection-resize`，工作树 `.worktrees/mgmt-step-5`。

## 问题（服务器笔记第七轮）

止盈收敛记录 222：三档止盈建成后，TP1（sz 5 @81100）09-04 08:34:43Z 成交，8 秒后收敛以
`convergence_partial_position_unexplained` 冻结并发 critical 告警；止损单与账本都停在 sz=10，在仓 5。
随后减仓批次 158 终检正确拒绝，进入 `recovery_required` 后无任何恢复或超时路径，冻结该策略三天。

## 任务

1. **可解释的部分减仓**：当仓位减少量恰好等于本 binding 自己的某张止盈单数量、且交易所
   `trigger-orders-history` 里该止盈单 `triggerTime` 非 0（已触发），把这次减少判定为
   `partial_take_profit_filled`，记录对应 `position_take_profit_orders` 行为 `completed`。
   判据必须同时满足“数量相等 + 该单 ordId 在本 binding 账本里 + 交易所历史显示已触发”三条，缺一不可；
   不满足仍按 `convergence_partial_position_unexplained` 冻结。
   （B 线阶段 6 上线后，WS 的 `TriggerOrder.TS` 变化会成为第四条更早的证据，本步先用 REST 历史。）
2. **止损缩量**：判定为 `partial_take_profit_filled` 后，用现有 `position_mutation_gateway` 的意图与回读校验，
   把主止损单数量改为当前在仓量（走 `set-position-sltp` / 现有修改保护的路径，不新建接口调用方式），
   幂等键 `sl-resize:<binding>:<pos_id>:<new_size>`。回读确认后才更新 `position_protection_ledger.size_text`。
   回读不一致 → 保持冻结并告警，不重试。
3. **批次超时**：`recovery_required` 的管理批次超过 `management_recovery_timeout_minutes`（新 trading_settings
   字段，默认 60）仍未解决 → 生成 `runtime_incidents`（类型 `management_recovery_timeout`，white-listed）并把
   批次标为 `blocked`（reason `recovery_timeout`），从而解除对该 lifecycle 的冻结；批次本身不自动重跑。
   仓位已在交易所消失的批次（用 positions 直读确认）直接 resolved（reason `position_closed_before_management`）。
4. 冻结与拒绝都记录判定点字段原文（在仓量、保护量、涉及的 ordId），不能只记结论。

### 5. 小仓位分档策略（1c 观察发现，需用户单独决定）

pos 1001125178552543 在仓 3 张，desired 50%/30%/20% 按 quantity_step=1 得 1.5/0.9/0.6，每档不足 1 张，
收敛 fail-closed 为 `convergence_target_size_below_minimum`，仓位因此没有任何止盈。既有行为，非缺陷，
但需要一个明确策略。候选：按可分配张数缩减档位数（3 张 → 两档 2/1 或一档 3），最少一档；或保持现状只挂止损。
**这是交易语义，须用户在本步领取前单独决定并记进证据区。**

## 禁止

- 不改止盈单价格或数量；只改主止损单数量且只能改小到等于在仓量。
- 不做任何“猜测来源”的部分减仓解释。
- 不用 `git add -A`。

## 验证（L2）

- focused：三条判据各自不满足时不解释；解释成功后止损缩量的幂等与回读；批次超时转 blocked；仓位已消失转 resolved。
- 全量 0 failed。部署 tg-deploy，记录回滚 SHA，部署前确认无在途管理批次。
- 观察：目标一个含 ≥5 条真实消息的 30 分钟健康窗口（后台监视器持续观察直到凑够）。若窗口内发生真实的
  部分止盈成交，逐笔确认止损缩量的意图、回执、回读；否则记录“无样本”，判据由测试证明。
- 交易所直读：本步窗口内的每一次 `set-position-sltp` 都必须能对应到一条判定为 `partial_take_profit_filled` 的记录。

## 完成条件

更新状态文件到 `current_step: 6`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-6-rejection-semantics-and-frozen-bypass.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、测试结论、窗口内止损缩量逐笔记录或“无样本”、超时转 blocked 的批次清单。
