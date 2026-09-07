# 2026-09-07 两条管理指令未执行 — 只读诊断报告

- 会话性质：**只读排查**。全程未改代码、未部署、未重启、未撤单、未下单、未修改任何数据库行、未修改生产设置。
- 证据来源：`sqlite3 "file:/opt/telegram-kol-analyzer/data/research.db?mode=ro"`、`journalctl`、以及一次只读的交易所 GET 查询
  （`list_positions` / `list_open_orders` / `list_trigger_orders_pending` / `list_position_history` / `get_ticker_price`，
  脚本 `/root/ro-diag/*.py`，用 `python -B` + `PYTHONDONTWRITEBYTECODE=1` 运行）。
- 交易所快照时间：2026-09-07 ≈07:30 UTC（温哥华 2026-09-07 00:30，UTC-7）。
- 时间标注：数据库与日志内部时间为 UTC；journald 显示为 `+08:00`（Asia/Shanghai）；用户口述时间为温哥华 UTC-7。

---

## 0. 首要安全结论（先看这一段）

**大镖客当前那个 BTC 仓位有止损，止损价 78343，但止损在成本价下方，不是保本位。**

| 项 | 值 |
|---|---|
| 交易所 posId | `1001125163581280`（BTC-USDT-SWAP，long，8 张） |
| 归属 | `execution_bindings.id=341` / `strategy_lifecycles.id=1097` / `strategy_threads.id=466`（大镖客 msg 4509） |
| 成本价 avgPx | 80118.3 |
| 当前止损 slTriggerPx | **78343**（对应条件单 ordId `1001125163582992`，`closeSLTriggerPrice=78343`） |
| 止损 vs 成本 | 低于成本 **1775.3 点**（约 −2.2%） |
| 快照时最新价 | 79502.3（已低于成本） |
| 未实现盈亏 | −4.928 USDT |

另有一件需要人工判断的事：**同一策略还有一张未成交的加仓条件单挂着**——
`ordId=1001125163581473`，Conditional buy 10 张 @ 79190（附带 SL 78500），
对应 `execution_order_legs.id=587`，`status=pending`。快照时最新价 79502.3，离 79190 只有约 312 点。
也就是说：「保本离场」没有执行，而**如果价格继续下探到 79190，系统还会自动加仓**。

本会话未对上述任何挂单/仓位做任何操作。

---

## 1. 时间线

温哥华时间 = UTC − 7。

