# 调整止盈：实施状态

- 设计稿：`docs/plans/2026-09-29-take-profit-adjustment-design.md`（第 5.5 节优先）
- 分支：`claude/take-profit-adjustment`（基于 `origin/main` 6450ac67）
- 风险级别：L3（新增会写交易所的生产者）；本分支只做到「候选 sha + 全量通过」，不推送、不部署
- 当前阶段：`in_progress`

## 分项进度

| # | 内容 | 状态 | 提交 |
|---|---|---|---|
| 1 | 纯函数模块 `take_profit_adjustment.py`（识别 + 单仓位规划）、directive 钩子、R3、回放夹具 | 完成 | 见 git log |
| 2 | 设置 `take_profit_adjust_mode`（默认 `shadow`） | 进行中 | |
| 3 | 规划器：`adjust_take_profit` 批次、180 s 截止、可被取代 | 未开始 | |
| 4 | 执行器：shadow / live、R4 收敛计划改写、超时收口、通知 | 未开始 | |
| 5 | 回放与合成用例、最终全量 | 未开始 | |

## 设计稿没写、由实施者决定的点（按「最少改变现有语义」取舍）

1. **钩子位置**：`resolve_management_directive` 里放在 `full_exit` 分支之后、第一个使用 `has_partial`
   的分支（`adjust_stop_loss` 原始动作分支）之前。撤单 / 加仓 / `exit_partial` / 全平四个分支仍然优先，
   所以「止盈出局」「全平」之类照旧是全平。
2. **「第一止盈位 P」（无冒号）不单独算正面证据**，只在同一条消息另有止盈标签、移动动词或分配比例时提供价位。
   原因：入场消息常写「第一止盈位 60950 移动止损至成本价」描述计划，现有测试
   （`test_ai_lifecycle_event_downgrades_first_take_profit_exit_to_management_update` 等）要求它仍走原路径。
3. **任何一个「现在减仓」的百分比都让整条消息回到现行减仓路径**（例如「止盈30%，剩下两个止盈位各50%」）：
   一条消息拆成「现在平」和「改止盈」两半正是不该猜的歧义。
4. **多目标消息不识别为调止盈**（`_explicit_multi_target`）：一段文字给多个策略时，同一个价位不能同时当两个币的止盈。
   对应设计稿 1.3「目标唯一时」。
5. **止损子项**：取标签形式「止损位：S」；没有标签但模型给了止损、且该价位在原文里以止损身份出现
   （复用 `_text_contains_explicit_stop_value`）时也取，避免「止盈位 X，止损移动到 Y」丢掉止损。
6. **只给比例的情况**：比例合计 100（「两个止盈位各50%」）→ 用策略价；合计不到 100 且没有价位
   （18294「至少50%自动止盈」）→ `take_profit_adjust_price_missing`。策略价档数（去掉已成交档后）与比例档数对不上 →
   新原因码 `take_profit_adjust_tier_count_ambiguous`，拒绝并提醒。
7. **只说一档且没有同价档**：按「第 N 止盈位」替换第 N 档（按离现价由近到远排序）；两者都没有时作为新档加入。
   合计超过剩余张数时从**其余档**的最远档削，被削到低于最小张数的档整档去掉；所说那一档本身不削。
8. **收敛计划改写用的比例**：按目标张数算比例，前几档向上取整到 6 位、余数给最后一档
   （`proportional_allocations`），保证收敛器用同一剩余张数重算时得到完全相同的张数。
   只说一档且合计不足剩余仓位时，收敛计划无法表达「部分覆盖」：收敛器若重试，会按比例覆盖全部剩余仓位。

## 新增原因码（需要中文标签，见汇报）

`take_profit_adjust_price_missing`、`take_profit_adjust_all_tiers_crossed`、`take_profit_adjust_size_below_minimum`、
`take_profit_adjust_already_satisfied`、`take_profit_adjust_allocation_invalid`、
`take_profit_adjust_tier_count_ambiguous`、`take_profit_adjust_tier_already_filled`、
`take_profit_adjust_size_invalid`、`take_profit_adjust_position_empty`、`take_profit_adjust_input_invalid`
