# Pending trigger protection intent 181–183 只读追查

核查时间：2026-09-05 04:13–04:20 UTC。生产 worker：`9501a5f39f0c5f196cc29f24f3e3b8786267126b`，PID `1525316`，`loaded_artifact_verified=true`，未冻结。

## 结论与当前保护

**不是已证实的 shadow 导致 pending 永不推进，也不是 binding 339 的活仓对应 intent 183。对象关联混淆是本次疑点的关键。**

- binding 339 的活仓 `1001125135694798` 属于 **market entry leg 583**：`active / verified`。
- intent 183 属于同一 binding 的另一条 **trigger_limit entry leg 584**：`pending / unassigned / pos_id=NULL`。保护腿 865–869 也全部属于 584，不属于 583。
- adoption 按 **entry leg** 而非整个 binding 选择 owner。584 不满足 active、有 pos_id、verified 三个必要条件，所以未进入实际归属评估；没有拒绝原因码被写入。
- worker 8002 GET 在 04:16:52Z 返回的持仓面板显示：583 的真实仓位有已验证止损 `1001125135694875`，触发价 **77500**，覆盖全部剩余 **6 contracts / 0.006 BTC**。该活仓不是无主止损。
- 但保护并非完整：面板仅显示上述一张止损；本地没有 339 的备用止损或止盈订单行。另有 incident **388**，属于 **583**，`backup_stop_blocked / primary_stop_missing_on_exchange`，创建于 `03:06:54.785970Z`，`delivery_status=pending`、`notified_at=NULL`。这证明另一路备用保护异常曾被记录，**不证明当前主止损缺失**，也不是 intent 183 的阻塞原因。本轮未扩展追查它的根因或补挂。

`retry_attempts=0` 不是“从未被任何循环扫描”的通用证明：成功的 intent 179/180 也都是 0。对于 181–183，本次能够确证的是其归属 owner 选择条件不成立、没有持久化 adoption/retry/refusal 进展。

## 1. 精确驱动链与前置条件

以下源码与生产 release 对应文件的 SHA-256 已逐一比对一致：execution_bindings、web_app、trading_settings、trigger_protection_rescue_worker、strategy_management_planner、production_safety_monitor。引用行号对应这些已核对文件。

1. [web_app.py:370](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/web_app.py:370)：`deepcoin_reconcile` 是 worker singleton task；[5042](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/web_app.py:5042) 启动该后台任务，与 lifecycle_monitor 分开。
2. [web_app.py:4855](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/web_app.py:4855)：默认启动延迟 **5 秒**，轮询间隔 **30 秒**。`run_deepcoin_execution_reconcile_loop` 在 [9469](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/web_app.py:9469) 创建读取客户端、检查 `list_open_orders` 能力、在 management worker 线程调用 reconciler。每轮工作完成后再 sleep，所以不是严格每 30 秒执行一次；会受远端读取及串行执行队列耗时影响。
3. [execution_bindings.py:413](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:413)：取得 position authority lock、加载一致快照，先运行 rescue tick，再 `_apply_reconcile_snapshot`。正常生产调用还可能运行备用止损/管理收敛，因此**本次没有手动调用此函数**，包括名字带 read_only 但仍会更新本地状态的 reconciler。
4. [execution_bindings.py:815](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:815)：读取有效 rollout 设置，加载 Deepcoin entry legs。快照错误分支位于 [894](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:894)，会提前处理不可用证据；完整快照下先更新入场归属，再于 [1118](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:1118) 调用 `_adopt_verified_trigger_entry_protection`。
5. [execution_bindings.py:1468](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:1468) → [2156](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2156)：读取全部 Deepcoin saved intents，构造 owner 集合，再取 `pending/retrying` 与到期者。不是仅由新 Telegram 消息触发，也没有“intent 创建超过 N 分钟才首次尝试”的条件。
6. [execution_bindings.py:2104](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2104)：owner 必须是 entry、trigger_limit、active、非空 pos_id，且每条 leg 恰好一个 intent、attribution_status=verified、intent/binding/leg 身份一致。

另一个正常调用入口是 [strategy_management_worker.py:463](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_worker.py:463)：允许执行且 contract spec provider 存在时，在止盈收敛之前再次调用 binding reconciler。它不绕过上述 owner 条件。

运行证据不仅是 lifecycle_monitor 日志：worker identity 的 `authority_evidence.reconcile_cycle` 显示 `successful=true / fresh=true / age_seconds=51.294397`；management cycle 同样成功且新鲜。03:00–04:20Z 的限定 worker journal 检索未见 `Deepcoin execution reconcile` 错误。成功循环不会为每个被 owner 筛选跳过的 intent 写日志。

## 2. intent 183 选择条件逐项对账

