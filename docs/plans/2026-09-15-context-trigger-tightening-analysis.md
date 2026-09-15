# 上下文二次判断触发条件：生产数据分析与收紧方案

日期：2026-09-15
状态：用户 2026-09-15 拍板方案甲，实施中（子代理）
数据来源：生产库 `data/research.db` 只读查询，窗口 2026-09-01 13:03 UTC → 2026-09-15（14 天），
表 `context_resolution_attempts` × `recognition_decisions` × `raw_messages`。
分析脚本在指挥会话 scratchpad（`ctx_audit.py` / `ctx_audit2.py` / `ctx_audit3.py`），一次性使用，不入库。

## 1. 结论先行

- 14 天 1902 次上下文调用，只有 158 次（8.3%）产出了可执行动作（manage / exit / cancel / revise，置信度 ≥ 0.7）。
  与用户「100 次里不到 10 次」的体感一致。
- 消耗 5354 万 token，其中 4871 万（91%）花在没有改变结果的调用上。单次平均 2.8 万 token（请求体中位数 74 KB）。
- **一个信号占了绝大部分浪费**：`multiple_same_source_candidates`（同来源多个候选策略）。它只看
  「这个来源当前有 ≥ 2 条活跃候选线程」，与本条消息是不是策略毫无关系。活跃群里几乎每条消息都命中
  （1798 / 1902）。
- **判定缺的是一道「本条消息可执行吗」的前置门**：第一次识别已经给出 `recognition_result` 与
  `lifecycle_event.event_type`。当两者都是「非策略 / none」时，上下文再怎么看也几乎不会变出动作
  （1611 次里只有 12 次改变了结果，0.7%）。

## 2. 数据

### 2.1 按第一次识别的结论分组

| 组 | 第一次识别 | 调用 | 产出动作 | 命中率 | token | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| A | 非策略 且 event=none | 1611 | 12 | 0.7% | 4507 万 | 其中 1373 次**仅**由「同来源多候选」触发（3874 万 token，4 次有用，546 次消息正文为空＝纯图片/媒体） |
| B | 非策略 但有事件（position_update 113 / exit_position 63 / cancel_entry 7 / entry_confirm 5） | 188 | 146 | 78% | 558 万 | **这是上下文二次判断真正的用武之地**：第一次识别看出了管理动作但定不到目标线程 |
| C | 是策略 | 98 | 1 revise | 1% | 274 万 | 93 次 new_thread＝结果不变；4 次被降级为 hold/unresolved（见 2.4） |
| D | 其他（识别失败等） | 5 | 0 | – | 15 万 | |

### 2.2 按触发信号（一次调用可有多个信号）

| 信号 | 次数 | 主要落在 |
| --- | --- | --- |
| multiple_same_source_candidates | 1798 | A 组 1553 |
| entered_holder_language | 164 | A 组 80、B 组 64 |
| management_without_exact_target | 159 | A 组 106（event=none 却命中，见 2.5）、B 组 49 |
| revision_language | 47 | A 组 38 |
| apparent_entry_may_be_revision | 43 | C 组 29 |
| text_image_conflict | 30 | A 组 24 |
| cancellation_language | 3 | |
| reply_target_disagreement | 1 | |

### 2.3 A 组里被上下文「救回来」的 12 条

| attempt | 信号 | 决策 | 消息 |
| --- | --- | --- | --- |
| 4446 / 4614 / 4899 | entered_holder + 多候选 | revise_thread | 「ETH 睡觉挂单多 挂2367 100倍 2%保证金…」——这其实是**入场策略**，第一次识别漏判成非策略 |
| 5209 / 5358 / 5512 | apparent_entry_may_be_revision（+revision_language） | revise_thread | 「比特币 方向：做空 入场：8.05万…」——同上，第一次识别漏判 |
| 5210 | apparent_entry_may_be_revision | new_thread | 同一条消息的另一次 |
| 5852 | text_image_conflict + 多候选 | manage_thread | 正文为空的图片消息 |
| 4856 / 6003 / 5815 / 6165 | **仅**多候选 | manage / exit | 「轻仓介入」「7.85万到喽 可以考虑先走一部分」「平仓出来」「不反弹那就止损出来」——真正的管理指令，第一次识别没给事件 |

