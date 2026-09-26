# 卡死的删除退出：说得出原因、不再刷屏、能自己收口 · 状态

日期：2026-09-26
分支：`stuck-deletion-exit-selfheal`（基线 `origin/main` = `983e4e26`）
设计稿：`docs/plans/2026-09-26-stuck-deletion-exit-selfheal-design.md`（已批准）
背景证据：`docs/2026-09-26-silent-stall-case-note.md`
状态：**三块代码 + 用例全部完成、全量测试绿、未部署、未推送**。
解封 310/311 在本项目开工前已由人工完成（`manual_release_exchange_empty_20260926`）。

本文件是本项目跨会话唯一的进度真相。

---

## 一句话

`release_reason` 进了告警词表（连同一个被它掩盖住的第二处缺陷），重复 capture 从每 5 秒一次
变成每条退出每 30 分钟一次（状态变化或释放立即发声），并且**手里没有任何交易所凭据、
lane 里每一张单都归属于别人**的删除退出现在会自己收口，不再永久封死 lane。

## 提交（逐路径 add，每块一提交，每提交前跑完整 pytest）

| 提交 | 块 | 全量测试 |
|---|---|---|
| `78a35cb1` | 甲 词表 + 摘要标签 + 通知行 | 9549 passed, 4 skipped (12:46) |
| `1817d0c9` | 乙 capture 节流 | 9553 passed, 4 skipped (12:47) |
| `03e303a1` | 丙 无凭据收口 | 9563 passed, 4 skipped (12:44) |

基线（未加本项目用例时）9546 条；甲 +3、乙 +4、丙 +10，共 17 条新用例。

## 落点

| 块 | 文件 | 函数 / 位置 | 做了什么 |
|---|---|---|---|
| 甲 | `src/telegram_kol_research/runtime_incidents.py` | `_SUMMARY_FIELDS` | 加 `release_reason`、`timeout_minutes`，按该文件既有注释风格写明理由与前两次同型失败（A-8c 的 `group_trading_mode`、A-10b 的 `pos_id`） |
| 甲 | `src/telegram_kol_research/runtime_incident_adapters.py` | `capture_source_deletion_exit_stuck` | `impact` 不再把分钟数焊进标签（改为 `lane_still_held_after_timeout`），分钟数改走整数字段 `timeout_minutes` |
| 甲 | `src/telegram_kol_research/system_operator_bot.py` | `format_runtime_incident_notification` 的 `labels` | 通知里多一行 `释放判定: <release_reason>` |
| 乙 | `src/telegram_kol_research/source_deletion_exit_timeout.py` | `STUCK_EXIT_CAPTURE_MIN_INTERVAL`、`_LAST_STUCK_CAPTURE`、`_should_capture`、`reset_stuck_exit_capture_throttle`、结果多一个 `captured` 字段 | 同一条退出至少隔 30 分钟才再 capture；`state`/`last_reason` 变化或本轮释放立即 capture；末尾那行 `logger.warning` 与 capture 同步节流 |
| 丙 | 同上 | `ExchangeAbsenceReader`（原闭包改类）+ `lane_footprint`、`ExchangeLaneFootprint`、`_has_no_execution_credentials`、`_no_exchange_footprint_verdict`、`_lane_identity`、`_unattributed_lane_rows`、`_release(reason=...)`、常量 `NO_EXCHANGE_FOOTPRINT_REASON` | 三条判据全成立才释放，`last_reason=released_no_exchange_footprint` |
| 用例 | `tests/test_stuck_deletion_exit_selfheal.py` | 新文件，17 条 | 见"测试清单" |
| 用例 | `tests/conftest.py` | `reset_stuck_deletion_exit_capture_throttle`（autouse） | 进程内节流状态不跨用例泄漏 |

## 为什么这么选

### 甲：词表少一个词，外加两处被它遮住的缺陷

1. **词表**：补 `release_reason`，这是设计稿点明的一行。
2. **标签长度（设计稿没写，但不做等于没做甲）**：补上词之后用例仍然红——**held 那一路的
   详细摘要照样被拒**。原因是 `impact` 的取值 `lane_still_held_after_120_minutes` 共 33 字符、
   含小写 + 数字 + 下划线三类字符、不同字符 ≥12，正好命中 `_looks_like_opaque_secret` 的
   「≥32 字符 + ≥3 字符类 + ≥12 个不同字符」判据。held 恰恰是这条告警存在的理由
   （release 那一路 31 字符，侥幸擦边通过；`timeout` 设成 1440 分钟时它也会被拒）。
   所以把分钟数从标签里拆出来单独走一个**整数**字段：`_walk_json_values` 只扫字符串值，
   整数不过脱敏扫描，而去掉数字的标签永远只有 2 个字符类，再长也碰不到那条判据。
