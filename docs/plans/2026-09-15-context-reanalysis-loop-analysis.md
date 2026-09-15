# 上下文二次判断：同一条消息被重分析 19 次的原因

日期：2026-09-15
状态：4.1 + 4.2 + 4.3 已于 2026-09-15 部署生产 `ae43312a`；第 8 节（关闭「同群新消息」代用路径）用户已批准，实施中
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

## 6. 已批准的实施规格（4.1 + 4.2 + 4.3）

用户 2026-09-15 拍板「做 1 2 3」。以下是给子代理的精确规格；4.4 / 4.5 不做。

### 6.1 按消息的重分析上限（4.1）

- 位置：`context_resolution_worker.py` 的 `run_context_resolution_once`，在 `is_eligible` 检查之后、
  `_has_terminal_instruction` 之前。
- 规则：统计 `context_resolution_attempts` 中 `raw_message_id == claim.raw_message_id` 且
  `created_at >= now - 24h` 的行数（**不含**正在认领的这一行以外的过滤，就是全部行）。
  行数 `>= max_reanalysis_per_message`（新参数，默认 5）时：
  `_finish_claim(status="reanalysis_capped", now=current)`，返回
  `{"status": "reanalysis_capped", "raw_message_id": ...}`，**不调用** `reanalyze`，不调模型。
- 常量 `DEFAULT_MAX_REANALYSIS_PER_MESSAGE = 5` 放在 worker 模块顶部，与 `DEFAULT_RETRY_DELAY` 并列。
- `reanalysis_capped` 是终态：不在 `_claimable` 里，也不在 `schedule_context_reanalysis` 的
  `status.in_(("completed","pending_reanalysis"))` 里，天然不会再被排队。
- 同一消息其余仍为 `completed`+unresolved 的旧行被事件重排后，认领时同样会被这条规则封顶，不产生模型调用。
- 页面：`web_queries._CONTEXT_TERMINAL_STATE_BY_STATUS` 加 `"reanalysis_capped": "reanalysis_capped"`；
  模板 `context_state_labels` 加 `'reanalysis_capped': '重分析已达上限'`；`app.css` 该状态用灰色
  （与 `not_needed` 同款）。设计文档 `2026-09-15-message-card-recognition-labels-design.md` 的状态表不改，
  在本文档记录即可。
- 运行时事件：封顶时 `logger.warning("context reanalysis capped raw_message_id=%s attempts_24h=%s cap=%s")`，
  不发通知、不记 runtime incident。

### 6.2 「下一条同群消息」只重排真正等它的行（4.2）

- 位置：`context_resolution_worker.schedule_context_reanalysis`，`normalized_event == "next_same_chat_message"` 分支。
- 现状：只要行声明了任意 `reanalysis_triggers` 就重排。
- 改为：只重排声明了 `reply_target_available` 的行（`next_same_chat_message` 不是模型可声明的触发名，
  `REANALYSIS_TRIGGERS` 里没有它；其余四个触发名各有自己的精确事件，见 `EVENT_TRIGGER_MAP`），
  **且**该行对应消息的回复目标此刻「可用」：
  - `raw_messages.reply_to_message_id` 非空；
  - 同 `chat_id` 下存在 `message_id == reply_to_message_id` 的原始消息；
  - 该原始消息有 `strategy_message_links` 指向的线程，其 `current_lifecycle_id` 的
    `lifecycle_status` ∈ `strategy_thread_candidates.ACTIVE_LIFECYCLE_STATUSES`（直接 import 这个常量，
    与候选生成保持同一口径，包括 `expired`）。
  - 任一条不满足 → 不重排（回复目标要么还没来、要么永远不合法，两种情况重分析都没有意义；
    前者等它真来时 `message_processing_worker` 会再发一次事件，那时就满足了）。
- 把这个判断抽成 `_reply_target_now_available(session, raw_message) -> bool`，便于测试。
- `EVENT_TRIGGER_MAP["reply_target_available"]` 这条**显式事件**路径保持原样（谁显式发这个事件就信谁）。

### 6.3 让「上下文没变就跳过」真正生效（4.3）

- 位置：`context_resolution.py` `_upsert_attempt` 中 `state_fingerprint = build_context_state_fingerprint(...)`
  （约 718-724 行）。
- 改为 `candidate_thread_ids=set(collect_candidate_thread_ids(request_payload))`
  （从 `context_request_storage` 导入；就是同一函数在几行之后算 `candidate_thread_ids_json` 用的那个）。
  这样落库值与 worker 重算（`_attempt_candidate_thread_ids` 读 `candidate_thread_ids_json`）用同一集合。
- 只改这一处；`build_context_state_fingerprint` 本身不动。

