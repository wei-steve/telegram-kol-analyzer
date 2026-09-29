# 2026-09-29 管理指令预检被拒却冻成 uncertain · 实施状态

设计稿：`docs/plans/2026-09-29-management-preflight-refusal-uncertain-design.md`（已批准，7.1 节裁定 Q1=B、Q2 新增类型、Q3 L3+L2、Q4 例外排除）
状态：**本地实现完成，未部署、未推送、未在生产上跑过任何命令**

## 0. 一句话

给"管理批次在预检阶段就被拒绝、一个字节都没发到交易所"这类消息，加了一条从批次账本本身读出的结构性证明；证明成立时记成 `closed_no_write`（不可自动重试的终态，行为与今天的冻结完全一致），不再写 `uncertain` / `uncertain_without_write`，也不再需要人工 L3 收口；证明不了的，照旧冻结。全过程不改任何交易所写入代码，不改表结构。

## 1. 第 0 步：写前日志审计

审计范围：`strategy_management_executor.py` 里对 Deepcoin 的每一个写方法调用点（`close_exact_position`、`submit_exact_position_sltp`、`cancel_exact_position_sltp`、原生 `cancel_trigger_order`/`cancel_order`），逐个确认调用之前提交了哪一行账本。

| 调用点（函数 / 大致行号） | 写方法 | 调用前已提交的账本 |
|---|---|---|
| `execute_trigger_protection_stop_rescue`（~364-372） | `submit_exact_position_sltp` | `rescue.status="reserved"` + `request_json`，独立事务已提交 |
| `_execute_break_even_by_market_batch` 主体（~1050-1086） | `submit_exact_position_sltp`（协议替换） | `transition_leg(planned→reserved, request=...)` |
| `execute_management_batch` 主关闭循环（~1621-1637） | `close_exact_position` | `transition_leg(planned→reserved, client_order_id=..., request=...)` |
| `execute_management_batch` 保本全平分支（~848-863） | `close_exact_position` | 同上，`transition_leg(planned→reserved, ...)` |
| `_execute_protection_batch` 主体（~2119-2141） | `submit_exact_position_sltp` | `transition_leg({planned|confirmed}→reserved, request=...)` |
| `_cancel_old_protection_after_replacement`（~4142） | `cancel_exact_position_sltp` | 调用发生在同一 leg 已 `reserved` 的段落内（该 leg 在进入本函数前已由调用方转 `reserved`） |
| `_cancel_exact_risk_reduction_protection_before_close`（~2701-2718） | `cancel_exact_position_sltp` | 先写 `ExecutionEvent(action=strategy_management_protection_precancel, status="reserved")` 并提交 |
| `_restore_precancelled_protection_for_rejected_close`（~3002-3028） | `submit_exact_position_sltp` | 先写 `ExecutionEvent(action=strategy_management_protection_restore, status="reserved")` 并提交 |
| `_cancel_deferred_entry_legs`（~4884-4892） | 原生 `cancel_trigger_order` / `cancel_order` | **无**（见下） |

### 发现的例外：`_cancel_deferred_entry_legs`

这是审计唯一发现的写前日志缺口：在调用 `deepcoin_client.cancel_trigger_order` / `cancel_order` 之前，**没有**任何账本写入（该 entry leg 此刻仍是原状态，不是 `reserved`）；leg 状态改成 `cancelled` 以及对应的 `execution_events` 行，都是在调用**返回之后**才写（第 4923-4968 行）。

按裁定 Q4：**不修执行器**，把这一类情况排除在证明之外。具体做法（不是靠时间窗口排除，而是靠证明本身的结构）：

- 该函数在两种正常控制流结局下都会补一条 `execution_events`：调用成功后写 `action=strategy_management_deferred_entry_cancel_diagnostic`（异常路径，第 4894-4921 行的 `except Exception`）或写成功记录（第 4939-4968 行）。
- 本次实现的证明第 6 条要求"该消息名下**没有任何** `execution_events`"。任何一次真正调用过 `_cancel_deferred_entry_legs` 且有实际待撤单匹配的批次，控制流正常完成时都会落下至少一条 `execution_events`，从而被第 6 条天然排除，不需要额外的专门条件。
- 唯一没被这条堵住的场景，是进程在"交易所调用已发出、还没来得及写诊断/成功记录"之间被杀掉（真正的中途崩溃）——这是一个通用的"崩溃在写后确认之前"问题，不是本设计要解决的东西；对应的冻结留在原地（`uncertain`），后续仍走运维收口。
- 结论：证明对"批次曾经走到过 `_cancel_deferred_entry_legs` 且有实际撤单动作"这一类情况**结构性地不成立**（因为条件 6 会失败），无需单独排查时间窗口。

