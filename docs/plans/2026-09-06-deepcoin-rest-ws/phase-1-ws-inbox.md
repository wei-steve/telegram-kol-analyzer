# 阶段 1：worker 内私有 WebSocket 采集，只持久化原始事件

风险等级：**L3**（新增数据库表 + 新增运行时依赖）。行为面是纯附加的只读采集，
不取得任何交易权威，不做任何交易所写入。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

**领取前必须取得用户对本阶段的单独批准**（schema 变更）。

## 目标

在 `worker` 进程里建立一条常驻的 Deepcoin 私有 WebSocket 连接，订阅
`Order`、`Trade`、`Position`、`TriggerOrder` 四个 table，把收到的**每一帧原始
payload**落进新的数据库收件箱表。此外什么都不做。

这一阶段结束时，系统应当能回答"过去 24 小时交易所推了哪些事件"，
但系统的任何决定都还不依赖这些事件。

## 前置

- 上一阶段：无（本项目第一个执行阶段）。
- 读完 `docs/rest-ws-trading-status.md` 的"阶段 0 只读核对结论"第 3、5、6、7 条。
- 确认生产 worker 正常：`GET http://127.0.0.1:8002/api/runtime/deployment-identity`
  返回的 worker 各 loop 存活且 `authority_evidence` 新鲜、successful（`loaded_artifact_verified` 自 2026-09-06 门禁退役后结构性为 false，不再作为判据）。
- 准备好生产数据库副本用于 schema 演练（L3 要求）。

## 任务

### 1. 加依赖

`pyproject.toml` 的 `dependencies` 增加 `websockets`。版本以实验已验证的 16.0 为
下限参考，但不要钉死小版本。部署后必须核实生产 venv（python 3.12）确实装上了，
仅本机装上不算。

### 2. 客户端补 listenkey 能力

在 `src/telegram_kol_research/deepcoin_client.py` 加：

```text
DeepcoinRestClient.acquire_listen_key() -> str
```

- 路径 `GET /deepcoin/listenkey/acquire`。
- **复用现有 `_request` 与 `build_deepcoin_auth_headers`**，不要照抄实验脚本里的
  `_signed_get_json` 另写一份签名逻辑。
- 响应形如 `{"code":"0","data":{...}}` 或 `data` 为单元素列表；取 `data.listenkey`。
  两种形状都要处理，缺字段抛 `DeepcoinClientError`。
- 同时把 `acquire_listen_key` 加进 `DeepcoinTradingClientProtocol`。
- **listenkey 是凭据。** 不得写进日志、异常信息、证据文件或数据库。
  连接 URL 含 listenkey，因此 URL 本身也不得记录。

listenkey 有效期按官方文档为滑动一小时。本阶段只需在连接建立前获取一次，
续期与重连留到阶段 2；但要在代码里留出续期钩子的位置并写注释说明它属于阶段 2。

### 3. 新表 `deepcoin_ws_events`

加进 `src/telegram_kol_research/models.py`，随 `Base.metadata.create_all` 建表
（`db.py:852 init_db`）。列至少包含：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK autoincrement | |
| `venue` | String(64) not null default `deepcoin` | |
| `channel` | String(32) not null | WS 的 `table` 值：Order/Trade/Position/TriggerOrder |
| `action` | String(64) not null | WS 的 `action` 值：PushOrder / PushTrade / PushPosition / PushTriggerOrder |
| `order_sys_id` | String(255) nullable index | 短键 `OS`，缺失存 NULL |
| `trade_unit_id` | String(255) nullable index | 短键 `TU`，缺失存 NULL；值可能是字面量 `default` |
| `position_id` | String(255) nullable index | 短键 `PI`（Position 频道），缺失存 NULL |
| `instrument_raw` | String(64) nullable | WS 原样合约名，如 `ETHUSDT`，**不归一化** |
| `exchange_time_ms` | BigInteger nullable | 从 `U`/`UM`/`IT` 里取，取不到存 NULL |
| `received_at` | DateTime not null | 本地接收时间 |
| `received_ms` | BigInteger not null | 本地接收毫秒，用于同秒内排序 |
| `raw_payload` | Text not null | 整帧 JSON 原文 |
| `payload_hash` | String(64) not null index | `raw_payload` 的 sha256 |
| `processed_state` | String(32) not null default `unprocessed` | 本阶段恒为 `unprocessed` |
| `created_at` | DateTime not null | |

