# 相邻消息准入死锁与「市价进场/价格」几何拒单：根因分析与修复选项

日期：2026-09-16
状态：A + B + C 已于 2026-09-16 部署 `7a8e67c1` 并通过 L2 观察窗（见状态文档）；用户 2026-09-17 追加拍板 D（纯市价入场，有止损即可下单），规格见第 9 节，实施中（子代理）
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

用户 2026-09-16 确认：四条全部按第 5 节默认值。

## 8. 已批准的实施规格（A + B + C）

给子代理的精确规格。三项各自独立成 commit，顺序 A → B → C。
不做的事：不补已过期的 6 笔入场；不改识别提示词；不碰纯「市价」无价格的形状（见 8.2 末尾）；
不部署、不推送、不发 Telegram。

### 8.1 A：邻居的权威决策已终态且无动作 ⇒ 视为已处理

文件 `src/telegram_kol_research/entry_assembly_admission.py`，函数 `_load_source_facts`。

- 在加载 `candidates` 之后（约 259 行附近）新增一次点查：
  `RecognitionDecision`（`models.py:1710`，`raw_message_id` 唯一）按 `raw_message_id.in_(raw_ids)` 读出，
  建 `decisions_by_raw: dict[int, RecognitionDecision]`。`raw_ids` 最多 41 条，走索引。
- 新增模块级纯函数：

  ```python
  _TERMINAL_NO_ACTION_STATUSES = frozenset({"completed", "blocked"})
  _NON_TERMINAL_SKIP_REASONS = frozenset({"mimo_authoritative_failed"})

  def _decision_is_terminal_no_action(decision) -> bool:
      if decision is None:
          return False
      status = str(decision.automation_status or "").strip().lower()
      reason = str(decision.automation_reason or "").strip().lower()
      if status in _TERMINAL_NO_ACTION_STATUSES:
          return True
      return status == "skipped" and reason not in _NON_TERMINAL_SKIP_REASONS
  ```

  取值依据 `authoritative_recognition.py` 2570–2590 行：`blocked`（源消息删除屏障）、`completed`、
  `skipped/<lifecycle_not_applied>`、`skipped/mimo_no_action`、`skipped/auto_trade_not_configured` 都是终态；
  `deferred`（屏障 hold）、`failed`、`uncertain`、`skipped/mimo_authoritative_failed`（随后作业重试）不是。
- 在 `extraction_status == "completed"` 分支里，把

  ```python
  application_pending = (
      fragment_application_pending
      or (action_expected and raw_id not in candidate_raw_ids)
  )
  ```

  改为

  ```python
  application_pending = fragment_application_pending or (
      action_expected
      and raw_id not in candidate_raw_ids
      and not _decision_is_terminal_no_action(decisions_by_raw.get(raw_id))
  )
  ```

  `fragment_application_pending` 与决策无关（碎片行直接由证据落库），**不**接入决策判断。
  `extraction_status != completed`、活跃提取租约、malformed JSON、无证据版本四个 fail-closed 分支一行不改。
- 注释写明：证据只说「这条消息像有事」，权威决策才说「决定不做」；两者都终态时候选永远不会来，
  引用本文档第 3 节与 6 条历史命中。
- `select_adjacent_entry_fragments`、`_persist_attempt`、`assess_entry_assembly_admission`、reconciler 不改。
  已 deferred 的 attempt 会在 reconciler 下一轮（`ENTRY_ADMISSION_RECHECK_DELAY` 5 秒后可重查）自然读到新分类并 woken。

测试 `tests/test_entry_assembly_admission.py`，复用 `_persist_strategy_and_later_claim`：

- 现有 `test_completed_strategy_evidence_without_candidate_stays_deferred`（422 行）与
  `test_completed_lifecycle_evidence_without_candidate_stays_deferred`（462 行）**原样保留**：它们没有决策行，是瞬态窗口。
- 新增五个用例，形状与 462 行相同（非策略 + `lifecycle_event.event_type` 非 none + 无候选），只多插一行 `RecognitionDecision`：
  1. `skipped / mimo_no_action` ⇒ `decision.status != "deferred"`（16972 形状）；
  2. `skipped / no_actionable_intent` ⇒ 不 deferred（17018 形状）；
  3. `blocked / source_message_deleted` ⇒ 不 deferred（16915 形状）；
  4. `skipped / mimo_authoritative_failed` ⇒ 仍 deferred；
  5. `deferred / <任意>` ⇒ 仍 deferred。
