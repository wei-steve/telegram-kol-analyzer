# Message Card Recognition Labels Status（识别结果标题去 MiMo 化 + 上下文二次判断状态明示）

设计：`docs/plans/2026-09-15-message-card-recognition-labels-design.md`（唯一设计真相）。
本文件是本项目的进度、决策与证据真相。

```yaml
project: message-card-recognition-labels
started: 2026-09-15
integration_branch: codex/deepcoin-auto-trading-v1
base_commit: 88acc5e7        # 设计文档提交；实施从这里起
implementer: 子代理（Opus 5 / high），worktree agent-ac81bdd9ea04920bd
commander: 指挥会话
current_phase: 1
phase_status: completed      # planned | in_progress | completed | blocked
deploy: 未部署               # 本次只做本地实施与全量测试，未 push、未 tg-deploy
```

## 阶段总览

| 阶段 | 内容 | 状态 | 提交 | 测试 |
|---|---|---|---|---|
| 1（一批） | 数据层新列 + 门结论写入 + 查询层 `execution_state` + 模板/前端 | completed | 见下方 | 全量 8861 passed / 0 failed / 4 skipped（933s） |

全量命令：`PYTHONPATH=<repo root> uv run pytest -q`。
注意：裸 `uv run pytest -q` 在本仓库会因 `tests/test_management_reliability_step5e.py` 与
`tests/test_strategy_management_worker.py` 的 `from tests.…` 导入而在收集阶段报
`ModuleNotFoundError: No module named 'tests'`（`pyproject.toml` 只设了 `pythonpath = ["src"]`）。
这是既有环境问题，与本次改动无关。

设计第 6 节要求「一批完成即可」，故只有一个提交，避免中间态出现红测试
（新测试同时依赖数据层与查询层，拆开则任一单独提交都不可能全绿）。

## 做了什么

**数据层**
- `models.py`：`RecognitionDecision` 新增 `context_resolution_gate_json TEXT NULL`。
- `db.py`：`SQLITE_COMPAT_COLUMNS["recognition_decisions"]` 新增同名项，旧库自动补列，无 alembic。
- `recognition_decisions.py`：`RecognitionDecisionRecord` 新增 `context_resolution_gate: dict | None = None`；
  四条写路径（terminal 新建/更新、pending 新建/更新）都落这列。
- `authoritative_recognition.py`：`assess_message_authoritatively`（`process_authoritative_message` 的实现体）
  在两道门都已知之后、**且在 1444 行 `except` 把 `mimo` 改写成识别失败之前**算好 `outcome`，
  四值为 `invoked` / `not_needed` / `resolver_disabled` / `recognition_failed`。

**查询层**
- `web_queries._serialize_mimo_runtime` 新增 `model_label`；`status_label` 去掉 MiMo 前缀；
  `version_label` 统一为「权威识别结果」（v1 回退为「权威识别结果（v2 失败，已回退 v1 合约）」）。
- `web_queries._serialize_context_resolution` 新增 `decision` / `model_labels` 参数，
  输出 `execution_state`（10 值）、`gate_outcome`、`gate_triggers`、`model`、`model_label`；
  只要有 decision 行就返回对象。
- `web_app._ai_model_labels()` 每次请求读一次配置（best-effort，读不到就空 map → 回落原始 id），
  传给三处 `load_group_message_page`。`web_queries` 保持无文件 IO。

**模板与前端**
- `_messages.html`：触发信号中文映射 + 状态徽章文案两个顶层 dict；折叠摘要、标题行模型徽章、
  技术明细模型行、「已结合」芯片带首个触发原因、`data-message-context-triggers`、
  上下文卡片 summary 徽章 + 卡片体第一行触发原因芯片 + 上下文模型行。
- `app.js`：`summarizeContextTriggers()` 客户端按次数降序汇总，追加到「上下文调用 N」后。
- `app.css`：`.mimo-runtime-model`、`.context-exec-state.is-*`、`.context-trigger-reasons`、
  `.context-trigger-chip`、`.context-model-line`。

未改：`requires_context_resolution` 的任何判定逻辑、触发频率、任何含 `mimo` 的表名/列名/合约名/
CSS 类名/Python 标识符/`mimo_analysis` 字段名。未回填历史行。

## 设计未覆盖之处与本次取舍

1. **`invoked` 但没有 attempt 行**：设计的 `execution_state` 表只列了 `无 attempt` 且
   outcome 为 `not_needed`/`resolver_disabled`/`recognition_failed`/空 四种。解析器在写 attempt 行
   之前就抛异常时会出现「gate=invoked 且无 attempt」。为把枚举保持在设计规定的 10 个值内，
   这种情况落到 `unknown`（「未执行（历史消息，未记录原因）」）。