| UTC | 温哥华 | 事件 | 证据 |
|---|---|---|---|
| 09-04 12:48:55 | 09-04 05:48 | 峰哥发 ETH 做多（2440/止损2370/止盈2540） | `raw_messages.id=14843`（chat `-1002409877375`, message_id 9181） |
| 09-04 12:49:16 | | 权威执行租约转 `uncertain`（`ExecutionBoundaryOutcomeUnknown / in_progress`） | `authoritative_execution_attempts.id=25` |
| 09-04 12:50:00 | | 生命周期 1081 被标记 `entered`，`entry_price_actual=2443.12`，但 `execution_binding_id` 为空 | `strategy_lifecycles.id=1081` |
| 09-04 12:52:47 | | 该条消息的 job 5 次重试后 `failed`（`processing_error:RuntimeError`） | `message_processing_jobs.id=3082` |
| | | → 指令项失败原因 `target_strategy_binding_visibility_retry_expired` | `message_instruction_items.id=969` |
| 09-04 09:07:11 | 09-04 02:07 | 大镖客发「到第一止盈…」 | `raw_messages.id=14797`（message_id 4497） |
| 09-04 09:21:02 | | worker 重启 | journal `17:21:02+08` |
| 09-04 09:22:59 | 09-04 02:22 | 为 lifecycle 1074 建管理批次 158（`partial_take_profit`），当场落到 `recovery_required / close_final_preflight_failed`，唯一 leg 139 停在 `planned`，从未提交 | `strategy_management_batches.id=158`、`strategy_management_legs.id=139` |
| 09-04 09:22:59 | | 产生 critical 运行时事件 2058，`notification_status=pending`（至今未送达） | `runtime_incidents.id=2058` |
| 09-04 13:07:38 | 09-04 06:07 | **交易所侧**：binding 337 的两条腿仓位全部平掉（`1001125126414222` @79200 停损、`1001125123045253` @80149.8）。数据库侧无人改状态 | 交易所 `list_position_history`（uTime `1788527258000`） |
| 09-06 15:21:29 | 09-06 08:21 | 峰哥发 ETH 短多 #1（2475/2430/2535） | `raw_messages.id=15144`（msg 9201） |
| 09-06 15:23:12 | | 租约转 `uncertain`；job 3383 最终 `failed` | `authoritative_execution_attempts.id=319`、`message_processing_jobs.id=3383` |
| 09-06 15:28:18 | 09-06 08:28 | **仓位其实开成功了**：市价单 `1001125157891231`，SL 2430 | `message_instruction_items.id=987`、`execution_bindings.id=340` |
| 09-06 23:10:28 | 09-06 16:10 | **峰哥发「现价2510获利出局」** | `raw_messages.id=15155`（msg 9203） |
| 09-06 23:11:13 | | 上下文解析结论 `unresolved` / `target_ambiguous`（置信 0.5） | `context_resolution_attempts.id=4833` |
| 09-06 23:11:14 | | 识别结论「非策略」，`automation_status=skipped`，`automation_reason=mimo_no_action`；无候选、无批次、无交易所动作 | `recognition_decisions.id=15152` |
| 09-06 23:11:14 | | 提醒记为 `ignored_not_strategy`，`forwarded_at` 为空（**没有任何提醒发出**） | `strategy_alerts.id=9621` |
| 09-07 00:59:19 | 09-06 17:59 | 大镖客发 BTC 多（80000-79100 / 78500 / 80700-81400-82100） | `raw_messages.id=15171`（msg 4509） |
| 09-07 01:00:03 | | 开仓成功：市价 8 张 `1001125163581280` + 条件单 10 张 @79190 | `execution_bindings.id=341` |
| 09-07 02:26:46 / 02:28:13 | | worker 两次重启 | journal `10:26:46+08` / `10:28:13+08` |
| 09-07 02:27:37 | 09-06 19:27 | 峰哥发 ETH 短多 #2（2505/2435/2595）——**落在重启窗口内** | `raw_messages.id=15186`；job 3425 `failed` |
| 09-07 02:38:34 | | 该单仍开成功：`1001125164628529` | `execution_bindings.id=342` |
| **09-07 03:01:12** | **09-06 20:01** | **大镖客发「求稳可以保本离场观望一下」** | `raw_messages.id=15201`（msg 4512） |
| 09-07 03:04:44 | | 上下文解析：`exit_thread` / `exit_full` / 目标 thread 443（lifecycle 1074），置信 0.85 | `context_resolution_attempts.id=4864`、`recognition_decisions.id=15194` |
| 09-07 03:04:45 | | 管理指令项**被拒**：`prior_partial_batch_unresolved` | `message_instruction_items.id=994` |
| 09-07 03:04:49 | | 租约转 `uncertain`（`partial_failed`），抛 `authoritative_execution_outcome_unknown` | `authoritative_execution_attempts.id=372` |
| 09-07 03:08:20 | | job 3440 5 次重试后 `failed` | `message_processing_jobs.id=3440` |
| 09-07 05:49:01 | 09-06 22:49 | 用户手动平掉峰哥 ETH #1，成交均价 2509.75（+10.01 USDT） | 交易所 `list_position_history`；`strategy_lifecycles.id=1095` `exit_reason=manual` |

**重启窗口对照**：2026-09-07 02:00Z–08:00Z 之间三个 unit 的启停（journald +08 → UTC）：
02:26:45/46/48/50/51（worker→web→ingest 全组）、02:28:13（worker 单独）、
04:43:32~37（全组）、04:58:53~57（全组）、05:12:32~37（全组）、05:15:29（worker）、
05:30:02~06（全组）、05:43:27~32（全组）、06:03:10~15（全组）、06:28:49~54（全组）。

- **大镖客保本（03:01–03:08Z）不在任何重启窗口内**——排除「重启吃掉指令」这一解释。
- 峰哥第二单（02:27:37Z）**正好夹在 02:26:46 与 02:28:13 两次 worker 重启之间**，这解释了它 job 耗时 10 分钟才终态失败，但不影响本次两条管理指令。
- 峰哥止盈（09-06 23:10Z = journald 09-07 07:10+08）附近无重启（前一次 09-07 01:58+08 = 09-06 17:58Z）。

