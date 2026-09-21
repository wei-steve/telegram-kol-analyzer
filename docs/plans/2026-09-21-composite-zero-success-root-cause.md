# 复合指令 `partial_then_break_even` 零成功根因报告（只读，未改任何文件，未连服务器）

## 0. 结论

"TP1 刚成交、账本滞后"这个假设**不成立**。主因是两处"测试夹具词汇与 Deepcoin 真实返回行词汇不一致"的确定性缺陷，加上同类的"成交识别"词汇缺陷。它们与时序无关，100% 复现。

1. **规划器**（`strategy_management_planner.py:3395-3400`）把保护单行上的 `side`（平仓方向：多单是 `sell`）当成仓位方向别名。任何真实 TPSL 行都得到 `{"long","short"} != {"long"}`，判为不匹配，规划器报 `protection_price_or_size_mismatch`。
   - 同一缺陷 09-05 已在收敛执行器和备份止损执行器里修过（`356630dd`），**规划器这一处漏修**。
2. **消费规划器**（`strategy_management_take_profit_consumption.py:244-252`、`:89-101`）要求挂单 TPSL 行带 `posId == 目标仓位`，并要求品种上每一张挂着的止盈都属于本腿。
   - 真实 TPSL 行**从不带 `posId`**（`docs/ARCHITECTURE.md` 4.8）。只要品种上有任何一张止盈在挂，三次尝试都返回 `take_profit_order_identity_conflict`，第四次进入时被标成 `take_profit_cancel_retry_exhausted`。**系统从未尝试撤任何单。**
3. **成交识别**同样读不到真实字段：Deepcoin 的 trigger-orders-history 没有 `state` 字段（只有 `triggerTime` 和 `errorCode`）。
   - `_terminal_state`（消费规划器 `:282-292`）读不到成交状态，永远返回 None。
   - `protection_health._successful_close`（`:661-662`）永远返回 False。
   - 结果是已成交的止盈在账本里被记成 `protection_missing`，并生成一条 critical 事故；消费规划器再因为这个状态拒绝。

**单独修第 1 处不够。** 172、166、159 会前进到组件一，然后死成和 146、150、153 一样的形态。最小可成功集合是 F1 + F2（F2 已含成交判据）。

## 1. `protection_price_or_size_mismatch`

### 触发位置与比较内容

- 原因码在 `strategy_management_planner.py:3273-3274` 的 `_unverified_protection_reason` 产生：该仓位有 `verified` 账本行，且至少一个 ordId 此刻在交易所挂着，但没有拿到 verified 保护。
- 调用点是 `:993-1007`，前面有两条路都会得到 `protection=None`：
  - `:967-981`：`match_position_protection` 已按 ordId 配上，但 `_ledger_confirmed_position_protection` 返回 None，或确认的集合不相等。
  - `:958-966`：匹配结果是 ambiguous 或 absent，兜底函数返回 None。此时 `protection` 被覆盖成 None，所以 ambiguous 原因也不会被上报。
- 逐字段比对在 `_ledger_row_matches_current_protection`（`:3368-3426`）。严格别名检查由 `877fbc33`（09-04）加入：
  - **`:3395-3400` 方向检查（主缺陷）**：别名键是 `("posSide","pos_side","side")`，并做 `buy→long`、`sell→short` 映射。真实行是 `posSide=long, side=sell`（`docs/2026-09-07-...server-notes.md` 里有 09-04、09-05、09-07 三个时点的原始行，键集合恒为 23 个）。集合变成 `{"long","short"}`，检查**必然失败**。
  - **`:3411-3415` 价格检查（潜伏缺陷）**：`purpose` 不在 `{"stop_loss","sl","loss"}` 时改用止盈键。复合替换写入的 `backup_stop` 行（`composite_executor.py:1016`）因此永远不匹配。目前只是潜伏，因为复合替换从未成功过。
  - **`:3416-3418` 零值**：没有把 `"0"` 当作缺失，与 `deepcoin_trigger_rows` 的"零即缺失"口径不一致。
- 测试夹具里没有一行带 `side: buy/sell`：`tests/test_strategy_management_planner.py` 中 `"side": "sell"` 出现 0 次。

### 时间线与生产证据

