# 首次分析分类改造 · 新契约设计稿（第一步：只定契约，不动代码）

**上游**：`docs/plans/2026-09-25-first-pass-classification-design-handoff.md`（唯一真相）。
**状态**：`draft` 第 3 版，五个决策全部拍板（§7），**契约本身已获用户确认**。
阶段 1 之前仍不写生产代码、不发提示词版本、不部署。
**风险级别**：改动改变「哪些消息进入管理链路」，按 **L2** 对待；**部署须用户单独批准**。

**第 3 版**：决策 1 的命名定为 `图片不可读`，决策 4 定为甲（§7）。

**第 2 版相对第 1 版的三处修改**（均来自用户 2026-09-24 的答复）：
1. 删掉分类值 `识别失败`。「识别失败」这个词**专指 HTTP 非 200 / 拿不到内容**，那是运行级
   故障，根本没有 payload，不占分类值域。模型正常返回、但输入本身读不出来的那一类，
   改名为 `图片不可读`（§2.2）。
2. `新策略` 必须有止损，**确认为硬判据**（§3.1）。
3. 分类改成**列表**。用户指出见过「对现有持仓离场 + 反手开新仓」写在同一条消息里的群组，
   一条消息可以同时属于两类。第 1 版的「优先级取其一」方案作废（§2.1、§2.4）。

---

## 1. 这一稿要回答的三个问题

1. 显式分类字段的值域与判据。
2. 配合它的字段是什么，以及每一类下**允许为空 / 必须为空 / 空即违规**。
3. 这两个字段存在后，触发上下文分析的判据怎么重写（这是省 token 的全部来源）。

不在本稿范围：改代码、改提示词版本、改执行语义、复活 MiMo v2、动 `006972e1` 的
`entry_confirm` 两形态实现。

---

## 2. 新契约

在现有 `trading.analysis.shared` 输出对象里**新增一个顶层字段**，
`recognition_result` / `lifecycle_event` / `strategy` / `instructions` / `entry_context`
/ `entry_fragments` / `evidence` / `input_reading` **全部原样保留**。

```json
{
  "message_classes": [
    {
      "class": "新策略 | 策略管理 | 仓位管理 | 闲话 | 图片不可读",
      "target": {
        "resolution": "exact | forthcoming | unknown",
        "lifecycle_id": null,
        "symbol": null,
        "side": null
      }
    }
  ]
}
```

每个元素是一对「**类别 + 它作用在谁身上**」。`class` 是用户要的显式分类字段，
`target` 是用户说的「配合这个分类的字段」。

### 2.1 为什么是列表

用户举的反例：同一条消息「XX 平掉 + 反手做多，入场…止损…」，
既是 `仓位管理` 又是 `新策略`。第 1 版用优先级选一个，会把另一个丢掉。

现有契约本来就允许一条消息带多个独立动作（`instructions` 数组，
提示词里写着「取消挂单/仓位管理和新开仓可以同时存在」），所以列表是对齐现状，不是新增复杂度。

**列表的约束（写死，不留模型自由裁量）**：
- 非空。空列表 / 缺字段 / 不是数组 = 契约违规。
- 元素顺序：**管理类在前、新开仓在后**，与现有 `instructions` 的排序约定一致。
- `闲话` 与 `图片不可读` **必须独占**：出现它们时列表长度必须为 1。
- 同一个 `class` 允许重复，**当且仅当 `target` 不同**（「BTC 和 ETH 都平掉」＝两个
  `仓位管理` 元素）。`(class, target)` 完全重复 = 契约违规。
- 长度上限 4。超过即违规——一条消息真有 5 个独立动作，那是该进人工核准的消息。

### 2.2 `class`：五个值

| 值 | 含义 | 用户原话对应 |
|---|---|---|
| `新策略` | 本条消息给出一笔**新开仓**，参数完整到可以执行 | 「有交易合约标的，有入场价格，有止损价格（允许暂时没有止盈目标）」 |
| `策略管理` | 目标策略**还没入场**：撤单、改价、改区间，或为**已发布但未入场 / 尚未发布**的策略定量（半仓、30%） | 「前面给出了策略，但是策略还没有入场，现在需要对策略作出调整」＋两个「半仓入场」的例子 |
| `仓位管理` | 目标**已经入场**：调止盈、调止损、临时离场、部分止盈、全平 | 「对已经触发入场的持仓进行管理」 |
| `闲话` | 不改变任何策略或持仓状态的一切 | 「闲话」 |
| `图片不可读` | 消息**带图**，模型正常返回了内容，但它读不出那张图：模糊、裁切、遮挡、关键数字看不清 | （见下） |

