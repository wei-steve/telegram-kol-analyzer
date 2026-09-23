# 被相邻消息推迟的入场：整单丢失 —— 实施状态

- **阶段 1（5.1.1–5.1.4）与阶段 2（5.2.1–5.2.3）**：已完成实现，
  **未部署、未推送、未连服务器、未触碰交易所、未改自动交易开关、未改 MQTT/Telegram 通知**。
- **阶段 3（5.3.1 / 5.3.2）**：未开始，本次一行不碰。设计稿在 3.1 留了两个互斥选项
  （新增终态 `executed` ／ 改所有读者去读 `entry_assembly_wakeup_executions`），
  由主会话审阅后再定。
- **分支 / worktree**：`worktree-agent-a99a926faa48f69a3`
  （`/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a99a926faa48f69a3`）
- **基线**：`codex/deepcoin-auto-trading-v1` 尖端 `5c44498f`（设计稿提交）。
  线上回退点 `e0c29263`（设计稿第 3 节所记）。
- **约束性文档**：`docs/plans/2026-09-24-deferred-entry-never-executes-design.md`
  （冲突时以它为准）。

| 提交 | 主题 |
|---|---|
| `b2c99aab` | 阶段 1 + 阶段 2 全部代码与用例（两者必须一起部署，故合成一个提交） |
| `f7d96e14` | 本文档第一版 |
| `ee0f9e0a` | 补全 1.4：把对账器挂上同一条 60 秒循环（主会话 2026-09-24 裁决），并给两个 6 小时常量加互指注释 |

**最终候选全量**：`uv run python -m pytest -q -p no:randomly` →
**9741 passed, 4 skipped, 107 warnings, 732.61s (0:12:12)，退出码 0**。
基线（`5c44498f`）为 9721 passed / 4 skipped，本次净增 20 条用例，零回归。
（`uv run pytest` 不带 `python -m` 仍在收集阶段报 `No module named 'tests'`，
既有问题，与本次无关。）

---

## 1. 阶段 1 做了什么

### 1.1 对账器不再宣布唤醒（5.1.1）

`entry_admission_reconciler.reconcile_due_entry_admissions` 的 release 分支
（原 `:198-213`）现在：清 visibility 延迟 → **CAS `status: pending → ready`**，
同一条 UPDATE 里把 `blocking_raw_message_ids_json` 清成 `"[]"`，不再写 `woken`、
不再写 `woken_at`。CAS 条件 `status == "pending"`，rowcount 不为 1 就整段回滚。

`woken` 从此只由 `finalize_recorded_wakeup` 在执行成功后写。

### 1.2 唤醒认领器接受第二个触发源（5.1.2）

`entry_assembly_admission.claim_ready_entry_assembly_wakeups`：

- 签名改为 `completed_raw_message_id: int | None = None`；
- 选择集 `status IN ("pending", "ready")`；
- `ready` 分支无条件可认领，CAS `status == "ready" → "claimed"`，不看 blockers；
- `completed_raw_message_id is None` 且遇到 `pending` 项 → 跳过（定时轮不代表任何消息）；
- 认领成功后两条路径走**同一个** `_finish_entry_assembly_wake_claim`：
  释放 visibility、算 `wake_generation`、建 `entry_assembly_wakeup_executions` 子围栏、
  返回 `EntryAssemblyWakeClaim`。`claim_limit = 1` 不变。

### 1.3 worker 定时调用（5.1.3，+ 主会话裁决的 1.4 补全）

都加在 `web_app._run_recognition_execution_scanner_loop` 里——**worker 专属、60 秒一轮、
启动条件已含 `runtime_role in {"worker","all"}` 且 recognition execution schema 有效**的
那条既有循环。一轮的顺序是 **scan → 对账 → 认领**，三段各自独立 try/except 失败开放，
互不拖累：

**(a) `_run_entry_admission_reconcile_cycle_async(app, observed_at=...)`（新增，2026-09-24
主会话裁决）**