前 7 条的本质是第一次识别漏判入场策略，上下文只是碰巧兜住了。它们都带**措辞类**信号
（entered_holder / apparent_entry / revision / text_image_conflict），不依赖「多候选」。
只有最后 4 条是纯靠「多候选」兜住的（1373 次里 4 次，0.3%）。

### 2.4 C 组（第一次识别＝是策略）被上下文降级的 4 条

| attempt | 决策 | 置信度 | 上下文给的理由 | 评价 |
| --- | --- | --- | --- | --- |
| 6000 / 6001 | hold | 0.8 / 0.9 | 与线程 559 参数完全相同，是重复转发 | **合理**：避免重复开仓 |
| 5994 | unresolved | 0.5 | 与线程 557 高度匹配但止损不一致 | 存疑：把一条完整新策略挡掉了 |
| 6014 | unresolved | 0.7 | 与候选线程参数部分相同 | 存疑，同上 |

四条都带 `apparent_entry_may_be_revision`。这一组的价值是**去重**而不是「找目标」，保留该信号即可。

### 2.5 其他发现

- `management_without_exact_target` 在 A 组（event=none）命中 106 次：代码判断是 `event_type != "none" and target is None`，
  但统计用的是决策表里**上下文改写后**的 first_pass 快照，改写会把事件清成 none。也就是说这 106 次
  第一次识别其实给了事件、只是目标为空——它们本应算 B 组，属于合理调用。这不影响结论：
  A 组 1373 次「仅多候选」是干净的浪费。
- 546 次仅多候选调用的消息正文为空（纯图片、媒体、贴纸）。第一次识别已经看过图并判为非策略。
- 同一条消息重复分析：raw_message 14636（「第二止盈位到了」）14 天内被分析 19 次，
  另有多条 5–6 次。来源是 unresolved 行的「下一条同群消息」重分析触发。总量 75 次，占比小，先不动。
- 上下文模型 14 天内 1897 次是 `mimo-v2.5`，只有 5 次 `gpt-5.6-luna`——上下文环节的模型链仍以 MiMo 为主，
  与权威识别环节已切到 gpt-5.6-luna 不同步。是否要换，属于另一个决定。
- 单次请求体中位数 74 KB、p90 96 KB（约 2.7 万 prompt token）。降频之外，缩小上下文窗口是第二个杠杆，本文不展开。

## 3. 方案

只改 `authoritative_recognition.requires_context_resolution`（`authoritative_recognition.py:194-263`）的组合规则，
8 个信号本身的检测逻辑不动。

先定义「本条消息可执行」：

```
actionable = recognition_result == "是策略" or lifecycle_event.event_type != "none"
```

### 方案甲（推荐）：结构信号只在可执行时生效

- `multiple_same_source_candidates` 仅在 `actionable` 时计入。
- 措辞类信号（entered_holder / revision / cancellation / apparent_entry / text_image_conflict / reply_target_disagreement）维持现状。
- `management_without_exact_target` 本身就要求有事件，不变。

| 指标 | 现状（14 天） | 方案甲预估 |
| --- | --- | --- |
| 调用次数 | 1902 | ≈ 529（−72%） |
| 有用调用 | 158 | 154（丢 4 条纯靠多候选兜住的管理指令） |
| 命中率 | 8.3% | ≈ 29% |
| token | 5354 万 | ≈ 1480 万（省 3870 万） |

代价：2.3 节最后 4 条那类「第一次识别没给事件的口语化管理指令」不再被兜住。
它们的正确解法是让第一次识别给出事件（提示词问题），不是靠上下文兜底。

### 方案乙（激进）：所有信号都要求可执行

