# Codex 值守补救 · 阶段 3 实施规格：worker 补救提案 + 确定性闸门 + 人工批准后执行

日期：2026-09-26
上位设计：`docs/plans/2026-09-18-codex-oncall-remediation-design.md`（3.1、3.2、4.3 A 线、4.4、第 8 节阶段 3、第 9 节内联按钮）
前置：阶段 1、2 已在生产运行（`docs/codex-oncall-status.md` 7.1、8.8–8.15；D6 见 `docs/oncall-d6-silent-stall-rules-status.md`）
验证等级：**L3**（新增生产库表 = schema 变更；给现有交易所写入通道新增一个触发入口 = 交易所写入语义变更）
状态：**第 11 节已于 2026-09-26 由用户裁定（经调度会话转达）。** 未实施；实施排在 uncertain-attempt-closeout 与通知分流两次部署之后，由调度会话另行派发（会与它们在表结构、`telegram_bot_commands.py` / `system_operator_bot.py` 上撞文件）。

## 1. 本阶段做什么 / 不做什么

**做**：值守建案后，请 worker 为该消息**用生产数据自己算**一份补救提案；worker 在系统 bot 会话里发一条带按钮的提案消息；
**用户本人**两次点击确认后，worker 在一道不含 AI 的确定性闸门之后，走现有的
`apply_position_management_remediation_action` → `execute_management_batch` 通道执行一次减风险的管理动作，并回报结果。

**不做**（明细见第 10 节）：任何无人批准的自动执行（阶段 4）；让 Codex 的输出决定或影响执行（本阶段 Codex 只留在诊断里）；
B 线补识别（阶段 6）；G1 调止盈、G2 `/choose` 消费者、G3 `recovery_required` 重跑（阶段 5）；任何增加风险的动作；
改动主链路的任何检查；碰任何自动交易开关。

## 2. 现有代码事实（2026-09-26 在 `origin/main` = `92b43cc0` 上核实）

### 2.1 补救通道本身已经存在，但只能在服务器上敲命令触发

`src/telegram_kol_research/position_management_remediation.py`：

| 函数 | 事实 |
|---|---|
| `build_position_management_remediation_plan(session_factory, *, deepcoin_client, now)`（:107） | 先拉一份交易所快照；快照有错误或任何 TPSL 观测不完整 → **整份计划只剩一条 `exchange_snapshot_incomplete` 冲突、零动作**。然后扫描 `SignalCandidate`（`parse_source='mimo_authoritative'`、`event_type IN (close_signal, position_update)`），用 `_item_requires_remediation`（:1551：指令项 `failed/unknown`，或 `succeeded` 且结果 `skipped/shadow_planned`）挑项，**用 `resolve_management_directive` 从消息原文 + 决策确定性地重算意图、价格、比例**，按 lifecycle 串成链，只有链头是 `ready_for_approval` |
| 已有批次时 | `ready/executing/reserved/submitted/reconciling/protection_ready` → `waiting_for_reconciliation`；`succeeded` → `resolved`；`partial_failed/submit_unknown/recovery_required` → `blocked: existing_management_batch_unresolved`（**不产出动作**）；该 lifecycle 已有成功全平 → `terminally_skipped` |
| 仓位核对 | 入场腿必须 `attribution_status='verified'`、有 `pos_id`、**全部**出现在这次的 live 快照里且 `_entry_positions_match_exact_live_identity` 成立，否则 `target_live_position_not_exact`，不产出动作 |
| 动作 | `PositionRemediationAction`（:51）：`action_id`（:609，对 `raw_message_id/candidate_id/lifecycle_id/action_kind` 取指纹前 20 位）、`fingerprint`（对 `action_kind/…/pos_ids/expected_effect/evidence` 取 sha256；链头再叠加前驱状态，非链头为 `not-executable`） |
| `apply_position_management_remediation_action`（:908） | 顺序：`live_management_execution_enabled` → 重建计划、按 `action_id` 取**链头** → 指纹相等 → 以 `execution_mode='disabled'` 调 `plan_strategy_management_batch`，必须落成 `blocked/management_disabled_plan_only` → `_require_batch_matches_confirmed_action`（目标、意图、比例、pos_id 集合、仓位数量与均价）→ `_require_batch_health_matches_confirmed_action` → **再重建一次计划**，动作与链指纹不变 → 再查 live 开关 → `_require_exchange_snapshot_fingerprint`（**再拉一次交易所快照**，指纹必须一致）→ 库内批次仍是 plan-only 且前驱签名未变 → 批次改 `live/ready` → `execute_management_batch`（`strategy_management_executor.py:1393`，带 `@serialized_position_authority_mutation`） |
| 幂等 | `_project_canonical_remediation_candidate`（:1097）按 `recognition_generation = "remediation:<指纹前 32 位>"` 复用投影候选，重复 apply 同一动作复用同一候选 |
| CLI | `cli.py:5207 repair-position-management`：**独立进程**。`position_authority_lock` 是进程内 `RLock`（`position_authority_lock.py:9`），CLI 与 worker 之间**没有共享锁**——今天手工 apply 只靠库内状态机兜底。本阶段改为**在 worker 进程内**执行，正好补上这个缺口 |

**`live_management_execution_enabled`**（`trading_settings.py:194`）= `management_execution_mode == 'live' and auto_trade_enabled`，
来自 `config/groups.yaml`。本阶段**只读**这个开关，从不写它。

**结论：apply 会把主链路的全部检查重跑一遍。** 当初被 `management_stop_action_conflict`、`protection_price_or_size_mismatch` 这类
**规则性**原因拦下的消息，重推时会被同一条规则再拦一次——补救只对"原因已经消失"的情形有效：
前一个批次当时没收口（`prior_partial_batch_unresolved`）、系统停摆、可见性重试超时但仓位其实在、瞬时的交易所 / 快照问题。
这是设计要的性质（Codex 与本闸门都推不倒主链路的拒绝），也意味着 7.1 那 15 条 / 30 天里**只有一部分**是本阶段能救的，验收时要按原因分开统计。
（另：7.1 里的 `stale_pending_voided_*` 只由一次性脚本 `one_off/stale_pending_instruction_void.py` 在 2026-09-07 写过，不是持续产生的原因码。）

