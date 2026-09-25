# 群组开关可用性 + 审批通知按范围收窄 · 实施状态

设计稿：`docs/plans/2026-09-25-group-symbol-scoped-approvals-design.md`（2026-09-25 用户已批准）
分支：`group-symbol-scoped-approvals`，基线 `origin/main` = `bf52fbd9`（生产 HEAD `f2fa9d9f`）
状态：甲（1/2/3）、乙、丙 已完成。未部署、未推送。

本文件是下一个读者的唯一进度依据。每块记落点、为什么这么选、测试、以及上线时人工要做的事。

---

## 甲-1 打开 groups.yaml 的写权限

**落点**：`deploy/systemd/telegram-kol-web.service`，在 `ai_recognition.yaml` 那条旁边加
`ReadWritePaths=/opt/telegram-kol-analyzer/config/groups.yaml`，并按同样的注释风格写清楚原因
（原地写、无 rename；文件必须 `root:telegram-kol-runtime 660`；两个条件互相独立）。

**没动 `group_config._write_group_config`**：它是原地 `path.write_text`，保持原样。
`ReadWritePaths` 放行的是这个**文件**而不是它所在的目录，临时文件 + rename 会失败。
代价是读者可能读到写了一半的文件，由甲-2 的 fail-closed 解析接住。

顺带核实了一件事，它决定了这条能不能成立：`_write_group_config` 里那句
`path.parent.mkdir(parents=True, exist_ok=True)` 在只读目录上**不会**抛错——
`Path.mkdir` 的 `except OSError` 分支在 `exist_ok` 且路径已是目录时会吞掉 EROFS。
`ai_recognition_config` 的写入路径一字不差是同一个形状，而它在生产上是能写成功的，
这也是这条推断的实证。

## 甲-2 跨进程热加载（最要紧的一块）

**落点**：`src/telegram_kol_research/web_app.py`

| 名字 | 作用 |
|---|---|
| `GROUP_CONFIG_RELOAD_INTERVAL_SECONDS = 5.0` | 轮询周期 |
| `_group_config_stat_signature(path)` | `(st_mtime_ns, st_size)`，读不到返回 `None` |
| `_refresh_group_config_from_disk(app)` | 同步的一轮：返回 `unconfigured` / `stat_failed` / `unchanged` / `parse_failed` / `empty` / `reloaded` |
| `_run_group_config_reload_loop(app, interval_seconds=…)` | 先 sleep 再 `asyncio.to_thread(_refresh…)` 的后台任务 |

- 基线存在 `app.state.group_config_stat_signature`，在 `create_web_app` 里与
  `app.state.group_config_path` 同时初始化：传进来的那份配置刚刚就是从这个文件解析的，
  所以第一轮不该再解析一次。
- **所有角色**都起这个任务（`app.state.group_config_reload_task`，挂
  `_log_background_task_result`，在 lifespan 的 `finally` 里取消）。真正下单的是 worker，
  它才是那个不能拿着旧副本的进程。
- 成功时**整体替换** `app.state.group_config`（一次对象赋值）。读点有几十处，
  逐字段更新会让读者看到半份配置。
- fail closed 的方向是「保持上次的配置」：stat 失败 / YAML 报错 / 读到半个文件 /
  解析出 0 个群，都只记 warning、保留旧值、**不动基线**，下一轮再试。
  这条不能让步：下游到处把「配置里没有这个群」当成「不是 auto_trade」，
  所以一次读不到文件若被当真，等于悄悄把一个群的开关退役。
- `reloaded` 那条日志带上 `auto_trade_chat_ids`，上线验证时不必查库或查交易所就能回答
  「worker 有没有跟上我刚点的开关」。
- 端点写完文件后仍然立即更新自己进程的 `app.state.group_config`（没依赖 5 秒轮询），
  并同步 `app.state.group_config_stat_signature`，免得轮询紧接着再解析一次。

