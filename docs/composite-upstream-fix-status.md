# 复合指令上游三项修复（F1 / F2 / F3）实施状态

- **分支**：`worktree-agent-af174ec402d8c3b9c`（worktree
  `/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-af174ec402d8c3b9c`）
- **基线**：`codex/deepcoin-auto-trading-v1` 尖端 `3dca05d6`，其上 cherry-pick
  `dde69b31`（剩余仓位市价全平）与 `746c88e8`（复合路径撤自己的挂单入场腿），
  两者在本工作树中分别是 `3efceae3` 与 `5e201ce1`。
- **设计依据**：`docs/plans/2026-09-21-composite-zero-success-root-cause.md`
  （含 09-21 生产点查附录）。该文件是本次实施的约束性文档。
- **未部署、未推送、未连服务器、未发任何 Telegram/MQTT 通知。**

| 序 | 提交 | 主题 |
|---|---|---|
| F1 | `3393fd68` | 规划器必须读 TPSL 行的平仓方向，而不是把它当仓位方向 |
| F2 | `5b8b0405` | 止盈归属改由绑定链解析；不依赖 `state` 字段判定成交 |
| F3 | `72f4ab2f` | 已离场的仓位没有止损可核验（监控误报） |

---

## 1. F1 — `protection_price_or_size_mismatch`

文件：`src/telegram_kol_research/strategy_management_planner.py`

- `_ledger_row_matches_current_protection` 拆成一个返回**失配原因**的
  `_ledger_row_protection_mismatch`，布尔版本保留为薄包装。
- 仓位方向改用 `native_tpsl.protection_order_position_sides(row)`（只读
  `posSide`/`pos_side`）；平仓方向改用
  `native_tpsl.protection_order_sides_consistent(row)` 单独校验。与
  `356630dd` 在收敛执行器和备份止损执行器里的改法一致。
- 止损价格键的 purpose 集合新增 `backup_stop`
  （常量 `STOP_LEDGER_PURPOSES`）。
- 价格别名中 `"0"` 与 `""` 视为缺失，与 `deepcoin_trigger_rows` 口径一致；
  **无法解析的价格仍然拒绝**——读不出不等于不存在。
- `_persist_blocked` 新增 `protection_mismatches` 参数，落进批次 target
  snapshot：每条记 `order_id / field / ledger / exchange`，上限
  `MAX_RECORDED_PROTECTION_MISMATCHES = 6`，另记 `protection_mismatch_count`
  总数；单值截断 64 字符。

**没有放宽的部分**（逐条在测试里锁住）：ordId 归属、全局唯一计数、
`explicit_pos_ids` 冲突、价格与数量的严格相等、`global_unowned_order_present`。

## 2. F2 — `take_profit_cancel_retry_exhausted` / 归属与成交判据

### 新增共享模块

`src/telegram_kol_research/take_profit_fill_predicate.py`（纯函数）

- `take_profit_fill_proven(order_id, recorded_order_statuses, trigger_history,
  pending_snapshot_complete)`。
- 两类证据：
  1. `position_take_profit_orders.status == "filled"`（对账轮用精确终态或
     完整的两次观测仓位减量证明写入，持久可审计）；
  2. trigger-history 行 `triggerTime != 0` 且 `errorCode ∈ {"", "0", "00000"}`，
     **并且**挂单快照完整。
- **绝不单凭仓位变小推断成交。** 不完整的挂单读取是"未知"，不是"已消失"。
- 止损阶梯设计文档第 1 节的差距 1 要的是同一个判据，直接 import 本模块即可，
  不要再长出第四个读取器。

### 消费规划器

`src/telegram_kol_research/strategy_management_take_profit_consumption.py`

- 新增关键字参数 `protection_authority`、`pending_snapshot_complete`、
  `recorded_order_statuses`。归属由
  `protection_authority.resolve_protection_authority()` 解析后传入，本模块保持纯函数。
- **authority 未解析一律不出计划**：`protection_order_unattributable` 映射为新原因码
  `take_profit_unattributable_pending_order`，其余原因码原样透出；
  `authority is None` → `take_profit_protection_authority_unavailable`。
- **已解析的 authority 已经安置了该品种上的每一张 TPSL 行**，所以它没有给我们的行
  属于别的仓位，跳过（不读、不改、不撤）。这一条同时解掉了单仓位和双仓位两层拒绝。