- 调 `reconcile_due_entry_admissions`，参数与消息驱动那处（`authoritative_recognition.py`
  `apply_authoritative_assessment` 内）**逐个对齐**：`limit=10`、
  `execution_contract_mode` 与 `entry_after_item_id` 都从 `load_trading_settings` 取，
  **没有硬编码**，所以一个灰度开关同时管住两个触发源；
- `now` 用循环自己的 `observed_at = app.state.now_provider()`，与 scan 同一时刻；
- 角色不对或 owner 为 None 直接返回（`web` 角色永不对账）；
- 它**不碰交易所**，A-3d 的边界没动。

**(b) `_run_entry_assembly_ready_wakeup_cycle_async(app)`**

- owner / registry 直接取 `app.state.recognition_execution_owner` /
  `app.state.recognition_execution_registry`，**与 `_run_authoritative_processor`
  同一处所有权来源**，没有另造 owner；
- 调用 `_run_entry_assembly_wakeups(..., completed_raw_message_id=None, ...)`；
- 角色不对、owner 为 None、`auto_trade_executor` 为 None 时直接返回。

两段都走 `_run_monitor_capture_writer`（单线程 executor + 取消时排空）：认领那段可能
真的下单，既不能阻塞事件循环，也不能被取消撕在半路；对账那段写的是指令项与契约的终态，
同样不该被撕断。

**先对账再认领**是有意的：同一轮里刚被 promote 成 `ready` 的 attempt 当场就能被执行，
不必再等一分钟。`tests/test_web_app.py::test_worker_scanner_loop_also_claims_reconciler_ready_entry_wakeups`
断言的就是这个顺序。

### 1.4 对账器继续看得见 `ready`（5.1.4）

- 选择条件 `EntryAssemblyAttempt.status IN ("pending", "ready")`；
- `_load_attempt_snapshot` 放行 `pending` 与 `ready`；
- `_expire_deferred_entry_truth` 的 attempt CAS 改为 `status IN ("pending","ready")`
  （**不含 `claimed`**：那条归唤醒路径所有，rowcount 判据会把整个作废回滚掉）。

**1.4 只改选择条件是不够的**——对账器当时只在 `apply_authoritative_assessment` 里被调用，
也就是只在有消息被处理时才跑，接不住「入场的到期由对账器接管」。1.3(a) 的定时调用是
1.4 的另一半，2026-09-24 由主会话裁决补上。它顺带还捡起了设计第 1 节统计到、但没单独
归因的另一条丢失路径：`attempt=pending / item=failed` 那 9 条（从未被唤醒过）——
定时重评准入会重新判定它们。

## 2. 阶段 2 做了什么

- **5.2.1**：`message_instruction_items.claim_next_visibility_retry_instruction_item`
  开头那段批量过期 UPDATE 加上 `instruction_kind == "management"`，与它下面的认领
  SELECT 对齐。
- **5.2.2**：入场项的到期从此只由 1.4 的对账器 deadline 分支负责，原因码沿用既有的
  `entry_admission_deadline_expired`（未新增码）。
- **5.2.3**：`oncall_alerts.REASON_LABELS`
  —— `target_strategy_binding_visibility_retry_expired` 改为
  「改仓位时一直没找到对应的持仓记录，重试超时」（现在只有管理项会拿到它）；
  新增 `entry_admission_deadline_expired` /
  `entry_admission_recheck_blocked` / `entry_admission_recheck_state_mismatch`
  三条入场自己的文案。

## 3. schema 迁移

`models.EntryAssemblyAttempt` 的 `ck_entry_assembly_attempts_status` 加入 `'ready'`。

SQLite 把 CHECK 写在建表语句里，`create_all` 遇到已存在的表什么都不做，
**所以新状态在每个新建的测试库上都能过、在唯一重要的那个库上会 fail closed**。
`db._widen_sqlite_entry_assembly_attempt_status_check()`（`init_db` 里调用，紧跟
既有的 `_make_sqlite_entry_assembly_preamble_nullable`）做标准的 12 步重建：

