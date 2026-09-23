# 入场确认消息：重复开仓、仓位语义丢失与生命周期完整性——分析与修复设计

日期：2026-09-23　状态：**已批准（2026-09-23），阶段 1+2 实施中**　线上版本：`77df50d4`（本设计的回退点）

本稿**取代** `docs/plans/2026-09-19-entry-confirm-duplicate-position-design.md`（未实施、未提交 git）。
09-19 拍板的决策 A 与 B2 原样纳入本稿阶段 1 与阶段 4；新增的阶段 2、3 来自 09-23 样本暴露的两件
09-19 稿没有覆盖的事：**仓位语义（半仓）被整条链路丢弃**，以及**生命周期被标成已入场却没有任何持仓**。

## 1. 现象

2026-09-23 陈哥群一笔交易开出 **34 张** BTC 空单（另有 5 张挂单未成交），策略本意是 **5 张左右**。
用户已于当日手动平仓并撤单。

## 2. 事实链（UTC；线上库只读查询）

| 时间 | 消息 | 系统动作 | 结果 |
|---|---|---|---|
| 09-22 01:17:30 | 10672「BTC 85700-86000 做空，止损 87200，止盈 84000-82000」 | 识别为策略，生命周期 **1271**；被相邻消息 10673「正常仓位操作」阻塞（attempt 29） | 01:18:06 attempt 置 `woken`，**此后从未下单**；指令项 1282 于 07:18 以 `target_strategy_binding_visibility_retry_expired` 失败。1271 停在 `pending_entry`，无绑定 |
| 09-23 06:03:40 | 10696「比特币市价86500附近，半仓入场做个短线空单」 | MiMo 判**非策略**：「可唯一对应已有BTC空单pending_entry策略1271」→ `entry_confirm` 分支 | 1271 置 `entered`；同时生成候选 2551（`entry_signal`，止损止盈**抄自 1271**：87200 / 84000-82000）→ 06:04:02 **市价开空 29 张 @86511.86**（绑定 372，无生命周期指向它） |
| 09-23 06:04:45 | 10697「BTC 86500-86700 做空，止损 88300，止盈 84200-82700」 | 识别为**新策略**，生命周期 1289 | 06:05:05 贪婪腿市价 5 张 @86480.3；06:05:14 保守腿限价 86610 挂 5 张，未成交 |

三处偏差，合成一个结果：

1. 10672 本该入场的那一笔**从未下单**；
2. 10696 这条只有仓位提示的消息**自己开了一笔** 29 张，用的是 10672 的止损止盈；
3. 「半仓」两个字在整条链路上**没有任何一处被读到**：29 张与 5+5 张都是按满仓风险预算算的（`risk_multiplier=1`）。

对用户而言最坏的后果不是多开，而是 **29 张成了孤儿**：绑定 372 不被任何生命周期指向，陈哥后续发平仓
消息时只会平到 1289/373 的 5 张。这与 2026-09-19 那次（绑定 358 的 13 张）是同一种结局。

## 3. 根因

### 3.1 入场确认被物化成一条无法与新策略区分的候选

[`message_recognition.py:1344`](../../src/telegram_kol_research/message_recognition.py) 的 `entry_confirm`
分支调用 [`_upsert_entry_confirmation_candidate`](../../src/telegram_kol_research/message_recognition.py)
（`:4533`），写出的候选是：

```python
"event_type": "entry_signal",
"target_lifecycle_id": None,
"stop_loss_text": _format_number(lifecycle.stop_loss),   # ← 抄目标生命周期
"take_profit_text": lifecycle.take_profit,
```

执行端本来有一道防护（[`auto_trade_execution.py:925`](../../src/telegram_kol_research/auto_trade_execution.py)）：

```python
if candidate.parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}:
    return _record_entry_auto_trade_skip(..., reason="lifecycle_event_not_new_entry", ...)
```

权威路径传入的 `parse_source` 是 `"mimo_authoritative"`，不在集合里，防护被绕过。**这与 09-19 的根因
逐字相同**，因为那次的修复始终没有实施。

这条候选还继承了目标生命周期的止损，于是一条自身没有任何价格的消息，凭继承来的 87200 通过了
「纯市价入场必须有止损」的闸门（2026-09-17 决策 D）。

### 3.2 仓位语义只有一条通路，而这条消息走不到

