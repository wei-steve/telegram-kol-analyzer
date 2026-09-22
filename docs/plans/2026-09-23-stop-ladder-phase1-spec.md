# 止损阶梯 · 阶段 1 实施规格：止盈成交档位判定 + 影子

日期：2026-09-23
上位：`docs/plans/2026-09-21-stop-ladder-policy.md`（规则 + 第 4 节用户拍板）、`docs/plans/2026-09-21-stop-ladder-design.md`（设计，含点查结果）
验证等级：**L1**（新增、默认关闭、影子模式零交易所写入、不改自动交易开关）

## 1. 本阶段做什么 / 不做什么

**做**：
1. 一个确定性的"该仓位 / 该策略已实际成交到第几档止盈"判定（纯函数 + 只读查询），落成订单级证据，不改表结构；
2. 修掉设计 2.1 指出的两个读取缺陷：`take_profit_fill_evidence._prove_exact_terminal` 对真实 trigger 历史行（无 `posId`）直接判失败；
   `execution_bindings` 观测归桶按 TPSL 行的 `posId` 分桶而该行从不带 `posId`（`pending_tpsl_json` 恒为 `[]`）；
3. 新设置 `stop_ladder_mode = disabled | shadow | live`（默认 `disabled`）与 `stop_ladder_activation_after_binding_id`；本阶段只允许 `disabled / shadow`：
   `live` 在本阶段被解析器接受但**行为等同 shadow**，并记一条 warning（阶段 3 才接通）；
4. 影子输出：每轮对每个在仓仓位算出 `{filled_level, target_price, target_source, would_action}` 写 `execution_events`（`action=stop_ladder_would_replace / stop_ladder_would_close / stop_ladder_no_change`），
   并让 `break_even_shadow` 输出同一信息；
5. 消息列的阶梯目标（设计第 3 节）**在 shadow 下只写证据**：`_break_even_reference_for_batch` 把"本应目标"写进 `target_snapshot["stop_ladder"]`，实际仍用入场参考价；
6. 值守探测器登记新查询形状，并对"订单级证据档位 > 影子已记录的最大档位持续 5 分钟"这一种情况**只计数不告警**（用户明确不要告警）。

**不做**：任何交易所写入；改执行器的 live 闸门或删除 `BREAK_EVEN_*_RELEASED_POS_IDS`；自动列 live；市价全平；改 `strategy_lifecycles.filled_tp_index`。

## 2. 规则（以用户拍板为准，覆盖设计中相冲突的部分）

### 2.1 档位（rung）的定义——取代设计 1.3 的"按策略文本价格映射"

某仓位的**档位序列 = 该仓位保护账本里 `purpose='take_profit'` 的行，剔除 `retired / cancelled / superseded` 之后，按盈利方向排序**
（多单按触发价升序、空单按降序）。第 1 张 = 第一档，第 2 张 = 第二档……**不再对照 `lifecycle.take_profit` 或 binding draft 的价格**；
KOL 改止盈价时系统撤旧挂新，序列自然跟随。缩档的仓位序列就短一截，按实际挂着的算。

### 2.2 "某一档已到"的判定——取代设计 1.2 的四条同时成立

一档止盈视为**已到**，当且仅当该行的单号**不在一次完整的挂单快照里**，且该行状态不是 `cancelled / retired / superseded`，且该单号没有我们的 cancel 类 `PositionMutationIntent`，且下列任一成立：

- **A（首选）**：trigger-orders-history 里有该单号，`triggerTime ≠ 0` 且 `errorCode ∈ {"", "0", "00000"}`（复用 F2 的 `take_profit_fill_predicate`）；
- **B（退一步）**：历史里查不到该单号，但相邻两条 `snapshot_complete` 观测显示该仓位数量**有减少**（任意数量，不要求等于该档 size）。

**不看成交数量**；证据里记 `evidence_form: "trigger_history" | "position_decrease"`。判定不出（快照不完整、观测缺失、单仍在挂单）→ 档位不变，**不告警**，只记 `stop_ladder_unproven` 计数。

### 2.3 策略档位与目标价

- 策略档位 N = 各在仓腿已到档位的**最大值**（用户拍板 1）。
- 目标价：N = 0 → 不动（策略止损价）；N = 1 → `resolve_break_even_reference`（已上线的策略入场价）；N ≥ 2 → **该仓位序列里第 N−1 张止盈单的触发价**
  （多腿时取"已到 N 档的那条腿"的序列；两腿都到 N 档则取更保护的那个：多单取高、空单取低）。
- 与现价冲突（多单目标 ≥ 现价、空单目标 ≤ 现价）→ `would_action = "close_at_market"`；已有止损已不低于目标的保护力度 → `no_change`；否则 `replace_stop`。
- 只收紧不放宽：用 `stop_is_at_least_as_protective`。

### 2.4 持久化（不改表结构）

- 订单级：账本行 `evidence_json["take_profit_fill"] = {level, evidence_form, trigger_time?, observation_ids?, decided_at}`，并把状态置 `filled`
  （F2 已让 `protection_health` 对证成成交的行写 `filled`；本阶段统一到同一个共享判据，避免两套写法）。