- 我们自己的止盈仍逐项严格比对：ordId 在账本内、行上若带 `posId` 必须是本仓位、
  instId 相等、`posSide` 等于本仓位方向、平仓方向一致、数量与触发价严格相等。
- 账本状态：
  - `HISTORY_LEDGER_STATUSES = {retired, cancelled, canceled, superseded}` → 视为历史，跳过；
  - `OWNED_LEDGER_STATUSES = {verified, protected, active, filled, cancel_requested,
    protection_missing}` → 本腿的档位；
  - 两者之外 → `take_profit_order_identity_conflict`（fail closed）。
- 第一档判定顺序改为设计规定的顺序：**在挂单里 → 计划撤单**；不在挂单里 →
  必须有成交证据才算已消费，否则 `take_profit_terminal_state_unknown`；
  仅当普通订单历史（有 `state`）明确给出 cancelled/expired 时走
  `exact_terminal_no_fill`。
- 没有任何存活止盈账本行时返回 `no_cancel_required`（`evidence_tier =
  "no_take_profit_ledger_row"`），**但这一判定排在挂单核对之后**——只要本仓位上
  还挂着一张账本不认识的止盈，仍然是 `take_profit_order_identity_conflict`。

### 复合执行器

`src/telegram_kol_research/strategy_management_composite_executor.py`

- `_plan` 解析 authority、收集本腿 `position_take_profit_orders.status`，
  一并传给规划器；`pending_snapshot_complete=True`
  （`_exchange_snapshot` 任一读取不是 list 就抛错，走到这里即完整）。
- `_load_component` 的品种名：本腿 `take_profit` 账本行 → 本腿全部保护账本行 →
  该 binding 全部保护账本行，三级回退，仍要求恰好一个非空品种。
  这解开的是"有止损没止盈"的仓位（批次 129、159 形态）三个组件全起不来。
- `_exchange_snapshot` 新增 `trigger_history` / `order_history` 两个独立键，
  `history` 保持原来的合并列表供 intent 对账使用。两个端点词汇不同，
  合并之后再问一个问题，会让同一张单看起来像两张。
- 组件一确认时把 `proven_filled_quantity` 与 `evidence_tier` 写进证据。

### 网关

`src/telegram_kol_research/position_mutation_gateway.py`
`_pending_cancel_retry_matches_authority` 不再要求挂单行带 `posId`
（行上若带则必须是本仓位），方向改用
`protection_order_position_sides` + `protection_order_sides_consistent`。

### 账本写 `filled`

`src/telegram_kol_research/protection_health.py`

- 只对 `PositionProtectionLedger` 且 purpose 属于 `{take_profit, tp, profit}`
  的行生效；证据取自共享判据；成立时写 `status="filled"`、在
  `evidence_json` 里记 `take_profit_fill`，**不再产生 `protection_missing` 事故**。
- 止损不走这条路：止损触发即平仓，平仓后该 pos_id 早已不在 `live_ids` 里。
- 无 schema 变更：`position_protection_ledger.status` 是 `String(32)`，没有 CHECK 约束。

---

## 3. 账本状态读取者审计（`position_protection_ledger.status`）

写 `filled` 之前，逐个核对了每一处读取。**没有任何一处的活跃集合包含
`filled`**，因此一行被判定成交之后对所有自动路径都不可见——这正是期望语义：
已成交的止盈不该再被撤销、替换或补挂。

