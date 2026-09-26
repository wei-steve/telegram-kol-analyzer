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
| `13eb058e` | D6a/D6b/D6c 判定、中文文案、夹具与用例 | `9616 passed, 4 skipped in 777.99s` |

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

值守这条线的六个测试文件单跑：

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
   → **已在 2026-09-26 补掉，见本文最后一节「D6a 的状态范围从一个扩到五个」。**
2. **D6c 不区分"这个类型本来就不发通知"**。要不要把 `telegram_notification_types` 的语义
   引进判据，得先看上线首日的分布；引进它也意味着值守要读一张配置表，那是另一件事。
3. 案例备注 5.2「给重复 capture 加节流」与 5.1「`_SUMMARY_FIELDS` 补 `release_reason`」
   已由 `1817d0c9` / `78a35cb1` 做掉，不属于本项目。

---

## 部署记录 · 2026-09-26（北京时间 13:08）

- 部署 sha `29eb3389`，**回滚 sha `0041ae06`**。顺序：候选推
  `claude/oncall-d6-silent-stall-rules` → `tg-deploy` → **`systemctl restart
  telegram-kol-oncall.service`**（它不在 tg-deploy 的重启清单里）→ 推 `main`。
- 两项部署检查各对两种答案测过：`0041ae06..29eb3389` 正确报 FAIL 并列出 7 个代码文件；
  部署后 `main` 对生产报 `PASS: 0 code files beyond production`。

### 上线前重新数过的首轮量（按新判据的原话，只读有界）

| 规则 | 判据 | 实测 |
|---|---|---|
| D6a | `state='recovery_required'` 且 `updated_at <= now-6h` | **0** |
| D6b | 水位线之后的 `deferred_expired` | **0**（水位线 `recognition_decisions=19189`，历史 39 条不回填） |
| D6c | `pending` + 高危 + `last_occurred_at >= now-1h` + 通知超 3 天 | **0** |

D6c 值得记一笔：两小时前它还会命中 2 条（刚清掉的那两个 revision batch），
是「`last_occurred_at` 还在推进」这条判据让它们在停止推进 60 分钟后自动出局——
**判据选「还在推进」而不是「repeat_count 大」的价值，上线当天就兑现了一次。**

### 部署后观察

值守服务 active，心跳逐轮推进（round 1 → 4，`last_error: null`），
`consecutive_read_failures = 0`，journal 无 Traceback。
新规则的案子 0 条，与上表一致；库里现存案子全部来自 D1/D2/D3/D4 等既有规则。

**真正的验证要等第一条命中**：下一条被封 6 小时的 lane、下一条被吃掉的消息、
下一条还在推进却三天没通知的高危告警。三条都有单测钉着行为，
但生产上的第一条命中才说明判据接到了真东西。

---

## 追加 · 2026-09-26：D6a 的状态范围从一个扩到五个

分支：`oncall-d6a-active-states`（基线 `origin/main` = `91621bcf`）
状态：**代码 + 文案 + 用例完成，全量测试绿，未部署、未推送。**

这一节补的就是上面「未做 / 留给下一轮」的第 1 条。上一轮的设计稿只授权
`recovery_required`，所以实现只看那一个状态，并把缺口写在了这份文件里：

> 如果哪天出现一条 6 小时没动的 `pending` 退出，D6a 不会报它。

指挥会话转达：用户已批准补上（批准来自用户本人，不是代理之间的传话本身）。**barrier 的条件是 `state != 'succeeded'`**，所以一条卡在
`pending` 的退出，封 lane 封得和 `recovery_required` 一模一样——而且更糟：
`source_deletion_exit_timeout` 的清扫只认 `recovery_required`，**活跃态没有任何自愈**。

### 状态词表（自己复核过，不是照抄提示）

| 状态 | 谁写的 | 封 lane 吗 | D6a 扫吗 |
|---|---|---|---|
| `pending` | `source_message_deletion.py:410`（重开被忽略的退出 / 从 `unbound` 转正） | 是 | **是（新增）** |
| `cancelling_entries` | `source_message_deletion_worker.py:1361` | 是 | **是（新增）** |
| `closing_positions` | worker 多处 | 是 | **是（新增）** |
| `reconciling` | worker 多处 + `_mark_reconciliation_waiting` | 是 | **是（新增）** |
| `recovery_required` | worker 多处 | 是 | 是（原有） |
| `succeeded` | `source_message_deletion.py:441`、worker、清扫器 `_release` | **否**（barrier 唯一放行的状态） | 否 |
| `unbound` | `source_message_deletion.py:262` | 否（`raw_message_id` 为 NULL，进不了 barrier 的第一个 inner join） | 否 |

