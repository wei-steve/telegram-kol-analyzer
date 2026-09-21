# 设计文档：`partial_then_break_even` 减仓后保本止损挂不上时，剩余仓位市价全平

日期：2026-09-21。只读调查，未改任何文件。验证等级 L3。

下文路径前缀：`SRC = /Users/steven/Documents/telegram获取消息/src/telegram_kol_research`，`TESTS = /Users/steven/Documents/telegram获取消息/tests`。

## 0. 结论

**推荐方案 A 的受限版本**：在 `replace_remaining_protection` 组件内新增一条"剩余仓位全平"分支。

- 不新增组件种类，不新增任何状态值，不改 schema。
- 兜底路径上任何一步不干净，就退回今天的行为：`operator_required`，原止损原样保留，等人工。
- 方案 B（终止复合批次、另建 `full_exit` 后继批次）复用度更高，但有四处硬伤，不推荐。对比见第 2 节。

调查中另发现三件事，在设计之外但影响验收，列在第 9 节，请指挥先核实：

- 生产监控有一条潜伏的误报。
- 复合路径从不执行合约里的 `cancel_deferred_entries=True`。
- 兜底平仓会被"减仓 intent 尚未对账"挡在第一次尝试上。

## 1. 现状事实（均已读代码核实）

### 1.1 组件状态机与批次

**组件状态**

- 状态机在 `SRC/strategy_management_components.py:15-39`。
- 终态为 `confirmed / operator_required / safely_skipped`。`safely_skipped` 在转移表里不可达。
- 状态列和 `component_kind` 列都没有 CHECK 约束（`SRC/models.py:2263-2335`）。
- 表上唯一的 CHECK 是 `leg_scope`；全库没有 `CREATE TRIGGER`。
- `component.reason_code` 是 String(128)，`batch.reason_code` 是 String(64)，都是自由文本。

**拓扑写死三件套，因此"往合约追加新组件种类"不可行**

- 写死的位置：
  - `create_composite_components_in_session`（`SRC/strategy_management_batches.py:903-911`）
  - `_composite_component_topology_is_exact`（`SRC/strategy_management_composite_executor.py:226-248`）
  - `validate_composite_management_completion`（`SRC/strategy_records.py:159-168`）
  - 合约的 `ALLOWED_COMPONENTS`（`SRC/strategy_management_contracts.py:13-20`）
- 追加种类的后果：旧代码读到未知种类会抛 `unknown required_components`，批次被冻结，回滚不兼容。

**保护组件的预检分支（`composite_executor.py:969-982`）**

- 终态原因集是：
  - `retained_take_profit_exceeds_position`
  - `position_size_increased_after_snapshot`
  - `position_below_target_remaining`
  - `requested_stop_market_side_invalid`
- 命中其中之一 → `operator_required`，上层 `_freeze_composite_batch` 把批次置 `recovery_required`（`:183-188`，`:251-259`）。

**批次互斥**

- 唯一活跃批次索引 `uq_strategy_management_batches_active_strategy` 的谓词是 `status NOT IN ('succeeded','blocked','resolved')`（`models.py:2141-2159`）。

### 1.2 `plan_composite_stop_replacement` 与"不得改松"

代码在 `SRC/strategy_management_market_policy.py:75-129`。

- 它先按 tick 归一：多单 FLOOR，空单 CEILING。
- 市价侧检查（`:96-99`）在读任何已有止损之前执行：多单 `primary >= market`、空单 `primary <= market` 即抛 `requested_stop_market_side_invalid`。
- 通过侧检查之后才计算 `tighter`，并返回 `keep_tighter_stop`。

**互斥性证明（空单；多单对称）**

- `keep_tighter_stop` 要求 `stop <= target` 且 `stop > market`。
- 侧检查失败要求 `target <= market`。
- 两者同时成立会推出 `stop <= target <= market < stop`，矛盾。
- 所以只要存在一张"更紧且仍有效"的已有止损，目标价必然在市价有效一侧，函数返回 `keep_tighter_stop`，永远不抛 `requested_stop_market_side_invalid`。

由此可见，兜底与 `keep_tighter_stop` 在构造上互斥，兜底不可能在"已有更紧有效止损"时触发。

补充一个既有行为：`keep_tighter_stop` 下复合执行器仍会按保留价重挂主备两张、再撤旧单。它不是"零写入"，但不平任何仓。本设计不改这一点。

### 1.3 市价来源

**复合路径**

- 取价：`live_position.get("markPx") or last or lastPx`（`composite_executor.py:956-960`）。
- 来自同一函数内 `:906` 的 `list_positions`。
- 在 worker 的 reconcile 轮内，这次读可能命中轮内读缓存（`ARCHITECTURE.md` 4.6）。陈旧度上限为一轮，实测中位 13.8 秒，被否决的均分配置下到过 32–44 秒。
- 内联流程里它紧跟减仓写入，缓存已作废，是新读。

