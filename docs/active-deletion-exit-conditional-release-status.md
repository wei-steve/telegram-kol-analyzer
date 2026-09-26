# 活跃态删除退出的有条件释放（L3）· 状态

日期：2026-09-26
分支：`active-exit-conditional-release`（基线 `origin/main` = `b345d2ba`，即 L1+L2 上线后的生产 HEAD）
设计稿：`docs/plans/2026-09-26-active-deletion-exit-selfheal-design.md` 第 4 节 **L3**（用户已批准 L1/L2/L3 全部三级）
前序：`docs/stuck-deletion-exit-selfheal-status.md`（`recovery_required` 的同形状自愈，已上线 `0041ae06`）、
`docs/active-deletion-exit-fairness-status.md`（L1）、`docs/oncall-d6-silent-stall-rules-status.md` 末节（L2）
案例：`docs/2026-09-26-silent-stall-case-note.md`
状态：**代码 + 用例完成，全量测试绿，未部署、未推送。**

本文件是 L3 这件事跨会话唯一的进度真相。

---

## 一句话

`expire_stuck_source_deletion_exits` 以前只扫 `recovery_required`；现在四个活跃态
（`pending` / `cancelling_entries` / `closing_positions` / `reconciling`）超过 **6 小时**没完成时
也会被判一次，**五条判据全成立**才把它收口成 `succeeded`，`last_reason` 写第三个常量
`released_active_no_exchange_footprint`。不撤单、不平仓、不碰交易所写。

## 落点

| 文件 | 改了什么 |
|---|---|
| `src/telegram_kol_research/source_deletion_exit_timeout.py` | 新常量 `ACTIVE_STATES` / `ACTIVE_STATE_STUCK_AFTER` / `ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON` + 四个"为什么没放"的原因常量；`_candidates` 多一条 OR 分支并多返回 `created_at`/`claim_token`/`claimed_at`；主循环多一条 `active` 岔路；新函数 `_judge_active_candidate`、`_claim_lease_is_free`、`_claim_lease`、`_release_unclaimed_active`；`_capture_stuck` 把 `timeout_minutes` 也交给注入的 capture |
| `src/telegram_kol_research/source_message_deletion_worker.py` | `_claim_next_job` 里的字面量 `timedelta(minutes=5)` 提成模块常量 `CLAIM_LEASE`（**纯提取，语义不变**，唯一的使用点还是原处），L3 从这里读同一个租约 |
| `src/telegram_kol_research/runtime_incident_adapters.py` | `capture_source_deletion_exit_stuck` 的 docstring 补一段：`state` 现在也可能是活跃态、`timeout_minutes` 那时是 360 |
| `tests/test_active_deletion_exit_conditional_release.py` | 新文件，23 条 |

**一行既有代码的判断逻辑都没改**：`recovery_required` 那条路（`_release`、
`_no_exchange_footprint_verdict`、`_lane_identity`、`_unattributed_lane_rows`、
`_should_capture`、30 分钟节流）逐字保留，活跃态走的是主循环里一条独立岔路。

## 释放判据：五条，全成立才放，任何一条不确定都判不放

前三条**完全复用**已上线的 `_no_exchange_footprint_verdict`（一个字没改）：

1. **无凭据**：`execution_binding_id IS NULL` 且该 binding 名下没有任何 `pos_id` / `order_id`
   （`_has_no_execution_credentials`）——我们从来没为这条消息在交易所放过任何东西；
2. **本轮交易所读取成功**（`ExchangeLaneFootprint.read`）。读失败 / 读不出 instId / 持仓数量解不出
   一律算"还在"，"不确定"永远不能当证据花；
3. **lane 内全部有主**：该 lane（chat + symbol + side）里每一个在仓持仓、每一张挂单，
   都能按 `ExecutionOrderLeg.pos_id` / `order_id` 精确查到**别的** `(chat_id, message_id)` 的绑定。
   有一张查不到 → 那可能正是这条退出该负责的孤儿 → 继续封着。

活跃态专属的两条（新增）：

4. **当前没有人正在做它**：`claim_token IS NULL`，或 `claimed_at <= now - CLAIM_LEASE`（租约已过期）。
   并且释放是一条**带 claim 条件的 CAS**（`_release_unclaimed_active`）：
   判定时看到无认领 → UPDATE 要求仍然无认领；判定时看到的是过期租约 → UPDATE 要求**同一个 token**
   且租约仍然过期。CAS 输掉就写不进任何字段，本轮记 `active_release_lost_the_race`，下一轮重判。
