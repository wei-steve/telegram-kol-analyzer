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

## 9. 阶段 6：提供商预设目录、模型列表拉取、统一端点拼接（2026-09-14 追加）

用户反馈：预设只有 DeepSeek / 智谱 / MiMo 三个，常见提供商都没有。参照 OpenMinis：它的提供商目录
来自 models.dev（`src/ios/Resources/models-dev-api.json` 是快照；线上 `https://models.dev/api.json`），
每个提供商带 OpenAI 兼容 `api` 根地址，每个模型带 `modalities.input`（是否收图）、`release_date`。

### 9.1 预设目录（数据文件 + 生成脚本）

- 新增 `scripts/build_ai_provider_presets.py`：读 models.dev（优先线上，失败用 `--source` 指定的本地快照），
  按下面的白名单筛选提供商，为每个提供商挑**最近发布的 ≤ 8 个聊天模型**（`modalities.output` 含 text；
  排除 id 匹配 realtime / tts / transcribe / whisper / audio / image-gen / imagine / video / embedding /
  moderation / sora / dall-e 的），写成 `src/telegram_kol_research/ai_provider_presets.json`
  （提交进仓库；生成脚本是可复现的来源，不在运行时联网）。
- 提供商白名单与 base_url（models.dev 没给 `api` 的用官方 OpenAI 兼容地址）：

| 组 | preset id | 显示名 | base_url | 备注 |
|---|---|---|---|---|
| 国内 | deepseek | DeepSeek | https://api.deepseek.com | 现有 |
| 国内 | zhipuai | 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | 现有 |
| 国内 | xiaomi | 小米 MiMo | https://api.xiaomimimo.com/v1 | 现有 |
| 国内 | alibaba-cn | 阿里百炼（通义 Qwen） | https://dashscope.aliyuncs.com/compatible-mode/v1 | |
| 国内 | moonshotai-cn | 月之暗面 Kimi | https://api.moonshot.cn/v1 | |
| 国内 | siliconflow-cn | 硅基流动 | https://api.siliconflow.cn/v1 | 聚合 |
| 国内 | stepfun | 阶跃星辰 | https://api.stepfun.com/v1 | |
| 国内 | minimax-cn | MiniMax | https://api.minimaxi.com/v1 | models.dev 给的是 Anthropic 格式地址，这里用其 OpenAI 兼容地址；模型列表取自 models.dev 的 `minimax-cn` |
| 国内 | volcengine | 火山方舟（豆包） | https://ark.cn-beijing.volces.com/api/v3 | 不在 models.dev；模型 id 是用户自己的接入点，预设不带模型 |
| 国际 | openai | OpenAI | https://api.openai.com/v1 | |
| 国际 | anthropic | Anthropic | https://api.anthropic.com/v1 | 走 Anthropic 的 OpenAI 兼容层 |
| 国际 | google | Google Gemini | https://generativelanguage.googleapis.com/v1beta/openai | OpenAI 兼容层 |
| 国际 | xai | xAI Grok | https://api.x.ai/v1 | |
| 国际 | openrouter | OpenRouter | https://openrouter.ai/api/v1 | 聚合，模型太多：预设只带 8 个，靠"拉取模型列表" |
| 国际 | groq | Groq | https://api.groq.com/openai/v1 | |
| 国际 | mistral | Mistral | https://api.mistral.ai/v1 | |
| 本地 | ollama | Ollama（本机） | http://127.0.0.1:11434/v1 | 无 Key；模型靠拉取 |
| 本地 | lmstudio | LM Studio（本机） | http://127.0.0.1:1234/v1 | 同上 |
| — | custom | 自定义（OpenAI 兼容） | 空 | 现有 |

- `GET /api/ai-provider-presets` 返回目录（分组、id、显示名、base_url、文档链接、模型列表含
  `supports_image`）。前端预设按钮**从这个接口渲染**，不再写死在 HTML/JS 里。
- 点预设：新建一张提供商卡片，预填 base_url、显示名、预设模型（用户删掉不要的再保存）；
  provider id 取 preset id（已存在则加 `-2`）。对已存在的提供商卡片提供「补充预设模型」按钮。
- 预设里的模型名以 models.dev 为准、随生成日期一起写进 JSON（`generated_at`、`source`），
  页面上注明"预设仅供起步，以「拉取模型列表」为准"。

### 9.2 拉取模型列表

- `POST /api/ai-providers/{id}/models`：用该提供商的 base_url + Key 请求 `GET {base}/models`
  （OpenAI 标准；Anthropic 兼容层需要额外 `x-api-key` 与 `anthropic-version` 头，一律附带，
  对其他提供商无害），15 s 超时，返回 `{models: [{id, owned_by?}], error?}`；失败按
  `classify_provider_failure` 给出 `failure_class`。
- 页面：卡片上「拉取模型列表」→ 弹出可勾选清单（已存在的置灰），勾选后加入卡片，
  能力默认只勾"文本"，若 id 与预设目录里某条一致则沿用其 `supports_image`。