| 条件 | 183 / leg 584 的字段 | 判定 |
| --- | --- | --- |
| venue=deepcoin；entry leg 已加载 | deepcoin，execution_binding_id=339 | 满足 |
| purpose=entry、order_kind=trigger_limit | entry / trigger_limit | 满足 |
| leg.status=active | **pending** | **不满足；最早筛选处跳过** |
| leg.pos_id 非空 | **NULL** | **不满足** |
| 同 leg 恰好一个 intent、binding 一致 | intent 183 → leg 584 → binding 339 | 满足 |
| leg.attribution_status=verified | **unassigned** | **不满足** |
| intent.recovery_state ∈ pending/retrying | pending | 满足 |
| next_attempt_at 到期 | NULL | 满足，NULL 就是立即到期 |
| settings 是否关闭整个现有路径 | auto_trade_enabled=true；liveness_v2=live | 没有关闭 |

`next_attempt_at=NULL` 的确切语义见 [execution_bindings.py:2585](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2585)。本次三条 intent 均为 `retry_attempts=0 / next_attempt_at=NULL / adopted_order_id=NULL / last_reason_code=NULL / recovery_disposition=NULL`。

binding 339 自身虽为 `active / position_ownership_verified`，其 pos_id 来自 **583**，不能继承给 584。worker GET 还实际显示 584 的父单 `1001125135694951` 是 **Conditional、开多、78290、8 张**。因此本次不仅有本地未归属字段，也有该父触发单仍待触发的现场证据。

五条 planned 保护腿：865 primary_stop 77500；866 backup_stop；867/868/869 take_profit 80200/81000/81700，均 `execution_order_leg_id=584 / pos_id=NULL / exchange_order_id=NULL`。此处 planned 是未成交触发腿的持久化计划，不等于 market 活仓的保护已失败。

## 3. shadow 是否阻断

现场设置为 `trigger_protection_lineage_attribution_mode=shadow`、`position_management_liveness_v2_mode=live`、lineage watermark=NULL。按 [trading_settings.py:210](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trading_settings.py:210)，有效模式确为 shadow；watermark 缺失只会阻止新 lineage 的 live 授权，不会关闭旧归属路径。

- [execution_bindings.py:2382](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2382) 记录 shadow 建议。
- [2391](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2391) 起，全局新血缘方案在 `lineage_evidence_required && !lineage_authority` 时跳过真实 adoption/refusal 写入。这是预期的“新方案只观察”。
- **没有因此 return 或把该 leg 标记 globally_processed**。后续 [2499](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2499) 起仍遍历未处理 eligible intents，执行旧 `plan_trigger_protection_intent_adoption`；成功则 finalize，拒绝/延迟则记录并安排重试（2540–2581）。

所以 shadow 会使“只有新血缘能证明”的保护继续受旧 fail-closed 规则约束，但不能据此推导所有 pending intent 停止推进。**181–183 在 owner 构造阶段就被排除，与 shadow/live/disabled 尚未发生关系。** 当前证据不支持该三条由 shadow 开关引入推进缺陷。

所有者提供切换时点 `2026-09-04T20:33:43Z`；本次独立验证了当前值和代码分支，未将当前 settings 行当成该历史切换时点的审计证明。

## 4. 最后成功时间线

时间均为 UTC，区分 reconciler 的观察时间和 intent 持久化更新时间。

| 时间 | 证据 |
| --- | --- |
| 09-04 03:49:26.193502 / .514543 | binding 337 创建 intent 179/180，分别属于 legs 579/580 |
| 09-04 06:31:25.894684 / 06:31:26.217280 | binding 338 创建 intent 181/182 |
| 09-04 08:05:33.416347 → 08:05:40.956670 | ledger 652 首次验证 leg 579 止损 `1001125123045252`；intent 179 更新为 adopted |
| 09-04 12:36:58.418660 → 12:37:02.071996 | ledger 657 首次验证 leg 580 止损 `1001125126414221`；intent 180 更新为 adopted |
| 09-04 20:33:43 | 所有者提供的 shadow 切换时点 |
| 09-05 03:06:47.732682 | market leg 583 创建，后来持有当前 pos；它不是 trigger protection intent 183 的 owner |
| 09-05 03:06:48.656754 / .666367 / .823699 | trigger leg 584 创建；intent 183 创建；父单身份记录后最后更新 |
| 09-05 04:17:43.399386 | 本次数据库对账：最新 adopted 行仍是 intent 180；其后 adopted 行数量为 **0** |

两个成功 ledger 的 evidence_source 均为 `reconciliation_trigger_protection_intent`。不能把最后一次成功早于 shadow 切换的时间相关性当成因果证据；后面的三条 owner 资格并不相同。

## 5. binding 338 对照及额外救援队列风险