---

## 2. 指令一：峰哥「现价2510获利出局」的完整追踪链

| 环节 | 表 / 行 | 关键字段原文 |
|---|---|---|
| 原始消息 | `raw_messages.id=15155` | `chat_id=-1002409877375`, `message_id=9203`, `posted_at=2026-09-06 23:10:28.000000`, `text="现价2510获利出局\n@Tarderfengge QQ:158241758"` |
| 作业 | `message_processing_jobs.id=3394` | `status=succeeded`, `attempt_count=0`, `last_reason=worker_completed`, `enqueued_at=2026-09-06 23:10:28.733282`, `completed_at=2026-09-06 23:10:28.761404` |
| 上下文解析 | `context_resolution_attempts.id=4833` | `status=completed`, `decision=unresolved`, `confidence=0.5`, `conflict_types=["target_ambiguous"]`, `target_thread_ids=[]` |
| 识别结论 | `recognition_decisions.id=15152` | `authoritative_status=非策略`, `automation_status=skipped`, `automation_reason=mimo_no_action`, `comparison_status=completed` |
| 识别原文（模型理由） | 同上 payload | 「当前消息为平仓指令（'现价2510获利出局'），涉及ETH，但未指定目标策略。候选策略中有两个活跃ETH策略（thread_id 464和450），thread_id 464有验证仓位，thread_id 450状态为entered但无验证仓位。消息无回复上下文，无法唯一确定目标，需人工确认。」 |
| 权威执行租约 | `authoritative_execution_attempts.id=329` | `status=succeeded`, `exchange_effect=not_started`, `automation_status=skipped`, `automation_reason=mimo_no_action` |
| 候选 | `signal_candidates` | **该 raw_message_id 无任何行**（未生成候选） |
| 管理批次 | `strategy_management_batches` | **无行**（`raw_message_id=15155` 不存在批次；09-05 之后全表只有 batch 159） |
| 管理腿 | `strategy_management_legs` | 无行 |
| 交易所动作 | `execution_events` / `position_mutation_intents` | **无行**（09-06 22:00Z–09-07 01:00Z 之间 `execution_events` 为空；最近一条 mutation intent 是 09-06 15:28:30 的 backup stop） |
| 提醒 | `strategy_alerts.id=9621` | `status=ignored_not_strategy`, `is_strategy=0`, `forwarded_at=NULL` |

**为什么会 target_ambiguous：**

- 候选 A：`strategy_threads.id=464` → `strategy_lifecycles.id=1095`（msg 9201，`execution_binding_id=340`，真实仓位 `1001125157891231`）。
- 候选 B：`strategy_threads.id=450` → `strategy_lifecycles.id=1081`（msg 9181），`lifecycle_status=entered`、
  `entered_at=2026-09-04 12:50:00`、`entry_price_actual=2443.12`、但 **`execution_binding_id` 为 NULL**。
- 候选 B 是**幽灵仓位**：它的入场指令 `message_instruction_items.id=969` 状态是 `failed`，
  原因 `target_strategy_binding_visibility_retry_expired`，交易所上从来没有对应仓位或挂单
  （`execution_bindings` 中不存在 `message_id=9181` 的行）。
  但生命周期监视器按价格把它「模拟成交」标成了 `entered`，于是它一直以「活跃 ETH 策略」的身份留在候选集里。

**关于「附带止损的入场条件单」这一假设：已排除。**
`execution_order_legs` 中 ETH 的 `order_kind='trigger_limit'` 最新两行是
`id=573/574`（binding 333，2026-09-03 创建），状态均为 `cancelled`；
23:10Z 时 ETH 唯一在场的腿是 `id=585`（binding 340，`order_kind=market`）。
`trigger_protection_intents` 最近三行分别属于 binding 341/339/338，均非 ETH 的这条链路。

**关于 `convergence_*` / `break_even_*` 拒绝码：已排除。**
`journalctl -u telegram-kol-worker --since 2026-09-04` 中
`convergence_pending_alias_conflict` / `convergence_pending_alias_conflict_before_write` 命中数为 **0**，
03:00Z 前后也没有任何 `convergence_*` / `break_even_*` 拒绝码。

