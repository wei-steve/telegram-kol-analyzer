# 值守 D6：三条"没人看见"的判据 · 状态

日期：2026-09-26
分支：`oncall-d6-silent-stall-rules`（基线 `origin/main` = `8fdefd63`）
设计稿：`docs/plans/2026-09-26-oncall-d6-silent-stall-rules-design.md`（已批准）
背景证据：`docs/2026-09-26-silent-stall-case-note.md`
所属程序：`docs/codex-oncall-status.md`（阶段 1 已部署在跑）
状态：**三条判据 + 文案 + 用例全部完成，全量测试绿，未部署、未推送。**

本文件是本项目跨会话唯一的进度真相。

---

## 一句话

值守现在会问三个以前没人问的问题：**还有没有哪条 lane 被删除退出封着**（D6a）、
**有没有真实策略被系统自己作废掉**（D6b）、**有没有告警还在响但早就没人被通知**（D6c）。

## 提交

| 提交 | 内容 | 全量测试 |
|---|---|---|
| `SHA_PLACEHOLDER` | D6a/D6b/D6c 判定、中文文案、夹具与用例 | `9616 passed, 4 skipped in 777.99s` |

基线（`8fdefd63`，未加本项目用例时）9563 条；本项目 **+53 条**。
提交时逐路径 `git add`，`git diff --cached --name-only` 核对，未用 `git add -A`。

## 落点

| 规则 | 文件 | 位置 | 做了什么 |
|---|---|---|---|
| 共同 | `src/telegram_kol_research/oncall_state.py` | `SEALED_LANE_CASE_PREFIX` / `VOIDED_MESSAGE_CASE_PREFIX` / `UNHEARD_INCIDENT_CASE_PREFIX` | 三个新的 case-key 命名空间，沿用 `health:` / `mgmt:` / `recog:` 的既有做法 |
| 共同 | `oncall_detector.py` | `ALLOWED_QUERY_SHAPES` | 新增四行形状声明（含"为什么不写 `state NOT IN`"） |
| 共同 | `oncall_detector.py` | `as_production_text()` | 本模块第一次在 SQL 里比较时间戳，所以把"生产 DATETIME 列的确切写法"单独做成函数并钉了用例 |
| 共同 | `oncall_detector.py` | `DetectorConfig.sealed_lane_stuck_after` / `incident_still_occurring_within` / `incident_notification_silence` | 三个阈值的模块级常量 + 可注入字段 |
| D6a | `oncall_detector.py` | `SealedLane`、`_read_sealed_lanes`、`_latest_candidate_symbol_side`、`_count_voided_messages`、`_sealed_lane_observations`、`_sealed_lane_clears` | 扫 `recovery_required` 的删除退出，命名 lane，数被作废的消息，满 6 小时建案；exit 变 `succeeded` 收口 |
| D6b | `oncall_detector.py` | `_evaluate_recognition_decision` 新分支、`_voided_message_observation`、`_blocking_sealed_lane` | `automation_reason='deferred_expired'` 独立建案（**不套持仓前提**）；`waiting_source_deletion_exit` 既不建案也不收口，继续盯 |
| D6c | `oncall_detector.py` | `_unheard_incident_observations`、`_unheard_incident_reason`、`_unheard_incident_clears` | 扫 `runtime_incidents`，四条件齐备才建案；停止推进 / 被认领 / 重新通知过即收口 |
| 文案 | `oncall_alerts.py` | `REASON_LABELS` 五条新词、`_hours_and_minutes`、`_instrument_label`、六个 formatter、`_CASE_FORMATTERS` | 三种新故事各有自己的中文文案；开案/收口的分派改成一张表，两条 `if` 阶梯不会再各说各话 |
| 用例 | `tests/oncall_test_support.py` | `add_deletion_exit`、`set_deletion_exit_state`、`add_runtime_incident` 扩参、`set_runtime_incident`、`build_sealed_lane_case`、`build_voided_message_case` | 夹具沿用现有风格（真实 SQLAlchemy 元数据建库） |
| 用例 | `tests/test_oncall_detector.py` | D6a/D6b/D6c 三节 + 形状正则 + 新增 EXPLAIN QUERY PLAN 用例 | 见"测试" |
| 用例 | `tests/test_oncall_alerts.py` | D6a/D6b/D6c 文案节 | 16 条 |
| 用例 | `tests/test_oncall_architecture_boundary.py` | `test_every_production_read_shape_the_detector_uses_is_declared` | 声明列表补四项断言 |

