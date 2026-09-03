# Recognition execution lease recovery 实施与生产副本演练记录

## 结论

- 实施基线：`6d6822b5edbd68307cf4da73398e89fe7e51ecc5`。
- 最终代码候选：`392a74730d5406d23e2080324e472fcdfdb1ea67`，已推送到
  `codex/deepcoin-auto-trading-v1`。
- 本轮只完成授权边界第 1 项：实现、测试和生产库副本演练。
  生产 schema、runtime 激活、服务重启、存量 29 行修复和交易所写入均未执行。
- 独立评审最终结论：P0=0、P1=0、P2=0。
- 最终完整套件：`6933 passed, 4 skipped, 32 warnings in 461.18s`。

## RED 回归与安全不变量

Task 1 先在旧实现上固化了根因链：嵌套 context reanalysis 在 claim 后抛异常，
context worker 吞掉异常并返回状态字典，外层 message job 仍会成功，decision 永久留在
`execution_running`。该用例在改动前按预期 RED，实现后证明原异常经分类后依然向上抛出，
不再伪装为 job succeeded。

最终实现保持以下顺序：

1. 主路径每个 authoritative generation 使用独立 attempt 和 exact claim token。
2. 调用 exchange-capable adapter 前，必须先以 exact token CAS 写入
   `side_effect_started_at`；CAS 失败时 adapter 调用数为 0。
3. `DeepcoinRequestOutcomeUnknown`、缺失的 exchange-effect envelope、未登记状态和多 leg 任一
   边界不清都持久化为 `outcome_unknown/execution_uncertain`，绝不自动重放。
4. `outcome_recorded` 只允许本地 finalize，恢复路径的 adapter 调用数为 0。
5. entry-assembly wakeup 使用独立子 fence，不复用主 lease；跨过副作用边界后不得被
   5 分钟 `claimed -> pending` 逻辑重置。
6. SIGTERM drain 先停止新 admission，再等待 queue、inline/reconcile/context 及
   `asyncio.to_thread()` 底层工作。scanner 的 scan + incident 写入也位于同一个可等待的
   专用线程周期中。
7. scanner 按 family 使用持久游标，每个周期可发现
   `job=succeeded + decision=execution_running/execution_uncertain`，poison row 不会使后续行饥饿。
8. backlog expiry 保留原 fail-closed 保护，并拒绝 `execution_uncertain`、active attempt 和
   active wakeup；不会为了让维护通过而清锁。

## 三张表的显式 schema 边界

新 schema 仅包含：

- `authoritative_execution_attempts`；
- `entry_assembly_wakeup_executions`；
- `recognition_execution_scan_cursors`。

`db.py` 明确将这三张表排除在常规 `Base.metadata.create_all()` 之外。唯一允许的建表入口是
hash-bound 的 `recognition-execution-schema`；应用后必须精确检查表、列、类型、空值性、默认值、
PK、unique/check/FK 约束和索引。任一表部分安装或结构偏离都 fail-closed。

激活前必须单独、显式验证三表及全部约束/索引已存在；不得依赖服务启动时的
`Base.metadata.create_all()` 偷渡建表。本轮没有执行该生产 schema 步骤。

## `comparison_status=execution_uncertain` 全量读取方审计

| 读取方 | 新值应否命中 | 最终行为与测试 |
| --- | --- | --- |
| `authoritative_recognition.py` | 不得当作 `execution_pending` 重新执行 | 只有 exact pending 返回 generation；自动重试对 uncertain fail-closed。 |
| `recognition_decisions.py` 两个 authoritative save 入口 | 必须命中不可覆盖保护 | `execution_running` 与 `execution_uncertain` 均抛错；claim/finalize 仍是 exact-state CAS。 |
| `authoritative_execution_attempts.py` | 必须命中终结状态机 | post-boundary 失联只能进 uncertain；uncertain 不可 claim/replay。 |
| `message_operation_contracts.py` | 必须当作 non-terminal | 显式列入 pending-blocked 集合；未登记状态进 error，不再默默落入 projection。 |
| `message_operation_supervisor.py` | 必须当作 non-terminal | 显式 terminal/non-terminal 词表，uncertain 不生成 operation contract。 |
| `semantic_review_control.py` | 不得被 pending/failed/running 语义误收 | 维持 exact `pending/failed` 目标与 semantic `running` 检查；uncertain 只进状态计数。 |
| `web_queries.py` / Web 渲染 | 必须显示未解决 | 先处理 uncertain，再处理 legacy auxiliary，显示“执行结果未知”；`web_app.py` 不再有另一套直接分支。 |
| `message_processing_backlog_expiry.py` | 必须阻塞过期 | running/uncertain 和未 reconcile attempt/wakeup 都拒绝；三表全缺失才兼容旧 runtime，1/2 表或 malformed 均拒绝。 |
| `recognition_execution_scanner.py` | 必须被主动发现 | running 与 uncertain 分族扫描，包含 `job=succeeded` 组合和 legacy 观测。 |
| `models.py` / `db.py` | 必须可持久，不得触发自动建新表 | 现有 VARCHAR 足以存值；新表从 core bootstrap 排除。 |