5. **年龄横杆 6 小时**（`ACTIVE_STATE_STUCK_AFTER`），量的是 `created_at`（"多久没完成"）。

### 为什么活跃态非要第 4 条守卫

`recovery_required` 不在 worker 的 `_ACTIVE_STATES` 里，**永远不会再被认领**——所以对它来说
"有没有人正在做"这个问题不存在，`_release` 只按 `id + state` 更新就够了。
四个活跃态恰恰相反：worker 每 5 秒一轮都可能认领它们，认领是一条带 5 分钟租约的 CAS，
认领成功会把 `pending` 提成 `cancelling_entries` 并写入新 token。

如果沿用 `id + state` 的释放：一条正被 worker 持有的 `reconciling` 行会在 worker
**正在向交易所对账的中途**被清扫器改成 `succeeded`。那之后 worker 的
`_transition_claimed`（它按 `claim_token` 过滤）会写不进去、错误被吞掉，而 lane 已经放开、
后面的消息已经开始入场——**同一条 lane 上会同时存在"正在平仓的动作"和"刚被放进来的新入场"**。
第 4 条的两半各挡一种时序：租约判据挡住"判定的时候人家就在做"，
CAS 挡住"判定之后、写入之前人家插进来"。

顺带一个实测到的好处（用例 `test_a_live_claim_never_loses_its_row` 的反向验证）：
把第 4 条的租约判据整段删掉，CAS 自己仍然拒绝了那次释放。两层是独立的，不是一层的注释。

### 为什么年龄量的是 `created_at`，而这不是第三个阈值

`updated_at` 只会向前走，初值等于 `created_at`，所以 `updated_at <= cutoff` 蕴含
`created_at <= cutoff`。也就是说单用 `created_at <= now-6h` 就等于 D6a 对活跃态的那个并集
（"没动过" ∪ "没完成过"），**不是另一套语义，而是同一个判据的最简写法**；
而且它是能覆盖 C2（每 5 秒被认领一次、永远回到同一个状态）的那一半。

`source_deletion_exit_timeout_minutes`（生产 120 分钟）保持原样，只管 `recovery_required`：
那个状态已经没有主人，不需要宽限期；活跃态按定义还是某个 worker 的工作，所以用更宽的 6 小时，
且与 D6a / `case_stale_after` 同一个数字。**告警里的 `timeout_minutes` 对活跃态报 360**，
否则那行字会拿 120 分钟去描述一条按 6 小时判的行。

## `last_reason` 三条释放路径怎么分辨

| `last_reason` | 谁写的 | 含义 | 前提 |
|---|---|---|---|
| `position_gone_confirmed` | `_release`（既有） | 凭 `pos_id` 向交易所证明**这个**仓位与它的挂单都不在了 | 只走 `recovery_required`，**有**凭据 |
| `released_no_exchange_footprint` | `_release`（既有） | 一条再也不会被认领的死行，lane 里每张单都归别人 | 只走 `recovery_required`，**无**凭据 |
| `released_active_no_exchange_footprint` | `_release_unclaimed_active`（L3 新） | 一条**还能被认领、只是永远不完成**的行，lane 里每张单都归别人，且当时没人持有它 | 只走四个活跃态，**无**凭据 |

三者互斥且不重名，事后一条 `SELECT state, last_reason` 就能说出是哪条路径放的。
另外四个字符串（`active_exit_is_claimed`、`active_exit_has_execution_credentials`、
`active_lane_judgement_throttled`、`active_release_lost_the_race`）只出现在**告警的
`release_reason` 字段**里，说明"为什么没放"，**从不写进 `last_reason`**。

## 明确没做（设计稿与提示的禁令，逐条对应）

- **不自动撤单、不自动平仓**：本轮没有任何新的交易所写调用；
- **不释放带凭据的退出**：活跃态第一条判据就是无凭据。顺带核实过一件事——
  `closing_positions` 只有在 `execution_binding_id` 存在时才可能到达
  （`worker:535` 的 `owned_positions` 按 binding_id 查 leg，binding 为空时转的是 `reconciling`），
  所以**一条真的在平仓途中的退出必然带凭据、必然被第一条判据挡住**。
  L3 实际可能释放的只有无 binding 的 `pending` / `cancelling_entries` / `reconciling`；
