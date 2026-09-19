# Codex 值守补救 · 阶段 1 实施规格：独立值守进程（检测 + 案件库 + 中文告警）

日期：2026-09-19
上位设计：`docs/plans/2026-09-18-codex-oncall-remediation-design.md`（第 4.1、4.5、7.1 节）
验证等级：**L1**（新增、默认休眠、无权限接管、不写交易所、**不写生产库**）

## 1. 本阶段做什么 / 不做什么

**做**：一个独立进程，每 60 秒只读地查看生产库，发现"消息要求了管理操作、目标有真实仓位、但交易所操作没发生"以及
"消息处理停摆"两类情况，建案件（存在自己的状态库里），并**自己直接**给用户发中文 Telegram 告警。

**不做**（后续阶段）：调用 `codex exec`；任何交易所读写；向生产库写任何东西；向 worker 发任何命令；重启服务；部署。
本阶段不得 import `deepcoin_client`、`position_mutation_gateway`、`deepcoin_execution_actions`、
`strategy_management_executor`、`worker_command_jobs` 的写入函数；不得以可写方式打开生产库。

## 2. 新增文件

| 文件 | 职责 |
|---|---|
| `src/telegram_kol_research/oncall_state.py` | 值守自己的 SQLite 状态库（`cases`、`watch_items`、`meta`、`alerts`），路径由参数给定；建表幂等 |
| `src/telegram_kol_research/oncall_detector.py` | 纯函数式检测规则：输入只读生产库连接 + 状态库 + `now`，输出新案件 / 案件状态变化 |
| `src/telegram_kol_research/oncall_alerts.py` | 中文告警文案（含原因码→中文词典）、去重 / 冷却 / 每日上限、Telegram 发送（同步，`urllib`，10 s 超时） |
| `src/telegram_kol_research/oncall_service.py` | 主循环、配置加载、心跳文件、systemd watchdog 通知、每日"值守正常"报平安 |
| `cli.py` 新子命令 `oncall-watch` | `--database-path`、`--state-path`、`--poll-seconds`（默认 60）、`--once`、`--dry-run` |
| `deploy/systemd/telegram-kol-oncall.service` | 见第 7 节 |
| `config/oncall.env.example` | 配置样例（不含真实值） |
| `tests/test_oncall_*.py` | 见第 8 节 |

## 3. 只读访问生产库的纪律（硬性）

- 用 `sqlite3.connect("file:<path>?mode=ro", uri=True, timeout=5)` 打开，每轮开、用完即关；**不用** SQLAlchemy 引擎
  （避免任何 bootstrap / `create_all` / 迁移副作用）。`PRAGMA query_only=ON`。
- **禁止全表扫描**（2026-09-15 的教训：整库扫描会拖住 worker 事件循环）。每张表只按主键水位线增量读：
  `WHERE id > :last_seen_id ORDER BY id LIMIT 200`；对"需要等一会儿再判"的行，把主键记进状态库 `watch_items`，之后按主键点查。
- 读失败（锁、文件不存在、schema 不符）→ 本轮记为 `read_failed`，**不当作"没有问题"**；连续 5 轮失败触发一条健康告警。
- 首次启动时水位线取各表当前 `max(id)`：**绝不回放历史**。

## 4. 检测规则

"管理指令"= `message_instruction_items.instruction_kind = 'management'`。动作取自关联的
`signal_candidates.management_action`。本阶段只关心减风险动作：
`full_exit`、`partial_take_profit`、`partial_then_break_even`、`move_stop_to_break_even`、`adjust_stop_loss`
（`replace_entry`、`cancel_pending_entry` 属于入场侧，不建案）。

### 4.1 "有真实仓位"判据（降噪的核心，7.1 节的结论）

一条管理指令只有在**目标确有在仓仓位**时才建案。按顺序判，任一成立即为"有仓位"：

1. 候选的 `target_lifecycle_id` → `strategy_lifecycles.execution_binding_id` → `execution_bindings` 行
   `venue='deepcoin'`、`status IN ('open','active')`、`pos_id` 非空；
2. 指令项的 `strategy_instance_id` 对应的 `execution_bindings` 行满足同样条件；
3. 目标未定（`target_lifecycle_id` 为空，例如确认超时）时：消息所在 `chat_id` 下存在同币种（若候选给了 symbol / side 则同向）
   满足同样条件的绑定。此时案件标 `target_uncertain=true`。

三条都不成立 → 不建案、不告警，只在状态库 `meta` 里累加 `skipped_no_position` 计数（每日报平安里带出）。
实现前先读 `auto_trade_execution._load_active_execution_bindings` 与 `management_target_verification.py`，
判据与之保持一致；如发现仓库里已有更权威的"在仓"判据，用它并在汇报里说明。

### 4.2 规则表