- 批次 158（09-04 09:22Z）还带着完整的保护快照。`877fbc33` 提交于 09-04 12:17Z。此后第一个要过这道检查的批次是 159，它被拦下。
- 批次 159 的现场（`docs/2026-09-05-protection-order-side-semantics-production-activation.md`）：BTC 多单 6 张，两张全仓止损，**没有任何止盈单**。TP 成交假设对 159 直接不成立。
- 172 与 173 是天然对照。同一仓位、同一账本、同一批交易所行，相隔约两小时：
  - `move_stop_to_break_even` 的有效动作是 `BREAK_EVEN_BY_MARKET_ACTION`，被 `:897-901` 排除在证据检查之外，所以 173 成功。
  - 172 要过这道检查，所以失败。

### 对"TP 成交竞态"假设的逐条检验

- **缺一张已成交的止盈不会导致 mismatch。**
  - `match_position_protection`（`protection_attribution.py:211-243`）按交易所现存行迭代，不按账本迭代。
  - `_ledger_confirmed_position_protection` 对不在挂单里的账本行直接 `continue`（`:3336-3340`）。
  - 两边集合仍然相等。
- **止损数量不会因为 TP 成交变动。** 交易所不会缩减止损的 `sz`，账本同样不动（server-notes 第六轮：TP1 成交后止损仍是 `sz=10`）。
- **规划器侧不存在竞态窗口。**
  - `plan_strategy_management_batch`（`:270-297`）在仓位权威锁内先读一份新快照，再用同一份快照同步跑一轮 `reconcile_deepcoin_execution_bindings`，之后才规划。账本记账和规划看到的是同一份快照。
  - 后台轮询间隔约 44 秒，单轮中位 13.8 秒（`ARCHITECTURE.md` 4.6）。这个节奏只影响面板和告警，不影响规划器的判断。

### 账本如何处理止盈成交

止盈成交由 REST 对账轮驱动，不是 WS。`DeepcoinWsEvent` 只用于 TU 归属和入场绑定。对账轮里有两条处理：

1. **`reconcile_trigger_take_profit_order_history`**（`position_take_profit_orders.py:173-290`，由 `execution_bindings.py:1181` 调用）
   - 只处理 `position_take_profit_orders` 表里的 TP1。
   - 凭据是历史终态或"仓位减量等于止盈数量"（`take_profit_fill_evidence.py`）。
   - 证据成立时写 `status=filled`，并记到保护腿上。**它不写保护账本。**
2. **`reconcile_position_protection_health`**（`protection_health.py:522-615`，由 `execution_bindings.py:1203` 调用）
   - 账本行不在挂单里时，判定依据是 `_successful_close`（`:661-662`），它要求历史行的 `state` 属于 `{filled, closed, completed}`。
   - Deepcoin 的 trigger-orders-history 没有 `state` 字段。字段清单见 `docs/2026-09-05-deepcoin-api-deterministic-link-research.md:117`，只有 `triggerTime` 和 `errorCode`。
   - 所以已成交的止盈一律被改成 `protection_missing`，并产生 `protection_missing` 事故（`:608-614`）。生产上 leg 579 在 TP1 成交 8 秒后出现 critical 事故 2057；binding 363 的 TP1、TP2 两行也是这个状态。
   - 全代码库没有任何一处把账本止盈行写成 `filled`。
   - 这条事故不可消除（`planner.py:174-200` 没有按"已解决"过滤）。该腿因此永久处于 protection_recovery 分支：
     - `partial_then_break_even` 走 `:1054-1080`。
     - 其它意图走 `:742-845` 的健康分类。

### 这道拒绝是否必要

这道拒绝的安全意图是对的：我们即将撤换的单必须与账本逐项一致。问题出在实现上读了错误的字段。

**最小修复 F1：**

- `:3395-3400` 改用 `native_tpsl.protection_order_position_sides(row)` 取仓位方向，并用 `protection_order_sides_consistent(row)` 校验平仓方向。这与 `356630dd` 的改法相同。
- `:3411` 的止损 purpose 集合加入 `backup_stop`。
- 价格比较把 `"0"` 和 `""` 视为缺失。
- 在 `_persist_blocked`（`:2544-2551`，现在只写 `positions: []`）里落下"哪个 ordId、哪个字段、账本值、交易所值"，补上可观测性缺口。

**不放宽的部分：**

- ordId 归属判据。
- 全局唯一计数。
- `explicit_pos_ids` 冲突检查。
- 价格和数量的严格相等。
- `global_unowned_order_present`。

