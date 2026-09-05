# 29 条 legacy execution_running 生产终结完成

## 结果

经所有者在副本演练通过后单独批准，生产于 **2026-09-05T12:57:55.215113Z**
提交单条精确 UPDATE，影响 **29 行**。`execution_running` 从 30 降至 **1**，
`execution_uncertain` 保持 **3**。

- raw **14214** 保持排除、execution_running，完整行未变。
- uncertain raw **14825 / 14843 / 14889**（decision 14822 / 14838 / 14887）
  全字段未变，未清锁、未重新分类。
- 29 条变更为 `comparison_status=completed`、`automation_status=failed`、
  `automation_reason=authoritative_execution_abandoned_before_side_effect`，不是执行成功。
- 生产处置前后均 `quick_check=ok`、`foreign_key_check=0 行`；新鲜备份检查也通过。
- **backlog expiry 的保护仍因剩余 1 running + 3 uncertain 而生效，阻塞没有全局解除。**
  没有执行 backlog expiry、重排 job 或重跑识别。

本轮只有上述授权的数据修复与证据写入；没有部署、重启、schema 变更或交易所写请求。
未处置历史 trigger protection intent、waiting_backup_stop、conflicted 或其他残留。

## 前提与新鲜核验

已批准的精确清单、逐行关联口径、raw 14214 的排除证据与副本演练见
[演练记录](2026-09-05-legacy-execution-running-rehearsal.md)。该历史文档的“等待确认”
状态由本生产记录取代，不改写当时尚未执行的事实。

本轮重新查询全部 29 条消息的所有 generation，而非仅当前 claim token：
candidate、instruction、contract、trade signal、binding、order leg、execution event、
management batch/leg/component、mutation、envelope/target、attempt、wakeup 均无关联记录。
源消息关联根为空后再检查 lifecycle→binding 与执行子记录，未通过时间/价格猜测归属。
29 条 job 均为 succeeded/failed，claim_token/claimed_at 为空。
全部原 30 条 running 与 3 条 uncertain 的完整行仍与演练前像相同。

这些检查先以 SQLite `mode=ro`、`PRAGMA query_only=ON` 完成，
又在生产写事务取得锁后重复，避免只依赖数小时前的证据。
发现任何前像漂移或关联记录便拒绝整个事务，不自动扩大或缩小已批准的 29 条集合。

