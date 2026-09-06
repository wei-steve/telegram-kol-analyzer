# Step 5 任务 1 — 一次性模块盘点

盘点对象：`src/telegram_kol_research/` 下文件名含
`repair / recovery / reconcil / remediation / cleanup / rescue / backfill / convergence /
terminalization / legacy / migration / alignment` 的模块，2026-09-06 实测 **47 个**（与步骤文件一致）。

## 判定方法（实际执行的口径）

**「在线路径是否引用」** — 用 AST 建全包 import 图（`ast.ImportFrom` / `ast.Import`，含函数体内的
延迟 import），从三个在线根做传递闭包：

- `web_app.py`（它自身 import 了 `RUNTIME_ROLE_SINGLETON_TASKS` 里全部 loop 函数，所以从它出发
  即覆盖了步骤文件要求的「lifespan 任务 + 各单例 loop」两类入口）；
- `message_processing_worker.py`；
- `strategy_management_worker.py`。

242 个模块中 **188 个** 落在这个在线闭包里。落在闭包里 = 在线，一律 `keep-online`，不再看其他列。

**「仅 CLI 可达」** — 反向 import 表里只出现 `cli`（或只出现同样仅 CLI 可达的模块）。

**「是否已完成」** — 在 `docs/*.md`、`docs/archive/**`、`docs/superpowers/**` 里搜模块名与其
CLI 命令名，要求找到**该修复已经执行完毕**的记录。本次实测的关键区分：

- 出现在 `docs/runbook.md` 里、带完整 dry-run→apply 操作步骤的，是**常备运维工具**，
  每次出现新的坏数据都会再跑一次 → 不是一次性，`keep-online`。
- 只出现在 `docs/archive/plans/**` 的实现计划里（`Create: xxx.py` / `Test:` / `RED→GREEN` 这类
  行）的，那是**建造记录**，不是执行记录 → 不构成「已完成」。
- `docs/per-chat-durable-lanes-status.md` 里 `*_production_apply_plan_status:
  not_built_not_authorized` 的，是**造好但从未在生产执行**的待办工具 → 明确不是「已完成」，必须保留。

### 两条超出步骤文件字面、需要记录的口径判断

1. **零引用模块**。步骤文件写的是「只被 `cli.py` 某个命令 import」。实测有 7 个模块**任何**
   生产模块都不 import（只有测试或 `scripts/` 里的一次性脚本引用）。零引用在「是否可能在线」
   这一维度上严格强于「仅 CLI 可达」，因此按满足该半条处理；它们的去留仍完全由「是否已完成」决定。
2. **`scripts/` 可达**。`legacy_backup_reconciliation` 与 `native_tpsl_migration` 只被
   `scripts/reconcile_legacy_backup_status.py` / `scripts/migrate_native_tpsl_protection.py`
   引用。`scripts/` 不是在线路径，但移动它们需要同时改脚本 import，超出步骤文件「更新 cli.py 的
   import」的授权范围；且两者都没有执行完毕的记录，所以本步一律 `unsure` 保留，这条口径未被用到。

## 结论汇总

| 结论 | 个数 |
|---|---:|
| `keep-online` | 39 |
| `move-one-off` | 1 |
| `unsure`（保留） | 7 |

`move-one-off` 只有 `historical_management_terminalization.py` 一个。理由见下表与「逐条理由」。

## 盘点表

