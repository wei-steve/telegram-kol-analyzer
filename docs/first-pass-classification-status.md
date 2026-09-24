# 首次分析分类契约 · 实施状态

**设计稿（规格）**：`docs/plans/2026-09-24-first-pass-classification-contract-design.md`
**背景交接**：`docs/plans/2026-09-25-first-pass-classification-design-handoff.md`
**基线**：全套 9533 passed / 4 skipped
**阶段 1 最终候选**：`9623 passed, 4 skipped`（`uv run python -m pytest -q`，738.83s）。
差额 +90 恰好等于本批次新增的 90 条用例（`tests/test_message_classification.py` 56 条 +
`tests/test_message_classes_shadow.py` 34 条），**一条既有用例都没有被改动或打破**。

> 跑全套要用 `python -m pytest`（`uv run python -m pytest -q`）。
> 直接 `uv run pytest -q` 会在收集阶段报 3 个 `ModuleNotFoundError: No module named 'tests'`：
> 有三个测试模块 `from tests.test_x import ...`，靠的是 `python -m` 把仓库根目录放进 `sys.path`，
> 而 `pyproject.toml` 的 `pythonpath` 只有 `src`。这与本批次无关，是既有性质。

> 归档的历史计划（`docs/archive/plans/`）里有多份已被取代的设计，**不要读**。

| 阶段 | 风险级别 | 状态 |
|---|---|---|
| 阶段 1 · 影子 | L1（additive dormant） | `completed`（本地；**未部署、未发布提示词版本**） |
| 阶段 2 · 观察与人工核准 | L0 | `planned` |
| 阶段 3 · 切换 | L2（须用户单独批准后才能部署） | `planned` |
| 阶段 4 · 收口 | 以后 | `planned` |

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

---

## 阶段 2 · 观察与人工核准（planned）

产出四张表：显式 vs 推导 的一致率、`forthcoming` 的全部样本、多元素消息的全部样本、
决策 2（新策略必须有止损）影响到的历史消息清单。人工核准复用现有
`message_recognition_labels`，**不加数据库列**。

## 阶段 3 · 切换（planned，须用户单独批准后才能部署）

① 触发判据换成 §5（删 3 留 4）；② 降级不再抹平首次分析；③ 契约类失败不重问。

## 阶段 4 · 收口（planned）

`recognition_result` / `lifecycle_event` 双轨是否退役、`识别失败` 旧值的两种含义怎么拆干净。
退役清单见设计稿 §11。
