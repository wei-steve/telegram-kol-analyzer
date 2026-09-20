# Codex 值守补救 · 实施状态

设计：`docs/plans/2026-09-18-codex-oncall-remediation-design.md`
阶段 1 规格：`docs/plans/2026-09-19-codex-oncall-phase1-spec.md`

```yaml
current_phase: 1
phase_name: standalone-oncall-watcher
phase_status: deployed_dry_run_observation
verification_level: L1
production_deployed: true
production_commit: 76ddb91d4498534ad24b8bd92248942bd0d7e9a5
rollback_commit: 72313b0e9cd63ebfb9e24c9c16f042e210a2261f
systemd_unit_installed: true
production_mode: "dry_run since 2026-09-20 07:39 CST; switch to notify after one clean day"
default_mode: "off (TELEGRAM_KOL_ONCALL_MODE absent means the process exits at once)"
writes_production_database: false
writes_exchange: false
calls_codex: false
```

## 1. 阶段 1 交付的东西

| 文件 | 作用 |
|---|---|
| `src/telegram_kol_research/oncall_state.py` | 值守自己的 SQLite 状态库：`meta` / `cases` / `watch_items` / `alerts`，建表幂等 |
| `src/telegram_kol_research/oncall_detector.py` | 只读检测：水位线增量读 + 主键点查；规则 D1a–D1d、D2、D4、D5a/D5b；4.1「有真实仓位」判据 |
| `src/telegram_kol_research/oncall_alerts.py` | 中文文案、原因码词典、去重 / 同群合并 / 健康冷却 / 每日上限 / 报平安、Telegram 发送 |
| `src/telegram_kol_research/oncall_service.py` | 配置、主循环、心跳文件、systemd watchdog |
| `src/telegram_kol_research/cli.py` | 新子命令 `oncall-watch` |
| `deploy/systemd/telegram-kol-oncall.service` | 单元文件（**只提交，未安装、未启用**） |
| `config/oncall.env.example` | 配置样例（无真实值） |
| `tests/oncall_test_support.py` + `tests/test_oncall_{detector,alerts,service,architecture_boundary}.py` | 92 条测试 |

## 2. 4.1「有真实仓位」最终判据

按规格三条顺序判（lifecycle 绑定 → 指令项 `strategy_instance_id` 的绑定 → 同群同币同向的在仓绑定），
每条都落到同一个 `classify_binding()`：

- `venue='deepcoin'`、`status IN ('open','active')`、`pos_id` 非空 —— 规格原文；
- **再分两档**：`recovered_at` 在 5 分钟内且 `last_exchange_status='position_ownership_verified'` → `verified_open`；
  否则 → `open_snapshot_stale`，**照样建案**，文案里注明「仓位快照未及时更新」。

依据：`management_target_verification.py` 的注释说明 reconcile 每轮重写每一行绑定，
`active` + `position_ownership_verified` 是「这一轮在交易所看见了这个仓位」的唯一证据；
`auto_trade_execution._load_active_execution_bindings` 用的则是 `venue + status IN ('open','active')`。
两者合起来才既准又不哑：worker 冻住时 `recovered_at` 会变旧，如果把旧快照当成「没有仓位」，
恰恰是在系统最坏的时候闭嘴。读代码还发现 `status='open'` 一定伴随 `pos_id=NULL`（`execution_bindings.py`
的 `_derive_binding_from_entry_legs`），所以规格里的 `'open'` 实际上不会命中，判据等价于「active + 有 pos_id」。

`status='unknown'`（`position_attribution_conflict`，归属冲突）按规格**不建案**，但单独计一个
`counter:skipped_position_attribution_unknown`，等阶段 2 有数据了再决定要不要收进来。

## 3. 群名取自哪里

`strategy_alerts.chat_title`（按 `chat_id` 索引定位，`ORDER BY message_id DESC LIMIT 1`）。
这是生产库里唯一存了群自己标题的表。取不到时退到 `sources.custom_label` / `sources.display_name`（同一 `chat_id` 的 KOL 名），
再取不到就直接显示 `chat_id`。群名的权威来源其实是 `config/groups.yaml`，但那在生产库之外，
沙箱里没有挂载，所以没用它。

## 4. 偏离规格之处（含理由）