前四个就是 `source_message_deletion_worker._ACTIVE_STATES`，
`historical_state_repair.py:44` 里还有同一组的第二份拷贝。
所以扫描集合 = `_ACTIVE_STATES` + `recovery_required` =
`SEALED_LANE_SEALING_STATES`（5 个），由
`test_the_sealed_lane_states_are_the_deletion_paths_own_spellings` 对着
worker 与清扫器两个源头钉住，任何一边改名都会红。

**`waiting` 不是状态**，提示里让我自己确认这点，确认结果：
`source_message_deletion_worker` 里 `final_state = "waiting"` 是个局部标签，
`counts` 字典里也有 `"waiting"` 这个键；写进行里的是 `reconciling`——
`_mark_reconciliation_waiting()` 第一行就是 `deletion_exit.state = "reconciling"`，
然后 `return "waiting"`。`test_waiting_is_a_counter_label_and_never_a_stored_exit_state`
把这件事对着 worker 源码钉住（它同时排除了 `.state = "waiting"` /
`state="waiting"` / `new_state="waiting"` 三种写法）。

判据里**保留**上一轮的 `seals_a_lane` 四样齐备（raw_message + chat + symbol + side），
所以 `unbound` 自然出局，一条「有 `raw_message_id` 但那条消息没有 symbol+side 候选」的
退出也照样出局——它其实没封住任何东西。

### 只读纪律：一条语句，`IN` 列表，走索引

`_read_sealed_lanes` 现在发的是：

```sql
SELECT ... FROM source_message_deletion_exits
WHERE state IN (?, ?, ?, ?, ?) AND updated_at <= ? ORDER BY id LIMIT ?
```

两种写法都实测过 `EXPLAIN QUERY PLAN`（在用真实 SQLAlchemy 元数据建的库上）：

| 写法 | 计划 |
|---|---|
| `state = ?`（旧） | `SEARCH ... USING INDEX ix_source_message_deletion_exits_state (state=? AND updated_at<?)` + `USE TEMP B-TREE FOR ORDER BY` |
| `state IN (?,?,?,?,?)`（新） | **同上，一字不差** |
| `state NOT IN (?, ?)`（被否掉的那种） | `SCAN`——这就是不许写它的原因 |

两条计划完全相同：SQLite 把 `IN` 展开成「每个状态一次索引 seek」。
所以选了**一条语句**而不是五条：

- 每轮一次往返，不是五次；
- `stuck_lane_limit=100` 变成**整轮的总天花板**，而五条语句就是 5×100=500 条 lane、
  每条两次点查 → 最坏 1000 次点查。一条语句的最坏成本只有五分之一；
- `ORDER BY id` 升序先给最老的行，而「能过 6 小时横杆」的正是最老的那些，
  所以 LIMIT 截断时截掉的是最不可能命中的。

第三行那个 `SCAN` 也钉成了用例（`test_the_spelling_the_sweep_rejected_really_does_scan`）：
`ALLOWED_QUERY_SHAPES` 里那句「never state NOT IN (...)」以前只是注释，现在有证据。
另外加了 `test_the_sweeps_own_statement_is_the_one_the_detector_sends`，
让手写进 EXPLAIN 用例的那条语句和模块真正发出的那条不会各自漂移
（占位符个数在运行时跟着 `SEALED_LANE_SEALING_STATES` 走）。

`updated_at <= ?` 的界**仍然是 `now` 而不是 `now-6h`**，没有改：同一次读取被 D6b 复用来
「点名挡住这条消息的那个退出」，而那个退出可能才封了十分钟。6 小时的横杆在
`_sealed_lane_observations` 里用 Python 判，位置没变。

### 两类卡死为什么必须分开说

同一件事（lane 被封着），两种成因，**能动手的人不一样**，所以案文与原因码分开：

| 类别 | `stall_class` | 原因码 | 含义 |
|---|---|---|---|
| 活跃态（`pending` / `cancelling_entries` / `closing_positions` / `reconciling`） | `active` | `source_deletion_exit_stalled_lane`（新） | worker 本该几秒走完，行还在它手里却不动了——认领或某一步在原地打转。**没有任何清扫会碰它**，案文明确这么写 |
| 卡死态（`recovery_required`） | `unclaimable` | `source_deletion_exit_sealed_lane`（沿用） | worker 永不再认领，只有系统的超时清扫或人工能动它；过了 6 小时还在，说明清扫也没放它过去 |