**`break_even_by_market` 路径**

- 用 `get_ticker_quote` 的 `last`（`SRC/strategy_management_executor.py:601-615`）。
- 该读不进缓存，且与止损的 `slTriggerPxType: "last"`（`composite_executor.py:998`）同口径。

### 1.4 既有全平路径实际做了什么

**`execute_management_batch`（`strategy_management_executor.py:1393-1856`）的顺序**

1. `_require_exact_entry_legs`。
2. `_cancel_deferred_entry_legs`（`:1497`、`:4781-4919`），对未成交挂单入场腿先撤单。
3. `_require_fresh_close_write_boundary`（`:2424`）。
4. 逐腿 `reserved → close_exact_position`：
   - 幂等键：`management:{batch}:{leg}:close:{clOrdId}`
   - 闸门：`exact_position_write_gate`
5. 批次置 `reconciling`。

**关仓前不撤保护单**

- 只有带 `protection_recovery` / `protection_maintenance` 标记的减仓才会预撤（`:2616-2627`）。
- 仓位上的 TPSL 随仓位被交易所作废，依据是 `SRC/protection_retirement.py:1-9` 的实测记录。

**确认与账面终结**

- 确认由只读对账器 `_reconcile_leg` 完成（`strategy_management_reconciliation.py:827-951`）。
- 账面终结由 `_terminalize_full_close`（`:1117-1185`）在同一事务内完成，写入如下：
  - 入场腿：`status = closed`，`terminal_reason = management_full_close_confirmed`
  - 绑定：`closed`
  - 保护账本与保护腿：由 `retire_protection_for_closed_binding` 退役
  - 生命周期：`exited`，`exit_reason="kol_signal"`，`management_action="full_close_confirmed"`
- 仍有其他在仓腿时，绑定保持 `active` 并返回 False。

**`break_even_by_market` 的 `full_exit` 分支**

- 每仓位的决策先经 `reserve_break_even_market_decision` 落库，重启后复用同一决策，不重新判断。
- 有任一仓位走 `full_exit` 时先 `_cancel_deferred_entry_legs`（`:809-821`）。
- 每个 `full_exit` 腿按 `preflight_size` 市价全平（`:824-986`）。
- 账面终结由 `_terminalize_selected_market_close_legs`（`reconciliation.py:752-824`）完成，同样写 `exit_reason="kol_signal"`。

### 1.5 三个约束设计的事实

**手动平仓扫描**

- 位置：`SRC/execution_bindings.py:4263-4480`。
- 处于 `executing / recovery_required` 等状态的批次，其仓位算"管理保留"，扫描会跳过（`:68-78`、`:4639-4657`）。
- 批次一旦进入 `succeeded`，保留即释放。
- 此时若账面没有在同一事务内终结，约 90 秒内该仓位会被记成：
  - 绑定 `manual_closed_or_not_found_on_exchange`
  - 生命周期 `exit_reason="manual"`
  - 并发出一条"被标记关闭"的事故
- 所以账面终结必须与批次置 `succeeded` 原子完成。

**值守探测器**

- 位置：`SRC/oncall_detector.py`。
- 批次 `succeeded` 或 `resolved` 即清案（`:975-983`）。
- 批次处于 `blocked / partial_failed / recovery_required / submit_unknown` 会告警（`:67-69`）。
- 指令项 `succeeded` 且 `result.status` 不是 `skipped / shadow_planned` 即清案（`:818-827`）。
- 指令项 `failed` 或 `unknown` 告警。
- 案件键用的是 `intent`。该处的注释写明：2026-09-20 曾因按 `effective_action` 取键，把一条消息拆成两个案件。

**生产安全监控**

- 位置：`SRC/production_safety_monitor.py:2398-2442`。
- 规则：凡 `replace_remaining_protection` 组件为 `confirmed`，或所在批次为 `succeeded`，就要求该仓位的账本里 `stop_loss` 和 `backup_stop` 都是 `verified`；否则报 `composite_position_without_verified_stop`（critical）。
- 这条检查不看仓位是否还在，也没有时间窗。

## 2. A 与 B 的对比

