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
current_step: 1
current_step_file: docs/plans/2026-09-06-post-migration-cleanup/step-1-workspace-and-docs.md
step_status: claimed          # planned | claimed | in_progress | completed | blocked
claimed_by: local_a288ae52-d04b-43c3-afa6-70eb62636341
last_completed_step: 0
last_completed_commit: null
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

