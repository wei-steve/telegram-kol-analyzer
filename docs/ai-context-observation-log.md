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
