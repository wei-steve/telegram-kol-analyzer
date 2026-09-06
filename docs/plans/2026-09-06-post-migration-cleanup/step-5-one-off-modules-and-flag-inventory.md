# Step 5 — 一次性修复模块归档 + 灰度开关清单（L1）

状态文件：`docs/post-migration-cleanup-status.md`。先领取。分支名：`cleanup/step-5-one-off-and-flags`。
前置：步骤 4 已 completed 并合并。

## 目标

把只跑过一次的历史数据修复模块从在线代码的命名空间里挪开；把所有灰度三态开关整理成一张有生产实际值和建议的表，供下一轮决策。本步不收敛任何开关。

## 任务 1：一次性模块盘点

候选：`src/telegram_kol_research/` 下名含 `repair / recovery / reconcil / remediation / cleanup / rescue / backfill / convergence / terminalization / legacy / migration / alignment` 的模块（2026-09-06 计 47 个）。

对每个模块判定，写入 `docs/plans/2026-09-06-post-migration-cleanup/step-5-inventory.md`：

| 列 | 判定方法 |
|---|---|
| 在线路径是否引用 | 从 `web_app.py` 的 lifespan 任务、`RUNTIME_ROLE_SINGLETON_TASKS` 对应的 loop 函数、`message_processing_worker.py`、`strategy_management_worker.py` 出发做静态 import 追踪；被引用 = 在线 |
| 仅 CLI 可达 | 只被 `cli.py` 某个 command import |
| 是否已完成 | 在 `docs/*.md`、`docs/archive/**` 里搜模块名，找到"已执行/completed/已修复"记录 |
| 结论 | `keep-online` / `move-one-off` / `unsure` |

只有同时满足"仅 CLI 可达"且"文档记录已完成"的才是 `move-one-off`。`unsure` 一律保留。
已知在线的例子（不要误判）：`break_even_convergence_*`、`trigger_take_profit_convergence_*`、`strategy_management_reconciliation`、`worker_command_reconciliation`、`position_management_liveness_recovery`、`trigger_protection_rescue_worker`。
已知很可能是一次性的例子（仍需按方法核实）：`batch150_management_terminalization`、`historical_management_terminalization`、`historical_state_repair`、`historical_attribution_cleanup`、`native_tpsl_migration`、`frozen_exchange_empty_state_alignment`、`legacy_backup_reconciliation`、`entry_assembly_fingerprint_repair`。

单独提交盘点。

## 任务 2：移动

- 新建子包 `src/telegram_kol_research/one_off/`，带 `__init__.py` 和一段 docstring："历史数据修复，已执行完毕，保留仅为可追溯；不得被在线路径 import"。
- `git mv` 每个 `move-one-off` 模块及其测试（测试移到 `tests/one_off/`）；更新 `cli.py` 的 import。CLI 命令名不改。
- 加一个守护测试 `tests/test_one_off_isolation.py`：静态检查 `telegram_kol_research.one_off` 不被 `one_off` 之外的任何模块 import，`cli.py` 除外。

## 任务 3：灰度开关清单

对 `trading_settings.py` 里每个 `Literal[...]` 开关（含 `*_mode`、`*_enabled`）写入 `step-5-flag-inventory.md`：

| 开关 | 代码默认 | 生产当前值 | live 起始日期 | 依赖的 `effective_*` 规则 | 建议 |
|---|---|---|---|---|---|

生产当前值来源：`docs/*.md` 与 `docs/archive/**` 里最近一次记录；找不到的写 `unknown`，不要猜。建议列只允许三种：`collapse-to-live`（删 disabled/shadow 分支）、`keep`（仍在灰度或有回滚需求）、`ask-owner`。
本步不改任何开关的逻辑。

## 禁止

- 不删除任何模块，只移动。不改任何开关。不改交易语义。
- 不 push，不部署，不用 `git add -A`。

## 验证（L1）

- 全量 `python -m pytest -q` 0 failed。
- `python -m telegram_kol_research.cli --help` 能列出全部 52 个命令（数量以 `grep -c "\.command(" cli.py` 为准，不减少）。
- 守护测试通过。

## 完成条件

1. 提交：盘点；移动；开关清单。
2. 更新状态文件到 `current_step: 6`、`current_step_file: .../step-6-integrate-and-deploy.md`。
3. `send_message` 给 `brain_session_id`：移动了哪些模块、`unsure` 有哪些、开关表中 `collapse-to-live` 与 `ask-owner` 的个数、全量末行。