| 维度 | A：组件内新分支 | B：父批次终结 + `full_exit` 后继 |
|---|---|---|
| 先例 | 复用 `converge_partial_close` 已在用的 `PositionMutationGateway.close_exact_position` + `before_submit` + 只读对账（`composite_executor.py:739-841`，`composite_reconciliation.py:167-211`） | 有现成先例 `create_race_resolved_successor_batch`（`batches.py:534-592`；worker `:1153-1283`）：同一事务里父批次置 `resolved`、建 `full_exit` 后继；指纹由 kind、父批次 id、父 target 指纹和 pos ids 构成；`target_snapshot["…_successor_of"]` 记因果 |
| 复用全平机器 | 撤挂单入场腿要调用执行器的 `_cancel_deferred_entry_legs`；账面终结调用 `_terminalize_full_close`。都是直接调用，不复制代码 | 整条路径免费得到，包括撤挂单竞态后继和 restart 校验 |
| "成功"的含义 | 一条指令对应一个批次；剩余仓位经交易所确认已平，之后才 `succeeded` | 受唯一活跃索引限制，父批次必须先终结才能建后继。于是父批次在剩余仓位真正平掉之前就已是终态 |
| 指令项 / 合约投影 | 与普通复合成功完全同路 | 适配器里有两个条件相互抵触，B 两头都满足不了（见表后的推导） |
| 值守案件键 | 单一键 `(raw, partial_then_break_even)` | 后继一旦失败，会产生第二个键 `(raw, full_exit)`，正是 09-20 修掉的那种拆案 |
| 多仓位批次 | 每腿各自判定，与 `break_even_by_market` 的逐仓决策同构 | 父批次里其余腿的组件停在 `pending`。状态机不允许 `pending→confirmed`，只能伪造转移链 |
| 新代码位置 | 复合执行器 + 复合对账器，属于最敏感的模块，是 A 的真实代价 | `batches.py`、复合执行器，外加必须动的 `instruction_execution_management_adapter`（1254 行合约逻辑） |
| 回滚 | 见第 7 节；零在途即干净 | 后继是普通 `full_exit` 批次，旧代码可读；但父批次的组件状态旧代码同样读不顺 |

**B 在"指令项 / 合约投影"一格里的推导**

- 后继的 `intent` 必须是 `full_exit`。理由：
  - 如果沿用 `partial_then_break_even`，`_transition_management_evidence` 会因终态类型不同而抛 `management_terminal_kind_contradiction`（`adapter.py:434-446`）。
  - 父批次得到 `verified_management`，后继得到 `verified_exit`，两者冲突。
- 后继用 `full_exit` 之后，`project_linked_management_batch_contract` 按 `management_action == batch.intent` 过滤（`:801`），后继无法关联到指令项。
- 父批次要投影成 `verified`，要求所有组件状态都落在成功集或确定失败集内（`:344-354`）。
  - 组件状态里只有 `confirmed` 满足。
  - 于是不得不把一个什么保护都没换的组件写成 `confirmed`。
  - 如果留 `operator_required`，投影结果是 `submit_unknown`，值守必然告警。

**结论：选 A。** B 的"复用"优势被适配器和多腿问题抵消，而且它违反"交易所确认之后才算成功"的语义。A 的代价是在敏感模块里新增分支。本设计用"兜底路径上任何不干净 → 退回今天的行为"把这份新增风险限制住。

## 3. 推荐设计（方案 A）

### 3.1 触发条件（全部满足才进入兜底）

1. 预检异常的原因恰好等于 `requested_stop_market_side_invalid`。
2. `contract.stop_mode == "actual_entry_price"`，即保本语义。
   - 包括 `explicit_tighter_adopted`：那种情况下合约已被中和，目标价从 `break_even_target_price(leg)` 取。
   - `explicit_price` 合约保持 `operator_required`。81fdc58a 之后新批次不应再出现这种合约；是否要覆盖它，见第 10 节问题 3。
3. `desired` 里没有 `protection_replacement_execution`，即本组件从未开始挂新止损。
   - 如果此前某次尝试已经挂过新止损，保持 `operator_required`，不变。
4. 前置 `partial_close_component_not_converged` 检查已通过（`composite_executor.py:912-919`，既有逻辑）。
   - 因此剩余仓位大小等于 `target_remaining_size`。
5. `live_execution_gate()` 为真。
   - 这就是减仓用的同一道 `effective_composite_management_v2_mode == "live"` 闸。
   - 不读不写任何开关。
   - 为假时转 `recovery_required / live_execution_disabled`。
6. **新鲜报价二次确认**：
   - 调 `deepcoin_client.get_ticker_quote(inst_id=…)`。
   - 校验项与 `executor.py:607-615` 逐字相同：
     - `instrument_id` 相符
     - `price` 非空
     - `price_field ∈ {last, lastPx}`
   - 用 `market_price=quote["price"]`、`existing_stop_prices=()` 再调一次 `plan_composite_stop_replacement`。
     - 再次抛 `requested_stop_market_side_invalid` → 确认越过，进入兜底。
     - 正常返回（与仓位行价格不一致）→ `preflighting → recovery_required`，原因 `break_even_market_side_disagreement`；下一次尝试重新预检，大概率正常挂止损。
     - 报价不可用或不合法 → `recovery_required / break_even_market_quote_unavailable`；重试耗尽后走既有的 `protection_replacement_retry_exhausted`，等于今天的结局。
   - 这样处置陈旧度：仓位行价格最多旧一轮，决定平仓的那个价永远是一次不进缓存的物理读，口径与止损触发类型一致。`market_policy.py` 一行不改。

