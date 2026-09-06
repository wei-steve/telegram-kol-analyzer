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
current_phase: 2
current_phase_file: docs/plans/2026-09-06-deepcoin-rest-ws/phase-2-dedup-and-resync.md
phase_status: planned             # planned | claimed | in_progress | completed | blocked
claimed_by: null
last_completed_phase: 1
last_completed_commit: f555ad864855f3f6433258a581c13b04656a0fc9
user_approval_required_for: [1, 2, 5, 6]   # 见"用户批准门"
```

## 阶段总览

| 阶段 | 名称 | 风险等级 | 是否改交易所写入语义 | 需用户单独批准 |
|---|---|---|---|---|
| 1 | worker 内私有 WebSocket 采集，只落原始事件 | **L3**（新表 + 新依赖） | 否 | 是（schema） |
| 2 | 去重、乱序保护、心跳、断线状态机与 REST 重同步 | **L2**（若加列则该提交按 L3 处理） | 否 | 是（若加列） |
| 3 | WebSocket 事件只唤醒现有 REST reconciliation | **L2** | 否 | 否 |
| 4 | 影子构建绑定链并与现有保护账本逐笔比对 | **L3**（新影子表）；行为面 L1 | 否 | 是（schema） |
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

- identity-note (2026-09-06, 指挥会话核实): 生产 `deployment-identity` 的 `loaded_artifact_verified=false` 与 capabilities 全 false 是门禁退役后 `/etc/telegram-kol-worker.env` 不再设置 `TELEGRAM_KOL_RELEASE_COMMIT` / `_MANIFEST_SHA256` 的结构性结果。代码核实：这些标志的唯一消费者是已退役的 `scoped_release_activation.py` 和已停用的 monitor 命令，不门控任何交易路径。各阶段文件的前置判据已改为“worker 各 loop 存活 + authority_evidence 新鲜”。可选后续：让 tg-deploy 写入 release commit 让身份端点恢复有意义。
- phase-1-approval (2026-09-06, 用户在指挥会话 local_858790fe 明确批准): 阶段 1（新表 `deepcoin_ws_events` + `websockets` 依赖，L3）获批领取。同轮用户告知已自行处理掉交易所上三张 2026-09-03 的历史条件入场单，当前无挂单；阶段 3/4 的比对基线不再需要为它们建模。用户同时提出硬性要求第 12 条（断线/重启后重新对齐）。
- defect-out-of-scope (2026-09-06): `trigger_take_profit_convergence_executor.py:506-511` 对未过滤的 pending 原始行逐行调用只适用于保护单的 `_native_tpsl_aliases_consistent`，入场条件单会触发 `convergence_pending_alias_conflict` 全局否决。不属于本项目范围，需单独立项：先写复现测试，再把否决范围收窄到保护单行。

执行会话在此追加，格式：`- phase-N (日期, 会话ID): 提交 SHA；做了什么；验证结果；遗留问题`。

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
