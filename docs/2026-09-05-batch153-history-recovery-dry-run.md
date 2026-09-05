# Batch 153 历史收敛 dry-run：等待 apply 授权

## 结论

2026-09-05T06:26:37.332130Z–06:26:40.016264Z，运行中 worker release
`9501a5f39f0c5f196cc29f24f3e3b8786267126b` 的既有
`recover-management-history --batch-id 153` 默认 dry-run 返回：
**`status=ready, decision=terminal_no_submission, reason_code=ready`**。
未执行 apply；本次无生产数据库写入、schema 变更、交易所写入、部署或服务控制。
第一件尚未完成，第二件 digest 移除未进入；上一轮已反证其退役假设，不能默认为已获准移除。

## 新鲜对象与交易所证据

worker 身份接口在 `06:24:40Z` 返回上述 release、manifest
`2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`、
`loaded_artifact_verified=true`。本次没有使用 `/opt` 工作树 HEAD 判断运行版本。

| 对象 | 实测状态 | 原因/次数 |
|---|---|---|
| batch 153 | recovery_required；lifecycle 1054；binding 325；raw 14424 | take_profit_cancel_retry_exhausted；completed_at=NULL |
| component 25 / leg 135 | consume_take_profit_stage；operator_required | take_profit_cancel_retry_exhausted；attempt=3；最后进展及完成于 2026-09-02T09:42:42.266622Z |
| component 26 / leg 135 | converge_partial_close；pending | reason=NULL；attempt=0；最后进展 2026-09-02T09:42:00.304609Z；deadline/completed_at=NULL |
| component 27 / leg 135 | replace_remaining_protection；pending | reason=NULL；attempt=0；最后进展 2026-09-02T09:42:00.304609Z；deadline/completed_at=NULL |
| management leg 135 | planned；entry leg 560；pos 1001125090990141 | client/exchange order ID、last_error 均 NULL |
| entry leg 560 | filled；binding 325；pos 1001125090990141 | attribution_status=verified |

本次复核确定上游终态原因为重试耗尽；三次原始 identity_conflict 的历史根因沿用既有调查，
未重新追查三次响应。binding 325 的 close 类 execution event 查询为 0；
其 position mutation intents 579–583、622 全为 confirmed，没有活跃 mutation intent。
其中 622 指向另一个历史 pos，不混为本次目标。

worker 8002 的 bounded GET 返回 `complete=true, position_count=1, open_order_count=0`，
fingerprint `eb64273c29d5b075d6e162c4f752cb1f931d9f273c5e31e574ec2ea2b218de8d`。
随后既有恢复 CLI 的完整只读 reconciliation snapshot 确认唯一当前 pos 为
`1001125135694798`，**目标旧 pos 1001125090990141 不存在**；open orders=0，
pending triggers=7，errors={}，BTC/ETH/SOL 的 TPSL completeness 均为 true。
这不代表全账户无仓，也不是当前新仓的完整保护审计。

对本 batch：leg 未提交、没有订单 ID、没有 durable close submission，
components 26/27 从未启动、25 已 operator_required。因此没有证据显示本 batch
存在正在执行的交易所动作；7 张全账户 trigger 不能误写为“全部委托为零”。

## 执行方式与零写入边界

复用原 CLI 函数 `cli.recover_management_history()`，显式传入 `apply=False`、batch=153；
没有修改恢复判据。为强制只读，在一次性 stdin Python 中将其 session factory 注入为
SQLite `mode=ro` + `PRAGMA query_only=ON`，关闭 autoflush。
CLI 原 `create_existing_session_factory()` 虽不 bootstrap，但本身不是 mode=ro；
本次加的是诊断连接防写保护，不是生产代码修改。

客户端使用 worker 进程已有环境中的凭据，仅在内存读取、不输出；不加载其他 env 文件。
在 HTTP 请求边界加 GET-only 防护，非 GET 或带 body 一律拒绝。
**原恢复 CLI 自行读取交易所，此部分不是经 worker HTTP 代理转发**；
总计 14 次 GET，交易所写请求 0。用户授权的既有恢复路径未被替换成自定义修复器。
所有 immutable import 均使用 `python -B` / `PYTHONDONTWRITEBYTECODE=1`。

运行 release 与本地审阅的 `management_history_recovery.py` SHA-256 同为
`1c231d5b91834ec093683cdd6b4a14c4d6b192793aff6a921bff7219f69467bd`。
该模块 `55–218` 的 planner 只查询；`222–333` 的 apply 不接收交易所客户端，
只更新选中 batch/legs 并新增一个 execution event。
CLI `4441–4530` 在 apply 前重新读取交易所、重新生成 decision，要求 fingerprint 匹配。
本轮未调用任何 apply。

## dry-run 精确计划与指纹

```json
{
  "mode": "dry_run",
  "batch_id": 153,
  "status": "ready",
  "decision": "terminal_no_submission",
  "reason_code": "ready",
  "evidence_fingerprint": "7b8776f35c9e40e2709d6264ccd5e0461c84fabd69421dd9691c67989e22f7f5",
  "source_fingerprint": "48ef301f9e59a2f9a0302cda82acabe4f2fd1d02255a315b96e9838c863e006a",
  "leg_count": 1
}
```

若另行获准且新鲜证据仍匹配，既有 apply 预计：

- batch 153：recovery_required → resolved；reason → history_no_submission_confirmed；
  设置 reconciled_at/completed_at/updated_at。