### 3.2 执行步骤

在 `execute_protection_replacement_component` 的 `except` 分支里，只对 3.1 成立的情形调用新函数 `_execute_remainder_close(...)`。其余原因保持原样。

**步骤 1：落决策、进 `submitting`**

- 新增 `_persist_remainder_close_plan_and_enter_submitting`，仿 `:1358-1382`。
- 写入 `desired["remainder_close_execution"]`，字段如下：
  - `reason`
  - `requested_stop`
  - `primary_stop`
  - `position_market_price`
  - `ticker_last`
  - `ticker_field`
  - `decided_at`
  - `phase: "cancel_deferred_entries"`
  - `deferred_entries_cancelled: false`
  - `intent_ids: []`
- 转移：`preflighting → submitting`。
- 这一步先于任何交易所写入落库，与 `reserve_break_even_market_decision` 同一原则。
- 此后决策有粘性：重试时不再重新判断市价侧，与按市价保本先例一致。

**步骤 2：撤未成交入场腿**

- 调用：
  - `load_management_batch`
  - `_load_exact_binding`
  - `_cancel_deferred_entry_legs(session_factory, batch=record, binding=…, deepcoin_client=…, cancelled_at=now)`
- 复合批次的 `target_snapshot.identity.deferred_entry_leg_ids` 由计划器统一写入（`planner.py:1277`），该函数可以直接使用。
- 没有挂单腿时，该函数零读零写（`:4606-4607`）。
- 任一异常都转 `submitting → operator_required`，原因 `remainder_close_deferred_entry_cancel_failed`。异常包括：
  - 身份漂移
  - 挂单腿已成交
  - 撤单被拒
  - 结果未知
- 此时未发任何平仓，原止损原样，与今天等价。
- 成功后在 `desired` 里写 `deferred_entries_cancelled: true`、`phase: "close"`。

**步骤 3：对账未决 intent**

- 取 `_exchange_snapshot`，调 `reconcile_submitted_position_mutation_intents`。
- 原因：
  - 减仓组件是靠仓位数量收敛而 `confirmed` 的。
  - 它的 close intent 往往还停在 `submitted`。
  - 网关的 `_has_other_unresolved_close` 会把新的平仓直接 `_block`（`gateway.py:363-368`、`:425-439`）。
- 对账后减仓 intent 仍未 `confirmed` 时，转 `submitting → recovery_required`，原因 `remainder_close_waiting_partial_close_confirmation`。5 秒后的 worker tick 会重试。

**步骤 4：发平仓**

- 先重建 authority：
  - 输入：新读的 `list_positions` + `build_position_mutation_authority`。
  - 失败时转 `recovery_required`，原因取 `str(exc)`。
- 调 `PositionMutationGateway.close_exact_position(...)`：
  - `size = str(live_position["pos"])`
  - `client_order_id = f"CM{batch.id}L{leg.id}R{attempt}"`
    - 用 `R` 后缀，避开减仓用的 `…A{n}`；网关在缺 ordId 时按 clOrdId 对账，两者不能撞。
    - 长度不超过 20。
  - `idempotency_key = f"{component.id}:close:remainder:attempt:{attempt}"`
    - 保留 `:close:` 片段，让监控的 `duplicate_composite_close_submission`（`monitor:2383-2395`）自动覆盖这笔新写入。
    - 前缀 `{component.id}:` 让 `_reconcile_protection_component` 的 LIKE 查询能取到它。
  - `before_submit` 把 `intent_id`、`pre_submit_size`、`client_order_id` 追加进 `desired`。
- 网关原有保障不变：
  - 所有权闸 `_load_verified_binding`
  - 仓位指纹复核
  - `reserved → submitting → submitted`
  - "未知即 `recovery_required`，绝不重发"

**步骤 5：结果处置**

与减仓组件 `:764-841` 同构。

| 网关返回 | 处置 |
|---|---|
| `submitted`，且再读 `list_positions` 看到该 posId 不存在或 `pos == 0` | `submitting → confirmed` |
| `submitted`，但仓位仍在 | `awaiting_exchange / remainder_close_not_yet_flat` |
| `recovery_required` | `awaiting_exchange / remainder_close_outcome_unknown` |
| `rejected` | `recovery_required / remainder_close_definitely_rejected` |
| 其它（含 `blocked`） | `recovery_required`，原因取 `result.reason` |

`confirmed` 时写入的 evidence：

- `outcome: "remainder_closed_at_market"`
- `close_intent_id`
- `remaining_size: "0"`
- `requested_stop`
- `primary_stop`
- `position_market_price`
- `ticker_last`
- `cancelled_deferred_entry_leg_ids`
- `evidence_tier: "exact_position_absent_after_accepted_close"`

**步骤 6：重试入口**

