# 首次分析分类契约 · 实施状态

**设计稿（规格）**：`docs/plans/2026-09-24-first-pass-classification-contract-design.md`
**背景交接**：`docs/plans/2026-09-25-first-pass-classification-design-handoff.md`
**基线**：全套 9533 passed / 4 skipped
**阶段 1 最终候选**：`71143e2b043e65959791775744b9613c981912f3`（分支 `phase1-message-classes-shadow`，已变基到 `origin/main`）。
全套 `9623 passed, 4 skipped`（751.99s）。此前在 `634ccd20` 的等价提交上由指挥会话独立复跑，
结果一字不同：`9623 passed, 4 skipped`（755.10s）。
差额 +90 恰好等于本批次新增的 90 条用例（`tests/test_message_classification.py` 56 条 +
`tests/test_message_classes_shadow.py` 34 条），**一条既有用例都没有被改动或打破**。

> 跑全套两种写法都可以：`uv run pytest -q` 或 `uv run python -m pytest -q`，都收集 9627 条。
> 曾经只有后者能跑（前者在收集阶段报 3 个 `ModuleNotFoundError: No module named 'tests'`），
> 本批次把 `pyproject.toml` 的 `pythonpath` 从 `["src"]` 改成 `["src", "."]` 修掉了。
> 这条改动是代码，按部署规则不能单独上共享分支，所以挂在阶段 1 分支上随它一起部署。

> 归档的历史计划（`docs/archive/plans/`）里有多份已被取代的设计，**不要读**。

| 阶段 | 风险级别 | 状态 |
|---|---|---|
| 阶段 1 · 影子 | L1（additive dormant） | `completed`（代码 `5ca19513`，提示词 v9 = `ai_prompt_versions.id=10` 已发布，观察窗通过） |
| 阶段 2 · 观察与人工核准 | L0 | `in_progress`（四张表已出，2026-09-29；人工核准 0/44，等用户标注） |
| 阶段 3 · 切换 | L2（须用户单独批准后才能部署） | `planned` |
| 阶段 4 · 收口 | 以后 | `planned` |

**仓库层面的一件事（2026-09-24，与本设计无关但影响所有会话）**：
共享分支已从 `codex/deepcoin-auto-trading-v1` 改为 **`main`**（`4e526b61`，纯 fast-forward，
`AGENTS.md` 已同步）。旧分支名停在同一个 sha 上冻结，不再推送。
起因就是本批次：agent worktree 默认开在 `main` 上，落后 2920 个提交，要改的模块在那棵树上根本不存在。

---

## 阶段 1 · 影子（completed，本地）

**分支**：`phase1-message-classes-shadow`
**一句话**：模型多输出一个 `message_classes`，落库、回读、投影到页面，并与从旧字段推导出的
分类并排比对；**不影响任何行为**。

### 做了什么

| 设计稿条目 | 文件 | 改了什么 |
|---|---|---|
| §2–§4（契约本体） | `src/telegram_kol_research/message_classification.py`（新建） | 常量、`parse_message_classes`、`derive_message_classes`、`compare_message_classes`、`message_class_identities`。纯函数，零副作用，不 import 任何执行链路模块 |
| §10 A2 | `prompt_defaults.py` | `DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT` 加 `message_classes` 输出块与规则（§2.1/§2.3/§2.4/§3.1–§3.5 照抄）。输出 JSON 里它排在 `recognition_result` 之前。**旧字段一个没删** |
| §10 A3 | `prompt_composition.py` | `trading_shared` 档加 marker：`"message_classes"` / `"class"` / `"target"` / `"resolution"` + 五个中文枚举 + `exact`/`forthcoming`/`unknown` |
| §10 A4 | `recognition_experiments.py` `_validate_authoritative_payload` | 调用 `parse_message_classes`，**缺字段与违规都不抛错**。重试次数、模型链回退一律未动 |
| §10 B1 | `message_evidence.py` `normalize_mimo_evidence` | 把规范形态的 `message_classes` 与 `message_classes_violations` 写进 `normalized_evidence_json` |
| §10 B2 | `authoritative_recognition.py` `_load_current_mimo_evidence_result` | 重建 payload 时把这两项带回来 |
| §10 E1 | `web_queries.py` | 投影 `message_classes` / `message_classes_derived`（实时算，不落库）/ `message_classes_agrees` / `message_classes_violations` |
| §10 E2 | `templates/_messages.html` + `static/app.css` | 在 AI 结论区域下方并排显示「显式分类」与「推导分类」，不一致加可见标记。**第 156–175 行的 `display_classification` 推导原样未动**，卡片主标签不变 |
| §10 E5 | `authoritative_recognition.compare_assessments` | 加 `message_classes` 差异项（提示词中心草稿 A/B 测试复用它） |

### 明确没做（阶段 3 及以后）

- `requires_context_resolution` / `CONTEXT_TRIGGER_ORDER` / `REVISION_LANGUAGE` /
  `CANCELLATION_LANGUAGE` / `ENTERED_HOLDER_LANGUAGE`：未动。
- `_resolved_mimo_result` 的降级逻辑：未动。
- `context_resolution.py` 的 `terminal = attempt_number == 2`：未动。
- 设计稿 §10 D 组那 12 处执行链路读者：未动。
- 数据库 schema：未加列、未写迁移。
- 未删任何现有字段、常量、模块。
- **未部署、未碰生产数据库、未发布提示词版本。**

### 行为零变化的证据

`tests/test_message_classes_shadow.py::test_the_new_field_changes_neither_triggers_nor_instructions`
（6 个参数化用例）对同一条 payload，带与不带 `message_classes`，断言
`requires_context_resolution(...)` 与 `normalize_authoritative_instructions(...)`
的输出**完全相等**。参数里既有与旧字段一致的分类，也有**与旧字段矛盾**的分类
（`[闲话]` 对上 `是策略 + exit_position`），还有畸形值（`None` / `[]` / `"broken"`）——
任何读者一旦开始按新字段分支，这条就会红。

