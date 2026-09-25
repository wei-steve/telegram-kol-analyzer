# 过期待入场策略收口 · 状态

设计稿：`docs/plans/2026-09-25-stale-pending-entry-convergence-design.md`
分支：`stale-pending-entry-convergence`（基线 `origin/main` = `8317914f`）

| 步骤 | 内容 | 状态 |
|---|---|---|
| A1 | 候选集合年龄硬过滤（代码 + 测试） | **done**，本地完成，未部署 |
| A2 | 过期复核超时自愈（代码 + 测试） | **done**，本地完成，未部署 |
| 部署 A1+A2 + 观察窗（L2） | 未开始 | **planned**，须单独批准 |
| B | 收口现存 42 条（L3 生产数据） | 未开始 | **planned**，须单独批准，不与 A 的部署混在一起 |

**本批次没有部署、没有推送、没有碰生产数据库、没有撤任何交易所挂单、
没有改任何自动交易开关。**

---

## A1 · 候选集合年龄硬过滤

`src/telegram_kol_research/strategy_thread_candidates.py`。

三条同时成立才把 lifecycle 挡在候选集合外：

1. `lifecycle_status ∈ {"pending_entry", "expired"}`
   （常量 `STALE_FILTERABLE_LIFECYCLE_STATUSES`）；
2. `signal_at < 当前消息 posted_at - STALE_LIFECYCLE_MAX_AGE`（72 小时）；
3. `_has_unsettled_exchange_leg(...)` 为假。

阈值 `STALE_LIFECYCLE_MAX_AGE = timedelta(hours=72)` 是**模块级常量，
并且同时被 `recent_active_thread` 加分项使用**——不是两个 72，是同一个对象用两次，
所以过滤与加分不可能各读各的钟。测试 `test_the_age_filter_reuses_the_recency_bonus_threshold`
断言这一点，`test_stale_unbound_pending_entry_leaves_the_set_but_a_bound_one_stays`
顺带断言"活下来的老 lifecycle 不带 recency 加分"。

### 条件 3「还有未了结的腿」的定义

判定全部来自现成的 `_binding_context`，不重新推导绑定状态。满足**任一**即算"还有未了结的腿"：

- `lifecycle.execution_binding_id is not None`——设计稿写的
  `execution_binding_id IS NULL` 就是这一条。**绑定已 closed 也算未了结**：
  "binding 行写着 closed" 是我们自己账本的说法，不是交易所的说法，
  而多留一条老线程的代价只是一个低分候选。
- `binding_summary is not None`——同一事实的另一侧读法。
- `risk_state != "no_current_risk"`——这一支专门接住
  "lifecycle 指向一条已经不存在的 binding 行"：`_binding_context` 那时返回
  `uncertain_risk` + 空 summary，上面两条都看不见它。
- `live_verified_pos_ids` / `pending_entry_leg_ids` / `uncertain_entry_leg_ids` 任一非空。

**今天第一条已经蕴含后面几条**（无 binding id 时 `_binding_context` 只会返回
`no_current_risk` 和空元组），所以后几条目前不可达。仍然写出来的理由：
如果哪天 `_binding_context` 学会在没有 binding id 的情况下报告腿，
这个过滤器必须继续 fail closed，而不是开始丢掉有敞口的 lifecycle。

### `posted_at is None`

按「不过滤」处理。理由写在代码注释里：没有时间戳的消息不能说比任何东西晚，
而缺时间戳不是"这条策略过期了"的证据；`recent_active_thread` 加分项对同一个输入
一直就是这么处理的，两边现在共用同一个 `stale_cutoff`。

### `exact_single_current_risk_thread` 不加过滤（判断与依据）

**不加。** 三条依据，第一条是决定性的：

1. **方向反了。** 这个函数是穷举扫描，多看一条线程只可能让它**拒绝**
   （任何一条非目标线程带风险就返回 `False`）。从它的循环里拿掉行，
   只会让平仓更容易被授权——而它之所以 `yield_per` 流式扫描全部活跃线程，
   正是因为不允许任何东西收窄它。
2. **今天加了也是空操作。** A1 会过滤掉的 lifecycle 必然没有执行绑定，
   于是 `_binding_context` 对它恒返回 `no_current_risk`，它本来就不会造成拒绝。
   唯一会变的是它自己当目标时——那会变成拒绝，方向安全但不可达。