- 预检开头如果发现 `desired` 里已有 `remainder_close_execution`，直接进 `_execute_remainder_close`。必须放在 `target_live_position_not_unique` 那一行之前判断。
- 仓位已不存在时：
  - 最后一个 close intent 是 `confirmed` → `confirmed`
  - 否则 → `operator_required / remainder_position_absent_without_confirmed_close`
- 仓位仍在时：
  - `deferred_entries_cancelled` 为真 → 跳过步骤 2，从步骤 3 续。
  - `phase` 还停在撤挂单、又没有 `cancelled` 标记 → `operator_required / remainder_close_interrupted_before_close`。

### 3.3 对账器

文件：`SRC/strategy_management_composite_reconciliation.py`。

`_reconcile_protection_component` 开头新增判断：`desired` 里有 `remainder_close_execution` 就走新函数 `_reconcile_remainder_close_component`。该函数只读交易所，只写本地状态。

| 情形 | 处置 |
|---|---|
| 没有任何 close intent，组件停在 `submitting`（崩在撤挂单阶段） | `operator_required / remainder_close_interrupted_before_close` |
| intent `reserved` | 置 `blocked`；组件 `recovery_required / remainder_close_reserved_before_write`（仿 `:178-188`） |
| intent 为 `submitting`、`submitted` 或 `recovery_required` | `awaiting`（绝不重发） |
| intent `confirmed`，且仓位不存在或 `pos == 0` | `confirmed`，evidence 同上，`evidence_tier: "exact_close_intent_confirmed"` |
| intent `confirmed`，但仍有残量 | `recovery_required / remainder_close_confirmed_with_residual`（下一次尝试按现量再平） |
| intent 为 `rejected` 或 `blocked`，仓位仍在 | `recovery_required / remainder_close_terminal_without_fill` |
| intent 为 `rejected` 或 `blocked`，仓位已不在 | `operator_required / remainder_position_absent_without_confirmed_close` |

用到的转移都在 `ALLOWED_COMPONENT_TRANSITIONS` 内，不需要动状态机。

### 3.4 批次完成与账面终结

位置：`_complete_composite_batch`，`composite_executor.py:262-333`。

- 扫描各腿保护组件的 evidence，把带 `outcome == "remainder_closed_at_market"` 的腿收为 `closed_legs`。
- `closed_legs` 非空时，在同一个 session 里、先于 `batch.status = "succeeded"`，做两件事：
  - 校验前置（同 `_identity_is_exact` 的核心条件）：
    - 生命周期 `entered`
    - `exit_reason is None`
    - 绑定处于 `open / active / stale`
  - 不满足就抛 `RuntimeError("composite_remainder_terminalization_identity_mismatch")`。现有 `except` 会把批次冻结成 `recovery_required`，事务回滚。
- 校验通过后调用 `_terminalize_full_close(session, batch=batch, legs=closed_leg_rows, now=completed_at)`。
  - 从 reconciliation 导入，没有循环依赖。
  - 写入如下：
    - 入场腿 `closed`
    - 保护账本与保护腿退役
    - 绑定 `closed`（还有其他在仓腿时保持 `active`）
    - 生命周期 `exited`，`exit_reason="kol_signal"`，`management_action="full_close_confirmed"`
  - 这与 `break_even_by_market` 的 `full_exit` 分支完全一致。
- 这一步与批次置 `succeeded` 同事务，因此手动平仓扫描永远看不到"仓位已平但账面未终结"的窗口（见 1.5）。

`batch.reason_code` 的取值：

- 有 `closed_legs` → `composite_remainder_market_closed`。这是新的自由文本值；库里没有任何地方按旧值 `composite_management_exchange_confirmed` 取键（已 grep）。
- 没有 → 保持原值。

完成通知的 summary 改写，仍走 `persist_composite_management_completion_in_session`，仍是成功类通知：

- `partial_close`: "剩余 0（保本价 X 已被市价 Y 越过，剩余仓位已市价全平）"
- `protection`: "未挂保本止损：仓位已全平，原保护单随仓位失效"

### 3.5 保护单清理

- 与既有 `full_exit` 完全一致：关仓前不撤任何止损或止盈。
  - 先撤会制造裸仓窗口。
  - 撤前回读逻辑因此一行不碰。
- 交易所侧，仓位 TPSL 随仓位作废。本地由 `retire_protection_for_closed_binding` 退役。
- 未成交入场腿上自带的止损随入场单撤销而消失。

### 3.6 监控

位置：`SRC/production_safety_monitor.py:2435-2442`，这处修改是必需的。

- 在 `replace_remaining_protection` 分支里解析该组件的 `evidence_json`。
- 任一条目带 `outcome == "remainder_closed_at_market"` 就 `continue`。
- 否则批次一成功、账本一退役，下一轮监控就会报 critical 的 `composite_position_without_verified_stop`。

### 3.7 事故 / 通知

