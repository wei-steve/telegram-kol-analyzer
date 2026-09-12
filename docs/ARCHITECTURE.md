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

**推论：仓位行上的 `slTriggerPx` 只是最近一次写入，不是"这个仓位的止损"。**
2026-09-10 6f 首笔实测——给仓位 `1001125216121996` 挂上备份止损 `75548.6` 之后，
**仓位行的 `slTriggerPx` 就变成了 `75548.6`**，而主止损单 `1001125216121995`（`75700`）
仍好好挂在 `trigger-orders-pending` 里。叠加语义下**仓位行显示最后写的那张，挂单表才是全集**，
而先触及的是 `75700` 那张——也就是说仓位行显示的**恰恰不是**会先生效的那张。
**所以任何"这个仓位的主止损是多少"的判断，只能读 `trigger-orders-pending` 全集、
按 `TU`/`ordId` 取，绝不能读仓位行。** 这条比读错字段名危险：读错字段名会拿到 `None` 而暴露，
读仓位行会拿到一个**存在的、格式正确的、含义错误的**价格。
**2026-09-11 的加强实例：同一个字段在同一天里出现了第三种含义——空。**
A 线给 `1001125216121996` 挂上止盈之后，该仓位行读到的是：

```
posId 1001125216121996   slTriggerPx = ""(空)      tpTriggerPx = 81900
posId 1001125216153672   slTriggerPx = 75548.6     tpTriggerPx = ""(空)
交易所 trigger-orders-pending 同时刻：两仓各有主止损 75700 与备份止损 75548.6，
                                    …121996 另有止盈 79800×7 与 81900×8
```

**`…121996` 的仓位行说它没有止损，而交易所上它有两张。** 原因仍是同一条：
仓位行只反映**最近一次 TPSL 写入**，而最近一次写的是止盈，止损位留空。
所以这个字段在一天之内先后表示过**主止损价、备份止损价、以及"空"**，
**三次都不是"这个仓位的止损是多少"的答案**。
**空尤其危险**：前两种至少给出一个数字，会让人去核对；
空会被读成"没有保护"，而**"没有保护"通常触发的是补挂动作**。
**但此刻不会有路径因这个空去补挂**（A-13 扫描结论，指挥会话 2026-09-11 转达）：
仓位行 `slTriggerPx` / `tpTriggerPx` 的读点只有 `build_position_evidence`，
**只进入入场归属的经济学比对，不作保护判据**；"这个仓位有没有保护"一律走
`protection_snapshot` / `protection_health` 读 `trigger-orders-pending`。
（`build_position_evidence` 那一半我在 2026-09-10 自己查过一次，结论相同。）
**记这条交叉引用而不是再扫一遍**——但要注意它保证的是**此刻**：
它是一份扫描结果，不是一条会在有人新写一处读点时转红的判据。

**第四种含义，2026-09-12，而且它推翻了上面"空=只有一张单"的直觉。**
6j 给 ETH 两仓挂上止盈之后，次日只读巡检读到：

```
posId 1001125231241310   slTriggerPx = ""(空)   tpTriggerPx = 2790
posId 1001125231241107   slTriggerPx = ""(空)   tpTriggerPx = 2790
交易所 trigger-orders-pending 同时刻：六张——主止损 2484 ×2、
                                    备份止损 2479.03 ×2、止盈 2790×0.9 ×2
```

**两个仓位行都说"没有止损"，而交易所上每个仓位各有两张止损。**
与 2026-09-11 那次的区别在于：那次是**一个仓位**空、另一个仓位显示备份价，
读起来还像"某种不一致"；这次是**两个仓位同时空、而保护比任何时候都齐全**。
**所以这个字段的四种含义是：主止损价、备份止损价、空（保护不全）、空（保护齐全）。**
最后两种**在字段上完全无法区分**——
`slTriggerPx == ""` 既可能是"这个仓位真的没有止损"，也可能是
"它有两张止损，只是最近一次写的是止盈"。
**一个字段能同时表示一件事和它的反面时，它就不是这件事的答案，一次也不是。**

**更正一处我自己说错的话。** 我曾据此告诉两条线"break-even 读的正是仓位行的这个字段"。
**查过了，不对**：`break_even_convergence_executor` 从不读仓位行的 `slTriggerPx`，
它读的是 `trigger-orders-pending`——**读对了表，用错了词汇**。两处（约 650 行的止损、
约 795 行的止盈）都对 TPSL 挂单行取 `posId` 与 `slTriggerPx` / `tpTriggerPx`，
而 TPSL 行**根本不带 `posId`**、价格字段叫 `slTriggerPrice` / `tpTriggerPrice`，
所以两个条件各自都必然不成立，`break_even_existing_stop_drift` 每次必抛。
**这条更正对 6h 的判据是实质性的**：把判据写成"必须读挂单表而不是仓位行"**不会改变任何事**，
因为它已经在读挂单表了。真判据是**词汇与归属**——TPSL 行没有 `posId`，
归属只能靠 ordId 或 `TU`，价格只能按 TPSL 行自己的键名读。
**而我那句错话的来历，正是本节反复讲的那个形状**：仓位行翻成备份价是我实测的（真）、
break-even 有坏读取器也是真的（A-13），我把两个真事实接成了一个从未验证的因果。

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
  **同一个坑在写下这条的第二天又踩了一次，所以补上它真正的成因：否定式断言。**
  2026-09-11 `test_a_released_position_is_not_held` 只断言
  `plan.status != "shadow_ready_adopted_primary"`；夹具把入场腿的 `pos_id` 写死成
  `"pos-1"`，而用例问的是另一个 id，于是 `_plan_submission` 在第一步就返回
  `blocked / binding_or_leg_unavailable`——**永远满足那条不等式**。
  把释放闸门硬接成常闭（`if True:`）**全绿**，而这条用例存在的唯一理由就是挡住这个变异。
  **关键不在"夹具写错了"，在于 `!=` 这种断言天生分辨不了"没走到"与"走到了并放行"。**
  **做法**：凡是用来证明某道门放行的用例，断言必须是**肯定式**的（`status == "ready"`
  且带上它本该产出的字段），并在**同一条用例里**用同一份夹具把门关上再跑一次，
  断言另一个结果。**一个仓位、一处差异、两种结局**——否则读者无从知道那处差异是不是原因。

