# Step 1 — 工作区与文档降噪（L0）

状态文件：`docs/post-migration-cleanup-status.md`。先按其中的领取协议领取本步，再开始。
本步不改任何 `src/` 运行代码。分支名：`cleanup/step-1-workspace-and-docs`。

## 目标

让 AI 和人打开仓库时看到的是"现状"，而不是四份重复的源码、四百份历史计划和一个已完成工作流的触发词。

## 任务 1：清理仓库内工作树 `.worktrees/`

只处理 `/Users/steven/Documents/telegram获取消息/.worktrees/*`。外部工作树（`~/.codex/worktrees/*`、
`/Users/steven/Documents/telegram获取消息-*`、`/private/tmp/*`）只列出到报告里，绝不动。

对每个 `.worktrees/<name>`，在主仓库执行判定：

```bash
git -C .worktrees/<name> status --porcelain          # 有任何输出（含 ??）视为脏
git rev-list --count origin/codex/deepcoin-auto-trading-v1..$(git -C .worktrees/<name> rev-parse HEAD)
```

- 干净 且 计数为 0 → `git worktree remove .worktrees/<name>`；其分支若 `git branch --merged origin/codex/deepcoin-auto-trading-v1` 列出，则 `git branch -d <branch>`（只用 `-d`，禁止 `-D`）。
- 其他情况一律保留，写进报告。2026-09-06 预检结果：`chen-management-consistency` 领先 13 个提交、`partial-close-protection` 领先 1 个、
  `context-hold-owner-alert` 有修改、`entry-candidate-direction-price-geometry` / `historical-attribution-cleanup` / `protection-order-side-semantics` 各有 1 个未跟踪文件。以执行时的实际判定为准。
- 完成后 `git worktree prune`，并把 `git worktree list` 全文写进报告。

## 任务 2：虚拟环境副本

`.gitignore` 已忽略 `.venv`、`.venv313`、`.venv313a`、`.venv313b`。先查明当前真正使用的是哪一个
（看 `pyproject.toml`、`uv.lock`、`scripts/bootstrap_mac_dev.sh`、`.claude/launch.json`、`README.md`，以及 `which python` 在项目目录下的解析）。
只有当能证明某个副本没有任何引用时才删除它；证明不了就保留并在报告里列出大小。不确定时保留。

## 任务 3：归档 `docs/plans/`

规则：一份 `docs/plans/**/*.md` 若没有被以下任何文件引用（按路径或文件名子串 grep），就用 `git mv` 移到 `docs/archive/plans/`（保留原相对目录结构）：

- `AGENTS.md`、`README.md`、`design-qa.md`
- `docs/*.md`（顶层状态与说明文件）
- `src/**`、`tests/**`、`scripts/**`（已知 `tests/test_position_authority_boundary_coverage.py` 引用了 docs/plans，必须保留它引用的文件）
- `docs/plans/2026-09-06-post-migration-cleanup/**`（本方案自身，保留）

写一个临时脚本（放在 scratchpad，不入库）算出被引用集合，先打印"将移动 N 份、保留 M 份"的清单，再执行。
主检出目录里 2026-09-05 的未跟踪 docs/plans 文件是 codex 的进行中工作；你的工作树里本来就没有它们，不要去主目录碰。

## 任务 4：修剪 AGENTS.md

- 删除 "Runtime Serialization Remediation" 整节：其状态文件 `current_phase: done`。
- "Runtime Incident AI Agent" 一节：读 `docs/runtime-incident-agent-status.md`，只有当它明确记录全部阶段已完成时才删除；否则保留原文。
- 在文件开头 "Project Workflow" 之前加一小节 "Current Architecture"，只写一句：先读 `docs/ARCHITECTURE.md`。
- 其余内容一字不改。

## 任务 5：新增 `docs/ARCHITECTURE.md`

一页纸，只写现状，不写历史。必须包含：

1. 进程拓扑：`deploy/systemd/telegram-kol-{web,ingest,worker}.service`，环境变量 `TELEGRAM_KOL_RUNTIME_ROLE`。
2. 每个角色启动的后台任务：逐字抄 `src/telegram_kol_research/web_app.py` 里的 `RUNTIME_ROLE_SINGLETON_TASKS` 表，并说明 `all` 是本地开发用的单进程模式。
3. 生产运行模式表：`message_pipeline_mode=queue`、`worker_command_mode=queue`、`message_lock_mode=global`，并注明代码默认值目前与之不同（步骤 2 会翻转）。
4. 一条消息在 queue 模式下的路径：ingest 的 Telethon 回调只落库 + 入 `message_processing_jobs`；worker 的 `message_processing_worker` 消费并做识别、上下文解析、执行；`worker_command_jobs` 承载 web 发起的四条权威路由。请从 `telegram_live_listener.py`、`message_processing_worker.py`、`web_app.py` 核对后再写，不要照抄本段。
5. 模块分类：一份"在线主路径模块"清单和一份"一次性修复/历史迁移模块"清单（名含 repair/recovery/reconcile/terminalization/legacy/migration/backfill/alignment 的先粗分，标注"待步骤 5 核实"）。
6. 一段"AI 协作提示"：改动前先看本文件的模式表；`inline`/`shadow` 分支是待删除的历史路径，不要在里面加功能。

## 禁止

- 不改 `src/`、`tests/`、`scripts/` 下任何代码。
- 不 push，不部署。不用 `git add -A`。
- 不动主检出目录。

## 验证（L0）

- `git status` 只包含本步预期改动；`git diff --cached --name-only` 逐一核对。
- `python -m pytest tests/test_position_authority_boundary_coverage.py -q` 通过（它读 docs/plans）。
- `python -m pytest --collect-only -q | tail -1` 收集数不减少。

## 完成条件

1. 分步提交：工作树清理不产生提交；文档归档一个提交；AGENTS.md + ARCHITECTURE.md 一个提交。
2. 更新状态文件：`step_status: completed`、`last_completed_step: 1`、`last_completed_commit: <SHA>`、
   `current_step: 2`、`current_step_file: docs/plans/2026-09-06-post-migration-cleanup/step-2-defaults-and-dead-code.md`、`step_status` 改回 `planned`、`claimed_by: null`；证据区追加一条。单独提交状态文件。
3. `send_message` 给状态文件里的 `brain_session_id`：分支名、最终 SHA、移动/保留的工作树与文档数量、保留原因、验证输出末行。
