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
| `src/telegram_kol_research/strategy_management_executor.py` | 两处目标价；`6938fcbc` 追加"不得改松"护栏 |
| `src/telegram_kol_research/strategy_management_composite_executor.py` | 一处目标价 |
| `src/telegram_kol_research/strategy_management_market_decisions.py` | 一处目标价 |
| `src/telegram_kol_research/strategy_management_market_policy.py` | `6938fcbc` 新增共用比较 `stop_is_at_least_as_protective` |
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
- `break_even_by_market` 路径（`_adjusted_protection_rows`）原本**没有**这道护栏，
  今天也没有：它把已有止损行的触发价直接改写成目标价。
  **`6938fcbc` 补上了**，并且两条路径现在共用同一个比较函数
  `stop_is_at_least_as_protective`——详见第 11 节。
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

1. ~~**（最重要）`break_even_by_market` 路径没有"不得改松"的护栏。**~~
   **已由 `6938fcbc` 关闭**，见第 11 节。原文保留作为记录：
   `_adjusted_protection_rows` 把已有止损行的触发价直接改写成目标价，
   路径上只有 `_require_explicit_stop_write_boundary` 做收紧检查，而它只对
   `adjust_stop_loss` 生效。R1 让这件事更容易发生：只成交贪婪腿的空单，
   策略价（区间下沿）必然高于我们的成交价，也就是更松。典型触发场景：
   同一策略先收到过一次保本或 TP1 自动保本（止损已在 80436），
   再收到第二条保本消息 → 止损会被改到 80500。
2. **规格 7.1 的"待决"与 R1 的关系没写死。** R1 等价于选项"甲"（跟 KOL 的价位），
   第 11 节的护栏把落地结果变成了 7.1 选项"丙"的一个更强版本：挂出去的止损取
   "已有止损与策略价中更保护的那个"，且**逐行**判定。用户若仍想明确拍板 7.1，
   现状已经是丙。
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

- 提交：`d9fc6300`（主体，13 个文件）、`6938fcbc`（第 11 节的护栏，4 个文件）。
- 全量（`d9fc6300`）：**9357 passed, 4 skipped, 0 failed**（711.71 s）。
- 全量（最终候选 `6938fcbc`）：
  `uv run python -m pytest -q` → **9393 passed, 4 skipped, 0 failed**（704.12 s / 11 分 44 秒）。
  注：`uv run pytest`（不带 `python -m`）在收集阶段即失败，是既有问题，与本次无关。
- 未推送、未部署。回滚即"不部署本提交"；若已部署，回滚为 `tg-deploy <上一个生产 sha>`，
  要求零在途批次（跨版本执行同一批次会让参考价字段被旧代码忽略）。

## 10. 首笔实盘样本必须逐项手工核对的项

1. 该策略的 `entry_range_low/high` 与成交的入场腿 `leg_index`，对照
   `target_snapshot["break_even_reference"]` 的 `source` / `price` 是否符合 R1。
2. 交易所上新挂的止损触发价 = 参考价按 tick 归一后的值（不是我们的 `avgPx`）。
3. 旧止损已撤净；减仓数量与 `planned_close_size` 一致。
4. `strategy_management_legs.avg_entry_price` 仍等于交易所 `avgPx`（身份未被污染）。
5. 若消息带了数字：`price_plausibility.removed[*].disposition` 是否为预期的那一行；
   若是 `explicit_tighter_adopted`，确认挂出去的正是消息里的价格。
6. 若已有止损比参考价更保护：确认**交易所上那张单一个字都没动**（没有撤、没有新单），
   且 `strategy_management_legs.request_json` 的 `break_even_stop.disposition` =
   `kept_tighter_existing_stop`，`rows[*].kept` 与实际相符。
   若是 `replaced_with_break_even_target`，确认新价 = `target_price` 且严格比
   `old_trigger_price` 更保护或相等。

## 11. 追加：`break_even_by_market` 的"不得改松"护栏（2026-09-21，关闭第 8 节风险 1）

指挥会话确认："规格 R3 的'不要改'针对的是目标价挂不上时的行为，不是放宽止损的许可"，
因此该护栏纳入范围。

### 规则

逐行判定，只在 `break_even_by_market` 路径生效：
对每一条带止损的已有行（`stop_loss`、`backup_stop`、以及 `combined` 行的止损半边），
若其现价**至少与保本目标同样保护**（空单 old ≤ target；多单 old ≥ target）
**且**仍在现价的有效一侧（空单 old > 现价；多单 old < 现价）→ **保留原价**；
否则照旧写入目标价。比较用 `Decimal`；无法解析的旧价一律不算"更紧"（走目标价）。