3. **语义不对。** 它服务 `_authorizes_exact_context_risk_reduction`，
   问的是"已入场的当前风险"，而设计稿 §5 明确不动 `entered` / `holding` 的任何判定。

所以"买不到任何东西，却在 `_binding_context` 一旦改动时削弱一道闸"。
判断与理由写进了该函数的 docstring，不只写在这里。

---

## A2 · 过期复核超时自愈

`src/telegram_kol_research/lifecycle_monitor.py`，
`_auto_close_out_timed_out_expiry_reviews` + `_claim_expiry_auto_closeout`。

模块级常量：

- `EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT_DAYS = 7`
- `EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT = timedelta(days=7)`（由上一行派生）
- `EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION = "expiry_auto_expired_review_timeout"`
- `EXPIRY_REVIEW_AUTO_CLOSEOUT_NOTE_PREFIX = "超时自动收口（非人工判定）"`

命中条件（选取与写入**两处都写全**）：

- `lifecycle_status == "pending_entry"`
- `management_action == "expiry_review_requested"`
- `expiry_review_next_at IS NULL`
- `expiry_review_notified_at IS NOT NULL` 且 `<= now - 7 天`
- **`execution_binding_id IS NULL`**

写入：`lifecycle_status='expired'`、`exit_reason='expired'`、`exited_at=now`、
`management_action=EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION`、`management_note=` 超时自动收口说明、
`last_checked_at`/`updated_at=now`。`expiry_review_notified_at` **保留不动**——
那是"我们什么时候问过"的记录。

**有执行绑定的一律不自动处理**，继续等人。这一条在 `_claim_expiry_auto_closeout`
里作为条件 UPDATE 的 WHERE 子句再写一遍：select 与 write 之间可能有人按了按钮、
有执行器挂上了 binding，而这条判据必须在**写入那一刻**成立
（"现在没有交易所挂单" ≠ "刚才没有交易所挂单"）。

事后可分辨：`management_action` 是一个全新值，不与 Telegram 按钮写的任何
`expiry_expired_*` / `expiry_cancelled_*` 重名；`management_note` 以
「超时自动收口（非人工判定）」开头，而 `telegram_bot_commands` 里每一条人工 note
都以「人工」开头。测试同时断言了 note **不**以「人工」开头。

---

## 设计稿没写、由我决定的几件事

1. **只收口 `pending_entry`，不收口 `entered`。** 设计稿只说"自动置为过期"。
   `entered` 且有未触发入场腿的 lifecycle 必然带执行绑定，所以"无绑定"规则本来就排除它；
   但把 `pending_entry` 写进查询里，是为了将来有人改了腿的判定逻辑时，
   真实持仓也不会悄悄走进这条路径。这是改动现有运行语义最小的读法。
2. **「每日一条」的落地方式：整个收口扫描每个 UTC 日最多跑一次**，
   一次扫描发一条汇总，列出这一轮关掉的全部 lifecycle。
   另一种读法（每轮都收口、通知每天一条）会让某些收口无人知晓，所以没采用。
   日期标记 `self._last_expiry_auto_closeout_day` 是**进程内状态，没有 schema 变更**：
   lifecycle_monitor 是 worker 单例任务；重启后最坏情况是当天多跑一次扫描，
   而那次只会关掉"新变得符合条件"的行，没有就完全沉默。
   标记在扫描**成功之后**才写，所以一次失败的扫描会在下一轮重试而不是被静默跳过。
3. **复用的通知路径 = 现有的 `expiry_review_notifier` 通道**，不新造。
   汇总 payload 带 `notification_kind = PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND`，
   `system_operator_bot.send_pending_entry_expiry_review` 据此分派到
   `format_pending_entry_expiry_auto_closeout_message`，并且**不挂按钮**——
   决定已经做完了，对一条已过期的策略提供「继续等待」按钮没有意义
   （它的 handler 本来也会拒绝）。web_app 的接线一行未改。
   常量定义在 `system_operator_bot` 而由 `lifecycle_monitor` import，
   方向是被迫的：`lifecycle_monitor` 已经传递依赖 `system_operator_bot`，
   反向 import 会成环。
