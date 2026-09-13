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
current_phase: 3
phase_status: planned        # planned | in_progress | completed | blocked
deploy: 未部署；部署与推送共享分支由指挥会话与用户决定
```

## 阶段总览

| 阶段 | 内容 | 状态 | 提交 | 测试 |
|---|---|---|---|---|
| 1 | 配置层：schema v2、迁移、load/save、派生兼容视图、stage 目录、`ai-config-show` | completed | `18d62606` | 全量 8713 passed / 0 failed / 4 skipped |
| 2 | 路由 + 权威识别链接线 + attempts `model` 列 + 健康线按链首过滤 + 预算/租约测试 | completed | `4e5e37ad` | 全量 8746 passed / 0 failed / 4 skipped |
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

## 已知限制 / 后续课题

- 主模型故障期间每条消息都先付一次主模型失败的代价；跨消息熔断/冷却未做（设计 §4）。
- 上下文结合分析触发频率偏高，本次不改（`docs/known-issues-and-deferred-work.md`）。
- 阶段 1 只改配置层，所有调用点仍走旧字段；行为与改动前一致。
- 阶段 2 只接了 `authoritative_recognition`。其余环节（context_resolution / semantic_review /
  strategy_alert / research_chat / batch_*）仍读旧字段，行为与改动前一致，阶段 3 再接。
- `mimo_recognition_runs.model` 与 attempts 的 `model` 存的是**模型名**（`AiModelConfig.model`），
  不是 stage 绑定里的 model id。同名模型挂在两个 provider 下时无法区分——这和 run 表原本就有的
  歧义一样，本次没有扩大也没有解决。

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