### 2.2 worker 这边已经有的零件

| 零件 | 事实 |
|---|---|
| worker HTTP | 与 web 同一个 FastAPI 应用，`--runtime-role worker --host 127.0.0.1 --port 8002`（`deploy/systemd/telegram-kol-worker.service`） |
| 内部令牌先例 | `require_monitor_capture_auth`（`web_app.py:7616`）：必须回环直连、无 `x-forwarded-for`，且请求头令牌 `hmac.compare_digest` 相等，否则一律 **404**；令牌格式校验在 `config.py:747` |
| 登录豁免 | `web_request_is_direct_loopback`（`web_app.py:635`）让回环直连绕过登录中间件——**所以新端点不能只靠"回环"，必须自己验令牌** |
| 系统 bot 命令循环 | `run_system_operator_bot_command_loop`（`telegram_bot_commands.py:327`），**跑在 worker 进程里**；`getUpdates` 已订阅 `callback_query`（:1232），已有 `answerCallbackQuery`（:1270）与内联按钮构造先例；offset 只存在内存、启动时取最新（:1207） |
| 发送者校验 | 现有回调只校验 **chat**（`_message_is_from_alert_chat`，:1257），**不校验 `from.id`**——本阶段的批准必须新增用户 id 校验 |
| 值守 | 只 `sendMessage`、从不 `getUpdates`（`oncall_alerts.py:739`），不会与 worker 抢更新；值守单元没有 `IPAddressDeny`，已能访问 `127.0.0.1:8002`；边界测试禁止值守 import `worker_command_jobs`、`position_mutation_gateway`、`sqlalchemy` 等（`tests/test_oncall_architecture_boundary.py:36`） |
| `worker_command_jobs` | 现成的持钥进程命令队列，但 `command_type` 有 CHECK 约束（`models.py:143`），且值守对生产库只读——**不选它**（见 3.1） |

### 2.3 要避开的一颗雷：计划器今天是全表扫描

`build_position_management_remediation_plan` 对 `signal_candidates` 做 `.all()`（无 `raw_message_id` / lifecycle 限定），再逐行点查，
并对**所有**出现过的 symbol 拉交易所快照。CLI 偶尔跑一次无所谓；**放进 worker 事件循环里就是 2026-09-15 冻结事故的原样重演**。
本阶段必须先做"按 lifecycle 限定范围"的计划（4.2），并把计划 / apply 全部放进 `asyncio.to_thread`。

## 3. 架构

```
telegram-kol-oncall（无特权；生产库只读；state.db 可写）
  ① 建案（D1a/D1b/D1d/D2-blocked，见 5.2）→ 建案告警（不变）→ Codex 诊断（不变，与本流程互不等待）
  ② POST 127.0.0.1:8002/internal/oncall/remediation/proposals
       头：x-oncall-remediation-token；体：只有标识符 {case_key, case_no, raw_message_id}
       ─────────────────────────────────────────────────────────────▶
telegram-kol-worker（唯一持交易所密钥者）
  ③ 闸门 G-A（提案闸门）→ 限定范围的补救计划（to_thread）→ 取该消息的链头动作
  ④ 写 oncall_remediation_proposals 一行（state=proposed）→ 经系统 bot 发提案消息 + 两个按钮
  ⑤ 用户点「✅ 执行补救」→ 系统 bot 循环收到 callback_query（本来就在 worker 里）
       → 闸门 G-B（批准闸门：人、会话、时效、状态）→ 改消息为二次确认
  ⑥ 用户点「确认执行」→ 闸门 G-B + G-C（执行闸门，全部重查）→ apply（to_thread）→ 回读 → 结果消息
```

### 3.1 为什么这样分工

- **批准权从不经过值守或 Codex。** 按钮回调由 Telegram 服务器投递给 worker 自己的 `getUpdates`；值守手里就算有 bot token，也伪造不出
  一个"用户点了按钮"的回调。值守（及其背后的 Codex）最多能做的是**请求 worker 算一份提案**——它是零权限的请求，最坏结果是多发几条提案消息（有上限）。
- **执行参数只来自 worker 自己从生产库和交易所算出的计划。** 请求体里没有价格 / 比例 / 目标字段，有就整条拒绝（`additionalProperties:false`）。
- **不用 `worker_command_jobs`**：值守对生产库是只读的，改成可写会破坏阶段 1 的核心纪律；加命令类型还要改 CHECK 约束。
  回环端点只多一个 HTTP 入口，而这个入口本身没有执行权。
- **提案消息由 worker 发、不由值守发**：谁校验批准，谁就签发提案与按钮令牌；值守不需要知道令牌，也就不存在"值守转述了一个被篡改的令牌"这一面。
  值守的建案告警与 Codex 诊断照旧由值守发，提案消息在正文里引用值守案件号，用户在同一个会话里按顺序看到三条。

### 3.2 Codex 在本阶段的位置

**不在权限路径上。** 状态文档 8.3 已钉住：注入成功的裁决在契约层面与诚实裁决不可区分。所以本阶段：
提案是否生成、按钮是否出现、执行是否放行，**都不读 `diagnoses` 表、不读 `verdict.json`**。Codex 的诊断照旧作为一条独立文字发给用户，供其判断要不要按按钮。
**用户裁定（第 11 节第 1 条）：Codex 判"不该执行"时不压按钮**，只作参考文字：提案消息里加一行"Codex 意见见值守 #<案件号> 的诊断消息"（worker 不读 Codex 输出）；值守在 `should_have_executed = no` 的诊断消息末尾加一行"（仅供参考，不影响补救按钮）"。

## 4. worker 侧实现

### 4.1 新增 / 修改的文件

