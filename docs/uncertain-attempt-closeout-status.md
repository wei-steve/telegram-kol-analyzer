# 37 条 uncertain 执行尝试的收口 + 扫描器噪音 · 实施状态

日期：2026-09-26
分支：`uncertain-attempt-closeout`（基线 `origin/main` = `92b43cc0`）
设计稿：`docs/plans/2026-09-26-uncertain-attempt-closeout-design.md`（已批准）
状态：**本地实现完成，未部署、未推送、未在生产上跑过任何命令（含 dry-run）**
进度：甲乙、丙各一个提交，都已落地（SHA 见第 6 节）。

---

## 0. 一句话

给 `authoritative_execution_attempts.status='uncertain'` 的行加了一条审计过的收口路径
（新 CLI，默认 dry-run），并把扫描器那条每天 2.7 万行的 `ERROR` 改成**按行节流 + 按 action 分级**。
**`recognition_decisions` 一个字没动**，所以被收口的消息依旧不可重新识别、依旧不可能被重放下单。

---

## 1. 三块的落点

### 甲 · 收口工具

| 位置 | 内容 |
|---|---|
| `src/telegram_kol_research/uncertain_attempt_closeout.py` | 新模块：分桶、判定、dry-run 计划、原子 apply |
| `src/telegram_kol_research/cli.py` | 新子命令 `close-out-uncertain-attempts`，默认 dry-run |
| `src/telegram_kol_research/authoritative_execution_attempts.py` | 声明两个收口终态常量（状态的声明处，避免在线模块 import 运维工具） |
| `src/telegram_kol_research/models.py` | 放宽 `ck_authoritative_execution_attempts_status` |
| `src/telegram_kol_research/db.py` | `_widen_sqlite_authoritative_execution_attempt_status_check`，由 `init_db` 调 |
| `docs/ARCHITECTURE.md` | 把新模块列进"只由 cli.py 可达的运维修复工具" |

**分桶用的是账本自己的规则，没有新写一套。** 一条执行事件算"写过交易所"的条件是
`execution_event_has_exchange_identity()` **或** `action NOT IN NON_EXCHANGE_WRITING_EXECUTION_ACTIONS`
——与 `source_message_deletion_worker` 用的是同一个析取式，所以没人分类过的新 action 天然算写入。
与那个调用点唯一的差别：**这里不排除任何 action**（那边排除自己的两条 outcome 行）。
一条 `source_message_deletion_outcome` 在这里会把尝试推进"需要绑定终态"的桶，
也就是 fail-closed 方向。

三个桶：

| 桶 | 判据 | 收口 |
|---|---|---|
| `no_execution_event` | 该消息名下一条执行事件都没有 | `closed_no_write` |
| `non_writing_events_only` | 有事件，但全是白名单里的通知/审计行 | `closed_no_write` |
| `exchange_write` | 有真实写入 | 绑定全部终态才给 `closed_settled_binding`；否则**拒绝** |

拒绝理由两种，都会出现在清单里：

- `binding_not_terminal`——至少一个绑定还活着（`live_binding_ids` 列出是哪些）；
- `no_binding_to_verify`——写过但找不到任何绑定，**无法证明已了结，所以不收口**。
  绑定从两个方向找并取并集：写入事件上的 `execution_binding_id`，以及
  `execution_bindings` 里同 `(chat_id, message_id)` 的行。"找不到绑定"不能成为收口的捷径。

`binding_ids` / `live_binding_ids` **对每个桶都会打印**，但只在 `exchange_write` 桶当闸门用。
设计稿给无写入两桶的判据就是"没有写入事件"，而一条从未写入的消息下面挂着的绑定是账本残留、
不是这次尝试造成的敞口；真正看活仓的是生命周期 / 删除退出那套机制，它们都不读尝试状态。
所以这里只把它印出来给人看，不擅自加一条设计稿没要求的拒绝
（用例 `test_a_no_write_bucket_reports_a_live_binding_without_refusing` 记录了这个取舍）。

绑定终态集合 `TERMINAL_BINDING_STATES` = `{closed, cancelled, completed, failed, resolved, superseded}`，
与 `position_take_profit_orders` / `historical_state_repair` 里那两份私有副本逐字相同，
有用例钉住三者相等（不跨模块 import 私有名）。

