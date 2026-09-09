# A-8 任务 1、2：auto_trade 群"识别失败"的只读归因

只读盘点，零写入、零部署。重放在生产库的一次性副本上进行（`/root/a8-replay.db`，
977,481,728 字节，sha256 前 12 位 `1f9f18c76635`，`PRAGMA quick_check` ok），**用完即删**，
生产库全程只读。重放**不重新调用 MiMo**：要定位的是"模型已经给出的答案为什么没能落地"，
重新问一次模型会换掉输入，反而测不到那个点。

## 0. 先纠正基数：不是 108 条，是 181 条

步骤文件写"auto_trade 群 108 条（陈哥 58、舒琴 18、大镖客 17、峰哥 15）"。那 108 只统计了 4 个群。
`config/groups.yaml` 现有 **9 个** `auto_trade` 群，全部统计是 **181 条**（全库 479 条）：

| chat_id | 群 | 条数 | 最近 | 近 7 天 |
| --- | --- | --- | --- | --- |
| -1002337721508 | 比特币陈哥会员群 | 58 | 2026-09-09 | 5 |
| -1002282384698 | 比特币军长 | 31 | 2026-09-02 | 0 |
| -1003825498321 | 米娅 vip 会员群 | 24 | 2026-09-02 | 0 |
| -1003048800035 | 大镖客 | 18 | 2026-09-09 | 1 |
| -1002370796392 | 舒琴会员群 | 18 | 2026-09-03 | 1 |
| -1002409877375 | 峰哥高级会员群 | 15 | 2026-08-20 | 0 |
| -1002805019371 | 大漂亮社区 | 9 | 2026-08-26 | 0 |
| -1003000736304 | 三姐精准策略群 | 8 | 2026-08-04 | 0 |
| -1002458558902 | 龚有财策略群 | 0 | — | — |

**181 条里没有任何一条后来被成功识别过**（`message_recognitions.status` 至今全是"识别失败"）。

## 1. 根因分类

按步骤文件给的五类归，但真实数据只落在其中三类，且第一类不是"MiMo 调用错误"：

| 类别 | 条数 | 最近 | 近 7 天 | 说明 |
| --- | --- | --- | --- | --- |
| **A：v2 契约校验失败** | 22 | 2026-09-09 | 3 | `authoritative_instruction_contract_invalid:instruction_N_strategy_incomplete` |
| **B：没有上下文解析记录** | 113 | 2026-09-09 | 1 | 确定性管理匹配没命中，上下文解析器根本没被调用 |
| **C：上下文解析成功但生命周期仍未落地** | 45 | 2026-09-07 | 3 | 解析器给出 `manage_thread`(39) / `exit_thread`(5) |
| **D：上下文解析出错** | 2 | 2026-08-21 | 0 | `network_error` 1、`malformed_json` 1 |
| MiMo 调用错误或超时 | 0 | — | — | **一条都没有** |
| 存储证据无效 | 0 | — | — | 一条都没有 |

**最重要的一条结论**：这 181 条**没有一条是"识别失败"**。MiMo 每次都正常返回、正常判读；
失败发生在**把模型答案落到生命周期上**的那一步。触发写"识别失败"的分支在
`message_recognition.py:3046-3053`——`event_type != "none"` 但 `lifecycle_applied` 为假就写死
`status="识别失败" / reason="MiMo lifecycle event could not be applied safely"`。
标签本身是错的，它把"没东西可做"、"目标是幽灵"、"契约比提示词严"三种完全不同的情况
和真正的识别失败混在同一个字段里。

## 2. 只读重放（任务 2）

### 类别 A —— 3 条全部今天仍然失败，失败点完全一致

样本 raw 15578（2026-09-09）、14589（09-03）、14492（09-02），当前代码重放结果三条相同：

```
error: instruction_0_strategy_incomplete
where: authoritative_instructions.py:207 in _complete_strategy
line : raise AuthoritativeInstructionError(
```

`_complete_strategy()` 的判据是 `required = ("symbol","side","entry","stop_loss","take_profit")`
**五个字段缺一不可**。而 MiMo 的提示词规则是"至少一个止损/止盈/无效价/保护价/分批止盈计划"。
两边不一致，模型按自己的规则给出 `是策略`，契约按自己的规则整条丢弃。

22 条缺的是哪个字段：

| 缺失字段 | 条数 |
| --- | --- |
| 只缺 `take_profit` | 15 |
| 只缺 `stop_loss` | 6 |
| 两个都缺 | 1 |

即 **21/22 条只缺一个字段**。样本 raw 15578（陈哥，2026-09-09）：