- leg 135：planned → failed；last_error → `{"reason":"history_no_submission_confirmed"}`；更新 updated_at。
- execution_events：新增一条 action=management_history_recovery、status=resolved，绑定 fingerprint。
- **component 25/26/27 保持原状，不删除、不重置、不重放。**
  `production_safety_monitor.py:2297–2305` 对父 batch 为 resolved/succeeded/blocked 的
  活跃 component 不再计 stalled，因此终结父 batch 即可消除这个来源的 stalled 告警。
- 不更新 lifecycle、binding、entry leg、保护单或 recognition decision。

不能承诺 apply 后 healthy=true：`06:27:37Z` 只读调用生产现有 invariant reader，
得到 preamble=`[stale_entry_preamble_unresolved]`、composite=`[stalled_composite_component]`。
这是两个专项 reader 的结果，不是完整 monitor diagnostic；其他健康原因需自然运行确认。
只处理 batch 后 preamble 原因仍可能存在，按要求列出并停止，不自行继续清理。

### apply 前必须另行完成（本轮未做）

L3：获得明确 apply 授权后，先验证容量、制作一致性备份并记录 SHA-256、quick_check、
外键检查及关键表计数；在生产副本演练原路径和回滚，不能以本次 dry-run 替代演练。
重新确认目标无活仓/在途动作、重跑 dry-run 并绑定相同 fingerprint，变更即停止。
原指纹未覆盖 component 的全部字段，应额外比对下列精确行镜像，不扩展原 CLI 写入范围。
apply 后逐字段核对预期变化并观察自然 monitor，不手动重启或掩盖 failed 状态。

回滚仅针对本次 batch/leg 变更及新增恢复事件，需按备份镜像和 after-image 精确条件授权；
不得把全库备份覆盖在线库、不得抹掉并发正常业务。回滚会恢复历史告警，不会恢复交易所仓位。
本轮不执行回滚，不执行 apply，不为 dry-run 再创建约 850 MB 的全库备份。

`06:27:18Z` 基线计数（运行中正常业务可变化，apply 前须重取）：

| 表 | 行数 |
|---|---:|
| strategy_management_batches / legs / components | 158 / 139 / 27 |
| execution_events | 3983 |
| entry_preambles | 16 |
| raw_messages / signal_candidates | 14973 / 2198 |
| execution_bindings / execution_order_legs | 339 / 584 |
| execution_running / execution_uncertain | 30 / 3 |

预期本次 apply 自身造成的计数变化：仅 execution_events +1，其余 +0；
不把并发业务新增错算成此次数据修复。
精确行镜像使用 SELECT *、固定 ID 排序、JSON sort_keys/紧凑分隔符/UTF-8 的 SHA-256：

| 对象 | SHA-256 |
|---|---|
| batch 153 | 8cdd49b833c337f665168d43410275082668d3b8a4e57b3ca2012ccff5c16fb4 |
| leg 135 | 0dee52ecaad0d2eda7bbf2492d669796a574d4d25c0119fb7e6e9ad03a672f40 |
| components 25/26/27 | abc1d66f08c5c004fff5bafce9581b32f13d435c1c4c9fd4a7d9b99bb6a9895c |
| preambles 14/15 | 7f806df1381be94a45a62fff524042acaf6b466016faf3b1c3032fa006733a73 |

## Preamble 14/15：精确清单，不执行

| id | raw / chat / message | 标的方向 | created_at（UTC） | 当前状态 |
|---|---|---|---|---|
| 14 | 14566 / -1002344190971 / 14897 | LIT long | 2026-09-03T07:56:34.739755Z | pending；consumed_at/invalidated_at=NULL |
| 15 | 14625 / -1002409877375 / 9143 | BTC long | 2026-09-03T13:19:50.512716Z | pending；consumed_at/invalidated_at=NULL |

两条 updated_at 与 created_at 相同；source posted_at 分别为
`2026-09-03T07:55:56Z`、`2026-09-03T13:15:55Z`，关联 entry_strategy_assemblies=0。
现行 adjacency 以策略消息 posted_at 的前后 30 分钟为边界
（`entry_assembly_admission.py:42,151–158`），两条均不在当前新消息消费窗口。
这不宣称它们在任何历史重放中都无意义；本轮不重放。

建议单独授权 L3 精确失效而非 DELETE：沿
`entry_preambles.invalidate_pending_entry_preamble_in_session():104–121` 的既有
pending→invalidated 语义，仅写 status/invalidated_at/updated_at，保留其余溯源字段。
该函数按 raw_message_id 匹配，执行前必须验证每个 raw 精确只有目标一条 pending，
事务内核对 id/raw/status/updated_at/fingerprint，任一不符整体回滚；总影响必须为 2。
仍需独立备份、副本演练、前后计数、after-image 绑定回滚；不得夹带到 batch 153 apply。

## 顺带观察记录

下列来自已提交观察日志 `0e00a6bb6560b9cc32eb44a22eb05fab6f5ea793` 的
`docs/ai-context-observation-log.md`，窗口为
`(2026-09-03T16:02:02Z, 2026-09-04T16:01:46Z]`，不是本次重新统计：

- shadow attempt 4700 / raw 14889 漏失实质管理决策，55/56=98.21%；
  不再满足实质改变召回 100% 的权威切换门槛，需重新评估；旧权威未被 shadow 替代。
- 主识别 6.375M token/日，较上次实测下界 4.086M 增加 56.03%；
  两窗口 usage 可得率为 99.53% 与 72.32%，不能把差额直接归因于流量或单条成本。
  下次须同时比较消息量、单条 token 与 usage 缺口。
- 更新锁文件存在不代表被持有的误判已更正，标准锁可获取/释放；本轮不删除锁文件。