- **每个用例都自己把依赖传进去，于是它们一起证明了"这个判定对"，却一起不管"有没有人接上它"。**
  2026-09-12 6k：一个新判定要 `group_trading_mode_provider`，它从 web_app 穿六层才到闸门。
  判定本身有 32 条用例、变异 6 项全红；两个执行器的用例也各自覆盖了放行与扣住。
  **但变异"web_app 不再传这个 provider"与"某一层不再接受这个参数"——两项都是哑的。**
  原因很简单：**每条用例都在自己的调用里把 provider 传了进去**，
  所以它们检验的是"拿到 provider 之后判定对不对"，而**没有一条在问"生产里它拿得到吗"**。
  缺 provider 的方向是**扣住**（安全），**所以这个静默失效不会以任何故障的形式出现**——
  它只会让整个阶段悄悄什么都不做，正是同一天上午 6i 那个"永远不触发"的形状。
  **做法**：凡是依赖注入且**缺失方向是安全的**参数，除了各层的行为用例之外，
  必须另有一条**链路用例**：逐个 `inspect.signature` 断言每一层都接受它，
  并对最上游的源码断言它**真的在传**。
  **"安全方向"是这条规则的触发条件而不是豁免理由**——正因为失败不出声，才必须专门去听。

  **同日，A 线用它的镜像把这条推广了一步，推广后的版本才是要记的那条。**
  A-16b 的去重检查写的是 `except Exception: logger.warning(...); return False`——
  **失败即"不是重复"，照旧开仓**。它的缺失方向是**放行**，不是扣住；
  写下这个默认的理由（"一个坏了的去重检查不该让入场停摆"）本身站得住。
  **但两者的静默程度完全相同**：扣住的静默让整个阶段什么都不做，
  放行的静默让守卫等于不存在，**都不会以故障的形式出现**。
  **所以触发条件不是"安全方向"，是"失败时走默认路径"**：
  > **凡是"失败时走默认路径"的守卫，那次失败本身必须产生一条能到人的记录**——
  > 不管默认路径是扣住还是放行。

  **规则用上面这句，但两个子类要分开留着，因为它们的难发现程度不同**
  （这一点是 A 线提醒的，我原本把窄的那条整个替换掉了，那会丢掉信息）：
  - **扣住方向更难发现**：整个阶段什么都不做，**连"该发生的没发生"都没人会去问**——
    6i 在生产跑了九十分钟零收割，是有人专门去核对那条腿才暴露的；
  - **放行方向次之**：守卫等于不存在，但它至少有一个可观测的后果
    （A-16b 的去重失效会让**重复入场真的发生**，那是会被看见的）。
  **两者都不以故障的形式出现，但前者连"现象"都没有。**
  只在 journal 里留一行 warning 不够：**窗内能数，窗一收就没人看了**
  （A 线据此立了 A-16b-1，把那两条 warning 升成 `ALWAYS_NOTIFIED` 事故类型，
  并要求用例**同时**断言"抛异常时产生事故"与"入场仍然放行"——
  两件事各自都可能被后来的人改掉一个）。

- **判据里不能放"分母在系统之外"的量——它与"近似时钟的量"是同一个病的两端：一个恒真，一个恒不可达。**
  2026-09-12 A 线：A-16b 的窗要 `msgs >= 5`，跑到 34 分钟仍是 `msgs=0`，
  其余全绿。**卡点不是时间，是消息到达率——那是等不来的东西。**
  A 线自己复盘出两重错，第二重是通用的：
  > **一个我无法影响的量放进完成条件，窗口就不再是我在观察系统，而是系统在等群里有人说话。**
  第一重是代理选错了：它要证的是"合法入场没被误判成重复"，
  **而消息不是入场**——群里绝大多数消息是闲聊，`msgs=5` 达到也不意味着有任何入场经过那条检查。
  **补上我这侧的第三种形态**（同日 6i）：`max_pages=5` 是
  **"分母在系统之内、却被一个没人想过的默认值改写成不可达"**——
  判据本身可达，是实现把它设成了不可达。
  **三者的共同点**：判据文本读起来都合理，**而它与现实的关系已经断了，且断得静默**。
  **做法**：写下一条完成条件时，先问它的**分母归谁管**——
  归系统管（可以要求）、归外界管（只能记录）、还是**名义上归系统管而实际被某个默认值接管了**。
  归外界的那类降为记录项，把它本来要证的性质移进待观测清单，**照实记"本窗无样本"**；
  卡住时**照实记"判据未达成"，不要悄悄放宽**。

- **只要求最后一次读到，挡不住中间读不到过。**
  2026-09-12 6k 起窗四分钟后 A 线指出：我的完成条件要求**末样本** `gates_ok == 1`，
  这挡住了"最后一次读不到"，**挡不住"中间读不到过、最后一次恰好读到了"**——
  而闸门在窗中间被换过又换回来，与从未变过，**在末样本上无法区分**。
  A 线那句点在要害上：**6j 那 77 采样之所以是证据，恰恰因为每一轮都读到了；
  那条性质该被判据要求，而不是碰巧成立。**
  改法是加 `gates_unreadable == 0`，并且**当场重启窗口而不是留到收窗时手工核对**——
  本节已经写过"一条只在人记得时才执行的判据等于没有判据"。
  **更一般地**：一个跨越整段窗口的性质，判据必须落在**每一个样本**上；
  只看首尾的判据回答的是另一个更弱的问题。

- **"替身比真身窄"有三种形态，第三种最难看见，因为每条用例单独看都是对的。**
  三者同在 2026-09-12：
  1. **测试 stub 比真身窄**——我的 `find_trigger_order_history_rows` stub 少了 `max_pages`，
     于是页预算这一维在测试里**不存在**，全绿放行了一个永远不会触发的实现；
  2. **生产代码里的假对象比真身窄**——A 线的 `_Binding` 只带四个字段，
     函数今天只读这四个所以能跑，**哪天多读第五个就在生产里 `AttributeError`**，
     而单测传的是真行、照样全绿；
  3. **每条用例都自带依赖**——6k 的 provider，每条用例都在自己的调用里传进去，
     于是"**依赖从哪来**"这一整维在测试里不存在。
  **三种都让缺陷在全绿下通过。** 第三种最难看见，因为前两种至少有一个"窄"的对象可以被指出来，
  而第三种里**每一条用例单独看都是正确且完整的**——缺的是它们之间没人负责的那一段。

- **`journalctl --utc` 只改显示，不改 `--since` 的解释；一个更宽的窗口不会让你查不到，只会让你数错。**
  2026-09-12：我一整轮的远程日志查询都写成
  `journalctl --since '2026-09-12 18:14:00' --utc`，以为边界是 UTC；
  服务器是 **CST (UTC+8)**，`--since` 按本地时区理解，**实际边界是 10:14Z，窗口宽了八小时**。
  本仓库早就写下过"`journalctl --since` 用 UTC/epoch"，观察器脚本里也一直是 epoch 锚定的——
  **是手工查的时候图省事没照做。**
  **它一直没被发现，正因为错的方向是"更宽"**：要找的东西仍在结果里，只是混进了八小时不该有的数据。
  代价是所有**计数**都偏大：我据此报过"reconcile 约 7 轮/分"，按真实 UTC 小时重数是
  **45 轮/小时 ≈ 0.75 轮/分——高估约 9 倍**，而我当时正拿这个数字估算一次部署的调用成本。
  **做法**：`--since "@$(date -u -d '<UTC 时刻> UTC' +%s)"`，`--until` 同理。
  更一般地：**一个查询出错的方向若是"更宽"，它不会以查不到的形式暴露，
  只会以一个看起来合理的、偏大的数字存在下去**——所以"查到了"从来不是查法正确的证据。

- **被调方的默认值，调用方没想过——于是一条完全正确的规则可以永远不成立。**
  2026-09-11 6i：判定函数调 `find_trigger_order_history_rows` 时没传 `max_pages`，
  继承了为别的用途定的默认 **5 页 = 500 行**；而这个账户的条件单历史是 **13 页 1176 行**。
  搜索每轮用尽预算、每轮如实返回"没找完"、模块每轮如设计地扣住。
  **代码对、判据对、测试全绿、部署成功，而它结构上永远不会触发。**
  **做法**：凡是把"读不到就不动"作为安全方向的判定，**必须显式写出它给读取方的预算/超时/范围**，
  并**用一次真实数据量校准**；继承来的默认值在这类判定里总是偏向"永远不动"，
  而那一侧不会报错、不会变红、也不会有人抱怨。
  **取值不要钉在测量上**：钉成 13 等于下一次交易之后再次静默失效；取 40 留余量，
  并让预算用尽这件事**自己出现在日志里**。

