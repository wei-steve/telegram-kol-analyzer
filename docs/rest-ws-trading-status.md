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
current_phase: 5
current_phase_file: docs/plans/2026-09-06-deepcoin-rest-ws/phase-5-order-entry-cutover.md
phase_status: in_progress             # planned | claimed | in_progress | completed | blocked
                                      # 阶段 5 本体续做：迁移代码在分支 rest-ws/phase-5-order-entry
                                      # （94dc4632，17 提交，未部署未合并），须先 rebase 到
                                      # codex/deepcoin-auto-trading-v1 最新（已含 5a + 5b）再继续；
                                      # 任务 1/3/4 未完成。阶段 5 改交易所写入语义，需用户单独批准。
claimed_by:
last_completed_phase: 5b
last_completed_commit: 230ba1cc8097d30ed89c860608467f17680a14ca
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
  V2 已只读调通（`index` 从 1 开始，`index=0` 被拒），支持 `ordId` 过滤。
  cell v 的探测集已扩为 V1 四种参数 + V2 三种（含按精确 ordId 过滤），
  待用 `--cell v --attempt 2` 在一笔活的限价单上取得定论。

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
