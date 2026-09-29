# 首次分析四分类 · 阶段 3（切换）实施方案

- 日期：2026-09-29
- 状态：**已批准**（2026-09-29 用户：§9 八个问题「全部按推荐，开始实施」）。
- 规格：`docs/plans/2026-09-24-first-pass-classification-contract-design.md`（下称「设计稿」）§5、§6、§8 阶段 3、§10
- 输入：`docs/first-pass-classification-status.md` 阶段 3 条目；`docs/plans/2026-09-29-first-pass-phase2-observation.md`（下称「观察文档」）§6、§7、§9、§10、§11；
  `docs/plans/2026-09-28-chen-btc-expired-repost-and-queue-block-design.md`（下称「陈哥稿」）§2.3、§3.3、§5
- 基线：`origin/main` = `a5601082`。生产 HEAD `d244dfeb`，两者之间只有文档差异（offenders 检查 0），下文行号即生产行号。
- 风险级别：**L2**（改变哪些消息进入上下文分析、哪些管理消息能写交易所）。部署须用户单独批准，由调度会话排期。
- 分工：设计与审阅 Opus 5.5；实施交给 Sonnet 5 子代理；每批交付后用生产数据核对子代理写的理由。

---

## 0. 一句话

触发判据从「看关键词」换成「看首轮分类说没说出目标、目标还能不能管」；上下文降级时首轮分类留档；契约类失败只问一次、不排队重试。
另加一道与模型无关的执行层闸门：**没有具体价位、也没有明确动作，或者是条件句 / 意向句的管理消息，不写交易所。**

---

## 1. ① 触发判据换成「删 3 留 4」，外加两条确定性判据

改动集中在 `authoritative_recognition.py` 的 `requires_context_resolution`（:194）与 `CONTEXT_TRIGGER_ORDER` / 三个关键词常量（:133-152）。

### 1.1 新判据集合

| 判据 | 状态 | 条件 |
|---|---|---|
| `revision_language` | **删**（独立触发） | 词表保留为 `apparent_entry_may_be_revision` 的内部局部条件（设计稿 §5 实现约束） |
| `cancellation_language` | **删**，前提是回放 18602 通过（§7 R1）；不通过就保留 | — |
| `entered_holder_language` | **删** | 观察文档 §9.2：+3 纠正、0 回退 |
| `management_without_exact_target` | **改写** | 任一 `策略管理`/`仓位管理` 元素 `resolution = unknown`；**或**（兼容提示词回滚到 v8、`message_classes` 缺失时）旧字段 `event_type != none` 且无 `target_lifecycle_id`。两者取并集 |
| `exact_target_outside_candidates` | **新增**，确定性 | 任一 `exact` 元素的 `lifecycle_id` 不在本次 `generate_strategy_thread_candidates` 产出的候选 `lifecycle_id` 集合内 |
| `exact_target_not_manageable` | **新增**，确定性 | `exact` 目标在候选里，但状态不能被这一类管理：<br>• 任一类：状态终态（不在 `entry_assembly_admission._LIVE_LIFECYCLE_STATUSES`，即 `expired`/`exited`/`cancelled`/`invalidated`）；<br>• `仓位管理` 指向 `pending_entry`（还没仓位，谈不上管仓） |
| `multiple_same_source_candidates` | 保留，**原样** | 含 09-15 方案甲的「首轮可执行」收紧 |
| `reply_target_disagreement` | 保留，原样 | — |
| `text_image_conflict` | 保留，原样 | — |
| `apparent_entry_may_be_revision` | 保留 | `是策略` 且有候选，且（文字含修订词 **或** 与某条 **`pending_entry`** 候选区间重叠）。**2a 的 `status == "pending_entry"` 条件原样保留**，只是把 `"revision_language" in reasons` 换成本判据内部的词表匹配 |

`forthcoming` 元素不触发（没有可解析的目标）。`message_classes` 存在但**有致命违规**时（§3.1），不走分类判据，按旧字段判据兜底，并且这条消息本身已被 fail-closed，不会执行。

### 1.2 两条确定性判据的定义与理由