配套的 `test_a_management_message_keeps_its_triggers_when_classified_explicitly`
针对 §5 将来要用的那个形状（`resolution=unknown` vs `exact`）另断言一次：
阶段 1 不改写触发判据。

### 审阅发现并当场处理：提示词内部有两套「新策略」判据

**问题**：阶段 1 加进 `DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT` 的【消息分类】段要求
`新策略` **必须有止损价**（设计稿 §3.1），而同一份提示词里原有的【新开仓识别】段仍写着
「止损/止盈/无效价/保护价/分批止盈计划**至少有一个**」。一份提示词里出现了两条关于同一件事的
相反规则，**而且没有任何说明**。

**为什么不能把其中一边改掉**：改【新开仓识别】就会改变 `recognition_result`，
也就改变了哪些消息进入执行——那不是 L1，是 L2。阶段 1 必须让旧字段保持旧语义。
所以矛盾本身是**刻意的**，它的差值正是阶段 2 要量的那批「只有止盈、没有止损」的消息。

**为什么必须写出来**：不告诉模型这是刻意的，它很可能自行调和——要么把 `recognition_result`
收紧，要么把 `message_classes` 放松。无论哪种，阶段 2 的观测都会失真，而且**失真了也看不出来**。

**处置**：在两段之间插入【两套判据刻意不一致，不要自行调和】，明确写出
「上面那套只决定 `message_classes`，下面那套只决定 `recognition_result` 等既有字段」，
并给出那条典型消息的正确输出（`recognition_result = 是策略`，同时 `message_classes` 不含 `新策略`）。
种子内容经 `validate_prompt_content` 复验仍通过。

**这段说明本身是有寿命的**：等阶段 3/4 让 `recognition_result` 与新判据统一或退役，
它就必须一起删掉，否则会变成一条描述已不存在的矛盾的错误说明。
已登记为设计稿 §11 的 **R10**。

### 部署与观察窗（2026-09-25，L1 通过）

| | |
|---|---|
| 部署 sha | `08380650a136d28507cd105e09b85838a9ffacd7` |
| 回滚 sha | `6c7d9ebeeed9ac19dea7edcc9b2dfcdebe1def78`（`tg-deploy` 它即可回滚） |
| 自有分支 | `origin/claude/first-pass-message-classes-phase1` |
| 观察窗 | 2026-09-25 07:17:01 – 07:31:03 +0800，连续 15 分钟 15 个采样点 |
| 证据文件 | 服务器 `/root/phase1-observation.log`（采集脚本 `/root/phase1_observe.sh`，只读） |

部署前核对：候选是生产 HEAD 的后代；`pyproject.toml` 只动 pytest 配置，**无依赖变更**，
服务器不需要 `pip install`。部署后核对：生产 HEAD = `origin/main` = 部署 sha，
offenders 检查 `PASS: 0 code files beyond production`。

窗口结果：worker / ingest / web 全程 `active`；1 条真实消息（`raw_message_id 18918`）；
错误行计数 15 个采样点全程平稳，与部署前基线一致；日志里 `message_class` 出现 0 次。

**这一批要证明的那件事，被一条真实消息证明了**：18918 的识别决策
`非策略 / text / completed`，证据行 `version 1 / completed / gpt-5.6-luna` 由新代码写入，
而它的 `normalized_evidence_json` **不含** `message_classes`，权威 payload 的顶层键也只有
`confidence / entry_context / evidence / input_reading / lifecycle_event / reason /
recognition_result / strategy` 八个。也就是说：新的写入点与回读点都在生产上跑过了，
在 v8 提示词下**什么都没写、什么都没改**——正是设计要求的「缺字段记 missing 而不是违规」。
`/api/runtime/loop-health` 与首页均 HTTP 200，含改动模板的页面渲染正常。

**顺带发现的两处既有噪声**（速率跨越部署点完全未变，与本批次无关）：
- `Runtime incident capture failed open: type=source_deletion_exit_stuck ...
  error=RuntimeIncidentBoundsError`，约 2850 条/小时，最早样本在部署前 24 小时。
  这类运行时事件**根本没被记录**，是一个监控盲区，已单开任务跟踪。
- `recognition execution finding ... action=observe_only/observe_uncertain`，
  稳定 1170 条/小时，内容全是基线以下的历史消息，以 ERROR 级别打出观察结论。

部署后零 `Traceback`。

### 发布提示词版本时撞到的事实：生产 v8 比代码种子**旧**（2026-09-25，待用户决策）

准备发布带 `message_classes` 的新版本时，先读了生产库里真正在跑的那份，结果与设计稿 §2
的假设不符。设计稿说「在现有 `trading.analysis.shared` 输出对象里新增」，写这句话时参照的是
**代码种子**；生产跑的 v8 是另一回事。

**提示词版本历史**（`ai_prompt_definitions.id = 1`，只有四个版本）：

| version id | 编号 | 状态 | 发布时间 | change_note |
|---|---|---|---|---|
| 1 | 1 | superseded | 2026-07-13 | Initial registry seed |
| 6 | 2 | superseded | 2026-07-21 | Support explicit multi-target management |
| 7 | 3 | superseded | 2026-07-22 | 唯一归属的"求稳可走/稳健者可走"执行全平… |
| **8** | 4 | **published** | **2026-08-05** | Add reviewed entry preamble risk-multipl… |

**生产 v8 比改动前的代码种子少 1407 个字符**，缺的是：`instructions` 整块、
`entry_fragments`（【相邻入场消息片段】整节）、以及 `strategy: null` 的新写法。

**三件已核实的事实：**

1. **生产 v8 拿改动前的校验器就已经过不了**，缺 `"entry_fragments"` marker。
   那个 marker 是 v8 发布（2026-08-05）之后才加进 `validate_prompt_content` 的，
   而提示词从此没再发布过。校验只在保存/发布路径跑、不在渲染路径跑，所以它一直没被发现。
   **后果：任何以 v8 为基础的新版本都发不出去**，除非先处理这个 marker。
