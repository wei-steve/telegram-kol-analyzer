# 227 / leg 583 与 waiting_backup_stop 队列只读诊断

核查窗口：2026-09-05 06:22:59–06:26:35 UTC。只读诊断，未补挂、未调用执行规划器、未修改生产配置或业务数据、未部署或重启。使用 bug-hunt / systematic-debugging 的逐分支取证方法；未执行技能中的修复步骤。

## 优先结论：当前 80200 止盈不能依赖自动任务兜底

**227 仍会被重查，但本轮不能证明它会自行恢复，也不能证明它依赖一个已经永久失败的前置。当前没有止盈订单；如所有者需要立即具备 80200 止盈，不能把 waiting 任务当成执行承诺。** 本轮没有代为补挂。

截至 06:26:35，worker GET 证据仍显示唯一活仓为 pos `1001125135694798`，BTC long 6 张，只有主止损 `1001125135694875` / 77500；无备用止损、无止盈。任务 227 的 `updated_at=06:26:26.673525`，说明持续重查，不是已经提交。

**无法在既定只读接口和已有持久化证据内，进一步判定这一轮究竟停在“精确活仓匹配”还是“主止损重新验证”。** 两个分支都写相同 status/reason，不保存输入或子判据。不能把 Web 投影里的“已验证”当作该门禁已经通过，也不能把 `primary_stop_missing_on_exchange` 翻译成“交易所确实没有止损”。这是本轮确认的可观测性缺口，而不是已定位到某个原始字段的根因结论。

另一个核心纠正：**`waiting_backup_stop` 不等于必须先挂备用止损。主止损或备用止损任一通过精确验证，止盈即可 ready。** 115 条混合了未取得仓位、历史仓位已不存在、保护验证失败等不同状态；不是 115 个当前活仓都卡在备用止损下单。

## 1. 227 就绪门禁逐项对账

生产 worker：`9501a5f39f0c5f196cc29f24f3e3b8786267126b`，PID `1525316`，`loaded_artifact_verified=true`。下列六个核心模块已用 SHA-256 确认本地源码与该 release 一致，因此下列源码行号可直接复核；没有从 release import 模块。

