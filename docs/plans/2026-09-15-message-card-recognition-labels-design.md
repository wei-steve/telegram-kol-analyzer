# 消息卡片：识别结果标题去 MiMo 化 + 上下文二次判断状态明示

日期：2026-09-15
状态：已实施并于 2026-09-15 部署生产 `1fe25a04`（审阅后追加 `invoked_unrecorded` 状态）；进度见 `docs/message-card-recognition-labels-status.md`
实施方式：Opus 5 (high) 子代理在 worktree 实施，指挥会话审阅

## 1. 问题

群组页右侧消息列表里，每条消息展开后有一组识别流程卡片。用户反馈两点：

1. 第一张卡片标题写死为「MiMo v1结果」。自 2026-09-14 的 AI 路由项目之后，
   权威识别环节可以配置任意提供商/模型（生产主用已是 codex 代理上的
   gpt-5.6-luna，MiMo 只是备用），标题继续叫 MiMo 会误导。
2. 「上下文二次判断」卡片看不出这条消息**到底有没有跑过**二次判断。

## 2. 根因（已在代码中核实）

### 2.1 标题与状态文案的来源

| 位置 | 现文案 | 说明 |
| --- | --- | --- |
| `web_queries.py:1251-1255` `version_label` | `MiMo v1结果` / `MiMo v1回退结果` | v1 指 MiMo **合约格式**（扁平 payload），不是模型；换了模型仍走 v1 合约就显示这个 |
| `web_queries.py:1387-1397` `status_label` | `MiMo识别进行中` / `MiMo识别失败` / `MiMo v2失败，已使用v1结果` / `MiMo识别成功` | 运行状态徽章 |
| `_messages.html:404` | 兜底 `MiMo第一次识别` | v2 合约时 `version_label` 为 None，走兜底 |
| `_messages.html:219` | 折叠态 `MiMo识别结果：…` | 卡片收起时的一行摘要 |
| `_messages.html:415` | `模型 {{ runtime.model }}` | 只在「运行技术明细」里显示原始模型 id，无显示名 |

`mimo_recognition_runs.model` 已经记录了真实模型 id（`MimoRecognitionRun.model`），
所以数据是有的，只是标题没用它。

### 2.2 上下文二次判断为什么看不出有没有执行

- 是否执行由两道门决定，且**都不落库**：
  1. 群组门：`TradingSettings.context_resolution_enabled_for_chat(chat_id)`
     （`trading_settings.py:255`）。web 入口 `web_app.py:4773-4783` 关门时把
     `context_resolver=None` 传进去；CLI worker `cli.py:5726` 同理。
  2. 触发门：`authoritative_recognition.requires_context_resolution`
     （`authoritative_recognition.py:194-263`）按 8 个确定性信号判断
     （修改/取消/入场持有措辞、管理事件无精确目标、多候选线程、回复目标不一致、
     图文冲突、疑似修改）。没命中就不调用。
- 只有真正调用了解析器才会写 `context_resolution_attempts` 行。没调用 = 没有任何记录。
- 触发信号 `context_triggers` 只放在内存里的 `AuthoritativeAssessment.context_resolution_triggers`
  （`authoritative_recognition.py:178, 1508`），从未持久化。
- 模板 `_messages.html:510` 的显示条件是 `attempt_status or linked_threads`。
  截图这条消息**没有 attempt**（否则芯片区会有「🔗 已结合(N 条)」），
  卡片之所以出现，是因为它被链接到了策略线程。于是卡片标题是「上下文二次判断」，
  内容却只有线程关联，用户自然分不清。

结论：要「明确有没有执行」，光改模板不够，因为「没执行」和「为什么没执行」
现在根本没有记录。

## 3. 方案

### 3.1 识别结果卡片去 MiMo 化（纯展示层）

只改用户可见文案，不改内部标识（`mimo_recognition_runs` 表名、
`mimo-authoritative-v2` 合约名、CSS 类名、`mimo_analysis` 字段名一律不动）。

| 位置 | 改为 |
| --- | --- |
| `version_label` | 统一为 `权威识别结果`；v1 回退时为 `权威识别结果（v2 失败，已回退 v1 合约）` |
| `history_label` | 保持 `MiMo 历史结果 · v1格式`（那批历史数据确实全是 MiMo，如实） |
| `status_label` | `识别进行中` / `识别失败` / `v2 失败，已用 v1 结果` / `识别成功` |
| 标题行 | 在 `<strong>` 后新增模型徽章 `<span class="mimo-runtime-model">{{ model_label }}</span>` |
| 折叠态 `_messages.html:219` | `AI识别结果 · {{ model_label }}：{{ status_label }}` |
| 技术明细 `_messages.html:415` | `模型 {{ model_label }}（{{ runtime.model }}）· 合约 …`；显示名与 id 相同时不重复括号 |