- **规则号仍是 D6a，case-key 仍是 `lane:<exit_id>`，severity 仍是 `high`。**
  它们是同一个问题的两种成因，不另开规则号（提示里的建议，我同意：从「这条线进不来新策略」
  这个后果看，两者一模一样）。
- 沿用旧原因码给 `recovery_required`，是为了**生产里已经开着的案子不用迁移**——
  它们当初就是按那个码立的案。
- 一条 lane 从活跃态熬成 `recovery_required` 时，**同一个案子原地换故事**
  （`upsert_case` 的 `reason_code` 以新值为准、evidence 合并），不会再弹第二条告警。
  这条有专门用例（`test_d6a_keeps_one_case_when_an_active_stall_gives_up_into_recovery`）。
- 阈值**仍是 6 小时一根横杆**，沿用 `SEALED_LANE_STUCK_AFTER`，没有引入第二个数字。
  对活跃态来说 6 小时远超必要（那几步是秒级的），但「一根横杆、不用维护第二个数」
  比「更早报一点」值钱。

顺带把两处文案做实了：
- 新增 `DELETION_EXIT_STATE_LABELS`（措辞抄自 `system_operator_bot` 的
  `source_message_deletion_outcome` 报告，补上它用不到的 `pending` / `unbound`），
  案文里状态从 `cancelling_entries` 变成「正在撤销原策略入场单（cancelling_entries）」——
  中文给人看，原词留给工程师 grep。认不出的状态标「未收录状态」，不猜。
- D6b 案文里「挡住它的是删除退出 #N（状态 …）」同样过这张表。

### 改了哪些文件

| 文件 | 改了什么 |
|---|---|
| `src/telegram_kol_research/oncall_state.py` | 新增 `LANE_STALL_ACTIVE` / `LANE_STALL_UNCLAIMABLE`。放这里是因为 detector 写、alerts 读，而这两个模块按架构边界**不能互相 import**（`oncall_state` 是它们唯一的共同词表） |
| `src/telegram_kol_research/oncall_detector.py` | `SEALED_LANE_ACTIVE_STATES` / `SEALED_LANE_SEALING_STATES`、`REASON_STALLED_LANE`、`SealedLane.stall_class` 与 `.reason_code`、`_read_sealed_lanes` 的 `IN` 写法、evidence 增加 `stall_class`、`ALLOWED_QUERY_SHAPES` 的 sealed-lane 行 |
| `src/telegram_kol_research/oncall_alerts.py` | `DELETION_EXIT_STATE_LABELS` + `deletion_exit_state_label()`、两类分开的 `cause_line`、两条原因码标签（旧的那条改成「卡死（系统不会再认领）」） |
| `tests/test_oncall_detector.py` | D6a 一节重写：见下 |
| `tests/test_oncall_alerts.py` | `open_stalled_lane_case` 夹具 + 6 条文案用例 |
| `tests/test_oncall_architecture_boundary.py` | 声明断言跟着改成 `WHERE state IN (?, ...) AND updated_at <= ?` |

### 用例

| 用例 | 条数 |
|---|---|
| 四个活跃态各自建案，reason_code / `stall_class` / `exit_state` 正确（参数化） | 4 |
| 两类成因在同一轮里各建一案，rule 与 severity 相同、原因码不同 | 1 |
| `succeeded` 不建案 | 1 |
| 活跃态也等满 6 小时，不足不建、过了就建（参数化） | 4 |
| `raw_message_id` 为 NULL 时，任何封锁态都不建案（参数化） | 4 |
| `unbound` 这个状态本身不在扫描集合里，也不建案 | 1 |
| 活跃态案子在 exit 变 `succeeded` 后 cleared（参数化） | 4 |
| 活跃态熬进 `recovery_required`：同一案、不重复开、故事换成卡死 | 1 |
| 状态集合等于 worker 的 `_ACTIVE_STATES` + 清扫器的 `STUCK_STATE` | 1 |
| `waiting` 是计数器标签、从不落库（对着 worker 源码断言） | 1 |
| `state IN (...)` 真的走索引、不含 `SCAN`（复用上一轮的 harness） | 1 |
| `state NOT IN (...)` 真的会 `SCAN`（给注释找证据） | 1 |
| 模块实际发出的语句与 EXPLAIN 用例里手写的那条一致 | 1 |
| 文案：两类各自的因果句、状态中文标签、缺 `stall_class` 的旧案子仍按卡死讲、每个可见状态都有标签、两条原因码都有中文 | 6 |

原有的 `test_d6a_only_reads_the_one_state_that_hangs_about_forever`
（断言 `pending` / `reconciling` **不**建案）**已删除**——它断言的正是被批准补掉的缺口。
它的 `succeeded` 那一半保留成了独立用例。

