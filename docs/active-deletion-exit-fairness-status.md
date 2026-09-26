# 活跃态删除退出的调度公平（L1）· 状态

日期：2026-09-26
分支：`active-exit-starvation-and-visibility`（基线 `origin/main` = `8cb05a03`）
设计稿：`docs/plans/2026-09-26-active-deletion-exit-selfheal-design.md` 第 4 节 L1（用户已批准 L1+L2，**L3 明确不做**）
前序：`docs/oncall-d6-silent-stall-rules-status.md`（D6a 已能看见五个封锁态）
状态：**代码 + 用例完成，全量测试绿，未部署、未推送。**

本文件是 L1 这件事跨会话唯一的进度真相。

---

## 一句话

`source_message_deletion_worker._claim_next_job` 的候选排序从「id 升序」换成
「**从没被认领过的优先 → 最久没动过的优先 → id 兜底**」，
一条活跃的删除退出不会再因为 id 太大而永远轮不到。

## 落点

| 文件 | 改了什么 |
|---|---|
| `src/telegram_kol_research/source_message_deletion_worker.py` | `_claim_next_job` 的 `.order_by(...)` 三个键 + 一段说明为什么这么排的 docstring。**没改别的东西**：WHERE 子句、5 分钟租约、`LIMIT 20`、CAS 的 UPDATE 一个字都没动 |
| `tests/test_source_message_deletion_worker.py` | `timedelta` / `_claim_next_job` 两个 import，三个夹具函数，六条用例 |

改动就是这一处排序：

```python
.order_by(
    (SourceMessageDeletionExit.attempt_count == 0).desc(),
    SourceMessageDeletionExit.updated_at.asc(),
    SourceMessageDeletionExit.id.asc(),
)
```

## 为什么这么排

### 键 2（`updated_at` 升序）本身就是轮转，不需要额外的退避机制

指挥提示让我自己复核这个结论。**复核结果：成立。** 逐条查过所有会写这一行的路径，
没有一条会把 `updated_at` 写回旧值：

| 路径 | 写的值 |
|---|---|
| `_claim_next_job` 的认领 CAS（worker:1378） | `updated_at=claimed_at`，即本 tick 的 `now` |
| `_transition_claimed`（worker:1405） | `updated_at=<调用方传的 now>` |
| `_mark_reconciliation_waiting`（worker:1192） | `updated_at=reconciled_at`；**七个调用点全部传 `reconciled_at=now`**（worker:716/952/1034/1046/1066/1081/1094） |
| worker 里六处直接放回（worker:877/897/920/937/1021/1104） | `deletion_exit.updated_at = now` |
| `source_deletion_exit_timeout._release`（timeout:624） | `updated_at: released_at` |

所以「被认领 → 自动排到队尾」是数据本身的性质，不是靠额外机制维持的。
一条反复失败的行每轮都会把自己推到队尾，**不可能独占 `max_jobs`**，
设计稿第 4 节担心的「退避那一半不能省」在这份代码上不成立。

### 键 1 必须是 `attempt_count == 0`，不能是 `claimed_at IS NULL`

**这是我对本轮提示的第一处纠正。** 提示建议第一个键用「`claimed_at IS NULL` 的排前（从没被认领过的）」。
`claimed_at IS NULL` 不等于「从没被认领过」：**每一条放回路径都会把 `claimed_at` 写回 `None`**
（上表里除认领 CAS 以外的全部路径）。所以一条被认领又放回一万次的行，`claimed_at` 照样是 NULL。
按那个键排，几乎所有空闲的活跃行都会落进「优先桶」，桶永远不会排空，
这个键退化成无效——更糟的是它会把**唯一真正 `claimed_at` 非空的那一类**
（进程猝死、租约已过期 5 分钟的行）排到全部行的后面，正好是最该赶紧接手的那一类。

真正表示「从没被认领过」的字段是 `attempt_count`：
全仓只有两处写它（`source_message_deletion.py:263` 建行时 `attempt_count=0`、
`worker:1378` 认领时 `+1`），**没有任何地方把它重置**。所以
`attempt_count == 0` ⟺ 这一行从来没被 worker 摸过。

这个桶按构造会排空：一次认领就 `+1`，此后永久离开桶。所以它不会饿死键 2 那一侧——
一批 K 条新退出最多把老行推迟 `ceil(K / max_jobs)` 轮。

### 为什么需要键 1