- **替身比真身窄，它藏起来的正是被它省掉的那一维。**
  同一天，同一处：我的测试 stub 写成 `find_trigger_order_history_rows(*, inst_id, order_id)`，
  **没有 `max_pages`**。于是整套用例**根本看不见页预算这一维**，
  32 条测试全绿地放行了一个永远不会触发的实现。
  把 stub 改成与真身同签名（**含默认值 5**）之后，"预算回到 5"这条变异立刻转红。
  **做法**：模拟一个真实接口时，签名要逐字对齐**包括默认值**；
  少一个参数不会报错，只会让那一维从此不存在。
  与本节"夹具是被谁写的，它就替谁背书"同源，但更隐蔽：
  那条是夹具**说了错话**，这条是夹具**没说话**。

- **扣住了却什么也不说，和根本没在跑，产生的证据完全相同——都是没有证据。**
  6i 上线后在生产跑了九十分钟，每轮都扣住、每轮都有理由，
  而事故表 0 条、日志 0 行、计数器没有。
  **我能发现它，只因为指挥会话让我去核对腿 582 收没收掉；没人问的话，它会一直读起来像做完了。**
  **做法**：任何"默认不动"的守卫，**扣住时必须每轮留一行**（照 A-15-1 止盈扣住那条的做法：
  审计行去重、日志行不去重），且这行要带**原因**与**能否继续找下去**，
  而不只是"held"。与本节"一个放开的闸门对谁都不宣告"配对：
  那条说**开着**要自报，这条说**关着**也要自报。

- **日志里的键名不一定等于它在数据库里的 action 名，按后者去 grep 日志会得到一个干净的零。**
  2026-09-11：查 `protection_authority_shadow` 还在不在跑，我按 action 名
  grep 生产 journal，**得到 0**，差一点写成"它停了"。实际每轮都在打，
  但日志行里的键叫 **`protection_shadow`**——少了中间那个词。
  **这个零之所以危险，是因为它和"真的停了"长得一模一样**，而且"按它在数据库里的名字
  去日志里找"这件事看起来天经地义。
  **做法**：拿一个名字去另一个介质里查"有没有"，**先用一个已知一定存在的样本验证这个查法
  本身能命中**（我后来是先 grep `deepcoin_reconcile_round` 拿到整行、再看里面有哪些键，
  才发现名字不同）。查法没被验证过时，零只能记成"没查到"，不能记成"没有"。
  与本节"一张去重过的表答不出'多久一次'"配对使用：那条说明**表**不能回答频率，
  这条说明**日志**能回答，但前提是你按对的键去问。

- **一个函数能把状态写进去、却没有能力把它写回来，这种状态会永久滞留。**
  2026-09-11 6i：`_refresh_exact_entry_leg_states` 在"单子还在交易所"时把入场腿写成
  `pending`；而它处理"单子消失了"的那一支写的是
  `if str(leg.status or "").lower() in {"open", "submitted"}:`——
  **`pending` 不在里面**。于是同一个函数：进得去、出不来。
  腿 582 就这样从 2026-09-04 停到 2026-09-11，连带 binding 永远 `open`
  （`_derive_binding_from_entry_legs` 只在全部入场腿终态时才归档）。
  **它不是漏了一个分支，是漏了自己写的那个值**——写入口和读出口列的是两份不同的状态表，
  而两处相隔两行。
  **做法**：凡是某个函数自己会写入的状态值，它的"反向"分支必须显式覆盖同一组值；
  判据不是读代码，是**把写入的那一支和判断的那一支并排列出来做集合差**，差集非空就是滞留。
  同源的还有：只认自己记录的事件来收终态（`_apply_recorded_terminal_entry_events`
  只扫我们自己发的撤单事件），于是"交易所那边没了、但不是我们撤的"这一类
  **没有任何路径负责**。

- **被测的量如果是一个差，那么让两边同时偏移的缺陷，变异检验看不见。**
  2026-09-11 6i：把"朴素时间戳按 UTC 解释"改成"按主机本地时区解释"，
  **7 条变异里唯独这条全绿**。原因不是断言太弱——是我那条用例给的 `now` 和
  `created_at` **都是朴素的**，两边一起偏移 7 小时，`now - created_at` 分毫未变。
  **用例把自己的主题消掉了。** 真正危险且生产上真实存在的是**混合**那一对：
  `recovered_at` 是 `datetime.now(UTC)`（aware），`leg.created_at` 从 SQLite 回来是朴素的；
  生产主机 UTC+8，按本地解释会让一笔 20 小时的单子量成 28 小时，**提前八小时被收掉**。
  改成 aware/naive 混合、并用 `monkeypatch.setenv("TZ", ...)` + `time.tzset()`
  强制一个非 UTC 时区之后，同一变异立刻转红。
  **做法**：断言一个差值时，先问"什么样的错误会让两边同向移动"——
  那类错误必须用**不对称的输入**去逼，否则用例只是在验算减法。
  与本节"夹具照着实现写"同源，但成因不同：那条是两边共享同一个错误来源，
  这条是**被测量本身对该错误免疫**。

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
  **2026-09-11 又踩了一次，而且这一条当时已经写在这里了。** A 线等 B 线 6g 收窗，用的正是
  `until ! pgrep -f "step6g_observe"`，空转 **4 小时 57 分**；kill 掉之后为了确认又跑了一次
  `pgrep -f step6g_observe`，**又匹配到这条 ssh 命令自己**。而同一批观察脚本收窗时本来就会往
  日志里写 `WINDOW_MET`——**标记文件一直在，只是没用**。
  **所以这条的问题不在于没写下来，在于它写成了一段散文，而散文在动手的那一刻不拦人。**
  真正拦得住的是把 `until grep -q WINDOW_MET <log>` 变成默认写法（A 线首笔止盈那次用的就是它，
  一次就对）。**两种失败方向的代价不对称，也要记住**：锚太死的 `ps` 是**假阴性**——它让人去部署；
  `pgrep -f` 是**假阳性**——它只让人空等。**同一天两种都占过，只有前者差点上进别人的窗口。**

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
  **而"成对"里必须有两个都能失败的量。** 2026-09-10 A 线自查发现：观察窗里 `rounds`
  （每轮 reconcile）与 `elapsed` 是**恒等式**——轮次由固定 60 秒定时器触发，所以
  `rounds == elapsed_minutes` 永远成立、**不可能失败、因此不携带信息**；它只能说明定时器没死，
  而 `worker_http=200` 已经说了。**而 A-10d 起的多条窗口记录把"`rounds` 在涨"当成了成对的另一半**
  （"`rounds` 从 0 涨到 28 而 `guard_skips` 全程 0"）。**成对里有一半是恒等式，就等于没有成对。**
  这与上一段"两例其实被上一层挡住"同源：**以为在读两个独立的量，其实一个是另一个的时间坐标。**
  **做法：分母要选一个能失败的量**（例如"本轮实际检查过的 binding 数"），不要选墙上的钟。
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
- **夹具是被谁写的，它就替谁背书。** 2026-09-10 6h：`break_even_convergence_executor` 的前置校验
  对 `trigger-orders-pending` 的 TPSL 行取 `posId` 与 `slTriggerPx`，而那个端点**不返回 `posId`**、
  价格键叫 `slTriggerPrice`——这条缺陷让自动保本从上线起一次没成功过。
  修好之后做变异检验，把 `posId` 相等条件**原样加回去**，**31 个测试全绿**。
  原因是那些测试的 TPSL 夹具**自己编了一个 `posId`**、也用了仓位行的价格拼法：
  **夹具和代码错在同一处，所以坏读取器在测试里一直是"对"的**。
  把四条夹具行改成交易所真实形状后，同一个变异从 0 红变 6 红。
  **所以"全量绿"只在夹具与真实响应一致时才是证据。** 一个照着实现写出来的夹具，
  测的是"实现和它自己一致"，而那永远成立。
  **做法**：凡是模拟交易所响应的夹具，字段必须来自**一次真实响应的原样记录**，
  不能来自"代码读了哪几个键"；并且**每条缺陷修复都要把缺陷放回去跑一次**——
  变异检验问的不是"测试过不过"，而是"把缺陷放回去，测试还过不过"，
  只有后者能发现夹具在替缺陷背书。
  这与第 4.7 节"在挂入场单自带的止损"是同一根：**我们对交易所形状的理解一旦写错，
  会同时写进代码和夹具，于是两边互相确认。**

