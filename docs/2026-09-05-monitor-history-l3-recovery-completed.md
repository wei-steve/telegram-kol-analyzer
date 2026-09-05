# Monitor 历史残留 L3 处置完成

## 结果

所有者授权的两项精确数据修复已分别提交。2026-09-05T06:46:02Z–06:46:09Z，
按授权触发一次现有 monitor oneshot，返回 **healthy=true、reason_codes=[]、退出码 0**。
timer 实测 `active` / `enabled`，主 service 为 `inactive` / `Result=success`，无 failed 状态。
本次是正常 monitor 命令的一次显式触发，不是等待 timer 自动触发，也不是 deployment diagnostic；
未 restart web/ingest/worker，未部署、改 schema 或发起任何交易所写请求。

| 精确对象 | 处置结果 | 提交后验证时间（UTC） |
|---|---|---|
| batch 153 | recovery_required → resolved；history_no_submission_confirmed | 2026-09-05T06:43:56.152909Z |
| management leg 135 | planned → failed；history_no_submission_confirmed | 同一事务 |
| execution event 3984 | management_history_recovery / resolved，新建一条 | 同一事务 |
| preamble 14 / raw 14566 | pending → invalidated | 2026-09-05T06:44:32.325488Z |
| preamble 15 / raw 14625 | pending → invalidated | 同一事务 |

component 25 保持 operator_required/attempt=3，26/27 保持 pending/attempt=0；
三行逐字段不变，不删除或重新执行。Monitor 按现有规则排除 resolved 父 batch 的
stalled component；未改告警判据。30 条 execution_running 与 3 条 execution_uncertain
截至 `06:47:21Z` 仍与备份中全部行字段逐一相同，不仅是计数相同。

## 授权、代码与证据

授权基础：`docs/2026-09-05-batch153-history-recovery-dry-run.md` 的精确计划，
以及所有者本轮明确批准的 batch apply 和 preamble 14/15 同轮处置。
执行技能用于按备份→演练→独立事务→观察的检查点推进；无应用代码修改，
不使用全量 pytest 代替 L3 数据演练。

