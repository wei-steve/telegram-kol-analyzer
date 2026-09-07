# 阶段 5a：`list_open_orders` 逐调用点分析

`list_open_orders` 从未文档化的 V1 `/deepcoin/trade/orders-pending` 切到官方 V2
`/deepcoin/trade/v2/orders-pending`（`index` 从 1 开始，`limit` 上限 100，按页翻到不足一页为止）。

**为什么这张表存在。** V1 对活着的普通限价单恒返回空（阶段 5 会话实测），生产至今也没有普通限价挂单
（限价腿走 trigger-order），所以下面每一个调用点**从来没有真的看见过一行普通挂单**。切到 V2 之后它们
第一次会看见，其中带撤单能力的路径会开始撤它以前看不见的单。这是交易所写入后果，必须逐条判定。

## 判定口径

- **用途**：只读展示 / 判定（读出来做分支，但本调用不写交易所）/ 触发写入（该读的结果直接决定撤单或改单）。
- **对当前生产已有对象可能产生写入**：以 2026-09-07 生产实况为准——账户当前**没有**普通挂单
  （部署前只读核实见状态文件证据区）。所以"可能"指的是本次改动**打开了**这条路径，而不是眼下就会触发。
- **护栏**：`src/telegram_kol_research/open_order_action_guard.py`。只允许对 `execution_order_legs` 里
  `venue=deepcoin`、`order_kind ∈ {market, limit}`（即经 `POST /trade/order` 提交的普通单）且 ordId /
  clOrdId 由本系统记录的对象动作；其他行一律记日志 + 写 `runtime_incidents`（`open_order_guard_blocked`，
  severity `high`）后从动作集合里丢掉，不撤、不改。

### 一条刻意的例外：确认读不加护栏

撤单后的"回读确认"（`cancel_revision_entry_leg` 的 remaining、`cancel_pending_entry_legs` 的
`remaining_by_leg`）**故意不接护栏**。那两处问的是"我们的单还在不在"，如果在那里丢行，一个外来对象的存在
就会被读成"撤单已确认"，把未知伪装成成功。它们本来就只按本系统的 ordId/clOrdId 取行，护栏在那里没有增益，
只有风险。

## 生产源码里的 16 处 `list_open_orders(`

其中 3 处是定义，13 处是真实调用。

