# 止损阶梯（stop ladder）实施状态

- **阶段 1（L1）**：已完成实现，**未部署、未推送、未连服务器、未触碰交易所**。
- **分支 / worktree**：`worktree-agent-a1225b893f7d540cf`
  （`/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a1225b893f7d540cf`）
- **基线**：`codex/deepcoin-auto-trading-v1` 尖端 `aff2f0e5`（生产运行 `a79a387b`，其后均为文档提交）。
- **约束性文档**：`docs/plans/2026-09-23-stop-ladder-phase1-spec.md`（规格，冲突时以它为准）、
  `docs/plans/2026-09-21-stop-ladder-policy.md`（用户规则与第 4 节拍板）、
  `docs/plans/2026-09-21-stop-ladder-design.md`（设计）。

| 提交 | 主题 |
|---|---|
| `680791fc` | 两处读取缺陷 + 共享判据的 B 型证据（`take_profit_fill_predicate` / `take_profit_fill_evidence` / 观测归桶） |
| `70c4d277` | `stop_ladder.py` / `stop_ladder_records.py` / 设置 / 影子 / 消息列证据 / 放行报告 / 值守计数 |
| （本提交） | 本文档与 `docs/ARCHITECTURE.md` 4.8 |

**最终候选全量**：`uv run python -m pytest -q` → **9650 passed, 4 skipped, 107 warnings,
759.03s (0:12:39)，退出码 0**。基线（`aff2f0e5`，F2 部署记录里的 9540/4）净增 110 条。
（`uv run pytest` 仍在收集阶段失败，既有问题，与本次无关。）

---

## 1. 本阶段实现了什么

### 1.1 档位（rung）与"已到"判定

- **档位 = 该仓位保护账本 `purpose in {take_profit,tp,profit}` 的行，剔除
  `retired/cancelled/canceled/superseded`，按盈利方向排序**（多单升序、空单降序，同价按
  `order_id` 决定顺序）。第 1 张 = 第一档。**不再对照策略文本或 binding draft**，KOL 改价时
  系统撤旧挂新，序列自然跟随；`filled` 的行仍然留在序列里（它就是要数的那一档）。
- **一档"已到"**（`stop_ladder.rung_reached`），四条排除 + 共享判据：
  1. 账本行状态不是 `retired/cancelled/canceled/superseded` → 否则 `stop_ladder_ledger_row_is_history`；
  2. 该单号没有我们的 cancel 类 `PositionMutationIntent`
     （`cancel_position_sltp` / `cancel_trigger_order`，**任何状态都算**）→ 否则 `stop_ladder_cancel_intended`；
  3. 本轮挂单快照对该品种**完整**（`snapshot_errors` 为空且该品种的 pending 观测完整）→
     否则 `stop_ladder_pending_snapshot_incomplete`；
  4. 该单号**不在**挂单快照里 → 否则 `stop_ladder_order_still_pending`；

  然后交给 `take_profit_fill_predicate.take_profit_fill_proven`：
  - **A 型**：trigger 历史里有该单号、`triggerTime ≠ 0`、`errorCode ∈ {"", "0", "00000"}`
    → `evidence_form = "trigger_history"`；
  - **B 型**：历史里**查不到该单号**，且相邻两条 `snapshot_complete` 观测显示该仓位数量
    **有减少（任意数量）** → `evidence_form = "position_decrease"`；
  - 历史行存在即由它决断：**触发失败（errorCode 非零）拒绝、未触发拒绝**，都不会被仓位减少翻案；
  - **不比较数量**（部分成交仍算已到，用户拍板 3）。
- 判定不出 = 档位不变，**不告警**，只计数（`StopLadderReconcileResult.unproven` 与各 `reason_code` 计数）。

### 1.2 账本 `filled` 的唯一写入者

F2 已经让 `protection_health` 对证成成交的止盈账本行写 `filled`。本阶段**没有新增第二个写法**：

- 判据统一在 `take_profit_fill_predicate.take_profit_fill_proven`（A 型不变，新增可选的 B 型参数
  `position_decrease_proven`，默认 `None` 即旧行为）；