| 文件 | 职责 |
|---|---|
| `src/telegram_kol_research/oncall_remediation.py`（worker 侧新模块） | 提案闸门 / 批准闸门 / 执行闸门、提案状态机、文案、限额与熔断；调用 4.2 的限定范围计划与现有 apply |
| `position_management_remediation.py` | 新增 `scope: RemediationScope \| None` 参数贯穿 `build_…_plan` 与 `apply_…_action`（4.2）；`scope=None` 时行为**逐字节不变**（CLI 不受影响） |
| `models.py` | 新表 `oncall_remediation_proposals`、`oncall_remediation_events`、`oncall_remediation_control`（4.5）；如 4.2 需要，新增 `signal_candidates.target_lifecycle_id` 索引 |
| `web_app.py` | 新路由 `POST /internal/oncall/remediation/proposals`（4.3），仅 `worker` 角色注册；令牌校验仿 `require_monitor_capture_auth` |
| `telegram_bot_commands.py` | 系统 bot 循环里接 `orm:` 前缀的回调与 `/fix P<提案号>`、`/oncall_off` 文本命令（第 6 节） |
| `config.py` | 新键（4.6） |
| `oncall_service.py` / `oncall_alerts.py` / `oncall_state.py`（值守侧） | 第 5 节 |
| 测试 | 第 9 节 |

`oncall_remediation.py` 是 **worker 模块**，不属于值守边界测试里的 `oncall_*` 集合——边界测试要**显式**把它排除，同时新增一条断言：
值守的七个模块仍然不 import 它（值守对它的唯一接触是 HTTP）。

### 4.2 限定范围的计划（`RemediationScope`）

- 范围 = **该 `raw_message_id` 的候选所指向的全部 lifecycle**；计划只考虑"目标落在这些 lifecycle 上的候选"（链的前驱关系按 lifecycle 成立，
  只扫这一条消息会把同 lifecycle 上更早的未解决消息漏掉，链头判断就错了）。
- 交易所快照只拉这些 lifecycle 的 symbol。apply 重建计划时**必须用同一个 scope**，否则快照指纹必然不同。
- 所有查询走索引：按 `raw_message_id`（已有索引）、按 `target_lifecycle_id`（**实现者先 `EXPLAIN QUERY PLAN` 核实**；若是 SCAN 就加索引，
  这属于本阶段的 schema 变更，进 L3 演练）。测试用 `EXPLAIN QUERY PLAN` 钉住"没有 SCAN"（仿 D6 的做法）。
- **等价性测试**：同一份库与同一份交易所桩，对范围内的动作，限定范围计划给出的 `action_id`、`action_kind`、`pos_ids`、`expected_effect`
  与全量计划**完全相同**；`fingerprint` 若因快照范围不同而不同，要在汇报里说明哪一项进了指纹。
- 候选里 target 未定（`target_lifecycle_id` 为空，例如确认超时）→ 范围为空 → 不产出提案，理由 `target_not_resolved`（这类归 G2 / B 线）。

### 4.3 回环端点

`POST /internal/oncall/remediation/proposals`

- 只在 `runtime_role == 'worker'` 时注册；web / ingest 上该路径 404。
- 必须回环直连、无 `x-forwarded-for`、`x-oncall-remediation-token` 与 `TELEGRAM_KOL_ONCALL_REMEDIATION_TOKEN` `hmac.compare_digest` 相等；
  任一不满足 → **404**（与 monitor capture 一致，不暴露端点存在）。worker 未配置该令牌 → 路由不注册。
- 请求体严格 JSON：`{"case_key": str≤64, "case_no": int, "raw_message_id": int}`，`additionalProperties:false`，≤ 2 KB。
- **同步只做登记，不做计算**：写一行 `proposals(state='requested')` 后立刻返回 `202 {"proposal_id": n, "state": "requested"}`；
  同一 `raw_message_id` 已有非终态提案 → 返回那一行（幂等，`200`）。计划计算由 worker 内一个后台单飞任务消费 `requested` 行完成（to_thread），
  **绝不在请求处理里拉交易所快照**。
- 返回体里**没有**价格、目标、指纹、按钮令牌——值守拿到的只有提案号与状态。

### 4.4 确定性闸门（全部在 worker 内，零 AI）

每条校验失败都写一条 `events`（`gate`、`check`、`reason_code`、实际值），提案进 `refused`，用户收到一行中文理由；**没有任何一条校验在失败时"放行并告警"**。

**G-A 提案闸门**（后台任务计算提案时）