3. **最后一公里（同样超出设计稿字面）**：`format_runtime_incident_notification` 的 `labels`
   里原来没有 `release_reason`，摘要里有、人在 Telegram 里看不到。加了一行「释放判定」。
   标签用中性词，因为这个字段两个方向都要说（为什么没释放 / 凭什么释放了）。

**副作用（有意接受）**：告警指纹 `_fingerprint` 含摘要 JSON，所以 `impact` 与
`release_reason` 变了之后，**同一条卡住的退出会产生一行新的 incident 行并发一次新通知**。
这正是我们要的（旧行为是第一条之后永远静默累加 `repeat_count`），但**上线首个 tick 会有一次
集中通知**，条数等于「当时处于 `recovery_required` 且已超时的退出数」。见"上线注意"。

### 乙：节流放进程内存，键 exit_id

- 30 分钟是设计稿定的，做成模块级常量 `STUCK_EXIT_CAPTURE_MIN_INTERVAL`，用例注入时钟，
  **无 sleep**。
- 比较的键是**库里的** `(state, last_reason)` + 上次时间。设计稿原话是「`state` 或
  `last_reason` 与上次不同则立即 capture」，这里逐字照做，没有把本轮算出来的
  `release_reason` 也算进去：否则交易所读取抖动（读失败/读成功交替）会让节流形同虚设，
  又变回刷屏。代价是「状态没变但原因换了一种说法」最多迟 30 分钟被看到。
- **释放一定发声**（偏离设计稿字面、保留其意图）：设计稿说释放后的 capture「频率由乙管」，
  照字面走会让「held capture 之后 1 分钟内发生的释放」被吞掉——而"lane 刚刚打开"是这条
  告警最有价值的一句话，且退出的 state 确实变了（`recovery_required` → `succeeded`），
  属于"状态变化优先于节流"的同一条规则。用例 `test_a_release_is_never_throttled` 钉住。
- 不落库、进程重启多打一次，按设计稿。`_LAST_STUCK_CAPTURE` 只随「本进程里曾经超时过的
  退出数」增长（生产量级两位数以内），不做淘汰。
- `runtime_incidents` 的 coalesce **一行未动**。
- 返回值加 `captured`（本轮真的落了告警的那些）；`alerted` 语义不变 = 本轮判定过的退出，
  这样既能观测节流，又不动既有用例与调用方的读法。

### 丙：释放的三条判据与它们的失败方向

`_no_exchange_footprint_verdict`，三条全成立才释放，任何一条不确定都判「不释放」：

1. `execution_binding_id IS NULL` 且 `pos_ids`/`order_ids` 皆空（`_has_no_execution_credentials`）；
2. 本轮交易所读取成功。原来的 `ExchangeAbsenceProof(proven, reason)` 分不清"还在仓"和
   "没读到"（两者都是 `proven=False`），而释放绝不能建立在"没读到"上，所以**新增**
   `ExchangeLaneFootprint`，带一个 `read: bool`——这是设计稿允许的扩展，理由就是这个二义性；
3. lane（chat + symbol + side）里交易所上的每一个在仓持仓、每一张挂单都能按
   `ExecutionOrderLeg.pos_id` / `order_id` 精确查到 binding，且那个 binding 的
   `(chat_id, message_id)` 不是这条退出自己那条消息。有一个查不到 → 不释放。

fail-closed 的具体方向（都写在注释里）：

- 交易所行的 `instId` 读不出来，或 `posSide` 是 `buy`/`sell` 这种无法映射成 long/short 的值
  → 算**在 lane 内**（宁可当成可能是自己的孤儿）；
- 持仓 `pos` / `size` 解不出数 → 算**在仓**；只有 `pos=0` 算历史；
- 归属查询只用**精确等值**匹配（`pos_id`/`order_id` 都有索引，不做全表扫描）。所以一条 leg
  的 id 若是逗号拼接的，它拥有的那一行会被判成"无人认领" → 继续封着，是安全方向；
- lane 的 symbol/side 经 `raw_message_id` → `signal_candidates` 取，直接复用
  `deferred_instruction_recovery._latest_candidate_symbol_side`（barrier 的 resume 路径用的
  同一个函数），不另立第二套；取不到就 `lane_identity_unknown` → 不释放；
- 传进来的 reader 若不带 `lane_footprint`（纯 callable，老用例里的 lambda）→
  `lane_footprint_reader_unavailable` → 不释放。

**新增的交易所读取被乙的节流挡住**（设计稿没写，我加的）：无凭据的退出原来是"凭记忆"回答
`exit_has_no_known_position`、一次交易所调用都不发；丙要看快照，若每 5 秒一轮，只要这种退出
还在，就是每分钟 24 次 REST。所以 lane 判定只在"本轮本来就要发声"的 pass 上跑——
封了好几天的 lane 再等半小时无所谓，交易所限频不能等。用例
`test_the_lane_read_only_happens_on_a_speaking_pass` 钉住。交易所调用点仍然只有原来那一个：
`build_exchange_absence_reader` 现在返回 `ExchangeAbsenceReader` 实例（仍可调用、签名不变），
`lane_footprint` 复用**同一份** memoized 快照。

