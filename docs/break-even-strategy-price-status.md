# 保本价取策略价 + 保本类消息不再因附带数字被拒：实施状态

日期：2026-09-21
规格：`docs/plans/2026-09-21-break-even-strategy-price-spec.md`（上位分析
`docs/plans/2026-09-21-break-even-stray-price-refusal-analysis.md` 第 8 节 R1–R4）
分支：`worktree-agent-a4603a3d24d1941e8`
（worktree `/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a4603a3d24d1941e8`）
基线：`b0d0bd4f`（`codex/deepcoin-auto-trading-v1` 当时的 tip；生产为 `840c83ba`，其后均为文档）
验证等级：**L3**（真实交易所写入语义变化）
状态：本地实施完成，**未推送、未部署、未连服务器、未发 Telegram / MQTT**。
首笔实盘样本核对由指挥会话负责。

## 1. 改动清单

| 文件 | 性质 |
|---|---|
| `src/telegram_kol_research/break_even_reference.py` | 新增。纯函数：`resolve_break_even_reference`、`break_even_target_price`、`planned_tpsl_reference_fields`、`adopted_break_even_reference` |
| `src/telegram_kol_research/management_price_plausibility.py` | 扩展为五种 disposition + 两级事故台账 |
| `src/telegram_kol_research/strategy_management_planner.py` | 参考价解析、数字处置、`planned_tpsl` 与 `target_snapshot` 写入 |
| `src/telegram_kol_research/strategy_management_executor.py` | 两处目标价 |
| `src/telegram_kol_research/strategy_management_composite_executor.py` | 一处目标价 |
| `src/telegram_kol_research/strategy_management_market_decisions.py` | 一处目标价 |
| `tests/test_break_even_reference.py` | 新增，49 例 |
| `tests/test_management_price_plausibility.py`、`tests/test_management_stop_price_gate.py`、`tests/test_strategy_management_planner.py`、`tests/test_strategy_management_executor.py`、`tests/test_strategy_management_market_decisions.py` | 回归与既有断言更新 |

## 2. 四处"保本目标价"的确切位置（规格 3.3）

全部改为 `break_even_reference.break_even_target_price(leg)`：

1. `strategy_management_executor.py`，`reserve_break_even_market_actions` 里的
   `assess_break_even_market(entry_price=...)` —— 决定 `set_break_even` 还是 `full_exit`；
2. `strategy_management_executor.py`，`_planned_stop_price` 的 break-even 分支 —— 实际写出去的 `slTriggerPx`；
3. `strategy_management_composite_executor.py`，`execute_protection_replacement_component` 里的
   `requested_stop`（原 `desired["avg_entry_price"]`）；
4. `strategy_management_market_decisions.py`，`_normalize_decisions` 里决策行与腿的一致性比对
   （原 `leg.avg_entry_price`）——必须与第 1 处同源，否则预约的价格与复核的价格会不一致。

## 3. 确认未触碰的身份 / 经济学比对

- `strategy_management_executor._preflight_exact_protection_positions`：交易所 `avgPx` 与
  `leg.avg_entry_price` 的漂移比对（由 `test_identity_still_compares_the_exchange_avg_px_to_our_own_fill` 钉住：
  交易所把 `avgPx` 报成参考价时必须仍然拒绝）；
- `break_even_convergence_executor.py`：整模块未改（规格 3.3 明确不在范围）；
- `break_even_shadow.py`：未改；
- `management_stop_price_gate.validate_batch_stops` 的 `entry_prices=[leg.avg_entry_price …]`（仅证据）；
- `strategy_management_planner` 中 `adjust_stop_loss` 闸门的 `entry_prices=[position["avg_entry_price"] …]`（仅证据）；
- `ManagementLegCreate.avg_entry_price`、`target_snapshot["positions"][*]["avg_entry_price"]`、
  `strategy_management_batches` 的 `desired_base["avg_entry_price"]`：写入与含义不变。

## 4. "有仓位的腿"在计划器里的取法

`_break_even_reference_for_batch` 取 `economics`（交易所预检后仍在仓、归属已验证、
未被能力延迟剔除的仓位）逐个映射回 `target_legs_by_pos_id[pos_id]`，
用该 `execution_order_legs` 行的 `leg_index` 组成集合。
全批次共用一个参考价——"两腿都成交 → 中点"是策略级事实，不是单仓事实。
生产上入场腿的 `leg_index` 是 1 / 2；任何其它取值（含测试夹具里的 0）落到回落行。

