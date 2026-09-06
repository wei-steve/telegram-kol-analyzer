# AI 上下文生产观测日志

## 2026-09-02 — ATTENTION

> **ATTENTION：窗口内发生多次 release / PID 漂移；monitor 从 2026-09-02T10:01:36Z 起连续 13 个周期 `healthy=false`，均含资金安全相关 `stalled_composite_component`，其中 13:01:25Z 另含 `event_recovery_status`。** Shadow 判据仍未影响权威路径；当前两个 BTC 空头持仓的交易所保护回读完整。

### 固定窗口

- 起点（开区间）：`2026-09-01T07:12:19Z`，承接计划文档最后一次 P0 截止；原始消息边界 `raw_messages.id > 14232`。
- 固定终点：`2026-09-02T16:02:13Z`；时长 1 天 8 小时 49 分 54 秒（1.367986 个 24 小时日）。所有数据库和 journal 窗口查询均沿用该终点，没有滑动。
- 新消息：272 条，ID `14233–14504`；`created_at` 为 `2026-09-01T07:29:39.376745Z–2026-09-02T15:51:14.430119Z`。`created_at` 或 `posted_at` 早于/等于窗口起点的行均为 0。
- 生产 checkout HEAD：`0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`。当前运行 release 与 checkout HEAD 不相同，详见运行身份。
- 数据来源：生产 `research.db` 的 `sqlite3 -readonly` 固定窗口查询；runtime identity / loop health；worker `http://127.0.0.1:8002` 的只读 exchange snapshot、持仓和当前委托 GET。未调用 Web 8000 获取交易所证据；未执行 Deepcoin 写入、消息处理或重放。

### 异常优先

1. **PID / SHA 漂移：是。** monitor 在 68 个周期中依次加载 `18434b45...`（2 次）、`4284d1a6...`（11 次）、`3205b074...`（19 次）、`0de19c1c...`（36 次）。当前 Web 又是单独的 `b78f1609...` release；当前 ingest / worker 为 `0de19c1c...`。
2. **资金安全类 monitor reason：是。** 13 个连续周期出现 `stalled_composite_component`，其中一个周期同时出现 `event_recovery_status`。当前 worker 核心循环虽为 fresh / successful，但不能用这一时点信号覆盖 durable composite stall 的 monitor 结论。
3. **Shadow 改变实际决策：否。** 65 条 `shadow_would_skip` 仍全部执行了权威上下文解析；59 条结果为 `hold`、6 条为 `unresolved`，第一层均为 `非策略 / none / 无目标`，动作族与目标集合均未实质改变。
4. **窗口前来源消息触发交易所写入：否。** 窗口内共有 11 条带 order/request 证据的 submitted exchange events，但与 `raw_messages` 精确关联后，来源消息早于窗口起点的行数为 0。
5. **持仓保护异常：否。** 两个 BTC 空头持仓的止盈、主止损、备份止损方向和数量均覆盖；worker 投影均为已验证归属/保护。
6. **交易所快照连续两次不完整：否。** 首次 worker snapshot 即 `complete=true`，未触发重试。
7. **P0 首次达到 500 条：否。** 累计 346 条，还差 154 条。
8. **主识别日均 token 较上次变化 >50%：无法比较。** 这是该日志首次记录，没有上次同口径值；本次建立直接测量基线，不将“无基线”判成变化。

### 1. Shadow 生产分歧

- 窗口 context attempts 191；其中具备 shadow 结果的生产样本 152（也是当前累计全历史 shadow 样本，因为更早行均为 NULL）。
- `shadow_would_trigger=true` 87；与权威调用一致 87；`shadow_would_skip` 65；`shadow_would_extra_trigger` 0；计算错误 0。
- 65 条 would-skip 中 59 条为 `hold`、6 条为 `unresolved`；所有第一层均为 `非策略 / none / target=NULL`，上下文 `target_thread_ids=[]`。下表逐条给出 attempt / raw；“否”表示动作族仍是 `no_action` 且目标集合仍为空。

| attempt / raw | 上下文结果 | 实质改变第一层 |
|---|---|---|
| 4329 / 14288 | hold | 否 |
| 4334 / 14293 | hold | 否 |
| 4339 / 14299 | hold | 否 |
| 4341 / 14301 | unresolved | 否 |
| 4343 / 14303 | hold | 否 |
| 4344 / 14304 | hold | 否 |
| 4346 / 14308 | hold | 否 |
| 4348 / 14311 | hold | 否 |
| 4351 / 14335 | hold | 否 |
| 4353 / 14341 | hold | 否 |
| 4357 / 14350 | hold | 否 |
| 4359 / 14352 | hold | 否 |
| 4360 / 14353 | hold | 否 |
| 4361 / 14354 | hold | 否 |
| 4362 / 14355 | hold | 否 |
| 4364 / 14359 | hold | 否 |
| 4366 / 14361 | hold | 否 |
| 4367 / 14362 | hold | 否 |
| 4368 / 14368 | hold | 否 |
| 4370 / 14372 | hold | 否 |
| 4382 / 14379 | hold | 否 |
| 4383 / 14380 | hold | 否 |
| 4385 / 14383 | hold | 否 |
| 4393 / 14392 | hold | 否 |
| 4395 / 14399 | hold | 否 |
| 4398 / 14402 | hold | 否 |
| 4400 / 14404 | hold | 否 |
| 4402 / 14406 | hold | 否 |
| 4408 / 14412 | unresolved | 否 |
| 4409 / 14413 | unresolved | 否 |
| 4410 / 14414 | hold | 否 |
| 4416 / 14423 | hold | 否 |
| 4421 / 14429 | hold | 否 |
| 4428 / 14435 | hold | 否 |
| 4431 / 14448 | hold | 否 |
| 4432 / 14449 | hold | 否 |
| 4433 / 14450 | hold | 否 |
| 4434 / 14451 | hold | 否 |
| 4435 / 14452 | hold | 否 |
| 4436 / 14453 | hold | 否 |
| 4437 / 14454 | hold | 否 |
| 4438 / 14455 | hold | 否 |
| 4439 / 14456 | hold | 否 |
| 4441 / 14459 | hold | 否 |
| 4442 / 14457 | hold | 否 |
| 4443 / 14460 | hold | 否 |
| 4444 / 14461 | hold | 否 |
| 4445 / 14464 | hold | 否 |
| 4448 / 14467 | unresolved | 否 |
| 4451 / 14471 | hold | 否 |
| 4452 / 14472 | hold | 否 |
| 4456 / 14476 | hold | 否 |
| 4457 / 14480 | hold | 否 |
| 4459 / 14482 | unresolved | 否 |
| 4464 / 14487 | hold | 否 |
| 4465 / 14488 | hold | 否 |
| 4466 / 14489 | unresolved | 否 |
| 4468 / 14491 | hold | 否 |
| 4470 / 14493 | hold | 否 |
| 4472 / 14495 | hold | 否 |
| 4473 / 14496 | hold | 否 |
| 4474 / 14497 | hold | 否 |
| 4475 / 14498 | hold | 否 |
| 4476 / 14499 | hold | 否 |
| 4478 / 14497 | hold | 否 |

用计划文档第 12.8.D 节同一动作族、置信度 `<0.7` 不可应用和目标集合口径复算：152 条 shadow 样本中 150 条有可比较 decision，18 条实质改变；shadow 保留 18/18，漏失 0，累计召回率 **100%**，Wilson 95% CI **82.41%–100%**。P0 累计消息 346/500，距 500 条还差 154 条；达到 500 也只满足样本停止条件，不能替代既定的人工标注、留出回放和零漏失评审。

### 2. 主识别真实成本

- 窗口内 `mimo_recognition_attempts` 289 行。直接计数 telemetry 只覆盖其中 191 行，记录 185 次 provider request；更早的 98 行没有 request counter，因此全窗口 provider 调用总数为未知，不能用 185 代替全量。
- 185 个 usage entry 中 184 个 `available=true`，真实 usage 可得比例 **99.46%**；1 个 provider request 无 usage。以下 token 均仅来自 184 个可得请求，是直接测量下界：prompt 4,381,840，completion 315,198，总计 **4,697,038**。
- 每请求 total token：中位数 **25,174**，P90 **38,880**，最大 **40,708**，平均 **25,527.38**。
- 组件字节按 185 个已计数请求加权，总 request 24,064,414 B：当前消息文本 36,090 B（0.150%）；图片证据 9,439,381 B（39.225%）；直接 reply 1,654,440 B（6.875%，是 authoritative context 的嵌套子集，分区时只扣一次）；其余 12,934,503 B（53.750%）。
- 主识别真实 telemetry 从 `2026-09-01T22:25:52.941030Z` 开始。到固定终点按 0.733565 日暴露归一化为至少 **6.403M token/日**；因一个请求 usage 不可得，这是下界。本日志首次建立该基线，暂无同口径上次值可做 50% 变化判断。

### 3. 成本对照

- 上下文解析在完整增量窗口有 191 attempts、195 provider requests，195/195 usage 可得；prompt 3,857,230，completion 255,694，总计 **4,112,924 token**。每请求平均 21,091.92；按完整 1.367986 日归一化为 **3.007M token/日**，按窗口 272 条消息为 15,121.04 token/消息。
- 为避免主识别 telemetry 晚上线导致错配，阶段对照只用共同窗口 `2026-09-01T22:25:52.941030Z–2026-09-02T16:02:13Z`，共 171 条消息：