4. **没有通知器就不自动收口。** 收口扫描放在 `_prepare_pending_expiry_reviews` 里面，
   而后者只在配置了 expiry review notifier 时才会被调用。
   所以不存在"悄悄收口、没人被告知"的部署形态。
5. **除通知外还留一行日志。** 通知投递失败会被 `_deliver_expiry_review_notifications`
   记日志吞掉，所以收口成功后额外 `logger.info` 一行带全部 lifecycle id；
   真正持久的记录是每行自己的 action 与 note。
6. **通知长度上限**（设计稿没提，写状态文档时才想到）。
   这条路径见到的**第一批**不是"一天的量"，而是积压的全部存量——今天是 42 条，
   而原则上没有上界。Telegram 超过 4096 字符直接拒收，**一次拒收丢的是整条通知**。
   所以列表按 `PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_LISTED_MAX = 40` 截断，
   条数仍然如实写在抬头，末尾补一句"另有 N 条未逐条列出"。
   实测 42 条渲染后 3779 字符。截断只影响这条新消息的排版，不改任何收口语义。

---

## 测试

新增两个文件：

- `tests/test_strategy_thread_candidate_age_filter.py`（9 条）
- `tests/test_lifecycle_expiry_review_auto_closeout.py`（13 条）

每条"过滤生效"的用例都配了一条同夹具、同时钟的"不生效"断言——
否则 `not in` 分不清"过滤器挡住了"和"夹具根本没建"。

### 变异检验

全部在被测改动之外的**备份文件**上做（先备份、变异、从备份拷回，前后核 sha256），
不用 `git checkout --`。

A1（`strategy_thread_candidates.py`）：

| 变异 | 结果 |
|---|---|
| 去掉条件 1（状态） | 2 红：`entered` / `holding` 两条 |
| 去掉条件 2（年龄） | 5 红（含 3 条既有用例，它们的 `pending_entry` 被全部过滤掉） |
| 去掉条件 3（敞口） | 2 红：有绑定的那半、有挂着入场腿的那条 |
| `posted_at is None` 不再 fail open | 1 红：`posted_at` 为空那条 |
| 整个过滤器关掉 | 5 红，含 19030 等价回放 |

A2（`lifecycle_monitor.py`，同一性质的两处守卫一起关）：

| 变异 | 结果 |
|---|---|
| 去掉 `execution_binding_id IS NULL`（select + claim） | 2 红：有绑定那条、写入前抢绑定那条 |
| 去掉 7 天判据（select + claim） | 3 红（含 1 条既有用例） |
| 去掉「人工已答复」判据（action + next_at，select + claim） | 1 红：人工「继续等待」那条 |
| 状态放宽到含 `entered` | 1 红 |
| 去掉每日一次的日期标记 | **第一版用例全绿** —— 见下 |

**日期标记那条变异第一次是绿的，用例被改掉了。** 原用例是"三条一次关完，
同一天再跑一次没有新通知"——而三条已经全被关掉，第二轮本来就没有可关的东西，
所以有没有日期标记都沉默，用例分辨不了。改成：第一轮关掉 A 并通知；
随后建一条同样符合条件的 B，同日再跑一次，断言 B **仍是** `pending_entry`
且通知数不变；隔天再跑，断言 B 被关掉且第二条通知出现。改后该变异转红。
（这正是 ARCHITECTURE「否定式断言分辨不了'没走到'与'走到了并放行'」的同一形状。）

### 全套

基线 **9623 passed / 4 skipped**。最终候选（全部生产代码改完之后跑的那一次，
`uv run pytest -q`，工作树在运行期间未改动）：

```
9645 passed, 4 skipped, 107 warnings in 802.91s (0:13:22)
```

差 **+22 passed**，恰好等于新增的 9 + 13 条用例，skipped 不变。没有新的失败。

跑了两次全套。第一次 `9644 passed / 4 skipped`（824.72s）之后，我在写状态文档时
发现汇总通知的条目列表没有上限（见下面"通知长度上限"），改了
`system_operator_bot.py` —— 生产代码改了就是新的最终候选，于是按 AGENTS.md
重跑一次，得到上面这个数。两次之间只差那一条新增用例。