### 共用比较

新增 `strategy_management_market_policy.stop_is_at_least_as_protective(existing, target, side, market_price)`，
并把 `plan_composite_stop_replacement` 里原来那段 `tighter = [...]` 列表推导改为调用它
（**逐字等价**，复合路径行为不变，其既有测试全部原样通过）。
执行器侧由 `_break_even_row_stop_price` 调用，market_price 取
`BreakEvenMarketDecisionRecord.quote_price`——即该批次保本决策本身预约时用的那个报价。
**没有 market_price 就不保留**（fail closed），因此通用保护路径与 `adjust_stop_loss` 逐字未变。

### 不写交易所

`_partition_replacement_rows` 增加 `retain_unchanged_stops`（只有保本路径传 True）：
被护栏留下的止损行与旧行逐字相同 → 进 `retained_rows` → **既不撤旧、也不挂新**，
账本按同一个 `order_id` 重新确认一次（与今天"未变的止盈"完全同一套机制）。
全部保留时 `replacement_rows` 为空，`_cancel_old_protection_after_replacement` 空转，
腿仍走 `reserved → succeeded`，批次 `succeeded / all_position_protection_replaced`。

`combined` 行**不纳入保留**：`_partition_replacement_rows` 比的是顶层 `trigger_price`，
而 combined 行的止损嵌在 `stop_loss` 里；为它写第二套相等判据不值得。
价格仍按护栏取（不会改松），只是会以同一个价格重挂一次——一次浪费的写入，没有行为风险。

### 证据

腿预约时写进 `request_json.break_even_stop`：
`disposition`（`kept_tighter_existing_stop` / `replaced_with_break_even_target`）、
`target_price`、`market_price`、以及每行的
`order_id / purpose / old_trigger_price / trigger_price / kept`。
先于任何交易所写入落库，所以写失败时也查得到。

## 部署记录（2026-09-21 20:06 CST）

- 用户 2026-09-21 明确批准部署。候选 `81fdc58a`（`d9fc6300` 实现 → `6938fcbc` 不得改松护栏 → 文档），最终候选全量 **9393 passed / 4 skipped / 0 failed**。
- 部署前：候选是生产 HEAD `840c83ba` 的后代、共享分支 tip 是候选的祖先（两项 PASS）；零在途（管理批次 / mutation intent / claimed job / worker command 均为 0）；
  当时在仓：BTC 多 1、ETH 多 1。
- `tg-deploy 81fdc58a…` → worker / web / ingest 均 active，web 200。**回滚 = `tg-deploy 840c83ba8e563a23f67708c7ed481e73bc667597`。**
- **自动交易开关未动**：`auto_trade_enabled=true`、`management_execution_mode=live`、`composite_management_v2_mode=live`，设置行 `updated_at` 仍为 2026-09-08。
- 部署后：新消息照常处理（raw 18151 之后的作业 succeeded），worker 自重启起错误行 0；值守两个单元 active、心跳正常。
- 待办：首笔命中新规则的真实保本消息按本文件的清单逐项核对（减仓数量、新止损 = 参考价按 tick 归一、旧止损撤净、`avg_entry_price` 仍等于交易所 `avgPx`、disposition）。
- 用户同日追加两项决定（另行实施）：①复合指令（减仓后保本）止损挂不上时，剩余仓位也按市价全平；②TP1 成交后的系统自动保本也改用策略价。

## 12. 追加：复合减仓后保本挂不上 → 剩余仓位市价全平（2026-09-21，关闭第 7.1 项）

设计（唯一规范）：`docs/plans/2026-09-21-composite-remainder-market-close-design.md`，方案 A。
分支 `worktree-agent-a17607ec0373071bc`
（worktree `/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a17607ec0373071bc`）。
基线 `6242df26`（`codex/deepcoin-auto-trading-v1` 的 tip；生产为 `81fdc58a`，其后均为文档）。
验证等级 **L3**（真实交易所写入语义变化）。状态：本地实施完成，
**未推送、未部署、未连服务器、未发 Telegram / MQTT**。

### 12.1 改动清单