释放动作复用 `_release`，只多一个 `reason` 参数；`last_reason` 写
`released_no_exchange_footprint`，与 `position_gone_confirmed` 区分；释放后照常
`_resume_behind_exit`，并强制 capture 一次（`lane_released=True`）。

**明确没做**（设计稿要求）：barrier 的 `state != 'succeeded'` 规则没动；91 条
`raw_message_id IS NULL` 的 `unbound` 退出没动（`_lane_identity` 对它们返回 None，
它们本来也不封 lane）；**已经 `deferred_expired` 的消息不会被恢复**——resume 只认
`automation_reason = 'waiting_source_deletion_exit'`，有回归用例钉住。

## 测试清单与结果

`tests/test_stuck_deletion_exit_selfheal.py`（17 条，全绿）：

甲
- `test_the_stuck_exit_alert_lands_with_impact_and_release_reason`：详细摘要落库，带
  `release_reason` / `impact` / `timeout_minutes`（改前落的是最小摘要，这条会红）
- `test_the_notification_a_person_reads_names_the_release_verdict`：Telegram 文本里有「释放判定」
- `test_every_stuck_exit_summary_field_is_inside_the_closed_vocabulary`：摘要键全在闭合词表内

乙（用「有 binding、持仓还在」的旧 held 形状，只量节流本身）
- `test_an_unchanged_stuck_exit_is_captured_once_per_interval`：6 次 5 秒 tick 只 capture 1 次；
  差 1 秒仍静默；过 30 分钟 + 1 秒再发一次；`alerted`/`held` 每轮照旧
- `test_a_state_change_is_captured_immediately_despite_the_throttle`：同状态被节流，
  `last_reason` 一变立刻 capture
- `test_a_release_is_never_throttled`：节流窗内发生释放照样发声
- `test_the_summary_log_line_is_throttled_with_the_captures`：4 个 tick 只留 1 行汇总 WARNING
  （用直接挂 handler 的 `_captured`，不用 `caplog`：`configure_application_logging` 把包
  logger 的 `propagate` 关了，`caplog` 在全量跑时收不到——这条先踩了一次才发现）

丙
- `test_a_credentialless_exit_over_a_fully_attributed_lane_is_released`：生产形状（米娅
  binding 383 拥有账户上唯一一张 BTC 多单）→ 释放，`released_no_exchange_footprint`，
  仍 capture 一次
- `test_an_unattributed_position_in_the_lane_keeps_it_sealed`：无主持仓 → 不释放（安全边界）
- `test_an_unattributed_resting_order_in_the_lane_keeps_it_sealed`：无主挂单 → 不释放
- `test_another_lane_does_not_keep_this_one_sealed`：BTC 空 / ETH 多不算这条 lane
- `test_a_failed_exchange_read_never_releases_a_lane`：读失败 → 不释放，原因 `lane_read_failed`
- `test_a_closed_position_row_does_not_keep_the_lane_sealed`：`pos=0` 是历史
- `test_an_exit_whose_lane_cannot_be_named_is_left_sealed`：无候选 → `lane_identity_unknown`
- `test_an_exit_with_a_binding_still_takes_the_position_proof_path`：有 binding 的退出走原路径，
  在仓时 `position_still_open`、消失后仍写 `position_gone_confirmed`
- `test_the_lane_read_only_happens_on_a_speaking_pass`：4 个 tick 只读交易所 1 次
- `test_releasing_the_lane_resumes_the_waiting_but_never_the_expired`：释放后
  `waiting_source_deletion_exit` 的消息重新入队，`deferred_expired` 的不入队

全量（本地 `uv run pytest -q`，每块提交前各跑一次）：

```
甲  9549 passed, 4 skipped, 109 warnings in 766.26s
乙  9553 passed, 4 skipped, 109 warnings in 767.90s
丙  9563 passed, 4 skipped, 109 warnings in 764.98s
```

## 上线注意（部署另行批准）

1. **首个 tick 会有一次集中通知**：甲改了摘要 → 指纹变了 → 每条「已超时的
   `recovery_required` 退出」会产生一行新 incident 并通知一次。部署前先在快照上数一下
   `SELECT count(*) FROM source_message_deletion_exits WHERE state='recovery_required'`
   （再按 `updated_at <= now-120min` 过滤），心里有数。之后由乙的 30 分钟节流管住。
   案例备注提到有 91 条 `unbound` 退出，它们的 state 本文件没有核实，这就是要先数的原因。