- **不恢复任何 `deferred_expired` 的消息**：释放后照常 `_resume_behind_exit`，而 resume 只认
  `waiting_source_deletion_exit`，用例
  `test_releasing_an_active_lane_resumes_the_waiting_but_never_the_expired` 钉住；
- 没动 `source_execution_barrier` 的 `state != 'succeeded'` 规则；
- 没动 L1 的认领排序（只把它里面的 `timedelta(minutes=5)` 提成常量，值与用法不变）；
- 没动 L2 / D6a / D6b / D6c 的任何判据；
- 没改任何既有用例；没碰生产库、没部署、没推送。

## 设计稿没写、我自己定的取舍

1. **候选查询合成一条，不加第二条 SQL。** 这条 pass 每 5 秒跑一次，多一条语句就是多一次
   几乎总是空手而归的读。实测 `EXPLAIN QUERY PLAN`（`python -B`，SQLAlchemy 元数据建的临时库，
   没碰生产库）：

   ```
   MULTI-INDEX OR
   INDEX 1  SEARCH ... USING INDEX ix_source_message_deletion_exits_state (state=? AND updated_at<?)
   INDEX 2  SEARCH ... USING INDEX ix_source_message_deletion_exits_state (state=?)
   USE TEMP B-TREE FOR ORDER BY
   ```

   `recovery_required` 那一支的计划**与改动前逐字相同**；新那一支是按 state 的索引 seek
   再过滤 `created_at`（索引是 `(state, updated_at)`，`created_at` 进不了 seek）。两支都不扫表。
   没有给候选查询加 `LIMIT`：既有那一支本来就没有，加上会改变 `recovery_required` 的既有行为。
2. **另写 `_release_unclaimed_active`，不给 `_release` 加参数。** 它的 WHERE 严格更大
   （本轮判定的那个 state + 本轮看到的那个 claim + 那个 claim 仍然可释放），
   把两种形状塞进一个函数会让守卫条件取决于参数，那是以后被顺手放宽的形状。
   分开之后，两条已验证的老路径的 UPDATE 语句不会因为 L3 的任何改动而变。
3. **活跃态候选不再问 per-exit 的 `ExchangeAbsenceProof`。** 那个证明对活跃态没有用
   （L3 永不释放带凭据的行），而问一次就会让**带凭据的活跃行每 5 秒摸一次交易所快照**，
   绕过那个专门为此存在的节流。所以主循环对活跃态直接跳过 `exchange_reader(...)` 调用。
   净效果是交易所读取比改动前**更少**，而不是更多；lane 判定仍然只在"本轮本来就要发声"的 pass 上跑。
4. **5 分钟租约提成 `CLAIM_LEASE` 并由 L3 读它**（函数内延迟 import，因为 worker 在 import 期
   反向依赖本模块）。否则"五分钟"会有第二个副本，而这两个数字必须一致才安全。
5. **`timeout_minutes` 也传给注入的 capture**（既有调用方全是 `**kwargs`，兼容）。
   否则"告警报的是哪条横杆"没法在用例里断言。
6. **活跃态的"held"也会告警**（不只是释放时）。这是既有契约"Alert always"自然延伸到新候选集的结果：
   一条被封 6 小时以上的活跃 lane 值得说一声。副作用是同一件事现在有两处发声——
   值守 D6a 的案子 + worker 的 `source_deletion_exit_stuck` 告警。频率由已上线的 30 分钟节流管。
   生产当前活跃态为 0 行，所以首轮量预期是 0，见下。

## 测试清单与结果

`tests/test_active_deletion_exit_conditional_release.py`，23 条，全绿：

**三个不能漂的数字**
- `test_the_active_state_list_is_the_worker_s_own`：`ACTIVE_STATES` == worker `_ACTIVE_STATES` == `SEALED_LANE_ACTIVE_STATES`
- `test_the_age_bar_is_d6a_s_one_bar`：`ACTIVE_STATE_STUCK_AFTER` == `SEALED_LANE_STUCK_AFTER` == 6h
- `test_the_claim_lease_is_the_worker_s_own`：`_claim_lease()` == worker `CLAIM_LEASE` == 5min