**「exact 不在候选集合内」**
- 集合 = 本次调用 `generate_strategy_thread_candidates` 的结果，与上下文分析拿到的候选**同一份**，保证触发之后解析器能看到的目标就是判据检查过的集合。
- 这里**不**调用 `parse_message_classes(..., allowed_lifecycle_ids=...)` 把它判成契约违规。理由：19308 一条消息里 `1207` 是对的、`909` 是陈旧的，判违规会连带丢掉 BTC 的第一止盈；触发上下文则两个元素都有机会被纠正。见 §9 问题 4。
- 注意 A-7 收窄：`auto_trade` 群的管理消息只给「交易所 5 分钟内看到过持仓」的候选（`_management_target_gate`，:380）。本阶段把这个收窄的开启条件从「旧字段 `event_type != none`」扩成「旧字段有事件**或**分类里有管理元素」，否则 A1 那类「旧字段降成 none、分类说仓位管理」的消息会拿到未收窄的候选。

**「目标已不可管理」**
- 数据：观察文档 §5，61 个 exact 里 10 个指向消息时刻已终结的目标。其中 1314 ×3、1346 ×2 在 72 小时内、合法地留在候选里，只有这一条判据能拦。
- 触发上下文而不是直接无动作：目标死了不代表消息没意义（19222「剩余的止损全部上移 83300」可能说的是另一笔活仓），交给解析器重定目标；解析器也说不出，就按现有降级无动作。
- 与 3d 的关系：3d 是「上下文**失败**之后、首轮目标已终态且无交易所腿」的无害收尾；本判据是「上下文**之前**」的触发。两者不冲突，3d 原样保留。

### 1.3 改动点

| 文件 | 函数 / 位置 | 改什么 |
|---|---|---|
| `authoritative_recognition.py` | `CONTEXT_TRIGGER_ORDER`、三个常量 | 删 3 条；`REVISION_LANGUAGE` 改名为 `_APPARENT_REVISION_WORDS`（私有，只被下一行用） |
| 同上 | `requires_context_resolution` | 按 §1.1 重写；读 `message_classes` 用 `parse_message_classes`（不传 `allowed_lifecycle_ids`，候选检查由本函数自己做） |
| 同上 | `_management_target_gate` | 开启条件加「分类里有管理元素」 |
| `context_resolution_shadow.py:117` 一带 | 影子判据 | 只核对：它按 trigger 名过滤，删掉的三个名字不再出现，不需要改逻辑；加一条用例钉住 |
| `web_queries.py` / 模板 | 上下文门的 triggers 展示 | 新增两个 trigger 名的中文标签 |

---

## 2. ② 降级不再抹平首次分析

现状：`_resolved_mimo_result`（:736）降级时只改写 `recognition_result`/`reason`/`strategy`/`lifecycle_event`/`confidence` 五个键，`message_classes` 其实**已经留在 payload 里**；但 `_context_resolution.first_pass` 快照只存了四个旧字段，也没有任何字段说明「执行授权被降级了、为什么」。

改动：
1. `first_pass` 快照加 `message_classes`（设计稿 §10 C3 的注明项）。
2. 每个降级分支（`hold`/`unresolved`/低置信度、`revise_thread`、`manage/cancel/exit` 重写）写 `_context_resolution.execution_downgrade = {"from": <首轮类别列表>, "reason": "hold|unresolved|low_confidence|revise_planner|retargeted"}`。只加键，不改已有键。
3. **不**恢复首轮旧字段去执行。降级的本意（目标不确定就别动仓位）保留；变的是下游看得出「这条曾被判为仓位管理、被上下文压下」。
4. 可见性：卡片与「为什么没执行」归因（`recognition_failure_attribution`）读到 `execution_downgrade` 时显示「首轮：仓位管理 → 上下文降级（原因）」；**不新增告警类型**（米娅那类由 ① 从源头消除：18895 首轮 exact、目标活着，新判据下不再触发上下文）。
5. 2b（`override_rejected = terminal_target`）原样保留。设计稿说 ② 吸收 2b，这里的吸收只到「同一个快照里都能看到」，2b 的行为不动。

改动点：`authoritative_recognition._resolved_mimo_result`；`recognition_failure_attribution.classify_unapplied_lifecycle_event`（:157）读新键；`web_queries.py` 投影。