**`图片不可读` 与「识别失败」是两件事，本契约把它们彻底分开。**

| | 发生什么 | 有没有 payload | 记在哪 |
|---|---|---|---|
| **识别失败**（运行级） | HTTP 非 200、超时、连接失败、空响应、JSON 解不开 | **没有** | 现有的 run/attempt 审计与 `error_message`，**不进 `message_classes`** |
| **`图片不可读`**（分类值） | 模型 200 返回并给出了内容，但它自己说读不出 | 有 | `message_classes` 的唯一元素 |

按用户的界定：「能返回内容就不算识别失败」。所以分类值域里不再有 `识别失败` 这个词，
运行级故障也不再借用分类字段表达。现有 `recognition_result = 识别失败` 一直同时承担
这两件事，是这次要拆开的旧债之一（旧字段本身不动，见 §8 阶段 4）。

### 2.3 `target`：配合字段

`target` 回答「这一类作用在谁身上」，是整个设计里承载空值语义的地方。

- `resolution`：三态，`target` 非 null 时**必须非 null**。
  - `exact` —— 模型能唯一指出目标，`lifecycle_id` 必须给出且必须在本次输入的候选集合内。
  - `forthcoming` —— 目标是**还没发出来的那条策略**（「BTC准备87500做空，半仓入场」）。
    没有可指的 `lifecycle_id`，但必须给出 `symbol`，否则这份定量无处归属。
  - `unknown` —— 模型确信这是管理动作，但**说不出目标**。这是**唯一**需要上下文分析的状态。
- `lifecycle_id`：只有 `exact` 时非 null，其余两态必须 null。
- `symbol` / `side`：证据充分才填，不得猜测。

### 2.4 空值语义总表（提示词与解析器逐格照抄）

按**元素**判定，不是按整条消息判定：

| `class` | 该元素的 `target` | 该类对 `strategy` 的要求 |
|---|---|---|
| `新策略` | **必须 `null`** | `strategy` 必须非空，且 `symbol`/`side`/`entry`/`stop_loss` 四项非 null；`take_profit` 允许 null |
| `策略管理` | **必须为对象**，三态均可 | 不要求 |
| `仓位管理` | **必须为对象**，只能 `exact` 或 `unknown`（**不得 `forthcoming`**，持仓不可能还没发生） | 不要求 |
| `闲话` | **必须 `null`** | `strategy` 必须 `null` |
| `图片不可读` | **必须 `null`** | `strategy` 必须 `null` |

- 「必须 `null`」= 出现非 null 即契约违规；「必须非空」= null 或全字段 null 即契约违规。
- 列表里**没有** `新策略` 元素时，`strategy` 必须为 `null`。
- `策略管理` 用 `forthcoming` 时，**必须**同时出现 `entry_context` 或 `entry_fragments`
  （承载「半仓 = 0.5」这类定量）。否则这条消息没有任何可携带给后续策略的信息，
  应判为 `闲话` 而不是 `策略管理`——见 §3.2 末尾。

---

## 3. 五类的判据（提示词里逐条写死）

### 3.1 `新策略`
同时满足：① 明确标的；② 明确方向；③ 明确入场方式（价/区间/市价/到价）；
④ **明确止损价或无效价**；⑤ 表达的是新开仓，不是复盘、教学、广告、历史截图。

**④ 是硬判据，用户已确认：单条消息没有止损价，一律不能认定为新策略。**
止盈缺失不影响判为 `新策略`。

### 3.2 `策略管理`
目标策略**尚未入场**。三种形态：
- **撤销 / 改价**：取消挂单、撤单、改入场价、改区间、改止损（针对未入场策略）。
- **入场定量（策略已发布）**：前一条是完整策略，本条「半仓入场」→ 目标是那条策略，
  `resolution = exact`，`lifecycle_id` 指向它。
- **入场定量（策略尚未发布）**：「BTC准备87500做空，半仓入场」——有标的有入场价
  **但没有止损**，按 §3.1 不是策略；它预告的是后面那条正式策略按半仓对待。
  `resolution = forthcoming`，`symbol = BTC`，`lifecycle_id = null`。

