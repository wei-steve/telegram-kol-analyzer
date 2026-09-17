# 相邻准入死锁 + 市价入场几何：实施状态

日期：2026-09-16
规格：`docs/plans/2026-09-16-adjacent-entry-deadlock-and-market-entry-geometry-analysis.md` 第 8 节（A + B + C）
分支：`worktree-agent-ab9e7bc4842ebb3bf`（worktree `/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-ab9e7bc4842ebb3bf`）
基线：`759ff4bf`（`codex/deepcoin-auto-trading-v1` 当时的 tip）
状态：本地实施完成，**未推送、未部署、未发 Telegram**。第 6 节的 L2 观察窗由指挥会话负责。

## 各项结果

| 项 | commit | 改动文件 | 聚焦测试 |
|---|---|---|---|
| A 准入：邻居终态无动作视为已处理 | `58e19d8f` | `entry_assembly_admission.py` + 两个测试文件 | `test_entry_assembly_admission.py` 20 passed；`test_entry_admission_reconciler.py` 23 passed |
| B 几何：市价 + 动作词 + 单价 = 有价市价腿 | `b00e57cb` | `entry_price_geometry.py` + 两个测试文件 | `test_entry_price_geometry.py` 97 passed；`test_auto_trade_execution.py` 94 passed（另跑 `test_recovery_scan` / `test_recovery_live_submit` / `test_system_operator_bot` / `test_price_normalization` 共 234 passed 确认无回归） |
| C 过期告警带阻塞消息及其决策 | `6af72636` | `entry_admission_reconciler.py`、`runtime_incident_adapters.py`、`runtime_incidents.py` + 两个测试文件 | `test_runtime_incident_adapters.py` + `test_entry_admission_reconciler.py` 合计 91 passed；`test_runtime_incidents.py` 20 passed |

全套（最终候选 `6af72636`）：**8944 passed, 4 skipped, 0 failed**（45 分 31 秒）。

## 规格未覆盖、由本次自行决定的地方

1. **（C，影响最大）`runtime_incidents.py` 的 `_SUMMARY_FIELDS` 必须加两个键。**
   规格把该文件列为只读参考，只要求"摘要必须在边界内"。但
   `_validate_redacted_json_contract` 对 `redacted_summary` 用的是**封闭字段表**：
   未登记的键直接 `RuntimeIncidentBoundsError` → 详细摘要被拒 → 退回最小摘要，
   正是规格明令不能再发生的那件事。因此在 `_SUMMARY_FIELDS` 里登记了
   `blocking_raw_message_ids` 与 `blocker_decisions`，并按该表既有惯例写了理由注释。
   这是实现规格 8.3 的必要条件，不是范围扩张；除这两个键外该文件一行未改。
2. **（C）两个新字段是标量字符串，不是列表。** 规格写"int 列表 / 字符串列表"，
   但同一处边界检查明确拒绝 dict/list 值（`redacted_summary fields must be scalar`）。
   边界优先：`blocking_raw_message_ids` = `"16972,16915"`，
   `blocker_decisions` = `"16972 skipped mimo_no_action, 16915 blocked source_message_deleted"`。
3. **（C）决策标签用空格分隔，不用 `{id}:{status}/{reason}`。**
   `_safe_label` 会把 `:` `/` 变成 `_`，于是 `16915_blocked_source_message_deleted`
   成为一个 36 字符、三种字符类、去重 ≥12 的单 token——正好命中
   `_looks_like_opaque_secret`，整条详细摘要会被拒。空格不在该正则的字符类里，
   所以按 `id status reason` 空格分隔后每个 token 都短，边界稳过。
   另加 `_blocker_token`：任何 ≥32 字符的标签按下划线拆开并逐段截到 31 字符，
   使"未来出现一个又长又混合的 automation_reason"也不会把告警打回最小摘要。
4. **（C）无阻塞消息时两个键不出现。** `_summary` 本来就丢弃空值，所以
   ws 观测那条分支与"blocker 列表为空"的情况摘要与今天逐字节相同，指纹不变。
5. **（C）`MAX_REPORTED_BLOCKERS = 5` 定义在 `runtime_incident_adapters.py`，
   reconciler 从那里 import**（已验证正反两个 import 方向都不成环），
   避免同一个上限写两份。