| 读取点 | 接受的状态 | 写 `filled` 之后的行为 |
|---|---|---|
| `protection_ledger.list_verified_account_ledger_rows` / `list_verified_ledger_rows_for_positions` | `verified` | 不再返回该行。规划器的 `ledger_rows_by_pos_id` 因此不含已成交止盈——它本来也不在挂单里，`_ledger_confirmed_position_protection` 原就跳过。 |
| `protection_authority._active_ledger_rows_by_order_id` | `ACTIVE_LEDGER_STATUSES = {verified, protected, active}` | 不再作为归属来源。已成交的单不在 `trigger-orders-pending` 里，authority 根本不会查到它。 |
| `protection_health.reconcile_position_protection_health` | `verified, protected, protection_missing` | 该行离开查询集合，后续轮次不再复检、不再生成事故。**`filled` 是终态。** |
| `protection_retirement.RETIRABLE_LEDGER_STATUSES` | `verified, protected` | 仓位关闭时不再把它改写成 retired，`filled` 保留"确实成交过"的事实（与 `RETIRABLE_LEG_STATUSES` 注释里对 `filled` 的既有立场一致）。 |
| `runtime_incident_scanner` | `verified, active, protected` | 不再进入事故扫描。 |
| `break_even_shadow.LEDGER_STATUSES` | `verified, protected` | 影子比较不再包含它。 |
| `break_even_convergence_executor`（:780、:1055） | `verified, protected` | 自动保本不会去动一张已成交的止盈。 |
| `trigger_take_profit_convergence_executor`（:824、:990、:1143） | `verified` | 止盈收敛不再把它算成在挂档位。 |
| `trigger_backup_stop_executor`、`backup_stop_repair`、`stop_loss_size_convergence`、`legacy_conditional_cancel`、`native_tpsl_migration`、`protection_incident_convergence`、`protection_replacement_persistence`、`entry_protection_ledger_repair`、`execution_bindings`（:1508、:5444）、`cli`、`web_app` | `verified`（`execution_bindings:5444` 为 `verified, active`） | 全部不再看见该行。 |
| `position_mutation_gateway`（:881 `ledger.status != "verified"` 的撤单前置） | `verified` | **已成交的止盈无法再被撤销**，这正是目的。 |
| `strategy_management_take_profit_consumption._ledger_owner_matches` | 旧：`{verified, active, filled, cancel_requested, cancelled}`；新：`OWNED_LEDGER_STATUSES` | 新旧都接受 `filled`。**回滚兼容性由此成立**（下节）。 |

**唯一需要留意的后续风险**：`protection_ledger.upsert_protection_ledger_row`
按 `(venue, order_id)` 覆盖写，会把 `filled` 覆盖回调用方传入的状态。它的所有
调用方都由"该单此刻在 `trigger-orders-pending` 里"驱动，而已成交的单不在其中，
所以当前没有可达路径。**新增任何一个不以挂单为前提的 upsert 调用点时必须重新核对。**

---

## 4. F3 — 监控误报

`src/telegram_kol_research/production_safety_monitor.py`
（`read_composite_management_invariants`，`composite_position_without_verified_stop`）

- 新增跳过条件：**实时仓位数量可得**（传入 `live_position_sizes` 或
  `live_position_snapshot_path`）**且该 posId 缺失或为 0**。
- 没有实时读取时不跳过——"没读"是未知，不是"仓位没了"。
- `dde69b31` 的 remainder-close 标记跳过保留在其后，它覆盖的是"故意平掉剩余仓位"
  那一条路径；本次覆盖的是此外的所有结束方式（新保本止损被触发、之后的
  `full_exit`、人工平仓），它们留下的形态完全一样。

---

## 5. 回滚

三项都是纯代码，无 schema 变更，回滚即 `tg-deploy <上一个 SHA>`。

- F1、F3 回滚后行为完全回到当前生产形态。
- F2 回滚后，新代码写下的 `status='filled'` 账本行会被旧代码怎样对待：
  - 旧的 `_ledger_owner_matches` 的接受集合**包含 `filled`**
    （`{verified, active, filled, cancel_requested, cancelled}`，已逐字核对），
    所以该行仍被当作本腿的一个档位；
  - 随后旧的 `_terminal_state` 读 `state`/`status`/`ordState`，在真实 trigger 历史行上
    恒为 `None`，于是落到 `take_profit_terminal_state_unknown` —— **拒绝，不是误动作**。
  - 其余所有读取点的活跃集合都不含 `filled`，旧代码同样看不到该行。
  - 结论：回滚后复合指令回到"拒绝"这一既有形态，不会因为 `filled` 产生任何新的写入。

---

## 6. 设计文档未覆盖、由我决定的事项

1. **`_ledger_owner_matches` 的接受集合**。设计只说"`retired`/`cancelled`/
   `superseded` 视为历史跳过"，没说接受集合。旧集合含 `cancelled`，与"跳过"冲突；
   我按设计的显式指令让 `cancelled` 进入历史集合，并把 `protection_missing` 加入
   接受集合——否则 `protection_health` 先于 F2 跑过的腿仍然全数被拒（第 3b 节的形态）。
   安全性由"第一档必须有成交证据"承担，不由状态白名单承担。集合之外仍 fail closed。