| # | 位置 | 调用方 / 角色 | 用途 | 切 V2 后行为变化 | 可能对现有对象写入 | 本阶段护栏 |
|---|---|---|---|---|---|---|
| 1 | `deepcoin_client.py:178` | `DeepcoinTradingClientProtocol` | 协议定义 | 无 | 否 | 不适用 |
| 2 | `deepcoin_client.py:498` | `DeepcoinRestClient` | 实现 | **本阶段改造点**：V2 + 分页 + 失败即抛 + SWAP 客户端过滤 | 否（GET） | 不适用 |
| 3 | `deepcoin_readonly.py:32` | `DeepcoinReadOnlyClient` | 只读协议定义 | 无 | 否 | 不适用 |
| 4 | `deepcoin_execution_actions.py:1158` `cancel_entry_order` | worker（`worker_command_jobs`） | **触发写入**：无 trigger 单时取普通挂单并逐条 `cancel_order` | 以前恒空 ⇒ 走"无挂单可撤"；现在能取到行，撤单路径首次可达 | **是** | **已加**：`guard_regular_open_orders(action="cancel_entry_order")` |
| 5 | `deepcoin_execution_actions.py:1311` `cancel_revision_entry_leg` | worker | **触发写入**：定位待撤的改单腿，随后 `cancel_order` | 同上；且以前 trigger 与 regular 只能命中 trigger | **是** | **已加**：仅当该 leg 的 `order_kind` 是普通单才读 regular 列表 |
| 6 | `deepcoin_execution_actions.py:1347` `cancel_revision_entry_leg` | worker | 判定（撤后回读 remaining） | 首次能看见残留普通单 ⇒ 撤单未生效时能正确报错 | 否 | **刻意不加**（见上文例外） |
| 7 | `deepcoin_execution_actions.py:1458` `cancel_pending_entry_legs` | worker | **触发写入**：可见性判定驱动逐腿撤单循环 | 以前普通腿恒不可见 ⇒ 走 absent 分支；现在可见 ⇒ 进撤单循环 | **是** | **已加**：`guard_regular_open_orders(action="cancel_pending_entry_legs")`，读顺序保持 trigger 在先 |
| 8 | `deepcoin_execution_actions.py:1627` `cancel_pending_entry_legs` | worker | 判定（撤后回读 remaining） | 同 #6 | 否 | **刻意不加**（见上文例外） |
| 9 | `strategy_management_executor.py:4191` `_match_exact_deferred_exchange_orders` | worker（管理批次） | **触发写入**：把延后入场腿精确匹配到一张挂单，随后按 `cancel_type` 撤 | 以前 regular 侧恒空 ⇒ 只能匹配 trigger；现在 regular 侧有行 | **是** | **已加**：`cancel_type == "regular"` 的行只允许被 `order_kind` 为普通单的 leg 认领；读本身保持无条件（该读在既有测试里有顺序与副作用语义） |
| 10 | `terminal_entry_cleanup.py:345` `_entry_order_is_still_visible` | worker | 判定（异常兜底时问"单还在不在"） | 以前普通单恒"不可见"；现在能看见 ⇒ 兜底更准 | 否（本调用只读；真正撤单在 #7，已受护栏） | 不加（判定读，且 `except → None` 已把读失败当未知） |
| 11 | `entry_revision_executor.py:159` `_read_exact_order` | worker | 判定（非 trigger 腿的精确状态） | 以前普通腿只能落到 history 或 `missing`；现在能落到 `pending` | 否（只分类） | 不加 |
| 12 | `instruction_execution_reconciliation.py:460` | worker | 只读证据快照 | 快照多出普通挂单行 | 否 | 不加 |
| 13 | `deepcoin_maintenance_evidence.py:126` | worker（维护证据） | 只读证据 | `regular` 桶首次可能非空 | 否 | 不加 |
| 14 | `runtime_agent_exchange_snapshot.py:132` | worker（`/api/runtime-agent/read-only-exchange-snapshot`） | 只读指纹 | `open_order_count` 首次可能非零；指纹随之变化 | 否 | 不加 |
| 15 | `deepcoin_readonly.py:66` `_load_all_open_orders` | web（只读页面） | 只读展示 | 展示层首次出现普通挂单 | 否 | 不加 |
| 16 | `deepcoin_ws_resync.py:304` | worker（WS 重同步） | 判定（重建 REST 快照） | 快照多出普通挂单；`_read` 的 `except` 已把失败记为 `RestReadFailure` 而非零 | 否 | 不加 |

## 另有 6 处按方法名的间接调用

不出现在 `list_open_orders(` 的 grep 里，但运行时会调到，一并判定。

| 位置 | 调用方 / 角色 | 用途 | 切 V2 后行为变化 | 可能对现有对象写入 |
|---|---|---|---|---|
| `web_app.py:1783` `_safe_deepcoin_list` | web | 只读展示（持仓页快照） | 首次出现普通挂单行；`_safe_deepcoin_list` 的错误态把读失败记为 unavailable | 否 |
| `web_app.py:2110` `_safe_deepcoin_list` | web | 只读展示（open-orders 标签页） | 同上；读失败 ⇒ `snapshot["error"]="unavailable"`，不降级为空 | 否 |
| `web_app.py:9576` `hasattr(client, "list_open_orders")` | worker（`deepcoin_reconcile` 循环） | 能力探测（不调用） | 无 | 否 |
| `worker_command_executor.py:56` / `:399` | worker（`sync_deepcoin_execution`） | 只读方法白名单 + 能力探测 | 无（白名单里的名字不变） | 否 |
| `execution_bindings.py:552` `_read_snapshot_rows` | worker（`reconcile_deepcoin_execution_bindings`） | 判定（对账快照） | `open_orders` 桶首次可能非空；读失败进 `snapshot.errors`，不当作零 | 否（对账写的是本地账本，不写交易所） |
| `worker_command_reconciliation.py:213` `conservative_reader` | 运维工具 | 只读可用性探测（恒返回 `complete: False`） | 无 | 否 |

`execution_bindings.py` 本阶段**未改动**（A 线会话正在该文件上作业）。

## 汇总

- 可能对交易所已有对象产生写入的调用点：**4 个**（#4、#5、#7、#9），全部已加护栏。
- 刻意不加护栏的确认读：**2 个**（#6、#8），理由见上。
- 其余 13 个（含间接调用）是只读或纯判定，切 V2 只是让它们第一次看见真实数据。