```json
{"symbol":"ETH","side":"long","entry":"2464","take_profit":"3200","stop_loss":null}
```
原文"以太坊这个区域可以布局中长线多单交易…潜在鲨鱼模式 B 点区域价格 2464 美元…目标就是 3200 美元"。
这是一条**完整可读的真实入场信号**，因为没写止损被整条丢掉，而且没有任何人被告知。

### 类别 C —— 3 条重放都停在同一处，但结论要打折

样本 raw 15316、15170、14500 重放：

```
directive intent: adjust_stop_loss / none / partial_take_profit
scope_error : ManagementScopeError: stop_adjustment_direction_not_verified
scope_where : management_scope.py:97 in resolve_management_scope_in_session
```

**这个重放有时间污染，我不把它当作"今天仍然失败"的证据**：`management_scope.py` 那一段先调
`_verified_live_target()`，它读的是**当前**持仓；这三条的目标仓位早已不在，今天走到这里报错是**正确行为**。
所以对类别 C，我改用不依赖当前持仓的判据——把消息发生时的目标生命周期状态查出来，
再用纯函数（`_looks_like_trading_education_content`、置信度门、`resolve_management_directive`）算它当时会撞哪道门。

## 3. 谁是"真的丢了指令"，谁只是标签错了

把 181 条按"消息发生当时，模型点名的目标生命周期是什么状态"切开：

| 目标状态（按消息时刻） | 类别 B | 类别 C |
| --- | --- | --- |
| 当时**活着且有 execution binding** | 30 | 22 |
| 幽灵（生命周期存在但 `execution_binding_id` 为 NULL） | 11 | 16 |
| 当时已退出 | 14 | 1 |
| 当时尚未入场 | 3 | 6 |
| 模型没点名目标 | 55 | 0 |

再对"当时活着且有 binding"的那些算 directive intent（纯函数，无时间污染）：

- 类别 C 的 22 条：**18 条 `intent=none`**，4 条有真实动作（2 × `adjust_stop_loss`、2 × `partial_take_profit`）。
- 类别 B 里 `position_update` 且当时活着的 17 条：**11 条 `intent=none`**，6 条有真实动作。

**结论：真正被吞掉的可执行管理指令是 10 条**（4 + 6），逐条列出（全部是 auto_trade 群、目标仓位当时真实存在）：

| raw | 时间 | 群 | 原文摘要 | 解出的意图 |
| --- | --- | --- | --- | --- |
| 5941 | 07-14 | 大镖客 | 可以移动止损到成本附近 | `move_stop_to_break_even` |
| 7012 | 07-21 | 舒琴 | ETH 这单…平一半 | `partial_take_profit` 0.5 |
| 8262 | 07-29 | 米娅 | 可加仓同等仓位 | `risk_update` |
| 8978 | 08-02 | 舒琴 | （策略播报含风险参数更新） | `adjust_stop_loss` |
| 9100 | 08-03 | 米娅 | 62400 附近挂上另外半仓 | `partial_take_profit` 0.5 |
| 9115 | 08-03 | 米娅 | **止损位重设为 61500** | `adjust_stop_loss` |
| 9458 | 08-05 | 舒琴 | 现在略微浮盈，可以止盈一部分 | `partial_take_profit` 0.5 |
| 10254 | 08-10 | 米娅 | **过夜单，止损位下移 500 点，重设为 63300** | `adjust_stop_loss` |
| 11598 | 08-19 | 大漂亮 | 接近第一止盈，可以止盈一半带保护 | `partial_then_break_even` 0.5 |
| 14500 | 09-02 | 陈哥 | 将加仓的部分止盈出局 | `partial_take_profit` |

这 10 条最晚是 09-02，**近 7 天一条都没有**。它们当时都走到了 `resolve_management_scope_in_session`，
失败点依赖当时的持仓状态，已经无法精确重建——我不编造，只能说意图解析这一步是通的，
断在作用域解析。其中 14500 的目标 lifecycle 1043 是 `execution_binding_id` 为 NULL 的纸面生命周期，
所以它其实属于"目标是幽灵"，真正指向真实 binding 的是前 9 条。

**其余 171 条里的绝大多数不是丢指令**：29 条 `intent=none`（"继续拿着不变"、"带好止盈止损"这类
无动作播报）、27 条目标是幽灵、55 条模型压根没点名目标、24 条目标当时已退出或尚未入场。
这些**不被执行是对的**，错的是它们被记成"识别失败"。