## 5. 与既有"保留更紧的已有止损"的相互作用

- 复合路径 `plan_composite_stop_replacement` 的 `keep_tighter_stop` **原样保留**：
  参考价按 tick 归一后若有一张"更紧且仍在现价有效一侧"的已有止损，仍然保留那一张。
  所以复合路径上 R1 不可能把止损改松——这是本次改动安全性的主要护栏。
- `break_even_by_market` 路径（`_adjusted_protection_rows`）**没有**这道护栏，今天也没有：
  它把已有止损行的触发价直接改写成目标价。详见第 8 节风险 1。
- `assess_break_even_with_existing_stop` 只被 `break_even_convergence_executor` 与
  `break_even_shadow` 使用，两者均不在范围，行为逐字不变。

## 6. 规格未覆盖、由本次自行决定的地方

1. **处置拆成两段执行，而不是一次调用。** 规格 3.4 写的是给
   `sanitize_management_prices` 增加 `side` 与 `break_even_reference` 两个入参。
   但参考价依赖"哪几条入场腿还有仓位"，那要等交易所预检跑完才知道；而"中和"必须发生在
   计划器的 `stop_action_conflicts` 之前（否则整条指令照旧被拒）。因此：
   - 第一段（原位置，闸门之前）：读一次报价，判定第 1、2 行，**无条件中和**所有显式价，
     其余留作 `PendingExplicitPrice`；
   - 第二段（`economics` 定稿后）：解析参考价，判定第 3、4 行。
   每个价格的 1→2→3→4 顺序完整保留，外部可见结果与规格表一致。
2. **第 1 行的高等级告警仍在第一段记录，低等级的 `management_price_disposed` 在第二段记录。**
   代价：若计划在两段之间被预检拦下（如 `target_position_snapshot_unavailable`），
   那条**低**等级记录会缺失。选择理由：保住今天已有的高等级告警行为不变，
   而低等级那条按规格本来就"不进任何 Telegram 类型表"。
3. **`side` 无法解析时落第 4 行（`superseded_by_strategy_price`），不新增第六种 disposition。**
   第 4 行本身就是规格的兜底行。同理，无法解析成正数的价格也落第 4 行。
4. **第 3 行的显式价校验通过一个注入的 validator 完成，`validate_management_stop` 以
   `action="adjust_stop_loss"` 调用。** 该函数里 `action` 只影响
   `stop_action_conflicts` 与一个证据字段；用保本类动作调用会立刻短路成冲突，
   而规格要的恰恰是"来源、偏离上限、方向"三项。没有 validator 时**一律不采纳**（fail closed）。
5. **回落参考价取"最保护的那个成交价"，不取第一条腿的。**
   多仓成交价不同时，回落情形下每条腿本来各按自己的均价保本；
   若拿其中一条去比"更紧"，一个夹在两者之间的数字会被采纳并把另一条腿的止损改松。
   因此空单取 min、多单取 max（`_most_protective_entry_price`）。单仓时与规格逐字一致。
6. **回落（`actual_fill_no_strategy_price`）时不往 `planned_tpsl_json` 写参考价字段。**
   写了也只是把 `avg_entry_price` 重复一遍，却会让一个行为完全相同的批次
   在存储上与改动前不同。证据仍写进 `target_snapshot["break_even_reference"]`。
7. **disposition 写在事故摘要的 `impact` 里，不新增摘要字段。**
   `runtime_incidents._SUMMARY_FIELDS` 是封闭表，新增键会让整条摘要被拒。
   形如 `explicit_price_treated_as_absent:superseded_by_strategy_price`。
8. **规格 4.2 里"现价 80600 的变体（复合，止损组件 operator_required）"用
   `_prepare_composite_protection_component` 的多单夹具实现**（参考价在现价之上 → 同一条终态分支），
   而不是重建一套 80500/80600 的空单复合夹具——该夹具的合约方向参与指纹校验，
   改方向等于重写整条夹具。空单 80500/80600 的几何另由
   `plan_composite_stop_replacement` 的直接断言覆盖。

## 7. 测试

- 三条生产样本回归（规格 4.2）先写、先看它们因正确的原因失败
  （17813 / 15475：`blocked / management_stop_action_conflict`；15402：快照缺 `break_even_reference`），
  再实施。
