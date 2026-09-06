# Post-Migration Cleanup Status

单进程 → 三角色进程 + 持久化队列 迁移完成后的代码与仓库清理。本文件是跨会话唯一的进度真相；
新会话只读本文件，再打开 `current_step_file` 指向的那一份步骤文件，不要读其他步骤文件。

```yaml
project: post-migration-cleanup
plan_index: docs/plans/2026-09-06-post-migration-cleanup/README.md
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，执行会话完成后必须 send_message 到这里
brain_session_title: 自动项目多线程迁移后的代码清理
integration_branch: codex/deepcoin-auto-trading-v1               # 本地部署分支；每步完成后由指挥会话本地合并，不 push
production_modes: "runtime roles web/ingest/worker (systemd x3); message_pipeline_mode=queue; worker_command_mode=queue; message_lock_mode=global (per_chat 从未启用)"
current_step: 3
current_step_file: docs/plans/2026-09-06-post-migration-cleanup/step-3-delete-inline-shadow-paths.md
step_status: in_progress              # planned | claimed | in_progress | completed | blocked
claimed_by: local_54ee9d81-eec6-4363-989a-d5de14b3c034
last_completed_step: 2
last_completed_commit: d0292216
```

## 步骤总览

| 步 | 名称 | 风险等级 | 性质 |
|---|---|---|---|
| 1 | 工作区与文档降噪 | L0 | 不改运行代码 |
| 2 | 默认值翻转与死代码/迁移期注释清理 | L1 | 改设置默认值与注释 |
| 3 | 删除 inline / shadow 消息与命令路径 | L2 | 删代码路径，语义不变 |
| 4 | 锁层收敛与补偿循环去重 | L2 | 需用户先确认方案 |
| 5 | 一次性修复模块归档 + 灰度开关清单 | L1 | 移动模块、产出清单 |
| 6 | 集成部署与 L2 观察 | L2 | 由用户/codex 走既有门控部署流程 |

## 执行会话的领取协议

1. 在新建会话中先读 `AGENTS.md`，再读本文件，再只读 `current_step_file`。
2. 确认 `step_status` 为 `planned`。若为 `claimed` / `in_progress`，停止并告知用户。
3. 把 `step_status` 改为 `claimed`，`claimed_by` 填本会话 ID（用 `get_session self` 取），单独提交这一个文件。
4. 开始改代码前把 `step_status` 改为 `in_progress`。
5. 完成后按步骤文件的"完成条件"更新本文件（`completed`、`last_completed_step`、`last_completed_commit`、在下方证据区追加记录），
   把 `current_step` 推进到下一步并填好 `current_step_file`，`step_status` 回到 `planned`，`claimed_by` 置空。
6. 用 `mcp__ccd_session_mgmt__send_message` 把摘要发给 `brain_session_id`。
7. 全程遵守 AGENTS.md：不用 `git add -A`，只暂存明确路径；不 push、不部署、不发多余 Telegram 通知。

## 硬性禁止（所有步骤）

- 不改任何交易语义：识别、策略、仓位归属、执行、交易所写入的"决定什么"不能变，只能变"在哪里跑、怎么组织"。
- 不动主检出目录里的未跟踪文件（那是 codex 的进行中工作）。
- 不动 `~/.codex/worktrees/*`、`/Users/steven/Documents/telegram获取消息-*`、`/private/tmp/*` 这些外部工作树，只允许列出并报告。
- 不 push，不部署，不改生产设置，不重启服务。

## 证据记录

执行会话在此追加，格式：`- step-N (日期, 会话ID): 提交 SHA；做了什么；验证结果；遗留问题`。

