# 30 条 legacy execution_running：29 条副本演练通过，生产等待确认

## 结果与授权门禁

本轮只完成逐条只读核验、完整备份与副本演练，**没有对生产执行 UPDATE**。
生产仍为 execution_running=30 / execution_uncertain=3，全部 33 行与核验前像一致。
新 authoritative_execution_attempts 实测 162 行，而不是背景中的 161；未修改新机制数据。

- 合格 29 条：本次要求的八类候选/执行证据均为零，且无 attempt、wakeup fence、
  instruction、execution contract、trade signal、management envelope/target。
- 排除 raw **14214**：candidate 2129、instruction 909 存在，违反本次“无 candidate”准入。
  无论属于哪个 generation、是否实际下单，都不纳入 UPDATE。
- 绝对排除 uncertain raw **14825 / 14843 / 14889**（decision 14822 / 14838 / 14887）。
- 副本单条批量 UPDATE 精确命中 29，running 30→1、uncertain 3→3；
  事务回滚验证通过，重复执行命中 0；quick_check=ok、外键检查 0 行。
- backlog 的 running/uncertain 状态谓词对合格 29 条返回 0；
  raw 14214 与三条 uncertain 仍会保留相应保护。**不等于全局维护门禁已解除。**

遵守“副本演练完成后先停下，等确认再生产执行”。本轮不部署、不重启、不改 schema，
不发起交易所写请求；不处置历史 protection intent、waiting_backup_stop 或 conflicted。
未构建逐行取证 CLI、恢复服务或自动回收机制；只有本轮一次性只读查询、证据和单条 SQL。

## 核验口径

生产 SQLite 连接使用 `mode=ro`、`PRAGMA query_only=ON`，初始核验在一个只读事务内取得。
按消息所有 generation 查询，而非只看当前 comparison_claim_token 对应的 generation：

- candidate、instruction、contract、attempt、envelope/target：raw_message_id；
- binding、trade signal：源消息 `(chat_id,message_id)`，并扩展已关联的 strategy_instance_id、
  lifecycle/contract/batch 的 execution_binding_id；
- order leg：binding 与 strategy_instance_id；event：source_message_id、源 chat/message、
  binding/strategy_instance/trade_signal；
- management batch：raw_message_id 或 recognition_decision_id；management leg/component：
  对应 batch、entry leg 与 management leg；mutation：binding、entry leg、strategy_instance；
- wakeup fence：strategy_raw_message_id 或 trigger_raw_message_id。

仅“被消息提到的旧 target lifecycle”不等于该消息已执行。没有用价格、方向、时间接近
推断实际提交，也没有用当前无仓来推断历史从未下单。字段、关联 ID 和原始行像留在服务器。
核验覆盖所有可关联的持久记录；不能据此声称审计了交易所全账户的全部历史订单。

30 条 job 中 28 succeeded、2 failed；均无 claim_token/claimed_at，未修改 job。
合格 29 条中为 27 succeeded、2 failed。所有 legacy decision 的最新更新时间不晚于
`2026-09-03T13:47:03.868720Z`；当前 web/worker 主进程从 `2026-09-04T22:04:37Z`
启动，ingest 从 `22:04:38Z` 启动（systemctl 输出 CST 已转换 UTC）。
这些旧 ownership 早于当前执行进程，且没有新 attempt 接管，不是仅凭“超时”判孤儿。

## 逐条核验结果

“八项全零”逐项指 candidate / binding / order leg / execution event /
management batch / management leg / management component / position mutation intent，
每条均分别查询；不是用一个汇总计数代替未检查的项。

