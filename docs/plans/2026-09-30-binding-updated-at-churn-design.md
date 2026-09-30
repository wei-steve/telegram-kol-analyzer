# 对账每轮刷新 execution_bindings.updated_at：源头与方案

- 日期：2026-09-30
- 状态：**用户已确认（2026-09-30）：问题 1 选 A、2 另开任务、3 不回填、4 不改**；实施中
- 分支：`claude/bold-kapitsa-caf323`（基于 origin/main `b5ec59e2`，生产 `7b9d2c40`）
- 验证级别：L2（改的是 worker 对账写入路径；不改表结构、不碰交易所写入）
- 来源：网页实时数据会话的排查（`docs/plans/2026-09-30-web-live-data-and-positions-first-design.md` 1.4 b、第 6 节问题 5）

## 1. 源头（只读核实）

### 1.1 哪条语句

不是 ORM 的 `onupdate`（`models.py` 里 `ExecutionBinding.updated_at` 只有 `default=utc_now`），是**显式赋值**：

| 位置 | 语句 | 何时执行 |
|---|---|---|
| `execution_bindings.py:4210-4211`（`_derive_binding_from_entry_legs` 末尾） | `binding.recovered_at = recovered_at`、`binding.updated_at = recovered_at` | 每轮对账，对每条「非人工终态、没有被平仓预留」的绑定都执行，**不管前面各分支有没有改动字段** |
| `execution_bindings.py:958-960`（`_apply_reconcile_snapshot` 快照不完整分支） | 同上两句 + `last_exchange_status = "position_attribution_evidence_unavailable"` | 交易所快照读失败的轮次，对所有非人工终态绑定执行 |

`_apply_reconcile_snapshot` 每轮载入 `venue='deepcoin'` 且状态在 `open/active/unknown/stale/closed/cancelled` 里的**全部**绑定（包括早已 `closed` 的），逐条交给 `_derive_binding_from_entry_legs` 按订单腿重新推导。

### 1.2 为什么没变化也会写

推导分支里对 `status`、`pos_id`、`last_exchange_status` 的赋值，值与原来相同时 SQLAlchemy 不会产生 UPDATE；真正让每一行都变「脏」的，是末尾这两句时间戳赋值——`recovered_at` 每轮都是新值，于是每一行每轮都被 UPDATE 一次，`updated_at` 跟着一起被写成本轮开始时刻。

### 1.3 生产频率与行数（2026-09-30 06:42–06:44Z，只读点查，走 `ix_execution_bindings_venue_status`）

- deepcoin 绑定共 392 条；**每轮被刷新 197 条**：`closed/entry_legs_terminal` 195、`active` 1、`stale/position_ownership_unassigned` 1。
- 不被刷新的：`manual_closed_or_not_found_on_exchange` 132、`unknown/position_attribution_conflict` 46、人工关闭 7、`closed/position_attribution_evidence_unavailable` 3、`cancelled` 1（这些要么属于人工终态被跳过，要么推导时被预留跳过）。
- 频率：连续采样得到的时间戳 06:42:32.9 → 45.7 → 52.6 → 43:12.1 → 31.4 → 49.8，**约 7–20 秒一轮**。定时轮（`by_timer`）约 1 分钟一次（worker 日志近 60 分钟 58 条 `deepcoin_reconcile_round`，每条 `touched_binding_count=197`）；其余来自管理 worker、管理规划器等其它调用 `reconcile_deepcoin_execution_bindings` 的地方。
- 写入代价：每轮一个事务里 197 次单行 UPDATE（两列都没有索引，不触发索引页写入），大约几十个数据页进 WAL。对 SQLite 单写锁不算大，但完全没有必要。

### 1.4 同一机制的另外两处（不在本批范围，单列）