---

## 3. 指令二：大镖客「保本离场」的完整追踪链

| 环节 | 表 / 行 | 关键字段原文 |
|---|---|---|
| 原始消息 | `raw_messages.id=15201` | `chat_id=-1003048800035`, `message_id=4512`, `posted_at=2026-09-07 03:01:12.000000`, `text="大镖客·Andy\n上插针80500没过，再次回踩，风险加大，求稳可以保本离场观望一下，看看中午怎么走，新策略会在群里再通知\n@Tarderfengge QQ:158241758"` |
| 作业 | `message_processing_jobs.id=3440` | `status=failed`, `attempt_count=5`, `last_reason=processing_error:RuntimeError`, `enqueued_at=2026-09-07 03:01:12.244494`, `completed_at=2026-09-07 03:08:20.069747` |
| 上下文解析 | `context_resolution_attempts.id=4864` | `status=completed`, `decision=exit_thread`, `management_action=exit_full`, `confidence=0.85`, `target_thread_ids=[443]`（后续 4867/4870/4872 三次重放结论一致；4865 一次 `exhausted`，`last_error=RuntimeError`） |
| 识别结论 | `recognition_decisions.id=15194` | `authoritative_status=非策略`, `automation_status=uncertain`, `automation_reason=authoritative_execution_outcome_unknown`, `comparison_status=execution_uncertain`；`lifecycle_event.event_type=exit_position`, `management_action=exit_full`, `target_lifecycle_id=1074` |
| 候选 | `signal_candidates.id=2216` | `event_type=close_signal`, `symbol=BTC`, `side=long`, `management_action=full_exit`, `target_lifecycle_id=1074` |
| **指令项（关键）** | `message_instruction_items.id=994` | `instruction_kind=management`, `strategy_instance_id=deepcoin:-1003048800035:4495:BTC:long`, `status=failed`, `error_json={"batch_id":null,"execution_mode":"live","reason":"prior_partial_batch_unresolved","status":"blocked"}` |
| 权威执行租约 | `authoritative_execution_attempts.id=372` | `status=uncertain`, `exchange_effect=outcome_unknown`, `error_class=ExecutionBoundaryOutcomeUnknown`, `error_summary=partial_failed`, `uncertain_at=2026-09-07 03:04:49.152353` |
| 管理批次 | `strategy_management_batches` | **无新行**（planner 在建批次之前就返回 blocked） |
| 管理腿 | `strategy_management_legs` | 无新行 |
| 交易所动作 | `execution_events` / `position_mutation_intents` | **无行**（03:01Z–03:10Z `execution_events` 只有 4016/4017/4018/4019，均属其他群） |
| 生命周期副作用 | `strategy_lifecycles.id=1074` | `management_action=exit_requested`, `management_signal_message_id=4512`, `updated_at=2026-09-07 03:04:44.999021`，但 `lifecycle_status` 仍为 `entered`，`exited_at` 为空 |

**为什么被 `prior_partial_batch_unresolved` 挡住（已在代码里核实）：**

`strategy_management_planner.py:570-578`
```python
partial_policy_state = _load_partial_policy_state(session, target_lifecycle_id=lifecycle.id)
if partial_policy_state.frozen:
    return ManagementPlanningResult(status="blocked", reason_code="prior_partial_batch_unresolved", ...)
```
`_load_partial_policy_state`（同文件 1904-1958）对该 lifecycle 的所有 `PARTIAL_INTENTS` 批次逐个判定：
只要有一个批次既不是「完全确认」也不在 `{"blocked", "resolved"}` 里，就 `frozen = True`。

命中的就是 **batch 158**：
```
id=158  raw_message_id=14797  target_lifecycle_id=1074
strategy_instance_id=deepcoin:-1003048800035:4495:BTC:long  execution_binding_id=337
intent=partial_take_profit  effective_action=partial_close  execution_mode=live
status=recovery_required   reason_code=close_final_preflight_failed
planned_at=started_at=updated_at=2026-09-04 09:22:59.637054  reconciled_at=NULL  completed_at=NULL
```
它唯一的腿：
```
strategy_management_legs.id=139  management_batch_id=158  execution_order_leg_id=579
pos_id=1001125123045253  leg_index=0  status=planned
preflight_size=5  planned_close_size=2  avg_entry_price=80490
client_order_id=NULL  exchange_order_id=NULL  last_error=NULL
```
`status=planned` + 无 `client_order_id`/`exchange_order_id`，且当时的指令项
`message_instruction_items.id=967` 的 `error_json` 明写 `"submitted": false`
——**batch 158 从未向交易所发出任何请求**，但它把 lifecycle 1074 冻结至今。