| 规则 | 触发条件 | 严重度 |
|---|---|---|
| D1a | 管理指令项终态 `failed` 或 `unknown`，且有真实仓位 | 高 |
| D1b | 管理指令项 `succeeded` 但 `result_json.status ∈ {skipped, shadow_planned}`，原因**不是** `kol_or_group_auto_trade_disabled` / `symbol_not_allowed` / `group_not_configured_for_auto_trade` / `confidence_below_minimum`（这些是用户自己的配置，不算问题），且有真实仓位 | 高 |
| D1c | 管理指令项停在 `awaiting_user_confirmation` 超过 10 分钟，且所在群有在仓仓位 | 高 |
| D1d | 管理指令项停在 `pending` / `executing` / `submitted` 超过 5 分钟仍无终态，且有真实仓位 | 中 |
| D2 | `strategy_management_batches.status ∈ {blocked, partial_failed, recovery_required, submit_unknown}` 超过 2 分钟（批次必然有仓位，不再判 4.1）；`blocked` 且 `reason_code='management_disabled_plan_only'` 除外 | 高 |
| D4 | `message_processing_jobs` 存在 `pending` 或 `claimed` 且 `created_at` 早于 3 分钟前 | 高（健康） |
| D5 | 生产库连续 5 轮读失败；或 worker 的 `http://127.0.0.1:8002/api/runtime/loop-health` 连续 3 轮无响应（2 s 超时；URL 可配置，留空则跳过） | 高（健康） |

同一指令项同时命中 D1 与 D2 时合并为一个案件（以 `raw_message_id + management_action` 为键）。
D3（识别失败类，B 线）留到阶段 2 之后，本阶段不做。

### 4.3 案件模型（状态库 `cases`）

`case_key` 唯一（`mgmt:<raw_message_id>:<action>` / `health:<rule>`）、`rule`、`severity`、`raw_message_id`、`chat_id`、
`item_ids_json`、`batch_ids_json`、`reason_code`、`target_uncertain`、`status ∈ {open, resolved, stale}`、
`first_seen_at`、`last_seen_at`、`alerted_at`、`resolved_at`、`evidence_json`（有界，≤ 8 KB，见 5.2）。

- 案件 `open` 后每轮按主键复查；若指令项 / 批次后来成功（项 `succeeded` 且结果非 skipped，或批次 `succeeded` / `resolved`）→
  `resolved`，发一条"已自行恢复"。
- `open` 超过 6 小时 → `stale`，不再复查。
- 健康案件条件消失 → `resolved`，发一条"已恢复"。

## 5. 告警

### 5.1 发送

- 配置来自环境变量（由 systemd `EnvironmentFile=/etc/telegram-kol-oncall.env` 注入）：
  `TELEGRAM_KOL_ONCALL_MODE=off|dry_run|notify`（默认 `off`；`off` 时进程启动后立即正常退出）、
  `TELEGRAM_KOL_ONCALL_BOT_TOKEN`、`TELEGRAM_KOL_ONCALL_CHAT_ID`、`TELEGRAM_KOL_ONCALL_WORKER_HEALTH_URL`（可空）、
  `TELEGRAM_KOL_ONCALL_DAILY_ALERT_CAP`（默认 30）。
- `dry_run`：完整运行检测与案件，告警只写进状态库 `alerts` 表和日志，不发 Telegram。
- 发送失败：记 `alerts.delivery_error`（只记异常类型，不记 token / URL），下一轮重试，最多 5 次；**绝不让发送失败中断主循环**。
- token 不得出现在日志、异常文本、状态库里。

### 5.2 文案（全部简体中文、大白话）

建案告警模板：

```
⚠️ 值守提醒 #<案件号>
群：<群名>    消息 #<raw_message_id>（<北京时间 HH:MM>）
消息要求：<动作中文>（<币种> <多/空>）<如有：止损→75850>
原文：「<消息前 80 字，去掉换行>」
现状：已过 <N> 分钟，交易所没有对应操作。
卡在：<原因中文>（<原因码>）
仓位：仍在持仓中<如 target_uncertain：；目标仓位未确定，群内在仓：BTC 多 / ETH 空>
```

- 动作中文：`full_exit` 全部平仓 / 离场、`partial_take_profit` 部分止盈、`partial_then_break_even` 部分止盈后保本、
  `move_stop_to_break_even` 止损移到保本、`adjust_stop_loss` 调整止损。
- 原因码→中文词典至少覆盖 7.1 节出现过的全部原因码（`prior_partial_batch_unresolved`、`confirmation_timeout`、
  `protection_missing_cancellable_order_id`、`protection_price_or_size_mismatch`、`management_stop_action_conflict`、
  `target_strategy_binding_visibility_retry_expired`、`close_final_preflight_failed`、`protection_recovery_bypassed_for_full_exit`、
  `revision_replacement_incomplete`、`stale_pending_voided_*`、`recovery_timeout`、`protection_authority_frozen:*` 等）；
  可复用 `web_queries._execution_reason_label` 里已有的译法；词典里没有的原因码原样显示并注明"（未收录原因）"。
- 群名从生产库已有的群 / 来源表读取（自行定位；读不到就显示 chat_id）。
- 健康告警、"已自行恢复"、"值守正常"各一个简短模板，风格一致。
- 消息原文属于不可信外部文本：只做截断和去换行后放进「」里，不做任何解释执行；Telegram 以纯文本发送（不设 `parse_mode`）。