181 → leg 581，182 → leg 582，均为 `trigger_limit / pending / unassigned / pos_id=NULL`；binding 338 为 `open / entry_order_pending / pos_id=NULL`。**与 183 在归属选择器上的跳过原因相同**。未填充实际 owner 的保护 intent 可以长期保持预置 pending；其存在时长本身不是归属恢复失败证明。

worker GET 返回父单 `1001125122023458`（leg 581）仍为 Conditional，开空 76410、24 张。第二个父单 `1001125122023573` 在本次两次有界面板 GET 中未显示；**其当前交易所最终状态为未确认**，不把未显示当成取消或成交。182 在本地选择器上被跳过的事实不受这一外部证据缺口影响；是否另有父单状态投影滞后不在此处冒认结论。

额外发现：这是与 adoption 不同的 **rescue 通道**。

- [trigger_protection_rescue_worker.py:40](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_protection_rescue_worker.py:40) 默认 `limit=20`；[65](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/trigger_protection_rescue_worker.py:65) 选择 pending/retrying/failed、disposition 为 NULL/retry/exact_backup、到期的 intent，按 ID 升序固定取前 20。
- 现场按同条件只读查询得到 **91** 条，181/182/183 分别排 **89/90/91**，均不在实际批次中。实现没有轮转游标；前排记录持续不变时存在后排饥饿风险。
- 即使进入救援批次，[strategy_management_planner.py:2579](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/strategy_management_planner.py:2579) 仍要求该 **leg** 为可管理已验证活仓、精确 pos_id 等，否则返回 `rescue_position_not_verified`。该原因是按当前字段的静态判断，**不是这三条已记录的执行拒绝码**。
- 不应拿这个额外队列风险替代 183 当前的 owner 不符合条件主因；常规 adoption 扫描并没有前 20 条的限制。

## 6. 超时与告警

1. **没有以 intent.created_at 计算的、覆盖该种“pending + next_attempt_at=NULL + 未进入 owner 集合”的通用超时推进。** 正常 `_schedule_trigger_intent_retry` 是在实际延期/拒绝后才调用：最多 5 次，按 5/10/20/40…分钟退避（上限 60），见 [execution_bindings.py:2596](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2596)。本次筛选跳过不调用它。
2. 终结收敛器 [execution_bindings.py:1652](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:1652) 仅处理 `retrying / wait / snapshot_incomplete` 且已验证终态 leg；不覆盖这三条原始 pending。
3. protection 拒绝通知由 [execution_bindings.py:2652](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/execution_bindings.py:2652) 在**出现拒绝**时生成审计/事故；owner 筛选静默 continue 没有生成拒绝。这三条没有对应 protection adoption audit 或 trigger-intent runtime incident。
4. **pending 超期监控确有覆盖空缺**：[production_safety_monitor.py:3325](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/production_safety_monitor.py:3325) 的 overdue 分支明确要求 `next_attempt_at IS NOT NULL AND next_attempt_at <= now`，另有 manual_review/terminal/submit_unknown/recovery_required 分支。这三条均不符合，且没有 created_at 年龄兜底。
5. 不能扩大成“系统任何监控都不会发现任何相关问题”：lifecycle_monitor 另有 pending entry 到期人工复核（[lifecycle_monitor.py:790](/Users/steven/Documents/telegram获取消息/src/telegram_kol_research/lifecycle_monitor.py:790)），它不等价于 intent 恢复活性监控；market leg 583 的 incident 388 也确实存在，但尚未送达。其未投递原因本次未定论，更未手动发送它。

**最终判断：**本次三个 pending intent 的归属跳过符合“触发入场腿须先成为精确已验证活仓”的安全前提；不能据此认定 shadow 使归属失活。另证实未调度 pending 的年龄监控空缺及 rescue 固定前 20 条风险，但均未修复、未手动触发。

## 证据与只读边界

服务端证据目录：`/var/lib/telegram-kol-cutover-evidence/9501a5f39f0c5f196cc29f24f3e3b8786267126b/pending-intent-readonly-20260905`。

- `database-evidence.json`：SHA-256 `dcf2ea9d3e6e4fe89f5174a9ec30512e8e79cb1006915950252b28366aa79459`。
- `adoption-ledgers.json`、`worker-identity.json`、`rescue-selection.json`、`worker-reconcile-journal.json`。
- `worker-positions-panel.html`、`worker-open-orders.html`：仅通过 worker `127.0.0.1:8002` GET 获取，不把渲染面板未列出某单当成完整终态证据。

所有直接 SQLite 查询均以 `mode=ro` 打开并强制 `PRAGMA query_only=ON`（实测 1）。仅使用 stdlib 诊断脚本和源码读取；远程 Python 均 `-B`。未调用 reconciliation/adoption/rescue/补挂入口，未改设置、代码、schema 或业务数据，未部署或重启，未做交易所写操作。仅新增本报告和隔离证据文件；未提交推送。