**RuntimeError 的完整栈（journald 11:04:49+08 = 03:04:49 UTC）：**
```
telegram_kol_research.message_processing_worker message processing job failed raw_message_id=15201 status=pending
  message_processing_worker.py:606 _run_claim_body → :197 process_message_job
  web_app.py:5858 <lambda> → :4253 _run_authoritative_processor
  authoritative_recognition.py:1561 process_authoritative_message
  authoritative_recognition.py:1939 _run_leased_authoritative_execution
RuntimeError: authoritative_execution_outcome_unknown
```
后续 4 次重试则被幂等护栏挡回：
```
authoritative_recognition.py:1638 _load_completed_execution_for_automatic_retry
RuntimeError: automatic retry blocked by active or uncertain authoritative execution
recognition_decisions.py:205 save_pending_authoritative_decision
RuntimeError: authoritative execution is already in progress or outcome is uncertain
```
`authoritative_recognition.py:1929-1939` 表明：`auto_trade_executor` 返回的
`ExecutionBoundaryOutcome.exchange_effect == "outcome_unknown"` 时一定抛这个错。
而 `auto_trade_execution.py:520` 显示 `partial_failed` 来自
`_message_instruction_status()`：只要有指令项 `status == "failed"` 就是 `partial_failed`。
也就是说：**这里的「outcome_unknown」是被 planner 明确拒绝（blocked）后被上层归类成的「结果未知」，
不是真的向交易所发过请求而结果不明。** 交易所侧确认零动作。

**额外问题：指令被解析到了一个早已不存在的仓位。**
`target_lifecycle_id=1074` → `execution_bindings.id=337` → 腿 579/580 → posId `1001125123045253` / `1001125126414222`。
交易所 `list_position_history` 显示这两个仓位在 **2026-09-04 13:07:38 UTC** 就已全部平掉：
```
posId=1001125126414222 avgPx=79688.2 closeAvgPx=79200 pos=14 closePos=14 pnl=-6.835   uTime=1788527258000
posId=1001125123045253 avgPx=80490   closeAvgPx=80149.8 pos=10 closePos=10 pnl=-3.4015 uTime=1788527258000
```
而数据库里 `execution_bindings.id=337` 仍是 `status=active`
（`last_exchange_status=position_attribution_evidence_unavailable`，`recovered_at=2026-09-07 07:19:20`），
腿 579/580 仍是 `status=active`，lifecycle 1074 仍是 `entered`。
用户在 03:01 真正持有的大镖客仓位是 **lifecycle 1097 / thread 466 / binding 341**（当天 01:00 开的），
识别却指向了 443/1074。**即使没有 frozen 拦截，这条指令也会打在一个空仓位上。**

---

## 4. 当前交易所状态（只读列举，2026-09-07 ≈07:30 UTC）

### 4.1 持仓（共 2 个）

| # | instId | posId | 方向/张数 | avgPx | slTriggerPx | tpTriggerPx | lastPx | 归属 binding / lifecycle / thread | 有无止损 | 止损 vs 成本 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC-USDT-SWAP | `1001125163581280` | long / 8 | 80118.3 | **78343** | 空 | 79502.3 | 341 / 1097 / 466（大镖客 msg 4509） | **有** | 低于成本 1775.3（−2.2%），**不是保本** |
| 2 | ETH-USDT-SWAP | `1001125164628529` | long / 1.6 | 2526.08 | **2430.13** | 空 | 2494.32 | 342 / 1100 / 469（峰哥 msg 9210） | **有** | 低于成本 95.95（−3.8%） |

### 4.2 条件单（`trigger-orders-pending`）