1. 读 `sqlite_master.sql`；含 `'ready'` 即已迁移，直接返回（幂等）；
2. 若表里有新约束集合之外的 status 值 → **记 error 后跳过重建**，保持库可读，
   交给审计过的修复（沿用 `_backfill_sqlite_indexes` 的先例）；
3. `CreateTable(EntryAssemblyAttempt.__table__)` 编译出 DDL（从模型生成，不手写，
   免得跟列定义漂移），只把 `CREATE TABLE entry_assembly_attempts ` 这个表头改名；
4. `PRAGMA foreign_keys=OFF` → `BEGIN IMMEDIATE` → 建新表 → 全列 INSERT…SELECT →
   DROP 旧表 → RENAME；
5. 重建时索引跟着旧表一起没了，事后用模型声明的 `table.indexes` 逐个
   `create(checkfirst=True)` 补回。

**现网既有行的影响**：29 行全部原样搬过去，status / fingerprint / 时间戳一字不改；
`shadow/pending/claimed/woken/expired` 仍然合法，只是多了一个 `ready`。
`entry_assembly_wakeup_executions.entry_assembly_attempt_id` 的外键文本指向表名，
DROP+RENAME 之后名字一致，引用不变（重建期间 `foreign_keys=OFF`）。
唯一的不可用窗口是重建那一个事务，发生在进程启动时。

## 4. 两条路径为什么不会重复下单

**互斥点只有一个：`entry_assembly_attempts.status` 上的那一次 CAS。**

| 触发源 | CAS 条件 | 目标 |
|---|---|---|
| blocker 消息完成 | `status == "pending" AND blockers == <读到的那串>` | `claimed` |
| 对账器判定通过 | `status == "pending"` | `ready` |
| 定时轮 / 任意唤醒调用 | `status == "ready"` | `claimed` |
| deadline 作废 | `status IN ("pending","ready")` | `expired` |

`claimed` 不在任何一条的条件里，所以一旦有人认领成功，其余三条的 rowcount 都是 0，
各自回滚、什么都不做。`claimed → woken` 由 `finalize_recorded_wakeup` 在
`status == "claimed" AND wake_claim_token == <本次 token>` 下写，
`woken` 又不在任何认领条件里，于是执行过的 attempt 再也不会被认领第二次。
子围栏 `entry_assembly_wakeup_executions` 另有 `(attempt_id, wake_generation)`
与 `claim_token` 两个唯一约束兜底。

证明它的用例（`tests/test_deferred_entry_ready_execution.py`）：

- `test_reconciler_first_then_wakeup_submits_exactly_once`：对账器先到 → 定时认领执行 →
  紧接着 blocker 消息的唤醒再跑一遍同一条 attempt；
- `test_wakeup_first_then_reconciler_submits_exactly_once`：唤醒先到执行 → 对账器再跑
  （`released == 0`）→ 定时轮再跑一遍；
- `test_a_claimed_attempt_is_invisible_to_the_reconciler`：`claimed` 的 attempt
  对账器一个字段都不碰。

三条的断言都是 `execution_events` 里 `create_limit_entry` 恰好一行、
`entry_assembly_wakeup_executions` 恰好一行，**不是 attempt 状态**。

## 4b. 对账器现在有两个调用点，为什么不会双写

消息驱动（`apply_authoritative_assessment`）与定时（1.3(a)）都在 **worker 进程内**，
可以真正同时跑。逐段核对：