- **一张会去重、会有条件写入的表，回答不了"多久一次"。**
  2026-09-10 我两次告诉两条线"reconcile 约 49 分钟一轮"，并据此解释了一个现象。
  那个数字是我从两条 `backup_stop_shadow_ready` 事故行的时间戳（19:18:56 → 20:08:02）推的，
  **而 `position_protection_incidents` 按 fingerprint 去重**——连续多轮内容相同就不新增行。
  所以那 49 分钟是**"内容保持不变的时长"**，不是"两轮之间的间隔"。真实周期是 **60 秒**
  （journal 连续 12 轮 `started_at`，`trigger: by_timer`）。
  差了 49 倍，而且这个错数字已经被别人拿去排期了。
  **判据：任何"多久一次 / 多少轮 / 上次是什么时候"的结论，只能取自调度侧的直接观测**
  （journal 的 `started_at`、`trigger`），**不能从任何带去重、带条件写入、带状态机的表反推**——
  那种表的行间距是"状态变化的间距"，两者只在"每轮都变"时才相等，而那恰恰是最不常见的情况。
  **这是同一形状在一天内的第四次**：读代码推执行器行为、看签名推 payload 带哪些键、
  把"我会加"读成"已经加了"、读去重表推调度周期。**四次的共同点不是粗心**——
  代码是真的、签名是真的、那句话是真的、事故行也是真的，
  **错的始终是"它能回答我正在问的这个问题"**。
  所以自查的问法不是"我看到的东西对不对"，而是**"我看到的这个东西，回答的是哪个问题"**。

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
- **用"我们发出的请求"回填出来的假响应，会让任何"把请求形状的键读在响应上"的代码通过测试。**
  假客户端的响应必须按**真实响应**造，否则它验证的只是"我们自己的字段名和我们自己的字段名一致"。
  2026-09-10 的实例（A-13）：`tests/test_break_even_convergence_executor.py` 的假客户端在
  `set_position_sltp` 里把请求原样回填成挂单行——于是假的 `trigger-orders-pending` 行带着
  `posId` 与 `slTriggerPx`，而**真实端点两个都没有**（前者不存在，后者叫 `slTriggerPrice`）。
  生产代码读的正是这两个键，**测试全绿、生产恒拒**：自动保本收敛从首次部署到 2026-08-03
  一次都没成功过，全表两行皆 `blocked`。
  **注意它不是"漏了断言"**——夹具与生产读的是**同一份错误词表**，所以再加多少断言也照样绿。
  **做法**：夹具里的响应行**逐字来自一次真实响应**（原样粘贴、注明抓取日期），而不是由请求推导；
  A-14 的 `tests/test_deepcoin_trigger_rows.py` 就是这么写的。
  与本节"成对的两个观测量必须来自独立的读"同源：**夹具与被测代码若共享同一个错误来源，它们的一致
  不构成证据。**
- **守卫与被守卫的动作写在同一口气里，而没有任何东西让后者依赖前者，等于没有守卫。**
  2026-09-11 一天之内三次，形状相同、一次比一次隐蔽：
  1. 跑了"共享分支有没有多余代码文件"的检查，**把清单打印出来、加了一句"这是故意还没部署的"，就过去了**；
  2. 为此写的判定式检查**自己是坏的**（`grep -qv` 在本环境恒返回"无违规"），差点变成一个稳定说 PASS 的检查；
  3. 写观察脚本模板前跑了 `test -e /root/observe_template.sh && echo EXISTS || echo "safe"`，
     **它打印了 `EXISTS`，而下一行的 `cat > ...` 是无条件执行的**。
     （这次没造成损失：文件是我几分钟前自己建的、无进程在跑、内容一致——**但那是运气，不是守卫**。）
  **三次的共同点**：守卫产出了一个正确的答案，**而动作不读它**。
  从命令历史上看三次都"做了检查"，这正是它比"忘了检查"更难防的原因。
  **做法**：守卫必须**终止**被守卫的动作，而不是打印给人看——
  ```bash
  if ps -eo pid,args | grep -q "[/]root/x.sh"; then
      echo "REFUSING: x.sh is running"; exit 1
  fi
  cat > /root/x.sh <<'EOF'
  ```
  **并且守卫要守正确的性质**：覆盖脚本的危险来自"**它正在被执行**"而不是"**它存在**"
  （bash 边执行边读文件）。`test -e` 守的是存在，是另一个问题的答案。
  **同一天的第四次，而且发生在写下上面这几行之后一小时**：我照这条改写了守卫——
  `if ps -eo pid,args | grep -q '[/]root/observe_template.sh'; then echo REFUSING; exit 1; fi`——
  **它当场误报并终止了一个安全的动作**。枚举后发现唯一匹配的是**检查命令自己的
  `bash -c` 命令行**（那行文本里就含着脚本名）。
  **`[/]` 括号只能防 grep 匹配自己的 grep 进程，防不了外层 shell 的 argv。**
  **所以这一条与上面那条 `pgrep -f` 是同一件事，而我隔了一小时又犯了一次**：
  守卫的**机制**做对了（真的 `exit 1` 了），**谓词**用错了（文本匹配回答身份问题）。
  改用 pidfile + `kill -0` 后通过。
  **失败方向如上一条所述是假阳性——只浪费时间，不动生产**，这次也确实如此；
  但它说明：**把规则写进文档，和在下一次动手时用上它，是两件事。**
  能替代记忆的只有把判据做成机制——**这里的机制就是脚本自己写 pidfile。**

- **判断"有没有东西在跑"：`pgrep -f` 假阳性、`ps | grep <锚定模式>` 假阴性，两个都不可靠。**
  2026-09-10/11 两条线各撞一次，方向相反：
  * **假阴性**——A 线用自写的锚定完整命令行的 `ps` 模式数 B 线的观察器，得到 **0**，
    差点按"对方已收窗"去部署；实际它在跑，模式要求 `bash /root/` 而进程是 `/bin/bash /root/`；
  * **假阳性**——A 线挂 `until ! pgrep -f "step6g_observe"; do sleep 30; done` 等 B 线收窗，
    **跑了 4 小时 57 分不退出**：`pgrep -f` 匹配到了**这条循环自己的命令行**
    （那行文本里就含着要找的字符串）。kill 之后再查一次确认，**又匹配到那条 ssh 的 shell**——
    同一个坑一分钟内踩两次。
  **两个方向的危险程度不同**：**假阴性让人去动生产，假阳性只让人空等。**
  所以"宁可锚得松一点"是错的直觉——松会假阳性（浪费），紧会假阴性（危险），
  **而问题不在松紧，在于用文本匹配回答一个关于身份的问题**。
  **可靠的只有两种**：脚本自己写 pidfile、用 **精确 pid** `kill -0` 判活；
  或**全量枚举 `ps -eo pid,args` 之后由人看一眼**。
  推论与本节"它回答的是哪个问题"同源：`pgrep -f <模式>` 回答的是
  "**有没有进程的命令行含这段文本**"，而提问者要问的是"**那个特定的进程还在不在**"——
  这两个问题在提问者自己也含着那段文本时，答案必然不同。