| instId | ordId | 类型 | 内容 | 归属 |
|---|---|---|---|---|
| BTC | `1001125163581378` | TPSL | `closeSLTriggerPrice=78500`（开仓时那张，已被 78343 那张取代） | binding 341 / leg 586 |
| BTC | `1001125163582992` | TPSL | `closeSLTriggerPrice=78343`（当前生效止损） | binding 341 / leg 586（backup stop，`position_mutation_intents.id=640`） |
| BTC | `1001125163581473` | Conditional | **buy 10 张 @ 79190**，附带 `closeSLTriggerPrice=78500` — 未成交的第二条入场腿 | binding 341 / `execution_order_legs.id=587`（`status=pending`） |
| ETH | `1001125164628647` | TPSL | `closeSLTriggerPrice=2435`（开仓时那张） | binding 342 / leg 588 |
| ETH | `1001125164631326` | TPSL | `closeSLTriggerPrice=2430.13`（当前生效止损） | binding 342 / leg 588（`position_mutation_intents.id=642`） |

普通挂单：`list_open_orders` 对 BTC、ETH 均返回空。

### 4.3 数据库与交易所的状态漂移（只列，未修改）

| 数据库行 | 数据库状态 | 交易所真实状态 |
|---|---|---|
| `execution_bindings.id=337` | `status=active` | 两条腿仓位已于 2026-09-04 13:07:38 UTC 全平 |
| `execution_order_legs.id=579/580` | `status=active` | 对应 posId 均不在持仓列表中 |
| `strategy_lifecycles.id=1074` | `lifecycle_status=entered` | 已无仓位 |
| `strategy_lifecycles.id=1081` | `lifecycle_status=entered`、无 binding | 从未有过仓位（入场指令项 969 `failed`） |
| `execution_bindings.id=338` | `status=open`, `last_exchange_status=entry_order_pending` | BTC 无任何 open order / 空头持仓 |

---

## 5. 根因判断

### 5.1 已证实

1. **大镖客「保本离场」未执行的直接原因：`prior_partial_batch_unresolved`。**
   `message_instruction_items.id=994` 的 `error_json` 直接写明；
   `strategy_management_planner.py:570-578` + `_load_partial_policy_state`（1904-1958）给出判定逻辑；
   冻结源是 `strategy_management_batches.id=158`（`recovery_required` / `close_final_preflight_failed`，
   腿 139 停在 `planned`），自 2026-09-04 09:22:59 UTC 起一直非终态。
   **一个 2026-09-04 遗留的未收尾批次，把该策略的所有后续管理指令永久锁死了。**

2. **这条指令从未向交易所发过任何请求。**
   `execution_events`、`position_mutation_intents` 在对应时间窗内均无相关行；
   `strategy_management_batches` / `strategy_management_legs` 无新行。

3. **`RuntimeError: authoritative_execution_outcome_unknown` 是「被拒绝」被上层升级成的「结果未知」，不是真实的交易所不确定。**
   `auto_trade_execution.py:520`（`_message_instruction_status`：有 `failed` 项 → `partial_failed`）
   → `authoritative_recognition.py:1929-1939`（`exchange_effect=="outcome_unknown"` → 抛错）。
   一个确定性的业务拒绝被表达成了不确定性结果，导致 job 5 次重试全废、租约留在 `uncertain`。

4. **峰哥「获利出局」未执行的直接原因：上下文解析 `unresolved` / `target_ambiguous`，未生成候选，因此按 `mimo_no_action` 跳过。**
   `context_resolution_attempts.id=4833`、`recognition_decisions.id=15152`、`signal_candidates` 无行。

5. **造成歧义的第二个 ETH 候选是幽灵：thread 450 / lifecycle 1081。**
   它的入场指令项 `id=969` 是 `failed`（`target_strategy_binding_visibility_retry_expired`），
   `execution_binding_id` 为空，交易所无对应仓位；但 lifecycle 被标成 `entered` 且带模拟成交价 2443.12。

6. **「保本离场」被识别为 `full_exit`（全平），不是 `move_stop_to_break_even`。**
   `signal_candidates.id=2216` `management_action=full_exit`；`recognition_decisions.id=15194` 的
   `lifecycle_event.management_action=exit_full`。

7. **指令被解析到了一个已空 2.6 天的仓位。**
   目标 lifecycle 1074 / binding 337 的仓位在 2026-09-04 13:07:38 UTC 已全平（交易所 position history），
   而真实在场的大镖客仓位是 lifecycle 1097 / binding 341。