- 策略级：不缓存，`derive_filled_tp_level(session, binding_id)` 每次从证据推导；重启后结论逐字一致。
- 影子：`execution_events` 一行 / 仓位 / 轮，`status="shadow"`，detail 含 `filled_level / rungs / target_price / target_source / market_price / would_action / existing_stops`。同一仓位同一档位同一 `would_action` 连续重复时**不再写**（去重键 `(pos_id, level, would_action, target_price)`），避免每轮刷屏。

## 3. 改动文件

| 文件 | 内容 |
|---|---|
| 新增 `stop_ladder.py` | 纯函数：`rungs_for_position(ledger_rows, side)`、`filled_level_for_position(...)`、`strategy_filled_level(...)`、`ladder_target(...)`、`ladder_decision(...)`；全部不抛错、不 I/O |
| `take_profit_fill_predicate.py` | 补 B 型证据（观测减少）；保持 F2 的 A 型 |
| `take_profit_fill_evidence.py` | `_prove_exact_terminal`：真实 trigger 历史行缺 `posId` 时不再直接 failure，落到下一层 |
| `execution_bindings.py` | 观测归桶：TPSL 行无 `posId` 时用账本 `order_id → pos_id`；对账里新增 `reconcile_take_profit_fill_levels`（紧接现有止盈对账），只读交易所、写订单级证据 |
| `trading_settings.py` | `stop_ladder_mode`（默认 `disabled`）、`stop_ladder_activation_after_binding_id`（默认 None）；解析沿用现有模式字面量风格 |
| `break_even_shadow.py` | 每轮输出各仓位阶梯决策；写去重后的 `execution_events` |
| `strategy_management_planner.py` `_break_even_reference_for_batch` | shadow 下把阶梯"本应目标"写进 `target_snapshot["stop_ladder"]`，实际参考价不变 |
| `release_gates.py` | 报告新设置的形状（`stop_ladder=shadow(after:none)`），旧常量不动 |
| `oncall_detector.py` | `ALLOWED_QUERY_SHAPES` 登记；一个计数器 `counter:stop_ladder_level_unrecorded`，**不建案不告警** |
| `docs/ARCHITECTURE.md` 4.8 | 一段"阶梯（影子）" |
| `docs/stop-ladder-status.md` | 新建状态文档 |

`break_even_convergence_worker / executor` 本阶段**不改**（仍按旧的 `tp1_fill` 入口，且入口在生产上永远不成立，等于关闭）。

## 4. 测试

1. `stop_ladder` 纯函数：多 / 空各一套；序列排序；`retired / cancelled / superseded` 剔除；1–5 档；N=1 → 入场参考价、N≥2 → 第 N−1 张价；两腿取最大、目标取更保护；冲突 → close；已有更紧止损 → no_change；空序列、非法输入不抛错。
2. 成交判定：A 型（真实 23 键 / 无 `state` 的历史行，复用 `tests/deepcoin_production_rows.py`）、B 型（观测减少任意数量）、被我们撤（有 cancel intent）→ 否、仍在挂单 → 否、快照不完整 → 不变且计数。
3. `_prove_exact_terminal`：真实历史行（无 `posId`）不再返回 failure。
4. 观测归桶：TPSL 行无 `posId` → 通过账本归到正确仓位，`pending_tpsl_json` 非空。
5. 设置：三态解析；`live` 在本阶段等同 shadow 且有 warning；默认 `disabled` 时零行为、零事件。
6. 影子事件：去重；`disabled` 零事件；`shadow` 下不产生任何 `PositionMutationIntent`（断言 intent 表行数不变）。
7. 消息列：shadow 下 `target_snapshot["stop_ladder"]` 有本应目标，实际 `planned_tpsl` 参考价不变。
8. 生产形状：空单区间 80500–81600，止盈 79800/79100/78400，仅腿 1 在仓——TP1 触发 → 档位 1、目标 80500、`replace_stop`；TP2 触发 → 档位 2、目标 79800；现价 79850 → `close_at_market`；已有止损 79500 → `no_change`；多单镜像。
9. 既有测试全绿：`test_break_even_*`、`test_take_profit_fill_*`、`test_position_take_profit_orders`、`test_release_gates`、F2 的复合形状测试、值守全部。最终候选跑一次全量 `uv run python -m pytest -q`。

## 5. 上线（指挥会话）

`tg-deploy`，零在途；部署后 `stop_ladder_mode=shadow`（交易设置，经 `/api/trading-settings`）。观察到首个真实止盈成交样本：核对档位、目标价、`would_action` 与交易所一致。
回滚：设置回 `disabled` 即停；代码回滚 `tg-deploy <上一个生产 sha>`，新增的账本 `filled` 状态与证据对旧代码无害（F2 已核）。

## 6. 提交与汇报

worktree 分支；禁止 `git add -A`；不 push、不部署、不连服务器、不真实调交易所。汇报：SHA、文件、每条规则对应的测试、全量结果、偏离及理由、你认为的风险。