2. **"属于别的仓位就跳过"的判据**。设计写"属于别的仓位的单跳过；同方向无法归属的单冻结"。
   纯函数拿不到"authority 把它安置到了哪里"。我采用的判据是：
   **已解析的 authority 蕴含该品种每一张行都已安置**，所以它没给我们的行就是别人的，跳过；
   冻结完全交给 authority 自己的 `protection_order_unattributable`。
   这样不会在 authority 已经放行之后再冻结一次。
3. **`_exchange_snapshot` 拆出 `trigger_history` / `order_history`**。
   设计未提。合并列表会让同一张单被计为两行而触发"历史行不唯一"的拒绝。
   保留原 `history` 键不动，纯新增。
4. **组件一确认时记录 `proven_filled_quantity` 与 `evidence_tier`**。
   设计未提，纯新增证据，无任何读取者据此决策。理由：第 7 节的策略问题在生产上
   必须可观测，否则"系统认为已经成交了多少"事后无法从库里还原。
5. **`_load_component` 三级品种回退**。设计只说"从该腿任意一条账本行或 binding 取"。
   我按"本腿 take_profit → 本腿全部 → binding 全部"的顺序，最窄优先，
   仍要求恰好一个非空品种，分歧仍 fail closed。
6. **单次全量测试**。设计要求"每阶段末全量绿 + 一个提交"，而任务的 TESTS 段
   要求"最后跑一次全量"。全量约 13 分钟。我按 `AGENTS.md`
   风险自适应验证一节（"把所有生产代码改动组装成最终候选后跑一次全量"）执行：
   每个阶段跑针对性的大范围子集，最终候选跑一次全量。子集结果见第 8 节。

## 7. 待指挥裁决的策略问题（事实，不含判断）

批次 172 形态（ETH 多单，原仓位 1.5，TP1 2690×0.7 已自行成交，实时仓位 0.8，
消息"第一止盈位已到，锁定利润"识别为 `partial_then_break_even`、fraction 0.5）：

- 规划器 `strategy_management_planner.py:1249` 取
  `trusted_start_size = Decimal(str(position["size"]))`，即**实时仓位 0.8**，
  不是原始仓位 1.5，也不回看 TP1 已成交的 0.7。
- `planned_close_size = 0.8 × 0.5 = 0.4`，`target_remaining_size = 0.4`。
- 组件一：TP1 证成成交（0.7），不撤；TP2 与 TP3 合计 0.8 超过目标 0.4，
  按既有规则释放较早的一档，**撤掉 TP2**，保留 TP3（0.4）。
- 组件二：**再市价减仓 0.4**，仓位变成 0.4。
- 组件三：在开仓价挂主备两张止损（各 0.4），读回确认后撤掉两张旧止损。
- 净结果：原 1.5 张里，TP1 拿走 0.7，组件二再拿走 0.4，**合计 1.1（约 73%）**，
  剩 0.4。若"50%"指的是原始 1.5 的一半（0.75），TP1 的 0.7 已几乎独自完成。
- `proven_filled_quantity = "0.7"` 由组件一返回，并已写入组件证据，
  **但组件二不读它**——fraction 仍然作用在当时的实时仓位上。
- 以上由 `tests/test_composite_production_batch_shapes.py::
  test_batch_172_shape_runs_all_three_components_to_succeeded` 逐项断言。

**本次实施没有改变这一行为。** 是否应当在 TP1 已成交时少减或不减，请指挥询问用户。

## 8. 测试

生产形态夹具集中在 `tests/deepcoin_production_rows.py`：
`trigger-orders-pending` 的 23 键、`trigger-orders-history` 的 25 键（**无 `state`**）、
`/account/positions` 的 20 键。构造函数内含 `assert tuple(row) == ...KEYS`，
任何一次形状漂移都会直接红。

