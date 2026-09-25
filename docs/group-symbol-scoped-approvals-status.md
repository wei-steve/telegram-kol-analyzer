# 群组开关可用性 + 审批通知按范围收窄 · 实施状态

设计稿：`docs/plans/2026-09-25-group-symbol-scoped-approvals-design.md`（2026-09-25 用户已批准）
分支：`group-symbol-scoped-approvals`，基线 `origin/main` = `bf52fbd9`（生产 HEAD `f2fa9d9f`）
状态：甲（1/2/3）、乙 已完成；丙 进行中

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
