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
current_phase: 2
phase_status: planned        # planned | in_progress | completed | blocked
deploy: 未部署；部署与推送共享分支由指挥会话与用户决定
```

## 阶段总览

| 阶段 | 内容 | 状态 | 提交 | 测试 |
|---|---|---|---|---|
| 1 | 配置层：schema v2、迁移、load/save、派生兼容视图、stage 目录、`ai-config-show` | completed | 见下方证据 | 全量 8713 passed / 0 failed / 4 skipped |
| 2 | 路由 + 权威识别链接线 + attempts `model` 列 + 健康线按链首过滤 + 预算/租约测试 | planned | | |
| 3 | 其余环节接线（context_resolution / semantic_review / strategy_alert / research_chat / batch_* / 探测 / 提示词测试） | planned | | |
| 4 | Web：`/api/ai-providers*`、`/api/ai-stages`、两页模板 + JS + CSS、旧接口兼容、浏览器验证截图 | planned | | |
| 5 | 文档：ARCHITECTURE 新节、example.yaml v2、README、本文件收口 | planned | | |

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

## 已知限制 / 后续课题

- 主模型故障期间每条消息都先付一次主模型失败的代价；跨消息熔断/冷却未做（设计 §4）。
- 上下文结合分析触发频率偏高，本次不改（`docs/known-issues-and-deferred-work.md`）。
- 阶段 1 只改配置层，所有调用点仍走旧字段；行为与改动前一致。

## 证据

### 阶段 1

- 提交：`feat(ai-routing): phase 1 provider/model/stage configuration (schema v2)`
  （SHA 在阶段 2 提交里补写，提交自身无法写下自己的 SHA）
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