| 段 | 事务边界 | 并发判据 | 输家会发生什么 |
|---|---|---|---|
| 选择 | 只读，自己的 session | 无 | 两边可能选到同一条，后面每一步各自拦 |
| `_load_attempt_snapshot` | 只读 | `status in {pending, ready}` | 已被认领/作废 → 返回 None，跳过 |
| release 分支 | **一个事务**：清 visibility 的 item UPDATE + attempt CAS | `status == "pending"` | rowcount≠1 → `session.rollback()`，**item 的 visibility 清除一并回滚**，不会出现「清了 visibility 却没 promote」 |
| `_expire_deferred_entry_truth` | **一个事务**：contract CAS（含 `state_version` 乐观锁）+ item CAS + attempt CAS + transition INSERT | 三个 rowcount 都必须是 1 | 任一不为 1 → 整体 rollback，返回 False；**不会有第二条 transition、第二次 item 改写、第二次 attempt 写** |
| 告警 | 在作废提交之后 | 只有 `_expire_deferred_entry_truth` 返回 True 才发 | 输家根本不发 |
| ws 推迟释放 | 单条 CAS | `visibility_next_attempt_at IS NOT NULL` | rowcount≠1 → 记 `skipped` |
| 作废 vs. ready 认领 | 各自一个事务 | 作废要 `IN (pending, ready)`，认领要 `== ready` | 谁先提交谁赢，输家整体回滚；**不可能既作废又下单** |

SQLite 的 WAL + `busy_timeout=30000`（`db._configure_sqlite`）让第二个写事务在第一个
提交前阻塞，而不是立刻失败；醒来后它读到的是已改过的状态，CAS 自然落空。

**`limit` 有界**：`bounded_limit = max(0, min(int(limit), 100))`，两个调用点都传
`limit=10`，即一轮最多 10 条 attempt + 10 条 ws 推迟项。定时轮 60 秒一次，
消息驱动那条频率不变。`ready` 的 attempt 已经跳过最重的
`assess_entry_assembly_admission`（第 6.2 条决定），所以新增负载就是每分钟一次有界扫描。

证明它的用例：

- `test_two_concurrent_reconciles_promote_one_attempt_exactly_once`
  —— 两个线程同时 release，`sum(released) == 1`；
- `test_two_concurrent_reconciles_expire_one_attempt_exactly_once`
  —— 两个线程同时作废，`sum(expired) == 1`、`sum(incidents) == 1`、
  `InstructionExecutionTransition` 恰好 1 行；
- `test_two_concurrent_reconciles_leave_a_still_blocked_attempt_alone`
  —— 两个线程同时重评一条仍被阻塞的 attempt：`_persist_attempt` 的 fingerprint 唯一约束
  不会造出第二行，状态不动；
- `test_expiry_racing_the_ready_claim_never_does_both`
  —— 作废与 ready 认领同时跑，`(attempt.status == "expired") != (下单数 == 1)`。

## 5. 新增用例（20 条）

`tests/test_deferred_entry_ready_execution.py`（新文件，15 条）

| 用例 | 覆盖什么 |
|---|---|
| `test_reconciled_entry_reaches_the_order_ledger_through_the_periodic_claim` | 设计 6.1（核心） |
| `test_three_production_deferral_shapes_each_submit_exactly_once` | 设计 6.2（形状回放，见 7.3） |
| `test_blocker_completion_still_wakes_and_submits_as_before` | 设计 6.3 |
| `test_reconciler_first_then_wakeup_submits_exactly_once` | 设计 6.4 |
| `test_wakeup_first_then_reconciler_submits_exactly_once` | 设计 6.4 |
| `test_a_claimed_attempt_is_invisible_to_the_reconciler` | 设计 6.4 |
| `test_ready_attempt_expires_through_the_entry_channel_with_one_alert` | 设计 6.5 |
| `test_deadline_passed_entry_expires_without_any_message_being_processed` | **1.4 补全的退化本身**：全程不调 `apply_authoritative_assessment` |
| `test_one_worker_cycle_reconciles_then_executes_the_same_attempt` | 一轮内先对账后认领 |
| `test_worker_cycle_reconciles_nothing_while_the_contract_mode_is_disabled` | 灰度开关仍然管得住定时轮 |
| `test_web_role_never_reconciles_entry_admissions` | `web` 角色不对账 |
| `test_two_concurrent_reconciles_promote_one_attempt_exactly_once` | 两调用点并发 |
| `test_two_concurrent_reconciles_expire_one_attempt_exactly_once` | 两调用点并发 |
| `test_two_concurrent_reconciles_leave_a_still_blocked_attempt_alone` | 两调用点并发（重评路径） |
| `test_expiry_racing_the_ready_claim_never_does_both` | 作废 vs. 认领 |