| # | 校验 | 数据来源 |
|---|---|---|
| A1 | 补救模式 ≥ `shadow`（4.6），且运行时总闸未关（`control.enabled = 1`） | worker env + 生产库 |
| A2 | `live_management_execution_enabled` 为真（只读，不改） | `load_trading_settings` |
| A3 | 源消息存在、未被删除，所在群 / KOL 此刻仍开着自动交易 | `raw_messages`、删除记录、交易设置 |
| A4 | 限定范围计划的交易所快照完整（否则计划本身就是零动作） | 计划 |
| A5 | 该消息在范围内**恰好有一个** `ready_for_approval` 的链头动作，且它的 `raw_message_id` 就是本消息；零个 → `no_ready_action`（附计划给出的 step 状态与原因），多个 → `ambiguous_action` | 计划 |
| A6 | 意图白名单（第 7 节表中"收"的四项）；`partial_then_break_even` 不收；`cancel_entry` 转来的 `full_exit`（晚成交转平仓）不收 | 动作 `action_kind` + `evidence.original_action_kind` |
| A6b | 该消息的指令项结果是 `shadow_planned`（当时系统有意只计划不执行）→ 拒绝 `shadow_planned_not_remediated`（用户裁定第 8 条） | 指令项 `result_json` |
| A7 | **不可推翻的拒绝**：该消息的指令项 `error_json` / 结果、同消息的管理批次 `reason_code`、`runtime_incidents` 中任一命中下列原因 → 拒绝：`*_ownership_not_verified`、`exact_position_write_gate` 系拒绝、`protection_authority_frozen:*`、`protection_order_unattributable`、`explicit_stop_adjustment_not_risk_tightening`、`management_price_implausible`、`management_stop_direction_invalid`、`operator_dismissed`、`kol_or_group_auto_trade_disabled` | 生产库主键 / 索引点查 |
| A8 | 时效：距**消息发布时间**，`full_exit` ≤ 60 分钟，`partial_take_profit` ≤ **20 分钟**（用户 2026-09-26 裁定），止损类（`adjust_stop_loss`、`move_stop_to_break_even`）≤ 120 分钟（2026-09-19 决定）；批准与执行时都必须仍在窗口内 | `raw_messages.posted_at` |
| A9 | 目标仓位**此刻仍在仓**：动作的 `pos_ids` 全部在这次快照的 live 仓位里（计划已保证，这里对同一份快照再断言一次，防计划实现回归） | 计划快照 |
| A10 | 幂等：`(raw_message_id, action_kind, lifecycle_id)` 从未有过 `succeeded / executing / uncertain` 的提案（**每条消息每个意图一生只补救一次**） | `proposals` 唯一约束 + 查询 |
| A11 | 限额：同一 lifecycle 10 分钟冷却；北京日内执行 ≤ 10 次、提案 ≤ 30 条 | `proposals` |
| A12 | 熔断未触发（4.7） | `control` |

通过 → 提案 `proposed`，把动作快照（`action_id`、`fingerprint`、`action_kind`、`pos_ids`、`expected_effect`、`evidence`、scope、快照指纹）存进提案行，
生成两个一次性令牌，发提案消息（6.1）。

**G-B 批准闸门**（每次按钮回调 / `/fix` 命令）

| # | 校验 |
|---|---|
| B1 | `message.chat.id == TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID` **且** `from.id ∈ TELEGRAM_KOL_ONCALL_REMEDIATION_APPROVER_IDS`；后者未配置 → 一律拒绝（模式也不能高于 `shadow`，见 4.6） |
| B2 | 回调数据格式 `orm:<提案号>:<步骤>:<令牌>`（≤ 64 字节）；令牌与该步骤存的令牌 `compare_digest` 相等；令牌一次性，用后作废 |
| B3 | 提案状态与步骤匹配（第一步要求 `proposed`，第二步要求 `confirming`），用 `UPDATE … WHERE id=? AND state=?` 做**比较交换**；0 行 → 回 "这条提案已处理 / 已过期"，不做任何事 |
| B4 | 未过期：提案 30 分钟、二次确认 2 分钟（均从各自发出时算）；过期 → `expired` |
| B5 | 模式为 `approve`；`shadow` 下提案消息根本不带按钮，`/fix` 回 "当前为只提示模式" |

**G-C 执行闸门**（第二次确认通过后，apply 之前；**G-A 全部重跑**，再加）

| # | 校验 |
|---|---|
| C1 | 单飞：全局同时至多一个提案处于 `executing`（进程内 `asyncio.Lock` + 库内 `state='executing'` 计数双重判定） |
| C2 | 用**同一 scope** 重建计划，按提案存的 `action_id` 取链头，`fingerprint` 必须与提案存的**逐字相等**——不等 → `plan_changed`，不执行、不自动重新提案 |
| C3 | A8 时效以**执行时刻**再判一次：批准在窗口内、执行时已超窗 → 拒绝 |
| C4 | 然后才调 `apply_position_management_remediation_action(…, action_id, expected_fingerprint, scope)`——它内部的全部检查（2.1）原样生效，本闸门**不替它做、不跳过它** |

### 4.5 新表（生产库，schema 变更）

- `oncall_remediation_proposals`：`id`、`case_key`、`case_no`、`raw_message_id`（索引）、`lifecycle_id`、`action_kind`、`action_id`、`action_fingerprint`、
  `action_snapshot_json`（≤ 8 KB）、`scope_json`、`state`（CHECK：`requested / proposed / confirming / executing / succeeded / failed / uncertain / refused / expired / dismissed / cancelled`）、
  `refusal_reason`、`step1_token_hash`、`step2_token_hash`（只存 sha256，不存令牌本身）、`telegram_message_id`、`approver_user_id`、
  `requested_at / proposed_at / approved_at / confirmed_at / executing_at / finished_at / expires_at`、`management_batch_id`、`result_json`（≤ 4 KB）；
  部分唯一索引：`(raw_message_id, action_kind, lifecycle_id) WHERE state IN ('executing','succeeded','uncertain')`。
- `oncall_remediation_events`：追加写审计流水——`proposal_id`、`at`、`actor`（`oncall_request / worker / telegram_user:<id>`）、`event`、`gate`、`check`、`outcome`、`detail_json`（≤ 2 KB）。
  **只 INSERT**，代码里没有 UPDATE / DELETE 它的路径（测试断言）。
- `oncall_remediation_control`：单行——`enabled`、`changed_at`、`changed_by`、`reason`、`consecutive_failures`、`breaker_tripped_at`。
- 三张表都是新增表，不改任何既有表的列；启动时由现有 bootstrap 建表。**不存任何令牌、密钥、账户标识以外的 id**（`pos_id` 在 `action_snapshot_json` 里，与现有批次表同一保密级别）。

### 4.6 配置键（worker）

