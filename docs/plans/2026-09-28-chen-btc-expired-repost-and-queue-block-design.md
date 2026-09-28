# 陈哥 BTC 多单三次未进场：过期重发被吞、队头阻塞、准入死锁 · 分析与修复设计

- 日期：2026-09-28
- 状态：**设计稿，待用户批准**。本文只有分析和设计，没有代码改动。
- 生产 HEAD：`9363a6c8`。它与本文所基于的 `origin/main`（`4ff0d43c`）在 `src/` 下没有差异，所以文中的行号就是生产代码的行号。
- 分析方式：生产库只读点查。所有查询都按索引走，或只扫几千行的小表，没有做 `VACUUM INTO` 快照。
- 本文里所有时间均为 UTC。

## 0. 结论先行

| # | 缺陷 | 后果 | 建议级别 | 与首次分析阶段 3 的关系 |
|---|---|---|---|---|
| 1 | 删除离场卡住，封了 lane（已知） | 09-25 首单一直等到过期，没有进场 | —（L3 已于 09-26 部署） | 无 |
| 2 | 过期未成交的策略被原样重发，上下文层把它判成对过期线程的 `manage_thread`，首轮的「是策略」被抹掉 | 09-28 02:58 的重发没有进场，而且**没有告警** | L2 | 独立先做；阶段 3 须继承这个改动（§5） |
| 3 | 上下文模型返回的目标不在候选集里（契约失败），被当成可重试错误，按群串行，同群后续消息被堵约 10 分钟 | 修正后的新策略晚了约 10 分钟才开始处理 | L2 | 其中「不重问」这一半就是阶段 3 的第 ③ 项，**并入阶段 3**；其余部分独立 |
| 4 | 处理任务已经终态失败，但准入屏障仍把 `mimo_authoritative_failed` 当成「还会重试」 | 修正后的新策略会一直等到 6 小时期限，然后失效 | L2 | 独立，**最先做** |
| 5 | incident 的详细摘要被字段白名单拒绝 | 告警里只剩最小摘要，关键上下文丢失 | L1 | 独立 |

需要用户拍板的问题见 §8。

## 1. 事件经过

陈哥群的 chat id 是 `-1002337721508`。

| 时间 | raw / msg | 内容 | 系统处理 |
|---|---|---|---|
| 09-25 14:07:58 | 19073 / 10758 | BTC 83000-83300 做多，止损 81500，止盈 85800-87000 | 识别为「是策略」。执行结果 `deferred / waiting_source_deletion_exit`：删除离场 310、311 卡住，封了 lane（缺陷 1）。lifecycle 1327（thread 696）在 09-26 01:32 过期。陈哥这单实际成交，报的收益约 1700 点 |
| 09-28 02:58:35 | 19481 / 10789 | 与 10758 **逐字相同** | 首轮是「是策略」，上下文触发原因 `apparent_entry_may_be_revision`。上下文判 `manage_thread → 696`，`management_action=null`。最终结果为非策略，`skipped / no_actionable_intent`，不告警（缺陷 2） |
| 03:13:35 | 19490 / 10791 | 「止盈止损有调整我删了重新发」 | 上下文 7 次调用里有 5 次 `target_outside_candidate_set`。处理任务按 15/30/60/120 s 退避重试，第 5 次失败后于 03:23:38 转入 `failed`。decision 行仍是 `skipped / mimo_authoritative_failed`（缺陷 3、4） |
| 03:13:40 | 19491 / 10792 | 修正后的新策略：止损 81400，止盈 85600-87000 | 在 19490 后面排队，03:25 才被认领。识别为「是策略」，结果 `handed_off_to_batch`。entry_assembly_attempt 34 被 `blocking=[19490]` 挡住，期限到 09:26:20（缺陷 4） |
| 03:14:01 | 19492 / 10793 | 「正常仓位操作300点区间均可入场」 | 非策略，`mimo_no_action`。判断正确，没有重复开仓 |
| 03:37:43 | — | 人工 L3 数据修复（§6） | 03:38:10 attempt 34 变为 ready，03:38:38 被唤醒，03:38:35 市价腿成交 |

## 2. 缺陷 2：过期后原样重发，被判成对过期线程的管理

### 2.1 机制（链条上每一环都有代码依据）

