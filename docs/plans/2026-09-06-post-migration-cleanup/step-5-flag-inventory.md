# Step 5 任务 3 — 灰度开关清单

对象：`src/telegram_kol_research/trading_settings.py` 的 `TradingSettings` 里全部 `Literal[...]`
开关与 `*_enabled` 布尔开关，共 **18 个**（14 个 Literal + 4 个布尔）。

**本步不改任何开关的逻辑，也不改任何开关的值。** 这份表只是给下一轮决策用的输入。

## 生产当前值的取值规则

只从 `docs/*.md` 与 `docs/archive/**` 的记录里取，每格都注明出处；找不到写 `unknown`，**不推断**。
本仓库的定期生产快照（`docs/ai-context-observation-log.md`、各份 production-activation 记录）
只固定采样 `auto_trade_enabled` 与 `worker_command_mode` 两项，其余开关只能靠零散记录，
所以 `unknown` 偏多是证据现状，不是没查。

## 建议列的三种取值

- `collapse-to-live`：生产已长期稳定在终态，且 `disabled`/`shadow` 分支已无回滚价值，可以删分支；
- `keep`：仍在灰度、或是需要保留的紧急关停/回滚开关；
- `ask-owner`：生产值或灰度阶段无法从文档确定，删分支的前提不成立，必须先问所有者。

| 建议 | 个数 |
|---|---:|
| `collapse-to-live` | 2 |
| `keep` | 5 |
| `ask-owner` | 11 |

## 清单

