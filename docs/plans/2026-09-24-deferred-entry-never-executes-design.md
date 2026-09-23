# 被相邻消息推迟的入场，七成整单丢失——分析与修复设计

日期：2026-09-24　状态：**已批准（2026-09-24），阶段 1+2 实施中**　线上版本：`e0c29263`（本设计的回退点）

这是 2026-09-19 记为「缺陷 2」、2026-09-23 稿第 3.5 节列为范围外的那个专题。查下来它比记的严重得多：
不是三次个案，是**一条入场策略只要被相邻消息推迟，就有约七成概率整单丢失**，而且现场留下的
失败原因指向一个完全无关的方向。

## 1. 数字

`entry_assembly_attempts` 全表 29 行（2026-08-17 ~ 2026-09-22），每一行都是一条被相邻消息推迟的入场策略：

| attempt 状态 | 指令项结局 | 条数 | 含义 |
|---|---|---|---|
| woken | submitted / succeeded | **6** | 真的下单了 |
| expired | succeeded | **2** | 8 月旧路径，下单了 |
| woken | failed | 8 | 被置为「已唤醒」，却从未执行 |
| pending | failed | 9 | 从未被唤醒，指令项被杀 |
| expired | failed | 4 | 到期作废 |

**29 次里 8 次下单，21 次丢失。** 丢失的每一次都是一条完整策略：识别对了、准入算对了、
风险预算算好了，然后无声无息地没有了。

决定性的对照：`entry_assembly_wakeup_executions` 表里只有 **5 条**记录，全部 `succeeded`，
对应 attempt 12、13、17、22、26 —— 正是那几条下了单的。**唤醒执行路径真跑过的，100% 成功；
剩下的 24 次，它一次都没跑。**

## 2. 机制

一条被推迟的入场有两个「可以继续了」的判定者，它们**共用 `attempt.status` 这一个字段**，
而其中只有一个有能力执行。

**唤醒路径（有执行能力）**：消息 B 处理完 → `_run_entry_assembly_wakeups`
（`authoritative_recognition.py:2347`）→ `claim_ready_entry_assembly_wakeups`
（`entry_assembly_admission.py:705`）→ 认领条件是 **`status == "pending"`** 且 B 在这条 attempt 的
blockers 里 → `run_claimed_entry_assembly_wakeup` 持 owner/registry 真正下单，成功后才写 `woken`。

**对账路径（无执行能力）**：`reconcile_due_entry_admissions`（`entry_admission_reconciler.py:48`），
由任意消息的处理触发（`authoritative_recognition.py:1905`，**位置在唤醒之前**），
挑 `status == "pending"` 且 `updated_at <= now - 5s` 的 attempt 重新评估准入。评估通过时它做两件事
（`:198-213`）：清掉指令项的 visibility 延迟，然后 **`status = "woken"`**。

它按设计不下单（A-3d，注释写着 "never invokes any exchange writer"），指望清掉 visibility 之后
「后面自然会有人执行」。**没有人。** 清掉之后：

- 唤醒认领器看到 `status != "pending"`，直接跳过 —— 而且再也不会回头，因为 `woken` 没有回路；
- 常规指令项认领器 `claim_next_message_instruction_item`（`message_instruction_items.py:157`）
  是**按 raw_message_id 驱动**的，只在「那条策略消息自己又被处理一次」时才会捡它，没有任何
  轮询会去扫全库的 pending 项；
- 专门的重试认领器 `claim_next_visibility_retry_instruction_item` 有两道门把它挡在外面：
  要求 `visibility_next_attempt_at IS NOT NULL`（对账器刚把它清成 NULL），
  以及 `instruction_kind == "management"`（这是入场项）。

于是指令项停在 `pending`，没有任何认领者，直到——

## 3. 第二层：杀死它的人拿错了凶器

`claim_next_visibility_retry_instruction_item` 函数开头有一段批量过期 UPDATE
（`message_instruction_items.py:400-424`）：

```python
.where(
    retired_at IS NULL,
    visibility_first_failed_at IS NOT NULL,
    visibility_first_failed_at <= now - VISIBILITY_RETRY_DEADLINE,   # 6 小时
    status == "pending" OR (status == "executing" AND 陈旧),
)
.values(status="failed", error_json={"reason": "target_strategy_binding_visibility_retry_expired"}, ...)
```

