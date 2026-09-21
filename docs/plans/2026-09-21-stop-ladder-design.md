# 止损阶梯（Stop Ladder）设计文档

日期：2026-09-21。只读设计：未改文件、未连服务器。文中"假设"均为读代码得出的推断，需第 0 节的点查确认。所有路径以 `/Users/steven/Documents/telegram获取消息/` 为根。

## 0. 先跑的生产点查

这些查询决定第 1、2 节里两个关键假设是否成立。

| # | 查询 | 要回答的问题 |
|---|---|---|
| Q1 | `SELECT id,trigger_type,trigger_identity,execution_mode,status,reason_code,planned_at FROM strategy_break_even_convergences;` | 08-03 那两行是 `tp1_fill` 还是 `confirmed_partial_close` 触发 |
| Q2 | `SELECT status,COUNT(*) FROM position_take_profit_orders GROUP BY 1;`，另取 `status IN ('filled','completed')` 最近 20 行的 `order_id,pos_id,trigger_price,size_text,substr(evidence_json,1,600)` | 现有成交证据是 `tp1_fill` 还是 `partial_take_profit_fill`，各有多少 |
| Q3 | `SELECT leg_index,status,COUNT(*) FROM position_protection_legs WHERE role='take_profit' GROUP BY 1,2;`；`SELECT planned_size,COUNT(*) FROM position_protection_legs WHERE role='take_profit' GROUP BY 1 ORDER BY 2 DESC LIMIT 10;` | `leg_index` 是否出现大于 3 的值（替换后递增）；`planned_size` 是否存的是百分比而不是张数 |
| Q4 | `SELECT COUNT(*),SUM(pending_tpsl_json<>'[]') FROM position_reconciliation_observations WHERE observed_at>'2026-09-01';` | 验证"观测里的挂单恒为空"（见 2.1） |
| Q5 | `SELECT evidence_source,status,COUNT(*) FROM position_protection_ledger WHERE purpose='take_profit' GROUP BY 1,2;` | 只存在于账本的止盈单占比；`protection_missing` 数量 |
| Q6 | 取 Q2 中一张已成交止盈单号和一张已撤止盈单号，各查 `SELECT id,channel,action,received_at,raw_payload FROM deepcoin_ws_events WHERE order_sys_id=:id ORDER BY id;` | WS `TriggerOrder` 帧的 `TS` 在"触发成交"与"被撤"时分别是什么值、是否有终态帧 |
| Q7 | 设置行里 `move_stop_to_breakeven_after_tp1`、`take_profit_allocations`、`entry_range_order_style` 的值 | 自动列今天的总开关是否为真 |
| Q8 | 取一条 `partial_take_profit_fill.close_order.ordId`，读它在 REST orders-history 的原始行 | 平仓单行是否带 `posId` 或来源止盈单号 |

## 1. 交付 1："实际成交的最高止盈档位"

### 1.1 现有记录各自能证明什么

- **`position_take_profit_orders`**
  - 写入者只有止盈收敛执行器（`trigger_take_profit_convergence_executor.py:555`）。
  - 成交态有三种：
    - `filled` 加 `evidence.tp1_fill`，只对 `leg_index==1` 生效（`position_take_profit_orders.py:236-281`）。
    - `filled`，来自 trigger-history 终态（`:282-290`）。
    - `completed` 加 `evidence.partial_take_profit_fill`，这是 A-5c 的解释路径，任意档位都适用（`:717-766`）。
  - 缺陷：管理路径替换出来的止盈单不进这张表。
- **`position_protection_ledger`（`purpose='take_profit'`）**
  - 写入者有三类：收敛（`trigger_take_profit_pending_readback`，`executor:586-603`）、管理替换（`strategy_management_executor.py:3976-4013`）、TU 认领。
  - 它是止盈单的**全集**，但没有"成交"状态。
  - 09-08 之后成交的止盈单会落成 `protection_missing` 并产生一条误报事故（`protection_health.py:608-614`）。原因是 trigger-orders-history 不再返回之后创建的 TPSL 单（`partial_take_profit_explanation.py:18-27`）。
- **`position_protection_legs`**
  - 提交时的 `leg_index` 等于策略档位（`recovery_live_submit.py:2203-2220`）。
  - 管理替换后 `leg_index` 取 `max+1`（`protection_replacement_persistence.py:134-169`），**不再等于策略档位**。
  - 缺陷：`planned_size` 在预置腿里存的是 `allocation_pct`（`recovery_live_submit.py:2219`），与 `take_profit_fill_evidence.py:48-53` 要求的"等于张数"必然不相等（待 Q3 确认）。