1. **仓位判据加了「快照过期」一档**（见第 2 节）。规格只给了硬判据；这一档只放宽「标签」，不放宽准入以外的东西，
   方向是宁可多报不可漏报。
2. **D5 拆成两个案件键**：`health:D5a_database_unreadable` 与 `health:D5b_worker_loop_health`。
   规格写的是 `health:<rule>` 一个键；两个条件互不相干，合成一个键会让其中一个恢复把另一个也标成 resolved。
3. **健康案件可以重开**。规格没说条件消失又出现时怎么办。已 `resolved` 的健康案件再次命中会重开成新一轮（`reopen=True`），
   靠「同规则 15 分钟冷却」防抖；管理类案件**不会**重开（它的键指的是一条消息的一个动作，这个故事只讲一次）。
4. **D4 用 `message_processing_jobs.enqueued_at`**，规格写的是 `created_at`——该表没有 `created_at` 列。
5. **同群合并的实现**：一轮内第 4 条及以后的案件合并成一条「另有 N 条类似情况」，跨轮各自合并各自的。
   规格只说合并，没说跨轮怎么算。
6. **每日报平安不受每日上限约束**。规格把它和上限分开写；沉默即故障的信号不能被上限吃掉。
7. **`dry_run` 的告警在状态库里标 `dry_run`**，不标 `sent`——状态库要能分清「发了」和「本来要发」。
8. **多了一个测试支持模块** `tests/oncall_test_support.py`（规格第 2 节的文件表里没有），用来共享生产库夹具。
9. **单元加了 `RestartPreventExitStatus=0`，`off` 模式退出前先发 `READY=1`**。规格同时要求
   `Type=notify` + `Restart=always` + 「`off` 时立即正常退出」，三者放在一起会让休眠单元每 10 秒重启一次，
   而且每次都被 systemd 记成「从未发送 READY=1」的启动失败。发一次 `READY=1` 再干净退出、并让 0 退出码不触发重启，
   既保留了规格的语义，也让休眠看起来就是休眠。非 0 退出（崩溃、被杀）照常重启。
10. **只读查询多了两种有界形状**（除水位线与主键点查外）：按 `strategy_instance_id`（有索引）`LIMIT 20`、
   按 `chat_id`（有索引）`LIMIT 50`，以及首次启动时每表一次的 `SELECT MAX(id)`。
   4.1 的第 2、3 条判据和群名查询没法只用主键表达。三种形状都是索引定位 + 上限，不是扫描；
   `tests/test_oncall_detector.py` 里对每一条实际发出的 SQL 做了形状断言。

## 5. 规格里我认为有问题 / 需要指挥会话决定的地方

1. **CLI 入口会把整个应用 import 进来**。`oncall-watch` 挂在 `cli.py` 上（规格要求如此），而 `cli.py` 顶层 import
   了 `deepcoin_client` 等一整套。值守进程**不调用**它们，架构边界测试也只管 `oncall_*` 四个模块的 import 闭包，
   但进程内存里确实有这些代码。要彻底隔离得给值守一个自己的 console script 入口，阶段 2 可以顺手做。
2. **D1d 的「停了 5 分钟」用 `updated_at` 判**。`last_progress_at` 会被 `deferred_instruction_recovery`
   的心跳刷新，用它会永远判不出卡住；`created_at` 又会把正常排队算进去。`updated_at` 只在状态真的变化时写。
3. **第一次启动必然漏掉启动前就卡住的东西**（水位线取 `max(id)`，这是规格明确要求的）。
   上线那一刻如果正好有一条卡住的管理指令或一批堆积的 job，值守不会报。上线前建议人工看一眼。
4. **D2 不限动作**。规格的规则表对 D2 没有写动作白名单，所以任何 `blocked/partial_failed/recovery_required/submit_unknown`
   的管理批次都会建案，包括 `cancel_entry` 这种入场侧的。如果只想要减风险动作，说一声就收窄。
5. **`sources` 兜底给出的是 KOL 名而不是群名**，两者在用户眼里不一样。真群名只有 `strategy_alerts` 有，
   而该表只对开了策略提醒的群有行。上线后如果告警里出现裸 `chat_id`，说明这个群没有 `strategy_alerts` 行。

## 6. 测试