**没做的事（有意）**：热加载只替换 `app.state.group_config`。`live_target_titles` 与
直播监听任务仍然只由端点更新（且只在非 web 角色上调 `ensure_live_tasks_match_targets`），
和改动前一样。让文件轮询去起停 Telegram 监听任务超出本次范围，风险也不对等。

## 甲-3 失败要说人话

- `POST /api/groups/{chat_id}/automation`：`update_group_automation_settings` 的
  `OSError` 被捕获 → **503** +
  `detail="群组配置文件不可写（服务器只读挂载或权限），开关未保存"`，
  并 `logger.error` 记 `errno=30(EROFS)` 这种形式的原始 errno
  （EROFS = 挂载只读，EACCES = 文件属主/权限，是服务器上两件不同的事）。
  注意这个 `except OSError` 也会盖住「读」失败（例如文件不存在时 `load_group_config`
  抛 `FileNotFoundError`）。措辞仍然成立（开关确实未保存），错误分类靠日志里的 errno。
- web 角色启动时打一条
  `group_config_write_check path=…;exists=…;writable=…;reason=…`
  （`_group_config_write_check` / `_format_group_config_write_check_for_log`），
  风格照 `format_release_gates_for_log`：这种只活在 systemd 单元和文件权限里的开关
  没人能事后确认，而它已经死了两个月都没说一声。
  两道探测各答一半问题：`os.access(W_OK)` 答属主/权限，`open(path, "r+")`
  答只读挂载（只读挂载只有在真的要求写权限时才会报出来）。`r+` 不截断不写入，
  文件内容与 mtime 都不动。
- 前端 `static/app.js` 的 `bindGroupAutomationToggles`：
  `await response.json()` 改成 `.catch(() => ({}))`（refusal 的响应体可能不是 JSON，
  不能因此丢掉 503 自己那句话），失败时把 `detail` 同时写进
  `setRecoveryStatus` 和**按钮自己的 `title`**——侧栏那条状态行离按钮很远。
  成功时把按钮原来的 title 还回去。失败时按钮状态一律不动（原状如此，加注释固定下来）。

---

## 乙 超时审批按「群 × 标的」收窄 + 出局的直接收口

**落点**：`src/telegram_kol_research/lifecycle_monitor.py`

| 名字 | 作用 |
|---|---|
| `EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION = "expiry_auto_expired_out_of_scope"` | 出局收口写的 `management_action`（仍以 `expiry_` 开头，现有 `startswith("expiry_")` 过滤自动把它排除在后续复核外） |
| `EXPIRY_OUT_OF_SCOPE_CLOSEOUT_NOTE_PREFIX = "不在自动交易范围（非人工判定）"` | note 前缀 |
| `_expiry_review_allowed_symbols()` | 每轮扫描读一次数据库 `trading_settings.global.allowed_symbols` |
| `_expiry_review_scope(session, row, *, allowed_symbols)` | 返回 `(in_scope, reason)` |
| `_lifecycle_has_unsettled_exchange_leg(session, row)` | 复用 A1 的 `_binding_context` + `_has_unsettled_exchange_leg` |
| `_claim_expiry_out_of_scope_closeout(...)` | 照 A2 形状的单条条件 UPDATE |

范围判定：`trading_mode == "auto_trade"` **且** `symbol ∈ 全局白名单`。
群模式取已注入的 `self._group_trading_mode_provider`；**配置里没有这个 chat（返回 ""）算不在范围**
——没配置过的群下不了单，替它猜 `auto_trade` 是往吵的方向猜。
白名单读数据库而非 YAML 的 `symbol_whitelist`：运行时
`apply_trading_settings_to_group_config` 会把每个群的白名单整体替换成全局白名单，
YAML 那份不是真正把关下单的值。

**fail-closed 例外**（凌驾上面两条）：这条 lifecycle 只要还可能有未了结的交易所腿，
一律照发审批。判定**复用** A1 在 `strategy_thread_candidates.py` 的
`_binding_context` + `_has_unsettled_exchange_leg`（两个都是那边的模块私有名，
故意直接 import：同一条规则推导两遍必然漂移，然后安全的那遍会输）。