2. **只读核对先 `VACUUM INTO` 快照**再查，别在生产库上扫表（记忆规则）。

## 上线后怎么验证

1. **噪音降级（最直接的可观测指标）**：`journalctl -u telegram-kol-worker` 里
   `source deletion exits stuck alerted=` 这类 WARNING，应从**约 6.9 万行/天**降到
   **每条卡住的退出每 30 分钟一行**（当前若无卡住的退出则为 0 行）。若仍是每 5 秒一行，
   说明节流没生效。
2. **告警终于说得出原因**：新产生的 `source_deletion_exit_stuck` 行，
   `json_extract(redacted_summary,'$.release_reason')` 必须非空（历史行是最小摘要，没这个键），
   `repeat_count` 不再以每 5 秒 +1 的速度增长；Telegram 通知里能看到「释放判定」一行。
3. **释放路径**：出现 `last_reason='released_no_exchange_footprint'` 的行时核对三件事：
   该行 `execution_binding_id` 为 NULL；同群同币同向在交易所上的持仓/挂单当时都能在
   `execution_order_legs` 里查到**别的** binding；释放后该 lane 的后续消息不再是
   `waiting_source_deletion_exit`。
4. **不该发生的事**：不应出现任何 `deferred_expired` 消息被重新入队
   （`message_processing_jobs` 里 `last_reason='deferred_resume'` 的 raw message 必须都还是
   `waiting_source_deletion_exit`）；不应出现新的下单。
5. **交易所调用量**：`list_positions` / `list_open_orders` 的频率不应因为本次改动变成
   每 5 秒一次；预期是「有卡住退出时，每 30 分钟一次」。

## 未做 / 留给下一位

- **未部署、未推送**，按指令。
- 案例备注 §4 建议的值守判据（D6a 被封的 lane / D6b `deferred_expired` 进 D3 损失判据 /
  D6c 喊了没人听）属于 `docs/codex-oncall-status.md` 那条线，本项目未碰。
- 案例备注 §3 的另外两例（`strategy_revision_batch` 9 / 10 的
  `revision_batch_too_stale_to_resume` 等）形状相同但目标已自行了结，本项目未碰。
- `_LAST_STUCK_CAPTURE` 无淘汰逻辑（量级极小，见上）。
- 丙只在「本轮要发声」的 pass 上判定，所以可释放的 lane 最长多封 30 分钟；若将来觉得慢，
  该调的是这个门槛而不是 fail-closed 的方向。

---

## 部署记录 · 2026-09-26（北京时间 11:15）

- 部署 sha `0041ae06`，**回滚 sha `977c50ab`**。顺序：候选推
  `claude/stuck-deletion-exit-selfheal` → `tg-deploy` → 推 `main`。
- 两项检查各对两种答案测过：`983e4e26..0041ae06` 正确报 FAIL 并列出 6 个代码文件；
  部署后 `main` 对生产报 `PASS: 0 code files beyond production`。
- 本次不涉及 systemd 单元与文件权限，没有人工两步。

### 上线前核实过的那件事

状态文档「上线注意」担心的是：甲改了摘要 → 指纹变 → 首个 tick 每条已超时的
`recovery_required` 退出会新开一行 incident 并发一次通知。**部署前查过生产：
`recovery_required` 为 0 条**（281 succeeded + 91 unbound，后者 `raw_message_id` 为 NULL，
本来就不进这条路径），所以没有任何集中通知，实际观察也是 0 条。

### 复核时另外验证的两点（不是转述子代理的结论）

1. **丙不是死代码。** 释放判据第 3 条要求 lane 内每一张在仓持仓都已归属别的绑定。
   查了生产：当前唯一的持仓 `posId=1001125399244160` 在
   `execution_order_legs` 里有行（leg 655 → binding 383 → 米娅群），**可归属**。
   也就是说如果 310/311 今天还卡着，这条新判据会自动放掉它们——与人工判断一致。
2. **TPSL 触发单不会误堵。** `resting_orders_loader` 走的是 `list_open_orders()`，
   不含止盈止损触发单（那是另一个端点，快照里单列 `tpsl_orders`），所以三张挂在
   米娅持仓上的触发单不会被算成"无主挂单"而否决释放。

### 部署后观察

worker/web/ingest 全部 active，无新异常（`RuntimeIncidentBoundsError` 与
`recognition execution finding` 两条既有噪音除外——前者本次之后应当消失，
因为它只在有卡死退出时才发生）。卡死告警 0 条（本来就没有卡死的退出）。
自动交易开关未动：auto_trade 群仍是 8 个，峰哥 `notify_only`、陈哥 `auto_trade`。

**下一条卡死的退出出现时，才是真正的验证时刻**：预期看到它每 30 分钟说一次而不是每 5 秒，
告警正文带「释放判定」一行，并且在 lane 里没有无主持仓时自行释放。
