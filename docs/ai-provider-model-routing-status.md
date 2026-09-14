# AI Provider & Model Routing Status（AI 提供商 / 按环节模型选择 / 备用切换）

设计：`docs/plans/2026-09-13-ai-provider-model-routing-design.md`（唯一设计真相）。
本文件是本项目的进度、决策与证据真相；每个阶段完成后更新。

```yaml
project: ai-provider-model-routing
started: 2026-09-13
integration_branch: codex/deepcoin-auto-trading-v1
base_commit: 869b7a06   # 设计文档提交；实施从这里起
implementer: 子代理 opus-implementer（Opus 5 / high）
commander: Claude Fable 5.1 指挥会话
current_phase: 6
phase_status: completed      # planned | in_progress | completed | blocked
deploy: c79db7042cc251a54a86b3a9609cfc01bae68ba6   # 2026-09-14T11:41Z tg-deploy；回滚参考 ef1688c5b4c4f291bafd10e57993502028a5f3f1
shared_branch_verified: PASS   # 部署 sha 在 origin/codex/deepcoin-auto-trading-v1 上，0 code files beyond production。上线步骤见「上线核对」一节
```

## 阶段总览

| 阶段 | 内容 | 状态 | 提交 | 测试 |
|---|---|---|---|---|
| 1 | 配置层：schema v2、迁移、load/save、派生兼容视图、stage 目录、`ai-config-show` | completed | `18d62606` | 全量 8713 passed / 0 failed / 4 skipped |
| 2 | 路由 + 权威识别链接线 + attempts `model` 列 + 健康线按链首过滤 + 预算/租约测试 | completed | `4e5e37ad` | 全量 8746 passed / 0 failed / 4 skipped |
| 3 | 其余环节接线（context_resolution / semantic_review / strategy_alert / research_chat / batch_* / 探测 / 提示词测试） | completed | `0ad2b842` | 全量 8768 passed / 0 failed / 4 skipped |
| 4 | Web：`/api/ai-providers*`、`/api/ai-stages`、两页模板 + JS + CSS、旧接口兼容、浏览器验证 | completed | `d287038f` | 全量 8786 passed / 0 failed / 4 skipped |
| 5 | 文档：ARCHITECTURE 新节、example.yaml v2、README、本文件收口 | completed | `4aadf158` | 全量 8788 passed / 0 failed / 4 skipped |
| 6 | 提供商预设目录（19 家）、拉取模型列表、统一端点拼接 | completed | 见下方证据 | 全量 8841 passed / 0 failed / 4 skipped |

## 阶段 1 做了什么

新增 `src/telegram_kol_research/ai_stage_catalog.py`：`AiProvider` / `AiModel` /
`AiStageDefinition` / `AI_STAGE_DEFINITIONS`（设计 §2.1 的 7 个 stage_key，中文名与能力要求照抄）、
provider id 的 host 映射与 slug 规则。该模块不 import 包内任何其他模块，Web、路由、配置层都能直接读。

`ai_recognition_config.py`：
- `AiRecognitionConfig` 新增 `providers` / `models` / `stages` / `config_warnings`；旧字段全部保留。
- `migrate_v1_ai_config(...)`：纯函数、幂等的 v1 → v2 迁移（设计 §3.1）。
- `normalize_ai_config_v2(...)`：一次性给出 `(providers, models, stages, warnings, errors)`；
  load 只吃 warnings，save 把 errors 抛成 `AiRecognitionConfigValidationError`（阶段 4 映射成 422）。
- `resolve_stage_models(config, stage_key)`：环节的有效模型链（跳过 disabled / 无 base_url 的成员）。
- `load_ai_recognition_config`：识别 `schema_version`；缺失即 v1，按 §3.1 在内存里迁移，**不落盘**。
- `save_ai_recognition_config`：一律写 `schema_version: 2`。
- `build_ai_config_view(config)`：脱敏视图（`api_key_configured` + 末 4 位），CLI 与阶段 4 的 GET 共用。

`cli.py`：新增 `telegram-kol-research ai-config-show [--ai-config-path PATH] [--json]`，
只读打印 providers / models / 每个 stage 的绑定与有效链，API Key 只显示末 4 位。

## 阶段 2 做了什么

新增 `src/telegram_kol_research/ai_model_router.py`：
- `resolve_stage_chain(config, stage_key)`；
- `run_with_fallback(chain, attempt, *, budget_seconds, min_remaining_seconds, classify, monotonic)`
  → `RouterResult(succeeded, model, value, fallback_from, failures, skipped_for_budget)`；
- `MIN_REMAINING_SECONDS = 20.0`；`request_reached_provider` 是本项目用的分类器
  （读异常上挂的 `MimoProviderAttemptTelemetry.provider_request_made`）。

审计：`mimo_recognition_attempts` 新增可空列 `model VARCHAR(128)`
（ORM + `db.SQLITE_COMPAT_COLUMNS` 补列 + `record_mimo_attempt(model=...)` + 视图字段）。
`complete_mimo_run(model=...)` 可在完成时把 run 的模型改写成真正答题的那个。

权威识别：
- `recognition_experiments.resolve_authoritative_chain(config)` 是唯一取链处；
  `_find_mimo_model` 保留为兼容包装（返回链首），探测与提示词中心照旧用它。
- `infer_mimo_authoritative_v2`（v2）与 `run_mimo_authoritative_for_message`（v1）都遍历链，
  每个模型内部保留原有的同模型重试与单请求 240 s 时限，**整条链共用 240 s 预算**。
- run 的 `model` 是答题模型（全失败写链首）；每条 attempt 写实际模型；
  `prompt_invocations` 与 `recognition_experiments` 结果里的 model 同样是实际模型。
- `authoritative_recognition._run_v1_authority_with_audit` 的
  `image_provider.model or "mimo-v2.5"` 改为链首；并按链里每个模型各写一条 attempt 行。

健康线（`mimo_provider_health`）：
- `resolve_chain_head_model()` 读 `authoritative_recognition` 链首；
  `_load_recent_attempt_rows` / `_load_streak_rows` 只取 `model IS NULL OR model = <链首>`；
  `load_latest_provider_outage` / 两个 tick 都接受 `chain_head_model` 或 `config_loader`。
