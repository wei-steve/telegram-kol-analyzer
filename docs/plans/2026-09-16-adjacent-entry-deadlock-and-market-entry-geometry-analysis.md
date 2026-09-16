# 相邻消息准入死锁与「市价进场/价格」几何拒单：根因分析与修复选项

日期：2026-09-16
状态：分析完成，等待拍板修复方案
触发：峰哥高级会员群 2026-09-15 14:45 UTC「以太坊现价2415做多 / 止损2355 / 止盈2620」
识别成功、群为 auto_trade，但交易所上既无持仓也无挂单；6 小时后入场指令过期。

本文只读生产库 `data/research.db`（按主键 / 索引列点查，未做全表扫描）与生产 HEAD
`5f26e721` 的代码得出。其他会话 2026-09-15/16 的改动（上下文重分析循环、
消息处理任务守护、确认提醒自锁）**没有触及**本文涉及的四个文件：
`entry_assembly_admission.py`、`adjacent_entry_assembly.py`、`entry_price_geometry.py`、
`auto_trade_execution.py`。所以两处缺口在当前生产代码里原样存在。

## 1. 结论

入场在下单前被两道彼此独立的闸门拦住：

1. **相邻消息准入死锁（真正卡住的那道）。** 准入模块把 3 分钟前那条
   「比特币，以太坊策略触发止损！」（raw 16972）判成「还没处理完」，于是把入场
   deferred；而 16972 的权威决策早已是终态「非策略 / skipped / mimo_no_action」，
   永远不会再产出候选，所以这个「没处理完」永远不翻转，直到 6 小时截止过期。
   **这不是 16979 一条的偶发**：同一形状在 2026-09-04 到 09-16 之间杀死了
   6 条 auto_trade 群的入场（第 3.4 节），全部 `entry_never_submitted`。
2. **几何门与识别提示词契约矛盾（解开死锁后仍会拒单）。** 提示词要求 MiMo 在
   原文同时有市价与具体点位时输出 `市价进场/1730附近` 这种形状；几何门
   `_has_unpriced_market_leg` 把同一形状判成「没有价格的市价腿」→
   `entry_price_geometry_ambiguous` → 拒单。峰哥每条 ETH 策略都落在这个形状上。

两处都不是其他会话本轮改动引入的，是 2026-08-08（准入加固）和更早（几何门）
就在的规则在真实消息序列上的组合结果。

## 2. 16979 的时间线（UTC）

| 时间 | 环节 | 结果 |
|---|---|---|
| 14:41:50 | raw 16972「比特币，以太坊策略触发止损！」落库 | 第一次权威处理 `authoritative_failed`，重试后 14:45:17 终态：非策略 / skipped / mimo_no_action；**无候选** |
| 14:45:23 | raw 16979 入场消息落库入队 | 作业 5218 正常 |
| 14:46:09 | MiMo 识别 | 是策略，ETH long，entry `市价进场/2415`，SL 2355，TP 2620，置信 0.98 |
| 14:46:24 | 上下文解析 | new_thread，生命周期 1200 = pending_entry，候选 2398，指令项 1164 |
| 14:46:24 | 第一次几何检查（`auto_trade_execution.py:867`） | indeterminate，只发告警（事件 4251，Telegram 消息 4457），不拦 |
| 14:46:24 | 相邻准入（`auto_trade_execution.py:1013`） | **deferred / adjacent_entry_context_pending，阻塞消息 [16972]**，attempt 21 |
| 14:46:29 起 | 准入 reconciler 每轮重查 | 每次同样答案，attempt 21 的 updated_at 一直刷新到 20:46:20 |
| 18:29:07 | 生命周期 1200 | expired（生命周期自身的过期，与准入无关） |
| 20:46:26 | 指令项 1164 | failed / escalation_state=expired；合约 410 expired / execution_contract_deadline_elapsed |

交易所侧无任何写入：`attempted_exchange_write = 0`，无 execution_binding，无 order leg。

## 3. 根因一：准入把「已终态、无动作」的邻居判成「未处理完」

### 3.1 机制

`entry_assembly_admission._load_source_facts()`（约 416–437 行）给每条相邻消息分类。
对证据 `extraction_status == completed` 的邻居：