| 文件 | 覆盖 |
|---|---|
| `tests/test_management_protection_evidence_production_shapes.py` | F1：真实行确认、`backup_stop`、零值、批次 172/159 形态；七种漂移（价格、数量、仓位方向、平仓方向、品种、posId、单据类型）仍拒绝并给出字段；全局唯一仍拒绝；拒绝详情落库与上限 |
| `tests/test_take_profit_fill_predicate.py` | 共享判据：无 `state` 字段、两类证据、错误码、未触发、快照不完整、历史行不唯一、他单历史 |
| `tests/test_strategy_management_take_profit_consumption.py` | 重写为生产形态；无 `posId` 即可归属；他腿止盈跳过；不可归属冻结；反方向与自家止损不误判；历史状态跳过；无账本行 → `no_cancel_required`；无账本行但有挂单 → 仍拒绝；价量漂移、失败触发、未触发、快照不完整、平仓方向不一致、authority 缺失/冻结/他仓位 |
| `tests/test_protection_health_take_profit_fill.py` | 已成交止盈写 `filled` 且不产生事故；两类证据；失败触发仍是 `stop_trigger_failed`；无证据消失仍是 `protection_missing`；止损不走此路；`filled` 为终态 |
| `tests/test_position_mutation_gateway_retry_match.py` | 无 `posId` 的真实行可重新武装；他仓位/反方向/平仓方向不一致/他品种/他单/挂单入场一律不武装 |
| `tests/test_composite_production_batch_shapes.py` | **批次 172 全流程到 `succeeded`**（TP1 证成、撤 TP2、减仓 0.4、两张新止损读回、撤两张旧止损）；无证据/失败触发/不可归属/价格漂移四种必须拒绝；**批次 159**（两张全仓止损、无止盈）全流程到 `succeeded`；**批次 150**（同品种两腿）互不冲突 |
| `tests/test_production_safety_monitor_closed_position.py` | F3：仓位仍在 → 仍报；持平 → 跳过；交易所不再列出 → 跳过；无实时读取 → 仍报 |

既有安全测试全部保持通过，包括 3b 合理性、止损闸门、never-loosen、
复合故障注入（`test_composite_management_fault_injection.py`）、
authority 边界覆盖（`test_protection_authority.py`）。

- 基线全量（cherry-pick 之后、任何修复之前）：**9457 passed, 4 skipped**。
- 最终候选全量：见下方"全量结果"。

### 全量结果

`uv run python -m pytest -q` 在最终树 `7dfe786a` 上跑一次：
**9530 passed, 4 skipped, 107 warnings, 785.10s (0:13:05)，退出码 0。**
对基线 9457 passed / 4 skipped 净增 73 条（新增 81 条，重写的消费规划器
文件用例数从 8 变成 26，净差由此而来）。
（`uv run pytest` 会在收集阶段失败，是既有问题，与本次改动无关。）

## 9. 首个实盘样本清单

F1+F2 上线之后，这条路径**第一次**会真的写交易所。逐项人工核对：

1. **规划阶段**：批次状态从 `blocked` 变成 `executing`；若仍是 `blocked`，
   读 `target_snapshot_json.protection_mismatches`——现在它会直接说出是哪张单的哪个字段
   （F1 之前这里永远是 `positions: []`）。
2. **组件一**：`strategy_management_components.evidence_json` 里必须出现
   `intent_id`（此前三个批次全部没有，即从未尝试撤单）；
   核对 `proven_filled_quantity` 与 `evidence_tier`。
   **预期是 `trigger_history_clean_trigger`**，不是
   `recorded_take_profit_order_status`——原因见第 10 节最后一条。
3. **撤单**：`position_mutation_intents` 里 `operation='cancel_position_sltp'` 的行
   与交易所 `trigger-orders-pending` 的差集一致；**确认没有撤掉任何一张不属于本仓位的单**。
4. **组件二**：`close_position` intent 的 `sz` 等于 `target_remaining_size` 的差额；
   实时仓位落到 `target_remaining_size`。
5. **组件三**：先看到两条 `set-position-sltp` 写入并读回（主 + 备），
   **之后**才有两条旧止损的撤销；任何时刻仓位都不裸奔。
6. **账本**：已成交的止盈行状态为 `filled`，`evidence_json.take_profit_fill` 有 tier；
   新止损两行 `verified`；旧止损两行 `superseded`/`cancelled`。
7. **事故**：该 pos_id 不再新增 `protection_missing` 事故。
   **注意：F2 不修复历史数据**——binding 363 上 09-21 之前已生成的
   `protection_missing` 事故是不可消除的，该腿仍会停在 protection_recovery 分支。
   若要让 172 这类既有批次通过，需要指挥单独批准一次生产数据处置，本次未做。
8. **监控**：仓位平掉之后不出现 `composite_position_without_verified_stop`（F3）。
9. **策略问题**：对照第 7 节，核对实际减仓量是否与用户的预期一致。

## 10. 未做的事

