# AI 提供商与按环节模型选择（含自动备用切换）设计

日期：2026-09-13。参照：OpenMinis（`src/ios/Providers/`）的 ProviderInstance → ModelEntry →
ModelGroup(fallback) → 用途绑定 四层结构。本文件是本项目的唯一设计真相；进度与证据写在
`docs/ai-provider-model-routing-status.md`。

## 1. 目标（用户原话整理）

1. Web 页面合适的位置进入「AI 提供商」页面和「AI 模型选择」页面。
2. 项目中每一个用到 AI 模型的环节，都能在「模型选择」页面里指定用哪个模型。
3. 单条消息识别用的模型可以是一个**列表**：第一个不行，自动用第二个重试，依此类推。
4. 上下文结合分析也可以指定模型。
5. 用户另有一条记录：**当前上下文结合分析触发频率偏高，后面要调整，本次不改**
   （已记入 `docs/known-issues-and-deferred-work.md`）。

## 2. 现状（改动前，2026-09-13 只读核实）

配置文件 `config/ai_recognition.yaml`，由 `ai_recognition_config.py` 读写；三个进程（web / ingest /
worker）都是**每次用到时重新加载**（worker 走 `ai_recognition_config_loader` / 每条消息
`load_ai_recognition_config`），所以 web 保存后其他进程无需重启即可生效。

现有结构把"提供商"和"模型"揉在一起：`ai_models[]` 每一项自带 `base_url` / `api_key`，另有
`active_text_model_id` / `active_image_model_id` / `context_resolution_model_id` 三个单选，并派生出
`text_provider` / `image_provider` 两个单一 provider 给几十处调用点用。Web 已有「AI配置」（模型列表 +
Key）和「AI模型选择」（三个下拉）两个 tab，入口在右上角 ⚙ 设置菜单（`templates/index.html`）。

**现状的两个实际问题**：

- 权威识别（生产主路径）根本不看「AI模型选择」页的选择：`recognition_experiments._find_mimo_model`
  写死找 id/model 为 `mimo-v2.5` 的条目；`image_provider.model` 只用来给 run 记个名字。
- 所有环节都是单一模型，任何一处失败就是这条消息失败（MiMo 有同模型重试，但没有换模型）。

### 2.1 用到 AI 模型的环节清单（代码核实）

| stage_key | 中文名 | 能力要求 | 生产路径 | 现在的模型来源（代码位置） |
|---|---|---|---|---|
| `authoritative_recognition` | 单条消息权威识别（MiMo 多模态，v1 / v2 合同共用） | 文本 + 图片 | **是，主路径**（worker `process_authoritative_message` → `assess_message_authoritatively`） | `recognition_experiments._find_mimo_model`（写死 mimo-v2.5）；`run_mimo_authoritative_for_message`、`infer_mimo_authoritative_v2` |
| `context_resolution` | 上下文结合分析（第二层） | 文本 | 是（权威识别判定需要时调用；另有重分析队列） | `context_resolution._select_provider`（`context_resolution_model_id`，否则 `text_provider`） |
| `semantic_review` | 语义分歧复核（只读顾问） | 文本 | 是（worker `semantic_review` 单例循环） | `semantic_disagreement_review.run_semantic_review_for_message`（`config.text_provider`） |
| `strategy_alert` | 策略提醒分类（Telegram 提醒 bot） | 文本 | 是，当 bot token 配置时 | `strategy_alerts.load_strategy_alert_config`（**环境变量** `TELEGRAM_KOL_ALERT_LLM_MODEL` / `TELEGRAM_KOL_LLM_*`，启动时加载一次） |
| `research_chat` | Web 群消息问答 | 文本 | web | `web_app` `app.state.llm_proxy_config`（**环境变量** `TELEGRAM_KOL_LLM_*`，启动时加载一次） |
| `batch_text_recognition` | 离线/批量文本识别（V1 `recognize_message_now`，含生命周期事件 AI） | 文本 | 否，只有 CLI / 批量工具 | `message_recognition`（`config.text_provider`） |
| `batch_image_recognition` | 离线/批量图片识别（V1；GLM-OCR 走 layout_parsing，其他走多模态 chat） | 图片 | 否，只有 CLI / 批量工具 | `message_recognition`（`config.image_provider`，`_is_glm_ocr_model`） |
| （派生）`provider_probe` | 每日 `max_tokens=1` 探测 | — | 是 | 跟随 `authoritative_recognition` 链首模型 |
| （派生）提示词中心「历史消息测试」 | `model_kind` = mimo / deepseek | — | web | mimo → `authoritative_recognition` 链首；deepseek → `batch_text_recognition` 链首 |
| **排除** `runtime_incident_agent` | 运行时事故代理 | — | 是 | 独立 env（`TELEGRAM_KOL_RUNTIME_AGENT_LLM_*`，fail-closed，见 `llm_chat.load_runtime_agent_llm_config`）。**有意不纳入**本页面，页面上注明。 |