工作树没有 `.venv`，按 ARCHITECTURE 的做法建了指向主检出的符号链接
（`tests/test_server_update_scripts.py` 与 `tests/test_minimal_server_updater.py`
会用 `<测试文件所在仓库根>/.venv/bin/python`，没有它那 15 条会一起 exit 2）。

---

## 下一步（都未开始，都要单独批准）

1. 部署 A1 + A2 并起 **L2** 观察窗。A1 改变的是"哪些策略能被选为管理目标"，
   属交易语义边界。观察窗要能回答的那件事：**过滤真的挡住过东西**——
   光看"没有再误选"分不清"守卫起了作用"和"这段时间本来就没有要挡的"。
2. **B**：收口现存 42 条，走既有的 `expiry_expire_cancel` 状态转换，不用裸 SQL，
   L3 规程（备份、`PRAGMA quick_check`、前后计数、42 个 id 清单）。
   注意 A2 上线后，这 42 条会在**第一个 UTC 日的第一轮扫描**里被自动收口掉——
   它们的形状正是 A2 的命中条件（全部 `expiry_review_requested`、
   `expiry_review_next_at` 全为 `NULL`、通知时间 2026-07-05～09-19、全部无执行绑定）。
   所以 **B 与 A2 的部署顺序要先想清楚**：先部署 A2，B 多半就不必手工做了，
   但那也意味着这 42 条会由自动路径一次性关掉并发一条汇总通知。
   这一条是我在实现时发现的，设计稿没有写。

## 指挥会话的复核（2026-09-25）

**独立复跑全套**：`9645 passed, 4 skipped`（794.90s），与实施方汇报的数字一字不差，
说明没有被并发跑测试的其他会话干扰（worktree 的 `uv run` 用的是主检出那个共享 `.venv`）。

### 一处判断被生产数据推翻：A2 的状态过滤是承重的，不是「belt-and-braces」

实施方在判断里写道：「`entered` 的策略只要有未触发的入场腿就一定有执行绑定，
所以无绑定规则本来就排除了它，把状态写进查询只是防止将来腿逻辑变化」。

**生产数据不是这样。** 2026-09-25 实测：

```
lifecycle_status = 'entered' 且 management_action = 'expiry_review_requested'  → 8 条
其中 execution_binding_id IS NULL                                              → 8 条（全部）
其中通知已超过 7 天（即满足 A2 其余全部判据）                                   → 1 条
```

| id | 标的 | 方向 | entered_at | 通知时间 |
|---|---|---|---|---|
| **1133** | BTC | long | 2026-09-10 | **2026-09-09（超 7 天）** |
| 1262 | BTC | long | 2026-09-24 | 2026-09-21 |
| 1270 | BTC | long | 2026-09-23 | 2026-09-22 |
| 1274 | BTC | long | 2026-09-23 | 2026-09-22 |
| 1275 | ETH | long | 2026-09-23 | 2026-09-22 |
| 1299 | NEAR | long | 2026-09-24 | 2026-09-23 |
| 1300 | BCH | long | 2026-09-23 | 2026-09-23 |
| 1308 | BTC | long | 2026-09-24 | 2026-09-24 |

也就是说：**那条 `lifecycle_status == "pending_entry"` 是今天唯一挡住 A2 自动过期
一条我们认为已入场的持仓（1133）的东西。** 代码写对了，理由写错了——而错的理由更危险，
因为将来有人读到「反正无绑定规则覆盖了」就可能把状态过滤删掉。
docstring 已改写，写明它是承重的、连同 1133 这个反例，并声明「不要以为无绑定规则覆盖了它」。

改动经 AST 比对确认**只动 docstring，可执行代码完全相同**，因此不构成新的最终候选，
不需要再跑一次全套（聚焦测试 22 条通过）。

### 顺带发现，不属于本批次：8 条 `entered` 却没有执行绑定

「已入场但没有执行绑定」本身就值得单独查——它意味着我们认为开着的仓位在账上没有绑定记录，
可能是归属缺口。**本批次不处理**，记在这里免得被忘掉。

### A2 首次 sweep 的实际打击面：41 条，不是 42