---

## 3. ③ 契约类失败不重问

### 3.1 首轮（设计稿 §6、§10 A4/A5）

**哪些违规是致命的。** 阶段 2 在 813 条里量到 10 条违规，其中至少 9 条是解析器或口径问题（观察文档 §1.4）。先把误报修掉（§4），再把剩下的分两类：

| 类 | 违规码 | 处置 |
|---|---|---|
| **规范化，不致命** | `duplicate_class_target`（去重）、`class_order_violation`（重排为管理在前）、`target_not_allowed` 且 target 全字段 null（视同 null，18970 的形状）、`strategy_not_allowed`（见下） | 记在 `message_classes_violations`，照常往下走 |
| **致命** | 其余全部：非列表 / 空 / 元素非对象 / class 缺失或越界 / target 必填缺失 / resolution 缺失或越界 / exact 缺 id / 非 exact 带 id / 仓位管理用 forthcoming / forthcoming 缺 symbol 或缺定量载体 / 独占类不独占 / 超过 4 个 / `新策略` 缺 strategy 或缺四项 / 纯文字消息判 `图片不可读` | 见下 |

`strategy_not_allowed` 必须是非致命：【两套判据刻意不一致】仍然有效（设计稿 §11 R10），「只有止盈没有止损」的消息会合法地出现「`recognition_result = 是策略` + `message_classes` 不含 `新策略`」。entry_fragments 会话补全那一节以后这类消息会恢复到约每天 0.4 条，把它判致命就等于把这批消息全部 fail-closed，并且破坏观察文档 §11 要做的「无止损的是策略是否恢复」统计。

**致命违规的处置：**
1. `_call_mimo_authoritative_with_retry`（recognition_experiments.py:667）：`_validate_authoritative_payload` 抛出新的 `MessageClassesContractViolation(ValueError)` 时**不做同模型重试**（直接 `break`），`failure_class = RESPONSE_INVALID`（已有）。
2. 是否换下一个模型：沿用现有链路规则（`_should_try_next_model`，请求已发出即换）。见 §9 问题 3。
3. 链上最后一个模型仍致命违规 → 整条 **fail-closed**：不进管理链路、不进执行、**不进消息处理任务的退避重试**。落成终态决策 `skipped / first_pass_contract_violation`，归因表与值守的「有损识别」集合同步登记（与 4c 的 `mimo_authoritative_failed_exhausted` 同样三处），入场准入屏障当终态处理，并在同一事务后跑 `_run_entry_assembly_wakeups`（沿用 38318d17 的 4a 写法）。
4. 模型原样输出与违规码必须留下来供人工核准。实施时先核实 `mimo_recognition_attempts` 一类审计表是否已有响应存档列；**有就用，没有就写进 `error_message`（违规码）+ runtime incident 摘要（字段白名单同步登记）**，不加列（加列是 L3）。
5. `message_classes` 缺失（提示词回滚到 v8）照旧 `present=False`、零违规，不影响识别。

### 3.2 上下文分析（设计稿 §6、§10 C5；陈哥稿 3c）

1. `resolve_contextual_strategy`（context_resolution.py:1056 起）：`terminal = attempt_number == 2` 改为「`network_error` 以外的失败，第一次就 `exhausted`」。`_TARGET_NOT_ALLOWED_CORRECTION` 那次「带纠错提示再问一遍」随之退役（它就是 attempt 内部的第二问）。`network_error` 仍走 `ContextNetworkRetryPolicy`。
2. 跨 attempt 的 `context_fingerprint` 变化后重分析**不动**。
3. **任务级**：现在 exhausted 之后 `assess_message_authoritatively` 把消息落成「识别失败」，消息处理任务按 15/30/60/120 s 退避重试 5 次；每次重试都会命中同一 fingerprint 的 exhausted 行直接抛错，不再调模型，但**同群后续消息照样被堵约 4 分钟**（陈哥稿 §3.1 的队头阻塞）。改为：`error_class ∈ CONTEXT_RESOLUTION_ERROR_CODES` 且非 `network_error` → 先走现有 3d 判断（目标已死且无腿 → `target_terminal_noop`），否则落成终态 `skipped / context_contract_failed`，抛 `TerminalAuthoritativeProcessingFailed` 的同类终态，不排队重试；准入屏障、归因、值守登记同 §3.1 第 3 条。
4. 这类消息里有真实指令（17972「止损改为2600」、18501「全部仓位止盈出局」、18897「止损统一修改83300」），所以 `context_contract_failed` **要告警**（进 `ALERTED_REASONS`），不能静默。

