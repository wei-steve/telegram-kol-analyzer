# 对账每轮刷新 strategy_lifecycles.updated_at：核实与方案

- 日期：2026-09-30
- 状态：**方案待用户确认**（确认前不写代码）
- 分支：`claude/reverent-gagarin-0143e2`（已快进到 origin/main `91f02f49`，即绑定 updated_at 修复 `5b0221c8` 落地后的版本；生产同为 `91f02f49`）
- 验证级别：L2（改的是 worker 对账写入路径，而且会改变上下文解析这条权威路径的一个输入；不改表结构、不碰交易所写入）
- 来源：`docs/plans/2026-09-30-binding-updated-at-churn-design.md` §1.4、§6 问题 2（用户选了「另开任务」）

## 1. 源头

| 位置（`91f02f49`） | 语句 | 何时执行 |
|---|---|---|
| `execution_bindings.py:6365`（`_attach_binding_to_lifecycle` 末尾） | `lifecycle.updated_at = updated_at` | 对账 `_derive_binding_from_entry_legs` 的 `active` 分支（`:4183`），**每轮**、对每条有已核实持仓的绑定 |
| `execution_bindings.py:6342`（同函数，终态已退出分支） | 同上 | 同上，生命周期已是终态（如 `kol_signal` 退出后仓位还在）时 |
| `execution_bindings.py:6324`（同函数，过期未入场分支） | 同上 | 真实状态变化（→ `expired`），本来就该写 |
| `execution_bindings.py:4264`（`_mark_lifecycle_pending` 末尾） | 同上 | 对账 `open` 分支（`:4231`），**每轮**、对每条入场单还挂着的绑定 |

两个函数各只有一个调用点，都在 `_derive_binding_from_entry_legs` 里，都传 `recovered_at`（本轮时刻）。前面对 `lifecycle_status`、`execution_binding_id` 等的赋值在值相同时不会产生 UPDATE，真正让行变脏的就是末尾这句时间戳。

**生产点查（2026-09-30 07:34–07:35Z，按主键只读）**：当前唯一持仓生命周期 1377（BTC，绑定 392 `active`）的 `updated_at` 依次为 `07:34:40.1 → 07:34:58.7 → 07:35:16.7 → 07:35:30.1`，间隔 13–19 秒，与对账轮次一致。`strategy_threads.updated_at` 不被对账写（`execution_bindings.py` 里没有 `StrategyThread`）。

## 2. 核实：「上下文重解析因此多跑」——机制属实，影响极小

### 2.1 机制（读代码）

- 生命周期监视器每个周期都对**每个有活跃信号的群**发 `exchange_snapshot_changed`（`lifecycle_monitor.py:728-733`，名字叫 changed，实际是无条件发）。
- `schedule_context_reanalysis` 把该群里「决策为 `unresolved`/`hold`、且模型声明了 `exchange_state_changed` 触发器」的尝试行改成 `pending_reanalysis`。
- worker 认领后重算指纹，**和上次相同就跳过**（`context_resolution_worker.py:643-656`，记 `completed`，不调 AI）。这道指纹比较是防止每个周期都调一次 AI 的唯一一道闸。
- 指纹包含候选线程当前生命周期的 `updated_at`（`:139-146`、`:186-194`）。候选里只要有正在持仓（或入场单挂着）的生命周期，指纹每轮都变，这道闸就失效，下一次事件必然真调 AI。
- 兜底：每条消息 24 小时内最多 5 行尝试（`DEFAULT_MAX_REANALYSIS_PER_MESSAGE`），超过记 `reanalysis_capped`，该行从此不再被调度（调度只挑 `completed`/`pending_reanalysis`）。

### 2.2 生产数据（`VACUUM INTO` 快照，2026-09-01 至 09-30）