- **持仓中那条生命周期的 `strategy_lifecycles.updated_at` 也每轮被刷新**：`active` 分支调 `_attach_binding_to_lifecycle`，其末尾 `lifecycle.updated_at = updated_at`（`execution_bindings.py:6315`、`:6338`）无条件执行；`open` 分支的 `_mark_lifecycle_pending`（`:4237`）同理。生产点查：每轮 1 条生命周期跟着跳（当前只有 1 个持仓）。
  - 读者里至少两处受影响：网页 `web_live_state.py:136` 的 strategies 版本号（`active_lifecycles` 的 `max(updated_at)`）——有持仓时版本号每轮都变，网页那边绕开 binding 后又从这里被带回来；`context_resolution_worker.py:146` 把候选线程当前生命周期的 `updated_at` 放进**上下文输入指纹**——有持仓时指纹每轮都变，上下文重解析的「指纹没变就跳过」（`:643-656`）基本失效，可能多花 AI 调用。后一点是读代码得到的推断，未用生产数据核对。
  - 生命周期的 `updated_at` 还被上下文解析、告警选最新一条等读取，改它需要单独做一遍读者审计，所以不混进本批（见第 6 节问题 2）。
- **持仓那条入场腿的 `execution_order_legs.updated_at` 也每轮跳**（生产点查 leg 669：`updated_at == last_verified_at ==` 本轮时刻）。它让对账轮次日志的 touched 计数永远至少包含持仓那条绑定。影响小，不在本批。

## 2. 读者清单与影响

`src/`、`scripts/`、`deploy/` 全量搜索（`scripts/`、`deploy/`、`db_retention`、值守 watch_items、runtime incidents 都**不读**这两列）。

### 2.1 读 `execution_bindings.updated_at` 的地方

| 位置 | 用途 | 现在是否被误导 | 修复后 |
|---|---|---|---|
| `oncall_remediation_auto.py:300` 阶段 4 的 D3「下单后仓位未被外部改动」 | `last_exchange_status == manual_closed_or_not_found_on_exchange` **且** `updated_at > posted_at` 才判外部改动 | **没有**。生产 132 条该状态的绑定 0 条被刷新；而且按构造不可能：对账推导一旦碰到某行，就会把 `last_exchange_status` 改写成推导结果，被刷新的行不可能同时保有这个状态。只读子代理说「对账刷新会让 D3 误判」，理由不成立 | 不变 |
| `oncall_detector.py:2253` 值守检测 D3「识别失败持续」 | 年龄取**识别行**的 `updated_at`；「有没有持仓」由 `read_chat_open_bindings` 按绑定 `status` + `recovered_at` 新鲜度判断 | 不读绑定 `updated_at`，不受影响 | 不变（`recovered_at` 仍每轮刷新，见第 3 节） |
| `web_queries.py:3917 / 3936` 历史列表（已关闭绑定） | 按 `updated_at` 倒序取前 N 条；并把 `updated_at` 当「退出时间」显示 | **是**。195 条已关闭绑定的「退出时间」都显示成刚才那一轮；取前 N 条时顺序等于按 id | 显示真实最后变化时间。**但历史值已丢**：这 195 行会停在部署前最后一轮的时刻（见问题 3） |
| `web_queries.py:4088` 已核实历史仓位 | 交易所历史没有平仓时间时，用 `updated_at` 兜底显示 | 同上 | 同上 |
| `web_app.py:3758` 订单号 → 绑定映射 | 按 `updated_at` 倒序，同一订单号先到先得 | 轻微：并列时退化成按 id 倒序 | 按真实变化时间挑，符合原意 |
| `message_operation_supervisor.py:1170` 消息执行结果的证据引用 | 同一策略实例 / 消息下按 `updated_at` 倒序取最多 32 条 | 轻微：通常远少于 32 条，只影响引用顺序 | 符合原意 |
| `web_app.py:11853` 对账轮次日志 touched 计数 | `updated_at >= 本轮开始` 的绑定 id | **是**。每轮恒为 197，这个计数没有信息量 | 变成本轮真正变化的绑定数（加上入场腿有变化的绑定）。这正是它文档字符串说的「ledger rows moved」 |
| `historical_state_repair.py:608 / 1158`、`batch150_management_terminalization.py`、`one_off/historical_management_terminalization.py` | 整行快照做「计划到执行之间没被改过」的漂移守卫 | **是**：对账每轮都改 `updated_at`，这些守卫几乎必然拒绝 | 不再因对账空转被拒（这几个是一次性修复工具，当前不在运行） |
| `oncall_casefile.py:195 / 628` | 案例文件导出 | 只是展示 | 展示真实值 |
| `web_app.py:4563` `_is_preferred_live_position_binding` | 并列时新者优先 | 死代码，无调用方 | — |
| `strategy_records.py:2785`、`web_live_state.py:119` | 网页实时数据会话已刻意不用 | — | 修复后可以重新用，但不在本批改（那是网页会话的文件） |