系统本来有专门机制：`entry_preambles` —— 不可执行的前置仓位指令，带 `risk_multiplier`，由
[`entry_assembly_admission.py:243`](../../src/telegram_kol_research/entry_assembly_admission.py) 在
**同群、策略消息前后 30 分钟、每侧最多 20 条**的窗口里作为 `risk_multiplier` 片段被下一条策略消费。
陈哥群 8 月的「半仓入场」都正确进了这张表（10084 / 10126 / 10185 / 10201，倍率 0.5）。

**这一节最初的诊断是错的，2026-09-23 审阅阶段 1+2 实现时用线上证据纠正如下。**

原判断是「MiMo 一旦能对应上已有策略就不再输出 `entry_context`」。线上负载证明相反——10696 的
`normalized_evidence_json` 里三样东西都在：

```json
"entry_context": {"kind":"entry_preamble","risk_multiplier":"0.5","side":"short","symbol":"BTC", ...},
"entry_fragments": [{"kind":"risk_multiplier","risk_multiplier":"0.5", ...}],
"evidence.text.fields.position_size": {"value":"半仓","confidence":1.0,"source":"text"}
```

**模型把半仓认出来了，是我们自己拒收的。** 写入侧
[`entry_preambles.py:140`](../../src/telegram_kol_research/entry_preambles.py) 的
`persist_authoritative_entry_preamble` 要求：

```python
payload.get("recognition_result") == "非策略"
and not _has_meaningful_value(strategy)
and str(lifecycle.get("event_type") or "") == "none"     # ← 10696 是 "entry_confirm"
```

只要 MiMo 同时判出**任何**生命周期事件，`entry_context` 就被丢掉。
`entry_strategy_fragments.py:82` 的 `lifecycle_is_entry_context` 是逐字相同的守卫，后果更彻底：
生产库 `entry_strategy_fragments` 表**至今 0 行**，v2 的片段通道（不止 `risk_multiplier`，
还有 `leg_allocation` 与补充价格）从上线起就没写过一行。`entry_preambles` 有 18 行，最后一条停在 2026-09-16。

这不改变阶段 2 的做法——2.1/2.2 在 `entry_confirm` 分支里自己写 preamble，绕开这道守卫，
拿到的倍率与 MiMo 给的 0.5 一致，且与消费侧
`has_entry_context and not current_preamble_persisted` 的时序检查正好对上。
**放宽守卫是另一个专题**：它会同时打开那条从未在生产跑过的 fragment 通道，而 `leg_allocation`
直接改变下单腿的分配，需要自己的 shadow 观察窗，不该搭在本稿里。见第 7 节。

### 3.3 即使有了 preamble，也传不到下一条策略

[`adjacent_entry_assembly.py:12`](../../src/telegram_kol_research/adjacent_entry_assembly.py)：

```python
HARD_BOUNDARY_KINDS = frozenset({"complete_entry", "cancel_entry", "opposite_entry", ...})
```

而 [`entry_assembly_admission.py:368`](../../src/telegram_kol_research/entry_assembly_admission.py) 把
**任何** `event_type == "entry_signal"` 的相邻候选归为 `complete_entry`（同币同向时）。确认消息的候选
正是 `entry_signal`，于是它在 10697 眼里是一堵硬边界，边界处及更早的全部片段被截断
（`fact.source_key > nearest_before_key`，边界自身也被排除）。

结论：**仅仅给确认候选加个跳过标记还不够**。只要它仍以 `entry_signal` 的身份出现在相邻扫描里，
挂在同一条消息上的 preamble 就会被它自己挡掉。阶段 1 与阶段 2 必须一起设计。

### 3.4 生命周期被标成 entered 却没有持仓

`entry_confirm` 分支无条件执行 `target.lifecycle_status = "entered"`。1271 从未下过单，于是库里留下一条
`entered`、`execution_binding_id IS NULL`、`entry_price_actual=86500` 的生命周期。它会被后续管理消息
命中却找不到持仓（2026-09-19 陈哥批次 167 的 `management_reconciliation_identity_mismatch` 即此形状），
而真正的分歧——**KOL 认为这单已经进场了，我们根本没进**——没有任何地方报出来。

### 3.5 起点：10672 从未下单（缺陷 2，本稿范围外）