### 乙 · 安全约束

**`recognition_decisions` 从不出现在这个模块发出的任何语句里。** 收口只改尝试行的
`status` / `error_summary` / `updated_at`，其余字段（含 `exchange_effect='outcome_unknown'`、
`uncertain_at`、`completed_at`、`evidence_refs_json`）一律不动——那笔请求在交易所那边到底怎样，
仍然是未知，而且永远是未知；收口确立的是**敞口已了结**，是另一件事，写在 status 和
`error_summary` 里，不写进关于交易所答复的字段。

回归用例（本次交付的验收核心）：

- `test_closed_out_message_still_cannot_be_re_recognised` —— 收口后直接调
  `save_pending_authoritative_decision` 与 `save_terminal_authoritative_decision`，
  两者仍抛 `AuthoritativeExecutionInProgress`，且 `comparison_status` 仍是 `execution_uncertain`。
  **故意不走 `process_authoritative_message`**：那条路上还有一层尝试状态的重试闸门，
  用它做断言的话，即使决定行被清空用例照样会绿——那正是这条用例要钉住的失败。
  （这是 `docs/ARCHITECTURE.md` 第 6 节"全部守卫一起关掉才算证明用例咬住了"的直接应用。）
- `test_closeout_leaves_every_decision_column_byte_identical` —— 决定行逐列 `repr` 比对，
  收口前后完全相同。

### 丙 · 噪音止血

| 位置 | 内容 |
|---|---|
| `src/telegram_kol_research/recognition_execution_scanner.py` | `should_report_finding` / `finding_log_level` / `FINDING_REPORT_MIN_INTERVAL` / `OBSERVE_ONLY_FINDING_ACTIONS` / `reset_finding_report_throttle` |
| `src/telegram_kol_research/web_app.py` | 调用点用两者，`logger.error(...)` → `logger.log(finding_log_level(...), ...)`，外面套节流 |

- **节流**：同一 `(family, row_id)` 在 `FINDING_REPORT_MIN_INTERVAL`（30 分钟，与
  `source_deletion_exit_timeout.STUCK_EXIT_CAPTURE_MIN_INTERVAL` 相等，有用例钉住）内只打一行；
  `phase` 或 `action` 一变立刻打。状态放在进程内存里、**故意不持久化**——重启后每条再说一次，
  好过重启继承别人的沉默。形状照抄 `_should_capture`。
  那个 dict 每个曾上报过的 `(family, row_id)` 留一个小元组、不清理，与删除退出那个节流同样的取舍：
  漏掉一条的代价只是多打一行日志，不值得为它加一轮清扫。
- **分级是允许清单**：只有 `observe_only` / `observe_uncertain` 降为 `WARNING`。
  `family_scan_raised` / `inspection_raised` / `finalize_raised` 这些真异常，以及
  `*_cas_failed`、`expired_owner_still_alive`、`owner_not_alive_lease_active` 等，**全部保持 ERROR**。
  方向与 `NON_EXCHANGE_WRITING_EXECUTION_ACTIONS` 一致：以后新加的 action 默认是 ERROR，
  除非有人特意把它列进来。
- **runtime incident 一侧没有被节流**：`capture_recognition_execution_state` 仍然每一轮
  每一条都调，`runtime_incidents` 按指纹 coalesce 并累加 `repeat_count`。
  用例 `test_the_cycle_logs_a_frozen_row_once_but_captures_it_every_pass` 同时断言
  "5 轮只有 1 行日志"和"5 轮 5 次 capture"。

**写这类用例的一个坑，记在这里。** 两条断日志级别的用例最初用 `caplog`，单文件跑全绿、
全量跑**失败**：`app_logging.configure_application_logging` 会把
`logging.getLogger("telegram_kol_research").propagate` 设成 `False`，而且是**进程级、永久**的——
全量里只要有任何一个更早的用例调过它，之后这个 logger 树的记录就再也到不了 pytest 装在 root 上的
handler，`caplog.records` 空着，同时那行日志明明白白打在 stderr 上。
现在的做法是把一个自己的 handler 直接挂到 `web_app.logger` 上（`_Recorder`），
并强制该 logger 的 level，不依赖 propagate。
另有一条独立守护：用 `-p` 预先把 `propagate=False` 设好再跑这个文件，25 条仍全绿。