- **成功通知**：3.4 的复合完成通知，走 operator bot。用户不在线也能事后看到"已全平"。
- **事故台账**：
  - 新类型 `composite_break_even_remainder_closed`，severity 为 `low`，不进任何 Telegram 类型表。
  - 仿 `management_price_plausibility._record_price_finding_incident`（`:415-485`）。
  - best-effort：记录失败不影响交易。
  - summary 只用 `_SUMMARY_FIELDS` 里的键：`component`、`reason_code`、`raw_message_id`、`impact`、`operation`。
  - 数字放进 `impact`。
  - 注意 `_OPAQUE_VALUE_PATTERN`：32 个以上连续的 `[A-Za-z0-9_+/=-]` 会被当成不透明值，拼字符串时要避开。
- **会告警的失败**：兜底路径上所有 `operator_required` 都经 `_freeze_composite_batch` 把批次置 `recovery_required`，值守 D2 照常告警。
- **值守读到的终态**：干净路径上批次 `succeeded`；指令项在内联路径为 `succeeded`（`auto_trade_execution.py:2033-2046`）。两者都清案。

## 4. 重启 / 崩溃安全

| 崩溃点 | 持久状态 | 恢复 |
|---|---|---|
| 减仓已确认、保护组件尚未认领 | 组件 `pending` | 正常执行，重新做新鲜判定 |
| 决策落库之前 | `preflighting` | 5 分钟 stale 之后重新认领（既有机制），重新判定 |
| 决策已落、撤挂单途中 | `submitting`，没有 intent，`phase` 还在撤挂单 | 对账器转 `operator_required / remainder_close_interrupted_before_close`。与既有全平路径在同一窗口的处置 `management_close_not_reserved_after_deferred_cancel` 同级。原止损仍在 |
| intent 为 `reserved` | 确证未发出 | 置 `blocked` → `recovery_required` → 用新的 attempt 键重发 |
| intent 为 `submitting`、`submitted` 或未知 | `awaiting_exchange` | 只读对账，按 ordId / clOrdId 的成交证据转 `confirmed` 或 `rejected`；绝不重发。`_has_other_unresolved_close` 兜底防双平 |
| 组件已 `confirmed`、批次还没完成 | 批次 `executing` | 下一 tick 进 `_complete_composite_batch`。终结与 `succeeded` 原子完成；期间仓位受管理保留，扫描不碰 |

- **尝试上限**：沿用 `attempt_count >= 3` → `protection_replacement_retry_exhausted`，不改。
- **崩溃期间的保护**：全程原始止损仍在交易所武装着，整条兜底路径不撤任何保护单。

## 5. 必须保持不变的其它去向

**`operator_required`**

- `protection_replacement_retry_exhausted`（`:867-880`）
- 预检终态：
  - `retained_take_profit_exceeds_position`
  - `position_size_increased_after_snapshot`
  - `position_below_target_remaining`
- `requested_stop_market_side_invalid` 在不满足 3.1 的情形下，包括：
  - `explicit_price` 合约
  - 已经开始挂新止损
- `duplicate_new_stop_order_id`（`:1021-1027`）
- 终检不变量（`:1168-1173`）：
  - `target_live_position_not_unique`
  - `replacement_stop_ownership_incomplete`
  - `retained_take_profit_*`
  - 账本持久化异常
- `_load_component` 的返回（`:1191-1243`）：
  - `management_component_identity_mismatch`
  - `management_component_kind_mismatch`
  - `management_instruction_component_dropped`
  - `management_component_contract_invalid`
  - `take_profit_order_identity_conflict`

**`recovery_required`**

- `composite_predecessor_not_confirmed`（`:887-891`）
- 预检非终态：
  - `positions_snapshot_incomplete`
  - `target_live_position_not_unique`
  - `partial_close_component_not_converged`
  - `management_size_invalid`
  - `target_remaining_delta_not_executable`
  - `break_even_side_invalid`
  - `requested_stop_invalid`
  - `break_even_market_price_invalid`
  - `price_tick_invalid`
  - `break_even_existing_stop_invalid`
  - `retained_take_profit_size_invalid`
  - `retained_take_profit_owner_conflict`
- `old_stop_cancel_unresolved`（`:1073-1083`）
- 对账器的 `protection_replacement_safe_to_resume`

**`awaiting_exchange`**

- `replacement_stop_readback_unresolved`
- `old_stop_cancel_unresolved`
- `old_stop_cancel_pending`

**批次级**

- `target_contract_spec_unavailable`
- 拓扑类原因
- 完成校验异常

以上全部不变。另外两个组件的全部原因码也不变。

## 6. 改动文件清单

1. `SRC/strategy_management_composite_executor.py`
   - `except` 分支做分流。
   - 新增：
     - `_break_even_fallback_applies`
     - `_confirm_market_side_invalid`
     - `_execute_remainder_close`
     - `_persist_remainder_close_plan_and_enter_submitting`
     - `_append_remainder_close_intent`
   - 预检开头加续跑入口。
   - 改写 `_complete_composite_batch`。