| 文件 | 性质 |
|---|---|
| `src/telegram_kol_research/strategy_management_composite_executor.py` | `except` 分流 + 兜底全平的全部新函数 + `_complete_composite_batch` 改写 |
| `src/telegram_kol_research/strategy_management_composite_reconciliation.py` | `_reconcile_remainder_close_component` 及其分派（只读） |
| `src/telegram_kol_research/production_safety_monitor.py` | 设计 3.6 的跳过 |
| `tests/test_composite_remainder_market_close.py` | 新增 |
| `tests/test_strategy_management_executor.py` | 夹具补 `identity` + ticker；`_CompositeRemainderCloseClient`；改写"参考价被越过"的既有用例 |
| `tests/test_production_safety_monitor.py` | 设计 3.6 的两行（跳过 / 不跳过） |
| `tests/test_composite_management_fault_injection.py` | 七条崩溃注入别名 |
| 文档 | 本节；规格 3.5 / 7.1 的指向 |

**未改**：`market_policy.py`、`position_mutation_gateway.py`、
`strategy_management_components.py`、`contracts.py`、`batches.py`、计划器、适配器、
任何设置或开关、schema。无新组件种类、无新状态值。

### 12.2 兜底分叉点与六个触发条件

分叉点只有一处：`strategy_management_composite_executor.execute_protection_replacement_component`
的预检 `except (ValueError, ManagementSizingError, BreakEvenMarketPolicyError, RuntimeError)`
分支内，在原来的 `terminal = reason in {...}` 之前。六个条件按顺序：

1. `str(exc) == "requested_stop_market_side_invalid"`（该字符串只由
   `plan_composite_stop_replacement` 的市价侧检查抛出）；
2. `contract.stop_mode == "actual_entry_price"`（`explicit_price` 合约无兜底，
   指挥裁决 3）；
3. `desired` 里没有 `protection_replacement_execution`（本组件从未开始挂新止损）；
4. 减仓已收敛——由 `except` 之前既有的 `partial_close_component_not_converged`
   检查保证；同时 `live_position` / `requested_stop` / `market_price` 三个局部变量
   都必须已赋值，否则退回旧分支；
5. `live_execution_gate()` 为真（与减仓同一道闸；不读不写任何开关）。
   为假 → `preflighting → recovery_required / live_execution_disabled`；
6. 新鲜 ticker 二次确认（指挥裁决 1 保留）：`get_ticker_quote`，校验
   `instrument_id` / `price` 非空 / `price_field ∈ {last, lastPx}` 三项与
   `strategy_management_executor` 的保本取价逐字相同；再用该价、
   `existing_stop_prices=()` 调一次 `plan_composite_stop_replacement`。
   再抛 `requested_stop_market_side_invalid` → 进入兜底；
   正常返回 → `recovery_required / break_even_market_side_disagreement`；
   报价不可用或非法 → `recovery_required / break_even_market_quote_unavailable`。

### 12.3 幂等键与 clOrdId

- 幂等键：`f"{component.id}:close:remainder:attempt:{attempt}"`。
  保留 `:close:` 片段，让监控的 `duplicate_composite_close_submission`
  （按 `:close:` 之前的前缀分组）自动覆盖这笔写入；`{component.id}:` 前缀让
  复合对账器的 LIKE 查询取得到它。
- clOrdId：`f"CM{batch.id}L{leg.id}R{attempt}"`（`leg.id` 是管理腿 id，与减仓的
  `…A{n}` 同源同形状，只换后缀字母）。长度 ≤ 20。`R` 与减仓的 `A` 不会相撞——
  网关在回包缺 `ordId` 时按 clOrdId 对账，两者必须可区分。

### 12.4 崩溃注入覆盖（设计第 4 节逐行）

| 设计第 4 节的崩溃点 | 用例 |
|---|---|
| 减仓已确认、保护组件尚未认领 | 生产形状用例本身（组件从 `pending` 起跑） |
| 决策落库之前 | `test_a_fresh_quote_that_disagrees_...`、`test_an_unusable_quote_closes_nothing`（断言 `desired` 里没有 `remainder_close_execution`，零写入） |
| 决策已落、撤挂单途中 | `test_restart_during_the_entry_cancel_stops_for_a_person`（对账器）+ `test_a_resume_that_never_cancelled_the_entry_legs_stops_for_a_person`（执行器） |
| intent 为 `reserved` | `test_restart_with_a_reserved_intent_blocks_it_and_allows_a_new_attempt`（intent 置 `blocked`，下一次用新 attempt 键） |
| intent `submitting` / `submitted` / 未知 | `test_restart_with_an_unknown_intent_stays_awaiting_and_writes_nothing`、`test_an_unknown_close_outcome_is_never_resent`（两次执行，一次写入） |
| 组件已 `confirmed`、批次还没完成 | `test_the_books_are_terminalized_in_the_same_transaction_as_the_success` |

