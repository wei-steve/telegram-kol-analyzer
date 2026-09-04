# 两起真实交易失败只读诊断

日期：2026-09-03（UTC）

## 结论

本轮没有发现当前仍处于无保护状态的实盘持仓，因此没有触发“立即停止后续调查”的条件。worker `127.0.0.1:8002` 在 `2026-09-03 06:53:28 UTC` 返回 `complete=true`、`position_count=2`、`open_order_count=0`；同一 worker 的当前持仓页面显示两个持仓分别有 5 条和 3 条已验证止盈止损。事件二仍保留的多单 `posId=1001125104601308` 是其中之一，页面显示 3 档止盈和 2 条止损。

两起失败不是同一个原因：

| 事件 | 确切对象 | 确切断点 | 记录的原因 |
|---|---|---|---|
| 三姐多单止盈 50% 并移动止损 | raw `14382`；candidate `2147`；lifecycle `1050`；binding `324`；management batch `152` | 已完成 MiMo 识别并生成、接纳管理 candidate，进入管理规划后因没有可安全撤换的保护单 ID 而阻断，未生成 management leg，也未发出部分平仓或改止损写请求 | 即时 blocker `protection_missing_cancellable_order_id`；batch 最终 reason `protection_visibility_retry_expired` |
| 大镖客只保留多单 | raw `14547`；多单 lifecycle/binding `1063/330`；空单 lifecycle/binding `1065/331`；该消息无 candidate | 权威识别给出 `lifecycle_event.event_type=none`；上下文二次判断因回复目标为空单但正文说持有多单而判 `hold / target_ambiguous`，候选层无可执行动作，执行链从未开始 | `context_resolution_attempt.decision=hold`、`conflict_types=[target_ambiguous]`；automation `skipped / mimo_no_action` |

事件二**没有**命中此前怀疑的 `mimo_authoritative_not_safely_applied`。事件一虽然具有“`recognition_result=非策略` + 非 `none` lifecycle event”的允许组合，但它成功生成并接纳 candidate，同样没有命中该 fail-closed 分支。

## 调查边界与证据口径

- 生产目录：`/opt/telegram-kol-analyzer`；数据库：`data/research.db`。
- 每次 SQLite 查询均以 `file:...?...mode=ro` 打开，并执行、核验 `PRAGMA query_only=ON`（返回 `1`）。没有写库、建表、事务修复或导出副本。
- 远端 Python 均使用 `python3 -B`，且只使用标准库，没有在 immutable release 中产生 `.pyc` / `__pycache__`。
- 交易所证据仅来自 worker `127.0.0.1:8002` 的 GET：`/api/runtime-agent/read-only-exchange-snapshot`、`/positions-panel?initial=positions`、`/positions-panel/tabs/order-history`、`/positions-panel/tabs/position-history`。
- 数据库时间按 UTC 记录；下文同时给出北京时间（UTC+8）。worker 历史委托页面的展示时区也是 `Asia/Shanghai`。
- `sources` 的实际标签是 `🏧三姐精准策略群🏧11分组`（chat `-1003000736304`），与所有者口述的“三姐精准策略群 11分组”唯一对应；`大镖客 11分组` 是 chat `-1003048800035`。定位以实际 chat ID、Telegram message ID、正文和订单血缘共同完成，不以名称近似单独推断。

## 事件一：三姐多单止盈 50% 并移动止损

### 1. 确切对象与时间

- 开仓消息：raw `14367`，Telegram message `1173`，`2026-09-02 02:32:47 UTC`（北京时间 `10:32:47`）。entry candidate `2142`。
- 管理消息：raw `14382`，Telegram message `1174`，`2026-09-02 03:32:22 UTC`（北京时间 `11:32:22`）。相关原文（已去除联系人信息）：

  > 比特币多单止盈50%，止损位移动至开仓价！

- 目标 lifecycle：`1050`，BTC long，strategy instance `deepcoin:-1003000736304:1173:BTC:long`。
- execution binding：`324`；entry leg `559`；父 trigger order `1001125090052318`；唯一 child regular order / `posId` 为 `1001125090080799`。
- 管理 candidate：`2147`；instruction item `927`；management batch `152`。

### 2. 摄入与识别

1. `raw_messages` 存在 raw `14382`：`created_at=2026-09-02 03:32:23.696167 UTC`，`source_status=active`。
2. `message_processing_jobs` 存在 job `2621`：`status=succeeded`、`last_reason=worker_completed`、`attempt_count=0`，于 `03:32:23.980161 UTC` 完成。不是 stale expiry。
3. `mimo_recognition_runs` 存在 run `4774`：`status=completed`、`became_authoritative=1`、`input_kind=text+image`，运行时间 `03:32:24.007194` 至 `03:34:51.897014 UTC`。
4. `media_assets` 存在 asset `177698`；run 与 decision 的 `input_kind` 都是 `text+image`，图片确实送入模型。