| 阶段 | provider 请求 | 实测 token | 日均 token | 两阶段占比 | token/消息 |
|---|---:|---:|---:|---:|---:|
| 主识别 | 184 个 usage 可得（185 个调用） | 4,697,038 | 至少 6,403,025 | 59.98% | 至少 27,468.06 |
| 上下文解析 | 135 | 3,134,303 | 4,272,697 | 40.02% | 18,329.26 |

共同窗口中，主识别比上下文解析多 **2.130M token/日**，负载约为后者 **1.50 倍**；以累计直接测量的绝对量看，优先优化主识别的潜在收益更大。但主识别路径直接决定第一层输入，风险也最高；该数字只说明成本侧优先级，不授权改变 prompt、窗口或决策路径。

### 4. 常规 P0 与运行健康

- 窗口消息 272；context attempts 191，覆盖 177 条消息，provider requests 195。状态：completed 185（187 requests，185 decisions）；exhausted 6（8 requests，4 decisions）。
- 8 个直接持久化触发器（非互斥）：`multiple_same_source_candidates=184`、`entered_holder_language=31`、`management_without_exact_target=16`、`apparent_entry_may_be_revision=3`、`revision_language=2`、`text_image_conflict=2`、`cancellation_language=0`、`reply_target_disagreement=0`。
- 189 条可比较 decision 中 22 条实质改变，描述值 11.64%；另 2 条无 decision。累计 P0 仍只有 346/500 条，因此该比例不是最终率，也不用于调整触发器、词表或阈值。
- 累计 P0 自 `2026-08-31T16:42:37Z` 起共 346 条消息，约 1 天 23 小时 19 分 36 秒；距离 500 条差 154，距离 7 个 24 小时日差约 5 天 40 分 24 秒，先到者作为停止条件。

当前三角色（采样于固定终点后 `2026-09-02T16:03:36Z–16:03:37Z`）：

| role | release / PID | artifact | entry freeze | 角色健康 | loop health |
|---|---|---|---|---|---|
| web | `b78f16098c591978fe764e15c9b793182fc97f5b` / 654288 | verified | false | event loop=true | stall_count=0，watchdog attached |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` / 3315585 | verified | false | event loop/listener/reconcile=true | stall_count=3，最近捕获均标记 `captured_business_blocker` |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` / 3315574 | verified | false | event loop/command/message processing=true；management、break-even、reconcile、close、TPSL、protection、rescue 均 fresh/successful；global exchange authority=true | stall_count=2，watchdog attached |

`auto_trade_enabled=true`，`worker_command_mode=queue`。三个角色当前均未冻结，但窗口内 PID / release 明确漂移。

monitor 共 68 个周期，55 个 healthy、13 个 unhealthy；所有周期 `monitor_error=null`，artifact identity 均 verified。以下逐周期列出 UTC 结果：

- `healthy=true, reason_codes=[], audit_ran=false`：2026-09-01 `07:30:30, 08:01:35, 08:07:44, 08:31:35, 09:01:55, 09:31:48, 10:01:39, 10:31:10, 11:01:04, 11:30:45, 12:01:25, 12:30:40, 13:00:31, 13:34:25, 13:35:10, 14:02:08, 14:31:55, 15:01:17, 15:31:41, 16:01:50, 16:30:22, 17:00:21, 17:30:46, 18:00:19, 18:30:25, 19:00:43, 19:30:42, 20:00:38, 20:30:49, 21:00:57, 21:31:13, 22:01:57, 22:31:57, 23:01:39, 23:32:07`；2026-09-02 `00:01:18, 00:30:20, 01:30:28, 02:00:36, 02:31:25, 03:01:07, 03:31:18, 04:01:08, 04:31:49, 05:01:15, 05:31:48, 06:00:18, 06:30:26, 07:00:20, 07:31:48, 08:00:32, 08:31:19, 09:01:19, 09:31:40`。
- `healthy=true, reason_codes=[], audit_ran=true`：2026-09-02 `01:03:34`。
- `healthy=false, reason_codes=[stalled_composite_component]`：2026-09-02 `10:01:36`（sent）、`10:30:32`（suppressed）、`11:01:58`（suppressed）、`11:31:41`（suppressed）、`12:01:19`（suppressed）、`12:31:40`（suppressed）、`13:30:42`（sent）、`14:00:37`（suppressed）、`14:31:49`（suppressed）、`15:01:11`（suppressed）、`15:31:04`（suppressed）、`16:00:23`（suppressed）。
- `healthy=false, reason_codes=[event_recovery_status, stalled_composite_component]`：2026-09-02 `13:01:25`（sent）。

窗口内 execution events：`create_trigger_entry submitted=6`、`create_backup_stop submitted=3`、`open_market_position submitted=1`、`set_position_tpsl submitted=1`；另有 `auto_trade_skipped=9` 和一条不含 order ID 的 `source_message_deletion_outcome/recovery_required`。带 order/request 证据的 11 条 submitted 写入均未关联到窗口起点之前的来源消息。

### 5. 交易所只读快照（worker 8002）

- `2026-09-02T16:03:37Z` 首次 bounded snapshot：`complete=true`，持仓 2，普通挂单 0，fingerprint `49709f0fdd5bba8200c207bf435fdbde77064a5067fa3d973d421667c09209d5`。没有执行第二次重试。
- `2026-09-02T16:09:27.540497Z` 当前委托 GET：loaded=true，共 10 条，全部为 BTC 触发单；BTC=10、ETH=0、SOL=0，普通挂单=0。10 条中 7 条为持仓保护，3 条为待入场空单（7 张@78410、4 张@77910、6 张@79910）。
- `2026-09-02T16:09:21.677911Z` 持仓 GET：

| posId | 仓位 | 保护回读 | 方向 / 数量覆盖 | reduce-only |
|---|---|---|---|---|
| `1001125097840035` | BTC short，18 contracts，均价 77003 | TP 74700 ×18；SL 78100 ×18；备份 SL 78256.2 ×全部剩余 | 三条均为 long 平空方向；TP 和任一 SL 均完整覆盖 18 | 是，均为绑定该 posId 的 Deepcoin native TPSL / close-position 语义；8002 投影不另暴露名为 `reduceOnly` 的字面布尔字段 |
| `1001125090990141` | BTC short，4 contracts，均价 77447.7 | TP 75400 ×2、76100 ×2；SL 79300 ×全部剩余；备份 SL 79458.6 ×全部剩余 | 四条均为 long 平空方向；TP 合计 4，任一 SL 覆盖全部剩余 | 是，同上，为 exact-posId native TPSL / close-position 语义 |

7 条保护均在 worker 投影中标为“已验证归属/已验证保护”，没有缺失、反向、数量不足或无法归属。最新 durable TPSL completeness 也显示 BTC `complete=true, response_count=10`，ETH `complete=true, response_count=0`；SOL 当前委托由 worker GET 完整返回 0，未把缺失证据解释为 0。

### 本轮边界

本轮只追加本日志。未修改计划文档、代码、settings、白名单、词表、阈值、prompt、schema、数据库或业务数据；未 stage、部署、激活、重启服务；未处理、识别或重放消息；未执行 Deepcoin 写入。ATTENTION 仅记录观测结果，不改变 shadow 或实际决策。

## 2026-09-03 — ATTENTION

> **ATTENTION：P0 累计消息首次达到 500 条；48/48 个 monitor 周期均 `healthy=false` 且全部含资金安全相关 `stalled_composite_component`；Web 在窗口内发生 PID / release 漂移；两笔补保护写入的原始策略消息早于窗口起点；当前一个 5 张 BTC 空仓的止损方向和数量虽匹配，但保护归属未验证。** Shadow 判据没有影响权威路径，worker 首次 bounded exchange snapshot 完整。

### 固定窗口

- 起点（开区间）：`2026-09-02T16:02:13Z`，承接本日志上次截止；原始消息边界 `raw_messages.id > 14504`。
- 固定终点：`2026-09-03T16:02:02Z`；时长 23 小时 59 分 49 秒（0.999873 个 24 小时日）。`raw_messages`、attempt、execution event 和 journal 均显式限制到该终点；终点没有滑动。
- 新消息：184 条，ID `14505–14688`；`created_at` 为 `2026-09-02T16:30:46.591342Z–2026-09-03T16:01:54.270089Z`。`created_at` 或 `posted_at` 早于/等于窗口起点的增量消息为 0。
- 生产 checkout HEAD：`0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`。当前三个运行 release 均与 checkout HEAD 不同，Web 与 ingest/worker 也不是同一 release。
- 数据来源：生产 `research.db` 的 `sqlite3 -readonly` 固定窗口查询、systemd/runtime identity/loop health、monitor journal，以及 worker `http://127.0.0.1:8002` 的只读 bounded snapshot、持仓和当前委托 GET。未使用 Web 8000 获取交易所证据。

### 异常优先