- **`position_reconciliation_observations`**
  - 每轮记录仓位的 size 和 avgPx，size 序列可信。
  - 缺陷：`pending_tpsl_json` 按挂单行的 `posId` 分桶（`execution_bindings.py:1327-1331`），而 TPSL 行根本不带 `posId`（`deepcoin_trigger_rows.py:37-39`），所以恒为 `[]`。
- **`deepcoin_ws_events`**
  - 原始帧可按 `order_sys_id` 和 `trade_unit_id` 查。
  - `TS` 取值的含义未知；`Trade` 帧和 `Order` 帧不带仓位字段。
- **`strategy_lifecycles.filled_tp_index`** 是模拟盘的"价格碰到"（`lifecycle_monitor.py:1399-1412`），不得复用。
- **`lifecycle_monitor._parse_take_profits` 不得复用**：它对空单也按升序排（`:65-75`），档位顺序是反的。

### 1.2 判定规则

新增纯函数模块 `take_profit_fill_levels.py`，全部确定性，证明不了就不升档。

**候选**：某仓位的账本止盈行（`status in verified/protected/protection_missing`），其单号在一次**完整**的挂单快照里已不存在。

**四条同时成立才算"成交"**：

1. **不是我们撤的。**
   - 账本状态不是 `cancelled` 或 `retired`。
   - 该单号没有 cancel 类的 `PositionMutationIntent`。
   - KOL 减仓经 `consume_take_profit_stage` 撤掉 TP1 的情形落在这一条（`strategy_management_take_profit_consumption.py:144-146`）。结论是"被消费，不是成交"。
2. **仓位数量确实减少了该档的量。**
   - 观测序列里存在相邻两条 `snapshot_complete` 观测，`prev.size − cur.size` 等于该档 size，或等于同时消失的若干档 size 之和。
   - 或者仓位已不在（整仓被末档平掉），且 `prev.size` 等于消失各档之和。
3. **交易所说它执行了。** 沿用 A-5c 的两种形式（`partial_take_profit_explanation.py:381-499`）：
   - 形式 i：trigger-history 里有该单号，`triggerTime` 非 0，且无错误码。
   - 形式 ii：orders-history 里有一张已成交的反向平仓单，满足全部条件：
     - 数量恰等于该档 size；
     - 均价在 `max(2 tick, 2bp)` 容差内；
     - 时间在"该档挂出"与"观测到减少"之间；
     - 没被别的档位用过。
4. **窗口内该仓位没有我们自己的平仓意图。**
   - 沿用 `_conflicting_position_mutations`（`position_take_profit_orders.py:586-602`）。
   - 如果有，必须能精确扣除，否则判为歧义。

**其他结论一律不算成交**：被撤、被替换（账本 `retired` 且带 `replacement_identity`）、被 KOL 减仓消费、无法证明。

**证明不了时的处置**：
- 不升档，止损保持现状。仓位仍有止损，这是安全方向。
- 记一条 `PositionProtectionIncident`，类型 `stop_ladder_fill_unproven`，证据里带全部被查过的字段。

### 1.3 已成交的单如何映射回策略档位

**按价格匹配，不按 `leg_index`、不按创建顺序。** 理由见 1.1：替换后 `leg_index` 已失真。

- **策略止盈价表**取自 `json.loads(execution_bindings.payload_json)["draft"]["take_profit_legs"]` 的 `{index, price}`。
  - 它已按 tick 归一（`deepcoin_order_builder.py:859-876`）。
  - 它就是实际挂出去的价（`executor:869-881`）。
- **匹配方式**：`Decimal(order.trigger_price) == Decimal(leg.price)`，精确相等。两边本来就同源，不需要容差。
- **小仓位缩档**保留的是前缀（`executor:1767-1808`），所以按价格匹配仍然正确。
- **draft 里匹配不到时**（KOL 改过止盈、管理路径按新价重挂）：
  - 再用 `lifecycle.take_profit` 解析后的列表匹配。解析要按方向排序：多单升序、空单降序。
  - 仍匹配不到则档位记为"未知"，证据写明 `stop_ladder_level_unmapped`。
  - 档位未知时，自动列不动；消息列退回第 0 档目标，也就是已上线的行为。

### 1.4 两个仓位（两条入场腿）如何合并

**策略档位 = 各仓位已证明档位的最大值。**

- 两仓的同档止盈触发价相同，都用 `last` 触发，正常情况下几乎同时成交，差别只在被观测到的先后。
- 现有收敛本来就是"一个触发、冻结策略下全部在仓腿"（`break_even_convergence_planner.py:123-174`）。
- 用户规则的措辞是策略级的："达到第一止盈价并且实际止盈"。
- 腿 1 的 TP1 成交、腿 2 的止盈压根没挂上时，腿 2 的止损同样移到入场参考价。
- 证据里逐仓位列出各自的档位。
- 取最小值会让腿 2 在价格已到过 TP1 后仍挂着策略止损，与规则相反。此项见第 5 节待确认。

