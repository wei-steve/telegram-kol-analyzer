# `entry_fragments` 为什么从未上线，以及它现在该怎么办（2026-09-25 调查）

只读排查，**没有改动任何代码、提示词或生产数据**。生产库的分析全部在
`VACUUM INTO` 出来的快照上做，用完已删除（`/tmp/ef-probe-20260925.db`，1.24 GB，
`PRAGMA quick_check` = ok，磁盘已回到 7.3 G 可用）。

## 0. 先更正一条前提

发起这次调查时说「当前激活版本 `ai_prompt_versions.id = 8`」。**已经不是了。**
2026-09-25 03:26 v9 发布成功，现在的激活版本是 `id = 10`（version_number 5，6704 字符），
`id = 8` 已转 `superseded`。这不改变任何结论：v9 = v8 原文 + `message_classes`，
同样不含 `entry_fragments`。

## 1. 时间线：代码比提示词早了三天，然后再也没有人回头

| 日期 | 事件 |
|---|---|
| 2026-08-05 20:30 | **v8 发布**，change_note `Add reviewed entry preamble risk-multiplier contract for all`。这一版第一次引入 `entry_context` / `risk_multiplier` / 「半仓」。 |
| 2026-08-06 | `entry_preambles.py` 等 `entry_context` 消费链落地（`1ab83898` → `20476479`）。**提示词先发、代码后到**，顺序是对的。 |
| 2026-08-08 | `entry_fragments` 整条链一天之内建完：`f945962a` 建表 → `20aeb69a` 归一化 → `e8d28aa6` 持久化 → `b5b4bd9d` 双向选取 → `d168fdfd` 准入屏障 → … → `c1770eb8` 运维手册。**没有任何一步发布提示词版本。** |
| 2026-08-05 之后 | `trading.analysis.shared` 再也没有发布过新版本，直到 2026-09-25 的 v9。 |

### 设计文档在哪

不在 `docs/plans/`。原始两份已在 2026-09-05 的 `4f9ca4c5 docs: archive 385 unreferenced plan files`
被归档，现址：

- `docs/archive/plans/2026-08-08-adjacent-entry-message-assembly-design.md`（311 行，设计稿）
- `docs/archive/plans/2026-08-08-adjacent-entry-message-assembly.md`（919 行，12 个 Task 的实施计划）

它们不是「被取代的设计」——没有任何后续设计取代过它们，只是因为没人再引用而被批量归档。
这本身就是线索的一部分：一个**从未完成**的功能，它的设计稿被当成完成品归了档。

### 根因：改了种子，以为就是改了提示词

实施计划 Task 2 Step 3 的原话是：

> Extend the prompt schema with an `entry_fragments` array. Preserve `entry_context`
> during rollout for backward compatibility, but adapt it into a single risk fragment
> when the new array is absent.

执行者照做了——改的是 `src/telegram_kol_research/prompt_defaults.py` 里的
`DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT`。**但这个常量在生产上是死的。**
`prompt_registry.seed_prompt_definition()` 第一件事就是：

```python
existing = session.query(AiPromptDefinition).filter(...).one_or_none()
if existing is not None:
    return _load_detail(session, existing)      # ← 直接返回，正文一个字都不写
```

生产库的 `ai_prompt_definitions.id = 1` 自 2026-07-13 08:58 起一直存在。
**自那以后，任何对代码种子的修改在生产上都是零效果**，只对全新数据库（测试、本地开发）生效。

对照组能说明问题：两天前的 entry-preamble 专题
（`docs/archive/plans/2026-08-06-all-configured-groups-entry-preamble-live.md`）
有一个 **Task 7: Push, deploy dormant, publish prompt, and activate all groups live**，
显式写了「发布已审阅的识别提示词」，于是有了 v8。
`entry_fragments` 专题的 Task 12（最后一个 Task，12 项交付、9 步灰度）里，
**没有任何一步提到提示词版本**——9 步全是 `entry_message_assembly_v2_mode` /
`entry_revision_v2_mode` 的 shadow/live 开关。模型输出契约这一环，静悄悄地掉了。