**模型显示名的来源**：`ai_recognition_config.AiRecognitionConfig.models_by_id`
（`ai_recognition_config.py:375`）里每个 `AiModel` 有 `label`。
在 `web_queries` 的 runtime 序列化里新增 `model_label` 字段：
`labels.get(run.model) or run.model`。`labels` 由 `web_app` 在每次消息查询时
`load_ai_recognition_config(app.state.ai_recognition_config_path)` 一次、
取 `{id: label or id}` 传入（找不到就回落到原始 id，历史模型如 `mimo-v2.5`
不会变成空白）。不要在 `web_queries` 里直接读配置文件，保持它无 IO 依赖的现状。

### 3.2 上下文二次判断：持久化「门的结论」，卡片明示状态

**数据层**（一个新列，走现有 `SQLITE_COMPAT_COLUMNS` 机制，无 alembic）：

- `recognition_decisions` 新增 `context_resolution_gate_json TEXT NULL`
  （`models.py` 的 `RecognitionDecision` + `db.py:540` 的 compat 字典各加一项）。
- 内容：

  ```json
  {"outcome": "invoked" | "not_needed" | "resolver_disabled" | "recognition_failed",
   "triggers": ["revision_language", "..."]}
  ```

  - `invoked`：命中触发且解析器已启用，实际调用了（会有 attempt 行）
  - `not_needed`：解析器启用但 8 个信号一个都没命中
  - `resolver_disabled`：命中了触发，但 `context_resolver is None`（群组门关着）
  - `recognition_failed`：第一次识别失败，根本没评估
- 写入点：`authoritative_recognition.py:1454` 构造 `RecognitionDecisionRecord`
  时带上；`RecognitionDecisionRecord` 加字段 `context_resolution_gate: dict | None = None`；
  `recognition_decisions.py` 两条写路径（`_save_terminal_authoritative_decision_in_session`
  与 `save_pending_authoritative_decision` 的新建/更新分支）都落这列。
  `telegram_live_listener.py:687` 那条构造不传（默认 None）。
- 注意 `outcome` 的判定要在 `process_authoritative_message` 里
  `needs_resolution` / `context_resolver is not None` 两个变量都已知之后做，
  且在 1444 行的 `except` 把 `mimo` 改成识别失败之前先算好 `invoked`，
  否则「上下文调用抛异常」会被误记成 `recognition_failed`。

**查询层**（`web_queries._serialize_context_resolution`）：

- 新增参数 `decision: RecognitionDecision | None`，输出增加：
  - `gate_outcome`（上述四值之一，历史行为 `None`）
  - `gate_triggers`（列表）
  - `model`（`attempt.model`，现在没序列化）
  - `execution_state`：模板直接用的最终状态枚举，规则如下（按优先级）：

    | 条件 | `execution_state` | 卡片徽章 |
    | --- | --- | --- |
    | attempt.status ∈ pending/running/retry_pending/pending_reanalysis | `in_progress` | 执行中 |
    | attempt.status == exhausted | `exhausted` | 重试耗尽 |
    | attempt.status == blocked_disabled | `blocked_disabled` | 已阻止：群组未启用 |
    | attempt.status == blocked_execution_terminal | `blocked_terminal` | 已阻止：执行已终结 |
    | attempt.status == superseded | `superseded` | 已被新结果取代 |
    | attempt.status == completed | `completed` | 已执行 · 决策 {decision} |
    | 无 attempt 且 gate_outcome == not_needed | `not_needed` | 未执行：未命中触发条件 |
    | 无 attempt 且 gate_outcome == resolver_disabled | `disabled` | 未执行：群组未启用上下文 |
    | 无 attempt 且 gate_outcome == recognition_failed | `not_evaluated` | 未评估：第一次识别失败 |
    | 无 attempt 且 gate_outcome == invoked | `invoked_unrecorded` | 已调用，但未留下尝试记录（调用异常） |
    | 无 attempt 且 gate_outcome 为空 | `unknown` | 未执行（历史消息，未记录原因） |

- 返回 `None` 的条件放宽：只要有 decision 行（消息经过权威识别），就返回对象，
  这样每条已识别消息都有卡片。完全没识别过的消息仍返回 `None`。

**模板层**（`_messages.html:510-524`）：

- 显示条件改为 `message.context_resolution is not none`。
- `<summary>` 改为：`<strong>上下文二次判断</strong><span class="context-exec-state is-{{ state }}">{{ 徽章文案 }}</span>`；
  `completed` 时保留现有「决策：…」。
- **触发原因必须醒目**（用户明确要求：他观察到 100 次上下文调用里真正需要的不到 10 次，
  要靠这个信息判断哪些触发信号在浪费 token）：
  - 卡片体第一行、不折叠、不放进「技术明细」：
    `<div class="context-trigger-reasons"><strong>触发原因</strong>{% for t in triggers %}<span class="context-trigger-chip">{{ 中文 }}</span>{% endfor %}</div>`，
    没有触发时显示「无（未命中任何信号）」。
  - 后面一行 `<small>上下文模型 {{ context.model_label or context.model }}</small>`。
  - 芯片区 `_messages.html:318` 的「🔗 已结合(N 条)」改为「🔗 已结合(N 条) · 触发：{{ 首个触发的中文 }}{% if 多于一个 %} 等 {{ 个数 }} 项{% endif %}」，
    让收起状态也能看到原因。
  - 触发原因的中文映射（8 个信号 → 中文短语，放模板顶部 dict，与
    `CONTEXT_TRIGGER_ORDER` 一一对应）：
    `revision_language` 修改措辞、`cancellation_language` 取消措辞、
    `entered_holder_language` 已入场/持有措辞、`management_without_exact_target` 管理指令无明确目标、
    `multiple_same_source_candidates` 同来源多个候选策略、`reply_target_disagreement` 回复目标与识别目标不一致、
    `text_image_conflict` 图文冲突、`apparent_entry_may_be_revision` 疑似入场实为修改。