`tests/test_message_instruction_items.py`
`test_management_expiry_sweep_never_rewrites_an_entry_item` —— 设计第 6 节第 6 条
（同一批条件下 management 项仍被改写，entry 项不再被碰，防 2.1 改过头）。

`tests/test_web_app.py`
`test_worker_scanner_loop_also_claims_reconciler_ready_entry_wakeups`（一轮里**先对账、
后认领**，且认领的 `completed_raw_message_id is None`、owner/registry 就是 app.state
那两个）、`test_web_role_scanner_cycle_never_claims_an_entry_wakeup`。

`tests/test_db_migrations.py`
`test_legacy_entry_assembly_attempt_status_check_gains_ready`（旧约束的库重建后能写
`ready`，行与索引都在）、`test_entry_assembly_attempt_status_rebuild_is_idempotent`。

改动的既有用例：`tests/test_entry_admission_reconciler.py` 里 5 处
`status == "woken"` 改成 `"ready"`——这正是 1.1 要改的语义，不是迁就实现。

## 6. 设计没写、我自己定的（都往「改动最小」那边靠）

1. **`ready` 认领的 `trigger_raw_message_id` 用 attempt 自己的
   `strategy_raw_message_id`。** 该列 NOT NULL 且有外键，设计没说这一路填什么。
   真正的 blocker 永远不可能是策略消息本身（`_load_source_facts` 明确排除
   `RawMessage.id != strategy.id`），所以这个值同时也是「这条认领来自对账器」的标记。
   备选是把列改成 nullable，那是一次多余的 schema 改动。
2. **`ready` 的 attempt 对账器不再重跑准入评估**，只过 deadline 分支。设计 1.4 给出的
   理由就是 deadline。若照旧重评，一条**晚到的相邻消息可以把已经放行的入场重新判成
   blocked 并作废**——那是 release 分支从来没有过的权力，属于新增语义，不做。
3. **执行失败回滚时 attempt 回到它原来的状态**（`_finish_entry_assembly_wake_claim`
   的 `revert_status`）：pending 路回 `pending`，ready 路回 `ready`。不会造成死循环
   （回 `ready` 的那次 `claimed` 为空，认领函数直接返回，`_run_entry_assembly_wakeups`
   的 while 循环随即结束）。
4. **`fail_safe_wakeup` 一个字没改。** 它把 attempt 打回 `pending` 并把 blockers 写成
   `[trigger]`；ready 路的 trigger 是策略消息自己，于是那条 attempt 变成「pending +
   一个假 blocker」。下一轮对账器（选择集含 pending）重新评估，通过就再置 `ready`，
   设计里的修复路径自己就能把它接回来，不需要额外代码。
5. **定时轮的频率取 60 秒**（宿主循环的既有周期）。设计说「频率与现有对账轮一致即可」，
   而现有对账其实是消息驱动的；60 秒是这条既有 worker 循环的周期，也是唯一一条同时
   持 owner/registry、worker 专属、且不会每 0.5 秒扫一次库的循环
   （`message_processing_worker` 是 0.5 秒，放这种活不合适）。
6. **`defer_message_instruction_item_for_visibility` 里那段同样 6 小时的作废判断没动。**
   设计 2.1 只点名 `:400` 的批量 UPDATE。那一段只在「指令项正在 executing 且这次又被
   推迟」时触发，与本次的丢失路径无关，改它属于扩大范围。
7. **定时对账的 `now` 取循环自己的 `observed_at`（`app.state.now_provider()`），
   不是 `utc_now()`。** 两者默认完全等价（都是 `datetime.now(UTC)`），但前者是这条
   循环里既有的时间源，scan 与对账因此共用同一时刻，测试也能注入。