1. **P0 首次达到 500 条：是。** 累计第 500 条为 `raw_messages.id=14658`，`created_at=2026-09-03T14:51:25.124334Z`；固定终点累计 530 条，超过目标 30 条。
2. **资金安全类 monitor reason：是。** 48/48 个周期均含 `stalled_composite_component`；另有 `adapter_failure`、`audit_abnormal`、`event_unknown_status`、`stale_entry_preamble_unresolved`。没有健康周期。
3. **PID / SHA 漂移：是。** 上次截止后 Web 从 `b78f1609...` / PID 654288 变为 `5aa7ca07...` / PID 1396631，当前进程启动于 `2026-09-02T16:55:31Z`；ingest、worker 仍为 `0de19c1c...` / PID 3315585、3315574。
4. **存在 binding 原始消息早于窗口的交易所写入：是。** `execution_events.id=3925` 和 `3950` 均为 `create_backup_stop submitted`；其 binding 原始消息分别为 raw 14403（`2026-09-02T07:34:10.669285Z`）和 raw 14389（`2026-09-02T04:07:11.467959Z`），都早于本窗口。两笔是既有仓位的补保护写入，不是新入场；event 没有直接 source raw，不能进一步证明是哪条当期消息触发，仍按既定 fail-closed 规则标记 ATTENTION。
5. **持仓保护证据异常：是。** `posId=1001125113096711` 的 5 张 BTC 空仓显示一张 83000 ×5 的平空 TPSL，但 worker 标为“保护归属未验证 / TPSL 未命中保护 ledger / 自动管理已冻结”，持仓卡片的已验证保护数为 0。方向、数量和 native TPSL 平仓语义相符，但 exact-posId 归属不能确认，因此保护完整性按未知处理。
6. **Shadow 改变实际决策：否。** 27 条 `shadow_would_skip` 仍全部走了权威上下文解析，且逐条均未实质改变第一层动作族或目标集合。
7. **交易所快照连续两次不完整：否。** 首次 worker bounded snapshot 即 `complete=true`，未重试。
8. **主识别实测日均 token 较上次变化 >50%：未实测到。** 可得 usage 下界从上次 6.403M 降至本次 4.086M token/日，表面变化 -36.19%；但 usage 可得率从 99.46% 降至 72.32%，62 个 provider request 没有 usage，故真实变化不能完整判定。

### 1. Shadow 生产分歧

- 窗口 context attempts 107，覆盖 88 条消息；shadow 样本 107，一致 80，`shadow_would_trigger=80`，`shadow_would_skip=27`，额外触发 0，计算错误 0。Shadow 字段只记录旁路判断，107 条权威 attempt 均未被它跳过。
- 27 条 would-skip 中 26 条权威结果为 `hold`、1 条为 `unresolved`；第一层均为 `非策略 / none / target=NULL`，上下文目标集合均为空，因此没有动作族或目标实质变化：

| attempt / raw | 权威上下文结果 | 实质改变第一层 |
|---|---|---|
| 4482 / 14517 | hold | 否 |
| 4483 / 14518 | unresolved | 否 |
| 4484 / 14520 | hold | 否 |
| 4488 / 14534 | hold | 否 |
| 4489 / 14537 | hold | 否 |
| 4490 / 14538 | hold | 否 |
| 4495 / 14544 | hold | 否 |
| 4499 / 14553 | hold | 否 |
| 4504 / 14564 | hold | 否 |
| 4514 / 14586 | hold | 否 |
| 4522 / 14595 | hold | 否 |
| 4525 / 14598 | hold | 否 |
| 4530 / 14603 | hold | 否 |
| 4537 / 14615 | hold | 否 |
| 4538 / 14616 | hold | 否 |
| 4541 / 14619 | hold | 否 |
| 4549 / 14638 | hold | 否 |
| 4559 / 14641 | hold | 否 |
| 4564 / 14643 | hold | 否 |
| 4566 / 14644 | hold | 否 |
| 4569 / 14645 | hold | 否 |
| 4570 / 14646 | hold | 否 |
| 4575 / 14655 | hold | 否 |
| 4577 / 14663 | hold | 否 |
| 4579 / 14665 | hold | 否 |
| 4580 / 14670 | hold | 否 |
| 4587 / 14678 | hold | 否 |

按计划文档同一动作族、置信度 `<0.7` 不可应用和目标集合口径复算：累计全历史 shadow 样本 259，39 条实质改变全部被 shadow 保留，召回率 **100%（39/39）**，Wilson 95% CI **91.03%–100%**。P0 消息停止条件所差样本数为 **0**，当前为 530/500；这只表示达到观测停止样本量，不替代人工标注、留出回放、零漏失评审和上线授权。

### 2. 主识别真实成本

- 窗口内 `mimo_recognition_attempts` 199 行、224 个 provider request；162 行 completed，37 行 `http_error / v1_authoritative_failed`，后者共发起 62 个 provider request。
- 224 个 usage entry 中 162 个 `available=true`，真实 usage 可得比例 **72.32%**；62 个均为 `provider_usage_not_returned`。以下 token 只来自 162 个可得请求，是直接测量下界：prompt 3,815,029，completion 269,959，总计 **4,084,988**。
- 每个可得 provider request 的 total token：中位数 **25,041**，P90 **39,435**，最大 **42,231**，平均 **25,215.98**。
- 224 个请求的持久化组件字节合计 34,304,191 B：当前消息文本 34,032 B（**0.099%**）；图片证据 15,980,237 B（**46.584%**）；直接 reply 1,571,234 B（**4.580%**，是 authoritative context 的嵌套子集，分区时只扣一次）；其余部分 16,718,688 B（**48.737%**）。
- 按 0.999873 日归一化，主识别实测为至少 **4.086M token/日**；按窗口 184 条消息为至少 **22,201.02 token/消息**。usage 缺口使这两个数只能作为下界。

### 3. 成本对照

- 上下文解析窗口有 107 attempts、121 个 provider request，121/121 usage 可得；prompt 2,845,017，completion 175,144，总计 **3,020,161 token**。日均 **3.021M**，每条窗口消息 **16,413.92 token**。
- 同一增量窗口的直接实测对照：主识别至少 4.086M token/日，占两阶段可得 token 的 **57.49%**；上下文解析 3.021M token/日，占 **42.51%**。主识别可得下界仍比上下文解析高约 **1.065M token/日**。
- 从主识别直接 telemetry 起点 `2026-09-01T22:25:52.941030Z` 到本次终点的共同累计口径，共 355 条消息：主识别 407 requests、344 usage 可得（84.52%）、8,711,340 token，折合至少 **5.025M/日、24,538.99/消息**；上下文解析 255/255 usage 可得、6,144,842 token，折合 **3.545M/日、17,309.41/消息**。两阶段累计可得 token 占比为 **58.64% / 41.36%**。
- 因此仅从累计直接测量绝对量看，主识别仍是更大的成本侧；优先优化它的数字收益上界更大。但主识别直接决定第一层输入，且本窗口 usage 缺失显著，本结论不授权改变 prompt、窗口或决策路径。

### 4. 常规 P0 与运行健康

- 窗口消息 184；context attempts 107，覆盖 88 条消息，provider requests 121。状态：completed 89（91 requests、89 decisions）；exhausted 18（30 requests、9 decisions）。
- 8 个直接持久化触发器（非互斥）：`multiple_same_source_candidates=98`、`entered_holder_language=9`、`management_without_exact_target=6`、`apparent_entry_may_be_revision=4`、`text_image_conflict=3`、`revision_language=2`、`reply_target_disagreement=1`、`cancellation_language=0`。
- 98 条可比较 decision 中 21 条实质改变，描述值 **21.43%**；另 9 条无 decision。该窗口仍只是一个流量切片，不用单日比例调整触发器、词表或阈值。
- P0 自 `2026-08-31T16:42:37Z` 起累计 530 条消息、约 2.972 个 24 小时日；消息样本目标已超过 30 条，时间条件尚未达到 7 日。按“先到者”规则，500 条停止条件已达到。

当前三角色（固定终点后采样于 `2026-09-03T16:03:17Z`）：

| role | release / PID | artifact | entry freeze | 角色与循环健康 |
|---|---|---|---|---|
| web | `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` / 1396631 | verified | false | event loop=true；当前 1 小时窗口 max=58.481 ms，累计 stall_count=4，watchdog attached |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` / 3315585 | verified | false | event loop/listener/reconcile=true；max=12.286 ms，累计 stall_count=7，watchdog attached |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` / 3315574 | verified | false | event loop/command/message processing=true；management、break-even、reconcile、close、TPSL、protection、rescue 均 fresh/successful；global exchange authority=true；max=344.226 ms，累计 stall_count=8，watchdog attached |

`auto_trade_enabled=true`，`worker_command_mode=queue`。三个角色当前均未冻结；Web 的 release/PID 漂移及三角色 release 不一致仍是 ATTENTION。

monitor 从上次截止后到固定终点共 48 个周期：0 healthy、48 unhealthy，全部 `monitor_error=null`；以下按完全相同的健康状态/reason codes 分组列出每个 UTC 周期：

