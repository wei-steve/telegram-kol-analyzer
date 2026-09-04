# 触发入场保护归属缺陷只读验证

## 结论

**三姐这笔 lifecycle `1050` 与 pos `1001125113096711` 是同一根因。**

这不是从管理 batch 的相同终态反推。lifecycle `1050` 的 trigger protection intent `163`
本身明确记录了：

- `last_reason_code=trigger_protection_candidate_predates_fill`；
- `last_evidence_json.candidate_order_ids=["1001125090080798"]`；
- `adopted_order_id=NULL`；
- `recovery_state=failed`、`retry_attempts=5`、`recovery_disposition=manual_review`。

同一时刻的 `position_attribution_audits.id=3624` 又独立记录了
`event_type=protection_adoption_refused`、相同 reason 和相同 candidate order ID。因此三选一结论是
**“同一根因”**，不是“不同根因”或“证据不足”。

该根因与管理失败的因果链也已闭合：候选止损未获得归属 -> 保护账本和逻辑保护腿没有
可撤换 `ordId` -> raw `14382` 的管理预检返回
`protection_missing_cancellable_order_id` -> batch `152` 在可见性重试后终结为
`protection_visibility_retry_expired`。

## 只读边界与时点

- 生产数据库：`/opt/telegram-kol-analyzer/data/research.db`；所有连接使用
  `file:<path>?mode=ro`，并验证 `PRAGMA query_only=ON` 返回 `1`。
- 远程 Python 调查命令均使用 `python3 -B`；没有从 immutable release 产生
  `__pycache__` 或 `.pyc`。
- 交易所只读证据仅来自 worker `127.0.0.1:8002` 的 GET。
- 活跃 worker 在 `2026-09-04 08:22:50 UTC` 报告 release
  `0de19c1cbb2089fd58b8940d9b01a65096f9a063`、`runtime_role=worker`、
  `loaded_artifact_verified=true`。本次涉及的 6 个源文件与本地同名文件 SHA-256 逐一一致，
  所以本文对代码分支的说明对应当时正在运行的实现。
- 本轮没有修改代码、数据库行或生产设置，没有部署、重启或调用交易所写接口。

## lifecycle 1050 的确切证据链

### 1. 入场提交和附带止损意图

| 记录 | 确切字段 |
| --- | --- |
| lifecycle / binding / leg | lifecycle `1050`；binding `324`；entry leg `559` |
| 父触发单 | `1001125090052318`；client order ID `TKSJ1173E1` |
| 成交 child / position | `1001125090080799`；leg 的 `attribution_status=verified` |
| `execution_events.id=3892` | `action=create_trigger_entry`、`status=submitted`、`created_at=2026-09-02 02:33:30.866384 UTC` |
| 提交请求 | `instId=BTC-USDT-SWAP`、`posSide=long`、`sz=21.0`、`slTriggerPx=76300.0`、`slOrdPx=-1` |
| 父单回包 | `code=0`、`data.sCode=0`、`data.ordId=1001125090052318`、`data.sMsg=Success` |
| binding 的 submitted order | `protection_request.slTriggerPx=76300.0`；`protection_response.code=0`；`data.attached_on_trigger_order=true` |
| intent `163` | `created_at=2026-09-02 02:33:30.714985 UTC`；`parent_trigger_order_id=1001125090052318`；提交前 TPSL 基线为 `[]` |

必须注意：当前提交实现是在带保护参数的 `trigger_order()` 父单成功返回后，由本地
路径持久化 `attached_on_trigger_order=true`。该标记可以证明这一份父单提交确实携带了
`slTriggerPx`，但它不含子止损 `ordId`，因此不能单独证明某个 TPSL 候选就属于该父单。

### 2. 交易所原生止损出现在仓位投影之前

候选止损 `1001125090080798` 的归一化行为：

- `instrument=BTC-USDT-SWAP`；
- `side=long`；
- `size=21`；
- `stop_loss_trigger_price=76300`；
- `take_profit_trigger_price=0`；
- `exchange_created_at=1788316588000` = `2026-09-02 02:36:28 UTC`；
- `exchange_updated_at=1788316596000` = `2026-09-02 02:36:36 UTC`。

这些字段保存于后续 intent `164` 的完整提交前 TPSL 基线中；更早的
`pending_tpsl_snapshot_observations.id=1188581` 已经在
`2026-09-02 02:36:31.919943 UTC` 以 `complete=1`、`response_count=2` 看到该 order ID。

与之相对，仓位 `1001125090080799` 的首次归属验证在
`position_attribution_audits.id=3623`，时间为
`2026-09-02 02:36:38.252094 UTC`，证据是
`evidence_source=trigger_child_order`、`evidence_type=direct_pos_id`、
`rank.time_distance_ms=8000`。同一时间的 audit `3624` 立即拒绝了止损候选：

```json
{
  "event_type": "protection_adoption_refused",
  "pos_id": "1001125090080799",
  "evidence_json": {
    "candidate_order_ids": ["1001125090080798"],
    "reason": "trigger_protection_candidate_predates_fill"
  },
  "created_at": "2026-09-02 02:36:38.252094"
}
```

这与已知 pos `1001125113096711` 的序列一致：父触发单携带止损，原生止损先生成，
后来的 live-position `cTime` 门把它排除。

还有一组比 live-position 投影时间更强的血缘佐证：

- lifecycle `1050` 的原生止损 `1001125090080798` 交易所
  `cTime/uTime` 为 `02:36:28/02:36:36 UTC`；先前已保存的 worker 8002 order-history GET
  显示它的唯一 child regular order `1001125090080799` 创建/更新时间也是
  `02:36:28/02:36:36 UTC`。