七条已在 `tests/test_composite_management_fault_injection.py` 里建别名，纳入同一道部署门。

### 12.5 兜底路径不动保护单；设计第 5 节不变

- `test_composite_protection_closes_the_remainder_when_the_reference_is_passed`
  （原 `..._operator_required_when_the_reference_is_passed`，按设计第 8 节改写）：
  断言事件序列里没有任何 `set_`、没有任何 `cancel_`、恰好一次 `close`，
  并断言 `stop-old-primary` / `stop-old-backup` 两条账本行仍是 `verified`。
- 生产形状用例：`cancel_position_sltp` 只被调用过一次且是被消费的第一止盈；
  两张原止损在交易所挂单表里原样健在；`set_position_sltp` 一旦被调用即 `AssertionError`。

第 5 节里与被改 `except` 同分支的去向，逐条回归（全部用**已被越过的参考价**作输入，
所以护栏真的被考到）：

| 原因码 | 用例 |
|---|---|
| `requested_stop_market_side_invalid`（`explicit_price` 合约） | `test_an_explicit_message_price_that_cannot_arm_still_stops_for_a_person` |
| `requested_stop_market_side_invalid`（已开始挂新止损） | `test_a_component_that_already_started_placing_stops_never_closes` |
| `retained_take_profit_exceeds_position` | `test_a_retained_take_profit_larger_than_the_position_still_stops_for_a_person` |
| `position_size_increased_after_snapshot` | `test_a_drifted_position_keeps_its_old_disposition_even_when_passed[11]` |
| `position_below_target_remaining` | 同上 `[4]` |
| `partial_close_component_not_converged` | 同上 `[6]` |
| `positions_snapshot_incomplete` | `test_an_unreadable_position_never_reaches_the_fallback[snapshot_incomplete]` |
| `target_live_position_not_unique` | 同上 `[position_missing]` |
| `break_even_market_price_invalid` | `test_a_position_row_without_a_price_never_reaches_the_fallback` |
| `protection_replacement_retry_exhausted` | `test_the_retry_cap_is_shared_with_the_protection_route_unchanged` |
| `duplicate_new_stop_order_id`、`replacement_stop_readback_unresolved`、`old_stop_cancel_*`、终检不变量、`_load_component` 的五个 | 既有用例原样通过（未改断言） |

### 12.6 设计未覆盖、由本次自行决定的地方

1. **二次确认时 `plan_composite_stop_replacement` 抛出的其它原因**（例如报价解析不出
   正数 → `break_even_market_price_invalid`）一律归到
   `recovery_required / break_even_market_quote_unavailable`。理由：设计 3.1 第 6 条
   已经把"报价不可用**或不合法**"指到这个原因码，不新增第七种。
2. **平仓后回读 `list_positions` 失败** → `awaiting_exchange /
   remainder_close_post_write_snapshot_incomplete`，仿减仓组件的
   `partial_close_post_write_snapshot_incomplete`。设计未提；选"绝不重发"一侧。
3. **步骤 3 的交易所快照读失败** → `submitting → recovery_required /
   remainder_close_snapshot_incomplete`（此刻未发出任何平仓，可以安全重试）。
4. **`pos` 解析不出数字** → `recovery_required /
   remainder_close_position_size_invalid`，而不是当成"已平"。
   把不可读当成已平会让一条没发生过的平仓被记成成功。
5. **持久化决策本身失败** → `preflighting → recovery_required /
   remainder_close_plan_not_persisted`；续跑时持久化冲突 →
   `recovery_required / remainder_close_resume_conflict`。两者都在任何交易所写入之前。
6. **网关调用整体包了一层 `except`**（`before_submit` 写库失败等）→
   `awaiting_exchange / remainder_close_outcome_unknown`。此时 intent 必为
   `reserved` 或 `submitting`，对账器分别置 `blocked`（可重发新键）或继续 `awaiting`
   （绝不重发），两条都不会造成双平。
7. **步骤 3 的"未决平仓 intent"判据不排除自己**：与网关
   `_has_other_unresolved_close` 同谓词（`pos_id` + `execution_order_leg_id` +
   `operation='close_position'` + 四个未决状态）。不排除任何 id 只会让我们多等，
   绝不会让我们多平。