**关于重试：**

- 不建议把这个原因直接加进 `RETRYABLE_PREFLIGHT_BLOCK_REASONS`。
- `_retryable_preflight_blocked_batch`（`:2700-2713`）规定：该腿只要有过任何 `PositionMutationIntent`，批次就不可重试。binding 363 的备份止损来自 `position_mutation_intent_readback`，所以重试分支对它本来就走不通。
- F1 之后再出现真正的 mismatch，多半是自动保本或缩量正在换单造成的。届时可以单独评估一个约 60 秒的有界重试。

## 2. `protection_visibility_retry_expired`（批次 152、157）

### 触发位置

- 外层原因码在 `strategy_management_worker.py:961-963` 写入。
- 内层原因是 `protection_missing_cancellable_order_id`（`planner.py:3271-3276`）。它属于 `_TEMPORARY_PROTECTION_VISIBILITY_REASONS`（`:2730-2735`），按 5、15、30、60、120 秒重试，满 5 分钟后过期。

### 现场

- 触发入场的仓位，账本行数为 0，而交易所上有 TPSL 单。批次 152 的现场：binding 324 的账本为 0，保护意图 163 最终停在 `trigger_protection_candidate_predates_fill`。
- 根因是保护归属链缺失，与时序无关。5 分钟重试不可能把归属补出来。

### 现状

- 已由 `877fbc33` 的 lineage 归属和之后的 `exchange_adopted_by_tu` 收养机制覆盖。binding 363 的 2600 止损就是被收养进账本的。
- **无需新改动**，用第 5 节的 Q8 确认即可。
- 仍应 fail-closed：没有 ordId 归属，就不允许撤单。

## 3. `take_profit_cancel_retry_exhausted`（批次 146、150、153）

### 这是"三次预检拒绝"的外壳

- 组件在 `attempt_count >= 3` 时转 `operator_required`，原因码记为 `take_profit_cancel_retry_exhausted`（`composite_executor.py:357-370`）。
- 每次预检拒绝都走 `:400-410`，证据写成 `{"phase":"preflight","refusal_code":...}`。
- 批次 153 的文档明写"三次原始 identity_conflict"。批次 153 自身的腿也没有任何订单 ID 和 close 提交。

### 它想撤什么

`plan_take_profit_consumption` 打算做的是：

- 撤掉本腿的第一档止盈。
- 为了让保留的止盈总量不超过 `target_remaining`，再撤掉多出来的后续档。

### 为什么必然失败

账本行取的是该腿的全部止盈行，**不按状态过滤**（`_plan`，`:1272-1277`）。

- **a. 挂单必须带 `posId`**
  - `_pending_owner_matches`（`:244-252`）要求挂单行的 `posId == 目标`。真实 TPSL 行没有 `posId`。
  - 任何一张挂着的止盈都会在 `:89-101` 被判为冲突。这一条足以解释全部三个批次，也足以解释单仓位的情形。
- **b. 账本行状态集合太窄**
  - `_ledger_owner_matches`（`:231-241`）只接受状态 `{verified, active, filled, cancel_requested, cancelled}`。
  - 已成交的 TP1 在账本里是 `protection_missing`（见第 1 节），被保本替换过的旧止盈是 `retired`。这两种都会在 `:66-67` 触发冲突。
  - "第一止盈位已到"这类消息恰恰都是在 TP1 成交之后才到达。
- **c. 读不到成交终态**
  - 即便绕过 a 和 b，`_terminal_state`（`:282-292`）读的是 `state`、`status`、`ordState`。trigger 历史行没有这些字段，恒得 None，于是落到 `take_profit_terminal_state_unknown`（`:149-150`）。
  - `order_history=()` 传入的是空值。父触发单和子成交单的 ordId 也不相同。
- **d. 重试重武装同样要求 `posId`**
  - 网关的 `_pending_cancel_retry_matches_authority`（`position_mutation_gateway.py:738-746`）也要求挂单行带 `posId`，重试重武装永远无法生效。
- **e. 没有止盈账本行时，三个组件都起不来**
  - `_load_component`（`composite_executor.py:1230-1243`）要求该腿至少有一条 `take_profit` 账本行，否则三个组件都立即 `operator_required / take_profit_order_identity_conflict`。
  - 批次 129 和 159 那种"根本没挂止盈"的仓位，永远执行不了复合指令。