### 3. 权威识别结论

`recognition_decisions.id=14381`：

- `authoritative_status=非策略`
- payload `recognition_result=非策略`
- payload `lifecycle_event.event_type=position_update`
- `target_lifecycle_id=1050`
- `management_action="partial_take_profit, move_stop_to_protect"`
- `stop_loss=77250`，即识别为移到实际开仓价
- `agreement_status=review_disabled`
- `comparison_status=completed`
- `automation_status=partial_failed`
- `automation_reason=NULL`

该组合符合“新开仓识别”和“生命周期事件识别”相互独立的现有契约；本事件没有因 `recognition_result=非策略` 而被候选层拒绝。

### 4. Candidate 与系统接纳

生成 candidate `2147`：

- `event_type=position_update`
- `target_lifecycle_id=1050`
- `management_action=partial_then_break_even`
- `management_fraction=0.5`
- `parse_source=mimo_authoritative`
- `confidence=0.85`
- management contract 要求 `consume_take_profit_stage`、`converge_partial_close`、`replace_remaining_protection`，并指定 `stop_mode=actual_entry_price`。

生产 Web 只读投影对 raw `14382` 的确切结果是：`system_acceptance.status=accepted`、`accepted_candidate_count=1`、`reason_code=NULL`。这里的“已接纳”只表示候选落地成功，不表示交易执行成功。

### 5. Instruction item 与 operation contract

- `message_instruction_items.id=927` 存在：`instruction_kind=management`、目标 strategy instance 正确，但 `status=failed`。
- `error_json`：`batch_id=152`、`execution_mode=live`、`status=blocked`、`reason=protection_missing_cancellable_order_id`。
- raw `14382` 没有 `message_operation_contracts` 记录（count `0`）。

### 6. 执行链对账

- 复用了既有 binding `324`，没有为管理消息创建新 binding。
- entry leg `559` 已通过 `direct_pos_id` 证据绑定 `posId=1001125090080799`，`attribution_status=verified`。
- management batch `152` 在 `2026-09-02 03:34:51.980840 UTC` 建立并立即 blocked：
  - `intent=partial_then_break_even`
  - `effective_action=partial_close`
  - `requested_fraction=effective_fraction=0.5`
  - `execution_mode=live`
  - `target_snapshot_json.blocked_reason=protection_missing_cancellable_order_id`
  - 最终 `status=blocked`、`reason_code=protection_visibility_retry_expired`，`updated_at=03:41:07.996322 UTC`
- batch `152` 的 `strategy_management_legs` 为 0，`strategy_management_components` 为 0；说明尚未形成任何可提交的部分平仓或保护替换 leg。
- binding `324` 的三条 `position_protection_legs`（IDs `761/762/763`）分别是 primary stop、backup stop、take profit，但全都停在 `protection_recovery_pending`，且 `exchange_order_id=NULL`。
- binding `324` 的 `position_protection_ledger` 为 0，`position_take_profit_orders` 为 0，`position_mutation_intents` 为 0。
- trigger protection intent `163` 后续累计 5 次恢复尝试，最终 `recovery_state=failed`、`recovery_disposition=manual_review`、`adopted_order_id=NULL`、`last_reason_code=trigger_protection_candidate_predates_fill`（最终更新时间 `03:52:10.496475 UTC`）。这条终态晚于管理消息，不倒推为消息到达瞬间的状态；它证明后续也没有获得可采用的保护订单 ID。
- `execution_events` 对 binding `324` 只有原开仓的 `create_trigger_entry` event `3892`；没有 raw `14382` 对应的部分平仓、撤保护、改止损或新保护提交事件。

### 7. 确切断点

断点位于 **candidate 已接纳之后、strategy management leg 生成之前的保护可见性/可撤换性预检**。

直接原因不是“没识别到止盈和止损”，而是系统不能证明当前保护单具有唯一、可安全撤销/替换的交易所 order ID：

1. instruction item 的即时原因码：`protection_missing_cancellable_order_id`；
2. batch target snapshot 的 blocker：`protection_missing_cancellable_order_id`；
3. 可见性等待到期后的 batch 最终原因码：`protection_visibility_retry_expired`。

因此两项动作作为一个 `partial_then_break_even` 管理合同整体 fail-closed：既没有执行 50% 止盈，也没有把剩余仓位止损移到开仓价。

### 8. 当时 lifecycle 与交易所持仓

