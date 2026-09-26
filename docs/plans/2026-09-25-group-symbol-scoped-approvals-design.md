# 群组开关可用性 + 审批通知按范围收窄 · 设计稿

日期：2026-09-25
基线：`origin/main` = `bf52fbd9`（生产 HEAD `f2fa9d9f`，两者只差一条 docs 提交）
状态：**2026-09-25 用户已批准**，四处决策如下（原文保留在各节）：

- 甲：走甲-1~3（放开写权限 + 5 秒热加载 + 失败说人话），数据库化单独立项；
- 乙：出局的那些走 **乙-甲**——不发通知并直接自动过期收口；
- 丙（几何拒绝通知）：**一起收窄**，与超时审批同一口径；
- 丁（SOL 阈值）：**先不改**，数值由用户自己定。

本稿合并处理用户 2026-09-25 提出的四件事：

| 编号 | 用户原话 | 本稿位置 |
|---|---|---|
| 1 | 人工不能点击"自动交易" | 甲 |
| 2 | 没开自动交易的群，策略超时不用发审批 | 乙 |
| 3 | 不在白名单的标的，不用发超时审批 | 乙 |
| 4 | 开了 BTC/ETH/SOL，却没有 SOL 持仓 | 丁（已查清，不改代码，只提建议） |

---

## 甲 · 群组「自动交易 / AI识别」按钮点了没反应

### 现象与证据

点击 → `POST /api/groups/{chat_id}/automation` → 500。生产 web 日志：

```
File ".../group_config.py", line 135, in update_group_automation_settings
OSError: [Errno 30] Read-only file system: 'config/groups.yaml'
```

该端点在日志保留窗口内被调用 8 次，**全部 500，零成功**；生产
`config/groups.yaml` 的 mtime 停在 2026-08-13。

### 根因一：web 角色无权写这个文件

`deploy/systemd/telegram-kol-web.service`：

```
ReadOnlyPaths=/opt/telegram-kol-analyzer
ReadWritePaths=/opt/telegram-kol-analyzer/data
ReadWritePaths=/opt/telegram-kol-analyzer/config/ai_recognition.yaml
```

只有 `data/` 与 `ai_recognition.yaml` 两个写口子。叠加文件本身的属主权限：
生产上 `config/groups.yaml` 是 `root:telegram-kol-runtime 0640`，运行组只有读。
两道各自独立，任何一道单独存在都会让写失败。

时间线：按钮 2026-06-14（`8dd09e37`）上线；分角色硬化单元 2026-08-22
（`c19c277f`）把 `/opt` 整体设为只读；`ai_recognition.yaml` 的豁免是
2026-09-14（`f8e8f877`）单独加的，`groups.yaml` 从未跟上。**按钮从 08-22 起就是死的。**

### 根因二：就算写成功了也不会生效（更危险的一条）

`trading_mode` 只来自 YAML：`apply_trading_settings_to_group_config` 原样透传
`group.trading_mode`，数据库侧没有任何覆盖。而 `load_group_config` 只在进程启动时
调用一次（`web_app.py` 的 `app.state.group_config = group_config or GroupConfig()`），
此后没有任何重载路径。

真正下单的是 **worker** 进程，它持有自己那一份启动时的内存副本。所以修好写权限
之后，点按钮的结果是：**web 进程的内存和页面显示已经关掉，worker 仍在按旧值自动交易。**
页面会骗人。这比现在的 500 更坏——现在至少是明着失败。

### 根因三：失败在界面上看不见

`static/app.js` 的 `bindGroupAutomationToggles` 在 `!response.ok` 时走
`setRecoveryStatus(result.detail || '群组开关保存失败', true)`。FastAPI 未捕获异常的
`detail` 只有 `"Internal Server Error"`，而 `setRecoveryStatus` 的落点不在侧栏按钮旁边。
用户看到的就是"点了没反应"。

### 方案

三条一起做，缺任何一条都留下"页面说关了、实际没关"的可能。

**甲-1 打开写权限。**
- 单元文件加 `ReadWritePaths=/opt/telegram-kol-analyzer/config/groups.yaml`；
- 生产文件改 `root:telegram-kol-runtime 0660`（与 `ai_recognition.yaml` 同款）。
- `_write_group_config` 是原地 `write_text`，不做 rename——这一点必须保持：
  `ReadWritePaths` 只放行了这个文件，放行不到它所在的目录，rename 会失败。
  代价是读者可能读到写了一半的文件，由甲-2 的 fail-closed 解析接住。

