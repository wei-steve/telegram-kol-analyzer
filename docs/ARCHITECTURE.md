# 当前架构（现状快照）

本文件只描述**当前生产运行的样子**，不记录历史演进。改动前先读这里的模式表。
历史方案在 `docs/archive/plans/`。

## 1. 进程拓扑

生产是三个独立进程，由三个 systemd unit 拉起，靠环境变量 `TELEGRAM_KOL_RUNTIME_ROLE`
区分角色（`src/telegram_kol_research/cli.py` 的 `--runtime-role` 也读这个 envvar）：

| unit | 角色 | 端口 |
|---|---|---|
| `deploy/systemd/telegram-kol-web.service` | `web` | 8000 |
| `deploy/systemd/telegram-kol-ingest.service` | `ingest` | 8001 |
| `deploy/systemd/telegram-kol-worker.service` | `worker` | 8002 |

合法角色集合 `RUNTIME_ROLES = {"all", "ingest", "worker", "web"}`
（`src/telegram_kol_research/web_app.py`）。只有 `all` 和 `ingest` 持有 Telegram 会话
（`runtime_role_owns_telegram_session`）。

## 2. 每个角色启动的后台单例任务

逐字来自 `src/telegram_kol_research/web_app.py` 的 `RUNTIME_ROLE_SINGLETON_TASKS`：

```python
RUNTIME_ROLE_SINGLETON_TASKS = {
    "ingest": frozenset({"live_listener", "reconcile"}),
    "worker": frozenset(
        {
            "authoritative_gap_recovery_loop",
            "break_even_convergence_worker",
            "contract_spec_refresh",
            "deepcoin_reconcile",
            "lifecycle_monitor",
            "message_operation_supervisor",
            "message_processing_worker",
            "position_snapshot_startup",
            "runtime_incident_notification",
            "semantic_review",
            "source_message_deletion_worker",
            "strategy_management_notification",
            "strategy_management_worker",
            "system_operator_bot_command",
            "telegram_bot_command",
            "worker_command_worker",
        }
    ),
    "web": frozenset(),
}
```

`web` 不启动任何后台单例任务，只服务 HTTP。
`all` 不是表里的键：`runtime_role_singleton_tasks("all")` 返回上表所有角色任务的并集，
这是**本地开发用的单进程模式**，生产不用。
`loop_lag_monitor` 是进程监控，不属于单例任务表，任何角色都会启动
（`runtime_role_starts_process_monitor`）。

## 3. 生产运行模式表

| 设置 | 生产值 | 代码默认值（`src/telegram_kol_research/trading_settings.py`） |
|---|---|---|
| `message_pipeline_mode` | `queue` | `queue`（`Literal["queue"]`） |
| `worker_command_mode` | `queue` | `queue`（`Literal["queue"]`） |

**代码默认值与生产一致**：两个设置的默认值都等于生产值，数据库里没有对应行时读到的就是生产模式。
两者现在都只有 `queue` 一种行为；生产数据库里遗留的 `inline` / `shadow` 值仍然读得回来——解析器记一条
warning 后按 `queue` 处理，从不抛错。

`message_lock_mode` 这个设置已经不存在了。删除前生产实际运行在 `per_chat`（2026-09-06 部署前只读核实 API 返回值），删除后 ingest 固定按 chat_id 加锁，行为等价。生产数据库的设置行里可能还留着
`message_lock_mode` / `message_lock_expected_mode` 两个键，读的时候当作未知键静默忽略，下一次写设置时自然
消失；`/api/trading-settings` 收到这两个键也不会报错。锁的现状见第 4.5 节。

## 4. queue 模式下一条消息的路径

以 `telegram_live_listener.py`、`message_processing_worker.py`、`web_app.py` 为准：

1. **ingest 落库 + 入队，不做处理。**
   Telethon 回调进入 `telegram_live_listener.persist_live_message_event`。它把原始行写进
   `raw_messages`，然后通过 `enqueue_hook` → `_try_enqueue_processing_jobs` 幂等地在
   `message_processing_jobs` 里建一条 job（`last_reason="queue_enqueued"`）。
   ingest 回调里没有任何识别或执行调用。
   ingest 的 `reconcile`（拉取补齐）路径同样只补入队，
   reason 为 `history_reconcile_enqueued`。

