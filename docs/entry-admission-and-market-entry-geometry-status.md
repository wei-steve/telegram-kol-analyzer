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