索引：`(channel, received_ms)`、`(order_sys_id)`、`(trade_unit_id)`、
`(position_id)`、`(payload_hash)`。

**本阶段 `payload_hash` 上不要建唯一约束。** 真实推送里同一条 TPSL 会被重复推送
且内容可能完全相同（实验中 `TU=default` 与 `TU=posId` 两帧内容不同，但不能假定
所有重复帧都不同）。去重是阶段 2 的事，本阶段必须**原样保留每一帧**，
否则会丢掉判断重复率的原始数据。

`processed_state` 这一列本阶段不用，但现在就建好，避免阶段 2 再做一次 L3 加列。

### 4. 新模块 `deepcoin_private_ws.py`

只做三件事：连接、订阅、落库。

- 用 `websockets.asyncio.client.connect`，**不要**用实验脚本里的
  `websockets.sync.client`（阻塞式，会卡住 worker 的事件循环）。
- URL `wss://stream.deepcoin.com/v1/private?listenKey=<key>`。
- 连接成功后立刻发 `{"action":"subscribe","tables":["Order","Trade","Position","TriggerOrder"]}`。
- 每收到一帧：解析 `payload.result[]`，对每个 `{table, data}` 写一行；
  **`raw_payload` 存的是整帧原文，不是拆出来的单条**，这样帧边界信息不丢。
  一帧含 N 条就写 N 行，N 行共享同一个 `raw_payload` 与 `payload_hash`。
- 解析失败、`result` 不是列表、`data` 不是 dict：仍然落一行，
  `channel` 记 `unparsed`，`raw_payload` 记原文。**永远不要丢帧。**
- 短键取值：`OS` → `order_sys_id`，`TU` → `trade_unit_id`，`PI` → `position_id`。
  取不到就是 NULL，**不要**去别的键里猜，也不要用长键名兜底。
- 本阶段的断线处理只有一条：连接断开就记录一条状态并按固定间隔重连
  （建议 5 秒，指数退避留给阶段 2）。**断线不得产生任何"零"或"无"的结论。**

### 5. 挂到 worker 单例任务表

- `RUNTIME_ROLE_SINGLETON_TASKS["worker"]`（`web_app.py:361`）加
  `"deepcoin_private_ws"`。
- 启动照 `deepcoin_reconcile` 的写法（`web_app.py:5001-5024`）：
  `runtime_role_starts_singleton_task(...)` 判定 + `asyncio.create_task`，
  句柄存 `app.state.deepcoin_private_ws_task`。
- 加进 `web_app.py:427` 的 `deployment-identity` tasks 字典，
  这样 `/api/runtime/deployment-identity` 能观察到它是否存活。
- 关停加进 `web_app.py:5418` 附近的 shutdown 序列：先 cancel、
  再 await、再置 None，和现有任务一致。
- **不要**挂 ingest 或 web。

### 6. 一个只读观察端点

`GET /api/runtime/deepcoin-ws-health`，localhost only（照
`/api/runtime-agent/read-only-exchange-snapshot` 的 `client_host` 判定写法）。
只返回计数与时间，**不返回任何 payload 内容**：

```json
{"connected": true, "last_event_at": "...", "events_last_hour": 0,
 "counts_by_channel": {"Order": 0, "Trade": 0, "Position": 0, "TriggerOrder": 0},
 "unparsed_count": 0}
```

## 禁止