- 任何信号都只在 `actionable` 时计入。

| 指标 | 方案乙预估 |
| --- | --- |
| 调用次数 | ≈ 291（−85%） |
| 有用调用 | 146（丢 12 条，含 7 条第一次识别漏判的入场策略） |
| 命中率 | ≈ 50% |
| token | ≈ 820 万 |

代价多丢的 8 条里有 7 条是**入场策略被第一次识别漏判**，上下文本来是最后一道网。
除非先确认 gpt-5.6-luna 作为第一次识别模型不再漏判这类「睡觉挂单」格式，否则不建议。

### 不建议的做法

- 直接删掉 `multiple_same_source_candidates`：B 组 188 次里 162 次带它，其中很多是它**唯一**的信号
  （例：4345、4374–4381、4501、4623）。删掉会伤到最有价值的那一组。

## 4. 实施要点（拍板后交子代理）

- 改 `requires_context_resolution`：在计算 `reasons` 后、组装 `ordered` 前，若 `not actionable`，
  从 `reasons` 里去掉 `multiple_same_source_candidates`（方案甲）或清空（方案乙）。
- 现有测试：`tests/test_context_resolution*.py`、`tests/test_recognition_authority_architecture.py`、
  `tests/test_recognition_context_gate.py` 里对 `requires_context_resolution` 的断言要跟着改；
  新增用例：非策略+无事件+多候选 → 不触发；非策略+有事件+多候选 → 触发；是策略+多候选 → 触发；
  非策略+无事件+entered_holder → 仍触发（方案甲）。
- 部署后页面统计行的触发原因分布会直接反映效果；「未执行：未命中触发条件」的卡片应明显增多。
- 影子判定（`context_resolution_shadow.py`）是独立的对照逻辑，不改；它会把「权威没触发、影子认为该触发」记成分歧，
  部署后一周看 `shadow_would_extra_trigger` 的分布可作为方案甲是否过紧的校验。

## 5. 顺带记录、本次不做

- 上下文环节模型仍是 MiMo v2.5 为主（2.5 节）。
- unresolved 行的重分析对同一消息最多可达 19 次（2.5 节），可加「同一消息重分析上限」。
- 请求体 74 KB 中位数，上下文窗口可能过大。
- 第一次识别对「ETH 睡觉挂单多 挂2367…」「比特币 方向：做空 入场：…」这类格式漏判为非策略（2.3 节），
  应作为识别提示词的回归样本。

## 6. 实施记录

实施日期：2026-09-15。基线提交 `1a6cac74`；本节所在提交即方案甲的实施提交，
提交标题 `feat(recognition): gate the multiple-candidate context trigger on actionability`
（提交自身的 SHA 无法写进自己的内容，由指挥会话在合入记录里补齐）。

### 6.1 代码改动

只有一处逻辑改动，在 `src/telegram_kol_research/authoritative_recognition.py`
的 `requires_context_resolution` 里，位置在 `apparent_entry_may_be_revision` 判定之后、
组装 `ordered` 之前：

```
actionable = recognition_result == "是策略" or event_type != "none"
if not actionable:
    reasons.discard("multiple_same_source_candidates")
```

- 8 个信号自身的检测逻辑、`CONTEXT_TRIGGER_ORDER`、返回值形状均未改动。
- `discard` 放在 `apparent_entry_may_be_revision` 之后。该判定只依赖
  `recognition_result == "是策略"`、`candidates` 非空、以及
  `"revision_language" in reasons` 或候选的 `overlapping_entry`，与
  `multiple_same_source_candidates` 无关；且 `discard` 之后除 `ordered` 外再无读取
  `reasons` 的逻辑，因此两者先后顺序不影响结果。
- 影子判定 `context_resolution_shadow.py` 按要求未改动。

### 6.2 既有测试

`grep -rn "requires_context_resolution\|multiple_same_source_candidates" tests/` 的全部命中
逐条核对后，**没有任何既有断言需要修改**：