- **检查命令必须自己给出判定，不能只给出供人判断的材料。**
  2026-09-11：我把未部署的代码推上了共享分支——**今天第二次**。第一次的诊断是"忘了跑检查"，
  并据此写下了"一条只在人记得时才执行的判据，等于没有判据"。
  **第二次我跑了检查**：`git diff <prod> <shared> --name-only`，
  **然后把输出打印出来、自己加了一句"这是故意还没部署的"，就过去了。**
  漏掉的是 `| grep -vE '^docs/|\.md$'` ——**把清单变成判定的那一步**。
  **两次的区别要紧**：第一次是没跑；第二次是**跑了、读了、然后用一句叙述替代了判定**。
  后者更难防，因为从命令历史上看"检查做过了"。
  **做法**：凡是本仓库里形如"必须满足 X 才能做 Y"的检查，其命令的输出必须是
  `PASS` / `FAIL` 本身，而不是一份需要人再判一次的清单：
  ```bash
  OFFENDERS=$(git diff "$PROD" "$SHARED" --name-only | sed -E '/^docs\//d; /\.md$/d')
  [ -n "$OFFENDERS" ] && echo "FAIL: $OFFENDERS" || echo "PASS"
  ```
  **而这条判据的第一版本身是错的，值得连同错误一起记**：我最初写的是
  `grep -qvE '^docs/|\.md$'`。本环境的 `grep` 是包装 `ugrep` 的 shell 函数，
  **它的 `-q` 与 `-v` 合用时报告的是"有没有行匹配 pattern"，而不是"有没有行被选中"**——
  而 diff 里几乎总有一个 `docs/` 文件，**于是那个写法几乎永远返回"没有违规"**。
  **一个稳定说 PASS 的检查，比那个它要取代的、被忘记执行的检查更糟。**
  发现它的唯一原因是**它的结论与我已知的事实矛盾**（被测分支明明带着未部署的代码），
  而我没有放过那个矛盾。
  **推论**：把一条检查写成判定式还不够，**判定式检查本身要用一正一反两个输入验过**——
  一个必须说 FAIL，一个必须说 PASS。否则"它给了我一个答案"会被当成"它算对了"。
  **同一条判据的两种写法，一种要人看着清单自己判，一种直接说 PASS 还是 FAIL。**
  前者依赖的是当时的注意力，而注意力在一天的末尾最不可靠——
  这与上一条"能红的用例不依赖记忆"是同一件事在**人工检查**上的形态。

- **一条能红的用例不依赖记忆；一条判据依赖。** 2026-09-11 6g：我写影子时直接复用了
  `evaluate_cancel_precheck`，而在那个位置构造它需要的 `ProtectionAuthority`，只能用**刚读回来
  的同一份 pending 列表**——于是比较的两边来自同一次读，**只可能答"一致"**。
  **这正是 6b 已经犯过、并且已经写进状态文件的那个重言计数器。** 我写下过那条判据，
  今天还是又写了一遍。
  **发现它的不是我想起那条判据，是我写了一条"改了尺寸应判为不一致"的用例，它红了。**
  **所以"成对观测量必须来自独立的读"这条判据，真正的落地形式不是记住它，
  而是为它写一条会红的用例**——判据活在人的记忆里，用例活在 CI 里，
  而本节已经写过：**一条只在人记得时才执行的判据，等于没有判据。**
  推论：凡是本节里形如"必须 X"的条目，都应当问一句**"哪条用例会在不 X 时转红"**；
  答不出来的，那一条目前只是一句提醒。
- **判据与现实脱钩有三种形态，三种的文本读起来都合理，而脱钩都是静默的。**
  | 形态 | 后果 | 实例（均为 2026-09-11/12） |
  |---|---|---|
  | **近似时钟的量** | 恒真，看起来像独立测量 | `rounds` 配 `elapsed_minutes`：不是 1:1，是约 0.76–0.87，**"差不多相等"比"精确相等"更像证据** |
  | **分母在系统之外** | **恒不可达**，窗口从"观察系统"变成"等群里有人说话" | A-16b 的 `msgs >= 5`：入场比消息稀得多，34 分钟 `msgs=0`，而我真正要的是"一条非重复入场顺利开了" |
  | **分母在系统之内，却被一个没人想过的默认值改写** | 判据本可达，**实现把它设成了不可达** | 6i 的 `find_trigger_order_history_rows(max_pages=5)`：账户历史 13 页 1176 行，每轮用尽预算、每轮如实扣住，**跑了九十分钟零收割** |
  **第二种还有一个特别的坏处**：它把"尚未被证明"变成了"永远无法收窗"，而这两件事在采样行上看不出区别——
  **一个一直没达标的窗，和一个永远不会达标的窗，长得一模一样。**
  **做法**：完成条件里只放**系统自己能决定**的量；凡是取决于外部到达率的（消息数、真实样本），
  降为**记录项**并把它要证明的那件事移入**乙类待观测**，照实记"本窗无样本"。
  **反面做法（不要）**：卡住时悄悄放宽门槛——那是"先量后立判据"，与把判据留到收窗时再手工检查同族。
- **一个判据只要有"读不到/无样本"这一档，就必须同时决定那一档对"收窗"意味着什么；否则
  "我们一次也没能查上"会静默地算通过。**
  2026-09-12：B 线的 `gates_ok` 读端点、读不到写 `-`（**这一半是对的：读不到与没变在采样行里长得
  不一样**），**但完成条件里根本没有 `gates_ok`**——于是**每一条样本都是 `-` 的窗，照样能收成绿**。
  6j 那次 77 采样全是 `1`，**那是运气不是判据**。已改成要求末样本 `gates_ok == 1` 并计 `gates_unreadable`。
  **同一个洞在我自己的观察器里也有，形状不同**：所有 journal 派生的字段都是
  `journalctl … 2>/dev/null | grep -c <pattern>`，**journalctl 本身失败时计数是 `0`，而 `0` 正是通过值**——
  "读不到日志"与"日志里没有坏消息"完全同形。`rounds` 本可以当这一档的哨兵（它为 0 一眼可疑），
  **但我没把它放进完成条件**。A-16b 这一窗里 `rounds=12`，所以那几个 0 是真的；**那是我事后手工核的，
  不是判据替我核的。**
  **做法**：(a) 每个可能"读不到"的取数，失败时落到一个**与通过值不同**的档；(b) **把那一档写进完成
  条件**（要么要求它不出现，要么要求一个非零哨兵同时在场）。**(a) 做一半是常见的；(b) 才是那句
  "我们一次也没能查上不是通过"真正落地的地方。**