2. **无门结论的写入者不清空该列**：`telegram_live_listener.py:687` 的 recovery guard 不评估两道门，
   传 `None`。两条 **更新** 路径在 `context_resolution_gate is None` 时不写该列，
   以免把先前已记录的门结论抹成 NULL。新建路径仍按 `None` 写 NULL。
3. **统计行的触发分布统计范围**：设计说「客户端从 data 属性汇总」，未限定只统计真正调用过的卡片。
   按字面实现为对所有已加载卡片求和。`not_needed` 天然贡献 0 个触发，
   `resolver_disabled`（群组门关）会贡献触发但没有实际调用；生产上群组门是开的，两者近似相等。
4. **模板兜底文案 `MiMo第一次识别` 与 `aria-label`**：`version_label` 现在恒非 None，兜底不可达；
   仍把兜底字符串与 `aria-label` 一并改为「权威识别结果」，否则 body 里仍留 MiMo 字样，
   与设计第 5 节要求同步 `tests/test_web_group_messages_route.py:186,188` 相矛盾。
5. **`superseded` 徽章配色**：设计只规定了 completed 绿 / in_progress 黄 / exhausted、blocked_* 红 /
   not_needed、disabled、unknown 灰，未提 `superseded`；按「其余为灰」处理。
6. **`load_selected_messages` / `load_messages_in_time_window` 未加 `model_labels`**：
   这两个入口不渲染消息卡片，保持原状；`model_label` 在无 map 时回落原始 id，无空白风险。
7. **技术明细在模型未配置显示名时会重复**：如 `gpt-5.6-luna（gpt-5.6-luna）`。
   这是设计第 3.1 节 `模型 {{ model_label }}（{{ runtime.model }}）` 的字面结果，未做去重。

## 被迫改动的既有测试

| 文件:行 | 原断言 | 现断言 | 原因 |
|---|---|---|---|
| `tests/test_web_mimo_analysis_projection.py:313` | `version_label == "MiMo v1回退结果"` | `== "权威识别结果（v2 失败，已回退 v1 合约）"` | 设计 3.1 表 |
| `tests/test_web_mimo_analysis_projection.py:525` | `status_label == "MiMo识别进行中"` | `== "识别进行中"` | 设计 3.1 表（设计已点名） |
| `tests/test_web_mimo_analysis_projection.py:560` | `version_label == "MiMo v1结果"` | `== "权威识别结果"` | 设计 3.1 表（设计已点名） |
| `tests/test_web_group_messages_route.py:186,188` | 顺序断言用 `MiMo第一次识别` | 用 `权威识别结果` | 设计已点名 |
| `tests/test_web_group_messages_route.py:211` | `"MiMo识别结果" in collapsed` | `"AI识别结果"` | 设计已点名 |
| `tests/test_web_group_messages_route.py:310` | `"MiMo v1结果" in body` | `"权威识别结果"` | 设计已点名 |
| `tests/test_web_page_render.py:586` | `"模型 mimo-v2.5" in image_card` | `"模型 MiMo V2.5（mimo-v2.5）"` | 设计 3.1 技术明细行的直接后果；设计未点名，但不改则与新文案冲突 |

## 降低上下文触发频率的观察（只观察，未改任何判定）

`requires_context_resolution`（`authoritative_recognition.py:194-263`）里，
**只有 `apparent_entry_may_be_revision` 检查了 `recognition_result == "是策略"`**。
其余七个信号在第一次识别成功（哪怕结论是「非策略」）后就无条件评估：

- `revision_language` 对 `("更新","修改","改为","调整","replace","update")` 做子串匹配。
  「调整」「更新」在纯行情点评里极常见（「行情调整」「更新一下看法」）。
- `entered_holder_language` 含「持仓」。「持仓比例」「大户持仓」等评论用语会命中。
- `multiple_same_source_candidates` 仅判断 `len(candidates) > 1`，与本条消息是不是策略消息无关；
  活跃群里只要同来源有 2 个以上候选线程，任何消息都命中。

最省事且语义改动最小的收紧方向，是把这几个纯措辞信号同样收到
「`recognition_result == "是策略"` 或 `lifecycle_event.event_type != "none"`」之下。
本次未做任何改动；新加的触发原因分布统计行正是为量化这一点而存在，
建议先看一周真实分布再决定。