## 3. 数据模型（YAML schema v2）

仍用 `config/ai_recognition.yaml`（三进程按需重读的机制不变），文件升级为：

```yaml
schema_version: 2
mode: ai_provider
providers:                       # 提供商 = 一个 OpenAI 兼容端点 + 一把 Key
  - id: deepseek                 # 稳定 id，slug，页面上不可改
    label: DeepSeek
    base_url: https://api.deepseek.com
    api_key: sk-...
    timeout_seconds: 60
    enabled: true
models:                          # 模型 = 某个提供商下的一个模型名
  - id: deepseek-v4-flash        # 稳定 id（延用现有 ai_models[].id，保证旧绑定/旧记录可对上）
    provider_id: deepseek
    model: deepseek-v4-flash     # 发给 API 的名字
    label: DeepSeek V4 Flash
    supports_text: true
    supports_image: false
    enabled: true
stages:                          # 环节 → 有序模型 id 列表；第 1 个是主用，后面是备用
  authoritative_recognition: [mimo-v2.5]
  context_resolution: [deepseek-v4-flash]
  semantic_review: [deepseek-v4-flash]
  strategy_alert: []             # 空 = 沿用环境变量（见 §6 兼容）
  research_chat: []
  batch_text_recognition: [deepseek-v4-flash]
  batch_image_recognition: [glm-ocr]
```

Python 侧新增 dataclass：`AiProvider`、`AiModel`（`provider_id` + 派生 `provider` 属性）、
`AiStageDefinition`（stage_key、中文名、说明、`requires_text` / `requires_image`、是否生产路径）、
`AI_STAGE_DEFINITIONS` 目录（上表）。`AiRecognitionConfig` 增加 `providers` / `models` / `stages`，
**并继续提供** `ai_models` / `text_provider` / `image_provider` / `active_text_model_id` /
`active_image_model_id` / `context_resolution_model_id` 作为派生只读视图（分别对应
`batch_text_recognition[0]`、`batch_image_recognition[0]`、`context_resolution[0]`），这样几十处未改
的调用点与现有测试在阶段 1 不受影响。

**校验规则（load 与 save 都做，save 失败抛 422 到页面）**：

- provider id / model id 唯一、slug 格式；model 的 `provider_id` 必须存在。
- 每个 stage 的每个 model 必须满足该 stage 的能力要求（`authoritative_recognition` 要 text+image）。
- stage 列表里未知的 model id、或其 provider/model `enabled: false`，加载时**跳过并 warning**，不抛错
  （与 OpenMinis `availableEntryIds` 同义：禁用/缺凭据的成员不参与路由）。
- API Key 只写不读：GET 返回 `api_key_configured: true/false` 与末 4 位；表单留空 = 保持不变（现有约定）。