**甲-2 跨进程生效。** 每个角色起一个轻量重载任务：每 5 秒 `stat` 一次
`config/groups.yaml`，mtime+size 有变化才重新解析，解析成功才整体替换
`app.state.group_config`（一次对象赋值，读者永远看到完整的一份）；解析失败
（读到半个文件、YAML 报错、群数为 0）只记一条 warning 并保留旧值，下一轮再试。
- 选这个而不是"读取时按 mtime 懒加载"：`app.state.group_config` 的读点有几十处，
  后者要改几十个调用点，前者只加一个任务。
- 5 秒是"点完按钮到生效"的上限，用户感知不到，也不会把 stat 打成负担。

**甲-3 失败要说人话。** 端点捕获 `OSError`，返回 503 +
`detail="群组配置文件不可写（服务器只读挂载或权限），开关未保存"`；进程启动时
在 web 角色做一次可写性自检（`os.access(path, W_OK)` + 挂载只读探测），
把结论打进启动日志。前端把 `detail` 显示在按钮旁边，并且**失败时按钮状态不变**
（现状已经是这样，保留）。

### 被否掉的另一条路：把开关搬进数据库

把 `ai_strategy_enabled` / `trading_mode` 迁到 `trading_settings`，天然跨进程、
不碰文件权限。否掉的理由不是它不好，而是它动的面太大：`group.trading_mode` 的读点
遍布执行、对账、恢复扫描、安全监视器，且 `web_app` 里明确写着
"Group trading modes live in the YAML group config, which the monitor role
deliberately cannot read"——搬进数据库等于动这条角色边界。**建议先做甲-1~3 把按钮
救活，数据库化单独立项。** 如果用户更想一步到位，本稿作废重写。

---

## 乙 · 超时审批按「群 × 标的」收窄（用户第 2、3 条）

### 现状

`lifecycle_monitor._prepare_pending_expiry_reviews` 选取 `pending_entry` / `entered`
的全部 lifecycle，只按 `management_action` 和复核时间过滤，**从不看这个群是否开了
自动交易，也不看标的是否在白名单**。

当前未了结（`pending_entry`/`entered`）的分布：

| 群 | 模式 | 条数 | 标的 |
|---|---|---|---|
| ROSE会员群 | notify_only | 11 | HYPE, BTC, ZEC, NEAR, BCH, BR, MUBARAK, PEPE |
| 三马哥会员群 | notify_only | 6 | BTC, ETH |
| 比特币飞扬 | notify_only | 3 | BCH, BTC |
| 币圈所长 | notify_only | 2 | BTC |
| 峰哥（今天刚关） | notify_only | 2 | ETH, BTC |
| 颜驰 | notify_only | 1 | BTC |
| 陈哥 / 大漂亮 / 大镖客 / 米娅 | auto_trade | 各 1~2 | BTC |
| 比特币军长 | auto_trade | 1 | **HBAR（不在白名单）** |

截图里那条 ROSE 会员群 ETH 的超时复核，就是第一行；军长的 HBAR 是第 3 条规则的例子。
按当前口径，这 31 条里只有 5 条属于用户真正需要审批的范围。

### 规则

在 `_prepare_pending_expiry_reviews` 的循环里，`_expiry_review_due` 之前插一道
`_expiry_review_in_scope(row)`：

```
in_scope = (群 trading_mode == "auto_trade") and (symbol ∈ settings.allowed_symbols)
```

- 群模式复用已有的 `self._group_trading_mode_provider`（构造函数里已经注入）。
  模式未知（配置里没有这个 chat）→ **视为不在范围**，不通知。理由见下面的 fail-closed 例外。
- 白名单来自数据库 `trading_settings.global.allowed_symbols`（现为 BTC/ETH/SOL），
  每轮扫描读一次，单行查询。**不读 YAML 的 `symbol_whitelist`**：运行时真正生效的是
  `apply_trading_settings_to_group_config`，它把每个群的白名单整体替换成全局白名单。
- `symbol` 形如 `BTC/ETH/SOL/ZEC` 这种复合串（库里真实存在）不在白名单里，自然出局。

### fail-closed 例外：有真实挂单的一律照发

**只要这条 lifecycle 还有未了结的交易所腿，无论群模式和标的，都照常发审批。**
判定复用 A1 已经落地的口径（`execution_binding_id` 非空 / `binding_summary` 非空 /
`risk_state != "no_current_risk"` / 三个 leg id 集合任一非空）。

理由是一个真实场景：今天峰哥群刚从 auto_trade 改成 notify_only，它的两条未了结策略
如果在交易所还挂着单，"群已经关了自动交易"不能成为不告诉用户"还有一张单挂在那里"的
理由——撤单按钮正是这条通知带来的。用户第 2 条要的是"别拿不下单的群烦我"，不是
"别告诉我还有挂单"。

### 出局的那些怎么收场（需要用户拍板）

两个选项：