| 开关 | 代码默认 | 生产当前值 | live 起始日期 | 依赖的 `effective_*` 规则 | 建议 |
|---|---|---|---|---|---|
| `auto_trade_enabled` | `False` | **`true`**（`docs/2026-09-05-codex-handover-closeout.md:13`，2026-09-05 生产只读复核） | unknown | 它自己就是根开关：`entry_submission_enabled` 直接等于它；`management_planning_enabled`、`live_management_execution_enabled` 以及 `effective_composite_management_v2_mode` / `effective_trigger_protection_stop_rescue_mode` / `effective_position_management_liveness_v2_mode` 的 `live` 分支全部再与它相与 | `keep`（全局急停开关，任何情况下都要留） |
| `management_execution_mode` | `"disabled"` | **`live`**（生产 monitor unit 的 `ExecStart` 里硬编码 `--expected-management-mode live`，`docs/ai-context-resolution-optimization-status.md:511`；`docs/composite-management-v2-live-verification.md:57` 亦记录 “existing `management_execution_mode=live` was unchanged”） | unknown | `management_planning_enabled`（`shadow`，或 `live` 且 `auto_trade_enabled`）、`live_management_execution_enabled`（`live` 且 `auto_trade_enabled`）；下游三个 `effective_*` 的 `live` 分支都要求它 `== "live"` | `keep`（管理侧独立于入场的关停开关；monitor 还把它当漂移判据） |
| `composite_management_v2_mode` | `"disabled"` | **`live`**（`docs/composite-management-v2-live-verification.md:107`：安全窗口后数据库与回环 API 双向 readback 均为 `composite_management_v2_mode=live`，effective 亦 `live`） | unknown（该文档无日期） | `effective_composite_management_v2_mode`：`shadow` 直通；`live` 需同时 `auto_trade_enabled` 且 `management_execution_mode == "live"` | `ask-owner`（同一文档写明回滚就是把它改回 `disabled`，是否还需要这条回滚路径要所有者定） |
| `trigger_protection_stop_rescue_mode` | `"disabled"` | `live`（`docs/archive/plans/2026-08-03-production-monitor-history-recovery.md:396` 的部署前生产快照） | `≤ 2026-08-03` | `effective_trigger_protection_stop_rescue_mode`：`shadow` 直通；`live` 需 `auto_trade_enabled` 且 `management_execution_mode == "live"` | `ask-owner`（记录已 34 天，需重新确认现值后再谈收敛） |
| `position_management_liveness_v2_mode` | `"disabled"` | `live`（`docs/archive/plans/2026-09-04-trigger-protection-lineage-attribution-design.md:330` 明写「不复用当前已为 `live` 的 `position_management_liveness_v2_mode`」） | `≤ 2026-09-04` | `effective_position_management_liveness_v2_mode`：`shadow` 直通；`live` 需 `auto_trade_enabled` 且 `management_execution_mode == "live"`。它同时是 `effective_trigger_protection_lineage_attribution_mode` 的前置 | `ask-owner`（是新灰度 lineage 开关的上游门，收敛它会连带改变 lineage 的判据） |
| `trigger_protection_lineage_attribution_mode` | `"disabled"` | **`disabled`**（`docs/2026-09-04-trigger-protection-lineage-production-activation.md:20`：候选已激活但「the production setting … remained `disabled`; no setting was changed in this deployment」） | 尚未 live | `effective_trigger_protection_lineage_attribution_mode`：`shadow` 需 `effective_position_management_liveness_v2_mode ∈ {shadow, live}`；`live` 需该值为 `live` **且** `trigger_protection_lineage_activation_after_intent_id` 是 `int` 且 `≥ 0` | `keep`（2026-09-04 才上线的休眠功能，正处灰度起点） |
| `entry_preamble_mode` | `"disabled"` | `unknown` — 见下方脚注 [1] | unknown | 无 `effective_*` 包装；由 monitor 的 `--expected-entry-preamble-mode` 做漂移比对 | `ask-owner` |
| `entry_message_assembly_v2_mode` | `"disabled"` | `unknown`（`docs/server-deployment.md:863` 只写部署时须为 `disabled`；`docs/runbook.md:1694/1706` 是切 `shadow` 与回滚模板） | unknown | 无 `effective_*` 包装；monitor `--expected-entry-message-assembly-v2-mode` 做漂移比对 | `ask-owner` |
| `entry_revision_v2_mode` | `"disabled"` | `unknown`（`docs/deepcoin-contract-cache-ownership-repair-status.md:474` 记的是一次把 `auto_trade_enabled=false` 与 `entry_revision_v2_mode=disabled` 一起设的静默处置，不是常态值） | unknown | 无 `effective_*` 包装；monitor `--expected-entry-revision-v2-mode` 做漂移比对 | `ask-owner` |
| `multi_instruction_mode` | `"disabled"` | `unknown`（`docs/migration-handoff.md:877`：「The rollout is dormant by default. `multi_instruction_mode=shadow` retains the …」，是设计描述不是生产读数） | unknown | 无 `effective_*` 包装；与 `multi_instruction_activation_after_raw_message_id` 水位联合生效 | `ask-owner` |
| `instruction_execution_contract_mode` | `"disabled"` | `unknown`（只有 `docs/archive/plans/2026-08-10-unified-execution-truth*.md` 的计划值） | unknown | 无 `effective_*` 包装；与 `instruction_execution_entry_after_item_id` / `instruction_execution_management_after_item_id` 两个水位联合生效 | `ask-owner` |
| `deepcoin_contract_specs_mode` | `"static"` | `live`（`docs/runtime-incident-agent-status.md:322`「No activation blocker remains. Live dynamic authority is active.」；`docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md:500` 记录生产实测 `deepcoin_contract_specs_mode=live`） | `≤ 2026-08-27` | 无 `effective_*` 包装；`contract_spec_refresh` 是 worker 角色的单例任务，monitor 另有 `contract-spec-health` 端点 | `ask-owner`（同一条目写明 `static` 仍是回滚目标） |
| `mimo_contract_mode` | `"v1"` | **`v1`**（`docs/migration-handoff.md:941/958/1033`：「Production remains `mimo_contract_mode=v1` with activation watermark `0`」） | 尚未切 `v2_live_adapter` | 无 `effective_*` 包装；与 `mimo_v2_activation_after_raw_message_id` 水位联合生效 | `keep`（v2 从未激活，属未完成的上线而不是灰度残留） |
| `message_pipeline_mode` | `"queue"` | **`queue`**（`docs/post-migration-cleanup-status.md` 的 `production_modes`；步骤 3 已把 `Literal` 收窄为单值） | 已是唯一值 | 无 | `collapse-to-live`（取值集合只剩 `queue`，分支已在步骤 3 删净，剩下的只是字段本身与历史值兼容解析器） |
| `worker_command_mode` | `"queue"` | **`queue`**（同上；`docs/ai-context-observation-log.md:499` 每次窗口都复核 `worker_command_mode=queue`） | 已是唯一值 | 无 | `collapse-to-live`（同上） |
| `telegram_source_deletion_exit_enabled` | `False` | `unknown`（只有 `docs/archive/plans/2026-08-02-telegram-source-deletion-exit*.md` 要求部署时确认为 `false`） | unknown | 无 `effective_*` 包装；`source_message_deletion_worker` 是 worker 角色的常驻单例任务，与本开关是两回事 | `ask-owner` |
| `semantic_review_enabled` | `False` | `unknown`（最近一次记录是 `docs/archive/plans/2026-08-22-authoritative-failure-notification-idempotency.md:130` 要求核验 `semantic_review_enabled=false`，距今 15 天） | unknown | 无 `effective_*` 包装；`semantic_review` 是 worker 角色的常驻单例任务 | `ask-owner` |
| `context_resolution_enabled` | `False` | **`true`**（`docs/deepcoin-contract-cache-ownership-repair-status.md:3797`：`context_resolution_enabled=true`，白名单 33 个 chat id，覆盖 34 个已启用会话中的 33 个） | unknown | `context_resolution_enabled_for_chat(chat_id)` = 本开关 **且** `live_management_execution_enabled` **且** `chat_id ∈ context_resolution_live_chat_ids` | `keep`（白名单本身仍在按会话灰度，`docs/plans/2026-08-31-ai-context-resolution-analysis.md:375` 还把关掉它当作省成本选项在评估） |