### 有没有过能用的提示词版本？没有，一次也没有

把 `ai_prompt_definitions.id = 1` 的全部五个版本正文取出来逐个查标记：

| 标记 | v1 | v6 | v7 | v8 | v10（现行） |
|---|---|---|---|---|---|
| `entry_fragments` | 0 | 0 | 0 | 0 | **0** |
| `leg_allocation` | 0 | 0 | 0 | 0 | 0 |
| `supplemental_entry` | 0 | 0 | 0 | 0 | 0 |
| 「补仓」/「各半仓」/「全仓」/「正常仓位」 | 0 | 0 | 0 | 0 | 0 |
| `entry_context` | 0 | 0 | 0 | 4 | 5 |
| `risk_multiplier` | 0 | 0 | 0 | 2 | 2 |

**生产从来没有一个提示词版本要求过模型输出 `entry_fragments`。** 不是「曾经有、后来退回去了」，
是从头到尾没有过。

## 2. 缺失实际影响了什么

### 2.1 先说结论：三种片段里，只有一种真的丢了东西

`entry_fragments` 契约定义了三个 `kind`。它们的生产命运完全不同：

| kind | 提示词有没有要求 | 生产实际发生了什么 | 真实损失 |
|---|---|---|---|
| `risk_multiplier`（半仓 / 百分比） | 没有，但 **`entry_context` 覆盖了同一语义** | 正常工作，20 条 `entry_preambles`、124 条 `entry_strategy_assemblies`（其中 4 条 0.5 倍率）都在跑 | **无。这一路是冗余的。** |
| `leg_allocation`（两个点位各半仓） | 没有 | 要么整条丢弃，要么被**误读成整单 0.5**——与设计稿明令禁止的「不得把整单再次减半」正好相反 | 有，但样本稀少（~7 条 / 4 个月，全部来自陈哥） |
| `supplemental_entry`（补仓：63400 附近） | 没有 | 跨消息的补仓价被丢弃；但**同一条消息内的补仓早已由 `strategy.entry` 承载** | 有，但样本更少（~5 条 / 3.5 个月），且是整条链里执行风险最高的一段 |

### 2.2 `risk_multiplier` 为什么没丢：两条兜底路径，一条真的在跑

**路径 A（存在但没接上）**：`message_evidence.normalize_mimo_evidence` 在
`entry_context` 存在、`entry_fragments` 缺席时，会**合成**一条 `kind=risk_multiplier` 的片段
写进 `normalized_evidence_json`。生产里 `message_evidence_versions` 共 10 767 行，
**22 行**带 `entry_fragments`——全是这样合成出来的。

但这条路**没有接到持久化上**：`authoritative_recognition.py:1391` 调用
`persist_authoritative_entry_fragments(payload=mimo.payload, ...)`，传的是**模型原始 payload**，
不是 normalized evidence；而 `entry_strategy_fragments.py:88` 读的正是 `payload.get("entry_fragments")`。
所以——

> **`entry_strategy_fragments` 表在生产上有 0 行。** 从建表至今一行没进过。
> `entry_assembly_fragments` 同样 0 行。

（顺带：`_load_current_mimo_evidence_result`（回放/恢复路径）**会**把合成片段读回 payload。
但既然表是空的，这条路径也从未导致过一次落库。）

**路径 B（真的在跑）**：v2 组装器本身认 `legacy_preamble_ids`。
`entry_strategy_assemblies` 里 4 条半仓组装，证据长这样：

```json
{"allocations":[], "fragment_ids":[], "fragment_generations":[],
 "legacy_preamble_ids":[4], "legacy_preamble_generations":[{...}],
 "effective_risk_budget_usdt":"10.00", "configured_risk_budget_usdt":"20.0"}
```