### 3.1 从 v1 迁移（`schema_version` 缺失时自动执行，写回时落成 v2）

- `ai_models[]` 每项按 `(base_url, api_key)` 去重生成 provider；id 取内置映射
  （`api.deepseek.com`→`deepseek`、`open.bigmodel.cn`→`zhipu`、`api.xiaomimimo.com`→`mimo`），
  其余取 host 的 slug；同 host 不同 key 加 `-2` 后缀。
- `models[]` 延用原 `id` / `label` / `model` / `supports_*`。
- stages 按**现在生产真实行为**映射，而不是按旧页面的选择：
  `authoritative_recognition` ← `_find_mimo_model` 找到的那一条（id 或 model 为 `mimo-v2.5`）；
  `batch_text_recognition` ← `active_text_model_id`；`batch_image_recognition` ← `active_image_model_id`；
  `context_resolution` ← `context_resolution_model_id`（空则同 text）；`semantic_review` ← 同 text；
  `strategy_alert` / `research_chat` ← 空（沿用 env）。
- 迁移必须是幂等纯函数，有单元测试：给定 `config/ai_recognition.example.yaml` 的 v1 内容，
  断言迁移结果；再 save→load 一轮结果相同。
- 服务器上真实的 `config/ai_recognition.yaml` 在第一次 web 保存前**不会被改写**（load 不落盘），
  上线时先只读核对迁移结果（CLI `telegram-kol-research ai-config-show`，输出脱敏）。

## 4. 备用切换（fallback）语义

新模块 `ai_model_router.py`：

```python
resolve_stage_chain(config, stage_key) -> list[AiModel]     # 过滤掉 disabled / 未配置凭据的，保序
run_with_fallback(chain, attempt, *, budget_seconds, min_remaining_seconds, classify) -> RouterResult
```

- **链**：stage 绑定的有序列表。空链 = 该环节未配置，行为与今天"provider 未配置"完全一致。
- **同模型重试不变**：MiMo 的 `MIMO_AUTHORITATIVE_MAX_ATTEMPTS` + 延时照旧，在**一个模型内**做完
  再换下一个；其他环节维持今天的单次尝试。
- **换下一个模型的条件**：请求已经发出之后的任何失败——网络/连接错误、超时（含 240 s 总时限）、
  HTTP 4xx/5xx（含 401/402/403/429）、空内容、JSON 解析失败、v2 合同校验失败。
  **不换**：请求发出之前的失败（媒体不可读、payload 组装失败），那不是提供商的问题。
  这对应 OpenMinis 的 `FallbackStrategy.always`，比 `limited` 更贴近用户"第一个不行就用第二个"的要求。
- **链级时间预算**（关键约束，来自 `recognition_experiments.MIMO_REQUEST_TOTAL_DEADLINE_SECONDS` 的
  注释：240 s + 60 s 必须落在 300 s 作业认领租约内）：`authoritative_recognition` 整条链共用
  **240 s** 预算；每个请求的总时限 = min(240, 剩余)；剩余不足 **20 s** 不再起下一个模型。
  测试必须钉死"最坏情况 ≤ 300 s"。其他环节预算 = 链内各模型 timeout 之和，无额外上限。
- **记录**：`mimo_recognition_runs.model` = 最终给出答案的模型（全失败则为链首）；
  `mimo_recognition_attempts` 新增可空列 `model VARCHAR(128)`（走 `db.SQLITE_COMPAT_COLUMNS`
  自动补列），每次尝试写实际用的模型。`prompt_invocations` / `recognition_decisions` 的
  `model` / `engine` 字段同样写实际模型。`RouterResult` 带 `fallback_from`（被跳过的模型 id 列表）。