### 5.3 去重、冷却、上限

- 每个案件只发一次建案告警 + 至多一次恢复通知。
- 同一个群 10 分钟内第 4 个及以后的案件合并成一条"<群名> 另有 N 条类似情况"。
- 健康类同规则 15 分钟冷却。
- 每日上限（默认 30）：达到后只再发一条"今日告警已达上限，其余 N 条见值守状态库"，次日（北京时间 0 点）重置。
- 每天北京时间 09:00 后的第一轮发一条"值守正常"：过去 24 小时建案数、已恢复数、因无仓位而忽略的条数、读库失败轮数。
  （目的：**沉默 = 值守挂了**，用户能分辨。）

## 6. 主循环与自身健康

- 每轮：读配置（只在启动时读一次）→ 检测 → 更新案件 → 发告警 → 写心跳文件 `<state 目录>/heartbeat.json`
  （`{"at": ISO8601, "round": n, "last_error": null|"ExcType"}`）→ 通知 systemd watchdog（`NOTIFY_SOCKET` 存在时发 `WATCHDOG=1`，
  纯 socket 实现，不引入新依赖）。
- 单轮内任何未预期异常：记日志（`logger.exception`）、计入 `last_error`，**循环继续**；`KeyboardInterrupt` / `SystemExit` 原样抛。
- `--once` 跑一轮后退出（测试与人工核对用）。

## 7. systemd 单元（本阶段只提交文件，不安装、不启用）

`deploy/systemd/telegram-kol-oncall.service`：`Type=notify`、`NotifyAccess=main`、`WatchdogSec=300`、`Restart=always`、`RestartSec=10`、
`User=telegram-kol-oncall`、`StateDirectory=telegram-kol-oncall`、`EnvironmentFile=/etc/telegram-kol-oncall.env`、
`ConditionPathExists=/etc/telegram-kol-oncall.env`；沙箱沿用 `telegram-kol-monitor.service` 的写法：
`TemporaryFileSystem=/opt/telegram-kol-analyzer:ro` + `BindReadOnlyPaths=` 只放 `.venv`、`src`、`data/research.db`、`-wal`、`-shm`；
`ReadWritePaths=/var/lib/telegram-kol-oncall`；`ProtectSystem=strict`、`ProtectHome=true`、`NoNewPrivileges=true`、`PrivateTmp=true`。
网络需要访问 `api.telegram.org` 与 `127.0.0.1:8002`，不设 `IPAddressDeny`。
`ExecStart=... telegram-kol-research oncall-watch --database-path /opt/telegram-kol-analyzer/data/research.db --state-path /var/lib/telegram-kol-oncall/state.db`。
（阶段 2 引入 Codex 时单元身份与隔离会再调整，本阶段不为 Codex 预留任何东西。）

## 8. 测试要求

用临时 SQLite 文件构造生产库夹具（用项目的 `Base.metadata.create_all` 建表后插入最小行），状态库用 `tmp_path`。至少覆盖：

1. D1a / D1b / D1c / D1d / D2 各自的命中与不命中；D1b 的四个"用户配置"原因不建案；
2. **无仓位不建案**：4.1 的三条判据各一正一反；`visibility_retry_expired` + 无绑定 → 不建案且 `skipped_no_position` +1；
3. 首次启动水位线 = 当前 max(id)，历史行不触发；重启后（同一状态库）不重复建案、不重复告警；
4. D1 与 D2 同一消息合并为一个案件；案件后续成功 → `resolved` + 恢复通知；6 小时 → `stale`；
5. D4 停摆命中 / 恢复；D5 读库连续失败 5 轮才告警，读失败不被当成"无问题"；
6. `dry_run` 不调用发送函数；`off` 直接退出；发送失败重试且不中断循环；token 不出现在日志 / 状态库（断言）；
7. 去重、同群合并、健康冷却、每日上限与次日重置、09:00 报平安只发一次；
8. 文案：中文、含群名 / 消息号 / 动作 / 原因中文 / 分钟数；未收录原因码的回退；原文截断到 80 字且无换行；
9. 架构边界测试（仿 `tests/test_runtime_agent_architecture_boundary.py`）：`oncall_*` 模块的 import 闭包里没有第 1 节列出的禁用模块；
   生产库连接串必须含 `mode=ro`；
10. 只读纪律：用 `sqlite3` 的 `set_authorizer` 或等价手段断言检测过程对生产库没有任何写语句；
    每张表的查询都带 `id >` 水位线或主键点查（对 SQL 文本做断言即可）。

开发中跑聚焦测试；全部完成后跑**一次**全量 `pytest`，汇报通过 / 失败 / 跳过数。与本改动无关的既有失败要列出但不要去修。

## 9. 提交与汇报

- 在 worktree 分支上提交；**禁止 `git add -A`**，只 `git add` 明确路径，提交前 `git diff --cached --name-only` 核对。不 push、不部署、不连服务器。
- 汇报内容：提交 SHA 与文件清单；4.1 最终采用的"在仓"判据及依据；群名取自哪张表；全量测试结果；
  任何偏离本规格之处及理由；你认为规格里有问题或遗漏的地方。