> 「有标的 + 有入场价 + 无止损」这条边界，正好接住被 §3.1 的 ④ 挡下来的消息。
> 但**只有当这条消息还携带了对后续策略有用的信息**（仓位倍率、分腿比例、补仓价）时
> 才算 `策略管理`；只是提了一嘴价格、什么定量都没有的，是 `闲话`。
> 这条限制是为了不让「没止损的半截策略」白白灌进管理链路。

### 3.3 `仓位管理`
目标**已经入场**。调止盈、调止损、移动止损到成本、部分止盈、临时离场、全平、继续持有。
`exit_position` 与 `position_update` 的区分规则**原样保留**现有提示词里那几条
（「第一止盈位 + 移动止损至成本价」是 `position_update` 不是全平），不合并、不改写。

### 3.4 `闲话`
行情观点、复盘、教学、情绪、广告、联系方式、群公告，以及「只有方向没有入场」
「只有价格没有方向」「已经错过」，和 §3.2 末尾那种没有任何定量的半截价格。

### 3.5 `图片不可读`
模型正常返回内容，但读不出图：模糊、裁切、遮挡、关键数字看不清。

**前置条件：这条消息必须真的带图。** 纯文字消息**永远不可能**是 `图片不可读`——
读得懂就按它真实的类别判，读不懂也只能是 `闲话`。这条前置条件让这个值可以被静态校验：
`input_reading.image_quality = none`（没有图片）时出现 `图片不可读` = 契约违规。

**不包括**两种情况：
- 「图读到了，但和文字互相矛盾」——消息仍然有真实类别，矛盾写进 `evidence.conflicts`，
  由 `text_image_conflict` 触发上下文分析（§5）。
- 「图文并茂，图看不清，但**光凭文字**已经够判类」——按文字判，不要因为图糊就整条作废。
  只有当图是判类的必要证据、而它读不出来时，才用这个值。

---

## 4. 与现有字段的映射（影子比对期的推导基准）

阶段 1 不切换任何行为，只把**显式列表**与**从现有两个字段推导出的列表**并排落库比对。
推导规则（只读，不写回）：

| 现有字段 | 推导出的元素 |
|---|---|
| `recognition_result = 识别失败` | `[{图片不可读, null}]`（旧值把运行级故障与读不出图混在一起，推导侧无法区分，这一格在阶段 2 的一致率里单独统计，不与其它四类混算） |
| `event_type ∈ {position_update, exit_position}` | 追加 `{仓位管理, target}` |
| `event_type ∈ {cancel_entry, entry_confirm}` | 追加 `{策略管理, target}` |
| `recognition_result = 是策略` | 追加 `{新策略, null}` |
| 以上都不成立 | `[{闲话, null}]` |

追加顺序即 §2.1 的排序约定（管理在前）。`target` 由 `lifecycle_event.target_lifecycle_id`
推出：非空 → `exact`，空 → `unknown`。
`lifecycle_event.targets`（多目标 fanout）展开成多个同类元素。

现有契约**本来就能**同时表达 `是策略` 与 `event_type != none`，所以「离场 + 反手」
在推导侧会自然得到两个元素——列表化不是凭空发明，是把已有的表达能力显式化。

`forthcoming` 在现有字段里**没有对应物**，它出现的每一条都是新增能力，单独统计。

---

## 5. 上下文分析的触发判据（缺陷 (a) 的正面解法）

用户要的是：「首次分析明确目标就不需要进行」。新契约让这句话可以直接写成代码：

```
需要上下文分析  ⟺  message_classes 里存在某个元素，
                   class ∈ {策略管理, 仓位管理} 且 target.resolution == "unknown"
```

一条消息里只要有**一个**管理元素说不出目标，就解析；全都说得出（或压根没有管理元素），
就不解析。

现有 8 条触发判据按下面处理。**这 8 条现在是「或」的关系——任意一条成立就多花一次
上下文分析**，所以删掉误触发的那几条，就是省 token 的全部来源。

**建议删掉的 3 条（纯看有没有某几个词，与这条消息是否真的缺目标无关）：**