### 指挥会话审阅更正（2026-09-29）

上面「两种正常控制流结局都会补一条 `execution_events`」**不成立**。还有第三种结局：撤单调用**成功返回**之后，
`_deferred_entry_still_matches_snapshot` 复核失败，函数直接 `raise ManagementBatchExecutionError("deferred_entry_cancel_leg_not_pending")`，
**不写任何执行事件**。这时交易所上确实撤过一张单，而批次腿仍是 `planned`、名下没有执行事件——账本证明会误判为「没写过」。

处理（本会话补的提交）：证明只在执行边界的追踪器**一次写入都没记录**时才运行（`authoritative_recognition.py`，
`boundary.evidence_refs` 里有任何 `kind=deepcoin_write` 就直接冻结，不查账本）。整条自动执行链路用的是
`TrackedDeepcoinClient`（`web_app._run_auto_trade_executor`），它拦截 `DEEPCOIN_WRITE_METHODS` 里的每一个方法，
包括 `cancel_order` / `cancel_trigger_order`，所以这条路径上的撤单必定留下追踪记录。两个见证各管一半：
追踪器管「有没有发过写请求」，账本管追踪器看不到的部分；**任何一方单独都不足以判定没写过**。
回放：`tests/test_authoritative_recognition.py::test_ledger_proof_is_never_consulted_after_a_tracked_write`
（修复前失败、修复后通过），`::test_a_proven_preflight_refusal_closes_and_is_never_replayed`（端到端：收口、不抛异常、决定行 completed/failed）。
另更正调用点注释：原注释写「消息仍可再次尝试，跟 failed_safe 一样」，与裁定 Q1=B 相反，已改为「不可自动重试」。

### 救援表 `trigger_protection_stop_rescues` 是否由管理批次路径创建

**不会。** `TriggerProtectionStopRescue` 只在 `strategy_management_planner.plan_trigger_protection_stop_rescue` 里创建（键在 `trigger_protection_intent_id`），这是一条完全独立的入口，不由 `execute_management_batch` 触发；`execute_trigger_protection_stop_rescue`（执行救援）也是独立顶层函数，不在管理批次执行链路里被调用。所以证明第 5 条（救援行检查）在这条路径上永远不会被触发——已在状态文档里记录这个结论，代码里仍保留这条防御性检查（成本几乎为零，且设计稿明确要求）。

## 2. 证明放在哪里执行

`execution_boundary.management_batches_prove_no_exchange_contact(session_factory, raw_message_id=...)`，新增函数，放在 `execution_boundary.py`（与 A-6 的 `_items_prove_no_exchange_contact` 同一文件，同一"边界证据"职责）。

理由：

- `build_execution_boundary_outcome` 本身是纯函数、不查库，保持不变；证明是一个**独立的、需要 DB 会话的辅助函数**，不塞进纯函数里。
- 调用点选在 `authoritative_recognition.py` 的 `if boundary.exchange_effect == "outcome_unknown":` 分支内（原来直接调 `mark_authoritative_execution_uncertain` 的地方），因为这里已经确认了 A-6 自己的证明失败、且 `session_factory` 与 `raw_message_id` 都在作用域内，是修改面最小的插入点。
- fail-closed：函数内部任何读取异常、缺行、意外形状，一律 `return False, ()`（外层 `try/except Exception` 兜底），绝不抛异常。

## 3. 证明成立后的终态（Q1 = B）

新函数 `authoritative_execution_attempts.record_management_preflight_refusal`：