- `healthy=false, reason_codes=[stalled_composite_component], audit_ran=false`（39）：2026-09-02 `16:31:16, 17:00:48, 17:31:58, 18:01:19, 18:31:06, 19:01:30, 19:31:50, 20:00:31, 20:31:58, 21:01:41, 21:31:43, 22:00:25, 22:32:05, 23:00:33, 23:30:48`；2026-09-03 `00:00:57, 00:30:49, 02:00:31, 02:30:50, 03:00:43, 03:30:34, 04:01:08, 04:31:26, 05:00:43, 05:30:44, 06:00:41, 06:31:24, 07:01:13, 08:02:06, 08:31:49, 09:02:02, 09:31:07, 10:00:42, 10:31:48, 11:00:59, 11:30:35, 12:00:49, 12:30:28, 13:30:21`。
- `healthy=false, reason_codes=[adapter_failure, stalled_composite_component], audit_ran=true`（1）：2026-09-03 `01:02:11`。
- `healthy=false, reason_codes=[audit_abnormal, stalled_composite_component], audit_ran=true`（1）：2026-09-03 `01:33:30`。
- `healthy=false, reason_codes=[event_unknown_status, stalled_composite_component], audit_ran=false`（2）：2026-09-03 `07:31:07, 13:01:45`。
- `healthy=false, reason_codes=[stale_entry_preamble_unresolved, stalled_composite_component], audit_ran=false`（5）：2026-09-03 `14:02:07, 14:31:47, 15:02:05, 15:31:27, 16:01:45`。

monitor 的 48 次加载身份均为 verified release `0de19c1c...`；通知结果为 sent 9、suppressed 39。窗口 execution events 中实际 `submitted` 写入 30 条：`create_trigger_entry=11`、`create_backup_stop=8`、`open_market_position=3`、`set_position_tpsl=3`、`strategy_management_close_submit=3`、`cancel_trigger_entry=1`、`strategy_management_cancel_deferred_trigger_entry=1`；另有 `auto_trade_skipped=2`、保护预取消 reserved/succeeded 各 8、terminal cleanup resolved 1。30 条 submitted 中 18 条有直接 source raw，均不早于窗口；其余 12 条没有直接 source raw。当前 lifecycle 状态能为其中 6 条提供窗口内 management message 指针，但该指针可能晚于 event，不能倒推为因果来源；另外 6 条连该指针也没有。其中两条补保护 event 的 binding 原始策略消息早于窗口，已在异常项逐笔列出，因此不能把缺失直接来源当成“没有历史来源”。

### 5. 交易所只读快照（worker 8002）

- `2026-09-03T16:03:17Z` 首次 bounded snapshot：`complete=true`，持仓 2，普通挂单 0，fingerprint `60bbcad96c097cdf6df061360b88c80f8c9c465590b4a9cdd050f9a565638716`；未触发第二次重试。
- `2026-09-03T16:04:13Z` 当前委托 GET：`loaded=true`，共 9 条触发单；BTC=5、ETH=2、SOL=2。BTC 包含 1 张待入场空单和 4 张平空 TPSL；ETH、SOL 各 2 张待入场多单。普通挂单为 0。
- `2026-09-03T16:09:16Z` 最新 durable TPSL 观察：BTC `complete=true, response_count=5`，ETH `complete=true, response_count=2`，SOL `complete=true, response_count=2`。这是固定终点后的当前状态交叉检查，不进入窗口计数。

| posId | 仓位 | 保护回读 | 方向 / 数量覆盖 | reduce-only / 归属结论 |
|---|---|---|---|---|
| `1001125113096711` | BTC short，5 contracts，均价 81110 | 持仓卡片已验证保护 0；另见 TPSL SL 83000 ×5，order `1001125113096710` | 委托显示 `side-long / TPSL 平空`，数量覆盖 5 | native TPSL 为平仓语义，但缺少 position protection ledger 和 exact-posId 归属；worker 明示“保护归属未验证 / 自动管理已冻结”，因此整体按未知，不宣称已验证 reduce-only/保护完整 |
| `1001125112876816` | BTC short，20 contracts，均价 80811 | TP 77890 ×20；SL 81800 ×20；备份 SL 81963.6 ×全部剩余 | 三条均为 `side-long / TPSL 平空`；TP 和任一 SL 均完整覆盖 20 | 三条均绑定 exact posId 并标为已验证保护；native TPSL / close-position 语义，按 reduce-only 保护确认 |

第二个仓位没有缺失、反向或数量不足。第一个仓位存在一张看似正确的止损，但归属证据不完整；按只读 fail-closed 口径保留 ATTENTION，不人工归属、不处理订单。

### 本轮边界

本轮只追加本日志。未修改计划文档、代码、settings、白名单、词表、阈值、prompt、schema、数据库或业务数据；未 stage、部署、激活、重启服务；未处理、识别或重放消息；未执行 Deepcoin 写入。ATTENTION 仅记录只读证据，没有让 shadow 影响实际决策。

## 2026-09-04 — ATTENTION

> **ATTENTION：本窗口出现 1 条 `shadow_would_skip` 漏失实质管理决策，累计召回从 100% 降为 55/56；主识别实测日均 token 较上次记录增加 56.03%；44/44 个已记录 monitor 周期均不健康且全部含 `stalled_composite_component`；三角色与 monitor 均发生 release/PID 切换；一笔补保护写入的 binding 原始消息早于窗口起点。** Shadow 仍只旁路记录，未改变权威决策；worker 8002 首次 bounded exchange snapshot 完整，当前无持仓。

### 固定窗口

- 起点（开区间）：`2026-09-03T16:02:02Z`，承接本日志上次截止；原始消息边界 `raw_messages.id > 14688`。
- 固定终点：`2026-09-04T16:01:46Z`；时长 23 小时 59 分 44 秒（0.999815 个 24 小时日）。终点在任何生产查询前固定，整轮没有滑动。
- 新消息 218 条，ID `14689–14906`；`created_at` 为 `2026-09-03T16:02:16.602114Z–2026-09-04T16:00:25.955413Z`，没有增量行早于或等于窗口起点。
- 生产 checkout HEAD 为 `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`；固定终点后三角色实际运行 release 已统一为 `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71`，与 checkout HEAD 不同。
- 数据来源：生产 `research.db` 的 `sqlite3 -readonly` / URI `mode=ro` 固定窗口查询，systemd/runtime identity/loop health，monitor journal，以及 worker `http://127.0.0.1:8002` 的 GET-only bounded snapshot 和持仓/当前委托投影。未使用 Web 8000 获取交易所证据。

### 异常优先

1. **Shadow 漏失实质决策：是。** Attempt `4700` / raw `14889` 的第一层为 `非策略 / event_type=none / no_action`，shadow 判定会跳过；权威上下文解析实际产生 `manage_thread / move_stop_to_protect / target_thread_ids=[451] / confidence=0.9`。既有完整解析仍然执行，shadow 未影响实际路径；但该候选判据已不满足零漏失上线门槛。
2. **主识别实测日均 token 变化 >50%：是。** 本窗口为 6.375M token/日，较上次记录的实测下界 4.086M 增加 **56.03%**。本次 usage 可得率 99.53%，上次仅 72.32%，因此这是按要求触发的实测值比较，不宣称为同可得率下的纯流量/模型成本变化。
3. **Monitor 资金安全类 reason：是。** 44/44 个有完整结果的周期均 `healthy=false`，全部含 `stalled_composite_component` 和 `stale_entry_preamble_unresolved`；另有 `adapter_failure`、`audit_abnormal`、`event_recovery_status`、`event_unknown_status`，一次 `monitor_error=state_write_failed`。
4. **PID / SHA 漂移：是。** 上次截止时 Web 为 `5aa7ca07...` / PID 1396631，ingest/worker 为 `0de19c1c...` / PID 3315585/3315574；当前三角色均为 `6a493d15...`，PID 分别为 1338479/1338490/1338473。Monitor 也在 `2026-09-04T12:30:32Z` 前后从 `0de19c1c...` 切到 `6a493d15...`。当前三角色已收敛到同一 verified release。
5. **来源早于窗口的交易所写入：是。** `execution_events.id=3958` 于 `2026-09-03T19:05:05.619225Z` 记录 `create_backup_stop submitted`，其 binding 指向 raw `14675`（`2026-09-03T15:26:48.768304Z`），早于窗口起点约 35 分钟。该 event 是既有仓位的补保护，不是新入场；event 没有直接 source raw，因此仍按 binding 原始消息口径标记 ATTENTION。
6. **当前持仓保护异常：否。** Worker 完整快照中当前持仓为 0，因此保护缺失、方向、数量覆盖和 reduce-only 检查本轮不适用。
7. **交易所快照连续两次不完整：否。** 首次 worker 8002 bounded snapshot 即 `complete=true`，没有重试。
8. **P0 首次达到 500：否。** 该事件已在上次窗口发生；本次固定终点累计为 748/500，超出 248。

### 1. Shadow 生产分歧

- 窗口 context attempts 122，覆盖 118 条消息；shadow 样本 122，一致 75，`shadow_would_trigger=75`，`shadow_would_skip=47`，额外触发 0，计算错误 0。122 条权威 attempt 均未被 shadow 跳过。
- 47 条 would-skip 中 46 条为 `no_action -> no_action`，没有实质改变；1 条为 `no_action -> manage[451]`，是实质漏失。逐条如下：

