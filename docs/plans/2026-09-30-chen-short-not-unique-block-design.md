# 陈哥 BTC 空单「target_live_position_not_unique」拦截：事实、根因与修复方案

- 日期：2026-09-30（时间均为 UTC）
- 对象：陈哥群 `-1002337721508`，lifecycle 1385，执行绑定 393，管理批次 191–194，值守提案 4–6
- 状态：**已批准（2026-09-30）：做 F1 + F2；F3 不做；两段式消息维持全平；167 / 175 / 191 历史记账不修。** 实施到候选 sha + 全量通过为止，不部署、不推 origin/main。
- 生产 HEAD：`bba228e3`（另一会话的 L2 观察窗进行中，本调查全程只读）

## 1. 一句话结论

**没有漏执行。** 陈哥的第一条消息（13:39:52）被判成 `full_exit`，程序在 13:40:11 市价全平，
交易所 13:40:19 以 84320.4 成交，毛利 +5.71 USDT。后面三条指令（保本 / 止盈 50% / 先减仓再保本）
被拦，是因为**仓位这时已经被我们自己平掉了**；`target_live_position_not_unique` 在这里的实际意思是
"目标 posId 在活仓里是 **0** 行"，不是"有两个仓位"。

真正的缺陷是另一处：**我们自己的平仓成交后，对账器用一条比执行器更严的身份判据把批次 191
冻结了**，于是它在 `recovery_required` 里挂了 60 分钟，最后被超时逻辑记成
`position_closed_before_management`（"仓位在管理前就没了"），入场腿被记成人工平仓，
绑定和 lifecycle 晚了一小时才关闭。批次 167（09-19）和 175（09-22）同一根因，
资金上没有损失，是记账和状态阻塞问题。

## 2. 事实时间线

来源：生产库只读点查（`strategy_lifecycles` / `execution_bindings` / `execution_order_legs` /
`position_protection_ledger` / `strategy_management_batches` / `strategy_management_notifications` /
`oncall_remediation_proposals`），Deepcoin 只读接口（fills、orders-history、trigger-orders-history、
positions-history、1 分钟 K 线），worker journal。

策略：空单，入场区间 85400–85600，止损 86900，止盈 84100 / 83700 / 82000，风险预算 20 USDT。

| 时间 | 事件 |
|---|---|
| 13:21:47 | 贪婪腿 E1（腿 671）市价 sell 6 张，成交 **85271.5**，posId `1001125415203997`（verified） |
| 13:21:50–13:22:17 | 挂止损 86900、备份止损 87073.8、止盈 84100×3 / 83700×1 / 82000×2 |
| 13:21:51–13:21:56 | 保守腿 E2（腿 672）限价 85510×6 挂出后被撤（`exchange_cancelled / cancel_regular_entry`），**0 成交、无 posId** |
| 13:39:52 | raw 20064「短线收益止盈出局…中长线止盈50%做成本保护止损改入场价…市价 84100 附近」 |
| 13:40:05 | raw 20065「先拿1400点利润，剩余止损修改入场价格」 |
| 13:40:11 | 批次 191 `full_exit` 规划并提交：市价 buy 6，`closePosId=…203997`，ordId `1001125415398457` |
| 13:40:19 | **交易所成交 84320.4，6 张全平**（reduceOnly）；5 张保护单随仓位作废，**无一触发** |
| 13:40:25 | 对账器把批次 191 冻结为 `recovery_required`，原因 `management_reconciliation_identity_mismatch`（通知行 149） |
| 13:40:37 | 批次 192 `move_stop_to_break_even` → blocked `target_live_position_not_unique`（目标快照 `positions: []`） |
| 13:40:47 | 值守提案 4 refused（`no_ready_action:blocked:target_live_position_not_exact`） |
| 13:41:19 / 13:41:32 | raw 20066「第一止盈位几分钟就到了」→ 批次 193 `partial_take_profit` → blocked（同上），提案 5 refused |
| 13:42:38 起 | 人工平仓清扫每轮看到账户确认为空，但因批次 191 仍占着该 posId（`recovery_required` 属于保留状态）而跳过绑定 393 |
| 13:44:24 / 13:44:46 | raw 20067「…建议做短线可以全部离场，其余的保留底仓做好成本保护」→ 批次 194 `partial_then_break_even` → blocked（同上），提案 6 refused |
| 14:40:14 | 60 分钟恢复超时：批次 191 → `resolved / position_closed_before_management` |
| 14:40:42 | 清扫接管：腿 671 → `manually_closed / manual_position_missing`，保护账本 5 行 retired，lifecycle → exited |
| 14:41:06 | 绑定 393 → `closed / entry_legs_terminal` |