8. **`primary_stop` 证据字段在复合执行器内本地按 tick 归一算出**
   （`_tick_normalized_stop`，多单 FLOOR / 空单 CEILING）。
   `plan_composite_stop_replacement` 是在返回之前抛的，拿不到它算的值；
   设计第 6 节又要求不改 `market_policy.py`。该值**只进证据**，
   不参与任何判定、不写向交易所。
9. **事故的 `source_kind` 取 `strategy_management_component`、
   `source_record_id` 取组件 id**，指纹为 `类型:组件id`（同一组件重复记录会合并
   `repeat_count`，不会刷屏）。设计只说"仿 `_record_price_finding_incident`"。
10. **完成通知里的"保本价 X / 市价 Y"取自组件证据的 `requested_stop` 与
    `ticker_last`**（即真正做决定的那两个数），不取仓位行的 markPx。
11. **`_terminalize_full_close` 的身份校验**按设计 3.4 实现为三条：生命周期
    `entered`、`exit_reason is None`、绑定 ∈ `{open, active, stale}`。
    未加 `_identity_is_exact` 的其余条款（那会把一个只读校验变成复制品）。

### 12.7 我认为设计里有问题 / 有风险的地方

1. **（新发现，不在设计里）同一合约的两个仓位无法同时跑
   `consume_take_profit_stage`。** `plan_take_profit_consumption`
   （`strategy_management_take_profit_consumption.py:89-101`）把**该合约上全部**
   pending 止盈行拿去和**单条腿**的账本比对，另一个仓位的止盈行必然
   `order_id not in owned_by_id` → `take_profit_order_identity_conflict`。
   split 模式下两条入场腿都成交时，复合批次就是这个形状。
   这是既有行为，与本次改动无关，也不在设计范围内，但它意味着
   **双腿复合批次今天根本走不到止损这一步**。请指挥单独核实生产是否出现过。
   本次的双腿用例因此从"减仓已完成"起跑（见 `_settle_reductions` 的注释）。
2. **设计第 9.1 条的监控潜伏误报没有被本次关闭。** 3.6 只跳过带
   `remainder_closed_at_market` 标记的组件；任何**普通**复合成功批次在其仓位
   日后离场、账本被 A-17 置 `retired` 之后，仍会永久报
   `composite_position_without_verified_stop`（critical）。设计把根治列为可选。
   建议指挥按设计 9.1 的三步只读核实后单独处理。
3. **`_has_other_unresolved_close` 的等待可能吃掉三次尝试里的第一次。**
   指挥裁决 6 是不加独立计数器。内联路径上减仓 intent 往往在下一轮对账才确认，
   所以第一次尝试大概率消耗在等待上，只剩两次真正的平仓机会。
   首笔实盘样本要特别看这一点。
4. **市价单无滑点上限**，与既有 `full_exit` 相同（设计第 10 节问题 8 已知悉）。

### 12.8 首笔实盘样本必须逐项核对

1. 减仓数量 = `planned_close_size`；平仓数量 = 减仓后的**实际**剩余量。
2. 未成交入场腿已撤：`execution_order_legs` 该腿 `cancelled` +
   `terminal_reason = management_full_close_cancelled_unfilled_entry_leg`，
   且交易所挂单表里确实没有了。
3. **交易所上没有任何新止损单**，两张原止损在平仓前一直挂着、平仓后随仓位消失
   （不是被我们撤的）。
4. 账面四处：入场腿 `closed` / 绑定 `closed`（若还有其它在仓腿则 `active`）/
   账本与保护腿 `retired` / 生命周期 `exited` + `exit_reason='kol_signal'` +
   `management_action='full_close_confirmed'`。
5. 批次 `succeeded` + `reason_code='composite_remainder_market_closed'`；
   值守对该 raw 只有**一个**案件键且已清案。
6. 组件证据里的 `requested_stop` / `ticker_last` / `position_market_price`：
   确认 `ticker_last` 与 `position_market_price` 是否一致；不一致的话记下差值，
   这是设计 1.3 里"仓位行最多旧一轮"的第一份实测。
7. `runtime_incidents` 里恰好一条 `composite_break_even_remainder_closed`（low）。
8. 尝试次数：看组件 `attempt_count`。若第一次就消耗在
   `remainder_close_waiting_partial_close_confirmation` 上，记下来（见 12.7 第 3 条）。