出局的收场：不在范围且无交易所腿 → 不发通知、**直接按超时收口**
（`expired` / `exit_reason=expired` / 新动作 / 新 note，note 正文写明群模式与标的为何出局、
当时的全局白名单是什么）。只对 `pending_entry` 做；`entered` 的行只是不通知，状态不动
（A2 的边界也是这样，理由同样是生产上存在无绑定的 `entered` 行）。

### 三处我自己拍的决定（设计稿没写到这一层）

1. **范围判定放在 `_expiry_review_due` 之后，而不是之前。** 指令原话是「在
   `_expiry_review_due` 之前插一道」，我没照字面做，原因是：`_expiry_review_due`
   的含义就是「这条已经超过 `max_age_hours` 该问人了」。放在它之前，等于对**任何年龄**的
   `pending_entry` 生效——notify_only 群里一条刚到 10 分钟的新信号会当场被标记过期，
   于是这些群赖以存在的 K 线回放（pending_entry → entered 模拟）再也不会发生，
   侧栏「待入场」对大多数群直接归零。那不是用户要的，也超出了设计稿里「超时审批」的范围。
   放在之后，命中的正好是「本轮本来会发通知的那些行」，「按超时收口」这句话也才成立。
   已有专门一条测试固定这个边界（`test_a_row_not_yet_timed_out_is_left_alone_even_out_of_scope`）。
2. **人工按过「继续等待」的行不收口，只是不再问。** 收口的认领条件里带
   `expiry_review_notified_at IS NULL` 和 `expiry_review_next_at IS NULL`，
   所以 `expiry_review_continued` 的行状态一律不动。理由照 A2 的先例：
   「人工选择继续等待」不该被一次配置变更悄悄推翻。代价是这类行会停在 `pending_entry`
   不再被处理（生产上属少数），比替人改主意可接受。
   顺带的好处：这两个条件让 **A2 与本条按谓词互斥**而不是靠顺序互斥——
   A2 只碰「通知过且 7 天无人答」的行，本条只碰「从未通知过」的行，交集为空。
3. **读不到范围就不收窄。** provider 没注入（部署里根本没有群配置）、
   provider 抛异常、`load_trading_settings` 抛异常、白名单读出来是空集合——
   四种情况都当作「本轮不收窄」，照旧发通知。这里 fail-closed 的方向是「照样问人」而不是
   「直接收口」：多一条通知只花掉一条消息，少一条可能留下一张没人看的交易所挂单；
   白名单为空更要防，否则一行坏设置就能把库里所有超时行全部过期掉。

### 乙 的测试

`tests/test_expiry_review_group_symbol_scope.py`（10 项，全绿）：

- 四种组合同一轮扫描：开×白名单 → 发通知；开×非白名单（HBAR，军长那条的形状）/
  关×白名单 / 关×非白名单 → 不通知且收口，动作与 note 前缀都对，`notified_at` 仍为空；
- note 分别写出「群组未开启自动交易」与「标的不在全局白名单 + 当时白名单 BTC,ETH,SOL」，
  且不以 `人工` / `超时自动收口` 开头；
- 关×有绑定 → 照发（同一轮里关×无绑定的兄弟行被收口，证明扫描真的跑过）；
- 配置里没有的 chat → 出局收口；
- `entered` 出局 → 不通知、状态/动作/exited_at 全不动；
- 人工「继续等待」的行 → 不通知、状态不动；
- A2 与本条不重复处理同一行（通知过 9 天的 notify_only 行只被 A2 收口，且只发一条 A2 汇总）；
- 未超时的行即便出局也不动；
- 没有 provider → 完全照旧；
- `load_trading_settings` 抛异常 → 照旧发通知、不收口。

