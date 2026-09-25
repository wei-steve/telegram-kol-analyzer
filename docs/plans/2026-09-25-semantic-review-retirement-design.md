# 分析复核（语义分歧复核）整条退役 · 设计稿

**状态**：**已批准**（用户 2026-09-25）。等第一批 `prompt-test-stage-models` 落地后开工。
**起因**：用户 2026-09-25：「现在系统已经是可以选择切换不同的模型，为什么还留着一些
mimo 分析 deepseek 复核的字眼，这以后还会继续造成误导。……分析复核已弃用。」
**级别**：删除生产代码路径与一个 AI 环节，按 **L2** 对待；**部署须用户单独批准**。
**关联**：提示词测试去厂商命名是**另一批**（`prompt-test-stage-models`），两批文件有重叠，
**必须串行**，本批在那批之后进行。

---

## 1. 现状（已核实 2026-09-25）

**生产是关着的**：`trading_settings` 里**没有** `semantic_review_enabled` 行，
取代码默认 `False`（`trading_settings.py:108`）。与用户所说一致。

它是只读顾问：不改交易、不阻断交易、不授权任何交易动作（提示词里明写）。

### 规模

| 类别 | 清单 |
|---|---|
| 专用模块 | `semantic_disagreement_review.py`（1312 行）、`semantic_review_control.py`（427 行） |
| 引用它的生产模块（15） | `ai_endpoints` `ai_recognition_config` `ai_stage_catalog` `authoritative_execution_attempts` `authoritative_recognition` `cli` `prompt_composition` `prompt_defaults` `recognition_decisions` `recognition_execution_scanner` `trading_settings` `web_app` `web_queries` + 两个专用模块 |
| worker 单例 | `RUNTIME_ROLE_SINGLETON_TASKS["worker"]` 的 `semantic_review`（`web_app.py:433`） |
| 运行器 | `web_app._supervise_semantic_review_runner` / `_build_semantic_review_notifier` |
| AI stage | `semantic_review`（`ai_stage_catalog.py:180`、`SEMANTIC_REVIEW_STAGE`）、`config/ai_recognition.yaml` |
| 提示词 | `trading.disagreement.semantic_review`（代码种子 + 生产库定义行） |
| 设置项 | `semantic_review_enabled` |
| CLI | `semantic-review-terminalize`、`semantic-review-terminalize-rollback`（`cli.py:6480/6544`） |
| 页面 | `index.html:299`「开启 DeepSeek 辅助复核」、`_messages.html:640`「DeepSeek辅助复核」、`app.js`、`app.css` |
| 测试 | 30 个文件 |

---

## 2. ⚠️ 本批最大的陷阱：`comparison_*` 列**不是**复核的

`recognition_decisions` 上那组 `comparison_*` 列，名字来自**早已不存在的**
「DeepSeek 对照 MiMo」那套设计，但**它们今天承载的是执行认领租约**，与分析复核无关。

实测引用方：

| 列 | 谁在用 | 属于 |
|---|---|---|
| `comparison_status` | `authoritative_execution_attempts`、`message_operation_supervisor`、`message_processing_backlog_expiry`、`recognition_execution_scanner`、`message_operation_contracts`、`oncall_casefile` … 共 14 个模块 | **执行机制，绝不能动** |
| `comparison_claim_token` | `authoritative_execution_attempts`（认领/释放租约） | **执行机制，绝不能动** |
| `comparison_payload_json` / `comparison_model` | `recognition_decisions`、`web_queries`（`_serialize_semantic_review` 从这里读复核结果） | 疑似复核专用，**须逐一确认** |
| `disagreement_severity` | `oncall_casefile`、`strategy_records`、`web_queries` | **有非复核读者，须确认** |
| `comparison_attempts` / `compared_at` | `semantic_disagreement_review`、`semantic_review_control` | 疑似复核专用 |
| `comparison_error` | 另有 `deepcoin_contract_specs` 同名局部变量（**同名不同物，别被 grep 骗了**） | 须确认 |

**硬性要求**：
1. **本批一列都不删**（删列是 L3，另议）。
2. 动任何一个 `comparison_*` 的读写之前，必须先列出该字段的**全部**引用方并逐一判定
   「属于复核」还是「属于执行机制」，把判定写进 commit message。
3. `comparison_status` 与 `comparison_claim_token` **完全不动**。

2026-09-24 的教训在这里第二次适用：MiMo v2 按「定义体含关键词」整块删，把同一条 `import`
里的无关符号一起带走，全套跑出 101 个失败。**`comparison_` 这个前缀正是同一形状的陷阱，
而且更危险——它连着执行认领。**

---

## 3. 退役范围

### 3.1 删除

- `semantic_disagreement_review.py`、`semantic_review_control.py` 两个模块
- `web_app` 的 worker 单例注册、`_supervise_semantic_review_runner`、`_build_semantic_review_notifier`
- `ai_stage_catalog` 的 `semantic_review` stage 定义与 `SEMANTIC_REVIEW_STAGE`
- `prompt_defaults` 的 `SEMANTIC_DISAGREEMENT_REVIEW_PROMPT` 与种子
- `prompt_composition` 对该 profile 的校验分支
- `trading_settings.semantic_review_enabled`
- 两条 CLI 命令及其辅助函数
- 页面上的开关与展示（`index.html:299`、`_messages.html:640`、对应 JS/CSS）
- `authoritative_recognition` 里 `semantic_review_status` 的产生与传递
  （**注意**：`AuthoritativeAssessment.semantic_review_status` 默认 `"not_applicable"`，
  删它会改变那个 dataclass 的形状，下游读者要一并处理）

### 3.2 保留

- 全部 `comparison_*` 数据库列与 `disagreement_severity`（见 §2）
- `config/ai_recognition.yaml` 里遗留的 `stages.semantic_review` 键：
  **加载时静默丢弃并 warning**，与 `research_chat` 退役时的做法一致（ARCHITECTURE §5.5 有先例）。
  **不要**让旧配置文件加载失败。
- 生产库里 `trading.disagreement.semantic_review` 的提示词定义行——**下线它是独立的数据库操作**，
  不在本批。

---

## 4. 顺序与验收

1. 先做另一批（`prompt-test-stage-models`）并合并，本批在其之后开工，避免文件冲突。
2. 本批**只做代码，本地完成**，不部署、不碰生产库。
3. 部署按 L2：须用户单独批准；观察窗 30 分钟 / ≥5 条真实消息。

**验收靠测试，不靠关键词匹配。** 具体要求：
- 全套回到基线或更高（基线以开工时 `origin/main` 的实测为准）。
- 必须有一条测试证明**执行认领链路不受影响**：`comparison_status` / `comparison_claim_token`
  的认领、释放、过期语义在删除前后完全一致。
- 必须有一条测试证明**旧配置文件仍能加载**（含 `stages.semantic_review` 键时静默丢弃 + warning）。
- 必须有一条测试证明 `RUNTIME_ROLE_SINGLETON_TASKS` 不再含 `semantic_review`，
  且 worker 其余单例任务集合不变。

## 5. 明确不做

- 不删任何数据库列（L3）。
- 不动 `comparison_status` / `comparison_claim_token` 的任何语义。
- 不下线生产库里的提示词定义行（独立数据库操作）。
- 不动首次分析四分类、候选集合过滤、执行语义。