- 新增一个用例：`recognition_result=非策略`、`strategy` 有实质字段（`{"entry":"市价77700附近","side":"long","symbol":"BTC"}`）、
  无候选、决策 `skipped / mimo_no_action` ⇒ 不 deferred（15808 形状）。
- `tests/test_entry_admission_reconciler.py` 新增一个用例：attempt 先因邻居 deferred；随后只写入邻居的终态决策行
  （不写候选、不改证据），`reconcile_due_entry_admissions` 一轮后 attempt 变 `woken`、`released == 1`。
  可仿照 77 行 `test_live_admission_persists_defer_and_wakes_once_on_terminal_evidence` 的骨架。

### 8.2 B：市价标签后接动作词再接单个价格 ⇒ 有价的市价腿

文件 `src/telegram_kol_research/entry_price_geometry.py`，函数 `_has_unpriced_market_leg`（约 498 行）。

- 新增模块级正则：

  ```python
  _MARKET_ACTION_PRICE_AFTER_RE = re.compile(
      r"^\s*(?:进场|入场|开仓|开单|建仓|介入|entry|open)"
      r"\s*[:：=/]?\s*\$?\s*\d+(?:,\d{3})*(?:\.\d+)?(?:万)?",
      re.IGNORECASE,
  )
  ```

  与批准范围的一处细化：分隔符为**可选**（`市价进场2415`、`市价进场：2415`、`市价进场/2415` 三种都放行），
  因为三者语义相同，只放行带 `/` 的会把同一 KOL 的不同标点写法分成两种结局。动作词**必须**存在：
  `market / 2`、`现价/挂单67000` 没有动作词，继续走现有判据 → 仍是无价市价腿。
- `_has_unpriced_market_leg` 的循环里，在现有两个 `continue` 之后加第三个：
  `if _MARKET_ACTION_PRICE_AFTER_RE.search(text[match.end():]): continue`。
  函数其余不改；`_proves_absolute_candidate_field`、`extract_normalized_prices`、`_FIELD_LABELS` 不改
  （「进场」「市价」本来就在 entry 标签表里，剥掉后剩 `/` 落在 `_FIELD_SEPARATORS_RE`，2415 会被正常抽出。
  已在本地验证：只要该函数放行，`市价进场/2415` 得 `valid`、`normalized_entry_prices == ("2415",)`）。
- 调用方 `auto_trade_execution.py` 867 行与 1133 行不改：第一次候选几何通过后，1133 行会再带 `reference_price` 与
  `resolved_entry_prices=entry_range` 重算一次，`_infer_entry_execution_type` 见「市价」返回 `market`，走市价腿。

测试 `tests/test_entry_price_geometry.py`：

- `test_proven_absolute_market_and_multi_leg_entries_are_accepted`（390 行）参数表追加：
  `市价进场/68000`、`市价进场/68000附近`、`市价进场68000`、`市价进场：68000`、`现价入场/68000`、`market entry/68000`。
- `test_market_relative_entry_expression_is_not_treated_as_absolute`（365 行）参数表**原样保留**，追加：
  `市价进场`（无价格）、`市价进场/挂单67000`（动作词后是另一条腿的标签）。
- 新增断言用例：`市价进场/2415`，SL 2355，TP 2620，long，ETH ⇒ `status == "valid"`，`normalized_entry_prices == ("2415",)`；
  同文 short ⇒ `invalid`（方向几何仍在管）。
- `tests/test_auto_trade_execution.py` 若已有「候选几何拒单」的集成用例可低成本复制，则加一条：`entry_text="市价进场/2415"`
  不再产生 `entry_price_geometry_rejected` 事件；没有现成骨架就不加，在状态文档里写明。

**明确不在范围内、要写进状态文档的发现**：纯「市价」「市价进场」「现价做多」「market」这类**没有任何数字**的入场文本，
现在也全部 indeterminate（本地已验证），而调用方只在第一次几何通过后才把参考价传进去，
所以纯市价入场从来过不了这道门。生产上成功的市价入场全是带价格的文本（`76700附近`、`77300`）。
这是独立问题，另议，本次不改。

