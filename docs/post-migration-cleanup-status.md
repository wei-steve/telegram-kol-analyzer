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
current_step: 2
current_step_file: docs/plans/2026-09-06-post-migration-cleanup/step-2-defaults-and-dead-code.md
step_status: planned              # planned | claimed | in_progress | completed | blocked
claimed_by: null
last_completed_step: 1
last_completed_commit: 3f88aafa
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