**它没有 `instruction_kind == "management"` 过滤，而紧随其后的认领 SELECT（`:441`）有。**
过期器管所有种类，重试器只管管理类 —— 这个不对称本身就是缺陷：入场项永远不会被这个循环重试，
却一定会被它在 6 小时后杀掉。

被杀时打上的原因码 `target_strategy_binding_visibility_retry_expired` 属于管理路径
（「改止损时找不到目标持仓」），on-call 告警把它翻成「**一直没找到对应的持仓记录，重试超时**」
（`oncall_alerts.py:107`）。对一条从未下过单的入场来说，这句话每个字都是错的，
它把每一次事后追查都引向持仓可见性，而真正的原因是没有人去执行。

陈哥 10672 就是这样：01:17:54 推迟，01:18:06 被对账器置 `woken`，07:18:07 被上面那段 UPDATE 杀掉，
报「找不到持仓记录」。11 小时后，陈哥发「市价半仓入场」确认这一单，系统又因为另一个缺陷
以那条消息为键开了 29 张（见 `2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md`）。
**这次事故的第一张多米诺骨牌就在这里。**

## 4. 第三层：`woken` 同时表示两件相反的事

执行成功后 `run_claimed_entry_assembly_wakeup` 写 `woken`（`entry_assembly_wakeup_executions.py:166`），
对账器抢跑后也写 `woken`。两者还都把 `wake_claim_token` 清空，所以从 attempt 行上**无法区分
「已经下单了」和「永远不会下单了」**。

2026-09-19 那次排查就是被这个字段骗的：当时验收只看了 `woken` 和 `released == 1`，判定修复 A 生效，
实际上那一单从未下单。**任何验收都必须落在 `execution_events` 的下单记录上，不能看 attempt 状态。**

## 5. 修复设计

三个阶段，阶段之间由 Claude 审阅后再派下一阶段。核心原则不变：**执行只发生在持有
execution_owner / execution_registry 的唤醒路径上**，对账器继续不碰交易所。

### 阶段 1：给「已准入」的 attempt 一个真正的执行入口

**1.1 对账器不再宣布唤醒。** `entry_admission_reconciler.py:198-213` 的 release 分支改为：
清 visibility 延迟之后写 `status = "ready"`（新状态，加进 `ck_entry_assembly_attempts_status` 约束），
并把 `blocking_raw_message_ids_json` 清成 `[]`。`woken` 从此只由成功的执行写。

**1.2 唤醒认领器接受第二个触发源。** `claim_ready_entry_assembly_wakeups`
（`entry_assembly_admission.py:705`）现在只认「`status == "pending"` 且 `completed_raw_message_id`
在 blockers 里」。增加一条并列路径：`status == "ready"` 的 attempt 无条件可认领，不要求
`completed_raw_message_id`（调用方可以不传）。认领后的动作与现有路径完全一致——写 claim_token、
交给 `run_claimed_entry_assembly_wakeup`、一次一条。

**1.3 worker 定时调用它。** 在 worker 已有的、持 owner/registry 的循环里（与
`_run_entry_assembly_wakeups` 同一处所有权来源）增加一次不带 `completed_raw_message_id` 的认领调用。
频率与现有对账轮一致即可；一次只执行一条，沿用现有的 lease 与 registry 准入。

这三条合起来就是把「对账器判定可以了」和「执行」之间那段断掉的路接上，而不是让对账器自己去下单。

**1.4 对账器继续看得见它们。** 对账器的选择条件 `EntryAssemblyAttempt.status == "pending"`
改为 `IN ("pending", "ready")`，这样 deadline 到期时它仍然能作废并发出**入场自己的**告警
（`_report_entry_admission_expired`），不再把这件事漏给管理过期器。

### 阶段 2：不让管理过期器碰入场项

**2.1** `message_instruction_items.py:400` 那段批量过期 UPDATE 加上
`instruction_kind == "management"`，与它下面的认领 SELECT 对齐。

**2.2** 入场项的到期从此只由 1.4 的对账器 deadline 分支负责，原因码用入场自己的
`entry_admission_deadline_expired`（该码已存在，attempt 23/24 用的就是它）。

**2.3** 顺带修 `oncall_alerts.py:107` 的文案对应关系：管理项才说「找不到对应的持仓记录」。

### 阶段 3：让状态说实话

