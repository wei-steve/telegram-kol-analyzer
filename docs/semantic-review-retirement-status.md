# 分析复核（语义分歧复核）整条退役 · 状态

**设计稿**：`docs/plans/2026-09-25-semantic-review-retirement-design.md`（用户 2026-09-25 批准）
**分支**：`semantic-review-retirement`，基线 `origin/main` = `467d45f2`
**基线全套**：9666 passed / 4 skipped（9670 collected）
**本批只改代码，本地完成**：不部署、不推送、不碰生产库。部署按 L2，须用户单独批准。

## 阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 审计 `comparison_*` 归属，建本状态文档 | completed |
| 2 | 退役运行路径（两个模块、worker 单例、设置、CLI、stage、提示词、通知、dataclass 字段） | completed |
| 3 | 退役页面展示（`web_queries` / 模板 / JS / CSS） | completed |
| 4 | 文档收口（`docs/ARCHITECTURE.md`） | completed |

---

## 1. `comparison_*` 字段判定表（本批一列都不删）

审计口径：`grep -rhoE '\bcomparison_[a-z_]+\b' src/ tests/ scripts/ config/`，逐个字段列出
**全部**引用方，再判定「属于复核」还是「属于执行认领机制」。

| 字段 | 全部引用方（src/） | 归属 | 本批动了没有 |
|---|---|---|---|
| `comparison_status` | `models`(列) `db`(DDL) `recognition_decisions` `authoritative_recognition` `authoritative_execution_attempts` `recognition_execution_scanner` `message_processing_backlog_expiry` `message_operation_supervisor` `message_operation_contracts` `oncall_casefile` `web_app` `web_queries` `cli` + 被删的 `semantic_review_control` | **执行认领租约**（`execution_pending` / `execution_running` / `execution_uncertain` / `completed`）**兼**复核状态机（`pending` / `running` / `failed`） | **列与执行语义一字未改**。只删掉复核那三个取值的**写入方**（复核模块与 `recognition_decisions` 的 5 个复核函数）。`"pending"` 的唯一写入方本来就是 `finalize_*(semantic_review_enabled=True)` 的 `case()` 分支与 `fail_semantic_review`，两者随复核一起消失；生产恒走 `semantic_review_enabled=False`，那条 `case()` 从未在生产取到 `"pending"` |
| `comparison_claim_token` | `models`(列) `db`(DDL) `recognition_decisions` `authoritative_execution_attempts` + 被删的 `semantic_review_control` | **执行认领租约 token 本体** | **一字未改**。只删复核函数里对它的写入（复核自己的租约） |
| `comparison_started_at` | `models` `db` `recognition_decisions` `authoritative_execution_attempts` + 被删的 `semantic_review_control` | 两套租约共用的「认领开始时刻」：执行侧只把它置 `None`，复核侧才写时刻 | 列未改；执行侧的 `None` 写入全部保留；只删复核侧的写入 |
| `comparison_next_attempt_at` | 同上 | 复核重试排程；执行侧只置 `None` | 同上 |
| `comparison_attempts` | `models` `db` `recognition_decisions` + 被删的 `semantic_disagreement_review` / `semantic_review_control` | **复核专用**重试计数；执行侧只在重置时写 `0`，从不读 | 列未改；执行侧写 `0` 的两处保留；复核侧的读写删除 |
| `comparison_payload_json` | `models` `db` `recognition_decisions` `web_queries` | **复核专用**内容；执行侧只在重置时写 `None` | 列未改；执行侧 `None` 写入保留；复核侧写入与 `web_queries` 的展示删除 |
| `comparison_model` | 同上 | **复核专用** | 同上 |
| `comparison_error` | `models` `db` `recognition_decisions` + **`deepcoin_contract_specs`（同名但完全无关的局部变量）** | **复核专用**；`deepcoin_contract_specs` 那个是 shadow 合同规格比对的局部变量，与本表无关 | 列未改；`deepcoin_contract_specs` **一字未动** |
| `comparison_payload` | `recognition_decisions`(复核函数参数) + 被删的 `semantic_disagreement_review` | **复核专用**（参数名，不是列） | 随复核函数删除 |
| `comparison_kind` / `comparison_started` / `comparison_now` | `runtime_agent_exchange_snapshot` `runtime_agent_executor` `cli` | **与本表无关**：运行时事件 agent 的「本地 vs 交易所只读快照」比对，不是 `recognition_decisions` 的列 | 未动 |
| `disagreement_severity`（同组遗留列） | `models` `db` `recognition_decisions` `web_queries` **`strategy_records`**（健康码 `recognition_disagreement` 的判据）**`oncall_casefile`**（只读导出） | **有非复核读者**：`strategy_records` 用它推 `recognition_disagreement`。NULL 与 `""` 都落在 `_NORMAL_DISAGREEMENT_SEVERITIES = {"", "none", "normal"}` 里，复核退役后新行恒为 NULL，**该判据恒不触发**，与生产现状（复核关闭、该列恒 NULL）完全一致 | 列未改；`strategy_records` / `oncall_casefile` **一字未动**；只删 `web_queries` 的展示与复核侧写入 |
| `compared_at`（同组遗留列） | `models` `db` `recognition_decisions` + 被删的 `semantic_disagreement_review` | **复核专用** | 列未改；执行侧写 `None` 保留 |