- 写入统一在**新的公开函数 `protection_health.record_take_profit_ledger_fill(row, *, order_id, evidence, observed_at)`**，
  `protection_health` 自己的分支与 `stop_ladder_records` 都调用它，字段与旧写法逐字兼容
  （`evidence_json["take_profit_fill"]`，新增 `level` / `evidence_form` / `decided_at` /
  `observation_ids`；`protection_health` 那条现在也带 `evidence_form`）；
- 无 schema 变更；`status` 仍是 `String(32)`；`docs/composite-upstream-fix-status.md` 第 3 节的
  读取者审计仍然成立（没有任何活跃集合包含 `filled`，成交的止盈对所有自动路径不可见）。

### 1.3 两处读取缺陷

- `take_profit_fill_evidence._prove_exact_terminal`：真实 trigger 历史行没有 `posId`，缺字段的行
  **跳过而不是直接 failure**，让"仓位减量"那一层有机会作答；所有匹配行都不可判时返回 `None`。
  同时新增：**触发失败（errorCode 非零）立即拒绝 `tp1_exact_trigger_failed`**，所以这次放行
  不可能把一次失败触发变成成交。
- `execution_bindings._record_owned_position_observations`：TPSL 行没有 `posId` 时，按**账本
  `order_id → pos_id`** 归桶（与 `protection_authority` 同一判据），`pending_tpsl_json` 不再恒为 `[]`；
  一个单号若映射到两个仓位则不归属（账本自相矛盾时按"无归属"读）。

### 1.4 对账

`execution_bindings.reconcile_deepcoin_execution_bindings` 在**现有止盈对账之后、
`protection_health` 之前**调用
`stop_ladder_records.reconcile_take_profit_fill_levels`（只读交易所快照、只写订单级证据）。
放在 `protection_health` 之前是刻意的：本轮证成的档位在健康检查看到它之前就已经是 `filled`，
不会再被报成 `protection_missing`。

### 1.5 策略档位与目标价（纯函数，`stop_ladder.py`）

- 策略档位 N = 各在仓腿已到档位的**最大值**（拍板 1）。
- 目标：N=0 不动；N=1 → `resolve_break_even_reference`（已上线的 R1 策略入场参考价）；
  N≥2 → **该仓位序列里第 N−1 张止盈单的触发价**；两腿都到 N 档取更保护的（多单取高、空单取低）；
  只有"确实到了 N 档"的腿才能出价。
- 决策：与现价冲突（多单目标 ≥ 现价、空单目标 ≤ 现价）→ `close_at_market`；
  已有止损按 `stop_is_at_least_as_protective` 判定已够保护 → `no_change`；否则 `replace_stop`。
- 全部不抛错：读不出的价格、未知方向、空序列都落到"不动"。

### 1.6 设置

- `stop_ladder_mode: disabled | shadow | live`，**默认 `disabled`**；
  `stop_ladder_activation_after_binding_id: int | None`，默认 `None`（阶段 3 的水位，本阶段无人据此决策）。
- `TradingSettings.effective_stop_ladder_mode`：`live` **解析通过但行为等同 shadow**，
  每次被取用时记一条 warning（本阶段任何模式都零交易所写入）。
- 经 `/api/trading-settings` 可写（该端点不做键白名单）。

### 1.7 影子

`break_even_shadow.run_break_even_shadow_pass` 每轮附带跑 `run_stop_ladder_shadow`：

- `disabled` → 一行不算、一行不写；
- `shadow`（含 `live` 降级）→ 每个在仓仓位算
  `{filled_level, rungs, target_price, target_source, market_price, would_action, existing_stops,
  per_position_levels}`，写一行 `execution_events`：
  `action = stop_ladder_would_replace | stop_ladder_would_close | stop_ladder_no_change`，
  `status = "shadow"`，明细在 `after_json`；
- **去重键 `(pos_id, level, would_action, target_price)`**：与该仓位**最近一条** `stop_ladder_*`
  事件的 `after_json.dedupe_key` 相同就不再写（连续重复去重，变化即记录）；
- 任何异常都只记日志：影子不决定任何事，不能因为它让对账轮失败。

### 1.8 消息列（证据，不改行为）

