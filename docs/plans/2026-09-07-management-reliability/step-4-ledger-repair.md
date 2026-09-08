# A-4：账本修复（L3，生产数据修复，需用户单独批准）

状态文件：`docs/management-reliability-status.md`。先领取；证据区必须有本步批准记录。分支 `mgmt/step-4-ledger-repair`，工作树 `.worktrees/mgmt-step-4`。

## 要修的行（服务器笔记第五、六、七轮）

| 对象 | 现状 | 交易所事实 | 目标终态 |
|---|---|---|---|
| `execution_bindings` 337 | active，`last_exchange_status=position_attribution_evidence_unavailable` | 两条 entry leg 的仓位 09-04 13:07:38Z 被止损全平 | closed，终态原因 `stop_loss_confirmed_by_position_history` |
| `execution_order_legs` 579 / 580 | active | 同上 | closed |
| `strategy_lifecycles` 1074 | entered | 同上 | exited（reason stop_loss） |
| `strategy_management_batches` 158 | recovery_required / close_final_preflight_failed | 仓位已不存在 | resolved（reason `position_closed_before_management`） |
| `strategy_management_notifications` 94 | pending | — | 标记 superseded |
| `position_take_profit_orders` 195 | active | TP1 09-04 08:34:43Z 已触发成交 | completed |
| `position_take_profit_orders` 196 / 197、`position_protection_ledger` 止损行（leg 579） | active | 仓位已平，单据已消失 | cancelled / closed |
| `strategy_lifecycles` 1081（峰哥幽灵） | entered，`execution_binding_id` NULL，来源 raw 14843 入场失败 | 无仓位 | cancelled（或项目等价终态） |
| leg 580 的止盈 852/853/854 | `protection_recovery_pending`，从未建立 | 仓位已平 | cancelled |
| `source_message_deletion_exits` 109（飞扬 BTC/short，08-14）、128（所长 ETH/short，08-19）、201（陈哥 BTC/long，08-29）、209（三马哥 ETH/long，09-02）、231（飞扬 ZEC/short，09-04） | `recovery_required`（终态，永不再认领），4 条 `frozen_ledger_identity_unverified`、1 条 `exact_lifecycle_missing`；barrier 以 `state != succeeded` 判 hold，这 5 条把各自「群 + symbol/side」泳道永久钉死，33 条积压里 28 条落在这些泳道上 | 逐条用交易所 position-history / 订单历史核实该退出对应的仓位与订单是否已经不存在 | 事实为“已不存在”的改 `succeeded`（reason `repair_2026_09_08_position_gone`）；事实不清的保持并记录，交人工 |
| `trigger_take_profit_convergences` 230（binding 341 / leg 586 / pos 1001125163581280）与 231（binding 342 / leg 588 / pos 1001125164628529） | conflicted / `convergence_exact_leg_not_verified`，由瞬时条件写成终态（1b/1c 证据） | 仓位活跃、leg verified、止损在、无分档止盈 | 若仓位仍活跃：复位为可重试（精确改前/改后、影响行数证明、回滚脚本），让修正后的判据重算；若仓位已平：closed |

## 方法

1. 只读复核：对上表每一行，重新用交易所 `position-history` / `trigger-orders-history` / `fills` 直读确认事实，
   把原始 JSON 存进证据目录；任何一行事实与表中不符就停下报告，不修那一行。
2. 优先用项目已有的修复命令（`cli.py` 里 `recover-management-history`、`historical_state_repair`、
   `terminal_entry_cleanup` 等）并带 `--apply`；只有没有现成命令的行才写一次性脚本，放进
   `src/telegram_kol_research/one_off/`，并有测试。
3. L3 流程：生产库 `sqlite3 .backup` → 在副本上演练 → `PRAGMA quick_check` → 记录五张关键表
   （`execution_bindings`、`position_protection_ledger`、`trigger_protection_intents`、`position_take_profit_orders`、
   `raw_messages`）与本步涉及表的 before/after 行数与被改行的 before/after 字段原文 → 再对生产执行。
4. 每一行的修改都写 `position_attribution_audits`（或项目等价审计表）一条记录，注明 `repair_2026_09_07` 与证据路径。

## 禁止

- 不改上表之外的任何行。不删行。
- 不对交易所做任何写入。
- 不用 `git add -A`。

## 验证

- 副本演练与生产执行的 quick_check 均 ok；被改行的 after 值逐字段与目标终态一致。
- 修复后 `lifecycle_monitor` 不再每分钟打印 binding 337 的 "Skipping simulated lifecycle exit"。
- 修复后大镖客群与峰哥群各只剩一个活跃策略（1097 / 1095 或当前实际），无 binding 为 NULL 的 entered lifecycle。
- 无需部署代码（若 one_off 脚本入库则部署以保持一致，L0）。

## 完成条件

更新状态文件到 `current_step: 3d`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-3d-entry-admission-reconciler-disabled.md`；
`send_message` 给 `brain_session_id`：备份路径、每一行 before/after、审计记录 id、复核时发现的任何不符。