`fragment_ids` 恒空、`allocations` 恒空、`legacy_preamble_ids` 非空——
**半仓这件事完全由 `entry_context` → `entry_preambles` 走通，`entry_fragments` 一次都没参与。**

所以问题里那句「`entry_context` 是否已经覆盖了大部分场景」——**是的，而且不是「大部分」，
是 `risk_multiplier` 这一路的全部**。

### 2.3 `leg_allocation` 确实丢了，而且有一次是**读反了**

窗口定义取代码本身：同群、策略消息前后 30 分钟、每侧最多 20 条
（`entry_assembly_admission.py:46` `ADJACENT_ENTRY_MAX_AGE`）。
窗口内共 6040 条消息，命中「各半仓 / 两个点位 / 各一半 / 分两次」的 20 条，
人工过目后真正是**入场分腿指令**的约 7 条，全部来自陈哥：

| raw_message_id | 原文（节选） | 生产实际识别结果 |
|---|---|---|
| 3229 | 两个点位可以各挂半仓操作。 | `authoritative_gap_recovery_expired`（根本没识别） |
| 5697 | 限价挂单，两个点位各挂半仓，带好止盈止损。 | 同上 |
| 6338 | 不等了，BTC 挂个限价单两个点位各挂半仓。 | 非策略，`entry_context = null` |
| 6925 | BTC 挂个限价多单，区间入场如果把控不好的可以两个点位各半仓。 | 非策略，`entry_context = null` |
| 7929 | 限价多单，**正常仓位操作**两个点位各半仓操作。 | 非策略，`entry_context = null` |
| 12042 | 限价空单…两个点位各挂半仓… | 非策略（走了 replace_entry），`entry_context = null` |
| **12769** | BTC 短线多单，76400 和 77000 附近**两个点位各半仓**入场。 | **`entry_context.risk_multiplier = "0.5"`** |

7929 是设计稿的教科书样本：「正常仓位操作 + 两个点位各半仓」= 整单 1.0、两腿各 0.5。
生产输出是 `null`，整句话丢了。

12769 更值得记一笔：它**被识别了，但读反了**。
`entry_preambles.id = 9` 记下 `risk_multiplier = 0.5`，含义是「整单只做半仓」，
而 KOL 的意思是「整单正常，分两腿各半」。设计稿第 79 行明写
「不得把整单再次减半」——生产现在做的正是这件被禁止的事。
这一条 `status = pending`（没被任何组装消费），所以**没有造成实盘 2 倍欠仓**，
但这是一个已经落库的、语义反向的活雷。

**要注意的是：修这个缺口不一定需要 `entry_fragments`。**
系统本来就会按 `symbol_entry_thresholds`（BTC first/second limit offset = 90）
把区间拆成两腿。「两个点位各半仓」在语义上**描述的就是我们已经在做的事**；
我们真正需要的只是**别把总预算再砍一半**——那是 `entry_context` 段落里一条规则的事，
不是一个新数组的事。

### 2.4 `supplemental_entry`：主流形态早就被 `strategy.entry` 接住了

窗口内提到「补仓」的 97 条，其中**不是策略本身**的 39 条。逐条读完之后：

- **压倒性多数的「补仓 + 价格」写在完整策略消息内部**，不是相邻的独立消息。
  三马哥的模板就是标准形态：「77400 附近市价直接多 2% 保证金 / 再挂 75388 补仓 3% 保证金 /
  止盈… / 止损…」。智哥、飞扬、舒琴的模板同理（「补仓价格：65650」「补仓：490」「2800 附近补仓」）。
- 这种形态**已经被正确接住**。raw 12794 的生产 payload：

  ```json
  "strategy": {"entry":"市价进场/75388", "order_type":"market+limit",
               "stop_loss":"73000", "take_profit":"78500/79500/80888", ...}
  ```

  补仓腿在 `strategy.entry` 里，不需要 `supplemental_entry`。
  raw 19016（舒琴 ETH「2750 附近建仓，2800 附近补仓」）同样：
  `evidence.text.fields.entry = "2750附近/2800附近"`。