attempt 29 在 12 秒内被置 `woken` 却没有执行，6 小时后指令项以
`target_strategy_binding_visibility_retry_expired` 失败。这是 09-19 记录的缺陷 2
（`reconcile_due_entry_admissions` 在 `apply_authoritative_assessment` 末尾先把 attempt 置 `woken`
只清 visibility 不执行，随后 `_run_entry_assembly_wakeups` 要求 `status=="pending"` 于是跳过）的
**第三次实盘命中**（前两次：峰哥 9343、raw 16913，均 09-15/09-17）。

它是本次事故的起点：10672 若正常入场，10696 就是一句普通的仓位补充，不会有任何一张多余的合约。
**本稿不修它**（属于另一块代码，且需要独立的并发设计），但它应当是下一个专题，优先级高于本稿阶段 4。

## 4. 修复设计

四个阶段，阶段之间由 Claude 审阅后再派下一阶段。阶段 1 与 2 必须**一起部署**（理由见 3.3）。

### 阶段 1：入场确认不再开仓（09-19 决策 A）

**1.1 标记。** `_upsert_entry_confirmation_candidate` 的 `desired` 增加 `"management_action": "entry_confirm"`。
该列已存在，入场候选此前恒为 `None`，不需要迁移；`event_type` 仍为 `entry_signal`，`target_lifecycle_id`
仍为 `None`。实施前必须实证：全仓库读取 `SignalCandidate.management_action` 且可能遇到 `entry_signal`
候选的位置，逐个确认不会把 `"entry_confirm"` 当成管理动作。

**1.2 统一判定，四处共用。** 新增（建议放在 `duplicate_entry_confirmation.py` 之外的独立小模块，
避免与 A-16b 的语义混同，例如 `entry_confirmation_candidates.py`）：

```python
def is_entry_confirmation_candidate(candidate) -> bool:
    return candidate.event_type == "entry_signal" and (
        candidate.parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}
        or candidate.management_action == "entry_confirm"
    )
```

替换 `auto_trade_execution.py:925`（命中 → `lifecycle_event_not_new_entry` 跳过，零交易所写入）、
`auto_trade_execution.py:2465`、`strategy_alerts.py:892` 与 `:985` 三处对 `parse_source` 集合的直接判断。
指令项照常生成并以「已核实跳过」终态收尾，保留审计与通知。

**1.3 例外：市价 + 自带止损 = 策略。** 在 `message_recognition.py:1334` 的 `entry_confirm` 分支之前判定：

- 市价字眼：复用决策 D 已落地的市价标签判定（`_infer_entry_execution_type` 的词表与「入场字段有市价标签」
  逻辑取同一来源，不另立词表）。
- 自带止损：取权威负载里**本条消息**读出的止损——`lifecycle_event.stop_loss` 或
  `evidence.text.fields.stop_loss`，必须能解析为单个绝对价格。**不得回退到 `lifecycle.stop_loss`。**

两者同时成立 → 不走确认分支，按一条新的纯市价策略处理：以**本消息**为键生成候选与生命周期，下单走 D 的
路径（实时价方向校验、3 分钟时效）。绑定与生命周期按 `(chat_id, message_id, symbol, side)` 自然关联，
后续平仓消息管得到它。目标的旧 pending 生命周期不动。

任一条件不成立 → 1.1/1.2，不开仓。**10696 属于后者**：有「市价」，止损是继承的。

**1.4 存量。** 历史候选不回填。

### 阶段 2：仓位语义落地（新增）

**2.1 确定性仓位词提取。** 新模块 `entry_position_sizing_terms.py`，纯函数，对**消息自身文本**取倍率：

| 文本 | 倍率 |
|---|---|
| 半仓 | 0.5 |
| 轻仓 | 0.5（与历史 preamble 一致） |
| 一成…九成（仓/仓位） | 0.1…0.9 |
| N%（仓位/仓）、N 在 1–99 | N/100 |
| 其它（含「正常仓位」「重仓」「满仓」，以及无仓位词） | 不产出 |

只产出 `0 < m < 1`；等于 1 与不产出等价，不写 preamble，避免多一条无效记录和一个边界分支。
一条消息出现多个互相冲突的仓位词 → 不产出（宁可按满仓走既有路径，也不猜）。
不依赖 MiMo 改口，与 09-19「规则落在确定性代码里」一致。