- step-1 (2026-09-06, local_a288ae52-d04b-43c3-afa6-70eb62636341): 分支 `cleanup/step-1-workspace-and-docs`，提交 4f9ca4c5（归档 385 份 docs/plans）+ 3f88aafa（AGENTS.md 修剪 + 新增 docs/ARCHITECTURE.md）。工作树：仓库内 `.worktrees/` 删除 8 个（干净且相对 origin 无领先提交），`git branch -d` 删除 7 个已合并分支，保留 6 个（chen-management-consistency 领先 13 提交；partial-close-protection 领先 1；context-hold-owner-alert 有 9 项改动；entry-candidate-direction-price-geometry / historical-attribution-cleanup / protection-order-side-semantics 各 1 项未跟踪文件）；`semantic-ai-disagreement-review` 不是本仓库工作树而是独立 git 克隆，未处理。外部工作树（~/.codex/worktrees/*、telegram获取消息-*、/private/tmp/*）按规定只列出未动。虚拟环境：`.venv`（77M，Python 3.12.12，唯一装有 console script 的可用环境）与 `.venv313b`（89M，被 README.md 引用）保留；`.venv313`（26M）与 `.venv313a`（12M）除 .gitignore 外无任何引用且 bin/python 已失效，但它们是主检出目录的未跟踪文件，按硬性禁止条款未删，留待用户决定。文档：docs/plans 441 份 → 保留 56 份、归档 385 份到 docs/archive/plans/（保结构）。AGENTS.md 删除 Runtime Serialization Remediation 整节（其状态文件 current_phase: done）；Runtime Incident AI Agent 一节保留（状态文件仍有 next_phase_after_8r_6a: 8R.6B 与 waiting_for_natural_update）。验证：`pytest tests/test_position_authority_boundary_coverage.py -q` → `7 passed in 0.25s`；`pytest --collect-only -q` → 7462（基线 02e68df7 同为 7462，未减少）。遗留：(1) 删除 Runtime Serialization Remediation 一节同时移走了 AGENTS.md 里“Never run `git add -A` in this repository”这条通用禁令，建议后续步骤把它并入 Project Workflow；(2) `.venv313` / `.venv313a` 待用户裁决；(3) `.worktrees/semantic-ai-disagreement-review` 独立克隆（44M）待用户裁决。
- step-2 (2026-09-06, local_97403b85-06f5-4170-8219-9b7922b8c678): 分支 `cleanup/step-2-defaults-and-dead-code`，三个提交 a95f3415（默认值翻转 + 测试）、f3a9b52d（死引用删除）、d0292216（迁移期注释改写）。任务 1：`trading_settings.py` 的 `message_pipeline_mode` 与 `worker_command_mode` 默认值 `inline` → `queue`，`Literal` 取值集合与 `message_lock_mode` 未动；解析走 `raw.get(key, defaults.<field>)`，DB 里已持久化的值照常读回。测试隐式依赖默认 inline 的共 **32 处**改为显式传参并加注释 `# inline path: scheduled for removal in cleanup step 3`（gap_recovery_loop 9、reconcile_live_history 10、telegram_live_listener 10、reconcile 1、live_listener_chat_isolation 1、message_pipeline_mode_exclusivity 1（inline_app）、web_app 2、message_processing_shadow_enqueue 1；断言语义一律未改，两处仅改测试函数名里的 `inline` → `queue` 以匹配新默认）。新增两个默认值测试：字段缺失 → queue（DB 与空 payload 两条路径）、显式 inline → inline。`cli.py` 的 `--runtime-role` 默认值 `all` 未改，只加了 help 说明生产是 web/ingest/worker 三进程。任务 2：删除 `telegram_live_listener.AUTHORITATIVE_GAP_RECOVERY_MAX_AGE`（全仓 grep 零代码引用，仅历史文档提及）；按 ruff F401/F841 删除 20 个未用导入与 3 个死局部变量（两处未用的 `except ... as exc` 别名、`web_app` 里一个结果被丢弃的只读查询 `backup_by_leg_id` 及随之失效的 `PositionBackupStopOrder` 导入）。**刻意保留** 8 项：`cli.py:5857` 的 `import uvicorn` 是带用户提示与退出码的依赖探测；`web_app` 的 `_schedule_authoritative_notification` 被 `tests/test_web_app.py:6281` 按字符串路径 monkeypatch；`entry_revision_exchange_authority` 的 `is_canonical_idle_entry_revision_exchange_authority` 是给 `manual_pending_entry_reconciliation` 的跨行重导出（一度误删，已用 AST 扫描全仓 import 块与属性访问确认修正）；其余 6 个 F841（`entry_protection_ledger_repair.py:1263 returned_order_id_set`、`message_recognition.py:1410 management_note` 与 `:3030 entry_applied`、`raw_ingest.py:118 inserted_current_message`、`strategy_management_batches.py:555 parent_snapshot`、`trigger_backup_stop.py:60 close_size`）位于识别/策略/交易所写入代码，是潜在缺陷信号而非迁移残留，按硬性禁止条款未动——其中 `entry_protection_ledger_repair.py` 那处与 `recovery_live_submit.py:2461` 是同一段代码的两份拷贝，后者在 2486 行实际使用了该变量，前者丢失了这次使用，建议单独排查。任务 3：改写 **7 处**迁移期注释（`message_lock_provider.py` 模块 docstring + `__call__` docstring 共 5 句、`keyed_async_locks.py` 模块 docstring、`telegram_live_listener.py` 两处 docstring、`lifecycle_monitor.py:664` 行内注释），去掉 "used to hold / byte-for-byte / pre-Phase-2 / rollback path Phase 2 / Phase 3 Task 1 compensator" 等表述，改为描述现状与所在角色进程；只动注释与 docstring，逻辑零改动。`message_processing_worker.py` 与 `web_app.py` 经全文扫描无迁移期注释。`system_operator_bot.py`(839/2691)、`runtime_agent_*.py`、`runtime_incident_handoff.py`、`cli.py:3552` 的 Phase 2/3/5/6 属于仍在进行的 Runtime Incident AI Agent 计划，是活文档而非迁移残留，未动；`legacy` 一词按要求未碰。`docs/ARCHITECTURE.md` 第 3 节模式表与"默认值不一致"那段已改为一致，第 6 节同样过时的一句一并改写。验证：全量 `python -m pytest -q` → `7460 passed, 4 skipped, 32 warnings in 442.24s (0:07:22)`，0 failed；收集数 7464（基线 7462，+2 为新增默认值测试）。`ruff check src/telegram_kol_research`：1546 → 1521 条，逐条 diff 比对**零新增告警**。遗留：(1) 上述 6 个 F841 待步骤 3/5 或单独任务处理，`entry_protection_ledger_repair` 与 `recovery_live_submit` 的拷贝分叉值得优先看；(2) 工作树里 `.venv` 是指向主检出目录 `.venv` 的软链接（`test_minimal_server_updater` / `test_server_update_scripts` 共 15 个测试硬编码 `<repo-root>/.venv/bin/python`，无此链接则在任何工作树里都失败，与本步改动无关），已写进主检出目录的 `.git/info/exclude`，合并后可删链接；(3) 本步 `step_status` 未经过 `in_progress` 中间态——该次编辑未提交，被修正提交 2 时的 `git reset --hard` 丢弃，其余领取协议字段正常。