| raw message | decision | posted_at 日期（UTC） | job | 八项结果 | 处置范围 |
|---|---|---|---|---|---|
| 12798 | 12797 | 2026-08-24 | succeeded | 全零 | 合格 |
| 12849 | 12848 | 2026-08-24 | succeeded | 全零 | 合格 |
| 12897 | 12898 | 2026-08-24 | succeeded | 全零 | 合格 |
| 13022 | 13022 | 2026-08-25 | succeeded | 全零 | 合格 |
| 13076 | 13073 | 2026-08-25 | succeeded | 全零 | 合格 |
| 13160 | 13159 | 2026-08-25 | succeeded | 全零 | 合格 |
| 13166 | 13165 | 2026-08-25 | succeeded | 全零 | 合格 |
| 13198 | 13197 | 2026-08-26 | succeeded | 全零 | 合格 |
| 13307 | 13306 | 2026-08-26 | succeeded | 全零 | 合格 |
| 13308 | 13307 | 2026-08-26 | succeeded | 全零 | 合格 |
| 13396 | 13395 | 2026-08-27 | succeeded | 全零 | 合格 |
| 13433 | 13433 | 2026-08-27 | succeeded | 全零 | 合格 |
| 13503 | 13502 | 2026-08-27 | succeeded | 全零 | 合格 |
| 13571 | 13570 | 2026-08-28 | succeeded | 全零 | 合格 |
| 13589 | 13588 | 2026-08-28 | succeeded | 全零 | 合格 |
| 13685 | 13683 | 2026-08-28 | succeeded | 全零 | 合格 |
| 13723 | 13723 | 2026-08-28 | succeeded | 全零 | 合格 |
| 13730 | 13729 | 2026-08-28 | succeeded | 全零 | 合格 |
| 13835 | 13834 | 2026-08-29 | succeeded | 全零 | 合格 |
| 14193 | 14192 | 2026-09-01 | succeeded | 全零 | 合格 |
| 14196 | 14195 | 2026-09-01 | succeeded | 全零 | 合格 |
| 14214 | 14213 | 2026-09-01 | succeeded | candidate=1，其余七项=0 | **排除** |
| 14220 | 14219 | 2026-09-01 | succeeded | 全零 | 合格 |
| 14243 | 14242 | 2026-09-01 | succeeded | 全零 | 合格 |
| 14289 | 14288 | 2026-09-01 | succeeded | 全零 | 合格 |
| 14374 | 14373 | 2026-09-02 | succeeded | 全零 | 合格 |
| 14378 | 14377 | 2026-09-02 | failed | 全零 | 合格 |
| 14428 | 14426 | 2026-09-02 | failed | 全零 | 合格 |
| 14497 | 14495 | 2026-09-02 | succeeded | 全零 | 合格 |
| 14636 | 14634 | 2026-09-03 | succeeded | 全零 | 合格 |

### 排除 raw 14214 的执行事实

candidate 2129：position_update / partial_then_break_even，target lifecycle 1040，
创建于 `2026-09-01T05:39:13.483091Z`；generation 为
`18fea99476f149418dbe5f58b4e36b3b`，不同于当前卡住 generation
`ae90b0f26128493b9a5b7d3233b3cf09`（decision 最后更新 `05:39:19.684298Z`）。
因此此次发现不证明 candidate 是上一轮以后新产生的，也不能以 generation 不同豁免排除规则。

instruction 909 被调度并终结为 succeeded，但 result_json 明确为：
`{"reason":"kol_or_group_auto_trade_disabled","status":"skipped"}`，error=NULL。
它不是成交成功。既有 `_auto_process_management_signal()` 在交易模式不是 auto_trade 时
直接返回该结果（当前 `auto_trade_execution.py:1445`，旧 `0de19c1c` 同路径 `:1433`），
在后续执行动作之前退出。源消息/strategy_instance 关联的 binding、trade signal、
order leg、event、management batch/leg/component、mutation 全为零。
目标 lifecycle 1040 当前 exited，execution_binding_id=NULL。

**现有持久结果与早退路径指向未发起交易所动作，没有发现跨边界迹象。**
这不是全账户交易所历史的完备证明；该行仍排除并保持 execution_running，
不擅自改为 abandoned，也不在缺乏跨边界证据时擅自改为 uncertain。

## L3 备份、演练与 SQL

证据目录（root-owned 0700，文件 0600）：

```text
/var/lib/telegram-kol-maintenance-evidence/legacy-running-rehearsal-20260905T073939Z/
```

- `readonly-inventory.json`：逐条关联 ID、job、generation、时间、running/uncertain 全列前像。
- `before.db`：SQLite online backup 一致性副本，**896,454,656 字节**，SHA-256：
  `f8da902fc63f31361be06626b7bb14ef8f83a8c614c83ec17de6b211f789a854`。
- `backup.json`：`2026-09-05T07:45:14Z` 完成记录；备份 quick_check=ok、FK=0 行；
  30/3 行与只读核验前像逐字段相同。
- `rehearsal.db`：初始 SHA-256 与 before.db 相同。使用 `cp --reflink=auto` 创建独立
  逻辑副本（不是硬链接），修改演练库后再次验证 before.db 哈希未变；不删除任何旧备份。
- `update.sql` / `update-parameters.json`：单条批量 UPDATE、固定 29 组精确前像参数。
- `rehearsal-result.json`：`2026-09-05T07:47:16Z` 演练通过。
- `production-unchanged.json`：演练后生产 30/3 全行原样保留的只读复核。