### 上线前必须重新数一遍首轮量（按新的五个状态）

上一轮部署前数出来 D6a=0，**那个数字只按 `recovery_required` 数的，对新判据无效。**
必须重数，因为新增的四个活跃态在生产里从没被任何东西盯过，
谁也不知道有没有长期卡着的行。

**在 `VACUUM INTO` 出来的快照上跑，不要碰在跑的库**（见 memory: no heavy scans on prod DB）。

```sql
-- 第一条：上限。判据的超集（还没检查"被删消息有没有 symbol+side 的候选"）。
-- 走 ix_source_message_deletion_exits_state，有界。
SELECT state, COUNT(*) AS n, MIN(updated_at) AS oldest_updated
FROM source_message_deletion_exits
WHERE state IN ('pending', 'cancelling_entries', 'closing_positions',
                'reconciling', 'recovery_required')
  AND raw_message_id IS NOT NULL
  AND updated_at <= datetime('now', '-6 hours')
GROUP BY state
ORDER BY n DESC;

-- 第二条：精确数，与 D6a 的 seals_a_lane 四样齐备一致。
-- 只有第一条数出非 0 才需要跑；join 都走各自的 raw_message_id 索引。
SELECT e.state, COUNT(DISTINCT e.id) AS n
FROM source_message_deletion_exits AS e
JOIN raw_messages AS r ON r.id = e.raw_message_id
JOIN signal_candidates AS c ON c.raw_message_id = r.id
WHERE e.state IN ('pending', 'cancelling_entries', 'closing_positions',
                  'reconciling', 'recovery_required')
  AND e.updated_at <= datetime('now', '-6 hours')
  AND c.symbol IS NOT NULL AND TRIM(c.symbol) != ''
  AND c.side IS NOT NULL AND TRIM(c.side) != ''
GROUP BY e.state;
```

第二条的结果就是**上线第一轮会开的 D6a 案子数**，按状态分好了类。
如果活跃态那几行加起来超过个位数，**先别部署**：那说明生产里确实有一批长期卡住的活跃退出，
应该先看它们卡在哪一步，而不是让值守一次性刷一屏。

顺便记一件复核时看到的机制，它让「活跃态长期卡住」比直觉上更值得盯：
`_claim_next_job` 每轮按 `id` 升序只取 20 条活跃行（`source_message_deletion_worker.py:1331`
起，陈旧认领的界是 5 分钟）。所以**几条永久卡住的低 id 活跃行会把后面的行一起饿住**——
一处卡死能封住的不止它自己那条 lane。这正是「活跃态卡 6 小时」需要有人知道的理由，
也是为什么首轮数出来的数字要按状态分开看，而不是只看总数。

（`seals_a_lane` 与 barrier 的 join 还差一个条件：barrier 另外要求
`raw_messages.source_status = 'deleted'`。D6a 没查这一条——有删除退出行就意味着消息被删过。
上面的 SQL 与 D6a 的实现对齐，不与 barrier 对齐，这样数出来的才是「值守会开几条案子」。）

部署步骤与上一节相同，**`telegram-kol-oncall.service` 仍不在 `tg-deploy` 的重启清单里**，
`tg-deploy <sha>` 之后必须单独 `sudo systemctl restart telegram-kol-oncall.service`。

### 这一轮明确没做

- **没碰 D6b 的建案判据**，也没碰 D6c。唯一被动变化：D6b 案文里
  `blocking_exit_*` 现在能点名一条活跃态的退出（以前只认 `recovery_required`，
  遇到活跃态挡路就写 `None`）。这是同一次读取被复用的结果，只影响那三个说明字段，
  **不影响 D6b 是否建案**（那只看 `automation_reason`），而且现在点到的才是 barrier 真正
  会拦下它的那条退出。判断：这是更准，不是更宽，所以留着并写进 `_blocking_sealed_lane` 的文档串。
- **没碰 barrier 本身**，也没碰 `source_deletion_exit_timeout` 的自愈范围。
  `03e303a1` 那条路径仍然只管 `recovery_required`——自动放掉一条**还在活跃状态**的退出
  是另一件事，风险完全不同（可能正好在撤单或平仓的半路上）。**这次坚决不扩大它。**
- 没碰生产库、没部署、没推送。

### 留给下一轮

1. **活跃态卡住之后，除了告警没有任何自愈。** 值守现在能看见了，但看见之后仍然只能靠人。
   要不要给活跃态也做一条超时清扫，是一个独立的、风险高得多的题目
   （撤单/平仓半路上被放掉会发生什么，得先想清楚）。