- 上下文尝试：约 2–9 次重解析 / 天；由 `exchange_snapshot_changed` 或 `entry_leg_status_changed` 触发的重解析**共 41 次**。
- 其中 25 次在两次解析之间同群有新消息——指纹里的 `latest_same_chat`（同群最新一条消息）本来就会变，与本问题无关。
- 剩下 16 次：8 次是 `entry_leg_status_changed`（生命周期状态真变了，重解析应当发生）；8 次是 `exchange_snapshot_changed`。逐条对照当时候选线程的生命周期是否有持仓 / 挂单：
  - **对得上本问题的共 3 次**（前两次在这 8 次里；第三次期间同群也有新消息，属于两个原因都可能）：
    - 尝试 6886（消息 19794，09-29 13:35→13:36）：候选线程 721 → 生命周期 1352 → 绑定 388，其入场腿 662 持仓至 09-29 16:33 才人工平掉。**真调了 1 次 AI**（新行 6887），新行没再声明状态类触发器，链条就此结束。
    - 尝试 6675（消息 18895，09-24 15:12→15:15）：候选线程 688 → 生命周期 1319 持仓中（14:45 入场、16:45 平仓）。**没调 AI**，直接 `reanalysis_capped`（该消息 24 小时内已有 5 行）。
    - 尝试 6549（消息 18479，09-23 05:58→06:00）：候选线程 657 → 生命周期 1288，绑定 371 入场单挂着（06:21 才取消）。**没调 AI**，`reanalysis_capped`。
  - 这 8 次里其余 6 次（4686、5859、6234、6242、6861、6871）候选里没有持仓 / 挂单，是别的指纹输入变了，与本问题无关。
- 反证：指纹跳过在无持仓时确实在起作用，例如尝试 6355 从 09-18 到 09-21 被 `exchange_snapshot_changed` 反复调度、每次都跳过（`updated_at` 一路前进、没有新行）。

### 2.3 结论

- **多花 AI 调用：一个月 1 次**（约 0.03 次 / 天），可以忽略。原因是触发面很窄：要同时满足「决策 unresolved/hold」「模型声明了 `exchange_state_changed`」（一个月约 20 行）「候选线程里正好有持仓 / 挂单的生命周期」；重解析后的新行通常不再声明状态类触发器；还有 5 次 / 24 小时的上限。
- **比 AI 费用更实在的副作用**：虚假触发会把本已接近上限的消息直接打成 `reanalysis_capped`，而 capped 行此后永远不再被调度——之后即使交易所状态真的变了，它也不会再被重看（6549、6675 两例）。
- 所以本修复的主要收益在网页和数据语义（见第 3 节），上下文解析这边是「消除一个偶发的错误触发」，不是省钱。

分析快照 `/var/backups/telegram-kol/20260930-lifecycle-churn/snap.db` 用完已删：489922560 字节，sha256 `3ee97942c36487bb1bbd3b9a06b0155328f919795730a917d711c3febe6172fb`。本机未留快照。

## 3. 读者清单与影响

`src/`、`scripts/`、`deploy/` 全量搜索（Sonnet 5 子代理考古，关键几处由本会话逐一复核）。`scripts/`、`deploy/`、值守（`oncall_*`）、保留期（`db_retention`、`media_retention`）、管理规划器、消息执行监督、管理目标核验**都不读**生命周期的 `updated_at`。**没有任何代码把它当存活 / 新鲜度信号**（没有 `now - lifecycle.updated_at < X` 之类的判断），所以不存在「修了之后以为持仓死了」的风险。