**判据 1–3（复用的三条）**
- `test_an_unclaimed_active_exit_over_an_unowned_lane_is_released[4 个状态]`：五条全成立 → 释放，
  `last_reason=released_active_no_exchange_footprint`，`claim_token`/`claimed_at` 清空、
  `completed_at` 落值，仍 capture 一次且 `lane_released=True`、`timeout_minutes=360`
- `test_an_unattributed_position_keeps_an_active_lane_sealed`：lane 里有**无主持仓** → 不释放，
  状态与 `last_reason` 一字未动
- `test_an_unattributed_resting_order_keeps_an_active_lane_sealed`：**无主挂单** → 不释放
- `test_a_failed_exchange_read_never_releases_an_active_lane`：**读失败** → 不释放（`lane_read_failed`）
- `test_an_active_exit_whose_lane_cannot_be_named_is_left_sealed`：没有候选 → `lane_identity_unknown`

**判据 4（认领守卫 + CAS）**
- `test_a_live_claim_never_loses_its_row`：租约差 1 秒未过期 → 不释放、**一个字段都没写**、
  worker 的 token 完好，且**一次交易所读取都没发**（租约判据在 lane 读之前）
- `test_a_claim_whose_lease_has_expired_is_releasable`：正好 5 分钟 → 可释放（守卫的另一半）
- `test_the_release_loses_the_cas_to_a_worker_claiming_the_same_row`：**并发用例**。
  在判定与 CAS 之间插入一次真实 `_claim_next_job`（包装 session_factory，第 4 次开 session 时下手），
  断言：worker 赢、`released == ()`、原因 `active_release_lost_the_race`、
  行仍是 `reconciling` 且带 worker 的 token、`completed_at` 仍为 NULL —— **两者不会同时成功**
- `test_a_released_row_is_no_longer_claimable_by_the_worker`：反向时序，清扫器先赢 →
  `_claim_next_job` 返回 `None`

**判据 5（6 小时）**
- `test_an_active_exit_is_judged_on_six_hours_not_on_the_stuck_timeout`：差 1 分钟时**连候选都不是**
  （结果是空对象），过 1 分钟后释放——横杆卡在正好 6 小时
- `test_an_active_exit_well_inside_the_stuck_timeout_is_also_left_alone`：2.5 小时（已过 120 分钟）
  仍然不动，证明 `timeout_minutes` 没有泄漏进活跃态这一支（每个用例都照常传 `timeout_minutes=120`）

**带凭据 / 老路径不受影响**
- `test_an_active_exit_with_execution_credentials_is_never_released`：有 binding + pos/order id →
  不释放（`active_exit_has_execution_credentials`），且不读交易所
- `test_the_stuck_state_still_releases_on_its_own_120_minute_bar`：`recovery_required` 在 3 小时
  （> 120 分钟、< 6 小时）照旧释放，写的是老常量
- `test_the_three_release_reasons_stay_distinguishable`：三条路径各跑一次，三个 `last_reason` 互不相同

**副作用与节流**
- `test_releasing_an_active_lane_resumes_the_waiting_but_never_the_expired`：
  `waiting_source_deletion_exit` 重新入队，`deferred_expired` **不入队**
- `test_the_active_lane_read_only_happens_on_a_speaking_pass`：4 个 tick 只读交易所 1 次
- `test_a_held_active_exit_is_captured_once_per_interval`：6 个 5 秒 tick 只 capture 1 次，
  过 30 分钟再一次

### 反向验证（证明用例钉的是对的东西）

| 临时破坏 | 结果 |
|---|---|
| 删掉第 4 条租约判据 | `1 failed, 22 passed` —— 红的正是 `test_a_live_claim_never_loses_its_row`（原因变成 CAS 拒绝，说明两层独立） |
| 删掉 CAS 的 claim 条件 | `1 failed, 22 passed` —— 红的正是并发那条 |
| 候选查询里给活跃分支加 `id < 0`（等于关掉 L3） | `18 failed, 5 passed` —— 绿的 5 条正是三个常量 + 两条"老路径不变" |

### 相关文件

```
tests/test_active_deletion_exit_conditional_release.py tests/test_stuck_deletion_exit_selfheal.py
tests/test_source_message_deletion_worker.py tests/test_source_message_deletion.py
tests/test_management_reliability_step5.py tests/test_oncall_detector.py
-> 314 passed in 21.05s
```