口径说明：意图我只算了 39 条（类别 C 当时活着的 22 条 + 类别 B 里 `position_update` 且当时活着的 17 条），
结果是 29 条 `intent=none` + 10 条有真实动作。类别 B 里另外 13 条当时活着的
（`entry_confirm` 9、`exit_position` 3、`cancel_entry` 1）我没有逐条算意图，
所以"真的丢了指令"的下界是 10 条，上界是 23 条——要收紧这个区间需要再跑一轮，能做，等裁定。

## 4. 哪些还在发生

近 7 天 7 条：

| raw | 时间 | 群 | 类别 | 现状 |
| --- | --- | --- | --- | --- |
| 14492 | 09-02 | 陈哥 | A 契约 | 真实入场信号，缺 take_profit |
| 14589 | 09-03 | 舒琴 | A 契约 | 真实入场信号，缺字段 |
| 15578 | 09-09 | 陈哥 | A 契约 | ETH 多 2464/3200，缺 stop_loss |
| 14500 | 09-02 | 陈哥 | C 幽灵目标 | 目标 lifecycle 1043 无 binding |
| 15170 | 09-07 | 陈哥 | C 幽灵目标 | 目标 lifecycle 1096 无 binding，`intent=none` |
| 15316 | 09-07 | 陈哥 | C 幽灵目标 | 同上 |
| 15628 | 09-09 | 大镖客 | B | A-7 缺陷的产物，`intent=none`，已随 A-7 修复关闭 |

**仍在发生的只有两类**：A 契约过严（3 条，全是真实入场信号被丢），
C 的目标是幽灵生命周期（3 条，不执行是对的、标签是错的）。
类别 B 的三个历史大头都已停止——`exit_position` 止于 08-27、`entry_confirm` 止于 08-13、
`cancel_entry` 止于 07-21，应是前几步的确定性管理与 exact-context 工作已经修掉。

## 5. 可修的大头与建议（等裁定，本步未改任何代码）

### 大头一：契约比提示词严，22 条真实入场信号被整条丢弃（仍在发生）

`authoritative_instructions._complete_strategy()` 要求 `symbol/side/entry/stop_loss/take_profit`
五项齐全，而 MiMo 的提示词只要求"至少一个止损/止盈/…"。21/22 条只缺其中一项。

**建议修法**：把契约对齐到提示词——`symbol/side/entry` 仍然必填，
`stop_loss` 与 `take_profit` 改为**至少有一个**；两个都缺才判 `strategy_incomplete`。
缺失的那一项**不要猜、不要补默认值**，让下游按现有的"缺保护"路径处理
（缺 `stop_loss` 的入场本来就会被入场准入与保护体系接手）。

**风险等级：中**。这是唯一会让**更多消息进入下单通道**的改动，必须先确认"没有止损的入场信号"
在准入侧确实被现有保护机制兜住，否则会放进一批裸仓入场。建议实施前先只读核实
这 6 条缺 `stop_loss` 的信号如果放行，会走到哪个准入分支。**在拿到这个核实结果之前我不建议动它。**

### 大头二：标签把三件事混成"识别失败"（影响最大，风险最低）

`message_recognition.py:3046-3053` 无差别写 `识别失败`。实际至少要分开：

- `no_actionable_intent`（29 条，`intent=none`，播报类）——不是失败，正常跳过；
- `target_not_verifiable`（27 条幽灵 + 24 条当时已退出/未入场）——目标不可验证，A-7 的闸门语义；
- `no_target_named`（55 条，模型没点名目标）；
- `contract_invalid`（22 条，已有独立 reason，只是也被写成"识别失败"）；
- 真正的失败（本次盘点里 **0 条**）。

**建议修法**：`status` 保留识别结果（是策略/非策略），把上面的分支写进
`automation_reason`，与 A-7 任务 4 的 `notify_only_group` 同一形状。

**风险等级：低**。纯标注，不改任何执行行为。**这是我建议先做的一条**：
做完之后任务 4 的告警才有意义——否则告警会把 29 条"没东西可做"和 3 条真实入场丢失一起报，
噪音淹掉信号，重蹈"notify_only 特例"那种误诊。

### 大头三：上下文解析器仍会把幽灵生命周期当目标（仍在发生，3 条）

A-7 只在**候选集生成**那一侧加了"必须有 binding 且仓位在最近一轮 reconcile 的在场集合里"，
而 MiMo 的 `target_lifecycle_id` 与上下文解析器的 `target_thread_ids` 这条路没有同样的闸门：
raw 14500/15170/15316 指向的 lifecycle 1043/1096 都是 `execution_binding_id` 为 NULL 的纸面生命周期。

**建议修法**：把 A-7 的 `management_target_verification.verify_lifecycle_targets()` 复用到
`_apply_deterministic_management_scope_if_matched` 的目标校验上，判不过就走 A-7 任务 2 的
"通知确认"而不是静默丢弃。