- 消息到达时 lifecycle `1050` 已是 `entered`：`entered_at=2026-09-02 02:33:00 UTC`，直到 `10:51:24.734448 UTC` 才被标为 `exited/manual`。raw `14382` 在这一区间内。
- worker GET `/positions-panel/tabs/order-history` 返回 child order `1001125090080799`：BTC 开多、`filled`、数量 `21`，北京时间 `2026-09-02 10:36:28` 创建、`10:36:36` 更新；早于管理消息北京时间 `11:32:22`。
- worker GET `/positions-panel/tabs/position-history` 在 `2026-09-03 06:55:49.934764 UTC` 完整返回该 `posId` 的 Deepcoin 历史仓位：BTC long，最大/已平 `0.021 BTC`，开仓均价 `77250`、平仓均价 `76295.7`。
- 结合 worker 的已成交 child order、Deepcoin position history 与 lifecycle 的 entered→exited 时间区间，可以确认管理消息到达时交易所确有对应 long 持仓，不是“目标仓位不存在”。
- worker 当前快照已不含该仓位。历史 GET 不提供当时 TPSL 的完整历史行，因此“原止损在交易所当时是否存在但无法归因”与“交易所当时根本没有止损”之间仍需进一步确认；本轮能够确定的是系统在本地没有得到可安全撤换的 order ID。

## 事件二：大镖客只保留多单

### 1. 确切对象与时间

- 多单开仓消息：raw `14536` / Telegram message `4474`，`2026-09-03 02:40:04 UTC`（北京时间 `10:40:04`）；entry candidate `2161`；lifecycle `1063`；binding `330`。
- 空单开仓消息：raw `14546` / Telegram message `4476`，`2026-09-03 03:46:00 UTC`（北京时间 `11:46:00`）；entry candidate `2163`；lifecycle `1065`；binding `331`。
- 管理消息：raw `14547` / Telegram message `4477`，`2026-09-03 03:56:56 UTC`（北京时间 `11:56:56`），明确 `reply_to_message_id=4476`，即回复空单消息。相关原文（已去除联系人和 KOL 个人信息）：

  > 算了，还是持有多，不搞多空

- 空单 market entry leg `569`：order / `posId=1001125105260562`。第二条未触发 trigger entry leg `570`：order `1001125105260730`。
- 多单当前 entry leg `567`：order / `posId=1001125104601308`；第二条 trigger leg `568` 尚 pending。

### 2. 摄入与识别

1. `raw_messages` 存在 raw `14547`：`created_at=2026-09-03 03:56:56.277319 UTC`，`source_status=active`。
2. `message_processing_jobs` 存在 job `2786`：`status=succeeded`、`last_reason=worker_completed`、`attempt_count=0`，于 `03:56:59.502300 UTC` 完成。不是 stale expiry。
3. `mimo_recognition_runs` 存在 run `4962`：`status=completed`、`became_authoritative=1`、`input_kind=text`，运行时间 `03:56:59.631031` 至 `03:57:38.303325 UTC`。
4. raw `14547` 没有 media asset，因此不存在“有图但只送 text”的情况。

### 3. 权威识别与上下文二次判断

`recognition_decisions.id=14545`：

- `authoritative_status=非策略`
- payload `recognition_result=非策略`
- payload `lifecycle_event.event_type=none`
- `agreement_status=review_disabled`
- `comparison_status=completed`
- `automation_status=skipped`
- `automation_reason=mimo_no_action`

context resolution attempt `4497` 随后在 `2026-09-03 03:58:33.187816 UTC` 完成：

- `status=completed`
- `decision=hold`
- `conflict_types=[target_ambiguous]`
- `management_action=NULL`
- `supporting_message_ids=[4477]`
- `opposing_message_ids=[4476]`
- `target_thread_ids=[]`
- `risk_reducing_fanout_allowed=false`
- 无 `reanalysis_triggers`
- `error_class=NULL`、`last_error=NULL`

模型给出的 unresolved reason 是：正文表达持有多单，但没有抽取出明确管理动作；同时消息回复的是空单策略 `4476`，正文方向与回复目标方向冲突，因而无法唯一确定目标线程并选择 `hold`。

### 4. Candidate 与系统接纳

- raw `14547` 的 `signal_candidates` 数量是 0。
- 生产 Web 只读投影：`system_acceptance.status=not_accepted`、`accepted_candidate_count=0`、`reason_code=mimo_no_action`。
- 多、空两张开仓消息本身分别已有 entry candidate `2161`、`2163`；缺失的是 raw `14547` 的退出/取消管理 candidate。

### 5. Instruction item 与 operation contract

- raw `14547` 的 `message_instruction_items` 为 0。
- raw `14547` 的 `message_operation_contracts` 为 0。

### 6. 执行链对账