### 6.4 测试

- `tests/test_context_resolution_worker.py`：
  - 既有 `test_next_same_chat_message_schedules_unresolved_attempt` 要按 6.2 调整：让它声明
    `reply_target_available` 且回复目标链接到活跃生命周期线程 → 仍被排队；
    新增反例：声明 `reply_target_available` 但回复目标线程生命周期为 `exited` → 不排队；
    声明 `evidence_version_changed`（无 `reply_target_available`）→ `next_same_chat_message` 不排队，
    但 `evidence_version_changed` 事件仍排队。
  - 新增：同一消息 24 小时内已有 5 行 → 第 6 次认领被标 `reanalysis_capped`，`reanalyze` 未被调用；
    4 行 → 正常调用。跨 24 小时的旧行不计入。
  - 新增：`_upsert_attempt` 落库后，`build_context_state_fingerprint(session_factory, raw_message_id)`
    （不传 candidate_thread_ids，走 `_attempt_candidate_thread_ids` 路径）与落库的 `state_fingerprint` 相等；
    请求体里回复链带一个不在 `candidate_strategy_threads` 的线程 id 也要相等（这正是修的场景）。
  - 既有 `test_unchanged_fingerprint_does_not_call_ai` 保持通过。
- `tests/test_recognition_context_gate.py` 的 `execution_state` 参数化加 `("reanalysis_capped","reanalysis_capped")`。
- `tests/test_web_recognition_card_labels.py` 或同类：渲染一条 `reanalysis_capped` 卡片，徽章文案「重分析已达上限」。
- 全量 `PYTHONPATH=. uv run pytest -q` 通过。

## 7. 实施记录

日期：2026-09-15。实施范围：第 6 节（6.1 + 6.2 + 6.3 + 6.4），4.4 / 4.5 未做。

### 7.1 改动文件

| 文件 | 改动 |
| --- | --- |
| `src/telegram_kol_research/context_resolution_worker.py` | 6.1 封顶 + 6.2 事件收紧 |
| `src/telegram_kol_research/context_resolution.py` | 6.3 指纹集合（`_upsert_attempt` 一处） |
| `src/telegram_kol_research/web_queries.py` | `_CONTEXT_TERMINAL_STATE_BY_STATUS` 加 `reanalysis_capped` |
| `src/telegram_kol_research/templates/_messages.html` | `context_state_labels` 加「重分析已达上限」 |
| `src/telegram_kol_research/static/app.css` | `.is-reanalysis_capped` 与 `.is-superseded` 同一条灰色规则 |
| `tests/test_context_resolution_worker.py` | 既有 1 例调整 + 新增 6 例 + 2 个 helper |
| `tests/test_recognition_context_gate.py` | 状态参数化加一行 |
| `tests/test_web_recognition_card_labels.py` | 新增 1 例 + `_build` 多一张卡片 |

6.1 实现要点：模块顶部新增 `DEFAULT_MAX_REANALYSIS_PER_MESSAGE = 5` 与 `REANALYSIS_CAP_WINDOW = timedelta(hours=24)`（与 `DEFAULT_RETRY_DELAY` 并列）；
`run_context_resolution_once` 新增关键字参数 `max_reanalysis_per_message`，默认取上述常量，位置在 `is_eligible` 之后、`_has_terminal_instruction` 之前；
计数走新的 `_recent_attempt_count(session_factory, *, raw_message_id, since)`（`created_at >= now - 24h` 的全部行，含被认领的这一行）；
封顶时按第 6 节写 `logger.warning("context reanalysis capped raw_message_id=%s attempts_24h=%s cap=%s")`，`_finish_claim(status="reanalysis_capped")`，
返回 `{"status": "reanalysis_capped", "raw_message_id": ...}`，不调用 `reanalyze`。`cli.py` 与 `web_app.py` 两个调用点不传该参数，即用默认 5。

6.2 实现要点：新增 `_reply_target_now_available(session, raw_message) -> bool`（`reply_to_message_id` 非空 → 同 `chat_id` 下存在该 `message_id` 的原始消息 →
该消息经 `strategy_message_links` 指向的线程，其 `current_lifecycle_id` 的 `lifecycle_status` ∈ `strategy_thread_candidates.ACTIVE_LIFECYCLE_STATUSES`，
常量直接 import，与候选生成同一口径）。`schedule_context_reanalysis` 的 `next_same_chat_message` 分支由「声明了任意触发条件就重排」改为
「声明了 `reply_target_available` 且此刻可用才重排」。`EVENT_TRIGGER_MAP` 未动，显式 `reply_target_available` 事件路径未动。