2. **`entry_fragments` 在生产输出里从来没出现过**：最近 939 条权威决策中 **0 条**含它
   （按主键索引范围查询，未做全表扫描）。代码里有完整的 `entry_strategy_fragments`
   消费链和「半仓 = 0.5 / 分腿 / 补仓」的设计，但**提示词从来没要求模型输出它**——
   这个功能在生产上是死的。
3. **`instructions` 出现 65/939（7%）**，但提示词里根本没提它。这些是**代码写的**，
   不是模型答的——`_resolved_mimo_result` 在上下文解析后会写 `payload["instructions"]`。
   这也解释了 `authoritative_instructions._legacy_management_kind`
   （从 `lifecycle_event` 倒推 kind）为什么是承重结构：生产的模型从来不直接给 instructions。

**因此发布这一步被挡住，等用户在三个方案里选一个**（详见当次对话）：
- **甲（最小、真惰性）**：v9 = v8 + 只加 `message_classes`；为此要处理校验器里那个
  生产从来没满足过的 `"entry_fragments"` marker。行为真零变化，立刻能拿阶段 2 的数据。
- **乙（顺带补齐 entry_fragments）**：会让模型第一次开始输出 `entry_fragments`，
  而它有真实消费者——**这是行为变化，不是 L1**，须按 L2 另行批准。
- **丙（全量对齐代码种子）**：同时引入 `instructions` 与 `entry_fragments` 要求，
  改动面最大，不在本次批准范围内。

无论选哪个，**「`entry_fragments` 为什么从未上线」都应单独立项**，它与本设计无关。

### 发布 v9：契约在真实模型上验证通过，但发布闸门卡在 DeepSeek 欠费（2026-09-25，待用户决策）

**已完成的步骤**（生产库，全部经应用自己的 API，未直接改表）：

| 步骤 | 结果 |
|---|---|
| 部署校验器改动 | `122bd712`，三服务 active，双向核对 PASS |
| 存草稿 | `ai_prompt_versions.id = 10`（v9，6704 字符 = 生产 v8 原文 + 只加 `message_classes`） |
| 校验 | `{"success": true, "errors": []}` |
| 历史对照测试 · mimo | `ai_prompt_test_runs.id = 18`，**completed**，实际模型 `gpt-5.6-luna` |
| 历史对照测试 · deepseek | `id = 19`，**failed**：`402 Payment Required`（api.deepseek.com） |

**契约本身已被真实模型验证。** 测试消息 `raw_message_id 18938`（BTC 做多、入场 84500-84800、
止损 82800、分档止盈）在 v9 下的输出是：

```json
"message_classes": [{"class": "新策略", "target": null}]
```

完全合契约：完整策略 → 单元素 `新策略`，`target` 为 `null`。同一条消息的
`recognition_result` 两边都是 `是策略`，**旧字段没有被新字段带偏**；
`compare_assessments` 报出的唯一差异就是 `message_classes`，说明 §10 E5 那处改动在生产上生效了。

**被挡住的原因**：`POST /api/ai-prompts/{key}/publish` 对 `category = trading` 的提示词要求
**mimo 与 deepseek 两个 model_kind 都有 completed 的历史测试**，且测试时的 active 版本组合与
当前一致。DeepSeek 账户 402，这一半永远拿不到 completed。

**`deepseek` 这个 kind 解析到哪**：`prompt_testing._deepseek_provider` 取
`batch_text_recognition` 链首，生产上是 `deepseek-v4-flash`。而按 `docs/ARCHITECTURE.md` §5.5，
`batch_text_recognition` **不在生产路径上**，只有 CLI / 批量工具会用。
也就是说这道闸门要求的第二个模型，是一个生产主路径根本不用的模型。
（另一个细节：`mimo` 这个 kind 实际解析到了 `gpt-5.6-luna`，即真正的权威链首，
所以两个 kind 的命名都已经与现实脱节，与 `reference_recognition_model_is_swappable` 记的命名债同源。）

**三个可选方向，等用户定**：
- **甲**：给 DeepSeek 充值。保留「两个独立厂商各读一遍提示词」这个闸门的原意，代价是花钱。
- **乙**：把 `batch_text_recognition` 链首改绑到一个可用模型（页面「AI模型选择」即可改）。
  不动闸门逻辑，仍然是两个模型独立读一遍；这个 stage 不在生产路径上，风险低。
  代价是 `deepseek` 这个 kind 名与实际模型进一步脱节。
- **丙**：改闸门，当某个 kind 没有可用提供商时不再强制要求它。改的是安全闸门本身，须谨慎。

### v9 上线后的第一个观察窗（2026-09-25 11:27–11:59，通过）

33 分钟，攒够 5 条带 `message_classes` 的决策后提前自停。证据在服务器 `/root/v9-observation.log`。

| 指标 | 结果 |
|---|---|
| 三服务 | 全程 `active` |
| 新消息 / 新决策 | 8 / 5 |
| 决策带 `message_classes` | **5 / 5** |
| **契约违规** | **0** |
| **识别失败** | **0** |
| 错误行 | 与部署前基线持平 |

**契约本身干净**：模型第一次输出这个字段就零违规，`图片不可读` 的前置条件没有被滥用
（5 条纯图片消息判为 `闲话`，说明模型读了图并作出了判断，而不是拿这个值当借口）。

#### 新字段立刻抓到了旧字段读不出来的东西

| 消息 | `message_classes` | 旧字段 | 原文摘要 |
|---|---|---|---|
| 19028 | `仓位管理 / unknown / ETH` | `非策略 / none` | 图片里提到「Bitget 在 2717 止损」 |
| 19029 | `仓位管理 / unknown` | `非策略 / none` | 「插针没有触发止损的伙伴可以继续持有，但是设置好止损位！」 |
| 19030 | `策略管理 / exact / 909 / ETH long` | `非策略 / none` | 「ETH 左侧单现在为右侧单，记得突破加仓，止损2660，止盈2780附近做第一止盈。」 |