- `tests/test_authoritative_recognition.py:873`
  （`test_context_resolution_triggers_are_closed_and_auditable`）：第一层为
  `是策略`，`actionable` 为真，六个信号的期望元组不变。
- `tests/test_authoritative_recognition.py:905`
  （`test_unambiguous_independent_entry_does_not_require_second_resolution`）：
  `candidates=[]`，本来就不会产生该信号。
- `tests/test_context_resolution.py`（369/417/462/514）、
  `tests/test_recognition_context_gate.py`（73/514/519）：把触发原因当**输入数据**传给
  记录与渲染路径，不调用 `requires_context_resolution`。
- `tests/test_context_resolution_shadow.py`：直接测影子函数，按要求不动。

第 4 节点名的 `tests/test_recognition_authority_architecture.py` 在本仓库中没有对该函数的
断言（文件内无相关命中），故无改动。

### 6.3 新增用例

新文件 `tests/test_context_trigger_actionable_gate.py`，5 例：

1. `test_non_actionable_message_ignores_multiple_candidates` —
   非策略 + `event_type=none` + 2 候选 → `(False, ())`。
2. `test_lifecycle_event_without_target_keeps_multiple_candidates` —
   非策略 + `position_update`（target 为空）+ 2 候选 → 触发，
   `management_without_exact_target` 与 `multiple_same_source_candidates` 都在。
3. `test_strategy_message_keeps_multiple_candidates` —
   是策略 + 2 候选 → 触发，`reasons == ("multiple_same_source_candidates",)`。
4. `test_wording_signal_still_triggers_without_the_structural_one` —
   非策略 + none + 2 候选 + 正文含「持仓」→ 触发，含 `entered_holder_language`，
   不含 `multiple_same_source_candidates`。
5. `test_apparent_entry_revision_is_unchanged_for_non_strategy_messages` —
   非策略 + none + 2 候选 + 正文含「修改」+ 候选带 `overlapping_entry` →
   `apparent_entry_may_be_revision` 行为与改动前一致（它要求 `是策略`，故不出现），
   `revision_language` 仍在，结果为 `("revision_language",)`。

### 6.4 全量测试

`PYTHONPATH=. uv run pytest -q`（裸跑会在收集阶段报 `No module named 'tests'`）：

```

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
8867 passed, 4 skipped, 107 warnings in 848.92s (0:14:08)
```

退出码 0，新增的 5 例包含在内。4 条 skip 未单独与基线对比。

### 6.5 实施中发现：影子判定无法充当方案甲的校验信号

第 4 节最后一条设想「部署后一周看 `shadow_would_extra_trigger` 的分布，校验方案甲是否过紧」。
按当前实现，这个信号**结构上恒为 0**，改动前后都是：

- `evaluate_context_resolution_shadow` 只在 `context_resolution.py:748` 被调用，
  而该调用点位于上下文调用**已经发生**之后的落库路径里，参数写死
  `authoritative_would_trigger=True`。权威没触发的消息根本不会产生 shadow 行。
- 因此 `disagreement_direction` 只可能取 `shadow_would_skip`，
  取不到 `shadow_would_extra_trigger`。`docs/ai-context-observation-log.md` 第 1 节
  记录的生产数据也正是 `shadow_would_extra_trigger 0`。
- 影子的 `multiple_allowed` 分支还以 `authoritative_triggers` 里存在
  `multiple_same_source_candidates` 为前提。方案甲上线后该信号只会在 `actionable`
  时出现，影子必然同时命中 `first_pass_strategy` 或 `lifecycle_event_present`，
  于是 `shadow_would_skip` 也会趋近 0——已记录的 65 条 would-skip 全部是
  「非策略 / none / target=NULL」，正是方案甲要掐掉的那一组。

按指令未改动影子判定。若确实需要「方案甲是否过紧」的校验信号，需要另行决定：
在权威未触发的路径上也记录一次影子评估（并传入真实的
`authoritative_would_trigger`），这超出本次改动范围。