2. 上一节留的第 2 条（D6c 不区分「这个类型本来就不发通知」）没动。
3. `seals_a_lane` 与 barrier 的 join 仍差 `source_status = 'deleted'` 一个条件。
   目前无害（有删除退出行就意味着消息被删过），但如果哪天出现「消息又被恢复」的路径，
   这里会多报。记在这里，本轮没改。

### 部署记录 · 扩到五状态（2026-09-26 北京时间 14:27）

- 部署 sha `df0a54ab`，**回滚 sha `29eb3389`**（只回退状态范围，D6a/b/c 本身仍在）。
- 顺序：候选推 `claude/oncall-d6a-active-states` → `tg-deploy` →
  `systemctl restart telegram-kol-oncall.service` → 推 `main`。两项检查各对两种答案测过。
- **上线前按新判据重新数过**（不是沿用上一轮那个只数 `recovery_required` 的 0）：
  五个封锁态在生产上一行都没有（全库只有 `succeeded` 283 + `unbound` 91），
  精确 join 版本同样是 0。所以首轮开案 0 条，实测也是 0。
- 部署后：值守 active，心跳 round 1 → 3 推进，`last_error: null`，
  `consecutive_read_failures = 0`，journal 无 Traceback；案子仍只有既有规则那 14 条。

两点复核结论（我自己验的，不是转述）：

1. `state IN (?,?,?,?,?)` 与旧的 `state = ?` 计划完全相同
   （`SEARCH ... USING COVERING INDEX`），而 `state NOT IN (...)` 确实 `SCAN`——
   这三条现在都由 `EXPLAIN QUERY PLAN` 用例钉着，其中「`NOT IN` 会扫表」以前只是注释。
2. `waiting` 确认不是数据库状态：`_mark_reconciliation_waiting()` 写进行里的是
   `reconciling`，`"waiting"` 只是它的返回值与计数器键名。有对着 worker 源码的断言用例。

---

## 追加 · 2026-09-26：D6a 再加一个判据——「一直在动却完不成」

分支：`active-exit-starvation-and-visibility`（基线 `origin/main` = `8cb05a03`）
设计稿：`docs/plans/2026-09-26-active-deletion-exit-selfheal-design.md` 第 4 节 L2
（用户已批准 L1+L2；**L3「有条件释放活跃态退出」本轮明确不做，一行都没碰**）
同批的 L1（worker 调度公平）单独成文：`docs/active-deletion-exit-fairness-status.md`
状态：**代码 + 文案 + 用例完成，全量测试绿，未部署、未推送。**

### 补的是什么洞

D6a 到上一轮为止只会问一个问题：**这一行多久没动过**（`updated_at <= now-6h`）。
一条每 5 秒被认领一次、每次都回到同一个活跃态的退出，认领的 UPDATE 会把
`updated_at` 写成当前时间，所以它的「没动过」的年龄永远长不到 6 小时——
**D6a 看不见它**，而 lane 一样被封死。设计稿把这一类叫 C2。

补法：同一条规则 D6a 之下再加一个判据——**活跃态的退出 `created_at` 距今超过 6 小时
且仍未终结，即使 `updated_at` 是新的也建案**。

**没有新开规则号**（仍是 D6a，case-key 仍是 `lane:<exit_id>`，severity 仍是 `high`），
但原因码是新的一类，与现有两类可分：

| 类别 | `stall_class` | 原因码 | 含义 |
|---|---|---|---|
| 活跃态、6 小时没动过 | `active` | `source_deletion_exit_stalled_lane`（原有） | 行还在 worker 手里却不动了 |
| 活跃态、一直在动却完不成 | `churning`（**新**） | `source_deletion_exit_churning_lane`（**新**） | **不是没人管它，而是一直有人在动它却完不成** |
| `recovery_required` | `unclaimable` | `source_deletion_exit_sealed_lane`（原有） | worker 永不再认领 |

案文里这三句话互不相同，有用例钉着（见下）。第三类的证据是 `attempt_count`：
案文写「它已经被认领 N 次，最近一次动作就在 X 前，但从建立到现在已经 Y 都没走完」。

### 依据：6 小时这条线不会误报（生产实测，设计稿第 2 节）

2026-09 至今 160 条 `succeeded` 的退出：≤1 分钟 **153** 条，1 分钟–1 小时 3 条，
**1–6 小时 0 条**，>6 小时 4 条且全是病例。1 到 6 小时这一档是空的，
所以 6 小时既不误伤健康的退出，也不需要往下压。**阈值沿用同一个
`SEALED_LANE_STUCK_AFTER`，没有引入第二个数字**——两个判据共用一根横杆。