- 未推送、未部署、未跑任何服务器端脚本、未接触真实交易所、未发 Telegram/MQTT。
- 未做生产数据修复（既有 `protection_missing` 事故见第 9 节第 7 项）。
- 未改自动交易 / 执行模式开关。
- **比分析文档更坏的一处，实测确认，本次未修**：
  `take_profit_fill_evidence.prove_first_take_profit_fill` 的
  `_prove_exact_terminal`（`:147-158`）对**每一条 ordId 相符的历史行**先取
  `posId`/`posSide`/`sz`，任一取不到就**直接 return 一个 failure**——不是返回
  `None` 让流程继续。真实 trigger 历史行没有 `posId`，所以：

  ```
  有一条真实 trigger 历史行 -> tp1_exact_history_incomplete（proven=False）
  完全没有历史行           -> exchange_position_delta（proven=True）
  ```

  也就是说**止盈一旦真的触发过，"仓位减量"那一层证据就永远走不到**，
  `position_take_profit_orders.status` 因此几乎不可能被写成 `filled`。
  推论：F2 的第一类证据（账本状态）在生产上基本不会命中，
  **实际生效的会是第二类（trigger 历史 `triggerTime`/`errorCode`）**。
  两类都已实现且都有测试，功能上不受影响，但首个实盘样本应预期看到
  `evidence_tier = "trigger_history_clean_trigger"`。
  修 `_prove_exact_terminal`（让它改用 `take_profit_fill_predicate`，
  并且"字段缺失"应当继续而不是终止）是一项独立后续项，不在本次范围内。

## 部署记录（2026-09-22 07:19 CST）

- 用户 2026-09-22 明确批准部署。候选 `fa72cf76` = 共享分支 `3dca05d6` + A `3efceae3`（复合指令止损挂不上时剩余仓位市价全平）
  + B `5e201ce1`（复合路径撤未成交入场腿）+ F1 `3393fd68` + F2 `5b8b0405` + F3 `72f4ab2f`；最终候选全量 **9530 passed / 4 skipped / 0 failed**。
- 部署前：候选是交易进程代码 `81fdc58a` 与服务器检出 `c1de56ce` 的后代，共享分支 tip 是候选的祖先（均 PASS）；**当时无任何在仓仓位**
  （因此没有仓位停在历史 `protection_missing` 的恢复分支里）。
- 零在途闸门第一次报 1：是 2026-08-12 的历史组件（批次 119，`consume_take_profit_stage / recovery_required`，其批次早已 `resolved`）。
  闸门改为"组件所属批次未终结才算在途"后为 0。**今后的部署闸门沿用这一口径。**
- `tg-deploy fa72cf76…` → worker / web / ingest 均 active，web 200。**回滚 = `tg-deploy c1de56ce120e4e2eebc8f1a10d16a27dc6eb23a0`**
  （交易相关代码等同 `81fdc58a`）。
- **自动交易开关未动**：`auto_trade_enabled=true`、`management_execution_mode=live`、`composite_management_v2_mode=live`，设置 `updated_at` 仍为 2026-09-08。
- 部署后：最近 15 个消息处理作业全部 succeeded，worker 自重启起错误行 0，值守两个单元 active、心跳正常。
- 待办：首个真实的"减仓后保本 / 部分止盈 / 调整止损价"样本，按本文件的首笔实盘清单逐项核对（批次不再 `blocked`；组件一证据里出现 `intent_id` 或
  `evidence_tier=trigger_history_clean_trigger`；只撤了本仓位的单；新止损读回后才撤旧止损；仓位离场后无 `composite_position_without_verified_stop`）。

---

## 11. 第 7 节策略问题的裁决与实施（2026-09-22，未部署）

用户（账户所有者）2026-09-22 给出的规则，取代第 7 节记录的现状：

> 形如"第一止盈位已到，锁定利润，及时移动止损"、被识别为 `partial_then_break_even` 的消息——
> **若系统的第一张止盈单在交易所已经成交**（由 F2 的成交判据证成，`evidence_tier` 为
> `trigger_history_clean_trigger` 或账本/状态那一类证据）→ **不再减仓**：组件二不平任何仓（目标剩余
> = 当前实时仓位），组件三照旧把止损移到保本目标。
> **若第一张止盈还挂着**（未成交）→ 按分数减仓（今天的行为）**并撤掉那张止盈单**（组件一本来就计划了这次撤单，保留）。

其余一律不变：**绝不单凭仓位数量推断成交**；只收紧不放宽；策略价基准；remainder-close 兜底；
归属/authority 闸门；止盈"先撤后挂"、止损"先挂两张—读回—再撤旧"的顺序全部原样。