---

## 2. 终态取值与影响面

新增两个 `status` 取值，不是一个：

- `closed_no_write` —— 桶 A / B1；
- `closed_settled_binding` —— 桶 B2（真写过、绑定已终态）。

**为什么是两个（设计稿说"名字可议"，这是取舍的记录）。** 设计稿第 3 节只点名了
`closed_no_write` 一个状态，同时又要求 B2 用不同的收口原因区分。若两桶共用
`closed_no_write`，那条真写过的行的 `status` 就在说谎——正是设计稿自己用来否决复用
`failed_safe` 的那条理由。两个状态让 `status` 本身就能分桶，`error_summary` 只承担
"原因 + 日期"，没有任何一行的状态是假的。两者都以 `closed_` 开头，
`authoritative_execution_attempts.CLOSEOUT_STATUSES` 供消费者一次取到。

`error_summary` 追加 `closed_out=<status>@<YYYY-MM-DD>`，截断到 512。

### 受影响的消费者（逐条核过）

1. **`recognition_execution_scanner`** —— 扫描集合是
   `('claimed','executing','outcome_recorded','uncertain')`，收口后的行不在其中，不再被扫。
   **噪音源头消失。** 用例连跑两轮（游标归零重扫的那一轮才是会复发的一轮）断言零发现。
2. **`message_processing_backlog_expiry`** —— "仍活跃"守卫是
   `('executing','uncertain','outcome_recorded')`，收口后的行不再算活跃，**不再挡住积压过期**。
   **这是设计稿点名的、有意的行为改变**，用例
   `test_backlog_expiry_no_longer_counts_a_closed_out_attempt_as_active` 写明了它是有意的。
   需要同时知道的一点：同一个函数里还有一条
   `execution_uncertain_decision_present` 守卫看的是**决定行**，那条没动，
   所以单靠这次改动，一条被冻结消息的积压仍然过不了期。
3. **`authoritative_recognition.AutomaticRetryBlocked`（设计稿漏了这个消费者）** ——
   原来的判据是尝试状态 ∈ `{claimed, executing, uncertain}`。收口后若不管它，
   这道闸门就会对这 37 条消息**自动打开**，重试会先跑一遍识别（花一次 AI 调用），
   再在决定行那层被拒。等于悄悄松掉两把锁中的一把。
   **本次把两个收口状态也列进这道闸门**（`RETRY_BLOCKING_ATTEMPT_STATUSES`），
   行为与今天完全一致；以后真要解冻，必须同时对两行表态，那才是这种决定应有的门槛。
4. **`count_owned_durable_executions`** —— 只看 `('claimed','executing','outcome_recorded')`，
   `uncertain` 本来就不在里面，收口不改变它。
5. **`web_app` 的 worker-command 那套 `"uncertain"`** —— 是 `worker_command_jobs` 的状态，
   与本表无关。

### 必须知道的 schema 事实（设计稿没提，这是本次最大的一处补充）

`authoritative_execution_attempts` 有一条 **CHECK 约束**限定 `status` 取值，而
`authoritative_execution_schema.require_recognition_execution_schema` 会**逐字校验**
CHECK 签名，并且几乎每一次权威执行调用（claim / heartbeat / 冻结 / 扫描）都会先跑它。

所以"新增一个 status 取值"不是改一行模型就完事：只改模型的话，**生产库会立刻变成
`check_signature:authoritative_execution_attempts` 不合法，权威执行全线 fail closed。**
SQLite 不能 ALTER 一条 CHECK，必须重建表。

处理方式照抄仓库已有的先例
`db._widen_sqlite_entry_assembly_attempt_status_check`（2026-09-24 给
`entry_assembly_attempts` 加 `ready` 时写的，注释里就写着"新状态会在每个全新测试库上通过、
在唯一重要的那个库上 fail closed"）：新增
`db._widen_sqlite_authoritative_execution_attempt_status_check`，由 `init_db` 调用，
而 `init_db` 由 `create_session_factory` 调用，三个角色进程启动时都会走到。幂等判据是
DDL 里有没有 `'closed_no_write'` 字样。重建后按模型重建两个索引
（它们都在 `REQUIRED_INDEXES` 里，少一个同样会让校验 fail closed）。