| 模块 | 在线闭包 | import 它的模块 | 仅 CLI 可达 | 文档记录已完成 | 结论 |
|---|---|---|---|---|---|
| `backfill.py` | 否 | `cli` | 是 | 否——`sync` 是常备历史抓取命令，`docs/runbook.md` 有 7 处 | `keep-online` |
| `backup_stop_repair.py` | 否 | `cli` | 是 | 否——`repair-backup-stops` 在 `docs/runbook.md:188` 是常备流程 | `keep-online` |
| `batch150_management_terminalization.py` | 否 | （无） | 零引用 | **否**——`docs/per-chat-durable-lanes-status.md:400` `batch150_production_apply_plan_status: not_built_not_authorized`，生产从未 apply | `unsure` |
| `break_even_convergence_executor.py` | 是 | `break_even_convergence_worker` | — | — | `keep-online` |
| `break_even_convergence_planner.py` | 是 | `break_even_convergence_worker`, `strategy_management_reconciliation` | — | — | `keep-online` |
| `break_even_convergence_worker.py` | 是 | `web_app`（worker 角色单例任务） | — | — | `keep-online` |
| `context_analysis_backfill.py` | 否 | （无） | 零引用 | 否——`docs/ai-context-resolution-optimization-status.md:402` 明说它是 offline one-time 工具，但同一句给了它**未来**的行为要求（归档后必须显式打开已验证归档或 fail closed），说明仍预期可用 | `unsure` |
| `current_protection_backfill.py` | 否 | `cli` | 是 | 否——只有 `docs/archive/plans/2026-07-26-supervised-current-protection-backfill-implementation.md` 的建造计划 | `unsure` |
| `entry_admission_reconciler.py` | 是 | `authoritative_recognition`, `system_operator_bot` | — | — | `keep-online` |
| `entry_assembly_fingerprint_repair.py` | **是** | `cli`, `production_safety_monitor` | 否 | — | `keep-online`（步骤文件把它列进「很可能一次性」，实测被 `production_safety_monitor` import，属在线，**不能移动**） |
| `entry_protection_ledger_repair.py` | 是 | `cli`, `execution_bindings` | 否 | — | `keep-online` |
| `evidence_backfill.py` | 否 | `cli` | 是 | 否——`backfill-mimo-evidence` 在 `docs/runbook.md:1348/1358` 是常备流程 | `keep-online` |
| `frozen_exchange_empty_state_alignment.py` | 否 | （无） | 零引用 | 否——除 `docs/ARCHITECTURE.md` 的粗筛清单外，全仓文档零记录，既无计划也无执行 | `unsure` |
| `historical_attribution_cleanup.py` | 否 | `position_attribution_repair` | 是（经 `position_attribution_repair` 传递） | 否——`docs/superpowers/plans/2026-07-15-historical-position-attribution-cleanup.md` 的步骤全是未勾选的 `[ ]`，且明确 stop before apply | `keep-online`（其上游 `repair-position-attribution` 是 runbook 常备流程） |
| `historical_management_terminalization.py` | 否 | （无） | 零引用 | **是** | **`move-one-off`** |
| `historical_state_repair.py` | 否 | `cli` | 是 | 否——只有 3 份 archive 建造计划；`docs/runbook.md:1859` 仅出现其测试命令 | `unsure` |
| `instruction_execution_reconciliation.py` | 是 | `system_operator_bot` | — | — | `keep-online` |
| `legacy_backup_reconciliation.py` | 否 | （无；`scripts/reconcile_legacy_backup_status.py` 引用） | 否（scripts 可达） | 否——全仓文档零执行记录 | `unsure` |
| `legacy_conditional_cancel.py` | 否 | `cli` | 是 | 否——`docs/archive/plans/2026-07-27-cancel-legacy-deepcoin-conditionals.md` 是建造计划，未见「六单已取消」的执行记录 | `unsure` |
| `management_history_recovery.py` | 否 | `cli` | 是 | **否，且反证** ——`recover-management-history` 于 **2026-09-05T06:26:37Z** 在生产 worker 上实跑过 dry-run（`docs/2026-09-05-batch153-history-recovery-dry-run.md`），`docs/runbook.md:436/447` 是常备流程 | `keep-online` |
| `manual_pending_entry_reconciliation.py` | 否 | `cli` | 是 | 否——只有 archive 建造计划 | `unsure` |
| `native_tpsl_migration.py` | 否 | （无；`scripts/migrate_native_tpsl_protection.py` 引用） | 否（scripts 可达） | 否——4 份 archive 计划全是 `Modify:` / `Test:` 建造行 | `unsure` |
| `position_attribution_repair.py` | 否 | `cli` | 是 | 否——`repair-position-attribution` 在 `docs/runbook.md:1034` 是常备流程 | `keep-online` |
| `position_management_liveness_recovery.py` | 否 | `cli` | 是 | 否——`recover-position-management-liveness` 在 `docs/runbook.md:1557/1564` 是常备流程；**步骤文件已列为已知在线** | `keep-online`（见「与步骤文件的两处出入」） |
| `position_management_remediation.py` | 否 | `cli` | 是 | 否——只有 archive 建造计划 | `unsure` |
| `position_reconciliation_observations.py` | 是 | `execution_bindings` | — | — | `keep-online` |
| `protection_incident_convergence.py` | 否 | `cli` | 是 | 否——`audit-protection-incidents` 在 `docs/runbook.md:1184` 是常备只读审计 | `keep-online` |
| `reconcile.py` | 否 | （无；只有 `tests/test_reconcile.py`） | 零引用 | 否——它不是修复工具，是一个纯函数 helper（`build_reconcile_window`） | `unsure`（见「顺带纠正的两处事实」） |
| `recovery_decisions.py` | 是 | `auto_trade_execution`, `recovery_runner`, `web_app` | — | — | `keep-online` |
| `recovery_execution_queue.py` | 是 | `recovery_order_confirmation`, `web_app` | — | — | `keep-online` |
| `recovery_live_submit.py` | 是 | `auto_trade_execution`, `cli`, `entry_assembly_fingerprint_repair`, `entry_revision_executor`, `worker_command_executor` | — | — | `keep-online` |
| `recovery_live_submit_gate.py` | 是 | `recovery_live_submit`, `web_app` | — | — | `keep-online` |
| `recovery_order_confirmation.py` | 是 | `auto_trade_execution`, `recovery_live_submit`, `recovery_live_submit_gate`, `web_app` | — | — | `keep-online` |
| `recovery_order_confirmations.py` | 是 | `recovery_live_submit_gate`, `recovery_order_confirmation` | — | — | `keep-online` |
| `recovery_runner.py` | 是 | `cli`, `web_app` | — | — | `keep-online` |
| `recovery_scan.py` | 是 | `auto_trade_execution`, `binance_market_data`, `deepcoin_readonly`, `gate_market_data`, `recovery_decisions`, `recovery_runner` | — | — | `keep-online` |
| `remediation_snapshot.py` | 是 | `position_management_remediation`, `strategy_management_executor`, `strategy_management_planner` | — | — | `keep-online` |
| `repair_confirmation.py` | 是 | `backup_stop_repair`, `current_protection_backfill`, `entry_protection_ledger_repair`, `legacy_conditional_cancel`, `native_tpsl_migration`, `take_profit_protection_leg_repair` | — | — | `keep-online`（经在线的 `entry_protection_ledger_repair` 进入闭包；同时是全部修复工具的共享确认层） |
| `strategy_management_composite_reconciliation.py` | 是 | `strategy_management_worker` | — | — | `keep-online` |
| `strategy_management_reconciliation.py` | 是 | `execution_bindings`, `strategy_management_composite_reconciliation`, `strategy_management_worker` | — | — | `keep-online` |
| `take_profit_protection_leg_repair.py` | 否 | `cli` | 是 | 否——只有 `docs/archive/plans/2026-08-11-take-profit-protection-leg-convergence.md` 建造计划 | `unsure` |
| `terminal_entry_cleanup.py` | 是 | `break_even_convergence_executor`, `deepcoin_execution_actions`, `execution_bindings`, `source_message_deletion_worker` | — | — | `keep-online` |
| `tpsl_ledger_backfill.py` | 否 | `cli` | 是 | 否——`backfill-canonical-tpsl-ledger` 在 `docs/runbook.md:1460/1470` 是常备流程 | `keep-online` |
| `trigger_protection_rescue_worker.py` | 是 | `execution_bindings` | — | — | `keep-online` |
| `trigger_take_profit_convergence.py` | 是 | `execution_bindings`, `recovery_live_submit` | — | — | `keep-online` |
| `trigger_take_profit_convergence_executor.py` | 是 | `execution_bindings`, `position_management_liveness_recovery`, `strategy_management_worker` | — | — | `keep-online` |
| `worker_command_reconciliation.py` | 否 | `cli` | 是 | 否——全仓文档零记录；**步骤文件已列为已知在线** | `keep-online`（见「与步骤文件的两处出入」） |