1. **候选集里有过期线程。** `ACTIVE_LIFECYCLE_STATUSES` 包含 `expired`（`strategy_thread_candidates.py:29-31`）。A1 的 72 小时年龄过滤按 `signal_at` 计算（`:368-370`，`:416-446`）。696 的 signal_at 是 09-25 14:07，到 19481 发出时只过了 60.8 小时，所以没有被滤掉。
2. **触发上下文分析。** 候选生成器发现 696 的入场区间和这条新消息完全重叠，打上 `overlapping_entry`。首轮结论是「是策略」，于是命中 `apparent_entry_may_be_revision`（`authoritative_recognition.py:242-250`）。生产上 `context_resolution_gate_json` 记录的也正是这条触发原因。
3. **模型判为 manage_thread。** 提示词（`context_resolution_prompt.py:11-56`）没有说明候选的 `status=expired` 意味着什么。模型的理由原文是「虽然 thread_id 696 已过期，当前消息可视为对该已过期策略的重新发布或确认」。
4. **首轮结论被抹掉。** `_resolved_mimo_result`（`authoritative_recognition.py:582-702`）的逻辑是：只要 `decision != "new_thread"`，就把 `recognition_result` 改写成「非策略」，并把 `strategy` 清空。首轮结果只留在 `_context_resolution.first_pass` 里存档。
5. **没有任何动作，也不告警。** `resolve_management_directive` 在没有匹配到任何意图时兜底返回 `intent="none"`（`management_directives.py:369-379`），得到 `authoritative_lifecycle_not_applied:no_actionable_intent`。这个原因不在 `ALERTED_REASONS` 里（`recognition_failure_attribution.py:114-124`）。
6. **没有终态守卫。** 全链路没有任何地方检查「manage/revise 的目标线程是否已经终态」。

### 2.2 生产范围

在全部 6805 条上下文调用里，同时满足「首轮是策略、上下文判 manage_thread、最终非策略」的**只有 19481 这一条**。

还有约 40 条 `manage_thread → 已终态线程 → 非策略` 的消息，但它们首轮本来就不是策略，大多是「兄弟们，跟上节奏，直接进场」这类跟帖，结果是对的。

所以这不是高频问题。但它的形态很危险：KOL 在老策略过期后原样重发，正是「这次我要进场」的信号，而这种情况会被静默吞掉。

### 2.3 修法（建议 2a + 2b，2c 可选）

- **2a · 触发层。** `apparent_entry_may_be_revision` 计算重叠时，只算 `pending_entry`，也就是还能被改单的策略。`expired` 已经终态，不存在「新策略其实是在改它」这种可能，一条已经放弃的策略没法被修改。
- **2b · 结果层兜底。** 在 `_resolved_mimo_result` 里加一条规则，四个条件**同时**成立时，保留首轮结论，按 `new_thread` 处理：
  - 首轮是「是策略」；
  - 上下文判的是 `manage_thread` 或 `revise_thread`，并且 `management_action` 为 null；
  - 所有目标线程都是 `expired`；
  - `_has_unsettled_exchange_leg` 为假。

  同时记录 `context_override_rejected:terminal_target` 供审计。这样即使将来有别的触发原因把消息送进上下文层，也不会再被吞。
- **2c · 提示词（可选）。** 候选里补一句说明：`expired` 表示已经放弃；原样重发一条已过期的策略，应判为 `new_thread`。提示词改动需要走 prompt 版本流程。有了 2a + 2b 之后，这条不是必需的。

**不做**的事：不把 `expired` 从候选集里整体移除。管理消息仍然需要看到它，比如 19490 的撤销就需要指向它。

## 3. 缺陷 3：目标在候选集之外，失败重试造成队头阻塞

### 3.1 机制