这笔单：**2 条入场腿、1 个 posId**；E1 由我们的 `full_exit` 市价平仓结束，E2 从未成交。
止损、止盈都没有触发。

## 3. 是否漏执行，以及和其他做法的比较

规模：6 张 × 0.001 BTC = 0.006 BTC，每 100 点 = 0.6 USDT。

| 做法 | 结果 |
|---|---|
| **实际**（跟随「短线止盈出局 / 全部离场」，84320.4 全平） | 毛利 **+5.71**，手续费 0.61，净 **+5.10** USDT |
| 按「止盈 50%、剩余止损改策略入场价」执行，剩余仓位保留原止盈 | 3 张 @84320.4 = +2.85；第二止盈 83700×1 在 14:23 触及 = +1.57；剩 2 张至 16:17 仍持仓（价格没回到 85271.5 以上，第三止盈 82000 未到）。按 16:17 现价约 84120 计浮盈 +2.30，合计约 **+6.73**；若之后打到保本止损（策略入场 85400–85600）则为 +4.17 至 +3.77；若到 82000 则为 +10.96 |

- 两者差距在 ±1–5 USDT 之间，结果取决于剩余 2 张之后怎么走，**不构成"漏执行造成的损失"**。
- 实际做法符合用户规则「群里有离场意愿就跟着离场」，而且陈哥 13:44 那条自己也说「做短线可以全部离场」。
- 一处小代价：消息 13:39:52 发出，平仓 13:40:11 提交、13:40:19 成交，成交价 84320.4 接近那一分钟的高点
  （13:40 这根 K 线：高 84340.5、低 83973.9）。和陈哥报的 84100 相比，6 张约少赚 1.3 USDT，
  来自识别加规划的约 19 秒延迟，不属于本问题。

## 4. 根因

### 4.1 主缺陷：执行器与对账器对"入场腿集合是否精确"用了两条不一致的判据

- 执行器预检 `_require_exact_entry_legs`（`src/telegram_kol_research/strategy_management_executor.py` 约 4365 行）：
  入场腿只要是终态（`TERMINAL_ENTRY_LEG_STATES`）就跳过，不要求它有 posId。所以批次 191 通过预检，平仓单正常提交。
- 对账器 `_identity_is_exact`（`src/telegram_kol_research/strategy_management_reconciliation.py` 1026–1049 行）逐条检查绑定的全部入场腿：
  - 1026–1031：终态腿放行，**但要求 `pos_id` 非空**；
  - 1032–1038：无 posId 的终态腿、且不在 deferred 快照里时放行，**但显式排除了 `full_close` / `full_exit`**；
  - 1039–1048：deferred 快照里的腿；
  - 1049：其余一律 `return False`。
- E2（腿 672）是**规划前就被别的路径撤掉**的限价腿：终态、无 posId、不在 deferred 快照里。对 `full_exit` 它落到 1049 行，
  于是 `_identity_is_exact` 返回 False，批次在 242–250 行被冻结为 `recovery_required / management_reconciliation_identity_mismatch`。