### 1.5 人工或 KOL 减仓之后

- 被消费或被撤的止盈单不计入成交（1.2 第 1 条）。
- 管理替换后重挂的止盈单只在账本里，因此判定必须以**账本**为全集。现有代码的盲区就在这里。
- 现有解释器用"计划总量 − 在仓量"触发判断（`position_take_profit_orders.py:397-415`）。减仓后可能 `live ≥ outstanding`，从而永远进不了判断。
- 新规则改用"单号消失 + 观测 size 序列"触发，不受这个问题影响。

### 1.6 重启安全与持久化

**不改表结构。**

- **事实落在订单级证据上**：
  - `position_take_profit_orders.evidence_json` 沿用现有键。
  - 只在账本里的单，写 `position_protection_ledger.evidence_json["take_profit_fill"]`，包含 `level`、`matched_price`、`close_order`、`observation_ids`、`evidence_form`。
  - 同时把账本状态置为 `filled`。该列是 `String(32)`，无枚举约束；`_ledger_owner_matches` 已认识这个值（`consumption:239-240`）。
  - 副作用：消掉今天的 `protection_missing` 误报。
  - 实施时须审计所有 `status.in_(...)` 读点。
- **档位按需从证据推导**：`derive_filled_tp_level(session, binding_id)`。不缓存，不写 `filled_tp_index`。重启后逐字得出同一结论。
- **自动列的"已处理过"**由收敛行的唯一索引承担（2.3 第 5 项）。

### 1.7 从当前数据无法知道的事

1. 交易所是否把 TPSL 单号与它触发出的平仓单关联起来（Q8）。若无关联，形式 ii 永远是"尺寸 + 价格 + 时间"的推断，不是身份证明。
2. WS `TriggerOrder.TS` 的终态取值，以及触发或撤单时是否推终态帧（Q6）。
3. trigger-orders-history 对 09-08 之后的 TPSL 是否永久缺失。
4. 历史 60 个策略里哪一档真的成交过：只有带 `tp1_fill` 或 `partial_take_profit_fill` 证据的可以还原；账本上的 `protection_missing` 分不清是成交、外部撤单还是仓位已平。
5. 止盈单部分成交（此时减少量不等于档位量）：无法解释，判为未证明。
6. 两个仓位同档、同量、同价时，同一张平仓单可能被两仓各用一次：
   - `used_close_order_ids` 只在单个收敛内去重（`:438`）。
   - 对档位结论没有影响（两档都确实消失了），但证据不唯一。
   - 已用平仓单的去重建议提到 binding 级。

## 2. 交付 2：自动列

### 2.1 为什么 08-03 之后一行都没有

**收敛行的两个入口**

- 入口一：`_plan_proven_tp1_fills`（`break_even_convergence_worker.py:214-290`）。
  - 要求 `PositionTakeProfitOrder.status='filled'`、`PositionProtectionLeg(role=take_profit, leg_index=1, status='filled')`、`evidence.tp1_fill.evidence_tier` 三者同时成立。
- 入口二：`_plan_confirmed_partial_close_break_even`（`strategy_management_reconciliation.py:411-419, 490-564`）。
  - KOL 减仓被交易所确认后触发。
- 总开关：
  - `move_stop_to_breakeven_after_tp1`（`trading_settings.py:145`，默认真）。
  - 模式取自 `management_execution_mode`（`worker:220-225`）。生产是 live，所以开关本身不是原因（Q7 确认）。

**入口一为什么永远不成立（假设，Q2 到 Q4 验证）**

唯一能写 `tp1_fill` 的是 `prove_first_take_profit_fill`，它有两个证明层级，两层都证明不了。

- `exact_order_terminal`（`take_profit_fill_evidence.py:132-189`）：
  - 要求历史里有同单号的行，且带 `posId`、`posSide`、size。
  - 09-08 之后 TPSL 单不进 trigger-history；平仓单是另一个单号。
  - 即使命中但缺 `posId`，直接返回失败 `tp1_exact_history_incomplete`，不会落到第二层。
- `exchange_position_delta`（`:76-129`）：
  - 要求上一条观测的 `pending_tpsl` 里有这张单。
  - 观测的 `pending_tpsl_json` 恒为 `[]`（1.1 列的 `posId` 词汇错），所以必然得到 `tp1_previous_order_not_verified`。
  - 这与 6h 修过的执行器读取缺陷是同一类错误，出现在第三个位置。
- 预置腿的 `planned_size` 存的是百分比，会先一步得到 `take_profit_ownership_conflict`（`:48-53`）。
- 真正在工作的成交证据是 A-5c 的 `completed` + `partial_take_profit_fill`，而入口一不读它。
- 另外，`BreakEvenConvergencePlanningError` 被静默吞掉（`worker:287-288`）。规划失败不留任何痕迹。