| 键 | 默认 | 含义 |
|---|---|---|
| `TELEGRAM_KOL_ONCALL_REMEDIATION_MODE` | `off` | `off`：路由不注册、后台任务不起、回调前缀 `orm:` 一律回"补救未启用"；`shadow`：算提案、发"本来会执行：…"，**不带按钮**；`approve`：带按钮，两次确认后执行 |
| `TELEGRAM_KOL_ONCALL_REMEDIATION_TOKEN` | 空 | 回环端点令牌，格式同 monitor capture（32–128 位 `[A-Za-z0-9_-]`）；空 → 路由不注册 |
| `TELEGRAM_KOL_ONCALL_REMEDIATION_APPROVER_IDS` | 空 | Telegram 用户 id；空 → `approve` 自动降为 `shadow` 并在启动日志说明。**用户裁定只允许用户本人**，生产值 = 系统 bot 会话 `TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID` 的值：2026-09-26 在服务器上只读核实该值为正数、10 位，即**私聊**，私聊的 chat id 就是对方用户 id（部署时从该键复制，本文不抄具体数字） |
| `TELEGRAM_KOL_ONCALL_REMEDIATION_DAILY_CAP` / `_COOLDOWN_MINUTES` / `_EXIT_WINDOW_MINUTES` / `_PARTIAL_TP_WINDOW_MINUTES` / `_STOP_WINDOW_MINUTES` | 10 / 10 / 60 / 20 / 120 | 4.4 的数值（用户裁定照用） |

模式与令牌都在 `/etc/telegram-kol-worker.env`（worker 专属）。**本阶段不向 `groups.yaml` 或交易设置表写任何东西。**

### 4.7 熔断

连续 2 次执行结果为 `failed` 或 `uncertain`，或回读与预期不符 → `control.enabled = 0`、记 `breaker_tripped_at`，发一条"补救已自动关闭：<原因>"，
所有 `proposed / confirming` 提案作废为 `cancelled`。熔断后只能人工恢复：批准人在系统 bot 会话发 `/oncall_on`（用户裁定第 3 条；同样过 B1 校验，并写 `events`）。成功一次清零计数。

## 5. 值守侧改动

### 5.1 请求提案

- 新配置 `TELEGRAM_KOL_ONCALL_REMEDIATION_REQUESTS=off|on`（默认 `off`）与 `TELEGRAM_KOL_ONCALL_REMEDIATION_URL`（默认 `http://127.0.0.1:8002/internal/oncall/remediation/proposals`）。
- 令牌放在**新的** `/etc/telegram-kol-oncall-remediation.env`（root 0600），**只由值守单元**用 `EnvironmentFile=-` 加载。
  **不得**写进 `/etc/telegram-kol-oncall.env`——那个文件被 Codex runner 单元共用（状态文档 8.6 第 3 点），runner 以 root 跑在 OpenAI 可间接触达的一侧。
  runner 单元的 `TemporaryFileSystem=/etc` 本就遮住新文件；探针新增一项"runner 读不到 `/etc/telegram-kol-oncall-remediation.env`"。
- 只对 5.2 的案件、只在案件**首次建案**时请求一次；`urllib`，3 s 超时；失败（网络、非 2xx、404）→ 在状态库记 `remediation_request_failed:<类别>`，
  **不重试到死**：同一案件最多 3 次、间隔 1 分钟；最终失败在下一条值守消息里带一行"补救提案请求失败，需人工"。任何失败都不影响建案告警与 Codex 诊断。
- 值守对 worker 的返回零信任：只取 `proposal_id`（整数）与 `state`（枚举），其余字段丢弃。
- 值守 `MODE != notify` 时不请求。

### 5.2 哪些案件会请求提案

| 规则 | 请求？ | 理由 |
|---|---|---|
| D1a（指令项 `failed/unknown`）、D1b（`succeeded` 但结果 `skipped`，已排除用户配置类原因） | 是 | `_item_requires_remediation` 的集合，去掉 `shadow_planned`（用户裁定第 8 条；worker 侧 A6b 再挡一次） |
| D1d（停在 `pending/executing/submitted` > 5 分钟） | 是 | 计划自己会把在途批次判为 `waiting_for_reconciliation` → 不出动作；让 worker 判，值守不猜 |
| D2 且 `status='blocked'` | 是 | `partial_failed / submit_unknown / recovery_required` **不请求**：计划对它们必然 `existing_management_batch_unresolved`（G3） |
| D1c（等待用户确认）、D3、D6a/b/c、D4、D5 | 否 | 目标未定 / 识别失败 / 健康类，本阶段没有安全的动作 |

### 5.3 状态库

`cases` 加 `remediation_proposal_id`、`remediation_request_state`；不新增表。案件何时 `resolved` 仍按阶段 1 规则（批次 `succeeded` 等），
不读提案表——**提案结果的权威记录在生产库的两张新表，不在值守**。若要在每日报平安里带出"今日补救 N 次"，只允许对 `oncall_remediation_proposals`
按 `id >` 水位线读（新查询形状进 `ALLOWED_QUERY_SHAPES` 并有 `EXPLAIN QUERY PLAN` 断言）。

## 6. /fix 批准流程

### 6.1 提案消息（worker 经系统 bot 发，纯文本，无 `parse_mode`）

```
🛠 补救提案 P<提案号>（对应值守 #<案件号>）
消息：<群名> #<raw_message_id>（<北京时间>）
将执行：<币种> <多/空> <动作中文>：<例：止损 2500 → 2484 / 平掉 50% / 全部平仓>
依据：这些数字由系统从生产数据重算，不来自 AI
参考：Codex 意见见值守 #<案件号> 的诊断消息（仅供参考，不影响按钮）
时效：本提案 <HH:MM> 前有效；消息的补救窗口到 <HH:MM>
[✅ 执行补救]  [❌ 忽略]
```

- `shadow` 模式：首行改为 `🛠 补救提案（只提示）`，末行改为"本来会执行上述操作；当前为只提示模式"，不带按钮。
- 提案被闸门拒绝时也发一条一行文字：`ℹ️ 值守 #<案件号> 没有可执行的补救：<理由中文>`（`no_ready_action` 附计划给的 step 原因中文）。
  这条消息计入值守的每日上限语义之外的 worker 侧上限（每日 ≤ 30 条提案类消息，超出只记库）。

### 6.2 两步确认

1. 点「✅ 执行补救」→ G-B → 提案 `proposed → confirming`，`editMessageText` 改为
   `确认执行：<同一行动作>？2 分钟内有效` + `[确认执行] [取消]`（新令牌）。