三条都被旧契约读成「非策略 + 无事件」。19029 和 19030 是明确的管理动作，正是 2026-09-19
那类事故的形状。**19028 尤其值得记**：模型的 reason 写着「无法在当前候选策略集合中唯一关联
目标生命周期」，于是老老实实输出了 `unknown` 而不是猜一个——这正是契约想要的行为。

#### 但 `exact` 的可信度有上限，而且上限不在模型身上

19030 的 `exact / lifecycle_id = 909` 看着精确，查下去有问题：

```
909 | ETH | long | pending_entry | signal_at 2026-08-20 | expiry_review_requested
     「待入场策略已超过 3 小时，需要人工确认继续等待、标记过期或撤销交易所挂单。」
```

**一条一个多月前、早就被标记待人工确认过期的 pending_entry。** 今天的消息指向它，几乎可以肯定是错的。

但责任不在模型：那个群里活着的候选只有两条（909 ETH long、1207 BTC long entered），
**909 是唯一的 ETH long 候选**。模型在契约允许的集合里挑了唯一符合标的与方向的那一条，
它守了规矩。**问题在候选集合本身被污染了——全库有 42 条超过 7 天的 `pending_entry`。**

还有一点：19030 **没有触发上下文分析**（`context_resolution_attempts` 0 条）。
按现行判据它不含任何关键词、`event_type` 又是 `none`，所以今天这条消息的目标本来也没人复核。
新字段至少把「模型认为这是个有目标的管理动作」这件事显式记了下来。

**这条发现改变阶段 2 要量的东西**：不能只量「显式 vs 推导一致率」，必须单独量
**`exact` 的正确率**，以及候选集合的新鲜度。也影响阶段 3：设计稿 §5 打算在
`resolution == exact` 时**跳过**上下文分析，而 `exact` 的质量上限由候选集合决定——
在 42 条僵尸 `pending_entry` 还在集合里的前提下跳过复核，等于把污染直接放行。
**阶段 3 切换前必须先处理候选集合的过期收口**，这一条已写进设计稿 §8 阶段 3 的前置条件。

### 用户 2026-09-25 的三条指示与处置

用户在发布被 402 挡住时给了三条指示，前两条已批准执行方式，第三条另开一批。
**原话要点**：
> 你说的部署闸门，我是不同意的，因为项目还没完善，过早设置太多闸门，有些改动让原来思路
> 完全变了，闸门却还没跟着改，那么 agent 就要一直循环自证，无意义的无法通过的自证检验。
> …现在系统已经是可以选择切换不同的模型，为什么还留着一些 mimo 分析 deepseek 复核的字眼，
> 这以后还会继续造成误导。…分析复核已弃用。

#### 处置 1 · 发布闸门已删除（本批次完成）

`POST /api/ai-prompts/{key}/publish` 里对 `category = trading` 要求「mimo 与 deepseek 两个
model_kind 都有 completed 历史测试」的那段整块删掉。保留的是**读草稿本身**的检查：
内容校验、必填 change_note、draft/active 版本号的比较交换。
新测试 `test_a_trading_prompt_publishes_on_validation_alone` 证明：
中间不跑 `/test` 也能发布，而未校验仍然 409——删掉的是厂商闸门，没有放松其余部分。

#### 处置 2 · 提示词测试改成按 stage 取模型（**本地完成，2026-09-25，未部署**）

分支 `prompt-test-stage-models`（基线 `origin/main` `0dd05f25`）。L1：只改提示词中心的
只读对照测试路径，不碰识别/执行，也不改数据库结构。

**改成了什么。** 接口从 `model_kinds: ["mimo", "deepseek"]` 变成 `model_ids: [<模型 id>]`，
候选由 `prompt_testing.prompt_test_models()` 从 `ai_model_router.resolve_stage_chain`
取，默认链首，不传 id 就是链首。新增 `GET /api/ai-prompts/{key}/test-models` 给页面
提供候选（链首标 `is_default`），页面的两个厂商勾选框换成一个下拉。
删掉了 `_model_name` / `_deepseek_provider` 和那条独立的 httpx DeepSeek 调用分支——
现在两次对照都走 `_call_mimo_direct_model`，也就是 `authoritative_recognition`
生产用的同一个调用器。`_find_mimo_model` 本身留着（`recognition_experiments`
与每日探测仍在用），只是 `prompt_testing` 不再 import 它。

**stage 对应关系（已核实，不是照抄）。** 两个提示词都归 `authoritative_recognition`：
`recognition_experiments.py:510` 和 `:1283` 用 `compose_trading_prompt(model_kind="mimo")`
把 `trading.analysis.shared` + `trading.analysis.mimo_vision` 合成一条 system prompt
送进 `resolve_authoritative_chain`，也就是 `authoritative_recognition` 链
（`prompt_composition.py:56-59` 是合成处）。`trading.analysis.shared` 另外还被
`batch_text_recognition` 合成（`message_recognition.py:253`、`:2835`），但按
ARCHITECTURE §5.5 那个环节只有 CLI / 批量工具，**没有取它作候选**——拿生产不会
调的模型测交易提示词，正是「DeepSeek 复核」这个误导的来源。判断写进
`PROMPT_TEST_STAGE_BY_PROMPT_KEY`，一条用例断言两个键都指向它。

**图片限制换成能力判据。** 原来是 `set(model_kinds) != {"mimo"}`；现在是
`model.supports_image`。同一份配置里两个模型都绑在同一环节，只差这一个标志位：
能读图的通过（肯定式断言：返回该模型、endpoint 200、`model_id` 回显），
不能读图的被拒（`cannot read images`）。候选列表也过滤掉不能读图的，页面不会
先给出一个随后会被拒的选项。变异检验：同时关掉解析器里的能力检查和候选过滤，
这条用例转红（拒绝理由从 `cannot read images` 变成 `not a usable member`）。