**没有把两个 widen 函数抽成公共 helper**：老那个目前零测试覆盖，重构它的收益抵不上风险。
两份代码显式并列，便于审阅。新增的这份有用例
（`test_legacy_narrow_check_is_widened_at_startup`：先造一个窄 CHECK 的库，
断言校验不通过，再 `create_session_factory` 一次，断言通过、数据还在、索引还在）。

**由此得出的部署顺序约束**（下节的跑法据此写）：
**必须先部署、让服务重启一次把表重建好，再跑收口命令。** 反过来跑，
`build_uncertain_attempt_closeout_plan` 会在 `require_recognition_execution_schema`
处直接拒绝并打印 `check_signature:authoritative_execution_attempts`——fail closed，不会写坏任何东西。
收口命令自己用 `create_existing_session_factory`（不跑 bootstrap），
与 `expire-message-processing-backlog` 的先例一致。

重启窗口的小提示：`tg-deploy` 的顺序是 worker → web → ingest，worker 一起来就会重建表，
此后到 web/ingest 重启完成之间的几秒里，旧代码进程看到的是新 CHECK，
`require_recognition_execution_schema` 会对它们报 `check_signature` 不合法。
web/ingest 角色不认领权威执行，这几秒里最多是日志噪声，不会写坏数据。

---

## 3. 我该怎么在生产上跑（确切命令）

> 下面的命令**我（实现方）一条都没跑过**，包括 dry-run。按顺序执行。

**前置**：本分支的提交先按 `AGENTS.md` 的部署路径部署（push 到自己分支 → `tg-deploy <40 位 sha>`
→ 把该 sha 推到 `origin/main` → 双向核对）。服务重启会把 CHECK 重建好。

**先确认要用哪个账号**（下面一律写 `telegram-kol-worker`，它是
`deploy/systemd/telegram-kol-worker.service` 里的 `User=`；`/var/backups` 的写入可能需要 root）：

```bash
ls -l /opt/telegram-kol-analyzer/data/research.db
systemctl show telegram-kol-worker -p User --value
```

### 3.1 先备份（L3：这是一次生产数据改动）

```bash
sudo -u telegram-kol-worker sqlite3 /opt/telegram-kol-analyzer/data/research.db \
  "VACUUM INTO '/var/backups/research-pre-uncertain-closeout-$(date -u +%Y%m%dT%H%M%SZ).db'"
ls -l /var/backups/research-pre-uncertain-closeout-*.db
sha256sum /var/backups/research-pre-uncertain-closeout-*.db
```

（`VACUUM INTO` 也是这个项目做任何分析前的既定做法，见记忆里"生产库禁止全表扫描"那一条。）

### 3.2 dry-run（不写任何东西），把清单给用户过目

```bash
cd /opt/telegram-kol-analyzer
sudo -u telegram-kol-worker PYTHONDONTWRITEBYTECODE=1 \
  /opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research \
  close-out-uncertain-attempts \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  | tee /tmp/uncertain-closeout-dryrun.json
```

人眼要看的几件事：

```bash
python3 -c "
import json,sys
d=json.load(open('/tmp/uncertain-closeout-dryrun.json'))
print('mode', d['mode'], 'scanned', d['scanned_count'],
      'closeable', d['closeable_count'], 'refused', d['refused_count'],
      'exchange_write_count', d['exchange_write_count'])
for r in d['rows']:
    print(r['attempt_id'], r['raw_message_id'], r['chat_id'], r['sender_name'],
          r['bucket'], r['closeout_status'] or ('REFUSED:'+str(r['refusal_reason'])),
          r['event_types'], r['live_binding_ids'])
"
```

期望（按设计稿第 1.2 节的生产事实）：`scanned_count=37`，桶分布 29 / 6 / 2，
`closeable_count=37`，`refused_count=0`，`exchange_write_count=0`。
**如果 `refused_count>0`，先不要 apply**——说明有绑定还活着，那是另一件事。
**如果 `scanned_count≠37`**，说明这期间又新产生了 uncertain，同样先看清楚再决定。