2. 点「确认执行」→ G-B → `confirming → executing`（比较交换）→ G-C → apply → 结果（6.3）。
3. 「❌ 忽略」/「取消」→ `dismissed / cancelled`，按钮移除。**忽略不写回主链路的任何状态**（不等同 `operator_dismissed`）。
- 所有回调都先 `answerCallbackQuery`（Telegram 要求 15 s 内应答），重活放后台。
- 文本兜底：`/fix P<提案号>` 等价于第 1 步（仍需第 2 步按钮）；`/fix` 必须来自 B1 的会话与用户。命令里只接受提案号，不接受任何参数。

### 6.3 重复与并发

| 情形 | 处理 |
|---|---|
| 同一按钮被点两次 / 两台设备同时点 | 比较交换只让一次成功；另一次回"这条提案已在处理 / 已处理" |
| 同一消息被再次请求提案 | 端点返回已有的非终态提案（幂等）；已有 `succeeded` → 新请求直接 `refused: already_remediated` |
| 同一 lifecycle 上两个提案都在 `proposed` | 允许同时展示，但执行受 C1 单飞 + A11 冷却；第二个在执行闸门重建计划时通常因链头 / 指纹变化被拒 |
| worker 在 `executing` 中途重启 | 启动时把 `executing` 行改为 `uncertain`（**绝不重跑**），发一条"补救执行中断，结果未知，需人工核对"；计入熔断 |
| worker 重启期间用户点了按钮 | offset 启动时取最新，那次点击被丢弃；提案仍在 `proposed`，用户可再点（或过期）——宁丢不重放 |

### 6.4 审计记录写在哪

- **权威**：生产库 `oncall_remediation_events`（每个请求、每道闸门每条校验、每次点击、每次状态变化、apply 结果）+ `oncall_remediation_proposals`。
- **执行本身**：沿用现有的 `strategy_management_batches / legs / components`、`execution_events`、`position_mutation_intents`——提案行存 `management_batch_id` 串起来。
- 值守 `state.db` 只存提案号与请求状态；journal 里只记提案号与状态，不记令牌、不记回调原始数据。

### 6.5 结果消息

- `✅ 已补救 P<n>：<动作>。交易所回读：<例：新止损 2484 已挂，旧止损已撤净 / 仓位已减至 0.5>`（回读取自 apply 返回与批次终态，不另发交易所请求）。
- `❌ 补救失败 P<n>：<理由中文>。<若熔断：补救已自动关闭>`
- `⚠️ 补救结果未知 P<n>：<理由>。请人工核对交易所。`（`uncertain`）

## 7. 补救动作白名单（第一版）

"收"的前提：现有执行路径已在生产上跑通过该意图（设计 7.1：全平 12、保本 3、部分止盈 2、保护替换 2 次成功）。

| 意图（`action_kind`） | 收？ | 执行路径（`strategy_management_executor.py`） | 备注 |
|---|---|---|---|
| `full_exit` | 收 | `execute_management_batch`（:1393）→ 全平分支（:1495 / :4237）→ `position_mutation_gateway.close_exact_position`（:986） | 窗口 60 分钟 |
| `partial_take_profit` | 收 | → `partial_close` 分支（:2595 起）→ `close_exact_position`（按比例） | 窗口 **20 分钟**；比例来自 `resolve_management_directive` |
| `move_stop_to_break_even` | 收 | 计划器落成 `effective_action='break_even_by_market'`（`strategy_management_planner.py:155`）→ 分派于 :1436 → `_execute_break_even_by_market_batch`（:744）：按仓位由 `reserve_break_even_market_actions` 决定"挂保本止损"或"价格已越过保本 → 市价平"，经 `position_mutation_gateway` 的 `submit_exact_position_sltp`（:806）/ `close_exact_position`（:986）落地 | 窗口 120 分钟；保本价取策略入场价（计划器既有口径）；**注意它可能变成市价平仓**，提案文案必须写出这种可能 |
| `adjust_stop_loss` | 收（**仅收紧**） | `_PROTECTION_ACTIONS`（:145）→ 保护单分支（:1454 起）→ 先挂后撤 | "仅收紧"由计划器 `_plan_strategy_management_batch_locked` 内联判定（`strategy_management_planner.py:1191–1215`），不收紧 → `explicit_stop_adjustment_not_risk_tightening`，属 A7 不可推翻 |
| `partial_then_break_even` | **不收**（用户裁定第 5 条） | 分批保护 saga（:2595–2604） | 生产上自 8 月中旬 **0 次成功**（状态文档 8.9）；收进来大概率只是再失败一次并触发熔断 |
| `full_exit`（由 `cancel_entry` 晚成交转来） | 不收 | 同全平 | 入场侧语义，另议 |
| `adjust_take_profit`（**G1**） | **排除**，前置 = 阶段 5 | 无：`resolve_management_directive` 不产出、`SUPPORTED_INTENTS`（`strategy_management_planner.py:134`）不含；交易所层 `deepcoin_execution_actions.py:373/2722` 有能力但**无生产者** | 这类消息今天必然不执行，补救也够不着 |
| `/choose` 之后的指令项（**G2**） | **排除**，前置 = 阶段 5 | 无：`choose_management_target`（`management_target_confirmation.py:154`）把项改回 `pending` 后无人认领；`claim_next_visibility_retry_instruction_item`（`message_instruction_items.py:387`）只认 `visibility_first_failed_at IS NOT NULL` | 目标未定的案件（D1c）本阶段不请求提案 |
| `recovery_required / partial_failed / submit_unknown` 批次（**G3**） | 排除 | 计划器判 `existing_management_batch_unresolved` | 阶段 5 |
| 开仓、加仓、放宽止损、撤止损、改杠杆、重启服务 | **永不** | — | — |

## 8. 失败即关闭、一键关闭与回滚

### 8.1 fail-closed 规则