### 11.1 判定在哪里做、存在哪里

| 位置 | 做什么 |
|---|---|
| `strategy_management_take_profit_consumption.plan_take_profit_consumption`（纯函数） | 唯一做判定的地方，因为**这里也是唯一证成成交的地方**。新增 `first_stage_consumed_by_fill`（仅当第一档不在挂单里且 `take_profit_fill_proven` 成立时为真；`exact_terminal_no_fill`（撤单/过期）与 `no_take_profit_ledger_row` 都是假）与 `effective_target_remaining_size`。 |
| `strategy_management_composite_executor._first_stage_evidence` | 组件一**三个**确认出口（`no_cancel_required` 直确认、`fill_race` 确认、撤单后的正常确认）统一写入证据：`proven_filled_quantity` / `evidence_tier` / `first_stage_consumed_by_fill` / `effective_target_remaining_size`。 |
| `_first_stage_consumed_by_fill(session_factory, batch_id, leg_id)` | 组件二与组件三**从库里读回**这个布尔值，绝不重算。组件一与组件二之间重启、组件二与组件三之间重启，都得到同一答案（`tests/...::test_batch_172_restart_between_components_keeps_the_same_decision`）。旧版本确认的组件没有这个键，缺键即 `False`，也就是旧版本的行为。 |

**合约一个字都没改。** `close_fraction` 仍是 `0.5`，`required_components` 仍是三件，
`management_contract_fingerprint` / `desired_json.contract_fingerprint` 的比对
（`_validated_candidate_composite_contract`、`create_composite_components_in_session`、
`management_component_set_is_complete_in_session`、`_load_component`）全部原样通过。
`desired_json` 也没有被改写——判定走证据，不走 desired。

### 11.2 "有效目标剩余量"为什么是实时仓位，不是 `trusted_start_size`

`trusted_start_size` 是**规划那一刻**的实时仓位。批次 172 形态里它等于实时仓位（0.8），但
"计划之后、组件一之前 TP1 才成交"这一路（组件一的 `fill_race` 分支）里它是成交**前**的 1.5。
用它当目标会有两种坏结果：组件二把 `current < target` 判成 `position_below_target_remaining`
直接 operator_required；组件一的超额计算以 1.5 为界，可能留下**总量大于仓位**的止盈。
所以 `_plan` 把 `snapshot["positions"]` 里本 posId 的实时数量传给纯函数，
纯函数在"第一档已成交"时以它为目标；**读不到实时数量就拒绝**
（`target_live_position_not_unique`，recovery_required 可重试），不退回合约目标——
读不到不等于没变。组件二、组件三同样各自用自己那次实时读取。

### 11.3 超额止盈的撤单口径怎么变的

`plan_take_profit_consumption` 里
`excess = max(0, retained_total - target_remaining)` 变成
`excess = max(0, retained_total - effective_target_remaining)`。

- 批次 172：TP2+TP3 = 0.8，实时仓位 0.8 → excess = 0 → **一张都不撤**（此前按 0.4 的目标撤掉 TP2）。
- TP1 仍挂着：目标仍是合约目标 0.8（1.5 的一半按 0.1 步长向下取整是 0.7），excess = 0 → 只撤 TP1。
- 阶梯超过仓位（TP2 0.4 + TP3 0.7 vs 实时 0.8）：excess = 0.3 → 仍然释放较早的 TP2。
  **"不再减仓"从不意味着留下比仓位还多的止盈。**

### 11.4 组件三也必须跟着走

组件三原来三处直接用 `desired["target_remaining_size"]`：收敛校验（`partial_close_component_not_converged`）、
新止损 payload 的 `sz`、以及写账本的 `size_text`。跳过减仓之后这三处若仍是 0.4，
**新止损只会保护半个仓位**。三处一并改用同一个 `effective_target_remaining`
（跳过时 = 本次实时读取的仓位数量，否则逐字等于原值）。
`retained_take_profit_total(..., live_position_size=...)` 这道
"止盈总量不得超过仓位"的终检原样保留。

### 11.5 完成校验与通知看到什么

- `validate_composite_management_completion` 只要求每个组件 `confirmed` 且证据非空，
  跳过路径照样写两条证据，通过。
- 组件二跳过时：`reason_code = partial_close_skipped_first_take_profit_filled`（`confirmed` 状态上带
  reason，`execute_composite_management_batch` 只看 status，不受影响），
  证据 `{"remaining_size": <实时仓位>, "planned_close_size": "0", "first_take_profit_filled": true,
  "evidence_tier": "first_take_profit_already_filled"}`。