**`break_even_market_preflight_unavailable` 的含义**

- 它是执行器 `:299-321` 的一个笼统 `except Exception`，盖住了至少八种原因：
  - 报价不新鲜；
  - 仓位不唯一；
  - `pos` 或 `avgPx` 漂移（`:763-770`）；
  - `break_even_existing_stop_drift`（`:786-803`）；
  - `remaining_take_profit_drift`（`:1039-1079`）；
  - REST 异常；等等。
- 08-03 那两行几乎可以确定是 `existing_stop_drift`：6h 之前执行器用仓位行的词汇去读 TPSL 行，每次必抛（ARCHITECTURE §4.8）。
- 结果是终态 `blocked`，永不重试。

### 2.2 泛化还是替换

**结论：泛化。** 保留表、worker、claim/lease、告警、影子分支、`replace_stop_group`、`close_exact_position`；替换触发源、目标价、放行闸和 preflight 的失败语义。

不能改成"系统发起的管理批次"：`strategy_management_batches.raw_message_id` 和 `recognition_decision_id` 都是 NOT NULL（`models.py:2165-2170`）。无消息的动作要么改表结构，要么伪造消息，两者都不可取。

### 2.3 具体设计

**1. 触发**

- 在对账里紧接现有止盈对账（`execution_bindings.py:1181`）加 `reconcile_take_profit_fill_levels`，按 1.2 写订单级证据。
- 同一改动里修 `pending_by_position`：TPSL 行没有 `posId` 时，用账本的 `order_id→pos_id` 归桶。
- worker 每 tick 执行 `_plan_proven_tp_level_fills`，取代 `_plan_proven_tp1_fills`：
  - 对每个有在仓腿的 binding 求 `derive_filled_tp_level`；
  - 若 N ≥ 1 且不存在该档的收敛行，调用 `plan_or_adopt_break_even_convergence(trigger_type="tp_level_fill", trigger_identity=f"level:{N}", …)`。
- `break_even_convergence_planner.py:68` 的合法触发集加这一种。
- `trigger_evidence` 带各仓位的证据、`confirmed_at` 和阶梯。
- 规划异常不再静默：连续失败超过 5 分钟，记 `stop_ladder_planning_blocked:<reason>` 事故。
- 延迟：WS 唤醒 → 对账 → worker 2 秒 tick。

**2. 目标价**

- N=1：`resolve_break_even_reference(...)`，"有仓位的腿"取自收敛的 `live_legs`。
- N≥2：策略的 TP(N−1) 价。
- 写入位置：`target_snapshot_json["stop_ladder"]` 和每条腿的 `decision_json.target_price / target_source / filled_tp_level`。
- 身份用途不动：`StrategyBreakEvenConvergenceLeg.avg_entry_price` 继续只做与 `avgPx` 的身份比对（`executor:768`）。
- 写交易所前按 tick 归一：多单向下取整、空单向上取整，与 `plan_composite_stop_replacement`（`market_policy:94-95`）一致。
- 现状直接发 `item["entry"]`（`executor:395`）。换成策略价后可能带浮点尾巴或半 tick 的中点，所以必须归一。

**3. 目标与市场冲突 → 市价平仓**

- 用 `assess_break_even_market(entry_price=target)` 判定，`allowed=False` 即 `full_exit`。
- 沿用执行器现有分支 `close_exact_position`（`position_mutation_gateway.py:968`，调用点 `executor:517-532`）。
  - 这与管理侧的 `break_even_by_market` 是同一条平仓路径（`strategy_management_executor.py:852`）。
  - 幂等键 `break-even:{cid}:{lid}:full-exit`，平仓后回读确认仓位为 0。
- 没有中间回落。

**4. 只收紧不放宽**

- 用 `stop_is_at_least_as_protective(existing, target, side, market_price)`（`market_policy:40-72`）逐张判定主止损，取代 `assess_break_even_with_existing_stop` 里的 `qualifying`（`:194-198`）。
- 旧判定只比价格，没有"仍在现价有效一侧"这一条。
- 全部已足够保护 → `keep_tighter_stop`，零写入。

**5. 幂等**

- 唯一索引 `(venue, strategy_instance_id, trigger_type, trigger_identity)`（`models.py:2802-2809`）保证每个策略、每个档位只有一行。
- `(convergence_id, pos_id)` 唯一（`:2863-2868`），保证每个（仓位，档位）只调整一次。
- 交易所写入的幂等键前缀不变，未知结果不重发。
- 高档位到来时：
  - 低档位仍是 `planned` → 标 `completed/superseded_by_higher_level`。
  - 低档位是 `recovery_required` → 高档位保持 `planned` 并告警，禁止在未知写入之上叠加新写入。