6. **（C）`_report_entry_admission_expired` 里没有决策行的阻塞消息记 `"absent"`**
   （status 与 reason 都记 `absent`），与规格一致；`automation_reason` 为 NULL
   但有决策行的记 `"none"`。
7. **（A）`recognition_decisions` 的点查放在 `candidates` 之后、证据行之前**，
   一次 `in_(raw_ids)`（最多 41 条，走唯一索引），与规格 8.1 的位置一致。
8. **（B）分隔符可选**：`市价进场2415` / `市价进场：2415` / `市价进场/2415` 三种都放行，
   这是规格 8.2 自己写明的细化。动作词仍然必须存在。
9. **（B）`tests/test_auto_trade_execution.py` 确实有可低成本复用的骨架**
   （`test_wrong_geometry_candidate_alerts_even_when_entry_submission_is_disabled`），
   所以按规格补了集成用例：`entry_text="市价进场/2415"` 不再产生
   `entry_price_geometry_rejected` 事件。
10. **（环境）worktree 创建时落在旧提交 `0c29d3f6` 上**，与程序 tip 相差很远。
    工作树干净、分支上无独有提交，已 `git reset --hard 759ff4bf` 后再开工。

## 范围外的发现（规格 8.2 末尾要求记录）

**纯市价、没有任何数字的入场文本，现在过不了几何门，而且本次没有改。**
`市价`、`市价进场`、`现价做多`、`market` 这类文本一律 `indeterminate` /
`entry_price_geometry_ambiguous`：B 只放行"市价 + 动作词 + 一个绝对价格"，
没有价格就没有可校验的几何。调用方也帮不上忙——参考价只在第一次几何通过之后
才带进第二次计算（`auto_trade_execution.py` 1133 行），所以纯市价入场从来过不了
这道门。生产上成功的市价入场全是带价格的文本（`76700附近`、`77300`）。
这是独立问题，另议。本次新增的参数 `市价进场`（无价格）就是把这个现状钉住的回归测试。

## 未做的事

- 已过期的 6 笔入场**不补单**（规格 8 明令）。
- 不改识别提示词、`ai_recognition_config.py`、`prompt_defaults.py`、任何 `deepcoin_*` 模块。
- 不推送、不部署、不发 Telegram。