- 真正的**跨消息、带价格、标的在 BTC/ETH/SOL 内**的独立补仓指令，
  3.5 个月里数得过来：259（补仓点 63588）、2077（补仓点 1838）、5863（补仓 64288）、
  16194（补仓挂 2538，且这条本身 `authoritative_gap_recovery_expired`）、
  18178（补仓点位 83188）。**约 5 条。**
- 其余是复盘（「补仓后成本 62900 附近」）、广告、教学、私聊话术，
  以及三马哥大量的「补仓一会出完整策略」——**没有价格，本来就不可执行**。

而打开这一路的代价是最大的：它对应设计稿 Task 7–9 的
「对已提交策略做持久化改单」流水线（`entry_revision_v2_mode` 现在已经是 `live`，
但从来没有一个片段喂给过它）。第一次真正喂进去，就是第一次让它对真实挂单动手。

### 2.5 顺带：这套机器今天的实际成本

`entry_message_assembly_v2_mode` / `entry_preamble_mode` / `entry_revision_v2_mode`
生产值**全部是 `live`**（`trading_settings.id = 1`，updated_at 2026-09-22）。
准入屏障因此在真实地拦截策略：`entry_assembly_attempts` 共 32 行，
**9 条 pending、6 条 expired**、17 条 woken。

6 条 expired 是**被屏障扣住直到过期、从未下单的策略**，
其中 `id = 21`（raw 16979，峰哥 ETH，2026-09-15 14:46）正是
`docs/plans/2026-09-16-adjacent-entry-deadlock-and-market-entry-geometry-analysis.md`
查的那一起。

也就是说：**这套为 `entry_fragments` 建的准入屏障在生产上只产生成本、不产生它本来要等的那种证据。**
它等的相邻证据，永远只可能是一条 `entry_context` 派生的 `risk_multiplier`，
永远不会是 `leg_allocation` 或 `supplemental_entry`。

### 2.6 另外两个顺手发现（不属于本议题，单独记）

1. **`entry_context` 的产出质量很差。** 全历史 `recognition_decisions` 19 030 行里
   8184 行带 `entry_context` 键、**仅 319 行非 null**，其中 258 行 `symbol` 为空（会被归一化拒掉），
   真正落到可交易标的的只有 BTC 31 + ETH 5 = **36 条**，最终落表 20 条。
   落表的 20 条里还混着明显的幻觉：`id=7` 大镖客「大饼盈利接近 4000 点」→ 0.5；
   `id=17` 大镖客「浮盈 500 点」→ 0.5；`id=13` ROSE「#AKE BULLISH」→ 0.5；
   `id=12` 陈哥「**轻仓**入场做空」→ 0.5，而提示词第 82 行明写
   「『轻仓』…不生成 entry_context，不得猜测倍率」。**模型在违反现行提示词。**
2. **测试在断言一个生产上不存在的事实。**
   `tests/test_prompt_composition.py:20` 与 `tests/test_ai_recognition_config.py:48`
   都断言 `'"entry_fragments"' in prompt`——断言的是代码种子。
   这两条断言在过去 48 天里一直是绿的，而被断言的契约在生产上一天都没存在过。

## 3. 结论：退役模型输出契约，不要补进提示词

**建议：把 `entry_fragments` 作为「模型必须输出的字段」这件事退役。**

理由按三个 kind 分别成立，不是一句概括：

1. `risk_multiplier` —— **冗余**。`entry_context` 已经在生产上承担同一语义，
   并且 v2 组装器本来就通过 `legacy_preamble_ids` 消费它。补进提示词只会让同一件事有两个来源，
   多一条分歧路径，不多一分能力。
2. `leg_allocation` —— **缺口是真的，但解法不是这个数组**。
   需要的是在现有【新开仓前置仓位指令】段落里补一条规则：
   「『两个点位各半仓』/『各挂半仓』指的是整单正常仓位分两腿，**不得**输出 `risk_multiplier = 0.5`」。
   一条规则，落在已经在跑的字段上，不需要新契约、不需要打开 v2 的 fragment 通道。
   （这仍然是提示词改动，仍然要单独批准，见下。）