| 位置 | 用途 | 现在 | 修复后 |
|---|---|---|---|
| `context_resolution_worker.py:146/191` 上下文状态指纹（及 `context_resolution.py` 存库指纹、`context_analysis_backfill.py` 的过期校验） | 指纹没变就跳过重解析 | 候选含持仓 / 挂单时指纹每轮变（见第 2 节） | 只随真实内容变化；**这是对权威路径输入的改变**，方向是让「跳过」按设计生效。指纹里的 `updated_at` 保留，它仍能捕获 `entered_at`、止损止盈价等不在指纹显式列里的真实变化 |
| `web_live_state.py:136` strategies 版本号（活跃生命周期 `max(updated_at)`） | 网页局部更新的变化信号 | 有持仓时每轮变，网页反复重拉（7b9d2c40 刚上线） | 稳定，只在真实变化时更新 |
| `strategy_records.py:3591`（列表 SQL 排序）、`:2477`（范围加载排序）、`:2781` 的 `latest_changed_at`（「最近变化」显示与 Python 侧排序，`web_app.py:1889`） | 策略列表排序、「最近变化」时间 | 持仓策略永远显示「刚刚」、永远排最前 | 显示真实最后变化时间。同文件 `:2785` 早已因同样理由排除了绑定的 `updated_at`，这里是漏网的另一半 |
| `web_queries.py:215` 首页事件流「策略状态更新」 | 按 `updated_at` 倒序取前 N | 持仓策略一直占前排 | 只在真实变化时上浮 |
| `strategy_alerts.py:887` `_find_related_lifecycle` | 管理 / 退出消息命中多条生命周期时取 `updated_at` 最新一条 | 持仓那条永远赢 | 取真正最近变化的那条；刚被该消息处理过的生命周期会被管理路径写 `updated_at`，仍然最新。只有一条候选时不变 |
| `position_attribution_repair.py:786`、`historical_state_repair.py:1641`、`batch150_management_terminalization.py:820` | 「评审到执行之间没被改过」的漂移守卫 | 对账一轮就让守卫拒绝 | 不再被空转误拒（一次性工具，当前不在运行） |
| `cli.py:3153` 事故快照 | 显示 | — | 显示真实值 |
| `db.py:1308/1317`、`lifecycle_monitor.py:2229`、`frozen_exchange_empty_state_alignment.py:79/871` | 启动回填 / 本事务刚写的值 / 比对前已剔除 `updated_at` | — | 不变 |

## 4. 修法（推荐方案 A）

与 `5b0221c8` 对绑定的做法一致：**按值比较内容，内容真变了才写 `updated_at`**。

- 「内容」＝ `StrategyLifecycle` 的全部映射列，去掉 `updated_at`。在模块里用 `sa_inspect(StrategyLifecycle).column_attrs` 推出列名，新增列自动纳入（宁可多算变化，不会漏算）。
- `_attach_binding_to_lifecycle`：找到生命周期后立刻记快照；三处 `lifecycle.updated_at = updated_at` 改成「和快照比，不同才写」。过期分支本来就一定有变化，统一走同一个辅助函数。`_clear_resolved_expiry_review`、`_refresh_lifecycle_prices_from_binding_payload` 的改动都在比较之前发生，自然被计入。
- `_mark_lifecycle_pending`：同样处理。
- 用值比较而不是 SQLAlchemy 属性历史：函数中间的查询会触发 autoflush 清掉历史（与 `5b0221c8` 同样的理由）。
- 不改表结构；不动其它任何写生命周期 `updated_at` 的地方（生命周期监视器、管理路径等都是真实状态变化时写）；不改上下文解析的代码和提示词。
- 取舍：持仓那一行不再每轮 UPDATE（它没有其它每轮变化的列，不像绑定还有 `recovered_at`），所以这里是真正的「内容没变就 0 次写」。

### 不推荐的替代

- **B：把 `updated_at` 从上下文指纹里去掉**。只治上下文一个读者，网页和列表照旧被带偏；而且指纹会丢掉 `entered_at`、止损止盈价变化这类只体现在 `updated_at` 上的真实变化。不做。
- **C：新增「对账最后核对」列**。生命周期没有任何读者需要这个信号（第 3 节），加列是 schema 变更（L3），没有收益。不做。

## 5. 风险级别：L2