- **与 MiMo 供应商健康线（`docs/mimo-provider-reliability-status.md`）的关系**：
  - 主模型失败的尝试照常分类（`classify_provider_failure`）、照常进入故障期判定与告警——用户应当知道
    主模型挂了。告警文案追加"已切换到备用模型 <id> 继续识别"（沿用 `_safe_text`）。
  - **故障期推导（`derive_provider_outage`）与连续失败计数（`derive_failure_streaks`）只看链首模型
    的尝试**（按新列 `model` 过滤；旧行 `model IS NULL` 视为链首）。备用模型的成功**不算**主模型恢复；
    主模型在后续消息里被再次尝试并成功才算恢复（每条新消息都从链首开始，天然会重试主模型）。
  - 一条消息通过备用模型得到了权威判定 = 已回答，`provider_outage_replay` 不会重放它（自然成立，
    但要有用例证明）。
  - 已知限制（记入 status 文档，不在本次做）：主模型故障期间每条消息都先付一次主模型失败的代价
    （402 是瞬时的，超时则是 60 s+）；跨消息的熔断/冷却是后续课题。
- 探测（`mimo_provider_probe`）探链首模型；链首变更后探的就是新链首。

## 5. Web 页面与 API

入口：右上角 ⚙ 设置菜单。「AI配置」改名为「**AI提供商**」，「AI模型选择」保留名字、内容重做；
底部快捷入口同步。两页都沿用现有 `dashboard-tab-panel` / `data-dashboard-tab` 机制与
`ai-recognition-panel` 样式（不引入新框架，`app.js` / `app.css` 内追加）。

### 5.1 「AI提供商」页（`data-dashboard-panel="config"` 重做）

- 提供商卡片列表：名称、Base URL、API Key（脱敏占位；留空保持）、超时、启用开关、
  **「测试连接」**按钮（`POST /api/ai-providers/{id}/test`，body `{model_id}`，发 `max_tokens=1`
  的 ping，返回 `ok / http_status / latency_ms / failure_class`，复用 `mimo_provider_probe` 的分类）。
- 每张卡片下是该提供商的模型列表：模型名、显示名、文本 / 图片 能力勾选、启用开关、删除；
  「添加模型」。
- 「添加提供商」：预设按钮 DeepSeek / 智谱 / MiMo / 自定义（OpenAI 兼容），预填 base_url 与常用模型。
- 删除提供商或模型时，若仍被某个 stage 引用，前端提示并由后端 422 拒绝（列出引用的环节）。
- 保存整页：`PUT /api/ai-providers`，body `{providers: [...], models: [...]}`；返回规范化结果。
- `GET /api/ai-providers` 返回同结构（Key 脱敏）。

### 5.2 「AI模型选择」页（`data-dashboard-panel="model-selection"` 重做）

- 一张表，每行一个 stage：中文名、一句话说明、能力要求标签、是否生产路径标签、
  **有序模型列表**（第 1 个标"主用"，其余标"备用 n"；每项可上移/下移/移除；
  「添加备用」下拉只列满足能力要求且启用的模型）。
- 行尾显示"当前生效：<链首 label>"；`strategy_alert` / `research_chat` 为空时显示
  "未绑定，沿用环境变量 <model>"。
- 页首一行说明：`runtime_incident_agent` 使用独立配置，不在此处设置。
- 保存：`PUT /api/ai-stages`，body `{stages: {stage_key: [model_id, ...]}}`；后端按 §3 校验。
- `GET /api/ai-stages` 返回 `{definitions: [...], stages: {...}, effective: {stage_key: [resolved models]}}`。

### 5.3 兼容

- 现有 `POST /api/ai-recognition-config` 保留，内部映射到 v2 结构（旧字段 → 派生规则同 §3.1），
  现有 `tests/test_ai_prompt_api.py` 等不改语义；`app.js` 里两个旧表单的提交逻辑改为调新接口。
- 模板变量 `ai_recognition_config` 继续可用（派生字段仍在）。

## 6. 运行时接线（按环节）