一条刚记录的删除退出 `created_at = updated_at = now`，是全表最新的 `updated_at`，
纯键 2 会把它排到最后。而新退出恰恰是最急的：它要去撤原策略的入场单。
`test_a_brand_new_exit_is_claimed_before_a_pile_of_older_active_rows` 钉住这一条。

### 键 3（id）

同一 tick 内被认领的行共享同一个 `updated_at`，需要一个稳定的 tiebreak。

## 饥饿的真实门槛不是 20，是 `max_jobs`

**这是我对本轮提示与设计稿的第二处纠正。** 两处都写「活跃行超过 20 条时高 id 的行进不了候选窗口」。
`LIMIT 20` 只是第二层。真正的门槛是 `max_jobs=10`：

`_claim_next_job` **每认领一个 job 就重新查一次候选**，且循环遇到第一个 CAS 成功就 `return`。
所以旧排序下它每次返回的都是**当前最小的可认领 id**；tick 内已处理的用 `excluded_exit_ids` 排除，
于是一个 tick 恰好处理 id 最小的 10 条。下一个 tick 从头再来，还是那 10 条。
**只要有 11 条以上持续可认领的活跃行，第 11 条就永远轮不到**，与 `LIMIT 20` 无关。
陈哥那条 lane 被封 11 天的形状里，这一层会让一处卡死连带封住后面所有 lane。

## 查询计划：实测，新旧一字不差

在用真实 SQLAlchemy 元数据建的库上跑 `EXPLAIN QUERY PLAN`
（脚本 `python -B`，临时库，没碰生产库）：

```
OLD: ORDER BY source_message_deletion_exits.id ASC LIMIT 20
  PLAN: SEARCH source_message_deletion_exits USING INDEX ix_source_message_deletion_exits_state (state=?)
  PLAN: USE TEMP B-TREE FOR ORDER BY

NEW: ORDER BY source_message_deletion_exits.attempt_count = 0 DESC,
              source_message_deletion_exits.updated_at ASC,
              source_message_deletion_exits.id ASC LIMIT 20
  PLAN: SEARCH source_message_deletion_exits USING INDEX ix_source_message_deletion_exits_state (state=?)
  PLAN: USE TEMP B-TREE FOR ORDER BY
```

对照的另外两种写法（都测了，都一样）：

```
ALT: ORDER BY updated_at ASC, id ASC（不分桶）
  PLAN: SEARCH ... USING INDEX ix_source_message_deletion_exits_state (state=?)
  PLAN: USE TEMP B-TREE FOR ORDER BY

提示建议的 claimed_at IS NULL DESC, updated_at ASC, id ASC
  PLAN: SEARCH ... USING INDEX ix_source_message_deletion_exits_state (state=?)
  PLAN: USE TEMP B-TREE FOR ORDER BY
```

**这是我对本轮提示的第三处纠正。** 提示说「现在的 `ORDER BY id` 走主键，改排序后可能引入临时
B 树排序」。实测：**旧写法已经在用临时 B 树**。SQLite 拿 `state IN (...)` 走
`ix_source_message_deletion_exits_state`（= `(state, updated_at)`）做索引 seek，
然后为 `ORDER BY id` 建临时 B 树。所以新排序的**增量代价是零**，不是「可以忽略」——
计划两行逐字相同。这个成本本来就一直在付。

（顺带：新排序里 `updated_at` 是索引的第二列，理论上 SQLite 有机会用索引的天然顺序，
但因为第一个键是 `attempt_count` 的表达式，它没有这么做。不影响结论。）

## 明确没做

- **没碰 `source_deletion_exit_timeout` 的释放逻辑**（设计稿 L3 本轮不做，一行都没碰）。
- 没碰 WHERE 子句、5 分钟租约、`LIMIT 20`、认领的 CAS UPDATE、`max_jobs`。
- 没碰 barrier、没碰 `_ACTIVE_STATES` 的成员。
- 没顺手重构别的东西：这是交易路径（它驱动撤单/平仓的认领顺序）。
- 没碰生产库、没部署、没推送。

## 测试

新增六条（`tests/test_source_message_deletion_worker.py` 末尾一节）：