- `_complete_composite_batch` 从所有组件证据里收 `remaining_size`，所以完成通知的
  `partial_close` 一栏显示的是"剩余 0.8"（真实剩余），不是旧目标 0.4。

### 11.6 设计未覆盖、由我决定的事项

1. **判定的存放形式**：写进组件一 `evidence_json` 的追加项，而不是改 `desired_json`。
   理由：`desired_json` 参与 `_load_component` 的身份校验与 `create_management_component`
   的不可变身份比较，证据列表则已有同构先例（`_remainder_closed_legs` 就是按证据里的
   `outcome` 标记回读的）。
2. **"有效目标剩余量"取实时仓位**（第 11.2 节），并在读不到时拒绝而不是回退。
3. **组件二仍然调用 `target_remaining_close_delta`**（只是把目标换成实时仓位），
   而不是另起一条完全绕过它的分支：这样"仓位不得变大""仓位必须为正""唯一仓位"
   三道既有闸门在跳过路径上继续生效，delta 必为 `"0"` 则由构造保证。
4. **`exact_terminal_no_fill` 不算成交**：第一档被撤销/过期没有落袋任何利润，
   分数照旧作用。`no_take_profit_ledger_row`（批次 159 形态）同理，行为不变。
5. **组件二的 reason code 记在 `confirmed` 上**。设计要求"显式原因"；
   现有代码没有"确认且带原因"的先例，但没有任何读取者把 `confirmed` + 非空 reason 当失败
   （批次执行器只看 status）。

### 11.7 已知风险（本次未修，非本次引入）

**止盈成交与账本状态之间有一个窗口。** 组件一证成成交**不写保护账本**——把成交的止盈行写成
`filled` 的是 `protection_health` 的对账轮（F2）。因此在"TP1 在本批次执行中途才成交"
（`fill_race`）这一路，TP1 的账本行此刻仍是 `verified`，三档相加超过实时仓位，
组件三的 `retained_take_profit_total` 抛 `retained_take_profit_exceeds_position`，批次停下。
**这与本次策略改动无关，改动前同一形态经"已在目标"路径停在同一处**；
测试 `test_batch_172_first_take_profit_filling_mid_cancel_does_not_reduce` 把它逐项钉住了。
停下来的那一刻**没有写过任何新止损，原 1.5 的止损仍然武装**，所以这是安全的一侧。
真正修它需要让组件一（或它调用的某处）在证成成交时就落账本状态，那是独立的一项。

### 11.8 测试

| 文件 | 新增/改写 |
|---|---|
| `tests/test_composite_production_batch_shapes.py` | 172 形态改为新策略（不撤任何止盈、不减仓、止损按 0.8 挂两张再撤旧）；TP1 仍挂着的同形态（撤 TP1、减 0.7、剩 0.8）；组件一与组件二之间**重启**后决策不变；**空单镜像**（2610/2580/2550，市价 2600）；阶梯超过仓位时仍释放较早一档；`fill_race`（撤单被拒 + 刚刚成交）路径。夹具新增 `side` 与 `tp1_pending` / `tp_sizes` 参数，`set_position_sltp` 的回读行改用 payload 的 `posSide`。 |
| `tests/test_strategy_management_take_profit_consumption.py` | 纯函数层：已成交的第一档以实时仓位为目标且不释放后续档；超过仓位时仍释放；**没有实时数量则拒绝**；仍挂着的第一档保持合约目标；`exact_terminal_no_fill` 不算成交。`_plan` 夹具新增 `live` 参数（默认 `"6"`）。 |

### 11.9 全量结果

`uv run python -m pytest -q` 在最终候选上跑一次：
**9540 passed, 4 skipped, 107 warnings, 830.43s (0:13:50)，退出码 0。**
对第 8 节记录的 9530 / 4 净增 10 条（批次形态文件 +6，消费规划器纯函数 +5，
原 172 的两条断言被改写而非新增，其中一条更名）。
（`uv run pytest` 仍在收集阶段失败，既有问题，与本次改动无关。）

**未部署、未推送、未连服务器、未触碰真实交易所、未发任何 Telegram/MQTT 通知。**
基线 `f807a79f`（生产运行 `fa72cf76`，其后均为文档提交）。
