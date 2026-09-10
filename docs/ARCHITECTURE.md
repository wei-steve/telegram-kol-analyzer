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

**已知的一次有意重读（A-10d，2026-09-10）。** `sync_manual_closed_deepcoin_positions`
在开头读一次 `positions` 给清理阶段用，**又在 binding 行查询之前紧挨着重读一次**，并以这次读取的
真实时刻作为"账本是否在快照之后才被认领"的参照。清理阶段没有交易所写时，这次重读由轮内缓存服务、
**零物理请求**；有写时缓存已作废，**这是每轮最多一个额外 GET**。
为什么要重读：开头那次读与行查询之间隔着清理阶段的交易所往返，实测达 **24 秒**，
而 `strategy_management_planner` 会在这段时间里用它自己的时间戳刷新所有活 binding 的 `recovered_at`
（它是 `reconcile_deepcoin_execution_bindings` 的另一个调用点）。**拿轮次开头的 `synced_at` 当快照时刻，
会让守卫每轮拒判所有 binding**——A-10c 上线后 25 轮全部如此。

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

## 4.8 保护单归谁，以及"改止损"到底是哪两步

**`set-position-sltp` 是叠加语义，不是修改语义。** 6-pre-3 只读观测了 18 次生产写入，
拿到 18 个互不相同的 `ordId`，**旧单一张都没消失**。所以"改止损"在这个交易所上永远是
"挂一张 + 撤一张"，没有第三种写法。再发一次 set-position-sltp 只会让同一个仓位上并存两张
触发价不同的止损，实际生效的是先被触及的那张——对止损而言就是**更靠近现价的那张**，
等于修改没生效。

**保护单归属只有一条判据：`TU == posId`。** `OS`（保护单自己的 ordId）每次写入都变，
REST 也从不在一个返回里同时给出 ordId 与 posId，所以新旧两张止损之间唯一的可查关联，
就是它们的 `TU` 都指回同一个 posId（6-pre-3：30 条能连上的帧 30 条相等）。
`protection_authority.resolve_protection_authority()` 就是按这条判据回答"这个仓位有哪些
保护单"：verified 入场腿 → `position_protection_ledger` 的行 → 加上 `TU` 指向本仓位的挂单。

- 账本不认识、但 `TriggerOrder` 帧的 `TU` 指向本仓位的挂单，**先认领进账本**
  （`evidence_source='exchange_adopted_by_tu'`）再动它。推送本身不被单独采信：
  该单必须同时出现在 REST `trigger-orders-pending` 里，且 instId / posSide /
  `triggerOrderType=TPSL` 相符。
- **谁都放不进去的一张 TPSL 挂单会冻结整个仓位**（`protection_order_unattributable`，
  落 `position_protection_incidents`）。那一刻"不是我们的"和"是我们的但没记下来"不可分辨：
  撤它是盲写，留它则意味着旧止损仍然武装、这次修改等于没生效。两种猜法都会错，所以停下来叫人。
- `triggerOrderType == "Conditional"` 是**挂单入场**，这条路径永远不撤它。
- **挂在"尚未成交的限价入场单"上的止损要排除掉，它不是任何仓位的保护单。**
  迁移后的限价腿把 `slTriggerPx` 带在 order 上（第 4.7 节），交易所在入场单还挂着的时候就把这张止损
  摆进 `trigger-orders-pending`，形状与"某仓位的无主止损"**完全一样**：`TPSL`、无 posId、`TU="default"`。
  2026-09-10 实测：binding 347 的两张在挂入场单（sz 6 / 14、`slTriggerPx` 81000）
  让**每一个 BTC 空头仓位**在整个影子窗口里被冻结 26 次。判据是两条**同时**成立才排除：
  (a) 该 ordId 的 `TriggerOrder` 帧 `TU == "default"`（仓位还不存在），且
  (b) `(instId, posSide, sz, slTriggerPrice)` 等于我们自己某条**仍 pending 的入场腿**的请求四元组。
  两条都查本地持久记录。**这是排除不是认领**——排除后既不进保护集合、也不冻结、更不会被撤，
  判错的代价是"我们不碰它"。入场成交后 `TU` 翻成子单 posId，那张单自然经 `TU` 归属，
  不需要特殊处理；所以判据必须是"`TU` 恰好只有 `default`"，而不是"`default` 出现过"
  （翻转会把两个值都留下）。每排除一张记一次 `excluded_pending_entry_stops`，与冻结计数成对看：
  冻结数降到零有两个成因，只有这个计数分得开。