- 故障告警在检测到备用模型正在答题时，摘要多一个 `fallback_note`
  （`已切换到备用模型 <id> 继续识别`，走 `_safe_text`），`impact` 改为
  `authoritative_recognition_on_fallback_model`；Telegram 文案的「影响」行随之改写，
  不再说「新消息无法完成权威识别」。`runtime_incidents._SUMMARY_FIELDS` 相应放行 `fallback_note`。

## 阶段 3 做了什么

`ai_model_router` 新增 `async_run_with_fallback`：语义与同步版逐条相同，只是 `await` 那次尝试，
共用 `RouterResult`；测试把两者对照跑。

| 环节 | 取链方式 | 记录实际模型 | 空链 / 无 v2 表时 |
|---|---|---|---|
| `context_resolution` | `resolve_context_model_chain` | `ContextResolutionAttempt.model` | 回退到 `context_resolution_model_id` 指的那条，否则 `text_provider` |
| `semantic_review` | `resolve_semantic_review_chain` | `SemanticReviewRun.model` + `prompt_invocations` | 回退到 `text_provider`；未配置则空链 |
| `strategy_alert` | `resolve_strategy_alert_chain`（调用时解析） | `prompt_invocations.model` | 沿用现有 `StrategyAlertConfig`（env） |
| `research_chat` | `llm_chat.resolve_research_chat_chain`（每次提问解析） | `prompt_invocations.model` + `proxy_payload.model` | 沿用 `app.state.llm_proxy_config` |
| `batch_text_recognition` | `_batch_text_provider`（链首，单次尝试不变） | 原有 `engine` 字段 | 回退到 `text_provider` |
| `batch_image_recognition` | `_batch_image_provider`（链首，单次尝试不变） | 原有 `engine` 字段 | 回退到 `image_provider` |
| 探测 / 提示词中心 mimo | `_find_mimo_model`（阶段 2 已是链首包装） | 不变 | 不变 |
| 提示词中心 deepseek | `prompt_testing._deepseek_provider` → `batch_text_recognition` 链首 | `_model_name` | 回退到 `text_provider` |

`context_resolution` 与 `semantic_review` 把「发请求 + 解 JSON + 过合同」整个放进链内的一次尝试里，
因为这三件事失败都意味着「这个模型没给出答案」，下一个模型可能会（设计 §4）。
两者都没有链级时限：它们不在 300 s 作业认领租约里，预算就是各模型自己的 timeout 依次相加。

健康线：`resolve_chain_head_model` 加了按文件身份 `(路径, mtime_ns, size)` 的缓存，
两个 tick 每轮各问一次链首只会解析一次 YAML；页面保存后文件变了，下一轮就看到新链首。

## 阶段 4 做了什么

四组接口（`web_app.py`）：
- `GET/PUT /api/ai-providers`：`{providers, models}`；Key 只写不读（GET 只给
  `api_key_configured` + 末 4 位，PUT 留空 = 保持原值）；`AiRecognitionConfigValidationError` → 422。
- `POST /api/ai-providers/{id}/test`：body `{model_id}`，复用
  `mimo_provider_probe.probe_mimo_provider` 的 `max_tokens=1` 探测与分类，返回
  `ok / http_status / latency_ms / failure_class / kind / error_type`。
  探测器由 `create_web_app(ai_provider_prober=...)` 注入，测试里不打网络。
- `GET/PUT /api/ai-stages`：`definitions` + `stages` + `effective` + `models` +
  `routable_model_ids` + `warnings`。
- 删除仍被某个 stage 引用的 provider / model → 422，并列出引用它的环节名。

两页（`templates/index.html` + `static/app.js` + `static/app.css`，沿用既有
`dashboard-tab-panel` / `ai-recognition-panel`，没引入任何新依赖）：
- ⚙ 菜单「AI配置」改名「AI提供商」，「更多工具」底部快捷入口补上「AI 模型选择」。
- 「AI提供商」：提供商卡片（id / 名称 / Base URL / Key 脱敏 / 超时 / 启用 / 测试连接 / 删除）
  + 卡片下的模型列表（模型 id / 模型名 / 显示名 / 文本、图片能力 / 启用 / 删除 / 添加模型）
  + 预设按钮 DeepSeek / 智谱 / MiMo / 自定义。
- 「AI模型选择」：每个 stage 一行 —— 中文名、一句话说明、能力标签、生产路径标签、
  有序链（主用 / 备用 n，上移 / 下移 / 移除）、「添加备用」下拉（只列满足能力且启用的模型）、
  「当前生效」；`strategy_alert` / `research_chat` 空链显示「未绑定，沿用环境变量 …」；
  不参与路由的成员画成灰色「已绑定，未参与路由」；页首注明 `runtime_incident_agent` 不在此处设置。
- 旧 `POST /api/ai-recognition-config` 保留，`app.js` 不再用它；
  旧的整表覆盖（`collectAiModelConfigs` 那条路径）连同它的浏览器 Key 缓存一起删掉了。

`cli.py` 的 `web` 命令新增 `--ai-recognition-config-path`，这样本地预览可以指向一份
一次性的配置而不碰真实文件；`.claude/launch.json` 里的 `ai-routing-preview` 就是它。

## 阶段 5 做了什么

- `docs/ARCHITECTURE.md` 新增 **5.5 AI 模型路由**：schema v2 三层结构、7 个环节表
  （stage_key / 中文名 / 能力 / 生产路径 / 取链的代码位置）、换模型的规则与时间预算、
  健康线按链首过滤、`runtime_incident_agent` 例外、两页与 `ai-config-show`。
  第 6 节「AI 协作提示」加一条：新代码取模型走 `resolve_stage_chain`，不要读旧字段。
- `config/ai_recognition.example.yaml` 改成 v2 形态，带注释说明 v1 会自动迁移、
  提示词那三段只是一次性种子。原来的 v1 内容冻结成
  `tests/fixtures/ai_recognition_v1_sample.yaml`，迁移测试改读它 ——
  example 已经升到 v2，再拿它当「v1 输入」就名不副实了。
- `README.md` 新增 *AI Providers and Per-Stage Models* 一节：两页、链与备用、
  v1 文件仍可用、`ai-config-show`，并指向 ARCHITECTURE 5.5。
- 本文件加「交接摘要」与「上线核对」两节。

## 阶段 6 做了什么（设计 §9）

用户反馈：「提供商太少了，常见的几个提供商都没有」。

