# A-6b：`uncertain` 必须带证据引用；reanalyze 撞护栏当预期状态（L1）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-6b-uncertain-evidence`，工作树 `.worktrees/mgmt-step-6b`。

## 问题（A-7 验收窗发现）

1. `authoritative_execution_attempts` 里 `status='uncertain'` 的行自 09-04 起 23 条，`evidence_refs_json` **全部为空**。
   `mark_authoritative_execution_uncertain`（`authoritative_execution_attempts.py`）冻结时只写 error_class / error_summary，
   不把执行边界跟踪到的 `tracker.writes`（deepcoin_write 引用：路径、幂等键、请求指纹、回执状态）写进 `evidence_refs_json`。
   于是"真的向交易所发过请求但无回执"与"什么都没发"从 attempt 行上看不出区别；监视器的 `uncertain_no_evidence`
   指标因此永远无法为 0。
2. `web_app.reanalyze` → `save_pending_authoritative_decision` 重试时撞到
   `authoritative execution is already in progress or outcome is uncertain` 护栏，把完整栈打进 err 级日志
   （A-7 窗内 12 行），而这是预期状态，不是故障。

## 任务

1. 冻结为 `uncertain` 时把执行边界的 `writes` 列表（每条：operation、idempotency_key、request_fingerprint、outcome、
   响应 sCode/sMsg 若有）序列化进 `evidence_refs_json`；writes 为空时写 `[]` 并在 `error_summary` 后追加
   `no_exchange_write_tracked`（区分"发过没回执"与"没发"）。A-6 已让 writes 为空的确定性拒绝不再落 uncertain，
   所以此后 uncertain 应当总带非空 writes；若出现空 writes 的 uncertain，记一条 `uncertain_without_write` 告警（ALWAYS_NOTIFIED）。
2. reanalyze 路径：护栏命中时不抛栈，记 info 级日志（raw_message_id、attempt id、当前状态）并返回"已在处理/结果未知"的
   明确结果给调用方；只有非预期异常才走 err。
3. 测试：uncertain 带 writes 引用；空 writes 的 uncertain 触发告警；护栏命中不产生 err 日志且返回结构化结果。
4. 只读盘点现有 23 条 uncertain：按 A-6 的三族分类已记录，本步不追溯改行。

## 禁止

- 不改执行边界的分类判据（A-6）。不追溯改历史行。不用 `git add -A`。

## 验证（L1）

- focused 与全量 0 failed；部署 tg-deploy；15 分钟或 5 条消息观察：新增 uncertain（若有）带非空 evidence_refs_json；
  err 级日志不再出现该护栏栈。

## 完成条件

更新状态文件到 `current_step: 9`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-9-bot-choose-candidate.md`；
`send_message` 给 `brain_session_id`。