## 阈值与理由

| 规则 | 阈值 | 常量 | 为什么是这个数 |
|---|---|---|---|
| D6a | 6 小时 | `SEALED_LANE_STUCK_AFTER` | 系统自己的清扫器用 `source_deletion_exit_timeout_minutes`（生产 120 分钟）。值守必须**晚于**系统的自愈窗口才不会抢在补救之前喊。6 小时与 `case_stale_after` 是同一根横杆 |
| D6b | 无 | — | `deferred_expired` 本身就是终态，它只能由**熬过** `deferred_resume_timeout_minutes`（生产 30 分钟）才写出来。再加一层等待等于把已经确定的损失继续压着 |
| D6c | 1 小时 | `INCIDENT_STILL_OCCURRING_WITHIN` | "现在还在发生"。用 `last_occurred_at` 而不是 `repeat_count`：35.6 万次同样可能属于一条上周就停了的告警，那种不需要叫人 |
| D6c | 3 天 | `INCIDENT_NOTIFICATION_SILENCE` | "早就没人被通知过"。`runtime_incidents` 按指纹 coalesce、只在第一次落库时通知，所以 `notified_at` 可以停在 11 天前而告警每 5 秒还在响 |

三个阈值都是模块级常量，`DetectorConfig` 的同名字段默认取它们；用例注入 `now` 与 config，**没有 sleep**。

## 只读纪律：四条新语句都落在形状里

| 语句 | 索引 | 计划（EXPLAIN QUERY PLAN，用例钉住） |
|---|---|---|
| `source_message_deletion_exits WHERE state = ? AND updated_at <= ? ORDER BY id LIMIT ?` | `ix_source_message_deletion_exits_state` = (state, updated_at) | `SEARCH ... USING INDEX ...(state=? AND updated_at<?)` |
| `runtime_incidents WHERE status = ? AND last_occurred_at >= ? ORDER BY id LIMIT ?` | `ix_runtime_incidents_claimable` = (status, claim_expires_at, last_occurred_at) | `SEARCH ... USING INDEX ...(status=?)` |
| `raw_messages WHERE chat_id = ? AND id > ? ORDER BY id LIMIT ?` | `ix_raw_messages_chat_id` | `SEARCH ... USING COVERING INDEX ...(chat_id=? AND rowid>?)` |
| `recognition_decisions WHERE raw_message_id IN (?, ...)` | `sqlite_autoindex_recognition_decisions_1`（唯一键） | `SEARCH ... USING INDEX ...(raw_message_id=?)` |

**没有一条是 `SCAN`。** 新增的 `test_the_new_sweeps_really_do_use_their_index` 就是为这件事写的：
形状正则只能证明语句"写得像"有界查询，证明不了它真的走索引——而 2026-09-15 冻住 worker 事件循环
八次的正是"看着有界、实际全表扫"的语句。三条 sweep 现在由计划本身把关。

读失败照旧走 `ProductionReadError` → `_record_read_failure` → `read_failed`，新规则没有任何
"读不到就当没事"的分支。

## 设计稿说错 / 说得不够的地方

### 1. D6a「`unbound` 的行 `raw_message_id` 全为 NULL 所以不封 lane」——结论对，理由不完整

自己复核了 `source_execution_barrier` 的 join（`source_message_deletion.py` 第 89 行起）：

```python
.join(RawMessage, RawMessage.id == SourceMessageDeletionExit.raw_message_id)
.join(SignalCandidate, SignalCandidate.raw_message_id == RawMessage.id)
.filter(RawMessage.chat_id == ..., RawMessage.source_status == "deleted",
        SignalCandidate.symbol == symbol, SignalCandidate.side == side,
        SourceMessageDeletionExit.state != "succeeded")
```