| 判据 | 它在看什么 | 为什么删 |
|---|---|---|
| `revision_language` | 消息里有没有「更新/修改/改为/调整/replace/update」 | 这些词在交易群里满天飞 |
| `cancellation_language` | 有没有「取消/撤销/撤单/cancel」 | 同上 |
| `entered_holder_language` | 有没有「有入场/已入场/持仓/保护成本/保本/继续持有」 | **2026-09-24 米娅 msg 696 的直接肇因**：文末「做无风险**持仓**」里的「持仓」两个字命中它，尽管首次分析已经明确给出 `target_lifecycle_id = 1319`。那次解析本不该发生 |

**建议保留的 4 条（不看措辞，看结构上有没有实际矛盾）：**

| 判据 | 它在看什么 | 为什么留 |
|---|---|---|
| `text_image_conflict` | 文字与图片在标的/方向/动作/价格上冲突（文字说 BTC、图片是 ETH） | 模型两个证据源打架，它给的目标不可信 |
| `reply_target_disagreement` | 这条消息**回复**的那条，和模型说的目标不是同一条策略 | Telegram 回复关系是外部事实，能证伪模型的目标 |
| `multiple_same_source_candidates` | 这个源同时有 ≥2 条活着的候选策略，**且**这条消息本身可执行 | 2026-09-15 方案甲已经收紧过，保持原样 |
| `apparent_entry_may_be_revision` | 模型说这是新策略，但它的入场区间和一条已有未入场策略重叠 | 「新策略其实是改单」的唯一防线 |

还有 1 条 `management_without_exact_target`：它本来就是用户想要的那条，**保留并改写**成上面
那个唯一判据（从「有事件但没目标」改成「有管理元素但 resolution=unknown」）。

**实现约束**：`revision_language` 目前还被 `apparent_entry_may_be_revision` 复用作输入。
删除时把它降级为该判据**内部**的局部条件，不再单独触发。

**用户已选甲**：删 3 留 4。收紧后 8 条判据变成 5 条，其中只有 1 条会因为「模型说不出目标」
而触发，另外 4 条只在结构上真有矛盾时才触发。

---

## 6. 契约违规的定义与处置（用户「不该重试」的落点）

**契约违规** = §2.1 的列表约束或 §2.4 的空值表格任何一格被违反，
或 `class` 越界，或 `exact` 的 `lifecycle_id` 不在候选集合内。

**首次分析：**
- **运行级失败**（HTTP 非 200、超时、连接失败、空响应、JSON 解不开）→
  这就是用户说的「识别失败」，按现有 `MIMO_AUTHORITATIVE_MAX_ATTEMPTS` 与模型链回退处理，
  **不变**。
- **契约违规**（拿到了 200 和内容，但内容不合契约）→ **不重问同一个模型**。
  用户原话：「识别错误那是模型能力问题，重试也没用」。当前
  `_call_mimo_authoritative_with_retry` 对两类失败一视同仁地重试到 2 次，这一半调用是纯浪费。
  改为：契约违规直接结束本模型，记 `failure_class = RESPONSE_INVALID`（已有），
  是否换下一个模型沿用现有链路规则。
- 契约违规的最终结果 **fail-closed**：不进管理链路、不进执行，完整记录违规字段与模型原样
  输出，供人工核准。注意它与 `图片不可读` 不同——后者是模型**守着契约**说「我读不出」，
  是合法答案。

**上下文分析（缺陷 (c)）：**
- `network_error` → 重试，沿用 `ContextNetworkRetryPolicy`。
- 其余 `CONTEXT_RESOLUTION_ERROR_CODES`（含 `target_outside_candidate_set`）→
  **attempt 内部不再问第二次**，直接 `exhausted`。结构错误第二次问不会有新信息。
  跨 attempt 的 `context_fingerprint` 不同那是「状态变化后重分析」，**不动**。
- `_rejected_response_diagnostic` 现在只记 `target_thread_count`，**必须补上**被拒的
  `target_thread_ids` 本身与当次 `allowed_thread_ids`。连续失败六次却只留下「不匹配」，
  是 2026-09-24 那次无法定位的直接原因。这一项是纯可观测性、零行为变化、L0，
  **可以脱离本设计单独做掉**。

---

## 7. 决策记录