**6c 统一端点拼接（先做，另外两件都依赖它）** —— 新模块 `ai_endpoints.py`：
`chat_completions_url` / `models_url`，规则是「base_url 的路径为空或仅 `/` → 加
`/v1/chat/completions`；否则 → 加 `/chat/completions`；已以 `/chat/completions` 结尾则原样」。
原来 7 处各自拼 URL，三套不同规则，Gemini（`/v1beta/openai`）、Groq（`/openai/v1`）、
百炼（`/compatible-mode/v1`）、火山（`/api/v3`）、智谱（`/api/paas/v4`）在其中任何一套下都会拼错。
7 处全部改过去；现有三个 base_url 与 env 默认值的结果逐字不变，测试把这三条**写死**而不是推导。

**6a 提供商预设目录** —— `scripts/build_ai_provider_presets.py` 从 models.dev 生成
`src/telegram_kol_research/ai_provider_presets.json`（提交进仓库，运行时不联网；`--check` 可重算比对）。
19 家：国内 9 / 国际 7 / 本地 2 / 自定义 1，每家带 OpenAI 兼容 base_url 与 ≤ 8 个最新聊天模型
（图片能力读 models.dev 的 `modalities.input`，不猜）。`GET /api/ai-provider-presets` 供页面渲染，
预设按钮不再写死在 HTML/JS 里；每张提供商卡片新增「补充预设模型」。

**6b 拉取模型列表** —— `ai_provider_models.list_provider_models` + `POST /api/ai-providers/{id}/models`：
`GET {base}/models`，15 s 超时，同时带 `Authorization: Bearer` 与 `x-api-key` / `anthropic-version`，
失败经 `classify_provider_failure` 给出 `failure_class`。页面弹出可勾选清单，已在卡片上的置灰；
能力按预设目录里的同 id 条目填，未知 id 只勾「文本」。探测器/列举器都由
`create_web_app(ai_provider_prober=..., ai_model_lister=...)` 注入，接口测试不走网络。

## 设计未覆盖、由实施者决定的事项

1. **旧字段保持为普通 dataclass 字段，而不是只读 property。**
   设计 §3 说旧字段是「派生只读视图」。真做成 property 会让 `AiRecognitionConfig(text_provider=...)`
   与 `dataclasses.replace(config, context_resolution_model_id=...)` 直接失效——前者有 195 处构造点，
   后者正是 `context_authority_cutover._prepare_candidate` 换模型的方式。
   所以派生发生在**读写文件的地方**（`load` / `save`），而不是构造函数里：手工构造出来的 config 仍然
   逐字是调用方给的值。影响：阶段 3/4 里任何只设置 v2 字段、又直接读旧字段的新代码拿到的是空值——
   新代码应当走 `resolve_stage_chain`，不要读旧字段。

2. **`resolve_stage_chain` 返回现有的扁平类型 `AiModelConfig`，不是 `AiModel`。**
   `AiModelConfig` 已经是「provider + model」的合并体，`_call_mimo_direct_model` 等调用点全部读它的
   `base_url` / `api_key` / `model` / `timeout_seconds`。让链直接给出这个类型，阶段 2/3 的接线就只是
   换一个取值来源，不用改任何请求代码。`AiModel.provider` 这个「派生 provider」仍然存在：配置层
   加载时把解析好的 `AiProvider` 绑在 model 上（不参与相等比较，也不写进 YAML）。

3. **provider 去重键是 `(base_url, api_key, timeout_seconds)`，设计写的是 `(base_url, api_key)`。**
   两条同端点、同 Key、不同 timeout 的 v1 条目如果合并成一个 provider，往返一圈之后其中一条的
   请求时限会被悄悄改掉。超时是请求截止时间，本项目不做这种无声更改，所以多带一个维度；
   同 host 的第二个 provider 仍然按设计拿 `-2` 后缀。

4. **`save_ai_recognition_config` 除了 v2 结构，还把旧字段作为派生镜像一起写回。**
   设计只说「一律写 v2」。同时写旧键不改变 v2 的权威性，却保证：回滚到 v2 之前的代码时，
   服务器上的 `config/ai_recognition.yaml` 仍然是那份代码读得懂的配置，而不是空配置。

5. **旧字段仍然能决定「它自己那个环节」的链首（`_promote_legacy_heads`）。**
   `replace(config, context_resolution_model_id=X)` 与旧的
   `POST /api/ai-recognition-config` 都只能表达一个模型。它们指定的模型被提到链首，
   原来的备用留在后面，而不是把链清成一个。

6. **v1 形态的保存会保留它表达不出来的备用链（`_preserved_stage_chains`）。**
   否则在阶段 4 之前，用户只要在旧「AI配置」表单上按一次保存，就会把新页面上配好的所有备用模型删掉。
   规则：磁盘上的链与本次重建的链首相同且更长时，保留磁盘上的；本次重建为空时也保留磁盘上的。

7. **被禁用 / 没填 base_url 的成员「仍然绑定，但不参与路由」。**
   设计说「加载时跳过并 warning」。如果 load 真的把它从 `stages` 里删掉，下一次保存就会把这个绑定
   永久删除——临时禁用一个 provider 不该有这个后果。所以 `stages` 保留成员（并 warning），
   由 `resolve_stage_models` 在路由时跳过，语义与 OpenMinis 的 `availableEntryIds` 一致。
   只有「未知 model id」和「能力不满足」两种成员会真的从 `stages` 里去掉。

8. **`production_path` 对 `research_chat` 取 False。** §2.1 表里它的「生产路径」是 `web`。
   它跑在 web 进程、由人触发，不在生产消息管线上，所以布尔值取 False，
   表里那句原话另存在 `production_note` 里，页面上照原样显示。

9. **迁移用的是「解析后的」模型 id，不是 YAML 里的原始字符串。**
   设计 §3.1 写 `batch_text_recognition ← active_text_model_id`。但今天 `_select_active_model`
   会在 id 不可用时回退（先按 provider 匹配，再取第一个满足能力且已配置的模型）。
   用解析后的结果，才满足验收里那条更强的要求：「行为与迁移前逐环节一致」。

