# A-9：操作员 bot 增加"选择候选"命令，闭环目标歧义确认（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-9-bot-choose-candidate`，工作树 `.worktrees/mgmt-step-9`。

## 问题

A-7 让目标不唯一或无活跃仓位的管理指令进入 `awaiting_user_confirmation` 并发 `management_target_needs_confirmation`
通知（列出候选），但操作员 bot 没有"选择候选"的命令，用户只能人工另行处理，确认态无法闭环。

## 任务

1. 通知文案给每个候选一个短编号（1、2、3…）与一个 `raw_message_id`，并写明回复格式：
   `/choose <raw_message_id> <编号>` 与 `/dismiss <raw_message_id>`。
2. `telegram_bot_commands` 增加这两条命令：只接受来自 SYSTEM bot 配置的 chat_id；`/choose` 把指令项从
   `awaiting_user_confirmation` 改回 `pending` 并把 `target_lifecycle_id` 固定为所选候选（写进 `result_json` 的
   `operator_choice`），随后由 worker 正常执行；`/dismiss` 把指令项标 `failed / operator_dismissed`。
   两条命令都写审计行（raw_message_id、候选列表、选择、操作者 chat_id、时间）。
3. 超时：`awaiting_user_confirmation` 超过 `management_confirmation_timeout_minutes`（新字段，默认 120）自动
   `failed / confirmation_timeout` 并再发一条通知；到期前 30 分钟提醒一次。
4. 执行时再验证一次：所选候选的 binding 仍 active 且 pos_id 仍在交易所持仓快照（复用 A-7 的验证），不在则拒绝并通知。
5. 测试：choose 正常路径、dismiss、超时、非授权 chat 拒绝、候选失效拒绝、重复 choose 幂等。

## 禁止

- 不自动选择候选。命令只改指令项与审计，不直接写交易所。不用 `git add -A`。

## 验证（L2）

- focused 与全量 0 failed；部署 tg-deploy；观察按 L2；若窗内出现真实歧义指令，人工用 /choose 走一遍并逐笔确认执行与审计。

## 完成条件

更新状态文件：`current_step: done`、`step_status: completed`；`send_message` 给 `brain_session_id`。A 线到此收口，
剩余项（A-5d 剩余梯子策略、开关收敛清单）转入后续独立排期。