[1] `entry_preamble_mode` 的间接证据：生产 `entry_preambles` 表有 16 行
（`docs/2026-09-05-batch153-history-recovery-dry-run.md:113`），monitor 近期唯一原因码是
`stale_entry_preamble_unresolved` 且明确指向 `entry_preambles.id=16`
（`docs/2026-09-05-codex-handover-closeout.md:70-71`），说明该功能在生产确实在产出数据、
且 monitor 的模式漂移判据没有报警。但**没有任何文档记下它当前的字面值**是 `shadow` 还是 `live`，
按取值规则记 `unknown`。

## 不在本表内、但顺带记录的两个布尔

`move_stop_to_breakeven_after_tp1`（默认 `True`）与 `allow_vision_auto_trade`（默认 `True`）
是交易行为参数，不是灰度三态开关，也不带 `*_enabled` 后缀，按步骤文件口径不列入本表，
**本步同样一字未动**。

## 给下一轮的三点提醒

1. **`collapse-to-live` 的两个不是灰度残留，是字段残留。** `message_pipeline_mode` 与
   `worker_command_mode` 的分支在步骤 3 就删光了，`Literal` 也收窄成了单值；现在留下的是字段本身
   加上一个把历史 `inline`/`shadow` 值降级成 `queue` 的兼容解析器。删字段要连着删 DB 里的历史键
   与 `/api/trading-settings` 的响应键，属于会被外部消费者看见的改动，不是纯内部清理。
2. **11 个 `ask-owner` 里有 6 个的生产值是 `unknown`。** 在拿到一次完整的
   `GET /api/trading-settings` 只读快照之前，任何「删 disabled/shadow 分支」的判断都没有依据。
   建议下一轮先补一次全量快照并写进 `docs/`，让这张表的第三列能一次填满。
3. **收敛顺序有依赖。** `auto_trade_enabled` → `management_execution_mode` →
   {`composite_management_v2_mode`, `trigger_protection_stop_rescue_mode`,
   `position_management_liveness_v2_mode`} → `trigger_protection_lineage_attribution_mode`
   是一条链，上游的 `effective_*` 规则被下游复用。从中间任何一环开始收敛都会改变下游的实际判据，
   必须自上而下、一次一个。