## 全套结果

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run pytest -q`（在 `6af72636` 这个最终候选上，
三项代码改动全部装配完成之后跑的唯一一次全套）：

```
8944 passed, 4 skipped, 107 warnings in 2731.18s (0:45:31)
```

之后只增加了本状态文档（纯文档），按 AGENTS.md 的风险自适应规则不需要再跑一次全套。

## 自查：断言本身是否成立（"test the check"）

C 的关键断言是"recorder 只被调用一次 = 没有退回最小摘要"。为确认它不是一条恒真断言，
用一次性脚本（`python -B`，不写字节码，已删除）把 `_blocker_labels` 换成一个
故意命中 `_looks_like_opaque_secret` 的 40 字符单 token，结果：recorder 被调用 **2 次**，
日志出现 `RuntimeIncidentBoundsError ... retrying minimal`，最终落的是最小摘要。
真实实现下是 1 次。断言两个方向都能区分。

## 部署与 L2 观察窗（指挥会话，2026-09-16/17）

- 审查：三处代码改动逐行对照规格第 8 节，一致。规格外的 `runtime_incidents.py` 改动只是把两个新摘要键登记进封闭字段表，
  否则详细告警会被边界检查打回最小摘要，属于实现 8.3 的必要条件，接受。
- 主检出快进合并到 `7a8e67c1`；聚焦测试 228 passed（admission / reconciler / geometry / incident adapters / incidents）。
- 预检：候选是生产 HEAD `5f26e721` 的直系后代；代码文件 5 个源文件 + 5 个测试文件；无 `pyproject.toml` / `uv.lock` 变更；
  交易所无在途入场腿；两条 pending 入场项（1112、1161）都不是相邻挂起，部署不会立即放行旧单。
- 部署：候选先推到 `origin/claude/entry-admission-deadlock`，`tg-deploy 7a8e67c138cc6a4d25cc7586959942b6d1258f3a`
  于 2026-09-16 21:25 UTC 完成，worker/web/ingest 全部 active；随后把同一 SHA 推到 `origin/codex/deepcoin-auto-trading-v1`。
  双向核对：`PASS: deployed sha is on the shared branch`、`PASS: 0 code files beyond production`。**回退点 `5f26e721`。**
- 部署后健康：三角色 loop-health 正常，Deepcoin WS `connected/healthy`，web `/login` 200，worker 日志无新错误
  （`recognition execution finding` 是部署前每 6 小时 5000+ 行的既有扫描输出，不计）。
- 观察窗：服务器只读监控（`/root/tg_observe_entry_admission.py`，每分钟采样，任一异常重置 30 分钟窗）。
  21:49:37 UTC 一次 4.5 s 事件循环停顿重置过一次窗口；栈在 `selector.poll`，同一天 worker 已有 6 次、ingest 4 次、web 6 次同类停顿，
  主机 2 GB 内存已用 swap 684 MB，判定为既有主机压力，与本次改动无关（见 `docs/plans/2026-09-16-db-lock-holder-and-loop-stall-analysis.md`）。
  **21:50:21 → 23:26:34 UTC 连续 1 h 36 min 无异常，5 条真实消息（2 个群），全部 `worker_completed` / 非策略 / mimo_no_action。**
  窗内：新增相邻挂起 0、几何拒单事件 0、交易所写入 0、`entry_admission_expired` 0。
- 证据：`/root/entry-admission-observation-2026-09-16.events`、`/root/entry-admission-observation-2026-09-16.samples.jsonl`（服务器）。
- 未在窗内出现的样本：没有自然到达的「无动作邻居 + 完整入场」序列，也没有「市价进场/价格」形状的入场。
  窗口证明的是真实流量下无回归；两条正向路径此刻只有单元/集成测试作证，**下一条命中这两种形状的自动交易群入场就是首个实盘样本**，
  届时核对 `entry_assembly_attempts`（应 woken 而非 pending 到期）与 `execution_events`（不应再有 `市价进场/…` 的几何拒单）。
- 范围外遗留（另议）：纯「市价」无数字的入场文本仍一律 indeterminate；主机内存压力导致的周期性事件循环停顿。
  （前者已由下面的 D 节实施，2026-09-17。）

---

# D：纯市价入场，有止损即可下单

日期：2026-09-17
规格：同一份分析文档的**第 9 节**（用户 2026-09-17 拍板：「市价入场，如果有止损价格也是可以的」，时效上限 3 分钟，
入场字段为空不算纯市价）。
分支：`worktree-agent-a32d270b618030baa`（worktree
`/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a32d270b618030baa`）
基线：`54865c98`（A+B+C 部署后、本节规格获批的那个提交）
状态：本地实施完成，**未推送、未部署、未发 Telegram**。验证等级 L2，部署与观察窗由指挥会话负责；回退点 `7a8e67c1`。

## 各项结果

| 项 | commit | 改动文件 | 聚焦测试 |
|---|---|---|---|
| 9.2 几何：纯市价 + 止损 ⇒ 只证保护侧 | `9623078e` | `entry_price_geometry.py`、`tests/test_entry_price_geometry.py` | `test_entry_price_geometry.py` 148 passed（改前 97）；另跑 `test_auto_trade_execution` / `test_recovery_scan` / `test_trading_decision` / `test_recovery_live_submit` / `test_system_operator_bot` 共 334 passed 确认零回归 |
| 9.3 + 9.4 下单路径：新鲜的纯市价按现价提交 | `523daaf8` | `auto_trade_execution.py`、`system_operator_bot.py`、`tests/test_auto_trade_execution.py`、`tests/test_recovery_scan.py`、`tests/test_trading_decision.py`、`tests/test_system_operator_bot.py` | 六个文件合计 488 passed（`test_auto_trade_execution` 97、`test_entry_price_geometry` 148、`test_recovery_scan` 19、`test_trading_decision` 10、`test_system_operator_bot` 121、`test_recovery_live_submit` 93） |
| 本状态文档 | 本 commit | `docs/entry-admission-and-market-entry-geometry-status.md` | 纯文档 |

全套（最终候选 `523daaf8`）：**9000 passed, 4 skipped, 1 failed**（1 小时 7 分 21 秒）。
新增测试 57 条，与 A+B+C 那次的 8944 passed 正好对得上（51 + 3 + 1 + 1 + 1）。

**那 1 条 failed 与本次改动无关，是机器负载下的挂钟抖动。**
`tests/test_authoritative_gap_recovery_loop.py::test_message_enqueued_within_one_fast_loop_interval`
起一个真实的 `run_authoritative_gap_recovery_loop`（`telegram_live_listener.py:1338`，**不在本次 diff 里**），
用 3 秒挂钟等作业出现。跑全套时本机 load average 11–22（我自己并发跑的三次 `--collect-only` 也在抢 CPU），
3 秒内那个 task 没被调度到，断言拿到空列表。核实方式两条：
- 单独跑该文件：`9 passed in 1.09s`；
- 按全套的真实顺序把**排在它之前及同批的全部用例**一起跑
  （`tests/archive tests/helpers tests/one_off tests/parsing tests/test_a*.py`）：`519 passed in 39.35s`。
本次改动的四个测试文件在字母序上全部排在 `test_authoritative_gap_recovery_loop` **之后**
（`test_auto_trade_execution` / `test_entry_price_geometry` / `test_recovery_scan` / `test_system_operator_bot` /
`test_trading_decision`），所以不存在"我的用例先跑污染了它"这条路径。

## 实际改了什么

- `validate_candidate_entry_price_geometry` 多一个关键字参数 `allow_reference_entry`（默认 False）。
  为真**且** `is_pure_market_entry_text()` 为真时才走新分支：跳过入场字段的绝对性证明，`entry_values = []`，
  止损/止盈的解析逐字沿用原代码。带 `resolved_entry_prices` 就把它当入场价交给共享校验器做完整方向几何；
  不带就只证保护侧（止损必须存在；多单每个止盈 > 止损，空单 <，相等即 `equal_boundary`），
  结果带 `reference_entry_required=True`。
- `auto_trade_execution` 两处几何调用都传 `allow_reference_entry=True`，并在 1121 行市价分支算出
  `entry_range` 之后、`geometry = candidate_geometry` 之前加时效门：`PURE_MARKET_ENTRY_MAX_AGE = 3 分钟`，
  超时或腿不是市价 ⇒ `_record_entry_geometry_rejection(..., stale_market_reference_result(entry_text))`。
- `recovery_scan.py:239` 与 `trading_decision.py:78` **不传**这个参数，两处各加了一条钉住现状的测试。

## 规格未覆盖、由本次自行决定的地方

1. **（本次最重要的一条）保护侧分支照抄共享校验器的归一化，而不只是比大小。**
   规格 9.2 只写了方向判据。实现里 `_reference_entry_protection_geometry` 与
   `validate_entry_price_geometry` 用同一套：`price_tick` 用 `_positive_decimal` 解析，
   解析不出 ⇒ `indeterminate/ambiguous(price_tick)`；止损与止盈一律经 `_normalize_value(tick=...)`
   转 Decimal 并按 tick 向下取整。否则同一个候选在「带参考价」与「不带参考价」两次调用之间
   会用两套数值语义，而这两次调用在生产上总是先后发生在同一个候选身上。
   检查顺序也照抄：side → price_tick → 止损 → 止盈 → 方向（共享校验器的顺序去掉 entries 那几步）。
2. **止损缺失的返回值按规格逐字写，没有补证据字段。**
   规格写的是 `_indeterminate(REQUIRED_VALUE_MISSING, "stop_loss", None)`；共享校验器在同一情形下
   会带上 `entries=` / `take_profits=`。两种都说得通，按「规格明写则照办」取前者：
   缺止损时告警里的 `take_profit_prices` 为空列表。
3. **只有 `resolved_entry_prices is None` 才走保护侧分支。**
   规格说的是「没带参考价（候选阶段）」。如果有调用方显式传了一个空集合，就落回共享校验器，
   得到 `required_value_missing/entry_prices`——与今天非纯市价候选拿到的答案一致，是 fail-closed 的那一边。
   生产两处调用传的都是二元组 `entry_range`，不会走到这里。
4. **`normalized_entry_prices` 在参考价以二元区间传入时是 `("76500", "76500")`，不去重。**
   `entry_min == entry_max == "76500"`。走的是共享校验器的原路径（`_result` 按 `entries` 逐个 `_display`），
   没有为纯市价另开去重逻辑——去重会让这一条的告警形状与别的候选不一样。
5. **`reference_entry_required` 加在 `EntryPriceGeometryResult` 末尾、默认 False，`_result()` 透传。**
   `bounded_evidence()` 多这一个键；`execution_events.enqueue_entry_price_geometry_rejection_notification`
   的白名单不含它，所以通知 payload 与今天逐字节相同，指纹不变。
   `passed` 仍然只看 `status == "valid"`。
6. **带上参考价重算之后 `reference_entry_required` 回到 False。** 这个标志的含义是「还欠一个入场价」，
   不是「这条曾经是纯市价」。测试对两种状态都有断言。
7. **`is_pure_market_entry_text` 先 `strip().lower()`，非字符串入参一律为假。**
   与 `_proves_absolute_candidate_field` 的入口处理一致。空入场字段恒为假（规格明令）。
8. **`_as_utc` 在 `auto_trade_execution.py` 里新写了一个局部的**（该模块原本没有，规格允许照邻近代码写），
   实现与 `entry_admission_reconciler._as_utc` 相同。**`now` 也过一遍 `_as_utc`**，规格只写了 `posted_at`：
   现有调用方传的 `processed_at` 都是 aware 或 None，所以这是空操作；但万一有人传 naive，
   规格原样的 `now - posted_at` 会抛 `TypeError`，而时效门抛异常等于放弃一次本该 fail-closed 的判断。
9. **`entry_execution_type != "market"` 这一支的现实含义。** 纯市价文本里 `current price` / `market price`
   能被 `_MARKET_LABEL_RE` 认出，却不在 `_infer_entry_execution_type` 的词表里（它只认 `market`/市价/现价/
   直接/马上/立即），于是会被这一支以 `market_reference_stale` 拒掉。规格写的就是这个分支，方向是 fail-closed，
   本次不扩词表——改词表会顺带改动限价腿的判定。
10. **通知那一行用插值实现。** `explanation` 对其他所有原因码都是空串，所以其余文案逐字不变
    （`test_entry_geometry_operator_alert_is_bounded_and_redacted` 增加了一条 `"说明" not in rendered` 断言来钉住）。
    `MARKET_REFERENCE_STALE` 长 43 字符，在通知 payload 的 64 字符截断之内（测试里也断言了）。
11. **状态文档单独一个 `docs(entry):` commit**（规格 9.6 允许二选一）：全套结果只有在最终代码候选装配完之后才存在。
12. **「开关为假时逐字节不变」是用对象相等证明的**：
    `test_opt_in_changes_nothing_for_a_text_that_is_not_a_price_less_market_entry`
    对 11 种非纯市价文本 × 2 种参考价设置，断言 `allow_reference_entry=True` 与默认调用返回的
    `EntryPriceGeometryResult` **相等**（frozen dataclass 逐字段比较），而不是只比 status。

## 未做的事

- 不碰下单量、腿构造、`deepcoin_*` 模块、识别提示词、`ai_recognition_config.py`、`prompt_defaults.py`，
  以及三处 `validate_order_draft_price_geometry` 调用点（它们仍是最后一道门）。
- 不补任何历史上被 `entry_price_geometry_ambiguous` 拒掉的纯市价入场（如 2026-09-10 raw 15832 / 候选 2293）。
- 不推送、不部署、不发 Telegram。
- **`PURE_MARKET_ENTRY_MAX_AGE` 没有做成运行时设置**，规格明令是常量。
- 正向路径此刻只有单元/集成测试作证。**下一条真实的纯市价 + 止损入场就是首个实盘样本**，
  届时核对 `execution_events` 是否有 `open_market_position` 而非几何拒单，以及仓位是否在成交后拿到止损。