### 全量

```
9695 passed, 4 skipped, 109 warnings in 788.47s (0:13:08)
```

基线 `origin/main`（`b345d2ba`）为 9672 条；L3 **+23 条**。既有用例一条没改。

## 上线前必须重新数一遍首轮量（判据是新的，旧数字无效）

L2 那一轮数的是"值守会开几条案子"；**L3 要数的是两个不同的东西**：
会有几条活跃行进入这条 pass（= 首轮告警量），以及其中几条**真的会被释放**（= 首轮状态写入量）。

**在 `VACUUM INTO` 出来的快照上跑，不要碰在跑的库**（见 memory: no heavy scans on prod DB）。
两条都走 `ix_source_message_deletion_exits_state` 的 `state IN` 等值 seek，有界。

```sql
-- 第一条：首轮会被判一次（并因此发一条告警）的活跃行。
-- 顺便把"会不会被释放"的三个前置条件分开数：无凭据 / 未被认领 / 有 raw_message。
SELECT
  e.state,
  COUNT(*)                                                         AS n,
  SUM(CASE WHEN e.execution_binding_id IS NULL THEN 1 ELSE 0 END)  AS no_binding,
  SUM(CASE WHEN e.claim_token IS NULL
            OR e.claimed_at <= datetime('now', '-5 minutes')
           THEN 1 ELSE 0 END)                                      AS lease_free,
  SUM(CASE WHEN e.raw_message_id IS NULL THEN 1 ELSE 0 END)        AS unbound_rows,
  MIN(e.created_at)                                                AS oldest_created,
  MAX(e.attempt_count)                                             AS max_attempts
FROM source_message_deletion_exits AS e
WHERE e.state IN ('pending', 'cancelling_entries', 'closing_positions', 'reconciling')
  AND e.created_at <= datetime('now', '-6 hours')
GROUP BY e.state
ORDER BY n DESC;

-- 第二条：首轮**可能**被释放的上限（还差交易所那一条判据，SQL 查不到）。
-- 只有第一条数出非 0 才需要跑；join 走各自的 raw_message_id 索引。
SELECT e.id, e.state, e.created_at, e.attempt_count, e.last_reason,
       r.chat_id, c.symbol, c.side
FROM source_message_deletion_exits AS e
JOIN raw_messages AS r ON r.id = e.raw_message_id
JOIN signal_candidates AS c ON c.raw_message_id = r.id
WHERE e.state IN ('pending', 'cancelling_entries', 'closing_positions', 'reconciling')
  AND e.created_at <= datetime('now', '-6 hours')
  AND e.execution_binding_id IS NULL
  AND (e.claim_token IS NULL OR e.claimed_at <= datetime('now', '-5 minutes'))
  AND c.symbol IS NOT NULL AND TRIM(c.symbol) != ''
  AND c.side   IS NOT NULL AND TRIM(c.side)   != ''
ORDER BY e.id
LIMIT 50;

-- 第三条（只读核对，确认交易所侧那一条不会误放）：当前每张挂单/持仓能不能归属。
-- 只在第二条数出非 0 时跑，用它输出的 symbol/side 去比对账户快照，
-- 不要反过来用 SQL 猜交易所有什么。
SELECT l.pos_id, l.order_id, b.chat_id, b.message_id, b.symbol, b.side, b.status
FROM execution_order_legs AS l
JOIN execution_bindings  AS b ON b.id = l.execution_binding_id
WHERE b.status NOT IN ('closed', 'cancelled', 'failed', 'superseded')
ORDER BY l.id DESC
LIMIT 50;
```

读数怎么用：

- **第一条为 0**（`b345d2ba` 部署当天的实测形状：全库只有 `succeeded` + `unbound`，
  活跃态一行都没有）→ L3 上线没有任何可观察的行为变化，**这也是合格结果**：
  这条路径存在的意义是下一次卡死时不用等人来救；
- **第一条非 0、第二条为 0** → 首轮只会多几条告警，不会有任何状态写入。可以上线，
  但要顺手看一眼那几行卡在哪一步（`last_reason` + `attempt_count` 是入口）；
- **第二条非 0** → **先别部署**。那意味着上线第一个 tick 就会真的放开 lane。
  先人工按第三条核对那条 lane 上的每张单是否都归属别人，确认与 L3 的判断一致再走。