### 读取形状：SQL 的 WHERE / ORDER BY / LIMIT 一个字没改

提示的判断正确：**不需要改 SQL 的形状**。判据落在 Python 里。
唯一的 SQL 变化是**投影**多了两列（`_EXIT_COLUMNS` 加 `created_at`、`attempt_count`）——
`created_at` 是新判据本身要读的，`attempt_count` 是第三类的证据。
`ALLOWED_QUERY_SHAPES` 里 sealed-lane 那一行写的是 `SELECT ... FROM ...`，
声明不需要改；架构边界用例照旧绿。

`EXPLAIN QUERY PLAN` 实测（用真实 SQLAlchemy 元数据建的库，`python -B`，没碰生产库）：

```
投影 = id（现有 EXPLAIN 用例手写的那条）
  SEARCH source_message_deletion_exits USING COVERING INDEX ix_source_message_deletion_exits_state (state=? AND updated_at<?)
  USE TEMP B-TREE FOR ORDER BY

投影 = 改动前的 _EXIT_COLUMNS
  SEARCH source_message_deletion_exits USING INDEX ix_source_message_deletion_exits_state (state=? AND updated_at<?)
  USE TEMP B-TREE FOR ORDER BY

投影 = 改动后的 _EXIT_COLUMNS（+created_at, +attempt_count）
  SEARCH source_message_deletion_exits USING INDEX ix_source_message_deletion_exits_state (state=? AND updated_at<?)
  USE TEMP B-TREE FOR ORDER BY
```

**后两条一字不差，加两列没有改变计划，也没有 `SCAN`。**

顺带纠正上一节留下的一处不准确：上一节表格里写这条 sweep 的计划是
`SEARCH ... USING COVERING INDEX`。那只在投影是 `SELECT id` 时成立——
现有 EXPLAIN 用例正是这么写的，而模块真正发出的语句要读 `last_reason` 等列，
所以它一直是 `USING INDEX`（取行），不是覆盖索引。seek 完全相同，结论不变，
但文档里的措辞过去是错的。新增用例
`test_the_sealed_lane_sweep_still_seeks_its_index_with_the_real_projection`
直接用模块自己的 `_EXIT_COLUMNS` 拼语句去 EXPLAIN，这样两者不会再漂移，
也把「真实投影」这一层补进了证据里。

### 一个刻意的范围限制

`created_at` 判据**只对四个活跃态生效**，不对 `recovery_required` 生效。
理由（自己复核的，不是照抄）：能写 `recovery_required` 这一行的只有
`source_deletion_exit_timeout._release`，而它在同一条 UPDATE 里就把 state 写成
`succeeded`——**不存在「刷新了卡死行的 `updated_at` 却让它继续卡着」的路径**，
所以这一类在生产上不可能出现「updated_at 新、created_at 老」的形状。
按「改动最小」的原则不扩大判据。
`test_d6a_leaves_a_freshly_touched_recovery_required_exit_alone` 把这个决定写成了用例
（它断言的是当前行为，不是说这个行为一定对）。

### 案文取「封了多久」的时钟换了一个（只对新类）

churning 这一类的 `updated_at` 按定义是秒级新的，
如果「封了多久」还读 `minutes_sealed`（= 距 `updated_at` 的时长），
会对一条封了 11 天的 lane 打印「封了多久：0 分钟」。
所以**只有 churning 这一类**改读新字段 `minutes_unfinished`（= 距 `created_at` 的时长）。
`active` 与 `unclaimable` 两类的文案一个字没改，原有 16 条文案用例全绿。

### 改了哪些文件

| 文件 | 改了什么 |
|---|---|
| `src/telegram_kol_research/oncall_state.py` | 新增 `LANE_STALL_CHURNING`（放这里的理由同前：detector 写、alerts 读，两个模块按架构边界不能互相 import） |
| `src/telegram_kol_research/oncall_detector.py` | `REASON_CHURNING_LANE`；`_EXIT_COLUMNS` 加两列；`SealedLane` 加 `created_at` / `attempt_count` 与 `is_active` / `idle_for` / `unfinished_for`；`stall_class` 与 `reason_code` 从属性改成带 `now` 与阈值的方法（三分支）；`_sealed_lane_observations` 的两个年龄判据取并集；evidence 增加 `exit_created_at` / `minutes_unfinished` / `attempt_count`；两处注释更正 |
| `src/telegram_kol_research/oncall_alerts.py` | 新原因码的中文标签；`format_sealed_lane_alert` 的因果句从两分支变三分支；churning 这一类的「封了多久」改读 `minutes_unfinished` |
| `tests/oncall_test_support.py` | `add_deletion_exit` 与 `build_sealed_lane_case` 增加 `created_at` / `attempt_count` 两个参数（`created_at` 默认跟 `updated_at`，所以既有调用行为不变） |
| `tests/test_oncall_detector.py` | D6a 新增 20 条 |
| `tests/test_oncall_alerts.py` | 新增 4 条 + `open_churning_lane_case` 夹具 |

