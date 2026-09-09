# A-8：权威识别器 477 条"识别失败"的归因（先只读，再决定修什么）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-8-recognition-failures`，工作树 `.worktrees/mgmt-step-8`。

## 问题（A-7 只读核实）

`automation_reason = mimo_authoritative_not_safely_applied` 全库 477 条，触发条件是 `recognition.status == "识别失败"`，
与群模式无关。其中 auto_trade 群 108 条（陈哥 58、舒琴 18、大镖客 17、峰哥 15）。这些消息里可能有真实的入场或管理指令
被识别层判失败而静默跳过。A-7 任务 4 的"notify_only 特例"诊断已被推翻（notify_only 管理消息只命中 1 条）。

## 任务

1. 只读盘点 auto_trade 群的 108 条：raw id、群、posted_at、正文摘要（脱敏）、`message_recognitions.reason` 原文、
   MiMo 的 error_message / 返回状态、上下文解析结果、是否有更早/更晚的同消息成功识别。按根因分类：
   MiMo 调用错误或超时 / 返回格式不合约（v2 契约校验失败）/ 上下文解析失败 / 存储证据无效 / 其他。
   每类给条数、最近发生时间、是否仍在发生（最近 7 天）。
2. 对仍在发生的类别各取 3 条，用只读方式重放识别（不写库、不下单），看当前代码是否仍失败；
   失败的记录准确的失败点（模块、行、字段）。
3. 输出 `step-8-attribution.md` 并 `send_message` 给指挥会话，附"可修的大头"与建议的修法与风险等级；等裁定后再改代码。
4. 无论如何，识别失败必须可见：`mimo_authoritative_not_safely_applied` 在 auto_trade 群发生时生成
   `runtime_incidents`（类型 `authoritative_recognition_failed`，high，summary 含 raw_message_id、群、失败点），
   进 ALWAYS_NOTIFIED；notify_only 群只记不投递。

## 禁止

- 盘点阶段零写入、零部署。不改识别模型提示词（需要就记遗留）。不用 `git add -A`。

## 完成条件

归因报告送达并获裁定；告警部署后按 L1 观察；状态文件推进（若为 A 线最后一步则 `current_step: done`）；`send_message` 给 `brain_session_id`。