### 2.2 读 `execution_bindings.recovered_at` 的地方（决定了它不能停写）

| 位置 | 依赖的语义 |
|---|---|
| `management_target_verification.py:112` `load_verified_position_ids` | `max(recovered_at)` 当「对账最近一轮」水位线：太旧就返回 `None`（不知道），否则返回集合（可能为空＝看过了、没有持仓）。识别热路径在用（`message_recognition`、`authoritative_recognition`、`management_target_confirmation`、`provider_outage_replay`） |
| `management_target_verification.py:126` | 只有 `recovered_at >= cutoff` 的 `active` 绑定算「已核实持仓」 |
| `oncall_detector.py:592` `classify_binding` | `now - recovered_at <= snapshot_max_age` 才算「已核实持仓」，否则「快照过期」。值守 D1 / D3 的「有没有持仓」都经过它 |
| `execution_bindings.py:5072` `_claimed_after_snapshot`（A-10c） | 有人在快照之后写过这条绑定就跳过；少写只会少跳过，不会误判 |

结论：`recovered_at` 本来就是「对账最后一次核对」，被刻意每轮刷新，**语义正确，不是 bug**。出问题的只有 `updated_at` 被顺手一起写。

## 3. 修法与取舍

### 方案 A（推荐，本批）：`updated_at` 只在内容真正变化时写；`recovered_at` 照旧每轮写

- `_derive_binding_from_entry_legs` 入口先记下绑定的内容快照（所有映射列，去掉 `updated_at`、`recovered_at`），末尾比较：
  `binding.recovered_at = recovered_at` 照旧；**只有内容有变化**才 `binding.updated_at = recovered_at`。
  - 用值比较而不是 SQLAlchemy 的属性历史：推导中间 `_attach_binding_to_lifecycle` 等会发查询触发 autoflush，flush 之后属性历史被清空，只看历史会漏掉真实变化。
  - 三个调用点（`_apply_reconcile_snapshot:1254`、人工平仓同步 `:4839`、`:4970`）都走这个函数，一处改全部覆盖。
- 快照不完整分支（`:958-960`）同样处理：`last_exchange_status` 真变了才写 `updated_at`；`recovered_at` 照旧。
- 不改表结构，不动其它任何写 `updated_at` 的地方，不动生命周期和订单腿。
- 取舍：**写入行数不减少**。`recovered_at` 每轮还是变，197 行仍各有一次 UPDATE（只是 SET 子句里少了一列）。按 1.3 的估算这个写入量不大；要真正降到「没变化就 0 次写」，需要方案 B。

### 方案 B（不推荐本批做）：再把 `recovered_at` 限制到非终态绑定

- 已关闭 / 已取消的绑定只在内容变化时才写 `recovered_at`，每轮写入从 197 行降到「非终态行数」（现在约 2 行）。
- 代价：`load_verified_position_ids` 的「对账最近一轮」水位线就是 `max(recovered_at)`。账户空仓、没有任何非终态绑定时，水位线会变旧，函数从「看过了，没有持仓」变成「不知道」，识别热路径的管理目标核验行为随之改变。要避免就得另立一个对账轮次水位线：库里没有通用的心跳 / 键值表，要么新建表（schema 变更，L3），要么借用别的表，都超出本批。

### 方案 C（不推荐）：拆字段（新增「对账最后检查时间」列）

- 就是把 `recovered_at` 的现有语义再复制一份，没有带来 A 做不到的东西；属于 schema 变更（L3），按「能不拆就不拆」不做。

