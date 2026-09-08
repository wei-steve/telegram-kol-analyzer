# A-3d：入场准入恢复器因 `instruction_execution_contract_mode=shadow` 空转（先只读评估，再决定）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-3d-entry-admission-reconciler`，工作树 `.worktrees/mgmt-step-3d`。

## 问题（B 线阶段 5 会话只读追查 raw 15496，2026-09-08）

峰哥群（auto_trade）14:07:49Z 的 ETH 多入场：指令项 1029 在准入层被 `adjacent_entry_context_pending` 推迟
（`adjacent_entry_assembly.py:162`），`visibility_next_attempt_at=14:09:30`、`execution_deadline_at=20:09:25`。
本应由 `entry_admission_reconciler.reconcile_due_entry_admissions` 到点重试，但它第一行是
`if execution_contract_mode != "live": return`，而生产 `instruction_execution_contract_mode = shadow`，
恢复器是空操作。指令项停在 pending、`visibility_retry_attempts=1`，直到 deadline 过期。
同时 `_message_instruction_status` 把 pending 聚合成 `in_progress`，边界按词表冻成 `outcome_unknown`（step 6 处理分类）。
`authoritative_execution_attempts` 里 `uncertain` 已 20 行（completed 11 / in_progress 5 / partial_failed 4），
识别扫描器每轮把它们按 ERROR 打一遍。

## 任务

1. **只读评估**（先做，写进 `docs/plans/2026-09-07-management-reliability/step-3d-assessment.md` 并 send_message 给指挥会话）：
   - `instruction_execution_contract_mode` 三档各自门控哪些行为（grep 全部读取点，逐处写明 shadow 与 live 的差异）；
   - 生产该开关的当前值与历史（docs/archive 与状态文件里的记录）、最近一次评估它为何停在 shadow；
   - 若翻到 live：哪些路径会开始产生交易所写入、哪些既有测试覆盖；若不翻：能否让 `reconcile_due_entry_admissions`
     在 shadow 下也执行"只重试准入、不改写入语义"的子集；
   - 过去 30 天被 `adjacent_entry_context_pending` 推迟且最终过期的入场条数与所在群（只读统计）。
2. 等指挥会话裁定后再改代码：要么（a）翻开关（需用户单独批准，L3 语义变更）；要么（b）把恢复器的"到点重试准入"
   部分从 `live` 门控里剥出来，在 shadow 下也跑，写入语义仍受原门控约束。
3. 无论哪种，`adjacent_entry_context_pending` 到达 `execution_deadline_at` 仍未成交必须生成
   `runtime_incidents`（类型 `entry_admission_expired`，ALWAYS_NOTIFIED），不得静默。

## 禁止

- 评估阶段零写入、零部署。改动阶段不改 B 线锁定的文件（recovery_live_submit.py 等）以外的入场写入语义。
- 不用 `git add -A`。

## 完成条件

评估报告送达指挥会话并获裁定；改动部署后按 L2 观察；更新状态文件推进到下一步；`send_message` 给 `brain_session_id`。