10. **链的时间预算按「每次请求」而不是「每个模型」重算（`_remaining_deadline`）。**
    设计说「每个请求的总时限 = min(240, 剩余)」。照字面只在模型开始时算一次是不够的：
    同一个模型的第二次重试会拿到一份全新的时限——这正是改动前就存在的一个洞
    （第一次尝试跑 239 s 不触顶、重试再给 240 s，两次请求加一次阻塞读会超过 300 s 租约）。
    现在每次请求前都用「这一片预算 − 已用」重算，重试延时也算在内，所以整条链的墙钟时间
    ≤ 240 s，加上最后一次阻塞读的 60 s 正好 ≤ 300 s 的作业认领租约。

11. **v1 审计改成「链里每个模型一行 attempt」。**
    改动前 v1 一次 run 只写 1 行（ordinal 1），同模型的重试藏在聚合里。
    要满足「attempt 1 是主模型失败、attempt 2 是备用成功」，就必须按模型拆行。
    单模型链的那一行与改动前逐字段一致：`started_at` 用整次调用的开始、`completed_at` 用结束、
    `duration_ms` 用整次调用的耗时（多模型链时最后一行吸收余量），所以没有配备用时什么都没变。

12. **`retry_of_ordinal` 只在同一个模型内部指向前一次。**
    备用模型的第一次请求不是主模型那次的重试，原来的 `ordinal - 1` 会把它写成重试。

13. **多模型失败时，错误信息里每个模型的摘要要先去掉 `response_body=` 尾巴
    （`ModelFailure.describe`）。**
    `mimo_recognition_runs._sanitize_error_message` 里 `response_body=.*$` 一直吃到字符串结尾，
    直接拼接会让第一个模型的响应体把后面所有模型的摘要吞掉，错误里就只剩一个模型。
    响应体本来就要被打码，所以在拼接前先截掉。单模型链的错误文本一个字节都没变。

14. **健康线读不到配置时「失败即全量统计」，而不是不统计。**
    `resolve_chain_head_model` 返回 `None` 时不加任何模型过滤，也就是改动前的行为。
    一个悄悄停止计数的健康检查，正是这个模块存在的理由。

15. **被禁用或未配置的成员不参与路由，所以链可能为空；空链 = 「未配置」。**
    v2 配置里 `authoritative_recognition` 解析为空时，走的是原本「MiMo model is not configured」
    那条路径，语义没变。只带 v1 字段的手工 config（大量既有测试）仍然回退到
    `id/model == mimo-v2.5` 的老规则，`resolve_authoritative_chain` 里写明了。

16. **健康线的两个 tick 各自读一次 `config/ai_recognition.yaml`（每轮 ~20 s 两次）。**
    没有把 `ai_recognition_config_loader` 从 `run_authoritative_gap_recovery_loop` 传下去，
    因为那要改调用点的实参，会打到所有传 stub tick 的测试；两次 YAML 解析（~40 KB）
    比这两个 tick 本来就要做的数据库扫描便宜得多。要优化留给阶段 3。

17. **`context_resolution` 的「链」是 `AiModelConfig` 列表，但调用点仍收 `AiProviderConfig`。**
    `model_caller(provider=...)` 是既有注入点，几十个测试按这个签名写的。链内用
    `candidate.provider` 取回同一个值（`AiModelConfig.provider` 每次构造的是相等的 frozen 对象），
    调用方一个字都不用改。没有 v2 表时用 `model_config_from_provider` 把旧字段包成一条单元素链。

18. **`context_resolution` 的熔断器仍然按链首计。**
    `record_success` 只在答题的就是链首时才调用——备用模型通了，不代表主模型的网络通了，
    和健康线是同一条规则。`context_fingerprint` 也仍然按链首算，否则换模型会让同一条消息的
    重试 / 去重找不到自己原来的那一行。

19. **`context_resolution` 的 provider usage 每次 attempt 仍然只记一条。**
    `existing_request_count` 同时兼着「已经做过几次 attempt」的账，链内多发的请求如果每个都记一条，
    重试计数会被顶掉、第二次 attempt 直接被跳过。usage 是尽力而为的审计，attempt 行的 `model`
    才是「谁答的」的真相。

20. **`semantic_review` 与 `research_chat` 在单模型链上重新抛出模型自己的异常。**
    这两处的调用方 match 的是合同错误的类型和文本（`pytest.raises(ValueError, match="closed contract")`、
    502 的 detail 由 `_build_chat_proxy_error_detail` 按 httpx 异常算）。只有真的有多个模型时
    才换成路由自己的合并消息。

21. **`strategy_alert` / `research_chat` 读不到 AI 配置时，回到 env 配置继续跑。**
    这两个环节的替代品是「一条提醒没发出去」「一个问题没答上」，比用上一版模型回答更糟。
    `_load_ai_config_for_alerts` / `_load_ai_recognition_config_best_effort` 吞掉异常并 warning。

22. **第 16 条改用「按文件身份缓存」而不是把 loader 传进 tick。**
    改 `asyncio.to_thread(provider_health_tick, session_factory)` 的实参会打到所有传 stub tick 的
    测试；缓存放在 `resolve_chain_head_model` 里，调用点与签名一个字都不用动，
    重复的那次只花一个 `stat()`。

23. **`/api/ai-stages` 另给一个 `routable_model_ids`。**
    设计只说 `effective` 是「解析后的模型」。但页面还要回答另一个问题：某个成员是
    「能用但还没绑」还是「绑了但不会被路由」。`effective` 只包含**已经绑定**的成员，
    拿它当判据会把刚添加、还没保存的备用画成灰色，说了一句不真的话。
    `routable_model_ids` = 启用 + provider 启用且有 base_url 的所有模型 id。

24. **两页在 tab 被点开时重新读一次数据。**
    在提供商页加完模型再切到模型选择页，下拉里必须能看到新模型。绑定时只读一次做不到。

25. **「测试连接」的探测器由 `create_web_app(ai_provider_prober=...)` 注入。**
    默认就是 `mimo_provider_probe.probe_mimo_provider`。注入点让接口测试能覆盖
    「可用 / 不可达 / 余额不足」三种回答而不打网络，也不用 monkeypatch 模块全局。

26. **删除被引用的 provider / model 由后端 422 拒绝，前端不做预检。**
    两页可以分别保存，前端的引用表随时可能是旧的；唯一可信的判断在读到当前
    `stages` 的那一侧。报错里带上引用它的环节名，用户知道该去哪一页先解绑。