8. **两条指令都没有产生任何送达用户的提醒。**
   峰哥那条：`strategy_alerts.id=9621` `status=ignored_not_strategy`、`forwarded_at=NULL`。
   大镖客那条：`strategy_alerts` 无行。
   batch 158 的 critical 事件 `runtime_incidents.id=2058` 自 2026-09-04 起 `notification_status=pending`；
   全表 `pending` 共 1868 条，最后一次 `delivered` 是 2026-09-05 16:26:13。
   其中 `management_recovery_required` 类型 12 条**全部** `pending`。

9. **重启窗口不是这两条指令的原因。**
   大镖客保本处理窗口（03:01–03:08Z）内无任何 unit 启停。
   （峰哥第二单 02:27:37Z 确实夹在 02:26:46 与 02:28:13 两次 worker 重启之间，但那单最终仍成功开仓。）

10. **`convergence_*` / `break_even_*` 拒绝码与本次事故无关。**
    自 2026-09-04 起 worker 日志中 `convergence_pending_alias_conflict` 命中 0 次。

11. **Deepcoin `trigger-orders-pending` 端点存在间歇性 401。**
    2026-09-06 20:00Z 起 worker 日志命中 210 次；本次只读复测 8 次里出现 1 次
    （同一凭据、同一 instId 时好时坏，与币种无关）。持仓读取正常
    （`deepcoin_reconcile_round` 的 `rest_read_failures: []`）。
    **未证实它与本次两条指令有因果关系**，但它会污染任何依赖「当前条件单」的判据。

### 5.2 推测（未证实，需进一步证据）

1. **batch 158 的 `close_final_preflight_failed` 具体触发点未确定。**
   worker 日志中 `close_final_preflight_failed` 关键字命中 0 次（该 reason 只落库，未打日志）。
   已知它发生在 2026-09-04 09:22:59 UTC，而 worker 刚在 09:21:02 UTC 重启完；
   **推测**重启后的首轮 preflight 拿不到完整的仓位/保护快照导致失败，但没有直接证据。

2. **推测两起事故共享一个根因族：非终态残留污染后续决策。**
   一类是「批次不收尾 → 冻结整条策略」（大镖客），
   一类是「入场失败但生命周期被模拟成 entered → 候选集多出幽灵 → 目标歧义」（峰哥）。
   两者都源于「执行失败时生命周期/批次没有被推到终态」。这是模式归纳，不是单条证据。

3. **推测上下文解析选择 443 而非 466，是因为 443 在候选集中显示为「已 entered 且持有多个仓位」。**
   模型理由原文提到「该策略已entered并持有多个仓位」，与 lifecycle 1074 的陈旧状态一致；
   但候选集的实际构造输入未逐字取证。

4. **推测 `trigger-orders-pending` 的 401 是签名/时间戳或该端点限流所致。**
   同凭据同参数时好时坏，其他私有端点正常。未做进一步验证（会涉及对交易所的额外探测）。

### 5.3 证据不足之处

- 未取到 batch 158 失败当时的 preflight 输入快照（`target_snapshot_json` 未在本次读取中展开逐字比对）。
- 未确认 lifecycle 1074 的仓位在 2026-09-04 13:07:38 平掉后，为何 `deepcoin_reconcile` / `lifecycle_monitor`
  在两天多里都没有把 binding 337 推到终态。日志里只看到反复的
  `Skipping simulated lifecycle exit for live execution binding: lifecycle_id=1074 binding_id=337 to_status=exited reason=stop_loss`
  ——监视器**看到了**该退出但因为是 live binding 而跳过，交给谁收尾未追到底。
- `runtime_incidents` 的 `pending` 积压跨度从 2026-07-21 到今天，说明并非本周新坏；
  但「哪些 incident_type 本来就不发通知、哪些是漏发」未区分。
- 未验证 `message_instruction_items.id=969` 的 `target_strategy_binding_visibility_retry_expired`
  当时的重试次数与超时配置。

---

## 6. 建议的修复方向（本会话不实施）

按优先级排列，均为方向性建议，需另开实施会话并各自走验证等级。