| # | 决定 | 结论 |
|---|---|---|
| 1 | 分类值域里要不要 `识别失败` | **不要**，用户已确认。「识别失败」专指 HTTP 非 200 / 拿不到内容，是运行级故障，没有 payload。模型返回了内容但读不出图，用户定名 **`图片不可读`**（§2.2、§3.5） |
| 2 | `新策略` 是否必须有止损 | **必须**。用户已确认。影响面（历史上「只有止盈、没有止损」的消息现在被判成什么、改判后去哪一类）在阶段 2 的观察窗里数出来给用户看 |
| 3 | 混合消息怎么表达 | **分类改成列表**，不再取优先级。用户举的「离场 + 反手」是原设计的反例 |
| 4 | 触发判据收紧力度 | **甲**，用户已确认：删 3 条关键词判据，保留 4 条结构判据，`management_without_exact_target` 改写成唯一的目标判据。详见 §5 |
| 5 | 枚举值语言 | **中文**。用户已确认 |

---

## 8. 落地顺序（每阶段的风险级别与批准点）

> 本稿只求批准 §2–§7 的契约本身。下面的阶段划分是为了让用户看清「批准之后会发生什么」，
> **每一阶段仍然单独开工、单独汇报**。
> **每个阶段动哪些文件，看 §10 的下游衔接清单；旧代码怎么退役，看 §11。**

- **阶段 1 · 影子（L1，additive dormant）**
  提示词新增 `message_classes` 并发布新版本；解析器**只读不用**：写进
  `message_evidence_versions.normalized_evidence_json` 与 Web 展示，并与 §4 的推导列表
  并排比对。`prompt_composition.validate_prompt_content` 的 `trading_shared` 档加上新字段
  marker，使**任何缺新字段的提示词版本无法发布**。
  行为零变化：触发判据、执行、管理链路一律不动。
  兼容性：解析器必须容忍**没有**新字段的旧提示词版本（记 `missing` 而不是违规），
  否则回滚提示词会打断识别。
  **发布顺序**：先部署代码，再发布提示词版本；回滚只需把提示词退回 v8，不必回滚代码。
  **提示词里会同时存在两套「新策略」判据**：新加的【消息分类】段要求必须有止损，
  原有的【新开仓识别】段维持「止损或止盈有其一即可」。这是刻意的——改后者就会改变
  `recognition_result`，那不是 L1。提示词里必须**明写**这一点，否则模型会自行调和，
  阶段 2 要量的那批消息就量不出来了。退役条件见 §11 R10。

- **阶段 2 · 观察与人工核准（L0）**
  用户要求「模型的能力应该以人工进行历史核准来判断」。观察窗产出四张表：
  显式 vs 推导 的一致率、`forthcoming` 的全部样本、**多元素消息的全部样本**
  （验证「离场 + 反手」这类到底多常见）、决策 2 影响到的历史消息清单。
  人工核准复用现有 `message_recognition_labels`（`labeled_recognition_result` +
  `labeled_event_type` 已足以反推分类列表），**本阶段不加数据库列**，避免无谓的 L3。

- **阶段 3 · 切换（L2，须用户单独批准后才能部署）**
  **§5 的触发判据必须改写（2026-09-25 阶段 2 实测）**：只按 `resolution == unknown` 触发是不够的。
  实测 4 条 `exact` 里上下文分析纠正了 2 条（目标已 expired / 已不在候选集合），
  而**触发它们的正是本设计打算删掉的那几条判据**。新判据至少要加上
  「`exact` 的目标不在候选集合内」——这一条是确定性的、零模型调用，
  而且 `parse_message_classes` 的 `lifecycle_id_outside_candidate_set` 已经写好了，
  只是**没有任何生产调用点传 `allowed_lifecycle_ids`**，接上即可。
  详见 `docs/plans/2026-09-25-first-pass-classification-phase2-measurement.md` §4.5。
  **前置条件（2026-09-25 首个观察窗发现）**：`resolution == exact` 时跳过上下文分析，
  其可信度上限由**候选集合**决定，不由模型决定。首个样本里模型给出的 `exact` 指向一条
  2026-08-20 的、早已 `expiry_review_requested` 的 `pending_entry`——而它是那个群里唯一的
  ETH long 候选，模型守了契约。全库有 42 条超过 7 天的 `pending_entry`。
  **候选集合的过期收口没做之前不得切换**，否则等于把污染直接放行。
  ① 触发判据换成 §5；② 降级不再抹平首次分析（缺陷 (b)）：`_resolved_mimo_result` 在
  `hold`/`unresolved` 时**保留** `message_classes`，只把**执行授权**降级，让下游仍然看得出
  这条消息曾被判为仓位管理；③ 契约类失败不重问（§6）。