生产依赖运行中 release `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，
worker identity 在 `06:37:19Z` verified=true、PID=1525316。
所有 immutable import 均 `python -B` / `PYTHONDONTWRITEBYTECODE=1`。

服务器完整证据目录（root-owned，目录 0700、文件 0600）：

```text
/var/lib/telegram-kol-maintenance-evidence/batch153-preambles14-15-20260905T063719Z/
```

目录内含 `before.db`、`rehearsal.db`、备份/检查结果、每阶段 before/after 行镜像与计数、
GET 快照及请求路径、源/结果 fingerprint、正常 monitor 输出、最终观察与 digest 反证。
一次性精确操作脚本保存在 `operator-l3.py`，SHA-256：
`1fdc8c1cd761e02c31fb238abf5a6ef7779bf95c7b0927521689bb8c5be2454a`。
它只在证据目录，不安装为服务或生产功能。

## 备份与演练

- 使用 SQLite online backup，从 `mode=ro`、`query_only=ON` 的生产连接取得一致性副本，
  无 schema bootstrap，无 WAL 手工 checkpoint。复制过程设 600 秒上限。
- 完整备份 `before.db`：**895,942,656 字节**；SHA-256：
  `dcafc3c5e509e6b8155b9ef09d49063d59a2e6c12a5d08d2c10c110946da0018`。
- 演练初始副本与备份 SHA-256 完全相等。该完整备份覆盖两项修复之前的现场；
  第二项的目标行在第一项后再次确认仍与备份相同，另留第二项紧邻事务的 before-image。
- 备份 quick_check=ok / foreign_key_check=0 行，检查耗时 60.85 秒。
- 副本分别演练两项提交，另分别验证未提交事务 rollback 后完整 state 与 before 相等；
  未将这种事务回滚测试冒充生产提交后的在线恢复演练。
- 演练后 quick_check=ok / FK=0 行，37.10 秒；生产两项处置后
  quick_check=ok / FK=0 行，34.53 秒，检查连接强制只读。
- sqlite_master 在两个生产事务内前后完全相同。没有 schema 动作。

## 第一项：batch 153 的精确提交边界

生产 apply 前重新通过原 planner，仍为 ready / terminal_no_submission，
evidence fingerprint 与授权 dry-run 完全一致：
`7b8776f35c9e40e2709d6264ccd5e0461c84fabd69421dd9691c67989e22f7f5`。
重新获取完整只读交易所快照并确认旧 pos `1001125090990141` 不存在，
查询 binding 325 无活跃 mutation intent，目标/leg/component 与备份行镜像一致。

复用 `apply_management_history_recovery()`，外部 `BEGIN IMMEDIATE` 持有短事务，
Session 使用 `join_transaction_mode=rollback_only`，原函数内部 commit 只 flush，
外部在字段、计数及受保护行校验通过后统一 commit。副本已验证这种事务边界。
事务内 before/validated 时间为 `06:43:56.044937Z` / `06:43:56.108642Z`，
提交后验证为 `06:43:56.152909Z`。

SQLite authorizer 仅允许本步骤预定的 batch/leg 列 UPDATE 与 execution_events INSERT，
拒绝其他表 UPDATE、DELETE 和 schema 修改；提交前再验证精确目标及预期增量。
原函数未接收交易所客户端，只更新本地账本；取证 HTTP 边界显式拒绝非 GET 或有 body 请求。
**此次操作没有提交、取消、修改任何交易所订单**，正常生产交易活动不等同于操作者写入。

## 第二项：preamble 14/15 的精确提交边界

生产事务内再次复核两条均满足：pending、consumed_at/invalidated_at=NULL、
权威 recognition_result=非策略、lifecycle event_type=none、automation skipped/mimo_no_action，
jobs succeeded/attempt=0；candidate、关联 assembly、源消息 assembly attempt 都为 0，
posted_at 已超过 30 分钟；每个 raw 精确只有指定的一条 pending。

无专用 operator CLI，本次用证据目录内最小脚本调用现有
`invalidate_pending_entry_preamble_in_session()`，仅传 raw 14566/14625。
`BEGIN IMMEDIATE` 内先核对 id/raw/完整 before-image，再调用既有函数，
每个 rowcount 必须=1，否则不提交；authorizer 仅允许 entry_preambles 的
status/invalidated_at/updated_at 三列，其他写入拒绝。

事务 before/validated/提交后验证时间为 `06:44:32.111215Z` /
`06:44:32.290037Z` / `06:44:32.325488Z`。
两个 invalidated_at 分别为 `06:44:32.120217Z`、`06:44:32.269337Z`。
其余溯源字段原样保留，preamble 总行数 16 不变，没有其他 pending 行被失效。

## 前后计数与回滚边界

以下在各独立事务内取值，避免将并发业务错算成本次修复。

| 表 | batch 前 → 后 | preamble 前 → 后 |
|---|---|---|
| management batches / legs / components | 158/139/27 → 同值 | 同值 → 同值 |
| execution_events | 3983 → 3984 | 3984 → 3984 |
| entry_preambles | 16 → 16 | 16 → 16 |
| raw_messages / candidates | 14974/2198 → 同值 | 同值 → 同值 |
| bindings / order legs | 339/584 → 同值 | 同值 → 同值 |
| protection legs / ledger | 869/659 → 同值 | 同值 → 同值 |
| mutation intents | 635 → 635 | 635 → 635 |
| recognition decisions / processing jobs | 14971/3213 → 同值 | 同值 → 同值 |

生产未执行回滚。若之后需要撤销，必须针对本次 after-image 与恢复事件 3984
重新绑定证据，仅恢复 batch 153、leg 135 和/或 preamble 14/15 的受影响列；
恢复事件的审计处置须明确授权。不允许用全库备份覆盖在线库或回放历史交易；
回滚会恢复历史告警，不会恢复交易所仓位。并发变更或 after-image 不符即停止。

## Monitor 验证口径

- 本轮按用户允许的“触发一次”执行 `systemctl start telegram-kol-monitor.service`，
  先确认 oneshot 非 active/activating。没有调用 restart/reset-failed，未修改 unit/env/timer。
- 运行 `06:46:02.171347Z–06:46:09.125436Z`；身份 verified=true，release=9501a5f3；
  healthy=true、reason_codes=[]、monitor_error=null、audit_ran=false、notification_status=not_needed。
- 主 service `Result=success, ExecMainStatus=0, ActiveState=inactive` 是 oneshot 完成后的正常状态；
  timer `is-active=active, is-enabled=enabled`。
- 旧 44/44 属于固定历史窗口，不能改写。重新查询自 `2026-09-03T16:02:02Z` 起
  可得主 service journal：处置前 73/73 个带 healthy 字段的结果均不健康；
  处置后总计 74 次、历史不健康仍 73 次，末尾连续不健康由 73 降为 0，最新 1 次 healthy。
  这些是日志可得结果数，不声称覆盖缺失 journal；本次成功是显式触发，不能称为自动定时成功。
- 本轮没有等待更多周期，不把一次恢复证明解释为长期健康保证。

## 第四项 digest：按门禁不移除

前三步完成后 `06:46:42Z` 再次读取实际有效 systemd/env 配置，对
`0de19c1c`、`5aa7ca07` 分别全树校验、前瞻主导入路径验证，
再以 monitor 用户且只有通用 release 变量导入旧身份代码：两者均通过。
标准回滚仍保留当前 canonical unit，仅重写通用 drop-in。
因此“清除专用键使旧 release 必然不可回滚”仍被反证，不满足移除授权条件；
没有移除豁免、修改代码或运行代码候选测试/部署。详见 `digest-recheck.json`。

结束可用空间 `10,675,023,872` 字节（约 9.94 GiB）；备份、演练库均保留，未删除旧证据。
此前 shadow 55/56 召回与 token 增长、锁文件不等于持锁的更正仍保留于已知问题清单。