| attempt / raw | 权威结果 | 第一层 -> 上下文动作族/目标 | 实质改变 |
|---|---|---|---|
| 4589 / 14711 | hold | no_action[] -> no_action[] | 否 |
| 4595 / 14718 | hold | no_action[] -> no_action[] | 否 |
| 4596 / 14721 | hold | no_action[] -> no_action[] | 否 |
| 4597 / 14722 | hold | no_action[] -> no_action[] | 否 |
| 4598 / 14723 | unresolved | no_action[] -> no_action[] | 否 |
| 4601 / 14743 | hold | no_action[] -> no_action[] | 否 |
| 4602 / 14744 | hold | no_action[] -> no_action[] | 否 |
| 4604 / 14752 | hold | no_action[] -> no_action[] | 否 |
| 4605 / 14753 | hold | no_action[] -> no_action[] | 否 |
| 4606 / 14754 | hold | no_action[] -> no_action[] | 否 |
| 4609 / 14758 | hold | no_action[] -> no_action[] | 否 |
| 4611 / 14761 | hold | no_action[] -> no_action[] | 否 |
| 4612 / 14762 | hold | no_action[] -> no_action[] | 否 |
| 4620 / 14769 | hold | no_action[] -> no_action[] | 否 |
| 4621 / 14771 | hold | no_action[] -> no_action[] | 否 |
| 4626 / 14775 | hold | no_action[] -> no_action[] | 否 |
| 4627 / 14776 | hold | no_action[] -> no_action[] | 否 |
| 4634 / 14783 | hold | no_action[] -> no_action[] | 否 |
| 4635 / 14784 | hold | no_action[] -> no_action[] | 否 |
| 4639 / 14789 | hold | no_action[] -> no_action[] | 否 |
| 4640 / 14790 | hold | no_action[] -> no_action[] | 否 |
| 4641 / 14791 | hold | no_action[] -> no_action[] | 否 |
| 4642 / 14792 | hold | no_action[] -> no_action[] | 否 |
| 4643 / 14793 | hold | no_action[] -> no_action[] | 否 |
| 4648 / 14809 | hold | no_action[] -> no_action[] | 否 |
| 4650 / 14814 | hold | no_action[] -> no_action[] | 否 |
| 4651 / 14815 | hold | no_action[] -> no_action[] | 否 |
| 4652 / 14816 | hold | no_action[] -> no_action[] | 否 |
| 4653 / 14817 | hold | no_action[] -> no_action[] | 否 |
| 4654 / 14818 | hold | no_action[] -> no_action[] | 否 |
| 4655 / 14819 | unresolved | no_action[] -> no_action[] | 否 |
| 4656 / 14821 | hold | no_action[] -> no_action[] | 否 |
| 4662 / 14827 | hold | no_action[] -> no_action[] | 否 |
| 4663 / 14828 | hold | no_action[] -> no_action[] | 否 |
| 4664 / 14829 | unresolved | no_action[] -> no_action[] | 否 |
| 4675 / 14844 | unresolved | no_action[] -> no_action[] | 否 |
| 4683 / 14865 | hold | no_action[] -> no_action[] | 否 |
| 4684 / 14871 | hold | no_action[] -> no_action[] | 否 |
| 4685 / 14866 | hold | no_action[] -> no_action[] | 否 |
| 4686 / 14867 | hold | no_action[] -> no_action[] | 否 |
| 4687 / 14868 | hold | no_action[] -> no_action[] | 否 |
| 4688 / 14867 | hold | no_action[] -> no_action[] | 否 |
| 4690 / 14873 | hold | no_action[] -> no_action[] | 否 |
| 4693 / 14881 | hold | no_action[] -> no_action[] | 否 |
| **4700 / 14889** | **manage_thread / move_stop_to_protect** | **no_action[] -> manage[451]** | **是** |
| 4701 / 14893 | hold | no_action[] -> no_action[] | 否 |
| 4702 / 14894 | hold | no_action[] -> no_action[] | 否 |

- 按计划文档的动作族、目标集合和 confidence `<0.7` 不可应用口径复算：累计全历史 shadow 样本 381，56 条实质改变中 55 条被 shadow 保留，召回率 **98.21%（55/56）**，Wilson 95% CI **90.55%–99.68%**。
- 距 500 条 P0 样本停止目标所差样本数为 **0**；但距可上线仍差 **1 个已观测漏失的消除和新 gate 零漏失复验**。继续增加成功样本无法把当前累计召回恢复为 100%，因此“还差多少条即可上线”没有有限数字答案。本轮不改判据、词表或窗口。

### 2. 主识别真实成本

- 窗口内 `mimo_recognition_attempts` 212 行：completed 209（其中 207 行 1 request、2 行 2 requests），`http_error / v1_authoritative_failed` 3（provider request count=0）；共 211 个 provider requests。
- 211 个 provider request 中 210 个 usage `available=true`，真实 usage 可得比例 **99.53%**。可得请求共 prompt 6,023,588，completion 350,724，total **6,374,312** token。
- 每个可得 provider request 的 total token：中位数 **33,075.5**，P90 **40,645.4**，最大 **52,803**，平均 **30,353.87**。
- 211 个实际 provider request 的持久化组件字节合计 33,848,727 B：当前消息文本 35,540 B（**0.105%**）；图片证据 13,321,935 B（**39.357%**）；直接 reply 1,604,970 B（**4.742%**，为 authoritative context 嵌套子集，分区时只扣一次）；其余部分 18,886,282 B（**55.796%**）。
- 按 0.999815 日归一化，主识别实测为 **6.375M token/日**；按窗口 218 条消息为 **29,239.96 token/消息**。单个 usage 缺口使这两个数仍是极接近完整的实测下界。

### 3. 成本对照

- 上下文解析窗口有 122 attempts、123 provider requests，123/123 usage 可得；prompt 3,386,316，completion 167,689，total **3,554,005 token**。日均 **3.555M**，每条窗口消息 **16,302.78 token**。
- 同一窗口的直接实测对照：主识别 6.375M token/日，占两阶段可得 token **64.20%**；上下文解析 3.555M token/日，占 **35.80%**。主识别高约 **2.821M token/日**。
- 从主识别直接 telemetry 起点 `2026-09-01T22:25:52.941030Z` 到本次终点，共 573 条消息：主识别 620 requests、556 usage 可得（89.68%）、15,146,372 token，折合至少 **5.541M/日、26,433.46/消息**；上下文解析 379/379 usage 可得、9,708,469 token，折合 **3.552M/日、16,943.23/消息**。两阶段累计可得 token 占比为 **60.94% / 39.06%**。
- 因此按累计直接测量的绝对量，主识别仍是收益最大的优化侧。但本轮 shadow 已观测到实质漏失，该数字结论不授权缩减 prompt、改触发器或影响权威路径。

### 4. 常规 P0 与运行健康

- 窗口消息 218；context attempts 122，覆盖 118 条消息，provider requests 123。状态：completed 120（121 requests、120 decisions）；exhausted 2（2 requests、2 decisions）。
- 8 个直接持久化触发器（非互斥）：`multiple_same_source_candidates=109`、`entered_holder_language=16`、`management_without_exact_target=8`、`revision_language=7`、`text_image_conflict=6`、`apparent_entry_may_be_revision=3`、`cancellation_language=0`、`reply_target_disagreement=0`。
- 122 条均有可比较 decision，17 条实质改变，描述值 **13.93%**。该窗口仍只是一个流量切片，不用单日比例调整触发器、词表或阈值。
- P0 自 raw `14159` 开始至固定终点累计 **748 条消息**；500 条样本目标已超过 248，不再缺样本数。

当前三角色（固定终点后采样于 `2026-09-04T16:02:45Z` 起）：

| role | release / PID | artifact | entry freeze | 角色与循环健康 |
|---|---|---|---|---|
| web | `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71` / 1338479 | verified | false | event loop=true；当前窗口 max=7256.759 ms，p95=1.658 ms，stall_count=1，watchdog attached；捕获的阻塞标记 `captured_business_blocker` |
| ingest | `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71` / 1338490 | verified | false | event loop/listener/reconcile=true；当前窗口 max=23.498 ms，p95=1.754 ms，stall_count=1，watchdog attached |
| worker | `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71` / 1338473 | verified | false | event loop/command/message processing=true；management、break-even、reconcile、close、TPSL、protection、rescue 均 fresh/successful；global exchange authority=true；当前窗口 max=3125.773 ms，p95=6.217 ms，stall_count=6，watchdog attached |

`auto_trade_enabled=true`，`worker_command_mode=queue`。三角色当前 systemd 单元均 active/running，均未冻结；虽然 release 已统一，但相对上次截止的 PID/SHA 切换仍是本窗口 ATTENTION 证据。

Monitor 从上次截止到固定终点共有 44 个完整结果周期：0 healthy、44 unhealthy，通知 sent 4、suppressed 40。旧 release `0de19c1c...` 有 36 个周期，新 release `6a493d15...` 有 8 个周期，所有 identity 均 `loaded_artifact_verified=true`。Journal 在 `2026-09-03T23:31:21Z–2026-09-04T02:00:31Z` 之间没有完整周期结果，该间隔不解释为健康；当前 timer active/enabled。按相同状态/reason 分组列出每个 UTC 周期：