8. **定时对账不传 `incident_reporter`**，与消息驱动那处一致，走
   `runtime_incident_adapters` 的默认通道。

## 7. 设计没覆盖到的地方

1. ~~入场项的到期现在只有消息驱动这一条路。~~ **已修（2026-09-24 主会话裁决）。**
   阶段 2.1 之后管理过期器不再兜底入场项，而对账器只在 `apply_authoritative_assessment`
   里被调用，于是群里长时间没新消息时，一条过了 6 小时 deadline 的入场项会一直停在
   `pending`，既不执行也不作废、告警也不发——把「错的告警」换成「静默停摆」是更糟的方向。
   裁决：按最小修法把 `reconcile_due_entry_admissions` 挂上 1.3 那条 60 秒循环
   （见 1.3(a)），算 1.4 的补全而非扩大范围。
   **补丁前该用例的红是实打实的**：`test_deadline_passed_entry_expires_without_any_message_being_processed`
   在只有空壳 cycle 的情况下报 `AssertionError: assert 'pending' == 'failed'`。
2. ~~两个 6 小时常量互不指认。~~ **已加注释（只加注释，未改数值）**：
   `entry_assembly_admission.ENTRY_ADMISSION_EXECUTION_DEADLINE` 与
   `message_instruction_items.VISIBILITY_RETRY_DEADLINE` 各自说明对方是谁、
   这个巧合曾经怎么变成竞速，以及 2.1 之后两者为什么再也碰不到（管辖的指令项种类不相交）。
3. **设计第 6 节第 2 条要「回放三条真实样本（attempt 29/25/20）」。** 生产库不在
   这个 worktree 里，我回放的是它们的**形状**（1/2/3 个相邻 blocker、全部由对账器放行），
   不是它们的真实行。用例名与 docstring 都写明了这一点。
   主会话已接受，真实回放归部署后核对。
4. **设计 3.2 那条活检告警（`ready` 超 10 分钟无执行行）属于阶段 3，本次没做。**
   在它落地之前，一条 `ready` 却始终没人认领的 attempt（例如 worker 循环整个停摆）
   仍然只有到 deadline 才会说话。
5. **新发现，未改动：`_persist_attempt`（`entry_assembly_admission.py:559`）会把
   `woken` 的 attempt 重置回 `pending`。** 它的条件是
   `existing.status in {"shadow", "pending", "woken"}`——`ready` 不在里面（安全），
   但 `woken` 在。也就是**一条已经下过单的 attempt 被重新评估时会被重新武装**。
   本次改动没有让它更容易发生：对账器的选择集是 `(pending, ready)`，`woken` 选不中；
   `ready` 又在评估之前就 `continue` 了。所以它只能由消息驱动的重新识别触发，
   与改动前一样。**阶段 3.1 如果采纳「新增终态 `executed`」，这一条会顺带被修掉**
   （`executed` 不在那个集合里）；如果采纳另一个选项，建议单独把 `woken` 从这个集合
   里拿掉。

## 8. 部署前还需要的

- 阶段 1+2 必须一起部署（2.1 单独上会让入场项永不到期）。回退点 `e0c29263`。
- 有 schema 变更（CHECK 重建），按 AGENTS.md 属 **L3**：需要变更与回滚方案、
  生产库副本上预演、备份 + `PRAGMA quick_check` + `entry_assembly_attempts` 的
  前后行数对照。
- 部署后 L2 观察窗 30 分钟，自然样本上限 24 小时；核对两件事：该 attempt 有
  `entry_assembly_wakeup_executions` 行、`execution_events` 有对应下单。
- 只读回归查询：`status='ready'` 且超过 10 分钟没有执行行的 attempt 应恒为 0。
- 定时轮是 worker 进程的第二个对账触发源，观察窗里顺带看一眼 worker 的事件循环延迟
  （`/api/runtime/loop-health`）：对账跑在 `_run_monitor_capture_writer` 的独立线程里，
  不应该出现新的卡顿；若出现，先看是不是 `assess_entry_assembly_admission` 在扫相邻消息。