- 一张同时带 `slTriggerPrice` 与 `tpTriggerPrice` 的单也冻结：按组替换会把另一半一起撤掉。

**两个组，两种相反的顺序**（`protection_replacement.py`，`deepcoin_execution_actions.adjust_position_tpsl`
与 `break_even_convergence_executor` 共用同一份实现）：

| 组 | 顺序 | 为什么是这个方向 |
|---|---|---|
| 止损（`stop_loss` / `backup_stop`） | **先挂新 → 回读确认 → 撤旧全集 → 确认撤净 → 才改账本** | 两张止损并存只是短暂**过度保护**（先触及者执行，方向仍是保护）；而撤与挂之间的空隙是**裸仓**。失败保留新单、记 `stop_resize_replace_incomplete` 并冻结，绝不回撤新单——回撤才是唯一可能把仓位变裸的写入。 |
| 止盈（`take_profit`） | **先撤旧 → 确认撤净 → 再挂新** | 两张止盈并存**不是无害的**：各自按自己的 sz 平仓，合计可能超过在仓量。而止盈的空窗期仓位仍有止损、不裸奔。挂新失败只告警并交止盈收敛重试，**不冻结止损**。 |

两组都要改时**先止损组、后止盈组**：先把下行定下来，再动上行。

**撤销前必须按 ordId 精确回读。** 解析出保护集合的那次读，和真正发出撤单的那一刻，是两个时刻；
中间交易所可能已经把这张单替换、成交或改量。所以撤之前再按 ordId 看一眼，instId、posSide、
触发价、数量四项全对得上才撤，任一不符就放弃并告警（`protection_cancel_target_*`）。
**撤完还要再读一次确认它真的离开了 `trigger-orders-pending`**——读失败算"不知道"，
绝不算"已经没了"，否则账本会把一张可能仍在武装的单标成已撤。

**拒绝本身就是告警。** 绑定 unverified、保护集合无法解析、认领写库失败，一律拒绝写入并落
`protection_authority_refused`。理由是从外面看，"止损没被移动"和"止损不需要移动"长得一模一样，
一次沉默的拒绝没人会发现。

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
- **要判断某个服务进程实际看到什么环境变量，只有 `/proc/<pid>/environ` 可信。**
  三个 unit 的变量来自 `EnvironmentFile=/etc/telegram-kol-<role>.env`，而
  `systemctl show <unit> -p Environment` **只列 unit 文件里内联的 `Environment=`**，
  不含 EnvironmentFile 的内容——在这台机器上它只会回一个 `TELEGRAM_KOL_RUNTIME_ROLE`。
  拿它当进程环境去复算配置，会得到"告警不会被捕获""密钥没配"这类完全错误的结论
  （A-3d 只读核实 `entry_admission_expired` 可达性时踩过一次，`captures` 假阴性）。
  正确做法：`PID=$(systemctl show <unit> -p MainPID --value)`，再解析
  `/proc/$PID/environ`（`\0` 分隔）喂给 `load_*_config(environ=..., environment_only=True)`。
  注意那份 environ 里含 bot token 等凭据，**只用不打印**。
- **仓位行的 `slTriggerPx` / `tpTriggerPx` 不能用来判断"这个仓位有没有止损"。**
  A-4 已经发现它只反映最近一对 TPSL，A-5b 又撞了一次：pos `1001125179691393`
  的仓位行 `slTriggerPx` 是空串，而 `trigger-orders-pending` 里明明挂着**两张**
  `triggerOrderType=TPSL`、`sz=0`（sz 为 0 表示全仓）、`slTriggerPrice` 分别是
  2530 与 2535.06 的止损单，账本里那两条 `stop_loss/verified` 行与它们逐字段一致。
  只看仓位行会得出"裸仓"的错误结论，进而做出错误的补挂动作。
  **判据只有一个**：读 `trigger-orders-pending`，筛 `triggerOrderType == "TPSL"`
  且 `posSide` 与仓位一致的行，看 `slTriggerPrice`。
  **字段名也不要想当然**：这个端点上叫 `slTriggerPrice` / `tpTriggerPrice`（另有
  `closeSLTriggerPrice` / `closeTPTriggerPrice`），**不是**仓位行上的 `slTriggerPx` /
  `tpTriggerPx`。取一个不存在的键得到 `None`，读起来和"交易所没有这张单"一模一样——
  A-5b 因此两次把有止损的仓位判成裸仓，其中一次差点让人去手动补挂。
  排查这类问题时先原样打印整行 JSON，再挑字段。
  同一份返回里 `triggerOrderType == "Conditional"` 的行是**挂单入场**（开仓方向的
  `side`），不是保护单，别把它算进保护里。
