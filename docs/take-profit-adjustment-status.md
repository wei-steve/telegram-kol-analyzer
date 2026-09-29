# 调整止盈：实施状态

- 设计稿：`docs/plans/2026-09-29-take-profit-adjustment-design.md`（第 5.5 节优先）
- 分支：`claude/take-profit-adjustment`（基于 `origin/main` 6450ac67）
- 风险级别：L3（新增会写交易所的生产者）。本分支只做到「候选 sha + 全量通过」：**未推送、未部署、未连服务器**
- 当前阶段：`in_progress`（代码完成，等待主会话审阅与上线决定）

## 分项进度

| # | 内容 | 状态 | 提交 |
|---|---|---|---|
| 1 | 纯函数模块 `take_profit_adjustment.py`（识别 + 单仓位规划）、directive 钩子、R3、回放夹具 | 完成 | `ff00ae26` |
| 6 | 设置 `take_profit_adjust_mode`（默认 `shadow`） | 完成 | `e6b8e4fb` |
| 2+4 | 识别到候选的端到端用例；规划器：`adjust_take_profit` 批次、180 s 截止、可被取代 | 完成 | `869bf1fe` |
| 5 | 执行器（shadow / live）、R4 收敛计划改写、超时收口、通知 | 完成 | `8fd1ad2f` |
| 7 | 回放与合成用例补齐、状态文档、最终全量（10438 passed, 4 skipped） | 完成 | 最后一个提交（见 git log） |

上线（不在本分支范围）：`take_profit_adjust_mode` 默认 `shadow`，部署后先看 3～5 笔真实样本的影子通知，
再由用户决定切 `live`；切 `live` 后第一笔实盘样本直接核对交易所历史（撤了哪几张、挂了哪几张、合计是否不超过持仓）。
回滚：设置改回 `disabled`，或 `tg-deploy` 回滚到部署前的生产 sha。无 schema 变更，无生产数据改动。

## 设计稿没写、由实施者决定的点（按「最少改变现有语义」取舍）

1. **钩子位置**：`resolve_management_directive` 里放在 `full_exit` 分支之后、第一个使用 `has_partial`
   的分支（`adjust_stop_loss` 原始动作分支）之前。撤单 / 加仓 / `exit_partial` / 全平四个分支仍然优先，
   所以「止盈出局」「全平」之类照旧是全平。
2. **「第一止盈位 P」（无冒号）不单独算正面证据**，只在同一条消息另有止盈标签、移动动词或分配比例时提供价位。
   原因：入场消息常写「第一止盈位 60950 移动止损至成本价」描述计划，现有测试
   （`test_ai_lifecycle_event_downgrades_first_take_profit_exit_to_management_update` 等）要求它仍走原路径。
3. **任何一个「现在减仓」的百分比都让整条消息回到现行减仓路径**（例如「止盈30%，剩下两个止盈位各50%」）：
   一条消息拆成「现在平」和「改止盈」两半正是不该猜的歧义。
4. **多目标消息不识别为调止盈**（`_explicit_multi_target`）：同一个价位不能同时当两个币的止盈。对应设计稿 1.3「目标唯一时」。
5. **止损子项**：取标签形式「止损位：S」；没有标签但模型给了止损（或识别层按 R3 从原文补出的止损）、
   且该价位在原文里以止损身份出现（复用 `_text_contains_explicit_stop_value`）时也取。
   规划器用同样的输入重新从原文读一次，**不读候选的 `stop_loss_text`**（它在没有明写止损时默认是策略原止损）。
   止损只允许收紧；与现有止损完全相同算「不用改」；放松则整条拒绝、零写入（`explicit_stop_adjustment_not_risk_tightening`）。
   止损写法沿用 `adjust_stop_loss`：把该仓位的每一张止损（主止损、备份止损）都挂到新价，保持各自的账本用途。
6. **只给比例**：比例合计 100（「两个止盈位各50%」）→ 用策略价；合计不到 100 且没有价位
   （18294「至少50%自动止盈」）→ `take_profit_adjust_price_missing`。策略价档数（先去掉已成交档）与比例档数对不上 →
   新原因码 `take_profit_adjust_tier_count_ambiguous`，拒绝并提醒。
7. **只说一档且没有同价档**：按「第 N 止盈位」替换第 N 档（按离现价由近到远排序）；两者都没有时作为新档加入。
   合计超过剩余张数时从**其余档**的最远档削，被削到低于最小张数的档整档去掉；所说那一档本身不削。
8. **收敛计划改写用的比例**：按目标张数算比例，前几档向上取整到 6 位、余数给最后一档
   （`proportional_allocations`），保证收敛器用同一剩余张数重算时得到完全相同的张数（有单测）。
   例外：只说一档且合计不足剩余仓位时，收敛计划无法表达「部分覆盖」，收敛器若重试会按比例覆盖全部剩余仓位。