`raw_message_id IS NULL` 进不了第一个 inner join，**这一半设计稿说对了**。但同一个 join 还要求
那条被删消息**拥有一个 symbol 与 side 都非空的候选**。也就是说"有 `raw_message_id`"不等于
"封了 lane"。所以实现里的判据是 `SealedLane.seals_a_lane`（raw_message + chat + symbol + side
四样齐备），比设计稿的两条更严，并各有一条用例。这不是收紧告警面，而是不去报一条其实没封住
任何东西的 exit。

另外：`state != 'succeeded'` 意味着 `pending` / `reconciling` / `closing_positions` 等**也**在封
lane。设计稿只让 D6a 看 `recovery_required`，实现照办——那些状态是 worker 的活跃状态，
120 分钟的清扫器会处理，6 小时之后还留在那儿的现实中只有 `recovery_required`。
**这是一个已知的覆盖缺口**，不是 bug：如果哪天出现一条 6 小时没动的 `pending` 退出，
D6a 不会报它。要补的话是同一个形状再查一次，成本很低。

### 2. D6a 的「被作废条数」没法按设计稿的字面写

设计稿说"同群 `recognition_decisions.automation_reason='deferred_expired'` 的计数，有界查询"。
**`automation_reason` 上没有任何索引**（`recognition_decisions` 只有 `raw_message_id` 唯一键和
`(agreement_status, updated_at)`），直接按 `automation_reason` 过滤就是全表扫——正是这个模块
禁止的东西。所以改成从群这一侧驱动：先用覆盖索引取"封锁之后这个群的最多 200 条消息 id"，
再按 `raw_message_id IN (...)` 点查它们的决定行。案文里因此写的是**"至少 N 条（只数了 M 条）"**，
而不是一个假装精确的数字。

### 3. D6b 要想能用，必须顺手改一处设计稿没提的行为

设计稿只说"不要报 `waiting_source_deletion_exit`"。照字面做会让 D6b 永远不触发：
现有代码把"拿不到 lossy 理由"的决定行判成**已恢复**并 `retire` 掉 watch item，而一条消息在
被值守第一次看到时通常正处于 `waiting_source_deletion_exit`——它会在变成 `deferred_expired`
（30 分钟后）之前就被忘掉。所以 `waiting_source_deletion_exit` 现在走"既不建案也不收口、
继续盯"这一路（和 `agreement_status='pending'` 同一个分支形状），6 小时的 watch 过期兜底。
`test_d6b_says_nothing_while_the_message_is_merely_waiting` 把两半都钉住了。

### 4. D6b 用自己的 case-key 前缀，不与 D3 共用

设计稿没说。共用 `recog:` 会有一个真实的坑：同一条消息若先被 D3 开过案并 resolved，
`upsert_case(reopen=False)` 不会再建新案，于是**后来的真实损失静默丢失**。所以 D6b 用
`voided:`。顺带好处是 `_format_open_alert` 的分派干净，两种故事不会串词。

## 几个"设计稿没写，按改动最小的那一侧选"的决定

1. **严重度**：三条都是 `high`。D6b 是设计稿指定的；D6a 一条被封的 lane 正在静默吞掉真实策略；
   D6c 的入场判据本身就是 `severity IN ('high','critical')`。
2. **D6c 的 severity 与 `notified_at` 在 Python 里过滤，不写进 SQL**：它们上面没有索引，
   写进 `WHERE` 不会让查询更快，只会让形状正则更难读。`status = ?` 的等值 seek 才是省的那一步。
3. **D6c 的 `status`**：设计稿只说 `status='pending'`。实现照此，并且**被认领（`claimed` 等）
   即收口**——有人接手了就不再是"没人听见"。