- 任何异常、超时、快照不完整、读库失败、指纹不符、状态不符 → 该提案 `refused / failed / uncertain` 之一，**绝不**降级成"跳过这条检查继续执行"。
- apply 抛错时按批次终态分：批次未进入提交（仍 plan-only）→ `failed`；进入过提交或状态不明 → `uncertain`（D2 会接住该批次，计入熔断）。
- 补救**从不自动重试**；一次 `failed` 后要执行只能等新的案件 / 新的提案（且受 A10 约束）。
- 值守→worker 请求失败、worker 发 Telegram 失败，都不影响值守告警与 Codex 诊断；worker 发消息失败 → 提案留在 `proposed` 直至过期，不会没人看见就执行（没有按钮就不可能被批准）。

### 8.2 关补救的三个层级（都不碰任何自动交易开关）

| 层级 | 操作 | 生效 | 影响 |
|---|---|---|---|
| 运行时总闸 | 系统 bot 发 `/oncall_off`（仅 B1 用户）；重新打开发 `/oncall_on`（同样仅 B1 用户，也用于熔断后恢复） | 立即；关闭时所有 `proposed / confirming` 作废为 `cancelled` | 只停补救；诊断、告警、主链路不受影响 |
| 值守不再请求 | `/etc/telegram-kol-oncall.env` 设 `…_REMEDIATION_REQUESTS=off`，`systemctl restart telegram-kol-oncall` | 下一轮 | 同上 |
| worker 关闭功能 | `/etc/telegram-kol-worker.env` 设 `…_REMEDIATION_MODE=off`，重启 worker（允许，见用户规则"重启可以"） | 重启后；路由消失 | 同上 |

### 8.3 代码回滚

- 记录部署前生产 HEAD 为回滚 sha；回滚 = `tg-deploy <回滚 sha>` + `systemctl restart telegram-kol-oncall`。
- 三张新表是**纯新增**：回滚后旧代码不读它们，保留原地即可（不 DROP；数据是审计记录）。若加了 `signal_candidates` 索引，同样保留（只加速，不改语义）。
- 若 `executing` 中回滚：同 6.3 的重启规则，旧代码不认识该表，提案行停在 `executing`——**回滚前先执行 `/oncall_off` 并确认 `executing` 为 0**，写进部署清单。

## 9. 测试清单

用项目夹具建临时生产库；交易所用现有 fake Deepcoin 客户端；Telegram 用桩；**测试从不连真实交易所 / Telegram**。

1. **限定范围计划**：scope 只含目标 lifecycle；与全量计划的动作等价（4.2）；同 lifecycle 更早未解决消息仍是链头；`scope=None` 与改动前逐字节一致（快照测试）；每条新 SQL `EXPLAIN QUERY PLAN` 无 SCAN；计划与 apply 在 `to_thread` 里运行（断言事件循环未被阻塞 > 阈值）。
2. **端点**：非回环 / 带 XFF / 无令牌 / 错令牌 → 404；未配令牌 → 路由不存在；web/ingest 角色 → 404；多余字段、价格字段、超长 → 拒绝；幂等返回同一提案；请求处理中**不调用**交易所客户端（桩计数为 0）。
3. **G-A**：A1–A12 每条一正一反；A7 九类原因各一条；A8 三类窗口的边界（19/21、59/61、119/121 分钟）；A6b `shadow_planned` 被拒；A5 零个 / 多个链头；A6 `cancel_entry` 转来的 `full_exit` 被拒。
4. **G-B**：错会话、错用户、未配置批准人；令牌错 / 复用 / 跨步骤；过期（30 分钟 / 2 分钟）；`shadow` 下无按钮、`/fix` 被拒；`/fix` 带额外参数被拒。
5. **G-C**：并发两个确认只有一个进入 `executing`；计划指纹变化 → `plan_changed` 且未调用 apply；执行时超窗；apply 内部检查失败原样上报。
6. **状态机**：全部合法迁移与非法迁移（比较交换 0 行）；`events` 只追加（静态断言无 UPDATE/DELETE）；重启时 `executing → uncertain` 且不重跑。
7. **熔断**：连续 2 次 `failed/uncertain` → 关闭 + 作废在途提案 + 告警；成功清零；熔断后提案请求一律 `refused`。
8. **总闸**：`/oncall_off` 立即生效（下一次回调即拒绝）；`/oncall_on` 只接受 B1 用户、能解除熔断并写 `events`；它**不修改** `groups.yaml`、交易设置、任何自动交易开关（断言这些对象在测试前后相等）。
9. **白名单端到端**（fake 交易所）：`full_exit`、`partial_take_profit`、`move_stop_to_break_even`、`adjust_stop_loss`（收紧成功 / 放宽被拒）各一条从请求到结果消息；`adjust_take_profit` 无提案；D1c / D3 / D6 / 健康案件值守不请求。
10. **回放**：用状态文档与设计 7.1 列出的历史形状做夹具——`prior_partial_batch_unresolved`（前驱已收口 → 出提案）、raw 17813 形状（`management_stop_action_conflict` → apply 内被同一规则再拒，提案 `failed` 而非执行）、raw 18371/18375 形状（仓位已平 → A9 拒绝）、`partial_failed` 批次 → 值守不请求。
11. **值守侧**：只对 5.2 的规则请求；3 次重试上限；失败不影响告警 / 诊断的发送（顺序与独立性断言）；只取 `proposal_id / state`；令牌不在 `state.db`、日志、案件包、Codex spool 里（全文搜索断言）；值守七个模块仍不 import `oncall_remediation` 与写交易所模块。
12. **runner 隔离**：探针新增"读不到 `/etc/telegram-kol-oncall-remediation.env`"。
13. **文案**：中文、纯文本、无 `parse_mode`、长度；消息原文不进提案消息（提案只描述动作）。

聚焦测试边开发边跑；最终候选跑**一次**全量 `uv run python -m pytest -q`（`uv run pytest` 收集期失败是既有问题）。