9. **规划器裁剪的检查**（只对 `adjust_take_profit`）：
   - 不跑保护证据块的「逐单价位/张数比对」部分（它正是 #18199 批次 174 被
     `protection_price_or_size_mismatch` 拦下的地方，比对的差异恰恰是这条指令本身）；只保留该块的
     「挂单快照完整」判据（不完整 → `target_protection_snapshot_incomplete`，沿用现有的可见性重试）。
   - 能力检查只要求基础写入条件（仓位精确、快照完整、无未决写入），不要求「已证明拥有止损」；
     止损的存在改在执行时由 `resolve_protection_authority` 判定，没有止损就拒绝（`take_profit_adjust_stop_missing`）。
   其余（身份、绑定、实时仓位经济学、合约规格、保护事故、部分减仓冻结、幂等）全部照旧。
10. **可被取代的判据**：「未开始写」= 该批次所有 leg 仍是 `planned`。执行器在第一次交易所写入前、在自己的事务里把 leg
    改成 `reserved`，且全程持有仓位权威锁（规划器也持同一把锁），所以不存在判断与写入之间的竞态。
    取代者：`full_exit`、`move_stop_to_break_even`、`adjust_stop_loss`（原因 `superseded_by_risk_reduction`），
    以及更新的一条调止盈（原因 `superseded_by_newer_take_profit_adjustment`）。已开始写的不取代，由 180 s 截止兜底。
    同一策略存在其它未终结批次时，新的调止盈直接 `prior_management_batch_unresolved`，不建批次。
11. **截止时间**：`management_recovery_timeout.expire_take_profit_adjustment_deadlines`，每个管理 worker tick 运行，
    对任何未终结状态（`ready` / `executing` / 其它）的调止盈批次生效，持仓位权威锁后收口为
    `blocked / take_profit_adjust_deadline_expired`，`transition_batch` 同事务写通知。执行器自己在开始时也检查。
    原有 `expire_stuck_management_recoveries`（只管 `recovery_required`）不变。
12. **撤单阶段失败**：撤单请求已被交易所接受的旧单在我们的模型里保持 `superseded`，其余逻辑腿与收敛计划恢复原样；
    `protection_replacement` 已记 `take_profit_replace_incomplete` 事件（原有逻辑，提示人工核对）。
13. **执行中断**：再次进入时发现有 leg 已离开 `planned`（写入结果未知），不重跑，直接
    `blocked / take_profit_adjust_interrupted` 并提醒。
14. **成功也通知**：「已一致」「已按新结构重挂」都写管理通知（`persist_strategy_management_notification_in_session`
    新增 `force` 参数，只有本执行器传）。通知标题【调整止盈】，原因码附中文说明，逐仓位列现有止盈与目标止盈。
    影子模式通知把模式显示为 `shadow`（批次本身的管理执行模式仍是 `live`）。
15. **幂等键**沿用 `management:{batch}:{leg}:…` 前缀，让指令执行契约能看到这些写入。
16. **收敛器匹配逻辑腿时排除 `superseded`**（`trigger_take_profit_convergence_executor._matching_take_profit_protection_legs`），
    否则同价重挂会留下两条同价逻辑腿，收敛器判 `convergence_protection_leg_conflict`。

## 新增原因码（系统 Bot 通知里已有中文说明；oncall 标签表未改）

识别 / 规划：`take_profit_adjust_price_missing`、`take_profit_adjust_all_tiers_crossed`、
`take_profit_adjust_size_below_minimum`、`take_profit_adjust_already_satisfied`、`take_profit_adjust_allocation_invalid`、
`take_profit_adjust_tier_count_ambiguous`、`take_profit_adjust_tier_already_filled`、`take_profit_adjust_size_invalid`、
`take_profit_adjust_position_empty`、`take_profit_adjust_input_invalid`、`take_profit_adjust_disabled`、
`take_profit_adjust_instruction_unavailable`、`superseded_by_newer_take_profit_adjustment`。

执行：`take_profit_adjust_shadow_planned`、`take_profit_adjust_applied`、`take_profit_adjust_deadline_expired`、
`take_profit_adjust_exchange_read_incomplete`、`take_profit_adjust_quote_unavailable`、`take_profit_adjust_position_not_found`、
`take_profit_adjust_protection_unresolved`、`take_profit_adjust_stop_missing`、`take_profit_adjust_price_tick_invalid`、
`take_profit_adjust_stop_replace_failed`、`take_profit_adjust_snapshot_invalid`、`take_profit_adjust_interrupted`、
`take_profit_adjust_execution_error`（另复用 `take_profit_replace_incomplete`、`explicit_stop_adjustment_not_risk_tightening`）。

## 已知限制

- 「第一止盈位 X，触发后上移止损做成本保护」这类同时带不带价保本的消息，保本部分不执行（它是触发后的计划，
  与止损阶梯的规则一致）；带价的止损照第 5 条处理。
- 执行结果不写 `ExecutionEvent`（`adjust_position_tpsl` 会写）；证据在批次 leg 的 `request_json` 和通知里。
- `docs/ARCHITECTURE.md` 第 4.8 节尚未提到这个新生产者；按「只描述生产现状」的约定，应在部署时补。
