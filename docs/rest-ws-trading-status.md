# Deepcoin REST + WebSocket 交易改造状态

把 Deepcoin 的下单、成交确认、仓位归属与止盈止损归属，从"推断式候选匹配"改造成
"REST 精确核验 + WebSocket 低延迟唤醒"的确定性链路。本文件是跨会话唯一的进度真相；
新会话只读本文件，再打开 `current_phase_file` 指向的那一份阶段文件，不要读其他阶段文件。

```yaml
project: deepcoin-rest-ws-trading
plan_index: docs/plans/2026-09-06-deepcoin-rest-ws/README.md
design_base: docs/2026-09-05-deepcoin-rest-ws-trading-handoff.md
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，执行会话完成后必须 send_message 到这里
brain_session_title: 自动项目多线程迁移后的代码清理
integration_branch: codex/deepcoin-auto-trading-v1               # 本地集成分支；阶段完成后由指挥会话合并
design_branch: rest-ws/phase-0-design
production_modes: "runtime roles web/ingest/worker (systemd x3); message_pipeline_mode=queue; worker_command_mode=queue; auto_trade_enabled=true; monitor timer 已停用；部署走 tg-deploy <sha>"
current_phase: 6
current_phase_file: docs/plans/2026-09-06-deepcoin-rest-ws/phase-6-protection-authority.md
phase_status: in_progress             # planned | claimed | in_progress | completed | blocked
                                      # 阶段 5 已完成：迁移本体 7a4d852a 于 2026-09-08T04:34Z 上线，
                                      # 2026-09-09T03:21Z 第一笔真实入场逐笔核对通过（市价腿 + 限价腿同时出现）。
                                      # 七个前置 6-pre-1..7 全部完成并上线（最后一个 6-pre-4，2026-09-10T09:07Z）。
                                      # 阶段 6 改交易所写入语义，**需用户单独批准后才能领取**——
                                      # 在用户明确批准前，任何会话不得把 phase_status 改成 claimed。
claimed_by: local_4a6676b0-cf9c-4971-916e-37048cac1b40   # 阶段 6 执行会话（B 线）
last_completed_phase: 5
last_completed_commit: 7a4d852a31708515aa92e58313a941c206f5637c
user_approval_required_for: [1, 2, 4, 5, 6]   # 见"用户批准门"
```

## 阶段总览

| 阶段 | 名称 | 风险等级 | 是否改交易所写入语义 | 需用户单独批准 |
|---|---|---|---|---|
| 1 | worker 内私有 WebSocket 采集，只落原始事件 | **L3**（新表 + 新依赖） | 否 | 是（schema） |
| 2 | 去重、乱序保护、心跳、断线状态机与 REST 重同步 | **L2**（若加列则该提交按 L3 处理） | 否 | 是（若加列） |
| 3 | WebSocket 事件只唤醒现有 REST reconciliation | **L2** | 否 | 否 |
| 4 | 影子构建绑定链并与现有保护账本逐笔比对 | **L3**（新影子表）；行为面 L1 | 否 | 是（schema） |
| 5a | list_open_orders 切 V2 orders-pending（阶段 5 前置） | L3 | 否 | 是 |
| 5b | Deepcoin 读限流与 50000 识别（阶段 5 前置） | L2 | 否 | 否 |
| 5 | 普通市价/限价入场迁到 order 并由新绑定驱动 | **L3** | **是** | **是（强制）** |
| 6 | 新绑定驱动 TPSL 修改、撤销与平仓 | **L3** | **是** | **是（强制）** |

风险等级按 `AGENTS.md` 的 Risk-Adaptive Verification（L0–L3）判定，每份阶段文件里的
"验证等级与具体检查项"是该阶段的最终依据，本表只作索引。

## 执行会话的领取协议

1. 在新建会话中先读 `AGENTS.md`，再读 `docs/ARCHITECTURE.md`，再读本文件，再只读 `current_phase_file`。
2. 确认 `phase_status` 为 `planned`。若为 `claimed` / `in_progress` / `blocked`，停止并告知用户。
3. 若本阶段在 `user_approval_required_for` 里，先取得用户在本轮对话中的明确批准，再领取。
   批准必须针对本阶段，不能引用其他阶段的批准。
4. 把 `phase_status` 改为 `claimed`，`claimed_by` 填本会话 ID（用 `get_session self` 取），单独提交这一个文件。
5. 开始改代码前把 `phase_status` 改为 `in_progress`。
6. 完成后按阶段文件的"完成条件"更新本文件（`completed`、`last_completed_phase`、
   `last_completed_commit`、在下方证据区追加记录），把 `current_phase` 推进到下一阶段并
   填好 `current_phase_file`，`phase_status` 回到 `planned`，`claimed_by` 置空。
7. 用 `mcp__ccd_session_mgmt__send_message` 把摘要发给 `brain_session_id`。
8. 全程遵守 AGENTS.md：不用 `git add -A`，只暂存明确路径；未经批准不 push、不部署。

一个用户轮次只做一个阶段。阶段内的常规步骤不需要反复确认；超出该阶段范围就停下来。

## 硬性禁止（所有阶段）

以下每一条都对应一次已实证的失败，不是保守惯例。

1. **任何阶段不得用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag
   单独认领归属。** 实验中 TPSL ordId 恰为入场 ordId 减 1、创建毫秒相同，这是分配模式
   不是外键；`GET /trade/order` 详情会把未提交的 clOrdId 回填成系统 ordId。
2. **REST 写入超时或响应不完整一律记 `unknown_exchange_outcome`，绝不自动重发。**
   `DeepcoinRestClient._request` 已把 POST 的网络/状态/JSON 失败映射为
   `DeepcoinRequestOutcomeUnknown`，新代码必须沿用，不得降级为普通异常后重试。
3. **外层 `code=0` 不等于成功。** 必须逐条检查 `data[].sCode`；
   `_raise_for_deepcoin_business_error` 是唯一正确入口。binding 338 与
   DuplicateAction 两次都栽在这里。
4. **WebSocket 断线不得解释为无订单或无仓位。** 断线只能产生 `disconnected` /
   `resyncing` 状态与"未知"，永远不能产生"零"。
5. **WebSocket 事件不得成为真相来源。** 推送只能落库并唤醒针对性 REST 核验，
   核验通过才写账本。回调里禁止任何交易所写入。
6. **WebSocket 事件必须允许重复与乱序；旧状态不得覆盖新状态。**
7. **无唯一 posId 或无 `TU == posId` 时，保护保持 `unverified`**，禁止自动修改、
   撤销或认领。
8. **每个改变交易所写入语义的阶段（当前为 5、6）必须由用户单独批准后才能领取。**
   批准是针对该阶段的，不可跨阶段复用，也不可由"已批准上一阶段"推导。
9. WebSocket 连接只能活在 `worker` 角色内（凭据隔离：Deepcoin 密钥仅在 worker）。
   `web` 角色没有执行权限，任何交易所写入必须经 `worker_command_jobs` 的四条命令。
10. 不新增进程内全局锁来做跨进程互斥（三进程拓扑下无效）；跨进程排他一律走数据库状态。
11. 不重新引入 `inline` / `shadow` 模式开关来做灰度（清理方案已删除该模式，见
    `docs/ARCHITECTURE.md` 第 6 节）。影子期用独立影子表 + 独立读路径，不用模式开关。

12. **断网、断线或进程重启后必须重新对齐（用户 2026-09-06 明确要求）。** 任何阶段的
    WebSocket 设计都必须假定会丢帧：重连或重启后先用 REST 重建订单、成交、持仓、TPSL
    快照，重放本地未处理的收件箱事件，再做一次 REST 快照覆盖"首次快照到订阅成功"之间的
    竞态窗口，全部收敛后才把连接状态改回 `healthy`。缺口期间不得开放任何依赖 WS 事件
    的决定；缺口本身要留下可查的记录（起止时间、水位）。阶段 1 只需记录缺口，阶段 2
    实现完整重同步，阶段 3 起每个阶段的验证都必须包含一次人为断线与一次重启。

## 用户批准门

| 阶段 | 为什么需要单独批准 |
|---|---|
| 1 | 新增数据库表 + 新增运行时依赖 `websockets`，属 L3 schema 变更，需在生产库副本上演练 |
| 2 | 若引入新列或新表则同上；若纯代码则可按 L2 直接领取 |
| 4 | 新增影子表，属 L3 schema 变更 |
| 5 | 改变入场的交易所写入语义（trigger-order → order） |
| 6 | 改变保护的修改/撤销/平仓写入语义 |

## 阶段 0 只读核对结论（2026-09-06）

以下全部为只读观测，没有下单、改单、撤单、配置修改、部署或重启。

### 1. 交接文档里的实验空仓已不存在

`GET /api/runtime-agent/read-only-exchange-snapshot`（worker 8002，localhost only）返回
`complete=true, position_count=0, open_order_count=0`。直接用 worker 凭据只读复查
`list_positions()` / `list_open_orders()` 同样为空数组。

**posId `1001125145471184` 已不在交易所。** 交接文档"历史仓位提示"一节的
2026-09-05 19:26:43 UTC 快照已过期，按其自身要求作废。

### 2. 但交易所上有三张历史 pending 条件入场单，且当前正在全局否决止盈收敛

`list_trigger_orders_pending` 返回三行，全部是 `triggerOrderType=Conditional` 的
**未触发入场单**（不是 TPSL），带 125 倍杠杆与嵌入式止损：

| instId | ordId | side/posSide | sz | triggerPx | closeSLTriggerPrice | cTime |
|---|---|---|---|---|---|---|
| ETH-USDT-SWAP | 1001125109770664 | buy / long | 1.8 | 2329 | 2280 | 1788433332000 |
| ETH-USDT-SWAP | 1001125109770668 | buy / long | 1.8 | 2312 | 2280 | 1788433332000 |
| BTC-USDT-SWAP | 1001125122023458 | sell / short | 24 | 76410 | 76000 | 1788503485000 |

把这三行原样喂给当前生产判据（本机导入 `src` 只读求值，无网络、无写入）：

```text
_row_has_protection_fields(row)        = True   （closeSLTriggerPrice 非空）
_native_tpsl_aliases_consistent(row)   = False  （protection_order_sides_consistent = False）
=> 三行都满足 trigger_take_profit_convergence_executor.py:506-511 的全局否决条件
```

不一致的具体位置：`native_tpsl.protection_order_sides_consistent` 要求 `side` 与
`posSide` **相反**（平仓方向）。这三张是**开仓**单，BTC 行 `side=sell` + `posSide=short`
方向相同，因此判为 False。该函数的 docstring 明确写着
"Call only for protection orders, not entry or position rows"，
但 `read_complete_pending_tpsl_snapshot` 返回的是未过滤的原始 pending 行，
调用方对**每一行**求值，于是入场条件单触发了本该只对保护单生效的判据。

这直接证实了 `docs/2026-09-05-codex-handover-closeout.md` 第三节第 1 条留下的未证实假设
（"BTC 上存在历史遗留条件单污染该快照"）。**结论：只要这三张 pending 条件单还在，
BTC 与 ETH 的三档止盈收敛都会被 `convergence_pending_alias_conflict` 全局否决。**

本会话不修、不撤、不改。这是既有生产缺陷，不属于 REST+WS 改造范围，已单独报告用户。

### 3. 现有 REST 客户端能力与超时

`src/telegram_kol_research/deepcoin_client.py`（827 行）：

- 写：`place_order`、`trigger_order`、`set_position_sltp`、`cancel_position_sltp`、
  `replace_order_sltp`、`cancel_order`、`cancel_trigger_order`。
- 读：`list_positions`、`list_position_history`、`list_open_orders`、`list_order_history`、
  `read_order_history`、`list_trade_fills`、`list_trade_fills_by_order_id`、
  `get_order_history_by_id`、`list_trigger_orders_pending`、`read_trigger_orders_pending`、
  `list_trigger_order_history`、`read_trigger_order_history`、
  `list_trigger_order_history_by_order_id`、`get_trigger_order_history_by_id`、
  `get_ticker_price`、`get_ticker_quote`、`list_swap_symbols`、`list_swap_instruments`。
- **没有 listenkey 相关方法**，也没有任何 WebSocket 能力。
- 超时：`DeepcoinCredentials.timeout_seconds` 默认 `15.0`，可由
  `DEEPCOIN_TIMEOUT_SECONDS` 覆盖；连接层与请求层共用同一个值（`httpx.Client(timeout=...)`）。
  没有分别的连接/读/写超时，也没有重试。
- 失败语义：POST 的 `RequestError` / `HTTPStatusError` / `JSONDecodeError` 一律抛
  `DeepcoinRequestOutcomeUnknown`；GET 抛 `DeepcoinClientError`。外层 `code` 非 0 抛
  `DeepcoinDefiniteRejection`，`data[].sCode` 非 0 同样抛
  `DeepcoinDefiniteRejection`。**这三类异常的区分必须在新代码里完整保留。**
- 限流：`DeepcoinTpslWriteLimiter`（15/秒、450/分）按凭据作用域进程内共享，只用于
  position TPSL 写入。

### 4. 现有下单路径与接口归属

入场提交唯一实现在 `src/telegram_kol_research/recovery_live_submit.py`，
按 `leg["order_type"]` 三分支：

| leg order_type | payload builder | 客户端方法 | 实际接口 |
|---|---|---|---|
| `market` | `build_deepcoin_market_order_payload` | `place_order` | **`POST /deepcoin/trade/order`** |
| `limit` | `build_deepcoin_trigger_order_payload` | `trigger_order` | `POST /deepcoin/trade/trigger-order` |
| 其他 | `build_deepcoin_trigger_order_payload` | `trigger_order` | `POST /deepcoin/trade/trigger-order` |

`build_deepcoin_place_order_payload`（`ordType=limit` + `px` + `clOrdId`）确实存在于
`recovery_live_submit.py:2979`，生产零调用点，只有 `tests/test_recovery_live_submit.py`
引用。

调用链：
`auto_trade_execution.process_trade_signal_live` 与
`worker_command_executor._execute_recovery` / `_execute_process_next`
→ `recovery_live_submit`。web 角色不直接调用，走 `worker_command_jobs`。

保护相关模块分工：

- `_deepcoin_embedded_sltp_fields`（`recovery_live_submit.py:3180`）：trigger-order
  只嵌 `slTriggerPx`（止损），显式 `del take_profit_leg`，止盈等成交后的确切 posId。
- `build_deepcoin_position_sltp_payload(s)`：成交后按 posId 调 `set-position-sltp`，
  split 模式强制要求 posId，缺则抛 `missing_pos_id_for_split_position_sltp`。
- `position_mutation_gateway.py`：所有 `set_position_sltp` / `cancel_position_sltp`
  的意图记账、回读校验（`_set_position_sltp_readback_matches`）与幂等边界。
- `execution_bindings.py`：binding / leg 账本，重试上限 5 次、5/10/20/40 分钟退避、
  常规认领只选 pending/retrying。
- `trigger_protection_intents.py`：trigger-order 嵌入式止损的认领意图，
  failed 且 disposition 为空时自动转 `manual_review`。
- `trigger_protection_rescue_worker.py`：救援，明确排除 `manual_review`。
- `position_take_profit_orders.py` + `trigger_take_profit_convergence*.py`：
  三档止盈收敛（当前被上文第 2 条全局否决）。
- `native_tpsl.py`：别名一致性与保护方向判据的共用实现。
- `deepcoin_execution_actions.py`：撤单/改单动作；`entry_revision_executor.py`：
  改单路径，是**唯一**同时可能走 `trigger_order` 与 `place_order` 的模块
  （`entry_revision_executor.py:1413-1415`）。

### 5. 可直接提炼进 src 的 WebSocket 代码

`scripts/deepcoin_rest_ws_tpsl_experiment.py` 里已实证可用、可原样提炼的部分：

| 内容 | 位置 | 提炼去向 |
|---|---|---|
| listenkey 获取 `GET /deepcoin/listenkey/acquire`（签名串 `ts+GET+path`，`data.listenkey`） | `_signed_get_json` + `LISTENKEY_PATH` | `deepcoin_client.acquire_listen_key()`，复用现有 `build_deepcoin_auth_headers` 与 `_request`，不要另写签名 |
| WS URL `wss://stream.deepcoin.com/v1/private?listenKey=...` 与订阅帧 `{"action":"subscribe","tables":["Order","Trade","Position","TriggerOrder"]}` | `PrivateWsCapture._run` | 新模块 `deepcoin_private_ws.py` |
| 事件信封解析：`payload.result[] -> {table, data}`，`action` 形如 `PushOrder`/`PushTrade`/`PushPosition`/`PushTriggerOrder` | `extract_ws_rows` | 新模块，保持"原始 payload 整条落库 + 解析视图分离" |
| 短键取值 `OS`（订单号）、`TU`（TriggerOrder 的仓位引用）、`PI`（Position 的仓位号） | `_ws_order_id` / `_ws_position_id` | 新模块的解码层，必须版本化，未知短键保留原文 |
| `durable_json` / `_append_event` 的 fsync 追加写 | `_append_event` | 只作为证据文件写法参考；入库走数据库，不复制这段 |

**不要提炼**的部分：`connect(...)` 的裸 `websockets.sync.client`（同步阻塞，与 worker 的
asyncio 事件循环不兼容，阶段 1 要用 `websockets.asyncio.client`）；实验脚本的
一次性锁、证据目录、`run_live` 编排。

### 6. WebSocket 采集应挂在哪

`RUNTIME_ROLE_SINGLETON_TASKS["worker"]`（`src/telegram_kol_research/web_app.py:361`），
新任务名建议 `deepcoin_private_ws`。理由与约束：

- worker 是唯一持有 Deepcoin 凭据的角色（`/etc/telegram-kol-worker.env`）。
- 启动位置照 `deepcoin_reconcile` 的写法（`web_app.py:5001-5024`）：
  `runtime_role_starts_singleton_task(...)` 判定 + `asyncio.create_task`，
  任务句柄存 `app.state.deepcoin_private_ws_task`，并在 `web_app.py:427` 的
  `deployment-identity` tasks 字典里登记，这样 `/api/runtime/deployment-identity`
  能直接观察它是否存活。
- 关停要加进 `web_app.py:5418` 附近的 shutdown 序列，按"先停收新意图 →
  等写租约 → 落完已收事件 → 记水位 → 关连接"的顺序。
- **不要**挂到 `ingest`（无凭据）或 `web`（无执行权限）。
- `loop_lag_monitor` 是进程监控不是单例任务，不要往那里挂。

### 7. 运行时依赖缺口

`pyproject.toml` 的 `dependencies` 里**没有** `websockets`；本机 `.venv`、服务器
`/opt/telegram-kol-analyzer/.venv`（python 3.12）与服务器系统 `python3`（3.11）
都没有安装。实验能跑是因为证据目录里有独立虚拟环境
`/var/lib/telegram-kol-cutover-evidence/eth-rest-ws-tpsl-short-no-clordid-test-20260905/.venv`
（python3.11 + `websockets 16.0`）。阶段 1 必须先加依赖并在部署后核实生产 venv 已装上。

### 8. 交接文档实验证据已复核，且比文档记载更强

`/var/lib/telegram-kol-cutover-evidence/eth-rest-ws-tpsl-short-no-clordid-test-20260905/live-ab734b3900f6/`
文件齐全，`live-summary.json` 的 `status=exact_chain_observed`、`ws_frames=7`、
`ws_error_type=null`，worker 身份 `af8676dc` + `loaded_artifact_verified=true`。

**文档未记载的两点，对设计有直接影响：**

- `Position` 推送带 `PI` 字段，值为 `1001125145471184`，即 REST 的 split posId。
  公开文档的 Position 字段表**没有列出**这个字段（见
  `docs/2026-09-05-deepcoin-api-deterministic-link-research.md` 第 4 节），
  但真实推送里有。这意味着私有 WS 的仓位号来源不止 `TriggerOrder.TU` 一条。
  仍需按未文档化字段对待：可用作证据，不可作为唯一依据，且要在阶段 2 的解码层
  做存在性检查而非假定。
- WS 的合约标识是 `ETHUSDT`，REST 是 `ETH-USDT-SWAP`。跨源比对前必须归一化，
  这是一个真实的字段格式差异，不是笔误。

## 证据记录

- phase-5-completed (2026-09-09, 会话 local_4a6676b0, **阶段 5 完成**):
  第一笔真实入场于 `2026-09-09T03:21:04Z` 发生并**逐笔核对通过**，阶段由 in_progress 转 completed。
  binding 346（chat -1003048800035 / message 4526，ETH 多），**一条信号里市价腿与限价腿同时出现**，
  所以切换的两半各自都被实盘验证。部署 SHA 仍为 `7a4d852a31708515aa92e58313a941c206f5637c`，
  期间未改任何代码；核对全程只读。
  **市价腿（leg 595）**：请求 `{clOrdId, instId, mrgPosition, ordType=market, posSide=long, side=buy, sz=2.2, tdMode}`
  ——市价腿照旧带 clOrdId。回执原样落库
  `{"code":"0","data":{"clOrdId":"TKDBK4526E1","ordId":"1001125194995925","sCode":"0","sMsg":"","tag":""},"msg":""}`，
  **确实没有 posId**——整个归属设计所依赖的那个事实，第一次在真实生产订单上（而非实验格）被观察到；
  而且存下来的就是交易所自己的回执，没有任何东西把 posId 写进去（循环论证已断）。
  归属 `pos_id == order_id == 1001125194995925`、`attribution_status=verified`，
  交易信号 warnings 里**没有** `entry_position_attribution_unverified`，
  `market_fill_attribution_unverified` **从未记录过**——说明三重确认是**在提交时**通过的，不是事后补救。
  交易所核对：仓位 `1001125194995925` ETH-USDT-SWAP long `pos=2.2` `avgPx=2487.94`，方向与数量与下单完全一致。
  **限价腿（leg 596）**：请求 `{instId, mrgPosition, ordType=limit, posSide=long, px=2467.0, side=buy,
  slTriggerPx=2445.0, sz=2.3, tdMode}`——**正好 9 个字段、就是实验白名单、不含 clOrdId，且被接受（sCode=0）**，
  这正是 cell 6a–6d 要确立的结论；trigger-order 的词汇一个都没漏过来（无 triggerPrice / slOrdPx /
  productGroup / isCrossMargin / orderType）。`order_kind` 是 `limit` 而非 `trigger_limit`，
  即确实走了 `place_order`；执行事件是 `create_limit_entry` 而非 `create_trigger_entry`。
  `client_order_id=TKDBK4526E2` 只落本地、从未发出。
  **止损都挂上了**：市价腿——账本 675 `stop_loss` 触发价 2445（`entry_protection_response`），
  交易所 `1001125194996463` slTriggerPrice=2445 live，另有三档止盈 2505/2525/2545（账本 676–678）；
  限价腿——止损随单附带，交易所触发单 `1001125194996696` slTriggerPrice=2445 sz=2.3 **在成交之前就已存在**，
  这正是限价那一半可以被接受的性质（成交那一刻起止损由交易所持有）。**无任何"成交但无可验证止损"。**
  **5a 护栏首次面对真实活挂单**：`list_open_orders`（V2）返回且仅返回
  `{ordId:1001125194996697, clOrdId:'', ordType:limit, px:2467, sz:2.3, side:buy, posSide:long, state:live}`
  ——这是该端点第一次需要显示的活普通单，被它取代的 V1 对这种形状恰好是失明的。只读跑护栏：
  `allowed=['1001125194996697']`、`blocked=[]`，**仅凭 order_id 匹配**（这单也只有 order_id）。
  `open_order_guard_blocked` 事故自切换以来 **0** 条。
  **计数**：`market_fill_attribution_unverified` **0**（从未触发）；`open_order_guard_blocked` **0**；
  部署后 `uncertain` 尝试 7 条（completed 4 / partial_failed 2 / in_progress 1），**没有一条属于本 binding**，
  三族均早于切换四天（成因见 `raw-15496-trace.md`）；止盈收敛 1 submitted / 1 waiting_backup_stop。
  **守望器**：2026-09-08T06:33Z 起常驻 20.8 小时、1246 个样本，`NEW_ENTRY_DETECTED` 自停；
  20 条异常全部是孤立的 WS 重连抖动，**无一连续**（判据设为连续 3 次才停正是为此）。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5/first-real-entry.md`、
  `entry-watch-samples.jsonl`（1246 行）、`entry-watch-marker.txt`、`anomalies.jsonl`。
- phase-5-raw-15496-trace (2026-09-09, 会话 local_4a6676b0, 应指挥会话要求的只读追查):
  raw 15496 落 `authoritative_execution_outcome_unknown`，**与阶段 5 无关**：入场在**指令准入层**被
  `adjacent_entry_context_pending` 推迟（item 1029 留 pending），`_message_instruction_status` 聚合成
  `in_progress`，而 `execution_boundary._KNOWN_UNKNOWN_STATUSES` 把 `in_progress` **无条件**判成
  `outcome_unknown`——与有没有写入无关，本次写入跟踪器为空。边界打开到判 uncertain 仅 591 毫秒。
  **零交易所接触**（无 trade_signal / 无 execution_event / 无 leg / `evidence_refs_json` 为空即
  `place_order` 从未被调用 / 交易所直读无对应仓位与挂单 / WS 收件箱该时段无帧）。
  当时 `permits_new_entry=True`，`ws_observation_blocked_new_entry` 至今 0 次。
  **不会自愈**：`reconcile_due_entry_admissions` 在 `execution_contract_mode != "live"` 时直接返回空，
  而生产是 `shadow`——A 线 step 3「被推迟的指令永不恢复」的实况；lifecycle 1121 为 `entered` 而
  binding 为 NULL，即阶段 5 文件硬性约束第 5 条点名的第二个缺陷。按该约束记为 **A 线未完成导致**。
  附带定位了阶段 5 观察窗里那 784 行 ERROR 的确切来源：`uncertain` 积压 20 行、跨五天、三族，
  识别扫描器每轮全量重打。证据：`raw-15496-trace.md`。
- phase-5-deployed-observed (2026-09-08, 会话 local_4a6676b0, **已部署已观察，阶段仍 in_progress**):
  分支 `rest-ws/phase-5-order-entry`，提交 **`7a4d852a31708515aa92e58313a941c206f5637c`**，已 `tg-deploy` 上线
  （2026-09-08T04:34:31Z–04:34:43Z）；**回滚 SHA `230ba1cc8097d30ed89c860608467f17680a14ca`**（部署前生产 HEAD）。
  用户对阶段 5 的批准见 phase-5-approval，部署由指挥会话依该批准放行。
  **迁移判据**（`deepcoin_limit_entry.limit_leg_requires_trigger_order`，逐腿判定）：只迁 `triggerPrice`
  恒等于 `price`、无任何触发语义的入场限价腿——即今天 draft builder 产出的每一条，因为
  `build_deepcoin_trigger_order_payload` 一直无条件把 `triggerPrice` 设成限价。带真实突破/回落条件、
  或选了 `last`/`mark`/`index` 价格来源的腿继续走 trigger-order，**没见过的腿形状拒绝迁移而不是默认迁**。
  **payload 白名单**（多一个字段就抛错）：`instId, tdMode, mrgPosition, side, posSide, ordType=limit, px, sz,
  slTriggerPx`（`tpTriggerPx` 可选但生产不发，止盈仍等确切成交 posId）。**不含 `clOrdId`**；本地幂等键仍落
  `execution_order_legs.client_order_id`，只是不发交易所、不作所有权证明。
  **归属转正**：普通 order 开出的仓位其 posId 等于该 order 的 ordId，且必须三重确认
  （WS `Position.PI == ordId` 且 `Po` 非零、REST 该 posId 存在、方向一致且数量非零不大于下单量）。
  任一不成立即 `attribution_status='unverified'`，而 `== 'verified'` 是全仓库自动修改/撤销/认领的前置条件。
  部署前只读核对生产 `execution_order_legs`：market 入场腿两 id 齐全的 **153/153 满足等式、零反例**；
  trigger_limit 的 **204 条一条都不满足**（条件单的仓位以派生子单命名）。同时**去掉了**旧代码把自家
  pos_id 写进 `response_json` 的做法——那正是阶段 4 判据 2 循环论证的来源。
  **入场准入门**：`deepcoin_entry_admission` 是唯一判定入口，worker/all 角色须有活 inbox 且
  `ws_observation_permits_new_entry()` 放行，否则 `RecoveryLiveSubmitError("ws_observation_blocked_new_entry:<原因码>")`
  ——**不提交，不是提交后撤**。角色由 web 启动时显式登记，不读环境变量（role 来自只是默认取环境变量的 CLI 选项）。
  **裸仓告警**（指挥会话部署前追加的硬性条件）：市价腿归属 unverified 时记
  `market_fill_attribution_unverified`（severity **critical**，来源 `deepcoin_entry_order:<ordId>`，
  `impact` 带 instId/side/sz/候选 posId），并进代码级默认投递白名单 `config.ALWAYS_NOTIFIED_INCIDENT_TYPES`
  ——**非空白名单才并入**，空白名单仍是 capture-only（那是明确表达"什么都别发"，不是遗漏）。
  详细 summary 若被越界检查拒绝，退回不含插值的最小 summary 再记一次。生产实测投递通道可用：
  `TELEGRAM_INCIDENT_ENABLED=true`、水位线 `AFTER_ID=272` 低于当前最大 id、事故走
  `TELEGRAM_KOL_SYSTEM_BOT_*`（两项均已设置，与 A-2 关注的那个空 `NOTIFICATION_BOT_CHAT_ID` 不是同一条通道），
  且 id>2000 的 `severe_protection_incident` 有 31 条 delivered。
  **测试**：focused `tests/test_deepcoin_phase5_cutover.py` 31 项 + `tests/test_deepcoin_limit_entry.py` 30 项；
  全量 **7771 passed / 0 failed / 4 skipped**；`tests/test_runtime_event_loop_blocking_census.py` 通过。
  **重启恢复已确认**：worker 重启一次（pid 3034800 → 3036911），三单元 active、NRestarts 0，
  WS 重新 healthy、`open_gap_count=0`、`last_resync_outcome=converged`，**7 个权威循环全部 fresh+successful**。
  **观察窗 `2026-09-08T04:43:42Z ~ 06:24:53Z`，104 个连续健康样本，零不健康**：HEAD 恒为部署 SHA、
  三单元恒 active、NRestarts 全程 0、WS 恒 healthy、`open_gap_count` 恒 0、`permits_new_entry` 恒 true、
  `reconcile_failures_last_hour` 恒 0、`rate_limited_last_hour` 恒 0（窗口内零 401/50000）。
  真实消息 **9 条 / 5 个群**，满足 L2 的 ≥5 条门槛并达到"尽量 2 个群"的偏好项。
  **零交易所写入**：`execution_events` 自部署起 2 条，**都是 `auto_trade_skipped`**
  （4042 ZEC short、4043 SNDK short，原因 `kol_or_group_auto_trade_disabled`）——真实信号被识别并在任何
  交易所写入之前正确拒绝；`execution_order_legs` 自部署起新增 **0** 行；`uncertain` 尝试 **0**；
  五张业务表行数与部署前基线完全一致（345 / 594 / 673 / 189 / 200）；`runtime_incidents` 自基线 id 2068 起 **0** 条。
  **窗口结束交易所直读**：`position_count=3, open_order_count=0`，指纹
  `949673250cd5438bfa4d22e570a1324803fa86ff954e5ed4174b27b783965e61`。与 5a/5b 的 `c4cd87ec…` 不同，
  **原因不是本次切换**：三个仓位的 `cTime` 全部早于部署（ETH long 09-07T02:38:38Z、BTC short 09-08T01:23:07Z、
  ETH short 09-08T03:11:20Z），后两笔落在 5b 窗口结束（09-07T23:39Z）到本次部署之间、跑的是切换前的代码。
  **窗口内零新开仓。** 顺带这三个仓位是判据的一次实盘复证：两个普通 order 仓位 `pos_id == order_id`，
  trigger_limit 那个不成立（order_id `1001125172997005` vs pos_id `1001125178552543`）。
  **日志**：窗口内无新错误族；两类既有错误频次与等长的部署前窗口持平或更低
  （`trade_merge` `MultipleResultsFound` 6 → 2；`recognition observe_uncertain` 770 → 784，属 A 线积压）；
  `ws_observation_blocked_new_entry` / `market_fill_attribution` / `open_order_guard` / 401 / 50000 全部 0 次。
  证据目录：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5/`
  （`window-summary-final.txt`、`window-summary.txt`、`observer-samples.jsonl` 104 行、
  `observer-samples-prerestart.jsonl`、`window-end-exchange-read.json`、`phase5_observer.py`、`phase5_exchange_check.py`）。
  **未完成，也是阶段留 in_progress 的唯一原因**：**窗口内没有发生真实新入场**，所以切换本身的实盘断言
  （限价腿真的走 order 并被接受、附带 slTriggerPx 成交即由交易所持有、市价腿按三重确认归属并挂上止损、
  5a 护栏认得新普通单、WS 缺口时准入门真的拒绝）**全部只有离线测试覆盖，未经实盘**。
  结项只差**一条能通过自动交易过滤器的真实入场信号**；按阶段文件禁止为凑样本下单。
  **未做**：未合并到 main 之外的分支；未推进阶段 6；未碰
  `execution_bindings.py` / `trigger_take_profit_convergence_executor.py` / `native_tpsl.py`（A 线在用）。
- followup-b5d-naked-market-fill (2026-09-08, 指挥会话立项，**不在阶段 5 部署范围**): **B-5d 市价成交裸仓安全网。**
  阶段 5 收紧归属后，市价腿的止损靠成交后 `set_position_tpsl` 写，而写入门要求 `attribution_status='verified'`，
  所以「市价成交且 posId != ordId」时仓位会裸奔（历史 153/153 满足等式、零反例，该分支从未发生）。
  本次部署先补告警（`market_fill_attribution_unverified`，severity critical，进代码级默认投递白名单）。
  安全网本身留作独立项：归属 unverified 超过 N 秒、且该 instId+side 上恰有**一个**无人认领、数量等于成交量的
  活跃仓位时，**只挂止损不挂止盈、不认领所有权**、标 `attribution=unverified_sl_by_unique_candidate` 并告警。
  阶段 6 之前完成，**需用户单独批准**。