**6. preflight 失败语义**

- `reason_code` 写真实原因，不再用笼统的 unavailable。
- 瞬时类（报价、REST、`remaining_take_profit_drift`）回到 `planned` 重试，上限 10 分钟，到期转 `blocked` 并告警。
- 身份类（`pos`、`avgPx`、止损漂移）立即 `blocked` 并告警。

**7. 与 KOL 管理批次并发**

- 两个 tick 共用单线程执行器（`runtime_worker_executor.py:1-13`），没有真正的并行，只有跨 tick 的交错。
- 认领前对每个目标仓位调 `protection_write_block_reason`（`position_protection_legs.py:25-69`）。它覆盖 `management_in_progress`、`position_mutation_in_progress`、`close_in_progress`。
  - 非空 → 不认领、不改状态，下个 tick 再看。
  - 受第 6 项的同一期限约束。
- 批次结束后重新 preflight：
  - KOL 已经把止损移到同一目标或更紧 → `keep_tighter_stop`。
  - KOL 已全平 → 无在仓腿，收敛 `completed/no_live_position`。
- `execute_break_even_convergence` 加 `@serialized_position_authority_mutation`。今天只有它没加这个装饰器。
- 管理批次的唯一活动批次索引（`uq_strategy_management_batches_active_strategy`）不受影响，因为是两张表。
- 同一 tick 里，止损缩量与阶梯会对同一张止损各做一次"挂新撤旧"（`worker:75-82`）。
  - 对有 `planned` 阶梯行的仓位跳过 `converge_stop_loss_sizes`。
  - 阶梯的新止损本来就用在仓量（`executor:398`）。

**8. 放行机制**

取代 `BREAK_EVEN_*_RELEASED_POS_IDS`（`executor:109-134`），沿用 6k "按谓词放行 + 黑名单"的先例（`source_release.py`）。

- 新设置（`trading_settings.py`，沿用 `:68-94` 的模式字面量）：
  - `stop_ladder_mode: disabled|shadow|live = "disabled"`。
  - `stop_ladder_activation_after_binding_id: int | None`。
- `effective_stop_ladder_mode`：
  - `shadow` → 影子。
  - `live` 且 `auto_trade_enabled` 且 `management_execution_mode=="live"` → live。
  - `move_stop_to_breakeven_after_tp1` 为假 → 自动列 disabled。
  - 本功能只读 `auto_trade_enabled`，不写它。
- 放行谓词：
  - 群是 `auto_trade`；
  - 入场腿 `verified`；
  - `binding.id` 大于水位；
  - 不在新常量 `STOP_LADDER_BLOCKED_POS_IDS`（默认空，给无法部署的时刻用）里。
- 换挡与市价全平共用同一个模式。用户的规则已把两者作为一条策略批准，不再分两道闸。
- `_runtime_mode_enabled`（`executor:695-707`）改读新设置，否则生产（管理模式 live）下影子行会被判成 `runtime_disabled`。
- `release_gates.py` 改为报告 `shape: predicate`，包含模式、水位、黑名单。fingerprint 形如 `stop_ladder=live(after:361;blocked:none)`。
- 两个旧常量在**第 3 阶段候选**里与 live 闸门替换一起删除，两个已平 ETH id 作废。第 1 阶段保留旧闸不动，见 4.1。

**9. 值守进程应看到的**

- `oncall_detector.py` 新增 `WATCH_BREAK_EVEN_CONVERGENCE`。现有类型见 `:86-88`；`ALLOWED_QUERY_SHAPES`（`:101`）需要登记这张表。
- 立案条件：
  - `status in (blocked, recovery_required, failed_terminal)`，且原因不是 `disabled`、`shadow`、`superseded`。
  - `planned` 超过 5 分钟。
  - "订单级成交证据的档位 > 已有收敛行的最大档位"超过 5 分钟（触发丢失）。
  - `stop_ladder_fill_unproven`。
- 市价全平成功也发一条高优先级通知：这是系统主动离场，应让人知道，不是故障。
- 影子期由执行器写 `execution_events`，`action=stop_ladder_would_replace / stop_ladder_would_close`，沿用 `:897-1014` 的形状，附带 `target_price`、`level` 和将撤的单号。

**10. `break_even_shadow.py`**

- 改为每轮输出各在仓仓位的 `{derived_level, target, action}`。
- 在等稀有真实样本期间，这是一个持续可观察的量。

**11. 需用户知情的既有写入**

