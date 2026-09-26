# 无声卡死案例 · 给值守（Codex on-call）的备注

日期：2026-09-26
写给：`docs/codex-oncall-status.md` 所属的值守检测线
来源：用户 2026-09-25 让我查一条日志噪音，顺着挖出来的。用户原话是
「agent 注意不到，系统也不说，人工也看不到，我正奇怪为什么陈哥一直没有持仓」。

**这份备注不改任何代码，只交一个真实案例和三处检测缺口。**

---

## 1. 案例：陈哥 BTC 多单线被封 11 天，全程无人知道

| 时间 | 发生了什么 |
|---|---|
| 2026-09-15 10:33 / 14:47 | 陈哥群（`-1002337721508`，auto_trade）删掉两条 BTC 多单消息，生成删除退出 310 / 311 |
| 同日 | 两条退出找不到确切 lifecycle（`exact_lifecycle_missing`），落进 `recovery_required` |
| 此后 | **worker 永不再认领这个状态**；超时清扫器每 5 秒看一次，但这两条退出没有 `execution_binding_id`、没有已知 `pos_id`，交易所缺席证明直接拒绝（`exit_has_no_known_position`），所以永远不会释放 |
| 09-15 → 09-25 | `source_execution_barrier` 的规则是「同群 + 同标的 + 同方向、存在任何 `state != 'succeeded'` 的删除退出 → 新消息 hold」。于是陈哥 **BTC/long** 这条 lane 被封死，**11 条消息**被 hold 到超时、标成 `deferred_expired` 后永不恢复 |

被作废的 11 条里有 **4 条入场策略**（09-18 76000-67300、09-20 80400、09-24 83300-83500、09-25 83000-83300）
和多条止损/保本指令（09-15 止损下移 74800、09-24 止损统一改 83300 等）。

执行侧对得上：09-15 之后陈哥群只开过 **BTC 空单**（09-18、09-21、09-23）和 **ETH 多单**（09-24），
**一笔 BTC 多单都没有**——方向不同不在同一 lane，所以空单毫发无伤。这正是用户看到的
「陈哥一直没有持仓」。

交易所侧已核对（2026-09-26 09:46 快照）：账户当前只有米娅群那一笔 BTC 多单（binding 383），
**陈哥零持仓、零挂单**。也就是说这两条退出封着的是幽灵——没有任何东西需要撤。

## 2. 三层都没发现，各有各的原因

### 2.1 值守 agent：没有规则能看见它

`oncall_detector.py` 的水位线只读五张表：

```
message_instruction_items / strategy_management_batches / message_processing_jobs
position_protection_ledger / recognition_decisions
```

`source_message_deletion_exits` 不在其中。唯一有机会撞上的是 D3（`recognition_decisions`），
但它的判据是 `LOSSY_RECOGNITION_REASONS`：

```python
{"target_not_verifiable", "mimo_authoritative_failed",
 "authoritative_gap_recovery_expired", "lifecycle_apply_failed",
 "management_recognition_unresolved"}
```

**`waiting_source_deletion_exit` 和 `deferred_expired` 都不在里面**，所以 D3 把
「被封 lane 挡下、超时作废」判成「这是一个决定，不是损失」，一个案子也没开。
这条判据的 docstring 写得很清楚：漏掉的那些是「用户自己的开关 / 消息没要求什么 / 没点名目标」——
但 `deferred_expired` 不属于这三种，它是**系统自己把一条真实策略吃掉了**。

### 2.2 系统：喊了 35 万次，等于没喊

`runtime_incidents` 按指纹 coalesce：同一条告警第一次落库时通知一次，之后只累加 `repeat_count`。

- `source_deletion_exit_stuck` 两行，`repeat_count` 合计 **356933**，通知时间停在 **2026-09-15**；
- 之后 11 天，它每 5 秒重喊一次，全部静默累加。

再加一个 bug 让它更看不见：`release_reason` 不在 `runtime_incidents._SUMMARY_FIELDS` 白名单里，
所以详细摘要恒被拒、退回最小摘要，**被拒掉的恰好是「为什么没释放」这唯一的答案**
（`exit_has_no_known_position`）。日志里每天因此多出约 6.9 万行 WARNING。

### 2.3 人工：页面上看不出「这条 lane 被封着」

被 hold 的消息不会出现在任何「待处理」清单里，`deferred_expired` 也不产生通知。
网页能看到的是「陈哥没有持仓」——而那正是结果，不是原因。

## 3. 同一形状的另外两例（大漂亮社区）

| incident | 对象 | 冻结起点 | repeat_count | 现在还有害吗 |
|---|---|---|---|---|
| `revision_batch_too_stale_to_resume` | `strategy_revision_batch` 9 | 2026-09-11 | 65848 | 目标 lifecycle 1136 已 exited、binding 347 已 closed → 只剩噪音 |
| 同上 | `strategy_revision_batch` 10 | 2026-09-23 | 9023 | 目标 lifecycle 1278 已 expired、binding 369 已 closed → 只剩噪音 |
| `revision_cancel_outcome_unresolved` | 同上两个 batch | 09-11 / 09-23 | 2071 | 同上 |

形状完全一样：卡在 `recovery_required` → 每轮扫描重喊 → 第一次之后再没人被通知 → 永不收口。
差别只在于这两个的目标仓位已经自己了结了，所以没造成损失。**下一次不一定这么走运。**

已核对的反例（不用担心的）：91 条 `unbound` 删除退出的 `raw_message_id` 全是 NULL，
进不了 barrier 的 join，**不封任何 lane**。目前被封的 lane 全网只有陈哥 BTC/long 这一条。

## 4. 建议值守增加的判据（草案，供那条线自己定夺）

1. **D6a｜被封的 lane**：`source_message_deletion_exits` 里 `state NOT IN ('succeeded')`
   且 `raw_message_id IS NOT NULL` 的行，存在超过 N 小时就建案；案文要带上
   「这条 lane 是哪个群 + 哪个币 + 哪个方向」和「期间已经被作废了几条消息」。
   这比读 `runtime_incidents` 更直接，因为它问的是「现在还有没有 lane 被封」，
   而不是「有没有人喊过」。
2. **D6b｜被系统吃掉的消息**：把 `deferred_expired` 加进 D3 的损失判据。
   一条 KOL 策略因为系统内部状态而永不执行，是 D3 存在的理由本身。
3. **D6c｜喊了没人听**：`runtime_incidents` 里 `status='pending'`、`severity IN ('high','critical')`、
   `last_occurred_at` 仍在推进、但 `notified_at` 已经超过 N 天的行——这一条通用，
   能同时接住本案和上面两个 revision batch，也能接住下一个还没出现的形状。
   注意：判据要用「`last_occurred_at` 仍在推进」而不是 `repeat_count` 大小，
   前者说的是「现在还在发生」，后者只说「以前发生过很多次」。

## 5. 与之配套、但不属于值守的三件事（另行处理）

1. `_SUMMARY_FIELDS` 补 `release_reason`（一行），否则这类告警永远说不出原因；
2. 给重复 capture 加节流，5 秒一次是把库和日志当草稿纸；
3. `recovery_required` + 无已知持仓 id 的删除退出**没有任何自动出路**，
   需要一条判定：既然我们手里没有任何「要撤的东西」的凭据，交易所也证实什么都没有，
   就该允许收口，而不是永远封着 lane。这三件事的处置等用户定。