### 9.3 统一端点拼接（修隐患）

现状有 7 处各自拼 URL，规则不一致：`message_recognition` / `context_resolution` /
`semantic_disagreement_review` 是"base 以 `/v1` 结尾则加 `/chat/completions`，否则加
`/v1/chat/completions`"；`strategy_alerts` / `llm_chat` 一律加 `/v1/chat/completions`；
MiMo 直调与探测一律加 `/chat/completions`。Gemini（`/v1beta/openai`）、Groq（`/openai/v1`）、
百炼（`/compatible-mode/v1`）、火山（`/api/v3`）、智谱（`/api/paas/v4`）在前两类规则下都会拼错。

新规则，放在一个模块 `ai_endpoints.chat_completions_url(base_url)`，7 处全部改用：
**base_url 的路径为空或仅 `/` 时加 `/v1/chat/completions`；否则加 `/chat/completions`**
（base 已以 `/chat/completions` 结尾则原样）。对现有配置逐一核对不变：
`https://api.deepseek.com` → `/v1/chat/completions`（同前）；`https://api.xiaomimimo.com/v1` →
`/v1/chat/completions`（同前）；env 默认 `http://127.0.0.1:8317` → `/v1/chat/completions`（同前）。
`{base}/models` 同理由 `ai_endpoints.models_url(base_url)` 给出。测试逐条钉死上表每个 base_url 的结果。

### 9.4 验收

- ⚙ →「AI提供商」看到三组预设按钮（国内 9、国际 7、本地 2、自定义 1）；点「阿里百炼」得到预填卡片，
  带最近的 Qwen 模型且图片能力标注正确；点「Ollama」不要求 Key。
- 对 DeepSeek 点「拉取模型列表」能列出 `deepseek-chat` 等；对不可达地址给出明确失败类别。
- 7 处调用的 URL 全部经 `chat_completions_url`，单元测试覆盖上表全部 base_url。
- 全量测试绿；不新增依赖；`ai_provider_presets.json` 可由脚本重新生成。

## 10. 阶段 7：显式「自动追加 /v1」开关 + 端点预览（2026-09-14 追加，用户指出 OpenMinis 已有此设计）

OpenMinis 的提供商配置页有三样东西：「Sign in with <provider>」OAuth 登录、「自动追加 "/v1"」开关
（`ProviderInstance.appendV1Suffix`，默认开，提示"只填主机地址"）、「API FORMAT: Chat Completions / Responses API」。
本项目采用第二项；前两项不采用：OAuth 登录是移动端个人账号场景，服务端只用 API Key；
Responses API 本项目所有环节都是 chat/completions 合同，改格式没有收益。

### 10.1 数据

- `AiProvider` 增加 `append_v1: bool | None`，YAML 键 `append_v1`。**缺省（None）时按 §9.3 的现有规则推导**
  （路径为空或 `/` → True，否则 False），因此现有 v2 文件与 v1 迁移的结果**逐字节不变**；save 时把推导结果落盘，
  以后就是显式值。
- `ai_endpoints.chat_completions_url(base_url, append_v1=None)` / `models_url(...)`：`append_v1=True` 时，
  路径不以 `/v1` 结尾则先补 `/v1`（已以 `/v1` 结尾不重复补）；`False` 时原样；`None` 时走旧推导。
  7 处调用点传入 `provider.append_v1`。
- 预设目录（`ai_provider_presets.json`）每家带 `append_v1`：base_url 是裸主机的（DeepSeek）为 True，
  其余（已含 `/v1`、`/api/paas/v4`、`/v1beta/openai`、`/openai/v1`、`/compatible-mode/v1`、`/api/v3`）为 False。

### 10.2 页面

- 提供商卡片 Base URL 下方一个开关「自动追加 "/v1"」，说明文字：
  「打开：只填主机地址（如 https://api.openai.com）；关闭：填完整 API 根地址（如 https://open.bigmodel.cn/api/paas/v4）」。
- 开关旁实时显示**端点预览**：「将请求 https://…/chat/completions」（前端用与后端相同的规则算；
  `GET /api/ai-providers` 同时返回 `chat_completions_url` 供核对）。点预设时开关按预设值设好。
- `PUT /api/ai-providers` 接受 `append_v1`；缺省按 10.1 推导。

### 10.3 验收

- 现有三家与 env 默认地址的最终 URL 不变（测试逐条钉死，沿用 `test_the_urls_already_in_production_are_unchanged`）。
- `https://proxy.example.com` + 开关关 → `https://proxy.example.com/chat/completions`；
  `https://host/api` + 开关开 → `https://host/api/v1/chat/completions`；
  `https://api.xiaomimimo.com/v1` + 开关开 → 不重复补 `/v1`。
- 页面上切换开关，预览实时变化；保存后 GET 回读 `append_v1` 与预览一致。