**3.1** attempt 增加终态 `executed`，由 `run_claimed_entry_assembly_wakeup` 成功时写；
`woken` 保留给历史行，新代码不再产生。或者更简单：保留 `woken`，但要求任何验收、告警、
运维面板都改读 `entry_assembly_wakeup_executions` 是否有对应行。**两者选一，不要都做。**
我建议前者：让状态字段自己说实话，比要求所有读者都记得绕开它更可靠。

**3.1 补充理由（2026-09-24 实施阶段 1+2 时发现）。** `_persist_attempt`
（`entry_assembly_admission.py:559`）在 `existing.status in {"shadow","pending","woken"}` 时把 attempt 重置回
`pending`——**`woken` 在这个集合里**，也就是一条已经下过单的 attempt 可以被一次重新识别重新武装，
再执行一次。本次改动没有让它更容易发生（对账器只选 `pending`/`ready`，`ready` 又在评估前短路），
它一直只能由消息驱动的重新识别触发，和从前一样。但这是一条真实的重复下单路径，
**3.1 选「拆出 `executed` 终态」会顺带修掉它**（`executed` 不在那个集合里）；选另一个方案则修不掉，
还得单独把 `woken` 从那个集合里摘出来。这是我建议选前者的第二个理由。

**3.2** 一条 attempt 进入 `ready` 后超过 N 分钟（建议 10）仍未产生
`entry_assembly_wakeup_executions` 行 → runtime incident 告警一次。这是这套机制唯一的活检：
上面所有改动如果哪天又被绕开，这条告警会说话，而不是六小时后一句「找不到持仓记录」。

### 不做

- 不让对账器直接下单（会破坏 A-3d 的执行边界）。
- 不缩短也不延长 `ENTRY_ADMISSION_RECHECK_DELAY`（5 秒）。竞速不是根因，断路才是；
  调时间只会改变两条路谁先到，不会让丢失的那条被执行。
- 不回填 21 条历史丢失的策略，它们早已过期。

## 6. 测试要求（先写测试）

1. **核心**：入场被推迟 → 对账器判定准入通过 → 定时认领 → `execution_events` 出现下单事件。
   验收断言落在下单记录上，不是 attempt 状态。
2. 形状回放三条真实样本：attempt 29（陈哥 10672）、25（峰哥 9343）、20（raw 16913），
   断言每条都产生恰好一次下单，且 `entry_assembly_wakeup_executions` 有对应行。
3. 原有触发源不回归：blocker 消息完成 → 唤醒照旧执行（attempt 26 的形状）。
4. 竞速两个方向都试：对账器先到（attempt 变 `ready`，随后定时认领执行）、唤醒先到
   （attempt 直接 `pending → 执行`），两者都必须恰好下单一次，不能重复。
5. `ready` 的 attempt 到了 deadline → 对账器作废它，原因码是入场自己的，告警发出一次。
6. 一个带 `visibility_first_failed_at`、超过 6 小时的 **entry** 指令项不再被管理过期器改写；
   同样条件的 **management** 项仍然被改写（防止 2.1 改过头）。
7. 3.2 的告警：`ready` 超过 10 分钟无执行行 → 告警一次，再跑一轮不重复。

## 7. 部署与验证

- 阶段 1+2 一起部署（2.1 单独上会让入场项永不到期）；阶段 3 单独一次。回退点 `e0c29263`。
- 部署后 L2 观察窗 30 分钟，自然样本上限 24 小时。
- 首个自然样本好等：29 次推迟分布在 5 周里，平均每周 5～6 次。核对两件事：
  该 attempt 有 `entry_assembly_wakeup_executions` 行；`execution_events` 有对应下单。
- 观察期内可以用一条只读查询盯住回归：`status='ready'` 且超过 10 分钟没有执行行的 attempt 应恒为 0。

## 8. 与其它专题的关系

- `2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md`（阶段 1+2 已于 09-24 部署）
  修的是「不该开的开了」；本稿修的是「该开的没开」。陈哥 09-22/09-23 那次事故两个缺陷各出一半力。
- 本稿阶段 1.4 让入场到期告警回到入场自己的通道，与那份稿子阶段 3.2 的「KOL 确认入场、我们没有持仓」
  告警是互补的两个信号：一个说「我们没执行」，一个说「KOL 以为我们执行了」。