- 禁止在 WS 回调里做任何交易所写入、账本写入、保护判断或仓位归属。
- 禁止让任何既有代码路径读 `deepcoin_ws_events`。本阶段这张表是只写的。
- 禁止把 listenkey 或含 listenkey 的 URL 写进日志、异常、证据文件或数据库。
- 禁止对帧做去重、过滤、合并或归一化。合约名原样存 `ETHUSDT`。
- 禁止把断线解释为"没有订单"或"没有仓位"。
- 禁止用 `git add -A`。
- 禁止在本阶段引入模式开关（`inline`/`shadow` 一类）做灰度。

## 验证等级与具体检查项

等级 **L3**。

### schema（L3 强制）

- [ ] 在**生产数据库副本**上跑一次 `init_db`，确认新表建成、既有表未变更。
- [ ] 副本上 `PRAGMA quick_check` 通过。
- [ ] 记录 `execution_bindings`、`position_protection_ledger`、
      `trigger_protection_intents`、`position_take_profit_orders`、`raw_messages`
      这五张关键表的 before/after 行数，必须完全相同。
- [ ] 写明回滚方案：本阶段回滚 = `tg-deploy <pre-deploy-sha>` + 新表保留不删
      （空表无害，删表才有风险）。回滚 SHA 必须在部署前记录。

### 测试

- [ ] focused：新模块的解析、落库、异常帧、断线重连间隔。
- [ ] 最终候选跑一次全量套件。注意：当前 HEAD 上
      `tests/test_server_update_scripts.py::test_deployment_docs_keep_both_workstation_helpers_visible`
      已存在失败（断言 AGENTS.md 含 `-Action stage`），与本阶段无关，
      报告时按"已知既有失败"记录，不要顺手改 AGENTS.md 去迎合它。
- [ ] 依赖核实：部署后在服务器上执行
      `/opt/telegram-kol-analyzer/.venv/bin/python -c "import websockets; print(websockets.__version__)"`。

### 生产观察（L3 按 L2 的观察强度执行）

- [ ] 部署后观察 30 分钟。
- [ ] `deepcoin_ws_events` 至少收到 1 条事件；若 30 分钟内交易所零活动导致零事件，
      记录为"流量不足"，**不要**延长观察窗口，改为核实连接状态端点
      `connected=true` 且无 `unparsed` 行。
- [ ] `unparsed_count = 0`；若非零，dump 那几行的 `raw_payload` 到服务器证据文件并
      在汇报里说明。
- [ ] 重启 worker 一次，确认任务重新拉起且 `deployment-identity` 里能看到它。
      （本阶段的核心主张之一就是进程生命周期，所以这次重启是必须的。）
- [ ] 确认 `deepcoin_reconcile`、`message_processing_worker`、
      `worker_command_worker` 三个既有任务在观察窗口内没有新增异常。
- [ ] 确认交易所侧零新增写入：观察前后各取一次
      `GET /api/runtime-agent/read-only-exchange-snapshot`，`fingerprint` 的任何变化
      都必须能用生产自身的正常交易解释，不能由本阶段产生。

## 完成条件

1. 上面全部检查项通过或已记录为"流量不足"。
2. 提交已推到 `origin/codex/deepcoin-auto-trading-v1` 并用 `tg-deploy <40位sha>` 部署。
3. 更新 `docs/rest-ws-trading-status.md`：`phase_status=completed`、
   `last_completed_phase=1`、`last_completed_commit=<sha>`、
   `current_phase=2`、`current_phase_file=docs/plans/2026-09-06-deepcoin-rest-ws/phase-2-dedup-and-resync.md`、
   `phase_status` 回 `planned`、`claimed_by=null`，并在证据区追加一行。
4. 发消息给 `brain_session_id`。

## 汇报格式

```text
阶段 1 完成 / 阻塞
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>
schema 演练：副本路径、quick_check 结果、五张关键表 before/after 行数
依赖：生产 venv websockets 版本
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟）
事件计数：Order/Trade/Position/TriggerOrder/unparsed
重启验证：任务是否重新拉起
交易所写入：零（前后 fingerprint 对比结论）
异常与遗留：
证据路径（服务器）：
```
