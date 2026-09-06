# 阶段 3：WebSocket 事件只唤醒既有 REST reconciliation

风险等级：**L2**（改变既有权威循环的调度节奏，可影响执行路径）。
不改任何判据，不改任何交易所写入语义。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

## 目标

让 `worker` 的 `deepcoin_reconcile` 循环从"固定 30 秒轮询"变成
"固定 30 秒轮询 + 相关事件到达时立刻跑一轮"。

唯一改变的是**什么时候跑**。跑起来之后做的事、用的判据、写的账本、
下的结论一个字都不变。这一阶段的价值是把 `outcome_unknown` 与保护裸奔的时间窗
从"最多 30 秒"收窄到"事件到达后立刻"，实验中 WS 比 REST 轮询早 4–5 秒发现成交。

## 前置

- 阶段 2 已 `completed`，连接状态机与去重/乱序在生产里稳定运行。
- 手上有阶段 2 的重复率与乱序数基线。
- 读 `docs/ARCHITECTURE.md` 第 4.5 节（两个补偿循环的分工）。
  注意：本阶段只碰 `worker` 的 `deepcoin_reconcile`，
  **不碰** `ingest` 的 `reconcile`（那是 Telegram 历史补齐，与交易所无关），
  也**不碰** `authoritative_gap_recovery_loop`。

## 任务

### 1. 唤醒信号

在 `deepcoin_private_ws` 与 `deepcoin_reconcile` 之间加一个进程内唤醒原语
（`asyncio.Event` 即可——两者同在 worker 进程，这是进程内唤醒的正当用法，
不是 `docs/ARCHITECTURE.md` 第 6 节禁止的"跨进程锁"）。

- WS 侧：一条事件写完并通过阶段 2 的去重/乱序判定后，`set()` 一次。
- reconcile 侧：把 `await asyncio.sleep(interval_seconds)` 改成
  "等待唤醒或超时"，两者先到先得，然后 `clear()`。

### 2. 唤醒必须被节流与去抖

原样唤醒会在成交瞬间产生几十次连续 reconcile，把 REST 打满并可能触发限流。

- **最小间隔**：两次由唤醒触发的 reconcile 之间至少间隔 N 秒（建议 2 秒，做成常量并写注释）。
- **合并**：间隔内到达的多个事件合并为一次唤醒。
- **上限**：每分钟由唤醒触发的次数设硬上限（建议 20）。达到上限后退回纯轮询，
  并在健康端点上暴露 `wake_throttled=true`。
- 定时轮询**保持不变**，唤醒是叠加不是替换。WS 挂了也必须照常 30 秒跑一轮。

### 3. 只让相关事件唤醒

不是每一帧都值得唤醒。只有以下频道/条件才唤醒：

- `Trade`：任何一帧（成交是最需要立刻核验的）。
- `Order`：状态字段 `Or` 发生变化的帧。
- `TriggerOrder`：`TS` 或 `TU` 发生变化的帧（尤其 `TU` 从 `default` 变为真实值）。
- `Position`：`Po`（仓位数量）发生变化的帧。

不满足的帧照常入库，只是不唤醒。判断"发生变化"用阶段 2 已经维护的"已知最新状态"，
不要再查一次数据库。

### 4. 可观测

健康端点加：`wakes_last_hour`、`wakes_throttled_last_hour`、
`last_wake_at`、`last_wake_channel`、`reconcile_runs_last_hour`（区分
`by_timer` 与 `by_wake`）。

### 5. 手工订单并存的验证准备（补测第 7 项）

阶段 0 时交易所上有三张历史 pending 条件入场单，用户已于 2026-09-06 自行撤掉，
当前没有不属于任何 binding 的订单。因此本阶段的第 7 项这样处理：

- 观察前只读列出交易所上所有不属于任何 `execution_bindings` 的订单、条件单和仓位
  （手工或历史遗留）。若存在，观察前后各取一次它们的状态，必须完全一致，且系统不得
  对它们发起任何写入。
- 若不存在，记录"本阶段第 7 项无真实基线，只有离线证据"，把它留给阶段 5 的受控实验
  （届时用一张手工最小量单覆盖）。**不要为了制造基线去下手工单。**
- 无论哪种情况，若观察中发现唤醒式 reconcile 对任何非系统订单做了动作，立刻停止并
  报告——那说明唤醒改变了行为，违反本阶段"只改什么时候跑"的约束。

### 6. 把 REST 快照种进乱序 tracker（阶段 2 遗留）

阶段 2 生产实测：重同步第 1/4 步的 REST 快照结果没有种进进程内乱序 tracker，
重启后 tracker 为空直到新帧到达（`tracked_entity_count=0`）。阶段 2 里 tracker 不驱动
决定所以无害；本阶段 WS 事件开始唤醒 REST 核验后，重连后到达的旧帧就必须拦得住。