| 用例 | 钉住什么 |
|---|---|
| `test_claim_order_reaches_every_active_row_in_bounded_rounds` | 25 条持续活跃的行，模拟三个 tick（每 tick 10 个 job、每个 job 按真实放回路径放回），**25 条全部被认领**，包括最大 id |
| `test_claim_order_does_not_starve_the_highest_id_across_many_ticks` | 21 条行，记下每条第一次被认领的轮次，**最大轮次 ≤ 2**（21/10 → 第三轮封顶） |
| `test_a_brand_new_exit_is_claimed_before_a_pile_of_older_active_rows` | 15 条老活跃行 + 一条 `created_at = now` 的新退出，第一个认领的是新退出，且 `pending` 被提为 `cancelling_entries` |
| `test_the_never_claimed_bucket_drains_after_one_claim` | 新退出先被认领，`attempt_count` 变 1，**下一轮换成老行**——优先桶会排空 |
| `test_a_claim_lease_still_lasts_exactly_five_minutes` | 4 分 59 秒时抢不走、满 5 分钟能抢走且换了新 token（租约行为不变） |
| `test_a_claim_lost_to_another_worker_is_not_returned_twice` | 在「读候选」与「CAS」之间插入另一个 worker 的认领（包装 `session_factory`，第二次开 session 时下手），`_claim_next_job` 返回 `None`，别人的 token 与 `attempt_count` 都没被破坏（CAS 保护不变） |

**反向验证（证明用例钉的是对的东西）**：把排序临时改回 `ORDER BY id` 再跑这六条：

```
FAILED test_claim_order_reaches_every_active_row_in_bounded_rounds
FAILED test_claim_order_does_not_starve_the_highest_id_across_many_ticks
FAILED test_a_brand_new_exit_is_claimed_before_a_pile_of_older_active_rows
FAILED test_the_never_claimed_bucket_drains_after_one_claim
4 failed, 2 passed, 50 deselected in 0.83s
```

四条公平性用例在旧排序下红、租约与 CAS 那两条在新旧排序下都绿——正是想要的形状
（后两条是回归护栏，不是新行为）。

删除退出这条线的三个文件：

```
tests/test_source_message_deletion_worker.py tests/test_source_message_deletion.py
tests/test_stuck_deletion_exit_selfheal.py
-> 86 passed in 7.44s
```

全量（`uv run pytest -q`，L1 树，未含 L2）：

```
9648 passed, 4 skipped, 109 warnings in 783.80s (0:13:03)
```

基线 `origin/main`（`8cb05a03`）为 9642 条；L1 **+6 条**。
**没有改任何既有用例**（既有 50 条在新排序下全绿）。

## 上线前要重新数一遍的量

L1 不建案、不写状态，所以没有「首轮开案量」。要数的是**这个改动有没有东西可改善**，
也就是生产上现在有几条活跃的删除退出。

**在 `VACUUM INTO` 出来的快照上跑，不要碰在跑的库**（见 memory: no heavy scans on prod DB）。
两条都走 `ix_source_message_deletion_exits_state` 的 `state IN` 等值 seek，有界。

```sql
-- 第一条：现在有几条活跃行，最老的一条多久没动过、被认领过几次。
-- 大于 10 条就意味着旧排序正在饿死一部分行。
SELECT state, COUNT(*) AS n,
       MIN(updated_at) AS oldest_updated,
       MIN(created_at) AS oldest_created,
       MAX(attempt_count) AS max_attempts
FROM source_message_deletion_exits
WHERE state IN ('pending', 'cancelling_entries', 'closing_positions', 'reconciling')
GROUP BY state
ORDER BY n DESC;

-- 第二条：有没有行正处在「从没被认领过」的桶里（键 1 会先服务它们）。
SELECT COUNT(*) AS never_claimed
FROM source_message_deletion_exits
WHERE state IN ('pending', 'cancelling_entries', 'closing_positions', 'reconciling')
  AND attempt_count = 0;
```

读数怎么用：

- **两条都是 0**（`df0a54ab` 部署当天的实测形状：全库只有 `succeeded` 283 + `unbound` 91）
  → L1 是纯预防，上线没有可观察的行为变化，这也是合格结果；
- **第一条加起来 ≤ 10** → 旧排序还没开始饿死任何行，L1 依然是预防；
- **第一条加起来 > 10** → 现在就有行被饿着。上线后应该看到 `attempt_count`
  在这批行上开始普遍增长（尤其是 id 最大的那几条），这是 L1 生效的直接证据。

上线后的观察点（不需要新工具）：D6a 的 `source_deletion_exit_stalled_lane`
案子数应当**不增加**。L1 让活跃行都能被认领，但它**不会**让一条卡在原地打转的行前进——
那是 L2 要看见的事，也是设计稿 L3（本轮不做）要处理的事。

部署路径与 `AGENTS.md` 相同；**本改动在 worker 里，落在 `tg-deploy` 的重启清单内
（worker → web → ingest），不需要像值守那样额外 restart。**