2. `SRC/strategy_management_composite_reconciliation.py`
   - 新增 `_reconcile_remainder_close_component` 及其分派。
3. `SRC/production_safety_monitor.py`
   - 3.6 的跳过。
4. 新增一个小函数记录 low 事故。
   - 放在复合执行器内，或新建一个小模块。
5. 文档
   - `docs/plans/2026-09-21-break-even-strategy-price-spec.md` 的 3.5 和 7.1
   - `docs/break-even-strategy-price-status.md`
   - `docs/ARCHITECTURE.md` 的 4.8 补一句

**不改的：**

- `market_policy.py`
- `position_mutation_gateway.py`
- `strategy_management_components.py`
- `contracts.py`
- `batches.py`
- 计划器
- 适配器
- 任何设置或开关
- schema

## 7. L3：变更与回滚

- **变更语义**：只新增一种减风险写入，即对已验证归属的精确仓位市价全平；外加撤未成交入场单。
  - 没有任何路径能开仓或加仓。
  - 不撤也不改任何止损，因此不可能放宽止损。
- **部署**：
  - `tg-deploy <sha>`。
  - 要求零在途：管理批次、mutation intent、claimed job、worker command 均为 0。
  - 自动交易相关开关全程不读不写。
- **回滚**：`tg-deploy <部署前生产 sha>`，当前为 `81fdc58a…`。
- **新增持久状态对旧代码的可读性**：
  - 可读，无害：
    - `desired_json` 里的未知键被忽略。
    - 原因码、批次 `reason_code`、事故类型都是自由文本。
    - close intent 的 `operation` 是既有的 `close_position`，旧对账函数能处理。
    - 生命周期和绑定写入的都是既有取值。
  - 在途批次跨版本（零在途即可避免）：
    - 旧对账器见不到 `protection_replacement_execution`，会永久返回 `awaiting`。
    - 15 分钟后监控报 `stalled_composite_component`。
    - 只是告警，没有任何写入。
  - 回滚后仍会吵的一点：
    - 旧监控会对已经走过兜底的批次报 `composite_position_without_verified_stop`。
    - 只告警，无交易影响。
    - 按第 9 节第 1 条的分析，这与 A-17 之后任何普通复合成功批次在仓位离场后的表现同类，不是新的状态类别。

## 8. 测试计划

**纯函数 / 单元**

- 3.1 六个条件逐一取反，每个取反都保持旧行为：
  - `explicit_price` 合约 → `operator_required`
  - 已有 `protection_replacement_execution` → `operator_required`
  - 闸门关闭
  - ticker 与仓位行不一致 → `recovery_required`，零写入
  - ticker 不可用或字段非法 → `recovery_required`
- 已有更紧有效止损 + 目标价可挂 → 走 `keep_tighter_stop`，零平仓，用来钉住 1.2 的互斥性证明。

**组件级（沿用 `_prepare_composite_protection_component`，`TESTS/test_strategy_management_executor.py:6930`）**

- 改写 `test_composite_protection_operator_required_when_the_reference_is_passed`（`:7133`）：
  - 断言 `confirmed`
  - 事件序列里没有 set-sltp、没有 cancel-sltp、恰好一次 close
  - `closePosId` 与数量正确
  - 幂等键与 clOrdId 格式正确
- 网关各种返回：`recovery_required`、`rejected`、`blocked`。
- 减仓 intent 仍是 `submitted` 的情形：
  - 第一次尝试停在 `recovery_required / remainder_close_waiting_partial_close_confirmation`
  - 对账确认后第二次尝试成功
- 撤挂单的分支：
  - 无挂单腿（零读）
  - 有一条且撤成功
  - 挂单腿已成交 → `operator_required`，零平仓
  - 撤单抛异常 → `operator_required`，零平仓

**对账器**

- 3.3 表格逐行。
- 断言对账器零交易所写入。

**批次级（`execute_composite_management_batch` 全流程）——生产形状**

- 空单：
  - 区间 80500–81600
  - 仅腿 1 成交：`avg_entry_price=80436`，`break_even_reference=80500 / strategy_first_leg`
  - 数量 6，减 50%
  - 腿 2 限价 81510 挂单中
  - 仓位行 markPx 80600，ticker last 80600
- 断言：
  - 减仓 3
  - 撤腿 2
  - 市价平 3
  - 批次 `succeeded / composite_remainder_market_closed`
  - 入场腿 `closed`，腿 2 `cancelled`
  - 绑定 `closed`，账本全部 `retired`
  - 生命周期 `exited / kol_signal`
  - 成功通知文案
  - low 事故一条
  - 原止损 82300 从未被撤
- 多单镜像：
  - 区间 64000–65000，目标价 65000，市价 64900