```python
action_expected = (
    recognition_result == "是策略"
    or has_material_strategy_evidence(strategy)
    or lifecycle_event.event_type != "none"
)
application_pending = fragment_application_pending or (action_expected and raw_id not in candidate_raw_ids)
kind = "unresolved" if application_pending else "unrelated"
```

`select_adjacent_entry_fragments()` 只要段内有一条 `unresolved` 就返回
`pending / adjacent_entry_context_pending`。判据里**只看证据，不看权威决策**。

### 3.2 这条规则原本要解决什么

2026-08-08 `b72f776b`「harden adjacent entry assembly invariants」引入，
2026-08-10 加固计划（`docs/archive/plans/2026-08-10-adjacent-entry-admission-hardening.md`）
的目标是「区分 MiMo 的惰性占位符与**尚未落地的可执行证据**」。它要覆盖的是一个
**瞬态窗口**：邻居的证据版本已经写入，但权威处理器还没跑完、候选还没写进
`signal_candidates`。在那个窗口里把邻居当作 unresolved 是对的，否则入场会抢在
「取消入场」「反向开仓」这类邻居生效之前提交。

测试 `tests/test_entry_assembly_admission.py` 约 460–500 行把这个窗口固化为
「非策略 + lifecycle_event=cancel_entry + 无候选 ⇒ deferred」。注意那个测试里
**没有** `recognition_decisions` 行，正是瞬态窗口的形状。

### 3.3 缺口

规则没有区分「候选还没来」和「候选永远不会来」。当权威决策已经落成终态且合法地
决定不动作，`raw_id not in candidate_raw_ids` 永远为真，邻居就永远 unresolved。
三种证据形状都能触发：

| 形状 | 例子 | 证据字段 | 权威决策 |
|---|---|---|---|
| 生命周期事件指向别的策略 | 16972「比特币，以太坊策略触发止损！」 | `lifecycle_event.event_type=exit_position`（BTC 1198） | 非策略 / skipped / mimo_no_action（BTC 已 exited，ETH 未入场） |
| 入场确认但无价格 | 17018「比特与以太现价开一层多单」、16916「正常仓位操作区间市价入场」 | `event_type=entry_confirm` | 非策略 / skipped / no_actionable_intent |
| strategy 有实质字段但判非策略 | 15808「BTC市价77700附近，在接一个多单直接」 | `strategy.entry=市价77700附近, side=long` | 非策略 / skipped / mimo_no_action |

三种决策在代码里都是终态：`authoritative_recognition.py` 2570–2590 行，
`blocked`（源消息删除屏障）、`skipped/<lifecycle_not_applied>`、
`skipped/mimo_no_action`、`skipped/auto_trade_not_configured`、`completed`。
只有两种不是终态：`deferred`（源消息屏障 hold）和 `skipped/mimo_authoritative_failed`
（16972 第一次就是它，随后作业重试）。

### 3.4 历史命中（`entry_assembly_attempts` 全表只有 24 行，逐条核对）

| attempt | 入场消息 | 群 | 阻塞消息 | 阻塞消息决策 | 入场结局 |
|---|---|---|---|---|---|
| 11 | 14843「以太坊做多 现价2440附近」09-04 | 峰哥 | 14837「止损改为78000，仓位不变」 | 非策略 / mimo_no_action，event_type=position_update | item 969 failed |
| 14 | 15496「以太坊做多 现价2460附近」09-08 | 峰哥 | 15471「比特币止损离场」 | 非策略 / mimo_no_action，event_type=exit_position | item 1029 failed / expired |
| 15,16 | 15809「陈哥 BTC 77700附近做多」09-10 | 陈哥 | 15808「BTC市价77700附近，在接一个多单直接」 | 非策略 / mimo_no_action，strategy 有实质字段 | item 1063 failed |
| 20 | 16913「陈哥 BTC 76500-76800 做多」09-15 | 陈哥 | 16915（已删）/ 16916「正常仓位操作区间市价入场」 | blocked/source_message_deleted；skipped/no_actionable_intent | item 1160 failed / expired |
| 21 | 16979「以太坊现价2415做多」09-15 | 峰哥 | 16972「比特币，以太坊策略触发止损！」 | 非策略 / mimo_no_action，event_type=exit_position | item 1164 failed / expired |
| 23,24 | 17019「军长 比特与以太现价开一层多单」09-15 | 军长 | 17018 同文无价格版 | 非策略 / no_actionable_intent，event_type=entry_confirm | item 1172 failed，事件 2180 |