### 结论

**没有任何一个字段同时被复核写入与执行认领读取。** 两套租约共用 `comparison_status` /
`comparison_claim_token` / `comparison_started_at` / `comparison_next_attempt_at` 四列，但
取值空间不重叠（复核用 `pending`/`running`/`failed`，执行用 `execution_*`，`completed` 是两者
共同的终态），CAS 条件各自带自己的状态常量，所以删掉复核侧的写入不会让执行侧的任何一条 CAS
改变结果。

---

## 2. 保留的东西（设计稿 §3.2 / §5）

- 全部 `comparison_*` 列、`disagreement_severity`、`compared_at`：**一列都没删**（删列是 L3）。
- 执行路径在重置时把复核字段写回 `None` / `0` 的那些写入**全部保留**。删掉它们会让历史复核
  数据在重新分析后留存，那是行为改变，本批不做。
- `save_pending_authoritative_decision` 里的 `preserve_completed_review` 分支保留：它决定
  重新分析时 `agreement_status` / `prompt_versions` 怎么合并，删它会改重新分析路径的语义。
- `finalize_*` 写入的 `agreement_status = "review_disabled"` 字面量保留：这就是生产现在写的值，
  `message_operation_supervisor` / `message_operation_contracts` 的终态覆盖判据读得到它。
- 生产库里 `trading.disagreement.semantic_review` 的提示词定义行：下线它是独立数据库操作，不在本批。
- `config/ai_recognition.yaml` 里遗留的 `stages.semantic_review` 键：加载时静默丢弃 + warning。

## 3. 旧配置兼容怎么做的

照 `research_chat` 的先例（`ARCHITECTURE` §5.5，2026-09-14）：**不加任何新代码**。
`ai_recognition_config._normalize`（约 660 行）本来就有一条通用规则——`stages` 里出现
`AI_STAGE_DEFINITIONS_BY_KEY` 不认识的键，就记一条 `dropped unknown stage '<key>'` 到
`config_warnings` 并丢弃，从不抛错；`save_ai_recognition_config` 只写回 catalog 里的 stage，
所以下一次保存时那个键自然消失。把 `semantic_review` 从 catalog 里删掉，它就自动落入这条规则。
新增的测试 `test_a_retired_semantic_review_stage_is_dropped_with_a_warning`
（`tests/test_ai_stage_config.py`）与 `research_chat` 那条同构，逐条断言：丢弃、warning、
保存后文件里消失。

## 4. 明确不在本批的命名债（发现但没动）

这些都是**别的**死设计留下的 `deepseek` 字眼，不属于分析复核，动它们会改别的路径的存储值或展示：

- `authoritative_recognition.py:847` `resolver="deepseek_context"` —— 写进
  `message_strategy_links.resolver` 的值，属上下文结合分析线。
- `message_recognition.py` 的 `deepseek_composition` / `infer_deepseek_auxiliary`、
  `prompt_composition.py:48` 的 `model_kind in {"deepseek","mimo"}` —— V1 批量识别线。
- `web_queries.py` `_build_recognition_comparison` 的 `"deepseek_text"` 键 —— 生产文本识别对照面板。
- `context_resolution.py` / `context_resolution_prompt.py` 的 DeepSeek 措辞 —— 上下文结合分析线。
- `ai_stage_catalog` / `ai_provider_presets.json` / `config/ai_recognition.example.yaml` 里的
  `deepseek` provider 预设 —— 那是一个真实可选的提供商，不是命名债。
- `scripts/archive/per_chat_phase7_observer.py` 读 `settings["semantic_review_enabled"]` ——
  已归档脚本，不 import 本包模块，靠 `.get(..., False)` 兜底，删设置项不会让它报错。