27. **`web` 命令新增 `--ai-recognition-config-path`。**
    `create_web_app` 早就支持这个参数，只是 CLI 没暴露。不加它就只能拿真实的
    `config/ai_recognition.yaml` 做页面验证——这正是不该做的事。

28. **本轮没有 PNG 截图。**
    本会话的浏览器工具只能把截图返回到会话里，不能落盘。
    `docs/evidence/2026-09-13-ai-routing/` 里留的是同等可核对的东西：
    12 步操作清单、两页渲染后的实际结构、两个 API 的原样响应、保存后磁盘 YAML 的内容，
    以及一条可复现的启动命令。没有伪造任何截图路径。

29. **example.yaml 升到 v2 之后，v1 样本冻结成测试 fixture。**
    迁移测试原来读 `config/ai_recognition.example.yaml` 当「v1 输入」。example 升级后
    这条就不成立了，但迁移路径在所有生产文件都被保存一次之前都还活着，必须继续有覆盖。
    `tests/fixtures/ai_recognition_v1_sample.yaml` 是那份内容的逐字冻结副本，
    另加一条用例断言它确实还是 v1（没有 `schema_version`、没有 `stages`）。
    同时新增一条断言**发出去的 example 是 v2**，且除了两个 env 兜底的环节之外
    每个环节都解析得出模型。

30. **火山方舟带上了预设模型，设计表里写的是「不在 models.dev；预设不带模型」。**
    线上 models.dev 现在**有** `volcengine`，8 个模型 id（`doubao-seed-*`、`deepseek-v4-*-ga-*`）
    都是 Ark 可以直接当 model 名调用的。设计那句备注的事实前提已经不成立，而本轮的目的正是
    「提供商/模型太少」，所以给了预设，并在它的 note 里写明「也可以直接填你自己创建的接入点 id（ep-...）」。
    **这是我唯一一处与设计表不一致的地方，请指挥会话过目。**

31. **Ollama / LM Studio 仍然不带预设模型，即使 models.dev 有 lmstudio 的 3 条。**
    本机跑着什么只有本机知道；发一份猜测的清单比空着更糟。两家都 `requires_api_key: false`。

32. **预设 JSON 作为包数据（`pyproject.toml` 的 `package-data`）随包安装。**
    页面运行时要读它。只放在源码树里的话，安装后的包就没有这个文件。

33. **`load_provider_presets` 读不到文件时回落到「只有自定义」，不抛错。**
    预设只是让「新增提供商」快一点；目录坏了不该让整个提供商页打不开——那页的正事是编辑已有配置。

34. **`GET /models` 一律同时带 Bearer 与 `x-api-key` / `anthropic-version`。**
    Anthropic 兼容层认后者，其他家忽略它。发两个无害的头，比维护一张「谁要哪种头」的表可靠。

35. **模型列表最多返回 500 条（`MAX_LISTED_MODELS`），并按 id 排序去重。**
    OpenRouter 一家就有 360+；页面要能用，而且没人会往下翻更多。

36. **「返回 0 个模型」按失败处理（`response_invalid`），不是一个空清单。**
    2xx 加空 `data` 几乎总是代理配错了，显示一个空弹窗只会让人以为是页面坏了。

37. **6c 先于 6a/6b 提交。** 字母对应设计的 §9.1/§9.2/§9.3，但 6b 的 `models_url` 就是 6c 的规则，
    6a 的预设 base_url 也要靠它才拼得对。提交顺序按依赖走。

38. **顺手修了一条阶段 5 之后留下的失败测试。**
    `tests/test_runtime_role_selection.py::test_split_runtime_provisioning_grants_shared_configs_read_only_access`
    要求两个共享配置都是 `chmod 0640`，但已部署的 `f8e8f877` 把
    `config/ai_recognition.yaml` 改成了 `0660`（web 要能写它）。测试改成按文件分别断言，
    并额外断言 web unit 里确实有那一条 `ReadWritePaths`。

## 交接摘要（下一个会话先读这一段）

阶段 1–5 已部署（生产 sha `f8e8f877`）。**阶段 6 已完成但未部署、未推送共享分支。**

这次做完之后的事实：

1. `config/ai_recognition.yaml` 升级成 `schema_version: 2` 的三层结构
   （providers / models / stages）。**没有 schema_version 的旧文件仍然直接可用**：
   加载时在内存里迁移，不落盘；只有在页面上保存一次，文件才真的变成 v2。
2. 设计 §2.1 的 7 个环节全部按「有序模型链」取模型，主用失败自动换下一个。
   只有 `runtime_incident_agent` 有意不纳入（它有自己的 fail-closed env 配置）。
3. 只有 `authoritative_recognition` 有链级时限：整条链共用 240 s，
   加上最后一次阻塞读的 60 s 正好落在 300 s 的作业认领租约里。
4. MiMo 供应商健康线**只统计链首模型**的尝试行：备用答上来不算主模型恢复；
   告警在有备用顶着时会说「已切换到备用模型 <id> 继续识别」。
5. ⚙ 菜单里两页：「AI提供商」「AI模型选择」；接口 `/api/ai-providers*` 与 `/api/ai-stages`。
6. （阶段 6）提供商预设 19 家，来自提交进仓库的 `ai_provider_presets.json`；
   每张卡片可以「拉取模型列表」问这家提供商它自己支持什么；所有端点 URL 走 `ai_endpoints`。

改动落在这些文件（按重要性）：`ai_stage_catalog.py`（新）、`ai_model_router.py`（新）、
`ai_recognition_config.py`、`recognition_experiments.py`、`authoritative_recognition.py`、
`mimo_provider_health.py`、`context_resolution.py`、`semantic_disagreement_review.py`、
`strategy_alerts.py`、`llm_chat.py`、`message_recognition.py`、`prompt_testing.py`、
`web_app.py`、`cli.py`、`templates/index.html`、`static/app.js`、`static/app.css`、
`models.py` + `db.py`（attempts 的 `model` 列）。

**要改这块代码之前**，先读「设计未覆盖、由实施者决定的事项」那 29 条，尤其第 1 条
（旧字段不是 property）、第 10 条（预算按每次请求重算）、第 7 条（禁用成员保留绑定）。

## 上线核对（服务器上按这个顺序）