## 13. 追加：复合路径在正常路线上也执行 `cancel_deferred_entries`（设计 9.2）

**单独一次提交**，在第 12 节之后。指挥可以只部署第 12 节而不带这一节。

### 13.1 问题

合约对 `partial_then_break_even` 恒为 `cancel_deferred_entries=True`
（`management_directives.py:481-486`，按 intent 判定，不是写死的 True），
非复合减仓路径无条件调用 `_cancel_deferred_entry_legs`
（`strategy_management_executor.py:1497`），**复合执行器里一行相关代码都没有**。
后果：KOL 说"减仓并把止损移到成本"，减仓照做，但挂着的第二条入场腿仍然有效，
它之后成交就会把刚刚减掉的仓位重新加回去。

### 13.2 改动

`execute_composite_management_batch` 里新增一处调用，位置与非复合路径同构：
**批次认领之后、组件循环（任何写入）之前**。

- 读合约的 `cancel_deferred_entries`，为假则什么都不做（不是写死 True——
  该字段按 intent 取值，复合合约今天恒为 True，但判据留在合约里）。
- 复用 `_cancel_batch_deferred_entry_legs`（由第 12 节的
  `_cancel_remainder_deferred_entries` 改名而来），兜底全平路线也走同一个函数。
- 失败语义与非复合路径逐字相同：`DeepcoinDefiniteRejection` →
  `deferred_entry_cancel_race_detected`，其余 →
  `deferred_entry_cancel_preflight_failed`；批次 `recovery_required`，
  此刻尚未发生任何写入。

### 13.3 唯一新增的东西：幂等

非复合路径每次执行只跑一次撤单然后就离开了；**复合批次执行器每个 worker tick 都会重入**。
所以第二个 tick 会看到腿已经是 `cancelled`，而
`_load_exact_deferred_entry_legs` 对此是 fail-closed 的（`deferred_entry_cancel_leg_not_pending`），
批次会被无辜冻结。因此加了一道守卫：

- 用**权威解析器** `_parse_exact_deferred_entry_leg_ids` 读快照——
  快照坏掉仍然 fail closed，"没有挂单腿"和"读不出来"不会长得一样；
- 名单为空 → 真正的空操作（零读、不加载 binding）；
- 名单里**每一条**都已是 `_is_management_cancelled_deferred_entry_leg`
  （`cancelled` + 我们自己的 `terminal_reason` + 无 pos_id）→ 跳过；
- **部分完成不算**：那会落到真正的撤单里，由它 fail closed。

### 13.4 语义变化（上线即生效）

一个今天能跑完的复合批次，若其挂单入场腿撤不干净（撤单被拒、腿刚成交、
身份漂移），**现在会在做任何事之前冻结**。这正是非复合路径的行为，
也正是合约要求的；而"撤不掉"这个状态下继续减仓、同时可能有第二条腿成交，
是真正无法判断的局面。

### 13.5 测试

先写、先看它们因正确的原因失败（把那行判定临时改成 `if False and ...`，
五条红、两条绿），再放开：

| 用例 | 钉住 |
|---|---|
| `test_a_resting_entry_leg_is_cancelled_before_any_write_of_the_batch` | 撤单发生在第一次 `cancel_position_sltp` 与 `place_order` 之前；腿落 `cancelled` + 正确的 `terminal_reason` |
| `test_the_entry_cancel_runs_once_across_the_batchs_many_ticks` | 跑两遍批次，`cancel_order` 恰好一次，两次都 `succeeded` |
| `test_a_batch_with_no_resting_entry_leg_reads_and_cancels_nothing` | 名单为空 → `list_open_orders` 零次 |
| `test_a_refused_entry_cancel_freezes_the_batch_before_any_position_write` | `deferred_entry_cancel_race_detected`，零 `place_order`、零 `cancel_position_sltp` |
| `test_an_entry_leg_that_already_filled_freezes_the_batch` | `deferred_entry_cancel_preflight_failed`，零写入 |
| `test_an_unreadable_identity_snapshot_fails_closed` | 快照缺 `deferred_entry_leg_ids` → 冻结，零调用 |

### 13.6 首笔实盘样本追加两项

9. 正常路线（保本止损**挂得上**）的复合批次：确认挂单入场腿已撤、
   交易所挂单表里确实没有了，且撤单发生在减仓之前。
10. 若该批次没有挂单入场腿：确认日志/事件里没有任何多余的挂单读取。