2. **worker 消费并做全部业务决定。**
   `message_processing_worker.run_message_processing_loop` 无条件消费，直到被取消。
   每轮按 `message_processing_max_parallel_chats` 上限
   `claim_message_processing_jobs` 认领作业，每条作业交给
   `run_message_processing_worker_tick` → `process_message_job`。
   `process_message_job` 就是**原来 ingest 回调里那条 post-persist 链**：上下文解析调度
   （`context_resolution_scheduler` / `context_resolution_worker`）、识别
   （`recognition_enabled`）、权威处理（`authoritative_processor`）、策略提醒
   （`strategy_alert_processor`）、系统 Bot 冲突通知。它只改变**在哪里调用**，
   不改变识别、策略解析、执行、提醒、通知的语义。
   过期作业由 `_classify_claim_expiry` 分类后走权威缺口恢复。

3. **web 发起的权威动作走 `worker_command_jobs`。**
   `web` 角色没有执行权限。`web_app.py` 用 `enqueue_worker_command` 把请求写进
   `worker_command_jobs`，`worker` 的 `worker_command_worker` 认领后由
   `worker_command_executor.execute_worker_command_adapter` 分发到恰好四种命令：

   | `command_type` | 适配器 |
   |---|---|
   | `sync_deepcoin_execution` | `_execute_sync` |
   | `close_bound_position` | `_execute_close` |
   | `recovery_live_submit` | `_execute_recovery` |
   | `process_next_trade_signal` | `_execute_process_next` |

   其他 `command_type` 一律 fail-closed（`unsupported_worker_command_type`）。

## 4.5 两个补偿循环，与锁在哪里

**两个补偿循环，分工不重叠。**

| 循环 | 角色 | 周期 | 对照谁 | 补什么 |
|---|---|---|---|---|
| `run_periodic_reconcile` → `run_reconcile_once` | `ingest` | 300s | **Telegram 历史** | 直播回调根本没收到的消息：拉回一小段最近历史，落库 + 入队（`history_reconcile_enqueued`） |
| `run_authoritative_gap_recovery_loop` → `recover_missing_authoritative_decisions` | `worker` | 20s | **数据库** | 已经落库、但至今没有权威决策行的消息：只入队（`recovery_enqueued`），不做任何 Telegram 调用 |

一句话：reconcile 补"没收到"，gap recovery 补"没处理"。只有 reconcile 碰 Telegram，所以 Telegram 会话
卡住不会连带拖住 gap recovery。过期分类（`authoritative_gap_recovery_max_age_minutes` 决定的
stall / stale）在 worker 的 `_classify_claim_expiry`，两个循环都不做。

**进程间不存在共享的进程内锁；跨进程互斥靠数据库状态。**

三个角色是三个操作系统进程，任何 `asyncio.Lock` 都只在自己进程内有效。所以：

- `ingest` 进程有且只有一把锁——`KeyedAsyncLockRegistry`（`keyed_async_locks.py`），
  按 `chat_id` 一把 `asyncio.Lock`，建在 `web_app.py` 的 `app.state.message_lock_registry`。
  取它的只有两处：直播回调（`run_live_listener` 的新消息与删除消息处理器）、以及 reconcile 每个
  会话的落库+入队那一小段。同群串行、跨群并行；Telegram 拉取本身**不**持锁，所以一次 reconcile
  再慢也不会冻住直播。它防的是同一个 chat 的两条写入路径撞在一起——`raw_messages` 上没有
  `(chat_id, message_id)` 唯一约束，靠这把锁保证不会插出重复行。
  `/api/runtime/loop-health` 在 ingest 角色下输出的就是这个 registry 的 `snapshot()`。