| 环节 | 改法 |
|---|---|
| `authoritative_recognition` | `_find_mimo_model` 改为 `resolve_stage_chain(config, "authoritative_recognition")`；`run_mimo_authoritative_for_message` 与 `infer_mimo_authoritative_v2` 内部改为遍历链（同模型重试保留在链内每个模型上），链级 240 s 预算；run/attempt 记录实际模型。`_run_v1_authority_with_audit` 里的 `image_provider.model or "mimo-v2.5"` 改为链首。 |
| `context_resolution` | `_select_provider` 改为链；`_default_model_caller` 外包一层 `run_with_fallback`。记录实际模型。 |
| `semantic_review` | `config.text_provider` 改为 `semantic_review` 链 + fallback。 |
| `strategy_alert` | 调用时（不是启动时）解析链；链空则用现在的 `StrategyAlertConfig`（env）。`request_strategy_alert_decision` 是 async httpx，fallback 需要一个 async 版本的 `run_with_fallback`（或以同步小函数 `to_thread`）。 |
| `research_chat` | 同上，链空则用 `app.state.llm_proxy_config`。 |
| `batch_text_recognition` / `batch_image_recognition` | `message_recognition` 里 `config.text_provider` / `config.image_provider` 改读链首（保持单次尝试，加 fallback 属于加分项）；GLM-OCR 判定改为按 model 名（`_is_glm_ocr_model`）不变。 |
| 探测 / 提示词测试 | 按 §2.1 派生规则取链首。 |

所有接线遵守：**不改变任何识别、策略解析、执行的语义**，只改变"用哪个模型 + 失败后是否换模型"。
`tests/test_recognition_authority_architecture.py` 之类的架构守卫测试必须继续通过。

## 7. 分阶段实施（子代理执行；每阶段独立提交，测试全绿再进下一阶段）

1. **阶段 1 配置层**：schema v2 dataclass、迁移、load/save、派生兼容视图、`AI_STAGE_DEFINITIONS`、
   `ai-config-show` CLI。测试：迁移幂等、校验、example.yaml 往返、旧测试全绿。
2. **阶段 2 路由与权威识别**：`ai_model_router`、attempts 新列、权威识别 v1/v2 链接线、健康线按链首过滤、
   告警文案、预算/租约测试、fallback 成功不重放的用例。
3. **阶段 3 其余环节**：context_resolution、semantic_review、strategy_alert、research_chat、batch_*、探测、提示词测试。
4. **阶段 4 Web**：两个 API 组、两页模板 + JS + CSS、旧接口兼容、`test_prompt_center_assets` 风格的模板/资源测试；
   用浏览器实际打开页面各操作一遍并截图到 status 文档。
5. **阶段 5 文档**：`docs/ARCHITECTURE.md` 新增"AI 模型路由"一节（模式表 + 环节表）、
   `config/ai_recognition.example.yaml` 改为 v2、status 文档收口、README 提一句。

不做：部署（`tg-deploy`）与推送共享分支——留给指挥会话与用户决定。

## 8. 验收

- Web ⚙ 菜单能进「AI提供商」与「AI模型选择」；提供商可增删改、可测试连接；每个 §2.1 环节都能
  独立选模型并排备用顺序；保存后 GET 回读一致；worker 下一条消息即用新配置（不重启）。
- 把 `authoritative_recognition` 设为 `[mimo-v2.5, <备用多模态模型>]`，用测试桩让首个模型返回
  402 / 超时 / 坏 JSON 三种情况，都能由第二个模型给出权威判定，run 记录 `model` 为备用模型，
  attempt 1 记录主模型失败，主模型故障告警照发且文案带"已切换到备用模型"，最坏总耗时 ≤ 300 s。
- `context_resolution` 与其余环节同样能配链并切换。
- 旧 `config/ai_recognition.yaml`（v1）不经人工编辑即可加载，行为与迁移前逐环节一致（测试断言）。
- `pytest -q` 全绿；不新增依赖。