## 10. 本阶段明确不做

- **阶段 4 的自动执行**：没有任何"无人点击即执行"的路径，也不预留开关值；`approve` 是本阶段最高模式。
- Codex 的裁决不参与提案、按钮、执行的任何判断；不给 Codex 暴露提案号、指纹、令牌、端点。
- B 线（Codex 给结构化指令、替用户选仓位）；G1 / G2 / G3；D1c、D3、D6、健康类案件的补救。
- 任何增加风险的动作；`cancel_entry` 晚成交转平仓；撤止损；改杠杆。
- 改动主链路的任何判据或拒绝原因（包括 `management_stop_price_gate` 对 raw 17813 形状的误拒——那是独立缺陷，另行立项）。
- 写 `groups.yaml` / 交易设置 / 任何自动交易开关；重启服务作为补救手段。
- 修 `worker_command_jobs`、CLI `repair-position-management` 的跨进程锁缺口（本阶段只保证新路径在 worker 内；CLI 维持原样，另议）。

## 11. 用户裁定（2026-09-26，经调度会话转达；原为待拍板问题）

| # | 问题 | 裁定 | 落到正文 |
|---|---|---|---|
| 1 | Codex 判"不该执行"时压不压按钮 | **不压**；它的意见只作参考文字，Codex 仍不在权限路径上 | 3.2 |
| 2 | 谁可以批准 | **只有用户本人**；id 取系统 bot 私聊的 chat id（2026-09-26 服务器只读核实为私聊） | 4.6 `APPROVER_IDS`、G-B B1 |
| 3 | 总闸 / 熔断后怎么重开 | 批准人在系统 bot 发 **`/oncall_on`**，不必登服务器 | 4.7、8.2 |
| 4 | 时效从哪算 | **按消息发布时间**，批准与执行时都要在窗口内 | A8、C3 |
| 5 | `partial_then_break_even` 收不收 | **本版不收** | A6、第 7 节 |
| 6 | 部分止盈窗口 | 60 → **20 分钟** | A8、4.6、第 7 节 |
| 7 | 限额数值 | **照用**：每日执行 ≤ 10、同仓位冷却 10 分钟、连续 2 次失败熔断、提案 30 分钟 / 二次确认 2 分钟 | 4.4、4.6、4.7、6.2 |
| 8 | 当初"只计划不执行"（`shadow_planned`）的项 | **本版不补救** | 5.2、A6b |

## 12. 部署与 L3 验证计划（指挥会话执行）

### 12.1 部署前

- 候选全量测试一次通过；候选是当前生产 HEAD 的后代（AGENTS.md 的双向检查）。
- **schema 演练**：服务器上 `VACUUM INTO` 生产库快照 → 在快照上用候选代码跑 bootstrap 建新表（与索引，如有）→ `PRAGMA quick_check` = ok →
  对快照做一次 `off` 模式的启动冒烟；记录耗时（若加索引，记录建索引耗时——它会在 worker 启动时对生产库执行）。演练完删快照，留尺寸与 sha256。
- **备份**：部署前 `VACUUM INTO` 一份生产库备份，记录路径、大小、sha256；before 计数：`strategy_management_batches`、`message_instruction_items`、
  `signal_candidates`、`execution_bindings`、`position_mutation_intents`、`worker_command_jobs`。
- 零在途：管理批次 / mutation intent / claimed job / worker command 均为 0，且没有正在进行的时效性策略操作（AGENTS.md）。
- 服务器上生成令牌写入 `/etc/telegram-kol-worker.env` 与 `/etc/telegram-kol-oncall-remediation.env`（两处 0600）；**任何会话都不打印其值**。
- 值守单元文件新增 `EnvironmentFile=-/etc/telegram-kol-oncall-remediation.env`：tg-deploy **不同步 systemd 单元**，需手工 `cp` + `daemon-reload`。

### 12.2 部署（休眠上线）

- `tg-deploy <sha>`，worker `MODE=off`、值守 `REQUESTS=off`。验证：三张新表存在、after 计数与 before 一致、worker 路由不存在（404）、错误行 0；
  `systemctl restart telegram-kol-oncall`，心跳正常；探针（含新增项）全 PASS。
- 部署后把同一 sha 推到共享分支，并跑 AGENTS.md 的 `OFFENDERS` 检查。

### 12.3 逐级打开

1. worker `MODE=shadow` + 值守 `REQUESTS=on`：真实案件只出"只提示"提案；人工逐条核对提案内容与闸门拒绝理由，**至少 3 个真实案件或 3 天**。
2. `MODE=approve`：等第一个真实案件由你亲手批准。**首笔样本逐项核对**：批准前后 `trigger-orders-pending` 全集（按 `TU`/`ordId`，不读仓位行 `slTriggerPx`，见 ARCHITECTURE 4.8）、
   仓位数量、批次 / 组件终态、`position_mutation_intents`、`oncall_remediation_events` 全链路、结果消息与交易所实况一致。
3. 首笔核对通过前不算阶段完成；不通过 → `/oncall_off`，记录，阶段保持 `in_progress`。

证据（原始 JSON、订单行）留在服务器证据文件；状态文档只记 sha、窗口、模式、指标、异常与证据路径。

## 13. 提交与汇报（给实现子代理）

- 基线：`origin/main` 当前 tip；在 worktree 分支上提交。**禁止 `git add -A`**；只 add 明确路径并 `git diff --cached --name-only` 核对；不 push 共享分支、不部署、不连服务器。
- 更新 `docs/codex-oncall-status.md`（阶段 3 小节：交付物、偏离、待指挥会话验收的清单）。
- 汇报：提交 SHA 与文件清单；4.2 等价性测试的结论与指纹差异说明；`signal_candidates` 的索引核实结果（是否新增）；每条新 SQL 的查询计划；
  全量测试结果；偏离本规格之处及理由；你认为规格里有问题、有风险或遗漏的地方。