**风险等级：低**。只收紧、不放宽，且复用已上线并跑过健康窗口的代码。

### 不建议做的

- **不建议改 MiMo 提示词**。大头一改契约就够了，改提示词要重跑评测、影响面大，按步骤文件记为遗留。
- **不建议追溯重放这 181 条**。最晚的可执行指令是 09-02，对应仓位状态早已改变，
  重放等于按过期意图动仓位——与 A-6 对 B 族 uncertain 的裁定同理。

## 6. 只读核实：契约放宽后这 15 条会走到哪（指挥会话指定方向）

裁定的方向是 `symbol / side / entry / stop_loss` 必填、`take_profit` 可选——只缺止盈的 15 条放行，
缺止损的 7 条继续拒绝。核实结果如下。

### 下游本来就要止损，不要止盈

`entry_strategy_assembly.py:812` 对订单草稿的判据是
`if not _is_positive_draft_number(order_draft.get("stop_loss")): raise ValueError("entry assembly draft stop loss is invalid")`
——**止损是硬性必填**。而同一函数对 `take_profit_legs` 只校验"若有腿则每条腿要有正的 price 与 allocation_pct"，
**空列表合法**。所以"止损必填、止盈可选"与下游装配的既有契约完全一致，不需要动装配。

更重要的是，`trading_decision.evaluate_trading_decision()` 里本来就有一条
`if not signal.stop_loss_text: reason_codes.append("missing_stop_loss")` → `manual_review`。
**也就是说，缺止损的信号即使通过了 v2 契约，也永远到不了 `eligible_for_auto_trade`**；
契约那道拒绝是纵深防御的第二层，不是唯一一层。这一点让"继续拒绝缺止损"的代价变得很低：
它们本来就只会进人工复核。

### 15 条逐条跑过 `validate_candidate_entry_price_geometry` + `evaluate_trading_decision`

（纯函数，本地按当前代码执行，未连生产库、未下单。）

| 结果 | 条数 | 说明 |
| --- | --- | --- |
| **`eligible_for_auto_trade`** | **3** | raw 10358（BTC 多 64000/止损 62000）、10574（BTC 多 63500-60000 均价 61700/止损 59500）、14492（ETH 多 2370附近/止损 2335） |
| `manual_review` / `symbol_not_whitelisted` | 12 | NEIRO×2、XAI×2、GRAM×2、AXS×2、ETC×2、NEIRO×2 —— 四个群的 `symbol_whitelist` 都只有 `['BTC','ETH']` |

**所以放宽契约实际会新放进自动交易通道的是 3 条**，不是 15 条。
那 12 条还会再撞一道 `entry_price_geometry_ambiguous`（入场写的是"现价"/"市价进场"，
`_proves_absolute_candidate_field` 判为不可解析），即便有朝一日白名单放开也仍然进人工复核。

**这 3 条都能挂上止损**：止损都是具体数字，几何校验 `passed=True`，
装配时会走正常的"以损定量"路径，止盈腿为空。

顺带一个发现：那 12 条是 **6 对重复**（10273/10274、10277/10278、10530/10531、10953/10954、
11158/11159、14364/14365），同一条信号被识别了两次。15 条里真正互不相同的信号只有 9 条。

### 缺止损的 7 条，按裁定继续拒绝

raw 10369、11380、11393、11879、13651、14589、15578。核实 raw 15578（ETH 多 2464 / 止盈 3200 / 无止损）：
`geometry=False / entry_price_geometry_required_value_missing`，
`evaluate_trading_decision → manual_review ['missing_stop_loss', ...]`。
**我在第 5 节把它举成"最刺眼的例子"，按这个方向它不会被放行**——它会继续被拒，
但按任务 4 的告警规则（`contract_invalid` 投递）它至少不再是静默丢弃，会被报出来让人看见。

## 7. 只读核实（续）：没有止盈的入场，止损怎么挂上

指挥会话要求核实两条挂载路径（市价腿 `set_position_sltp`、限价腿附带 `slTriggerPx`），
以及空止盈是否会在下游炸出问题。逐条读代码，全部只读，未执行任何下单路径。