测试夹具里同样是臆造的字段：`tests/test_strategy_management_take_profit_consumption.py:60,118` 给挂单行加了 `posId`，给历史行加了 `state: filled`。

### 修复 F2

- **归属**
  - 改用已经过生产验证的 `protection_authority.resolve_protection_authority()`（`:181-413`）。它按 ordId→账本 或 `TU==posId` 判断归属；batch 173 的成功路径就经过 `deepcoin_execution_actions.py:467`。
  - 属于别的仓位的单跳过。
  - 同方向上无法归属的单冻结，并用新原因码 `take_profit_unattributable_pending_order`。
  - 本仓位的止盈仍然严格比对价格和数量（`ledger_drift`，`:499`）。
  - `posId` 只在行里存在时才要求相等。
- **档位**
  - 状态为 `retired`、`cancelled`、`superseded` 的行视为历史，跳过。
  - 第一档按下面顺序判定：
    1. 在挂单里：计划撤单。
    2. 不在挂单里：必须有成交证据才算"已消费"，否则保持 `take_profit_terminal_state_unknown`。
  - 成交证据可以是两种之一：
    - i. `position_take_profit_orders.status='filled'`。它由对账轮用精确终态或仓位减量证明写入，是落库的持久证据。
    - ii. trigger 历史行满足 `triggerTime≠0` 且 `errorCode` 属于 `{"", "0", "00000"}`，即 `_trigger_failed` 的反面，并且挂单快照完整。
  - **绝不单凭"仓位变小"就推断成交。**
- **`_load_component`**
  - 没有止盈账本行时，组件一走 `no_cancel_required`。
  - 品种 ID 改从该腿任意一条账本行或 binding 取。
- **网关重试匹配**
  - 同步去掉对 `posId` 的要求。ordId 加 authority 已经足够。
- **账本状态**
  - `protection_health` 对"已证成交"的止盈行写 `filled`，不再写 `protection_missing`，也不再产生事故。
  - 止损阶梯文档列的差距 1（系统不知道哪一档止盈实际成交）要的也是这个判据，所以该判据应抽成共享函数。

**不放宽的部分：**

- 每次撤单仍要经过 `cancel_owned_position_sltp` 的 authority、指纹和实盘闸门。
- 止盈保持"先撤后挂"。
- 止损保持"先挂两张、读回确认之后再撤旧单"（`:1037-1097`）。
- 无法归属的单不撤。

## 4. 双仓位下的 `take_profit_order_identity_conflict`：证实，而且范围更大

- `:77-81` 取的是**整个品种**的全部挂着的止盈。
- `:89-90` 的 `order_id not in owned_by_id`，对任何不属于本腿的止盈都判冲突。这包括：
  - 同一 binding 的另一条腿；
  - **其它策略在同品种上的仓位**；
  - 人工挂的止盈。
- 执行器按顺序号遍历，组件一没确认就返回（`:128-190`）。两条腿的组件一会互相看到对方的止盈，全部卡死。
- 批次 150 就是这种形态：binding 320 有两条腿（553、554），各持仓 11。
- 第 3 节的 a 条使单仓位也必然失败，双仓位只是多了一层拒绝。用 authority 解析器确定归属范围（F2）之后，两层问题同时消除。

## 5. `explicit_break_even_stop_not_risk_tightening`（批次 135）

- 这个原因码是**死码**，`src` 里已经找不到。`d1e3d858`（09-05）删除了这个规划器分支。
- 它原来的含义是：保本类意图带着消息里的显式价格，且这个价格没有收紧（多单要求高于等于全部成交均价，也高于等于 `lifecycle.stop_loss`）。
- 它的后继是 `management_stop_action_conflict`（`planner.py:538-548`）。今天上线之后的处理方式是：
  - `_identity_without_explicit_break_even_prices`（`:525-535`）先剥掉显式价格；
  - 或者按 `MESSAGE_EXPLICIT_TIGHTER` 采用这个更紧的价格。
- **无需改动。**

## 6. 与今日上线和未部署改动的关系

- **`break_even_reference`（目标取策略价）与 never-loosen**
  - 在复合路径上，它们位于规划器 `:1211-1224` 和组件三 `:948-968`，两处都在被拦位置的下游。
  - 这条路径在生产上还没有执行过一次。
  - 它们不是任何一个拦截的原因。