6.3 实现要点：`_upsert_attempt` 的 `state_fingerprint=build_context_state_fingerprint(...)` 的 `candidate_thread_ids=` 由
`_collect_ids(request_payload["candidate_strategy_threads"], …)` 改为 `set(collect_candidate_thread_ids(request_payload))`（该函数原本已在本模块 import，
几行之后就用它算 `candidate_thread_ids_json`）。`build_context_state_fingerprint` 本身未动；`_collect_ids` 在同文件另有使用，保留。

### 7.2 既有测试的调整

| 位置 | 原断言 | 新断言 | 原因 |
| --- | --- | --- | --- |
| `tests/test_context_resolution_worker.py::test_next_same_chat_message_schedules_unresolved_attempt` | 仅 `_persist_unresolved(chat_id=88)`（消息无 `reply_to_message_id`，声明 5 个触发条件），断言 `scheduled == 1` | 先建回复目标（`entered` 生命周期线程 + `strategy_message_links`），消息 `reply_to_message_id=4`、只声明 `reply_target_available`，仍断言 `scheduled == 1` | 6.2 后「声明任意条件就重排」不再成立；这一例的语义变成「真正等回复目标、且目标此刻可用 → 仍重排」，正是规格要求的保留路径 |
| `tests/test_context_resolution_worker.py::_persist_unresolved`（helper） | 建 `RawMessage` 时不设 `reply_to_message_id` | 新增关键字参数 `reply_to_message_id=None` 并透传 | 6.2 的用例需要造「回复了某条消息」的行；默认 `None` 时既有全部用例行为不变 |

除上述两处，`grep -rn "next_same_chat_message\|state_fingerprint\|reanalysis_triggers" tests/` 命中的其余文件均无需改动：
`tests/test_message_processing_worker.py` 只断言调度器被传入了哪些事件名（用假调度器，不进 `schedule_context_reanalysis`）；
其余命中处要么是显式事件路径（`message_edited` / `exchange_snapshot_changed` / `evidence_version_changed` 等，6.2 未触及），
要么直接调用 `build_context_state_fingerprint`（6.3 未改该函数），要么只是造数据时写 `reanalysis_triggers_json`。
既有 `test_unchanged_fingerprint_does_not_call_ai` 未改动且仍通过。没有既有测试与第 6 节规格冲突。

### 7.3 新增用例

`tests/test_context_resolution_worker.py`（新增 helper `_persist_reply_target_thread`、`_add_completed_attempts`）：

- `test_next_same_chat_message_skips_a_reply_target_that_can_never_be_chosen`：回复目标线程生命周期 `exited` → `scheduled == 0`，行仍是 `completed`。
- `test_next_same_chat_message_ignores_rows_waiting_on_another_trigger`：只声明 `evidence_version_changed` → `next_same_chat_message` 不排队；随后 `evidence_version_changed` 事件仍排队。
- `test_sixth_reanalysis_of_one_message_in_24_hours_is_capped`：24 小时内共 5 行 → 第 6 次认领返回 `reanalysis_capped`，行落 `reanalysis_capped`，`reanalyze` 被断言为不可调用。
- `test_reanalysis_below_the_per_message_cap_still_runs`：共 4 行 → 正常调用 `reanalyze`。
- `test_attempts_older_than_the_cap_window_do_not_count`：6 行落在 25 小时前 → 不计入，正常调用。
- `test_stored_state_fingerprint_matches_the_worker_recomputation`：请求体的回复链带一个不在 `candidate_strategy_threads` 的线程 id（对应本案例的 432），
  `_upsert_attempt` 落库后 `build_context_state_fingerprint(session_factory, raw_message_id)`（不传 candidate_thread_ids）与落库的 `state_fingerprint` 相等；
  同时断言旧的「只取候选集」投影得到的哈希与之不同，避免这条断言变成恒真。

`tests/test_recognition_context_gate.py`：`execution_state` 参数化加 `("reanalysis_capped", "reanalysis_capped")`。

`tests/test_web_recognition_card_labels.py`：`_build` 增加一条状态为 `reanalysis_capped` 的上下文尝试卡片，新增
`test_capped_context_card_says_the_reanalysis_ceiling_was_reached` 断言渲染出 `<span class="context-exec-state is-reanalysis_capped">重分析已达上限`。

### 7.4 全量测试

`PYTHONPATH=. uv run pytest -q`：全量 `8875 passed, 4 skipped, 107 warnings in 758.17s (0:12:38)`，无失败、无新增跳过（4 个 skip 与改动前一致）。最后三行原样：

```
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html

8875 passed, 4 skipped, 107 warnings in 758.17s (0:12:38)
```

### 7.5 规格未覆盖之处的取舍