- 1032–1038 的排除来自 `be6e3dfd`（2026-07-22「fix: reconcile partial exits with terminal legs」）。那次提交只是把部分平仓放开了，
  提交信息和测试都没有说明为什么 full_exit 要继续拒绝；看起来是范围收得过窄，而不是有意设防。
  而对 full_exit 真正要防的"还有别的活仓没被平"，另有 `_preflight_exact_position_identity`（活仓 posId 集合必须等于批次腿集合）和对账器逐腿按 posId 核对剩余数量把关，与这条腿无关。

### 4.2 冻结以后为什么一小时都出不来

- 对账器只扫描 `_ACTIVE_RECONCILIATION_STATUSES`（47–56 行），**不含 `recovery_required`**，冻结后不再确认。
- 人工平仓清扫（`execution_bindings.py` 4463–4466 行）跳过 `_active_management_reserved_pos_ids` 里的 posId，而保留状态显式包含 `recovery_required`（68–79 行）。所以绑定 393 被整整跳过一小时。
- 唯一出口是 `management_recovery_timeout.py` 的 60 分钟超时（`trading_settings.management_recovery_timeout_minutes`）。它只看"所有腿的 posId 都不在活仓"，就记 `position_closed_before_management`，**分不清这是我们自己平的还是别人平的**。
- 超时之后清扫接管，把腿 671 记成 `manually_closed / manual_position_missing`。**我们自己的平仓在账面上变成了人工平仓。**

### 4.3 为什么 13 个 full_exit 成功、这 3 个失败

近 30 天 full_exit 共 16 个：13 个 `succeeded / management_close_exchange_confirmed`，3 个是本缺陷（167、175、191）。
成功的 13 个里，第二条入场腿要么不存在、要么有自己的 posId（两腿都成交）、要么是**管理批次自己撤掉**的（`management_full_close_cancelled_unfilled_entry_leg`，能通过 deferred 快照放行）。
失败的 3 个全都是同一形态：**第二条限价腿在管理之前已被别的路径撤掉、无 posId**。

### 4.4 批次 192–194 的 `target_live_position_not_unique`

- 抛出点：规划器 `strategy_management_planner.py` 约 756–763 行调用 `position_attribution.canonical_live_position_economics`，
  后者 151 行要求**每个目标 posId 在账户活仓快照里恰好 1 行**。三种 intent 都在按 intent 分支之前被这一关拦下。
- 这时 posId `…203997` 已被平掉（0 行），但腿 671 因为 4.2 的原因还没被终态化，规划器仍把它选为目标腿。
- 原因码名不副实：0 行（仓位不在了）和 2 行（真的重复）用了同一个名字，调度会话和值守因此都往"两腿两 posId"方向理解。
- 值守补救提案的 `target_live_position_not_exact`（`position_management_remediation.py` 761–815 行）是同一事实的另一种说法。
- **"两条入场腿各自成仓、有两个 posId 所以不唯一"这个猜测不成立**：近 30 天有 13 个绑定 ≥2 个 posId，它们的 8 个管理批次**无一**被 `not_unique` 拦下；规划器、执行器、复合执行器本来就按 posId 逐腿处理多仓。
- 近 30 天 `not_unique` 一共 4 条批次：192、193、194，以及 176（09-22，绑定 368，也就是批次 175 那一笔）。**4 条全部是 4.1 的连带后果**，目标快照都是 0 行。

### 4.5 批次 191 为什么判成 full_exit

`management_directives._resolve_management_directive_unchecked`（约 358–374 行）：消息命中 `_FULL_EXIT_TERMS` 里的「止盈出局」，而排除词只有「剩余仓位 / 剩余持仓 / 其余仓位 / 剩下仓位」，没有「中长线」，所以直接返回 `full_exit`，排在部分平仓与保本分支之前。
这是一条两段式消息（短线全出、中长线减半加保本），分类器不分段。按用户规则「有离场意愿就跟着离场」，判成全平可以接受；是否要改见第 7 节问题 2。

