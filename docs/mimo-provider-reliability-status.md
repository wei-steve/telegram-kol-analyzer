# MiMo Provider Reliability Status（识别模型供应商故障的检测、告警与恢复）

2026-09-12 MiMo 余额耗尽、识别静默停摆 14 小时 42 分钟（A 线 step-18 只读排查）的修复项目。
本文件是跨会话唯一的进度真相。事实基础见 `docs/management-reliability-status.md` 的 step-18 条目，
本文件"事实基础复核"一节记录本项目开工时对它的独立复核结果。

```yaml
project: mimo-provider-reliability
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，每步完成后 send_message 到这里
integration_branch: codex/deepcoin-auto-trading-v1
deploy: tg-deploy <sha>（AGENTS.md 部署一节，四步）
worktree_pattern: .worktrees/mimo-step-N
current_step: 1
step_status: in_progress        # planned | claimed | in_progress | completed | blocked
claimed_by: (本执行会话，见证据区首条)
production_head_at_start: 0ed2d488aa5843187fa2e1e11ef9d986af7c648b
base_commit: 8046208c277efd06d045dc2e73d6caa729e8f485   # origin 共享分支尖端，相对生产只多文档
```

## 步骤总览

| 步 | 名称 | 风险 | 状态 |
|---|---|---|---|
| 1 | 供应商错误分类：402/401/403/429/5xx/超时/网络 → `mimo_provider_unavailable`（首条即发、每 30 分钟一条、恢复通知），与请求内容错误分开 | L1（新增告警，不改权威与交易） | in_progress |
| 2 | `ALERTED_REASONS` 遍历式守卫：凡"权威判定未产生"的 reason 必在告警集合；`mimo_authoritative_failed` 进 auto_trade 群告警 | L1 | planned |
| 3 | 补救窗口与供应商状态解耦；恢复后按序重放 auto_trade 群消息，管理类先核目标仓位；逐条记录并通知 | L2（恢复路径） | planned（入场重放策略待裁定） |
| 4 | 主动巡检：连续同码失败计数告警；每日 `max_tokens=1` 探测（不进业务表）；余额接口（如有） | L1 | planned |
| 5 | 用 step-18 的 494 次失败离线重放，验证 1–3 的判定与限流 | L0（离线） | planned |

## 事实基础复核（2026-09-12，只读，生产库 + journal）

- **494 的出处是 `ai_prompt_invocations`**：窗口 02:50–18:10Z，`feature='message_recognition' AND status='failed'`
  逐小时相加 71+72+35+10+10+15+10+10+9+79+74+40+21+28+10 = **494**，全部错误文本以
  `MiMo failed after 2 attempts: attempt 1: Client error '402 Payment Required'` 开头。
  **每行是一次带内部重试（`MIMO_AUTHORITATIVE_MAX_ATTEMPTS=2`）的调用**，所以实际 HTTP 请求约为两倍。
- `mimo_recognition_runs` 同窗口失败 **496** 个、`mimo_recognition_attempts` `http_error` **496** 行。
  **差的 2 条已查明**：raw 16346、16413 的 `final_error_message` 是 `message has no readable text or image`
  ——空输入提前返回，不写调用记录，**与供应商无关**。所以"402 失败 = 494 次调用"成立，496 不是另一个口径的 402 数。
- 受影响消息：失败 run 覆盖 **116 个不同 raw_message_id**（与 step-18 的 116 条一致）。
- **journal 里没有任何 MiMo 失败的日志行**：三个 unit 同窗口 `grep -ci mimo` / `xiaomimimo` / `payment` 全为 0
  （worker 22253 行）。**这个 0 是"失败不写日志"，不是"没有失败"**——同窗口数据库有 494 行。
  推论：本项目的任何观察判据**不得从 journal 数 MiMo 失败**，只能从上面两张表数。
- 当前 `mimo_recognition_attempts.error_code` 对 v1 失败一律写 `v1_authoritative_failed`（src 与 tests 无消费者），
  **402 与"请求内容错误"在落库层面同形**——与 step-18 第 (6) 条一致。

## 设计要点（第 1 步）

- **分类只看结构化信号，不看文本**：在 `_call_mimo_direct_model` 捕获 `httpx.HTTPStatusError`（取 `status_code`）、
  `httpx.TimeoutException`、`httpx.TransportError` 时，把分类写进该次尝试的遥测；v1 尝试行的 `error_code`
  由 `v1_authoritative_failed` 细分为 `mimo_provider_unavailable.<kind>`（仅当该次调用的**全部**请求都是供应商不可用类）。
- **状态不新增表**：供应商"事故期"由 `mimo_recognition_attempts`（append-only、已有 `(status, created_at)` 索引）推导：
  从最新一行往回数，连续的 `mimo_provider_unavailable.*` 行即当前事故期。
- **每 30 分钟一条靠指纹**：事故摘要只放稳定字段（原因、事故期起点、30 分钟桶序号），同桶内重复出现只增
  `repeat_count`（`record_runtime_incident` 按指纹合并），跨桶产生新行、触发一次投递。
- **没有消息时也要按时说话**：检查挂在 worker 的 `authoritative_gap_recovery_loop` 每轮（20s）上，不依赖新消息到达。
- **恢复通知**：事故期之后出现一次"供应商回答了"的尝试（完成、或非供应商类失败），且该事故期发过告警 →
  `mimo_provider_recovered`，以事故期起点为键合并，只发一次。

## 待裁定

- **入场类消息的重放策略（第 3 步）**：已发指挥会话，见证据区。

## 证据记录

格式：`- step-N (日期, 会话): 提交 SHA；做了什么；验证结果；遗留问题`。