- **阶段 4 · 收口（以后）**
  `recognition_result` / `lifecycle_event` 双轨是否退役、`识别失败` 这个旧值的两种含义
  怎么拆干净，届时另议。本次**不删任何现有字段**。

---

## 9. 明确不做的事

- 不复活 MiMo v2 契约，不重新引入它的十一类意图。
- 不推翻 `006972e1` 的 `entry_confirm` 两形态实现。
- 不合并 `exit_position` 与 `position_update`。它们在 `仓位管理` 之下仍是两个不同的
  `event_type`，现有防混淆规则原样保留。
- 不改任何执行语义、入场几何、止损阶梯。
- 不在本稿动数据库：`trading.analysis.mimo_v2_authoritative`（def=6）在生产库里仍 active，
  是一条独立的、与本设计无关的下线操作。

---

## 10. 下游衔接清单（改一个字段会波及谁）

> **这一节是给未来会话看的。** 首次分析的返回格式一改，下面每一处都要跟着对齐；
> 即使某一项还没轮到，也必须留在清单里，否则换一个会话接手就会漏掉，系统会在别的地方出错。
> 行号以 `6c7d9ebe` 为准，只作定位用，不作断言。
> **阶段**列写的是这一处最早必须动的阶段；空着表示只需确认不受影响。

### A. 契约的产生与校验

| # | 位置 | 要做什么 | 阶段 |
|---|---|---|---|
| A1 | 生产提示词 `trading.analysis.shared`（def=1，**现为 v8，真值在数据库不在代码**） | 发布新版本，加入 `message_classes` 输出块与 §2.4 的空值规则 | 1 |
| A2 | `prompt_defaults.DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT` | 同步种子内容（只影响全新库，不影响生产） | 1 |
| A3 | `prompt_composition.validate_prompt_content`，`trading_shared` 档的 `required_schema_markers` | 加 `"message_classes"` / `"class"` / `"target"` / `"resolution"` 与五个中文枚举 marker，**缺字段的提示词版本从此发不出去** | 1 |
| A4 | `recognition_experiments._validate_authoritative_payload`（:737） | 阶段 1 只在缺字段时记 `missing`（**不得抛错**，否则回滚提示词会打断识别）；阶段 3 才升格为契约违规 | 1 / 3 |
| A5 | `recognition_experiments._call_mimo_authoritative_with_retry`（:659，`MIMO_AUTHORITATIVE_MAX_ATTEMPTS = 2`） | 契约违规不再重问同一模型（§6） | 3 |
| A6 | `recognition_experiments._upsert_experiment_result`（:1212）与 `MIMO_EXPERIMENT_STATUSES`（:66） | run 的 `status` 取自 `recognition_result`；新分类出来后这里要么跟着改，要么明确保持旧口径 | 3 |

### B. 落库与重建（漏掉这两处，字段会在恢复路径上凭空消失）

| # | 位置 | 要做什么 | 阶段 |
|---|---|---|---|
| B1 | `message_evidence.py` 的 `normalized_evidence`（:440 起） | **必须**把 `message_classes` 写进 `normalized_evidence_json`。这是首次分析结论的唯一不可变留存 | 1 |
| B2 | `authoritative_recognition._load_current_mimo_evidence_result`（:450） | 从 `normalized_evidence_json` 重建 payload 的地方，**必须**把 `message_classes` 带回来。B1 写了而 B2 不读，等于重放/恢复路径上字段静默丢失 | 1 |
| B3 | `recognition_decisions.upsert_*`（:217 的 `changed = row.authoritative_payload_json != payload_json`） | payload 多一个字段会让这个比较判为"变了"。阶段 1 要确认这不会造成重复写入或误触发 `comparison_status` 重置 | 1 |
| B4 | `models.MessageRecognitionLabel`（`labeled_recognition_result` / `labeled_event_type`） | 阶段 2 用这两列**反推**分类，不加列；是否加 `labeled_message_classes` 留到阶段 4（加列是 L3） | 4 |

### C. 路由与降级