- `healthy=false, reason_codes=[stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=false`（41）：2026-09-03 `16:31:23, 17:01:19, 17:30:49, 18:01:23, 18:31:35, 19:00:22, 19:31:43, 20:02:13, 20:30:43, 21:01:31, 21:31:52, 22:00:23, 22:31:55, 23:01:46, 23:31:21`；2026-09-04 `03:01:33, 03:31:13, 04:01:08, 04:30:23, 05:01:59, 05:30:25, 06:01:01, 07:00:31, 07:31:59, 08:00:24, 08:32:07, 09:02:04, 09:32:05, 10:01:44, 10:31:29, 11:01:13, 11:31:36, 12:01:15, 12:30:37, 13:01:07, 13:31:38, 14:00:22, 14:31:47, 15:00:41, 15:30:11, 16:00:21`。
- `healthy=false, reason_codes=[adapter_failure, stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=state_write_failed, audit_ran=true`（1）：2026-09-04 `02:00:31`。
- `healthy=false, reason_codes=[audit_abnormal, stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=true`（1）：2026-09-04 `02:32:22`。
- `healthy=false, reason_codes=[event_recovery_status, event_unknown_status, stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=false`（1）：2026-09-04 `06:31:48`。

窗口 execution events 中实际 `submitted` 写入 7 条：`create_trigger_entry=4`、`create_backup_stop=3`；另有 `auto_trade_skipped=6`、`source_message_deletion_outcome reconciling=3 / succeeded=2 / recovery_required=1`。7 条 submitted 中 6 条的直接或 binding 原始消息在窗口内；仅 event `3958` 的 binding 原始消息 raw `14675` 早于窗口，已在异常项列出。

### 5. 交易所只读快照（worker 8002）

- 固定终点后 `2026-09-04T16:02:45Z–16:02:52Z` 采样。首次 bounded snapshot：`complete=true`，持仓 0，普通挂单 0，fingerprint `e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`；未触发第二次重试。
- `2026-09-04T16:02:52.162747Z` 当前委托 GET 投影 `loaded=true`，共 5 条触发单：BTC=1（待入场空单）、ETH=2（待入场多单）、SOL=2（待入场多单）；普通挂单=0。
- 当前无任何持仓，因此本轮没有需要逐仓验证的保护单，也没有保护缺失、反向、数量不覆盖或非 reduce-only 异常。

### 本轮边界

本轮只追加本日志。未修改计划文档、代码、settings、白名单、词表、阈值、prompt、schema、数据库或业务数据；未 stage、部署、激活、重启服务；未处理、识别或重放消息；未执行 Deepcoin 写入。ATTENTION 仅记录只读证据，没有让 shadow 影响实际决策。

## 2026-09-05 — ATTENTION

> **ATTENTION：48 个 monitor 周期中 29 个含资金安全相关 `stalled_composite_component`，窗口内 monitor 先后运行两个 release，三角色相对上次截止也发生 release / PID 切换；主识别实测日均 token 较上次下降 56.65%。** Shadow 判据仍只旁路记录，本窗口 19 条 `shadow_would_skip` 均未实质改变第一层结论；worker 8002 首次快照完整，当前 1 个 BTC 多仓的止损方向、数量覆盖、exact-posId 归属和原生平仓语义均正确。

### 固定窗口

- 起点（开区间）：`2026-09-04T16:01:46Z`，承接本日志上次截止；原始消息边界 `raw_messages.id > 14906`。
- 固定终点：`2026-09-05T16:01:32Z`；时长 23 小时 59 分 46 秒（0.999838 个 24 小时日）。终点在任何生产查询前固定，整轮没有滑动。
- 新消息 102 条，ID `14907–15008`；`created_at` 为 `2026-09-04T16:22:30.516233Z–2026-09-05T14:36:06.217395Z`，没有增量行早于或等于窗口起点。
- 生产 checkout HEAD 为 `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`；固定终点后三角色实际运行 release 均为 `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，与 checkout HEAD 不同。
- 数据来源：生产 `research.db` 的 `sqlite3 -readonly` 固定窗口查询、systemd/runtime identity/loop health、monitor journal，以及 worker `http://127.0.0.1:8002` 的 GET-only bounded snapshot、持仓和当前委托投影。未使用 Web 8000 获取交易所证据。

### 异常优先

1. **Monitor 资金安全类 reason：是。** 48 个周期中 29 个含 `stalled_composite_component`，其中 28 个同时含 `stale_entry_preamble_unresolved`，1 个还含 `audit_abnormal`；另有 1 个周期同时含 `event_unknown_status`。该 reason 在 `2026-09-05T03:30:11Z` 后未再出现，但后续仍有 1 个 `event_unknown_status` 周期和 8 个仅含 `stale_entry_preamble_unresolved` 的不健康周期，不能把中间 10 个健康周期外推为整窗健康。
2. **PID / SHA 漂移：是。** 上次截止三角色为 `6a493d15...` / PID 1338479、1338490、1338473；当前三角色均为 `9501a5f3...` / PID 1525321、1525328、1525316。Monitor 在本窗口先加载 `877fbc33...` 12 次，再加载 `9501a5f3...` 36 次；48 次 artifact identity 均 verified。
3. **主识别实测日均 token 变化 >50%：是。** 本窗口为 2.764M token/日，较上次 6.375M token/日下降 **56.65%**。本次 102/102 provider requests 的 usage 均可得；这是实测窗口对比，但单日流量与图片构成会波动，不把下降直接解释为模型或 prompt 优化结果。
4. **Shadow 改变实际决策：否。** 19 条 `shadow_would_skip` 均仍执行权威上下文解析，结果全为 `hold`，动作族和目标集合均未实质变化。历史累计仍保留上一窗口已出现的 1 条实质漏失，因此候选判据仍不满足零漏失上线门槛。
5. **来源消息早于窗口的交易所写入：否。** 窗口内 3 条 `submitted` 均直接且通过 binding 指向 raw `14962`（`2026-09-05T03:04:54.150026Z`），位于固定窗口内。
6. **当前持仓保护异常：否。** 当前 BTC 多仓 6 contracts 的唯一止损为 77500、平多方向、覆盖全部剩余仓位，并由 protection ledger 验证绑定 exact posId；worker 投影未显示缺失、反向、数量不足或非平仓语义。
7. **交易所快照连续两次不完整：否。** 首次 worker 8002 bounded snapshot 即 `complete=true`，没有重试。
8. **P0 首次达到 500：否。** 该事件已在更早窗口发生；本次固定终点累计为 850/500，超过目标 350 条。

### 1. Shadow 生产分歧

- 窗口 context attempts 46，shadow 样本 46；一致 27，`shadow_would_trigger=27`，`shadow_would_skip=19`，额外触发 0，计算错误 0。46 条权威 attempt 均未被 shadow 跳过。
- 19 条 would-skip 的权威结果均为 `hold`，第一层与上下文动作族均为 `no_action`，目标集合均为空，逐条如下：

| attempt / raw | 权威结果 | 第一层 -> 上下文动作族/目标 | 实质改变 |
|---|---|---|---|
| 4710 / 14915 | hold | no_action[] -> no_action[] | 否 |
| 4711 / 14916 | hold | no_action[] -> no_action[] | 否 |
| 4713 / 14918 | hold | no_action[] -> no_action[] | 否 |
| 4716 / 14920 | hold | no_action[] -> no_action[] | 否 |
| 4721 / 14942 | hold | no_action[] -> no_action[] | 否 |
| 4722 / 14943 | hold | no_action[] -> no_action[] | 否 |
| 4723 / 14945 | hold | no_action[] -> no_action[] | 否 |
| 4724 / 14947 | hold | no_action[] -> no_action[] | 否 |
| 4727 / 14953 | hold | no_action[] -> no_action[] | 否 |
| 4728 / 14954 | hold | no_action[] -> no_action[] | 否 |
| 4730 / 14959 | hold | no_action[] -> no_action[] | 否 |
| 4733 / 14966 | hold | no_action[] -> no_action[] | 否 |
| 4738 / 14978 | hold | no_action[] -> no_action[] | 否 |
| 4740 / 14984 | hold | no_action[] -> no_action[] | 否 |
| 4747 / 14997 | hold | no_action[] -> no_action[] | 否 |
| 4748 / 14998 | hold | no_action[] -> no_action[] | 否 |
| 4749 / 14999 | hold | no_action[] -> no_action[] | 否 |
| 4750 / 15000 | hold | no_action[] -> no_action[] | 否 |
| 4751 / 15001 | hold | no_action[] -> no_action[] | 否 |

- 按计划文档的动作族、目标集合和 confidence `<0.7` 不可应用口径复算：累计全历史 shadow 样本 427，416 条有可比较 decision；61 条实质改变中 60 条被 shadow 保留，召回率 **98.36%（60/61）**，Wilson 95% CI **91.28%–99.71%**。
- 距 500 条 P0 消息停止目标所差样本数为 **0**。距可上线仍没有有限的“再增加多少成功样本”答案：历史累计已有 1 条漏失，必须先形成新 gate 并完成零漏失复验；本轮不改判据、词表或窗口。

### 2. 主识别真实成本