`stall_class` / `reason_code` 从属性改成方法是唯一的接口变化。
全仓只有 detector 自己读这两个成员（`grep stall_class` 的另外几处都是读 evidence 字典），
所以没有连带影响。

### 用例

| 用例 | 条数 |
|---|---|
| 四个活跃态各自「created_at 老 + updated_at 新」建案，reason_code / `stall_class` / `attempt_count` / `minutes_unfinished` 正确（参数化） | 4 |
| 新判据也要等满 6 小时（5 小时不建，+2 小时后建，且是 churning） | 1 |
| `created_at` 老到 30 天但状态是 `succeeded` → 不建案 | 1 |
| 三类成因在同一轮各建一案：三个原因码、三个 `stall_class`、同一个 rule 与 severity、共 3 条案子 | 1 |
| 两个判据同时成立 → **只开一案不重复**，且措辞取「没动过」那一侧 | 1 |
| churning 熬成「没动过」：同一案原地换故事，不弹第二条告警 | 1 |
| churning 的案子在 exit 变 `succeeded` 后 cleared（参数化四态） | 4 |
| `recovery_required` + 新 `updated_at` + 老 `created_at` → 不建案（刻意的范围限制） | 1 |
| `unbound`（`raw_message_id` NULL）即使永不完成也不建案，`seals_a_lane` 优先（参数化四态） | 4 |
| 三个 `stall_class` 与三个原因码互不相同，且阈值只有一个 6 小时 | 1 |
| 真实投影下 sweep 仍命中索引、不含 `SCAN`（语句由模块自己的 `_EXIT_COLUMNS` 拼出） | 1 |
| 文案：churning 的因果句 / 它读的是 `minutes_unfinished` / `attempt_count` 缺失时不打印「0 次」/ 三类文案互不相同 | 4 |

**反向验证（证明用例钉的是新判据）**：把 `unfinished_too_long` 临时改成 `False` 再跑：

```
11 failed, 128 passed in 9.91s
```
红的正是 churning 那 11 条（4 建案 + 1 阈值 + 1 三类分辨 + 1 换故事 + 4 收口），
其余 128 条全绿——既有 D6a/D6b/D6c 行为没有被改动。

值守六个文件 + 删除退出三个文件：

```
tests/test_oncall_detector.py tests/test_oncall_alerts.py
tests/test_oncall_architecture_boundary.py tests/test_oncall_service.py
tests/test_oncall_casefile.py tests/test_oncall_codex.py
tests/test_source_message_deletion_worker.py tests/test_source_message_deletion.py
tests/test_stuck_deletion_exit_selfheal.py
-> 463 passed in 23.80s
```

全量（`uv run pytest -q`，L1+L2 最终树）：

```
9672 passed, 4 skipped, 109 warnings in 788.19s (0:13:08)
```

L1 commit（`26323fef`）的全量是 `9648 passed, 4 skipped`，所以 L2 恰好 **+24 条**
（detector 20 + alerts 4），与上表相加一致。基线 `origin/main`（`8cb05a03`）为 9642 条。

### 提交

| 提交 | 内容 | 全量测试 |
|---|---|---|
| `26323fef` | L1：worker 的认领排序（另见 `docs/active-deletion-exit-fairness-status.md`） | `9648 passed, 4 skipped in 783.80s` |
| 本节 | L2：D6a 的第三个判据、文案、夹具与用例 | `9672 passed, 4 skipped in 788.19s` |

两者刻意分成两个提交：风险等级不同（L1 在交易路径上，L2 只读且不写状态），
回滚粒度要分得开。提交时逐路径 `git add`，`git diff --cached --name-only` 核对，
未用 `git add -A`。

### 上线前必须重新数一遍首轮量（新判据是新的，旧数字无效）

上一轮部署前数出来 D6a=0，**那是按「五个封锁态 + `updated_at <= now-6h`」数的，
对新判据无效**：新判据会额外命中「`updated_at` 很新但 `created_at` 很老」的活跃行，
这类行以前一行都没被数过。