4. **D6b 不套 `_message_has_management_work` 的让路**：D3 遇到已有管理工单会站下，
   D6b 不会。被作废的入场压根不产生管理工单；而一条被作废的管理指令确实可能同时触发 D1d，
   两条案子 case-key 不同、文案不同，同群 10 分钟 3 条的合并阈值会兜住噪音。
5. **收口用点查复核，不用"本轮没扫到就收口"**：sweep 有 LIMIT，截断会造成假收口。
   D6a 只在 exit 真的是 `succeeded`（或行已消失）时收口；D6c 点查后重跑一遍四条件。
6. **一轮最多看多少**：`stuck_lane_limit=100`（每条 lane 要两次点查才能命名）、
   `voided_scan_limit=200`、`incident_limit=200`。生产上 `recovery_required` 一直是个位数。

## 测试

全量（`uv run pytest -q`，本项目最终树）：

```
9616 passed, 4 skipped, 109 warnings in 777.99s (0:12:57)
```

值守这条线的五个文件单跑：

```
tests/test_oncall_detector.py tests/test_oncall_alerts.py
tests/test_oncall_architecture_boundary.py tests/test_oncall_service.py
tests/test_oncall_casefile.py tests/test_oncall_codex.py
-> 327 passed in 11.60s
```

新用例清单：

**D6a（9 条 + 1 条常量对齐）**
- 满 6 小时建案，案文带 symbol/side/封锁时长/exit 状态与原因
- 不满 6 小时不建案，过了横杆再跑就建
- 案文带上"期间已被作废的条数"，并且不把非 `deferred_expired` 的消息算进去
- exit 变 `succeeded` → cleared
- exit 只是 `updated_at` 被碰了一下（仍 `recovery_required`）→ **不** cleared
- `unbound`（`raw_message_id` NULL）→ 不建案
- 被删消息没有 symbol+side 的候选 → 不建案
- `succeeded` / `pending` / `reconciling` 三个状态都不进判定（参数化 3 条）
- `SEALED_LANE_STUCK_STATE == source_deletion_exit_timeout.STUCK_STATE`

**D6b（6 条）**
- `deferred_expired` 建案，case-key `voided:`，规则 `D6b`，带 symbol/side/原文
- **没有任何持仓也照样建案**，且 `skipped_no_position` 计数为 0（专门钉这条）
- `DEFERRED_EXPIRED_REASON` / `DEFERRED_HOLD_REASON` 都**不在** `LOSSY_RECOGNITION_REASONS` 里
- 两个原因码等于 `deferred_instruction_recovery` 的同名常量
- `waiting_source_deletion_exit` 不建案，且之后转 `deferred_expired` 时仍能建案
- 案文带上挡住它的那条 exit
- 水位线之前的历史行不回填

**D6c（10 条）**
- 还在推进 + 从未通知 → 建案（`runtime_incident_never_notified`）
- 还在推进 + 上次通知 11 天前 → 建案（`runtime_incident_notification_stale`）
- 停止推进 → 不建案
- 6 小时前刚通知过 → 不建案
- `info` / `low` / `medium` → 不建案（参数化 3 条）
- `claimed` / `diagnosed` / `resolved` / `closed` → 不建案（参数化 4 条）
- 停止推进 / 被通知 / 被认领 三种收口各一条
- `INCIDENT_PENDING_STATUS`、`INCIDENT_LOUD_SEVERITIES` 与 `runtime_incidents` 的取值对齐

**形状与架构**
- 三条 sweep 的 `EXPLAIN QUERY PLAN` 都命中指定索引且不含 `SCAN`（参数化 3 条）
- 全部生产语句仍匹配形状正则；形状测试现在会真的跑到三条 sweep
- `ALLOWED_QUERY_SHAPES` 声明列表补齐（架构测试）
- 三个阈值等于设计稿的数字，且 `DetectorConfig` 默认值与常量一致
- `as_production_text` 的写法等于生产存的写法

**文案（16 条）**
- 三种故事各自的开案文案与收口文案、与 `compose_case_alerts` 的分派
- 五个新原因码都有中文标签且不落到"未收录原因"
- "11 天 3 小时"这类时长可读；没有被作废消息时说"还没有"；认不出 exit 时不硬写