- **带命名空间前缀的 id，配上一个会丢掉前缀的匹配器，是一类专门的缺陷。**
  触发条件不是"有两个并列的处理器"（那在本仓库到处都是），而是：
  **一个形如 `batch:7` / `signal:7` 的 id，遇上 `int()`、`split(':')[-1]`、子串包含这类
  会把命名空间丢掉的匹配**。2026-09-10 的实例：6-pre-7 的 signal 扫描若不检查 `signal:` 前缀，
  `batch:7` 同样解析成整数 7，于是它会去查**无关的** trade_signal 7、发现它是终态、
  然后归还一把**仍在运行的批次**持有的租约——两个命名空间的 7 是不同的东西，前缀是唯一的区分。
  A 线同期自查出一个同族的：`marker in text` 的子串匹配丢掉了"这句话属于哪个命名空间"。
  **按这个条件筛，一个模块通常只命中一两处，规则才用得起来。**

- **「松匹配」本身不是缺陷等级，它落在哪个方向才是。** 同样是匹配过松：
  子串匹配误吃一条识别失败，代价是 auto_trade 群多告一次警（over-alert，本该沉默的多说了一句）；
  前缀丢失误归还一把仍在运行的租约，代价是另一个批次拿到交易所写入权。
  前者可以排期，后者必须当场补。**这个判据能回答"那我先修哪个"，而"匹配要严"回答不了。**

- **名字声称覆盖双向的用例，要确认它真的两个方向都跑了。判据是删掉那道检查它会不会红，不是它的名字。**
  6-pre-7 有一条 `test_the_two_sweeps_do_not_touch_each_others_holders`，读起来覆盖双向，
  实际只测了"batch 扫描遇到 signal 持有者"；把前缀检查删掉后它**照样全绿**，变异检验才把缺口指出来。
  会写这条用例的人通常也会漏掉其中一个方向，所以靠"记得写反向用例"防不住，**靠变异检验才防得住**。
  **但变异检验本身在纵深防御下会说谎，这是上面那条的修正。** 同一条性质若被多处守卫挡着，
  只关掉其中一处，用例照样全绿——读起来就是"这些用例根本没在测"，而事实恰恰相反。
  2026-09-10 6c 的实例：`unverified` 绑定不得修改/撤销/平仓，单独关掉
  `require_verified_position_ownership` 时**十条用例全绿**，因为网关里还有一处独立的
  `attribution_status` 比较；两处一起关才有 6 条转红。**所以判据要改成：把同一条性质的
  全部守卫一起关掉，才算证明用例咬住了。** 只关一处得到的绿，既不能证明用例没用，
  也不能证明它有用——它什么都不能证明。
  **同一次检验还要盯住"拒绝的理由对不对"：因为夹具的原因被拒，不算拒绝。**
  6c 的平仓用例最初拿到的是 `position_not_bound_to_exactly_one_active_binding`——
  夹具没给 binding 写 `pos_id`，于是它在触及归属检查**之前**就失败了，
  而"因为夹具坏了被拒"与"因为归属未核实被拒"在断言里长得一模一样。
  修好夹具让用例真正走到那道门之后，单点变异才让它转红。

- **在工作树里跑全量之前，先确认 `.venv` 存在（没有就建符号链接指向主检出的那个）。**
  `tests/test_server_update_scripts.py` 与 `tests/test_minimal_server_updater.py` 把
  `PLANNER_PYTHON=<仓库根>/.venv/bin/python` 传给被测脚本，`<仓库根>` 是**测试文件所在的那个**——
  主检出有 `.venv`、工作树没有，于是这两个文件的 15 条一起 `exit 2 "Planner Python is unavailable."`。
  2026-09-10 B 线因此报了三次"全量 15 failed，与基线相同"；**"与基线相同"不是解释**，
  它只是把两个都没查的现象并排放着。两条线跑同一套件结果不同时，先对命令行与环境
  （cwd、`.venv`、env 文件、`-p no:randomly`），不要先假定是代码。