- 改的是 worker 对账写入路径（每轮都跑、持有 position authority 锁），并且改变上下文解析指纹这一权威路径的输入（方向是让跳过按设计生效）。不改任何分支判断，不改 `lifecycle_status` 等字段的推导结果，不碰交易所。
- 主要风险：内容真变了却漏写 `updated_at`，会让网页版本号 / 上下文指纹看不到这次变化。由按全部列比较 + 第 6 节逐分支测试兜底。
- 与其它工作：只改 `execution_bindings.py` 两个函数，与已部署的 `5b0221c8`（同文件、不同函数）在其之上叠加；不碰 `context_resolution*.py` 和提示词，与 10-06 四分类阶段 3 批次 B 的提示词改动不冲突。
- 部署后观察（由调度会话排期）：一个连续 30 分钟、至少 5 条真实消息的窗口，全部满足即通过：
  1. 持仓中生命周期的 `updated_at` 不再每轮前进；同一绑定的 `recovered_at` 仍每轮前进，`active` 状态不变；
  2. 网页 `/api/live-state`（或其版本语句）的 strategies 版本在无真实变化时保持不变；
  3. 上下文尝试：无异常增长；如窗口内出现 `exchange_snapshot_changed` 调度，候选含持仓的行走「跳过」（`completed`、无新行）；
  4. worker 无新错误，对账耗时不上升。
  不需要额外重启、不需要交易所历史核对（执行路径没变）。
- 回滚：`tg-deploy <部署前 sha>`，无数据迁移。
- 历史值：只影响当前持仓 / 挂单中的生命周期（现在 1 行），部署后它停在最后一轮的时刻，不回填。

## 6. 测试方案

新增 `tests/test_lifecycle_updated_at_churn.py`：

1. **对账跑一轮、内容没变 → 生命周期 0 写入**：active 绑定 + `entered` 生命周期，同一快照跑两轮 `_apply_reconcile_snapshot`（不同 `recovered_at`）。第二轮后生命周期 `updated_at` 等于第一轮的值；用 `before_cursor_execute` 统计第二轮对 `strategy_lifecycles` 的 UPDATE 为 0。
2. **挂单分支**：open 绑定 + `pending_entry` 生命周期，同上，第二轮 0 写入；第一轮若从别的状态转成 `pending_entry` 则前进。
3. **终态已退出分支**（`:6342`）：生命周期已 `exited`、绑定仍 active，第二轮 0 写入。
4. **真实变化一定前进**：`pending_entry → entered`（持仓出现）、`exited/kol_signal → entered` 重新打开、过期未入场 → `expired`、清除到期复核、从绑定 payload 补止损 / 止盈——每条变化后 `updated_at` 等于本轮时刻。
5. **上下文指纹稳定**：一条消息的候选线程指向持仓中的生命周期；对账前后各算一次 `build_context_state_fingerprint`，持仓不变时相等；让生命周期真实变化后不等。
6. **网页版本号稳定**：对账前后 `strategies` 版本语句结果相等。
7. **绑定侧不回退**：`recovered_at` 仍每轮前进，`load_verified_position_ids` 仍返回持仓集合。

开发中跑相关测试（本文件、`test_binding_updated_at_churn.py`、`test_execution_bindings*.py`、上下文解析相关测试），最终候选跑一次全量 `uv run python -m pytest -q`。做到候选 sha + 全量通过为止，不部署、不推 origin/main。

## 7. 需要用户拍板的问题

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| 1 | 修法 | A：内容真变才写 `updated_at`（两个函数、按全部列比较）；B：只把 `updated_at` 从上下文指纹里去掉；C：新增列 | **A** |
| 2 | 核实结果显示上下文这边只省约 1 次 AI 调用 / 月，主要收益在网页版本号、列表「最近变化」与排序、不再误打 `reanalysis_capped`。按 L2 做（30 分钟、≥5 条消息的观察窗）是否仍值得 | 照做；搁置 | **照做**。改动小、读者审计没有变坏项，网页局部更新（7b9d2c40）正受它影响 |
| 3 | 修复后，持仓中的策略在列表里不再永远显示「刚刚」、不再永远排最前，而是按真实最后变化时间排 | 接受；要持仓优先另行处理 | **接受**。持仓优先由网页侧「持仓优先」面板承担（7b9d2c40），不靠这个时间戳 |
| 4 | 当前持仓 / 挂单生命周期的 `updated_at` 已被覆盖，部署后停在最后一轮时刻 | 不回填；回填（数据修复，L3） | **不回填**，只有 1 行，且没有读者依赖它的历史值 |