- **触发原因的数据来源有两处，按优先级合并**：
  1. 新列 `context_resolution_gate_json.triggers`（本次新增）。
  2. `context_resolution_attempts.invocation_triggers_json`（**已存在**，
     `ContextResolutionAttempt.invocation_triggers_json`，每次真正调用时由
     `context_resolution.py:1051/1173/1213` 写入）。历史消息靠它就能显示原因，
     不必等新数据。序列化时 `gate_triggers = gate.triggers or json.loads(attempt.invocation_triggers_json or "[]")`。
- **顶部统计行加触发原因分布**：卡片 `<article>` 加 `data-message-context-triggers="a,b,c"`；
  `static/app.js:266-277` 的统计里，在「上下文调用 N」后追加
  「（修改措辞 12 · 同来源多候选 9 · …）」，按次数降序，客户端从 data 属性汇总。
  这样用户一眼能看出哪个信号触发最多。
- `open` 条件不变（进行中/未解决/耗尽时默认展开）。
- 徽章样式：`completed` 绿、`in_progress` 黄、`exhausted`/`blocked_*` 红、
  `invoked_unrecorded` 红、`not_needed`/`disabled`/`unknown` 灰。复用 `.mimo-runtime-status.is-*` 的配色变量。

**统计与筛选**：顶部「上下文调用 N」与「用了上下文」筛选继续以 `context_called`
（有 attempt）为准，不改。

### 3.3 不做的事

- 不改上下文触发频率、不改 `requires_context_resolution` 的任何判定
  （已记在 `docs/known-issues-and-deferred-work.md`）。用户报告：100 次调用里真正需要的
  不到 10 次。本方案只把触发原因显示并汇总出来，作为下一步降频的观察依据；
  子代理若发现某信号明显过宽，只在汇报里指出，不得擅自收紧。
- 不回填历史 `recognition_decisions` 行的 `context_resolution_gate_json`；历史消息显示「未记录原因」。
- 不重命名任何表、列、合约名、CSS 类、Python 标识符里的 `mimo`。

## 4. 涉及文件

- `src/telegram_kol_research/models.py`（`RecognitionDecision` 加列）
- `src/telegram_kol_research/db.py`（`SQLITE_COMPAT_COLUMNS["recognition_decisions"]` 加项）
- `src/telegram_kol_research/recognition_decisions.py`（record 字段 + 两条写路径）
- `src/telegram_kol_research/authoritative_recognition.py`（计算 outcome 并传入 record）
- `src/telegram_kol_research/web_queries.py`（`version_label`/`status_label`/`model_label`/context 序列化）
- `src/telegram_kol_research/web_app.py`（加载 config 生成 `model_labels` 传给查询）
- `src/telegram_kol_research/templates/_messages.html`（标题、折叠摘要、上下文卡片）
- `src/telegram_kol_research/static/app.css`（徽章与触发原因芯片配色）

## 5. 测试要求

- 现有断言要同步改：`tests/test_web_mimo_analysis_projection.py:525, 560`；
  `tests/test_web_group_messages_route.py:186, 188, 211, 310`。
- 新增：
  - `recognition_decisions`：四种 outcome 各一条写入/读回；旧库缺列时 compat 迁移能补列。
  - `process_authoritative_message`：群组门关 → `resolver_disabled`；无信号 → `not_needed`；
    识别失败 → `recognition_failed`；调用成功 → `invoked` 且 attempt 存在；
    解析器抛异常 → 仍是 `invoked`。
  - `_serialize_context_resolution`：11 种 `execution_state` 各一条。
  - `_serialize_context_resolution`：gate 列为空但 attempt 有 `invocation_triggers_json` 时，`gate_triggers` 取自 attempt。
  - 路由渲染：触发原因以中文芯片出现在卡片体第一行；「已结合」芯片带首个触发原因；
    `data-message-context-triggers` 属性正确；统计行出现触发原因分布。
  - 路由渲染：已识别但无 attempt 的消息也出现「上下文二次判断」卡片且徽章为「未执行：…」；
    标题不含「MiMo v1结果」，含模型显示名；未知模型 id 回落为原始 id。
- 全量 `uv run pytest -q` 通过后才汇报。

## 6. 分阶段派工

一批完成即可（改动面小、相互依赖强）。子代理汇报格式：改动文件清单、
新增/修改测试清单、全量测试结果、本地 `uv run` 预览截图或 HTML 片段各一。