- `worker` 进程的互斥边界是 `position_authority_lock.py`（按仓位/符号），那是真正会写交易所的地方。
- `web` 进程**没有**任何消息锁。它既不持有 Telegram 会话也不执行交易，需要跨进程排他的操作一律走
  数据库：设置写入用 `BEGIN IMMEDIATE` + 期望值比较交换（`transition_message_concurrency_settings`、
  `save_trading_settings`），权威动作走 `worker_command_jobs` 队列。

不要为了"保险"再引入一把进程内的全局锁：它在三进程拓扑下保护不了任何跨进程的东西，只会把同一个
进程里本可以并行的活动串起来。

## 4.6 Deepcoin 读限流：为什么按角色分配 3/1/1

Deepcoin 的频率限制是 **每个 API key 5 次/秒**，按整个账户计，不按进程计。超限的返回是
`HTTP 401` + 响应体 `{"code":"50000","msg":"Trigger the api frequency limiting"}`，
头部 `X-Ratelimit-Limit: 5 / Remaining: 0 / Window: 1s / Retry-After: 1`。
`50000` 不在官方错误码表里，而 401 平时表示认证失败，所以**只有 401 与 code 50000 同时成立**
才算限流（`DeepcoinRateLimited`）；其余 401 一律仍按认证失败处理，绝不重试。

限流器是进程内的（`DeepcoinReadRateLimiter`，令牌桶），而生产是 web/ingest/worker 三个操作系统
进程，任何一个都看不见另外两个的请求。进程内限流器唯一能保证账户不超限的办法，是**各自只持有配额的
一份固定份额**。份额**按角色分配，不是平均分**（`DEEPCOIN_READ_LIMIT_PER_SECOND_BY_ROLE`，
由环境变量 `TELEGRAM_KOL_RUNTIME_ROLE` 解析）：

| 角色 | 份额 | 为什么 |
|---|---|---|
| `worker` | **3/s** | 唯一有持续读循环的角色（`deepcoin_reconcile` + 阶段 4 影子 pass） |
| `web` | **1/s** | 只在人工 API 调用时读 |
| `ingest` | **1/s** | 只在断线重同步时读 |
| `all` | 5/s | 本地单进程开发模式，一个进程跑三个角色的循环，所以持有三份 |
| 其他（运维 CLI 工具、临时脚本） | 1/s | 它们是**在三个服务之外**多出来的进程，账户配额已经分完，只能拿最小份额 |

3+1+1 = 5，正好用满，**没有给写入留余量**：写入稀疏且突发、走自己的限流器，真撞上天花板产生的
401 按"结果未知、绝不重发"处理，语义本来就是对的。

平均分（每进程 2/s）在 2026-09-07 实测过并被否决：worker 的 reconcile 轮时长从中位 13.8 秒
变成 32–44 秒，轮间隔从约 44 秒变成约 65 秒——那是拿保护收敛延迟去换 web/ingest 根本用不到的余量。

**不要单独调高某一个角色的值。** 这几个数只有作为一组才是安全的；调高一个而不调低另一个，就是账户
超限的确切来路。也不要把它做成运行时开关（数据库设置、环境变量），常量是刻意的。

**限流器按物理 HTTP 请求计数，不按逻辑调用计数。** `list_open_orders` 走 V2 分页后，一次逻辑读会
展开成 N 页 N 次请求，每页各取一个令牌；一次限流重试也再取一个。按逻辑调用计数会把真实速率低估整整
一个页数倍。

**限流重试只对 GET，且最多一次**（等 `Retry-After`，上限 2 秒）。POST 一律不重试：被限流的写入
仍然是"结果未知"的写入，沿用 `DeepcoinRequestOutcomeUnknown`（见硬性禁止第 2 条）。

**轮内读缓存。** `worker` 的一轮 `deepcoin_reconcile` 会为同一个 instId 重复读
`positions` / `trigger-orders-pending` / `orders-pending`。`DeepcoinRestClient.begin_round_read_cache()`
在这一轮内让每条路径只真正请求一次。作用域严格等于一轮：**任何经同一 client 的写入立即整体作废
缓存**（写后再读一定是新读），轮结束无条件丢弃，**绝不跨轮**。命中缓存不产生物理请求，因此也不取令牌。
历史与成交（`orders-history` / `fills` / `trigger-orders-history`）不进缓存——一轮内没人重复读它们。