3. `supplemental_entry` —— **证据不足以支撑它的风险**。主流形态已被 `strategy.entry` 接住；
   跨消息形态 3.5 个月约 5 条，而打开它等于第一次让「已提交策略改单」流水线对真实挂单动手。
   收益 5 条、代价是整条链里最危险的一段，不划算。

### 退役具体包含什么（全部是本地代码/文档改动，L0–L1，不碰生产数据库）

- 从 `prompt_defaults.py` 的代码种子里删掉【相邻入场消息片段】整节与 `entry_fragments` 示例块，
  让种子与生产真正在跑的那份对齐。**这在生产上是零效果**（种子只对全新库生效），
  目的是终止「代码看起来要求了、生产其实没要求」这个骗局。
- 删掉 `tests/test_prompt_composition.py:20` 与 `tests/test_ai_recognition_config.py:48`
  两条断言（它们断言的是刚被删掉的种子内容）。
- `prompt_composition.validate_prompt_content` 里的 `entry_fragments` marker
  已于 `122bd712`（2026-09-25）移除，注释里写着「Restore it once the live prompt actually
  carries the field; the separate investigation into why it never shipped owns that decision」——
  **本文就是那个调查，结论是不恢复**，把注释改成结论。

### 什么**不**退役

- `entry_strategy_fragments` 表、`normalize_entry_strategy_fragments`、
  `adjacent_entry_assembly` 的 `leg_allocation` / `supplemental_entry` 分支：
  **留着**。它们是内部表示，代价为零（表恒空），
  而且 `docs/plans/2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md` 第 302 行
  已经排了一个专题要放宽 `event_type == "none"` 守卫、让合成片段真正落表。
  那个专题如果做，用的就是**由 `entry_context` 合成**的片段，
  不需要模型多输出一个数组——正好印证本文结论。
- `persist_authoritative_entry_fragments` 读 `mimo.payload` 而不是 normalized evidence
  这个错接：**这是上面那个专题要修的东西**，不在本文范围内，但应当写进它的输入条件。

### 需要用户单独批准、本次没有做的事

1. **任何提示词改动**（包括 2 里那条 `leg_allocation` 规则）都是生产数据库操作，
   本次一个字都没改，按要求等批准。
2. 12769 那条语义反向的 `entry_preamble`（`status = pending`，`risk_multiplier = 0.5`）
   要不要清理，属于生产数据修复（L3），单独议。
3. 2.6 的两个顺手发现（entry_context 幻觉率、模型违反「轻仓」规则）另立专题。

## 4. 证据出处

| 结论 | 出处 |
|---|---|
| 五个提示词版本均不含 `entry_fragments` | `ai_prompt_versions` 正文逐版本标记扫描（快照） |
| 生产输出全历史 0 条 `entry_fragments` | `recognition_decisions` 19 030 行全量 `LIKE` 扫描（快照） |
| 合成片段 22 条 | `message_evidence_versions` 10 767 行全量扫描（快照） |
| 片段表恒空 | `SELECT COUNT(*) FROM entry_strategy_fragments` = 0（快照） |
| 半仓走 legacy 路径 | `entry_strategy_assemblies` 四条 0.5 组装的 `evidence_json`（快照） |
| 三个 v2 模式均为 live | `trading_settings.id = 1`（快照） |
| 6 条策略被屏障扣到过期 | `entry_assembly_attempts` status 分布（快照） |
| 窗口口径 ±30 分钟 / 每侧 20 条 | `entry_assembly_admission.py:46-47` |
| 种子对已存在定义零效果 | `prompt_registry.py:195-203` |
| 原始设计与计划 | `docs/archive/plans/2026-08-08-adjacent-entry-message-assembly{,-design}.md` |