- 本阶段新增 92 条（`tests/test_oncall_*.py`），覆盖规格第 8 节的 10 项。
- 全量：`uv run python -m pytest -q`。
  **注意 `uv run pytest -q` 会在收集阶段就失败**——`tests/test_management_reliability_step5e.py` 与
  `tests/test_strategy_management_worker.py` 里的 `from tests.test_... import` 需要仓库根目录在 `sys.path` 上，
  只有 `python -m pytest` 这种跑法才会加。这是既有现象，与本阶段无关，没有去动它。

## 7. 部署前需要人工确认的事

- 建 `telegram-kol-oncall` 系统用户与 `/var/lib/telegram-kol-oncall`；写 `/etc/telegram-kol-oncall.env`（0600，root 所有）。
- ~~值守要用自己的 bot~~ **用户 2026-09-19 决定沿用现有系统 bot**：独立性来自"独立进程自己发"，不来自 token 不同。
  单元直接加载 `config/system_operator_bot.env`（该文件只有 token 与 chat id 两个键，systemd 以 root 读入），
  代码在未配置 `TELEGRAM_KOL_ONCALL_BOT_TOKEN` 时回退到 `TELEGRAM_KOL_SYSTEM_BOT_TOKEN / _CHAT_ID`；没有人复制或填写 token。
  `notify` 模式而两处都没有凭据 → 进程以 78 退出且不重启循环（`76ddb91d`）。
- 先 `TELEGRAM_KOL_ONCALL_MODE=dry_run` 跑一天，看 `state.db` 的 `cases` / `alerts` 两张表里
  建了哪些案件、文案长什么样、`counter:skipped_no_position` 有多大，再转 `notify`。
- 单元文件只把 `.venv`、`src`、`research.db`（及 `-wal`/`-shm`/`-journal`）只读挂进来；
  确认服务器上这几个路径确实存在，否则 `BindReadOnlyPaths` 会让单元起不来。
- 阶段 2 引入 Codex 时，设计 4.2 要求的 `InaccessiblePaths=`（挡住 `/etc/telegram-kol-*.env`、
  `data/telegram.session*`、`data/backups`）还没有加进这个单元——阶段 1 不跑 Codex，所以没加；
  **阶段 2 必须先加。**

## 7.1 部署记录（2026-09-20 07:39 CST）

- 候选 `76ddb91d`（`f48f6d03` 子代理实现 + `b0a232e0` 文案修补 + `3e08c4cc` 设计 / 冒烟脚本 + `76ddb91d` 沿用系统 bot），
  最终候选全量 `uv run python -m pytest`：**9097 passed / 4 skipped / 0 failed**。
- 部署前：候选是生产 HEAD `72313b0e` 的后代、共享分支 tip 是候选的祖先（两项 PASS）；零在途
  （管理批次 / mutation intent / claimed job / worker command 均为 0），无在仓绑定。
- `tg-deploy 76ddb91d…` → worker / web / ingest 均 active，web 200；回滚 = `tg-deploy 72313b0e…`。
- 部署后：同一 SHA 推到共享分支；`PASS: 0 code files beyond production`、`PASS: deployed sha is on the shared branch`。
- 值守安装：建系统用户 `telegram-kol-oncall`；`/etc/telegram-kol-oncall.env`（root 0600，**不含任何密钥**：
  `MODE=dry_run`、worker 健康 URL、每日上限 30）；单元安装并 `enable --now`。
- 值守验证：`NRestarts=0`；心跳逐轮推进（`last_error: null`）；状态库水位线 = 当时各表 max(id)
  （items 1235 / batches 168 / jobs 6002），**0 案件 0 告警**（未回放历史）；只读打开 WAL 生产库正常（读失败 0 轮）；
  worker 健康探针正常；bot token 在状态库与 journal 中出现次数均为 **0**。
- 待办：dry_run 满一天后核对 `cases` / `alerts` / `counter:skipped_no_position`，干净则把
  `/etc/telegram-kol-oncall.env` 的 `MODE` 改为 `notify` 并 `systemctl restart telegram-kol-oncall`（不需要 tg-deploy）。
  停用：`systemctl disable --now telegram-kol-oncall`（对交易主链路零影响）。

## 8. 下一阶段

阶段 2（Codex 运行器，只诊断）。本阶段没有为它预留任何东西，这是规格要求的。
