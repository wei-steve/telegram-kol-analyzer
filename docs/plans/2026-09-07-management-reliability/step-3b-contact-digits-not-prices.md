# A-3b：联系方式里的数字串不得被解析为价格（L2）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-3b-contact-digits`，工作树 `.worktrees/mgmt-step-3b`。

## 问题（2026-09-08 实例，A-2 只读取证）

raw 15402（大镖客 11分组，06:25:30Z）：

```
大镖客·Andy
第一止盈位已过，注意锁定利润，及时移动止损！
@Tarderfengge QQ:158241758
```

识别把签名里的 QQ 号 `158241758` 解析成显式止损价（`stop_mode=explicit_price, stop_price=158241758`），
止损价闸门（2026-09-05 的 d1e3d858）正确地以 `management_stop_action_conflict` 拒绝，于是 KOL 要求的
"减仓 50% + 移动止损"一件都没执行，且（step 2 之前）无人收到告警。同一签名在 2026-09-05 的 raw 15013
也把 QQ 号当成过 BTC 止损价（见 `docs/2026-09-05-codex-handover-closeout.md` 第二节）。大镖客每条消息都带这个签名，
**这是会持续复发的缺陷**。

## 任务

1. 在管理指令与入场指令的价格抽取之前，统一做一次"联系方式脱除"：
   `QQ[:：]?\s*\d+`、`微信[:：]?\s*\S+`、`VX[:：]?\s*\S+`、`@\S+`、`电话/手机[:：]?\s*\d+`、连续 ≥9 位的纯数字串
   （加密货币价格不会有 9 位整数；若将来出现，以该合约的价格量级校验兜底）。脱除后的文本才进入价格解析。
   脱除只作用于价格/数量抽取的输入，不改保存的原文，不影响识别的其他字段。
2. 对 MiMo 返回的 `stop_price` / `take_profit` / `entry_price` 等显式价格做量级校验：与该合约最近成交价或 mark 价
   相差超过 10 倍的价格视为"未提供"，并记一条 `management_price_implausible` 事件（进 ALWAYS_NOTIFIED），
   不再把它当作显式价格送进闸门。闸门本身不改。
3. 回归测试：raw 15402 与 raw 15013 的原文作为固定用例，断言 stop_price 不再是 QQ 号；带正常止损价的消息不受影响；
   `@handle` 出现在价格前后都不影响价格抽取。
4. 只读复核：部署后用只读方式对最近 30 天大镖客群的管理消息重放价格抽取，统计修复前后 `stop_price` 变化的条数，
   写进证据。

## 禁止

- 不改止损价闸门的判定。不改识别模型的提示词（那是 AI 提示词注册表的事，若确需改提示词，记为遗留）。
- 不用 `git add -A`。

## 验证（L2）

- focused 与全量 0 failed。部署 tg-deploy，记录回滚参考。
- 观察：按 L2 用后台监视器等一个 ≥5 条真实消息的 30 分钟健康窗口；若窗内出现大镖客的管理消息，逐条确认
  `stop_price` 不再是签名数字。

## 完成条件

更新状态文件到 `current_step: 4`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-4-ledger-repair.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚参考、回归用例结论、30 天重放统计。