健康端点 `/api/runtime/deepcoin-ws-health` 输出本进程的 `read_limit_per_second`、
`rate_limited_last_hour` 与 `retry_after_waits_last_hour`；worker（8002）那一份才是有意义的
那一份。

阶段 5 入场迁到普通 order 之后，V2 `orders-pending` 的分页会放大 worker 的请求量，届时要按实测
重新评估这组份额。

## 4.7 入场腿走哪个端点，以及一笔入场归到哪个仓位

**普通入场限价腿走 `POST /deepcoin/trade/order`（`ordType=limit`），不再走 trigger-order。**
迁移判据在 `deepcoin_limit_entry.limit_leg_requires_trigger_order()`，逐腿判定：只有
`triggerPrice` 恒等于 `price`、不带任何触发语义的腿才迁；带真实突破/回落条件或
`last`/`mark`/`index` 价格来源选择的腿**继续走 trigger-order**，并保留原来的父子归属流程。
认不出的腿形状一律留在 trigger-order——判据不明确就不迁。

payload 字段组合是 2026-09-07 受控实盘实验的结论，白名单在
`deepcoin_limit_entry.LIMIT_ENTRY_PAYLOAD_FIELDS`，多一个字段就报错：
`instId, tdMode, mrgPosition, side, posSide, ordType=limit, px, sz, slTriggerPx`
（`tpTriggerPx` 可选，生产是止损单发，止盈仍等确切成交 posId）。
**不带 `clOrdId`**：四组单变量对照证明**这个字段存在本身**就会被 `sCode=14 DuplicateAction`
拒绝，与并发、与订单经济属性、与取值都无关。市价腿一直带 `clOrdId` 且 149 次全成功，
**不要**把它去掉。本地生成的幂等键仍然落 `execution_order_legs.client_order_id`，
但**不发给交易所、也不作为交易所所有权证明**——成交回包里的 `ordId` 才是订单身份。

**一笔普通 order 开出的仓位，其 posId 等于该 order 的 ordId。** 这是一条**等式**，不是外键：
`POST /trade/order` 的响应字段只有 `ordId/clOrdId/tag/sCode/sMsg`，没有 `posId`；没有任何
读接口同时给出 ordId 与 posId；流上 `Order`/`Trade` 不带仓位字段，`Position` 带 `PI` 不带订单字段。
因为是等式而不是交易所给的值，**绝不单独采信**，必须三重确认同时成立
（`deepcoin_ordinary_entry_binding.resolve_ordinary_entry_attribution()`）：

1. 流上推过 `PI` 等于该 ordId 且 `Po` 非零的 `Position` 帧（仓位真的开了）；
2. REST 在该 posId 下确实列出一个仓位；
3. 方向一致，且仓位数量非零、不大于下单量（部分成交仍是本单的仓位，更大就不是）。

任一不成立即 `attribution_status='unverified'`，而 `== 'verified'` 是本仓库每一处自动修改、
撤销、认领的前置条件，所以 unverified 就是自动动作全部止步。

**迁移后的限价腿在 order 上自带 `slTriggerPx`，止损从成交那一刻起就由交易所持有，与归属无关；
市价腿不同**——它的 payload 不带止损，止损靠成交后 `set_position_tpsl` 写，而那道写入门要求
verified。所以「市价成交但归属 unverified」是唯一一种仓位可能裸奔的情形，必须让人立刻知道：
生成 `market_fill_attribution_unverified`（severity **critical**，来源 `deepcoin_entry_order:<ordId>`，
`impact` 里带 instId / side / sz / 候选 posId），并且**无论环境变量的投递白名单列了什么都会送达**
（`config.ALWAYS_NOTIFIED_INCIDENT_TYPES`；`telegram_notifications_enabled` 仍是唯一的总开关）。
详细 summary 万一被越界检查拒绝，会退回一份不含插值的最小 summary 重记一次——少说一点的告警
远胜于没有告警。旧代码在这里会用 symbol+side 扫描认领并标 verified；在 split 模式多仓并存时
那可能把止损挂到别人的仓位上，所以认不出是谁的仓位就不动作。裸仓安全网是独立后续项 B-5d。
**这条等式只对普通 order 成立**：2026-09-07 只读核对生产 `execution_order_legs`，
market 入场腿两个 id 齐全的 153 条 **153 条**满足，trigger_limit 的 204 条**一条都不满足**
（条件单的仓位以它派生的子单命名）。所以条件单那条链原样不动。