- live 分支会先撤掉未成交的入场腿（`executor:240-251`）。管理侧的保本同样如此（`strategy_management_executor.py:815, 1497`）。与"不加仓"一致，见第 5 节。
- 换止损只撤主止损，备份止损留给 `trigger_backup_stop_executor` 重算。

**12. `confirmed_partial_close` 触发**

- 按新规则（无止盈、无保本消息时止损不动）应当退役。
- 复合路径的减仓后保本自己处理止损，不依赖它。
- 见第 5 节。

## 3. 交付 3：消息列的最小扩展

**新增纯函数**（`break_even_reference.py`，不抛异常）：

```python
def resolve_stop_ladder_reference(*, entry_reference, side, filled_tp_level,
                                  strategy_take_profit_prices) -> BreakEvenReference
```

- **档位 ≤ 1 或未知**：原样返回 `entry_reference`，只在证据里加 `filled_tp_level`。这就是已上线的行为。
- **档位 N ≥ 2**：
  - `price = TP(N−1)`，`source = "strategy_take_profit_level_{N−1}"`，并入 `STRATEGY_SOURCES`。
  - 校验：正数，且在入场参考价的盈利一侧（空单 < 参考价，多单 > 参考价）。
  - 校验不过 → 退回 `entry_reference`，证据写 `ladder_fallback_reason`，记低等级事故。
- **止盈价来源**：
  - binding draft 的 `take_profit_legs[i].price`（1.3）。
  - 备选：`lifecycle.take_profit`，按方向排序。

**接入点**：`_break_even_reference_for_batch`（`strategy_management_planner.py:1669-1742`），在 `resolve_break_even_reference` 之后、`dispose_pending_break_even_prices` 之前调用。

- R2 的"消息给了更紧的价就采纳"自然以阶梯目标为比较基准。
- 档位取 `derive_filled_tp_level(binding.id)`，与自动列同一个函数。
- `stop_ladder_mode=shadow` 时，只把"本应目标"写进 `target_snapshot["stop_ladder"]`，实际仍用入场参考价。

**`planned_tpsl_json` 如何承载（不改表结构）**：

- 仍是 `break_even_reference_price` 和 `_source` 两个键，值换成阶梯目标。
- 再加一个仅作证据的 `break_even_reference_filled_tp_level`。
- 执行侧四处取价都经过 `break_even_target_price(leg)`（`:183-197`），它只读 price 键，所以**执行器零改动**。
- 旧批次没有这些键，自动回落，行为不变。

**证据**：`target_snapshot["break_even_reference"]` 增加四项：

- `filled_tp_level`；
- `per_position_levels`；
- `fill_evidence_refs`（订单号与账本行 id）；
- `take_profit_prices` 和 `price_source`。

**与自动列的收敛**：
- 自动列已把止损移到同一目标 → 消息列走 `kept_tighter_existing_stop`，零写入（状态文档第 11 节）。
- 冲突时的全平：
  - `move_stop_to_break_even` 已经是全平。
  - 复合指令依赖未部署的 `dde69b31`。

## 4. 交付 4：上线、测试、回滚

### 4.1 分阶段

**阶段 0（L0，无部署）**：第 0 节点查，结论记档。

**阶段 1（L1，成交证据 + 影子）**

- 内容：
  - 成交证据判定；
  - 观测归桶修复；
  - 账本 `filled` 状态；
  - 档位推导；
  - 新设置，默认 `disabled`；
  - 影子输出；
  - 值守登记。
- 部署后设 `stop_ladder_mode=shadow`。零交易所写入。
- 本阶段**不改执行器**：live 闸门替换与旧常量删除都在阶段 3。
  - 影子行的 `execution_mode` 是 `shadow`，走 `_execute_shadow_market_decisions`，本来就到不了 live 分支里的旧闸门。
- 涉及对账与保护台账，因此全量测试一次。
- 验收标准：直到出现第一个真实止盈成交样本，核对档位推导正确、`protection_missing` 误报消失。
- 到达率低，按"未到达的样本"登记。

**阶段 2（L3，消息列阶梯 live）**

- 这一改动只影响 TP2 以上之后到来的保本类消息。
- 给出确切的变更与回滚计划；零在途窗口；开关不动。

**阶段 3（L3，自动列 live，含市价全平）**

- 本阶段候选把执行器的 live 闸门从 `BREAK_EVEN_*_RELEASED_POS_IDS` 换成 2.3 第 8 项的谓词，并同时删除两个旧常量。
  - 旧常量与 live 闸门必须在同一个候选里替换。
  - 否则旧闸会把 live 行全部扣成 `blocked`（`executor:418, 506`）。
- 先把水位设为当前最大 `binding.id`。只对新仓位生效。
- 首笔样本核对通过后，再决定是否下调水位。

### 4.2 测试计划