改动点：`context_resolution.py`、`authoritative_recognition.assess_message_authoritatively` 的 `except ContextResolutionError` 分支、`message_processing_worker.py`（终态分支）、`recognition_failure_attribution.py`、`oncall_detector.LOSSY_RECOGNITION_REASONS`、`web_queries` 准入失败集合、`entry_assembly_admission._decision_is_terminal_no_action`。

---

## 4. 解析器与口径修复：F1、F2、F3、F4

| # | 问题 | 改法 | 文件 |
|---|---|---|---|
| **F1** | 多币种 unknown 被判 `duplicate_class_target`（7 条：19309/19310 TST+PUMP、19780 BTC/ETH/SOL/ZEC 等） | 查重键与比对键分开：**查重键**对 `exact` 用 `(class, exact, lifecycle_id)`，对 `unknown`/`forthcoming` 用 `(class, resolution, symbol, side)`；**比对键**（影子比对、`compare_assessments`）维持阶段 1 的「不看 symbol/side」（状态文档阶段 1 自主判断 7）。两个 symbol 都为空的 unknown 仍算重复，但按 §3.1 只去重、不致命 | `message_classification.py`：`MessageClassTarget.identity()` 旁新增 `duplicate_key()`，`parse_message_classes` 改用它 |
| **F2** | `strategy_required` 用最终 payload 校验造成误报（19351、19481：上下文把 strategy 清成 `{}`） | 生产代码里凡要**使用**违规结论的地方（§3.1 致命判断、网页、归因），一律读首轮：识别时点的校验结果，或 `normalized_evidence_json` 里落库的 `message_classes_violations`；**禁止对上下文改写后的 payload 重跑 `parse_message_classes`**。加一条用例：19481 形状（首轮 strategy 完整、最终被清空）不报 `strategy_required` | `web_queries.py`、`recognition_failure_attribution.py` 等读者 |
| **F3** | `derive_message_classes` 不认 `targets[].lifecycle_id`（19741） | `_derive_target` 同时认 `target_lifecycle_id` 与 `lifecycle_id`，与 `authoritative_instructions.py:111` 一致 | `message_classification.py` |
| **F4** | 网页「分类不一致」按最终 payload 比，多标约 80 条 | 推导侧改用首轮旧字段（`_context_resolution.first_pass` 存在时覆盖三个旧字段后再推导），与观察文档口径一致；卡片上另标「上下文改写」 | `web_queries.py:1062` 一带 |

还有一项阶段 2 遗留：`normalized_evidence` 把 `strategy: null` 归一化成 `{}`（状态文档「校验器缺陷修复」一节末）。F2 的读首轮规则 + `_has_any_value` 已经把 `{}` 当空，**不需要另改**；加一条用例钉住回放路径不因此报 `strategy_not_allowed`/`strategy_required`。

---

## 5. 可执行性判据：两层都要

用户裁定：没有具体价位、也没有明确动作的「预告 / 意向」不能触发任何交易所动作；可执行性要看**有没有确定的对象**、**是不是条件句**，不能按「继续持有」几个字判。

### 5.1 识别层（提示词）：意向不是管理元素

**现状核实**（子代理只读追踪，已抽查代码）：
- 旧字段 `exit_position` 单独就足以走到 `full_exit`（`management_directives.py:304`），不要求文字里有明确离场动词；
- 「今晚比特涨到86左右我们多单应该就要准备平仓了」如果被首轮或上下文判成 `exit_position` / `exit_thread`，**今天就会被市价全平**。它现在没出事，只是因为首轮判成了 none、又没有关键词命中；
- 阶段 3 的新判据会让「仓位管理 + unknown」的这类消息**开始**触发上下文（A1 那 19 条里现在只有 7 条走了上下文），上下文一旦给出 `exit_thread`，这条路径就打通了。
- 全执行链路没有任何条件句 / 意向词检查（唯一近似的是 `future_take_profit_level`，只管「到P…止盈」）。