- **乙-甲（建议）**：不在范围且无交易所腿 → 直接按超时收口，复用 A2 的
  `EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION`，note 写明"不在自动交易范围，未发审批"。
  好处：库里不会永远躺着一堆 `pending_entry`，A1 的候选集合也更干净。
- **乙-乙**：只是不通知，状态不动，继续留在网页里当历史。
  好处：完全不改变状态机；坏处：这些行会一直停在 `pending_entry`，且 A2 的自愈
  只对"发过通知"的行生效，等于永远不收口。

### 顺带：同一道闸也该管几何拒绝通知（需要用户拍板）

截图第三条【入场方向/价格几何拒绝】来自 `书'shu-crypto`（notify_only）、标的 TRUTH。
查到原因：`auto_trade_execution` 第 883–897 行的几何校验与通知入队，**排在群模式闸
（第 963 行）和白名单闸（第 971 行）之前**，所以不下单的群、不在白名单的币，照样会
推一条人工复核通知。`recovery_scan` 那一支已经按 `trading_mode == "auto_trade"` 收窄，
但同样没看白名单。

建议把这段几何通知移到两道闸之后（纯位置调整，不改判定），口径与乙一致。
**这条超出用户原话的范围，单独列出来等批准**，不批准就只做超时审批那一项。

---

## 丙 · 实施与验证

- 分支从 `origin/main` 开新 worktree（共享检出当前停在已冻结的
  `codex/deepcoin-auto-trading-v1`，不要在上面动）。
- 单测：甲-2 的重载任务（mtime 未变不重读 / 解析失败保留旧值 / 成功整体替换）；
  乙的范围判定四种组合（开×白名单、开×非白名单、关×白名单、关×非白名单）
  与 fail-closed 例外（关×有绑定 → 照发）。
- 部署按 `AGENTS.md`：先推 `main`，再 `tg-deploy <sha>`，记录回滚 SHA；
  单元文件变更后需要 `systemctl daemon-reload`，生产文件权限那一步要 root 手工执行。
- 验证：部署后在页面上点一次峰哥的「自动交易」开→关，确认
  ①返回 200；②YAML 文件 mtime 变化；③5 秒内 worker 日志里该群的 auto_trade 判定跟着变；
  ④再点回去复原。这一步会真实开关一次自动交易，**执行前单独征得用户同意**。
- 观察窗：上线后 24 小时统计超时审批通知条数与所属群，应只剩 auto_trade 群 × 白名单币，
  外加有挂单的例外。

---

## 丁 · 为什么没有 SOL 持仓（第 4 条，已查清）

**不是被系统拒掉的，是没机会。** 证据：

- 全局设置里 `allowed_symbols = [BTC, ETH, SOL]`、`auto_trade_enabled = true`、
  `symbol_max_loss_usdt.SOL = 10`——SOL 确实是开着的。
- 合约规格缓存里 `SOL-USDT-SWAP` 存在且 `state=live`（09-25 20:54 同步），
  下单能力闸不会拦它。
- 9 月至今全部 SOL 策略只有 5 条，其中 3 条在自动交易群：
  - 09-03 舒琴群 SOL long：**真的下了两条限价触发单**（`create_trigger_entry submitted`），
    一直没成交，09-06 人工确认过期并撤单；
  - 09-10 舒琴群 SOL long：`entry_price_geometry_rejected` /
    `entry_price_geometry_ambiguous`，转人工复核没下单，7 天无人答复后被 A2 自动收口；
  - 09-04 陈哥群 SOL short：没有任何执行事件，是一条模拟 lifecycle。
  其余 SOL 信号都来自 notify_only 的群（`kol_or_group_auto_trade_disabled`）。

### 但确实有一个配置缺口：SOL 没有入场阈值

`trading_settings.global.symbol_entry_thresholds` 只有 BTC 和 ETH：

```
BTC: market_leg_threshold 200 / first_limit_offset 90 / second_limit_offset 90
ETH: market_leg_threshold 4   / first_limit_offset 2  / second_limit_offset 2
```

SOL 走 `SymbolEntryThresholds.zero()`，而 `market_price_is_near_entry_edge` 与
`_hybrid_market_entry_price` 都在 `max_distance <= 0` / `market_leg_threshold <= 0`
时直接返回 False/None。**含义：SOL 永远不会触发贪婪腿（贴市价立刻进场），
只会挂纯限价单。** 09-03 那次"挂了两条限价、没成交、过期撤单"正是这个形状。

建议在网页的交易设置里补一组 SOL 阈值（SOL 价格量级 ~200，按 ETH 的比例推算
大约 `market_leg_threshold 1.5 / first_limit_offset 0.8 / second_limit_offset 0.8`，
具体数值请用户定），否则 SOL 开着也几乎等于只挂单不进场。这一条是配置，不是代码，
**改之前等用户确认数值**。