**2.2 确认消息产出 preamble。** 在 `entry_confirm` 分支内、与生命周期更新**同一事务**：2.1 拿到倍率时，
构造 `EntryPreambleEvidence(symbol=lifecycle.symbol, side=lifecycle.side, risk_multiplier=m,
confidence=<本次识别置信度>, reason="<本条消息的仓位词，注明来自确认消息>")`，调用现成的
`persist_entry_preamble_in_session`（`entry_preambles.py:52`），`evidence_version_id` 取本消息当前证据版本，
`recognition_generation` 取权威代次。指纹、同消息旧 pending 的作废、`consumed`/`expired` 流转全部复用现有逻辑。

作用域不变：同群、±30 分钟、同币同向 —— 10696 与 10697 相隔 65 秒，正好落在窗口内。

**2.3 确认候选不再是硬边界。** `entry_assembly_admission.py:366` 附近，候选转 fact 的分支改为：
`is_entry_confirmation_candidate(other_candidate)` 为真时，`kind = "entry_confirm"`，既不进
`HARD_BOUNDARY_KINDS`，也不是 fragment，也不是 unresolved。其余候选行为不变。

没有这一条，2.2 写出的 preamble 会被同一条消息的确认候选挡在边界外，整个阶段 2 无效。

**2.4 预期结果（以本次样本回放）。** 10696 不开仓、产出 `risk_multiplier=0.5` 的 pending preamble；
10697 的组装消费它，`applied_risk_multiplier=0.5`，开出约 2+3 张而不是 5+5。全案从 34 张变成 5 张。

### 阶段 3：生命周期完整性（新增）

**3.1 没有持仓就不要说 entered。** `entry_confirm` 分支里的状态跃迁加前置条件：目标生命周期存在
**live 执行绑定**（`execution_bindings.status == "active"` 且有 `attribution_status == "verified"` 的入场腿）
时才置 `entered`；否则保持 `pending_entry`，只记录确认事实（`entry_signal_message_id`、确认时间），
不写 `entered_at` / `entry_price_actual`。

**3.2 脱节告警。** 上述「KOL 确认入场、我们没有持仓」的情形，经现有 runtime incident 通道告警一次
（同一生命周期不重复），含群、币种、方向、目标生命周期 id、原策略消息 id、确认消息 id。
这是唯一能让人当场发现「该进的没进」的信号——本次事故里它本可以在 06:03 就响，比事后追查早 11 小时。

**3.3 同向重复入场护栏（收紧 A-16b）。** 现有 `find_duplicate_entry_binding`
（`duplicate_entry_confirmation.py:78`）要求**入场价数值相等**，因此 09-18（81000 vs 80910/81210）与本次
（86500 vs 86480/86610）都没拦住。收紧为：同群、同币、同向、2 小时窗口内已有 live 入场腿 → 即便价格不同也
`park_duplicate_entry`，`result_json` 记下两边的价格。

必须如实说明这条的实际语义：挂起状态 `awaiting_user_confirmation` 的三条出路里，`/choose` 与 `/dismiss`
目前**没有可用的接收入口**（已知缺口 G2），所以实际结局是 2 小时后 `confirmation_timeout` → 不开仓 + 通知。
方向是安全的（宁可不开，不可重复开），代价是 KOL 真正的同向加仓也会被挡下。
**用户 2026-09-23 拍板：不采纳。** 阶段 1+2+3.1+3.2 落地后本次的三张多余仓位一张都不会出现，
而 3.3 会挡掉 KOL 真正的同向加仓，且挂起后没有人工放行入口，等于静默丢单。阶段 3 只含 3.1 与 3.2；
阶段 1–3 上线后观察一两周，若仍有漏网再重开此条。

### 阶段 4：全平顺带平孤儿（09-19 决策 B2）

**4.1 孤儿定义（全部满足）：** `execution_bindings.status == "active"`；有 `attribution_status == "verified"`
且带 `pos_id` 的入场腿；没有任何 `strategy_lifecycles.execution_binding_id` 指向它；live 模式。

**4.2 全平扇出。** 管理计划器为生命周期 L 规划 `full_exit` 且为 live 时，查找同 `chat_id`、同 `symbol`、
同 `side`、且绑定 `message_id` 大于 L 的 `message_id` 的孤儿绑定，为每个生成同样是 `full_exit` 的兄弟批次，
`target_lifecycle_id = L`，原因码 `orphan_sibling_full_exit`，沿用现有的 `may_close_exact_position`、
幂等指纹与对账确认。只在 `full_exit` 触发；部分止盈、移动止损、撤单一律不扇出；只减仓，永不开仓或改保护单。
平仓通知单列「顺带平掉的未关联持仓：N 张，posId …」。