- **一个"几乎是时钟"的量，比一个"就是时钟"的量更会骗人。**
  step-14 曾断言 `rounds == elapsed_minutes` 是恒等式，依据是那一个窗口 15 分钟正好 15 轮。
  **后来自己的两个窗口都不是**：A-16a 15 分钟 **13** 轮（0.87/分），A-16c 79 分钟 **60** 轮
  （0.76/分）。B 线 2026-09-12 独立更正了同一个数（它此前报 7 轮/分，真实约 **0.75 轮/分**，
  高估九倍，根因是 `journalctl --since` 的裸时间戳被按服务器本地时区解释，窗口宽了八小时）。
  **所以 reconcile 不是 60 秒一轮，是约 75–80 秒一轮。**
  **而这让原来那条教训更重而不是更轻**：如果 `rounds` 精确等于分钟数，读的人迟早会察觉它没有信息；
  **而它只是"差不多等于"，就会一直看起来像一个独立的测量**——`rounds=13` 配 `elapsed=15m` 读起来
  像两个来源，实际仍是同一个时钟乘了一个我没测过的系数。**判据里不要放"近似时钟"的量**，
  要放"本轮实际检查过几件事"这种能为零、也能因故障而停住的量。
  **`journalctl --since` 的裸时间戳按本机时区解释，`--utc` 只改显示不改解释**；观察器里一律用
  `--since "@<epoch>"`（我的两个脚本都是），**手工查时图省事写裸时间戳，就是 B 线这次八小时的来源**。
  **它一直没被发现，正因为错的方向是"更宽"**——要找的东西仍在结果里，只是混进了不该有的数据。
  **一个更宽的窗口不会让你查不到，只会让你数错，所以"查到了"从来不是查法正确的证据。**
- **把 KOL 原文里的数字与 `request_json` 里的数字对比时，文本相等永远是错的判据。**
  一边是人手打进消息的（`77000`、`75700`），一边是我们自己序列化进报文的（`"px":"77000.0"`、
  `"slTriggerPx":"75700.0"`）。**两边的书写习惯从来不一致，而它们表示同一个数。**
  2026-09-12（A-16b）：重复入场判据写成按数值比之后，**把它变异成按文本比，4 条用例转红，
  其中包括对真实事件 10434/10435 的离线重放**——真实那条腿的 `px` 是 `"77000.0"`，消息里是
  `77000`，**所以按文本比的版本对那次事件根本不会命中**：它会放过第二条入场，30 张仓位照旧发生，
  而除了专门为此写的那条用例之外全都是绿的。
  **这就是 6f-1 的 `"75700.0" != "75700"` 在一个全新判据里原地复现**（那次挡了 27 个仓位、
  三个多月）。**区别只在于这次它在写下去的同一小时里被变异检验抓住了。**
  **所以要记的不是"记得用 `Decimal`"**——那条早就写在本节里了，照样又犯。要记的是**这一类比较本身
  就是高危的**：两侧的字符串来自两个互不知情的书写者，**任何跨越这条边界的相等判断都必须先变成数**。
- **给自己这一步定的判据，量必须限定在自己这一步上；全局计数器在多线并行时不是判据，是环境的性质。**
  2026-09-11 A-16c 的 L2 窗，判据 A4 写的是 `position_mutation_intents` **窗内新增 = 0**，本意是
  "本步不向交易所写任何东西"。**那张表是全系统共用的。** B 线另一条线的四条合法写入落在
  **窗口重置前 36 秒**，于是重置后计数真的是 0——**判据通过了，而且没说谎**。可是它晚一分钟，
  **窗口就会因别人的合法动作判失败**，而查的人会去找"本步为什么写了交易所"，答案是它一个字没写。
  **正确写法**：按 `idempotency_key` 前缀（或本步可能产生的那类行）过滤。
  **还有一层更隐蔽的，采样行自己不会说**：`WINDOW_MET` 那行写着 `new_intents=0`，读起来像
  "观察期间没有任何交易所写入"，**而它只对重置之后那一段为真**——重置把一件真实发生的事挪出了窗口。
  **所以凡是发生过重置的窗，记录必须自己写明重置前那一段里发生了什么**，否则采样行会被读成一句
  它没说的话。
- **"我这一项在集合里"与"集合里每一项都接上了线"是两个性质，各有各漏掉的那一半。**
  2026-09-11，`ALWAYS_NOTIFIED_INCIDENT_TYPES`：这个集合是"告警会不会到人"的唯一开关，而漏一项
  **不会以任何形式报错**——代码、测试、四步部署、全量绿，每一处都照常绿，只是那条告警此后永远不
  存在（A-10b 的形状）。为它写了两种断言，B 线的变异检验说明了为什么两种都要留：
  | 变异 | 点名式（"我的类型在集合里"） | 遍历式（"集合里每项都 captures 且 notifies"） |
  |---|---|---|
  | 从集合里删掉我的类型 | **红** | 绿——剩下的每一项确实都还接着线 |
  | 把 `_with_always_notified_types` 改成原样返回 | 绿——类型确实还在集合里 | **红** |
  **前者守"这一项没被删"，后者守"接线机制没坏"。** 各自都能在对方全绿时转红，所以两条都留。
  **遍历式还要先断言集合非空（或 `>= N`）**：**遍历一个空集合会无声通过**，而集合变空正是它要抓的
  失败之一——这正是本节"一个少了一项的集合，读起来和完整的集合一模一样"的极端情形。

- **一个与你脑子里的说法矛盾的数字，本身就是一个待办——不能因为当下的任务不需要它而放过。**
  2026-09-10：A 线的观察窗 11 分钟拿到 **11 轮** reconcile（一分钟一轮），就在采样行第一列；
  而 B 线此前告诉过它"约 49 分钟一轮"，A 线也照这个数在排期。**两者直接矛盾，而 A 线只是把那个
  数字读了过去**——当时的任务是等窗口达标，不需要知道周期。矛盾直到 B 线自己更正才被说出来
  （那个 49 分钟是从**按 fingerprint 去重**的事故表推的，读到的其实是"内容变化的间隔"）。
  **这与本节"不推动动作的描述得不到防线保护"是一体两面**：那条讲**错误的描述**因为不要求动作
  而长期存活；这条讲**正确的观测**因为不要求动作而不被用来检验任何东西。
  **两边指向同一件事：只有会引起动作的信息才会被验证。**
  **做法**：读到一个与已有说法不符的数字，当场记一句"这与 X 说的不一致"，**哪怕当下不查**——
  写下来的矛盾会被下一个人撞到，没写下来的只会在下一次事故里重新出现。
  **正面用例（同日，A-15 全量）**：预期 8343 条、实际 **8344**，差 1。**没有放过**，去查出是
  B 线一个提交里的测试参数化（它此前提过），对账完毕。**差 1 与差 100 一样值得停一次**——
  能对上的数字才是证据，"大概对得上"不是。
  另附一条同源的、并非从数字来的：**一个更正能不能变成资产，取决于收到的人愿不愿意拿它去撞
  自己已经在用的东西。** B 线更正"60 秒一轮"，A 线没有停在"收下"，而是拿它回头把自己一条
  沿用了四步的窗口判据判死（`rounds` 是恒等式）。**这条对两条线同样成立。**