**新入场在 WS 观测不完整时不提交。** `deepcoin_entry_admission` 是唯一的判定入口：
worker（与本地 `all`）角色必须有活的 inbox 且 `ws_observation_permits_new_entry()` 放行，
否则 `RecoveryLiveSubmitError("ws_observation_blocked_new_entry:<原因码>")`——
**是"不提交"，不是"提交后再撤"**，原因码随失败落库而不是被静默吞掉。
理由是阶段 4 补测 12：Deepcoin 私有流**重连不重推**，只推变化；错过
`TU: default → posId` 那一帧就没有第二次机会，REST 也补不回来。角色由 web 启动时
显式登记（`set_entry_admission_runtime_role`），不读环境变量——角色来自一个只是
**默认取**环境变量的 CLI 选项，环境变量不是它的权威来源。

回退就是 `tg-deploy <上一个 SHA>`，**没有运行时模式开关**，也不需要回滚任何状态。

## 5. 模块分类（已核实）

`src/telegram_kol_research/` 共 240 个业务模块（另有 3 个 `__init__.py`）。分类方法与逐条判定见
`docs/plans/2026-09-06-post-migration-cleanup/step-5-inventory.md`：用 AST 建全包 import 图，
从 `web_app.py`（它 import 了 `RUNTIME_ROLE_SINGLETON_TASKS` 里全部 loop 函数）、
`message_processing_worker.py`、`strategy_management_worker.py` 三个根做传递闭包。
**243 个 `.py` 里有 188 个落在这个在线闭包里。**

**在线主路径（每条消息/每笔交易都可能走到）：**

- 摄入与生命周期：`telegram_live_listener.py`、`web_app.py`、`cli.py`
- 队列与执行边界：`message_processing_worker.py`、`worker_command_executor.py`、
  `worker_command_jobs.py`、`recovery_execution_queue.py`
- 识别与解析：`authoritative_recognition.py`、`context_resolution.py`、`semantic_review*.py`
- 交易与保护：`deepcoin_*.py`、`trading_settings.py`、`position_*.py`、
  `strategy_management_*.py`、`entry_*.py`、`protection_*.py`
- 通知与运维：`system_operator_bot.py`、`telegram_bot*.py`、
  `runtime_incident_*.py`、`runtime_deployment_identity.py`

**名字像一次性修复、实际在线的模块。** 下面这些文件名含
`repair`/`recovery`/`reconcil`/`convergence`/`rescue`/`cleanup`/`remediation`，
但都在在线闭包里，**不要按名字判断可删**：

```
break_even_convergence_executor.py       recovery_live_submit.py
break_even_convergence_planner.py        recovery_live_submit_gate.py
break_even_convergence_worker.py         recovery_order_confirmation.py
entry_admission_reconciler.py            recovery_order_confirmations.py
entry_assembly_fingerprint_repair.py     recovery_runner.py
entry_protection_ledger_repair.py        recovery_scan.py
instruction_execution_reconciliation.py  remediation_snapshot.py
position_reconciliation_observations.py  repair_confirmation.py
recovery_decisions.py                    strategy_management_composite_reconciliation.py
recovery_execution_queue.py              strategy_management_reconciliation.py
terminal_entry_cleanup.py                trigger_protection_rescue_worker.py
trigger_take_profit_convergence.py       trigger_take_profit_convergence_executor.py
```