### 8.3 C：过期告警带上阻塞消息及其决策

文件 `src/telegram_kol_research/entry_admission_reconciler.py` 与 `runtime_incident_adapters.py`。

- `reconcile_due_entry_admissions` 相邻准入的过期分支（约 148–166 行）已经持有 `attempt` 快照：
  解析 `attempt.blocking_raw_message_ids_json`（坏 JSON 当空列表），作为新关键字参数
  `blocking_raw_message_ids` 传给 `_report_entry_admission_expired`。ws 观测那条分支（约 293 行）传 `None`。
- `_report_entry_admission_expired` 新增 `blocking_raw_message_ids: list[int] | None = None`。
  在已有的 `with session_factory() as session:` 里，若列表非空，取前 5 个 id 点查 `RecognitionDecision`，组装
  `blockers = [{"raw_message_id": id, "automation_status": ..., "automation_reason": ...}]`
  （没有决策行的写 `"absent"`），作为 `blockers=` 传给 `incident_reporter`。
- `capture_entry_admission_expired` 新增 `blockers: list[dict] | None = None`。详细摘要（`_summary(...)`）追加两个键：
  `blocking_raw_message_ids`（int 列表，最多 5 个）和 `blocker_decisions`
  （`f"{id}:{status}/{reason}"` 经 `_safe_label` 后的字符串列表，最多 5 个）。最小摘要（fallback）不改。
  写之前读 `runtime_incidents.py` 270–340 行的边界检查，摘要必须在边界内；
  journal 里每天都有 `RuntimeIncidentBoundsError ... retrying minimal` 的例子，这次不能再多一个。
- 指纹（`_fingerprint`）由摘要算出，摘要加键会改变指纹。这是可接受的：过期事件按 `message_instruction_item` 每项恰好一次。

测试：

- `tests/test_entry_admission_reconciler.py` 约 560–585 行的 alert 断言追加：`alert["blockers"][0]["raw_message_id"]` 等于阻塞消息 id，
  `automation_status/automation_reason` 与写入的决策一致；没有决策行时为 `"absent"`。
- `tests/test_runtime_incident_adapters.py`（或该模块现有测试文件）新增：5 个 blockers 的详细摘要通过边界检查、
  `redacted_summary` 含 `blocker_decisions`；用假 `recorder` 断言**没有**退回最小摘要。

### 8.4 状态文档、测试与提交

- 状态文档：`docs/entry-admission-and-market-entry-geometry-status.md`，记录每项的 commit、测试数、
  规格未覆盖处的自选决定、8.2 末尾的范围外发现。
- 每项完成后跑对应聚焦测试；三项装配完的最终候选跑一次全套 `PYTHONPATH=. uv run pytest -q`，
  把通过 / 跳过数写进状态文档。
- 三个 commit，只 `git add` 明确路径，提交前 `git diff --cached --name-only` 核对；
  提交信息分别以 `fix(entry):`、`fix(geometry):`、`fix(incident):` 开头。
- 不推送、不部署、不发 Telegram；完成后向指挥会话汇报。部署、L2 观察窗由指挥会话负责。

## 9. 追加：纯市价入场，有止损即可下单（D）

用户 2026-09-17 拍板：「市价入场，如果有止损价格也是可以的」；时效上限 **3 分钟**；
**入场字段为空不算纯市价**（只认识别结果里明确写了市价标签的，不从正文里猜）。

### 9.1 现状

识别结果的入场字段是 `市价`、`市价进场`、`现价做多`、`market` 这类**没有任何数字**的文本时，
`validate_candidate_entry_price_geometry` 在 `_has_unpriced_market_leg` 处判 `indeterminate`。
`auto_trade_execution.py` 867 行的候选校验不带参考价；1133 行 `geometry = candidate_geometry`，
第一次没过就不再带 `resolved_entry_prices` 重算，直接拒单。所以纯市价入场从来过不了这道门。
生产命中：2026-09-10 军长「比特现价加一层仓，止损放76000」（raw 15832，候选 2293：BTC long，entry `市价`，SL 76000）
结局 `entry_price_geometry_ambiguous`。

入场字段为空的几条（16274、16702、16802）结局是 `missing_entry_range`，它们的正文本来就不是市价开仓指令，保持拒绝。

### 9.2 几何模块 `src/telegram_kol_research/entry_price_geometry.py`