## 5. 修复方案

### F1（核心，建议做）：对账器与执行器判据对齐

在 `_identity_is_exact` 中，对**不在 deferred 快照里**、**无 posId**、**已是终态**的入场腿，对所有 `effective_action`（含 `full_close` / `full_exit`）一律放行，也就是删掉 1033 行的 `full_close/full_exit` 排除，与执行器 `_require_exact_entry_legs` 的语义一致。

- 不放宽的部分：deferred 快照里的腿仍按原规则处理（full_exit 只接受管理批次自己撤掉的腿）；有 posId 的腿仍必须在 managed_identity 里，或终态且带 terminal_reason；`strategy_instance_id` 不符仍拒绝。
- 效果：同形态的 full_exit 在下一个对账轮就会确认为 `succeeded / management_close_exchange_confirmed`，入场腿记 `closed`（不是 `manually_closed`），绑定和 lifecycle 当场关闭，后续指令不会再撞上"半死不活"的腿。
- 交易所写入语义不变：只影响**成交之后**的确认和记账。

### F2（建议做，小）：把 0 行和多行拆成两个原因码

`canonical_live_position_economics` 在 0 行时报 `target_live_position_missing`，多于 1 行时仍报 `target_live_position_not_unique`。值守的 `target_live_position_not_exact` 同样细分。
这只改原因码的文字，不改拦截与否，也不改重试属性（两者都不在 `RETRYABLE_PREFLIGHT_BLOCK_REASONS` 里，blocked 仍是终态）。

**实施范围（2026-09-30 定稿）**，`git grep` 核对过，生产代码里没有任何地方按这两个字符串做分支判断：

| 位置 | 改法 |
|---|---|
| `position_attribution.canonical_live_position_economics`（151 行） | 0 行 → `target_live_position_missing`；>1 行 → 保持 `target_live_position_not_unique` |
| `position_mutation_gateway._build_fresh_authority`（1032 行） | 同上 |
| `position_management_remediation`（797 / 813 行） | 所有 verified 入场腿的 posId 都不在活仓 → `target_live_position_missing`；其余不精确情形保持 `target_live_position_not_exact`；`late_fill_identity_not_exact` 分支不变 |

**不改**：`strategy_management_composite_executor.py` 各处（执行期、结果是 `recovery_required`，1709 行已把 0 行当作已平）；`strategy_management_take_profit_consumption.py:312`（语义是"读不到实时数量"，`docs/composite-upstream-fix-status.md:361` 有定义）。

### F3（可选，待定）：已有全平在途时，后续管理指令直接视为"已被全平覆盖"

同一绑定已有一个提交过平仓的 full_exit 批次（`reconciling` 或 `recovery_required`）时，后续管理批次记 `resolved / superseded_by_pending_full_exit`，不记 blocked，也不建值守案件。
有了 F1，这种窗口只剩几秒到一轮对账（约 45 秒），F3 的收益主要是少几条误报。**建议先不做**，F1 上线后看是否还出现。

### 不在本方案内（记为后续）

- `management_recovery_timeout` 在判 `position_closed_before_management` 前，没有先核对批次自己的平仓单是否已在交易所成交。F1 之后这条路径基本不会被本形态触发，但其他冻结原因仍可能落到这里。
- `management_history_recovery` 为什么没有接管这个 `recovery_required` 批次（它自己的 `_durable_identity_is_exact` 没有这条多余检查），未查。
- 167 / 175 / 191 三笔历史记账（腿记成 `manually_closed`、批次原因名不副实）：不影响资金，**建议不修数据**；如要修，属于 L3 生产数据修复，需另行批准。

## 6. 风险级别与验证