## 唯一 `move-one-off` 的完整理由

`historical_management_terminalization.py`（1607 行，六个历史管理批次的受监督终结工具）：

1. **零生产引用**：`cli.py`、`web_app.py`、两个 worker 都不 import 它；全仓只有
   `tests/test_historical_management_terminalization.py` 引用。它自带 `if __name__ == "__main__"`，
   本来就是 `python -m` 单跑的工具，全仓文档里没有任何一处写它的 `-m` 调用行。
2. **工具本身已被文档判定不可再用**：`docs/archive/plans/2026-08-21-historical-management-terminalization-l3-v2.md`
   §11.3 —— 生产复核时精确前像只剩 `42/47` 匹配，「The rehearsed plan is now evidence only and
   **must not** be used for production apply. Its exact CAS gate correctly fails closed.」
3. **它要解决的问题已被所有者验收为完成**：同文件 §14「Runtime-canonical terminal state accepted」
   记录 batches `123,127,129,133,144,146` 全部 resolved、execution legs `496,497,503,511,530,531,540`
   全部 closed、bindings `282,283,287,292,307,313` 全部 closed 且 `pos_id=NULL`、lifecycles
   `816,819,834,859,910,921` 全部 exited，该步骤直接数据库写入为零。目标状态由运行时收敛达成并获所有者接受。

