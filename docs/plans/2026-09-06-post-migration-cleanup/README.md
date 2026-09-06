# Post-Migration Cleanup — 方案索引

状态文件：`docs/post-migration-cleanup-status.md`（唯一进度真相）。

## 背景

2026-08-18 至 08-22 的 runtime serialization remediation 把系统从"单进程 + 一把全局锁 + 回调内联处理"
改为"web / ingest / worker 三个 systemd 进程 + `message_processing_jobs` 持久化队列 + `worker_command_jobs`"。
生产已经稳定在 `message_pipeline_mode=queue`、`worker_command_mode=queue`、`message_lock_mode=global`。

但代码仍停留在迁移过程中的形态，导致 AI 协作时反复误判主路径：

| 问题 | 证据 |
|---|---|
| 代码默认值与生产相反 | `TradingSettings` 默认 `message_pipeline_mode="inline"`、`worker_command_mode="inline"`；生产是 `queue` |
| inline / shadow / queue 三路并存 | `telegram_live_listener.py` 第 143、494-526、1208-1311、1618 行等处三向分支 |
| 锁层为未启用的 `per_chat` 而存在 | `message_lock_provider.py`、`keyed_async_locks.py`、`lock_all()`；状态文件记录 per_chat 从未启用；且 web 进程里的 `lock_all()` 已无法锁住 ingest 进程 |
| 新旧补偿循环重复 | ingest 跑 300s 的 `run_periodic_reconcile`，worker 跑 20s 的 gap recovery loop |
| 死常量与迁移期注释 | `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE` 无引用；大量 "pre-Phase-2 / byte-for-byte" docstring |
| 灰度三态开关堆积 | `trading_settings.py` 至少 12 个 `disabled/shadow/live` 或 `v1/v2` 开关 |
| 一次性修复模块留在 src | 240 个模块中 47 个名含 repair/recovery/reconcile/terminalization/legacy/migration |
| 仓库噪音 | `.worktrees` 652MB 含 14 个工作树各带 `.venv`；`docs/plans` 416 个文件、全部 markdown 6.5MB；AGENTS.md 仍保留已完成工作流的触发词 |

## 步骤文件

每份步骤文件自包含，执行会话只读自己那一份：

1. `step-1-workspace-and-docs.md`
2. `step-2-defaults-and-dead-code.md`
3. `step-3-delete-inline-shadow-paths.md`
4. `step-4-lock-layer-and-reconcile.md`
5. `step-5-one-off-modules-and-flag-inventory.md`
6. `step-6-integrate-and-deploy.md`

## 组织方式

- 指挥会话（brain）：制定方案、审核每步结果、把步骤分支本地合并进 `codex/deepcoin-auto-trading-v1`、生成下一步会话入口。
- 执行会话：用户从指挥会话生成的入口新建，模型 Opus 5、思考程度 high，每个会话只做一步，在独立工作树、独立分支 `cleanup/step-N-<slug>` 上工作。
- 步骤 1→5 严格串行。步骤 4 开始前需要用户确认锁层方案（见 step-4 文件"待确认决策"）。
- 部署统一放在步骤 6，前面各步只在本地提交。