- **观察监视器必须实时比对生产 HEAD，只看 unit 是否 active 抓不到换版。**
  `tg-deploy` 的重启只花几秒，而监视器通常一分钟采一次样，正好采不到 unit 非 active 的那一刻。
  2026-09-09 两条线各撞一次：B 线 6-pre-5 的窗口被 A 线部署换了 HEAD，40 条采样**每一条都
  `units_ok=1 / healthy=1`、`window_reset` 计数为 0**——账面上"连续健康"的窗口，实际后半段观察的
  已经是别人的版本。更隐蔽的是 `deploy_sha` 若写成启动参数而非每次实时读，采样看起来还会一直显示
  "我在观察我部署的那一版"。**做法**：每次采样 `git -C /opt/telegram-kol-analyzer rev-parse HEAD`，
  与本次期望的 SHA 比对，不等就判不健康并重置窗口。这个判据不依赖"恰好采到重启那一刻"。
  A 线当时是实时读 HEAD 但**只记录不重置**，于是 A-6c / A-8b 两个窗口各有 11/16 的采样跨版仍被算作达标——
  记录跨版和让跨版使窗口失效是两回事，必须两样都做。

- **多线并行时，部署前先问对方有没有在跑的观察窗，并等到明确答复。** 同上那次的成因就是双方都没问。
  紧急项可以不等，但要在消息里写明"这是紧急项、不等回复"，而不是默认不等。
  给对方的可用时段要按**窗口真正的达标条件**算：L2 窗口卡在"≥5 条真实消息"上时，时长早已满足也可能
  遥遥无期，这时应当直接说"别等我"，而不是给一个基于时长的假承诺（B 线当天就给错过一次）。

- **判断后台进程是否还活着，不要用 `pgrep -f <脚本名>`。** 发起这次检查的命令行**自身**
  就含有那个模式，于是 `pgrep` 匹配到自己，永远返回"还活着"。经 ssh 执行时尤其隐蔽：
  远程那条 `bash -c "pgrep -f observe.sh"` 的命令行里就有 `observe.sh`。
  6-pre-3 之前踩过一次——监视器其实早已达标退出，等待器却又空转了 6 小时才被人发现。
  正确做法：让脚本退出时写一个**标记文件**（`DONE` / `WINDOW_MET`）并检查该文件，
  或者启动时记下**精确 PID** 再用 `kill -0 <pid>` / `ps -p <pid>` 判断。
  同一天 A 线用 `pkill -f step7b_observe.sh` 清理监视器，那个模式匹配到了自己的 ssh 命令行，
  **直接把整条会话打断**（exit 255）。
  **停进程时还要确认杀的是脚本本身，不是它的父 `bash`**：B 线 kill 了 `bash -c` 的外层 pid，
  脚本活得好好的，与新起的实例**同时往同一个采样文件里写**了三分钟，两段输出混在一起只能整段作废。
  用 `ps -eo pid,cmd | awk '/^bash \/root\/observe-x\.sh/'` 这类**锚定完整命令行**的方式取 pid，
  停完再数一次确认为 0。

- **这台机器的本地时区是 UTC+8，而数据库里所有时间戳是 UTC。** 两者差 8 小时，
  两条最容易踩的线：
  `journalctl --since "2026-09-08 22:05"` 把裸时间戳按**本地**时间解析，所以传一个 UTC
  时刻进去，实际查的是 8 小时之前——查部署后日志会查回上一个 worker 进程的日志，
  把早已不存在的 PID 的历史报错当成本次部署引入的问题（A-5 部署核实时踩过一次）。
  正确写法是锚在 epoch 上：`journalctl -u <unit> --since "@$(date -u -d "<UTC 时刻> UTC" +%s)"`。
  反过来，`sqlite3` 查 `created_at >= '<时刻>'` 里的时刻必须是 **UTC**，因为
  `models.utc_now()` 写进去的就是 UTC；用本地时间去比会多算 8 小时的数据。
  判断某个时间戳是哪一边：`date -u`、`date`、`SELECT MAX(created_at) FROM raw_messages` 三个一起看。
- **`trading_settings` 是 key/value 表，但全局设置全在 `key='global'` 那一行的 JSON 里**，
  不是一个设置一行。`SELECT ... WHERE key LIKE '%_delivery_after_id'` 会查出空集，
  然后让人误以为设置没写进去。要看某个字段：
  `sqlite3 -readonly <db> "SELECT value_json FROM trading_settings WHERE key='global';"` 再解 JSON，
  或者直接 `curl -s http://127.0.0.1:8000/api/trading-settings`。
  写入走 `POST /api/trading-settings`（只带要改的键，服务端与现有值合并）。