回归：`tests/test_lifecycle_monitor.py`、`tests/test_lifecycle_expiry_review_auto_closeout.py`、
`tests/test_lifecycle_exit_intents.py`、`tests/test_candidates.py`、
`tests/test_entry_confirm_sizing_and_lifecycle.py` 全绿（60 项）。

---

## 丙 几何拒绝通知同一口径

**落点 1**：`src/telegram_kol_research/auto_trade_execution.py`
`_auto_process_single_message_trade_signal`

- 把 `apply_trading_settings_to_group_config` + `_resolve_runtime_config` 这两句
  **上提**到几何校验之前（两个都是纯函数：前者构造一个新 `GroupConfig`，
  后者只读配置），下面原来的两道闸继续用这同一份 `runtime_config`，位置与顺序不动；
- 几何通知的入队改成带判定：
  `if not candidate_geometry.passed and _entry_geometry_notice_in_scope(runtime_config, symbol=…, settings=settings)`；
- 新增 `_entry_geometry_notice_in_scope(runtime_config, *, symbol, settings)`：
  `runtime_config is None` → 出局；`trading_mode != "auto_trade"` → 出局；
  symbol 不在 `settings.allowed_symbols` → 出局。

**我没有按字面「把这段几何通知移到那两道闸之后」，而是原地加判定。** 理由：
几何通知与那两道闸之间全是早退分支——`mimo_symbol_review`、
`auto_trade_disabled`（**全局**入场开关）、`deployment_entry_frozen`、
`deepcoin_client_unavailable`、`lifecycle_event_not_new_entry`——
真把入队挪到闸后，这些状态下 auto_trade 群 × 白名单币的几何问题也会一起哑掉。
这不是用户要的第二个行为改动，而用户要的那一个只需要这条判定。

这不是推测：仓库里已经有一条专门写来固定这个行为的测试——
`tests/test_auto_trade_execution.py::test_wrong_geometry_candidate_alerts_even_when_entry_submission_is_disabled`
断言**全局自动交易关掉时几何告警照样发**。按字面移动会直接弄坏它。
几何判定本身、`auto_trade_skipped` 各分支的语义与顺序都没动。

**落点 2**：`src/telegram_kol_research/recovery_scan.py`
`load_recovery_signals_from_db` 的 `geometry_alerts` 入队，
补上 `_recovery_symbol_allowed(runtime_config, symbol=…)`。
这里的白名单取 `runtime_config["symbol_whitelist"]`，它就是**全局**白名单：
唯一的调用者 `run_recovery_dry_run` 传进来的 config 已经过
`apply_trading_settings_to_group_config`，而那个函数把每个群的白名单整体换成
`settings.allowed_symbols`。再读一次数据库等于对同一行问同一个问题。
narrowing 只影响「通知谁」，不影响这条扫描看见什么（signals 仍然照旧返回）。

### 丙 的测试

`tests/test_entry_geometry_notice_scope.py`（5 项，全绿）：

- auto_trade 群 × 白名单币 → 照旧入队一条（对照组）；
- notify_only 群 → 不入队，`reason` 仍是 `kol_or_group_auto_trade_disabled`；
- auto_trade 群 × `TRUTH`（生产实例）→ 不入队，`reason` 仍是 `symbol_not_allowed`；
- 配置里没有的 chat → 不入队，`reason` 仍是 `group_not_configured_for_auto_trade`；
- 恢复扫描：同一轮里 BTC 入队、TRUTH 不入队，两条 signal 都照旧返回。

回归：`tests/test_auto_trade_execution.py`、`tests/test_recovery_scan.py`（116 项）
与另外 10 个含 geometry 的测试文件（888 项）全绿。

---

## 上线时人工要做的两步

1. `systemctl daemon-reload`（单元文件改了；随后 `tg-deploy` 的重启才会带上新的
   `ReadWritePaths`）。