- `parse_context_resolution_decision` 发现目标 id 不在 `allowed_thread_ids` 里，就抛出 `target_outside_candidate_set`（`context_resolution.py:391-396`）。`allowed_thread_ids` 取自候选里所有的 `thread_id` / `strategy_thread_id`（`:944-947`）。
- **19490 的 7 次调用里，696 一直在候选集里**（每行的 `candidate_thread_ids_json` 都包含 696）。5 次被拒的回复里，判定分别是 `revise_thread` 一次、`cancel_thread` 四次，目标数都是 1，但目标 id 不是 696。两次通过的回复原文写的是「策略1327（thread_id 696）」。**所以最可能的情况是模型把 lifecycle id 1327 填进了 `target_thread_ids`。** 这一点无法坐实，因为 `rejected_response_diagnostic_json` 只记了 `target_thread_count`，没有记被拒的是哪个 id。
- 这类失败被当成普通异常。单次上下文调用内最多试 3 次（`context_resolution_worker.py:584`，`:702`），24 小时内最多 5 次（`:55-56`，`:610-617`）。
- 失败会一路传成 `authoritative_failed`，再变成 `AuthoritativeProcessingFailed`（`message_processing_worker.py:283`），处理任务按 15 s 起、翻倍增长的间隔退避，第 5 次转为 `failed`（`:28`，`:42-43`，`:515-524`）。
- 按群串行：认领时取 `ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY raw_message_id)`，只认领 `lane_position = 1` 的那条（`:371-387`）。**正在退避等待的行仍然排在第 1 位**，所以同群后面的消息全部看不到。
- 两次通过的回复都判了 `cancel_thread → 696`，但目标已经过期，所以走 `target_not_verifiable` 无害跳过（`management_target_verification.py:365-370`）。结果成败取决于模型这一次回复得对不对。

### 3.2 生产范围

- 共 29 条消息出现过 `target_outside_candidate_set`。
- 09-18 以来有 7 条的处理任务因此终态失败，每条堵住同群 7 到 13 分钟：17467、17972、18032、18375、18501、18897、19490。
- 这 7 条里有**真实的管理指令**，例如 17972「止损改为2600」、18501「全部仓位止盈出局」、18897「止损统一修改83300」。**所以「目标在候选集外就无害跳过」只对「目标已经终态、交易所上也没有我方仓位或挂单」的情况成立，不能推广到全部。**

### 3.3 修法

- **3a · 可观测（独立，L1）。** `rejected_response_diagnostic_json` 里补上被拒的目标 id，以及它是否恰好等于某个候选的 `lifecycle_id`。这样上面那个推断就能被验证或推翻。
- **3b · 编号纠错（独立，L2）。** 如果被拒的 id 恰好是某个候选线程的 `current_lifecycle_id`，而且只对应这一个线程，就把它映射成那个 thread_id，并记录 `target_id_remapped_from_lifecycle`。前提是 3a 先证实确实存在这种混淆。另一种做法是在提示词里只给出 thread_id、不给 lifecycle_id，这个要和阶段 3 的提示词版本一起评估。
- **3c · 契约失败不重问（并入阶段 3 第 ③ 项）。** 契约类失败是确定性的，重试 5 次、耗时 10 分钟换不来什么。这一项和阶段 3 的「契约类失败不重问」完全重合，**不要单独再做一遍**，由阶段 3 实现；本文只提供上面的生产数据作为它的验收样本。
- **3d · 撤销指向已终态目标时无害结束（独立，L2）。** 同时满足以下条件时，把这条消息落成终态 `skipped / target_terminal_noop`，不算失败，也不重试：
  - 首轮证据是撤销或降风险类（`lifecycle_event.event_type ∈ {cancel_entry, close_signal}`）；
  - 首轮给出的目标 `resolution=exact`，并且该 lifecycle 已经 `expired` 或已离场；
  - 没有未结清的交易所腿。

  19490 就满足这些条件：首轮证据写着 `target_lifecycle_id=1327`，`resolution=exact`。
- **不建议**放开「按群串行」。它保证的是同群消息的因果顺序，比如管理消息必须排在它管理的那条入场之后。放开后可能出现「先撤后入」这种反向执行。队头阻塞应该通过 3c、3d 让失败更快结束来解决，而不是让后面的消息插队。

## 4. 缺陷 4：已经终态的失败，仍被准入屏障当成「还会重试」

### 4.1 机制