SQL 与现有 failed_safe 对 decision 的投影一致
（`authoritative_execution_attempts.py:319–339`），不新增 attempt 行：

```sql
UPDATE recognition_decisions
SET comparison_status = 'completed',
    agreement_status = 'review_disabled',
    comparison_claim_token = NULL,
    comparison_started_at = NULL,
    automation_status = 'failed',
    automation_reason = 'authoritative_execution_abandoned_before_side_effect',
    updated_at = ?
WHERE comparison_status = 'execution_running'
  AND (id, raw_message_id, comparison_claim_token, updated_at)
      IN (/* 29 组绑定参数，见服务器文件 */)
  AND NOT EXISTS (
      SELECT 1 FROM signal_candidates c
      WHERE c.raw_message_id = recognition_decisions.raw_message_id)
  AND NOT EXISTS (
      SELECT 1 FROM authoritative_execution_attempts a
      WHERE a.raw_message_id = recognition_decisions.raw_message_id);
```

完整 SQL SHA-256：`f4ffc7a89698ca5b97d4195e2a791849ecc0c333797499f77e2b6af132122911`。
不把无候选自动推断为全链无执行；前面的逐条全链核验也是必需前提。
未来生产执行必须在短 `BEGIN IMMEDIATE` 事务内重新核对全链零记录与精确前像，
影响行数不等于 29 就 rollback，不能静默接受新变化、扩大集合或覆盖新 generation。

副本分别验证未提交事务 rollback 回到原 30 行，然后提交相同 UPDATE：
影响 29 行，running=1、uncertain=3。再次执行相同精确前像谓词命中 0 行。
除上述 7 列外，29 条的其余字段均不变；其他全部 decision 的流式 SHA-256 前后相同：
`0be26d6c99a50b8d31f0c51102611b3ba0aec4e860631e13b19a4ea0804c8fb5`。
3 条 uncertain 和 raw 14214 全列另做相等校验。SQLite authorizer 拒绝其他表/列的写入、
INSERT、DELETE 及 schema 动作；sqlite_master 前后相同，演练后 quick_check=ok、FK=0 行。

### 副本前后计数（全部不变）

| 表 | 前 → 后 |
|---|---:|
| recognition_decisions | 14973 → 14973 |
| raw_messages / signal_candidates | 14976/2199 → 同值 |
| execution_bindings / order legs / events | 339/584/3986 → 同值 |
| management batches / legs / components | 158/139/27 → 同值 |
| position_mutation_intents | 635 → 635 |
| authoritative_execution_attempts / wakeup executions | 162/0 → 同值 |
| message_processing_jobs | 3215 → 3215 |
| trigger_protection_intents | 183 → 183 |
| position_protection_legs / ledger | 869/659 → 同值 |

没有重排 job、重跑识别、解除 uncertain 或触发交易执行；未对生产库运行该 UPDATE。
备份及副本完整性检查不冒充“生产处置后检查”；真正生产执行后的检查尚待授权。

## Backlog expiry 验证的准确边界

`message_processing_backlog_expiry.py` 对调用者指定的 expected_ids 检查 running/uncertain，
不是只看全局计数。副本上按其相同状态谓词查询合格 29 条，阻塞计数为 0；
剩余 raw 14214 为 running，raw 14825/14843/14889 为 uncertain，保护继续有效。

**没有调用 apply expiry、没有改 job 来构造可过期样本，也没有宣称完整 planner 放行。**
这些 job 本身是 succeeded/failed，还会受到 planner 的 pending、attempt、claim、
精确水位等其他门禁。未来维护若覆盖剩余四条，仍可能被拒，应按保护原义报告，不能弱化检查。

## 后续生产与回滚边界

本轮到此停止。确认后执行前还需 fresh backup/哈希、容量核对、完整前像与全链复验，
不得将本轮副本结果当作未来生产状态。生产只做同一条精确 UPDATE 的单事务，
提交前确认 29、排除集合不变，提交后 quick_check/FK、计数与逐字段保护校验。

回滚仅恢复这 29 行的上述七列，使用对应完整 before-image，并以本次 after-image
（含生产 applied_at、状态与 NULL token）做精确条件；任何行已获新 generation 或发生
并发变化就停止，不覆盖、不自动重放。绝不可用全库备份覆盖在线生产、不可改 job 或 attempt。
事务内失败直接 ROLLBACK；提交后如需恢复须绑定新鲜证据并单独明确授权。
本轮只验证事务未提交时的 rollback，不声称已演练提交后的在线并发恢复。