| 核对项 | 结果 |
|---|---|
| 满足 A2 全部判据的 | **41**（另 1 条通知不满 7 天，这轮轮不到） |
| 其中人按过「继续等待」的 | 0 |
| **有执行绑定却也满足其余判据的** | **0** |
| 通知时间跨度 | 2026-07-05 ～ 2026-09-18 |

## 部署与 L3 收口（2026-09-25，已完成）

| | |
|---|---|
| 部署 sha | `e08ac174e353f3c271f1907ccc399f1e53b0fe80` |
| 回滚 sha | `5ca19513fdd9cc023777ad9dd014f1e065699517` |
| 备份 | `/root/research-backup-preA2-20260925T130524Z.db`，1,237,438,464 字节 |
| 备份 sha256 | `66a56ff73b50760305dd70c810080a68b5b741a6abbf94b2565df14cc7b8a80c` |
| 备份完整性 | `PRAGMA quick_check` → `ok`（跑在快照上，不加重生产库） |
| 事前 id 清单 | `/root/preA2_target_ids.txt`（41 条） |
| 观察窗证据 | `/root/a2-observation.log` |

用户选择「把这次部署当作 B 来办」：A2 上线后第一次 sweep 即完成数据收口，
因此部署前按 L3 备份、取整数计数、并**预先存下将被影响的精确 id 清单**，事后逐字比对。

### 前后计数

| 指标 | 前 | 后 | 预期 |
|---|---|---|---|
| `strategy_lifecycles` 总数 | 1323 | 1323 | 不变 ✅ |
| `pending_entry` | 66 | 25 | 66−41 ✅ |
| `expired` | 314 | 355 | 314+41 ✅ |
| `entered` | 18 | 18 | 不变 ✅ |
| `holding` | 0 | 0 | 不变 ✅ |
| `execution_bindings` | 383 | 383 | 不变 ✅ |

**事前清单与实际收口清单 `diff` 完全一致**——恰好那 41 条，含肇事的 909，没有多动一条。
审计留痕：`INFO lifecycle_monitor Auto-expired 41 unanswered pending-entry expiry reviews
after 7 days: [360, ..., 1227]`；每条 `management_action = expiry_auto_expired_review_timeout`，
note 写明「超时自动收口（非人工判定）」，事后可与人工判定分辨。

### 那条承重注释在生产上当场兑现

**1133 安然无恙**，仍是 `entered` / `expiry_review_requested`；8 条 `entered` 无绑定的一条没少。
若当初按「无绑定规则反正覆盖了」省掉状态过滤，这次 sweep 会把 1133 这条我们认为开着的
BTC 持仓标记成过期。

### L2 观察窗（21:11–21:50，连续 40 分钟）

三服务全程 `active`；4 条真实消息、4 条决策、**4/4 带 `message_classes`**；
错误行全程持平于既有基线；**`自动收口累计` 40 分钟恒为 41——一日一次的标记生效，当天没有第二次 sweep**。

**诚实记一笔**：窗口内 4 条消息全是闲话（「BTC moves like drunk」这类），
**没有一条管理消息**，所以 A1 的过滤没有被实时流量走到。A1 的验证靠下面的回放。

### A1 生产回放（设计稿 §4 要求的那一条）

在生产上用 `python -B` 只读重算候选集合：

```
消息 19030 symbol=ETH  : 候选 = []            909 在里面? False
消息 19028 symbol=ETH  : 候选 = []            909 在里面? False
消息 19030 不限 symbol : 候选 = [1207]        909 在里面? False
```

- **909 消失了**。改动前它是那条消息唯一的 ETH long 候选，模型据此给出 `exact`。
- **1207 还在**——2026-09-16 的 BTC long、`entered`。**真实持仓一条没被过滤掉**，
  这正是 A1 三条件里「条件 1 永不过滤 `entered`」要保的东西。

后果是：今后这类消息拿不到虚假的 `exact`，只会得到 `unknown`，从而走上下文解析去问，
而不是静默指向一条一个月前的僵尸。这正是首次分析四分类设计稿 §8 阶段 3 的前置条件。

### 待确认

汇总通知应已发到运维 Bot 群，日志无发送失败记录，但投递结果在现有通知器里是吞掉的，
从日志看不出是否真的送达——**需要人在群里确认收到那条 41 条的汇总**。