这是一次 **L1** 改动：没有 schema/数据迁移，没有交易所写语义变化，行为在单模型链上
与改动前一致。真正的变化只有「用哪个模型 + 失败后是否换模型」。

1. **tg-deploy 之后、观察窗口之前，先只读核对迁移结果。**
   `ai-config-show` 是这次新加的命令，服务器上要等代码上去了才有，所以核对只能排在
   部署之后；它不写文件、不需要重启，跑它本身是安全的：

   ```bash
   cd /opt/telegram-kol-analyzer   # 或当前 release 目录
   telegram-kol-research ai-config-show --ai-config-path config/ai_recognition.yaml
   ```

   要确认三件事：`authoritative_recognition` 的链首是 `mimo-v2.5`（**不是** glm-ocr）；
   `context_resolution` / `semantic_review` / `batch_*` 各自的链首与今天在用的一致；
   `strategy_alert` / `research_chat` 是空链（继续沿用 env）。
   如果哪一条对不上，先别在页面上保存（保存会把文件写成 v2），拿这份输出回来对。

2. **文件这时候还是 v1，什么都不用做。** 三个进程都能读 v1，迁移只发生在内存里，
   行为与部署前逐环节一致。

3. **想用备用模型时**，在 ⚙ →「AI提供商」加提供商与模型，
   再到「AI模型选择」把它排进对应环节的链里，保存。
   **第一次保存会把 `config/ai_recognition.yaml` 改写成 v2。**
   保存前先备份一份：`cp config/ai_recognition.yaml config/ai_recognition.yaml.v1.bak`。

4. **回滚**：
   - 只回滚配置：把 `.v1.bak` 拷回去即可，三个进程下一次用到时就读到它，不用重启。
   - 回滚代码（`tg-deploy <上一个 sha>`）：v2 文件里同时写着旧 v1 字段的派生镜像，
     旧代码读得懂，所以**代码回滚不需要同时回滚配置**。代价是备用链在旧代码里不生效
     （旧代码只认链首那一个模型），这正是回滚该有的行为。

5. **观察**：L1 的窗口（15 分钟或 5 条真实消息，先到为准）。要看的是
   `mimo_recognition_attempts` 里新写的 `model` 列有值，且与链首一致；
   以及 journal 里没有 `mimo provider health could not read the model chain` 这条 warning
   （出现它说明配置读不到，健康线退回了全量统计）。

## 已知限制 / 后续课题

- 主模型故障期间每条消息都先付一次主模型失败的代价；跨消息熔断/冷却未做（设计 §4）。
- 上下文结合分析触发频率偏高，本次不改（`docs/known-issues-and-deferred-work.md`）。
- 阶段 3 之后，设计 §2.1 的 7 个环节全部按链取模型。只有 `runtime_incident_agent` 有意不纳入。
- `batch_text_recognition` / `batch_image_recognition` 只取链首、保持单次尝试（设计 §6 说 fallback
  属于加分项）。它们是 CLI / 批量工具，不在生产消息管线上。
- `mimo_recognition_runs.model` 与 attempts 的 `model` 存的是**模型名**（`AiModelConfig.model`），
  不是 stage 绑定里的 model id。同名模型挂在两个 provider 下时无法区分——这和 run 表原本就有的
  歧义一样，本次没有扩大也没有解决。

- 预设目录是**生成日期那天**的 models.dev 快照（`generated_at`）。模型会过时，重新生成即可：
  `python scripts/build_ai_provider_presets.py`（可加 `--source` 指本地快照）。
  页面上已注明「预设只是起步，模型名以「拉取模型列表」为准」。
- `ai_provider_presets.json` 里带着各家的 base_url，但**不带 Key**；预设只填地址与模型名。
- 「拉取模型列表」用的是**已保存的**提供商记录，所以新加的提供商要先保存一次才能拉取。

## 证据

### 阶段 1

- 提交：`18d62606`（`feat(ai-routing): phase 1 ...`）
- 全量：`uv run python -m pytest -q` → **8713 passed, 4 skipped, 0 failed**（684 s）
- 新增测试文件：`tests/test_ai_stage_config.py`（24 例）。关键用例：
  - `test_example_v1_config_migrates_every_stage_to_production_behaviour`
  - `test_authoritative_stage_follows_find_mimo_model_not_active_image_model`
  - `test_migration_is_idempotent_through_the_derived_v1_view`
  - `test_loading_a_v1_file_never_rewrites_it`
  - `test_v1_derived_view_is_unchanged_by_the_migration`
  - `test_save_writes_schema_v2_and_reloads_identically`
  - `test_save_refuses_a_stage_member_that_cannot_serve_the_stage`
  - `test_load_skips_a_broken_stage_member_and_warns_instead_of_raising`
  - `test_disabled_members_stay_bound_but_do_not_route`
  - `test_a_v1_shaped_save_keeps_fallbacks_it_could_not_express`
  - `test_replacing_the_context_model_id_promotes_it_to_the_chain_head`
  - `test_config_view_masks_api_keys` / `test_ai_config_show_prints_the_effective_chain`
- 上线前只读核对命令（服务器上执行，不写文件）：
  `telegram-kol-research ai-config-show --ai-config-path config/ai_recognition.yaml`

### 阶段 2

- 提交：`4e5e37ad`（`feat(ai-routing): phase 2 ...`）
- 全量：`uv run python -m pytest -q` → **8746 passed, 4 skipped, 0 failed**（645 s）
- 新增测试文件：`tests/test_ai_model_router.py`（30 例）；`tests/test_ai_stage_config.py` 追加 3 例。关键用例：
  - `test_v2_falls_back_to_the_backup_model_and_records_both[http_402|timeout|bad_json]`
    —— 402 / 超时 / 坏 JSON 三种都由第二个模型给出权威判定，run.model = 备用，
    attempt 1 = 主模型失败、attempt 2 = 备用成功
  - `test_v2_retries_the_primary_within_itself_before_changing_model`
  - `test_v2_reports_both_models_when_the_whole_chain_fails` —— 错误信息含两个模型各自的摘要，
    run.model = 链首
  - `test_v2_does_not_change_model_for_an_unreadable_image`
  - `test_v2_does_not_change_model_when_the_request_never_left`
  - `test_the_next_model_is_not_started_below_the_minimum_remaining_budget`
  - `test_every_request_gets_what_is_left_of_the_one_shared_budget`
  - `test_the_whole_chain_plus_one_blocked_read_fits_inside_the_claim_lease`（240 + 60 ≤ 300）
  - `test_a_slow_first_attempt_does_not_hand_its_retry_a_fresh_deadline`
  - `test_a_single_model_chain_records_exactly_one_attempt` /
    `test_the_v1_audit_of_a_single_model_chain_is_unchanged`
  - `test_v1_falls_back_and_reports_the_answering_model` /
    `test_the_v1_audit_writes_one_row_per_model`
  - `test_the_v1_run_model_is_the_chain_head_not_the_image_provider`
  - 健康线：`test_a_backup_answering_is_not_the_primary_recovering`、
    `test_the_primary_answering_on_the_next_message_is_a_recovery`、
    `test_rows_written_before_the_column_existed_count_as_the_head`、
    `test_a_backup_answer_does_not_break_the_primary_failure_streak`、
    `test_the_outage_alert_says_a_backup_is_carrying_recognition`、
    `test_the_telegram_alert_says_recognition_continued_on_the_backup`、
    `test_a_message_answered_by_the_backup_is_never_replayed`