| # | 位置 | 要做什么 | 阶段 |
|---|---|---|---|
| C1 | `authoritative_recognition.CONTEXT_TRIGGER_ORDER` / `REVISION_LANGUAGE` / `CANCELLATION_LANGUAGE` / `ENTERED_HOLDER_LANGUAGE`（:123-142） | 删三条关键词判据；`REVISION_LANGUAGE` 降级为 `apparent_entry_may_be_revision` 的内部条件 | 3 |
| C2 | `authoritative_recognition.requires_context_resolution`（:175） | 换成 §5 的唯一判据 | 3 |
| C3 | `authoritative_recognition._resolved_mimo_result`（:531，:585/:613/:645 三处 `recognition_result="非策略"` 抹平） | 降级时**保留** `message_classes`，只降执行授权（缺陷 (b)）。注意 `_context_resolution.first_pass` 已经存了四个旧字段，新字段要一并进去 | 3 |
| C4 | `context_resolution_shadow.py:117` | 影子判据读 `recognition_result`，切换时同步 | 3 |
| C5 | `context_resolution.py:1154` 的 `terminal = attempt_number == 2` | 契约类失败不重问（§6） | 3 |
| C6 | `context_resolution._rejected_response_diagnostic`（:467） | 补上被拒的 `target_thread_ids` 与当次 `allowed_thread_ids` | **可提前，L0** |

### D. 执行链路里读首次分析 payload 的地方

这些是"哪些消息进入管理链路"的实际承接点。阶段 1/2 它们**一律不动**（新字段只读不用），
阶段 3 切换时必须逐个确认。

| # | 位置 | 它读什么 |
|---|---|---|
| D1 | `authoritative_instructions.normalize_authoritative_instructions`（:138、:163、`_legacy_management_kind` :178） | 从 `recognition_result` + `lifecycle_event` **造** `instructions`。分类列表出来后，这里是全仓最可能需要重写的一处 |
| D2 | `entry_assembly_admission.py:483` | `recognition_result == "是策略"` |
| D3 | `entry_strategy_assembly.py:352` | 同上 |
| D4 | `entry_preambles.py:142` | `recognition_result != "非策略"` 决定要不要收 `entry_context` |
| D5 | `entry_strategy_fragments.py:81` | `payload["lifecycle_event"]` |
| D6 | `management_directives.py:149-159` | `event_type` + `management_action` → 管理指令 |
| D7 | `management_fraction_gate.py:20` | `payload["lifecycle_event"]` 取比例 |
| D8 | `message_operation_contracts.py:319` | 决策 payload 的 `lifecycle_event` |
| D9 | `message_recognition.py:2744 / 2807 / 2937 / 3052 / 3220 / 3241` | 多处按 `recognition_result` 定 status |
| D10 | `semantic_disagreement_review.py:896 / 915` | 语义复核的输入（只读顾问，不执行） |
| D11 | `recognition_failure_attribution.classify_unapplied_lifecycle_event`（:157） | 归因"为什么没执行" |
| D12 | `auto_trade_execution.py:936` | `lifecycle_event_not_new_entry` 这个拒绝理由的语义 |

### E. Web 页面与只读接口

| # | 位置 | 要做什么 | 阶段 |
|---|---|---|---|
| E1 | `web_queries.py:889-1018` | 把 `recognition_result` / `lifecycle_event_type` 投影给页面的地方，加上 `message_classes` 投影（以及 §4 的推导结果，供并排比对） | 1 |
| E2 | **`templates/_messages.html:156-175`** | **页面现在自己用那两个字段推出一个显示分类**：识别异常 / 需要上下文 / 开仓信号 / 仓位管理 / 闲聊无关 / 结论未记录。这正是新字段要替代的推导，而且它是**单值**的——「离场 + 反手」那种消息现在只会显示一个标签。阶段 1 先并排显示"显式 vs 推导"，阶段 3 改成直接读 `message_classes`，阶段 4 删掉推导块 | 1 / 3 / 4 |
| E3 | `_messages.html:239-240` 的 `data-message-recognition-result-recorded` / `-event-type-recorded` | 两个 data 属性是"结论有没有记下来"的判据，改字段后要重新定义 | 3 |
| E4 | `_messages.html:82-89` 的筛选按钮 + `static/app.js:267-276` `messageMatchesInsightFilter` | 现有筛选没有"按分类筛"。阶段 2 的人工核准要按分类捞样本，这里很可能要加按钮 | 2 |
| E5 | `authoritative_recognition.compare_assessments`（:265） | 被**提示词中心的草稿 A/B 测试**（`prompt_testing.py:151`）复用。不教它认 `message_classes`，页面上就看不出新旧提示词的分类差异——阶段 1 的调试全靠它 | 1 |
| E6 | `_messages.html:141-142`、`_strategy_lifecycle_timeline.html` | 消息卡与策略时间线的字段来源，跟着 E1/E2 走 | 3 |

