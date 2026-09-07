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
current_step: 1b
current_step_file: docs/plans/2026-09-07-management-reliability/step-1b-convergence-retry-whitelist.md
step_status: planned          # planned | claimed | in_progress | completed | blocked
claimed_by: null
last_completed_step: 1
last_completed_commit: b1c12213ad2740e08ac1ecdba39e843e55ce239f
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
| 1b | 被误判冻结的止盈收敛允许重试（为当前持仓重建分档止盈） | L2 | 是 |
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

- step-1 (2026-09-07, local_912f7e68-19a2-43f3-bd62-62ff8d223a2a): 提交 `b1c12213ad2740e08ac1ecdba39e843e55ce239f`（分支 `mgmt/step-1-tp-veto-scope`，已 fast-forward 进 `codex/deepcoin-auto-trading-v1`）。
  **做了什么**：新增 `native_tpsl.is_protection_order_row(row)`，判据只看订单类型字段（`triggerOrderType` 别名集合唯一且为 `TPSL`），不用价格或数量；类型缺失或自相矛盾时仍按保护单处理，保留 fail-closed。判据来源见该函数 docstring（引用 `docs/2026-09-05-deepcoin-order-vs-trigger-order.md` 与本项目诊断文档 4.2 节的实测行）。`trigger_take_profit_convergence_executor.py` 的两个否决点（计划期 506、写前复核 746）收窄为只作用于保护单行；真保护单之间仍不一致时否决保留，并把冲突行 ordId 与判定点读到的全部字段原文写进 `trigger_take_profit_convergences.error_json`（该表无 `reason_detail` / `evidence_json` 列，`error_json` 是等价字段，**未加列**）。同一判据应用到 `trigger_backup_stop_executor.py` 的 644/702/734/746 四处；647 那处原本已有等价的类型分支，改写后逐 case 等价。未改其他收敛判据、交易所写入参数、已有账本行，未引入模式开关。
  **验证结果**：复现测试证明修复前两个判定点分别返回 `convergence_pending_alias_conflict` 与 `convergence_pending_alias_conflict_before_write`，修复后不再否决且三档止盈 `sz=5/3/2` 正常提交；判据正反用例覆盖入场条件单 / TPSL / 市价 reduceOnly / 缺字段行 / 类型自相矛盾行。全量 `pytest -q` **7617 passed, 4 skipped, 0 failed**。部署前 `active_write_count=0`、无 planned/executing/reconciling 批次；回滚 SHA `294bd54b2881b6a44e2d749d9ec479d453985d36`。观察窗（服务器端只读监视器，证据在 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-1/`）：`2026-09-07T10:09:35Z`–`10:38:36Z`，30 个采样，5 条真实消息 / 2 群，三个 unit 全程 active，**anomaly_count=0**。窗内 `trigger_take_profit_convergences` 零新增记录，`convergence_pending_alias_conflict` / `_before_write` 零出现（worker 日志自部署起同样 0 次）；`position_mutation_intents` 全程停在 642，`position_take_profit_orders` 停在 197，无任何账本行数下降，即**零非预期交易所写入**。窗末直读交易所条件单（`exchange-pending-at-window-end.txt`）：BTC 3 行、ETH 3 行，全部 `triggerOrderType=TPSL`、`side=sell` / `posSide=long`，`is_protection_order_row` 全为 True，无一会被新判据否决。
  **止盈重建逐笔确认：无样本。** 窗内没有新建任何分档止盈，原因有两条且都不是本步引入：（a）触发该缺陷的入场条件单 `1001125163581473`（`triggerOrderType=Conditional`）已在观察前成交（腿 587 现持 `pos_id=1001125167675481`），当前 pending 快照里已无 Conditional 行，本次窗口不具备复现旧缺陷的输入；（b）见下面的遗留问题。判据修正本身已由复现测试证明，待下一次持仓且出现「未成交入场条件单与 TPSL 行并存」时复核分档止盈的方向、数量、价格与账本一致性。
  **遗留问题（新发现，本步未修，留给后续步骤）**：`execution_bindings.py:1330-1335` 只把 `reason_code` 属于 `convergence_verified_stop_missing` / `convergence_unowned_take_profit_present` / `convergence_exchange_preflight_unavailable` 三者之一的 `conflicted` 收敛拉回重试，`convergence_pending_alias_conflict` 不在白名单内。因此被旧缺陷冻死的收敛 **227 与 230 是终态，本次修复不会追溯解冻它们**（230 的 `updated_at` 至今仍是 `2026-09-07 01:00:12`，腿 586 为 `active`/`verified` 但从未被重新 ready）；231 的 `convergence_exact_leg_not_verified` 同样不在白名单。这属于「被冻结项的恢复」范畴（step 3 / step 5 主题），本步按「不得顺手修另一步的问题」未动。

