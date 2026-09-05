# raw 14214 精确终结：生产完成记录

## 结果与授权边界

经所有者单独批准，2026-09-05T13:39:33.541219Z 已提交 decision 14213 / raw 14214
的单行 L3 终结，影响 **1 行**。此前 29 条加本次 1 条，遗留 execution_running 已全部终结。
2026-09-05T13:40:11.399364Z 提交后复核：**running=0、uncertain=3**。
raw 14825 / 14843 / 14889 三条 uncertain 全字段与处置前一致。

backlog expiry 的保护仍因三条 uncertain 而生效；包含这些 ID 的操作仍会被
`execution_uncertain_decision_present` 拒绝，**不得表述为全局阻塞已解除**。
本轮没有执行积压过期、重跑识别、部署、重启、schema 变更或任何交易所写操作。

## 前像与安全依据

先复核完整前像，再在生产写事务锁内重复验证：

- decision 14213 为 execution_running，token=`ae90b0f26128493b9a5b7d3233b3cf09`，
  updated_at=`2026-09-01 05:39:19.684298`；没有该消息的新 lease attempt。
- candidate 2129 属旧 generation `18fea99476f149418dbe5f58b4e36b3b`，目标 lifecycle 1040。
- instruction 909 数据库存储 status=succeeded，结果为
  `skipped / kol_or_group_auto_trade_disabled`，终结于 05:39:13.550140Z，
  比当前卡死 generation 的 claim 早 6.134158 秒；旧执行器只认领 pending。
- lifecycle 1040 为 exited、execution_binding_id=NULL。扩展至目标的 binding、
  order leg、management batch/leg/component、mutation、保护全链无新增关联。
  唯一根消息 execution event 3883 是 auto_trade_skipped，不是交易所提交回执。
- candidate、instruction、job、context/run、lifecycle 及相关记录均与上一份只读证据一致；
  不删除候选、不改 instruction，不以“有候选”推断已跨写边界。

完整证据链及历史代码位置见
[只读核查](2026-09-05-raw14214-write-boundary-read-only-audit.md)。
**具体导致卡死的异常始终未能确定**：没有取得 traceback，前后 worker PID 相同，
不能归因于重启；处置完成并不补齐这个历史根因证据缺口。

## 备份、副本演练与完整性

证据根目录（root-owned、0700；文件 0600）：
`/var/lib/telegram-kol-maintenance-evidence/raw14214-terminalization-20260905T133139Z`

生产备份使用 SQLite online backup，一致性备份保存在该目录 `before.db`：

- 大小：**900,026,368 字节**。
- SHA-256：`e977688721228d64ba08569a909348b9d4e4dec8a33dfae4e063b9621b343fd9`。
- 备份元数据时间：2026-09-05T13:36:27.271590Z。
- 生产处置前 quick_check=ok、foreign_key_check=0 行（41.85 秒）；
  备份亦为 ok / 0 行（28.81 秒）。

独立 `rehearsal.db` 初始字节摘要与备份完全相等。副本依次验证：

1. 精确 UPDATE 1 行后 ROLLBACK，恢复全部原像。
2. 相同 SQL 精确 UPDATE 1 行并 COMMIT，running 1→0、uncertain 3→3。
3. 使用原 token/updated_at 重复 UPDATE，影响 0 行。
4. 演练后 quick_check=ok、foreign_key_check=0 行（24.68 秒）。

演练完成再次确认生产仍为 1/3、完整前像未变；备份 SHA 未改变，才执行生产事务。
生产提交后再次 quick_check=ok、foreign_key_check=0 行（22.94 秒）；
候选、instruction、job、关联记录和三条 uncertain 全字段比较通过。

## 生产单事务与回滚边界

BEGIN IMMEDIATE：2026-09-05T13:39:31.623596Z；
提交完成：2026-09-05T13:39:33.541219Z；事务总耗时 **1.917852 秒**。
精确 SQL SHA-256：`63efb5b6806ee9de356ee074a3e04ab59c784ed449f81ac7c33d188b32d78f19`。

与前 29 条采用同一终结形态，仅修改这七列：

| 字段 | 处置后值 |
|---|---|
| comparison_status | completed |
| agreement_status | review_disabled |
| comparison_claim_token | NULL |
| comparison_started_at | NULL |
| automation_status | failed |
| automation_reason | authoritative_execution_abandoned_before_side_effect |
| updated_at | 2026-09-05 13:39:31.621012 UTC |

SQL 绑定 id/raw ID、原状态、exact token/updated_at、candidate generation/target、
instruction 终态/时间及无新 attempt；锁内还比较完整 decision、uncertain 和关联前像。
SQLite authorizer 只允许这一表的七列 UPDATE，禁止 INSERT/DELETE/DDL；
确认目标表无 trigger、sqlite_master 前后相等，rowcount 和 total_changes 增量均为 1。
一次性操作只导入 Python 标准库，不调用业务执行器、adapter、HTTP 或交易所接口。
所有检查通过并 fsync 事务证据后才 COMMIT；不创建通用逐行取证/修复工具。

提交前任一失败直接 ROLLBACK。提交后若需撤销，必须另行授权并对精确完整后像做 CAS，
仅恢复该 decision 的七列；有新 generation 或任何后续字段变化即停止。
**不能把全库备份覆盖在线生产**，不能覆盖后续业务，也不能自动重放该消息。
恢复前像会重新建立其 running 锁和相应阻塞，绝非无影响操作。

## 受影响与关键业务表前后计数

以下计数在同一写事务中前后相等；提交后独立复核亦一致。

| 表 | 前 | 后 |
|---|---:|---:|
| recognition_decisions | 14999 | 14999 |
| raw_messages | 15002 | 15002 |
| signal_candidates | 2199 | 2199 |
| message_instruction_items | 979 | 979 |
| message_operation_contracts | 21 | 21 |
| instruction_execution_contracts | 296 | 296 |
| execution_bindings | 339 | 339 |
| execution_order_legs | 584 | 584 |
| execution_events | 3986 | 3986 |
| strategy_management_batches | 158 | 158 |
| strategy_management_legs | 139 | 139 |
| strategy_management_components | 27 | 27 |
| position_mutation_intents | 635 | 635 |
| authoritative_execution_attempts | 188 | 188 |
| entry_assembly_wakeup_executions | 0 | 0 |
| recognition_execution_scan_cursors | 5 | 5 |
| message_processing_jobs | 3241 | 3241 |
| trigger_protection_intents | 183 | 183 |
| position_protection_legs | 869 | 869 |
| position_protection_ledger | 659 | 659 |
| position_take_profit_orders | 197 | 197 |

初始可用空间 8,807,268,352 字节；结束可用 7,005,601,792 字节。
保留备份和演练副本，未删除任何历史备份、release 或日志。

## 证据索引与剩余范围

上述证据根目录内：`fresh-preimages.json`、`production-pre-health.json`、`backup.json`、
`rehearsal-rollback-transaction.json`、`rehearsal-commit-transaction.json`、
`rehearsal-result.json`、`production-still-unchanged.json`、
`production-sql-and-rollback.json`、`production-transaction.json`、
`production-commit.json`、`production-postcheck.json` 与 `one-off-approved-operation.py`。
详细行值保留在服务器证据中，本文不输出原消息正文或凭据。

剩余 raw 14825 / 14843 / 14889 为 execution_uncertain，需要独立对账与人工授权，
不属于本次 29+1 legacy running 终结范围。仅以只读状态及
`message_processing_backlog_expiry.py` 的 uncertain 拒绝分支核实保护仍在，
没有实际调用积压过期维护。