**空链行为。** 环节没有可用模型（没绑、或 provider 被禁用/没填 base_url）→
`PromptTestModelError`（`ValueError` 子类，web 层照原样转 422），
**在写任何 `ai_prompt_test_runs` 行之前**就拒。这是行为变化：旧代码在这里回退到
字面量 `"mimo-v2.5"`，然后写一行 status=failed、model 指向没人配过的模型。
两条用例（模块级 + endpoint 级）都断言一行都没写、runner 一次都没被调。

**`ai_prompt_test_runs.model_kind` 这一列现在存 stage key**（`authoritative_recognition`），
不是模型 id。理由：同表已有 `model` 列存实际模型名，模型 id 与模型名信息高度重合；
而旧行里的 `mimo` / `deepseek` 本来就是「某个环节的链首」的意思
（分别是 `authoritative_recognition` 与 `batch_text_recognition`），存 stage key
就是把同一个事实说准，而且换绑模型后它仍然为真。`String(32)` 装得下
（`authoritative_recognition` 25 字符）。**列没删**（删列是 L3），列名的债留在原处，
新含义写在 `models.py` 该列的注释里。

**文案。** `_ai_prompt_center.html` 的「DeepSeek = A + C / MiMo = A + B + C」改成
「送给模型的 = A + B + C」加一行「权威识别环节当前绑定的模型，换绑后自动跟随」；
`index.html` 与 `ai_recognition_config.py` 的 `tag=` 从「DeepSeek / …」「MiMo / …」
改成「批量文本识别 / …」「权威识别 / …」，即按环节而不是按厂商命名。
`test_web_page_render.py` 顺带加了两条反向断言，钉住这两个厂商串不再出现在页面上。
`app.css` 里那两个勾选框用的横排规则改成竖排（下拉 + 一行来源说明）。

**明确没做**：没删 `model_kind` 列；没恢复发布闸门（处置 1 已删）；没碰
`semantic_review`（处置 3）；没碰首次分析契约 / `message_classes` / 候选集合过滤；
未部署、未动生产库、未发布提示词版本。
`system_operator_bot.py:1469/1479` 的「权威结果: MiMo / 复核结果: DeepSeek /」
也是厂商字眼，但属于分析复核的通知文案，留给处置 3。

**测试。** 全套 `9666 passed, 4 skipped`（基线 9648/4，净增 18 条，没有新的 skip）。
新增/改写的用例分布：`tests/test_prompt_testing.py` +11、
`tests/test_web_prompt_registry.py` 2 条改成 9 条、`tests/test_ai_stage_routing.py`
把 `test_the_prompt_centre_deepseek_test_follows_the_batch_text_chain` 换成
`..._follows_the_authoritative_chain`、`tests/test_web_page_render.py` 换断言。
两项变异检验都转红：关掉能力检查 + 候选过滤 → 图片用例红；
把默认值钉成某个名字而不是链首 → 换绑用例红。

**留给下一个读者的两个观察。**
1. `templates/index.html:89` 那句 `<label>` 文案是乱码（`鎻愮ず璇?`，本该是「提示词」），
   在本批改动之前就在，属于编码事故不是命名债，没动它。
2. `mimo_direct_prompt` 这个配置字段名、`trading.analysis.mimo_vision` 这个提示词键、
   `_call_mimo_direct_model` 这个函数名都还带 `mimo`。改字段名要动配置文件，
   改提示词键要动生产库里的定义行，都不是本批的 L1 范围。

波及面（原清单，全部处理完）：

| 位置 | 内容 |
|---|---|
| `prompt_testing.py` | `model_kind` 参数、`_model_name`、`_deepseek_provider`、`_find_mimo_model`、`_call_configured_model` |
| `web_app.py` | `/api/ai-prompts/{key}/test` 的 `model_kinds` 校验；`MIMO_VISION_PROMPT` 那条 kind 限制 |
| `models.py` / `db.py` | `ai_prompt_test_runs.model_kind` 列（**留列，删列是 L3**） |
| `templates/_ai_prompt_center.html:11-12,63-64` | 「DeepSeek = A + C」「MiMo = A + B + C」说明行，以及两个 kind 勾选框 |
| `templates/index.html:103,118,133,138` | 提示词标签「DeepSeek / 文本策略识别」等 |
| `ai_recognition_config.py:413,420,427` | `AI_PROMPT_DEFINITIONS` 的 `tag=` 文案 |
| `static/app.js` | `promptCenterState.tested`、model kind 勾选框读取 |

#### 处置 3 · 分析复核整条退役（**单独一批，已批准**）

生产状态：`trading_settings` 里**没有** `semantic_review_enabled` 行，取代码默认 `False`——
生产是关着的，与用户说的一致。

规模（已核实，供下一个会话估工）：

| 类别 | 清单 |
|---|---|
| 专用模块 | `semantic_disagreement_review.py`（1312 行）、`semantic_review_control.py`（427 行） |
| 引用它的生产模块（15 个） | `ai_endpoints` `ai_recognition_config` `ai_stage_catalog` `authoritative_execution_attempts` `authoritative_recognition` `cli` `prompt_composition` `prompt_defaults` `recognition_decisions` `recognition_execution_scanner` `trading_settings` `web_app` `web_queries` + 两个专用模块自身 |
| worker 单例 | `RUNTIME_ROLE_SINGLETON_TASKS["worker"]` 里的 `semantic_review` |
| AI stage | `semantic_review`（`ai_stage_catalog`、`config/ai_recognition.yaml`） |
| 提示词 | `trading.disagreement.semantic_review`（代码种子 + 生产库里的定义行） |
| 设置项 | `semantic_review_enabled` |
| 页面 | `index.html:299`「开启 DeepSeek 辅助复核」、`_messages.html:640`「DeepSeek辅助复核」、`app.js`、`app.css` |
| 测试 | 30 个文件（`grep -rl "semantic_review\|semantic_disagreement" tests/`） |
| 数据库 | `recognition_decisions` 上的复核相关列**留着**，删列是 L3 |