三个群在服务器 `config/groups.yaml` 里都是 `trading_mode: auto_trade`。
其余 7 条 attempt（10、12、13、17、18、19）正常 woken，说明瞬态窗口的设计本身在工作；
死的全是「邻居终态无动作」这一种。

注意 2026-09-13 峰哥「以太坊现价2474做多」（16518）过期原因是 `ws_observation_pending`
（事件 2129），是另一条路径（私有流观测缺口），不算在本组里。

### 3.5 现有的告警覆盖

每次过期都产出 `entry_admission_expired`（severity high，已投递），但摘要里只有
`reason_code=adjacent_entry_context_pending`，不带阻塞消息 id 与其决策，所以从告警上
分不出「邻居真的还在处理」和「邻居永远不会处理」。

## 4. 根因二：几何门与识别提示词契约矛盾

### 4.1 契约

`ai_recognition_config.py:119` 与 `prompt_defaults.py:55`：

> 如果原文同时出现市价/现价入场和具体入场点位，`strategy.entry` 必须输出
> `市价进场/1730附近`，不能只输出 `市价进场`。

对「以太坊现价2415做多」MiMo 输出 `entry = 市价进场/2415`，`order_type = market`。

### 4.2 几何门

`entry_price_geometry._proves_absolute_candidate_field()` 对 `entry_prices` 先调
`_has_unpriced_market_leg()`（498 行）：找到「市价 / 现价」标签后，要求**紧挨着**它的前或后
就是数字；`市价进场/2415` 中间隔着「进场/」，判成无价市价腿 → 整个字段 indeterminate →
`entry_price_geometry_ambiguous`。

本地用同一函数复现（`python -B`，不写字节码）：

| entry_text | `_has_unpriced_market_leg` | 几何结论 |
|---|---|---|
| `市价进场/2415` | True | indeterminate / ambiguous |
| `市价进场/2507附近` | True | indeterminate / ambiguous |
| `市价2415` | False | valid |
| `现价2415` | False | valid |
| `现价68000/挂单67000`（现有测试） | False | valid |
| `现价/挂单67000`（现有测试，故意拒） | True | indeterminate |

现有测试 `test_market_relative_entry_expression_is_not_treated_as_absolute` 故意把
`现价/挂单67000` 判成 ambiguous，理由成立：那是**两条腿**，市价腿没有价格、挂单腿有。
`市价进场/2415` 是**一条腿**：市价，参考价 2415。两者形状相近但语义不同，
现在的判据分不开。

### 4.3 在执行路径上的位置

`auto_trade_execution.py`：

- 867 行第一次几何检查：失败只入队告警（事件 `entry_price_geometry_rejected`），**不拦**。
- 1013 行相邻准入：deferred 则直接返回，后面的都不跑。
- 1133–1150 行终局几何检查：`geometry = candidate_geometry`，第一次没过就不再重算，
  直接 `_record_entry_geometry_rejection` → `skipped / entry_price_geometry_ambiguous`。

所以 16979 即使准入放行，也会在 1150 行被拒；这就是为什么必须两处一起修。

### 4.4 影响面

生产库里 `entry_text LIKE '市价进场/%'` 的入场指令项 6 条：4 条被更早的门
（`kol_or_group_auto_trade_disabled`）拦住、1 条 ws 观测过期、1 条就是 16979。
没有任何一条到过 1150 行，所以这条契约矛盾此前从未在生产上「显形」，只在告警里
反复出现（事件 4168、4177、4193、4207、4229、4251 都是同一形状）。

## 5. 修复选项

### A. 准入：终态决策视为「已处理」（推荐）

在 `_load_source_facts()` 里为相邻消息多读一份 `recognition_decisions`
（`raw_message_id in raw_ids`，按 id 点查，不扫表）。分类改为：

```
邻居无候选 且 action_expected：
  - 无决策行，或决策 automation_status ∈ {deferred, failed, uncertain}，
    或 (skipped 且 reason == mimo_authoritative_failed)      → unresolved（瞬态，保持现状）
  - 决策 automation_status ∈ {completed, blocked} 或
    (skipped 且 reason ≠ mimo_authoritative_failed)          → unrelated（终态，无动作）
```