- 尝试行 → `status=closed_no_write`，`exchange_effect="not_started"`，`error_class="ManagementPreflightRefusal"`，`error_summary="<reason_code>:refused_before_write"`，`evidence_refs_json` 写证明（每个批次一条 `{"kind":"management_batch_no_contact","management_batch_id":...,"leg_count":...,"note":"all_legs_planned_no_request"}`）。
- 决定行 → `comparison_status="completed"`、`agreement_status="review_disabled"`、`automation_status="failed"`、`automation_reason=<reason_code>`（与 `record_authoritative_deterministic_refusal` 完全同构的 CAS：只认 `executing` 且 `comparison_status="execution_running"` 且 `comparison_claim_token` 匹配本代际）。
- **不写** `uncertain_without_write` / `authoritative_execution_uncertain`；**不抛** `authoritative_execution_outcome_unknown`。
- `RETRY_BLOCKING_ATTEMPT_STATUSES` 本来就包含 `CLOSEOUT_STATUSES`（`closed_no_write` 在内），**未做任何改动**即已经把这条新终态纳入自动重试 / 上下文重分析的拦截范围（用 R5-h 的测试钉住）。
- `CLOSED_NO_WRITE` 常量处的注释已更新，说明它现在有两类写者：09-26 的运维收口工具（事后审计已沉睡的 `uncertain`）与本模块（在原本要冻结的那一刻，用批次账本的结构性证明代替冻结）；`error_class` 用来区分两者。

`exchange_effect` 记成 `not_started`（与设计稿 3.2 一致），与运维收口对"证据不明"那批历史行采用的语义（`outcome_unknown` 但状态是 `closed_no_write`）不同——这次是**有结构性证明的**"确实没写过"，两者在字段语义上不冲突：运维收口从不覆盖有 `evidence_refs` 的新写路径，`exchange_effect` 这一列对两类写者各自独立地反映各自证明的强弱。

reason_code 的取法：读该消息所有失败管理条目的 `error_json.message`，各自取第一个 `:` 之前的部分（`_management_preflight_refusal_reason_code`，`authoritative_recognition.py`）；多个不同原因码时按字典序拼接（生产两个样本都只有一个条目，属于罕见分支，未见生产样本）。

## 4. 新 incident 类型（Q2）

`management_refused_before_write`：

- 适配器 `runtime_incident_adapters.capture_management_refused_before_write`，severity=`high`；`component=authoritative_execution`、`source_status=closed_no_write`、`reason_code`（经 `_safe_label`）、`operation`（有批次号时 `management_batch_{id}`，否则 `raw_message_{id}`）、`raw_message_id`、`attempt_id`、`impact=management_instruction_withheld_before_exchange_contact`。用到的字段全部已在 `_SUMMARY_FIELDS` 白名单里，**未新增字段**。
- `config.ALWAYS_NOTIFIED_INCIDENT_TYPES` 已加入该类型；**未**加入 `system_operator_bot.NOTIFICATION_BOT_INCIDENT_TYPES`（留在事件处理 bot）。
- `system_operator_bot.INCIDENT_ACTION_HINTS["management_refused_before_write"]` = "这条管理指令在下单前被拒、没有执行；请决定是否手动处理。"
- `source_kind="authoritative_execution_attempt"`、`source_record_id=attempt_id` —— `load_incident_context` 已经原生支持这个 `source_kind`（通过 `AuthoritativeExecutionAttempt.raw_message_id` 反查），无需改动即可让事件通知定位到源消息、群名、仓位上下文。
- 测试：`tests/test_runtime_incident_detailed_summaries.py::_CASES` 已加入 `capture_management_refused_before_write` 用例（生产形状：attempt 4631、raw_message 19598、reason_code `protection_rows_unattributed_on_exchange`、batch 184），静态 AST 遍历测试与详细摘要落盘测试均覆盖到。

## 5. 回放用例

全部位于 `tests/test_management_preflight_refusal.py`（证明 + 终态 + 重试拦截）与 `tests/test_management_preflight_refusal_write_before_log.py`（写前日志守卫）。

修复前 vs 修复后：**修复前的代码没有 `management_batches_prove_no_exchange_contact` 这个函数**，所以下面的"修复前"说明是指"若把新增调用去掉、退回只有 A-6 证明"时的行为——已用直接调用旧路径（`mark_authoritative_execution_uncertain`）的方式在人工审阅时核对过，未额外写一份"关掉新代码路径"的参数化测试（详见第 7 节偏离说明）。