- **remainder-close 兜底（`dde69b31`，未部署）**
  - 它要求"减仓已收敛"，同样走不到。
- **F1 和 F2 落地后，上述三条路径会首次拿到真实样本：**
  - "首个实盘样本清单"才第一次有意义。
  - remainder-close 设计文档 9.1 记的监控误报会开始出现：`production_safety_monitor.py:2398-2442` 会对"复合成功后已离场"的仓位永久报 critical。它应与 F2 同批修，或紧随其后。
  - 文档 9.2 指出复合路径从不撤未成交的入场腿，这一点仍然成立。
- **待指挥裁决的策略问题**
  - 背景：
    - TP1 已自行成交之后，`trusted_start_size` 取的是当时的实时仓位（`:1249`）。
    - 组件二还会在此基础上按分数再减一次。
    - `proven_filled_quantity` 只返回，不参与计算。
  - 需要裁决："第一止盈位已到、锁定利润"这类消息，在 TP1 已经成交的情况下还要不要再减一次？

## 7. 优先级修复计划（L3：真实交易所写入语义）

| 序 | 改动 | 解锁 | 风险 |
|---|---|---|---|
| F1 | 规划器方向别名、`backup_stop` 价格键、零值、拒绝详情落库 | 172、166、159 通过规划；顺带解锁 `adjust_stop_loss` 和部分止盈的保护证据检查 | 纯读侧 |
| F2 | 消费规划器改用 authority 解析归属；成交判据；`_load_component`；网关重试匹配；账本写 `filled` | 146、150、153 形态；F1 放行后的 172、166、159 | 触及撤止盈的写入路径 |
| F3 | 监控误报 9.1 | F2 成功后的噪音 | 低 |

- F1 单独部署是安全的，但不会带来成功。批次会从 `blocked` 变成 `recovery_required`，值守仍会告警。
- 我建议 **F1 和 F2 同批上线**；或者先上 F1，再用 Q1 的结果证明它确实放行。

### 测试（生产形态夹具）

- **TPSL 挂单行**：逐字采用 server-notes 里的 23 键原始行：
  - 带 `side` 和 `posSide`；
  - 不带 `posId`；
  - 同时带 `slTriggerPrice` 和 `closeSLTriggerPrice`；
  - `sz` 取 `"0"`。
- **trigger 历史行**：采用 `docs/2026-09-05-deepcoin-api-deterministic-link-research.md:117` 记录的字段清单，没有 `state` 字段。
- **batch 172 形态**：
  - 被收养的止损 1.5；
  - 一张备份止损；
  - TP1 0.7 已触发，TP2 和 TP3 各 0.4 还在挂；
  - 仓位 0.8。
  - 期望结果：规划成功 → 组件一以 `no_cancel_required` 或"撤掉多出来的那一档"确认 → 减到目标仓位 → 两张新止损读回确认后撤旧单 → `succeeded`。
- **batch 159 形态**：只有两张全仓止损，没有止盈。
- **batch 150 形态**：同品种两条腿各自有止盈，期望互不冲突。
- **反例（必须仍然拒绝）**：
  - 同方向上有无法归属的止盈；
  - 账本值与交易所值的价格或数量漂移；
  - trigger 历史行带 `errorCode≠0`；
  - 挂单快照不完整。
- 把现有那些臆造 `posId` 和 `state` 的夹具改成生产形态。
- 跑全量测试，并跑 `test_composite_management_fault_injection.py`。

### 验证步骤

1. 用只读的 dry-plan 脚本，在生产的实时仓位上同时跑新旧两个匹配函数，对比结果。这一步零写入。
2. 首个实盘样本人工盯着，对照"首个实盘样本清单"逐项核对：
   - 撤单 intent；
   - 新止损读回；
   - 旧止损撤销；
   - 账本状态。

### 回滚

- 两个改动都是纯代码、无 schema 变更，走既有的按角色回滚。
- F2 如果新增了账本 `filled` 状态，回滚后旧代码会把它当作非活跃行忽略。
- **例外**：旧的 `_ledger_owner_matches` 接受 `filled` 状态，行为兼容，这一点已经核过。

### 请指挥执行的只读点查（只走主键或已有索引）

- **Q1** 自 09-05 起是否有任何"要过保护证据检查"的批次成功。预期：`adjust_stop_loss`、部分止盈类、`partial_then_break_even` 均为 0 成功。

  ```sql
  SELECT id,intent,effective_action,status,reason_code,planned_at FROM strategy_management_batches WHERE id BETWEEN 159 AND 175;
  ```