## 上线首轮预期命中量

设计稿在生产实测过：**D6a=0 / D6b=0 / D6c≈0–2**。
**这份快照不可信，上线前必须重新数一遍**，理由有两条：

1. 快照是 2026-09-26 取的，此后 `03e303a1`（丙：无凭据的删除退出自己收口）已经进了 `main`。
   陈哥那种"手里没有任何要撤的凭据"的形状现在会在 120 分钟内自己解封，所以 D6a 的稳态期望
   比设计稿写的时候**更低**，而且新出现的同型 lane 根本活不到 6 小时。
2. D6c 的判据里没有"这个 incident 类型是否在通知白名单里"。`telegram_notification_types`
   之外的类型 `notified_at` 永远是 NULL，所以任何**仍在推进**的 high/critical 未通知类型都会命中。
   1 小时的推进窗口是唯一的收窄器，它够不够窄只有当天的数据能回答。

### 怎么数（只读、有界，**在 `VACUUM INTO` 快照上跑，不要碰在跑的库**）

```sql
-- D6a：现在有几条被封 6 小时以上的 lane（走 ix_source_message_deletion_exits_state）
SELECT COUNT(*) FROM source_message_deletion_exits
WHERE state = 'recovery_required'
  AND raw_message_id IS NOT NULL
  AND updated_at <= datetime('now', '-6 hours');

-- D6c：现在有几条"还在响、没人听"（走 ix_runtime_incidents_claimable 的 status 等值）
SELECT COUNT(*) FROM runtime_incidents
WHERE status = 'pending'
  AND severity IN ('high', 'critical')
  AND last_occurred_at >= datetime('now', '-1 hours')
  AND (notified_at IS NULL OR notified_at < datetime('now', '-3 days'));

-- D6c 的分布，看会不会刷屏
SELECT incident_type, severity, COUNT(*) AS n, MAX(last_occurred_at) AS latest
FROM runtime_incidents
WHERE status = 'pending' AND last_occurred_at >= datetime('now', '-1 hours')
GROUP BY incident_type, severity ORDER BY n DESC;
```

D6b 的首轮期望是 **0，而且是结构性的**：`recognition_decisions` 已经在水位线表里，新规则只对
水位线之后的新行生效，库里的历史 `deferred_expired` 不会在上线时集中炸出来。想知道"多久会
命中一次"，只能扫全表——**`automation_reason` 上没有索引，这条只许在快照上跑**：

```sql
-- 只在 VACUUM INTO 出来的快照上跑：全表扫
SELECT date(updated_at) AS day, COUNT(*) FROM recognition_decisions
WHERE automation_reason = 'deferred_expired' GROUP BY day ORDER BY day DESC LIMIT 30;
```

## 部署时多一步（和前几次不同）

`telegram-kol-oncall.service` **不在 `tg-deploy` 的重启清单里**（它只重启 worker → web → ingest）。
所以 `tg-deploy <sha>` 之后还必须：

```
sudo systemctl restart telegram-kol-oncall.service
```

**前后各看一次** `/var/lib/telegram-kol-oncall/heartbeat.json`，确认心跳时间戳继续推进。
上线后 1 小时内清点新开案子的条数与规则分布；若明显多于上面数出来的数字，**先停服务再查**，
不要让它刷 Telegram。

## 未做 / 留给下一轮

1. **6 小时没动的 `pending` / `reconciling` 删除退出没人管**（见"设计稿说错"第 1 条）。
   设计稿只授权 `recovery_required`，没有自行扩大。
2. **D6c 不区分"这个类型本来就不发通知"**。要不要把 `telegram_notification_types` 的语义
   引进判据，得先看上线首日的分布；引进它也意味着值守要读一张配置表，那是另一件事。
3. 案例备注 5.2「给重复 capture 加节流」与 5.1「`_SUMMARY_FIELDS` 补 `release_reason`」
   已由 `1817d0c9` / `78a35cb1` 做掉，不属于本项目。