**只由 `cli.py` 可达的运维修复工具。** 不在任何角色进程的路径上，由操作者手工按
`docs/runbook.md` 的 dry-run → apply 流程调用。它们是**常备**工具，不是一次性残留：

```
backfill.py (sync)                        management_history_recovery.py
backup_stop_repair.py                     position_attribution_repair.py
evidence_backfill.py                      position_management_liveness_recovery.py
historical_attribution_cleanup.py         protection_incident_convergence.py
                                          tpsl_ledger_backfill.py
                                          worker_command_reconciliation.py
```

**一次性修复，已归档。** `one_off/` 子包（步骤 5 建立）：目标数据已处置完毕、文档把工具本身
标为 evidence-only、代码上零在线引用。`tests/test_one_off_isolation.py` 静态守护它不被
`cli.py` 以外的任何模块 import。当前只有 `one_off/historical_management_terminalization.py`。

**造好但从未在生产 apply 的一次性工具（`unsure`，仍在包根目录）。** 它们是**在建工作**而不是
残留，删或移都会丢东西：

```
batch150_management_terminalization.py   legacy_backup_reconciliation.py
context_analysis_backfill.py             legacy_conditional_cancel.py
current_protection_backfill.py           manual_pending_entry_reconciliation.py
frozen_exchange_empty_state_alignment.py native_tpsl_migration.py
historical_state_repair.py               position_management_remediation.py
                                         take_profit_protection_leg_repair.py
```

**注意 `reconcile.py` 不是 ingest 的单例任务。** `RUNTIME_ROLE_SINGLETON_TASKS["ingest"]` 里那个
名叫 `"reconcile"` 的任务是 `telegram_live_listener.run_periodic_reconcile`
（`web_app.py:5214` 创建 `app.state.reconcile_task`）。模块 `reconcile.py` 本身只有一个纯函数
`build_reconcile_window`，生产代码零引用，只有 `tests/test_reconcile.py` 还 import 它。

## 6. AI 协作提示

- 改任何东西之前，先看上面第 3 节的模式表：**生产和代码默认值都跑 queue**。
  运行时真值仍以数据库里的设置行为准，不要只看默认值就下结论。
- 代码里已经没有 `inline` / `shadow` 分支了（清理方案步骤 3 删除）。消息与命令路径各只有一条，
  **不要重新引入模式开关**来做灰度或回滚。`message_processing_jobs.shadow` 列还在表上，
  新行恒为 `0`，worker 认领时用 `shadow = 0` 过滤掉历史行；删列是以后的 L3 工作。
- `web` 角色没有执行权限。任何需要写交易所或改仓位的动作，必须经 `worker_command_jobs`
  走那四条命令之一，不要在 web 进程里直接调交易所客户端。
- Deepcoin 读限流是进程内的、按物理请求计数的（第 4.6 节）。加新的交易所读调用时不需要自己限速，
  但**不要绕过 `DeepcoinRestClient` 直接发 HTTP**，那会让限流器和计数同时失明。
- 锁只在自己进程里有效（第 4.5 节）。要跨进程排他就用数据库状态，不要新加进程内全局锁，
  也不要把 `KeyedAsyncLockRegistry` 当成跨进程的锁用。
- **运行时事件的告警类型有一组代码基线，env 只能加不能减**
  （`config.ALWAYS_NOTIFIED_INCIDENT_TYPES`）。`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`
  与 `..._CAPTURE_TYPES` 只要**非空**，加载时就会与这组基线取并集：管理指令失败、权威执行落
  uncertain、后台任务放弃重启、市价成交无法归属这几类，不能因为运维改错了一行 env 就静音。两个关断位仍然有效：
  键**缺席**等于"全类型"，键为**空串**等于"一条都不发"。要单独关掉基线里的某一类，只能改代码，
  这是刻意的代价。
- 迁移只改变"在哪里跑、怎么组织"，从不改变"决定什么"。任何看起来需要改交易语义的改动
  都是读错了需求，停下来问。