- lifecycle `1072` 的原生止损 `1001125113096710` 交易所
  `cTime/uTime` 为 `15:52:12/15:52:25 UTC`；本次 worker 8002 order-history GET 显示
  child regular order / pos `1001125113096711` 的创建/更新时间同样是
  `15:52:12/15:52:25 UTC`。

这个精确相等关系与“交易所在 child 成交时创建 attached stop”一致。它仍不能单独代替归属；
设计中只会将它作为父单提交证言、owner-specific 基线、精确形状和账户级双向唯一之上的一条必要边条件。

### 3. 归属拒绝后的保护收敛中断

intent `163` 最终值：

```text
recovery_state=failed
retry_attempts=5
recovery_disposition=manual_review
adopted_order_id=NULL
last_reason_code=trigger_protection_candidate_predates_fill
last_evidence_json.candidate_order_ids=[1001125090080798]
updated_at=2026-09-02 03:52:10.496475 UTC
```

binding `324` 的三条逻辑保护腿在 `2026-09-02 02:36:41 UTC` 全部进入
`protection_recovery_pending`：

| protection leg | role | 计划 | `exchange_order_id` |
| --- | --- | --- | --- |
| `761` | `primary_stop` | `76300`, size `21` | `NULL` |
| `762` | `backup_stop` | 待根据主止损计算 | `NULL` |
| `763` | `take_profit` | `78970`, 100% | `NULL` |

该 binding 没有 `position_protection_ledger` 记录，没有 `position_backup_stop_orders`，
没有 `position_take_profit_orders`，也没有上述保护的 `position_mutation_intents`。
`trigger_take_profit_convergences.id=202` 一直是
`status=waiting_backup_stop`、`reason_code=convergence_waiting_backup_stop`。

### 4. raw 14382 的管理失败正好依赖这个缺失 ID

- raw `14382` 的 `posted_at=2026-09-02 03:32:22 UTC`。与本次核对相关的原文是：
  “比特币多单止盈50%，止损位移动至开仓价！”本文不引用其中的无关联系信息。
- candidate `2147` 已成功生成并接纳；目标是 lifecycle `1050` / binding `324`。
- instruction item `927` 于 `2026-09-02 03:34:51.957299 UTC` 生成，随即为
  `status=failed`，`error_json.reason=protection_missing_cancellable_order_id`。
- management batch `152` 的 `target_snapshot_json.blocked_reason` 也是
  `protection_missing_cancellable_order_id`，且 `positions=[]`；六次可见性重试后于
  `2026-09-02 03:41:07.996322 UTC` 终结为
  `status=blocked`、`reason_code=protection_visibility_retry_expired`。
- 没有 management leg，没有部分平仓或改止损的 execution event / mutation intent。

现有 management planner 要求保护账本中的 order ID 与当前完整 TPSL 快照中唯一可见的
order ID 一致，才取得取消/替换能力。当前并不是“有一张同价格止损就放行”。
因此 intent `163` 未认领 `1001125090080798` 直接导致 planner 无法得到可撤换的精确 ID。

## 对照组：归属成功后现有收敛链确实会继续

binding `336` 的第二条腿 `578` 是同一运行实现下的有效对照：

- intent `178` 认领主止损 `1001125115150881`，`recovery_state=adopted`；
- ledger `648` 将该止损唯一绑定到 leg `578` / pos `1001125115150882`；
- 随后产生并回读确认备用止损 `1001125115153805`；
- 随后产生并回读确认两档止盈 `1001125115156648` / `1001125115156965`；
- 三个交易所写入都有独立、确定的 `position_mutation_intents.idempotency_key`。

这证明归属修复的直接收益不只是 Web 显示：它会解锁现有备用止损和止盈收敛前置。

## 当前暴露和存量分类

截止 `2026-09-04 08:19:40.760895 UTC`，worker 8002 的当前持仓页面只有一个与本缺陷存量无关的
BTC long pos `1001125123045253`，显示 3 档止盈、2 条止损，5 条都是已验证归属。
`/api/runtime-agent/read-only-exchange-snapshot` 另外返回
`complete=true`、`position_count=1`、`open_order_count=0`。

全历史七条 `trigger_protection_candidate_predates_fill` intent 中：

- 六条已是 `failed/manual_review`；
- 一条是 `retrying` 但其 binding 已关闭；
- 七条的 binding 现均为 `closed`，腿为 `closed` 或 `manually_closed`；
- 当前 worker 唯一实盘 posId 不在这七条中。

因此本次只读时点没有发现这七条存量对应的当前实盘仓位；它们当前是历史记录，
不得因未来修复而自动重放、补单或补写归属。

## 可观测性现状

- 首次确定性拒绝 audit `3624` 在 `02:36:38 UTC` 已写入，但
  `notification_status=pending`、`notified_at=NULL`。
- runtime incident `2014` 只在 intent 于 `03:52:10 UTC` 进入 `failed/manual_review`
  后创建，理由为 `trigger_protection_candidate_predates_fill`；它在
  `04:01:14 UTC` 才 `notification_status=delivered`。

从首次拒绝到送达终态告警约 84 分 36 秒；在此之前，管理消息已因缺少可撤换 ID 失败。
这证明现有告警太晚，而且 Web 的“暂无已验证交易所止盈止损”无法替代主动通知。

## 证据充分性说明

本文没有把“同样的 batch 终态”当成根因证据。定性依据是同一个 intent 和同一条 audit 中的精确
reason code + candidate order ID，并由以下序列补强：提交前基线为空 -> 父单带精确
`slTriggerPx` 成功提交 -> 完整交易所 TPSL 快照出现形状完全一致的新止损 ->
仓位归属验证同刻以 `candidate_predates_fill` 拒绝该 ID -> 账本/逻辑腿没有 ID ->
管理预检以缺少可撤换 ID 阻断。