## 4. 风险级别：L2

- 改的是 worker 对账写入路径（每轮都跑、持有 position authority 锁），行为上只少写一列，不改任何分支判断、不改 `status` / `pos_id` / `last_exchange_status` 的推导结果、不碰交易所。
- 主要风险是「内容明明变了却漏写 `updated_at`」，由第 5 节的逐分支测试兜底。
- 观察（部署由调度会话排期）：部署后一个 30 分钟窗口，满足以下即通过：
  1. 对账轮次日志 `touched_binding_count` 从恒 197 降到个位数，且只在有真实变化时非零；
  2. 生产点查：非终态绑定 `recovered_at` 每轮仍在前进（`load_verified_position_ids` 不回 `None`），持仓那条 `active` 绑定仍被判为已核实；
  3. 已关闭绑定的 `updated_at` 不再前进；
  4. worker 无新错误、对账耗时不上升。
  不需要重启以外的动作，不需要交易所历史核对（不改执行路径）。
- 回滚：`tg-deploy <部署前 sha>`，无数据迁移，回滚后恢复原样。

## 5. 测试与回放

新增 focused 测试（`tests/test_execution_bindings.py` 或新文件）：

1. **内容没变 → `updated_at` 不变**：造一批绑定（已关闭 / active / stale / open 各一），跑两轮 `_apply_reconcile_snapshot`（同一份快照、不同 `recovered_at`）。第二轮后所有行 `updated_at` 等于第一轮值；`recovered_at` 等于第二轮值。
2. **只写变了的那一行**：第二轮前让其中一条的入场腿变终态（或持仓从快照里消失），第二轮后只有那一行 `updated_at` 前进，其它行不动。
3. **语句层面**：用 SQLAlchemy `before_cursor_execute` 统计第二轮对 `execution_bindings` 的 UPDATE：内容没变的行 SET 子句只有 `recovered_at`、不含 `updated_at`。
4. **逐分支**：`active`、`all_terminal`、`verified_missing`、`has_unavailable`、`has_conflict`、`has_pending`、兜底 `stale`，以及 `_attach_binding_to_lifecycle` 把 `active` 改成 `stale`（过期未入场）这一条：真实变化时 `updated_at` 一定前进。
5. **快照不完整分支**：第一次进入时 `updated_at` 前进，连续第二次不前进。
6. **`load_verified_position_ids`**：两轮之后仍返回持仓集合（`recovered_at` 仍在刷新）。
7. **对账轮次日志**：更新 `tests/test_deepcoin_shadow_binding_phase4.py` 里 touched 计数相关断言（如有依赖空转的）。

开发中跑相关测试，最终候选跑一次全量 `uv run python -m pytest -q`。

## 6. 需要用户拍板的问题

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| 1 | 修法 | A：只修 `updated_at`，`recovered_at` 照旧每轮写（写入行数不减少）；B：再把 `recovered_at` 限制到非终态绑定（写入降到约 2 行 / 轮，但要另立对账水位线，可能涉及新表） | **A**。B 若要做另开任务 |
| 2 | 持仓中那条生命周期 `updated_at` 也每轮刷新（1.4），它会让网页 strategies 版本号每轮变、并可能让上下文重解析的指纹跳过失效 | 本批一起改；另开任务（先审计 `strategy_lifecycles.updated_at` 的读者，并用生产数据核实上下文重解析是否真的多跑了） | **另开任务**。读者更多、牵涉上下文解析（权威路径），不混进本批 |
| 3 | 195 条已关闭绑定的 `updated_at` 历史值已被覆盖，部署后会停在部署前最后一轮的时刻，网页历史列表的「退出时间」仍不对 | 不回填；回填（按订单腿终态时间等推一个值写回，属于生产数据修复，L3） | **不回填**，本批不做数据修复；若网页需要，由网页侧改用交易所历史或订单腿时间显示 |
| 4 | 持仓那条入场腿的 `updated_at` 也每轮跳（1.4），让 touched 计数恒含持仓那条绑定 | 本批一起改；不改 | **不改**，影响小 |