1. **给「冻结」加时限与可观测出口（最高优先）。**
   `_load_partial_policy_state` 的 `frozen` 目前没有过期、没有告警、没有自动收尾路径。
   建议：`recovery_required` 批次超过阈值时间未收尾，必须（a）产生一条能真正送达的告警，
   （b）提供一条明确的运维终结命令，(c) 或允许**风险降低方向**的指令（`full_exit` / `move_stop_to_break_even`）绕过冻结
   ——冻结的本意是防重复减仓，不应该连「离场」和「移止损保本」都一起挡掉。

2. **区分「确定性拒绝」和「结果未知」。**
   planner 返回 `blocked` 是一个**确定**结论，不应经由 `_message_instruction_status` 变成 `partial_failed`
   再变成 `exchange_effect=outcome_unknown`。建议在 `auto_trade_execution` 里把
   「blocked / 未提交」与「已提交但结果不明」分成两个状态，前者走 `completed + not_started`，
   这样 job 不会 5 次重试全废，租约也不会留在 `uncertain`。

3. **入场失败的生命周期不得被标成 `entered`。**
   lifecycle 1081 是「指令项 failed + 无 binding」却 `entered`。
   建议：`lifecycle_monitor` 的模拟成交对「有执行意图但无 verified binding」的策略应拒绝推进，
   或至少标一个 `unbound` 子状态，并把这类策略排除出上下文解析的候选集。

4. **候选集必须以「交易所可验证的持仓」为准。**
   上下文解析把 443（空仓 2.6 天）和 450（从未有仓）当作活跃候选。
   建议候选构造时加一道「binding 有 verified 持仓」的过滤，或至少把无持仓的候选降权并在理由里显式标注。

5. **binding / leg 的终态收敛要有兜底。**
   binding 337 在交易所空仓 2.6 天后仍是 `active`，`last_exchange_status=position_attribution_evidence_unavailable`。
   建议：当 `position_attribution_evidence_unavailable` 连续 N 轮且 position history 能证明已平仓时，
   走一条明确的终结路径而不是无限期停留。

6. **告警通道要有积压监控。**
   1868 条 `pending`、最后一次 delivered 在 2026-09-05 16:26。
   建议：给 `runtime_incidents.notification_status='pending'` 且 `severity='critical'` 的数量加一个自监控阈值，
   并明确哪些 incident_type 是「设计上不通知」的白名单。

7. **修 `trigger-orders-pending` 的间歇 401。**
   独立于本次事故，但它会让任何依赖条件单读取的判据不可靠（包括保护健康检查）。
   建议先加一次带 `X-Request-Id` 的重试与失败计数，定位是签名、时钟还是限流。

8. **「保本离场」的语义。**
   当前被识别为 `full_exit`。本次价格在成本上方时全平≈保本离场，结果可接受；
   但若价格在成本下方，`full_exit` 与「保本」语义相反。建议在提示词/后处理里区分
   「保本离场」（价格在成本上方 → 全平或移止损到成本）与「移止损到保本」（`move_stop_to_break_even`）。

---

## 7. 附：本次只读查询清单

```
sqlite3 "file:/opt/telegram-kol-analyzer/data/research.db?mode=ro"
  sources / raw_messages / message_processing_jobs / recognition_decisions /
  authoritative_execution_attempts / context_resolution_attempts / signal_candidates /
  message_instruction_items / strategy_threads / strategy_lifecycles / execution_bindings /
  execution_order_legs / strategy_management_batches / strategy_management_legs /
  strategy_break_even_convergences / trigger_protection_intents / execution_events /
  position_mutation_intents / position_reconciliation_observations / runtime_incidents / strategy_alerts

journalctl -u telegram-kol-worker / -u telegram-kol-ingest / -u telegram-kol-web
curl -s http://127.0.0.1:8002/api/runtime-agent/read-only-exchange-snapshot   （GET，只读）

/root/ro-diag/exchange_readonly.py      list_positions / list_open_orders / list_trigger_orders_pending / get_ticker_price
/root/ro-diag/eth_trigger_retry.py      list_trigger_orders_pending ×8（401 复现）
/root/ro-diag/poshist.py                list_position_history
   （均以 python -B + PYTHONDONTWRITEBYTECODE=1 运行，仅 GET）
```