- **Q2** binding 363 的腿。

  ```sql
  SELECT id,leg_index,status,pos_id FROM execution_order_legs WHERE execution_binding_id=363;
  ```

- **Q3** binding 363 的账本行。看 `side` 是否为 `long`，以及 TP1 行的 `updated_at` 相对 172 的 `planned_at` 在前还是在后。

  ```sql
  SELECT order_id,purpose,trigger_price,size_text,status,side,evidence_source,updated_at FROM position_protection_ledger WHERE execution_binding_id=363;
  ```

- **Q4** 172 规划时刻附近的观测。把 `:t` 换成 172 的 `planned_at`。看当时 TP1 是否已不在挂单里、仓位是 0.8 还是 1.5、其余各单数量是否与账本一致。如果一致，就排除了真的价量漂移，只剩方向缺陷这一个解释。

  ```sql
  SELECT observed_at,size_text,snapshot_complete,pending_tpsl_json FROM position_reconciliation_observations WHERE venue='deepcoin' AND pos_id=:pos AND observed_at BETWEEN :t_minus_3min AND :t_plus_1min ORDER BY observed_at;
  ```

- **Q5** 该仓位的保护事故。用来验证"TP 成交会被记成事故"。

  ```sql
  SELECT id,incident_type,created_at FROM position_protection_incidents WHERE venue='deepcoin' AND pos_id=:pos;
  ```

- **Q6** 组件证据。先查 150 和 153 的两个组件，再查 146。预期 `evidence_json` 里有 3 次 `refusal_code=take_profit_order_identity_conflict`，且没有任何 `intent_id`，即从未尝试过撤单。

  ```sql
  SELECT id,status,reason_code,attempt_count,evidence_json FROM strategy_management_components WHERE id IN (22,25);
  SELECT id,status,reason_code,attempt_count,evidence_json FROM strategy_management_components WHERE management_batch_id=146;
  ```

- **Q7** 两条相关 binding 的止盈账本行。用来验证 3b（账本状态是否为 `protection_missing`）和双腿情形。

  ```sql
  SELECT order_id,execution_order_leg_id,pos_id,purpose,status,size_text FROM position_protection_ledger WHERE execution_binding_id IN (320,325);
  ```

- **Q8** 保护归属缺口和批次 135。

  ```sql
  SELECT id,execution_binding_id,status,reason_code FROM strategy_management_batches WHERE id IN (152,157,135);
  SELECT COUNT(*) FROM position_protection_ledger WHERE execution_binding_id=336;
  ```

- **可选（交易所只读 GET）** 拉一次 `trigger-orders-history?instId=ETH-USDT-SWAP`，找到 TP1 的 ordId，确认三件事：没有 `state` 字段、`triggerTime≠0`、`errorCode` 为空。

### 关键实现文件

- `/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_planner.py`
- `/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_take_profit_consumption.py`
- `/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_composite_executor.py`
- `/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/protection_authority.py`
- `/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/protection_health.py`

---

## 附：指挥会话的点查结果（2026-09-21，生产库主键 / 索引点查）

- **Q1（批次 150–175）**：`partial_take_profit` 155、156 于 09-03 成功（`877fbc33` 合入前）；09-04 之后凡要过保护证据检查的批次全部被拦——
  159（09-05）、166（09-15）、172（09-21）均为 `protection_price_or_size_mismatch`。同期成功的只有 `full_exit`（154、162–165、168、170、171）
  与 `break_even_by_market`（151、173），两者都不经过该检查。172 与 173 同仓位、相隔一小时，一拦一过。**与报告第 1 节完全一致。**
- **Q3（binding 363 账本）**：全部行 `side='long'`；TP1 2690×0.7、TP2 2720×0.4 为 `protection_missing`（已成交却被记成缺失）；与报告第 1、3 节一致。
- **Q6（146 / 150 / 153 的组件一）**：均 `operator_required / take_profit_cancel_retry_exhausted`、`attempt_count=3`；153 的证据是三次
  `{"phase":"preflight","refusal_code":"take_profit_order_identity_conflict"}`，146 / 150 是三次 `RuntimeError`（旧版证据格式）；**没有任何 intent_id，从未尝试撤单。**