- 架构守卫 `tests/test_recognition_authority_architecture.py` 继续通过。
- 顺带发现（未修，不在本次范围）：`tests/test_web_app.py::
  test_ingest_loop_health_exposes_admission_state_without_database` 是一个**既有的不稳定断言**。
  它用 `assert "303" not in str(payload)` 证明 chat id 没有泄漏，但同一个 payload 里有
  `now`（带微秒的 ISO 时间戳）和 `uptime_seconds`，任一处凑出 "303" 就会失败。
  第一次全量跑到了这一下，重跑即过；单独跑、整文件跑都过。与本阶段改动无关。

### 阶段 3

- 提交：`0ad2b842`（`feat(ai-routing): phase 3 ...`）
- 全量：`uv run python -m pytest -q` → **8768 passed, 4 skipped, 0 failed**（640 s）
- 新增测试文件：`tests/test_ai_stage_routing.py`（22 例）。关键用例：
  - `test_the_async_router_decides_exactly_what_the_sync_one_decides`、
    `test_the_async_router_honours_the_same_budget_rule`、
    `test_the_async_router_does_not_change_model_when_classify_refuses`
  - `test_context_resolution_falls_back_and_records_the_answering_model` /
    `test_context_resolution_single_model_chain_is_unchanged` /
    `test_context_resolution_changes_model_for_a_body_that_will_not_decode` /
    `test_context_resolution_without_a_v2_table_keeps_the_old_rule`
  - `test_semantic_review_falls_back_and_records_the_answering_model` /
    `test_semantic_review_single_model_chain_is_unchanged` /
    `test_semantic_review_without_a_v2_table_keeps_the_text_provider`
  - `test_a_bound_strategy_alert_uses_the_chain_and_keeps_the_bot_settings` /
    `test_an_unbound_strategy_alert_keeps_the_environment_model` /
    `test_an_unreadable_ai_config_does_not_lose_the_alert`
  - `test_a_bound_research_chat_uses_the_chain_and_keeps_the_egress_socket` /
    `test_an_unbound_research_chat_keeps_the_environment_proxy`
  - `test_the_batch_stages_read_their_own_chain_heads` /
    `test_a_config_without_a_v2_table_keeps_the_v1_providers` /
    `test_glm_ocr_is_still_decided_by_the_chain_heads_model_name`
  - `test_the_prompt_centre_deepseek_test_follows_the_batch_text_chain`
  - `test_the_chain_head_is_not_reparsed_on_every_tick`

### 阶段 4

- 提交：`d287038f`（`feat(ai-routing): phase 4 ...`）
- 全量：`uv run python -m pytest -q` → **8786 passed, 4 skipped, 0 failed**（655 s）
- 新增测试文件：`tests/test_ai_provider_api.py`（18 例）。关键用例：
  - `test_providers_are_listed_with_their_models_and_no_keys`
  - `test_an_empty_key_keeps_the_stored_one` / `test_a_new_key_replaces_the_stored_one`
  - `test_adding_a_provider_and_a_model_round_trips`
  - `test_deleting_a_model_a_stage_still_uses_is_refused` /
    `test_deleting_a_provider_a_stage_still_uses_is_refused`
  - `test_a_model_on_an_unknown_provider_is_a_422_not_a_500`
  - `test_the_connection_test_reports_a_healthy_provider` /
    `test_the_connection_test_names_why_an_unreachable_provider_failed` /
    `test_the_connection_test_sends_the_stored_key_without_returning_it`
  - `test_stages_report_the_catalogue_the_bindings_and_what_routes`
  - `test_a_fallback_can_be_added_and_reordered`
  - `test_a_stage_left_out_of_the_body_keeps_its_binding`
  - `test_binding_a_model_that_cannot_serve_the_stage_is_a_422`
  - `test_a_disabled_model_stays_bound_but_stops_routing`
  - `test_the_worker_sees_a_saved_chain_without_a_restart`
- 改写的模板 / 资源用例：`tests/test_web_page_render.py::test_model_selection_page_hosts_one_row_per_stage`、
  `tests/test_web_assets_smoke.py::test_app_js_drives_the_provider_and_stage_pages`。
- 浏览器验证：`docs/evidence/2026-09-13-ai-routing/browser-verification.md`
  （12 步操作清单 + 渲染结构 + `api-ai-providers.json` / `api-ai-stages.json`）。
  实际走通了：改名后的 ⚙ 菜单与快捷入口、添加提供商与模型并保存、对不可达地址
  「测试连接」返回 `不可用（provider_unavailable）`、给 `authoritative_recognition`
  加一个备用并保存、回读一致、磁盘上升级成 `schema_version: 2` 且未填的 Key 原样保留。
  验证中发现并修掉了三个页面 bug（角色标签不渲染、切页不刷新、新加成员被错误画灰）。

### 阶段 5

- 提交：`4aadf158`（`docs(ai-routing): phase 5 ...`）
- 全量：`uv run python -m pytest -q` → **8788 passed, 4 skipped, 0 failed**（644 s）
- 新增用例：`tests/test_ai_stage_config.py::test_the_shipped_example_is_v2_and_binds_every_production_stage`、
  `::test_the_frozen_v1_sample_still_describes_a_pre_migration_file`