入口：[execution_bindings.py:1289](../src/telegram_kol_research/execution_bindings.py#L1289)。

| 顺序 / 判据 | 583 / 227 的实际证据 | 能否判定 |
| --- | --- | --- |
| convergence 属于待处理状态 | 227=`waiting_backup_stop`，reason=`convergence_waiting_backup_stop` | 满足状态选择 |
| 能在本轮 legs 中找到腿；没有未完成 child fill | DB 有 583，`purpose=entry, order_kind=market`；updated_at 持续变化 | 静态对象存在；未保存该轮 legs/child 列表，不能伪造完整输入复盘；该 continue 分支本身不更新时间 |
| binding 存在 | 339=`active, BTC, long, split, cross` | 满足本地存在性；binding 的 split 不等于交易所原始 mrgPosition 已核实 |
| leg.pos_id 非空；row.pos_id 没有冲突 | leg=`1001125135694798`，convergence.pos_id=NULL | 满足；NULL 在此明确允许 |
| 活仓 instId、posId、posSide 精确对应 | GET 页面投影为 BTC / long / 上述 pos，6 张；独立 count GET=`complete=true,position_count=1` | 投影支持存在唯一目标；未返回同一轮原始对象，不能替代逐字段验证 |
| 原始 `mrgPosition or posMode` 必须为 `split` | DB position_mode=split；页面无上述原始字段 | **缺原始证据** |
| 原始 `pos or size` 为正数且所有条件组合匹配数恰为 1 | 页面数量为 6；未保存门禁原始字段和组合匹配数 | **缺同轮证据** |
| 精确 binding+leg+pos 的 verified stop_loss/combined 账本 | ledger 659，verified/stop_loss，order=`1001125135694875`，trigger=`77500.0`，size_text=NULL | 账本查询有结果；NULL size 不是该函数直接拒绝条件 |
| 主止损精确回读或备用止损精确回读至少一个成立 | 当前页面主 SL order ID/77500/sz=0；backup 表无记录 | 备用分支无凭据；**主分支的原始 aliases、normalize/match 结果未落库，不能确定具体失败子项** |

两个不可区分的落库点：

- `execution_bindings.py:1362`：binding/pos 缺失或 `len(position_matches)!=1` → waiting，同一 reason，刷新 updated_at，然后 continue。**这一分支失败时，止损验证根本不会执行**，不能说“两条都失败”。
- `execution_bindings.py:1391`：`stop_evidence_fingerprint is None` → 同样 waiting/reason/updated_at。

`trigger_take_profit_convergence_executor.py:915` 明确为 native primary **OR** exact backup；`:1044` 对 primary 使用现仓数量和 `Decimal(0)` 分别匹配，所以 **sz=0 的全仓主止损本身不是充分的拒绝原因**。主止损账本有 order ID、价格，不需要账本 size_text 非空。候选 alias 一致性、原生 TPSL 规范化、精确订单 ID 唯一性、方向、价格、数量及可用仓位引用仍须通过；本轮没有放宽或绕过它们。

### 1.1 缺少什么，怎样才能区分（仅记录需求，不实施）

227 的 request_json/response_json/error_json/reserved_at/completed_at 均 NULL。583 的 position reconciliation observation 只有 129：observed_at=`03:06:46.502952`，size=6，pending_tpsl_json=[]，snapshot_complete=1。这不是 06:26 本轮的原始输入，更不能拿它证明当前 SL 不存在。

`execution_bindings.py:1203` 的观察投影只按 pending 单自身 posId/pos_id/closePosId 分组；没有这类字段的、靠已知订单账本关联的 SL 不进入对应 pending 列表。投影还不保留所有原始 alias 字段。worker 的 `/strategy-records/1088` 输出也是展示投影；count GET 不包含原始 position/TPSL 对象。这里存在“页面能显示已有保护，但就绪门禁失败无法还原”的证据断层。

为精确区分，需要在**真实循环的原有判定位置**关联保存：convergence/leg ID、版本、时间、快照指纹与完整性；position 匹配数及 instId/posId/posSide/mrgPosition/posMode/pos/size 的白名单原值；命中的 ledger IDs；目标 SL 原始订单身份/方向/类型/数量/SL 价格 aliases；失败子门禁、expected/actual、后续门禁是否未执行。保留首次失败及证据变化，避免重复噪声和凭据。这是所需证据清单，不是本轮新增日志或代码的授权。

## 2. 能否自动通过、需要什么变化

当前 `auto_trade_enabled=true`、`position_management_liveness_v2_mode=live`。`trigger_protection_lineage_attribution_mode=shadow` 是另一模式，**没有在 227 的 ready 选择条件中作为禁止条件**。583 是 market 腿；同 binding 下 trigger 腿 584 / intent 183 不是它的前置依赖。

自动路径仍存在：

1. 一轮完整 reconciliation 获得符合上述字段的唯一活仓。
2. 已知主 SL 精确回读成功，**或者**已有合格备用 SL 精确回读成功；生成 stop fingerprint，写入 convergence.pos_id，转 ready。
3. TP runner 只选 ready，重新执行 exact leg/ownership、完整快照、保护、数量分配等执行前门禁，之后才可能提交并回读止盈。

来源：`execution_bindings.py:1130,1396`；`trigger_take_profit_convergence_executor.py:364,456`。管理 worker 在 `strategy_management_worker.py:463` 先 reconciliation，`:473` 再跑 TP lane；`execution_bindings.py:448` 另尝试备用 SL，成功后重新取快照并再次判 ready（`:454`）。常规 reconciliation 循环默认每轮工作结束后等待 30 秒；管理 worker 默认等待 5 秒，均不是严格执行 SLA。

waiting 没有尝试次数耗尽或按创建时间终结的门禁；backup 的 blocked incident 也不是终结标记，eligible 腿后续仍会规划。故不能根据旧 incident.updated_at 推断“只试过一次”或“永久停止”。如果原始快照/验证结果不变，重查会继续产生同一 waiting，**时间流逝本身不会修复它**。

目前无法证明需要变化的是哪一个原始字段，也没有证据证明其会自然变化。因此结论是“自动路径仍在，但自行恢复未获证明”，不是“已经错过一次性时机”，也不是已证实“永远不会自己好”。

## 3. 115 条 waiting：分母、时间与现仓分层

SQL 分析快照为 06:24:11 UTC；每条关联精确 execution_order_leg_id，不把同 binding 另一条腿的 TP 算过来。全历史 convergence 共 227，waiting 115（50.66%）是**当前状态占比**，不是当前活仓保护失败率。

| 创建月份 | 全部 convergence | waiting | waiting 中没有 leg.pos_id |
| --- | ---: | ---: | ---: |
| 2026-07（最早 07-22） | 79 | 51 | 31 |
| 2026-08 | 120 | 49 | 34 |
| 2026-09（截至 09-05） | 28 | 15 | 9 |
| 合计 | 227 | 115 | 74 |

09 月 waiting：09-02=3，09-03=7，09-04=3，09-05=2。服务端 summary.json 保留完整逐日分布。早至 07-22 已存在，不能解释成 09-04 shadow 切换后才出现的统一退化；当前幸存状态也不能作为各月新增失败率。

| 当前分层 | 数量 | 含义 |
| --- | ---: | --- |
| 无 posId，腿已 cancelled/exchange_cancelled/manually_closed，binding closed | 67 | 当前没有可用于该门禁的精确仓位身份；不等于 67 个裸仓 |
| 无 posId，pending/unassigned，binding open/active | 7 | 尚未取得可管理仓位；其中含 584，不能和 583 混用 |
| 有历史 posId，腿 terminal，binding closed | 38 | 当前完整仓位清单中不存在，不应自动为历史任务补挂 |
| DB active/verified，但当前不在交易所 | 2 | leg 561、580；本地 active 不能充当 live 证明 |
| DB active/verified 且当前交易所存在 | 1 | 227 / leg 583 / binding 339 |

合计：74 无 posId、40 历史 posId 当前不在交易所、1 当前活仓。105 条 binding closed；3 条 active/verified；7 条 pending/unassigned。按**当前**仓位条件前 114 条都无法满足精确活仓门禁，但本轮不能反推它们历史第一次进入 waiting 的原因。

115 条均无专用 position_take_profit_orders 行；其中 6 条仍命中宽口径 verified TP-purpose 账本（任务 5、15、16、20、26、29；16/26 是 take_profit，其余 supervised_current_tpsl）。所以旧口径 139/235 与本次 115/227 的对象、状态和证据语义不同，不能当成互补数或同一根因的证明。另有 8 条 waiting 有备用止损订单历史记录，也反对“waiting 就是备用从未挂出”。

### 3.1 备用止损事件分组：这是历史记录，不伪装成当前失败子门禁

为每个 waiting 对应 leg，按 `(created_at,id)` 取最新一条 backup_stop* incident。不同原因会生成不同指纹，但相同原因复发不更新时间；因此“最新事件”不一定是最后一次实际评估结果。85 条没有该类 incident，不能臆造 backup 根因。

| 最近持久化 reason_code | 任务数 | 可支持的解释 |
| --- | ---: | --- |
| 无 backup incident | 85 | 缺少该路径的拒绝证据；其中 74 条没有 posId |
| primary_stop_not_verified | 15 | 当时未取得合格主止损账本/价格 |
| primary_stop_missing_on_exchange | 1 | 227；当时主止损精确回读未通过，具体子项未保存 |
| live_position_not_unique | 2 | 当时精确活仓非唯一/未命中 |
| live_position_snapshot_unavailable | 3 | 活仓读取不可用，不等于没有仓位 |
| backup_stop_missing_on_exchange | 2 | 已有 backup 的回读问题，不是必然从未提交 |
| backup_stop_readback_unavailable | 1 | backup 回读不可用 |
| backup_close_in_progress | 1 | 平仓互斥前置 |
| backup_position_mutation_in_progress | 3 | 仓位变更互斥前置 |
| backup_management_in_progress | 1 | 管理执行互斥前置 |
| contract_spec_unavailable | 1 | 合约规格不可用 |
| 合计 | 115 | **多类情况，不是单一根因** |

这些腿共有 53 条 backup 类 incidents，本次读回 delivery_status 全为 pending。不能据此断言所有其他告警通道都未投递；当前 388 至少没有旧通道投递成功记录。waiting 就绪分支本身只写同一原因与时间，无分支级告警证据、无等待年龄截止；已有事件去重也隐藏重复失败次数。

## 4. 备用止损路径与事件 388

入口 `execution_bindings.py:448` → [trigger_backup_stop_executor.py:60](../src/telegram_kol_research/trigger_backup_stop_executor.py#L60)。前提为调用者持有账户 authority lock、有 contract spec provider；liveness disabled 不执行，shadow 只记计划，live 才可提交。本轮模式为 live。

`:280` 选择 open/active binding 的 active、verified、非 manual_bind 入场腿，必须有 posId 和 authoritative persisted ownership。`:310` 规划依次检查仓位写互斥、已有 backup、主 SL verified 账本及价格、主 SL order ID、规格、原始活仓 alias 和唯一性、完整 pending TPSL，最后验证主 SL 确实仍在交易所。之后仍有无主保护冲突、几何、数量、幂等和 exact-position 写门禁，不能跳过。

**388 精确记录：** binding 339 / leg 583 / pos `1001125135694798`；incident_type=`backup_stop_blocked`；evidence_json=`{"reason_code":"primary_stop_missing_on_exchange"}`；created_at=updated_at=`2026-09-05 03:06:54.785970`；delivery_status=pending；notified_at=NULL；delivery_error=NULL。

此码唯一对应 `trigger_backup_stop_executor.py:417–424` 的 `_pending_matches_primary(...) == False`。该次规划已走过主账本/ID、规格、live-position 唯一匹配及 pending 读取；**当时失败发生在主 SL 回读层，不是前面的活仓唯一性检查**。但它不是 TP readiness 同一原始快照，更不能据此保证今天 readiness 的活仓分支通过。

`:717` 的布尔失败可能包括：alias 冲突过滤、不是原生 TPSL、精确 order ID 数量不为 1、size 缺失、native matcher 的仓位/方向/价格/数量验证失败。388 只保留最终 reason，未保留其中哪一项。`:581` 同指纹事件存在就 return，既不更新 last_seen，也不保存失败次数。

583 主 SL 的归属证据是 ledger 659 的 `evidence_source=entry_protection_response` 和 `match=exchange_returned_order_id_exact_readback`；不是匿名附带 SL 等待认领。主止损提交成功与后续两个执行器使用的严格重新验证是不同阶段，因此“主 SL 成功、backup/TP 未执行”可以同时成立。

## 5. 是否都是缺确定性血缘的下游；修归属能否解开 115 条

结合[会话 3 的链接调研](2026-09-05-deepcoin-api-deterministic-link-research.md)：公开父触发→子单→仓位→自动附带 SL 链没有找到完整确定性接口，不能凭相似形状消除身份门禁。本轮没有重新调用 Deepcoin 直接 API。

但它**不等于所有原生 SL 都没有身份**：系统自己向已知 posId 发出 set-position-sltp，保存成功返回 ordId，再精确回读，可以建立本地确定性账本。583 属于已知主 SL order ID 的 market 路径。它在 backup 规划中失败的是后续回读，不是“没有父子订单引用所以找不到匿名 SL”。因此**不能把 583 归为同一个已证实血缘缺失根因**；共同依赖保护验证，不代表相同失败子项。

115 条中确有归属拒绝子集：6 条关联 intent 的 last_reason_code=`trigger_protection_candidate_predates_fill`，任务/腿分别为 145/499、152/509、157/514、180/537、202/559、220/577；均 failed/manual_review。其中 202/559 即三姐 binding 324 的路径。另有 18 条对应 intent 已 adopted 仍 waiting，74 条 intent pending、11 条 failed 但 last_reason_code=NULL、4 条 resolved/terminal、2 条无 intent。

15 条 primary_stop_not_verified 也不能全部等同 predates：其中包含已 adopted 后没有合格主账本、旧 failed 原因缺失、terminal resolved 等情况。更不能把最新事件受 mutex 阻断的 157 只按 predates 一类覆盖掉。

**修保护归属不能宣称连带解开全部 115 条。** 已不存在仓位的历史记录不应因修改归属而补挂；未成交腿尚无目标仓位；互斥、规格、回读、未保存子判据各有边界。对于仍有精确活仓、确实仅缺归属证据的任务，归属成功只能解除一个必要前置，后续 fresh readback/ownership 等仍须通过。上述 6 个旧故障当前均不是活仓，不能据此承诺可恢复交易。

## 6. 30 条 conflicted 与 completed 82 的真实含义

| 状态原因 | 数量 | 对账 |
| --- | ---: | --- |
| convergence_partial_position_unexplained | 28 | **全部已有专用 TP 订单记录**，不是“止盈从未挂出” |
| convergence_exact_leg_not_verified | 2 | 任务 21 / leg 360、22 / leg 361；无专用 TP 行 |

28 条的代码分支在 [position_take_profit_orders.py:372](../src/telegram_kol_research/position_take_profit_orders.py#L372)：任务已经 submitted、有归属一致的 TP 行，`live_size < sum(TP.size_text)` 且没有 TP row.status=filled，才设置该 reason。表示缩仓未被本路径的止盈成交解释；**不是证明发生非法缩仓，也没有记录足够数据区分人工平仓、其他管理动作或成交回读延迟**。error_json 全 NULL，没有把失败时的 live_size/planned_size 一并存入 convergence。本轮不补写这些数值。

21/22 的 convergence.pos_id 与当前 leg.pos_id 一致，但当前腿均 manually_closed；updated_at 同为 `2026-07-26 22:35:46.189327`，request/response/error 均 NULL。代码可在 row/leg pos 冲突或执行前 exact leg 条件不满足时产生该 reason；**不能拿今天的已关闭状态倒推出当时具体是哪一项失败**。

30 条所指 pos 均不在当前唯一活仓清单。上述两种 reason 不在 `execution_bindings.py:1330` 的自动重新验证 conflicted 白名单中，因此不能把它们当作和 waiting 一样持续尝试的任务。

completed 82 同样不是“82 次止盈成功成交”：70=`convergence_position_terminal`，1=`convergence_position_terminal_prior_authority_restored`，2=`convergence_submit_rejected_position_terminal`，9=`parent_trigger_cancelled_before_entry`。尤其 9 条入场前取消和 2 条提交被拒后终结，不能记为成功挂 TP。当前收敛函数对 terminal 的处理主要面向 submitted 或指定 rejected 路径（position_take_profit_orders.py:300），不能自动覆盖所有历史 waiting，解释了终态 binding 仍残留 waiting 的统计污染。

## 7. 可复核证据与边界

所有生产 SQLite 连接均 `file:...research.db?mode=ro`，执行 `PRAGMA query_only=ON`（读回 1）；汇总查询使用显式 read transaction。本轮交易所证据只取 worker 8002 的 GET：deployment-identity、read-only-exchange-snapshot、strategy-records/1088。count 和页面是相邻请求，不冒充同一原子快照；前后 count 均 complete=true、position_count=1。没有调用任何 POST、规划器或 reconcile 函数；所有远端 Python 都带 `-B`。

服务端隔离证据目录：

```text
/var/lib/telegram-kol-cutover-evidence/9501a5f39f0c5f196cc29f24f3e3b8786267126b/tp-waiting-cohort-readonly-20260905/
```

`cohort.json` 保留 227 条任务的精确腿关联及 backup incidents/intents/ledger/TP 行；`summary.json` 保留状态/逐日分布；`target.json`、`current-position-projection.json`、`worker-1088.html`、`identity.json`、`exchange-count.json` 保留目标与 GET 证据；`source-sha256.json` 保存六个已核对模块。manifest.json SHA-256：`ad0ca3b8f8f3f7fcef8d3d304d803fa9bdd8eb7c9882509b9f1a4e747d4a50a3`。

只新增本报告与隔离证据文件；未修改其他会话文档或任何代码。没有开展修复、历史记录处置或独立代码评审；没有新生产代码，故未运行 pytest。