**验收方式按 2026-09-24 的教训**：靠测试验收，不靠关键词匹配。MiMo v2 那次按
「定义体含关键词」整块删，把同一条 `import` 里的无关符号和只是「提到」它的 fixture 一起带走，
全套跑出 101 个失败。

### 落地时的自主判断（设计稿未写明的地方）

1. **缺字段 vs 显式 `null`。** §2.1 说「缺字段 = 违规」，§8 又说阶段 1 必须容忍没有新字段的
   旧提示词版本。取法：**键不存在** → `present=False`，**零违规**（这是回滚到 v8 的场景）；
   **键存在但不是数组（含 `null`）** → `present=True` + `classes_not_a_list`。
   理由：只有「键不存在」才是提示词回滚，`null` 是新提示词下的真实违规。
2. **`image_quality` 缺失或为空时不判 `图片不可读` 违规。** §3.5 的静态判据只给了
   `image_quality = none` 这一格；读不到这个字段时无法证明「消息没带图」，按「最少改变语义」
   选择不判违规。
3. **`strategy` 全字段 null 视同 `null`。** 现有共享提示词自己写着「旧的 strategy 全字段 null
   形式也可接受」，所以 §2.4 的「必须为 null」按这个既有约定解释，否则会把生产上一直在用的
   兼容形态判成违规。
4. **`strategy` 四项按字段分别给违规码**（`strategy_missing_symbol` / `_side` / `_entry` /
   `_stop_loss`），而不是一个合并码。`take_profit` 不在其中（§2.4 允许为 null，§3.1 只把止损
   定为硬判据）。
5. **`class_order_violation`。** §2.1 写了「管理类在前、新开仓在后」但没说违反算什么。
   按「列表约束」逐条校验的要求把它也列为违规码。
6. **`lifecycle_id_outside_candidate_set` 做成可选参数。** §6 把它算进契约违规，但候选集合不在
   payload 里。`parse_message_classes(payload, allowed_lifecycle_ids=...)`；不传就不查这一条，
   而不是默认判所有 id 都错。**阶段 1 的两个生产调用点都没传**（识别侧与落库侧），
   所以这条规则目前只在测试里被触发。
7. **比对忽略 `symbol` / `side`。** 任务书要求的，落在 `MessageClassTarget.identity()`：
   §2.3 允许证据不足时不填，填不填不能算分类分歧。比对按**多重集**做，顺序不算分歧
   （顺序是 §2.1 的契约规则，由解析器管）。
8. **`compare_assessments` 的「两边都没有」不算差异。** 旧提示词版本根本不回答这个问题，
   把它算成 disagreed 会让所有历史消息在页面上变红。只有一边有、或两边不同才记差异。
9. **页面并排行始终渲染**（只要投影里有这几个键）。显式侧缺失时显示「未输出（旧提示词版本）」，
   而不是整行隐藏——否则「模型这次没给分类」和「这条消息没走新提示词」在页面上长得一样。

### 已核实的两个高风险点

**(1) 加 marker 会不会让生产已激活的 v8 提示词在运行时被拒？——不会。**
`validate_prompt_content` 的 `trading_shared` 档只在三处跑：
`POST /api/ai-prompts/{key}/validate`（页面上的校验按钮）、
`publish_prompt_draft` 读取草稿**已记录的**校验结果、
`render_registered_prompt`（只被 `strategy_alerts` 用，档位是 `strategy_alert`）。
首次分析走的是 `compose_trading_prompt` → `resolve_active_prompt`，**中间没有校验**。
`tests/test_message_classes_shadow.py::test_the_validation_gate_is_not_on_the_render_path_of_the_active_prompt`
把这一点钉住了。

**(2) §10 B3 的 `changed` 比较会不会造成重复写入或误重置 `comparison_status`？——不会。**
`changed` 只决定 `preserve_completed_review`（是否保住已完成的 auxiliary 复核）；
`comparison_status` 在 `changed` 为真或为假时**都**被写成 `execution_pending`，这是既有语义，
与新字段无关。新字段本身是惰性的：payload 相同就相同，不同才不同
（`test_adding_the_field_does_not_change_whether_two_payloads_compare_equal`、
`test_an_unchanged_payload_carrying_the_new_field_is_still_unchanged`）。
规范化是幂等的（`test_the_stored_form_is_idempotent_under_a_second_parse`），
所以重放路径反复保存不会来回翻转。
**注意**：重建 payload 与实时 payload 本来就不相等（重建丢掉 `instructions` / `input_reading`
等键），这是改动前就有的性质，新字段没有让它变坏。

**(3) `compare_assessments` 加差异项会不会改变生产的 `agreement_status`？——不会。**
全仓只有一个调用者：`prompt_testing.py:151`（提示词中心的草稿 A/B 测试），
它把差异写进 `ai_prompt_test_runs.differences_json`，不碰 `recognition_decisions`。
生产的权威/辅助比对路径**不**经过这个函数。

### 提示词版本的发布顺序（阶段 2 之前要做的事，尚未做）

设计稿 §8：**先部署代码，再发布提示词版本**；回滚只需把提示词退回 v8，不必回滚代码。
本批次只交付代码与种子，**没有**在生产数据库里发布任何提示词版本。

### 校验器缺陷修复部署：v9 下 20% 的 gpt 调用被自己的校验器拒收（2026-09-27 01:32:14 UTC，部署 `e1d29708`）

- **本线的所有测量必须以 2026-09-27 01:32:14 UTC 为界，分前后两段统计。**
- 部署 sha：`e1d29708012720db1134d67caeeb286425f04f55`；回滚点：`ba93788a`。
- 部署由识别时延评估会话执行，用户在该会话中亲自确认。
- 完整分析见 `docs/plans/2026-09-26-recognition-latency-and-jev-assessment.md` §7，在分支 `claude/recognition-latency-jev` 上。