**提示词规则（加进【消息分类】段 §3.2/§3.3 与【生命周期事件】段）：**
- 管理元素必须同时满足：① **确定的对象**（能指出是哪一笔：exact 目标，或明确的币种 + 方向 + 现存仓位 / 挂单）；② **陈述或祈使，不是条件句 / 意向句**（「如果突破可继续持有」「涨到86左右准备平仓」「预计会分批止盈」「只考虑加仓」「做好加一次仓的预期」都是意向）；③ 有**具体价位**或**明确动作**（止盈 X% / 平掉 / 撤单 / 设止损 P / 上移止损到 P / 对确定对象的「继续持有」）。
- 不满足 → 这一部分判 `闲话`（整条只剩它就是 `[闲话]`），旧字段 `lifecycle_event.event_type = none`。
- 给出正反例：18555「浮盈1200点，继续持有中原计划不变」（有确定现存仓位 → 仓位管理）对 18843「如果突破可继续持有」（条件句 → 闲话）；19692「JTO设个止损0.51」（指令）对 19360「VIRTUAL未跌破启动点前，只考虑加仓」（意向）。
- **不加新字段**（见 §9 问题 1 的备选）。契约 marker 不变，解析器不变。

**这是提示词改动，必须以 entry_fragments 会话发布后的新版本为底**（§6.3）。

### 5.2 执行层（代码）：确定性闸门，与模型怎么分类无关

新增纯函数模块 `management_actionability.py`，一个入口 `assess_management_actionability(text, lifecycle_event, intent) -> Refusal | None`。只在**会写交易所**的意图上判；`none` / `hold_update` 本来就不写。

拒绝规则（按子句判：以 `，。；！？\n` 切句，只看动作动词所在的那个子句）：
1. **意向 / 预告标记**与动作同句：`准备`、`预计`、`打算`、`考虑`、`应该`、`可能`、`看情况`、`做好…预期` → 拒绝。
2. **假设条件**与动作同句：`如果`、`若`、`假如`、`万一`、`一旦`、`…的话` → 拒绝。
3. **价格触发条件**（`涨到/跌到/到/突破/跌破/站稳 P …时/再/就`）与**立即成交类**动作同句（全平、市价部分平、加仓）→ 拒绝；与**挂单类**动作（设/移止损到 P、调止盈到 P、「P附近止盈Y%」的 `adjust_take_profit`）同句 → 放行，价格就是挂单价。
4. **需要价格的意图**（`adjust_stop_loss`、`adjust_take_profit`、移止损到 P）没有可解析的具体价位 → 拒绝（大多已有，统一到这里）。
5. **只凭 `event_type = exit_position` 得出的全平**：文字里必须有明确离场动词（`平`/`出局`/`离场`/`走`/`清仓`/`止盈掉`/`落袋`/`全部止盈`），否则拒绝。这一条补的就是 :304 那个缺口。

落点（三条应用路径都要过，不能只挂一处）：
- `management_directives.resolve_management_directive`（:198）：在 :304 的全平分支前调用，拒绝时返回 `intent="none"` + `reason="management_not_actionable:<规则>"`；
- `message_recognition._apply_lifecycle_event_decision`（:1155）：在 `record_lifecycle_exit_intent`（:1420/:1450）与撤单写入之前；
- `_admit_one_explicit_management_target_in_session`（:2167）与 `_apply_deterministic_management_scope_if_matched`（:2416，无 exact 目标的扇出，正是「其它币种多单继续持有」这种没有确定对象的形状）；
- 规划器 `_plan_strategy_management_batch_locked`（strategy_management_planner.py:523）作最后兜底。

记录：拒绝落 `management_not_actionable:<规则>`，卡片与归因可见；**默认不告警**（对评论消息它是预期行为，告警会淹没值守）。见 §9 问题 7。

必须保持通过的既有回归：米娅 M1「止盈50%，剩余仓位止损位下移至84500」→ `partial_then_break_even`；调止盈的「P附近止盈Y%」→ `adjust_take_profit`；M8「保本出局」；Q1 加仓拒绝。