### F. 测试

当前触及这些字段的测试文件：`recognition_result` 53 个、`lifecycle_event` 45 个、
字面量「是策略」36 个。阶段 1 只新增测试不改旧测试；阶段 3 改判据时这批会大面积变动，
**必须回到 9533 passed / 4 skipped 或更高**。

---

## 11. 退役清单（测试通过之后才做，写在这里是为了不被忘掉）

> 2026-09-24 的教训 2：**「删干净」靠测试验收，不靠关键词匹配。** 上次按「定义体含 v2 关键词」
> 整块删，把同一条 `import` 里的非 v2 符号和两个只是"提到 v2"的 fixture 一起带走了，
> 全套跑出 101 个失败。这次每一项都写明**前置条件**和**验收方式**。

| # | 退役对象 | 前置条件 | 验收方式 |
|---|---|---|---|
| R1 | `_messages.html` 的 `display_classification` 推导块（§10 E2） | 页面已改为直接读 `message_classes`，且观察窗里两者一致 | 页面快照测试 + 人工看一屏消息卡 |
| R2 | `authoritative_instructions._legacy_management_kind`（event_type → kind 映射） | 所有 `instructions` 都由模型直接给出，或由 `message_classes` 生成 | 全套测试 + 该模块的单测覆盖率不下降 |
| R3 | `requires_context_resolution` 里三个关键词常量及其 `CONTEXT_TRIGGER_ORDER` 条目 | 阶段 3 已部署并观察通过 | `grep -rn 'REVISION_LANGUAGE\|CANCELLATION_LANGUAGE\|ENTERED_HOLDER_LANGUAGE' src/ tests/` 为空（`apparent_entry_may_be_revision` 内部那份除外，届时已改名） |
| R4 | `recognition_experiments.MIMO_EXPERIMENT_STATUSES` 里的旧中文分类值（`入场确认`/`取消入场`/`离场信号`/`仓位管理`/`策略调整`） | 确认只有 `_upsert_experiment_result:1213` 一个读者 | 全套测试 |
| R5 | 提示词里 `recognition_result` / `lifecycle_event` 的输出块 | §10 D 组**全部**改读 `message_classes`，且 `validate_prompt_content` 的旧 marker 同步移除 | 发布一个不含旧字段的提示词草稿版本，在提示词中心跑 A/B 通过 |
| R6 | `_validate_authoritative_payload` 与 `compare_assessments` 里的旧字段分支 | R5 完成 | 全套测试 |
| R7 | `recognition_result = 识别失败` 这个旧值的双重含义（运行级故障 vs 读不出图） | R5 完成 | 专门的回归测试：HTTP 非 200 与「模型说图看不清」必须落在两个不同的记录位置 |
| R8 | 数据库里仍 active 的 `trading.analysis.mimo_v2_authoritative`（def=6） | 与本设计无关，代码已零引用 | 一条独立的数据库操作，**不要顺手混进本次改动** |
| R10 | 提示词里的【两套判据刻意不一致，不要自行调和】说明段 | `recognition_result` 的「新策略」判据已与 §3.1 统一，或该字段已退役（R5） | 提示词中心发布一份不含该段的草稿版本，A/B 测试里「只有止盈没有止损」的样本两边判断一致 |
| R9 | `docs/ARCHITECTURE.md` 与本文件 | 全部阶段完成 | 架构文档写明新契约是现状，本文件移入 `docs/archive/plans/` |

**退役的统一规矩**：每删一项，先跑该模块的聚焦测试，再跑全套；
不得用「文件里出现某关键词就整块删」的方式操作。

---

## 12. 验收基线

- 全套测试基线 **9533 passed / 4 skipped**，任何阶段的最终候选都必须回到这个数或更高。
- 本仓库多会话共用检出：只 stage 显式路径，**绝不 `git add -A`**。