- 变体：
  - 市价 80450 → 正常挂 80500，回归旧路径
  - 双腿批次一腿挂止损、一腿全平 → 绑定保持 `active`，生命周期仍 `entered`

**崩溃注入（`TESTS/test_composite_management_fault_injection.py`）**

- 第 4 节表格逐行。

**监控**

- 带 `outcome` 标记的组件不报 `composite_position_without_verified_stop`。
- 旧用例 `TESTS/test_production_safety_monitor.py:2078` 保持原样通过。

**值守与适配器**

- `oncall_detector` 对该批次和指令项清案。
- 适配器对新路径的投影，与普通复合成功的投影逐字段相同。

**收尾**

- 既有复合、按市价保本、网关、对账、`test_position_authority_boundary_coverage` 全部保持通过。
- 最终候选跑一次全量 `uv run python -m pytest -q`。
- **首笔实盘样本核对**：
  - 减仓量正确
  - 腿 2 已撤
  - 平仓量等于剩余量
  - 没有任何新止损单
  - 账面四处终态正确
  - 通知文案正确

## 9. 设计之外的发现

以下均为读代码所得，没有对照生产数据。

1. **监控潜伏误报。**
   - A-17 之后，绑定关闭会把账本行置 `retired`。
   - `monitor:2435-2442` 对所有历史 `confirmed` 的保护组件都要求两张 `verified` 止损。
   - 因此任何普通的复合成功批次，只要它的仓位之后离场，就会永久报 critical。
   - 今天 R2 上线后，复合成功会变多。
   - 建议的只读核实步骤：
     1. 查 `strategy_management_components` 中 `component_kind='replace_remaining_protection' AND status='confirmed'` 的行。
     2. 查这些行对应绑定的状态。
     3. 查对应账本行的状态。
   - 根治方案：有实时仓位数量、且该 posId 不在仓时跳过这条检查。可与 3.6 一起做，也可分开。

2. **复合路径从不撤未成交入场腿。**
   - 合约恒为 `cancel_deferred_entries=True`（`management_directives.py:481-486`）。
   - 复合执行器里没有任何相关代码。
   - 而非复合的减仓路径无条件调用 `_cancel_deferred_entry_legs`（`executor.py:1497`）。
   - 17813 那样的场景下，减仓后腿 2 的 81510 限价单仍会挂着。
   - 本设计只在兜底全平前撤，正常路径不动。

3. **兜底平仓第一次尝试会被减仓 intent 挡住。**
   - 减仓组件 `confirmed` 之后，它的 close intent 常停在 `submitted`，见 3.2 步骤 3。
   - 没有步骤 3 的话，兜底平仓在内联路径上几乎必然被网关 `blocked`。
   - 实施者务必先为它写用例。

## 10. 风险与待指挥裁决

1. **二次报价确认（3.1 第 6 条）。**
   - 保留：更稳，多一次 GET；两个价格不一致时晚一个 tick。
   - 去掉：直接信任仓位行的价格。
   - 我建议保留。
2. **决策粘性。**
   - 采用之后，即使重试时价格已回到可挂一侧，仍继续全平。
   - 这与按市价保本的先例一致，也符合用户"有离场意愿就跟"的原则。
   - 是否接受？
3. **`explicit_price` 复合合约**是否也要兜底全平？我建议不要，保持人工处理。
4. **撤挂单失败 / 挂单腿刚成交**时，v1 转人工。
   - 是否改成"仍然平掉本腿剩余、绑定保持 `active`"？
5. **`remainder_position_absent_without_confirmed_close`**（疑似原止损或人工先平掉了）在 v1 转人工。
   - 交给 `expire_stuck_management_recoveries` 和手动平仓扫描收尾，是否可以？
6. **3 次尝试上限与挂止损共用。**
   - 内联第 1 次常因问题 9.3 被消耗在等待上。
   - 是否需要给兜底单独计数？我建议先不动，用样本观察。
7. **`exit_reason="kol_signal"`** 是否就是想要的 PnL 归因口径？它与按市价保本的全平一致。
8. **市价单无滑点上限**，与既有 `full_exit` 相同，不新增风险，但请知悉。
9. **指令合约适配器的终态判定。**
   - 生产 `instruction_execution_contract_mode=shadow`。
   - 复合批次的管理腿状态恒为 `planned`，适配器对 `succeeded` 批次要求产物状态都在终态集（`adapter.py:344-354`）。
   - 这是普通复合成功的既有表现，本设计不改变它。
   - 切到 live 之前需要单独处理。
10. **`break_even_convergence`（TP1 自动保本）**不在本次范围。

### Critical Files for Implementation
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_composite_executor.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_composite_reconciliation.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_executor.py（只调用 `_cancel_deferred_entry_legs` 和 `_load_exact_binding`，不改）
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_reconciliation.py（只调用 `_terminalize_full_close`，不改）
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/production_safety_monitor.py