- 新常量 `MARKET_REFERENCE_STALE = "entry_price_geometry_market_reference_stale"`（43 字符，通知 payload 截断上限 64）。
- `EntryPriceGeometryResult` 新增字段 `reference_entry_required: bool = False`；`bounded_evidence()` 增加同名键；
  `_result()` 增加同名关键字参数（默认 False）并透传。`passed` 语义不变（`status == "valid"`）。
- 新公共函数 `is_pure_market_entry_text(entry_text, *, symbol) -> bool`，同时满足才为真：
  1. 文本非空；2. `_MARKET_LABEL_RE` 命中；3. 全文**没有任何数字字符**（`any(ch.isdigit())` 为假）；
  4. 依次剥掉匹配的币种别名（`_strip_matching_symbol_aliases`）、入场标签（`_FIELD_LABELS["entry_prices"]`）、
     货币符号（`_ABSOLUTE_CURRENCY_RE`）、分隔符（`_FIELD_SEPARATORS_RE`）后**剩余为空**。
  期望：`市价`、`市价进场`、`现价做多`、`market`、`BTC市价进场` 为真；
  空串、`市价-100U`、`现价/挂单67000`、`market / 2`、`等回调再市价`、`市价进场/2415` 为假。
- `validate_candidate_entry_price_geometry` 新增关键字参数 `allow_reference_entry: bool = False`。
  仅当 `allow_reference_entry` 为真且 `is_pure_market_entry_text(...)` 为真时走新分支，其余路径**逐字节不变**：
  - 跳过入场字段的 `_proves_absolute_candidate_field` 检查，`entry_values = []`；
  - 止损、止盈的解析与现有代码完全一致（绝对性检查、恰好一个止损、止盈非空即须解析出价格）；
  - **带了 `resolved_entry_prices`**：沿用现有 `elif not entry_values and resolved_entry_prices is not None` 分支，
    把它当入场价交给 `validate_entry_price_geometry` 做完整方向校验（多单 SL < 入场 < 每个 TP）；
  - **没带 `resolved_entry_prices`**（候选阶段）：只做保护侧校验——止损缺失 ⇒
    `_indeterminate(REQUIRED_VALUE_MISSING, "stop_loss", None)`；对每个止盈，多单要求 `TP > SL`、空单要求 `TP < SL`，
    相等 ⇒ `_invalid(EQUAL_BOUNDARY, "take_profit", tp, ...)`，方向反 ⇒ `_invalid(TAKE_PROFIT_SIDE_INVALID, "take_profit", tp, ...)`；
    全部通过 ⇒ `_result(status="valid", reason_code=None, entries=(), stop=stop, take_profits=tps, reference_entry_required=True)`。
    side 不是 long/short ⇒ 与共享校验器一致返回 `_indeterminate(AMBIGUOUS, "side", side)`。
- 新公共函数 `stale_market_reference_result(entry_text) -> EntryPriceGeometryResult`：
  返回 `_indeterminate(MARKET_REFERENCE_STALE, "entry_prices", entry_text)`。

### 9.3 下单路径 `src/telegram_kol_research/auto_trade_execution.py`

- 模块常量 `PURE_MARKET_ENTRY_MAX_AGE = timedelta(minutes=3)`，注释写明：纯市价没有 KOL 报价做锚，
  超过这个时长（典型来源是相邻消息挂起）现价已不是 KOL 说的现价；是常量不是运行时开关。
- 867 行与 1135 行两处调用都加 `allow_reference_entry=True`。
- 在 1121 行市价分支算出 `entry_range = (market_price, market_price)` 之后、1133 行 `geometry = candidate_geometry` 之前插入：

  ```python
  if candidate_geometry.passed and candidate_geometry.reference_entry_required:
      posted_at = _as_utc(raw_message.posted_at)   # 复用模块里已有的 UTC 归一化工具；没有就照邻近代码写一个局部的
      age = (now - posted_at) if posted_at is not None else None
      if entry_execution_type != "market" or age is None or age > PURE_MARKET_ENTRY_MAX_AGE:
          return _record_entry_geometry_rejection(
              session_factory,
              raw_message=raw_message,
              candidate=candidate,
              geometry=stale_market_reference_result(candidate.entry_text),
              processed_at=now,
              message_instruction_item_id=message_instruction_item_id,
              execution_contract_mode=execution_contract_mode,
          )
  ```

  `age` 为负（时钟偏差）按 0 处理。`_record_entry_geometry_rejection` 自带持久告警与合约拒绝投影，不另起通道。