对上述每个需要修改的分支都有对应的聚焦用例；特别覆盖了 contract 非集合分支、
supervisor、semantic review、Web 降级、backlog guard、两个 save 入口和 scanner 游标公平性。

## 测试与独立评审

- 最终候选前的广义聚焦套件：`642 passed`。
- 独立评审在中间轮次发现并已修复：CLI 在已安装 schema 时仍可生成 legacy running、
  scanner 的 incident writer 未纳入 shutdown drain，以及 backlog 缺少部分安装回归。
- 修复后独立聚焦复跑：`463 passed, 2 warnings`。
- 最后 async wrapper 仅修正 blocking-census 的 AST 可见结构，不改变 cancellation drain；
  评审与三个相关用例再次通过。
- 最终完整套件：`6933 passed, 4 skipped, 32 warnings in 461.18s`。
- `git diff --check`：通过。

## 生产库副本演练

证据目录：

`/var/lib/telegram-kol-cutover-evidence/392a74730d5406d23e2080324e472fcdfdb1ea67/recognition-execution-lease-rehearsal-20260903T044159Z`

核心证据：

- root-owned mode-0600 在线备份：`pre-recognition-execution-schema.db`；
  853,778,432 bytes；SHA-256
  `525124a0a3623f9f586b5b52ddda981ac7660034fb56fc43d7fdd694c4407414`。
- 备份与演练副本均为 `quick_check=ok`、外键违规 0。
- 规划 SHA-256：`ed2a95303a8d50ac58c9a1b5c1276c889c32b5f42a3cebdb3ad2cad432d8afa4`。
- 第一次演练精确新建三张表；第二次返回 `created_tables=[]` 且 `changed=false`。
- 演练后 exact validation 通过：5 个 unique、7 个 check、4 个 FK 和 5 个普通索引的
  名称、列签名及 unique 属性全部匹配。
- 三张新表演练后行数均为 0。以下九张表在演练副本前后计数一致：
  `raw_messages=14547`、`recognition_decisions=14545`、`message_processing_jobs=2786`、
  `signal_candidates=2161`、`strategy_lifecycles=1063`、`execution_bindings=331`、
  `execution_order_legs=570`、`execution_events=3922`、`entry_assembly_attempts=7`。
- 当前生产 worker 的旧 runtime `0de19c1cbb2089fd58b8940d9b01a65096f9a063` 对三表的
  静态引用数为 0；在演练副本上执行该旧 runtime 的常规 DB bootstrap 后，三表行数与
  schema SHA-256 `b56543acfe922667099fab7ae86142bf7ab41b94c00d07f3b3e68124a47ef36c`
  前后一致，证明旧 runtime 完全忽略它们。
- 演练后再次只读检查生产源库：三表仍不存在，`query_only=1`、
  `total_changes=0`、`quick_check=ok`、外键违规 0。在线服务正常将
  `execution_events` 从备份时的 3922 推进到 3924；这是演练期间的生产活动，演练从未连接源库写路径。
- 演练前后 PID 稳定：Web `1396631`、ingest `3315585`、worker `3315574`；
  三角色 `loaded_artifact_verified=true`。
- `rehearsal-summary.json` SHA-256：
  `81db081c11888db6fb9e4772559344f1801f1ed211fa08f1cc72ceb8b24bd95e`。

初次创建证据目录时曾误用一个未验证的长 SHA 作目录名。在任何 schema 演练前，
整个证据目录已原子移动到上述实际候选 SHA 路径；备份内容和 SHA-256 未变。
`evidence-relocation.json` 保留了该更正记录。

## 未执行的后续步骤

本提交不是生产 schema 授权，也不是 runtime 激活或存量行修复授权。下一阶段必须依次独立授权：

1. 生产三表 schema 动作；
2. worker runtime 激活；
3. 逐行证明孤儿后的 29 行存量处置。

本轮对这 29 行的修改数为 0。