**任务指定用例**（空单，区间 80500–81600，止盈 79800/79100/78400，仅腿 1 在仓，`avgPx` 80436，原止损 82300）：

| # | 场景 | 期望 |
|---|---|---|
| 1 | TP1 成交（现价 79790） | 档位 1 → `tp_level_fill/level:1` → 新止损 80500（`strategy_first_leg`），只撤主止损；身份比对仍用 80436 |
| 2 | TP2 成交（现价 79090） | 档位 2 → 止损 79800 |
| 3 | TP2 之后 KOL 发"及时移动止损" | 参考价 79800（`strategy_take_profit_level_1`）。自动列已动过 → `kept_tighter_existing_stop`，零写入；自动列为 shadow → 写 79800 |
| 4 | TP2 成交，但执行时现价 79850（79800 ≤ 现价） | `full_exit` → `close_exact_position`，回读仓位为 0，未挂任何新止损 |
| 5 | 已有止损 79500（由 `adjust_stop_loss` 设），目标 79800，现价 79200 | `keep_tighter_stop`，零写入 |

**其余用例**

- **映射**：
  - `leg_index` 为 4 的替换单按价格映射到正确档位；
  - 缩档后只剩 TP1；
  - KOL 改过止盈后匹配不到 → 档位未知 → 自动列不动，消息列回退到 80500。
- **判定**：
  - KOL 减仓消费 TP1（撤单） → 不算成交；
  - 我们自己的替换 → 不算；
  - 无平仓单佐证 → 未证明，并记事故；
  - 同量两档只靠形式 ii 的价格区分；
  - 末档整仓平掉；
  - 两仓同价同量；
  - 观测缺失。
- **两腿**：
  - 两仓都在 → 档位 1 的目标是 81050；
  - 腿 1 成交 TP1 而腿 2 无止盈 → 两仓都移。
- **幂等与恢复**：
  - 同一档位重复 tick 只有一行；
  - 重启后推导结果相同；
  - 提交结果未知 → `recovery_required`，不重发；
  - 瞬时 preflight 失败重试后成功；
  - 到期后 `blocked`；
  - 低档位 `recovery_required` 时挡住高档位。
- **并发**：管理批次进行中 → 不认领；批次完成后保持现状或照常执行。
- **模式**：
  - `disabled`：零行为；
  - `shadow`：只记 would-*，零 intent；
  - `move_stop_to_breakeven_after_tp1=False`；
  - `auto_trade` 关 → 退化为不执行。
- **其他**：tick 归一（中点落在半 tick，多单和空单方向各一例）；多单镜像全套。
- **既有测试保持通过**：
  - `test_break_even_convergence_*`
  - `test_break_even_reference`
  - `test_dabiaoke_tp1_break_even_regression`
  - `test_position_take_profit_orders`
  - `test_take_profit_fill_evidence`
  - `test_release_gates`（随闸形状更新）
- **最终候选**跑一次 `uv run python -m pytest -q`。

### 4.3 回滚

- **首选**：把 `stop_ladder_mode` 设回 `disabled` 或 `shadow`，无需部署，立即停止新写入。
- **代码回滚**：`tg-deploy <上一个生产 sha>`，要求零在途。
- **回滚后的残留**：
  - 已写的订单级证据、账本 `filled` 状态、收敛行。
  - 旧代码对 `tp_level_fill` 行只会在认领时遇到未知触发类型。回滚前应把非终态行置为 `blocked/rolled_back`（一条点更新，写进回滚步骤）。
  - 旧 `protection_health` 不加载 `filled` 行，无影响。

### 4.4 首笔实盘样本清单（自动列）

1. 订单级证据的形式；平仓单的价格、数量、时间与该档吻合；档位与价格匹配正确。
2. 收敛行的 `trigger_identity`、`target_snapshot.stop_ladder`、每腿 `decision_json`。
3. 交易所层面：
   - 新主止损等于目标按 tick 归一后的值，size 等于在仓量；
   - 旧主止损已撤净；
   - 备份止损在后续轮次被重算到新主止损外侧；
   - 剩余止盈单一张未动。
4. `StrategyBreakEvenConvergenceLeg.avg_entry_price` 仍等于交易所 `avgPx`。
5. 未成交的入场腿已撤，且撤单有回读确认。
6. 同一 tick 内没有重复的"缩量替换"。
7. 若走的是市价全平：
   - 只有一笔 close，intent 为 `confirmed`；
   - binding 和 lifecycle 被对账正确终结；
   - `exit_reason` 合理；
   - 残留的止损单和止盈单被交易所清掉或由我们清掉；
   - 通知已送达。
8. 值守无误报；`protection_missing` 事故没有针对该止盈单产生。

## 5. 交付 5：风险与待拍板