**在 `VACUUM INTO` 出来的快照上跑，不要碰在跑的库**（见 memory: no heavy scans on prod DB）。

```sql
-- 第一条：上限。新判据的超集（还没检查被删消息有没有 symbol+side 的候选）。
-- 走 ix_source_message_deletion_exits_state 的 state IN 等值 seek，有界。
-- 三类分开数，好知道首轮各会开几条。
SELECT
  CASE
    WHEN state = 'recovery_required' THEN 'unclaimable'
    WHEN updated_at <= datetime('now', '-6 hours') THEN 'active(没动过)'
    ELSE 'churning(一直在动)'
  END AS stall_class,
  COUNT(*) AS n,
  MIN(created_at) AS oldest_created,
  MAX(attempt_count) AS max_attempts
FROM source_message_deletion_exits
WHERE state IN ('pending', 'cancelling_entries', 'closing_positions',
                'reconciling', 'recovery_required')
  AND raw_message_id IS NOT NULL
  AND (updated_at <= datetime('now', '-6 hours')
       OR (state != 'recovery_required'
           AND created_at <= datetime('now', '-6 hours')))
GROUP BY stall_class
ORDER BY n DESC;

-- 第二条：精确数，与 seals_a_lane 四样齐备一致。
-- 只有第一条数出非 0 才需要跑；join 都走各自的 raw_message_id 索引。
SELECT e.state, COUNT(DISTINCT e.id) AS n
FROM source_message_deletion_exits AS e
JOIN raw_messages AS r ON r.id = e.raw_message_id
JOIN signal_candidates AS c ON c.raw_message_id = r.id
WHERE e.state IN ('pending', 'cancelling_entries', 'closing_positions',
                  'reconciling', 'recovery_required')
  AND (e.updated_at <= datetime('now', '-6 hours')
       OR (e.state != 'recovery_required'
           AND e.created_at <= datetime('now', '-6 hours')))
  AND c.symbol IS NOT NULL AND TRIM(c.symbol) != ''
  AND c.side IS NOT NULL AND TRIM(c.side) != ''
GROUP BY e.state;
```

第二条的结果就是上线第一轮会开的 D6a 案子数。
`df0a54ab` 部署当天全库只有 `succeeded` 283 + `unbound` 91，所以**预期仍是 0**；
但那个快照是上一轮取的，**必须重新跑，不要沿用**。
如果 churning 那一行数出来不是 0，**先别部署**：那说明生产上确实有退出在原地打转，
应该先看它卡在哪一步（`attempt_count` 与 `last_reason` 是入口），
而不是让值守一次性刷一屏。

L1 上线后，`active(没动过)` 这一类的期望会更低（被饿死的行现在都能被认领），
而 `churning` 这一类**不会**因为 L1 变少——L1 让行能被认领，不会让一条原地打转的行前进。
**这正是 L2 存在的理由，也是设计稿 L3（本轮不做）要处理的东西。**

部署步骤与前两节相同，**`telegram-kol-oncall.service` 仍不在 `tg-deploy` 的重启清单里**，
`tg-deploy <sha>` 之后必须单独 `sudo systemctl restart telegram-kol-oncall.service`，
前后各看一次 `/var/lib/telegram-kol-oncall/heartbeat.json`。
（同批的 L1 改的是 worker，落在 `tg-deploy` 自己的重启清单内。）

### 这一轮明确没做

- **没碰 `source_deletion_exit_timeout` 的释放逻辑**，一行都没碰。
  设计稿 L3（有条件释放活跃态退出）本轮不做：它是唯一会自动改变
  「要不要继续封着 lane」这个判断的东西，而一条活跃态的退出可能正好在撤单或平仓的半路上。
- 没碰 D6b、D6c 的建案判据；没碰 barrier；没碰 `_ACTIVE_STATES` 的成员。
- 没改任何既有用例。
- 没碰生产库、没部署、没推送。

### 留给下一轮

1. **活跃态卡住之后仍然只有告警，没有自愈。** 现在三种成因都看得见了（C1 由 L1 消失，
   C2 由 L2 可见，C3 本来就该有人看），但看见之后还是只能靠人。L3 是那件事。
2. `created_at` 判据不覆盖 `recovery_required`（见上「一个刻意的范围限制」）。
   目前无害，理由已复核并写成用例；如果哪天出现会刷新卡死行 `updated_at` 的新路径，
   这里要跟着改。
3. 上两节留的第 2、3 条（D6c 不区分「这个类型本来就不发通知」；
   `seals_a_lane` 与 barrier 的 join 仍差 `source_status = 'deleted'`）都没动。