- **备份与演练副本有保留上限，磁盘不是无限的。** 生产库现在接近 1G，一份整库副本就是 1G。
  2026-09-08 盘点时 50G 的盘只剩 2.8G，其中 9G 是历史备份与演练副本：`data/evidence/` 下六份
  08-25 的整库快照、`data/backups/` 下 07-26 的八份、`data/manual-reconciliation-backups/` 两份、
  `data/` 顶层十几份 `.bak`，以及 A-3 作废积压时留下的两份 `rehearsal*.db`。
  规矩：**演练副本用完即删**（演练结束、结论写进证据文件那一刻就删，`rehearsal-report.json`
  这类结论文件保留）；**修复备份只保留最近两份**，更早的在下一次修复开始前删掉。
  证据目录里的 JSON / md / 脚本一律保留——占地的是 `.db`，不是它们。
  清理前把路径、大小、sha256 前 12 位写进当步的证据文件，清理后记 `df`
  （范例：`/root/evidence/step5/disk-cleanup.md`）。
- **`position_reconciliation_observations` 是"变更追加"表，不是心跳表，也不能当"当前持仓"用。**
  写入方 `execution_bindings.py::_record_owned_position_observations` 只收 `_has_nonzero_size()`
  为真的仓位行，且按 `snapshot_fingerprint` 去重：**指纹没变就不写新行**。两个后果各坑过一次（A-7）：
  一是**它的最新一行有多旧完全说明不了 reconcile 是否还在跑**——生产上最新一行停在 04:44、
  当时 09:08，而 reconcile 每约 20 秒一轮跑得好好的。A-7 第一版拿"5 分钟内有完整快照行"当新鲜度判据，
  于是 auto_trade 群**每一条**管理指令都拿不到候选、全变成 `management_target_needs_confirmation`
  （incident 2082 / raw 15628 / `snapshot_stale`），减风险指令也一起被挡。
  二是**仓位平掉后不会补一行 `size=0`**，只是不再出现，最新一行仍是它当初的持仓量，
  所以"取每个 pos_id 最新一行、size>0 即在场"会把每个历史已平仓位永远判成在场。
  **要问"现在有哪些仓位、我们的视图新不新鲜"，读 `execution_bindings`**：reconcile 每轮用实时持仓列表
  重算每条 binding 并盖 `recovered_at`（在场 → `status='active'` + `last_exchange_status='position_ownership_verified'`；
  仓位没了 → 同一轮改成 `closed/entry_legs_terminal` 或 `stale/verified_position_missing_from_exchange`）。
  `max(recovered_at)` 是新鲜度，逐行的 `recovered_at` 也要在同一个窗口内——reconcile 会跳过
  manual-terminal 和 pos_id 冲突的 binding，它们的行停在旧时间。
  同类陷阱还有 `position_protection_ledger.last_seen_at`（变更式，停在 04:44）与
  `.updated_at`（每轮刷新）：**同一张表上，一个字段是心跳、另一个是变更时间，用之前先确认是哪一种。**