**缺陷是什么。** 本线的 v9（`ai_prompt_versions.id = 10`；`prompt_versions_json` 中 `trading.analysis.shared` 记为 10）要求「没有 新策略 元素时 strategy 必须为 null」。但 `recognition_experiments._validate_authoritative_payload` 仍要求 `strategy` 必须是 dict，模型照契约输出 null，就被判为 `MiMo response missing strategy`。设计稿 §10 A4 本来规定阶段 1「缺字段与违规都不抛错」，这一处是阶段 1 漏改的。

**规模。**

| 版本 | gpt-5.6-luna 调用 | 其中「missing strategy」 |
|---|---|---|
| v8 | 2 229 | 0 |
| v9 | 301 | 61（20%） |

此外 mimo-v2.5 在 v9 下 79 次调用中也有 8 次。

被拒的调用先在同模型上重试一次，约 28 s，再回退到 mimo-v2.5，又要 35–100 s。回退的结果全部是非策略（66 条 none、3 条 position_update、1 条 exit_position），所以**「识别失败」计数没有反映出来**。

第一次失败发生在 2026-09-25 03:56 UTC（北京时间 11:56），正好落在本线 v9 首个观察窗之内。那次观察窗的「识别失败 0」是真实的，但它漏看了 attempt 行上的失败。

**对本线数据的影响。**
- 部署前：v9 下约 20% 的非新策略消息，是由 **mimo-v2.5 回退作答**的；它们的 `message_classes` 来自 mimo，不是链首的 gpt。
- 部署后：这部分改由 gpt-5.6-luna 作答。
- 因此按模型、按时延、按分类一致率做的任何统计，都要按上面的时刻分段。部署前那段里的「gpt 分类样本」天然偏向没被拒收的那 80%。

**改了什么、没改什么。**
- 校验器：`是策略` 时仍要求 `strategy` 是 dict；非策略或识别失败时，接受 `null` 或缺键。其他类型（字符串、列表）照旧拒收。
- **不改写 payload**，`parse_message_classes` 看到的仍是原样的 null。
- 另一个提交：HTTP 5xx 且链上还有备用模型时，不再做同模型重试，直接回退。
- 提示词、契约语义、合同违规的同模型重试策略（本线阶段 3 的事）**都没动**。

**L1 观察窗（2026-09-27 01:35–01:50 UTC，满 15 分钟，通过）。**
- 三服务全程 `active`。
- 新消息 2 条（raw 19282、19283），都是 v9 形态：`非策略`、`message_classes = [闲话]`、**`strategy: null`**。两条都由 gpt-5.6-luna **一次请求**答完，耗时 11.0 s 和 12.5 s。
- 「missing strategy」新增 0 条，失败 attempt 0 行，没有新的 error_code，没有回退。
- 这两条在修复前都会被拒收并回退到 mimo，所以修复路径已被真实消息走通。
- 窗口内没有遇到 502，5xx 跳过重试这一支在生产上尚未被触发，由测试覆盖。

**遗留，交本线决定。** `message_evidence` 写 `normalized_evidence` 时把 `strategy: null` 归一化为 `{}`，这是既有行为。于是回放路径重建出的 payload 是 `{}`，实时路径是 `null`。阶段 3 如果要在回放路径保留 null 语义，需要另行处理。

---

## 阶段 2 · 观察与人工核准（in_progress）

产出四张表：显式 vs 推导 的一致率、`forthcoming` 的全部样本、多元素消息的全部样本、
决策 2（新策略必须有止损）影响到的历史消息清单。人工核准复用现有
`message_recognition_labels`，**不加数据库列**。

### 2026-09-29 第二次测量（四张表已出）

完整表格、逐条清单与人工核准清单见 **`docs/plans/2026-09-29-first-pass-phase2-observation.md`**。

- **数据**：v9 发布（09-25 03:26:34Z）至 09-29 13:01:42Z，共 814 条决策。
  - 快照 486 490 112 B，sha256 `d5a5d026…a3daced86`，`quick_check ok`。服务器副本和本机副本都已删除。
  - 窗口内只有 v9 一个提示词版本。
- **口径**：拿显式分类和**首轮**旧字段推导出的分类比，不和最终 payload 比，因为上下文降级只改写旧字段、不改写 `message_classes`。
  - 按 01:32:14Z 分成前段 314 条、后段 500 条，前段有 24% 由 mimo 回退作答。

| 表 | 前段 | 后段 | 合计 |
|---|---|---|---|
| 一 · 类别级一致率（严格口径数值相同） | 307/314 = 97.8% | 472/499 = 94.6% | 779/813 = 95.8% |
| 一 · 其中**含管理类**的消息 | 49/56 = 87.5% | 81/108 = 75.0% | 130/164 = 79.3% |
| 二 · forthcoming | 1 | 0 | 1（19016，预告的策略没有出现） |
| 三 · 多元素消息 | 2 | 14 | 16（占不一致的 35%；「离场 + 反手」0 例） |
| 四 · 决策 2（v9 窗口） | 0 | 0 | **0**。全历史 38/1 040，8 月 4 日以后没有一条建出生命周期 |

**34 条不一致方向完全一致**：全部是显式分类认出了管理动作，而旧字段是闲话，或者旧字段只装下了其中一个目标。归因如下：
- A1：说不出目标，旧字段降成 none，19 条；
- A2：对未入场策略改参数，旧契约没有对应事件，5 条；
- A3：forthcoming，1 条；
- E：单事件装不下多个目标，8 条；
- D：推导函数不认 `targets[].lifecycle_id`，1 条，是阶段 1 的代码缺陷。

反向不一致和新策略分歧都是 0。

**新发现，交阶段 3 处理**（观察文档 §6）：
- F1：多币种 unknown 被误判为 `duplicate_class_target`，7 条。
- F2：`strategy_required` 用最终 payload 校验造成误报。
- F3：推导函数的键名缺陷。
- F4：网页「分类不一致」按最终 payload 比，多标约 80 条。
- F1、F3、F4 行为零变化，可以提前做。
- exact 目标在消息时刻仍活着的比例为 51/61 = 83.6%。第一次测量是 4/8。