## 上线后 7 天要盯的那个反例

设计稿第 6 节点名的反例是：**释放完之后又冒出无主挂单**——也就是判据 3 当时看的是一张干净的快照，
但那条 lane 上其实还有属于这条退出的东西，只是那一刻交易所没返回。
这是唯一一种"L3 放错了"的形状，查法是**先找释放事件，再看它之后的账户**：

```sql
-- 1) 本周期 L3 放过哪些行，什么时候放的，放的是哪条 lane。
SELECT e.id, e.completed_at, r.chat_id, r.message_id, c.symbol, c.side,
       ROUND((julianday(e.completed_at) - julianday(e.created_at)) * 24, 1) AS sealed_hours
FROM source_message_deletion_exits AS e
LEFT JOIN raw_messages AS r ON r.id = e.raw_message_id
LEFT JOIN signal_candidates AS c ON c.raw_message_id = r.id
WHERE e.last_reason = 'released_active_no_exchange_footprint'
ORDER BY e.completed_at DESC;

-- 2) 反例本身：那条 lane 上、释放之后仍然活着、而且**没有任何 binding 认领**的单。
--    把上面每一行的 chat_id/symbol/side/completed_at 代进来，一条 lane 查一次。
--    无主 = 交易所上有、execution_order_legs 里查不到。所以这一步要以账户快照为准：
--    read-only 取一次 list_positions + list_open_orders，再拿每个 posId/ordId 点查：
SELECT l.id, l.execution_binding_id, b.chat_id, b.message_id, b.symbol, b.side
FROM execution_order_legs AS l
JOIN execution_bindings  AS b ON b.id = l.execution_binding_id
WHERE l.pos_id = :pos_id OR l.order_id = :ord_id;
--    查不到行 → 这就是反例，记下 posId/ordId、发现时间、那条释放的 exit id。
```

三件事一起看，缺一件就不算看过：

1. **有没有反例**（上面第 2 步查不到归属的单）。**有 → 立刻回滚 L3 这一个提交**
   （它独立成提交就是为了这个），并把那张单的归属查清楚再谈第二次上线；
2. **放了几条、每条放之前封了多久**（第 1 步的 `sealed_hours`）。设计稿第 2 节的数据说健康的退出
   1 分钟内结束，所以任何一个 `sealed_hours` 落在 6 到 12 之间都值得回头看一眼那条行卡在哪；
3. **告警噪音**：`journalctl -u telegram-kol-worker | grep 'source deletion exits stuck'`
   应当是"每条卡住的退出每 30 分钟一行"，不是每 5 秒一行。若变成每 5 秒，
   说明有一条活跃行的 `state`/`last_reason` 在被反复改写（C2 那一类），
   节流对它天然失效——那是要单独处理的事，不是本轮的回归。

另外两件"不应该发生"的事，顺手确认：

- 不应出现任何 `deferred_expired` 的消息被重新入队（用例钉住了，但生产再确认一次：
  `message_processing_jobs` 里因 `deferred_resume` 入队的 raw message，其
  `recognition_decisions.automation_reason` 必须都是 `waiting_source_deletion_exit`）；
- 不应出现新的 `position_gone_confirmed` / `released_no_exchange_footprint` 行为变化——
  这两条路径本轮一行未动，它们的计数与分布应当和 L3 上线前一致。

## 留给下一位

- **未部署、未推送**，按指令。部署路径与 `AGENTS.md` 相同；**本改动全在 worker 进程里，
  落在 `tg-deploy` 自己的重启清单内（worker → web → ingest），不需要额外 restart
  `telegram-kol-oncall.service`**（本轮没碰值守）。
- 活跃态"held"现在会发 `source_deletion_exit_stuck` 告警，和值守 D6a 的案子说的是同一件事。
  两处发声不是缺陷（一处给人看、一处给 D6c 数），但如果将来觉得吵，该调的是 30 分钟那个门槛。
- C2（反复认领、永不完成）在生产上仍然没有样本。L3 能救它的前提是它在两次认领之间被本 pass 撞上
  （那一瞬间 `claim_token` 是 NULL）；撞不上就只会告警。真出现这一类时，
  正确的下一步是去看它卡在哪一步，而不是把第 4 条放宽。
- `_LAST_STUCK_CAPTURE` 仍然没有淘汰逻辑（量级极小，与上一轮同）。
