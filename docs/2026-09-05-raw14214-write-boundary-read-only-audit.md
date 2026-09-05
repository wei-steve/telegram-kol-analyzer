# raw 14214：未跨越管理写边界，卡死的具体异常尚无日志证明

## 结论：甲；本轮不执行处置

**针对 raw 14214 / candidate 2129 的系统执行路径，已证明未跨越交易所写边界。**
证明不只是“没有 binding”：旧 generation 的 instruction 909 有明确的终态
`skipped / kol_or_group_auto_trade_disabled`，对应当时代码在任何管理规划/交易写之前的返回；
当前卡死 generation 比该终态晚约 6.134 秒，已有 instruction 不再是 pending，
执行器不能重新认领它。全链持久记录与这两个实际分支相互一致。

可在另行批准后以 L3 精确终结 decision 14213；不能直接复用上一批
`NOT EXISTS signal_candidates` 的 SQL，因为本条确有应永久保留的候选与 instruction。
本轮只给计划，**未修改 raw 14214 或三条 uncertain 的任何字段**。

两个问题不能混为一谈：

- **写边界已经查清**：该条不是“已下单但漏 automation 记录”；没有依据把它改成 uncertain。
- **哪一个异常导致 finalize 缺失仍未查清**：现有 journal 没有该条栈；没有证据证明进程重启，
  也不把已确认的一般异常吞没结构当作这一行具体异常的证明。

历史账户里所有人工交易的绝对不存在，不在这个结论之内：系统记录不能证明所有者从未手工下单。
本结论依赖 source/generation 的确定执行路径，不依赖“当前空仓”或不完整历史列表。

## 范围、版本与只读证据