- 窗口内 `mimo_recognition_attempts` 103 行：completed 102，均各发出 1 个 provider request；另 1 行 `http_error / v1_authoritative_failed` 在 provider request 前失败。实际 provider requests 共 102。
- 102 个 provider request 的 usage 全部 `available=true`，真实 usage 可得比例 **100%**。Prompt 2,603,255，completion 160,112，total **2,763,367 token**。
- 每个 provider request 的 total token：中位数 **33,302**，P90 **39,415.4**，最大 **40,515**，平均 **27,091.83**。
- 102 个实际请求的持久化组件字节合计 17,728,985 B：当前消息文本 13,739 B（**0.077%**）；图片证据 8,835,185 B（**49.835%**）；直接 reply 593,376 B（**3.347%**，为 authoritative context 的嵌套子集，分区时只扣一次）；其余部分 8,286,685 B（**46.741%**）。
- 按 0.999838 日归一化，主识别实测为 **2.764M token/日**；按窗口 102 条消息为 **27,091.83 token/消息**。

### 3. 成本对照

- 上下文解析窗口有 46 attempts、46 provider requests，46/46 usage 可得；prompt 1,042,939，completion 50,376，total **1,093,315 token**。日均 **1.093M**，每条窗口消息 **10,718.77 token**。
- 同一窗口直接实测：主识别占两阶段 token **71.65%**，上下文解析占 **28.35%**；主识别高约 **1.670M token/日**。
- 从主识别直接 telemetry 起点 `2026-09-01T22:25:52.941030Z` 到本次终点的共同累计口径，共 675 条消息：主识别 721 requests、657 usage 可得（91.12%）、17,870,254 token，折合至少 **4.787M/日、26,474.45/消息**；上下文解析 424/424 usage 可得、10,792,162 token，折合 **2.891M/日、15,988.39/消息**。两阶段累计可得 token 占比为 **62.35% / 37.65%**。
- 因此按累计直接测量绝对量，主识别仍是收益最大的优化侧。但累计主识别仍有 64 个 usage 缺口，且 shadow 历史已有实质漏失；该数字结论不授权缩减 prompt、改触发器或影响权威路径。

### 4. 常规 P0 与运行健康

- 窗口消息 102；context attempts 46，覆盖 46 条消息，provider requests 46；全部 completed 且均有 decision。
- 8 个直接持久化触发器（非互斥）：`multiple_same_source_candidates=42`、`entered_holder_language=5`、`revision_language=3`、`management_without_exact_target=3`、`cancellation_language=1`、`text_image_conflict=1`、`reply_target_disagreement=0`、`apparent_entry_may_be_revision=0`。
- 46 条可比较 decision 中 5 条实质改变，描述值 **10.87%**。该窗口只描述当日流量，不用于调整触发器、词表或阈值。
- P0 自 raw `14159` 开始至固定终点累计 **850 条消息**；500 条样本目标已超过 350，不再缺样本数。

当前三角色（固定终点后采样于 `2026-09-05T16:02:21Z` 起）：

| role | release / PID | artifact | entry freeze | 角色与循环健康 |
|---|---|---|---|---|
| web | `9501a5f39f0c5f196cc29f24f3e3b8786267126b` / 1525321 | verified | false | event loop=true；当前窗口 max=7340.057 ms，p95=1.677 ms，stall_count=3，watchdog attached |
| ingest | `9501a5f39f0c5f196cc29f24f3e3b8786267126b` / 1525328 | verified | false | event loop/listener/reconcile=true；当前窗口 max=59.192 ms，p95=1.719 ms，stall_count=3，watchdog attached |
| worker | `9501a5f39f0c5f196cc29f24f3e3b8786267126b` / 1525316 | verified | false | event loop/command/message processing=true；management、break-even、reconcile、close、TPSL、protection、rescue 均 fresh/successful；global exchange authority=true；当前窗口 max=3270.655 ms，p95=6.243 ms，stall_count=7，chat cap=3 / active=0 / peak=2，watchdog attached |

`auto_trade_enabled=true`，`worker_command_mode=queue`。三角色 systemd 单元均 active/running、`NRestarts=0`，当前均未冻结；相对上次截止的 release/PID 切换仍是本窗口 ATTENTION 证据。

Monitor 从上次截止到固定终点共有 48 个完整周期：10 healthy、38 unhealthy；通知 sent 4、suppressed 34、not_needed 10。旧 release `877fbc33...` 有 12 个周期（`16:31:29Z–22:00:33Z`），新 release `9501a5f3...` 有 36 个周期（`22:31:55Z–15:30:07Z`）；所有 identity 均 `loaded_artifact_verified=true`。按相同状态/reason 分组列出每个 UTC 周期：

- `healthy=false, reason_codes=[stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=false`（27）：2026-09-04 `16:31:35, 17:00:40, 17:31:55, 18:00:21, 18:30:33, 19:02:03, 19:30:43, 20:00:30, 20:32:04, 21:00:39, 21:30:59, 22:00:38, 22:32:01, 23:01:49, 23:31:28`；2026-09-05 `00:01:40, 00:30:06, 01:31:39, 02:02:06, 02:31:04, 03:01:24, 04:00:58, 04:31:41, 05:01:32, 05:31:19, 06:01:39, 06:31:27`。
- `healthy=false, reason_codes=[audit_abnormal, stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=true`（1）：2026-09-05 `01:03:53`。
- `healthy=false, reason_codes=[event_unknown_status, stale_entry_preamble_unresolved, stalled_composite_component], monitor_error=null, audit_ran=false`（1）：2026-09-05 `03:30:11`。
- `healthy=true, reason_codes=[], monitor_error=null, audit_ran=false`（10）：2026-09-05 `06:46:08, 07:01:36, 07:31:31, 08:31:47, 09:02:06, 09:31:26, 10:00:22, 10:31:38, 11:00:58, 11:30:24`。
- `healthy=false, reason_codes=[event_unknown_status], monitor_error=null, audit_ran=false`（1）：2026-09-05 `08:01:50`。
- `healthy=false, reason_codes=[stale_entry_preamble_unresolved], monitor_error=null, audit_ran=false`（8）：2026-09-05 `12:01:15, 12:31:04, 13:01:19, 13:30:51, 14:01:07, 14:30:06, 15:00:15, 15:30:12`。

窗口 execution events 中实际 `submitted` 写入 3 条：`create_trigger_entry=1`、`open_market_position=1`、`set_position_tpsl=1`；另有 `auto_trade_skipped=4`、`entry_price_geometry_rejected / manual_review=2`、`management_history_recovery / resolved=1`。3 条 submitted 均直接且通过 binding 指向窗口内 raw `14962`，没有来源早于窗口起点的交易所写入。

### 5. 交易所只读快照（worker 8002）

- 固定终点后 `2026-09-05T16:02:22Z–16:02:23Z` 采样。首次 bounded snapshot：`complete=true`，持仓 1，普通挂单 0，fingerprint `eb64273c29d5b075d6e162c4f752cb1f931d9f273c5e31e574ec2ea2b218de8d`；未触发第二次重试。
- `2026-09-05T16:02:23.361175Z` 当前委托 GET 投影 `loaded=true`，共 7 条触发单：BTC=3（2 条待入场触发单、1 条已验证止损）、ETH=2、SOL=2；普通挂单=0。

| posId | 仓位 | 保护回读 | 方向 / 数量覆盖 | reduce-only / 归属结论 |
|---|---|---|---|---|
| `1001125135694798` | BTC long，6 contracts，均价 79519 | SL 77500 ×全部剩余仓位（当前 6 contracts / 0.006 BTC），order `1001125135694875` | `止盈止损/平多`，方向正确并完整覆盖当前数量 | exact-posId protection ledger 已验证；Deepcoin native TPSL / close-position 语义，worker 投影不另暴露名为 `reduceOnly` 的字面布尔字段 |

当前已配置的唯一保护是全量止损；worker 未显示保护缺失、反向、数量不足或无法归属。未人工归属订单，未执行任何 exchange 写入。

### 本轮边界

本轮只追加本日志。未修改计划文档、代码、settings、白名单、词表、阈值、prompt、schema、数据库或业务数据；未 stage、部署、激活、重启服务；未处理、识别或重放消息；未执行 Deepcoin 写入。ATTENTION 仅记录只读证据，没有让 shadow 影响实际决策。

## 2026-09-06 — ATTENTION

> **ATTENTION：固定窗口内 monitor 24/24 不健康；固定终点后读取的 web / ingest / worker 三角色均为 `loaded_artifact_verified=false`、无 release SHA，且 monitor timer 当前 disabled/inactive。窗口内另有 5 条交易所 `submitted` 写入绑定到窗口起点前的来源消息。** Shadow 仍只旁路记录，34 条 `shadow_would_skip` 均未实质改变第一层结论；worker 8002 两次只读 GET 均给出完整快照，当前 ETH 多仓保护方向、覆盖和 exact-posId 归属正确。

### 固定窗口

- 起点（开区间）：`2026-09-05T16:01:32Z`，承接上次记录的窗口截止；原始消息边界 `raw_messages.id > 15008`。
- 固定终点：`2026-09-06T16:02:10Z`；时长 24 小时 38 秒（**1.00044** 个 24 小时日）。终点在任何生产查询前固定，整轮没有滑动。
- 新消息 137 条，ID `15009–15145`；`created_at` 为 `2026-09-05T16:02:53.045375Z–2026-09-06T15:21:42.518881Z`，没有增量行早于或等于窗口起点。
- 生产 checkout 在固定终点后读取为 `0371fc9f4fc41c588fab1534f8e33419aef4d6cf`。数据来自生产 `research.db` 的 SQLite URI `mode=ro` / `PRAGMA query_only=ON`、systemd/runtime identity/loop health、monitor journal，以及 worker `http://127.0.0.1:8002` 的 GET-only 快照和仓位/当前委托投影；未使用 Web 8000 获取交易所证据。