### 3.3 apply（把 dry-run 数出来的 `closeable_count` 原样传进去）

```bash
cd /opt/telegram-kol-analyzer
sudo -u telegram-kol-worker PYTHONDONTWRITEBYTECODE=1 \
  /opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research \
  close-out-uncertain-attempts \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --apply --expected-count 37 \
  | tee /tmp/uncertain-closeout-apply.json
```

`--expected-count` 与事务内重算的 `closeable_count` 不等就**整体拒绝、退出码 2、一行不写**
（计划在 `BEGIN IMMEDIATE` 里重建，每一条更新都是 `(id, status='uncertain')` 的 CAS）。
`--apply` 不带 `--expected-count` 也直接拒绝。

### 3.4 收口后核对

```bash
sudo -u telegram-kol-worker sqlite3 /opt/telegram-kol-analyzer/data/research.db "
SELECT status, COUNT(*) FROM authoritative_execution_attempts GROUP BY status;
SELECT COUNT(*) AS decisions_still_frozen FROM recognition_decisions
  WHERE comparison_status='execution_uncertain';
"
```

期望：`uncertain` 归零，`closed_no_write` = 35，`closed_settled_binding` = 2，
**`decisions_still_frozen` 仍然是 37**（决定行一个字没动 —— 这一条是最重要的核对项，
它等于在生产上复现了乙的那条回归用例）。

### 3.5 回滚

- 只回滚数据：`tg-deploy` 不管数据。把 3.1 的备份拷回去即可（需停服）。
  或者更轻的办法，因为收口只改了 `status` / `error_summary` / `updated_at` 三列，
  且 `closed_*` 状态只有这条命令会写：
  ```sql
  UPDATE authoritative_execution_attempts
     SET status='uncertain'
   WHERE status IN ('closed_no_write','closed_settled_binding');
  ```
  （`error_summary` 的尾巴留着无害，它本来就是审计文字。）
- 只回滚代码：`tg-deploy <部署前的生产 HEAD>`。注意**代码回滚后 CHECK 仍然是宽的**，
  旧代码的 `require_recognition_execution_schema` 会因此判不合法 —— 所以
  **代码回滚必须连数据一起回滚**（把备份拷回去），这是本次改动唯一一处
  "代码与 schema 绑在一起"的地方，记在这里免得半夜只回一半。

---

## 4. 上线后怎么验证噪音归零

1. **立刻**（收口 apply 之后的一分钟内）：
   ```bash
   sudo journalctl -u telegram-kol-worker --since "-5min" \
     | grep -c "recognition execution finding" || true
   ```
   期望：0。收口后没有非终态行，扫描器一条都扫不出来。
2. **24 小时**：
   ```bash
   sudo journalctl -u telegram-kol-worker --since "-24h" \
     | grep "recognition execution finding" | wc -l
   ```
   期望：远小于改动前的约 27000。理想是 0；若期间新产生了 uncertain，
   每条每 30 分钟最多 1 行，一天上限 48 行/条。
3. **7 天观察窗（设计稿第 7 节）**：若出现新的 uncertain，检查它
   (a) 是 `WARNING` 不是 `ERROR`；(b) 30 分钟内只出现一次而不是每两分钟一次；
   (c) `runtime_incidents` 里那条记录的 `repeat_count` 仍在正常累加（说明节流没让账本漏记）：
   ```bash
   sudo -u telegram-kol-worker sqlite3 /opt/telegram-kol-analyzer/data/research.db "
   SELECT source_record_id, repeat_count, first_occurred_at, last_occurred_at
     FROM runtime_incidents
    WHERE incident_type='recognition_execution_orphan'
    ORDER BY id DESC LIMIT 10;"
   ```
4. 别用"日志安静了"当作系统健康的证据：这次改的就是"喊得太吵"，
   真正的健康判据仍是 `authoritative_execution_attempts` 里有没有新的 `uncertain` 行。

---

## 5. 明确没做

- 没解冻任何消息、没重放、没下单；
- 没改 `mark_authoritative_execution_uncertain` 的冻结语义；
- 没碰 `pending_entry` 的 3 小时限价单超时复核；
- 没追查"34 条 `evidence_refs_json` 为空"的根因（边界追踪器为何没记 evidence）——
  设计稿第 6 节说好另立项；