- **"缺陷不再发生"本身从来不是证据。观察窗里必须有一个东西记下"它差点发生、被挡住了"。**
  修好一个判据之后，坏结果的计数会降到零——但它降到零有两个成因：**守卫真的挡住了**，
  或者**这段时间本来就没有要挡的东西**，而只看那个计数分不开这两者。2026-09-10 两条线各自
  独立踩到同一个形状：B 线的静默探活**通过时不写缺口行**，所以"缺口变少"既可能是探活起了作用、
  也可能是流本来就不静默；A 线 A-10b 的 `marked_closed` 降为零，既可能是缺席证据规则生效、
  也可能是那半小时根本没有 binding 需要关闭。**做法**：为守卫的每一次生效加一个**正向观测量**，
  和坏结果的计数**成对**去看。A-10b 用的是两个落库的计数——`position_absence_observed`
  （看了、没定罪）与 `skipped_claimed_after_snapshot`（快照比事实旧，整条拒判）；B 线用的是
  journal 里可回读的单次探活决定。**优先落库或落 journal，不要只放进程内计数器**：
  重启归零、只能从 health 端点读的计数器，事后无法回答"那一刻到底挡没挡"。
  这条与本节前面"记录跨版和让跨版失效是两件事"同源——**指标下降有多个成因时，
  必须另找一个能区分它们的观测量**，而不是把最省事的那个解释当成结论。
  **但正向观测量本身也会错，而且错了更危险**——因为整个判据现在都靠它。同日 6-pre-4 的实例：
  一次探活打了两条日志（新加的观测行与更早的 `gets=` 行并存），而观测脚本用 `grep -c` 数次数，
  于是"探活通过次数"从第一个样本起就翻倍；同一个脚本里 `journalctl --since` 按**本地**时区解释、
  而 sqlite 那半边按 UTC 比，新窗第一个样本零秒就把上个窗口的探活算了进来。
  两个都是**观测量的定义依赖了一个没人保证的不变量**（"一次事件一行"、"时间戳自带时区"）。
  **同一条纪律也适用于跑测试本身：全量运行期间不得改工作树。** 静态 `ast` 扫描类用例
  （`test_one_off_isolation` / `test_naked_fill_stop_net_boundary` /
  `test_position_authority_boundary_coverage`）**直接从磁盘读源文件**，读到编辑中间态会红——
  2026-09-10 A 线因此收到 4 个假失败，四条单独跑全绿。**而反过来更危险**：读到编辑前的旧内容会给出
  一个**对不上当前代码的绿**，而绿没人复查。所以**静态 ast 测试的绿只对运行那一刻的磁盘内容成立**；
  要它对某个提交成立，就得在 `git status` 干净、`HEAD` 等于那个提交的树上重跑一遍
  （B 线当天就是这么把自己的绿证实的，而 A 线只证明了红是假的）。
  **成对的两个观测量必须来自独立的读，否则它们只是同一个数的两份拷贝。** 前面那条说"守卫要有
  成对的正向观测量"，但没说这一句，而它才是让配对成立的前提。2026-09-10 6b 的实例：
  "撤销前按 ordId 回读四项"若与**解析这次授权所用的那一次读**相比，两个数出自同一次读、必然一致——
  它不是一个会失败的检查，而是一个**会一直说"没问题"的计数器**。这里"缺的"不是配对观测量，
  是配对的两个不独立，所以并排看也救不回来。正确形状是**两次读**：解析一次、回读一次，
  问的才是"命名它的那次读与即将动它的那一刻之间，这东西有没有变"。
  **而假阳性不会有人追问。** 同日三个坏观测量里，A 线的 `rounds` 恒 0、B 线的 `auto_trade` grep
  恒非零，都是**假阴性**——不出声，迟早有人问"为什么一直是 0"；6b 那个是**假阳性**——它一直出声
  说"没问题"，**没有人会去问"为什么一直是 match"**。所以正向计数除了要有一条断言它计数正确的
  测试，还要有一条**变异检验证明它可能失败**（把第二次读换回第一次读，对应用例必须转红）。
  **要设卡，先有测量。** 6b 的第二版把回读结果改成与账本比，全量立刻红一条：分批止盈成交后
  账本记的数量落后于实时数量，那是**合法的陈旧**，按账本卡会拦下一条今天能正常执行的管理指令。
  所以偏差先记成**只观测不设卡**的量（`ledger_drift`），等测出真实分布再决定卡不卡——
  "偏差很罕见"在被测量之前只是个假设，**按未测量的假设设卡，拦下的是合法的东西**。
  **所以正向计数要跟着一条断言它计数正确的测试**（"三次探活恰好三行"，并做变异检验），
  而不是只断言被观测的行为正确。
  **A 线在写下这一条之后当天就自己违反了两次**，一并记着，因为它说明这条不是靠自觉能守住的：
  一是观测脚本按 **logger 名字**猜日志所在的 unit（`deepcoin_reconcile_round` 的 logger 叫
  `telegram_kol_research.web_app`，日志却由 **worker** unit 打），于是 `rounds` 计数整窗恒为 0，
  实测 web unit 0 行 / worker unit 25 行；二是 A-10c 的守卫拿轮次开头的 `synced_at` 当
  "快照时刻"，而它与实际 `list_positions()` 相差 24 秒，导致守卫每轮拒判所有 binding、
  扫描连续 25 轮什么都不判——**而这个失败从外面看和"系统很安静"一模一样**，
  是人翻 journal 才发现的，那不是检测机制。修法是给守卫本身也配一个成对观测量
  （连续 3 轮拒判全部即告警），并且**这个计数必须落库**：进程内计数器一重启，
  一个已退化一小时的守卫就重新显得健康。
- **事故捕获在生产上失败即静默是对的，但它让"告警键越界"变成一个只有日志能看见的永久静默。**
  `runtime_incidents` 的 `_SUMMARY_FIELDS` / `_DIAGNOSIS_FIELDS` 是**封闭词表**，摘要里出现表外的键
  会让整条记录被拒（`RuntimeIncidentBoundsError`），而适配层把拒绝**记日志、不抛异常**——
  于是那条告警从上线起就一次也发不出，而所有测试照绿。**这个坑本仓库已经踩过两次**：
  A-8c 是 `group_trading_mode`（当时改为写进允许的 `impact`），A-10b 是 `pos_id`
  （三条真实"写掉持仓"无人被告知，靠观察窗里 `marked_closed=3` 而 `marked_incidents=0` 才发现）。
  **做法有两条，都要**：(1) 给告警写一条**断言 incident 行真的存在**的用例——只断言"事件行写了"
  或"适配器被调用了"抓不到它，那是在断言自己造的东西而不是自己依赖的东西；
  (2) 测试期把最终那次捕获失败改成**抛出**（`TELEGRAM_KOL_RUNTIME_INCIDENT_STRICT_CAPTURE`，
  `tests/conftest.py` 默认开），让下一次越界在单测里就红。详细→最小的回退不受影响：
  只有最小那次才算最终。