---

## 6. 与已上线改动的关系

| 已上线 | 关系 | 本阶段怎么对待 |
|---|---|---|
| 2a（`4798d360`，只算 `pending_entry` 重叠） | 在 `apparent_entry_may_be_revision` 里 | **原样保留**，回放 19481（§7 R3） |
| 2b（`terminal_target` override） | ② 的窄子集 | 原样保留；② 只加留档键 |
| 3a（被拒目标 id 诊断）、3d（`target_terminal_noop`，`2e60c628`/`a0b00007`） | 3d 在上下文失败后判断 | 保留，且 ③ 的终态分支排在 3d **之后**；`a0b00007`「同时再入场的消息不能被吞」的用例必须仍然通过 |
| 4a/4b/4c（`38318d17`，`mimo_authoritative_failed_exhausted`） | ③ 新增两个终态原因 | 复用 4a 的写法；两个新原因同步登记到 4c 的三处 |
| 校验器修复（`e1d29708`，非策略允许 `strategy: null`） | ③ 在同一个函数里加致命违规 | 保留；新异常只在「分类字段存在且有致命违规」时抛 |
| 米娅修复（M1/M2/M3/M5/M6/M8、Q1） | 都在 `management_directives` | 闸门放在这些分支之前的**全平缺口**与条件句判断上，不改它们的解析；全部既有用例必须通过 |
| 调止盈（`ae6620ad` 等，`take_profit_adjust_mode` 默认 shadow，已在生产） | `adjust_take_profit` 分支 | 闸门规则 3 明确放行挂单类；加一条用例：shadow 模式下「P附近止盈Y%」判定不变 |
| Q5 管理预检拒绝（`d244dfeb`） | 执行边界「零写入才信账本」 | 闸门拒绝发生在规划之前，零写入；不产生新的预检拒绝样本。**不影响 Q5 至 10-06 的待验证项** |

### 6.1 设计稿 §10 D 组（执行链路读者）逐个确认

执行链路**继续读旧字段**；本阶段只有 §5.2 的闸门改变执行。D1–D12 逐个确认不需要改：D1 `normalize_authoritative_instructions`（退役见 R2，阶段 4）、D2/D3 读 `是策略`（不变）、D4 `entry_preambles`、D5 `entry_strategy_fragments`、D6 `management_directives`（加闸门）、D7 `management_fraction_gate`、D8、D9、D10（已退役的分析复核，确认无读者）、D11 归因（加新键）、D12。A6 `_upsert_experiment_result` 明确**保持旧口径**。

### 6.2 网页（设计稿 §10 E2/E3/E6）

E2：卡片主标签在 `message_classes` 存在且无致命违规时直接读它（多元素显示多个标签），否则回退旧推导块。E3 的两个 data 属性不动。推导块本身留到阶段 4（R1）。见 §9 问题 6。

### 6.3 与 entry_fragments 会话的冲突与测量界线

- 那个会话正在改 `prompt_defaults.py`、`tests/test_ai_recognition_config.py`、`tests/test_prompt_composition.py`，并将发布新的 `trading.analysis.shared` 版本（补全截断的【两套判据刻意不一致】一节、新增 `leg_allocation`）。
- 本阶段**代码批次不碰这三个文件**。§5.1 的提示词规则作为**第二批**：等它的版本发布、提交进 `origin/main` 后，以那个版本的**生产原文**为底改种子与生产版本。
- 测量界线因此变成四段：`01:32:14Z`（校验器修复）/ entry_fragments 版本发布时刻 / **阶段 3 代码部署时刻** / 阶段 3 提示词发布时刻。都写进状态文档。
- 观察文档 §11 的「无止损的是策略是否恢复」统计：本阶段的代码不动 `recognition_result` 的判定，`strategy_not_allowed` 非致命（§3.1），所以不破坏它；提示词第二批只改管理元素的可执行性，不碰【新开仓识别】与【两套判据】两节。它的第一周最好不和本阶段提示词发布重叠，见 §9 问题 2。

---

## 7. 验收回放（修复前失败、修复后通过）