- 24 小时窗口以模块常量 `REANALYSIS_CAP_WINDOW = timedelta(hours=24)` 表达（第 6 节只点名了 `DEFAULT_MAX_REANALYSIS_PER_MESSAGE`）；
  与既有 `DEFAULT_STALE_AFTER` / `DEFAULT_RETRY_DELAY` 同一写法，不引入配置项，运行时语义与文字规格一致。
- 上限值不经配置下发：`cli.py` 和 `web_app.py` 两个调用点都用默认值，改动对现有运行时参数面零影响。
- `app.css` 里 `is-not_needed` 没有独立规则（它走 `.context-exec-state` 基础样式的灰色）；为了状态名显式可见，
  `is-reanalysis_capped` 与 `is-superseded` 合并成同一条规则，颜色值与基础灰色完全相同，视觉上即「与 not_needed 同款」。
- `schedule_context_reanalysis` 对同一 `raw_message_id` 的多行只算一次回复目标可用性（本地 dict 缓存），纯粹避免同一事件内重复查询，不改变判定结果。

### 7.6 部署记录（指挥会话补记）

- 子代理提交 `ae43312a`；指挥会话独立复跑全量 8875 passed / 0 failed / 4 skipped（851 s）。
- 2026-09-15 深夜 `tg-deploy ae43312a2881c37663cacd7217ffa055374917fa`，回滚 SHA `fa1e0c09`。
  worker / web / ingest 均 active，启动无异常；共享分支 `origin/codex/deepcoin-auto-trading-v1` = 部署 SHA。
- 验证方式：只读查 `context_resolution_attempts`，一周后看是否还有单条消息 24 小时内 > 5 行；
  以及 worker 日志 `context reanalysis capped` 的出现次数（每次封顶一行 WARNING）。

## 8. 追加：关闭「同群新消息」代用路径（用户 2026-09-15 批准）

### 8.1 依据（生产库 60 天只读统计）

- 声明 `reply_target_available` 的记录 66 条：32 条消息根本没有 `reply_to_message_id`；34 条回复目标在分析前就已入库；
  **0 条**目标是分析之后才入库。用户的判断成立：KOL 人为发消息，回复目标一定早已入库，「同群新消息」不可能让回复目标变得可用。
- `next_same_chat_message` 事件 60 天重排 203 次；重分析产出动作的 21 次里紧跟其后的只有 2 次，其中 1 次是同一消息重复同一结论。
- 真正有价值的重分析来自精确事件（`entry_leg_status_changed`、`exchange_snapshot_changed`）与错误重试路径。
- Telegram 监听器 `telegram_live_listener.py:336-356` 在「回复目标不在库里 → 主动拉取成功」时发**显式** `reply_target_available` 事件。
  这是回复目标真正「从无到有」的唯一途径，**保留**。

### 8.2 改动

1. `message_processing_worker.py:181-186`：删除对每条新消息发 `next_same_chat_message` 事件的调用；`message_edited` 的发送保留。
2. `context_resolution_worker.schedule_context_reanalysis`：删除 `next_same_chat_message` 的全部特殊分支。
   未知事件（不在 `EVENT_TRIGGER_MAP`）按现有逻辑 `return 0`，所以即使旧代码路径仍传入该事件名也只是空操作。
3. 删除 6.2 新增的 `_reply_target_now_available` 及其 import（`ACTIVE_LIFECYCLE_STATUSES` 若无其他使用则一并删）。
4. **不改**：`EVENT_TRIGGER_MAP["reply_target_available"]`（显式事件路径）、`REANALYSIS_TRIGGERS`、提示词、`CONTEXT_RESOLUTION_PROMPT_VERSION`、
   6.1 的封顶、6.3 的指纹、`telegram_live_listener.py` 的发送方。

### 8.3 测试

- `tests/test_context_resolution_worker.py`：删除 `test_next_same_chat_message_schedules_unresolved_attempt`、
  `test_next_same_chat_message_skips_a_reply_target_that_can_never_be_chosen`、
  `test_next_same_chat_message_ignores_rows_waiting_on_another_trigger`（及只为它们服务的 helper）；
  新增 `test_next_same_chat_message_event_is_a_no_op`：声明了任意触发条件的 unresolved 行，收到 `next_same_chat_message` → 返回 0、状态不变。
  新增 `test_explicit_reply_target_event_still_schedules`：声明 `reply_target_available` 的行收到显式 `reply_target_available` 事件 → 排队（若已有等价用例则不重复）。
- `tests/test_message_processing_worker.py:96` 附近断言事件名的用例：改为断言**不再**发 `next_same_chat_message`，`message_edited` 在有 `edit_date` 时仍发。
- 全量 `PYTHONPATH=. uv run pytest -q` 通过。