- **包含式词表默认关闭，排除式词表默认开放——控制"是否执行"的包含式词表，必须有一条断言
  它与它的 planner 同集。**
  2026-09-10（A-15-0/A-15-1）：同一个仓位上，止损执行器用排除式（`order_kind != "manual_bind"`），
  止盈执行器用包含式（`order_kind in {"trigger_limit", "market"}`）。阶段 5 新增了 `limit`
  入场，**排除式那个自动接纳了它，包含式那个自动排除了它**——止损挂得上，止盈三个月一张没挂出。
  **两种遗漏都是静默的，但只有排除式那种会被发现**：它会产生一个可见的动作，而包含式产生的是
  一个"没有"，**而"没有"不推动任何人做任何事**。这是本节"不推动动作的描述得不到防线保护"的
  第三种形态（前两种是只写进日志的描述、和只在事故里出现的字段）。
  **做法**：断言**两个集合相等**（或干脆共用同一个常量对象），而不是断言它含哪些值——
  值会随业务变，"与 planner 同集"这个关系不会。
  **并且断言要写成行为**：A-15-1 的用例除了断同一对象，还有一条"把共享常量收窄，执行器必须
  跟着收窄"——内联回字面量会让这条 monkeypatch 失效而转红，这是"单一来源"唯一测得出来的形式。
  **参数化不要遍历被测常量**：那样从常量里删一个值，用例会**悄悄少一个而不是转红**。
- **单值 `==` 就是一个只有一个元素的集合，只是没写成集合——扫"词表不一致"时必须把它算进去。**
  2026-09-10：按"具名集合常量"扫，全仓只有 6 处、结论是"`executor:506` 是唯一漏掉 `limit` 的"；
  按"把单值 `==` 也算进去"扫，是 30 处，而**这次真正咬人的第三道门**
  （`execution_bindings` 1491/1514 的 `order_kind == "trigger_limit"`）**恰好只在后一个口径里**。
  前一个口径抓"集合忘了加成员"，后一个抓"根本没写成集合"——**后者更隐蔽，因为它连"这里有个
  词表"都不显式**。
- **只按 `pos_id` 开的放开闸门，并不保证"就是那一个仓位"——我们自己的归属链已经出现过同号跨实例。**
  2026-09-11：四个释放常量（止盈、保本替换、保本全退、备份止损收养）全部以 `pos_id` 集合为键。
  B 线复核时发现**全库有 3 个 `pos_id` 各自挂在两个不同的 strategy instance 下**，其中一个还横跨
  两个群（同为 ETH short、相隔一天）。**几乎肯定不是交易所回收号，而是旧匹配器把同一个活仓位
  归给了两个信号**——但**从那些行本身分辨不了"交易所复用"与"我们误归属"**，两种都会让一个
  按号开的闸门授权到它本不打算授权的对象。
  （我自己只在 `position_mutation_intents` 上复核过，那张表里**没有**同号跨实例；B 线那三例来自
  别的表，我没有重新推导——**这里写的是它测得的现象，不是我测得的**。）
  **顺带一个常被当成反证的论证要作废**：`pos_id` **不是随时间单调递增的**（相邻 358 对里 66 对递减），
  所以"号一直往上走，所以不会回收"不成立。
  **做法**：这不改变"闸门按号开"这个设计（它已经是目前最紧的粒度），但意味着**闸门里的号必须随
  仓位消亡而清掉**——一个指向已消失仓位的号，在归属链出错或号被复用时就不再是死数据，而是一条
  对陌生仓位的预授权。**退役的动作因此不是整理卫生，是关闸。**
  **还有一条口径要分清，我第一次就写错了**：退役这些号时，`adopted_primary_backup_stop` 是
  **"已兑现完、对象也消失"**的许可（intent 664/665，09-10 20:36Z/21:09Z 两笔真实备份止损，我自己在
  `position_mutation_intents` 上按 `idempotency_key` 前缀核过：`trigger-backup-stop` 两条 confirmed），
  而 `break_even_replacement` 是**从未被执行过**的许可（同一批 posId 上零条 `break-even` 前缀写入）。
  **两者并排写会让前者读起来像后者**，而"清一条用过的"与"清一条没用过的"在复盘时是两件事。
- **一个闸门放开之后，它的状态只存在于放开者的记忆与生产文件里；对其他线而言，没有任何事件宣告过它。**
  所以跨线协作时，**"对方的闸门现在是什么状态"必须去读，不能问也不能推**。
  2026-09-11：A 线要放开限价入场的止盈，需要知道 B 线的保本闸门是否已放开。**推**会错（A 线据一条
  过期的排队消息断定"它还没部署"）；**问**也会慢（对方的回信同样是记忆）。**去读生产上的那个文件**，
  一次就拿到了确定答案——`BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS` 已含两个仓位、`FULL_EXIT` 仍为空集。
  **做法**：放开闸门时留下一条可观测的东西（启动日志 / 健康端点列出当前放开集合），让别人不必读源码；
  在它存在之前，读源码是唯一可靠的办法。
  **但那条自报行描述的是"这份源码"的闸门，不是"生产"的闸门**（B 线 2026-09-11 的干净反例）：它在
  自己工作树里渲染出的那一行第四位带着一个 posId，而生产那一位已经是 `-`——**两行都正确、都不过期，
  只是描述两棵不同的树**。所以把它当窗口判据时，基线必须是**部署那一刻从生产读到的那一条**
  （端点，或生产 journal 里的启动行），**不能是本地渲染或从源码推出来的**。
  多分支并行时，后者会给出一条读起来一样可信的、错的基线——**指纹做成可读而不是哈希，正是为了让人
  一眼看出差在哪一位；前提是比的那两条是对的两条。****"我改了并提交了"不是"它在生产上"**，中间隔着一次部署。
- **未读到不等于不存在——这条对授权链同样成立，而且方向相反。**
  我们一直用它防"读失败被当成零"。2026-09-11 出现了反向误用：**把"我没有看到那段出示原文"当成了
  "那段出示不存在"**，于是两条线各停了一轮，而原文一直都在（用户看到的 6h 出示里，"止盈成交后把主
  止损抬到 77000" 是本体，不是未说的连带）。
  **但要把代价的方向写清楚**：在**不可逆、动真钱**的动作上，**先停再问是对的**——停的代价是一轮对话，
  不停的代价是一笔未经授权的交易。**这条不是"别停"，是"停下来之后第一件事是去要原文，而不是就地推断"。**