实现为测试：首轮 payload 从生产库按主键点查取出（`recognition_decisions` / `context_resolution_attempts.request_summary_json.mimo_first_pass` / `message_evidence_versions`，按 `raw_message_id` 索引），脱敏后存成 fixture；候选按当时的生命周期状态构造。**每条用例都先在 `origin/main` 上跑一遍确认失败**，失败原因写进提交说明。

| # | 样本 | 修复前 | 修复后必须 |
|---|---|---|---|
| R1 | raw 18602「先取消先观望」 | 首轮不可执行，靠 `cancellation_language` 抓回（上下文 `cancel_thread` 0.93） | 用 v9 契约重答一次（§9 问题 5），得到 `策略管理/unknown` → 新判据触发。**接不住就保留 `cancellation_language`**，方案改为「删 2 留 5」 |
| R2 | 18893、18895（米娅，09-24 事故形状）、18555、18375 | `entered_holder_language` 单独触发，首轮被压下 / 整条失败 | 不触发上下文，首轮结论生效（18893 `entry_confirm → 1319`、18895 `position_update → 1319`、18555 继续持有无写入、18375 首轮生效） |
| R3 | raw 19481（陈哥 BTC 原样重发） | （2a 前）`apparent_entry_may_be_revision` → `manage_thread` → 非策略 | 新判据集合下不触发 `apparent_entry_may_be_revision`，得到「是策略 → new_thread」；同时核对原文不含修订词 |
| R4 | 18843「如果突破可继续持有」 | `position_update → 1314` | 执行层：无交易所写入（规则 2）；识别层（第二批提示词后）：`闲话` |
| R5 | A1 评论 7 个内容（19011、19115、19360、19445、19448、19653、19439） | 分类 `仓位管理/unknown`；若上下文给出 `exit_thread`，19011 会被全平 | 执行层：即使构造上下文返回 `exit_thread`，也 `management_not_actionable`、零写入 |
| R6 | A1 指令 4 个内容（19692 JTO 0.51、19694 TAO 281、19071、19454） | 旧字段 none，永远不执行（漏单） | 触发 `management_without_exact_target`；构造上下文返回确定目标后，19692/19694 走到 `adjust_stop_loss` 且价格正确；19454「add more」按 Q1 仍被拒（加仓不支持） |
| R7 | F1：19309、19780 | `duplicate_class_target` | 无违规 |
| R8 | F2：19351、19481 | 对最终 payload 报 `strategy_required` | 读首轮，无违规 |
| R9 | F3：19741 | 推导为两个 unknown | 推导为 `exact#1365` + `exact#1361`，与显式一致 |
| R10 | 18970（闲话带全 null target） | `resolution_missing` + `target_not_allowed` | 规范化，不致命 |
| R11 | 19308（1207 + 陈旧 909） | 不触发上下文（旧判据无关键词） | 触发 `exact_target_outside_candidates`（909 不在候选）；BTC 元素不受影响 |
| R12 | 18978/18979/19222 → 1314（expired）；19551/19594 → 1346 | 不触发，或靠关键词偶然触发 | 触发 `exact_target_not_manageable` |
| R13 | 19490 及 17467/17972/18032/18375/18501/18897 的上下文契约失败 | attempt 内问两次；任务退避 5 次，堵同群 7–13 分钟 | 每个 fingerprint 只发 1 次请求；19490 走 3d `target_terminal_noop`；其余落 `context_contract_failed` 终态并告警，任务不再排队；同群下一条立即被认领 |
| R14 | 首轮致命违规（构造） | 同模型重试一次 | 同模型不重试；链尾仍违规 → `first_pass_contract_violation` 终态、不入重试队列、入场准入不被挡 |
| R15 | 米娅 M1、调止盈「P附近止盈Y%」、M8「保本出局」 | 通过 | 仍通过（闸门不误伤） |
| R16 | 提示词回滚到 v8（无 `message_classes`） | — | 旧字段兜底判据生效；零违规；识别不中断 |

**全量**：`uv run python -m pytest -q`，必须回到当前基线（开工时在本 worktree 实测记录）或更高，且只允许本阶段有意改写的旧用例变动，每一处改写在提交说明里列出理由。

---