- `_NON_TERMINAL_SKIP_REASONS = {"mimo_authoritative_failed"}`（`entry_assembly_admission.py:68`）。`_decision_is_terminal_no_action`（`:122-140`）据此认为这条消息的结论「还可能到来」。
- 19490 的证据里 `lifecycle_event=cancel_entry`，所以 `action_expected=true`；它没有 candidate，decision 又不是终态。于是它被当作 `unresolved`，成了 19491 的挡路消息（`:483-499`）。
- 挡路消息被移除只有一条路径：该消息自己再跑一次 authoritative，并调用 `_run_entry_assembly_wakeups(completed_raw_message_id=…)`（`authoritative_recognition.py:2218-2225`，`entry_assembly_admission.py:894-945`）。**19490 的处理任务已经 `failed`，这次调用永远不会再发生。** 而且 attempt 34 是在 19490 最后一次运行**之后**才创建的，连错过的唤醒都没有机会补上。
- 余下唯一的出口是 6 小时期限（`entry_admission_reconciler.py:156-176`），到期后入场直接失效。
- 在这之前没有任何环节检查「挡路消息的处理任务是否已经终态」。

这是 09-16 相邻入场死锁（A+B+C）留下的一个漏网分支：修复 C 处理了「结论已终态且没有动作」，却把「重试已经耗尽的失败」保留成了非终态。

### 4.2 修法（两处都做，双保险）

- **4a · 写入侧。** `_defer_or_fail_message_processing_job` 在 `terminal=True` 的同一个事务里，做两件事：
  - 如果该消息的 decision 是 `skipped / mimo_authoritative_failed`，改写为 `mimo_authoritative_failed_exhausted`，即本次人工修复用的值；
  - 事务提交后调用 `_run_entry_assembly_wakeups(completed_raw_message_id=该消息)`。
- **4b · 读取侧。** `_decision_is_terminal_no_action` 遇到 `mimo_authoritative_failed` 时，再查一次该消息的 `message_processing_jobs.status`，如果已经是 `failed`，就按终态处理。这是为了覆盖 4a 上线之前已经存在的行，以及任何绕过 4a 的写入路径。
- **4c · 报表同步。** 新值 `mimo_authoritative_failed_exhausted` 必须加进以下几处，否则这类消息会从告警和统计里消失：
  - `recognition_failure_attribution`：`MIMO_AUTHORITATIVE_FAILED` 所在的集合，`:81`，`:94`，`:101`；
  - `oncall_detector.LOSSY_RECOGNITION_REASONS`（`:103`）；
  - `web_queries` 的准入失败集合（`:1656`）。

  本次人工修复后，19490 已经从这三处消失了。它本来就是一条无害的撤销，所以没有实际损失，但代码修复时不能留下这个缺口。
- **4d · 残留数据。** 生产上还有 9 条 2026-08-17 到 09-15 的 `entry_assembly_attempts` 仍是 `pending`（id 3、4、5、6、7、8、11、14、21）。它们的指令项已经全部是 `failed`，不会被唤醒去下单，属于无害残留。是否清理由用户决定，不在本文范围内。

## 5. 与首次分析分类契约阶段 3 的关系

阶段 3 的内容见 `docs/first-pass-classification-status.md:507-528`，以及 `docs/plans/2026-09-24-first-pass-classification-contract-design.md` §5。

- **缺陷 2**：阶段 3 **保留** `apparent_entry_may_be_revision`，并且原样不改。所以阶段 3 上线后，19481 这类消息**照样会被吞**。结论是 2a、2b **独立先做**。阶段 3 改写触发判据时，必须保留 2a 加的「只算 `pending_entry`」这个条件，并把 19481 加进阶段 3 的验收回放：用它重跑，必须得到「是策略 → new_thread」。
- 阶段 3 的第 ②「降级不再抹平首次分析」和 2b 方向一致，但范围更大。2b 是它的一个窄子集，可以先上；等阶段 3 上线后，由第 ② 项吸收 2b。
- **缺陷 3**：3c 就是阶段 3 的第 ③ 项，**并入**，不单独做。3a、3b、3d **独立**。
- **缺陷 4**、**incident 摘要**：和阶段 3 没有交集，**独立**。

## 6. 本次人工 L3 数据修复（2026-09-28，用户在会话中直接批准）

- **改动**：`recognition_decisions` id 19488（对应 raw 19490）的 `automation_reason`，从 `mimo_authoritative_failed` 改为 `mimo_authoritative_failed_exhausted`，`updated_at` 为 `2026-09-28 03:37:43.017907`。
  - 用单条 UPDATE，条件是 `raw_message_id=19490 AND automation_status='skipped' AND automation_reason='mimo_authoritative_failed'`；
  - 在 `BEGIN IMMEDIATE` 事务里执行，`rowcount=1` 后 COMMIT；
  - 只改了这一次。