- phase-5b-completed (2026-09-07, 会话 local_3a8d3395, **阶段 5b 完成**):
  分支 `rest-ws/phase-5b-rate-limiter`，提交 `230ba1cc8097d30ed89c860608467f17680a14ca`
  （rebase 到 A 线 `e402692c` 之后），已 `tg-deploy` 上线；**回滚 SHA
  `e402692c149e8d7ac0a993cf73e17e8b3c330156`**（部署前生产 HEAD）。
  **识别**：只有 `HTTP 401` 且响应体 `code=50000` 才是 `DeepcoinRateLimited`（`DeepcoinClientError`
  子类，带 `retry_after`）；其余 401（含非 JSON body）仍按认证失败处理，绝不因此重试——否则一个坏签名
  会变成静默重试循环。**重试只对 GET、最多 1 次**（等 `Retry-After`，无则按实测 1 秒，上限 2 秒）；
  **POST 一律不重试**，被限流的写入仍是"结果未知"，沿用 `DeepcoinRequestOutcomeUnknown`（硬性禁止第 2 条）。
  **配额按角色分配，不是平均分**（`DEEPCOIN_READ_LIMIT_PER_SECOND_BY_ROLE`，由
  `TELEGRAM_KOL_RUNTIME_ROLE` 解析）：worker 3/s、web 1/s、ingest 1/s，合计正好 5；`all`（本地单进程）
  持三份=5；其余进程（运维 CLI、临时脚本）取最小份额 1/s。**先按 2/2/2 上线（618a8524）并实测否决**：
  轮时长中位 13.8s → 32–44s，轮间隔 44s → 65s，是拿保护收敛延迟换 web/ingest 用不到的余量；指挥会话
  裁定改 3/1/1（见 phase-5b-ruling）。做成常量而非运行时开关是刻意的——这组数只有作为一组才安全。
  **按物理 HTTP 请求计数**：V2 分页每页一个令牌，重试再取一个，缓存命中不取（5a 裁定第 4 条）。
  **轮内缓存**：`begin_round_read_cache()` / `end_round_read_cache()` 在 `_request` 层按请求路径缓存
  positions / trigger-orders-pending / orders-pending（因此 `list_` 与 `read_` 两个读法自动共用一份）。
  **经同一 client 的任何写入立即整体作废缓存**，写后再读一定是新读；轮结束无条件丢弃，绝不跨轮。
  离线按生产 3 个 instId 精确计数：安静轮 15 → 14 次 GET，一轮加载两次快照且中间无写入 29 → 23
  （三个可缓存端点 11 → 5），**真实"有写入"工作轮 29 → 28**——正确性优先于省请求，这是设计如此。
  **健康端点**新增 `read_limit_per_second`、`rate_limited_last_hour`、`retry_after_waits_last_hour`
  以及供后续项用的 `read_requests_last_hour` / `read_throttled_seconds_last_hour`。
  **测试**：focused 34 项通过；全量 **7674 passed / 0 failed / 4 skipped**；
  `tests/test_runtime_event_loop_blocking_census.py` 通过（重试等待在管理工作线程，不在事件循环）。
  **观察窗口 `2026-09-07T22:48:06Z ~ 23:39:08Z`，51 个连续健康采样点，零不健康**：HEAD 恒为部署 SHA、
  三单元 51/51 active、NRestarts 全程 0、`complete` 恒 true、交易所指纹逐字节恒为
  `c4cd87ec9db6b3bf4d3a38ba1a858fb8e90a586c4836a6a51617016e5698e50a`（position_count=3,
  open_order_count=0）、WS state 恒 healthy、open_gap 恒 0、`reconcile_failures_last_hour` 恒 0。
  末次采样近 30 分钟**真实消息 5 条 / 3 个群**——满足 ≥5 条门槛，**并且达到了 L2「尽量 2 个群」的偏好项**
  （5a 未达到）。**`rate_limited_last_hour` 与 `retry_after_waits_last_hour` 51 个采样点全为 0，
  journal 401 自部署起 0 次**（部署前基线 3 小时 14 次 / 1 小时 6 次，全在 worker 的
  trigger-orders-pending）。`execution_events` 与 `runtime_incidents` 自部署起均为 0，即零撤单、
  零非预期交易所写入；reconcile 结论与部署前一致（bindings 恒 174、shadow candidates_seen 恒 14）。
  **重要发现（B-5c 基线）**：401 不是偶发突刺。worker 的持续读需求实测 **8747 次/小时 = 2.43 次/秒**，
  顶满 3/s 份额，**49.5% 的墙钟时间在等令牌**（180 秒抽样复核：437 次 / 89.1 秒，同为 2.43/s、49%）。
  部署前它无节制地跑在约 4.5 次/秒，持续贴着账户 5/s 天花板——这才是 401 的成因。一个周期全进程约
  145 次物理 GET，其中 reconcile 本体只占约 17 次，而 `runtime_worker_executor` 是 `max_workers=1`
  的单线程执行器，所有管理循环共用，那 49% 的等待被串行叠加进每个循环——**这才是轮次变慢的真实机制，
  不是 reconcile 自己的读多**。因此 2/s → 3/s 几乎没有改善（需求远在两者之上），**没有哪个配额取值能
  同时拿到零 401 和原来的时延**；降需求是独立后续项 B-5c，不是调参。轮间隔 62s 只是**无事件时的兜底**，
  阶段 3 起真实成交由 WS 唤醒立即触发 reconcile，保护面不依赖它。事件循环停顿无回归（部署后 3 小时
  1 次，部署前等长窗口 1 次；阻塞在管理工作线程而非事件循环）。
  证据目录：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5b/`
  （`window-summary.txt`、`observer-samples.jsonl` 51 行、`window-end-snapshot.json`、
  以及被否决/中间版本的 `observer-samples-2ps-superseded.jsonl`、
  `observer-samples-323b98b4-superseded.jsonl`）。
  **未做**：未合并；未改阶段 5 迁移本体（分支 `rest-ws/phase-5-order-entry` 仍未部署未合并）；
  未碰 `execution_bindings.py` / `trigger_take_profit_convergence_executor.py` / `native_tpsl.py`（A 线在用）。
  **遗留观察（未处理，未来某阶段可考虑）**：`web_app.bind_live_position` 是 async 端点却直接同步调
  `list_positions()`，本阶段让它在事件循环上最多多等约 1 秒令牌 + 2 秒重试。属既有问题（那里本来就有
  15 秒超时的同步网络调用），人工触发、极少发生，未改以免动到权威路径。
- phase-5a-completed (2026-09-07, 会话 local_32e174b0, **阶段 5a 完成**):
  分支 `rest-ws/phase-5a-open-orders-v2`，提交 `86825b8915377574b6c7fed7d98ab3d2e792ac4e`
  （rebase 到 A 线 `af6f2515` 之后），已 `tg-deploy` 上线；**回滚 SHA
  `7c2fc797b6dd08686c93114fca14171010229eaf`**（部署前生产 HEAD）。
  **分页语义**：官方「获取未成交订单列表」只写 `index` 是「页码」，未写起点；由官方示例仓库
  `deepcoinapi/openapi_python_example` 的 `rest/trade/get_orders_pending.py`
  （`index='1'`，`deepcoin_api.get_orders_pending` 的 uri 正是 `/deepcoin/trade/v2/orders-pending`）
  确定 **index 从 1 开始**，`limit` 上限 100，翻到不足一页为止。任何一页失败、畸形或业务错一律抛
  `DeepcoinClientError`，结构上不返回部分结果；服务器忽略 `index`（相邻页身份重复）同样抛。
  **字段映射**：V2 在消费方读取的 20 个字段上与 V1 恒等（`DEEPCOIN_OPEN_ORDER_V1_TO_V2_FIELDS` +
  测试守护），行原样返回。真正的差异在请求侧：V2 **没有** `instType` 参数，V1 的 `instType=SWAP`
  过滤移到客户端；`instType` 缺失的行**保留**（不可分类是未知，不是不存在）。
  **调用点**：`docs/plans/2026-09-06-deepcoin-rest-ws/phase-5a-callsites.md`，
  生产源码 16 处 `list_open_orders(`（3 定义 + 13 调用）+ 6 处按方法名的间接调用逐条判定。
  可能对现有对象写入的 **4 处**（`cancel_entry_order`、`cancel_revision_entry_leg`、
  `cancel_pending_entry_legs`、`_match_exact_deferred_exchange_orders`）全部加护栏
  `open_order_action_guard.py`：只允许 `execution_order_legs` 里 `venue=deepcoin`、
  `order_kind ∈ {market, limit}` 且 ordId/clOrdId 由本系统记录的对象动作，其余记日志 +
  `runtime_incidents`（`open_order_guard_blocked`, severity high）后丢弃。
  **撤单后的 2 处确认读刻意不加护栏**——在那里丢行会把外来对象的存在读成「撤单已确认」。
  **部署前只读**（worker 凭据）：V2 全量挂单 **0 行**，V1 同为 0，**无不可归属对象**，本阶段无需
  也未撤任何单。**部署后直读**证明生产真的走 V2：`?index=1&limit=100` 与
  `?instId=ETH-USDT-SWAP&index=1&limit=100`，均 0 行、无异常。
  **测试**：focused 17 项（分页、失败即抛、字段映射、护栏）通过；全量 **7635 passed / 0 failed**。
  **观察窗口 `2026-09-07T17:42:13Z ~ 20:29:35Z`，167 个连续健康采样点（2 小时 47 分）**，
  全程恒定：HEAD 恒为部署 SHA、三单元 30/30 `active`、NRestarts 全程 0、
  `complete` 恒 true、交易所指纹逐字节恒为
  `c4cd87ec9db6b3bf4d3a38ba1a858fb8e90a586c4836a6a51617016e5698e50a`
  （`position_count=3, open_order_count=0`）、`open_order_count` 恒 0、
  **护栏命中恒 0**、近 30 分钟撤单恒 0、执行事件恒 0。
  末次采样的近 30 分钟回看区间内**真实消息 5 条 / 1 个群**，满足 L2 的 ≥5 条门槛；
  **未达到 L2「尽量 2 个群」的偏好项**（窗口内只有 1 个群有流量，属流量而非代码问题）。
  部署至今 `execution_events` **零新增**，即窗口内零撤单、零非预期交易所写入。
  **日志**：V2 `orders-pending` 零错误、零 401；`open_order_guard` 零行。
  仍在的 401 全部落在**未改动的** `trigger-orders-pending`（22 次 / 2h48m；部署前等长窗口 16 次，
  逐小时 8/4/6/18/0/6 波动明显，含部署与重启的那一小时是 18、随后一小时是 0），
  即既有 `out-of-scope-401`，正是阶段 5b 的对象；本次改动未增加请求量（0 挂单时 V2 与 V1 均为 1 次请求/调用）。
  证据目录：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5a/`
  （`pre-deploy-v2-orders-pending.json`、`post-deploy-v2-live-path.json`、
  `window-end-snapshot.json`、`observer-samples.jsonl` 167 行）。
  **未做**：未合并、未改阶段 5 迁移本体（分支 `rest-ws/phase-5-order-entry` 仍未部署未合并），
  未碰 `execution_bindings.py` / `trigger_take_profit_convergence_executor.py` / `native_tpsl.py`（A 线在用）。
- followup-bind-live-position (2026-09-07, 指挥会话记录): `web_app.bind_live_position` 是 async 端点却同步调用 `list_positions()`（既有 15s 同步网络调用），5b 后最多再多等约 3 秒令牌与重试；人工触发、极少发生。列为 B-5c 的一部分（把该调用放进工作线程），不单独立项。
- phase-5b-ruling-2 (2026-09-07, 指挥会话裁定): 选 (A) 接受现状收窗。实测 worker 真实读需求约 145 次/周期（reconcile 本体只占 17 次），单线程 runtime_worker_executor 串行叠加约 49% 的等令牌时间，任何正确的限流都会把轮间隔拉到约 62s；提高配额只是把延迟换回 401。5b 目标（401 清零、零回退）已达成。后续独立项 **B-5c 读放大归因与降需求**：按端点/调用点计量每周期读次数，找出 145 次里的大头，用 WS 收件箱状态替代轮询读、或合并跨循环的重复快照读，目标把周期读次数降到 3/s 以下且不靠限流等待；在阶段 5 之后排期。保护反应面的补充说明：阶段 3 起真实成交由 WS 唤醒立即触发 reconcile，62s 只是无事件时的兜底轮询间隔。
- phase-5b-ruling (2026-09-07, 指挥会话裁定): 读限流配额按角色分配而非按进程平均——worker 3/s、web 1/s、ingest 1/s，合计不超过账户 5/s。理由：worker 是唯一有持续读循环的角色（实测原速率约 4.5/s，2/s 使 reconcile 轮间隔 44s → 65s，保护反应最坏多等 20 秒）；web/ingest 只在人工调用与断线重同步时读。阶段 5 上线后 V2 分页会放大 worker 请求量，届时按实测重新评估配额。要求重新部署并重新计窗。
- phase-5a-rulings (2026-09-07, 指挥会话裁定，用户要求由指挥会话判断): (1) 观察窗只覆盖 1 群——接受，5a 是只读端点切换、零交易所写入、指纹逐字节不变，不为群数再等一轮；(2) 护栏零实战命中——接受，首次实战验证推迟到阶段 5，不为造样本下单；(3) 护栏严格度——保持严格，只认 execution_order_legs 里 order_kind 为普通 order 且由本系统记录的 ordId，缺 leg 时记事故并停手，不放宽到 execution_bindings；(4) 5b 限流器必须按物理 HTTP 请求计数（V2 分页每页一次），不按逻辑调用计数，已写入 phase-5b 文件。5a 维持 completed，代码保持在线。
- followup-ws-gap-entry-retry (2026-09-09, 指挥会话记录，A-5 会话发现): 阶段 5 的准入门在 WS 缺口（含每次 tg-deploy 重启的几秒）期间以 ws_observation_blocked_new_entry 直接拒绝提交而非延后，缺口内到达的入场会丢。阶段 6 前置项：被准入门挡下的入场应进入可重试的推迟态（复用 A-3d 修好的 entry_admission_reconciler 到点重试），deadline 内 WS 恢复即提交，到期则 entry_admission_expired 告警。
- phase-6-pre-1-completed (2026-09-09, 会话 local_4a6676b0): WS 缺口期间的入场从终态拒绝改为可重试推迟。
  提交 **`6e6241911b85892e6f14f12ce0a4cd558ae4c681`**，2026-09-09T04:14Z 经 tg-deploy 上线，
  **回滚参考 `21aab901e4532c0de8603789b39fded718b5d0cd`**（回滚即 `tg-deploy 21aab901...`）。
  **改了什么**：`deepcoin_entry_admission.ws_observation_entry_defer_result()` 在流不放行时返回
  `{"status":"deferred","reason":"ws_observation_pending"}`，并把 6 小时入场 deadline
  （`ENTRY_ADMISSION_EXECUTION_DEADLINE`）**一次性**盖到指令项与执行合约（`IS NULL` 才写，重复推迟不顺延）；
  `auto_trade_execution._auto_process_single_message_trade_signal` 在 admission 判定之后、
  任何交易所读写之前调它（**唯一一处 18 行**，指挥会话单独放行）；`ws_observation_pending` 进
  `VISIBILITY_DEFER_REASONS`，因此复用既有指令项推迟写入器与 A-3d 的 `entry_admission_reconciler`，
  退避照搬既有 `VISIBILITY_RETRY_DELAYS`（首次 5 秒）而非另起平坦 30 秒；恢复器新增 WS 分支：
  每 tick 只读一次流状态、放行即清 `visibility_next_attempt_at`、到 deadline 走 expired +
  `entry_admission_expired` 告警（该分支无 attempt 行，共用的过期写入器把 attempt 改为可选）。
  **为什么推迟必须发生在这里**：`_prepare_instruction_entry_submission` 之后合约已是 `submitting`，
  而 `LEGAL_INSTRUCTION_EXECUTION_EDGES` 里**没有 (submitting → deferred)**；在更晚的地方改成推迟
  就得给状态机加一条"已声明要写交易所的合约可以退回可重试态"的边。
  **`recovery_live_submit` 的两处检查点一字未动**，仍然抛 `ws_observation_blocked_new_entry` 并 fail-closed：
  本次只把**第一次**拒绝提前到合约尚未声明写入意图的位置，任何时刻 `permits_new_entry` 为假仍然不提交。
  **测试**：全量 **8033 passed / 4 skipped / 0 failed**；新增 `tests/test_ws_observation_entry_defer.py` 19 条
  与 `tests/test_auto_trade_execution.py` 末尾 4 条端到端（缺口→推迟→恢复→提交且只提交一次、
  缺口持续到期→告警且零下单、healthy 与阶段 5 逐字段一致）；`tests/test_instruction_execution_outcomes.py`
  的封闭集断言同步加一项。**变异检验双向**：去掉调用点 4 条端到端转红 3 条（healthy 那条保持绿，正确）；
  恢复器里把门写死 `True` 转红 2 条、忽略 deadline 转红 3 条。
  `tests/test_runtime_event_loop_blocking_census.py` 3 条通过。
  **观察（L2）**：窗口 04:24:30Z–04:54:34Z 连续 30 分钟，7 条真实消息、3 个群、每分钟采样 43 条全部健康
  （证据 `/root/evidence/phase-6-pre-1/observer-samples.jsonl`）。04:14:28Z 起的第一个窗口在 04:24:30Z
  因采样时**正处在一个未闭合的 silence_timeout 缺口**被判不健康而重置——判据偏严（开着的缺口正是本阶段
  要处理的常态而非故障），如实记录。
  **没有拿到实盘样本，如实记**：部署自身的重启缺口（gap 367，`process_start`，04:14:19.855→04:14:25.566Z，
  5.71 秒）**期间零消息到达**；窗口内唯一的缺口（gap 369，`silence_timeout`，04:39:11→04:39:19Z，8.64 秒）
  同样零消息；整个窗口**没有产生任何入场指令项**（7 条消息是止盈/出场/行情评论），
  所以推迟路径与 healthy 提交路径都**只有测试证据、没有生产样本**。
  为观测而下单是被禁止的，未做。
  **交易所直读**：部署前指纹 `72336a44d52f67b5e307d8787a599ac091449d858398f6c0ba3f76facd4cae8b`
  （4 仓 1 挂单），窗口结束 `29ca3626118f31af7f7610f6ab04d35088e1e53fe09255ec2a78b383ecfaedfb`（仍 4 仓 1 挂单）。
  **指纹变化已逐笔归因，零非预期写入**：窗口内仅两次交易所写入，
  `execution_events` 4057 `strategy_management_close_submit`（04:28:59Z，chat -1002409877375 message 9210
  的"获利出局/头仓也出/清仓"，指令项 1036 → batch 162，`close_submissions_pending_reconciliation`）
  与 4058 `create_backup_stop`（04:44:46Z，既有 `trigger_protection_stop_rescue_mode=live` 安全网，
  pos 1001125195880289）。两者都是既有路径、与本阶段无关；本阶段只动入场准入，而窗口内零入场。
  新增的 5 行 `position_protection_ledger`（679-683）`evidence_source` 全是
  `reconciliation_trigger_protection_intent` / `position_mutation_intent_readback` /
  `trigger_take_profit_pending_readback`，是**读回观测**不是写入。
  `execution_order_legs` 与 `execution_bindings` 全窗口不变（596 / 346）。
  部署后 `runtime_incidents` 零条、worker journal 零 error 零 traceback、`submit_unknown` 恒为 0。
  另用 `python -B` 只读加载**已部署**代码，对生产库跑了一次恢复器的 WS 选择查询，确认它能执行
  （返回空集），排除"异常被运维循环的 `except Exception: pass` 吞掉"这一可能。
  **未做**：未碰 `recovery_live_submit`、`execution_boundary`、`authoritative_recognition`、
  `strategy_management_planner`；陈旧指令项 1022/1023 按指挥会话裁定交 A 线 step 6 会话处理，本会话未动。
- phase-6-pre-6-ruling (2026-09-09, 指挥会话): owner_pid 是常驻 worker 的 pid，进程存活判定修不了“进程活着但代码路径丢 token”的批次 7 类；而“终态一律归还”会推翻 6 条刻意锁定的跨批次隔离测试（含糊写入未澄清期间不让别的批次拿到写入权）。裁定 (c)：含糊写入期间租约保持 held，批次落终态那一步按 generation + owner_kind 归还，归还接口只在批次已终态时可调、静态守护限制调用者、写审计。三态存活判定（None 不当作已死）与旧 blocked 文档格式兼容接受。
- entry-revision-authority-deadlock (2026-09-09, A 线会话只读发现，指挥会话裁定): raw 15668（峰哥 BTC 限价多 78300）在提交前被 entry_revision_exchange_authority_expired_blocked 拒绝，根因是批次 7 的租约自 09:51Z 起未归还，14:19Z 被翻成 blocked 且无复位路径，此后所有改单被拒。裁定：一次性复位为 idle（generation 28、审计、通知）由 A 线会话执行；结构性修复列为 B-6-pre-6，撤单回执自动确认列为 B-6-pre-5。这笔入场本身已丢失（消息 14:19Z，不重放）。
- revision-batch-7-decision (2026-09-09, 用户在指挥会话决定): 改单批次 7（raw 15633，BTC short，strategy deepcoin:-1002370796392:3633）撤旧单一半成功：leg 591 / 单 1001125173252446 交易所已撤（history uTime 09:51:05Z、triggerTime=0、零成交）但账本仍 pending；leg 592 / 单 1001125173252560（触发价 81910、sz 6、SL 83000）仍挂。用户决定**保留** 592，不按新参数重挂。账本对齐交 A 线会话做定点修复：591 → cancelled（evidence 引用 history uTime），批次 7 → resolved / operator_kept_remaining_leg，592 不动。B 线后续项 B-6-pre-5：撤单回执丢失后用 trigger-order-history 的 uTime/triggerTime 自动确认撤单结果，避免 recovery_required 卡死。
- phase-6-pre-2-note (2026-09-09, 指挥会话记录): 安全网上线（90b66311），窗内三处痕迹全零、零写入，触发路径在生产从未真实走过（历史 153/153 满足等式，预期）。回滚参考已被 A 线后续部署覆盖，单独撤销需 revert。待人工了结：execution_events 4065 cancel_revision_entry_leg / submit_unknown（09:51Z，入场条件单 1001125173252446，BTC short）与 attempt 818/820 的 uncertain——已让 6-pre-3 会话先只读核查该单在交易所的实际状态。后台等待器 pgrep 自匹配导致会话空等 6 小时，教训写入 ARCHITECTURE 第 6 节。
- writer-allowlist-3 (2026-09-09, 指挥会话知情记录): 仓库架构不变量 test_all_position_writes_cross_the_exact_gateway 的写入者白名单 ALLOWED_WRITER_PATHS 由 position_mutation_gateway.py、deepcoin_client.py 两个文件扩为三个，新增 naked_fill_stop_net.py（6-pre-2 裸仓安全网的最小止损写入器）。放行理由：只构造含 slTriggerPx 的 set-position-sltp、每笔单最多一次、四条前置缺一不可、写前耐久意图 + 写入前重验 + 回读确认、不认领所有权、经写限流器；反向约束测试断言白名单恰好三个文件。既有三道所有权门（require_verified_position_ownership / exact_position_write_gate / _load_verified_binding）一行未改。
- phase-6-pre-2-ruling (2026-09-09, 指挥会话): 既有 set-position-sltp 路径三道门都要求 verified 所有权，安全网不得在其上开口子；裁定新增只服务安全网的旁路 authority 构造器（前置 (a) 市价腿 unverified、(b) 成交满 60 秒、(c) 该 instId+side 恰有一个无人认领且数量相等的活跃仓位、(d) 快照完整），只能构造 include_take_profit=False 的止损写入，静态守护测试保证只有安全网模块能用；不加运行时开关；每次触发留 critical 告警、审计行、attribution 标记三处痕迹；跑在 worker 5 秒运维 tick，异常单独捕获。事实更正：现有代码在 unverified 时已尝试挂止损但被所有权门拒绝（position_protection_failed_after_entry_submitted）。
- phase-6-pre-2-completed (2026-09-09, 会话 local_4a6676b0): 市价成交裸仓安全网 B-5d 上线。
  提交 **`90b663116342394fc62b292e2ef6efc917be6199`**，2026-09-09T05:39Z 经 tg-deploy 上线。
  **回滚参考 `6e6241911b85892e6f14f12ce0a4cd558ae4c681`（6-pre-1），但已不能单独回滚**：
  A 线随后部署了 step-8 与 6b，生产 HEAD 现为 `c0520b94`（`90b66311` 是它的祖先，本阶段代码在线）。
  回到 `6e624191` 会同时撤掉 A 线那批工作，要单独撤本阶段须另做 revert 提交。
  **实现形状与阶段文件不同，原因是只读核实推翻了它的前提**：文件说"走既有 position_mutation_gateway 与
  set-position-sltp 路径"，做不到——那条路在**三处独立**拒绝 unverified 仓位
  （`build_position_mutation_authority` → `require_verified_position_ownership`；调用方的
  `exact_position_write_gate`；以及 `set_exact_position_sltp` 内部的 `_load_verified_binding`，
  它要求 `attribution_status` 逐字等于 `verified` 并**再调一次**所有权检查）。
  复用就必须削弱其中之一，即硬性禁止第 7 条与 ARCHITECTURE 4.7 依赖的那一条保证。
  经指挥会话两次裁定，改为**只活在 `naked_fill_stop_net` 的最小写入器**：只发 `slTriggerPx`
  （payload 出现任何 `tp*` 键即抛错）、写前落 `position_mutation_intents` 耐久意图
  （key `naked-fill-sl:<ordId>:<posId>`，operation `naked_fill_set_position_sltp`）、
  写入前最后一刻重验 (c)(d)、POST 失败按硬性禁止第 2 条记 unknown 不重发、
  回读 `trigger-orders-pending` 按 `slTriggerPrice`（**不是**仓位行的 `slTriggerPx`）匹配才 confirmed、
  经 `DeepcoinTpslWriteLimiter`。三道既有所有权门**一行未改**。
  **对阶段文件的一处事实更正**（已获指挥会话接受）：文件说市价腿"止损靠成交后 set_position_tpsl 写，
  而那道写入门要求 verified"，对；但现在的代码**并非没尝试**——`recovery_live_submit.py:1451-1540`
  归属 unverified 时 `pos_id` 退回前后快照差并照样调 `submit_exact_position_sltp`，
  是**被自己的所有权门挡住**，落一条 `position_protection_failed_after_entry_submitted` 警告。
  **另一处只读发现**：`execution_order_legs` 有 `(venue, pos_id)` 唯一索引（pos_id 非空时生效），
  所以一个仓位在库层面至多被一条 leg 持有。"无人认领"因此只能读作
  **不被任何其他 leg 持有且不在保护账本里**——严格读作"不被任何 leg 持有"会让本网永远不可能触发，
  因为被救的那条 leg 通常已经持有快照差得来的 `pos_id`。
  **写入者白名单由 2 变 3**（指挥会话知情记录见 `066bd007`）：仓库级架构不变量
  `test_all_position_writes_cross_the_exact_gateway` 只允许 `position_mutation_gateway.py` 与
  `deepcoin_client.py` 直接调 `set_position_sltp`；本模块加入白名单并在同处写明放行理由，
  同时补**反向约束** `test_the_position_writer_allowlist_is_exactly_three_files`——加第四个必须先让它变红。
  **测试**：全量 **8054 passed / 4 skipped / 0 failed**。新增 `tests/test_naked_fill_stop_net.py` 14 条
  （唯一候选→挂一次止损且 payload 无 `tp*`；两个候选→只告警；无候选→只告警；已 verified 不触发；
  宽限期内不触发；快照读不到→只告警且**不等于**无候选；其他 leg 持有→不是候选；保护账本已有→不是候选；
  draft 无止损价→只告警；回读失败→记 unknown 且第二次不重发；重验在决定与写入之间失败→intent blocked 零请求；
  payload 构造只能带止损）与 `tests/test_naked_fill_stop_net_boundary.py` 7 条静态守护
  （本模块外不得 import 旁路构造器或整体 import 本模块；`submit_exact_position_sltp` 调用者集合不变；
  安全网不得触碰那条证明所有权的写入器；三道既有门的代码原样还在；白名单恰好三个文件）。
  **两个自己测试抓到的真 bug（已修并锁住）**：最后一刻重验拒绝、或幂等键已被占用时写入器返回 None，
  而调用方仍给 leg 打标记、写审计行、计为 attached——等于记录一次**根本没发生**的写入。
  **一处自己引入的回归（已修）**：为免安全网被 `instruction_execution_contract_mode` 意外关掉而无条件构造
  交易所客户端，导致没有凭据的角色里 `build_deepcoin_client_from_env()` 抛异常会**拖垮整个运维 cycle**
  （连 6-pre-1 的入场恢复器一起不跑）。改为构造失败只记 warning 并降级 `execution_client=None`。
  **观察（L2，达标）**：窗口 05:40:04Z–06:22:11Z 连续 30 分钟，5 条真实消息、2 个群，
  44 条每分钟采样**零不健康、零重置**（证据 `/root/evidence/phase-6-pre-2/observer-samples.jsonl`）。
  **预期无样本，确认无样本**：全窗口 `unverified_market_legs` 恒为 0，
  安全网三处痕迹（`position_mutation_intents.operation='naked_fill_set_position_sltp'`、
  `execution_events.action='naked_fill_stop_attached'`、
  `runtime_incidents.incident_type='naked_market_fill_safety_net'`）**全程恒为 0**，
  `rescued_legs` 恒为 0。历史 153/153 市价成交满足等式，此网本就预期永不触发；本次只证明它**静默且零写入**，
  **未在生产中被真实触发过**，触发路径只有测试证据。
  **交易所直读：零非预期写入。** 部署前与窗口结束指纹**逐字节一致**
  `29ca3626118f31af7f7610f6ab04d35088e1e53fe09255ec2a78b383ecfaedfb`（均 4 仓 1 挂单）；
  `execution_order_legs` 596、`position_protection_ledger` 683、`position_mutation_intents` 658 全程不变。
  部署后 worker journal 零 error、零 `naked_fill` 相关告警。
  **窗口结束后（09:51Z、09:59Z，已在 A 线 c0520b94 部署之后）出现的两条
  `authoritative_execution_uncertain`（attempt 818/820，raw 15633/15635）与
  `execution_events` 4065 `cancel_revision_entry_leg / submit_unknown`
  （`revision_cancel_not_terminally_confirmed`，strategy `deepcoin:-1002370796392:3633:BTC:short`）
  属于既有的改单撤销路径，与本安全网无关**（本网三处痕迹全零，从未运行）。
  按硬性规则不可重放，需要人来了结——已一并报给指挥会话。
  **一处过程失误**：本阶段的后台等待器用 `pgrep -f observe-6pre2.sh` 判断监视器是否退出，
  而这条 ssh 远程命令自身的命令行**就含有该模式**，于是它一直匹配到自己、永不退出。
  窗口其实 06:22:11Z 就达标了，直到指挥会话 12:31Z 提醒才发现。监视器本身与结论不受影响。
- phase-6-pre-3-completed (2026-09-09, 会话 local_4a6676b0，**全程只读、未下单、未撤单、未改账本**):
  补测第 10 项完成，结论写进 `phase-6-protection-authority.md` 的"补测第 10 项"一节。
  样本：生产自然发生的 `set-position-sltp` **5 个仓位 / 18 次已确认写入**
  （2026-09-07 01:00Z ~ 2026-09-09 04:45Z，含 A-5 跨 13 小时的止损缩量与保本移动），
  远超要求的 3 个；对照 `deepcoin_ws_events` 的 70 条 `TriggerOrder` 帧与
  REST `trigger-orders-pending` 实时读数。
  **(1) `OS` 每次修改都变**——18 次写入拿到 18 个互不相同的 ordId。
  **(2) `TU` 恒等于 posId，30/30 零例外**；入场腿的 `TU: default → posId` 翻转又观测到 5 例，
  翻转值恒为 `OS + 1`。
  **(3) `trigger-orders-pending` 的行是"新增"，不是更新也不是替换，旧行原样留着**——
  posId `1001125195880289` 上实时并存 5 张 TPSL，其中 `...880288`(SL 2530) 与 `...885731`(SL 2535.06)
  **两张都是止损且价格不同**；posId `1001125179691393` 同样并存 SL 2530 与 SL 2535.06。
  **(4) 新旧关联 = `TU == posId`**，可查且稳定。
  **判定：阶段 6 设计前提成立，不需要停下重新设计**（`OS` 变了但第 4 条给出了可查关联）。
  **但第 3 条改变了阶段 6 任务 1 的定义**：`set-position-sltp` 是**叠加**语义而非**修改**语义，
  "改止损"不能实现成"再发一次"——那只会多挂一张，两张触发价不同的止损并存时
  **更靠近现价的那张先执行**，等于修改没生效。正确形状是**先按 `TU == posId` 收全旧单、撤干净、再挂新，
  撤销失败就不许挂新**。已写进阶段 6 文件。
  这也解释了 ARCHITECTURE 第 6 节 A-5b "两张止损单"现象的成因。
  方法：`sqlite3 -readonly` 读库，worker 真实凭据经 `/proc/<MainPID>/environ` 取得，
  `python -B` 不写字节码，只调 GET 类接口。
  顺带把 6-pre-2 的 pgrep 自匹配教训写进 ARCHITECTURE 第 6 节（判活用标记文件或精确 PID）。
- phase-6-pre-6-completed (2026-09-09, 会话 local_4a6676b0): 入场改单授权租约不再成为死锁。
  提交 **`192588ddaac2517ec85215a65e923918f192bfbe`**，2026-09-09T15:15Z 经 tg-deploy 上线。
  **回滚参考 `00719ea010f46eb91f8b3aebe1029dac5c29267e`，但已不能单独回滚**：A 线随后又部署两次
  （`a1b4ff06` step-6c、`127d3a19` step-5e），生产 HEAD 现为 `127d3a19`（`192588dd` 是其祖先，本阶段代码在线）。
  单撤本阶段须另做 revert 提交。
  **根因**：`entry_revision_executor` 在"写入含糊且结果非成功"时直接 return **不归还租约**——
  这条隔离分支本身是对的（含糊写入未澄清时不许别的批次写交易所），错的是**时长**：
  token 随该次调用消亡，租约此后由**无人**持有，10 分钟过期后下一个申请者把文档翻成 `blocked`，
  而 `blocked` 没有任何复位路径。批次 7 走的正是这条分支，此后所有入场被拒直到人工干预，丢了一笔真实入场（raw 15668）。
  **三条改动，均不削弱那条隔离：**
  (a) `_blocked_document` 带上 `owner_pid` / `owner_start_ticks`；**旧形状 `_LEGACY_BLOCKED_KEYS` 保持合法**——
  部署瞬间生产若是旧 blocked 文档，判它非法会把"可恢复的阻塞"变成"解析不了的阻塞"，比原 bug 更糟。
  (b) `reset_blocked_entry_revision_authority`：持有者**可证已死**立即复位；**判不出**（旧文档 / `/proc` 读不到 / 跨机 pid）
  则等与租约同长的宽限后复位。存活判定返回**三态**，`None` 绝不当成"已死"——否则会在持有者正在写时复位掉它；
  身份用 `pid + start_ticks` **成对**，因为 pid 复用会读成"活着"。复位留审计行 + `entry_revision_authority_blocked_reset`
  告警（已进 ALWAYS_NOTIFIED）；idle 文档键集固定 4 键，理由确实只能进审计与告警。
  (c) `release_authority_for_finished_batches`：**独立的收尾扫描**，把仍为已终态批次持有的租约按 generation 归还。
  **刻意做成扫描而不是执行器里的一行**——指挥会话裁定 (c) 在原调用点上有内在矛盾：
  在 `execute_entry_revision` 里"写入含糊"与"批次落终态"是同一刻，就地归还必然让 6 条隔离测试转红。
  做成扫描后：执行器返回时租约仍 held（隔离保持，6 条测试全绿），下一个运维 tick（5 秒）再归还。
  **秒级恢复，而不是"过期 10 分钟 + 阻塞宽限 10 分钟"的约 20 分钟**——对入场而言这就是救回与丢失的差别。
  免 token 归还是一个刻意开的洞，所以它**自己重新读批次**、非终态就拒、generation 被别人拿走就拒，
  并有静态守护测试把调用者集合钉在一个（与 6-pre-2 同形）。
  **测试**：全量 **8134 passed / 4 skipped / 0 failed**。新增 `tests/test_entry_revision_authority_deadlock.py` **18 条**，
  覆盖四条 + 复位后 acquire 可取 + 存活持有者绝不被复位 + 旧格式仍合法 + 两处痕迹齐全 +
  运行中批次不被扫走 + 陈旧 generation 被拒 + 普通释放仍需 token + 调用者集合守护。
  **变异检验双向**：去掉终态守卫转红 1 条、去掉存活守卫转红 2 条。
  **观察（L2，达标）**：15:16:39Z–15:46:43Z 连续 30 分钟，**27 条真实消息、5 个群**，31 条采样
  **零不健康、零重置**，租约取值**恒为 idle**，worker 零 error、零本阶段 warning。
  三条新路径痕迹全为 0（复位审计 / 终态归还审计 / 复位告警）——生产此刻没有卡住的租约，
  **本窗口证明的是新逻辑不误伤，不是它救过一次**；触发路径只有测试证据。
  **一处监视器指标的局限，如实记**：`lease_nonidle_secs` 实际是"距 `updated_at` 的秒数"，
  只有在状态非 idle 时才有意义；窗口内状态恒为 idle，所以该值（最大 4507）**不代表非 idle 持续时长**，
  健康判据 `state != idle && secs > 1500` 也从未触发。指标无害但命名有误导，下次应改为仅在非 idle 时计算。
  **未修、需另立项的同形状风险**：`recovery_live_submit` 新入场路径（`attempted_writes > 0` 时不释放）
  持有者是 `signal:<id>` 而非 `batch:<id>`，**没有批次行可以证明终态**，本次扫描明确跳过它
  （已加测试 `test_a_signal_holder_is_never_swept` 固定该行为）。那条路径的死锁风险仍在，已报指挥会话待裁。
- phase-6-pre-5-completed (2026-09-09, 会话 local_4a6676b0): 撤单回执丢失后自动确认。
  提交 **`89d9657be2a2bf2b4dea5902cd212a3dedd9934c`**，2026-09-09T16:29Z 上线；回滚参考 `2779c2fb`
  （生产 HEAD 随后被 A 线 `9299c228` 取代，`89d9657b` 是其祖先、代码在线；单撤本阶段须另做 revert）。
  全量 **8171 passed / 0 failed**。
  **实现**：两次独立只读确认才定论——仍在 `trigger-orders-pending` = 撤单没生效；不在 pending **且**
  `trigger-order-history` 有终态行（`triggerTime=0`）= 已撤；其余一律 unknown（含任一读失败、两处都查不到、
  history 显示它先触发了），保持 `recovery_required` 并告警。**不在 pending 本身不是任何证据**，
  当成"已撤"会对一张实际已成交的单重发撤单。
  **reconciler 零交易所写入**：确认仍挂着时只盖幂等键 `cancel-retry:<ordId>`（记在 leg 的 error_json）
  并把批次放回 `planned`，重试由**既有的、已审计的** advance 撤单路径执行——不新造第二条写入路径。
  幂等键在重试**之前**盖，崩溃也不会产生第二次。有测试用"任何写入方法都抛断言"的假客户端固定这一点。
  **部署前的确认拦下了一次事故（本阶段最重要的一件事）**：例行核对"新 reconciler 上线后会选中什么，必须为 0"
  时得到 **3**——批次 3/4/6 自 2026-08-17~08-21 冻结，替换意图是 **BTC 多单 60000-73000 而 BTC 现价约 8 万**。
  确认已撤会把它们放回 `planned`，advance 随即按三周前的判断挂出三组单。与 A-3d 部署前必须作废 item 1029 同类。
  **因此新增 `STALE_BATCH_HORIZON = 6 小时`**（与 `ENTRY_ADMISSION_EXECUTION_DEADLINE` 同量级）：
  批次 `planned_at` 超期只告警不动作、保持冻结，新告警 `revision_batch_too_stale_to_resume` 进 ALWAYS_NOTIFIED。
  这是代码层面的修复，不只是绕开这一次。
  **三个陈旧批次已按指挥会话裁定作废**（备份 `research-backup-20260909T161250Z.db` sha256 `8d233b54...`、
  副本演练两次可复现、生产执行与演练逐字一致、`PRAGMA quick_check` ok、演练副本用完即删）：
  批次 3/4/6 → `resolved / stale_batch_voided_2026_09_09`，改单腿 5/6/7/8/10 → `cancelled`（腿 11 已终态未动），
  共 **8 处变更、9 条 `historical_cleanup` 审计行**，全库其余表行数一字未变，
  SYSTEM bot 聚合通知 message_id **4092**，**零交易所动作**。
  **一处判定限度写进审计与通知**：涉及的两张单在 `trigger-order-history` 里查不到，但它们是三周前的单而
  history 有保留窗口，所以"查不到"= 看不到，**不等于**确认已撤；零敞口的依据是三证
  （不在 pending、无成交、账户无任何 BTC 多头持仓）加账本里 2026-08-31 操作员撤销记录。
  **顺带加固**：`resolved` 加进 `TERMINAL_REVISION_STATES`。批次 7 是用户手工了结并明确保留一条挂单的，
  原先只靠调用方检查 `!= "planned"` 保护——防护在调用方而非被调方，任何别的调用方直接走 advance 就会撤掉那张单。
  **观察（L2，指挥会话裁定 (a) 接受）**：第二段窗口 17:14:15Z 起 **30 分钟连续健康**，31 条采样
  **零不健康、零重置**，`head_ok` 全绿，`unsettled_batches=0`、`kept_leg_intact=1`
  （已了结的批次没被复活、用户保留的挂单没被动），零非预期交易所写入。
  **但 `messages=0`，未达 L2 的 ≥5 条门槛，如实记。**
  裁定接受的理由：本阶段 reconciler 的触发条件（`recovery_required + revision_cancel_outcome_unknown +
  completed_at IS NULL` 的批次）在生产中为 **0**，且 `any_nonterminal_revision_batch` 也为 **0**——
  窗口内该路径必然零动作，**与消息量无关**；"再等 5 条消息"买不到关于这段新代码的任何证据。
  该路径**在生产无真实样本，只有测试证据**；"有流量时仍健康"的一般性信号由 6-pre-7 的白天窗口承接。
  流量事实：16:00Z 后一小时仅 4 条、最近 40 分钟 0 条（北京时间凌晨 1:30，KOL 群停更）。
  **两起过程事故，已处理并写进 ARCHITECTURE 第 6 节（提交 `c1210b2d`）**：
  (1) **监视器骗过了我**——A 线 17:06 部署换 HEAD，第一段 **40 条采样每条 `healthy=1`、`window_reset=0`**，
  因为重启只几秒而采样一分钟一次、且 `deploy_sha` 是启动参数不是实时读；**一个跨版窗口冒充了干净窗口**。
  该段归档为 `observer-samples-segment1-interrupted-by-a-line-deploy.jsonl` **不作达标窗口用**，
  监视器加实时 HEAD 比对（`head_ok`）后重新起窗。
  (2) **kill 错 PID**——停旧监视器时杀了父 `bash` 而非脚本本身，旧实例与新实例同写一个采样文件三分钟，
  该段归档为 `segment2-mixed-instances.jsonl` 作废。判进程存活今天栽两次（先 pgrep 自匹配、后 kill 错 PID），
  而"用精确 PID"正是本会话自己写进架构文档的那条。
  证据目录 `/root/evidence/phase-6-pre-5/`。
- phase-6-pre-7-completed (2026-09-10, 会话 local_4a6676b0): 新入场路径的租约同形洞已补。
  提交 `824b19ad`，部署 **`7fd87e5b83efaf0d320b347158d86dbd79215ded`**，2026-09-10T00:51Z 上线，
  **回滚参考 `9299c228`**（A 线 A-8c）。全量 **8184 passed / 0 failed**。
  **补的是 6-pre-6 明确跳过并加测试固定的那个洞**：新入场路径把租约持有成 `signal:<id>`，
  没有批次行可证终态，所以那次的收尾扫描绕过它。现在用 `trade_signals` 的终态做同样的按 generation 归还。
  **终态用白名单**（`submitted / failed / partial_submission_failed / unknown_exchange_outcome`）
  而不是"非运行中"——没预料到的状态一律不动；`pending` / `processing` 不扫；
  generation 或 owner_kind 不匹配不动；静态守护把免 token 归还的调用者集合钉在一个（与 6-pre-2 / 6-pre-6 同形）；
  审计行 `entry_revision_authority_signal_release` 已登记为非交易所写入动作。
  **变异检验抓到一个真实测试缺口**：去掉持有者前缀判别后测试**全绿**——
  `test_the_two_sweeps_do_not_touch_each_others_holders` 名字读起来覆盖双向，实际只测了
  "batch 扫描遇到 signal 持有者"。反向才危险：`batch:7` 与 `signal:7` 同样解析成整数 7，
  没有前缀检查时 signal 扫描会去查**无关的** trade_signal 7、发现它终态、
  然后归还一把**仍在运行的批次**持有的租约。补 `test_the_signal_sweep_refuses_a_batch_holder` 后变异才转红。
  测试由 18 条增至 **26 条**。
  **观察（L2，完整达标）**：窗口 00:52:54Z–01:35:01Z，**11 条真实消息、3 个群**，
  **44 条采样、零不健康、零重置**；全程 `head_ok=1`（实时比对生产 HEAD，本次无跨版）、
  **`released_while_running=0`**（本阶段最不该发生的事：放掉一把仍在运行的持有者的租约——没有发生）、
  `signal_releases=0` / `batch_releases=0` / `resets=0`（生产无卡住的租约，三条路径全程零动作）、
  `unsettled_batches=0`、`kept_leg_intact=1`、`submit_unknown=0`。
  窗口结束时生产 HEAD 仍是 `7fd87e5b`、租约仍 `idle gen28`、worker 零 error。
  **本窗口证明的是新逻辑不误伤，不是它救过一次**；触发路径只有测试证据。
  证据 `/root/evidence/phase-6-pre-7/observer-samples.jsonl`。
  **本轮同时把三条与 A 线共同撞出的教训写进 `docs/ARCHITECTURE.md` 第 6 节**：
  (1) 带命名空间前缀的 id 配上会丢掉前缀的匹配器是一类专门的缺陷，触发条件是
  `batch:7`/`signal:7` 这类 id 遇上 `int()` / `split(':')[-1]` / 子串包含，而**不是**"有两个并列处理器"；
  (2) 「松匹配」本身不是缺陷等级，它落在哪个方向才是——over-alert 可以排期，
  误归还仍在运行的租约必须当场补，这个判据能回答"先修哪个"；
  (3) 名字声称覆盖双向的用例要确认真的两个方向都跑了，**判据是删掉那道检查它会不会红，不是它的名字**，
  靠"记得写反向用例"防不住、靠变异检验才防得住。按第 6 节体例不署名。
- unmanaged-position-1001125178552543 (2026-09-10, 6-pre-4 会话 local_4a6676b0 只读发现，**已发现、未处置、转 A 线归因**):
  **账本以为已平、交易所上仍在的仓位。** pos **`1001125178552543`**（BTC short，`pos=3`，`avgPx=79412.8`，
  `cTime=uTime=2026-09-07T01:23:07Z`）此刻仍在交易所；而 `execution_order_legs` **589**
  （binding **343**，ordId `1001125172997005`）自 **2026-09-08 01:23:03** 起是
  **`manually_closed` / `manual_position_missing`**。
  **【2026-09-10 更正】本条最初写的"整整一天后"是错的**：我把仓位 `cTime` 的 epoch
  `1788830587000` 算成了 09-07，实际是 **2026-09-08T01:23:07Z**。正确时间线（只读核对）：
  leg 589 创建于 09-07 16:09:48（条件单 `1001125172997005` 提交）；
  **01:23:03.102990** 系统 `cancel_trigger_entry` 撤掉该条件单并做 `terminal_entry_cleanup_outcome`，
  同刻把 leg 589 判成 `manually_closed / manual_position_missing`；
  **01:23:07.27–33** WS 连续推来 `Trade` / `Position` / `Order` / `TriggerOrder`；
  **01:23:10.548** `create_backup_stop`，同刻写了 leg 589 的 `last_verified_at`。
  **【2026-09-10 第二次更正：我基于上面时间线做的"归属挂错"推断也是错的，已由 A 线 A-10a 查实推翻】**
  我当时写"`TU = OS + 1`，所以 552543 是 552542 开出的仓位、leg 589 挂错了对象"。**方向反了**：
  `1001125178552543` **本身就是那张成交开仓单的 id**（订单历史：`limit / sell / short / sz 3 /
  accFillSz 3 / avgPx 79412.8 / filled / 01:23:07`），这正是 4.7 节"**普通 order 的 posId == ordId**"；
  而 `1001125178552542` 是**止损单**（挂单里 `triggerOrderType=TPSL / buy / short / sz 3 /
  slTriggerPrice 83000`，保护账本第 667 行记为 `stop_loss`），id 比仓位**小 1**。
  全库没有 552542 的 leg 是**正常**的——止损单不进 `execution_order_legs`。
  **我的错误是把条件单的 `TU = OS + 1`（6-pre-3 观测到的、条件单派生子单的规律）
  错套到了一张普通 limit 单上**，两者的 id 关系本就不同。
  归属审计 `position_attribution_audits` 3802 写的是 `unassigned → verified` /
  `evidence_source=trigger_fill` / `time_distance_ms=0` / 01:23:10.548695——**leg 589 的归属是对的**。
  **A 线查实的真实机制**（一轮 reconcile 内部，`started 01:22:56.17 / finished 01:23:46.99 / 50.8s`）：
  轮次开头取一次 `synced_at=01:23:03.102990` 并盖给该轮所有写入；真实时间 01:23:10.548
  从订单侧认领了 leg 589 ← pos 552543 并挂上备份止损；**之后同一轮的
  `sync_manual_closed_deepcoin_positions` 调 `list_positions()` 里没有 552543**，
  于是把 binding 343 判 `closed / manual_closed_or_not_found_on_exchange`。
  `updated_at` 早于 `recovered_at` 的怪状即由那个轮次级时间戳造成。
  **根因是"单轮持仓快照缺席被当成已平"**，由 A 线 A-10b 修（缺席必须由交易所历史证明、
  或相隔 ≥60 秒两次都看不到；快照为空则整轮跳过；标记时必发告警——此前完全静默，
  正是这个仓位躺了 34 小时无人知晓的原因）。
  **【未证实，不得引用】** 曾有一版推断说"同一轮内持仓端点与订单端点互相矛盾"。
  **A 线自行收回，我已撤下。注意措辞：是"未证实"而不是"已证伪"**——
  A 线做到的是给出一个**不需要端点滞后就能完整解释全部时间戳**的机制，
  这让端点滞后从解释里**变得不必要**，但**不必要 ≠ 已被推翻**：
  那一刻 `list_positions()` 的原始响应早已不存在、事后不可复原，
  没有任何证据能说明端点里当时有没有那个仓位。
  （把"我找到了更简单的解释"写成"我证明了另一种解释是错的"，本身就是把推断当结论——
  这一条是 A 线指出来的，我第一次用【已证伪】/【待验证】这套标记就用错了，一并记在此处。）
  那个更简单的机制是一次 **lost update**，不需要端点滞后：
  `sync_manual_closed_deepcoin_positions` 的 `list_positions()` 在函数**开头**（拍于 01:23:03），
  而仓位 **01:23:07 才诞生**——快照里当然没有它；binding 行却是在中间那几次往返（含一次真实撤单 POST）
  **之后**才读的，那时并发路径已把 pos_id 写了进去。于是盖 01:23:03 的写入者后提交、
  覆盖掉 01:23:10 刚认领好的 `status=active`，只留下它擦不掉的 `recovered_at=01:23:10.548695`——
  这正是"`updated_at` 早于 `recovered_at`"那个怪状的来源。**旧快照回答新事实，不是端点说谎。**
  根因由 A 线 A-10b 修（缺席须由交易所历史证明、或相隔 ≥60 秒两次都看不到；快照为空整轮跳过；
  标记时必发告警）。

  **但这次事故的形状直接改进了 6-pre-4，值得记**：它是"两次读之间世界变了"，
  而我的静默探针恰好也会踩同一个形状——**基线拍摄之后若收到过 WS 帧，那帧已经把变化告诉了我们**，
  此时拿新快照与旧基线比会报出一个"我们并没有漏掉"的差异，进而重连。
  而"有活动之后才进入静默"正是最常见的场景，**不修的话本阶段的收益会被这类必然误报大量抵消**。
  因此新增 `PROBE_REFRESHED`：收到帧即把基线标记为过期，下一次探活**刷新基线并放行**
  （帧刚到过本身就是流当时活着的证据），之后的探活才做真正的比较。
  语义也因此更准确——探针问的是"**自最后一帧以来**有没有变化"，而不是"自某个更早的基线以来"。
  代价写明：帧后第一次探活必放行，检测被推迟一轮（600 → 1200 秒）；换来的是消除最常见的误报。
  **不是裸奔**：按 ARCHITECTURE 第 6 节唯一判据（`trigger-orders-pending` 筛 `triggerOrderType=TPSL`、
  `posSide` 一致、看 `slTriggerPrice`，**不看仓位行的 `slTriggerPx`**）挂着两张止损——
  `1001125178552542`（sz **3**，`slTriggerPrice=83000`）与 `1001125178555463`（sz **0** 即全仓，
  `slTriggerPrice=83166`）；`position_protection_ledger` 里这两条也在且均 `verified`。
  **风险方向**：系统认为自己没有这个仓位，因此**不会再管理它**——改止损、保本移动、止盈收敛、平仓
  都不会作用于它。最坏是**"止损不会被更新"**而非"没有止损"。
  **本会话未做任何处置**（不认领、不改状态、不动交易所）。指挥会话已交空闲的 A 线做只读归因
  （重点：2026-09-08 01:23 那次判定的依据，是否一次读失败被当成了"零"——硬性禁止第 4 条）。
  **对 6-pre-4 的副作用**：它证实本地账本与交易所存在真实漂移，因此静默探活的重连判据
  **不能**用"与本地账本对照"（每次都会不一致 → 每 600 秒照样重连，毫无改善），
  改用**快照指纹自比**；账本差异降级为观测字段。
- phase-6-pre-4-silence-contract (2026-09-10, 6-pre-4 会话 local_4a6676b0): 6-pre-4 改的是一条**既有测试写死的契约**，必须显式记：
  `test_an_open_but_silent_socket_is_treated_as_a_gap`（"开着但静默的 socket 就是缺口"）
  正是本阶段要推翻的断言，已拆成两条覆盖两个方向的测试并逐条变异验证：
  `test_a_silent_socket_whose_picture_moved_is_still_a_gap`（快照变了 → 仍记 silence_timeout 缺口、仍重连）
  与 `test_a_silent_socket_that_missed_nothing_is_not_a_gap`（快照未变 → 不记缺口、保持 HEALTHY）。
  把守卫改成恒真只红前者、改成恒假只红后者，各 1 秒内断言失败。
  **发现过程本身是个教训**：全量测试卡死 3 小时 54 分（CPU 仅 8 分 54 秒），
  两次 faulthandler 因 `exit=True` 不 flush 而无输出；改为写文件后拿到栈，
  显示主线程在事件循环空转、工作线程闲置——**根本不是线程卡住，是探活通过后 `continue`
  回到 recv，0.05 秒又超时、再探活再通过的无限循环**。
  我此前加的 `DEEPCOIN_WS_PROBE_TIMEOUT_SECONDS=15` 是对的防御但不是本次病因，
  "加了超时还卡"曾把我引向错误方向（以为 `asyncio.to_thread` 不可取消）。
  **生产为什么不会这样循环下去**：静默超时 600 秒、listen key TTL 2700 秒，
  `key_limited` 分支在 key 到期前抢先触发，因此连续通过最多 4 次就被 key 轮换强制重连并重新 resync。
  测试里循环不止是因为 `monotonic_ms_provider` 是冻结时钟，deadline 永不到达——
  这是测试替身的性质，不是产品行为。新测试因此都给静默连接加了读取次数上限：
  守卫一旦被改坏必须**快速断言失败**，不能变成又一次全量卡死（卡死的套件什么都报不出来）。
- phase-6-pre-4-probe-cost (2026-09-10, 6-pre-4 会话 local_4a6676b0，部署前只读实测): 阶段文件写的
  "最多三个 GET" **是错的**。生产账本当时有 8 条活跃腿、两个 instrument（BTC-USDT-SWAP、
  ETH-USDT-SWAP）、2 条限价腿，因此一次探活实际 `1 + 2 + 1 = 4` 个 GET。
  真实开销是 `1 + 持仓 instrument 数 + (有限价腿则 1)`，**instrument 数没有代码上界**。
  每 600 秒一次、对着 5/s 配额不构成风险，不加限；但阶段文件、模块 docstring 都已改成实情——
  一个没人执行的上界不能当上界写下来。发现方式：部署前拿生产库只读跑一遍取样口径。
- phase-6-pre-4-double-counted-probes (2026-09-10, 6-pre-4 会话 local_4a6676b0，**第一次部署后 13 分钟自查发现**): 探活在生产上第一次跑通就暴露了我自己造的一个计数缺陷。
  日志证据（08:40:32Z，部署后第一次静默到点）：
  `Deepcoin silence probe missed_nothing (no_change_during_silence): pass=1 reconnect=0 refresh=0`
  **同一次探活还打了第二条** `Deepcoin silence probe: missed_nothing (...) gets=4`——
  后者是我更早写的，加前者时没发现已有一条。观测脚本靠 `grep -c "Deepcoin silence probe"`
  数探活次数，于是样本里 `probes=2 / probe_passes=1`：**探活数悄悄翻倍**。
  形状是"**观测量的定义依赖一个没人保证的不变量**"（一次事件一行日志），
  和 A 线 A-8c、A-10b 的正向计数是同一族。修法两处：
  (1) 合成一条日志（含 `gets=` 与三个计数），并加测试断言"三次探活恰好三行"，
      变异（把日志加回两条）确认变红；
  (2) 观测脚本的正则改成只匹配新格式 `Deepcoin silence probe <status> (`。
  **顺带印证了 4 个 GET**：`gets=4`，与部署前只读实测一致。
  第一个窗口 `messages=0`（什么都没攒到）时重开，代价近似为零。
- phase-6-pre-4-completed (2026-09-10, 6-pre-4 会话 local_4a6676b0，**阶段完成**): 静默到点先探活、证明没漏东西才保住连接。
  **上线**：`3d40a59a0d431b9667f4dcd417728769fe54f3af` 于 2026-09-10T09:07Z（回滚 SHA `7fd87e5b`）。
  全量 8208 passed / 4 skipped / **0 failed**（用例总数 8212，相对 merge-base `dac91806` 的 8191 恰好 +21：
  新文件 `test_ws_silence_probe.py` 20 条 + `test_deepcoin_ws_phase2.py` 45→46）。
  **L2 观察窗**：2026-09-10T09:08:45Z → 09:51:57Z，**43 分 12 秒**，44 个指标样本，
  **零窗口重置、零不健康样本、`head_ok` 全程 1**（生产 HEAD 每个样本现读现比，不是启动参数）。

  | 指标 | 基线 | 窗口内 |
  |---|---|---|
  | `silence_timeout` 缺口 | 3 个 / 30 分钟（43 分钟应有 ~4.3 个） | **0** |
  | 探活次数 / 通过 | — | **4 / 4** |
  | `other_gaps`、`open_gaps`、`criticals`、`submit_unknown`、`ws_deferred_entries` | — | 全程 0 |
  | 消息 / 群 | ≥5 条（L2 门槛） | 5 条 / 4 群 |

  **判据是 `probe_passes=4`，不是"缺口降为 0"**：缺口变少有两个成因（探活挡住了、本来就不静默），
  只看缺口分不开，必须有一个正向计数记下"它差点发生但被挡住了"。这一点是 A 线（A-8c / A-10b 的
  `absence_obs`、`skipped_claimed_after_snapshot`）指出的更一般形式，本阶段两次踩到（见上两条）。

  **三条如实的限制，不得写成"全路径已验证"**：
  1. 窗口内 **`frames=0` 全程**，流从头到尾静默。生产上只验到"探活通过 → 连接保住"这一个方向；
     **"快照变了 → 重连"生产上一次都没走过**，仅有测试覆盖（双向变异验证过）。
  2. ~~43 分钟未达 2700 秒 listen key 硬过期，计划内轮换路径本窗口未走~~
     **——收窗后 58 秒被观测推翻，改记如下**：2026-09-10T09:52:55Z 生产发生一次
     `listen_key_renewal` 缺口，7.8 秒后干净重连（09:53:03）。它在**正式窗口之外**
     （窗口 09:51:57Z 收），因此**是生产实证但不是窗口判据**。
     同时它坐实了本阶段声称的生产上界：连接 09:07:53 → 轮换 09:52:55 = **2702 秒**，
     即 2700 秒 TTL；其间探活恰好 4 次，与"600 秒静默 / 2700 秒 TTL ⇒ 连续通过最多 4 次
     即被 key 轮换强制重连"完全吻合。**这个上界此前只是算术，现在有一次实测。**
     发现方式：写 24 小时追记脚本时试跑，看到 `gaps_other=2` 顺手查了原因——
     另一个是 09:07:53 的 `process_start`（部署本身），5.4 秒。
  3. 残留风险不变且不可消除：**"订阅已死但恰好无事发生"探活无法分辨**。
     该情形下不重连不产生信息损失；暴露上界是下一次探活（600 秒）或 key 硬过期的计划内重连，二者取先。
     生产上界实测：600 秒静默 / 2700 秒 TTL ⇒ 连续通过最多 4 次即被 key 轮换强制重连并重新 resync。

  **未挂追记**：24 小时缺口统计（数量、秒数、占比）对照基线 145 次 / 1060 秒 / 1.23%，**不阻塞阶段完成**，
  由后续会话在 2026-09-11T09:07Z 之后取一次即可。
- phase-6-pre-4-observer-timezone (2026-09-10, 6-pre-4 会话 local_4a6676b0): 观测脚本第二次起窗，
  **首样本就写着 `probes=3` 而窗口才开 0 秒**——`journalctl --since` 按**本地时区**解释，
  脚本传的是 UTC 字符串，服务器 UTC+8，等于把查询窗口往前多开了 8 小时，
  把上一个窗口的探活算进了新窗口。同一时间戳实测：本地解释取到 17169 行，加 ` UTC` 后 117 行。
  **与本阶段早先那次读错 epoch（把 1788830587000 读成 09-07）是同一类错**：
  时间戳在说明时区之前不是一个数。数据库那半边没错（sqlite 存 UTC、比较也用 UTC），
  只有 journalctl 这一路混了口径——**同一个脚本里两种时间口径并存**才是真正的坑。
  修法：journalctl 单独传 `"$WS_ISO UTC"`。前两次窗口的样本作废存档为
  `observer-samples-void-1.jsonl`（旧日志双计数）与 `observer-samples-void-2.jsonl`（时区错配），
  正式窗口从 2026-09-10T09:08:45Z 起，deploy_sha `3d40a59a`。
  **作废前那 27 分钟仍有参考价值但不作判据**：2 次探活全通过、0 个 silence_timeout，基线是 3 个/30 分钟。
- ws-gap-quantified (2026-09-09, 6-pre-1 会话发现，指挥会话记录): 过去 24 小时 145 个 WS 缺口、1060 秒、全天 1.23%，134 个来自 600 秒静默重连；阶段 5 的终态拒绝意味着约 1.2% 的新入场会被静默判死。6-pre-1 改为推迟重试后影响消除；新增 6-pre-4 改静默重连为先探活。item 1022/1023（17 小时的陈旧 pending 指令项）交 A 线 step 6 收尾时作废。
- phase-6-pre-2-approval (2026-09-09, 用户在指挥会话明确批准): 6-pre-2 市价成交裸仓安全网（B-5d，L3）获批领取：市价腿归属 unverified 超 60 秒且该 instId+side 恰有一个无人认领、数量恰等于成交量的活跃仓位时，只挂止损不挂止盈、不认领所有权、attribution 标 unverified_sl_by_unique_candidate 并记 critical 告警；不唯一只告警。
- phase-6d-restart-protection (2026-09-10, 会话 local_4a6676b0, **子步 6d：方向是少写不多写**):
  阶段文件任务 4 的规则是"'本地没有记录'不等于'交易所没有保护'，判断缺失必须以 REST 查询为准；
  禁止因为本地没有保护记录就重新创建一套保护"。**逐路径核查后找到一处真的会重复挂**：
  `naked_fill_stop_net`（6-pre-2 的裸仓安全网）判定"这个仓位无人认领"用的四条前置**全部是本地判据**——
  没有别的腿引用它、没有保护账本行引用它。重启后（或任何一次账本写入没落地之后）这四条**照样全过**，
  而交易所可能正拿着一张我们本地不认识的止损：它会挂上**第二张**。
  该模块此前**从未读过 `trigger-orders-pending`**（只在挂完之后回读一次）。
  **本步新增前置 (e)**：挂之前读一次挂单列表，若该候选 posId 上已有 TPSL 止损 → 不挂、只告警
  （`position_already_protected_on_exchange`）；**读不到算不知道**（`protection_snapshot_incomplete`），
  与"读不到持仓"同规则，绝不当成"没有保护"。挂之前那道 last-moment 复核也同步改为**两个快照都重读**——
  否则它会重问四条前置、而第五条沿用一个已经变旧的读数。
  匹配判据只认挂单行**自带的 posId**：不带仓位 id 的行不计入（可能是在挂入场单自带的止损，
  它属于一张尚未成交的单，在这里什么都不保护）。**这里的松是刻意放在安全方向的**——
  这个函数只可能**阻止**一次写入，误判的代价是一条告警加一次人工瞥一眼，
  漏判的代价是一张活仓上多出来的止损。
  新增两条用例并各自做了变异检验（把"读不到"当无保护、把"已有止损"忽略掉，对应用例各自转红）：
  "本地无记录但交易所有保护 → 不产生重复 TPSL"、"挂单列表读不到 → 算不知道不算无保护"。
  全量 **8291 passed / 4 skipped / 0 failed**。
  **未改的部分**：重启后的重同步本身（阶段 2 已实现）、`position_snapshot_startup`（只读快照循环，
  不挂保护）；确认缺失后的补挂仍走原受控流程（挂 → 回读 → 才落账本），本步没有放宽它。
- phase-6c-close-authority (2026-09-10, 会话 local_4a6676b0, **子步 6c：核实为主，无交易所写入语义变化**):
  阶段文件任务 3 要求"`close_bound_position` 按绑定链的确切 posId 平仓、unverified 拒绝走人工、
  web 角色无执行权限"。**逐条核实的结论是这三条在代码里已经成立**，本步因此没有改交易语义，
  只补上阶段文件明确要求、而此前缺失的"三个独立用例"，并把核实结果记在这里：
  路径 `web → worker_command_jobs → worker`（`close_bound_position` 是四条命令之一），
  `close_bound_position_market` 按精确 `pos_id` 选仓、`_require_verified_binding_positions` +
  网关 `_load_verified_binding` 双重要求 `attribution_status='verified'`。
  新增 `tests/test_phase6_unverified_refusals.py`：修改 / 撤销 / 平仓**三个独立用例**，
  每个都断言两件事——拒绝了，**且交易所客户端一次都没被调用**（只断言 raises 的用例，
  会被"发出去了但随后报错"骗过）。
  **变异检验暴露了这套守卫的真实形状，值得记**：单独关掉
  `require_verified_position_ownership` 时**十条用例全绿**——因为修改与撤销还被网关里
  另一处独立的 `attribution_status` 比较挡着；把两处一起关掉才有 6 条转红。
  也就是说**单点变异检验在纵深防御下会给出"测试没在测"的假象**，要证明用例真的咬住，
  必须把**同一条性质的所有守卫**一起关掉。
  **同时暴露了我自己一个更糟的写法**：平仓用例最初拿到的拒绝是
  `position_not_bound_to_exactly_one_active_binding`——因为夹具没给 binding 写 `pos_id`，
  于是它**因为夹具的原因被拒**，而不是因为归属未核实；这正是那条用例自己的注释警告的
  "refusal for the wrong reason looks exactly like a refusal for the right one"。
  修好夹具后单点变异即可让三条平仓用例转红。
  全量 **8289 passed / 4 skipped / 0 failed**。
- phase-6c-premise-correction (2026-09-10, A 线 local_22ee72a5 核出、本会话复核确认, **更正上一条的前提**):
  上一条我写的"`PositionMutationAuthorityError` 只从 `submit()` 之前抛出，**因此**等于一次交易所写入都没有"
  **不成立**，有两条写入之后仍会抛出它的路径：
  (1) 写入返回后 CAS `submitting → submitted` 失败（并发方动过该 intent）→ 返回库里当前状态 →
      落到 `_require_submitted_response` 的兜底 `raise PositionMutationAuthorityError`；
  (2) **不需要并发方的那条**：放行条件是 `status in {submitted, confirmed} and response is not None`，
      而 `_intent_result` 的 `response=_load_json(row.response_json) or None`——
      **回执没存好（空/存不下/解不出），一次落地的写入就会被报成授权错误**。这条比 (1) 更容易发生。
  **错法本身值得记**：我是从"哪些地方会抛"推出"抛出时系统处于什么状态"的，
  中间缺了一步"**还有谁会抛**"；而且报出去时把推理当成了核对结论。
  A 线是**去读那个兜底本身**才发现的——判断一个异常类型意味着什么，要读它的**全部**抛出点，
  不是读你想到的那几个。
  处置：A-11 改成白名单（只有可证明未提交的状态才算确定失败，其余一律 `DeepcoinRequestOutcomeUnknown`）。
  **本会话 6a/6b 的替换件没有踩这个洞**（已核）：只把 `DeepcoinDefiniteRejection` 当确定失败，
  其余一切（含该异常）→ `outcome_unknown` → 冻结告警，恰在安全侧。
- failure-must-stop-on-the-over-protected-side (2026-09-10, A 线 local_22ee72a5 在 A-11 读出、本会话引用):
  A 线改补救平仓腿的失败分类时发现：那条腿在尝试平仓**之前撤过保护**，所以拒绝而不还原
  会把"平仓被拒"变成"仓位现在裸着"——**两个失败里更坏的那个**。
  这与 6a"撤旧失败保留新单、只告警冻结"是同一个判断方向：
  **失败时要停在"过度保护"那一侧，不能停在"没有保护"那一侧。**
  另附一条同源观察：**修正一个错误分类会顺手拿掉它附带的噪音，而那噪音可能正是唯一在报警的东西**——
  管理平仓腿原本标错成 `submit_unknown`，至少还冻结批次逼人来看；分类改对之后失败变得很安静，
  所以必须补 `management_close_authority_refused` 告警，它成了"KOL 要求平仓而什么都没平"唯一的出口。
- phase-6c-finding-close-refusal-mislabelled (2026-09-10, 会话 local_4a6676b0 只读发现,
  **不在本步改，报指挥会话定归属**): `strategy_management_executor` 的自动平仓腿
  （批次执行，非 worker_command 的人工平仓）在网关因归属未核实而**拒绝写入**时，
  拿到的是 `PositionMutationAuthorityError`，而它落在**通用 `except Exception`** 分支里 →
  腿被置为 **`submit_unknown`**、reason `submission_outcome_unknown`。
  但 `PositionMutationAuthorityError` 只在 `_block`（全部发生在 `submit()` 之前）与
  授权构建阶段抛出，**意味着一次交易所写入都没有发生**。
  把"我们拒绝写"记成"可能已经写了"，代价是：批次被冻结等人处理一个从未被触碰的交易所状态，
  并按 `ALWAYS_NOTIFIED` 发出 `management_submit_unknown` 告警。
  阶段文件任务 3 的范围是 `close_bound_position`（人工路径），而这条在管理批次执行器里、
  属 A 线模块，**故本步不动**，交指挥会话裁定归属。
- phase-6cd-window-closed (2026-09-10, 会话 local_4a6676b0, **判据首次取得生产样本**):
  窗口 15:54:19Z ~ 17:46:49Z（**1 小时 52 分**）、113 采样、**零重置、head_ok / units_ok 全程 1**、
  125 轮 reconcile。**真实消息 4 条 / 1 群，未达 L2 的 ≥5 条门槛**——按指挥会话裁定按现状收窗，
  因为**消息数对本步判据没有信息量**：本步判据要的是活仓与活保护单，不是消息流量。
  证据 `/root/evidence/phase-6cd/observer-samples.jsonl`。
  **16:07Z 交易所开出两个仓位**（`1001125216121996` / `1001125216153672`，BTC 多头、限价入场、
  binding 349/350、`attribution_status=verified`），窗口性质因此从"零样本"变为有样本：
  ```
  positions=220  cancel_precheck={"match":220}  excluded_pending_entry_stops=440
  set_mismatch=220  agreed=0  chain_frozen=0  ledger_drift=0
  ```
  **判据逐条**：
  - **6b (1) `cancel_precheck.match ≥ 1`：取到，220/220**——跨两次真实读、四项一致，零不一致。
  - **6b (3) `ledger_drift` 读数：取到，为 0**——这是"有活保护单在场时测得 0"，不是空窗的零。
  - **6a (3)：字面未达成、实质达成**（见下条判据错误）。
  - **6a (1)(2) 与 6d 全部判据：未取到**——窗口内无真实止损替换、无保本收敛、裸仓网一次未触发。
  - **6b 切换放行条件仍未满足**：条件是"一次**真实止损替换**走通新路径并回读一致"（判据 (1)），
    本次到来的是新仓位归属，不是替换。**不以此充数。**
  **三方联合证据**：本窗口 HEAD 同时含 A-11、6c、6d，所以"无回归"是三方联合的，非任一单独。
- phase-6a-criterion-3-was-written-on-a-false-assumption (2026-09-10, 会话 local_4a6676b0):
  我把 6a 判据 (3) 写成"新入场成交开出仓位，**新链把它解析成 `agreed`**"。
  实际到来的样本是 `set_mismatch`，**而 `agreed` 在这种情形下根本不可能成立**：
  `agreed` 的定义是新链与旧匹配器给出同一个 ordId 集合，而旧匹配器对这两个仓位给出的是
  **`absent`**（账本里没有任何行能给出那两张随单止损的归属）。
  **我写判据时假设了"两边都会认出来"，而这一步存在的理由恰恰是旧匹配器认不出来。**
  判据改写为：**新入场开仓后新链 `status=resolved` 且认出该仓位的保护单**（无论旧匹配器说什么），
  若同时有在挂入场单的自带止损则 `excluded_pending_entry_stops ≥ 1`。
  **同源教训**：判据也会写错，而写错的判据在收窗时会伪装成"未达成"。
  这与"窗口合格 ≠ 判据被验证"是一对：那条防的是拿窗口充数，这条防的是**拿一个不可能成立的
  判据把真样本判成没取到**。
- check-before-you-act-on-someone-elses-description (2026-09-10, 会话 local_4a6676b0 与 A 线互查后记):
  A 线把我一句"**我会**确认那个安全阀对这条路径也生效"读成"已经加了"，并据此建议我"回头简化那道多余的闸"——
  **而那道闸根本不存在**（`protection_order_unattributable` 冻结是 6a 就有的，不是为此新增的）。
  我核了才发现，于是没有去简化一个不存在的东西。
  **为什么这次被发现，值得说清楚**：不是因为我警觉，是因为**它的建议要求我动手**，
  而我动手前先看了对象在不在。**如果那条建议是"保持现状"，我不会去核，那个错误印象会一直留着。**
  所以防线不是"我核了"，而是：**任何需要动手的建议，动手前先看一眼对象是否如描述。**
  同族的还有：A 线把一次推理当成实测写进结论（"会挂新不撤旧"，实为整批拒绝），
  我则差点把自己一句"我会加"当成"已经加了"来接受简化建议——**两个都是叙述跑在事实前面**，
  区别只在于错的是自己的判断还是别人的状态，而后者**只有对方能发现**。
- observation-instrument-gap-repeated (2026-09-10, 会话 local_4a6676b0, **同一个错，两小时内犯第二次**):
  6e 起窗后第一份采样里**没有 `would_adopt`**——而那正是判据 (1) 要的那个数。
  轮日志里它在（`would_adopt: 2`，判据其实达成了），是**观测脚本没有抽取它**。
  两小时前我刚把同一件事写成教训（`phase-6b-shadow-window-criteria` 末段：
  "写判据只做了一半，另一半是确认观测装置真的会记下这些判据所需的数"），
  当时补的是 `cancel_precheck` / `ledger_drift`；`would_adopt` 是**那之后才加进代码的**，
  于是脚本又落后了一步。
  **这次和上次的区别，正是它值得单独记的原因**：上次是"想不到要检查"，
  这次是"知道要检查、但检查不在流程里，而我在加完计数之后没有再想起来"。
  **与"推共享分支"那次同形**（一条只在人记得时才执行的判据等于没有判据），
  也与"默会的正确做法在被明文化的那一刻最脆弱"同源：
  **我把'加一个观测量'和'让采样器抽它'当成了两件事，而它们必须是一件事。**
  真正的解法是让它们不可分离——加计数的那次改动本身就该同时改采样器，
  或者让采样器不做白名单抽取、而是把整个 `protection_shadow` 对象原样落进采样行。
  **后者更彻底**（新增字段自动进证据），作为后续改进记在这里。
  已补 `would_adopt` 并重起窗（18:21:10Z），损失一分钟。
- fake-responses-built-from-requests (2026-09-10, A 线 local_22ee72a5 在 A-13 归纳、本会话自查确认):
  **用"我们发出的请求"回填出来的假响应，会让任何"把请求形状的键读在响应上"的代码通过测试。**
  break-even 的假客户端把 `payload["posId"]` 与 `payload["slTriggerPx"]` 原样写进假挂单行，
  而 `trigger-orders-pending` **两个都不返回**（它给 `slTriggerPrice`，且没有仓位 id）——
  于是生产恒拒、测试全绿。
  **本会话按这条标准自查了 `tests/test_deepcoin_execution_actions.py`**：它的假客户端**有同一个写法**。
  改成真实响应形状（去掉回填的 `posId`、改真实键名、补 `triggerOrderType`）后 **138 条一条没红**——
  本会话这条链的归属走账本与 `TU`，不依赖挂单行自带的 `posId`。
  **但改动保留了**（`cafdf208`）：即使当前用例不依赖，**夹具会一直向下一个人示范错误的词表**。
  **"当前没坏"不是保留一个错误示范的理由。**
  **值得补的一句**：这条的可怕之处在于**假响应越像真的越危险**——一个敷衍的假响应（`{}`）会让
  测试立刻红，而一个用请求精心回填的假响应看起来最专业、也最能骗过所有人。
- position-evidence-stop-loss-is-not-a-protection-criterion (2026-09-10, A 线提请、本会话查证):
  `execution_bindings.build_position_evidence` 3177/3178 从**仓位行**取 `slTriggerPx` / `tpTriggerPx`
  填 `PositionEvidence.stop_loss` / `take_profits`。键名在仓位行上是**对的**，
  但 ARCHITECTURE 已明文"仓位行那两个字段不能当'有没有止损'的判据"，所以要查它被谁消费。
  **查了：`PositionEvidence.stop_loss` 只参与 `position_attribution` 的入场归属经济学比对
  （把仓位与入场腿的止损意图对上），没有任何一处拿它当保护判据**——那条判据在
  `protection_snapshot` / `protection_health`，走 `trigger-orders-pending`。**用途正确，不改。**
  记在这里是为了让下一个人不必重新查一遍。
- phase-6h-linkage-authorization-resolved (2026-09-11, 指挥会话给出出示原文, **我的担心基于一个错前提**):
  A 线即将做限价入场止盈首次真实写入时，我与 A 线都停下，理由是：
  "止盈成交后自动把止损移到成本价"这层连带，**似乎没有在任何一次出示里被单独说过**——
  用户批 6h 时限价仓还拿不到止盈，那条前提"读起来像遥远的将来时"。
  **指挥会话给出了出示原文，前提不成立。** 6h 申请原文：
  *"允许系统在四项前提全部满足时，把主止损从 75700 抬到成本价 77000。
  前提是第一档止盈已证实成交……批准后在止盈修好前不会产生任何交易所动作"*；
  止盈申请同页第二条：*"批准 6h（保本移止损）：它在止盈成交后才会有动作"*。
  **用户一条回复同时批了两项。** 所以这层连带**正是 6h 出示的本体**，不是未说的连带。
  **我错在哪**：我把"我没有看到那段出示原文"当成了"那段出示不存在"。
  两者的区别是本仓库反复写的那条——**未读到不等于不存在**，
  而我这次是把它用在了授权链上，代价是两条线各停了一轮。
  **正确做法本该是：先向指挥会话要出示原文，再决定要不要停**，
  而不是先停下再问。停是安全的方向，但它不是免费的。
  **仍然成立的那一半，如实留着**：我的 6h 切换是**基于转达**执行的
  （指挥会话转述"用户原话：批准 6h"），**我没有直接向用户求证**；
  同一时刻 A 线对它那一步是**直接问了用户**才动的。
  **这次结果证明转达是准确的**，但"转达准确"是事后才知道的，
  **它不改变两种做法在动手那一刻的证据强度差异**。
  指挥会话已另行向用户索取一句明确的"确认连带"并将原样转达双方。
- phase-6g-shadow-window-closed (2026-09-11, 会话 local_4a6676b0, **甲类全部达成、乙类无样本**):
  上线 **`6457b77e55924c883632f6afe4c57160b160aaad`**，回滚参考 **`6863d66c`**。
  四步部署四项全绿，第 4 步用判定式检查（`sed` + 空判定，事前用一正一反验过）。
  **全量 8386 passed / 4 skipped / 0 failed**——**合并共享分支时带进了 A 线的
  `trigger_take_profit_convergence_executor.py` 改动，所以这是在新候选上重跑的一次**，
  不是沿用合并前那次同样数字的结果。
  生产源码已核实影子在线（`observe_cancel_precheck` 出现 3 次：1 import + 2 撤单点）。
  窗 **09:43:21Z ~ 10:13:29Z（1810 秒）**、**31 采样、零重置、零不健康、
  `head_ok`/`units_ok`/`reads_ok` 全程 1**、**27 轮**、`WINDOW_MET`。
  **甲类（本窗可判定）逐条**：
  (1) 仪表在线：上述健康项全程成立 ✓
  (2) **行为零改变**：`be_writes` / `mgmt_writes` / `other_writes` **全程恒 0**
      （按 idempotency key 前缀三分归因，**不是数总数**——
      "有没有东西写"回答不了"6g 仪表有没有写"，而它按设计一笔都不该写）✓
  (3) wall clock 字段：**本窗无任何 intent 产生，故无样本**——如实记，不算达成也不算失败。
  **乙类（本窗不可判定）**：`precheck_events` 全程 **0**。
  **这是"无样本"，不是"通过"**——管理保护替换约每 4 天一次，本窗期望值约 0.005。
  **收窗表述按起窗前写死的形式**：**甲类全部达成、乙类无样本**。
  **不得写作"6g 影子窗通过"。**
  证据 `/root/evidence/phase-6g/observer-samples.jsonl`。
  **P1 观察器同期独立运行并已停**：09:28:09Z ~ 14:44:21Z、**317 条逐分钟采样**、
  `convergences` 恒 2、`break_even_intents` 恒 0、`held_records` 恒 0、**零足迹**。
  它覆盖了 P1 前提成立的**全部 5 小时 16 分钟**（09:23 成立 → 14:42 因手工平仓消失）。
  **所以"P1 未取样"不是"没人去看"，是"看了 317 次，什么都没发生"**——
  这个区别正是当初设这个观察器的理由。
  证据 `/root/evidence/p1-watch/p1-samples.jsonl`。
- phase-6h-tpsl-after-close-n2 (2026-09-11, **用户手工平仓提供了 n=2，且比 n=1 覆盖更宽**):
  用户在交易所手工平掉两笔 BTC 多单（陈哥群仓位）。**这给了那条 n=1 结论第二个样本，
  而且是我们无法自己造出来的那种**——平仓不是本系统发出的。
  **交易所侧（`trigger-order-history`，逐单读，原样）**：
  ```
  1001125216121995  主止损 75700      cTime 09-10 15:08:17  uTime 09-11 14:42:10  triggerTime 0
  1001125219289222  备份止损 75548.6  cTime 09-10 20:36:43  uTime 09-11 14:42:10  triggerTime 0
  1001125226308043  止盈 79800×7      cTime 09-11 09:23:25  uTime 09-11 14:42:10  triggerTime 0
  1001125226308486  止盈 81900×8      cTime 09-11 09:23:28  uTime 09-11 14:42:10  triggerTime 0
  1001125216153671  主止损 75700      cTime 09-10 15:11:25  uTime 09-11 14:42:12  triggerTime 0
  1001125219582177  备份止损 75548.6  cTime 09-10 21:09:49  uTime 09-11 14:42:12  triggerTime 0
  ```
  **每个仓位的整套 TPSL 在同一秒转终态**（…121996 的四张在 14:42:10、…153672 的两张在 14:42:12，
  两仓相隔 2 秒平掉），**全部 `triggerTime = 0`，一张都没被触发**。
  收窗时刻交易所 `trigger-orders-pending` **为 0 条、活仓 0 个**。
  **排除本系统撤单**：全库提到这六个 ordId 的 `cancel*` intent **0 条**；
  近 8 小时全部 intent 只有三条——670（另一仓位的管理平仓）、671/672（A 线两张止盈）。
  **`break-even:` 前缀 intent 有史以来 0 条**。
  **n=2 比 n=1 强在两处**：
  (1) **覆盖了止盈**——n=1 只有一张备份止损，当时明确记着"未验证止盈类是否同样处理"，现已验证：同样作废；
  (2) **平仓不是本系统发出的**——n=1 那次是我们自己的 `close_position`，
      本次是用户在交易所手工平的，**所以它排除了"是我们的平仓路径顺手做了什么"这个解释**。
  **仍然不改的措辞**：这是 n=2，不是规律。两次都在 BTC/ETH、都在本账户、都在几秒内；
  未覆盖"部分平仓后剩余 TPSL 如何"（那是 6g 的 `precancel` 场景，仍未取到样本）。
- pending-observations-registry (2026-09-11 起维护, 指挥会话要求, **每次收窗时顺带核对本节**):
  **本节登记的是"判据已写死、但样本到达率低于任何单个窗口分辨率"的观测项。**
  它们不属于任何一个已收窗口；**每个新窗口收窗时必须顺带核对本节是否已有样本到达**，
  到达即在对应条目下补记结果并划掉。
  设本节的理由：这类观测项**没有任何时刻会提醒人去看它**——
  窗口有收窗动作，它没有。而本仓库已经写下过：
  **一条只在人记得时才执行的判据，等于没有判据。**
  **(P1) break-even 释放后的第一次真实替换**（6h 切换）
  - 触发条件：这两个仓位之一 **TP1 真实成交** → 产生 break-even convergence →
    决策为 `set_break_even` → 闸门放行。
  - 到达率：取决于 A 线放开限价入场止盈（`TAKE_PROFIT_LIMIT_ENTRY_RELEASED_POS_IDS`）；
    在那之前**结构上不可能到达**。
  - 要核对什么：按 A-5e 顺序（先挂 77000 → 回读 → 只撤主止损 → 确认撤净 → 才动账本）；
    备份止损**未被一并撤掉**；下一轮由 `trigger_backup_stop_executor` 按新主止损重挂。
  - **状态：前提于 2026-09-11 14:42Z 消失，未取得样本。** 用户手工平掉了两笔仓位，
    仓位没了、TP1 不可能再成交、convergence 不会产生。**P1 在"可到达"状态下只存在了约 5 小时 19 分钟
    （09:23:16Z ~ 14:42:10Z），期间零足迹**（观察器逐分钟采样，`convergences` 恒 2、
    `break_even_intents` 恒 0）。**释放常量里那两个 posId 现已无对应仓位，按裁定保留不动。**
    **这一条记为"未取样"而不是"通过"**：闸门放开了、前提成立了、然后前提消失了，
    **中间什么都没发生，而"什么都没发生"不证明闸门会正确工作。**
  - **（历史）状态（2026-09-11 09:23:16Z 起）：前提已成立，等待 TP1 成交。**
    A 线给 `1001125216121996` 挂上止盈 `1001125226308043`(79800×7) 与
    `1001125226308486`(81900×8)，**我独立读交易所核实**：挂单 4 → 6，
    两仓主止损 75700 与备份 75548.6 均仍在，break-even 未动。
    **在此之前 P1 是"结构上不可能到达"，此刻起是"随时可能"**——
    而这个变化本身不产生任何事件，所以在此登记。
  **(P2) `full_exit` 分支在真实决策下仍被扣住**（6h，用户未批准该支）
  - 触发条件：同 P1，但决策落在 `full_exit`（市价停在入场价亏损一侧那一瞬）。
  - 要核对什么：`break_even_would_close` 事件产生、`released` 为 `false`、**零 `close_position`**。
  - **状态：未到达**（6h 影子窗取到过 `full_exit` 分支的**影子行**，
    但那不是执行器事件——两者不可混记）。
  **(P3) 管理路径撤前四项回读的第一个判定**（6g 影子）
  - 触发条件：一次真实的管理保护替换（任一路径）。
  - **到达率实测**：近 21 天管理批次 17 个、其中产生保护替换的 **5 个**——**约每 4 天一次**。
    30 分钟窗内期望值约 **0.005**。
  - 要核对什么：`management_cancel_precheck_shadow` 事件的 `verdict` 分布；
    `unchanged` 之外的比例决定 6g 是否该从"只记"改成"拦截"。
  - **自己叫人**：首次判定与每次非 `unchanged` 判定落
    `management_cancel_precheck_observed`，已进 `ALWAYS_NOTIFIED_INCIDENT_TYPES`。
  - **状态：未到达。**
  **(P4) `precancel` 场景的真实裸露时长**（6g，原 6g-2 之一）
  - 触发条件：一次真实的风险削减批次（先撤光保护 → 部分平仓 → 重挂）。
  - 到达率：该路径历史总计 **17 笔 / 4 个仓位**，**近 21 天 0 次**。
  - 要核对什么：同批次内 `observed_at_wall` 的最早与最晚之差
    （这正是现在不可查、而 6g 仪表补上的那个量）。
  - **状态：未到达。**
  **(P5) `_restore_precancelled_protection_for_rejected_close` 是否真兜住过**（6g，原 6g-2 之二）
  - 触发条件：一次 `precancel` 之后**平仓被拒**。
  - **状态：未查**（需逐笔看那 17 笔的平仓回执，排在 6g 影子起窗之后）。
- phase-6h-cutover-window-closed (2026-09-11, 会话 local_4a6676b0, **切换窗达成；它补上了影子窗的空白，也留下一个相反的空白**):
  上线 **`c99b33b21cc23404093f31ccf2d5e5d920455859`**，回滚参考 **`29f7e09a`**。
  全量 **8375 passed / 4 skipped / 0 failed**。四步部署四项全绿。
  **生产源码已逐行核实**（读服务器上的文件而非看提交）：
  `BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS = {"1001125216121996","1001125216153672"}`、
  `BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS = frozenset()`。
  窗 **08:04:10Z ~ 08:34:20Z（1812 秒）**、**31 采样、零重置、零不健康、
  `head_ok`/`units_ok` 全程 1**、**25 轮**、`WINDOW_MET`。
  **判据逐条**：
  (1) `be_writes == 0`（按 idempotency key 前缀 `break-even:` 归因）✓，
      `other_writes == 0` ✓，`released_events == 0` ✓；
  (2) 影子行：25 轮 **100 / 100 / 100**（examined / resolved / legacy_refused）、
      `read_failures == 0`、每轮两仓各 1 行、撤单集合 ⊆ 主止损且不含备份 ✓；
  (3) 零非预期写入：全窗 `position_mutation_intents` **零新增**，无需归因 ✓；
  (4) `released_closes == 0` ✓（但见下）；
  (5) 30 分钟连续、零重置 ✓。
  **(1) 的 0 的意义，与起窗前写的一致，不改口**：闸门开着而没有写入，
  **是因为没有 convergence（无 TP1 成交），不是因为闸门挡住了**。本窗不证明闸门有效。
  **本窗补上了影子窗的空白**：市价整窗在入场价 77000 上方，
  **`actions_seen` 全窗 `{"set_break_even": 50}`（25 轮 × 2 仓）**——
  这正是 `phase-6h-shadow-window-closed` 里记为"本窗未取到样本"的那一支。
  两窗合起来，`set_break_even` 与 `full_exit` 两条分支**各有 25 轮以上的真实样本**。
  **但本窗留下一个方向相反的空白，必须同样如实记**：全窗无 `full_exit` 轮次，
  所以判据 (4) 的前半"`would_close` 照常记录"**本窗无样本**；
  只有后半 `released_closes == 0` 成立，而它是**平凡成立**的
  （没有 `would_close` 可言，自然没有被标记为 released 的）。
  **上一窗只取到 `full_exit`，本窗只取到 `set_break_even`——没有任何一窗同时取到两支。**
  这不是缺陷，是市价决定的；但"两窗合起来覆盖了两支"这句话，
  与"某一窗同时验证了两支"**不是一回事**，不能混写。
  证据 `/root/evidence/phase-6h-cutover/observer-samples.jsonl`。
  **待观测项（跨阶段，不属任何已收窗口）**：A 线放开限价入场止盈、TP1 真实成交之后的
  **第一次 break-even convergence**——那一刻才第一次检验：
  释放常量里的仓位真的执行替换（A-5e 顺序、只撤主止损、备份下一轮重挂），
  且 `full_exit` 分支即使决策为它也仍被空集挡住。
- phase-6g-scope-corrected-before-writing-it (2026-09-11, 会话 local_4a6676b0, **指挥会话拦下我一句印象**):
  我报告 intent 670（`management:163:141:close:`，2026-09-11 07:18:56，市价平掉 pos
  `1001125222877510` 7 张，`succeeded / management_close_exchange_confirmed`）时写道：
  自动管理路径"**不走绑定链、不走撤前四项回读**"。**指挥会话要求与 A-11 记录对齐后再定 6g，不要按印象写。**
  **查完了，前半句是错的。** 实际调用链（从 idempotency key 入手逐层追）：
  `strategy_management_executor:841 / 1599` → `close_exact_position`（模块级适配器，
  `position_mutation_gateway:968`）→ `:982 _build_fresh_authority` 重建
  `PositionMutationAuthority` → 网关方法 `:283 _load_verified_binding`。
  撤单侧同理：`strategy_management_executor:3894` → `cancel_exact_position_sltp`
  （`position_mutation_gateway:936`）→ `:949 _build_fresh_authority`。
  **所以管理路径的平仓与撤单本来就走绑定链、本来就要求 verified 归属。**
  **后半句要拆开，其中一半是范畴错误**：平仓**无撤单动作**，"撤前回读"对它不适用；
  撤单侧 `_cancel_old_protection_after_replacement`（3880-3905）**确实缺撤前四项回读**——
  它的 docstring "每笔替换完成回读之后才撤旧"回读的是**新单**，不是**即将被撤的那张旧单**。
  **6g 的真实范围**：不是"接上绑定链"（已接），而是**接上 6a 共用件 + 6e 撤前四项回读**。
  证据：`grep pre_cancel_check src/telegram_kol_research/*.py` 命中
  `break_even_convergence_executor` / `deepcoin_execution_actions` / `protection_replacement`，
  **不含 `strategy_management_executor`**；后者也未导入
  `replace_stop_group` / `replace_take_profit_group` / `resolve_protection_authority` /
  `evaluate_cancel_precheck`，它有自己一套替换序列。
  **intent 670 作为 6g 起点样本要降一级**：它是**平仓**，而平仓正是这条路径上**已经正确**的那一半，
  **它不能证明 6g 要修的缺陷**。6g 需要的起点样本是一次**管理指令驱动的保护替换**，
  阶段文件里先只读查：历史上走过几次 `_cancel_old_protection_after_replacement`、
  有没有"撤掉了一张已经不是当初那张的单"的痕迹。**查完再定改什么。**
  **形状**：我把"A 线查的止盈三道门"与"管理路径"两件真事接成了一条没验证的因果——
  与 2026-09-10 我在 break-even 上犯的是同一个错（把仓位行观测接到 break-even 缺陷上）。
  当时我自己写下过判据："把结论交给别人之前再问一遍它回答的是哪个问题。"
  **这次我没做到，是上游拦下的。** 上次我自己发现，这次没有——
  **所以那条判据目前只在我想起它时生效，等于还没有落成机制。**
- phase-6h-approval (2026-09-11, 用户在指挥会话 local_858790fe 明确批准，原话"批准 6h"):
  **批准的是"移止损"那一支，`full_exit`（市价平掉整仓）同页出示、明确未批准。**
  两个常量因此分开：`BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS` 加入
  `1001125216121996` / `1001125216153672`；
  **`BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS` 保持空集。**
  **出示给用户的明细逐项如下（与实现逐字对应）**：
  **触发前提四项，缺一不发生**——(a) 该仓位 **TP1 已被证明成交**（convergence 的唯一入口）；
  (b) `move_stop_to_breakeven_after_tp1=True` 且 `management_execution_mode=live`（均已开）；
  (c) 前置校验通过（主止损在交易所、价格与账本一致、数量与均价未漂移）；
  (d) **市价停在入场价的盈利一侧**（多头即 > 77000）。
  **动作**：按 A-5e 顺序**先挂后撤**——挂新止损
  `{"instType":"SWAP","instId":"BTC-USDT-SWAP","posId":<仓位>,"slTriggerPx":"77000",`
  `"slTriggerPxType":"last","slOrdPx":"-1","sz":"15"}`
  （**这一张带 `sz`**，与 6f 那张备份止损不带 `sz` 不同）→ 回读确认 →
  **只撤主止损**（`…121995` / `…153671`，75700）→ 确认消失 → 才动账本。
  **备份止损不动**（`…219289222` / `…219582177`，75548.6），由
  `trigger_backup_stop_executor` 下一轮按新主止损 77000 重算并按 A-5e 顺序替换。
  **净效果**：止损从 75700 抬到成本价 77000；**全程任一时刻仓位都有止损，无裸露窗口。**
  **失败处置**：新单挂不上 → 不撤任何东西、报事故；旧单撤不掉或结果未知 →
  **新单保留**、`recovery_required`、报人，仓位处于"两张止损并存"的**过度保护**而非裸仓；
  撤单被接受但回读仍在 → 同上，且账本**不会**把仍在交易所上的单标记为已退休。
  **`full_exit` 出示内容（未批准）**：市价在入场价亏损一侧时，政策的答案是
  `close_position` / `ordType=market` / **平掉全部 15 张**、**不先撤止损**；
  遗留 TPSL 由交易所在平仓后自行作废（历史证据 n=1：一条 ETH 备份止损平仓后 **11 秒**
  转终态、`triggerTime=0` 从未触发、我方零撤单记录）。
  **为什么必须分开批准**：两支来自同一个决策，**由市价那一瞬间落在入场价哪一侧决定**——
  6h 影子窗内实测该开关 1 分钟内翻过一次（77156.6 → 76957.8）。
  合用一个开关等于批准"移止损"时把"市价平掉整仓"一起批了，而后者的后果是仓位没了。
  **补充（2026-09-11，用户对"止盈成交 → 自动移止损"这层连带的单独确认，原话"确认连带"）**：
  出示原文为——*"止盈单挂出后，第一档止盈（79800 或 80000）一旦成交，系统会自动把该仓位的
  主止损从 75700 移到成本价 77000：先挂 77000 新止损、回读确认、再只撤 75700 主止损，
  备份止损由下一轮按 77000 重算重挂，不平仓（'全部离场'分支仍关闭）。"*
  **为什么在已经批过 6h 之后还要这一句**：批 6h 时限价入场拿不到止盈，
  那条前提读起来像遥远的将来时；A 线放开止盈把它变成了现在时。
  **同一句出示在两个时刻的含义不同**——文字没变，而它描述的事件从"不可能发生"
  变成了"随时可能发生"。这一句确认针对的正是这个变化，不是针对文字。
  （我与 A 线曾因此各停一轮，原因是我把"我没读到那段出示"当成了"那段出示不存在"，
  见 `phase-6h-linkage-authorization-resolved`。前提是错的，但这一句确认本身是有价值的。）
- phase-6h-cutover-window-criteria (2026-09-11, 会话 local_4a6676b0, **起窗前写下**):
  **收窗判据**：
  (1) **`released_events == 0`**——无 TP1 成交则无动作。**这一条预期为 0，且它的 0 有明确解释**：
      convergence 的唯一入口是 TP1 成交，而这两个仓位的止盈由 A 线 A-15-1 影子扣住、
      **`TAKE_PROFIT_LIMIT_ENTRY_RELEASED_POS_IDS` 为空集**。**所以本窗的"零动作"不证明闸门有效**，
      只证明"前提未成立"——**这两者必须分开记**（见 `phase-6h-shadow-window-closed` 的补记：
      一个"没有"可以由多个原因共同造成）。
  (2) 影子行照旧：每轮两仓各恰 1 行、`legacy_would_refuse == stops_examined`、
      `stops_resolved == stops_examined`、`read_failures == 0`、
      撤单集合 ⊆ 该仓主止损且**绝不含备份**；
  (3) **零非预期写入**：`position_mutation_intents` 窗内若有新增，**必须逐笔归因**——
      A 线 A-15-1 首笔止盈会让交易所挂单 **6 → 8**，那是它们的写入不是我的；
      **本线（break-even）产生的写入必须为 0**；
  (4) **`full_exit` 分支仍被扣**：`would_close` 照常记录、`released_closes == 0`；
  (5) 30 分钟连续窗、`head_ok`/`units_ok` 全程 1、零重置。
  **本窗最弱的一点，写在前面**：判据 (1) 是一个**预期为 0 的量**，
  而"零"无法区分"闸门挡住了"与"根本没东西可挡"。**本窗属于后者**，如实记。
  真正能证明闸门的那一刻，是 A 线放开止盈、TP1 真的成交之后的第一次 convergence——
  **记为待观测项，不在本窗判据内。**
- phase-6h-shadow-window-closed (2026-09-10, 会话 local_4a6676b0, **七条判据逐条达成，第三次窗**):
  上线 **`d11592e2cb8d8ce613428f7450c40512564f343a`**，回滚参考 **`29c11354`**。
  全量 **8363 passed / 4 skipped / 0 failed**。四步部署四项全绿。
  窗 **23:01:51Z ~ 23:32:00Z（1810 秒）**、**31 采样、零重置、零不健康、
  `head_ok`/`units_ok` 全程 1**、**28 轮**、`WINDOW_MET`。
  **判据逐条**：
  (1) **每轮 `legacy_would_refuse == stops_examined`** —— 全窗 **112 / 112**：
      旧判据（`posId` 相等 + `slTriggerPx`）在同一批行上**逐条全拒**，
      新读法**逐条全解析**。**这是本窗唯一能失败的成对观测，它分叉了** ✓
  (2) `stops_resolved == stops_examined` **112 / 112**，两处 `read_failures` 全程 0 ✓
  (3) `would_cancel_order_ids` **全窗不含任何备份 ordId** ✓
  (4) `position_mutation_intents` 全窗 **零新增**；`released_events` / `released_closes` 全程 0 ✓
  (5) 每轮两仓各恰 1 行；(5a) 不变量全程成立；(5b) 本窗无 `set_break_even` 轮次（见下）✓
  (6) 30 分钟连续、零重置 ✓
  (7) 每轮 `full_exit` 行均给出 `would_close_size=15` / `endpoint=close_position` /
      `ord_type=market` / **`cancels_stops_first=False`**；`would_close_positions` 全窗 **56**
      （= 2 仓 × 28 轮）✓
  **本窗全程走 `full_exit` 分支**（`actions_seen {"full_exit": 56}`）——市价整窗在入场价 77000 下方。
  **补记（A 线 A-15-1 于 23:36:13Z 上线后追加，因为本条原来的解释不完整）**：
  `set_break_even` 没被走过，当时我只写了一个原因（市价在入场价下方）。
  **实际有两个原因**，第二个是**根本不可能有任何 convergence 存在**——
  convergence 的唯一入口是 TP1 成交，而当时全库活着的入场腿都是 `limit`、
  拿不到止盈（卡在 `trigger_take_profit_convergence_executor:506`）。
  **A-15-1 上线后第二个原因消失了。** 也就是说：本窗观测到的"零交易所写入"，
  当时由两道门共同保证，**现在只剩我自己那两个释放常量**
  （`BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS` / `BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS`，皆空集）。
  **形状（A 线原话，值得单独记）**：**一个"没有"可以由多个原因共同造成，
  而减少一个原因不产生任何可见事件。** 所以"某件事一直没发生"这类结论，
  必须**逐个列出它没发生的全部原因**，否则其中一个悄悄消失时，
  结论看起来仍然成立、而它的支撑已经少了一根。
  这与本仓库"不推动动作的描述得不到防线保护"是同一族：
  **一个原因的消失同样不推动任何动作。**
  **这不是缺陷，但要如实标明它对判据 (5b) 的影响**：`set_break_even` 分支在本窗**没有被真实数据走过**，
  它只有单元测试与部署前那一轮（23:0xZ 之前，市价 77156.6 时）的手工读作为证据。
  **(5b) 记为"本窗未取到样本"，不记为"通过"。**
  证据 `/root/evidence/phase-6h/observer-samples.jsonl`。
  **两次作废窗留档**（目录名即原因）：
  `phase-6h-aborted-criterion-not-invariant`（判据把依赖市价的结果写成了不变量）、
  `phase-6h-aborted-full-exit-ungated`（部署的 sha 让 `full_exit` 分支没有闸门）。
  **三次起窗、两次作废，两次都是我自己的问题而不是系统的**——第一次是判据写错，
  第二次是改动漏挡一个分支。**两次都在起窗后几分钟内被观察器或自查抓到**，
  代价是重新计时；如果任何一次是在收窗时才发现，代价就是一份错误的切换依据。
- phase-6-followup-naked-fill-stop-net-market-only (2026-09-10, **阶段 6 追加清单，本步不做**；A 线扫到、指挥会话转入本线):
  `naked_fill_stop_net.py:192` 用 `str(leg.order_kind or "") != "market"` 排除非市价入场腿
  （911 行另有 `order_kind == "market"` 的查询过滤）。这张网的作用是
  **"归属尚未解析的普通入场腿成交了就先给它挂上止损"**，
  而阶段 5 之后**限价入场同样是普通单**——`open_order_action_guard.REGULAR_ORDER_LEG_KINDS`
  已经写成 `{market, limit}` 并在 2026-09-07 更新过。**限价入场拿不到这张网。**
  应改为与 `REGULAR_ORDER_LEG_KINDS` **同集**并加同集断言
  （判据形式见 A 线 A-15-1：断言两个集合相等而非断言含哪些值）。
  **风险形态**：归属未解析 + 已成交 + 无网 = 真裸仓。
  **但这是观测不是因果**——A 线只扫到该行，**未验证这条组合在生产上是否真会发生**；
  我也未验证。待办包含"先查它是否发生过"，不是直接改。
- phase-6-order-kind-two-scanning-lenses (2026-09-10, 会话 local_4a6676b0 + A 线, **两个口径都要**):
  我扫"`order_kind` 集合判定"得 6 处，结论"`executor:506` 是唯一还漏 `limit` 的"——
  **在"具名集合常量"这个口径下成立**，但**漏掉了单值 `==`**。
  A 线按"把单值 `==` 也算进去"扫得 30 处，而这次限价入场止盈的**第三道门恰好落在那一格**：
  `execution_bindings:1491/1514` 的 `str(leg.order_kind or "") == "trigger_limit"`
  是唯一能给预建止盈腿盖 `pos_id` 的路，它不是集合字面量，所以不在我的表里。
  **两个口径抓的是不同的病**：我的抓"集合忘了加成员"，
  A 线的抓"**根本没写成集合**"——**后者更隐蔽，因为它连"这里有个词表"这件事都不显式。**
  **我的三处自核（应 A 线之请，自己查而非采信）**：
  `recovery_live_submit:2145` 选事件 action 名（`create_limit_entry` / `create_trigger_entry`）、
  `2483` 选 order id 提取策略（`trigger_limit` 走触发单提取，其余走普通单 + `DeepcoinRequestOutcomeUnknown`）
  ——两处都是**按 kind 分派**，`limit` 各有自己的分支，不是"是否执行"的闸门；
  `2726` 是过滤（`order_kind == "market"`），但它是**补充路径**：
  主创建点 `recovery_live_submit:2089` 在下单时对任何带 `take_profit_legs` 的入场腿创建收敛、
  **无 `order_kind` 过滤**，生产库里 `limit` 收敛 6 条即由此而来（另 market 64、trigger_limit 175）。
  **三处在两个口径下都干净。**
- phase-6h-full-exit-was-ungated (2026-09-10, 会话 local_4a6676b0 自查, **我自己改动里的缺口，部署之后才发现**):
  6h 第一版把 `set_break_even` 挡在了释放常量后面，**却漏掉了平级的 `full_exit` 分支**：
  ```
  320|  if action == "set_break_even":
  363|      if pos_id not in BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS:   ← 闸门在这里
  441|  if action == "full_exit":
  442|      close_exact_position(...)                                    ← 无闸门，市价平掉整仓
  ```
  修 650/795 之前前置校验每次必抛，**两个分支都到不了**；把读修对之后
  `set_break_even` 被挡住、**`full_exit` 直通**。
  **形状**：我验证的是"`set_break_even` 会不会写"，而不是**"这次改动让哪些写变成可能"**。
  修一个读的副作用是把**所有**下游分支一起接通，而不只是我正在看的那一支。
  **判据**：任何"修好一个前置校验"的改动，必须先列出**该校验通过后可达的全部分支**，
  逐个回答"这一支会不会写交易所"，再决定挡哪些——**不能只挡自己正在做的那一支**。
  修法（指挥会话采纳）：`BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS` **独立常量、默认空集**，
  闸门置于 `close_exact_position` 之前，未列入者记 `break_even_would_close`
  （pos_id / 数量 / 端点 / `ordType=market` / **`cancels_stops_first=False`**）并返回
  `blocked / break_even_full_exit_not_released`。
  **两个常量刻意分开**：`set_break_even` 与 `full_exit` 来自同一个决策
  （市场政策按市价在入场价哪一侧选分支，2026-09-10 实测 1 分钟内翻过一次：
  `lastPx` 77156.6 → 76957.8，入场 77000），但**"移止损"与"市价平仓"是两种风险量级，
  用户应当能分别批准**；合用一个常量等于批准前者时静默批准了后者。
  **变异检验补了两处此前沉默的**：把两个常量写成别名（`FULL_EXIT = REPLACEMENT`）
  **运行时测不出来**——monkeypatch 只重绑一个名字，另一个仍指向原对象，
  所以加了一条**读源码**的静态判据（两个常量必须各自声明为独立的空 frozenset）；
  影子省略 `would_close` 字段也曾全绿，补了对应用例后转红。
- phase-6h-tpsl-after-market-close (2026-09-10, 会话 local_4a6676b0, **只读历史证据，n=1**):
  指挥会话问：仓位被市价平掉后，交易所侧遗留的 TPSL 会怎样？**用历史证据答。**
  取到一条完整链：备份止损 `1001125164631326`（仓位 `1001125164628529`，ETH）
  - `cTime` **2026-09-07 02:38:52Z** 由本系统 `set_position_sltp` 挂出（intent 642，confirmed）；
  - 平仓 intent 654 `close_position` **confirmed 2026-09-09 04:28:59Z**；
  - 该单 `uTime` **2026-09-09 04:29:10Z**——**平仓后 11 秒**；
  - `triggerTime = "0"`（**从未触发**）；现处于 `trigger-order-history` 而非 pending，
    当前 ETH pending 全表为 0 条。
  - **排除是我们撤的**：全库 `position_mutation_intents` 中提到该 ordId 的**只有** 642 那一条
    `set_position_sltp`，**cancel 类 0 条**；`execution_events` 只有 `create_backup_stop`。
  **结论**：交易所会在仓位平掉后自行把仓位绑定的 TPSL 置为终态，不需要我们撤单，
  且它不是被触发而是被作废。**但这是 n=1**——一条样本、一个币种、一次平仓，
  未在多次平仓上复现，也未验证止盈类挂单是否同样处理。**按 n=1 记，不当作规律。**
- phase-6h-shadow-window-criteria (2026-09-10, 会话 local_4a6676b0, **起窗前写下**, 判据由指挥会话确认):
  影子上线（`break_even_shadow` 每轮计算 + 执行器 650/795 读法修正 + 释放常量默认空集）。
  **收窗判据**：
  (1) 每轮 `break_even_shadow.legacy_would_refuse == stops_examined`——
      旧判据在同一批行上仍然全拒，证明缺陷真实存在且影子读的是同一批行；
  (2) 每轮 `stops_resolved == stops_examined`，两处 `read_failures == 0`；
  (3) `would_cancel_order_ids` **不含任何备份 ordId**（`1001125219289222` / `1001125219582177`）；
  (4) 全窗 `position_mutation_intents` **零新增**；`break_even_would_replace` 事件若出现，
      其 `released` 必须全为 False；
  (5) 每轮 `break_even_shadow.rows` 对两个仓位**各恰好 1 行**，且：
      **(5a) 不变量，恒须成立**——`action` ∈ {`set_break_even`, `keep_tighter_stop`, `full_exit`}
      （三者都是市场政策的合法结论）；`would_cancel_order_ids` 是该仓位主止损的子集，
      **绝不含任何备份 ordId**；`entry_price == "77000"`；
      **(5b) 条件项**——仅当 `action == "set_break_even"` 时，
      `target_stop_price == entry_price` 且 `would_cancel_order_ids` **恰为**该仓位主止损
      （`…121995` / `…153671`）。
      **本条在起窗后第 1 分钟改过一次，原因如实记**：原判据把
      `action == "set_break_even"` 与 `target == "77000"` 写成了恒成立项，
      而它们**依赖实时市价**——起窗时 `lastPx=77156.6`（高于入场 77000，故 `set_break_even`），
      1 分钟后 `lastPx=76957.8`（跌破入场），决策合法地翻成 `full_exit`，
      观察器立刻报 `bad_rounds=1`。**是判据错了，不是系统错了。**
      我把那一次的证据目录留名 `phase-6h-aborted-criterion-not-invariant` 保存，
      **改判据后重新起窗计时**，不在原窗上打补丁——
      在一个已经开始的窗里放宽判据，与"先量后立判据"没有区别。
  (6) 30 分钟连续窗、`head_ok`/`units_ok` 全程 1、零重置。
  **(7) 起第三次窗前追加（`full_exit` 闸门补上之后，见 `phase-6h-full-exit-was-ungated`）**：
      每轮凡 `action == "full_exit"` 的行，必须同时给出
      `would_close_size` 非空、`would_close_endpoint == "close_position"`、
      `would_close_ord_type == "market"`、`would_close_cancels_stops_first is False`；
      且全窗 `break_even_would_close` 事件若出现，其 `released` 必须全为 False。
      **这一条是在新窗起窗之前写的**，不是在跑着的窗里加的——
      前两次窗（`phase-6h-aborted-criterion-not-invariant`、
      `phase-6h-aborted-full-exit-ungated`）都已作废留档，本窗从零计时。
  **消息数不作为判据**：本步的观测量每轮由 reconcile 产生，不依赖消息流
  （6f 收窗时实测该时段到达率约 1.3 条/小时，见 `phase-6f-completed`）。
  **(1) 是本窗唯一能失败的成对观测**：一侧是旧判据、一侧是新读法，两者读同一批行；
  若两侧同时为 0，说明根本没读到止损，而不是"一致"。
  **待观测项（不在本窗判据内，下一次真实 convergence 发生时核对）**：
  执行器写出的 `break_even_would_replace` 事件三项——每个被扣仓位恰 1 条、
  `target_stop_price` 为该仓位 `avgPx`、`would_cancel_order_ids` 恰为其主止损 ordId。
  **为什么不放进本窗**：该事件由执行器在被扣住时写，而执行器只在存在 convergence 批次时运行；
  convergence 由 `_plan_proven_tp1_fills` 生成，**前提是 TP1 已被证明成交**。
  生产 `strategy_break_even_convergences` 全表仅 2 行、皆 2026-08-03、之后再无。
  所以 30 分钟窗内该事件的期望值是 **0 条**，写成判据只会逼出"事后改判据"。
- phase-6h-open-question-no-take-profits (2026-09-10, 会话 local_4a6676b0, **观测，未定性；已交 A 线只读查**):
  查判据 (6) 可测量性时读到：`1001125216121996` 与 `1001125216153672`
  **在交易所上一张止盈都没有，`position_protection_ledger` 里也一行 `take_profit` 都没有**，
  而下单计划有四档（腿 945 `79800.0`、946 `81900.0`、949 `80000.0`、950 `81900.0`），
  四条全是 `status=planned`、`pos_id` 空、`exchange_order_id` 空；
  仓位行 `tpTriggerPx=""`，交易所 pending 6 张全是 TPSL 止损。
  **我没有定性**：止盈是否本来就等某个条件才挂、是否由管理路径在别的时机挂、
  是否与"限价入场自带止损"的迁移形态有关——**一条都没验证**，按规矩作为观测上报而非因果。
  **成因已由 A 线 A-15-0 查明（2026-09-10），并更正了我上面一句含混的因果**：
  卡在 `trigger_take_profit_convergence_executor.py:506`——executor 只收
  `order_kind in {trigger_limit, market}`，而它自己的 planner
  （`trigger_take_profit_convergence.py:24` `AUTOMATIC_ENTRY_ORDER_KINDS`）收
  `{trigger_limit, limit, market}`。**planner 为限价入场生成收敛行，executor 永远执行不了。**
  词表分叉自 `d3e423bf`（2026-09-07 普通限价入场）加宽 planner 起；
  逐行核过 `git log -L 506,506`，那道门自 `dbd484f5`（2026-07-25）没动过，
  且 `git show --stat d3e423bf -- <executor>` 为空——**09-07 加宽了 planner，从未打开 executor**。
  阶段 5 之后限价止盈腿 16 条、挂出 0 条。
  **我原话"没有止盈就不会有 TP1 成交，也就不会有 convergence"要分两张表说**：
  `trigger_take_profit_convergences` 里这两个仓位**是有行的**（244/245，`conflicted`，
  `reason_code=convergence_exact_leg_not_verified`，A 线逐个合取项查库，只有
  `order_kind=limit` 落空）；缺的是 TP1 成交，因而缺的是
  `strategy_break_even_convergences`（全表 2 行、皆 2026-08-03）。两张表不能混。
  **对 6h 的依赖关系，写清楚免得下一个人误读**：这个前提**不会自己长出来**——
  它不是"等时机"，是一道代码门。在 506 加宽之前，**限价入场永远不会有止盈、
  因而永远不会有 TP1 成交**，6h 修好的那条执行器写路径在这类仓位上**永远不触发**。
  所以"切换之后 break-even 会真的动手"这个判断，**对限价入场的仓位挂在 A-15-1 上**；
  对 `trigger_limit` / `market` 入场的仓位不受此限。
  **影子窗若观测不到执行器事件，是这道门的必然结果，不是流量或时机问题。**
- tooling-git-checkout-granularity (2026-09-10, 会话 local_4a6676b0 自查):
  我用一条过于贪婪的正则改测试夹具改坏了，想回退那一次编辑，用了
  `git checkout -- tests/test_break_even_convergence_executor.py`——
  **它把该文件自 HEAD 以来的全部编辑一起回退了**，不只是坏的那次：
  文件头重写、新增 fixture、五处签名改动、三处断言恢复，全没了，只能重做一遍。
  **`git checkout --` 的粒度是"文件回到某个提交"，不是"撤销上一次编辑"。**
  做法：在一连串未提交的编辑中途要回退其中一次，先 `cp` 一份再改，
  或者把每一步都先提交（可以后面 squash），不要用 `checkout --` 当 undo。
- phase-6f-completed (2026-09-10, 会话 local_4a6676b0, **两笔真实交易所写入，逐笔核对通过；6f 完成**):
  上线 **`c51894a45c0a06af4784ed1e1e090be69ff6a381`**（含 6f-1），回滚参考 **`d9a32f18`**（A-14）。
  合并后全量 **8341 passed / 4 skipped / 0 failed**（合并前 8333；不拿合并前的绿当合并后的证据）。
  四步部署四项全绿。第二次部署前按 A 线给的时间 **21:09:00Z** 等到 21:09:09Z 才动，未打断其 A-14 窗。
  **两笔**：`1001125219289222`（pos `…121996`，20:36:36Z）与 `1001125219582177`（pos `…153672`，21:09:40Z），
  皆 `set_position_sltp` / `status=confirmed` / `slTriggerPx=75548.6` / `slOrdPx=-1` / 无 `sz` 键 /
  交易所侧 `sz=0`（全仓）/ 无 `reduceOnly`。
  **交易所 4 → 5 → 6，两次 `removed=[]`**，每一步原有 ordId 逐条仍在。
  **账本 4 行**（2 主 `exchange_adopted_by_tu` + 2 备 `position_mutation_intent_readback`）。
  **零撤单**：`position_mutation_intents` 当日只有 `set_position_sltp`，无任何 `cancel_*`。
  **6f-1 两次在生产验证**：腿 943 / 947 的 `planned_trigger_price` 仍是 `"75700.0"` **未被改写**，
  同时正常绑定 `pos_id` / `exchange_order_id` / `verified`；944 / 948 由 NULL 回填 `75548.6` / `0`。
  **观察窗** 21:15:53Z ~ 21:52:03Z，**37 采样、零重置、零不健康、`head_ok`/`units_ok` 全程 1**、35 轮。
  起窗前写下的五项**全程单一取值**：`ledger_rows` 恒 4、`bound_backups` 恒 2、
  `window_writes` / `window_cancels` / `backup_incidents` 恒 0；
  窗中途追加的 liveness 量 `positions_seen/round` **恒 2.0**、两处 `read_failures` 恒 0。
  证据 `/root/evidence/phase-6f-2/observer-samples.jsonl`、
  `phase-6f-2-before-A/B/C.json`（三次独立基线，皆 5 张、ordId 逐条相同，**全部在部署之前**）、
  `phase-6f-2-after-order.json`、`phase-6f-1-after-order.json`、`phase-6f-before.json`。
  **收窗依据（指挥会话裁定甲），如实记**：判据里**只有"窗内 ≥5 条真实消息"未达成，窗内 0 条**。
  不是"再等等就有"：本窗最后一条消息发生在 **20:36:51Z，比起窗还早 39 分钟**；
  实测按 UTC 小时计的到达分布为 `09:1 10:8 11:17 12:21 13:23 14:4 15:9 16:3 17:2 18:0 19:0 20:3 21:0`——
  **流量是昼夜性的**，活跃带（约 10:00–15:00Z）峰值 23 条/小时、轻松满足判据，
  而本窗落在 17:00–21:00Z 的静默带（12 小时共 91 条，静默带内约 1.3 条/小时）。
  **所以这条判据不是不可达，是在这个时段不可达。**
  **更要紧的是：6f 这一步的证据本来就不来自消息流**——两笔的证明是 payload 原样、回执 ordId、
  前后全表读、`removed=[]`、账本行——**消息量在这一步既不能证实也不能证伪任何东西**。
  硬等一个与判据无关的量，反而会让这条记录看起来像是靠它成立的（A 线原话：
  "记法是为了不夸大，不是为了自谦"）。流量信号由 6h 影子窗承接，那个窗要看的
  `legacy_would_refuse` 与 `stops_resolved` 分叉每轮都产生，不依赖消息。
- phase-6h-defect-restated (2026-09-10, 会话 local_4a6676b0 自查, **我说错了一句，而它已经被写进 6h 判据**):
  我曾向指挥会话与 A 线两次表述："break-even 的坏读取器读的正是仓位行的 `slTriggerPx`，
  6h 只改字段名而仍读仓位行会拿到备份止损价"。**查过之后，这句是错的。**
  `break_even_convergence_executor` **从不读仓位行的 `slTriggerPx`**。它对 `list_positions`
  的两处使用（约 410 行的平仓回读取 `posId`/`pos`、约 542 行的漂移校验取
  `instId`/`posSide`/`pos`/`avgPx`）**都是仓位行本来就带的字段，用法正确**。
  **真实缺陷是"读对了表，用错了词汇"**：约 650 行（止损）与约 795 行（止盈）拿的是
  `trigger-orders-pending` 的 TPSL 行，却对它取 `posId` 与 `slTriggerPx` / `tpTriggerPx`——
  而 TPSL 行**根本不带 `posId`**（A-14 已把这条写成命名读取器），价格键叫
  `slTriggerPrice` / `tpTriggerPrice`。**两个条件各自单独都必然不成立**，
  所以 `break_even_existing_stop_drift` 每次必抛，与 A-13 观测到的"从未成功过"吻合。
  **对 6h 判据的实质影响**：指挥会话据我那句话把判据定为
  "必须读 `trigger-orders-pending` 全集、不读仓位行"。**这条判据不会改变任何事**，
  因为它已经在读挂单表了。**真判据是词汇与归属**：TPSL 行无 `posId`，
  归属只能按 ordId 或 `TU`；价格只能按 TPSL 行自己的键名读；比较前转 `Decimal`（6f-1）。
  **这句错话的来历**：仓位行下单后翻成备份价，是我亲自实测的（真）；break-even 有坏读取器，
  是 A-13 查出来的（真）。**我把两个真事实接成了一条从未验证过的因果。**
  **这是同一形状在一天内的第五次**，而且是最贵的一次——前四次错的是我自己的判断，
  这次错的东西**被上游采纳成了判据**。**说明"它能回答我这个问题吗"这一问，
  在把结论交给别人之前必须再问一遍**，因为交出去之后，纠正的成本不再只由我承担。
- phase-6f-2-window-criteria-amended-midwindow (2026-09-10, 会话 local_4a6676b0, **窗中途追加，如实标明**):
  A 线（`local_22ee72a5`）在他们 A-14 收窗时发现：他们的观察脚本一直把 `rounds` 当成
  "系统在干活"的证据，**但 reconcile 是固定 60 秒定时触发，`rounds == elapsed_minutes` 是恒等式**，
  它只能说明定时器没死，而那件事 `worker_http=200` 已经说了。
  **成对观测里有一半是恒等式，等于没有成对。**
  拿这条去查我自己正在跑的 6f-2 窗，结论更难看：**起窗前写下的五项里，三项是零
  （`window_writes` / `window_cancels` / `backup_incidents`）、两项是静态存储值
  （`ledger_rows=4` / `bound_backups=2`）**。worker 死掉，这五项一个都不会变——
  **"没有坏事发生"与"什么都没发生"在这五个数上不可分辨**，而本窗 `window_messages=0`，
  正好落在那个不可分辨的区间里。
  **追加判据**：`protection_shadow.positions_seen / rounds == 2`
  且 `protection_shadow.read_failures == 0` 且 `protection_adoption.read_failures == 0`。
  理由：`positions_seen` 是每轮**真的去交易所读回来的活仓位数**，交易所读失败、凭据失效、
  仓位消失都会让它掉，**它不是定时器的函数**。阈值 2 取自已知账户状态（该 instId 恰有两个活仓位），
  不是从观测数据里挑出来的。
  **但必须标明：这一项是在窗开始之后追加的，而且我是在看到它当时读数为 2.0 之后才写下它的。**
  按本仓库自己的规矩（"先量后立判据"是失格的），**它的证据效力低于起窗前写下的那五项**，
  只当作liveness 的补充，不单独作为收窗依据。下一次起窗前必须把它写在前面。
- rule-baseline-must-be-read-before-the-deploy (2026-09-10, 会话 local_4a6676b0, 指挥会话要求写成规矩):
  **要在一次部署引发的第一笔交易所写入之前拿到基线，那次读必须发生在部署之前，不是之后。**
  reconcile 轮 **约 60 秒一轮**（`trigger: by_timer`；journal 连续 12 轮实测
  `20:40:41 → 20:41:38 → 20:42:37 → 20:43:57 → … → 20:51:59`），
  所以**任何一次部署后 60 秒内必然有一轮**，"部署完再去读基线"这个做法本身就不成立。
  6f 首笔即栽在这里：我打算作为下单前基线的那次读落在了写入之后
  （见 `phase-6f-first-order-verified`）。
  **顺带更正一个我先前报错的事实**：我曾两次称 reconcile "约 49 分钟一轮"，
  那是从两条 `backup_stop_shadow_ready` 事故行的时间戳（19:18:56 → 20:08:02）推出来的，
  **而 `position_protection_incidents` 按 fingerprint 去重**——连续多轮内容相同不新增行，
  所以那个间隔是"内容变化的间隔"，不是"调度周期"。
  **形状**：拿一个真实存在的数据事实，去回答一个它并不回答的问题。
  这已经是同一形状的第三次（读代码推运行时行为、看签名推 payload、读事故表推调度周期）。
  **判据**：任何"多久一次 / 多少轮"的结论，只能取自调度侧的直接观测
  （journal 的 `started_at`、`trigger`），不能从任何**带去重或带条件写入**的表反推。
- phase-6f-second-release-window-criteria (2026-09-10, 会话 local_4a6676b0, **起窗前写下**, 判据由指挥会话给定):
  放第二笔 `1001125216153672`（把它加进 `ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS`）。
  **收窗判据，L2**：
  (1) **两个仓位各持主 + 备两张**：`1001125216121996` = 主 `1001125216121995`(75700) + 备 `1001125219289222`(75548.6)；
      `1001125216153672` = 主 `1001125216153671`(75700) + 备 <新单>(75548.6)；
  (2) **交易所 `trigger-orders-pending` 全表恰 6 张**（起窗前 5 张 + 本次 1 张），
      且**原 5 张 ordId 逐条仍在**——只增不减；
  (3) **`position_protection_ledger` 该两仓位恰 4 行**（2 主采纳 + 2 备回读）；
  (4) **零撤单**：窗内 `position_mutation_intents` 无任何 `cancel_*` 操作，
      `set_position_sltp` 恰 1 行且 `status=confirmed`；
  (5) 六项核对（触发价 / 无 `sz` / `posSide` / 端点 / 无显式 reduce-only /
      `slOrdPx=-1` 且交易所侧 `sz=0`）逐项通过；
  (6) 30 分钟连续窗口、≥5 条真实消息、`head_ok`/`units_ok` 全程 1、零重置。
  **下单前基线**：起窗前读 `trigger-orders-pending` 全表并存证（预期 5 张）。
  **上一次的教训已应用**：6f 首笔那次，部署后第一轮 reconcile 在 **worker 重启时立刻跑**，
  不是按 ~49 分钟的间隔，我打算作为"下单前基线"的那次读因此落在了写入之后。
  **这次基线在部署之前读，不在部署之后读。**
- phase-6f-first-order-verified (2026-09-10, 会话 local_4a6676b0, **本项目第一次由采纳而来的主止损驱动出真实交易所写入**):
  部署 `586da3c2d2edda494fdae8858c8684bdfeda6d9c`（含 6f-1），回滚参考 `681257331d23a6337088ae0c8e22e91d2dedcf72`。
  四步部署四项全绿。全量 **8332 passed / 4 skipped / 0 failed**。
  20:36:36Z 发出备份止损 **`1001125219289222`**，`position_mutation_intents` id=664 `status=confirmed`。
  **实际 payload 原样**：
  `{"instType":"SWAP","instId":"BTC-USDT-SWAP","posSide":"long","mrgPosition":"split","tdMode":"cross","posId":"1001125216121996","slTriggerPx":"75548.6","slTriggerPxType":"last","slOrdPx":"-1"}`
  **六项逐条**：(1) 触发价 `75548.6` = 75700 × (1−20bps)，< 主止损、> `liqPx` 68116.6 ✓
  (2) **无 `sz` 键**（键仅 instId/instType/mrgPosition/posId/posSide/slOrdPx/slTriggerPx/slTriggerPxType/tdMode）✓
  (3) `posSide=long` ✓ (4) 端点 `set_position_sltp` ✓ (5) 无 `reduceOnly` 键，靠 posId 绑定的 TPSL 端点语义只减仓 ✓
  (6) `slOrdPx="-1"`、交易所侧该单 **`sz=0`** 即全仓 ✓
  **三条护栏逐条**：只对 `exchange_adopted_by_tu` 放开 ✓；
  **自部署起全库 `position_mutation_intents` 仅 1 行**（限流生效，第二笔的保护腿 947/948 仍 `planned`、`pos_id` 空）✓；
  **主止损未被撤**——交易所挂单 **4 → 5，只增不减**，原 4 张 ordId 逐条仍在 ✓
  **6f-1 在生产上按设计生效**：腿 943 `primary_stop` 的 `planned_trigger_price` **仍是 `"75700.0"` 未被改写**，
  同时正常绑定 `pos_id` / `exchange_order_id` / `verified`；腿 944 由 NULL 回填为 `75548.6` / `0`。
  **流程偏差，如实记**：指挥会话要求"首笔下单前后各读一次"，我**没有抢在下单前读**——
  部署后第一轮 reconcile 在 worker 重启时立刻跑了。结论不受影响（部署前基线读过两次且一致，
  皆为同样 4 张、ordId 逐条相同），但我把误名的证据文件从 `phase-6f-1-before.json`
  改名为 `phase-6f-1-after-order.json` 并写了 `phase-6f-1-README.txt` 说明为什么——
  **留一个名字叫 before 的 after，正是"不要求任何人动手的错误描述可以无限期存活"**。
  证据：`/root/evidence/phase-6f-before.json`（真基线）、`phase-6f-1-after-order.json`、`phase-6f-1-README.txt`。
  **一条新观察（已写入 ARCHITECTURE 4.8，并作为 6h 判据）**：下单后
  **仓位行的 `slTriggerPx` 变成了备份价 `75548.6`**，而主止损单仍在 `trigger-orders-pending` 里。
  叠加语义下仓位行只显示最后写的那张，而先触及的是 `75700` 那张——**仓位行显示的恰恰不是会先生效的那张**。
  **（后续更正，见 `phase-6h-defect-restated`：我当时说"break-even 读的正是仓位行这个字段"，
  查过之后是错的——它读的是挂单表，只是用了仓位行的字段名。仓位行"只显示最后一次写入"
  这个观测本身成立且重要，但它不是 break-even 那个缺陷的成因。）**
- phase-6f-1-planned-values-compared-as-strings (2026-09-10, 会话 local_4a6676b0, **6f 首笔被一条既有守卫挡住，指挥会话裁定作为 6f 前置缺陷在 6f 内修**):
  6f 部署（`681257331d23a6337088ae0c8e22e91d2dedcf72`）后第一轮 reconcile（20:08:02Z），
  释放的仓位 `1001125216121996` **没有下出去**，记 `backup_stop_blocked` /
  `protection_leg_conflict`；未释放的 `1001125216153672` 按预期持有，
  且这次带了完整明细（`primary_stop` / `proposed_backup_stop` / `proposed_endpoint` /
  `proposed_pos_side` / `proposed_size`）——`phase-6e-shadow-ready-missing-payload` 的修复生效。
  **零写入已双向核实**：`position_mutation_intents` 近 90 分钟 0 条；
  `trigger-orders-pending` 下单后复读仍是下单前那 4 条、ordId 逐条相同
  （`/root/evidence/phase-6f-before.json`）。
  **根因**：`create_or_get_protection_leg` 对 `planned_trigger_price` / `planned_size`
  做的是**字符串** `!=` 比较。计划腿由下单计划写入、经 Python float 格式化成
  `"75700.0"`；6e 采纳把交易所原文 `"75700"` 写进 `position_protection_ledger`；
  `trigger_backup_stop_executor` 从账本取 `primary_stop` 原样传给
  `materialize_verified_position_protection` → `"75700.0" != "75700"` → `ValueError`
  → `protection_leg_conflict`。本地用真实函数成对复现：
  `stored=75700.0 incoming=75700` 抛、`75700/75700` 放行、`75700.0/75700.0` 放行。
  **这不是 6e/6f 引入的缺陷**，但 6e 是第一条把交易所原文写进账本的路径，
  所以是第一次必然撞上它的路径。
  **修**：改为 `Decimal` 相等比较（`_planned_value_changed`）。`None` 语义不变
  （一侧无计划＝不是比较，仍走回填）；不可解析文本、非有限值一律按"无法证明相同"
  保留拒绝；**不改写已存的计划值**——接受第二种写法不等于把第一种覆盖掉，
  否则这条守卫会去动它自己要按住的东西。
  **历史范围**：`protection_leg_conflict` 自 2026-07-26 起共 **27 个不同仓位、每仓 1 次**，
  最近一次即本次。其中 `1001125104601308`（2026-09-03）在库里同样呈现
  `76500.0` vs `76500` 的纯格式差异。据此判断这 27 笔属**格式差异导致的误拒**，
  但**只对本次这一笔做了证明，其余 26 笔未逐笔证明**。
  **这条守卫此前没有任何测试**——46 天里挡下 27 个仓位，零覆盖。本次补 19 个用例
  （等值不同格式放行含生产原值对、真实差异仍抛含 `75700` vs `75700.01`、
  不可解析文本抛、非有限值抛、端到端走 `materialize_verified_position_protection` 复现生产那次）。
  变异测试三处，全部咬住：还原成字符串比较 → 7 failed；恒不拒绝 → 10 failed；
  去掉有限性检查 → **第一次 0 failed**，因为 `nan`/`inf` 与数字比本来就不等、
  是 Decimal 比较自己挡的，只有 `inf` vs `Infinity`、`-inf` vs `-Infinity`
  这两种**同值不同拼写**才会走到有限性检查；补这两例后 → 2 failed。
  教训：**测试写了"这一例是为某个分支而设"的注释，不等于那一例真的走到那个分支**；
  是变异测试而不是阅读发现了这条注释在说谎。
  验证：focused 27 passed；相关面 581 passed；全量 **8332 passed / 4 skipped / 0 failed**。
- phase-6f-shown-detail-correction (2026-09-10, 会话 local_4a6676b0 自查, **出示给用户的明细有一项不准**):
  我向用户出示 6f 时写了"**数量 15**"，实际发出的 payload **没有 `sz` 字段**。
  `build_backup_stop_trigger_payload` 返回的键只有
  `instType / instId / posSide / mrgPosition / tdMode / posId / slTriggerPx / slTriggerPxType / slOrdPx`；
  `size` 参数**只用于入参校验**（必须为正），不进 payload。它是**仓位绑定**的 TPSL，
  `slOrdPx=-1` 表示市价平掉**触发时该仓位的全部持仓**。
  **差别是实质性的**："数量 15"读起来像固定 15 张，实际**跟随仓位**——仓位变 20 张就平 20 张。
  今天两者数值相同（仓位正好 15），不影响首笔的风险判断，但与"最多只平 15 张"的理解不符。
  **怎么发现的**：写用例断言 `payload["sz"] == "15"` 时 `KeyError`——**逐字段核对 payload 才撞出来**。
  我原来那句是从函数签名的 `size=` 参数**推**出来的，**又一次把签名当成了行为**；
  与"叙述跑在事实前面"同族，区别是**这次跑在前面的叙述已经送到用户面前**。
  处置：已报指挥会话，建议向用户补一句更正再下单；首笔核对第六项改为
  **"新单不带 `sz`、`slOrdPx=-1`、覆盖全仓"**而不是核对"数量=15"。**未答复前不部署 6f。**
- phase-6f-approval (2026-09-10, 用户在指挥会话 local_858790fe 明确批准，原话"批准 6f"):
  **6f（在真实仓位上放开由采纳而来的备份止损下单）获批。** 出示给用户的明细逐项如下：
  拟备份止损**触发价 `75548.6`**（= 主止损 75700 × (1 − 20bps)，long 向下取整到 tick；
  `liqPx=68116.6`，代码硬校验要求备份止损 < 主止损且 > 强平价）、**数量：payload 实际不带 `sz`**（见 `phase-6f-shown-detail-correction`，出示时写的是"数量 15"）、
  方向 `posSide=long`、端点 **`set_position_sltp`**（非 trigger-order）、
  payload `{"instType":"SWAP","instId":"BTC-USDT-SWAP","posSide":"long","mrgPosition":"split",
  "tdMode":"cross","posId":<pos>,"slTriggerPx":"75548.6","slTriggerPxType":"last","slOrdPx":"-1"}`、
  **无显式 reduce-only**（仓位绑定 TPSL，按端点语义只减仓）、
  **只对 `evidence_source=exchange_adopted_by_tu` 的主止损放开**（其余来源今天本来就在下单，不在本次批准范围）、
  **首笔单仓位限流**、失败即停且**不撤主止损**（该模块零撤单调用，grep 计数 0）。
  执行顺序（指挥会话裁定）：6e 收窗 → 6f 提交 → 全量 → 四步部署 →
  **首笔下单前后各读一次 `trigger-orders-pending` 全表**、六项核对结果原样报指挥会话 →
  确认后才放第二笔 → 收窗。
- phase-6e-cutover-completed (2026-09-10, 会话 local_4a6676b0, **五条判据全部达成，含交易所侧证据**):
  接线本体 **`14fdf1b1ba29e441644b7c57cfabaee689915ed2`**，19:17Z 上线，**回滚参考 `a8a3a069`**，
  四步部署（第 3 步第三次撞并发推送，按不变量合并不变基）。全量 **8312 passed / 4 skipped / 0 failed**。
  窗口 19:20:31Z ~ 19:50Z（**1810 秒**）、31 采样、**零重置、head_ok / units_ok 全程 1**、32 轮。
  证据 `/root/evidence/phase-6e-cutover/observer-samples.jsonl`。
  **判据逐条**：
  (1) **两行账本，各一行** ——
      `1001125216121996|1001125216121995` 与 `1001125216153672|1001125216153671`，
      皆 `stop_loss / 75700 / 15 / verified / exchange_adopted_by_tu`，与交易所那两张随单止损逐字段一致 ✓
  (2) 两条 `protection_adopted_from_exchange` 事件 + 两条 runtime incident，**均 `delivered`**（19:19:07Z），
      `refused_positions` 全窗 0 ✓
  (3) 两条 `backup_stop_shadow_ready`，**且收窗时交易所 TPSL 仍为 4 张、四个 ordId 与部署前完全相同** ✓
  (4) `backup_stop_blocked` 不再产生（已越过 `primary_stop_not_verified` 那道门）✓
  (5) **全窗零交易所写入**：`position_mutation_intents` 窗内 **0 行** ✓
  **(3)(5) 是本步最重要的两条**，都用交易所侧读数证明，而不是只看我们自己的计数。
  **两个自己走出来的确认**：`adopted_rows` 全窗恒 **0**（幂等按设计生效，无重复采纳）；
  影子判定从 `chain_resolved_legacy_absent` 自动转为 **`chain_resolved_legacy_ambiguous`**（64 = 2 × 32）——
  账本现在能给出那两张止损的归属，旧匹配器不再说 absent，但仍被那两张**在挂入场单自带止损**卡成 ambiguous。
  **未达标项**：`msgs=0/5`（深夜安静时段），与前两窗同一口径：**消息数对本步判据没有信息量**。
- phase-6e-shadow-ready-missing-payload (2026-09-10, 会话 local_4a6676b0 自查, **随 6f 提交修**):
  指挥会话要求 `backup_stop_shadow_ready` **含拟价/量**，而落库的两条只有
  `{"reason_code": "primary_stop_adopted_from_exchange"}`——**没有价也没有量**。
  原因：我把闸门放在了"计算之前"而不是"提交之前"，于是根本没算就返回了。
  **拟下单内容（按生产真实持仓与代码逐行算出，供 6f 申请用）**：
  两仓位相同——`posSide=long`、数量 `15`（全量）、主止损 `75700`、
  **拟备份止损 `75548.6`**（= 75700 × (1−20bps)，long 向下取整到 tick）、`liqPx=68116.6`（校验要求
  备份止损 < 主止损且 > 强平价）、端点 **`set_position_sltp`**、
  payload `{...,"posId":<pos>,"slTriggerPx":"75548.6","slTriggerPxType":"last","slOrdPx":"-1"}`、
  **无显式 reduce-only**（仓位绑定 TPSL，按端点语义只减仓）、
  失败处置：该模块**零撤单调用**（grep 计数 0），主止损绝不会被动。
  修法：把闸门下移到 payload 算完之后、提交之前。
- observer-count-trap-hit-in-a-check (2026-09-10, 会话 local_4a6676b0 自查):
  收窗时我用 `ps -eo pid,cmd | awk "/step6_observe/"` 数残留监视器，得到 **3**——
  **那三个是这条检查命令自己的进程**（ssh 的 `bash -c`、`ps`、`awk` 的命令行里都含该模式）。
  换成锚定完整命令行的 `awk "/^ *[0-9]+ \/bin\/bash \/root\/step6_observe_v2\.sh/"` 后是 **0**。
  **ARCHITECTURE 第 6 节点名的正是这一条**（"不要用 `pgrep -f <脚本名>`，发起检查的命令行自身就含那个模式"），
  而我在写监视器时守住了它、却在**临时敲的一条检查命令里**踩了进去。
  **教训与"一条只在人记得时才执行的判据"同源**：规矩写在脚本里，而临时命令不经过脚本。
- phase-6e-cutover-window-criteria (2026-09-10, 会话 local_4a6676b0, **起窗前写下**, 判据由指挥会话给定):
  6e 接线（真正的采纳）上线后，**要取到下列全部才算证明本步**：
  (1) 两个仓位（`1001125216121996` / `1001125216153672`）**各新增恰好 1 行**保护账本行：
      `evidence_source=exchange_adopted_by_tu`、`status=verified`、`purpose=stop_loss`、
      ordId 与价量与交易所那张随单止损对应；
  (2) 两条 `protection_adopted_from_exchange` 事件、且 runtime incident **`delivered`**；
  (3) 两条 `backup_stop_shadow_ready`（各 1），**且交易所 `BTC-USDT-SWAP` 的 TPSL 张数仍为 4**
      ——即备份止损**没有真的下单**；
  (4) `protection_health` 对这两个仓位不再给 `primary_stop_not_verified`；
  (5) **全窗零交易所写入**：`position_mutation_intents` 无新行。
  **(3) 与 (5) 是本窗最重要的两条**：6e 被定级为"只写账本"，而它的下游 `trigger_backup_stop_executor`
  在 `position_management_liveness_v2_mode=live` 下本来会下单——**一次账本写入若在同一轮变成一次
  交易所写入，就是本步最可能造成的伤害**，所以要用交易所上的张数（4）来证明它没有发生，
  而不是只看我们自己的计数。
  **预期能取到**：样本此刻就在交易所上。若窗内仓位被平掉，照实记"样本中途消失"。
- phase-6h-break-even-field-names (2026-09-10, 会话 local_4a6676b0 发现, **指挥会话立为 6h，需用户批准**):
  **自动保本收敛在生产上从未成功过，而且按现在的代码必然失败。**
  `break_even_convergence_executor` 的市场预检拿 `trigger-orders-pending` 的**原始行**比对：
  `exchange_row.get("posId") != leg.pos_id` 且 `_decimal_equal(exchange_row.get("slTriggerPx"), ...)`。
  而生产返回的行**每一行 `posId` 都是 `null`**、触发价字段叫 **`slTriggerPrice`**（2026-09-10 只读探测原样）。
  两个条件因此恒真，只要账本里有该仓位的 stop_loss 行，预检必抛 `break_even_existing_stop_drift`。
  **生产数据吻合**：`strategy_break_even_convergences` 全表**只有 2 行、全部
  `blocked / break_even_market_preflight_unavailable`**（最后一次 2026-08-03），**无一次成功**。
  **这正是 ARCHITECTURE 第 6 节 A-5b 点名的那个陷阱**，而这处代码没有照它改。
  **怎么撞出来的**：为让 6e 判据成立，我把 break-even 测试夹具改成生产真实形状
  （补 `triggerOrderType`、`slTriggerPx` → `slTriggerPrice`），**改完三条测试立刻红**——
  它们此前一直绿，因为夹具用的是**生产不存在的字段名**。
  **测试通过证明的是"代码与夹具一致"，而夹具与生产不一致。**
  处置：**不顺手修**（修好等于让一条从未运行过的交易所写入路径开始运行，性质同 6f）。
  三条用例改为断言现网会在预检阻塞并在文件头写明原委——**不把夹具改回不真实的形状让它们变绿**，
  那正是这个缺陷长期存活的原因。6h 形状：先影子（预检改用归一化行、只记 `break_even_would_replace`），
  用户批准后再切换。A 线另行只读全库扫同类读点。
  **对 6e 下游清单的更正**：原写"保本收敛会按 A-5e 替换那张随单止损，属修复，允许"，
  该格改为 **"当前不可达（字段名缺陷），修复后需单独批准"**；
  我 6a 给 break-even 加的 A-5e 序列与刚加的四项回读，**在生产上一次都没执行过**。
- phase-6e-shadow-window-closed (2026-09-10, 会话 local_4a6676b0, **本阶段第一次判据逐轮精确达成**):
  影子本体 **`a8a3a0691221fd6b00ba487e8f2a8e88077e6e19`**（含 A 线 A-11b），18:21:10Z 起窗，
  回滚参考 `117f8b09`。窗口 **1865 秒（31 分 05 秒）**、32 采样、**零重置、head_ok / units_ok 全程 1**、
  35 轮 reconcile。证据 `/root/evidence/phase-6e-shadow/observer-samples.jsonl`。
  **三条判据（起窗前写下）全部达成，且每项都是精确倍数，没有一轮例外**：
  `would_adopt` 累计 **70 = 2 × 35**；`excluded_pending_entry_stops` **140 = 4 × 35**
  且 `would_adopt` 始终只有 2/轮（被排除的两张从未混进来）；
  `chain_resolved_legacy_absent` **70 = 2 × 35**、`set_mismatch` **全窗 0**。
  附带：`cancel_precheck` 全 `match`、`ledger_drift` 全窗 0。
  **未达标项：`msgs=0/5`**（19:00Z 前后安静时段），按指挥会话裁定按现状收窗——
  **消息数对本步判据没有信息量**：本步要的是活仓与活保护单，两者全窗都在。
  与前几窗照同一口径分开记：**L2 时长达标、消息门槛未达、本步三条判据全部取得生产样本**。
- phase-6e-cutover-selfcheck (2026-09-10, 会话 local_4a6676b0 自查, **接线前必须先补**):
  我给指挥会话的下游清单里写了"保本收敛撤旧会走 6a 的撤前四项回读"。核实后**当时并不成立**：
  该路径确实已走 `protection_replacement`（6a 改的），但**四项回读 `pre_cancel_check` 是可选参数，
  break-even 那条调用没有传**。
  **这正是"一条只在人记得时才执行的判据等于没有判据"的又一个实例**，而且这次它差点变成
  写进清单、报给指挥会话、却在代码里不成立的一句话。处置：把该参数改为**必传**而不是"记得传"。
- phase-6e-shadow-window-criteria (2026-09-10, 会话 local_4a6676b0, **起窗前写下**):
  本窗覆盖 A 线 A-11b 与本会话的 `chain_resolved_legacy_absent` 分档 + `would_adopt` 计数（6e 影子）。
  **仍然只观测、不写账本、不写交易所。**
  **要取到下列才算证明了 6e 影子**：
  (1) **`would_adopt` 对当前两个仓位各恰好 1**（即窗口内每轮 `would_adopt == 2`）——
      它们各有一张随单止损、账本各无行；
  (2) **被排除的挂单入场自带止损不进 `would_adopt`**：binding 347 那两张仍在挂的
      （`1001125208806869` / `1001125208807099`）必须只出现在 `excluded_pending_entry_stops` 里，
      **`would_adopt` 不得因它们增加**；
  (3) `chain_resolved_legacy_absent` 取代此前的 `set_mismatch`——同样两个仓位、每轮各 1。
  **本窗与前几窗不同：判据预期能取到**，因为样本（两个活仓 + 两张随单止损 + 两张在挂入场止损）
  此刻就在交易所上。若窗内仓位被平掉而样本消失，照实记"样本中途消失"，不追补。
  同时顺带核对 `phase-6a-open-verification` 的三条常开判据（真实止损替换仍预期取不到）。
- phase-6e-blame-combined-protection-gate (2026-09-10, 会话 local_4a6676b0, **改之前先查为什么**):
  指挥会话要求把 `_request_has_combined_trigger_protection`（要求请求同时带 `tpTriggerPx` 与
  `slTriggerPx`）放宽成"带 SL 即可"之前，先查当初为何要求两者都有。查了：
  引入提交 **`a1fab461`（2026-07-20，"feat: adopt verified trigger entry protection"）**——
  **提交信息无理由，函数无 docstring 无注释，同批测试全部用"两者都带"的夹具，
  没有一条断言"只带 SL 必须被排除"。**
  **但"当时只有 combined 这一种形状"这个最省事的解释被生产数据否掉**：
  `order_kind='trigger_limit'` 的入场腿里 **SL-only 最早出现在 2026-07-09**，比这道门早 11 天，
  到 2026-08-01 已 74 条。**作者是面对着已经存在的 SL-only 形状把它排除在外的，不是没见过。**
  旁证两条：`has_tp` 形状最后一条是 2026-07-21（门写完第二天就不再产生），
  该采纳路径最后一次产出 2026-07-24、此后事实停产；SL-only 一直产到 2026-09-08（全期 189 条）。
  **结论：理由无记录，且无法证明它不存在。** 因此**不放宽该谓词**——放宽会让历史上 74+ 条
  被刻意排除的腿一并变成可采纳，而我们不知道当初排除它们的理由。
  改为**新增一条并列判据**，准入不看请求形状而看阶段 6 的证据标准
  （`TU==posId` + 该 ordId 在 `trigger-orders-pending` 在场 + instId/posSide 相符 + `TPSL` 类型），
  **比原路径的证据更强而非更弱**，对 limit / trigger_limit、SL-only / combined 一视同仁；原路径原样保留。
  **方法论**：查不到理由时，可选的不是"那就当没有理由"，而是**换一条不依赖那个理由的路**。
- phase-6a-coverage-fact (2026-09-10, 会话 local_4a6676b0, **我此前没说清的覆盖面**):
  6a 改的是 `deepcoin_execution_actions.adjust_position_tpsl`，而**自动（KOL）管理指令进不了那个函数**——
  它开头就是 `automated_position_tpsl_requires_management_batch`，非人工来源一律拒绝、要求走管理批次。
  **所以新绑定链目前只覆盖人工管理路径，不覆盖自动路径**；自动路径在 `strategy_management_executor`，
  仍用 `match_position_protection`（行 658）。
  **但它不会"挂新不撤旧"**：匹配器答不出来时预检抛
  `protection_preflight_rows_ambiguous_or_drifted`，整批拒绝。
  所以这两个账本无行的仓位上，**自动管理指令的现网后果是"做不了任何事"，不是"多挂一张"**。
- phase-6cd-shadow-verdict-gap (2026-09-10, 会话 local_4a6676b0, **待随 A-11b 之后上线**):
  影子把"旧匹配器 `absent` + 新链 resolved"归进了 `set_mismatch`，读起来像"新旧两条路打架"，
  实际与 `chain_resolved_legacy_ambiguous` 同族，是**改进**。分类少了一档
  （拟名 `chain_resolved_legacy_absent`）。补丁会重置窗口，故按裁定排在 A 线 A-11b 之后。
- phase-6cd-deployed (2026-09-10, 会话 local_4a6676b0): 6c + 6d 上线，
  **`20445fc6fc08a7ffb56f8752de174fb133197711`**（变基到 A 线 A-11 `113cd70c` 之上），
  15:53Z 经 `tg-deploy`，**回滚参考 `113cd70c`**。全量 **8296 passed / 4 skipped / 0 failed**
  （= 共同基线 8279 + 本会话 12 条 + A 线 5 条，逐项对得上）。三服务 active、worker 200、
  部署后 3 分钟相关报错 0。窗口 15:54:19Z 起，判据见 `phase-6cd-window-criteria`。
- shared-branch-push-race (2026-09-10, 会话 local_4a6676b0): 按新四步顺序做到第 3 步
  "把那个确切 sha 推共享分支"时被拒（non-fast-forward）——**A 线在我 `tg-deploy` 的那一分钟里
  推了 A-11 的收窗记录**（纯文档）。
  **处理：合并，不变基。** 变基会把 `20445fc6` 重写掉，而它正是**生产上跑着的那个 sha**；
  重写之后"部署的 sha 必须在共享分支上"就不再成立，得为一个纯文档差异重新部署一次、
  并把刚起的窗口清零。合并后共享分支**包含**生产的 sha，且只比生产多出对方那份文档，
  两条判据同时成立（已复核：祖先 = YES，共享分支比生产多出的代码文件数 = 0）。
  **这里真正缺的是一条不变量，不是一个补救动作**（A 线 local_22ee72a5 指出，本会话同意）：

  > **已部署的 sha 不能被重写。**

  四步顺序通篇假设那个 sha 稳定，却从没说出来。第 3 步一旦撞上并发推送，"变基"是 git 用户的
  默认反射，而在这里它会**把生产上正在跑的那个对象重写掉**——于是"部署的 sha 在共享分支上"
  从成立变成不成立，**不是因为谁做错了后续动作，是因为那个 sha 不存在了**。
  写成不变量而不是"撞车时合并不变基"，是因为**不变量能推出后者，反过来推不出来**：
  下一个人遇到的可能是别的撞法（例如 `--force-with-lease` 的诱惑），不变量对那些情形照样给答案。
  **另一条同源的**：共享分支现在含合并提交，所以**分支尖端不再是一个线性可部署候选**——
  部署必须指名一个具体 sha，`origin/…` 的尖端可能是一个从未跑过全量的合并结果。
  暂不进 AGENTS.md：这条规矩今天已改过三轮，再动一次的边际收益不如让它先跑一阵；
  攒到第二次真的撞上再定。
- phase-6cd-window-criteria (2026-09-10, 会话 local_4a6676b0, **起窗前写下；待 A-11 上线后部署**):
  这一窗覆盖**三样东西**：A 线 A-11（写入结果分类改白名单）、6c（三个拒绝用例，无生产代码）、
  6d（裸仓安全网前置 (e)）。**所以"无回归"是联合证据，不是任何一步单独的**——
  与 6a 那次同一条规矩，两条线都已按此声明。
  **要取到下列才算证明了 6d（本步唯一有生产代码的部分）**：
  (1) `naked_fill_stop_net` 的补挂路径**被触发一次**——即出现一条 `market` 入场腿、
      `attribution_status=unverified`、过了宽限期，从而被那一轮扫描选中；
  (2) 该次决定的审计行里 `preconditions` 含 **`e:pass`**（问过交易所、答"没有止损"）
      或 **`e:fail` / `e:unknown`**（已有止损 → 不挂只告警 / 读不到 → 算不知道），三者任一都是样本；
  (3) `position_already_protected_on_exchange` 与 `protection_snapshot_incomplete`
      两个 incident 的条数与归因。
  **预先声明极可能取不到**：该路径的设计前提就是"**永远不该发生**"——生产 153/153 市价入场腿
  都满足身份等式，这个网自上线以来一次都没动过。**再加上起窗时交易所 0 仓位**，
  (1) 取不到的概率接近 1。取不到就照实记"仅测试覆盖"，**不为验证而构造一次裸仓**（阶段文件明令）。
  同时顺带核对 `phase-6a-open-verification` 的三条常开判据与 6b 的 `cancel_precheck`（同样预期为空）。
  **这一条本身就是"窗口的证据价值由被改的判据决定"的极端例子**：一个正确的安全网，
  它的正面样本要求系统先出一次它专门防的故障。
- shared-branch-rule-violated-by-its-author (2026-09-10, 会话 local_4a6676b0 自查并回滚):
  我把分支尖端推上共享分支，**里面带着未部署的 6d 生产代码**（`naked_fill_stop_net.py`）——
  正是我自己写进 AGENTS.md 的那条"未部署的代码不得进共享分支"。
  共享分支一度是 `92893c80`，已 `--force-with-lease` 回滚到 `cd3118ef`（A 线原本的变基基点，未受影响），
  回滚后判据复核为空。暴露窗口约 4 分钟，lease 确认期间无他人推送，已即时通知 A 线重新 fetch。
  **怎么犯的**：我执行的是"记录收窗 → 推共享分支"，而"推共享分支"这个动作在我这里
  **是和文档提交绑定的心智习惯**——前五次推的确实都是纯文档；这次分支尖端在两个文档提交之间
  夹了 6c/6d 的代码，我推的是**尖端**而不是那批文档。
  **判据我写了、也真的会跑，但这次没跑**：我落那条规矩时写的正是"规矩写完就该立刻拿它检验
  自己那次提交"，随后在**别的**提交上照做了三次，唯独这次没有。
  **所以结论不是判据不好用，而是：一条只在人记得时才执行的判据，等于没有判据。**
  它与"观测装置不采集判据所需字段"是同一个形状——**规矩在纸上成立、在流程里不成立**。
  真正的解法是让 push 那一步自己带上检查（推共享分支只用一个固定的封装命令），
  而不是指望下一次记得；在有封装之前，这条只能靠"每次推之前跑那句 grep"顶着，
  而今天已经证明那顶不住。
- phase-6b-shadow-completed (2026-09-10, 会话 local_4a6676b0, **6b 停在影子，切换未做**):
  影子本体 **`760b2ab8dc740bab6bf9b7b3e9ea7f10557c42b7`**，14:33Z 上线，回滚参考 `5c07ab6d`，
  部署后已推共享分支。窗口 14:34:01Z ~ 15:24:11Z（**50 分 10 秒**）、51 采样、
  **零重置、head_ok 全程 1、units_ok 全程 1**、真实消息 5 条 / **3 个群**，`WINDOW_MET` 自行退出，
  监视器已自行结束（精确 PID + marker 判定，锚定命令行数过 = 0）。
  证据 `/root/evidence/phase-6b-shadow/observer-samples.jsonl`。
  59 轮 reconcile，**全窗 `positions_seen` = 0**；窗口内新增影子行 0、保护 incident 0、
  `position_mutation_intents` 0。
  **对照起窗前写下的三条判据（`phase-6b-shadow-window-criteria`）：一条都没取到。**
  `cancel_precheck` 全窗为空字典、`ledger_drift` 恒 0——因为 0 仓位就没有活的保护单可问。
  **6b 的四项精确回读在生产上零样本，仅有测试覆盖**（focused 30，含两处变异检验：
  把第二次读换回第一次读、把账本比较写回去，对应用例各自转红）。
  **限制照旧一起读**：本窗口 HEAD 只含 6b 影子（A-10e 之上），所以"无回归"这一条是干净的；
  但**消息数达标（5 条 / 3 群）不等于本步判据被触发**——前者衡量有流量时系统是否健康，
  后者要求有活的保护单可问，本窗满足前者、完全没有后者。
  **6b 切换（撤销走新链）按指挥会话裁定停在此处**，放行条件见 `phase-6a-open-verification`：
  需 6a 三条判据至少取到一条真实样本，**而那要等市场先给出一个仓位**，不是时长问题。
- phase-6b-shadow-window-criteria (2026-09-10T14:35Z, 会话 local_4a6676b0, **起窗前写下**):
  6b 影子已上线：**`760b2ab8dc740bab6bf9b7b3e9ea7f10557c42b7`**，14:33Z 经 `tg-deploy`，
  **回滚参考 `5c07ab6d`**（A 线 A-10e），部署后已立即推共享分支（已核实）。
  它**不撤任何单、不写交易所**：每轮 reconcile 为每张活的保护单问一遍撤销会问的四项
  （instId / posSide / 触发价 / 数量，按精确 ordId），并做**两次读**——解析一次、回读一次。
  **要取到下列才算证明了 6b 影子**：
  (1) `cancel_precheck.match ≥ 1`——至少一张活保护单跨两次读四项一致（正向观测量）；
  (2) 任何 `cancel_precheck` 的不一致结果逐条记录并归因；
  (3) `ledger_drift` 取得读数（账本与交易所的偏差有多常见，这是**只观测不设卡**的测量，
      将来若要把撤销门卡在账本上，得先有这个数）。
  **预先声明取不到的情形**：起窗时交易所 0 仓位、0 保护单，因此 `cancel_precheck` 会是空字典、
  `ledger_drift` 恒 0——**上面三条一条都取不到**，照实记"仅测试覆盖"，不拿窗口达标充数。
  **同时顺带核对 `phase-6a-open-verification` 的三条常开判据**（本窗口同样预期取不到）。
  **起窗后一分钟发现并修掉的一处**：判据写下来了，但**观测脚本并不采集这些判据所需的字段**——
  它只抽 `counts_by_verdict` / `positions_seen` / `rounds`，`cancel_precheck` 与 `ledger_drift`
  根本不会进采样行。那样收窗时会拿到一份"合格"的采样表，而判据一栏无数据可填。
  已补齐三个字段并重起窗（14:34:01Z）。**教训**：把判据写在起窗之前只做了一半，
  另一半是**确认观测装置真的会记下这些判据所需的数**——否则"起窗前写判据"本身也会变成一道
  看起来做过、实际取不到数的手续。
- phase-6b-shadow-observable-defect (2026-09-10, 会话 local_4a6676b0 自查, **假阳性那一侧**):
  6b 影子的第一版把四项回读结果与"解析这次授权所用的那一次读"相比——**拿一次读跟它自己比，
  只可能答 `match`**，是一个不可能失败的计数器，而且它会一直报"全部一致"，看起来像证据。
  改成"与账本比"之后全量立刻红了一条：分批止盈成交后账本记的数量落后于实时数量，
  那是**合法的陈旧**，按账本卡会把一条今天能正常执行的管理指令拦下来——**是测试告诉我这个"修复"也是错的**。
  最终形状是**两次读**（解析一次、回读一次），问的正是"命名它的那次读与即将撤它的那一刻之间这张单有没有变"；
  账本与交易所的偏差改为 `ledger_drift` 只观测不设卡。
  **与同日另外两个坏观测量的区别值得单记**：A 线的 `rounds` 恒 0、本会话的 `auto_trade` grep 恒非零，
  都是**假阴性**——不出声，迟早有人追问"为什么一直是 0"；这一个是**假阳性**——它一直出声说"没问题"，
  **没有人会去追问"为什么一直是 match"**。所以正向观测量除了要有断言它计数正确的测试，
  还要有一条**变异检验证明它可能失败**：把第二次读换回第一次读，对应用例必须转红。
- phase-6a-open-verification (2026-09-10, 指挥会话裁定, **常开收尾项，不随子步关闭**):
  6a 的三条判据（真实管理 TPSL 修改走到止损组替换 / 保本收敛走到同一序列 /
  新入场开仓后新链解析为 `agreed` 且排除判据取到正面样本，见 `phase-6a-cutover-window-criteria`）
  在 6a 窗口内**一条都没取到**（全程 0 仓位）。裁定：**之后每个观察窗都顺带核对这三条**，
  一旦交易所出现仓位并发生一次真实保护修改，就逐笔核对并补记到本文件，**不等 6b/6c 结束**。
  在补记之前，6a 的生产验证是不完整的，任何地方都不得用"6a 窗口达标"代替它。
  **6b 切换的额外放行条件**：至少取到其中一条真实样本（一次真实止损替换走通新路径且回读一致），
  或由指挥会话明示免除；没取到就停在影子阶段报指挥会话。
- read-failure-symmetry (2026-09-10, A 线 local_22ee72a5 指出、本会话记录): 硬性禁止第 4 条
  （"读不到不得解释为零"）在两条线上各出现了一次，**方向相反、判据同一条**：
  A 线是**入口**——账户读为空时，第一次空读不做任何事（`empty_unconfirmed`），
  ≥60 秒后第二次空读才认（`empty_confirmed`）；2026-09-10 窗口里一次真实的
  `Connection reset by peer` 恰好落在一次空读判定上，规则守住了，**这是一次没人构造的故障注入**。
  B 线是**出口**——撤单之后回读挂单列表若读失败，算"不知道"而**不算"已经撤掉了"**
  （`protection_old_order_absence_unproven`），否则账本会把一张可能仍在武装的止损标成已撤。
  **同一条禁止，一个管"没读到不等于没有"，一个管"没读到不等于已经没了"。**
- phase-6a-completed (2026-09-10, 会话 local_4a6676b0, **子步 6a 完成，但判据未取得生产样本**):
  切换本体 **`e99d829f0060053024c10d0f24f7e02c275585b5`**，2026-09-10T13:14Z 经 `tg-deploy` 上线，
  **回滚参考 `42034e08`**（部署前生产 HEAD，A 线 A-10d）。部署后已按新规矩立即推上共享分支。
  含三个提交：切换本体、排除判据、AGENTS.md 部署规矩。
  影子提交 `75652eec` 见 `phase-6a-shadow-window`。
  **窗口（对 HEAD `5c07ab6d`，含 A 线 A-10e）**：13:30:29Z ~ 14:00:35Z（30 分 06 秒）、31 采样、
  **零重置、head_ok 全程 1、units_ok 全程 1**、真实消息 12 条 / **4 个群**，`WINDOW_MET` 自行退出。
  证据 `/root/evidence/phase-6a-cutover-r2/observer-samples.jsonl`。
  35 轮 reconcile，`positions_seen` **31 个采样全为 0**；窗口内新增：保护 incident 0、
  `position_mutation_intents` 0、`trigger_protection_intents` 0、影子行 0、`uncertain` 0。
  **对照起窗前写下的三条判据（`phase-6a-cutover-window-criteria`）：一条都没取到。**
  起窗时交易所 0 仓位（用户 12:50Z 手工平掉 BTC 空单），窗口全程 0 仓位，所以
  管理 TPSL 修改替换、保本收敛替换、新仓位归属与排除判据的正面样本**全部为零**。
  **6a 的新写入路径在生产上仅有测试覆盖（focused 26 + 全量 8267 passed / 0 failed），无仓位样本。**
  **覆盖面更正（2026-09-10 事后查明，见 `phase-6a-coverage-fact`）**：6a 改的是
  `adjust_position_tpsl`，而**自动（KOL）管理指令进不了那个函数**，所以新绑定链**只覆盖人工管理路径**；
  自动路径仍走 `strategy_management_executor` 的 `match_position_protection`，
  改造它已立为追加子步 **6g**（排在 6e/6f 之后）。
  **两条限制必须一起读**：(1) 本窗口观察的 HEAD 同时含 A 线 A-10e，所以"无回归"是**联合证据**，
  不是 6a 单独的（A 线对其 L1 窗做了对称声明）；(2) 消息数达标（12 条 / 4 群）**不等于**本步判据被触发——
  L2 的消息门槛衡量的是系统在有流量时是否健康，与"保护写入路径是否被走到"是两件事，
  本窗口满足前者、完全没有后者。
  **待补**：交易所再次出现仓位、且发生一次真实保护修改时，需按判据逐笔核对一次；在那之前
  6a 的生产验证是不完整的，不得以"窗口达标"代替。
- phase-6a-cutover-window-criteria (2026-09-10T13:30Z, 会话 local_4a6676b0, **起窗前写下，不是收窗后补的**):
  6a 切换窗口（生产 HEAD `5c07ab6d`，含 A 线 A-10e；回滚参考 `e99d829f`）**要取到下列任一才算证明了本步**：
  (1) 一次真实的管理 TPSL 修改走到止损组替换——逐笔记下挂了哪个 ordId、回读结果、撤了哪些旧单、撤净回读结果；
  (2) 一次保本收敛走到同一条替换序列；
  (3) 一次新入场成交开出仓位，新链把它解析成 `agreed`；若同时有在挂入场单的自带止损，
      `excluded_pending_entry_stops ≥ 1`（排除判据的正面样本）。
  同时要求：`protection_authority_refused` 为 0 或逐条归因；`stop_resize_replace_incomplete` 为 0。
  **预先声明取不到的情形**：起窗时交易所 0 仓位（用户 2026-09-10T12:50Z 手工平掉 BTC 空单），
  只剩 binding 347 的两张挂单入场。若窗口结束时仍是 0 仓位，上面三条**一条都取不到**，
  那就如实记"6a 切换无仓位样本、仅测试覆盖"，**不拿窗口达标充数**。
  **另一条必须写明的限制**：本窗口观察的 HEAD 同时含 A 线 A-10e，所以"无回归"是**联合证据**，
  不是 6a 切换单独的。A 线对自己的 L1 窗做了对称声明（其 HEAD 含本会话的真实交易所写入语义）。
- window-evidence-value (2026-09-10, B 线 local_4a6676b0 与 A 线 local_22ee72a5 同日对照后记):
  **一个窗口的证据价值由"被改的判据"决定，不由"窗口的健康度"决定。** 同一个市场状态对两条线的价值可以相反：
  2026-09-10T12:50Z 用户手工平掉最后一个仓位之后，交易所 0 仓位——
  对 B 线的 6a 切换（改的是保护单归属与替换顺序）这意味着**新写入路径一个样本都取不到**，
  30 分钟零重置全绿的窗口只能证明"没有回归"；
  对 A 线的 A-10e（改的正是"账户真空时扫描永久冻结"）**真空恰恰是它的实测条件**，
  同一段时间反而是它今天唯一能拿到"守卫生效"正面样本的机会。
  所以窗口达标**不是**一个通用结论，报告里必须写清"这个窗口证明了哪条判据、哪条没被触发"，
  而不是用"30 分钟全绿"覆盖过去。与本文件"缺陷不再发生本身不是证据"同源：
  那条讲的是计数为零有多个成因，这条讲的是**窗口合格与判据被验证是两件事**。
- phase-6a-suite-environment (2026-09-10, 会话 local_4a6676b0, 指挥会话质询后查明): 我先前报的"全量 15 failed，与基线 da1add77 逐条相同"，
  **事实无误但框定错了**——那从来不是基线的性质，是**我的工作树里没有 `.venv`**。
  `tests/test_server_update_scripts.py` 与 `tests/test_minimal_server_updater.py` 把
  `PLANNER_PYTHON=ROOT/.venv/bin/python` 传给被测 shell 脚本，`ROOT` 是测试文件所在的仓库根；
  主检出有 `.venv`、工作树没有，脚本一律 `exit 2 "Planner Python is unavailable."`。
  工作树内建 `.venv -> ../../.venv` 符号链接（`.gitignore` 已覆盖，不提交）后**全量 8257 passed / 4 skipped / 0 failed**。
  教训与本文件多处同源：**"与基线相同"不是解释，只是把两个都没查的现象并排放着**；
  差异出现时要问"我和对方的命令行与环境差在哪"，而不是先假定是代码。
- phase-6a-pending-entry-stop-lookalike (2026-09-10, 会话 local_4a6676b0 只读查明，**推翻本会话先前的归因**):
  6a 影子窗口里 26 次 `chain_frozen` 全部归因于仓位 `1001125178552543`，我先前报成"那个 unmanaged 仓位的两张无主挂单"。
  **真实成因不是那个仓位，而是一类同形**：`1001125208806869`（sz 6）与 `1001125208807099`（sz 14）
  是**我们自己两张仍在挂着的限价入场单自带的止损**（binding 347 的腿 597/598，request
  `sz 6.0/14.0`、`slTriggerPx 81000.0`、同 instId/posSide，落库 02:41:16.95Z / 02:41:18.61Z；
  cTime 02:41:17Z / 02:41:18Z）。止损随入场单附带写入（ARCHITECTURE 4.7），**不走 set-position-sltp**，
  所以 `position_mutation_intents` / `execution_events` / 保护账本里一行都没有——这不是异常，是那条路径的正常形状。
  止损 ordId 恰为入场 ordId 减一，**记录但不采信**（硬性禁止第 1 条：id 相邻是分配模式不是外键）。
  WS 收件箱两帧俱在、`trade_unit_id="default"`（仓位尚未存在），02:41 前后最近的缺口是 02:33:01–09 与 02:51:18–28，**都不覆盖**。
  该仓位只有 3 张，而这两张止损是 6 和 14，本来就不可能是它的。旧两张止损（83000 sz 3 / 83166 sz 0）仍在挂单里。
  **结论**：一张挂在"尚未成交的限价入场单"上的止损，与"某仓位的无主止损"在 `trigger-orders-pending` 里完全同形
  （`TPSL` + 无 posId + `TU=default`），于是保护链会冻结**任何与在挂限价入场单同 instId+posSide 的仓位**。
  旧匹配器今天有同一个盲区（`global_unowned_order_present`），所以这是复现既有缺陷而非新引入，
  但排除判据待指挥会话裁定后补。
- phase-6a-shadow-window (2026-09-10, 会话 local_4a6676b0, **6a 影子部署已完成并收窗**):
  分支 `rest-ws/phase-6-protection-authority`，提交 **`75652eec1ce21c9646b434b4ab69df8a3d915651`**，
  2026-09-10T11:22Z 经 `tg-deploy` 上线；**回滚参考 `4953490b4d8414b449c790fc0784e4bac83f40b9`**（部署前生产 HEAD）。
  纯影子：新绑定链只计算不写交易所。窗口 11:24:00Z ~ 11:54:08Z（30 分 09 秒）、31 个采样、
  **零重置、head_ok 全程 1、units_ok 全程 1**、真实消息 7 条 / 2 群，达标退出写 `WINDOW_MET`。
  证据 `/root/evidence/phase-6a/observer-samples.jsonl`。
  **逐笔比对结果**：26 轮 reconcile、104 次仓位比对，`agreed` 78、`chain_frozen` 26、
  **`set_mismatch` 0、`chain_resolved_legacy_ambiguous` 0、`unbound_position` 0、读失败 0**。
  26 次冻结全部是同一个仓位 `1001125178552543`（即 6-pre-4 已发现、转 A 线归因的 unmanaged 仓位），
  冻结原因 `protection_order_unattributable`，两张挂单 `1001125208806869` / `1001125208807099`
  既不在账本、也没有 TU 帧；**旧匹配器对同一个仓位同样拒绝**（`global_unowned_order_present`），
  所以新链没有丢失任何现有能力，两条路在同一个地方停下。
  execution_events 只落了 4 行（首轮各仓位一行），此后 26 轮无变化即不追加——变更追加的去重按预期生效。
  **本窗口没有出现的两种情形，如实记为未取样**：`chain_resolved_legacy_ambiguous`（新链的增量能力）
  与"一张同时带 SL 和 TP 的合并单"（会冻结）在生产 30 分钟内**一次都没发生**，只有测试覆盖。
- phase-6a-contract-changes (2026-09-10, 会话 local_4a6676b0): 6a 改了两条既有测试写死的契约，显式记录以免日后被当成偷改。
  (1) `tests/test_deepcoin_private_ws.py::_ALLOWED_INBOX_READERS` 加入 `protection_authority.py`：
  `TU` 是唯一能把保护单连回仓位的字段（`OS` 每次写入都变、REST 从不同时给出两者），所以保护链必须读
  `deepcoin_ws_events`。**推送不被单独采信**——被认领的单必须同时出现在 REST `trigger-orders-pending`
  且 instId / posSide / `triggerOrderType=TPSL` 相符，这是硬性禁止第 5 条要求的"先唤醒、再 REST 核验"形状，
  不是它的例外。
  (2) `tests/test_naked_fill_stop_net_boundary.py` 通往 `submit_exact_position_sltp` 的调用者名单：
  加 `protection_replacement.py`、去掉 `deepcoin_execution_actions.py` 与 `break_even_convergence_executor.py`
  （两者改为经共用件调用，直接 import 成了死代码并删除）。**路径数因此少一条而不是多一条。**
- phase-6a-deploy-gate-defect (2026-09-10, 会话 local_4a6676b0 自查): 部署前的"2 分钟无 auto_trade 消息"
  用 `journalctl | grep -ci auto_trade` 判定，**它永远不会归零**：生产上每约 65 秒有一条
  `lifecycle_monitor Skipping simulated lifecycle entry in an auto_trade group: lifecycle_id=1145`，
  那是一条说明"什么都没做"的周期日志，却含有 `auto_trade` 字样。照此判据等下去，部署窗口永不出现。
  形状与同日 A 线的 `rounds` 恒为 0、以及 6-pre-4 的探活翻倍**同源**：
  **观测量的定义依赖了一个没人保证的不变量**（"含 auto_trade 字样的日志行 = 有交易活动"）。
  改用可判定的库内判据：`message_processing_jobs` 无 claimed/processing、`position_mutation_intents`
  无未决行、近 2 分钟无 `execution_events`、无运行中批次；并单独确认那条日志之外**再无**其他
  auto_trade 行。本次部署即按新判据放行（四项全 0）。
- release-authority-delegation (2026-09-11, 用户在指挥会话明确授权，原话"授权放开"): 用户授权指挥会话直接放开满足以下条件的交易所写入路径，事后告知用户，不再逐项请示：(1) 该路径已经过影子期，影子记录证明算出的动作与出示明细一致且全窗零非预期写入；(2) 作用对象仅限系统自己在 auto_trade 群开出、归属已 verified 的仓位。仍须用户单独批准的两类：市价平仓类动作（含 break-even 的 full_exit、任何 close_position 新路径）；改变交易语义的新规则。用户同时确认：历史数据不掐断（对速度无影响，系统本就只管理已核实归属的仓位）；后续优先级以"未来消息精准安全自动执行"为唯一目标：6g（自动管理路径改走绑定链）→ 6b 切换 → 退役旧匹配器 → A 线扫描类降级为仅做阻塞项。
- phase-6-approval (2026-09-10, 用户在指挥会话 local_858790fe 明确批准，原话"批准阶段6"): 阶段 6（新绑定链驱动 TPSL 修改、撤销与平仓，L3）获批。批准前已向用户出示：阶段 5 逐笔保护确认（binding 346 市价腿回执无 posId、三重确认在提交时通过、止损 2445 与三档止盈在交易所；限价腿 9 字段无 clOrdId 被接受、止损随单附带在成交前已存在；无"成交但无可验证止损"）与补测第 10 项结论（set-position-sltp 为叠加语义、每次写入新 ordId；TU 恒等于 posId 30/30，即新旧保护单的可查关联）。附加裁定：任务 1 的替换顺序改为 A-5e 形状"先挂新→回读确认→按 TU==posId 撤旧全集→确认撤净→改账本"，撤旧失败保留新单并告警冻结，phase-6-protection-authority.md 已同步改写。领取条件：6-pre-4 completed 后 current_phase 置 6，由 B 线会话领取；unverified 绑定一律拒绝修改/撤销/自动平仓，撤销前精确回读，web 角色无执行权限，重启后先查询核对认领不重复挂保护。
- phase-6-pre (2026-09-09, 指挥会话): 阶段 5 完成（首笔真实入场 binding 346：市价腿回执无 posId、三重确认在提交时通过；限价腿 9 字段无 clOrdId 被接受、止损随单附带在成交前已存在；5a 护栏首次面对真实活挂单 allowed）。阶段 6 之前插入三项前置：6-pre-1 WS 缺口入场改为可重试推迟（L2）；6-pre-2 B-5d 市价成交裸仓安全网（L3，需用户批准）；6-pre-3 补测第 10 项修改 TPSL 后 OS/TU 稳定性只读观测。见 phase-6-pre.md。
- phase-5-deploy-authorization (2026-09-08, 指挥会话): 依据用户 2026-09-07 的 phase-5-approval 与用户明确授权由指挥会话裁定，放行部署候选 a3713ec418b5e6e6487631482bcc630079daa52b（回滚 230ba1cc）。裁定 (a) 收紧接受：市价成交归属 unverified 时记录 + critical 告警（market_fill_attribution_unverified，进代码级默认白名单）+ 不动作；后续 B-5d 安全网在阶段 6 前完成、需用户单独批准。观察要求：窗口内每笔新入场逐笔核对止损已挂上，任何“成交但无可验证止损”立即回滚。用户可随时以“回滚阶段 5”否决。
- phase-5a-approval (2026-09-07, 用户在指挥会话 local_858790fe 明确批准): 阶段 5a（list_open_orders 切 V2 orders-pending，含分页、fail-closed、逐调用点分析与护栏，L3）获批领取；部署前须只读拉一次生产当前挂单，确认为空或逐条可归属，不能归属的对象只出示不动作。
- phase-5-checkpoint (2026-09-07, 指挥会话，依据阶段 5 会话 local_ad007c43 汇报): 前置受控实验 10 格全部由用户本人执行完成，实验前后交易所 fingerprint 一致，零非计划写入。结论：限价 order 可用字段组合 = instId, tdMode, mrgPosition, side, posSide, ordType=limit, px, sz, slTriggerPx (+可选 tpTriggerPx)，**不含 clOrdId**（判重键就是 clOrdId 字段存在本身）；市价腿继续带 clOrdId 不动；撤未成交入场单时附带 TPSL 同帧消失，无需额外清理。迁移本体代码在分支 rest-ws/phase-5-order-entry（94dc4632，17 提交）**未部署、未合并**，任务 1/3/4 未完成，阶段留 in_progress。
  **两项影响判断依据的发现**：(a) 阶段 4 判据 2 是循环论证——_ledger_entry_records 读的 response_json 里的 posId 是 _record_submitted_order_legs 写入的、来自 symbol+side 扫描的值，所以阶段 4 的"2/2 exact"不成立；官方 POST /trade/order 响应无 posId，任何读接口都不同时给出 ordId 与 posId。判据 2 已经用户在该会话批准改为"分仓身份等式（普通 order 的 posId == ordId）+ 三重确认（WS Position.PI == ordId 且 Po 非零、REST 该 posId 存在、方向与数量一致）"，任一不成立即 unverified；该等式对条件单不成立，只适用普通 order 入场。阶段 4 差异报告数字作废，待阶段 5 上线后用真实普通 order 入场重取。(b) 间歇 401 = 限流（code 50000，5 次/秒），且 list_open_orders 调的 V1 orders-pending 对普通限价单恒返回空，V2 才命中。
  **指挥会话决定**：在阶段 5 迁移本体部署前插入两个前置阶段——5a list_open_orders 切 V2（L3，会激活从未触发的撤单路径，需用户单独批准）、5b 读限流与 50000 识别（L2）。阶段 5 分支保留，5a/5b 完成后 rebase 继续。
- phase-5-progress (2026-09-07, 会话 local_ad007c43, **前置实验完成，代码进行中，阶段留 in_progress**):
  分支 `rest-ws/phase-5-order-entry`（工作树 `.worktrees/rest-ws-phase-5`），起点 `5c8c2682`。
  **未部署**，生产 HEAD 仍是 A 线的 `b1c12213`。
  **前置受控实验全部完成**，10 格全部由用户本人执行，证据在服务器
  `/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5/`
  （`findings-final.md`、`findings-retest6.md`、`findings-retest-1-3-11.md`、
  `findings-posid-link.md`、各 `cell-*/live-*/`）。
  实验前后交易所 fingerprint 逐字节一致 `c4cd87ec9db6b3bf4d3a38ba1a858fb8e90a586c4836a6a51617016e5698e50a`。
  共提交 12 笔（9 接受 3 拒绝）、撤销 6 笔、成交 2 笔，零非计划写入；
  两个成交仓位由自己的附带保护平掉（一止损一止盈，净约 -0.02 USDT），
  证明附带保护确实会执行。

  **可用字段组合（补测 6，四组单变量对照）**：
  `instId, tdMode, mrgPosition, side, posSide, ordType=limit, px, sz, slTriggerPx`
  （+ 可选 `tpTriggerPx`），**不含 clOrdId**。判重键就是 clOrdId 字段的存在本身，
  与并发无关、与订单经济属性无关、与取值无关、逐笔判定。
  生产市价单一直带 clOrdId 且 149 次全成功，市价腿不要去掉。
  只带 slTriggerPx 的形状（生产将要发的）由 cell s 单独实测接受。

  **补测 3**：撤未成交入场单，附带 TPSL 同帧 `TS:0→4` 并从 pending 消失，无需额外清理。
  **补测 1**：cell 1 第二次（可成交限价）拿到完整链条与 `TU: default→posId` 翻转。
  **补测 2**：最小规模下**不可产生**部分成交（触价挂单量 1687 张 vs 我们 0.2 张），
  一次全量成交，如实记录，未放大规模。
  **补测 11**：`unknown_exchange_outcome` 正确、零重发；但 REST 认不出这笔单。

  **三条改变设计的发现：**
  (1) **`GET /trade/orders-pending` 对活着的普通限价单整体失明**——带保护（cell 11）
  与不带保护（cell v）在四种参数下一律 0 行，而 `GET /trade/order?ordId=` 同刻为 `live`。
  今天没暴露是因为生产限价腿走 trigger-order、市价腿瞬间成交，从无普通活挂单。
  **迁移后 `list_open_orders` 的 20 个调用点会对新入场单读到"没有挂单"**
  （含 `deepcoin_ws_resync` 五步重同步、`execution_bindings`、`terminal_entry_cleanup`、
  `entry_revision_executor`、`runtime_agent_exchange_snapshot`）。绑定链只能按精确 ordId 回读。
  **这一条是阶段 5 剩余工作的已知风险，必须在收尾前处理或明确记录。**
  (2) 精确 ID 回读在状态变更瞬间**短暂返回空**（撤单后立刻读为 `[]`，几秒后为 `canceled`）。
  空回读必须当 unknown 重读，不得判终态。
  (3) 响应丢失后只有 WS 能认出订单，"缺口期间暂停新入场"是硬需求。

  **修掉一个已部署的缺陷：阶段 4 判据 2 是循环论证。**
  `_ledger_entry_records` 读 `ExecutionOrderLeg.response_json` 当作交易所原文，
  但 `_record_submitted_order_legs` 先写了 `stored_response["posId"] = pos_id`，
  而该 pos_id 在响应无 posId 时来自 `_find_open_position_id` 的 symbol+side 扫描。
  **阶段 4 报告的 exact 是自我确认的**（影子链不写交易所、不写业务账本，属报告口径缺陷）。
  官方文档核对确认：`POST /trade/order` 响应字段就是 ordId/clOrdId/tag/sCode/sMsg，
  五笔限价原始回包与生产市价原始回包全部一致，**无 posId**；
  任何读接口都不同时给出 ordId 与 posId；WS 的 Order/Trade 不带仓位字段、
  Position 带 PI 不带订单字段。唯一同时返回二者的是 `POST /trade/increase-position`，
  但 posId 必填、只能加仓。
  判据 2 已改为**分仓身份等式**：普通 order 开出的仓位其 posId 等于该 order 的 ordId，
  且必须三重确认（WS Position 帧 PI 等于该 ordId 且 Po 非零、REST 该 posId 存在、
  方向与数量一致），任一不成立即 unverified。该等式对条件单**不成立**
  （binding 341 的 trigger 腿 order_id `…581473` vs pos_id `…675481`），
  所以只有普通 order 入场能用这条链。用户 2026-09-07 明确批准该形式。
  官方 `close-position-by-ids` 请求用 `positionIds`、错误项用 `tradeUnitId`，
  为 WS 的 `TriggerOrder.TU` 提供了官方命名佐证。

  **已完成的代码**（本地提交，未部署）：
  `scripts/deepcoin_phase5_entry_experiment.py` 实验工具（34 项离线测试）；
  `src/telegram_kol_research/deepcoin_limit_entry.py` 新限价 payload 与迁移判据
  （30 项测试，含用真实 draft builder 产出的腿断言今天所有限价腿都可迁）；
  `deepcoin_shadow_binding.py` 判据 2 重写 + 6 项阶段 4 测试改写（54 项通过）。
  相关测试 308 passed；**全量 7670 passed / 4 skipped / 0 failed**
  （跑的是判据 2 重写之后的最终代码候选；若后续再改生产代码需重跑）。

  **剩余工作**：任务 1 的接线（`recovery_live_submit` limit 分支改走 `place_order`）、
  任务 3（判据转正写真实绑定）、任务 4（`ws_observation_permits_new_entry` 接入入场路径，
  需改 `web_app` 暴露 inbox 与那个守护它只被两模块引用的静态测试）、
  任务 5 复核、全量套件、rebase 到最新、部署、30 分钟观察窗、重启 worker、状态收尾。
  **未部署；未观察。** 全量套件已在当前代码上跑过（见上）。

  **间歇 401 已定性：就是 Deepcoin 的限流。** 只读探针 16:16:20Z / 16:17:41Z 抓到两次，
  响应体均为 `{"code":"50000","msg":"Trigger the api frequency limiting"}`，
  响应头 `X-Ratelimit-Limit: 5` / `Remaining: 0` / `Window: 1s` / `Retry-After: 1`，
  与官方限频页一致（该端点 5 次/秒 150 次/分，全部端点最低档）。
  **`code=50000` 不在官方错误码表里**（表内只有 50100–50115 的认证类），
  限频页又没写超限返回什么，两页之间正好缺这一环，所以前三次排查都判不出来。
  服务器时钟已排除（NTP 同步，偏移 0.000186 秒，与 Deepcoin `Date` 头差 376 毫秒）。
  探针自身仅 0.1 次/秒，够不着限额——**是生产自己的流量在同一秒内用光配额**：
  `list_trigger_orders_pending` 在线调用点众多且各自按合约循环
  （`deepcoin_execution_actions` 8 处、`break_even_convergence_executor`、
  `backup_stop_repair`、`web_app._load_deepcoin_pending_tpsl_orders`，
  以及**阶段 4 新增的 `deepcoin_shadow_binding`**）。今日 129 次。
  现状语义安全（记 `evidence_available=False` 后 continue，未降级成“没有挂单”），
  代价是每天丢约 129 次保护快照证据。
  修法建议（单独立项）：识别 401+50000 为限流而非认证；尊重 `Retry-After` 做一次有界重试
  （读接口，无写入风险）；照 `DeepcoinTpslWriteLimiter` 加一个覆盖 5 次/秒读端点的限流器；
  合并同轮内重复的 pending 快照读。证据 `findings-401.md`、`probe-401.jsonl`。
  阶段 5 收尾必须带上限流识别与重试，否则 `rest_read_incomplete` 会周期性把
  本可 exact 的链压成 unverified（本轮实验已被打断两次）。

  **`orders-pending` 盲区可能是调用了错的接口。** 官方侧边栏“获取未成交订单列表”
  指向 `/docs/zh/DeepCoinTrade/ordersPendingV2`，即 `GET /deepcoin/trade/v2/orders-pending`；
  客户端在用的 V1 `/deepcoin/trade/orders-pending` **没有任何文档页**
  （五个候选 slug 全 404，只在限频表里出现），疑为遗留接口。
  **cell v 第二次（16:21:49Z，ordId `1001125173133199`）取得定论：确实是接口用错了。**
  同一笔 `state=live` 的普通限价单上：V1 四种参数全部 0 行、`contains_our_order=false`；
  **V2 三种全部命中**（`index=1+instId`、`index=1` 无 instId、`index=1+ordId`），
  各返回 1 行且就是本单。V2 还支持按精确 `ordId` 过滤，正是绑定链需要的查法。
  限频上 V2 与 V1 同属「获取未成交订单列表」10 次/秒 / 300 次/分档，比 5 次/秒档宽松。

  **含义：`orders-pending` 盲区不是端点层面的失明，是 `DeepcoinRestClient.list_open_orders`
  在调用未文档化的 V1 遗留接口。** 改用 V2 即可一次性消除 20 个调用点的风险。
  但这不是一行改动：V2 的 `index` 是必填分页参数（从 1 开始，`index=0` 被拒）、
  `limit` 上限 100，必须翻页并在读不完整时 fail-closed；
  更重要的是它把 `list_open_orders` 从「在本账户上恒返回空」变成「真的列出挂单」，
  会激活此前从未被触发的代码路径（例如 `terminal_entry_cleanup` 会开始看见并撤销
  它以前看不见的入场单）——这是有交易所写入后果的行为变化，
  **必须单独做一遍逐调用点分析、测试与观察窗，不能顺手带进阶段 5 的提交。**

- phase-5-approval (2026-09-07, 用户在指挥会话 local_858790fe 明确批准): 用户判断“只用 REST 拿不到确定性外键，不改源头修不完”，决定阶段 5 不再暂缓，与事故修复（docs/management-reliability-status.md）两线并行。阶段 5 收益定位改为“确定性替代推断式候选匹配”而非降低延迟。前置受控实验仍需逐笔由用户本人执行真实下单命令，执行会话只准备命令与分析证据。两条硬性约束写入阶段文件。
- phase-5-hold (2026-09-07, 指挥会话): 阶段 5 暂不领取。原因一：用户报告两起管理指令未执行事故（峰哥止盈、大镖客保本），正在只读排查，实盘保护优先于改造；原因二：阶段 4 差异报告显示 WS 链在入场归属上没有延迟收益（timing_only 中位 -2.652 秒，市价入场的 posId 在下单响应里同步可得），阶段 5 的收益要重新定位为“确定性替代推断式候选匹配”，需用户就此达成共识后再批准。阶段 5 的两条硬性约束已确认：place_order 响应体必须整体持久化（posId 只在那一次出现）；保护单归属唯一确定性来源是 WS 的 TriggerOrder.TU，REST 无法佐证，重连不重推，缺口期间暂停新入场不能放松。
- phase-4-approval (2026-09-07, 用户在指挥会话 local_858790fe 明确批准): 阶段 4（影子绑定链与差异报告，新表 `deepcoin_shadow_bindings` / `deepcoin_shadow_diffs`，L3）获批领取。附加门槛：影子表至少 3 条真实入场产生的链才算观察完成，上限 48 小时；不为凑样本下单。
- out-of-scope-401 (2026-09-07, 指挥会话): `GET /deepcoin/trade/trigger-orders-pending` 间歇 401 Unauthorized 已在 2026-09-06 17:04Z、18:31Z、2026-09-07 02:27Z 复现三次，全部落在同一端点、同一既有路径 `web_app._load_deepcoin_pending_tpsl_orders`，语义正确（记为证据不可用，不降级为无挂单）。尚未判定是签名时间戳容差（服务器时钟漂移）还是限流。不在本项目范围，建议单独排查：先比对服务器 NTP 偏移与 Deepcoin 返回头里的时间，再看该端点的调用频率。
- phase-2-open-ended-observation (2026-09-06, 用户在指挥会话 local_858790fe 明确授权): 两次 30 分钟观察均因夜间零消息停止，代码侧无待办。用户决定阶段 2 的收尾观察改为开放式：服务器端后台监视器每分钟采样，直到出现一个完整的 30 分钟窗口满足 ≥5 条真实消息、尽量 2 个群且全部健康检查通过为止，自行停止；会话按定时查看结果。AGENTS.md L2 已加入该例外条款。
- phase-2-completed (2026-09-06, 会话 local_a6d6d24f, **开放式观察达标，阶段 2 完成**):
  只读观察收尾，**未改代码、未部署、未做任何交易所写入**；生产 HEAD 全程
  `0371fc9f4fc41c588fab1534f8e33419aef4d6cf`（分支 `live`）。
  监视器 `open-observer.sh`（PID 2363944）自 `18:01:57Z` 每 60 秒采样，共 **310 个采样点 / 5 小时 9 分**
  （`18:01:57Z ~ 23:11:19Z`），自行判定达标后写 `DONE` 退出。
  **合格窗口 `2026-09-06T22:42:17Z ~ 23:11:19Z`（30 个连续健康采样点），整段落在本会话那次
  worker 重启（`17:58:05Z`）之后。** 窗口末次采样的近 30 分钟回看区间内
  **真实消息 9 条 / 2 个群**（`-1002282384698` 5 条 23:07:33~23:09:09Z、
  `-1002409877375` 4 条 23:10:28~23:11:06Z），首次满足 L2 的 ≥5 条 ≥2 群目标。
  窗口内逐项恒定：三单元 30/30 `active`、NRestarts 全程 0；`state` 恒 `healthy`、
  `connected` 与 `permits_new_entry` 恒 true、`open_gap_count` 恒 0、
  `last_resync_outcome` 恒 `converged`、`unparsed_count` 恒 1（零新增，仍是阶段 2 部署前那条
  listen key 过期帧）、`processed=6 / unprocessed=0 / duplicate=0` 无积压。
  窗口内 3 次 `silence_timeout` 计划内重连（缺口 id 49/50/51，2.81/3.44/2.94 秒）全部闭合，
  每个采样点看到的都是 `open_gap_count=0`。
  交易所首尾 fingerprint **逐字节一致**
  `283091021fc8391834efb3c2b49c968fd576940d4a8d01b91c3c287a4b79d70b`
  （`complete=true, position_count=1, open_order_count=0`），**零新增写入、零差异**，
  与前两次观察也完全相同；那一个仓位仍是生产自动交易 15:28:19Z 自己开的。
  **基线仍未取到真值**：`events_last_hour` 在全部 310 个采样点上恒为 0，WS 事件表整期零新增
  （`processed` 恒 6）。因此 `duplicate_rate_1h=0.0`、`out_of_order_count_1h=0` **依旧是零流量
  地板值，不是实测速率**——达标的是 Telegram 消息流量，账户在这 5 小时内没有任何交易活动，
  所以没有业务帧可去重、可乱序。真基线要等第一个有真实 WS 业务帧的窗口，阶段 3 应顺带取到。
  **异常 1 条（采样点口径），日志口径实为 4 次断线 5 条记录**：`20:09:06Z` 采样抓到
  `state=disconnected` / `open_gap_count=1` / `last_resync_outcome=not_converged:incomplete_rest_read`
  / `permits_new_entry=False`。日志显示完整经过：`20:09:01Z` 静默 600s 触发计划内重连 →
  `20:09:04Z` 五步重同步第一轮 REST 读不完整，**拒绝收敛**并关闭新入场 →
  `20:09:09Z` 重试收敛回 `healthy`，缺口 id=33 共 8.054 秒闭合。
  全期扫日志：31 次 `silence_timeout` 重连里有 **4 次**首轮出现 `incomplete_rest_read`
  （`18:58:32Z` 与 `18:58:35Z` 属同一次断线的连续两轮、`20:09:04Z`、`20:59:26Z`、`22:19:57Z`），
  约 13%；每次都 fail-closed 后数秒内重试收敛，**没有一次把读失败当成"零"**，
  32 条缺口（1 条 `process_start` + 31 条 `silence_timeout`）全部闭合、零未闭合、
  `deepcoin_private_ws` 零 ERROR 零 traceback。这正是硬性禁止第 4 条要的行为。
  **401 复现，不再是孤例**：`18:31:55Z` 又一次
  `GET /deepcoin/trade/trigger-orders-pending?instId=ETH-USDT-SWAP` 返回 `401 Unauthorized`
  （既有 `web_app._load_deepcoin_pending_tpsl_orders` 路径，抛 `DeepcoinClientError`、
  置 `evidence_available=False` 后 `continue`，语义正确）。加上 `phase-2-observation-2` 记的
  `17:04:07Z` 那次，这是**间歇性可复现**现象而非偶发。
  遗留 (c)：`incomplete_rest_read` 的日志只有结论没有细节，无法直接判定它是否与这个间歇 401 同源
  （时间上不重合，但两者都指向同一批 Deepcoin GET）。阶段 3 让 WS 事件唤醒 REST 核验时，
  应把失败的具体调用与 HTTP 状态一起记进重同步结果，否则这类 incomplete 无法归因。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-2/`
  （`open-observer.sh`、`open-observer.jsonl` 311 行、`open-observer-summary.json`、`DONE`、
  `exchange-snapshot-open-start.json`、`exchange-snapshot-open-end.json`）。

- phase-2-open-observer-started (2026-09-06, 会话 local_a6d6d24f, **只读**): 按上一条的用户授权启动服务器端开放式监视器。
  只读核实：生产 HEAD `0371fc9f`（分支 `live`）、三单元 active/NRestarts=0、ws-health `healthy` / `open_gap_count=0`。
  本阶段要求的那一次 worker 重启：`2026-09-06T17:58:05Z`（MainPID 2345403→2362557），缺口行 id=20 `process_start`
  2.427 秒闭合，五步重同步 931/3/0/813/0 ms `converged`，`17:58:11Z` 回 `healthy`。**合格窗口必须整段落在该重启之后。**
  起点 fingerprint `283091021fc8391834efb3c2b49c968fd576940d4a8d01b91c3c287a4b79d70b`
  （`complete=true, position_count=1, open_order_count=0`，与前两次观察逐字节一致）存于
  `rest-ws-phase-2/exchange-snapshot-open-start.json`。
  监视器 `rest-ws-phase-2/open-observer.sh`，**PID 2363944**，`2026-09-06T18:01:57Z` 起每 60 秒采样一次写
  `open-observer.jsonl`；只做 curl 本机 ws-health、`systemctl show`、`sqlite3 ?mode=ro` 三件事，不写库不碰交易所。
  停止条件：某次采样近 30 分钟消息 ≥5 且群数 ≥2，且最近 30 个采样点全部健康（三单元 active、NRestarts=0、
  state 恒 healthy、open_gap_count 恒 0、last_resync_outcome 恒 converged、unparsed_count 恒 1 无新增、
  unprocessed=0）；消息 ≥5 但仅 1 群持续超 2 小时亦接受并在 summary 标注。上限 24 小时写 TIMEOUT。
  健康项不通过不停止，记 anomaly 后健康窗口从下一次通过重新累计。

- phase-2-approval (2026-09-06, 用户在指挥会话 local_858790fe 明确批准): 阶段 2（去重、乱序保护、心跳、断线状态机、REST 重同步，L2；若加列则该提交按 L3）获批领取。用户强调硬性规则第 12 条是本阶段的核心验收。
- identity-note (2026-09-06, 指挥会话核实): 生产 `deployment-identity` 的 `loaded_artifact_verified=false` 与 capabilities 全 false 是门禁退役后 `/etc/telegram-kol-worker.env` 不再设置 `TELEGRAM_KOL_RELEASE_COMMIT` / `_MANIFEST_SHA256` 的结构性结果。代码核实：这些标志的唯一消费者是已退役的 `scoped_release_activation.py` 和已停用的 monitor 命令，不门控任何交易路径。各阶段文件的前置判据已改为“worker 各 loop 存活 + authority_evidence 新鲜”。可选后续：让 tg-deploy 写入 release commit 让身份端点恢复有意义。
- phase-1-approval (2026-09-06, 用户在指挥会话 local_858790fe 明确批准): 阶段 1（新表 `deepcoin_ws_events` + `websockets` 依赖，L3）获批领取。同轮用户告知已自行处理掉交易所上三张 2026-09-03 的历史条件入场单，当前无挂单；阶段 3/4 的比对基线不再需要为它们建模。用户同时提出硬性要求第 12 条（断线/重启后重新对齐）。
- defect-out-of-scope (2026-09-06): `trigger_take_profit_convergence_executor.py:506-511` 对未过滤的 pending 原始行逐行调用只适用于保护单的 `_native_tpsl_aliases_consistent`，入场条件单会触发 `convergence_pending_alias_conflict` 全局否决。不属于本项目范围，需单独立项：先写复现测试，再把否决范围收窄到保护单行。

执行会话在此追加，格式：`- phase-N (日期, 会话ID): 提交 SHA；做了什么；验证结果；遗留问题`。

- phase-4 (2026-09-07, 会话 local_98b3dc80, **完成**):
  提交 `294bd54b2881b6a44e2d749d9ec479d453985d36`（部署的就是它），
  部署 `tg-deploy 294bd54b…`，回滚 SHA `4bdc6ba6c43dfadb393ac905a66104d65b651d2b`
  （回滚保留影子表不删）。
  **共 6 次部署**，每一次都是前一次在生产上暴露出错数字后的修正
  （`da7ef255` → `9a9834bf` → `afcad2a2` → `59156895` → `d62d25f6` → `ddda43cb` → `294bd54b`）。
  实现：两张影子表随 `init_db` 建；`deepcoin_shadow_binding.py` 按五条合取判据建链，
  每条判据单独不满足时落到各自具名的 `refusal_reason`；
  `deepcoin_shadow_diff.py` 产出八类差异与只读汇总；
  `deepcoin_shadow_ownership.py` 把「这个对象属不属于系统」收敛成一处、覆盖 9 张账本
  （含阶段 4 要求的 5 张，以及阶段 3 漏掉的 `position_take_profit_orders`）；
  localhost-only 端点 `/api/runtime/deepcoin-shadow-binding-report` 只返回计数；
  CLI 导出器 `deepcoin-shadow-binding-export` 把明细写服务器证据文件；
  `deepcoin_reconcile` 每轮加一行结构化日志（触发来源、唤醒帧接收时间、起止时间、
  触碰 binding 数）。
  **影子写入用会话级守卫**：flush 前拒绝任何非影子表对象，配静态 import 守护测试。
  schema 演练（生产库副本）：`quick_check` 前后均 `ok`，五张关键表行数完全不变
  （342 / 666 / 184 / 197 / 15209），524 个既有 sqlite_master 对象
  **removed 0、modified 0**，新增 11 个（2 表 + 9 索引），表数 90→92。
  验证：focused 53 passed；全量 **7605 passed / 4 skipped / 0 failed**；
  `tests/test_runtime_event_loop_blocking_census.py` 通过（新增调用都经
  `run_on_management_worker`，未进允许清单）。
  观察窗口 `06:50:04Z ~ 07:19:07Z`（30 个连续健康采样，监视器自判达标）：
  真实消息 **8 条 / 3 个群**；`state` 恒 healthy、`open_gap_count` 恒 0、
  `last_resync_outcome` 恒 converged、`unparsed_count` 恒 1 零新增、
  `reconcile_failures` 恒 0；`by_wake` 恒 0（窗口内零业务帧，退回纯轮询，符合设计）。
  异常 1 条且**在合格窗口之前**（`06:49:04Z` 空闲静默计时器的计划内重连，fail-closed 正确）。
  交易所首尾 fingerprint 逐字节一致 `ddcaa6a0aae69c2f4fef9d224844a5e59d8b3260f79b3f27208e8acb2956effc`；
  七张交易账本窗口前后**一行未动**，仅 `authoritative_execution_attempts` 412→418
  由窗口内 8 条消息解释。

  **差异报告关键数字（阶段 5 的批准以此为依据）：**
  链总数 **5**；**exact 2 / unverified 3**（`exact_ratio` 0.40）。
  stage：active 2 / order_live 1 / terminal 2。
  八种 diff_kind：**shadow_only 0、ledger_only 2**、pos_id_mismatch 0、
  protection_ord_id_mismatch 0、side_mismatch 0、size_mismatch 0、price_mismatch 0、
  **timing_only 4**，提前量 -4.903 / -4.666 / -0.637 / +4.531 秒，
  **中位提前量 -2.652 秒（负值＝既有账本先得出结论）**。
  `exact_ratio` 的分母含两条本来就不可能 exact 的对象（一条未成交挂单、
  一条系统外手工平仓）；**在「有成交帧的真实入场链」这个真正分母上是 2/2 全部 exact**。
  `ledger_only` 两条均已逐条归因，无一指向影子链认错或漏认：
  (1) `1001125163581473` = binding 341 的条件入场单，已提交在挂、尚未成交，
  无 Trade 帧故判据 1 不成立，stage `order_live`；
  (2) `1001125157891231` = binding 340 的入场，仓位在 `05:49:01Z` 被系统外手工平仓，
  两张保护单同帧转 `TS=4` 后 REST 不再返回，stage `terminal`（此前它确实曾判为 exact）。
  `shadow_only` 为 0 **且在当前架构下不可能非 0**，原因见下条发现 1。

  **生产实测四个新事实（都是先看到错数字再查出来的，不是推理）：**
  **(1) REST 没有任何「读」接口能把 ordId 关联到 posId。** 逐个只读查过：
  `list_trade_fills*` / `list_order_history` / `get_order_history_by_id` 都没有 `posId` 字段；
  `list_positions` / `list_position_history` 有 posId 但没有 ordId；
  `list_trigger_orders_pending` / `list_trigger_order_history` 两者都没有。
  唯一同时出现的位置是 `POST /deepcoin/trade/order` 的响应体**顶层** `posId`
  （与 `data` 平级），生产账本本来就在用它（`direct_order_position_id`，evidence tier 0）。
  判据 2 因此改为读这份响应、再要求交易所在 positions / position_history 里
  确认该 posId 存在且方向一致。**对阶段 5 的硬性含义：新绑定必须在 `place_order`
  返回的那一刻把整个响应体存下来，只存 ordId 就永久丢失唯一的确定性链接。**
  **(2) REST 的 pending TPSL 行既无 `posId`、`sz` 也恒为 `"0"`。** 因此判据 3
  （`TriggerOrder.TU == posId`）**只能**由 WS 证明，REST 无法佐证；判据 5 的
  「数量一致」在 REST 侧拿不到可用数字。阶段 6 要用新绑定驱动 TPSL 修改/撤销时，
  必须知道保护单归属的唯一确定性来源是 `TriggerOrder.TU`，而它只在 WS 上。
  **(3) 补测项 12：重连后不重推。** 前提具备（3 个活仓、每个都带 `TU=posId` 的活 TPSL、
  影子链判 exact）。专门重启 worker `05:15:29Z`（MainPID 2578954→2580000，
  缺口 id=90 2.64 秒闭合，五步 1074/4/0/861/1 ms converged，seeded 10），
  重新订阅后 **95 秒零帧**；当天另有 6 次本地断线重连（缺口 84–89），同样零推送。
  **Deepcoin 私有流在（重新）订阅时不发快照，只推变化。** 这证实阶段 2 五步 REST
  重同步是必需的，且「缺口期间暂停新入场」不能因「重连会补推」而放松。
  **(4) 补测项 9 拿到真实数据。** `05:49:01Z` 一次平仓：仓位 `Po→0`，
  **两张保护单在同一批帧里一起 `TS:"1"→"4"`**，没有单独撤单动作，随后 REST 不再返回。
  触发平仓的不是这两张保护单（止损 2430 / 2425.14，成交价 2509.75）。
  补测项 8：三条真实入场链**每条都挂 2 张**保护单，按集合比对通过。

  **窗口内一笔系统外手工平仓，已完整归因。** ordId `1001125166582264`
  （market / reduceOnly / clOrdId 为空 / +10.0099 USDT）对全库**逐表逐列扫描
  不存在于本系统任何一张表**；系统既有路径正确处理（binding 340 → `closed`，
  leg 585 → `manually_closed` / `manual_position_missing`）。
  它暴露了归属扫描的真实缺口：**只查活对象的扫描看不见一笔立即成交的手工平仓**。
  扫描已扩到 `list_trade_fills` + `list_order_history` 并按时间窗过滤，
  窗口内未归属对象**恰好 1 个**，就是它，其余全部归属明确。
  **阶段 5/6 必须采用扩展后的口径**：「这个对象属不属于系统」要查成交流水，不能只查活对象。

  **本阶段代码零交易所写入**：直读交易所历史 `complete=true`、零 read failure、
  `04:40Z` 起本系统零新增对象；影子模块只调用既有 `list_*` GET，且有静态测试
  守护它不 import 任何账本写入模块。
  遗留：(a) 间歇 401 本阶段**未复发**（前三次分别在 09-06 17:04Z、18:31Z、09-07 02:27Z），
  仍未定位，不在本项目范围。
  (b) `exact_ratio` 的分母包含 terminal / 非入场对象，读报告必须先看 `counts_by_stage`；
  若阶段 5 需要单一指标，建议另出「有成交帧的入场链」口径。
  (c) 空闲账户上静默计时器每 10 分钟一次计划内重连，会周期性打断健康采样连续段
  （本次 49 个采样点里命中 1 次）；阶段 2 遗留 (b) 未处理，观察成本而非缺陷。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-4/`
  （`deployment.md`、`findings.md`、`observation-summary.md`、`schema-rehearsal.json`、
  `open-observer.sh`、`open-observer.jsonl` 50 行、`open-observer-summary.json`、`DONE`、
  `shadow-report-baseline.json`、`shadow-report-final.json`、`final-numbers.json`、
  `summarize.py`、`nonbinding_scan.py`、三份 `non-binding-*.json`、
  三份 `exchange-snapshot-*.json`、`exchange-writes-since-phase-start.json`）。

- phase-3 (2026-09-07, 会话 local_c8d0dc4e, **完成**):
  提交 `4bdc6ba6c43dfadb393ac905a66104d65b651d2b`（部署的就是它），
  部署 `tg-deploy 4bdc6ba6…`，回滚 SHA `0371fc9f4fc41c588fab1534f8e33419aef4d6cf`。
  部署后另有一个**只加测试、不改生产代码**的提交 `e4f0116f`（断言两个 worker 任务
  拿到的是同一个唤醒信号对象），生产运行的始终是 `4bdc6ba6`，四个改动文件与提交逐字节一致。
  **无 schema 变更、无依赖变更**：新增的 `Or`/`TS`/`Po` 只在进程内存里，
  入库行改为按显式列白名单构造（有测试盯着白名单与表定义一致），按纯 L2 执行。
  实现：worker 的 `deepcoin_reconcile` 从"固定 30 秒轮询"变成
  "固定 30 秒轮询 + 相关事件到达立刻跑一轮"，**只改什么时候跑**；
  判据、账本、结论一个字未动（等价性测试逐调用逐库比对唤醒轮与定时轮）。
  唤醒用进程内 `asyncio.Event`（两端同在 worker 进程，非跨进程锁），
  最小间隔 2 秒、间隔内合并、每分钟硬上限 20 次、超限退回纯轮询并暴露 `wake_throttled`；
  定时那一半用绝对 deadline，所以任何唤醒/去抖/节流都推不动原来的 30 秒节奏；
  `wake_signal=None` 时行为与阶段 3 之前**完全一致**。
  只让相关帧唤醒：`Trade` 任何帧、`Order` 的 `Or` 变化、`TriggerOrder` 的 `TS`/`TU` 变化、
  `Position` 的 `Po` 变化，判据全部来自阶段 2 的已知最新状态，不多查一次库；
  重复帧与乱序帧一律不唤醒。
  **补掉阶段 2 遗留 (a)**：重同步第 4 步后用二次快照把 Order/TriggerOrder/Position
  的身份与交易所时间种进乱序 tracker（只在 `complete=True` 时种，不完整快照什么都不种；
  只种身份与时间不种状态——REST 的状态词汇与 WS 短键不是一回事，翻译就是本项目要消灭的推断）。
  **补掉阶段 2 遗留 (c)**：每一次 REST 读失败现在记调用名、异常类型、HTTP 状态码
  （不记响应体、不记凭据），进 `RestSnapshot` / `ResyncOutcome` / 健康端点；
  `compare_forward_only` 改为先看 `complete` 再读集合，并加 AST 静态守护测试
  盯住四个流模块里"先读集合后看 complete"的写法。
  健康端点新增 `wakes_last_hour`、`wakes_throttled_last_hour`、`last_wake_at`、
  `last_wake_channel`、`wake_throttled`、`wake_requests_seen`、
  `reconcile_runs_last_hour{by_timer,by_wake}`、`reconcile_failures_last_hour`、
  `last_reconcile_failure`、`seeded_entity_count`、`last_resync_read_failures`。
  验证：focused 48 passed（新文件 `tests/test_deepcoin_ws_phase3.py`）；
  全量 **7552 passed / 4 skipped / 0 failed**（部署候选那次是 7551，差的 1 条是事后加的测试）；
  `tests/test_runtime_event_loop_blocking_census.py` 通过——新增的
  `_record_reconcile_failure` 是纯内存记账，按既有格式登记进允许清单并写明理由。
  **专门重启 worker 一次**：`02:28:13Z`，MainPID 2525953 → 2526485，
  重启前 `active_write_count=0`、无 planned/executing/reconciling 批次；
  重启后立刻 `tracked_entity_count=7 / seeded_entity_count=7`——
  阶段 2 的"重启后 tracker 为 0 直到新帧到达"**已消除**。
  观察窗口 `02:30:09Z ~ 02:59:11Z`（30 个连续采样点，监视器自判达标写 DONE，
  等待总时长 29 分钟，`anomaly_count=0`）：真实消息单次回看最少 5 条 / 最多 14 条、
  最多 6 个群，**首次一次性达标**；三单元 30/30 active、NRestarts 全程 0；
  `state` 恒 healthy、`open_gap_count` 恒 0、`last_resync_outcome` 恒 converged、
  `unparsed_count` 恒 1 零新增、无积压。
  **唤醒在生产上被真实成交触发**：`02:38:38Z` 一次真实成交推 3 帧
  （Trade/Order/Position）+ `02:38:39Z`、`02:38:52Z` 两帧 TriggerOrder，
  5 帧全部命中唤醒条件（`wake_requests_seen=5`），经去抖**合并成 2 次**唤醒式 reconcile；
  `by_timer` 3 → 42、`by_wake` 0 → 2，`wakes_throttled_last_hour` 全程 0、
  `wake_throttled` 全程 false，离每分钟 20 次上限极远。
  第二次唤醒比第三帧晚 13 秒，是因为第一轮还在跑——唤醒被合并而不是叠一轮，
  正是"不重入"的设计行为。
  **真基线首次取到**：窗口内有 5 条真实业务帧，`duplicate_rate_1h=0.0`、
  `out_of_order_count_1h=0`、`out_of_order_count_total=0` 是**实测值**而非阶段 2 的
  零流量地板值（样本仍只有 5 帧，不足以谈长期速率）。
  **交易所写入恰好 3 条，全部来自生产自动交易同一条信号**
  （`raw_message_id=15186` → binding 342 ETH long）：
  `open_market_position` 02:38:34Z、`set_position_tpsl` 02:38:34Z、
  `create_backup_stop` 02:38:46Z；交易所侧仓位 2→3、条件单 5→7 完全对得上，
  fingerprint `4efeed96…` → `0952faf4…`，**零条无法解释的写入**。
  账本增量（bindings 341→342、legs 587→588、protection_ledger 664→666、
  protection_legs 881→884、mutation_intents 640→642）全部属于这一笔。
  **新增 uncertain 1 条可归因**：id=360 / `raw_message_id=15186` /
  `ExecutionBoundaryOutcomeUnknown` / `in_progress` / 02:33:46Z，
  与既有 6 条同一形态（最近一条 2026-09-06 15:23:12Z，早于本次部署），
  且已收敛（同一笔 02:38:38Z 达到 `position_ownership_verified`）；
  阶段 3 根本没接入入场路径。
  **补测项 7 只有离线证据**：观察前后两次只读扫描 `non_binding_count` 都是 0——
  交易所上不存在任何不属于系统账本的订单/条件单/仓位，留给阶段 5 的受控实验，
  未为制造基线下任何手工单。离线侧已构造"存在非 binding 条件单"的场景，
  断言唤醒轮与定时轮的调用序列与整库指纹完全一致、且客户端连写方法都不存在。
  **扫描口径的一个真实教训**：第一版归属判定只查 `execution_bindings` /
  `execution_order_legs`，把 4 张系统自己下的 TPSL 止损单判成"不属于任何 binding"
  （保护单的交易所单号本来就写在保护账本里而不是 binding 行上）。扩到全部保护账本后归 0。
  在一项"不许碰别人的单"的检查里，这个方向的错误最危险，故记录在案。
  遗留：(a) **401 又复发一次**，累计第 3 次：`02:27:57Z`
  `GET /deepcoin/trade/trigger-orders-pending?instType=SWAP&instId=ETH-USDT-SWAP&limit=100`
  返回 401。它发生在**既有**的 `web_app._load_deepcoin_pending_tpsl_orders` 路径，
  不在五步重同步内也不在 reconcile 循环的异常处理内，所以本阶段新增的归因字段**覆盖不到它**；
  该路径语义正确（`evidence_available=False` 后 continue，没有降级成"没有挂单"）。
  三次都落在 `trigger-orders-pending` 同一个端点，仍无法判定是签名时间戳容差还是限流。
  (b) 窗口内**没有出现任何 incomplete 重同步读**，所以新增的归因字段没取到样本，
  阶段 2 遗留 (c) 的能力已就位但尚未被真实失败验证过。
  (c) reconcile 循环不打每轮日志，所以"唤醒比定时早多少秒"无法逐事件精确测量；
  只能说成交推送比交易所写入晚 4 秒（02:38:34 写、02:38:38 推），
  而 binding 342 的归属核验就盖在 02:38:38——与帧同一秒。窗口内实测的定时周期约 43 秒
  （30 秒等待 + 约 13 秒一轮），所以纯轮询会晚 0–43 秒。阶段 4 若要精确数字，
  需要给 reconcile 加一行每轮日志。
  (d) 节流参数无需调整：一轮 reconcile 本身约 13 秒，唤醒式轮次的天然上限就在 4–5 次/分，
  每分钟 20 次的硬上限实际上永远不会绑定；真正的串行化来自"单任务不重入"而不是节流器。
  2 秒最小间隔同理，但都建议**保留**——它们是 WS 异常放量时的兜底，不是常态调节旋钮。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-3/`
  （`deployment.md`、`observation-summary.md`、`open-observer.sh`、
  `open-observer.jsonl` 31 行、`open-observer-summary.json`、`DONE`、
  三份 `exchange-snapshot-*.json`、三份 `non-binding-*.json`、`nonbinding_scan.py`）。

- phase-2 (2026-09-06, 会话 local_cf4e65e6, **流量不足留 `in_progress`**):
  提交 `0371fc9f4fc41c588fab1534f8e33419aef4d6cf`（6 个提交），
  部署 `tg-deploy 0371fc9f…`，回滚 SHA `f555ad864855f3f6433258a581c13b04656a0fc9`。
  **无 schema 变更**：复用既有 `deepcoin_ws_events.processed_state` 列与
  `deepcoin_ws_connection_gaps` 表，本阶段按纯 L2 执行，未做 L3 副本演练；
  依赖未变（服务器 venv 已有 `websockets 17.1`，未跑 pip）。
  实现：四状态机（`connecting`/`healthy`/`disconnected`/`resyncing`，
  `healthy` 只能经收敛的重同步到达——交接链路里的 `connecting → healthy`
  被拆成 `connecting → resyncing → healthy`，因为硬性禁止 12 要求重启也重新对齐，
  这是同样四个状态名下更严格的路径，没有新增状态）；
  去重键 `(channel, payload_hash, exchange_time_ms)`，重复帧标
  `processed_state='duplicate'` 绝不删行、幂等；
  按实体身份的乱序保护，`TriggerOrder.TU` 从 posId 回 `default` 单独拦截；
  五步 REST 重同步（第 4 步二次快照保留），只用既有 `list_*` GET 方法，
  读失败记 incomplete 并阻断收敛，绝不当作"零"；
  从 `list_swap_instruments` 建的显式双向合约名映射表（生产 266 条），
  未知合约 fail-closed，另加一次强制重建重试以免新上市合约永久卡死；
  指数退避带抖动（1s 起、60s 上限）、应用层静默计时器（600s）、
  listenkey 轮换（2700s）；`ws_observation_permits_new_entry` 只暴露不接线
  （静态测试守护它只被本模块与状态机模块引用）；健康端点扩展九个字段。
  **生产实测两个新事实**（阶段 1 未记录）：
  (1) listen key 是**硬 60 分钟**而非滑动一小时——13:56:25Z 订阅的连接在
  14:56:31Z 收到 `{"code":"50118","event":"error","msg":"listen key expired,
  connection closing"}` 后断开，盲区 5.57 秒；45 分钟轮换有 15 分钟余量。
  (2) 该错误帧在阶段 1 被记成 `unparsed`，会耗尽"解码器不认识"这个信号；
  现解码为独立 `control` 频道并在收到后立即计划内重连。
  验证：focused 79 passed；全量 **7504 passed / 4 skipped / 0 failed**。
  补测项 5 用阶段 1 实验采集的 7 条真实帧（`tests/fixtures/
  deepcoin_ws_recorded_frames.jsonl`，来源
  `eth-rest-ws-tpsl-short-no-clordid-test-20260905/live-ab734b3900f6/ws-events.jsonl`），
  含真实的 `TU: default → posId` 翻转。
  补测项 4 离线覆盖成交前/成交中/成交后三个断线时点。
  观察 15:31:06Z~16:01:06Z 共 31 个采样点：状态 healthy 29 / resyncing 1 /
  connecting 1（正好抓到重启瞬间的转移）；重启 worker 一次
  （PID 2314160→2319390），缺口 2.47 秒闭合，五步耗时
  975/3/1/800/1 ms，`last_resync_outcome=converged`；
  7 条缺口行全部闭合、窗口结束 `open_gap_count=0`；
  `processed=6 / unprocessed=0 / duplicate=0` 无积压；零 error 日志；
  交易所首尾 fingerprint 完全一致 `28309102…`（**零新增写入**；
  窗口内那一个仓位是生产自动交易 15:28:19Z 自己开的，早于本次部署）。
  **未达标项：窗口内真实消息 0 条 / 0 群**（部署前 30 分钟曾有 8 条 / 5 群，
  属正常安静而非摄入故障，ingest 全程 active 且零错误）。按 AGENTS.md L2
  规则停止而不延长，阶段保持 `in_progress`，`current_phase` 不推进。
  基线 `duplicate_rate_1h=0.0`、`out_of_order_count_1h=0` 是**零流量下的地板值**，
  不是实测速率，首个有真实帧流量的窗口才会给出真基线。
  遗留：(a) 重同步第 1/4 步的 REST 快照结果没有种进进程内乱序 tracker，
  重启后 tracker 为空直到新帧到达；阶段 2 里 tracker 不驱动任何决定所以不阻塞，
  但阶段 3 让 WS 事件唤醒 REST 核验时应把快照种进去，否则重连后的旧帧拦不住。
  (b) 静默计时器在空闲账户上每 10 分钟制造一次计划内重连（生产已实测两次，
  各约 3 秒），代价是每次几个 GET；若阶段 3 觉得吵可以调阈值，但不要为了安静
  而删掉它——协议层 pong 存活不等于业务流存活。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-2/`
  （`deployment.md`、`observation-summary.md`、`observation.jsonl`、
  两份 `exchange-snapshot-window-*.json`）。

- phase-2-observation-2 (2026-09-06, 会话 local_471e9727, **第二次观察仍流量不足，
  阶段继续留 `in_progress`**):
  只读观察补做，**未改代码、未部署、未做任何交易所写入**；生产 HEAD 仍是
  `0371fc9f4fc41c588fab1534f8e33419aef4d6cf`（分支 `live`），三单元 active。
  窗口 `2026-09-06T16:56:51Z ~ 17:26:59Z`（30 分钟，31 个采样点，每分钟 1 次）。
  通过项：三单元 31/31 采样点 active、NRestarts 全程 0；ws-health `state` 31/31
  恒 `healthy`、`open_gap_count` 恒 0、`last_resync_outcome` 恒 `converged`、
  `connected` 与 `permits_new_entry` 恒 true、`instrument_map_size` 恒 266；
  `processed=6 / unprocessed=0 / duplicate=0` 无积压。
  重启 worker 一次（17:05:06Z，PID 2319390→2345403），缺口行 id=14
  `process_start` 2.738 秒闭合，17:05:26Z 已回 `healthy`，五步耗时
  953/2/1/818/1 ms，重启期间的空观测没有被写成"零"（事件表前后同为 6 行、
  仓位数前后同为 1）。窗口内另两条缺口 id=15/16 都是遗留项 (b) 的
  `silence_timeout` 计划内重连（3.36s / 3.49s），全部闭合。
  交易所首尾 fingerprint 逐字节一致
  `283091021fc8391834efb3c2b49c968fd576940d4a8d01b91c3c287a4b79d70b`
  （`complete=true, position_count=1, open_order_count=0`），**零新增写入**；
  那一个仓位仍是生产自动交易 15:28:19Z 自己开的，早于本窗口。
  **未达标项：窗口内真实消息 0 条 / 0 群**（门槛 ≥5 条、尽量 2 群；窗口内最后一条
  消息 16:42:47Z 早于起点，近 6 小时全局 34 条 / 10 群，ingest 全程 active 且窗口内
  零 error，属正常安静而非摄入故障）。按 AGENTS.md L2 规则停止而不延长。
  因此 `duplicate_rate_1h=0.0`、`out_of_order_count_1h=0`、
  `out_of_order_count_total=0`、`events_last_hour=0` **仍是零流量地板值，不是实测基线**；
  WS 事件表整窗零新增，真基线仍待首个有真实业务帧的窗口。
  `unparsed_count` 全程恒为 1、窗口内零新增——该 1 行是 14:56:31Z 的 listen key
  过期帧（阶段 2 部署前记录），这个字段是累计计数而非窗口计数，后续窗口应按
  "无新增"而非"等于 0"来判读。
  **新发现异常**：17:04:07Z 一次 Deepcoin `GET /deepcoin/trade/trigger-orders-pending`
  返回 `401 Unauthorized`，在既有 `web_app._load_deepcoin_pending_tpsl_orders`
  里抛 `DeepcoinClientError`。24 小时内仅此一次、未复发；语义正确
  （该函数置 `evidence_available=False` 并 `continue`，没有把读失败当成"没有挂单"，
  符合硬性禁止第 4 条）；发生在既有 REST 对账路径而非五步重同步内，当时
  state 恒 `healthy`。但它是阶段 3 的真实风险信号：WS 唤醒的 REST 核验必须能把
  偶发 401 记成 unknown/incomplete，绝不能降级成"零"。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-2/`
  （`observation-2.jsonl`、`observation-2-summary.md`、
  `exchange-snapshot-obs2-start.json`、`exchange-snapshot-obs2-end.json`）。

- phase-0 (2026-09-06, 设计会话): 提交 `17a662f9` 保存 codex 交接材料 25 个文件并修
  `scripts/deepcoin_*.py` 的 umask 未还原；本文件与
  `docs/plans/2026-09-06-deepcoin-rest-ws/` 下 7 份设计文件为第二个提交。
  验证：focused 71 passed；全量 7427 passed / 4 skipped / 1 failed，
  唯一失败 `tests/test_server_update_scripts.py::test_deployment_docs_keep_both_workstation_helpers_visible`
  在本会话之前即已存在（断言 AGENTS.md 含 `-Action stage`，而 `408e68c4` 已把
  AGENTS.md 改写为 tg-deploy 路径），与本会话改动无交集。
  遗留：上文第 2 条的止盈收敛全局否决缺陷；该失败测试与 AGENTS.md 的不一致。

- phase-1 (2026-09-06, 会话 local_3d228d16): 提交 `f555ad864855f3f6433258a581c13b04656a0fc9`。
  worker 内新增常驻私有 WebSocket 采集：`websockets>=16.0` 依赖、
  `DeepcoinRestClient.acquire_listen_key()`（复用 `_request` 与既有签名）、
  新表 `deepcoin_ws_events`（原样落帧，`payload_hash` 不加唯一约束）与
  `deepcoin_ws_connection_gaps`（硬性禁止第 12 条的缺口记录）、
  新模块 `deepcoin_private_ws.py`（asyncio 客户端、固定 5 秒重连、不可解析帧记
  `channel='unparsed'` 绝不丢帧、只读 `OS`/`TU`/`PI`/`I` 短键不做长键兜底）、
  worker 单例任务 + deployment-identity 观察位 + shutdown 序列、
  localhost-only 只读端点 `GET /api/runtime/deepcoin-ws-health`。
  缺口记录选用独立小表而非往事件表插行：事件表是阶段 2 要逐行去重解码的原始帧收件箱，
  生命周期行没有 `raw_payload`/`payload_hash`，混在一起会逼所有后续读取方反复过滤。
  验证：schema 演练在生产库副本上跑 `init_db`，`quick_check` 前后均 `ok`，
  五张关键表行数完全不变（339/660/183/197/15125），表数 88→90，
  516 个既有 sqlite_master 对象逐个比对零变更；
  focused 31 passed；全量 7459 passed / 4 skipped / **0 failed**
  （阶段文件提到的既有失败 `test_deployment_docs_keep_both_workstation_helpers_visible`
  已由 `d3a6a850` 修复，本次全量已无失败）。
  部署 `tg-deploy f555ad86…`，回滚 SHA `61c3ed43a4dca1db9d71bbdda42c91ec37c42e48`
  （回滚保留新表不删）；生产 venv `websockets 17.1`。
  观察 13:45:56Z~14:16:01Z 共 31 个采样点：`connected` 恒为 true、任务恒存活、
  `unparsed_count=0`、既有任务零 error 零 traceback。
  事件数 0，按阶段文件记为**流量不足**（窗口内账户 0 仓位 0 挂单），未延长窗口。
  重启 worker 一次（PID 2280349→2284211）任务重新拉起并在 1 秒内重新订阅。
  缺口表两行均为 `process_start` 且均已闭合（1.16s / 0.58s），窗口内无非计划断线。
  交易所零新增写入：前后 fingerprint 完全一致 `e0f66201…`。
  证据：`/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-1/`
  （`schema-rehearsal.md`、`observation-summary.md`、`observation.jsonl`）。
  遗留：生产 `deployment-identity` 的 `loaded_artifact_verified=false`、
  全部 capability 标志为 false——因 2026-09-06 退役不可变发布流程后
  `TELEGRAM_KOL_RELEASE_COMMIT` / `_MANIFEST_SHA256` 两个环境变量不再设置，
  属本阶段之前既有状态，与本次改动无关；阶段 1 文件的前置条件
  “返回 `loaded_artifact_verified=true`”已过时，后续阶段文件应改用
  “worker 各 loop 存活且 authority_evidence 新鲜”作为前置判据。
