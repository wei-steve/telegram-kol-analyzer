# 上下文二次判断：同一条消息被重分析 19 次的原因

日期：2026-09-15
状态：分析完成，修复方案待用户拍板；未改代码
关联：`docs/plans/2026-09-15-context-trigger-tightening-analysis.md` 第 2.5 / 5 节记录了这个现象。
数据来源：生产库 `data/research.db` 只读查询；worker 日志已过保留期，9 月 3 日的记录拿不到。

## 1. 案例

raw_message 14636，群 -1003048800035，2026-09-03 13:45:36 UTC：

> 大镖客·Andy 第二止盈位到了 @Tarderfengge …

它**回复**了 4474 号消息。4474 是策略线程 432（BTC 多，生命周期 1063）的根消息。

19 条 `context_resolution_attempts` 全部落在 13:46 → 14:49 这 63 分钟内，同一个证据版本（6420），
同一组候选 id。时间线（UTC）：

| 尝试 | 时间 | 阶段 | 结果 | 次数 | 下次触发 |
| --- | --- | --- | --- | --- | --- |
| 4544 | 13:46:18 | 首次 | unresolved 0.6 | 3 → exhausted | reply_target_available |
| 4545 | 13:47:03 | 重分析 | unresolved 0.5 | 3 → exhausted | evidence_version_changed |
| 4547 | 13:49:01 | 重分析 | unresolved 0.5 | 3 → exhausted | reply_target_available |
| 4548 | 13:51:10 | 重分析 | unresolved 0.7 | 3 → exhausted | reply_target_available |
| 4550 | 13:54:00 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4551 | 13:54:07 | 重分析 | unresolved 0.3 | 2 → completed | |
| 4553 | 13:56:23 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4554 | 13:56:33 | 重分析 | 错误 management_action_incompatible | 2 → exhausted | |
| 4556 | 13:58:42 | 重分析 | unresolved 0.5 | 3 → exhausted | reply_target_available |
| 4557 | 13:59:03 | 重分析 | unresolved 0.7 | 3 → exhausted | reply_target_available |
| 4558 | 14:02:05 | 重分析 | unresolved 0.5 | 3 → exhausted | strategy_state_changed |
| 4560 | 14:04:04 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4561 | 14:04:15 | 重分析 | unresolved 0.5 | 3 → exhausted | reply_target_available |
| 4562 | 14:05:57 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4563 | 14:07:55 | 重分析 | unresolved 0.5 | 1 → completed | |
| 4565 | 14:09:19 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4567 | 14:09:58 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |
| 4568 | 14:11:43 | 重分析 | unresolved 0.3 | 3 → exhausted | reply_target_available |
| 4572 | 14:49:42 | 重分析 | 错误 target_outside_candidate_set | 2 → exhausted | |

模型 19 次给的理由是同一句话：「消息回复 4474，目标是线程 432，但候选列表里只有 423（空）和 22（已过期的多），
无法唯一确定目标」。有 7 次它干脆直接选了 432，被合约校验拒绝（`target_outside_candidate_set`）。

## 2. 三层原因

### 2.1 语义死结：回复目标不是合法候选，但又一直摆在模型面前

- 生命周期 1063 在消息发出前一小时（12:44）已经是 `exited`。
  候选生成 `strategy_thread_candidates.generate_strategy_thread_candidates` 只收
  `pending_entry / entered / holding / expired` 四种状态（`ACTIVE_LIFECYCLE_STATUSES`），
  所以线程 432 永远进不了 `candidate_strategy_threads`。
- 但请求体里的回复链（`message_context.reply_chain[0].strategy_links`）和交易所状态仍然写着 432 / 1063。
  模型看得见「这条消息在管理 432」，却被告知 432 不可选。系统提示词要求「不能唯一确定目标时输出 unresolved」，
  于是每一次都是 unresolved，或者违规选 432。
- 顺带发现：线程 22 的生命周期 652 早在 7 月 29 日就 `expired`，五周后仍被当作候选送给模型，
  因为 `expired` 也在 `ACTIVE_LIFECYCLE_STATUSES` 里。它只是干扰项。

**这个死结不会随时间解开**：432 不会因为再来几条消息就变回活跃。所以无论重试多少次，答案都一样。

### 2.2 没有「同一条消息」的记忆：每次重试都是一行新记录、计数从零开始

- 每次调用的 `context_fingerprint` 是**整个请求体**（74 KB，含消息窗口、交易所状态里的实时价格）的哈希
  （`context_resolution.py:931-943`）。同群任何一条新消息、任何一次价格变化都会让哈希变化，
  所以每次重分析都新建一行 `context_resolution_attempts`，`attempts` 从 0 起。
- 现有的两个上限都是**按行**算的：解析器每行最多 2 次模型调用（`range(…, 3)`），
  worker 每行最多 3 次异常重试（`max_attempts=3`）。跨行不累计。**没有任何按消息的上限**（全库 grep 无）。
- worker 的「上下文没变就跳过」保护（`run_context_resolution_once` 里 `fingerprint == claim.context_fingerprint`）
  在这个案例里从未生效。19 行里有 13 行落库的 `state_fingerprint` 完全相同，却照样重分析了。
  原因是两边算指纹用的候选集不一样：落库时用请求体里 `candidate_strategy_threads` 的 id（423、22），
  worker 重算时用 `candidate_thread_ids_json`（对整个请求体递归收集的 id：22、410、423、432、434，
  `context_request_storage.collect_candidate_thread_ids`）。集合不同，哈希必然不同，「没变」永远判成「变了」。