| 用例 | 内容 | 结果 |
|---|---|---|
| R5-a `test_r5a_the_break_even_batch_184_shape_proves_no_contact` | 4631 完整形状：批次 184、腿 158/159 均 planned、无组件/救援/市价决策/执行事件、条目错误 `protection_rows_unattributed_on_exchange:...` | **证明成立**，`refs[0].management_batch_id==184`、`leg_count==2` |
| R5-b `test_r5b_the_partial_close_batch_187_shape_proves_no_contact` | 4705 形状：批次 187、腿 163、`management_stop_provenance_invalid` | **证明成立** |
| R5-c `test_r5c_a_reserved_leg_with_client_order_id_keeps_it_frozen` | 一条腿已 `reserved` 且有 `client_order_id` | **护栏**：证明不成立（A-6 自己的按条目证明在此场景下也早已不成立，因为条目形状本就不同；本用例是新证明自身的正向回归） |
| R5-d `test_r5d_a_component_with_an_attempt_keeps_it_frozen` | 一个组件 `attempt_count=1` | 证明不成立 |
| R5-e（市价决策分支）`test_r5e_a_market_decision_row_keeps_it_frozen` | 存在一条 `strategy_management_market_decisions` 行 | 证明不成立 |
| R5-e（救援分支）`test_r5e_a_non_ready_rescue_for_the_same_position_keeps_it_frozen` | 同一 pos_id 上存在一条非 `ready` 的救援行 | 证明不成立 |
| R5-e（执行事件分支）`test_r5e_an_execution_event_for_the_message_keeps_it_frozen` | 消息名下存在任意一条 `execution_events` | 证明不成立 |
| R5-f（错误类分支）`test_r5f_a_different_error_class_keeps_it_frozen` | 条目错误是 `IntegrityError` | 证明不成立 |
| R5-f（混合条目分支）`test_r5f_a_mixed_entry_item_keeps_it_frozen` | 混有一条 `entry` 类型条目 | 证明不成立 |
| R5-g `test_r5g_a_read_failure_fails_closed` | `session_factory()` 本身抛异常 | 证明不成立（`(False, ())`），不抛异常 |
| （额外正向回归）`test_a_batch_with_no_legs_is_not_proven` / `test_no_management_items_at_all_is_not_proven` | 批次无腿 / 消息无管理条目 | 证明不成立 |
| （终态）`test_the_proven_attempt_closes_no_write_not_uncertain` | 证明成立后调用 `record_management_preflight_refusal` | 尝试行 `closed_no_write`、`error_class=ManagementPreflightRefusal`、决定行 `completed/failed/<reason_code>`，且状态 `!= "uncertain"` |
| （CAS）`test_the_cas_refuses_a_stale_claim_token` | 错误的 `claim_token` | 返回 `False`，attempt 状态不变 |
| R5-h `test_r5h_a_closed_no_write_message_blocks_automatic_retry` | 攻 `attempt.status=closed_no_write` 后调用 `_load_completed_execution_for_automatic_retry` | 无论 `explicitly_retrying` 是 `True` 还是 `False`，均抛 `AutomaticRetryBlocked`（`RETRY_BLOCKING_ATTEMPT_STATUSES` 本就含 `CLOSEOUT_STATUSES`，未改代码） |
| R5-i（关闭腿）`test_close_leg_is_reserved_before_the_exchange_call_raises` | 假客户端 `place_order` 一调用即抛异常 | 两条腿在调用瞬间账本里已是 `reserved` 且带 `client_order_id`/`request`（复用 `_FakeClient`，它在调用时自己查库记录状态） |
| R5-i（TPSL 替换）`test_protection_replace_leg_is_reserved_before_the_exchange_call_raises` | 假客户端 `set_position_sltp` 一调用即抛异常 | 两条腿调用前已 `reserved` 且 `request_json` 非空，随后执行器自己的异常处理把失败腿转 `recovery_required`（这是执行器既有行为，不是本设计改的） |
| R5-i（救援） `test_rescue_is_reserved_before_the_exchange_call_raises` | 假客户端 `set_position_sltp` 一调用即先读库断言 `reserved`+`request_json` 非空，再抛异常 | 断言在调用内部即通过；执行器把异常吞掉、终态化为 `submit_unknown`（不重试，符合既有设计） |