## 8. 部署、回滚与观察（L2）

**批次**
- **批次 A（代码）**：①②③、F1–F4、执行层闸门、E2。不碰提示词。交付到「候选 sha + 全量通过」为止，**不部署、不推 `origin/main`**。
- **批次 B（提示词）**：§5.1 规则。前置：entry_fragments 版本已发布并已入 `origin/main`；以其生产原文为底；经提示词中心对照测试（A1 评论 / 指令各取样）后发布。

**部署（调度会话执行）**：推自有分支 → 核对候选是生产 HEAD 的后代 → `tg-deploy <sha>` → 同一 sha 推 `origin/main` → 双向核对 + offenders 检查。无 schema 变更，无依赖变更。**不在陈哥 / 米娅等群有未完结的入场或管理操作时部署**。

**回滚**：代码 `tg-deploy <部署前生产 HEAD>`；提示词退回上一个 published 版本（批次 B 与代码独立，可单独回滚）。无数据迁移，回滚后旧判据立即恢复。

**观察判据**（L2，连续 30 分钟且 ≥5 条真实消息、尽量 2 个群，服务器只读监视脚本每分钟采样，达标自停，上限 24 小时）：
1. 三服务 `active`；
2. `context_resolution_gate_json.triggers` 中不再出现三个被删的名字；新两个名字出现时逐条列出；
3. 每条决策的上下文请求数 ≤ 1（非网络错误）；
4. `first_pass_contract_violation` / `context_contract_failed` 出现即列出，逐条核对不是误报；
5. `识别失败` 与失败的消息处理任务不高于部署前同时长基线；
6. 没有同群消息因上一条排队超过 60 秒；
7. `management_not_actionable` 出现即列出原文；
8. 窗口内若有管理写入，直接核对交易所历史。

**窗口后一周（不阻塞收口，记进状态文档）**：每天上下文调用次数（阶段 2 前约 449 次 / 14 天）与触发原因分布；被新两条判据接住的 exact；`management_not_actionable` 全部原文，人工抽查是否误伤。

---

## 9. 需要用户拍板的问题

| # | 问题 | 推荐 | 备选 |
|---|---|---|---|
| 1 | 识别层的可执行性怎么表达 | **不加字段**：条件句 / 意向 / 无确定对象的「管理」直接判 `闲话`，旧字段 `event_type = none`。与 A 组「评论 12 条」的标注一致，契约与解析器不动 | 给管理元素加 `可执行: 指令 / 意向` 字段：多一个可量的信号，但要改契约、marker、解析器，是契约变更 |
| 2 | 提示词第二批何时发 | **代码先部署**；提示词等 entry_fragments 版本发布满一周（它的「无止损的是策略」统计做完）再发，避免两次提示词变更叠在同一周 | entry_fragments 发布后立即发，快，但那一周的统计会混进第二个变量 |
| 3 | 首轮致命违规后换不换下一个模型 | **按设计稿沿用链路规则（换）**。致命违规在阶段 2 修掉误报后约为 0，成本可忽略 | 不换，直接 fail-closed：更贴近「重试没用」，但一次偶发违规就丢整条消息 |
| 4 | exact 不在候选集合内 | **触发上下文**（19308 两个元素只错一个） | 判契约违规、fail-closed（设计稿 §6 原文的读法） |
| 5 | R1 需要让模型用 v9 重答 18602 一次 | **同意**：经提示词中心对照测试，写 `ai_prompt_test_runs` 一行，不碰识别与执行 | 不做，直接保留 `cancellation_language`（删 2 留 5） |
| 6 | 网页卡片主标签改读 `message_classes`（E2） | **本阶段做**（纯展示） | 留到阶段 4 |
| 7 | 执行层闸门的拒绝要不要告警 | **不告警**，只记录、卡片可见、一周后看全部原文 | 对「有确定活仓 + 有价位、只因条件句被拒」的告警 |
| 8 | 闸门的条件 / 意向词表（§5.2 规则 1–3） | 按 §5.2 列的词表起步。这是关键词，但方向是**拒绝写入**（误伤 = 少做一次动作并留痕），与被删的「关键词触发上下文」性质相反 | 你补充或删减词 |
