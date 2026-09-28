# 2026-09-28 核对问题修复 — 问题 2 / 3 / 4 实施状态（分支 `audit-fix-b`）

- 设计稿：`docs/plans/2026-09-28-audit-fixes-design.md`（已批准，第 6 节全部按推荐）。
- 范围：问题 2、3、4。问题 1 由另一条线实施，本分支不碰
  `protection_attribution` / `strategy_management_executor` / `strategy_management_planner` /
  `web_app` / `protection_authority`。
- 基线：`f2846c41`。不推送、不部署、不改识别提示词。

| 问题 | 状态 | 提交 |
|---|---|---|
| 2 比例错配 + 「加仓后」 | 完成（本地测试） | 见 git log「audit fix 2」 |
| 3 告警送达 | 完成（本地测试） | 见 git log「audit fix 3」 |
| 4 模糊止损修饰词 | 未开始 | |

## 问题 2

- `management_directives._percentage_values`：找到最近的数量动词后，调用
  `_percent_belongs_to_verb(clause)`；返回 False 时跳过该百分数（`previous_quantity=False`）。
- `resolve_management_directive`：风险增加词判定前剔除「平加仓」和「加仓后」（仅这一写法）。
- **与设计稿字面的偏差（有意）**：设计稿写「动词与百分数之间出现句子边界就跳过」。按字面实现会让
  既有的加固测试失败——`减仓\n150%`、`减仓；比例120%`、`保留\n-20%`、`减仓 -\n10%`、`减仓1,5%`
  必须仍被拒绝（A-8 的不变量：给出了无效内容就不能退回默认 50%）。设计稿同一段也写了
  「其余情况与现在完全一样」。所以实现为：把动词与百分数之间的文字按句子边界
  （换行、`。！!？?；;‼`）和分句逗号（`，`，以及不夹在两个数字之间的 `,`）切块，**只要百分数所在块之前的任一块含数字**
  就跳过。即：动词已经带了自己的数字/陈述，百分数属于另一句话。
  - 设计稿的全部回放用例（R2-a…f）都满足这一条（#19597 中间有「83200」，#17900/#18153 中间有
    「80600」「64100」，#18603 中间有「95点」，`平仓78031.7，盈利126.05%` 中间有「78031.7」）。
  - 残留缺口：「全部出局！持仓收益高达370％」这种动词和百分数之间**没有任何数字**的写法仍会被拒
    （与修复前相同）。生产样本里没有见到这种形态；如需覆盖，要先决定怎样区分它和 `减仓；比例120%`。
- 测试：`tests/test_management_directives.py` 中 `test_r2a_*`…`test_r2f_*`；修复前 6 个失败、修复后全部通过。

## 问题 3

- 3a：`config.ALWAYS_NOTIFIED_INCIDENT_TYPES` 加入 `management_fraction_rejected`。notify_only 群的行在写入时
  已被 `record_fraction_rejection` 标成 `suppressed`，不会多发。没有既有测试断言该集合的精确内容（只有
  「某类型在集合里」的断言），无需改动。
- 3b：`oncall_alerts`
  - `compose_case_alerts` 的「已恢复」告警、`compose_diagnosis_alert` 的诊断告警：不再检查上限、不再计数。
  - 开案：`_case_open_bypasses_cap(case)` 为真则不受上限拦截，但仍计数；其余开案维持上限 30。
    合并通知、上限通知、每日汇报不变。
  - **「auto_trade 群」的判定（设计稿未说明，本实现的选择）**：采用保守规则
    「严重度 high/critical，且（规则含 D6c，或案例带 `raw_message_id`）」。原因：群的交易模式只在
    `groups.yaml` 里，生产库没有；值守进程不读它，且架构边界测试只允许 `oncall_alerts` 导入
    `oncall_codex` / `oncall_state`；要拿到模式就得给值守新增一次生产配置读取。带 `raw_message_id`
    的规则（D1a–c、D2、D3、D6a、D6b）都只在群里有持仓 / 批次 / 删除退出时才开案，实际就是交易群。
    仍受上限约束的：D1d（medium）、D4/D5 健康类、D5a 读库失败。合并规则的 `evidence["rules"]` 里含 D6c 也算 D6c。
  - 既有测试 `test_the_daily_cap_stops_at_the_limit_and_resets_the_next_beijing_day` 用的是高严重度、带消息的案例，
    按批准的设计它们现在不受上限拦截；改为 medium（D1d）案例，测试的上限机制本身不变。
- 测试：`tests/test_system_operator_bot.py::test_r3a_*`（2 个），`tests/test_oncall_alerts.py::test_r3b_*`（2 个）、
  `test_r3c_*`（1 个）；修复前全部失败、修复后通过。
- 部署提醒（设计稿第 5 节）：值守代码有改动，部署后要单独重启 `telegram-kol-oncall.service`。