`strategy_management_planner._break_even_reference_for_batch` 现在返回第三个值
`stop_ladder_evidence`，写进 `target_snapshot["stop_ladder"]`：
`{mode, applied: false, reference_price, reference_source, target{level,price,source,reason_code}, ladder{...}}`。
**`planned_tpsl_json` 与实际参考价一个字节都没变**；`disabled` 时整个键不出现。
组装证据失败只记 warning，绝不让管理批次失败。

### 1.9 放行报告与值守

- `release_gates`：新增 `stop_ladder` 项，`shape: "setting"`，fingerprint 形如
  `stop_ladder=shadow(after:361)`。它是**设置**不是常量，所以由调用方（`web_app` 的启动日志与
  `/api/runtime/release-gates`）读库后传入；没读到时报 `unread`，**绝不报成 `disabled`**。
- `oncall_detector`：`ALLOWED_QUERY_SHAPES` 登记了阶梯那一条按 `pos_id` 的有界查询；
  新增计数器 `counter:stop_ladder_level_unrecorded`：订单级证据的档位 > 影子已记录的最大档位，
  且该证据已超过 5 分钟 → **只计数一次，不建案、不告警**（用户明确不要告警）。
  整段读取包在自己的 try/except 里，阶梯的读失败**不会**把这一轮判成 `read_failed`。

---

## 2. 没有做的事（本阶段范围外）

- 任何交易所写入。影子测试逐项断言**零 `PositionMutationIntent`**。
- 不改 `break_even_convergence_worker` / `break_even_convergence_executor`、
  不删 `BREAK_EVEN_*_RELEASED_POS_IDS`、不动自动交易 / 执行模式开关、
  不写 `strategy_lifecycles.filled_tp_index`、无 schema 变更。
- 自动列（阶段 3）与消息列 live（阶段 2）都没有接通。

---

## 3. 规格未覆盖、由实施决定的事项

1. **新增了 `stop_ladder_records.py`**。规格的文件表只列了 `stop_ladder.py`，同时要求它
   "全部不抛错、不 I/O"。为了让纯函数真的纯，所有读库的部分（`derive_filled_tp_level`、
   `reconcile_take_profit_fill_levels`）放在第二个模块里；`execution_bindings` 仍按规格在对账里调用它。
2. **账本 `filled` 的共享写入点放在 `protection_health`**（`record_take_profit_ledger_fill`），
   而不是新建一个 writer 模块：F2 已经在那里写了，把第二个调用方接到同一个函数上，
   才是"统一到一处"而不是"再长一个"。
3. **B 型证据用布尔量传进共享判据**（`position_decrease_proven`），排除条件由调用方建立。
   默认 `None` 让所有既有调用方逐字不变。
4. **"相邻两条完整观测"取的是最近两条 `snapshot_complete=True` 的观测**，中间若夹着不完整的
   观测不影响判定——减少确实发生在两次完整读之间。
5. **观测归桶用账本全部状态**（不只 `verified`）：问的是"这张单是谁的"，不是"它还活着吗"，
   而 `(venue, order_id)` 唯一，所以一个单号不可能指向两个仓位。
6. **`release_gates` 的第三种取值 `unread`**：设置读不到时不能报 `disabled`。
7. **值守只对"证据带 `level`"的行比较**：`protection_health` 写的那类证据没有 `level`，
   无从比较，直接退休不计数。
8. **影子的第 1 档参考价**取 `resolve_break_even_reference`，开仓腿集合取该 binding 下
   `status ∈ {active, partially_filled}` 且 `attribution_status = verified` 且带 `pos_id` 的入场腿，
   与规划器的口径一致。

---

## 4. 风险

1. **两处读取缺陷修好之后，旧的 `tp1_fill` 入口可能第一次成立。**
   `break_even_convergence_worker._plan_proven_tp1_fills` 要求
   `PositionTakeProfitOrder.status='filled'` + `PositionProtectionLeg(leg_index=1, status='filled')`
   + `evidence.tp1_fill`。`_prove_exact_terminal` 不再拦死、`pending_tpsl_json` 不再恒空之后，
   这条链在理论上可以走通，于是可能**第一次**出现 `tp1_fill` 触发的收敛行（纯数据库行）。
   - 生产上仍有一道天然拦截：预置止盈腿的 `planned_size` 存的是百分比（Q3 已核），
     `prove_first_take_profit_fill` 会先给出 `take_profit_ownership_conflict`。
   - 即便走通，执行器的两个旧放行常量（replacement 只含两个**已平**的 ETH 仓位、full-exit 为空）
     会把每一行扣成 `blocked` 并只写 `break_even_would_*` 证据，**不会有任何交易所写入**。
   - 首次部署后值得看一眼 `strategy_break_even_convergences` 是否出现 `tp1_fill` 行。
