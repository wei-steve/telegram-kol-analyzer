# Step 2 — 默认值翻转、死代码与迁移期注释清理（L1）

状态文件：`docs/post-migration-cleanup-status.md`。先领取。分支名：`cleanup/step-2-defaults-and-dead-code`。
前置：步骤 1 已 completed 并合并进 `codex/deepcoin-auto-trading-v1`；`docs/ARCHITECTURE.md` 已存在。

## 目标

让代码里的默认值与生产一致，删掉无引用的常量，把描述"旧行为"的注释改成描述"现状"。本步不删任何执行分支，分支删除在步骤 3。

## 任务 1：翻转设置默认值

文件 `src/telegram_kol_research/trading_settings.py`：

- `message_pipeline_mode` 默认 `"inline"` → `"queue"`
- `worker_command_mode` 默认 `"inline"` → `"queue"`
- `message_lock_mode` 保持 `"global"`（步骤 4 决定去留）。
- `Literal[...]` 取值集合本步不变。
- 解析函数：确认 DB 里持久化的旧值仍能正确读回（生产行里存的就是 `queue`，默认值只在字段缺失时生效）。补一个测试：缺失字段 → `queue`；显式 `inline` → `inline`。

CLI 的 `runtime_role` 默认值 `all` 不改（本地单进程开发需要），但在 `cli.py` 该 Option 的 help 里写明"生产为 web/ingest/worker 三进程，all 仅用于本地"。

跑全量测试前先预估影响：`grep -rn "TradingSettings(" tests | wc -l`。凡是测试隐式依赖默认 `inline` 的，改为显式传 `message_pipeline_mode="inline"` / `worker_command_mode="inline"`，并在改动处加注释 `# inline path: scheduled for removal in cleanup step 3`，方便步骤 3 一把删掉。不要为了让测试通过而改测试断言的语义。

## 任务 2：删除死引用

- `src/telegram_kol_research/telegram_live_listener.py` 的 `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE`：先 `grep -rn` 全仓确认零引用，再删除。
- 用 `ruff check --select F401,F841 src/telegram_kol_research` 找未使用导入/变量，只删 ruff 报告的、且 `grep` 确认无动态引用的项。

## 任务 3：迁移期注释改写

在 `src/telegram_kol_research/` 下搜索这些短语并逐处改写为描述现状，不改任何逻辑：

```
pre-Phase-2 / pre-phase / byte-for-byte / used to hold / used to / no longer / Phase 2 / Phase 3 / Phase 5 / Phase 6 / rollback path Phase
```

改写原则：说清"这段代码现在做什么、在哪个角色进程里跑"，不提迁移阶段编号。涉及的主要文件：`message_lock_provider.py`、`keyed_async_locks.py`、`telegram_live_listener.py`、`message_processing_worker.py`、`web_app.py`。
`legacy` 一词出现 636 次，分布在 45 个文件，本步不碰（它多数指业务上的历史订单/旧备份语义，不是迁移注释）。

## 禁止

- 不删任何 `if pipeline_mode == ...` / `worker_command_mode` 分支（步骤 3）。
- 不改 `message_lock_mode` 及锁相关逻辑（步骤 4）。
- 不改任何识别、策略、执行、交易所写入逻辑。
- 不 push，不部署，不用 `git add -A`。

## 验证（L1）

- 聚焦：`python -m pytest tests/test_trading_settings*.py tests/test_message_pipeline_mode_exclusivity.py tests/test_telegram_live_listener.py tests/test_web_app.py -q -x`
- 最终候选跑一次全量：`python -m pytest -q`，记录 "N passed, M skipped" 末行，必须 0 failed。
- `ruff check src/telegram_kol_research` 不引入新告警。
- `git diff --stat` 里不应出现除 `trading_settings.py`、`cli.py`、`telegram_live_listener.py`、注释所在文件、测试文件、`docs/ARCHITECTURE.md`（更新"默认值目前不同"那句）之外的文件；若有，说明理由。

## 完成条件

1. 三个提交：默认值翻转 + 测试；死引用删除；注释改写。
2. 更新状态文件到 `current_step: 3`、`current_step_file: .../step-3-delete-inline-shadow-paths.md`，其余字段按领取协议。
3. `send_message` 给 `brain_session_id`：分支、SHA、改了多少处测试的隐式默认、全量测试末行、ruff 结果。