- raw `14547` 没有 `strategy_management_batches`、management legs/components，也没有 execution event。
- 不存在由 raw `14547` 触发的 close reservation、position mutation intent、止盈撤单或止损替换记录。
- 空单 binding `331` 在消息到达时已经有真实、已验证的 market leg `569`；其 2 条 stop ledger 和 3 条 take-profit ledger 最晚在 `03:47:02.637281 UTC` 已 verified，早于 raw `14547`。
- 后续 `2026-09-03 04:50:49.818956 UTC`，系统在发现交易所空单已经不存在后，把 lifecycle `1065` 标为 `exited/manual`、binding `331` 标为 closed，并提交 event `3923` 去取消**仍未触发的第二条空单入场单** `1001125105260730`；event `3924` 是 `terminal_entry_cleanup_outcome/resolved`，reason `exchange_position_missing`。
- event `3923/3924` 都发生在所有者手动平仓之后，且来源仍是 entry message `4476`，不是 raw `14547`。它们证明系统做了终态清理，不能当作系统响应“只保留多单”而平掉实际空单的证据。
- 空单的 3 条 `position_take_profit_orders` 后续在 `04:50:58.833296 UTC` 标为 expired；这同样是仓位消失后的收敛，不是管理消息执行。

### 7. 确切断点

断点位于 **上下文二次判断 / candidate 投影之前**：

1. 权威 payload 没有生成 lifecycle event（`event_type=none`）；
2. context resolution 虽然读取了 direct reply `4476`，但以 `target_ambiguous` 判 `hold`；
3. 因 `target_thread_ids=[]` 且 `management_action=NULL`，未生成 signal candidate；
4. automation 以 `mimo_no_action` 跳过；后续所有指令、合同、batch 和执行表均没有 raw `14547` 的记录。

因此本事件不是“进入执行后平仓失败”，而是可执行的“关闭空单、保留多单”意图从未形成。

### 8. 当时 lifecycle 与交易所持仓

- raw `14547` 到达时，多单 lifecycle `1063` 已 `entered`（`entered_at=02:40:24.786738 UTC`），空单 lifecycle `1065` 也已 `entered`（`entered_at=03:46:51.708136 UTC`）。空单直到 `04:50:49.818956 UTC` 才被标为 `exited/manual`。
- worker GET `/positions-panel/tabs/order-history` 返回空单 market order `1001125105260562`：BTC 开空、`filled`、数量 `8`，北京时间 `2026-09-03 11:46:52` 创建并更新；管理消息是北京时间 `11:56:56`。
- worker GET `/positions-panel/tabs/position-history` 完整返回 `posId=1001125105260562` 的 Deepcoin 历史仓位：BTC short，最大/已平 `0.008 BTC`，开仓均价 `77617.3`、平仓均价 `77613.5`。
- 这确认消息到达时对应空单已经真实成交并形成仓位，而不是只有未触发挂单。当前 worker 持仓中不再有该 short `posId`。
- 多单 `posId=1001125104601308` 仍在当前 worker 快照中，数量 `10 contracts / 0.01 BTC`，有 3 档止盈 `78300/79000/79700` 和两条止损 `76347/76500`。

## 已知失败模式逐条判定

| 模式 | 事件一 raw `14382` | 事件二 raw `14547` | 字段证据 |
|---|---|---|---|
| `expired_stale_instruction` | 未命中 | 未命中 | jobs `2621/2786` 均为 `succeeded / worker_completed`，不是 `expired`；两条消息也都有 completed authoritative MiMo run |
| `execution_running` 卡死 | 未命中 | 未命中 | decisions `14381/14545` 的 `comparison_status` 均为 `completed`，automation 分别已落为 `partial_failed`、`skipped`，不是 automation NULL |
| `mimo_authoritative_not_safely_applied` | 未命中 | 未命中 | 事件一虽为 `非策略 + position_update`，但 candidate `2147` 已生成且 system acceptance 为 `accepted`；事件二是 `非策略 + none`，automation reason 为 `mimo_no_action`，不是该拒绝码 |
| 上下文二次判断未解决 | 未命中 | **命中** | 事件一无 context attempt；事件二 attempt `4497` 为 `decision=hold`、`conflict_types=[target_ambiguous]`、`target_thread_ids=[]`，Web `unresolved_reason` 为该 hold 的自然语言 reason |
| 图片未送入模型 | 未命中 | 未命中 | 事件一有 media asset `177698`，run `4774` 与 decision 都是 `input_kind=text+image`；事件二没有 media asset，`input_kind=text` 与实际输入一致 |

## 最终根因归类

- **事件一：执行安全证据断裂。** 识别、目标定位、管理 candidate 和系统接纳均正确；binding/posId 也存在。失败发生在管理 planner 需要撤换现有保护时，本地只有 `protection_recovery_pending` 投影，没有 ledger 或可采用的 exchange order ID，遂以明确原因码 fail-closed。
- **事件二：语义到可执行动作的投影断裂。** 系统读到了消息与 direct reply，但将“回复空单 + 正文持有多单”视为目标冲突，选择 hold；没有形成退出空单 candidate，因而执行层没有失败记录，因为执行层从未被调用。

本报告只记录诊断，不包含修复方案，也没有对历史消息进行重放。