生产 worker 的只读身份为 `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，
manifest `2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`，
loaded_artifact_verified=true；未以 `/opt` 工作树 HEAD 代替运行版本。
所有一次性脚本使用 `python -B` / `PYTHONDONTWRITEBYTECODE=1`，未导入业务执行模块。

## L3 备份与证据

服务器证据目录 root-owned、0700，证据文件 root-owned、0600：

```text
/var/lib/telegram-kol-maintenance-evidence/legacy-running-production-20260905T125030Z/
```

- `before.db`：SQLite online backup 一致性副本，**899,104,768 字节**。
  SHA-256：`da6077d5e9db13c62b058581b55b9277be48e63c96b4fdab833d3b44dc85bccf`。
  apply 前再次计算并核对备份 SHA-256。
- `production-pre-health.json`：生产 quick_check=ok、FK=0 行，耗时约 45.26 秒。
- `backup.json`：备份完整性检查通过、状态仍 30/3，完整前像及无关联记录核验通过。
- `fresh-readonly.json`：新鲜的 29 条全链核验及完整 33 行前像。
- `production-update.sql.json`：实际 SQL、精确参数及本次 updated_at。
- `transaction-verified-before-commit.json`：事务内全链复验、前后计数、33 行前后像，
  在 COMMIT 前已写入并 fsync；证据保存失败会回滚而非无证据提交。
- `commit.json`：提交时间、影响行数、事务时长。
- `production-postcheck.json`：提交后的生产 quick_check/FK、逐字段与计数校验。
- `rollback-boundary.json`：29 行完整前像、允许恢复的七列及精确后像条件。
- `one-off-approved-operation.py`：本次执行留证，非新增生产 CLI 或自动恢复工具。

没有删除任何旧备份或证据。处置前根分区可用 9,725,403,136 字节；
提交后检查时可用 **8,822,358,016 字节（约 8.22 GiB）**。

## 单事务与精确写入边界

事务开始 `2026-09-05T12:57:53.318691Z`，提交
`2026-09-05T12:57:55.215113Z`，持续约 **1.90 秒**。
使用已演练的原 SQL，SHA-256：
`f4ffc7a89698ca5b97d4195e2a791849ecc0c333797499f77e2b6af132122911`。
29 组 `(id,raw_message_id,comparison_claim_token,updated_at)` 前像参数不变，
仅 SET 的 updated_at 换成本次时间。

步骤为 `BEGIN IMMEDIATE` → 完整前像/无执行关联复验 → 单条 UPDATE →
检查 rowcount=29、total_changes=29、状态计数及所有目标/排除行 → 保存证据 → COMMIT。
未通过检查即 ROLLBACK，没有重试或强制覆盖。

仅修改七列：comparison_status、agreement_status、comparison_claim_token、
comparison_started_at、automation_status、automation_reason、updated_at。
值与演练计划相同：completed / review_disabled / NULL / NULL / failed /
authoritative_execution_abandoned_before_side_effect / 本次时间。
29 条其他字段逐字段不变；raw 14214 与三条 uncertain 的所有字段独立验证不变。

SQLite authorizer 仅允许本条 SQL 对 recognition_decisions 七列 UPDATE，
拒绝其他表/列写入、INSERT、DELETE 和 schema 动作；确认目标表没有触发器，
sqlite_master 前后完全相同。执行程序只使用 SQLite/文件证据操作，不加载 adapter、
executor、通知或业务回调，因此此 UPDATE 不提交交易所订单。

### 事务内前后计数

下表是持有同一事务锁时的前后计数，避免把并行正常业务写入误算成本次修复。

| 表 | 前 | 后 |
|---|---:|---:|
| recognition_decisions | 14992 | 14992 |
| raw_messages | 14995 | 14995 |
| signal_candidates | 2199 | 2199 |
| execution_bindings | 339 | 339 |
| execution_order_legs | 584 | 584 |
| execution_events | 3986 | 3986 |
| strategy_management_batches | 158 | 158 |
| strategy_management_legs | 139 | 139 |
| strategy_management_components | 27 | 27 |
| position_mutation_intents | 635 | 635 |
| authoritative_execution_attempts | 181 | 181 |
| entry_assembly_wakeup_executions | 0 | 0 |
| recognition_execution_scan_cursors | 5 | 5 |
| message_processing_jobs | 3234 | 3234 |
| trigger_protection_intents | 183 | 183 |
| position_protection_legs | 869 | 869 |
| position_protection_ledger | 659 | 659 |

相较演练时 attempt=162，本轮新鲜基线为 181，这是先前正常运行的增长，
不是本次 UPDATE 创建 attempt。生产提交后 `12:58:40.963138Z` 复核时上表计数仍相同，
quick_check=ok、FK=0 行（约 33.91 秒），29 条完整后像及四条完整保护行均符合预期。

## Backlog 保护与回滚

只读复用现有 running/uncertain 状态谓词：合格 29 条不再命中这两个谓词；
剩余 raw 14214 仍 running，raw 14825/14843/14889 仍 uncertain。
`message_processing_backlog_expiry.py` 按调用者的 expected_ids 检查，维护范围包含这些行时，
相应 `execution_running_decision_present` / `execution_uncertain_decision_present`
保护仍生效。没有削弱检查、执行过期或宣称完整 planner 已放行；job 状态等其他门禁仍在。

事务内失败直接回滚，本轮实际提交成功。提交后若需撤销，须另行授权，
仅恢复这 29 行上述七列，逐行以本次完整 after-image 作为 CAS 条件。
任何行已发生新 generation/并发更新即停止，不覆盖后续业务；
不得用全库备份覆盖在线生产，不得自动重放消息，不得改 job、attempt 或 excluded/uncertain。
本轮没有执行提交后的回滚。