- F1 + F2：改的是管理批次的**确认与记账**（持久消费者 / 恢复路径），不改交易所写入、不改 schema、不修数据 → **L2**。
- 开发期：聚焦测试 `tests/test_strategy_management_reconciliation.py`、`tests/test_strategy_management_executor.py`、规划器与值守相关测试；最终候选跑一次全量。
- 部署（另行批准）：`tg-deploy`，L2 观察窗 30 分钟 ≥5 条真实消息；检查无新增 `management_reconciliation_identity_mismatch`、无 `recovery_required` 积压。本形态（第二腿先被撤后 full_exit）近 30 天出现 3 次，正向样本要等自然出现。
- 回滚：`tg-deploy bba228e3…`（或部署时的前一个 sha），无状态需要回滚。

### 回放 / 回归用例（写成单元测试，数据取自生产形态）

1. **191 形态**：绑定两条入场腿，E1 市价 verified 有 posId、`active`；E2 限价 `exchange_cancelled / cancel_regular_entry`、无 posId、不在 deferred 快照；`full_exit` 提交后交易所快照显示该 posId 已平、有成交 → 期望批次 `succeeded / management_close_exchange_confirmed`，E1 → `closed`，绑定 closed。修复前同一用例应得 `recovery_required / management_reconciliation_identity_mismatch`（先写失败测试）。
2. **175 / 167 形态**：同 1，E2 撤单发生在规划前数小时（验证与撤单时刻无关）。
3. **仍须拒绝**：E2 在 deferred 快照里、仍 pending（未被管理批次撤）且 full_exit → 仍拒绝。
4. **仍须拒绝**：存在另一条 verified、有 posId、非终态、却不在批次腿里的入场腿 → 仍拒绝。
5. **仍须拒绝**：无 posId 终态腿但 `strategy_instance_id` 不同 → 仍拒绝。
6. **部分平仓不回归**：`be6e3dfd` 原有用例保持通过。
7. **F2**：目标 posId 0 行 → `target_live_position_missing`；2 行 → `target_live_position_not_unique`；值守文案与重试集合对应更新。

## 7. 需要用户拍板的问题

1. **修复范围**：做 F1 + F2（推荐），还是只做 F1？F3 是否先不做？ → **用户：F1 + F2，F3 不做。**
2. **两段式消息的分类**：像 raw 20064 这样「短线止盈出局 + 中长线止盈 50% 保本」的消息，继续按**全平**处理（现状，符合「有离场意愿就跟着离场」），还是改成**先减仓 50%、剩余止损改到策略入场价**？推荐维持现状，因为对跟单账户，这类消息里"短线出局"是明确动作，"中长线"是给另一类读者的建议。 → **用户：维持全平。**
3. **三笔历史记账**（167 / 175 / 191）是否不修？推荐不修，只在状态文档里注明。 → **用户：不修。**

## 8. 实施结果

- 候选代码 sha：`61339348`（分支 `claude/kind-jepsen-76240c`，基于 `9941109b` / `b822e856`）。**未部署、未推送任何分支。**
- 全量：`uv run python -m pytest -q -p no:cacheprovider`，**10936 passed / 4 skipped / 0 failed**（1178 秒）。与 `9941109b` 记录的 10918 相比净增 18 个用例，全部是本次新增。

### 8.1 改动文件

| 文件 | 改动 |
|---|---|
| `strategy_management_reconciliation.py` `_identity_is_exact` | F1：删掉无 posId 终态腿放行条件里的 `effective_action not in {full_close, full_exit}`；其余分支原样 |
| `position_attribution.py` `canonical_live_position_economics` | F2：0 行 → `target_live_position_missing`；>1 行仍 `target_live_position_not_unique` |
| `position_mutation_gateway.py` `_build_fresh_authority` | F2：同上（`PositionMutationAuthorityError`） |
| `position_management_remediation.py` | F2：`pos_ids` 为空（没有一条 verified 入场腿的 posId 在活仓）→ `target_live_position_missing`；其余不精确仍 `target_live_position_not_exact`；`cancel_entry` 的 `late_fill_identity_not_exact` 不变 |