2. **B 型证据的误判面**：仓位在两次完整观测之间因**别的原因**变小（人工平仓、爆仓、复合减仓），
   而恰好某张止盈单也从挂单里消失且历史查不到它——此时会被判成"已到"。
   排除条件（不是我们撤的、快照完整、历史沉默）把这一面收得很窄，但它确实存在，
   这是用户在拍板 3 里明确接受的退一步判据。代价方向是"止损被收紧一档"，不是裸仓。
3. **`filled` 是终态**：一旦写下，该账本行对所有自动路径不可见（撤单闸门也会拒绝撤它）。
   误判的后果是"这张止盈单从此不再被我们管理"，需要人工介入才能恢复。
4. **一次减少可以同时结清多档。** B 型是**逐单**判定的（规格的合取式也是逐单的）：
   一轮里若有两张止盈单同时从完整快照里消失、历史都查不到、仓位有一次减少，
   两档都会被判为已到，档位因此是 2。价格确实一路穿过两档时这是对的；
   若实际只成交了一档，"本应止损"就比真相紧一档。
   本阶段它只是影子里的一行，已列入首笔样本核对清单
   （`tests/test_stop_ladder_records.py::test_one_decrease_settles_every_rung_that_vanished_with_it`
   把这一行为钉住了）。
5. **影子事件量**：每个在仓仓位每轮最多一行，且连续重复去重；但价格来回穿越目标价时
   `replace_stop ↔ close_at_market` 会交替写行。观察窗里按 `pos_id` 看，不要按总数看。

---

## 5. 部署与观察（指挥会话）

1. `tg-deploy`，零在途；回滚 = `tg-deploy <上一个生产 sha>`（`a79a387b`）。
   设置回 `disabled` 即可立刻停止一切新写入，无需部署。
2. 部署后设 `stop_ladder_mode = shadow`（交易设置，经 `/api/trading-settings`）。
3. **首个真实止盈成交样本必须逐项核对**：
   - 账本该行 `status='filled'`，`evidence_json.take_profit_fill` 的 `level` 与
     `evidence_form`（预期 `trigger_history`）与交易所一致；
   - `position_reconciliation_observations.pending_tpsl_json` **不再是 `[]`**（归桶修好了）；
   - 该 pos_id 不再产生 `protection_missing` 事故；
   - `execution_events` 里 `stop_ladder_*` 那一行的 `filled_level` / `target_price` /
     `would_action` 与手工按规则算的一致（尤其是 N=1 取策略入场参考价、N≥2 取第 N−1 张的价）；
   - 若这段时间有保本类消息：批次 `target_snapshot.stop_ladder` 有"本应目标"，
     而 `planned_tpsl` 仍是入场参考价（`applied: false`）；
   - `strategy_break_even_convergences` 是否出现 `tp1_fill` 行（见风险 1）；
   - 若同一轮里有两张止盈单一起消失：核对交易所到底成交了几档（见风险 4）；
   - 值守计数器 `counter:stop_ladder_level_unrecorded` 是否长期上涨（涨 = 影子没跟上，不是故障）。

## 部署记录（2026-09-23）

- 用户批准部署。候选 `9ce48d37`（`680791fc` 读取缺陷修复 → `70c4d277` 阶梯/设置/影子 → `9ce48d37` 文档），最终候选全量 **9650 passed / 4 skipped / 0 failed**。
- 零在途、无在仓仓位；`tg-deploy 9ce48d37…` → 五个服务 active、web 200、worker 错误行 0；自动交易开关未动。**回滚 = `tg-deploy a79a387bf20d85e032a728b95fe42f28b1ae2934`**。
- `/api/runtime/release-gates` 仍报告旧的两个按仓位常量（阶段 3 才替换）。
- 部署后把 `stop_ladder_mode` 设为 `shadow`（见下一条记录）。