- 焦点套件（599 passed）：`test_break_even_reference`、`test_management_price_plausibility`、
  `test_management_stop_price_gate`、`test_strategy_management_planner`、
  `test_strategy_management_executor`、`test_strategy_management_market_decisions`、
  `test_strategy_management_market_policy`、`test_composite_management_fault_injection`、
  `test_break_even_convergence_executor`、`test_break_even_shadow`、
  `test_dabiaoke_tp1_break_even_regression`、`test_strategy_management_batches`、
  `test_strategy_management_worker`。
- 全量：见第 9 节。

### 因 R2 而必须改写的既有断言

| 测试 | 旧断言 | 新行为 |
|---|---|---|
| `test_no_usable_quote_means_no_check_at_all` | 报价不可用 → 原样返回 | → `ignored_quote_unavailable`，照常保本 |
| `test_a_plausible_price_is_returned_verbatim` | 合理价原样返回 | → 中和 + `pending`，由参考价定夺 |
| `test_explicit_break_even_stop_must_tighten_exact_live_position` | `management_stop_action_conflict` | → 更松 → `superseded_by_strategy_price`，批次 ready |
| `test_even_safe_explicit_break_even_stop_is_rejected_as_conflict` | 同上 | → 拆成"更紧被采纳"与"报价不可用照常保本"两条 |
| `test_plausible_composite_stop_still_conflicts_and_reads_no_ticker` | 同上 | → 减仓与止损都执行，合约为 `actual_entry_price` |
| `test_stop_gate_blocks_before_components…` 的 `move_stop_to_break_even` 行 | 同上 | 该行删除，原因写进表内注释 |

`management_stop_price_gate.stop_action_conflicts` 与 `validate_management_stop`
**一行未删**，其冲突用例保留为纵深防御，并新增
`test_a_neutralised_break_even_batch_passes_the_execution_recheck`
钉住"中和后的批次在执行前复核里既不冲突、也不需要任何读"。

## 8. 我认为规格里有问题 / 有风险 / 缺失的地方

1. **（最重要）`break_even_by_market` 路径没有"不得改松"的护栏。**
   `_adjusted_protection_rows` 把已有止损行的触发价直接改写成目标价，
   路径上只有 `_require_explicit_stop_write_boundary` 做收紧检查，而它只对
   `adjust_stop_loss` 生效。今天该路径就可能把一张更紧的已有止损改松（用我们的均价），
   R1 让这件事**更容易发生**：只成交贪婪腿的空单，策略价（区间下沿）必然高于我们的成交价，
   也就是更松。典型触发场景：同一策略先收到过一次保本（止损已在 80436），
   再收到第二条保本消息 → 止损被改到 80500。
   这与用户原则"不放宽止损"冲突，但规格 R3/3.5 明确要求这条路径"行为不变、不改"，
   所以**本次没有加护栏**。建议后续单独立项：把复合路径已有的
   `keep_tighter_stop` 语义搬到 `break_even_by_market` 上。
2. **规格 7.1 的"待决"与 R1 的关系没写死。** R1 等价于选项"甲"（跟 KOL 的价位），
   但 7.1 的选项"丙"（取 min(P, A) 中可挂的）恰好能消掉风险 1。
   若用户接受丙，风险 1 自动消失。
3. **`lifecycle.stop_loss` 不会被保本更新。** `_confirm_protection_lifecycle` 只在
   `planned_tpsl["stop_loss_text"]` 非空时回写 `lifecycle.stop_loss`，而保本类动作下它恒为 None。
   这是既有行为，规格未提，本次未改：改了会影响所有读 `lifecycle.stop_loss` 的展示与判定。
   后果是页面上策略的止损仍显示原止损（如 82300），而交易所上已是 80500。
4. **`entry_range_low/high` 是 `Float` 列。** 参考价由它经 `Decimal(str(value))` 得出，
   `80500.0` → `"80500"` 没问题，但一个尾数不整齐的区间端点会带出浮点尾巴
   （执行侧随后按 tick 归一，所以不会挂出非法价）。规格未提，本次照实取用。
5. **规格 4.2 要求断言"复合批次的止损目标 80500"**，但复合止损目标还要过
   `plan_composite_stop_replacement` 的 tick 归一与 `keep_tighter_stop`。
   本次按第 6.8 条拆成两层断言覆盖。

## 9. 全量测试与提交

见提交信息与最终汇报。