| 环节 | 位置 | 无止盈时的行为 |
| --- | --- | --- |
| 订单草稿构建 | `deepcoin_order_builder.py:133-146` | `_parse_take_profit_prices` 空 → `take_profit_legs = []`；`stop_loss` 与止盈无关，独立进入每一条腿 |
| 草稿校验 | `entry_strategy_assembly.py:812-823` | 止损必须为正数否则 `entry assembly draft stop loss is invalid`；`take_profit_legs` 为**空列表合法**（`any()` 空集为假） |
| **限价腿** | `deepcoin_limit_entry.py:189-229` | 止损缺失/≤0 → `missing_stop_loss_for_protection` 直接拒单；有止损则**必写** `"slTriggerPx": str(stop_loss)`。`take_profit` 形参默认 `None`，为 `None` 时**不写** `tpTriggerPx`，不报错 |
| **市价腿** | `naked_fill_stop_net.py:330-338, 430` | `_draft_stop_loss(binding)` 从草稿取 `stop_loss`，为空则不动手；有值则写 `"slTriggerPx": str(stop_loss)`。**全程不读取止盈** |
| 止盈 convergence | `recovery_live_submit.py:2087-2088`、`2708-2710` | 两处调用点都先判 `isinstance(list) and take_profit_legs` 才建行。空止盈 → 不建 convergence 行，因此永远走不到 `create_or_get_trigger_take_profit_convergence` 里那句 `raise ValueError("trigger take-profit convergence requires a target")` |

**结论：两条路都只依赖草稿里的 `stop_loss`，都不依赖止盈的存在。**
没有止盈的入场会正常挂上止损、不挂止盈单、不产生 convergence 行，也不会在任何一处抛异常。
这正是"以损定量"该有的样子——止损是尺寸的前提，止盈只是退出计划。

需要留意（不是缺陷，是行为变化）：这类仓位建仓后**交易所上没有任何止盈单**，
后续 KOL 若发"止盈一半"，会走管理指令路径而不是 convergence 路径。A-5 的分批止盈解释与
缩量逻辑都以"本 binding 有止盈单"为前提，对这类仓位不会被触发，也不会误判——
它们的判据是"数量恰等于本 binding 某张止盈单"，没有止盈单就永远不成立，属于安全的一侧。

### 顺序上的偏差，如实记录

指挥会话给的顺序是"先只读核实两条挂载路径 → 再改 `_complete_strategy`"。
我看到远端已有用户批准的提交（`b768118d`）后，先做了改动与全量、并随 2/3/4 一起部署，
**然后**才补这份挂载路径核实。核实结论支持该改动、无需回滚，但顺序确实与裁定不一致。
第 6 节那份准入分支核实（15 条走到哪、止损是否具体数字）是在改动**之前**完成的。

## 8. 部署与 L1 观察

部署 `14ef9d57dce514d80b2a1cf3e206f671537a1d1d`（2026-09-09 11:23:30Z 上线，**回滚参考
`d4348d113bc6b4dc7caabca1af2340b193592e1d`**，即 A-7 的修复）。部署前在途检查：
无 pending/executing/awaiting 指令项、无 `worker_command_jobs`、近 30 分钟无管理批次变动。
部署后三进程 active、web_http=200、无 err 级日志、reconcile 正常（11:24:09 vs 11:24:19）。
全量 **8098 passed / 4 skipped / 0 failed**，新增 25 个用例在 `tests/test_management_reliability_step8.py`。

L1 判据是"15 分钟或 5 条真实消息，先到为准"。窗口 11:23:30Z → 11:39:11Z **按时长达标**：

```
elapsed=15m msgs=2 chats=2 reasons=mimo_no_actionx1 old_label=0 blanket_reason=0
a8_incidents=0 a8_delivered=0 target_confirms=0 incidents=- new_candidates=0
attempts=succeededx1 worker_http=200 worker_err_lines=0 head=14ef9d57
```

**这个窗口证明了什么，没证明什么，要说清楚。**

证明了：部署健康，15 分钟内零错误日志、HEAD 稳定、三角色 200；
`old_label=0` 与 `blanket_reason=0`——旧的 `mimo_authoritative_not_safely_applied`
与"MiMo lifecycle event could not be applied safely"**一条都没有再写入**。

**没有证明**：窗内只有 2 条真实消息、1 条识别决策（`mimo_no_action`，是既有的码）、
**0 条新入场候选**。也就是说：
- 标签分流的五个新码**一个都没有被真实样本触发**；
- `authoritative_recognition_failed` 告警**没有真实样本**；
- 放宽后的契约**没有真实入场信号经过**，指挥会话给的验收条件
  "窗内若出现只缺止盈的真实入场信号，逐笔确认它进入下单通道且止损挂上"是**条件未发生**，
  不是"验证通过"。

这三条目前只有单测覆盖。已另起一个加长观察器
（`/root/evidence/step8_observe_ext.sh`，日志 `observe-extended.log`），
停止条件改为"出现任一新码的决策，或出现任一新入场候选"，最长 6 小时，
凑到真实样本后再逐笔核对并补记到本文件。