- 在重同步第 4 步（二次快照）完成后，用快照里的 Order / TriggerOrder / Position
  当前状态初始化 tracker 的"已知最新状态"，排序依据用交易所时间，缺失则用快照时间。
- 只在快照 `complete=True` 时种入；不完整快照什么都不种（不完整 = 未知，不是空）。
- 种入后重连前到达的旧帧必须被判为 out_of_order 且不覆盖，加离线测试。

### 7. 新增 REST 读的硬规则

阶段 2 执行会话指出状态机里最脆弱的转移是 `resyncing → healthy`，它的判据完全依赖
`RestSnapshot.complete`。本阶段若新增任何 REST 读：

- 必须经 `deepcoin_ws_resync.RestSnapshot` 构造，先检查 `complete` 再读集合；
  任何"先读集合再看 complete"的写法都是错的。
- 加一个静态守护测试：`deepcoin_private_ws.py`、`deepcoin_ws_resync.py`、
  `deepcoin_ws_stream_state.py` 及本阶段新文件里，对 `positions` / `open_orders` /
  `trigger_orders` / `fills` 的访问前必须有对 `complete` 的判断（用 AST 或最简单的
  行序检查都可以，目的是把纪律变成测试）。
## 禁止

- 禁止修改 reconcile 内部任何判据、阈值、退避、认领条件或状态转移。
- 禁止让 WS 事件直接写任何账本。事件只能唤醒，核验仍由 REST 做。
- 禁止去掉或延长原有的 30 秒定时轮询。WS 不可用时行为必须与今天完全一致。
- 禁止唤醒 `ingest` 的 `reconcile` 或 `authoritative_gap_recovery_loop`。
- 禁止对三张历史 pending 条件单做任何写入（撤单、改单、认领）。
- 禁止顺手修 `convergence_pending_alias_conflict`。那是独立缺陷，单独立项。
- 禁止任何交易所写入语义变更。
- 禁止用 `git add -A`。

## 验证等级与具体检查项

等级 **L2**。

### 补测项（交接文档 12 项中的第 7 项）

- [ ] **第 7 项：手工订单与系统订单并存。**
      - 离线：构造"存在不属于任何 binding 的 pending 条件单"的场景，
        断言唤醒式 reconcile 与定时式 reconcile 产出完全相同的决定。
      - 生产：观察窗口前后各取一次三张历史 pending 条件单的完整原始行，
        逐字段比对必须相同；确认零写入。

### 测试

- [ ] focused：唤醒/超时二选一的循环、节流与合并、每分钟上限、
      WS 不可用时退回纯轮询、只有指定频道条件才唤醒。
- [ ] **等价性测试**：同一份输入下，"被唤醒触发的一轮"与"被定时器触发的一轮"
      调用序列与结果一致。这是本阶段的核心断言。
- [ ] 并发/恢复测试：唤醒信号在 reconcile 正在跑时到达，不得产生重入。
- [ ] 最终候选跑一次全量套件（记录已知既有失败）。

### 生产观察

- [ ] 观察 30 分钟，覆盖至少 5 条真实消息、尽量 2 个群；
      不足 5 条则停止、留 `in_progress`、记录流量不足。
- [ ] `reconcile_runs_last_hour` 的 `by_wake` 与 `by_timer` 分布合理，
      `by_wake` 没有失控（未持续触顶节流上限）。
- [ ] 检查是否出现重复处理：同一 binding/leg 在窗口内被 reconcile 处理的次数
      与事件数关系合理，账本没有重复行。
- [ ] 直接查交易所历史：本阶段窗口内的所有交易所写入都必须能由生产正常交易解释，
      条数与 binding 数对得上。
- [ ] 三张历史 pending 条件单前后完全一致。
- [ ] 因为本路径可影响执行，检查 `authoritative_execution_attempts`
      在窗口内没有新增 `uncertain`；若有，必须能归因到正常交易而非本改动。
- [ ] 重启一次仅在本阶段涉及生命周期时才需要；本阶段不改生命周期，
      **可以不重启**（AGENTS.md L2 允许）。

## 完成条件

1. 上面全部检查项通过，或流量不足已记录且阶段留 `in_progress`。
2. 提交已推送并 `tg-deploy` 部署，回滚 SHA 已记录。
3. 更新 `docs/rest-ws-trading-status.md`：推进到阶段 4
   （`phase-4-shadow-binding.md`），证据区追加一行。
4. 发消息给 `brain_session_id`。

## 汇报格式

```text
阶段 3 完成 / 阻塞 / 流量不足留 in_progress
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>
等价性测试结论：
补测项 7：离线结论 + 三张历史条件单前后比对结论
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟），真实消息数、覆盖群数
reconcile 次数：by_timer / by_wake / 节流次数
新增 uncertain：条数与归因
交易所写入：条数与归因（必须全部来自正常交易）
异常与遗留：
证据路径（服务器）：
```