**更正时延报告的一处解读**：`entered_holder_language` 单独触发、首轮可执行的 30 行里，所谓「8 次改变或重定目标」的上下文目标**全部与首轮相同**（置信度 0.90–0.99），是确认，不是纠正。真正结果有变化的只有 6 条消息：18032、18375、18555、18843、18893、18895。见观察文档 §7.2。

**人工核准（未完成）**：
- 清单：A 组 38 条（不一致 + 多元素 + forthcoming），B 组 6 条（上面那 6 条回放样本）；C 组 10 条可选。
- 入口：网站消息页的「管理类 / 目标未知」筛选。
- 标注约定：判定针对显式分类；备注第一个词写 `显式对 / 推导对 / 都对 / 都错：应为 X`。
- **标完这两组，阶段 2 即可改为 `completed`。**

**待用户决定**：
- A1 这类「继续持有」「预期加仓」的不动仓管理，目标 unknown 时要不要触发上下文（观察文档 §8 问题 1）。
- 建议等人工核准结果出来再定。

**排期结论**（观察文档 §10）：
- **(a) 阶段 3**：硬前置「候选集合过期收口」已于 09-25 满足（`e08ac174`）。它管不到 72 小时内的终结目标，所以 exact 的两条补充判据须随阶段 3 一起实现。
  - 瓶颈是人工核准。标得快的话，最早约 10-03 部署，现实约 10-06。
- **(b) 过渡触发门**：**不先做**。一周只省约 40 次调用，却要多一次部署和一条测量界线。若 10-09 阶段 3 仍未部署，再改为先做。
- **(c) entry_fragments 的两项改动**：**可以开始**。本线数据已经取完。
  - ① 发布新提示词版本时，必须以生产 v9（id=10）为底；
  - 发布时刻要记到本文件，作为第二条测量界线；
  - ② 的数据清理对本线零影响。

## 阶段 3 · 切换（planned，须用户单独批准后才能部署）

① 触发判据换成 §5（删 3 留 4）；② 降级不再抹平首次分析；③ 契约类失败不重问。

**切换前的验收回放项（2026-09-27 追加）。**

- 出处：识别时延报告 `docs/plans/2026-09-26-recognition-latency-and-jev-assessment.md` §9，分支 `claude/recognition-latency-jev`，提交 `63646d60`。
- 数据口径：首轮结论取自 `context_resolution_attempts.request_summary_json.mimo_first_pass`，统计范围是 2026-09-16 方案甲生效以来。
- 调度会话裁定：上下文触发收紧（时延报告 §8.3）**并入本阶段，不单独做**。过渡门是否先做，等 09-29 派发阶段 2 时，看本阶段的预计时间再定。

1. **`cancellation_language` 删除前须回放 raw 18602。**
   - 该判据 14 天只触发 3 次，其中 1 次是真实抓回：raw 18602「……先取消先观望……」首轮为不可执行，上下文判为 `cancel_thread`，置信度 0.93。
   - 它是仅有的两个在首轮不可执行时真正抓回过动作的判据之一，另一个是 `text_image_conflict`（raw 17368），本阶段保留。
   - 验收要求：用 v9 契约重跑 18602 这类消息，确认会被判为「策略管理 + `resolution=unknown`」、由新判据接住。**接不住就保留 `cancellation_language`。**
2. **`entered_holder_language` 删除前须人工标注首轮可执行时的单独触发样本。**
   - 09-16 以来，该判据单独触发、且首轮可执行的有 30 次。
   - 其中 **8 次**改变或重定目标，**3 次**压下首轮动作，17 次 exhausted，2 次 reanalysis_capped。
   - 那 11 次有结果变化的调用，删除后要靠新判据接住（`unknown`，或「`exact` 目标不在候选集合内」）。
   - 它们里面哪些是纠正、哪些是 2026-09-24 米娅 msg 696 式的误覆盖，**尚未人工标注**。
   - 验收要求：重做快照取出这 11 条的 raw_message_id，逐条人工标注，再用新判据回放。时延报告当时的分析快照已按规定删除，所以样本 id 需要重新取。
3. **`apparent_entry_may_be_revision` 换掉触发判据时，必须保留 2a 的收窄条件（只算 `pending_entry` 的重叠），并回放 raw 19481。**
   - 出处：`docs/plans/2026-09-28-chen-btc-expired-repost-and-queue-block-design.md` §2.3、§5；实现提交见本仓库 `git log` 中对应的 `fix:` 提交（`src/telegram_kol_research/authoritative_recognition.py` 的 2a/2b 改动）。
   - 背景：陈哥群 `-1002337721508` 09-25 14:07:58 的 raw 19073（lifecycle 1327、strategy_thread 696）从未成交，于 09-26 01:32 过期。09-28 02:58:35 raw 19481 逐字重发同一条策略；候选生成器只因入场区间重叠就给 696 打了 `overlapping_entry`，命中 `apparent_entry_may_be_revision`，上下文判 `manage_thread → 696` / `management_action=null`，首轮「是策略」被抹成「非策略」，静默漏单。
   - 2a 把 `overlapping_entry` 收窄成只统计 `pending_entry`（还能被改单）的候选；`expired` 已经终态，不存在「新策略其实是在改它」这种可能。阶段 3 重写触发判据时，这个收窄条件必须原样保留，不能被 §5 的判据整理覆盖掉。
   - 验收要求：用阶段 3 的新判据集合重放 raw 19481，必须得到「是策略 → `new_thread`」（不再经过 `manage_thread` 降级），且不再触发 `apparent_entry_may_be_revision`。


## 阶段 4 · 收口（planned）

`recognition_result` / `lifecycle_event` 双轨是否退役、`识别失败` 旧值的两种含义怎么拆干净。
退役清单见设计稿 §11。