- 没部署、没推送、没在生产上跑任何命令（包括 dry-run）。

---

## 6. 提交

| 阶段 | 提交 | 内容 |
|---|---|---|
| 甲 + 乙 | `8071314e` | 收口模块 + CLI + 终态 + CHECK 放宽与重建 + 重试闸门 + 用例 + 本文档 |
| 丙 | 见最终回报（紧随其后） | 扫描器节流与分级 + web_app 调用点 + 用例 + 本文档更新 |

分成两个提交是为了回滚粒度：丙（噪音）可以单独回滚而不动收口能力，反之亦然。
唯一的例外在第 2 节最后一段：甲乙那个提交的**代码回滚必须连数据一起回滚**，
因为 CHECK 一旦放宽就不能只退代码。

测试：`python -m pytest -q` 全量，两个阶段各跑一次。

- 甲乙：**9719 passed, 4 skipped, 815s**。
- 丙：**9744 passed, 4 skipped, 806s**。

丙的第一次全量有 3 条红：2 条是我自己那两条断日志级别的用例（上面那个 `caplog` 坑，已改）；
第 1 条是 `tests/test_message_processing_worker.py::test_worker_loop_reloads_parallel_limit_before_each_refill`
断言三个并发 asyncio 任务的启动顺序 `started == [1, 2, 3]`，那次拿到 `[1, 3, 2]`。
**这条与本次改动无关**：它在甲乙那次全量里是绿的，单文件、单条、连跑 25 次、再加 6 个
`yes` 抢 CPU 连跑 15 次，全部绿；丙的第二次全量也是绿的。是这条用例本身的竞态
（断言的是并发任务谁先记录，不是并行度本身），已另立后台任务，不在本稿范围内。

---

## 部署与执行记录 · 2026-09-26（北京时间）

### 部署

- sha **`1c992eae`**（甲乙 = `8071314e`，丙 = `c96e6d8a`，外加并入 main 的四条他人文档提交），
  **回滚 sha `7b5f0053`**。
- **回滚有约束**：本次代码与 schema 绑定（CHECK 加宽 + 表重建）。回滚代码前要想清楚
  已经写成 `closed_no_write` / `closed_settled_binding` 的 37 行怎么办——
  旧代码的 schema 校验器会因 CHECK 签名不符而对权威执行全线 fail closed。
- 备份没做全库 `VACUUM INTO`：服务器磁盘 91% 已用、仅剩 4.9 GB，而本次写入只碰一张表。
  改为单表 dump：`/root/authoritative_execution_attempts.dump-20260927-014822.sql`（2.3 MB，4285 行）。

### 部署后实测（复核方自己跑的）

CHECK 已加宽为
`(status IN (...,'uncertain','closed_no_write','closed_settled_binding'))`；
4285 行完好；四个索引齐全（`ix_..._raw_message_id`、`ix_..._status_lease` 与两个 autoindex）；
worker/web/ingest active，无 `schema_invalid`、无 Traceback。

### dry-run → apply

```
dry_run: scanned 37 | closeable 37 | refused 0 | exchange_write_count 0
apply  : changed_count 37 | refused 0 | exchange_write_count 0
         transaction_lock_seconds 0.61 | manifest_sha256 2910df19...
```

分桶：`closed_no_write` 35（29 条无任何执行事件 + 6 条只有确认提醒/超时通知）、
`closed_settled_binding` 2（峰哥 ETH：attempt 319→绑定 340、attempt 360→绑定 342，
写入事件 `open_market_position` + `set_position_tpsl`，`live_binding_ids` 均为空）。

### 收口后核对（三条全部通过）

| 检查 | 结果 |
|---|---|
| `uncertain` 归零 | ✅ 状态表里已无该取值 |
| `closed_no_write` / `closed_settled_binding` | ✅ 35 / 2 |
| **`decisions_still_frozen`** | ✅ **仍为 37** —— 决定行一字未动，消息继续冻结 |

最后一条是本次交付的验收核心在生产上的复现：收口没有解冻任何消息，
因此不可能有 9 月的策略被重新识别进而下单。