- **"命令历史上做过检查"不等于"有一道约束"。检查必须在动作的控制路径上，而且必须测那个真正
  要紧的性质。** 2026-09-10/11 一天之内，两条线一共踩了**七次同一个形状**，每一次事后看命令
  历史都像"我检查过了"：
  | 检查 | 它实际返回什么 | 为什么不构成约束 |
  |---|---|---|
  | `git push … \| tail -2; echo "已推"` | 恒打印"已推" | 管道吞掉退出码，`echo` 无条件执行；那次推送其实被拒 |
  | `nohup obs.sh > dir/log` | 空日志 | 目录不存在，脚本**根本没启动**；靠枚举进程才发现 |
  | `sqlite3 … 2>/dev/null`（列名写错） | 空结果 | SQL 报错被吞成"没有行"，而 `COUNT(*)` 同时说有两行 |
  | `grep -qvE …` 判 PASS/FAIL | 恒 PASS | 本机 `grep` 是包 `ugrep` 的函数，`-v` 有反选行时仍返回 1 |
  | `pgrep -f <脚本名>` | 恒"还在跑" | 匹配到发起检查的命令行**自己**；空转 4 小时 57 分 |
  | `test -e f && echo EXISTS \|\| echo safe` 后接无条件 `cat > f` | 打印 EXISTS 然后照样覆盖 | 守卫与被守卫的动作**写在同一口气里，后者不依赖前者**；而且它测的是"文件存在"，**危险却来自"文件正在被执行"** |
  | 跨会话 `send_message` 返回 `success` | 恒 success | 它确认的是"送到了一个存在的地址"，**不是"送到了我要找的人"**——有几条投给了第三个会话 |
  **两条判准，比逐条记住这七个坑有用**：
  1. **这个检查的结果，有没有可能让接下来那一步不执行？** 不能，它就不是守卫，是注释。
  2. **它测的性质，和我怕的那件事，是同一件吗？**"文件存在"不是"文件在被执行"；"地址有效"不是
     "对方收到"；"我发了命令"不是"它生效了"。
  **第八例，同日、写下规则一小时之后**（B 线）：守卫这次**机制对了**——真的 `if … exit 1`——
  **但谓词错了**：`ps -eo pid,args | grep -q '[/]root/observe_template.sh'` 当场误报并终止了一个
  安全的动作，因为唯一的匹配是**这条检查命令自己的 `bash -c` argv**。
  **`[/]` 这个括号写法只防 `grep` 匹配到自己那个 grep 进程，防不了外层 shell 的命令行。**
  它落在判准 2 上：**用文本匹配回答了一个关于身份的问题。** 换成 pidfile + `kill -0` 才对。
  **而这一例最值得记的不是谓词**：规则当天写进了这份文档，一小时后照样犯——**能替代记忆的
  不是条目，是机制**（让脚本自己写 pidfile，于是"谁在跑"不再需要猜）。
  **可执行的替代**：退出码要么 `if cmd; then … else … fi`、要么改成**计数式**（`N=$(… \| wc -l)`；
  `[ "$N" -eq 0 ]`）；"有没有在跑"用脚本自己写的**标记文件**或**精确 pid**；"对方收到了没有"用
  对方**回话的内容**，而不是投递回执。**并且判据本身要先用一组已知真假的输入喂一次**——上表第四行
  正是在我把"判据要自己说话"写进文档的同一屏内失效的。
  **这条条目以一个正面样本收尾，而不是以八个坑收尾**（B 线的建议，接受）：同一天唯一一次
  **守卫真的救了人**，是 `test_no_production_module_reads_the_phase_one_inbox_table`——B 线把 WS 帧
  当成交证据写进影子，它在全量里当场转红。它满足上面两条判准：红会让部署那一步不执行（判准 1）；
  它测的性质就是要怕的那件事——"有没有生产模块读那张表"（判准 2）。
  **而它有那八个坑都没有的一个特征：它不是当事人写的，也不需要当事人想起来。** 那八次失败，
  每一次的修复都要靠下一次有人记得；这一次不需要任何人记得。
  **所以复盘时该问的不是"这条写清楚了没有"，而是"这条有没有对应的机制"。** 今天两条线写下的
  条目里，同时落成机制的只有少数几条：`EXECUTOR_ENTRY_ORDER_KINDS is AUTOMATIC_ENTRY_ORDER_KINDS`
  （同一对象断言）、`*_RELEASED_POS_IDS` 的完整性测试（读源码而不是手写清单）、`gates_ok`
  （窗口自己会因闸门变化而重置）、以及上面那条收件箱守卫。**其余的都还只是条目，而条目靠记忆执行。**
- **`echo "成功"` 不是确认——一个无条件打印的字符串，是把会失败的量换成了恒真的量。**
  2026-09-10（A-15-1 收窗后）：`git push ... 2>&1 | tail -2; echo "已推共享分支"`。
  **管道吞掉了 `git push` 的退出码**，`echo` 无条件执行，于是一次 non-fast-forward 拒绝
  （别人先推了一个提交）被报成了成功；直到下一条命令去读远端 ref 才发现。
  **这与本节"`rounds == elapsed_minutes` 是恒等式"是同一个病的两种形态**：前者把一个能失败的
  量替换成恒真的量，后者把一个恒真的量当成能失败的量在读。
  **做法**：会失败的动作要么用 `if cmd; then ... else ... fi` 读真实退出码，要么**回去读它应该
  改变的那个东西**（远端 ref、进程表、数据库行）。**"我发出了这条命令"与"它生效了"是两件事**——
  同一天另一次是 `nohup` 的重定向目录不存在、观察脚本根本没起来，也是靠枚举进程才发现的。
- **"两种读法读同一批行"与"两条独立路径到达同一答案"，强度差一个量级。**
  两者看起来都是"对上了"，但只有后者满足本节那条判准（这份证据有没有可能与被证对象不一致）。
  2026-09-10 同一天的两个实例：**B 线 6h 的 112/112**——旧判据逐条全拒、新读法逐条全解析，
  读的是**同一批交易所行**，所以它证明的是"两种读法对同一数据给出不同答案"（这足以证明修对了
  读，但不证明别的）；**A-15-1 的四张止盈单**——一条路径是库副本 + 打了补丁的源码副本上的演练，
  另一条是生产真代码在真数据上跑出来的，**两者完全可能分叉，没分叉才是证据**。
  **做法**：说"对上了"之前先问一句——**这两个数如果不一致，是不是真的有可能？** 不可能，
  那"一致"就没有信息量。
- **拿来作证的东西，如果它的内容完全来自被证的对象，它不可能反驳它。**
  这条统摄两个已经各自吃过亏的形态：**夹具照着实现写**（于是"自洽"被当成"正确"，见本节
  "夹具是被谁写的，它就替谁背书"）、和**数字照着代码算**（于是"推导"被当成"运行"，见本节
  "能跑就别算"）。判断方法只有一个问句：**这份证据有没有可能与被证对象不一致？**
  不可能，就不是证据。
  **附带一条写法**：本节条目由多条并行会话追加，**"见上一条 / 下一条"这种位置引用会被别人的
  追加打断**——本条第一版就是这么写的，一次变基之后指错了地方。**引条目名，不引位置。**
- **能跑就别算——尤其在准备把数字拿给人批准的时候。**
  2026-09-10（A-15-1）：为了出示给用户，本该由算术给出的四张止盈单（`7+8=15`，自洽且事后证明
  数值正确），改成在**生产库副本 + 打了补丁的源码副本 + 只读交易所**上跑真实计划器。
  **第一次跑出来不是 `ready`，是 `convergence_protection_leg_conflict`**——那是读了一整天代码
  都没读出来的第三道门。**算术替运行作证，与夹具替实现作证是同一个病**：拿一个自洽的东西
  代替真实的东西。
- **遍历式守卫要数次数，不要只问有没有。**
  2026-09-12（A-17）：守卫第一版问的是"凡把 binding 写成 `closed` 的函数，里面有没有调用终态化"。
  `_derive_repaired_bindings` 在两个分支里各关闭一次，**去掉其中一处调用，函数里仍"有调用"，守卫通过**。
  **按函数的 yes/no 会把同一函数内的第二处吞掉**——这正是"点名三处、接了十处里的三处"在函数内部的缩小版。
  **做法**：守卫比较计数（调用次数 ≥ 写入次数），并带一个"至少 N 处"的哨兵，防止遍历什么都没解析到也判通过。
- **变异检查被"改坏了"骗过一次：用例转红，不等于断言抓到了变异。**
  2026-09-12（A-17）：用正则从 import 非贪婪匹配到目标标签来删掉一处调用。同文件里三处同缩进，
  **最左边那个 import 一路跨函数匹配到了目标**，删掉了几个函数，于是该模块全部用例报红——
  包括与被删调用毫无关系的用例。**"不相关的用例也红了"本身就是信号**：红得太多和红得太少一样需要解释。
  **做法**：变异要断言自己的形状（删了几行、含几个调用），并且看**哪些**用例红了，而不只看红没红。
- 迁移只改变"在哪里跑、怎么组织"，从不改变"决定什么"。任何看起来需要改交易语义的改动
  都是读错了需求，停下来问。