- 新增 fixture：`tests/fixtures/ai_recognition_v1_sample.yaml`（v1 样本的逐字冻结副本）
- 文档：`docs/ARCHITECTURE.md` 第 5.5 节、`config/ai_recognition.example.yaml`（v2）、
  `README.md` 的 *AI Providers and Per-Stage Models*。

## 部署记录（2026-09-14，指挥会话）

- 预检：生产 HEAD `ef1688c5`，候选 `c79db704` 是其后代（PASS）；无 `pyproject` / `uv.lock` 变更；
  服务器 `config/ai_recognition.yaml` 为 v1（无 `schema_version`），部署前备份为
  `config/ai_recognition.yaml.v1.bak-20260914T114144Z`。
- `tg-deploy c79db704`：HEAD 一致，worker / web / ingest 三个 unit 均 active。
- 部署后只读核对 `ai-config-show`：providers deepseek / zhipu / mimo 三个，模型三条，
  `authoritative_recognition=[mimo-v2.5]`、`context_resolution=[mimo-v2.5]`（生产原本就把
  `context_resolution_model_id` 指到 mimo-v2.5，迁移照实保留）、`semantic_review=[deepseek-v4-flash]`、
  `strategy_alert` / `research_chat` 空链沿用 env、`batch_text=[deepseek-v4-flash]`、`batch_image=[mimo-v2.5]`。
  磁盘文件未被改写（仍 v1，等页面第一次保存）。
- 本机 `GET /` 200，`GET /api/ai-stages` 与 CLI 一致、`warnings=[]`。
- 部署 sha 已推到共享分支，两个方向核对均 PASS。
- 用户在真实环境测试中；测试结论待记录。

## 部署后修正（2026-09-14，指挥会话）：web 沙箱写不了配置文件

用户在「AI提供商」页点保存得到「保存失败，请检查服务状态」。web 日志：
`OSError: [Errno 30] Read-only file system: 'config/ai_recognition.yaml'`。
原因：`telegram-kol-web.service` 是 `ProtectSystem=strict` + `ReadOnlyPaths=/opt/telegram-kol-analyzer`，
只有 `data/` 可写；文件本身又是 `root:telegram-kol-runtime 0640`。旧「AI配置」页在生产上从来没有成功写过
这个文件（文件 mtime 停在 08-23，由 root 手工改）。设计 §2 "web 保存后其他进程按需重读"的前提是 web 能写，
本次补上：

1. `deploy/systemd/telegram-kol-web.service` 增加 `ReadWritePaths=/opt/telegram-kol-analyzer/config/ai_recognition.yaml`
   （只放行这一个文件；`save_ai_recognition_config` 是原地 `write_text`，不做临时文件改名，所以文件级绑定挂载够用）。
2. 服务器 `chmod 0660 config/ai_recognition.yaml`（属主仍 root，组 telegram-kol-runtime）；
   `docs/server-deployment.md` 的权限说明同步改为 0660。
3. 安装新 unit → `daemon-reload` → 只重启 web。验证：`PUT /api/ai-providers`（原样保存）200，
   文件已升级为 `schema_version: 2`（14736 字节，Key 原样保留），worker 仍可读，web 无 traceback。

### 阶段 6

- 提交：`09c73f23`（6c 统一端点）→ `56eab0bd`（6a 预设目录）→ `69bef96b`（6b 拉取模型列表）
- 全量：`uv run python -m pytest -q` → **8841 passed, 4 skipped, 0 failed**（648 s）
- 新增测试文件：
  - `tests/test_ai_endpoints.py`（30 例）：§9.1 表里每个 base_url 的 chat/models 端点逐条钉死；
    `test_the_urls_already_in_production_are_unchanged` 把现有三条写死；
    `test_no_call_site_still_joins_this_url_by_hand` 扫源码防止有人再手拼一次。
  - `tests/test_ai_provider_presets.py`（15 例）：目录与 §9.1 表逐项相等、中文显示名逐项相等、
    每家 ≤ 8 条、本机两家不带 Key 也不带猜的模型、生成脚本的筛选与排序、目录坏掉时只剩「自定义」。
  - `tests/test_ai_provider_models.py`（8 例）：OpenAI 形状解析与去重排序、Anthropic 头随行、
    无 Key 不发 Authorization、401 / 连不上 / 空列表分别给出 failure_class、
    接口按预设目录标能力、Key 不出现在响应里。
- 浏览器验证：`docs/evidence/2026-09-13-ai-routing/browser-verification.md` 的「阶段 6 追加验证」一节
  （9 步）+ `api-ai-provider-presets.json`。实际走通了：四组预设按钮（9/7/2/1）、
  点「阿里百炼」得到预填卡片且图片能力标注正确、保存后磁盘上多出 `alibaba-cn` 与 8 个模型、
  对 DeepSeek 点「拉取模型列表」得到 `拉取失败（HTTP 401，provider_unavailable）`
  （真的打到了 `https://api.deepseek.com/v1/models`，顺带证明 §9.3 的拼接是对的）、
  「补充预设模型」把 1 条补到 4 条且不重复。

## 部署记录（2026-09-14，阶段 6，指挥会话）

- 指挥会话独立复跑全量：8841 passed / 0 failed / 4 skipped（661 s）；本地预览实测 19 个预设按四组渲染，
  点「Google Gemini」得到 8 个模型的预填卡片、图片能力自动勾选。
- 预检：生产 HEAD `f8e8f877`，候选 `f0337b84` 是其后代；唯一的非 src 代码改动是 `pyproject.toml`
  的 `package-data`，服务器是 editable 安装（`__editable__.telegram_kol_research-0.1.0.pth` → `src/`）
  且三个 unit 的 `PYTHONPATH` 都指向 `/opt/telegram-kol-analyzer/src`，预设 JSON 直接从源码树读到，
  无需重装。`/opt/telegram-kol-releases/*` 只被 monitor 系列 unit 引用，与三个角色无关。
- `tg-deploy f0337b84`：三个 unit active；`GET /` 200；`GET /api/ai-provider-presets` 返回 19 家；
  `/api/ai-stages` 有效链与部署前一致；重启后 2 分钟内三个角色 0 traceback；配置文件未被改写。
- 部署 sha 已推到共享分支，两个方向核对 PASS。回滚参考 `f8e8f877`。