生产查询为 SQLite `mode=ro` + `PRAGMA query_only=ON`；使用一致只读事务采集主记录。
运行身份从 worker 8002 GET 读取：当前 `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，
manifest `2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`，verified=true。
当时路径按 **18434b4552938ae3acb1160ad32618aab9c3ecf4** 的 Git 源文件审阅，
不是拿后来加入 lease 的当前代码冒充 9 月 1 日代码。
该旧 release 的运行依据见状态文档 `Read-only live-position protection audit and P0 checkpoint`
（同一 worker PID 944565、immutable 身份），并与当天 journal 的 03:18–08:03 进程窗口对齐。

服务器证据目录（root-owned；主 DB 证据含完整字段，报告仅提取相关结构）：

```text
/var/lib/telegram-kol-maintenance-evidence/raw14214-readonly-20260905T131000Z/
```

`database-audit.json` 保存各表完整命中行、原始 JSON 和四条受保护 decision 前像；
`worker-window.log`、`worker-day.log`、两个角色日志、`all-units-window.log` 保存 journal；
`exchange-current.json`、`positions.html`、`history.html` 是 worker GET 回包；
`current-group.json` / `groups-file-metadata.json` 保存当前配置的相关字段与文件指纹；
`incidents.json` 与 `readonly-final-check.json` 保存 incident 查询和结束复核。
临时脚本均使用 `python -B` / `PYTHONDONTWRITEBYTECODE=1`；没有加载交易执行模块运行。

## 1. 两个 generation 与记录时间线

raw 14214：chat **-1003095914903**，Telegram message **3212**，
posted_at **2026-09-01T05:37:46Z**。以下时间全部 UTC。

| 时间 | 记录 | 含义 |
|---|---|---|
| 05:37:46.816569 | job 2453 succeeded / attempt_count=0 / worker_completed | 早于后面的权威处理；不是后续 finalize 成功的凭据 |
| 05:37:47.179871–05:38:19.146247 | MiMo run 4604 completed，became_authoritative=1 | 一次 v1_authoritative，无 provider error |
| 05:38:47.490215–05:39:13.139215 | context attempt 4279 completed，attempts=2 | 初次上下文，manage_thread / partial_take_profit，target thread 409 |
| 05:39:13.461505 | decision 14213 created | 首个记录创建时间，不能当作当前 generation 的 claim 时间 |
| 05:39:13.483091 | candidate 2129 created | generation `18fea99476f149418dbe5f58b4e36b3b` |
| 05:39:13.487846–05:39:13.550140 | instruction 909 created→succeeded | result 为 skipped / kol_or_group_auto_trade_disabled；error_json=NULL |
| 05:39:19.662701 | context attempt 4280 completed / reanalysis | manage_thread / partial_take_profit，strategy_state_changed，target 409 |
| 05:39:19.684298 | decision 最后更新、execution_running | 当前 token `ae90b0f26128493b9a5b7d3233b3cf09`；automation 两列 NULL |

candidate 2129 内容：BTC long、position_update、mimo_authoritative、confidence=0.85，
target_lifecycle_id=1040，management_action=partial_then_break_even，fraction=0.5。
管理 contract v2 表达：消费首档止盈、部分平仓 50%、剩余保护替换，明确保护价 78000，
cancel_deferred_entries=true。它是**已形成的管理意图**，不是开仓或实际平仓凭证。
contract fingerprint：`4f0d8e469de46419b5ac06b199e605cde49c5b5cb78c28be990d25fa8fbd9257`。

## 2. 全链逐表结果：源消息与目标历史分别计数

源范围按 raw/candidate/instruction/decision ID；另扩展 target lifecycle 1040、
其根消息 `(chat -1003095914903,message 3207)` 与精确 strategy instance
`deepcoin:-1003095914903:3207:BTC:long`，再沿 binding/order leg/batch/contract 外键查询。
下面的 0 都实际检查过，不是只看到 binding=0 后跳过整个表。
JSON 额外按 raw ID、candidate_id 与精确 instance 搜索线索，未发现额外命中；
文字搜索只作补充，不代替强关联。

| 表 | raw 14214 直接关联 | 扩展目标历史后的总数 | 实际内容 |
|---|---:|---:|---|
| message_instruction_items | 1 | 1 | 909：candidate 2129，management，sequence=0，succeeded；result skipped/kol_or_group_auto_trade_disabled，error=NULL，retired_at=NULL，visibility_retry_attempts=0，无重试/执行期限 |
| message_operation_contracts | 0 | 0 | 无行，不与下表 instruction contract 混称 |
| instruction_execution_contracts | 0 | 0 | 无行、无 attempted_exchange_write 记录 |
| instruction_execution_transitions | 0 | 0 | 无行 |
| strategy_management_batches | 0 | 0 | 同时查 raw/decision/target_lifecycle/strategy_instance，无行 |
| strategy_management_legs | 0 | 0 | 无行 |
| strategy_management_components | 0 | 0 | 无行 |
| execution_bindings | 0 | 0 | 源消息与 target 根消息/instance 均无行 |
| execution_order_legs | 0 | 0 | 无行 |
| execution_events | 0 | **1** | 3883 属于根消息 raw 14203 / message 3207，非 raw 14214 的管理写；详见下文 |
| position_mutation_intents | 0 | 0 | 无行 |
| position_protection_ledger | 0 | 0 | 无行 |
| position_protection_legs | 0 | 0 | 无行 |
| trigger_protection_intents | 0 | 0 | 无行 |
| position_take_profit_orders | 0 | 0 | 无行 |
| trigger_take_profit_convergences / stop_rescues | 0 / 0 | 0 / 0 | 无行 |
| position_protection_revisions / incidents / attribution_audits | 0 / 0 / 0 | 0 / 0 / 0 | 无行 |
| trade_signals | 0 | 0 | 源消息与 target 根消息/instance 均无行 |
| management_message_envelopes / targets | 0 / 0 | 0 / 0 | 无行 |
| authoritative_execution_attempts / entry_assembly_wakeup_executions | 0 / 0 | 0 / 0 | legacy，无 lease attempt 或 child fence |

**唯一新增发现 event 3883 的内容**：创建于 `2026-09-01T02:55:41.905786Z`，
action=auto_trade_skipped、status=skipped、reason=kol_or_group_auto_trade_disabled，
chat=-1003095914903、message=3207、BTC long、venue=deepcoin。
binding_id/trade_signal_id/order_id/client_order_id/pos_id/related_order_id/exchange_event_time
均 NULL，response_json/after_json/before_json 均 NULL。
request_json 是 candidate 2126 的快照：entry_signal、mimo_authoritative、confidence=0.95，
市价/78400 附近、stop=77500、TP=80000。它**不是 Deepcoin HTTP 请求体**。
旧 `auto_trade_execution.py:1282–1348` 的 `_record_entry_auto_trade_skip()`
将候选字段放入 `ExecutionEventRecord(request=payload)`，只记录本地 skip。
该表名与 `request_json` 列名不能被误读成已向交易所发送订单。

所有查询匹配的请求/响应/错误内容均未提供提交、改单、撤单或设置 TPSL 的痕迹。
raw 本身没有请求行；instruction 的 skipped 结果是明确的成功跳过，不是吞掉未知响应后伪装跳过。

## 3. 写边界证明与交易模式的确切含义

以下行号均对应旧 commit 18434b45，而非当前工作树：

1. `auto_trade_execution.py:1406–1433`：管理入口先读取有效 group/sender runtime config；
   config 不存在返回另一理由 `group_not_configured_for_auto_trade`；
   **只有 `runtime_config["trading_mode"] != "auto_trade"` 才返回本条理由**。
   此返回在后续管理规划、batch、writer 之前。因此 instruction 909 的持久结果
   证明 **05:39:13 那次调用的有效交易模式确实不是 auto_trade**，不是靠今天设置推断。
2. `recovery_scan.py:327–359`：匹配 sender 时优先使用 sender.trading_mode，否则使用
   匹配 group.trading_mode。`trading_settings.py:397–421` 保留这两个层级的交易模式；
   全局 auto_trade_enabled=true 不能覆盖这一局部禁用。
3. 当前 groups.yaml 对该 chat 为 enabled=true、ai_strategy_enabled=true、
   trading_mode=notify_only；文件 mtime 为 `2026-08-13T05:39:33.225293Z`。
   这是佐证，不把可修改的 mtime 冒充不可变的历史配置审计。
   **当时有效模式非 auto_trade 已证实；仅凭现库不能独立重建当时每个 sender 覆盖值。**
4. 当前 running token 与 candidate token 不同。`message_recognition.py:3391–3437`
   允许语义相同的已关联终态候选保留旧 generation，不将它重置 pending；
   `message_instruction_items.py:48–145` 复用已有 item，也不重置状态。
5. `auto_trade_execution.py:288–300,324–348,474–502` 有 instruction 时只执行该路径；
   `message_instruction_items.py:149–211` 查询及 CAS 均要求 status=pending。
   909 已是 succeeded、没有别的 item，新 generation 即使走到 executor，也只汇总终态，
   不能再调用 `_auto_process_single_message_trade_signal()` 为本条提交管理请求。
   没有 instruction 则走 legacy 的 else 在这里不成立。

由此可区分：第一次是**明确禁止自动交易而跳过**；第二次是**权威流程未 finalize**。
这不是“本应自动执行的一笔真实管理动作被系统吞掉”的已证实案例。
实际遗留影响是阻止该消息再写 authoritative decision，以及触发适用范围内的 backlog 保护；
不能缩写为只有页面状态问题。

## 4. lifecycle 1040：本地 entered 不等于曾有实盘仓位

根消息 raw 14203 / message 3207，posted_at `02:55:05Z`；lifecycle 创建 `02:55:41.854159Z`。
当前表记录 entered_at=`02:56:00Z`，entry_price_actual=78400；exited_at=`08:39:48Z`，
lifecycle_status=exited，exit_reason=kol_signal，exit_signal_message_id=3216，
updated_at=`08:41:04.732408Z`，execution_binding_id=NULL。
所以按本地持久时间字段，**05:39:13 时处于 entered，当前 exited**。

该 entered 不是成交回报：旧 `lifecycle_monitor.py:1087–1152` 以 candle 命中入场价形成
StateTransition，`:1512–1514` 写 entered_at/entry_price_actual。根入场 event 3883 已明确 skip，
没有对应 binding/订单/pos_id。本地退出也不能当作 Deepcoin 平仓证明。

worker 当前完整 GET 摘要 complete=true、position_count=1，**并非空仓**；
详细 GET 显示唯一 pos **1001125135694798**，binding **339**，
source `chat -1003048800035 / message 4501`，不是本目标的 chat/message/instance。
该绑定 active；这里只确认归属不同，不扩展处置其保护。

worker 历史仓位 GET 按 2026-09-01 退出日期过滤返回 loaded=true、1/1、无下一页：
pos **1001125076084723**，归属 chat -1003048800035/message 4456，也不是 lifecycle 1040。
**该历史接口是 bounded 列表后过滤，不是全账户无限历史证明**；不足以断言从未有任何
人工 BTC 同向仓位。可确定的是：没有可归属到 1040 的系统仓位记录；其系统入场本来就被禁用，
当前唯一实盘仓位也与它无关。

## 5. 卡死原因：结构已确认，具体异常不能从空日志补造

旧 `authoritative_recognition.py:1418–1499` 的 claim→apply→barrier→executor→finalize
没有 try/finally。`recognition_decisions.py:268–291` 将 claim 单独提交；
finalize 缺失便留下 running。这里没有提前 return 或把非 failed assessment 改成 failed 的分支。

旧 `context_resolution_worker.py:604–694` 捕获重分析异常后返回状态字典；
`message_processing_worker.py:291–293` 丢弃其返回值，结构上允许 job 成功而 decision 留锁。
但本条 job 在 05:37:46 已完成，早于两个 context 与当前 claim；
**本条不能单凭 job=succeeded 确认它具体经过了哪一个异常 catch 分支**。
context 4279/4280 当前 completed、last_error/error_class=NULL，关联 runtime_incidents=0，
没有保存可定位到异常种类的线索。

还有一个不能忽略的限制：旧 `context_resolution_worker.py:41–43,498–509,578–588`
在入口会阻挡已 succeeded 的 instruction。现库只保存解析结果完成时间，没有保存这次
重分析入口检查的时刻和调用栈；无法确定它是在 909 终结前已通过检查，还是来自其他调用入口。
**不将“并发竞态”写成已证实根因，也不把整批已知 catch 路径直接套到该行。**
这不改变下游 executor 的 pending CAS 与已有 skipped 结果对写边界的证明。

日志实测：

- worker、web、ingest 在 `05:35–05:45Z` 均为 `-- No entries --`。
- worker 整日仍有约 454 KB 日志，并非整天 journal 已轮转不可读。
- 最近前后日志为 `05:31:29.204660Z` 与 `06:01:45.060708Z`，均 PID **944565**；
  当天该进程启动于 03:18，下一次记录的停止在 08:03。
- 全 unit 同窗口 1041 行，未见 suppression/drop/OOM/kill/segfault/corruption 线索。

这不支持“05:39 时服务重启导致”，与普通异常吞没结构一致；但缺少具体栈，
**异常种类、准确失败语句，以及是否存在未记录的其他中断，仍需进一步确认**。
无法证实是轮转导致缺失，也不能把无日志等同于未发生异常。
若要补齐，需当时捕获的 traceback、保留的该 invocation 日志或同期更细诊断证据；
现在重跑识别既不还原历史栈，也不在授权范围。

## 6. 精确处置计划：另行授权后才执行

可以采用与前 29 条相同的**终结语义**，不是取消候选或再执行管理：

1. 新鲜只读复核 decision **14213/raw14214** 完整前像、current token
   `ae90b0f26128493b9a5b7d3233b3cf09`、updated_at=`2026-09-01 05:39:19.684298`；
   candidate 2129 与 instruction 909 的完整前像仍保持本报告的 generation 与 skipped 终态，
   全链无新写入关联。任何变化停止，不因本报告曾选甲而豁免新证据。
2. L3：核对容量、做新鲜一致性全库备份与 SHA-256、quick_check/FK；
   先在生产副本演练精确命中 1、重复命中 0、ROLLBACK 可恢复原像。
   本轮不建立备份、不运行演练，也不构建逐行修复工具。
3. 生产单个 BEGIN IMMEDIATE 事务，锁内再次验证上述条件；只对
   id=14213 AND raw_message_id=14214 AND comparison_status=execution_running AND
   exact token/updated_at 的一行 UPDATE。**不能保留上一批的无 candidate 谓词，也不能
   删除 candidate 来让旧 SQL 通过；应改为校验 2129/909 精确已证明的前像。**
4. 七列更新与前批一致：comparison_status=completed、agreement_status=review_disabled、
   comparison_claim_token=NULL、comparison_started_at=NULL、automation_status=failed、
   automation_reason=authoritative_execution_abandoned_before_side_effect、updated_at=处置时间。
   仅表示当前 running generation 安全弃置；旧 instruction 的 skipped 真相仍完整保留。
5. rowcount 必须 1；candidate、instruction、job、三条 uncertain 全字段不变；
   affected/critical 表前后计数不变，schema 不变，提交前任何检查失败直接 ROLLBACK。
   提交后重新 quick_check/FK、精确后像与计数。预期 running 1→0，uncertain 3→3；
   **三条 uncertain 仍保留 backlog expiry 保护，不宣称全局阻塞解除。**
6. 提交后回滚须另行授权，以精确 after-image CAS 仅恢复该 decision 七列；
   如已有新 generation/其他字段变化则停止，不覆盖后续业务，不用全库备份覆盖在线库，
   不自动重放消息，不动 candidate/instruction/job/attempt 或任何保护记录。

## 结束状态

`2026-09-05T13:19:14.183942Z` 只读复核：raw 14214 与 uncertain
14825/14843/14889 完整行均与本轮开始前相同；running=1、uncertain=3。
不部署、不重启、不改任何 DB 行、schema、设置或服务状态、不做交易所写操作。