- **备份**：`/root/evidence-2026-09-28-chen-19490-decision-before.json`（1094 B，sha256 `7888d6195a0deb855e40955713ae96d124ca588122811ea54e58a4f3a93b812c`）。
- **回滚**：把 reason 改回原值即可，但已经下出去的单不会因此撤回。
- **放行过程**：03:38:10 attempt 34 变为 ready，挡路列表清空为 `[]`；03:38:38 被唤醒；指令项 1419 变为 `submitted`。
- **交易所状态（以 WS 推送为准）**：
  - 腿 1：市价多 5 张，成交价 83404.7，pos `1001125406857038`。
  - 腿 2：限价多 5 张 @ 83090，挂单中（`1001125406857169`），自带止损 81400。
  - 止损 81400 已挂，备份止损 81237.2 已挂，止盈 85600 和 87000 已挂。
  - 两腿合计 10 张，约 0.01 BTC，到 81400 的风险约 20 U。
- 这次修复证明了 4a 的写法是可行的：只改 decision 的原因值，正常的对账器在 5 秒这一档内就能自己放行，不需要去动 attempt 或指令项。

## 7. incident 捕获：日志说「failed open」，其实已经落库，只是丢了详细信息

- 日志先后出现 `capture failed open … RuntimeIncidentBoundsError` 和 `detailed summary refused; retrying minimal`。实际发生的是：详细摘要被拒，改用最小摘要**写入成功**。
  - 生产上 09-25 以来有 15 行 `authoritative_recognition_failed` 和 1 行 `protection_adopted_from_exchange`，最近一行是 19490 的 id 2401。
  - 本地用生产代码和 `TELEGRAM_KOL_RUNTIME_INCIDENT_STRICT_CAPTURE=1` 复现，结果一致。
- **越界的是字段白名单**，报错是 `redacted_summary contains unsupported fields`，不是长度，也不是看起来像密钥的启发式判断：
  - `authoritative_recognition_failed`：`failure_point` 不在 `_SUMMARY_FIELDS` 里（`runtime_incidents.py:64` 起）。
  - `protection_adopted_from_exchange`：`instrument_id`、`adopted_order_ids`、`adopted_count` 三个字段不在白名单里。
  - `source_deletion_exit_stuck`：09-25 被拒的那次发生在 L3 之前。按当前代码，被封 lane 的详细摘要校验能通过（`runtime_incident_adapters.py:1350-1361` 的注释记录了当时的修法）。
- **修法（L1）**：把上面 4 个字段加进白名单。每个字段的值都已经过 `_safe_label` 或 `_safe_sentence` 处理，或者本身就是整数，脱敏扫描照常执行。另外补一条测试：用每个适配器的详细摘要分别调用 `_validate_redacted_json_contract`，必须通过。这样以后新加字段忘了登记，会在测试里当场失败，而不是在生产上静默退回最小摘要。日志的措辞也改一下：只有最小摘要也写入失败时，才打 `failed open`。

## 8. 需要用户拍板的问题

1. **缺陷 2 的修法范围**：只做 2a + 2b，还是连 2c（提示词）一起做？建议只做 2a + 2b。
2. **缺陷 3 的 3b（lifecycle id 自动映射成 thread id）**：是先上 3a 观察一周、证实确实存在这种混淆后再做，还是直接做？建议先上 3a。
3. **实施顺序**：建议按 **4（L2）→ 5（L1）→ 2（L2）→ 3a/3d（L1/L2）** 排；3c 跟着阶段 3 走。
4. **残留数据 4d**：那 9 条老的 `pending` attempt 要不要清理？它们无害，建议不动，由阶段 4 收口时统一处理。

## 9. 用户裁定（2026-09-28）

用户经调度会话转达，四条都按本文建议执行：

1. 缺陷 2 只做 2a + 2b，不改提示词（不做 2c）。
2. 3b 暂不做。先上 3a 观察，等证实确实存在 lifecycle id 与 thread id 的混淆之后再议。
3. 实施顺序为 4 → incident 白名单 → 2 → 3a / 3d。3c 并入首次分析四分类阶段 3，本线不做。
4. 那 9 条老的 `pending` attempt 不动。按群串行也不改。

同意开始写代码。交付到「候选 sha + 全量测试通过」为止：不部署，不推 main。部署按 L2 执行，由调度会话排期。