未改：`strategy_management_composite_executor.py`、`strategy_management_take_profit_consumption.py:312`、`RETRYABLE_PREFLIGHT_BLOCK_REASONS`；`_planning_reason_from_attribution` 对新码原样透传（未命中任何前缀分支）。

### 8.2 测试

新增：

- `tests/test_strategy_management_reconciliation.py`
  - `test_full_exit_confirms_despite_pre_cancelled_unfilled_entry_leg`（4 组参数：191 形态 full_exit / full_close；167/175 形态撤单早于规划 6 小时；被**另一个**管理批次撤掉、不在本批次快照里的腿）→ `succeeded / management_close_exchange_confirmed`，E1 `closed / management_full_close_confirmed`，绑定 closed，lifecycle exited。修复前 4 组均得 frozen（先写失败测试已确认）。
  - `test_full_exit_still_rejects_snapshotted_deferred_entry_still_pending`
  - `test_full_exit_still_rejects_other_live_verified_entry_leg`
  - `test_full_exit_still_rejects_terminal_unfilled_leg_of_other_strategy`
- `tests/test_position_attribution.py::test_canonical_live_position_economics_separates_missing_from_duplicate`（0 行、只有别的 posId、2 行）
- `tests/test_position_mutation_gateway.py::test_fresh_authority_separates_missing_from_duplicate_live_position`（同上三例，且断言未发生交易所写入）
- `tests/test_position_management_remediation.py`：`test_management_step_reports_missing_when_no_target_position_is_live`（1 腿 / 2 腿）、`test_management_step_stays_not_exact_when_only_some_positions_are_live`、`test_management_step_stays_not_exact_for_duplicate_live_position`
- `tests/test_strategy_management_planner.py::test_missing_and_duplicate_target_positions_block_with_distinct_reasons`（0 行 → `target_live_position_missing`，2 行 → `target_live_position_not_unique`；两者都不在重试集合，快照恢复后再规划仍 blocked、同一批次）

修改的既有断言（1 处）：

- `test_full_close_reconciliation_rejects_other_terminal_deferred_entries` 删去参数组 `(snapshotted=False, "management_full_close_cancelled_unfilled_entry_leg")`。它断言"不在快照里、无 posId、已终态的腿对 full_close 要冻结"，正是 F1 要去掉的行为，与第 5 节 F1 规格直接冲突；该形态已移到上面新测试里改为断言成功。保留的参数组 `(snapshotted=True, "exchange_cancelled")`（快照内的腿不是管理批次撤的 → 仍冻结）不变。

F2 没有既有测试把 0 行场景断言成 `not_unique`，未改任何 F2 相关断言（`test_strategy_management_executor.py` 里的 `not_unique` 是 monkeypatch 注入的字符串，与抛出点无关）。

### 8.3 规格未写明、由实施者决定的地方

- 值守 F2 的"所有 verified 入场腿的 posId 都不在活仓"按 `pos_ids` 为空实现；当绑定上**根本没有**可管理的 verified 入场腿时 `pos_ids` 同样为空，也报 `target_live_position_missing`（原来报 `not_exact`）。
- `position_mutation_authority.build_position_mutation_authority` 用单行 `[live_position]` 调 `canonical_live_position_economics`；传入行的 posId 与目标不符时，原因码随之由 `not_unique` 变为 `missing`。只是文字变化，拦截与否不变。

### 8.4 同类判据核对（未改）

- `strategy_management_executor._require_exact_entry_legs`：终态腿对所有 action 一律放行，F1 后两边一致。
- `management_history_recovery._durable_identity_is_exact`：只逐条核对批次自己的腿（binding / strategy / posId / verified），不遍历绑定上的其他入场腿，**没有**同形排除，不存在 F1 的镜像缺陷。它为什么没有接管批次 191，仍按第 5 节"不在本方案内"待查。
