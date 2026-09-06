# leg 583 止盈未挂出：只读诊断

核查窗口：2026-09-05 05:27–05:38 UTC。生产 worker 为 `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，PID 1525316，`loaded_artifact_verified=true`。

## 优先结论：现在会不会自动挂止盈

**有持久化自动收敛路径，没有错过一次性挂单时机；但当前路径被就绪门禁阻断，不能依赖“再等一会儿就会自动挂出”。**

- leg 583 / binding 339 / pos `1001125135694798` 对应 **trigger_take_profit_convergence 227**。
- 227 创建于 `03:06:46.971124Z`，保存三档目标：80200（50%）、81000（30%）、81700（20%）。
- `status=waiting_backup_stop`、`reason_code=convergence_waiting_backup_stop`、`pos_id=NULL`；`reserved_at/completed_at/request_json/response_json/error_json` 均 NULL。
- 本轮先后读到 `updated_at=05:27:36.328330Z`、`05:31:59.357969Z`、`05:33:46.195013Z`，仍是上述状态。不是任务根本没有建立，也不是 retry 次数耗尽。
- worker GET `/strategy-records/1088` 在 `05:28:39Z` 返回：仓位 6 张 BTC long，实际主止损 77500、订单 `1001125135694875`、sz=0（全部剩余仓位）、已验证归属；实际止盈为空，第二止损未设置。
- 数据库对 583 没有 `position_take_profit_orders`，没有 TP mutation 或 TP submit event。入场后的 `set_position_tpsl` 仅含 SL。
- 返回前 `05:38:24Z` 复核：227 仍为 waiting_backup_stop，最后更新 `05:38:16.931887Z`；TP 记录仍为 0。新一次 worker GET 仍显示 6 张、77500 主止损、无止盈、无备用止损（`end-check.json`、`worker-1088-end.html`）。

### 不能越过的证据边界

已经定位到**确切就绪门禁及持久化原因码**，但尚不能从现存证据区分该轮走的是：

1. `execution_bindings.py:1362` 的精确活仓匹配失败分支；还是
2. `execution_bindings.py:1391` 的 owned-stop fingerprint 未通过分支。

两个分支都写同一个 `convergence_waiting_backup_stop`，且均不绑定 convergence.pos_id。GET 面板投影不是该轮 reconciler 原始输入，也不暴露所有 position/TPSL alias 字段；不能用面板的“已验证”反推两个执行检查都通过。**没有证据可把原因进一步确定为某个方向 alias、数量零值、pos mode 或其它原始字段。**

主止损的 order ID 持续出现在本轮完整 pending 快照记录（例如 observation 1301757，05:30:19.462834Z，complete=1），所以不能将原因码字面解释为“交易所根本没有主止损”。本轮没有通过新接口、手动执行 planner 或补挂来补齐缺失的执行输入证据。

## 1. 主止损与止盈是不同路径

以下行号以生产 SHA 为准。除 recovery_live_submit.py 外，所列核心本地文件与生产逐文件 SHA-256 一致；该文件因几何门禁新增代码而移动了行号，已用生产 commit 内容核对，未沿用旧行号。

### 市价入场的主止损

生产 `recovery_live_submit.py:1421–1444` 在市价入场获得 pos_id 后构建保护请求，明确 `include_take_profit=False`（1425），随后通过 exact-position mutation gateway 提交并要求 readback。

本地对应逻辑：[recovery_live_submit.py:1380](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/recovery_live_submit.py:1380)；生产精确路径为 `/opt/telegram-kol-releases/9501a5f39f0c5f196cc29f24f3e3b8786267126b/src/telegram_kol_research/recovery_live_submit.py:1421`。

本笔证据：

- event 3980：`open_market_position / submitted`；6 张 BTC long。
- event 3981：`set_position_tpsl / submitted / entry_protection`；请求 `slTriggerPx=77500 / slOrdPx=-1`，**无 tpTriggerPx**；响应 code=0、ordId=`1001125135694875`。
- mutation 635：confirmed；ledger 659：`stop_loss / verified / entry_protection_response`。

所以主止损成功不代表同一请求也提交过止盈。

### 止盈是持久化延迟任务

1. 生产 `recovery_live_submit.py:1583` 调用 `_record_market_take_profit_convergences`（定义 2340）；按已记录 market leg 和精确 pos_id 创建任务。583 的计划落在 227，不是另一条 pending trigger leg 584 的任务 226。
2. [trigger_take_profit_convergence.py:20](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence.py:20) 为每个自动 entry leg 保存一份不可变 TP 计划，初始 waiting_position。
3. [execution_bindings.py:1289](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:1289) 周期性读取 waiting_position、waiting_backup_stop、ready，以及有限白名单的 conflicted 任务。核对 entry leg、当前唯一活仓（合约、posId、方向、split 模式、正数量）和 verified owned stop。
4. [trigger_take_profit_convergence_executor.py:951](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:951) 生成止损证据 fingerprint；其 [915](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:915) 明确是**主止损或备用止损任一来源通过即可**，不是强制要求第二张止损。主止损检查同时支持当前数量和 sz=0（[1044](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:1044)）。
5. 成功后绑定 convergence.pos_id 并标 ready（[trigger_take_profit_convergence.py:77](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence.py:77)）。227 尚未到此处。
6. [strategy_management_worker.py:463](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_worker.py:463) 在允许执行时先 reconcile，再执行 ready TP lane。该 worker 默认每轮结束后等待 5 秒（[953](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_worker.py:953)）；另有默认 30 秒间隔的 deepcoin reconcile。间隔不包括网络和串行任务耗时。
7. [trigger_take_profit_convergence_executor.py:364](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:364) 只取 **status=ready** 的任务，默认每批 5 条。有效 liveness=disabled 不执行；shadow 只规划；live 才执行。本轮 `position_management_liveness_v2_mode=live`、auto_trade_enabled=true；独立 lineage=shadow 不关闭此 TP lane。
8. [同文件:456](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:456) 再做精确归属、保护、活仓、快照 alias、contract spec、分档数量等预检；[137](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_take_profit_convergence_executor.py:137) 执行阶段先 reserved，再逐单 exact_position_write_gate、提交 TP-only 请求、readback，成功后写专用 TP 记录和账本。

**时间结论：**waiting_backup_stop 没有“创建太久就不再选择”的条件，227 仍可在未来真实证据满足时自动 ready。该条件目前不满足；不能承诺解除时间，也不能保证后续执行门禁一定通过。本轮没有执行任何这些带写行为的函数，包括名称叫 plan 但会修改任务状态的函数。

## 2. backup_stop_blocked 388 与止盈的关系

incident 388 属于 583，创建 `03:06:54.785970Z`：`backup_stop_blocked`，evidence.reason_code=`primary_stop_missing_on_exchange`；本轮仍 pending、无 notified_at。

- [trigger_backup_stop_executor.py:410](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_backup_stop_executor.py:410) 读 pending 后调用 `_pending_matches_primary`；返回 false 时，在 [424](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_backup_stop_executor.py:424) 产生该原因码，**尚未提交备用止损**。
- [同文件:717](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_backup_stop_executor.py:717) 核对唯一订单、可解析数量、alias 一致性、合约/方向/posId/触发价/数量；并不是单纯查有没有同号订单。未通过可由多种子条件导致。
- incident 388 仅保存汇总原因，没有保存当次失败的原始 pending 行及失败字段。无法将其历史原因为何与当前主止损显示结果进行精确逐字段重放。
- 备用止损入口会周期性重新评估 eligible market/trigger legs，不是 incident 388 创建一次后就永不执行（[同文件:60](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_backup_stop_executor.py:60)、[280](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_backup_stop_executor.py:280)）。事件存在不等于已写入备用单。

两条路径均依赖主止损重新验证，并使用相近的 native TPSL matcher，因此可能存在共同的验证问题；但 **388 并不是 TP 必须先成功的硬依赖**，TP 可只凭 verified primary stop 放行。现有证据不足以证明二者是同一个原始字段问题；不能把“备用没挂 → 止盈必然没挂”当作已证明因果链。

## 3. 修正判据后的精确全历史统计

### 关联口径还需修正

修正 purpose 漏匹配是必要的，但止盈记录必须按 **venue、binding_id、entry_leg_id、pos_id 四项全部一致**关联，不能只按 binding。

| 目标腿 | 实际有止盈记录的腿 | 证据 |
| --- | --- | --- |
| 561 / binding 325 / pos 1001125110903466 | **560** / pos 1001125090990141 | TP 177–179；verified TP ledger 608/609，属于不同 pos |
| 580 / binding 337 / pos 1001125126414222 | **579** / pos 1001125123045253 | TP 195–197；verified TP ledger 655/656，属于不同 pos |
| 583 / binding 339 / pos 1001125135694798 | 无 | 仅 ledger 659 stop_loss |

所以现场不支持“561/580 已证实是误报”；它们在**精确腿层面**仍无命中止盈记录。不能据此把整个系统问题限定为只有 583 一条，也不能把这两条旧腿的 active 投影当作当前真实活仓。

### 分母与指标

本次在同一只读事务内统计（05:32:17Z）：Deepcoin entry legs、非空 pos_id，且当前 attribution_status=verified 或存在 new_state=verified 的归属 audit；不限定当前 leg/binding 为 active，避免剔除已结束交易。取截至当前仍可从持久化证据证明的“曾归属”样本 **235** 条。仅使用当前 attribution_status=verified 的敏感性口径为 234 条。

分子按本轮授权判据：精确关联的 ledger.status=verified 且 purpose ∈ {take_profit, combined, supervised_current_tpsl}，**或**精确关联的 position_take_profit_orders 存在记录。专用 TP 记录包括后来取消/完成的历史记录，不限 active。按腿去重。

| 指标 | 条数 | 占比 |
| --- | ---: | ---: |
| 命中授权止盈记录口径 | 139 | **59.15%** |
| 无上述 TP 记录，但有 verified stop_loss | 35 | 14.89% |
| 两者均未命中 | 61 | 25.96% |
| 总计 | 235 | 100% |

当前 verified 的另一口径为 139/234=59.40%。按订单类型：market 68/95=71.58%；trigger_limit 71/140=50.71%。这些是观察性分组，不等价于类型的因果效应。

### 按 entry leg 创建日期分布（UTC）

| 创建时间 | 曾归属腿 | 有 TP 记录 | 比例 | 只有 verified SL | 两者均未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-07 | 126 | 64 | 50.79% | 17 | 45 |
| 2026-08 | 90 | 62 | 68.89% | 15 | 13 |
| 09-01 | 1 | 1 | 100% | 0 | 0 |
| 09-02 | 7 | 4 | 57.14% | 1 | 2 |
| 09-03 | 8 | 7 | 87.50% | 0 | 1 |
| 09-04 | 2 | 1 | 50% | 1 | 0 |
| 09-05 | 1 | 0 | 0% | 1 | 0 |

“只有止损”并非 09-04/05 才出现，7、8 月均有样本。09-04/05 合计只有 3 条，且较新的任务可能未走完，不能据此认定某个精确部署/开关时刻之后发生普遍退化。

**统计限制：**这不是完整交易所全历史的“最终成功率”。当前账本状态可变；combined/supervised_current_tpsl 是用户指定的宽口径，有些证据并不明确区分 SL/TP；没有记录不等于从未存在人工或未纳管订单。曾归属后丢失 pos_id、且不可由本次分母还原的样本未涵盖。表格保留“无 TP 且无 verified SL”类别，未把这些样本误算成只有止损，也未声称所有样本都具备同等目标/交易机会。

## 4. 三姐 raw 14382：从未挂出，还是挂了改不动

重新查询确认：lifecycle 1050 / binding 324 / leg 559 / pos 1001125090080799：

- convergence **202** 计划 TP **78970**，waiting_backup_stop；pos_id、request/response、reserved/completed 均 NULL。
- TP 保护腿 **763** 为 protection_recovery_pending、exchange_order_id=NULL。
- binding 324 的 protection ledger、专用 TP orders、mutation intents 均无记录；execution_events 只有原始 create_trigger_entry，无 TP 提交或原投诉消息的部分平仓/保护替换。
- intent **163** 的原生保护归属失败：`trigger_protection_candidate_predates_fill / failed / manual_review / adopted_order_id=NULL`。
- batch **152** 是 `partial_then_break_even`，不是单纯“修改预挂 TP”；即时 `protection_missing_cancellable_order_id`，最终 `protection_visibility_retry_expired`，未生成管理腿。

因此，**系统计划的预挂止盈从未进入提交阶段；不是已挂出的 TP 改不动。** 同时，KOL 后来的“止盈 50% 并移保本”要求的是组合管理动作，该部分平仓也在保护预检阶段被拒，不能将它与预挂 TP 混为一个动作。

但是“前者必与 583 同源”的推论不成立：

- 三姐有明确的原生主止损归属拒绝，系统拿不到可撤换的已验证保护 ID。
- 583 已有精确账本主止损，阻塞发生在后续就绪/重新验证路径，底层失败字段未完全查明。

**同属保护前提不通过而阻断后续 TP/管理的失败类别；不能判定为相同底层根因。** 历史人工交易所订单的绝对“从未有过”不在本次证据能力内；这里的“从未挂出”明确指该系统保存的入场止盈计划没有提交证据。

## 未决点与只读边界

尚未确证：227 当前循环具体失败的原始字段；388 当时 native matcher 失败的子条件；两者是否同一个字段问题。现有 worker GET 投影和持久化原因码不能区分上述分支，本轮没有新增诊断接口或执行生产 planner 来绕过边界。

只读 SQLite：`mode=ro`、`PRAGMA query_only=ON`（实测 1）；统计使用同一读事务。交易所读取仅经 worker 8002 GET。远端脚本均 python -B，使用 stdlib，未从 release 导入可写业务代码。未改代码/设置/schema/业务数据，未补挂/部署/重启，未做交易所写操作。

仅新增本地报告及服务端隔离证据，未提交推送。证据目录：`/var/lib/telegram-kol-cutover-evidence/9501a5f39f0c5f196cc29f24f3e3b8786267126b/leg583-tp-readonly-20260905`。

- `worker-1088.html`：worker GET 当前持仓投影。
- `target-evidence.json`：SHA-256 `23fc83faf75384cb541bcaa11c6916c3cf9eb32993cafe8f1028edf15d3ba142`。
- `history-stats.json`：逐腿标记与聚合统计。
- `leg583-observations.json`：唯一既有持仓观察，不能冒认为当前 reconciler 完整输入。
- `source-sha256.json`：生产核心源码指纹。