**4.3 孤儿告警。** 挂在现有对账轮，只读库：孤儿持续超过 10 分钟 → runtime incident 告警一次（同一绑定不重复）。

阶段 1–3 到位后孤儿不再新增，阶段 4 是对存量与漏网的兜底，优先级低于 3.5 所述的缺陷 2 专题。

## 5. 测试要求（先写测试）

阶段 1：

1. 权威路径的 `entry_confirm`（无自带止损）生成的候选带 `management_action == "entry_confirm"`。
2. `parse_source="mimo_authoritative"` + `management_action="entry_confirm"` + **入场价非空** →
   `lifecycle_event_not_new_entry`，假交易所客户端零写入。
3. 形状回放（本次 10672/10696/10697，以及 09-18 的 10595/10596）：断言**恰好一个绑定**，
   `execution_events` 里没有以确认消息为键的下单事件。
4. 「现价入场」无止损（Nick 1509 形状）→ 不开仓；「现价开个多…止损 0.152」（SUSHI 形状）→ 以本消息为键
   开仓、止损取消息自带值；构造「消息无止损、目标生命周期有止损」→ 不开仓。
5. 真正的新策略（`management_action is None`）行为不变，现有全套用例通过。

阶段 2：

6. 词表单测：半仓/轻仓/三成仓/20%仓位/正常仓位/重仓/无仓位词/冲突词，逐条断言倍率或不产出。
7. 确认消息产出 pending preamble，symbol/side 取自目标生命周期，倍率 0.5。
8. **端到端**：10696 → 10697 相隔 65 秒，10697 的 `entry_preamble_assembly.applied_risk_multiplier == "0.5"`，
   下单张数为满仓的一半（本样本：5 → 2+3）。这一条同时验证 2.3；把 2.3 改回 `complete_entry` 必须让它失败。
9. 边界仍然有效：真正的相邻完整入场（非确认）依旧构成硬边界。

阶段 3：

10. 目标生命周期无绑定 → 确认后仍 `pending_entry`，无 `entered_at`，告警发出一次，再跑一轮不重复。
11. 目标生命周期有 live 绑定 → 照旧置 `entered`，无告警。
12. （若 3.3 采纳）同群同币同向、价格不同、2 小时内 → 挂起；异群/异币/反向/超窗 → 放行。

阶段 4：

13. 全平 + 孤儿 → 两个批次都提交平仓，原因码正确，通知含孤儿条目。
14. 不扇出：部分止盈；异群；异币；反向；已有生命周期指向；绑定 `message_id` 小于 L；非 live。
15. 孤儿告警：有生命周期指向不告警；超 10 分钟告警一次；不重复。

验收标准是 `execution_events` 里的下单/平仓记录，不是候选或 attempt 的状态字段。

## 6. 部署与验证

- 阶段 1+2 合并为一次 `tg-deploy`（回退点 `77df50d4`）；阶段 3、阶段 4 各一次，回退点为上一次的部署提交。
- 每次部署后 L2 观察窗 30 分钟，自然样本，上限 24 小时。
- 阶段 1+2 的首个样本：陈哥群几乎每笔策略前后都跟一句仓位或「区间均可入场」，预计一两天内出现。
  核对三件事：确认消息的指令项为 skipped / `lifecycle_event_not_new_entry`；同一策略只有一个绑定；
  若消息含仓位词，下一条策略的 `applied_risk_multiplier` 等于该倍率。
- 阶段 4 的扇出没有自然样本可等，以测试与双向核对为准。

## 7. 不在本次范围

- **缺陷 2（入场被抢走唤醒、从未下单）**：见 3.5。已三次命中，是本次事故的起点，建议作为下一个专题，
  优先级高于本稿阶段 4。
- 「KOL 重发同一笔交易」算新策略还是修订：本次 1271 从未下单，所以修掉阶段 1–3 后不会再多开；
  但若原策略**已经入场**、KOL 重发一张调价后的策略卡，仍会开出第二笔。3.3 是针对它的确定性护栏，
  真正的语义判定（修订 vs 新策略）需要独立专题。