- 1133–1150 行现有逻辑不改：候选校验通过后带 `resolved_entry_prices=entry_range` 重算，
  现价已越过止损或止盈 ⇒ `invalid` ⇒ 拒单 + 告警。这是 2026-09-15 军长 17019 那种形状（多单止损 76000 高于现价 75740）的拦截点。
- 仓位、腿的构造、`slTriggerPx`、市价腿 `clOrdId` 等一律不动。
- `src/telegram_kol_research/system_operator_bot.py` 2244 行几何拒绝通知：当 `reason_code == MARKET_REFERENCE_STALE` 时，
  在「原因」行后追加一行 `说明: 纯市价入场超过 3 分钟未执行，已放弃追价`。其余文案不改。

### 9.4 明确不放宽的调用方

`recovery_scan.py:239` 与 `trading_decision.py:78` **不传** `allow_reference_entry`（默认 False），行为不变：
恢复路径按定义就是迟到执行，`trading_decision` 里没有时钟，两处都无法执行 3 分钟时效，所以纯市价入场继续在那里判
`indeterminate`。下单前的三处 `validate_order_draft_price_geometry`（`deepcoin_execution_actions`、`recovery_live_submit`、
`entry_revision_executor`）按订单草稿的腿价格校验，不受影响，仍是最后一道门。

### 9.5 测试

- `tests/test_entry_price_geometry.py`：
  - `is_pure_market_entry_text` 的真假参数表（9.2 列出的全部样例）。
  - 默认参数下纯市价仍 `indeterminate`（`市价进场` 现有回归用例保持不动）。
  - `allow_reference_entry=True` 无参考价：long SL 76000 无 TP ⇒ valid 且 `reference_entry_required`；
    long SL 76000 TP 75000 ⇒ invalid/TP side；SL 缺失 ⇒ indeterminate/required_value_missing(stop_loss)；
    SL `5%` ⇒ indeterminate/ambiguous(stop_loss)；TP == SL ⇒ equal_boundary。
  - `allow_reference_entry=True` 带参考价：long SL 76000 参考 76500 TP 78000 ⇒ valid、`normalized_entry_prices == ("76500","76500")` 或去重后的等价形式（以实现为准，写进状态文档）；
    long SL 76000 参考 75740 ⇒ invalid/stop_side（17019 形状）；short 镜像各一条。
  - `allow_reference_entry=True` 对非纯市价文本（`市价进场/2415`、`77300`、`现价/挂单67000`）结果与默认参数逐项相同。
- `tests/test_auto_trade_execution.py`（复用上一轮加的几何集成骨架）：
  - 纯市价 + 止损、消息 1 分钟前 ⇒ 不产生 `entry_price_geometry_rejected`，走到市价下单（按该文件现有市价用例的断言方式）。
  - 同上但消息 4 分钟前 ⇒ 一条 `entry_price_geometry_rejected`，`reason == MARKET_REFERENCE_STALE`，无交易所写入。
  - 同上 1 分钟前但现价低于多单止损 ⇒ `entry_price_geometry_stop_side_invalid`，无交易所写入。
- `tests/test_recovery_scan.py` / `tests/test_trading_decision.py`：纯市价候选仍产生几何告警 / `manual_review`（钉住 9.4）。
- `tests/test_system_operator_bot.py`：新原因码的通知含「说明」行；其他原因码文案逐字不变。

### 9.6 提交与范围

- 两个代码 commit：`fix(geometry): admit a price-less market entry that carries a stop loss`（9.2 + 其测试）、
  `fix(entry): submit a fresh price-less market entry against the live price`（9.3 + 9.4/9.5 其余测试）；
  状态文档 `docs/entry-admission-and-market-entry-geometry-status.md` 追加「D」一节，可并入第二个 commit 或单独 `docs(entry):`。
- 最终候选跑一次全套 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run pytest -q`。
- 不推送、不部署、不发 Telegram。验证等级 L2（改变交易所写入准入语义），部署与观察窗由指挥会话负责；回退点 `7a8e67c1`。