- **一个窗口的证据价值由被改的判据决定，不由窗口的健康度决定。** 30 分钟零重置、全绿、
  `head_ok` 全程 1 的窗口，对一条线可能是充分证据，对另一条线可能一个样本都没有取到，
  **而两者的采样表看起来一模一样**。2026-09-10 同一个市场状态（用户手工平掉最后一个仓位、
  交易所零持仓）对两条线的价值正好相反：A 线 A-10e 改的是"账户真空时扫描永久冻结"，
  **真空正是它唯一的实测条件**；B 线 6a 改的是保护单归属与替换顺序，**没有仓位就没有可归属的对象**，
  同一个窗口只能证明"没有回归"。**做法**：起窗前先写下"这个窗口要取到什么样本才算证明了本步"，
  收窗后照实分开记"证明了没有回归"与"未取得该判据的样本"，**不要拿"窗口达标"当通用结论**——
  A 线 A-10b 的第一个窗口就是这么被误当成生产验证的（三个正向计数全 0，事后才知其中一半是
  守卫退化根本没走到判定）。
  **写下判据之后还要保证观测装置真的记得下它，而正确的做法是取消"抽取"这个动作。**
  2026-09-10 同一个下午撞了两次：6b 起窗一分钟后发现判据要的 `cancel_precheck` / `ledger_drift`
  **根本不被采集**；补上之后，6e 又因为 `would_adopt` 是**补丁之后才加进代码的**再次落空。
  **缺字段的采样表会以"合格"的样子交付一个填不出来的判据栏**，而且它比一个恒为 0 的计数更隐蔽：
  恒为 0 至少还在行里、是个能被看见的异常，字段不存在的采样行**看起来完全正常**。
  两次的区别正是解法的分界：第一次是"想不到要检查"，第二次是**"知道要检查、但检查不在流程里"**——
  而根源是采样器**按名字挑字段**，于是每加一个观测量就欠下一次"记得同步采样器"。
  **做法：观测量不要白名单抽取，原样落盘。** 把整个观测对象泛型地折叠进采样行
  （数值相加、字典逐键相加、列表记长度），新加的字段**自动进证据**，不需要任何人记得。
  退路是（无法原样落盘时）起窗前拿判据逐条去对采样行的字段名——**那是要求人每次多做一步，
  防的是这一次；原样落盘防的是这一类。**
- **默会的正确做法，在流程被明文化的那一刻最脆弱。** 2026-09-10 同一小时里两条线各撞一次，
  成因相反而形状相同：B 线把"推共享分支"和"纯文档提交"在脑子里绑成了一件事——前五次确实都是
  文档，第六次分支尖端夹了未部署代码，推的是尖端；A 线前几次部署一直是"先推自己的分支再 deploy"，
  但**从没把这一步从行为里抽出来写下**，等到照一条写错顺序的规矩执行时，就把自己一直在做对的
  那步丢了（`tg-deploy` 拿不到只存在于本地的 sha）。
  **两次都不是不守规矩，而是"一直在守一条没写下来的规矩"**：它在无人明文化时靠习惯成立，
  在被明文化成另一个样子的那一刻失效。**所以把一条做对的流程写下来时，要写它的每一步，
  尤其是那些"当然要做"的步骤**——正是它们最可能既不在纸上、也不在下一个人的习惯里。
- **一个动作失败时，要停在"保护过度"那一侧，不能停在"没有保护"那一侧。** 判断一次确定性失败
  能不能自动收尾，看的不是"是否确定"，而是**这次确定性失败留下了什么**。2026-09-10 三条线各撞一次、
  方向一致，已经不是巧合：B 线 6a——替换止损时撤旧单失败，**保留新单**而不是把仓位交出去；
  A 线 A-11——补救平仓腿在尝试平仓前撤过保护，平仓被拒时**必须把旧止损还原**，否则"平仓被拒"
  变成"仓位裸着"；A 线 A-11b——`_restore_precancelled_protection_for_rejected_close` 里
  **"什么都没写"反过来是坏消息**，因为那次没写成的写入正是把旧止损放回去，所以确定性拒绝意味着
  **欠保护**，不能自动收尾，只能 `recovery_required`。
  **推论**：同一个异常类型在不同位置该有不同处置。A-11b 那五处里，两处的确定性失败留下的是
  "多挂了几张待回滚的保护单"（可以自动回滚收尾），三处留下的是"该撤的单还在 / 该回来的单没回来"
  （必须留给人）。**按异常类型统一分类是错的形状**，按"留下了什么"分才对。