1. **两腿合并取最大值**（1.4）。请用户确认。
2. **"入场价"按"仍在仓的腿"还是"曾成交的腿"。**
   - 现状是前者（规格 3.1）。
   - 腿 1 单档整仓止盈后只剩腿 2 → 目标是区间远端（空单 81600）。
   - 用户原话是"入场"。是否改为后者？
3. **止盈后撤掉未成交的第二腿**：既有行为，未被明确批准过，请知情确认。
4. **`confirmed_partial_close` 自动保本触发**：是否退役（2.3 第 12 项）。
5. **止盈单部分成交，或尺寸与分配比例不符**（缩档、减仓后重挂）：
   - 档位按价格定、不按数量定。
   - 部分成交无法证明，不升档，只告警。是否接受？
6. **形式 ii 是推断，不是身份证明**（1.7 第 1 项）。
   - 极端情形：人工按止盈价、恰好平掉同样数量。
   - 视 Q8 的结果决定能否升级为身份证明。
7. **KOL 改止盈之后"档位"指什么**：
   - 建议以成交那张单挂出时的阶梯为准，目标价取同一阶梯的 TP(N−1)。
   - 匹配不到则不动，只告警。
8. **功能上线前已开的仓位**：
   - 默认用水位排除在自动列之外。
   - 消息列不设水位，因为有证据才会升档。
9. **超过三档的策略**（最多 5 档）：按通式 N → N−1 处理？
10. **检测延迟下的全平**：
    - WS 丢帧时退到约 62 秒轮询。
    - TP2 成交后价格快速回到 TP1 → 按规则市价全平。
    - 这是规则本意，但首次发生时会显得激进。
11. **消息列第二、三行对复合指令的依赖**：上游两个拦截和 `dde69b31` 都未部署（政策文档第 3 节第 4、5 条）。阶梯不解决这两个问题。
12. **放行从"按仓位常量"改为"设置 + 谓词"**，等于撤销 `release_gates.py` 文档里"每次放行都要一次部署"的设计。
    - 需用户明确同意。
    - 保留黑名单常量，并在启动日志和 `/api/runtime/release-gates` 里报告。
13. **`lifecycle.stop_loss` 不回写**（状态文档第 8 节第 3 条）：
    - 页面仍显示原止损。
    - 阶梯生效后这个差异更大，建议另开一个只影响展示的修复。

### Critical Files for Implementation

- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/position_take_profit_orders.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/break_even_convergence_worker.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/break_even_convergence_executor.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/break_even_reference.py
- /Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py（`:1170-1210` 对账顺序；`:1304-1371` 观测归桶）

其余会改到的文件：
- `strategy_management_planner.py:1669-1742`
- `trading_settings.py`
- `release_gates.py`
- `break_even_convergence_planner.py:68`
- `protection_health.py`
- `oncall_detector.py`
- `break_even_shadow.py`

---

## 附：第 0 节点查结果（指挥会话，2026-09-21；Q1–Q5 在 09-16 的生产快照上执行，Q7 为线上点读）

| # | 结果 | 结论 |
|---|---|---|
| Q1 | 全部 2 条收敛：`confirmed_partial_close`（trigger_identity 95 / 96）、`live`、`blocked / break_even_market_preflight_unavailable`、2026-08-03 | **`tp1_fill` 触发的收敛一条都没有过**——2.1 的判断成立 |
| Q2 | `position_take_profit_orders`：expired 213、completed 6、active 3、cancelled 1、**filled 1** | 真实成交证据是 A-5c 的 `completed`（6 条，均带 `native_tpsl` 证据），而自动保本入口只认 `filled + tp1_fill` |
| Q3 | 止盈腿 `leg_index` 出现 4（3 行）；`planned_size` 最常见值 `50.0`(127) / `20.0`(92) / `30.0`(83) / `100.0`(43)，之后才是张数 | `planned_size` 存的是百分比、`leg_index` 会在替换后递增——两条假设均成立 |
| Q4 | 09-01 起 50 条观测，`pending_tpsl_json <> '[]'` 的为 **0** | 观测里的挂单恒为空——成立 |
| Q5 | 账本止盈行 `protection_missing`：`position_mutation_intent_readback` 13 + `trigger_take_profit_pending_readback` 7（另有若干） | 成交后的止盈单落成 `protection_missing` 误报——与 1.1 一致 |
| Q7 | `move_stop_to_breakeven_after_tp1 = true`、`take_profit_allocations = [50, 30, 20]`、`entry_range_order_style = eager`、`management_execution_mode = live` | 用户一直开着"TP1 后自动保本"，但它从未执行过 |

Q6（WS `TriggerOrder.TS` 终态）与 Q8（平仓单是否带来源止盈单号）需要读 WS 原始帧 / 交易所 REST，留到阶段 1 实施时核实。
