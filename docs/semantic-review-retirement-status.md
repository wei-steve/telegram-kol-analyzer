# 分析复核（语义分歧复核）整条退役 · 状态

**设计稿**：`docs/plans/2026-09-25-semantic-review-retirement-design.md`（用户 2026-09-25 批准）
**分支**：`semantic-review-retirement`，基线 `origin/main` = `467d45f2`
**基线全套**：9666 passed / 4 skipped（9670 collected）
**本批只改代码，本地完成**：不部署、不推送、不碰生产库。部署按 L2，须用户单独批准。

## 阶段

| 阶段 | 内容 | commit | 全套 | 状态 |
|---|---|---|---|---|
| 1 | 审计 `comparison_*` 归属，建本状态文档 | `b251da2f` | 未改代码 | completed |
| 2 | 退役运行路径（两个模块、worker 单例、设置、CLI、stage、提示词、通知、dataclass 字段） | `7283e9e0` | 9530 passed / 4 skipped / 1 计时用例偶发 | completed |
| 3 | 退役页面展示（`web_queries` / 模板 / CSS） | `31023602` | 9520 passed / 4 skipped / 0 failed | completed |
| 4 | 文档收口（`ARCHITECTURE` / `runbook` §10 / `context/ai-prompt-registry`） | 本次提交 | 只改文档（L0） | completed |

**测试数：9670 → 9524（−146）。** 逐项账在阶段 2 与阶段 3 的 commit message 里，
两段加起来正好 −146：阶段 2 −135，阶段 3 −11。

阶段 2 的那条失败是
`test_web_page_render.py::test_positions_panel_stale_snapshot_does_not_wait_for_background_refresh`
——它量的是墙钟耗时、卡在一个 2 秒屏障上，与本批无关；单跑与单模块跑都过，阶段 3 的全套
（同样 12 分钟）也过。

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

## 5. 部署

**尚未部署。** 本批只做代码，本地完成。设计稿 §4 要求按 L2 部署、**须用户单独批准**，
观察窗 30 分钟 / ≥5 条真实消息。分支 `semantic-review-retirement`，未 push、未开 PR。

回滚边界：本批不改数据库 schema、不改任何 `comparison_*` 列、不写生产库，回滚就是
`tg-deploy <部署前 HEAD>`。

## 6. 本批做的自主判断（设计稿没写的）

1. **`finalize_authoritative_automation_outcome` / `finalize_recorded_authoritative_execution`
   保留"复核关闭"那条分支的全部写入**，包括 `agreement_status = "review_disabled"` 字面量。
   生产恒走这条分支，保留它等于零行为改变；换个名字会改变
   `message_operation_supervisor` / `message_operation_contracts` 读到的值。
2. **执行路径在重新分析时把复核字段写回 `None` / `0` 的那些写入全部保留。** 删掉它们会让
   历史复核数据在重新分析后留存，那是行为改变。
3. **`save_pending_authoritative_decision` 的 `preserve_completed_review` 分支保留。**
   它的名字来自复核，但它决定的是"重新分析遇到未变更 payload 时怎么合并
   `agreement_status` / `prompt_versions`"，属于重新分析语义。
4. **`comparison_status == "execution_uncertain"` 的页面提示保下来了。** 它原先长在复核控件里
   （文案「AI复核：执行结果未知」），但它讲的是执行租约被冻结、无人重试、需要人看，
   不是复核。整块删掉会让这条安全提示从页面消失，而 `_serialize_execution_outcome`
   接不住它（uncertain 行的 `automation_status` 是 `uncertain`，会落到「未执行」）。
   现在是 `web_queries._serialize_execution_uncertainty`，独立的键与独立的告警块，
   去掉了「AI复核」字样。
5. **`AuthoritativeAssessment.deepseek_payload` 确实有读者**，与交接说明相反：
   `telegram_live_listener._build_authoritative_notification_payload` 读它来填告警的辅助段。
   由于所有构造点一直传 `None`，那一段本来恒为空、`auxiliary_review_disagrees` 恒返回
   `False`。删字段后 payload 仍保留一个空的 `"deepseek"` 键，所以判据、格式化函数和
   「没有第二个模型就不发」的规则一个字没动。
6. **`/api/messages/{id}/recognize` 的 `semantic_review_status` 响应字段删除。**
   设计稿 §3.1 点名要删它的产生与传递；页面 JS 与模板都没有消费者。
7. **`scripts/archive/per_chat_phase7_observer.py` 不动。** 已归档脚本，不 import 本包，
   读设置走 `.get(..., False)` 兜底。
8. **`docs/runbook.md` §10 重写而不是删除**：那一节的 SQL 现在讲执行认领租约怎么查，
   因为列还在、租约还在跑，只有复核那部分作废。
9. **`docs/context/ai-prompt-registry.md` 只改复核相关的三处**，没顺手修它里面
   `research.chat.*`（2026-09-14 已删）那类更早的陈旧内容——那是另一批的事。