`fragment_application_pending`（证据里有 entry_fragments 但还没落成 fragment 行）
与本判据无关，原样保留。`extraction_status != completed`、活跃提取租约、
malformed JSON 三种 fail-closed 分支原样保留。

- 需改测试：现有「非策略 + cancel_entry + 无候选 ⇒ deferred」的用例保留（它没有决策行），
  **新增**同形状加一条终态决策行 ⇒ 不 deferred 的用例；再补一条
  `skipped/mimo_authoritative_failed` ⇒ 仍 deferred 的用例。
- 一个必须回答的边界：源消息删除屏障 `blocked/source_message_deleted`（16915）当终态，
  因为删除路径自己有 `source_message_deletion_worker` 收口，且该消息在
  `raw_messages.source_status='deleted'`。
- 重新识别（上下文重分析、A-16d `/choose` 后重识别）会写新的证据版本并更新决策行，
  reconciler 下一轮（5 秒后可重查）自然读到，不需要额外处理。
- 已经过期的 6 条**不补单**。2026-08-10 加固计划的原则是「never create a late order
  automatically」，而且行情早已走开。

### A'. 准入：只按证据判「与本策略无关」（不推荐）

把 `action_expected` 收窄为「lifecycle_event 的 symbol/side 与本策略相同」。问题是
它还是不看决策：一个 `hold` 决策（等待 `/choose`）后来可能真的产出针对本策略的动作。
A 直接读决策，语义更准。

### B. 几何：认出「市价 + 动作词 + 分隔符 + 单个价格」是一条带参考价的市价腿（推荐）

在 `_has_unpriced_market_leg()` 中，市价标签之后允许一段**动作词**
（进场、入场、开仓、开单、建仓、介入、entry、open）与一个分隔符（`/`、`：`、`:`、空格），
再紧跟一个绝对价格（可带 附近/左右/一线），则该市价腿视为有价。判据必须**同时**满足：

1. 分隔符之后到下一个分隔符之前**只有一个**绝对价格；
2. 那一段里**没有**另一条腿的标签（挂单、限价、limit、补仓、加仓、首仓、接）。

这样 `市价进场/2415`、`市价进场/2507附近`、`市价进场/1730附近` 通过；
`现价/挂单67000`、`market / 2`、`市价-100U` 保持 indeterminate（现有测试不改）。
通过后 `extract_normalized_prices` 会拿到 2415 作为 entry_values，方向几何
（多单 SL < entry < TP）照常校验：2355 < 2415 < 2620 成立。

### B'. 改识别提示词让 MiMo 输出 `现价2415`（不推荐）

提示词版本在 `ai_prompt_definitions` 里按库管理，模型输出不确定，且不能修历史与
`ai_recognition_config.py` 里已写死的契约文本。解析器修法是确定性的、可单测的。

### C. 告警补信息（顺手，低成本）

`entry_admission_expired` 的 `redacted_summary` 加 `blocking_raw_message_ids` 与每条
阻塞消息的 `automation_status/automation_reason`。以后一眼分得出瞬态与终态。

## 6. 验证等级与风险

A 与 B 都**改变交易所写入的准入语义**：原本一定不下单的两类入场会开始下单。
按 AGENTS.md，这属于必须明确纳入批准范围的 exchange-write semantics 变化，
建议按 **L2** 走：

- 开发期：两个模块的聚焦测试 + 最终候选跑一次全套。
- 部署后：一个 30 分钟观察窗，理想样本是一条 auto_trade 群里「无动作邻居 + 完整入场」
  的自然序列，以及一条 `市价进场/价格` 形状的入场。消息流量不可控，按既定规则
  用只读监控自行收口，上限 24 小时。
- 回退：`tg-deploy <前一 SHA>`，无状态需要回滚。

风险点：A 放行后，入场会在邻居终态之后**立即**提交；如果邻居的「无动作」决策本身是错的
（例如本该是取消入场），A 会让错误更快变成一笔真实订单。缓解是 A 只认**终态**，
并保留 C 的告警细节；不建议为此再加等待时间，那只是把死锁换成慢锁。

## 7. 待拍板

1. 是否按 A + B + C 一起修，还是先修 A（解死锁）再看 B。
2. B 的动作词与分隔符白名单是否按第 5 节列的范围。
3. 已过期的 6 条入场确认不补单。
4. 验证按 L2，观察窗样本要求是否接受「自然到达、上限 24 小时」。