**未覆盖的写路径**（成本原因，已在测试文件顶部注释说明）：`_cancel_old_protection_after_replacement`、`_restore_precancelled_protection_for_rejected_close`、`_cancel_exact_risk_reduction_protection_before_close` 的原生守卫测试。这三处均已在第 1 节的静态审计里确认"调用前已有账本写入"（前两处依赖调用它们的 leg 已处于 `reserved`；后两处各自先写一条 `execution_events(status="reserved")`）。构造覆盖这些分支需要额外的风险削减 / 精算恢复批次场景，性价比低，故只做静态审计、不额外写驱动测试。

## 6. 跑过的测试命令与结果

```
uv run python -B -m pytest -q \
  tests/test_execution_boundary.py \
  tests/test_authoritative_recognition.py \
  tests/test_authoritative_execution_attempts.py \
  tests/test_authoritative_execution_schema.py \
  tests/test_uncertain_attempt_closeout.py \
  tests/test_runtime_incident_detailed_summaries.py \
  tests/test_system_operator_bot.py \
  tests/test_management_preflight_refusal.py \
  tests/test_management_preflight_refusal_write_before_log.py \
  tests/test_strategy_management_executor.py \
  tests/test_recognition_execution_scanner.py
# 524 passed

uv run python -B -m pytest -q \
  tests/test_event_bot_quality_worker.py \
  tests/test_cli_authoritative_recognition.py \
  tests/test_message_processing_worker.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_incidents.py \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_phase5_config.py
# 235 passed

uv run python -B -m pytest -q \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_management_reliability_step5.py \
  tests/test_composite_remainder_market_close.py \
  tests/test_composite_management_fault_injection.py \
  tests/test_management_add_position_rejection.py \
  tests/test_management_directives.py \
  tests/test_strategy_management_planner.py \
  tests/test_trigger_backup_stop_executor.py
# 443 passed
```

全量未跑（按指挥会话职责划分，由指挥会话在最终候选上跑一次）。

## 7. 偏离设计之处

- 证明函数命名为 `management_batches_prove_no_exchange_contact`（模块级公开函数，无前导下划线），设计稿允许"名字可调"；放在 `execution_boundary.py` 而不是新文件，理由见第 2 节。
- reason_code 的多条目合并策略（排序后逗号拼接）是本次新增的细节决定，设计稿未指定；生产两个样本都只有一个条目，这条分支目前没有生产样本验证，是推断而非已验证行为。
- 未额外写"关掉新证明代码路径、确认退回旧行为"的参数化对照测试；改为在实现前手工确认了旧代码在相同输入下确实会走到 `mark_authoritative_execution_uncertain`（读代码 + 复述设计稿第 2.1 节的推导），因为要不改动生产代码本身去做 A/B 对照，成本超过收益。
- 写前日志守卫（R5-i）覆盖关闭腿、TPSL 替换、救援三类（设计稿允许的最低覆盖要求），另外三处仅做静态代码审计，未驱动测试；已在第 5 节列出并说明理由。

## 8. 对生产行为的推断（未用生产数据核对，标注为推断）

- **推断**：若把本次改动部署上线，攻 4631/4705 那批历史"每 1-2 天一条"的模式重复出现时，会直接落地为 `closed_no_write`，不再产生 `uncertain` / `uncertain_without_write` 事件，也不再需要人工 L3 收口——这是基于代码逻辑推出的结论，尚未用生产数据核对（生产上这两条已经在 2026-09-29 10:58Z 被 `close-out-uncertain-attempts` 收口过，不会再自然复现同一形状用于验证）。
- **推断**：`management_refused_before_write` 这个新 incident 类型上线后，第一次真实触发时事件处理 bot 能靠 `load_incident_context` 原生支持的 `source_kind=authoritative_execution_attempt` 正确带出群名/原文/仓位——这一点已用单元测试验证了 `load_incident_context` 的既有代码路径覆盖这个 `source_kind`，但未在真实生产库上跑一次端到端的通知渲染。
- 以上两条建议指挥会话在部署后用生产数据核对（设计稿第 6 节：记录上线后第一个管理预检拒绝样本，核对它落在 `closed_no_write` 且账本证明与事实一致；7 天内没有样本记为待验证）。

## 9. 提交

见指挥会话汇总的 sha 列表（本文件与代码改动在同一批提交中，按显式路径分次提交，未使用 `git add -A`）。