- 管理批次的 `management_reconciliation_identity_mismatch`：根因修复后同向重复仓位不再出现，先观察。
- **手动平仓后 `position_backup_stop_orders` 不收口**（见 8 节实证）：一行代码量级的缺口——
  `retire_protection_for_closed_binding` 里把该绑定仍为 `active`/`pending` 的备份止损行一并置终态。
  不放进本稿是因为它与入场语义无关，且没有交易所写入风险；建议随手捎在下一个触碰该模块的专题里。
  交易所侧已确认无残留：用户 2026-09-23 核对，两张触发单都不在了——**绑定持仓的止损单在该持仓平仓离场时
  由交易所自动撤销**。所以这个缺口纯粹是库内记录问题，不会留下裸触发单。
- **放宽 `event_type == "none"` 守卫**（见 3.2 的更正）：让带 `lifecycle_event` 的消息也能落下
  `entry_context` / `entry_fragments`。价值是让 MiMo 已经算对的东西不再被丢，代价是第一次真正打开
  v2 的 fragment 通道（`leg_allocation` 会改变下单腿分配）。独立专题，先 shadow。
  顺带记一笔：`evidence.text.fields.position_size` 是结构化的「半仓」，比 2.1 的正则更可靠，
  那个专题里应当把它排在词表之前作为首选来源。
- 提示词不改。

## 8. 存量数据与手动干预的留痕（实证）

用户于 2026-09-23 07:15 手动平掉两笔持仓并撤掉那张限价挂单。这一段留痕在此记录，供日后回放这批行时
**不必重新调查**：它们是人为操作的结果，不是系统缺陷的证据。

私有 WS 完整捕捉到了：07:15:21–07:15:24 共收到 `Order` / `Trade` / `Position` / `TriggerOrder` 四类帧，
全部入库且 `processed_state='processed'`（`deepcoin_ws_events` 697–711）。但 WS 在本系统里没有判定权——
按 `deepcoin_reconcile_wake.py` 的设计，一帧的全部权限是「现在去看一眼」，核实归 REST（硬规则 5）。
REST 对账随即收口，全程不到一分钟：

| 时间 | 记录 |
|---|---|
| 07:16:13 | 腿 638、639 → `manually_closed` / `manual_position_missing`；事件 `position_marked_manually_closed`（依据 `position_history_full_close`）；生命周期 1289 → `exited` / `manual` |
| 07:16:20 | 绑定 373 → `closed` / `manual_closed_or_not_found_on_exchange` |
| 07:16:40 | 被撤的限价腿 640 → `manually_closed` / `manual_lifecycle_terminal` |
| 07:21:40 | 绑定 372 → `closed` / `entry_legs_terminal` |
| 同上 | `position_protection_ledger` 766–773 八行全部 `retired`，`closed_by='manual_close_sweep'` |

三处需要知道的遗留：

- **备份止损行没有收口**：`position_backup_stop_orders` 156（触发价 87374.4，单号 `1001125386363802`）与
  157（88476.6，`1001125386363848`）至今仍是 `active`。`retire_protection_for_closed_binding`
  （`protection_retirement.py:76`）只退休 `position_protection_ledger` 与 `position_protection_legs`，
  不碰这张表，手动平仓扫描也没有补这一刀。**不会引发交易所写入**——`backup_stop_repair.py:96` 只扫
  `ExecutionBinding.status in ("open","active")` 且腿 `active` + `verified` 的行，这两个绑定都已 `closed`。
  交易所侧无残留：用户当日核对，两张触发单都已不在——绑定持仓的止损单在持仓平仓离场时由交易所自动撤销。
  所以它只是一条会误导读者的僵尸行。见 7 节。
- **生命周期 1271 停在假 `entered`**（无绑定、无 `exited_at`），它不会被任何收口路径碰到。阶段 3 只改变
  今后的行为，不回填历史；实施者确认它不阻塞别的生命周期即可。
- **腿 640 现在显示 `manually_closed`**，这掩盖了一个事实：入场限价腿本来**没有任何超时或过期机制**，
  不撤就会一直挂着，只有 `cancel_pending_entry` 与 `entry_revision_executor` 会动它。
  日后分析这条腿时不要从终态倒推出「系统会自己收挂单」。