### 异常优先

1. **Monitor 资金安全相关 reason：是。** 固定窗口内 24 个完整周期均 `healthy=false`：22 个 `stale_entry_preamble_unresolved`，1 个 `audit_abnormal + stale_entry_preamble_unresolved`，1 个 `event_unknown_status`；通知 3 次 sent、21 次 suppressed。
2. **PID / SHA 漂移与加载身份：是。** 上次记录的三角色为 release `9501a5f3...`；固定终点后采样的 web / ingest / worker 分别为 PID `2314171` / `2314197` / `2319390`，三个端点均返回 `release_commit=null`、`manifest_sha256=null`、`loaded_artifact_verified=false`。这不是可验证的部署身份，按未知/不通过处理。窗口 monitor 先后记录 `9501a5f3...` 1 次、`af8676dc...` 23 次（均在当时 verified）；不能用这些历史 monitor 身份替代当前角色的未验证状态。
3. **来源消息早于窗口的交易所写入：是。** 10 条 `submitted` 中有 5 条 `cancel_trigger_entry` 的 binding 来源分别为 raw `14591`（ETH，两条）、`14592`（SOL，两条）和 `14785`（BTC，一条），均早于本窗口起点；另有 `create_backup_stop`、一条 BTC cancel、ETH 开仓/两道保护合计 5 条绑定 raw `14962` 或 `15144`。这里只记录时间关系，不推断重放或人工处理。
4. **Shadow 改变实际决策：否。** 34 条 `shadow_would_skip` 全部仍执行权威解析，第一层与上下文结果均为 `no_action[]`；累计仍保留 1 条历史实质漏失。
5. **持仓保护异常：否。** 当前 ETH 多仓的两道保护均为平多、全量覆盖、已验证 exact-posId 归属；未见缺失、反向、数量不足或非平仓语义。
6. **交易所快照连续两次不完整：否。** worker 8002 bounded snapshot 首次 `complete=true`；后续 positions/open-orders GET 投影也正常，故未作重试。
7. **P0 首次达到 500：否。** 已在早前窗口发生；本次固定终点累计为 987/500，超过目标 487 条。
8. **主识别日均 token 变化超过 50%：否。** 本窗口实测 **3.867M token/日**，相对上次记录的 2.764M/日增加 39.2%，未触发该阈值。

### 1. Shadow 生产分歧

- 窗口 context attempts 71，shadow 样本 71；一致 / `shadow_would_trigger` 37，`shadow_would_skip` 34，计算错误 0。71 条权威 attempt 均未被 shadow 跳过。
- 34 条 would-skip 均为以下同一结论：权威 `hold` 或 `unresolved`；第一层 `no_action[] -> no_action[]`；**未实质改变**：`4756/15012`、`4758/15018`、`4761/15023`、`4766/15028`、`4767/15029`、`4768/15030`、`4769/15031`、`4772/15039`、`4777/15055`、`4779/15058`、`4781/15060`、`4782/15061`、`4783/15062`、`4784/15063`、`4785/15064`、`4786/15065`、`4789/15090`、`4791/15092`、`4792/15093`、`4796/15101`、`4797/15102`、`4800/15105`、`4801/15106`、`4802/15108`、`4803/15110`、`4809/15121`、`4810/15122`、`4812/15126`、`4813/15129`、`4814/15132`、`4816/15134`、`4817/15135`、`4820/15139`、`4826/15145`。
- 以动作族、目标集合及 confidence `<0.7` 不可应用口径复算，累计全历史 shadow 样本 498；80 条实质改变中 79 条被 shadow 保留，召回率 **98.75%（79/80）**，Wilson 95% CI **93.25%–99.78%**。
- 距 500 条 P0 消息停止目标所差样本数为 0。距可上线仍无有限“成功样本数”答案：已有 1 条漏失，须先形成新 gate 并完成零漏失复验；本轮不改判据、词表或窗口。

### 2. 主识别真实成本

- 窗口内 `mimo_recognition_attempts` 158 行、provider request count 185；其中 126 条 request 有真实 usage，**可得比例 68.11%**。可得 request 共 prompt 3,664,006、completion 204,505、total **3,868,511 token**。
- 可得 request 的 total token：中位数 **32,223**，P90 **38,567.5**，最大 **39,293**，平均 **30,702.47**。
- 持久化组件字节合计 25,419,683 B：当前消息文本 23,049 B（**0.091%**）；图片证据 10,121,312 B（**39.817%**）；直接 reply 1,066,184 B（**4.194%**，属于 authoritative context 嵌套子集，分区时只扣一次）；其余部分 15,275,322 B（**60.092%**）。
- 按 1.00044 日归一化，主识别实测下界为 **3.867M token/日**；按 137 条窗口消息为 **28,237.31 token/消息**。59 个 usage 缺口意味着日均和单消息数只能作为已观测下界。

### 3. 成本对照

- 上下文解析窗口有 71 attempts / 71 provider requests，71/71 usage 可得；prompt 1,684,193，completion 82,444，total **1,766,637 token**，日均 **1.766M**，按窗口消息 **12,895.16 token/消息**（按 context attempt 为 24,882.21）。
- 同一窗口直接实测下界：主识别占两阶段可得 token **68.65%**，上下文解析 **31.35%**；主识别高约 **2.101M token/日**。
- 自主识别 telemetry 起点 `2026-09-01T22:25:52.941030Z` 至本次终点，共 812 条消息：主识别 906 requests、783 usage 可得（86.42%）、21,738,765 token，折合至少 **4.592M/日、26,771.88/消息**；上下文解析 495/495 usage 可得、12,558,799 token，折合 **2.653M/日、15,466.50/消息**。累计两阶段可得 token 占比 **63.38% / 36.62%**。
- 因而在累计直接测量的绝对量下，主识别仍是收益最大的优化侧；但 usage 不完整且 shadow 历史已有实质漏失，这一成本事实不授权收缩 prompt、调整触发器或影响权威路径。

### 4. 常规 P0 与运行健康

- 窗口消息 137；context attempts 71，覆盖 71 条消息，全部 `completed` 且均有 decision；可比较决策 71 条，9 条实质改变，描述值 **12.68%**。
- 8 个直接持久化触发器（非互斥）：`multiple_same_source_candidates=70`、`entered_holder_language=10`、`management_without_exact_target=8`、`revision_language=2`、`text_image_conflict=1`、`apparent_entry_may_be_revision=1`、`cancellation_language=1`、`reply_target_disagreement=0`。
- P0 自 raw `14159` 至固定终点累计 **987 条消息**；500 条目标已超过 487。
- 固定终点后采样：三个业务 unit 都为 `active/running`、`NRestarts=0`；web / ingest / worker event-loop 分别 `true`、`true`、`true`，p95 分别 1.607 / 1.730 / 6.190 ms，当前窗口 stall_count 均为 0。ingest listener/reconcile=true；worker command/message-processing/deepcoin-private-ws=true，管理、break-even、reconcile、close、TPSL、protection、rescue循环均 fresh/successful。
- `auto_trade_enabled=true`、`worker_command_mode=queue`，但 worker identity 的 `global_exchange_authority=false` 且所有 loaded artifact identity 均未验证；不以 API 配置值覆盖该身份缺口。monitor timer 当前 `disabled/inactive`，这是固定终点后新鲜状态，未回推成窗口内的零周期。
- Monitor 固定窗口 24 个周期：全数 unhealthy；reason code 分布见异常项。它不是正常运行健康的证据。

### 5. 交易所只读快照（worker 8002）

- 固定终点后 `2026-09-06T16:05:12Z–16:05:36Z` 采样，worker `read-only-exchange-snapshot` 首次为 `complete=true`、持仓 1、普通挂单 0、fingerprint `283091021fc8391834efb3c2b49c968fd576940d4a8d01b91c3c287a4b79d70b`；未触发第二次重试。
- worker `positions-panel` / `open-orders` 的 GET-only 投影：持仓为 ETH 1；普通挂单 0；BTC 触发单 0、ETH 触发单 2（均为保护单）、SOL 触发单 0。

| posId | 仓位 | 保护回读 | 方向 / 数量覆盖 | reduce-only / 归属结论 |
|---|---|---|---|---|
| `1001125157891231` | ETH long，3.1 contracts，均价 2477.46 | SL 2430（`1001125157891310`）及 backup SL 2425.14（`1001125157893804`） | 两条均“止盈止损/平多”，均显示全部剩余仓位（当前 3.1 contracts / 0.31 ETH） | 两条均 exact-posId protection ledger 已验证；Deepcoin native TPSL / close-position 语义，投影不暴露名为 `reduceOnly` 的字面布尔字段 |

当前没有保护缺失、反向、数量不覆盖或无法归属的证据。未人工归属订单，未执行任何 exchange 写入。

### 本轮边界

本轮只追加本日志。未修改计划文档、代码、settings、白名单、词表、阈值、prompt、schema、数据库或业务数据；未 stage、部署、激活、重启服务；未处理、识别或重放消息；未执行 Deepcoin 写入。ATTENTION 仅记录只读证据，没有让 shadow 影响实际决策。