2. 在服务器上以 root 执行，把文件权限改成与 `ai_recognition.yaml` 同款：
   ```
   chown root:telegram-kol-runtime /opt/telegram-kol-analyzer/config/groups.yaml
   chmod 660 /opt/telegram-kol-analyzer/config/groups.yaml
   ```
   两步缺任何一步，开关仍然保存失败——只是现在会明确回 503 而不是 500。

## 上线后怎么验证

1. web 启动日志里那条 `group_config_write_check …;writable=true;reason=ok`。
   若是 `writable=false`，`reason` 直接指向上面两步里漏掉的那一步。
2. 在页面上点一次某个群的开关 → 期望 **200**（不是 500/503）；
   `config/groups.yaml` 的 mtime 变化。
3. 5 秒内 **worker** 日志出现
   `group config reloaded path=… groups=N auto_trade_chat_ids=[…]`，
   且列表跟着变。这一条就是「页面不再骗人」的证据。
4. 再点回去复原。**这一步会真实开关一次自动交易，执行前单独征得用户同意。**
5. 乙：上线后 24 小时统计超时审批通知的条数与所属群，应只剩 auto_trade 群 × 白名单币，
   外加有挂单的 fail-closed 例外。同时查一次
   `SELECT COUNT(*), symbol FROM strategy_lifecycles WHERE management_action =
   'expiry_auto_expired_out_of_scope'`——首日会有一批历史行被收口（设计稿那张表里的
   26 条量级），之后应该只零星出现。
6. 丙：`SELECT COUNT(*) FROM execution_events WHERE action =
   'entry_price_geometry_rejected' AND created_at > <上线时刻>`，
   新增的应只来自 auto_trade 群 × 白名单币。

### 甲-2 与乙的耦合（值得知道）

lifecycle monitor 的群模式来自 `lambda chat: _group_trading_mode(app.state.group_config, chat)`,
读的是**调用时**的 `app.state.group_config`。所以甲-2 上线后，把一个群的自动交易关掉，
5 秒内它的超时审批通知也随之停止，不需要重启。这是想要的行为，但也意味着
甲-2 与乙会在同一次部署里一起生效，观察窗要同时盯这两件事。

## 测试

`tests/test_group_config_hot_reload.py`（11 项，全绿）：

- mtime/size 未变 → `unchanged` 且**一次解析都没有**（monkeypatch 计数）；
- 文件变了 → `reloaded`，`app.state.group_config` 换成新对象，旧对象内容完好；
- YAML 坏了 / 群数为 0 / 文件不存在 → 保留旧值、基线不动，改回去后下一轮恢复；
- 没配置路径 → `unconfigured`；
- 端点写入后基线同步（写完再跑一轮 `unchanged`、零解析）；
- 端点在 `OSError(EROFS)` 下回 503 与那句中文 detail，进程内配置不动，
  日志里有 `EROFS`；
- 轮询任务真的捡起变化，并能干净取消；
- `web` 角色启动时起了轮询任务、关闭时清掉，并打出那条自检日志；
- 自检把「文件不可写」与「挂载只读」当成两个独立原因分别报出。

回归：`tests/test_web_app.py`、`tests/test_web_cli.py` 全绿（259 项）。

---

## 全量测试（最终候选）

```
uv run pytest -q
9546 passed, 4 skipped, 109 warnings in 790.02s (0:13:10)
```

三块代码全部就位后跑的一次，对应提交 `33775d3e`（丙）之后的工作树。
没有新增 skip，没有新增 warning 类别。

## 提交

| SHA | 内容 |
|---|---|
| `96d92f9b` | 甲-1/2/3 + `tests/test_group_config_hot_reload.py` |
| `3fd5ffb3` | 乙 + `tests/test_expiry_review_group_symbol_scope.py` |
| `33775d3e` | 丙 + `tests/test_entry_geometry_notice_scope.py` |

**未推送、未部署**（按指令）。分支 `group-symbol-scoped-approvals`，从 `origin/main`
（`bf52fbd9`）开出。