### 2.3 两台重试引擎互相喂

- **异常重试（2 分钟一次）**：模型违规选 432 → 解析器抛 `ContextResolutionError` →
  `assess_message_authoritatively` 把它记成识别失败（`authoritative_failed`）→ CLI 的 `reanalyze` 抛 `RuntimeError` →
  worker 把**被认领的那一行**标成 `pending_reanalysis`、`attempts+1`、2 分钟后再来（`DEFAULT_RETRY_DELAY`），
  最多 3 次。表里 13:47、13:49、13:51、13:54、13:56、13:58、14:02、14:04、14:05、14:07、14:09、14:11 的两分钟节奏就是它。
  每次再来又新建一行（2.2），新行自己也是 unresolved 或错误，于是又成为下一轮的种子。
- **事件重试（同群任何新消息）**：unresolved 行会声明「下次触发条件」，如 `reply_target_available`。
  `message_processing_worker.py:184` 对**每一条**新入库的同群消息都发 `next_same_chat_message` 事件；
  `schedule_context_reanalysis` 对这个事件的匹配规则是「只要声明了任意触发条件就重排」（`context_resolution_worker.py:404-407`），
  并不检查声明的条件是否真的发生了。那一小时内该群来了 7 条消息，每条都把所有仍处于 `completed` 且 unresolved 的行重新排队。
  而 `reply_target_available` 这个条件在这里**永远不可能满足**——回复目标早就存在，只是不合法。
- 链条在 14:11 因各父行 `attempts` 到 3 而暂停，14:49 又一条同群消息把还处于 `completed` 的行（4551、4563）再次唤醒，产生第 19 次。

## 3. 影响面

最近 30 天，60 分钟内被分析 ≥ 3 次的消息有 300 条，但要分开看：

| 类型 | 消息数 | 说明 |
| --- | --- | --- |
| 网络错误连环重试 | 283 | 全部集中在 8 月 21–23 日，模型 `deepseek-v4-flash`（1367 行）。那是一次提供商故障，token 几乎为零（请求没发出去）。现在已不复现（9 月只有 4 行）。 |
| unresolved / hold 循环 | 9 | 本文的机制。约 33 万 token。 |
| 目标/合约错误循环 | 7 | 本文的机制（模型违规选目标）。约 46 万 token。 |

也就是说，当前仍在发生的是后两类，共 16 条消息、约 80 万 token / 30 天。占比不大，
但每次都是**同一个问题问同一个模型十几遍**，纯浪费，而且这类消息（对已离场策略发的止盈通知）以后还会来。

## 4. 修复方案（待拍板）

按收益/风险排序，可以只做前两条。

### 4.1 给「同一条消息」加重分析上限（必做，最小改动）

- 在 `run_context_resolution_once` 认领后、调用 `reanalyze` 前，统计该 `raw_message_id` 在
  `context_resolution_attempts` 里的总行数（或最近 24 小时内的行数）；超过上限（建议 5）就把认领行标成新的终态
  `reanalysis_capped`，不再调用模型。
- 页面的上下文卡片状态表加一项 `reanalysis_capped` →「重分析已达上限」（灰）。
- 预期：本案例从 19 次降到 5 次；不影响真正因为「上下文变了」而需要的重分析（那种通常 1–2 次就解决）。

### 4.2 「下一条同群消息」不再无差别重排（建议做）

- `schedule_context_reanalysis` 对 `next_same_chat_message` 事件，只重排声明了 `next_same_chat_message`
  或 `reply_target_available` 的行，并且 `reply_target_available` 要额外检查回复目标**现在是否在合法候选里**；
  不在就不排（它永远不会满足）。
- 更直接的替代：模型声明 `reply_target_available` 但回复目标的生命周期已经 `exited`/`cancelled` 时，
  解析器把决策改写成 `hold` 且不声明任何触发条件，让它一次终结。

### 4.3 修好「上下文没变就跳过」（建议做，一行改动）

- `_upsert_attempt` 落库 `state_fingerprint` 时，改用 `collect_candidate_thread_ids(request_payload)`
  （与 worker 重算时同一个集合），两边一致后这条保护才真正生效。

### 4.4 语义层（可选，另议）

- 回复目标的生命周期已 `exited` 时，候选生成可以把它作为「已离场的回复目标」显式告知模型，
  或者第一次识别阶段直接把这类消息判为「对已结束策略的通知」，不进上下文。
- `expired` 生命周期是否还该当候选（线程 22 过期五周仍在列表里），值得单独看。

### 4.5 不建议

- 单纯把 `max_attempts` 从 3 降到 1：治标，且会伤到真正的网络错误重试。
- 把 `context_fingerprint` 改成只哈希消息本身：会让「上下文确实变了」的重分析被误判为重复。

## 5. 实施要点（拍板后交子代理）

- 4.1：`context_resolution_worker.py` 的 `run_context_resolution_once`；新状态要加进
  `web_queries._CONTEXT_TERMINAL_STATE_BY_STATUS` 与模板 `context_state_labels`；测试
  `tests/test_context_resolution_worker.py` 加「第 6 次认领被封顶」用例。
- 4.2：`schedule_context_reanalysis` + `tests/test_context_resolution_worker.py`。
- 4.3：`context_resolution.py` `_upsert_attempt` 的 `state_fingerprint=` 一行 + 一个「落库与重算指纹相等」的测试。
- 全量测试同前：`PYTHONPATH=. uv run pytest -q`。