- **一个不要求任何人动手的错误描述，可以无限期存活。** 2026-09-10 两条线各自发现的错误里，
  几乎每一个都是**因为有人要据此动手**才暴露的：A 线建议 B 线"回头简化那道多余的闸"，
  B 线动手前去看了一眼，发现**那道闸根本不存在**（它把一句"我会确认"读成了"已经加了"）；
  反过来，A 线"A-10b 的告警从上线起一次没触发过"这句错误描述**躺了整整一版**没人发现，
  正因为它**不要求任何人做任何事**。
  **所以防线不是"要警觉"，而是一条可执行的：任何需要动手的建议，动手前先看一眼对象是否如描述。**
  推论也要记住：**不推动动作的描述得不到这道防线的保护**——状态文件、报告、证据里那些"顺带一提"
  的结论，正是最可能长期错着而无人察觉的部分。
- **不要覆盖一个正在运行的 `.sh`。** bash 是**边执行边读脚本文件**的，覆盖之后运行中的实例
  会从错误的字节偏移继续读，而它**多半不会崩**——它会继续跑、继续写输出，看起来完全正常。
  这与"静态 `ast` 测试读到编辑中间态"是同一形状（读到的不是你以为的那份内容），但**方向相反**：
  那个是假失败（红，有人会去查），这个是**假成功**，而假绿没人复查。
  做法：改观察脚本时写**新文件名**，让在跑的窗口用旧脚本跑完（2026-09-10 6e 的采样器改造即如此处理）。
- **成对的正反例只能证明"这一例会红"，不能证明"它因为你以为的那个原因而红"。**
  2026-09-10 6f-1：新守卫里有一层有限性检查，测试里两例（`inf`、`nan`）的注释白纸黑字写着
  "这两例是为它设的"。把那层检查删掉做变异——**0 failed**。原因是这两例本来就被上一层
  （Decimal 比较）挡住了：`inf` 与任何数字都不等，`nan` 与一切都不等，包括它自己。
  真正只有那层检查能挡的，是 `inf` vs `Infinity` 这种**同值不同拼写**；补上后变异才 2 failed。
  这与"单点变异在层层守卫下会撒谎"是同一条根，但**暴露面不同**：那条讲的是**代码**有几层，
  这条讲的是**测试注释宣称的覆盖关系可能是假的**——而注释不会被执行，所以它可以一直假下去。
  **做法：要证明"某一例覆盖某一分支"，唯一的手段是单独删掉那一分支看它红不红；
  读注释、读代码、"这一例明显是为它写的"，都不算证据。**
  更难受的地方在于**结果一直是对的**：那两例确实拒绝了，只是拒绝它们的不是我以为的那层。
  **"结论对、理解错"是最不容易被发现的一类，因为正确的结论没人复查。**
- **一个时序失败可以伪装成一个配置失败，然后把下一个人送去查一个没有问题的地方。**
  2026-09-10，A 线用 `/proc/<worker MainPID>/environ` 取交易所凭据时**恰好撞上 B 线部署**：
  取 MainPID 与读 environ 之间那个进程被重启换掉了，而报出来的错是
  `missing Deepcoin credentials`（`deepcoin_client.py:330`）。**下一个人会去查环境变量，
  而环境变量完全正常。** 这与硬性禁止第 4 条同根——"读不到"被说成了别的东西——
  只是第 4 条防的是把读失败当成"零"，这里是把读失败当成"缺配置"。
  **做法：凡是从 `/proc/<pid>/` 取值的脚本，取 pid 与读文件之间存在窗口，部署会正好落在窗口里；
  读失败必须自报"读失败 + 当时的 pid"，不能让它掉进下游任何一个"值缺失"的分支。**
  （附：该失败到底经由哪一步变成这句错误，B 线未逐步验证，只核实了这句错误确实来自
  `deepcoin_client.py:330`；此处记录的是现象与防线，不是完整机制。）
- 迁移只改变"在哪里跑、怎么组织"，从不改变"决定什么"。任何看起来需要改交易语义的改动
  都是读错了需求，停下来问。
