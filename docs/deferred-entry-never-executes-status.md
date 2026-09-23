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
| （本提交） | 本文档 |

**最终候选全量**：`uv run python -m pytest -q -p no:randomly` →
**9733 passed, 4 skipped, 107 warnings, 731.06s (0:12:11)，退出码 0**。
基线（`5c44498f`）为 9721 passed / 4 skipped，本次净增 12 条用例，零回归。
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

### 1.3 worker 定时调用（5.1.3）

加在 `web_app._run_recognition_execution_scanner_loop` 里——**worker 专属、60 秒一轮、
启动条件已含 `runtime_role in {"worker","all"}` 且 recognition execution schema 有效**的
那条既有循环。新增 `_run_entry_assembly_ready_wakeup_cycle_async(app)`：

- owner / registry 直接取 `app.state.recognition_execution_owner` /
  `app.state.recognition_execution_registry`，**与 `_run_authoritative_processor`
  同一处所有权来源**，没有另造 owner；
- 走 `_run_monitor_capture_writer`（单线程 executor + 取消时排空），因为这条路可能
  真的下单，既不能阻塞事件循环，也不能被取消撕在半路；
- 调用 `_run_entry_assembly_wakeups(..., completed_raw_message_id=None, ...)`；
- 角色不对、owner 为 None、`auto_trade_executor` 为 None 时直接返回。

### 1.4 对账器继续看得见 `ready`（5.1.4）

- 选择条件 `EntryAssemblyAttempt.status IN ("pending", "ready")`；
- `_load_attempt_snapshot` 放行 `pending` 与 `ready`；
- `_expire_deferred_entry_truth` 的 attempt CAS 改为 `status IN ("pending","ready")`
  （**不含 `claimed`**：那条归唤醒路径所有，rowcount 判据会把整个作废回滚掉）。

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

## 5. 新增用例（12 条）

`tests/test_deferred_entry_ready_execution.py`（新文件，7 条）

| 用例 | 对应设计第 6 节 |
|---|---|
| `test_reconciled_entry_reaches_the_order_ledger_through_the_periodic_claim` | 1（核心） |
| `test_three_production_deferral_shapes_each_submit_exactly_once` | 2 |
| `test_blocker_completion_still_wakes_and_submits_as_before` | 3 |
| `test_reconciler_first_then_wakeup_submits_exactly_once` | 4 |
| `test_wakeup_first_then_reconciler_submits_exactly_once` | 4 |
| `test_a_claimed_attempt_is_invisible_to_the_reconciler` | 4 |
| `test_ready_attempt_expires_through_the_entry_channel_with_one_alert` | 5 |

`tests/test_message_instruction_items.py`
`test_management_expiry_sweep_never_rewrites_an_entry_item` —— 设计第 6 节第 6 条
（同一批条件下 management 项仍被改写，entry 项不再被碰，防 2.1 改过头）。

`tests/test_web_app.py`
`test_worker_scanner_loop_also_claims_reconciler_ready_entry_wakeups`（定时轮确实调了，
且 `completed_raw_message_id is None`、owner/registry 就是 app.state 那两个）、
`test_web_role_scanner_cycle_never_claims_an_entry_wakeup`。

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

## 7. 我认为设计没覆盖到的地方（**未自行改动，交主会话判断**）

1. **入场项的到期现在只有消息驱动这一条路。** `reconcile_due_entry_admissions`
   当前只在 `apply_authoritative_assessment` 里被调用，也就是「有消息被处理」才跑。
   阶段 2.1 之后，管理过期器不再兜底入场项，于是**群里长时间没有新消息时，一条过了
   6 小时 deadline 的入场项会一直停在 `pending`，既不执行也不作废，告警也不发**。
   之前它至少会被杀掉（虽然原因码是错的）。
   代价对比：错误告警 → 静默滞留。二者都不好。
   最小的补法是把对账器也挂进 1.3 那条 60 秒循环（我没做，因为设计没写，
   而且它会让对账器的触发频率发生变化，属于新增运行时语义）。
   **建议部署前决定这一条。**
2. **`ENTRY_ADMISSION_EXECUTION_DEADLINE` 与 `VISIBILITY_RETRY_DEADLINE` 都是 6 小时。**
   设计第 3 节把「6 小时后被管理过期器杀掉」当成入场自己的 deadline 之外的事，
   实际上两者数值相同，谁先到是竞速。阶段 2.1 之后这个巧合消失了（只剩入场自己的），
   但如果哪天有人改其中一个，另一边的注释不会提醒他。
3. **设计第 6 节第 2 条要「回放三条真实样本（attempt 29/25/20）」。** 生产库不在
   这个 worktree 里，我回放的是它们的**形状**（1/2/3 个相邻 blocker、全部由对账器放行），
   不是它们的真实行。用例名与 docstring 都写明了这一点。
   若要真正回放，需要一份生产快照（按 `docs/` 的规矩先 `VACUUM INTO`，不要直接扫生产库）。
4. **设计 3.2 那条活检告警（`ready` 超 10 分钟无执行行）属于阶段 3，本次没做。**
   在它落地之前，第 7.1 条那种静默滞留没有任何信号。

## 8. 部署前还需要的

- 阶段 1+2 必须一起部署（2.1 单独上会让入场项永不到期）。回退点 `e0c29263`。
- 有 schema 变更（CHECK 重建），按 AGENTS.md 属 **L3**：需要变更与回滚方案、
  生产库副本上预演、备份 + `PRAGMA quick_check` + `entry_assembly_attempts` 的
  前后行数对照。
- 部署后 L2 观察窗 30 分钟，自然样本上限 24 小时；核对两件事：该 attempt 有
  `entry_assembly_wakeup_executions` 行、`execution_events` 有对应下单。
- 只读回归查询：`status='ready'` 且超过 10 分钟没有执行行的 attempt 应恒为 0。