即：修复目标已完成、工具已被明确标为 evidence-only、代码上零引用。三条同时成立。

移动后的调用方式从 `python -m telegram_kol_research.historical_management_terminalization`
变为 `python -m telegram_kol_research.one_off.historical_management_terminalization`。

## 与步骤文件的两处出入（均按「宁可少动」处理，未移动）

1. **`position_management_liveness_recovery` 与 `worker_command_reconciliation` 静态上只被
   `cli.py` import**，不在在线闭包里，与步骤文件「已知在线」的表述不符。两者都**没有**执行完毕的
   文档记录，所以无论按哪种口径都是保留；`recover-position-management-liveness` 还是
   `docs/runbook.md` 里的常备流程。本步按步骤文件的显式指示记为 `keep-online`，不移动。
2. **`entry_assembly_fingerprint_repair` 被步骤文件列进「很可能是一次性的例子」，但它确实在线** ——
   `production_safety_monitor.py` import 它，而 `production_safety_monitor` 在 web_app 闭包内。
   已改判为 `keep-online`。

## 顺带纠正的两处事实（同步写进 `docs/ARCHITECTURE.md` 第 5 节）

1. **`reconcile.py` 不是 ingest 的单例任务**。`RUNTIME_ROLE_SINGLETON_TASKS["ingest"]` 里那个名叫
   `"reconcile"` 的任务是 `telegram_live_listener.run_periodic_reconcile`
   （`web_app.py:5214` 创建 `app.state.reconcile_task`）。模块 `reconcile.py` 本身只有一个纯函数
   `build_reconcile_window`，**生产代码零引用**，只有 `tests/test_reconcile.py` 还 import 它。
   它既不满足「仅 CLI 可达」也不是修复工具，本步按 `unsure` 保留并在此记录，供以后单独裁决。
2. `docs/ARCHITECTURE.md` 原第 5 节把 `entry_admission_reconciler` / `recovery_*` / `terminal_entry_cleanup`
   等在线模块混在「一次性候选」清单里。核实后的分类已替换该节内容。

## 留给以后

- 7 个 `unsure` 里，`batch150_management_terminalization` 是**待办**而不是残留：
  batch 150 的生产 apply 计划从未构建也未获授权，删或移都会丢掉在建工作。
- `frozen_exchange_empty_state_alignment`（958 行）在全仓文档里零记录，来历不明，建议单独问所有者。
- `reconcile.py` 是确定的孤儿（生产零引用），但不在本步「只移动一次性修复模块」的授权范围内。
