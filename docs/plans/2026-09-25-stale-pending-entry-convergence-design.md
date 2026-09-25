# 过期待入场策略收口 · 设计稿

**状态**：`draft`，等用户批准后交实施。
**起因**：2026-09-25 首次分析四分类阶段 1 的首个观察窗。模型对一条今天的 ETH 管理消息
给出 `resolution = exact, lifecycle_id = 909`，而 909 是 2026-08-20 的 `pending_entry`，
早已 `expiry_review_requested`。模型没有猜——**909 是那个群里唯一的 ETH long 候选**，
它在契约允许的集合里选了唯一匹配项。问题在候选集合本身。
**关联**：`docs/plans/2026-09-24-first-pass-classification-contract-design.md` §8 阶段 3
的硬前置条件就是这件事。

---

## 1. 现状（已核实，2026-09-25）

| 事实 | 数据 |
|---|---|
| `pending_entry` 总数 | 66 |
| 超过 7 天 | **42**（17 条超过 30 天，最老 `signal_at = 2026-07-05`） |
| 42 条里有执行绑定的 | **0** —— 交易所没有任何残留挂单，**没有资金风险** |
| 42 条的 `management_action` | 全部 `expiry_review_requested` |
| 通知时间 | 2026-07-05 ～ 2026-09-19，**每条只通知过一次** |
| 有执行绑定的 `pending_entry` | 只有 2 条（09-22、09-24），都正常 |

### 根因一：过期复核问一次就永远等着

`lifecycle_monitor._prepare_pending_expiry_reviews` 发出复核后写
`management_action = expiry_review_requested` 与 `expiry_review_notified_at`，
`expiry_review_next_at` 保持 `NULL`。Telegram 那边三个按钮都在
（`build_pending_entry_expiry_review_reply_markup`：继续等待 / 过期并撤单 / 更新状态），
**机制是完整的**——缺的是「没人按」时的后续：既不重问，也不超时。
漏看一条 Telegram 消息就留下一条永久僵尸，三个月攒了 42 条。

### 根因二：候选集合没有年龄过滤

`strategy_thread_candidates.py:355-358` 的 72 小时判断只是**加分项**
（`reasons.append("recent_active_thread")`），不是筛选。一个月前的 `pending_entry`
照样进候选集合，只是分低——**而当它是该币种该方向唯一的候选时，分低也照样赢**。

并且 `ACTIVE_LIFECYCLE_STATUSES = {"pending_entry", "entered", "holding", "expired"}`
里**含 `expired`**，所以**只把那 42 条标记成过期，并不会把它们移出候选集合**。
收口数据是卫生，年龄过滤才是解决污染的那一刀。

---

## 2. 方案

### A1 · 候选集合加年龄硬过滤（核心，代码）

在 `generate_strategy_thread_candidates` 里，满足**全部**下列条件的 lifecycle
**不进候选集合**：

1. `lifecycle_status` ∈ `{"pending_entry", "expired"}`；
2. `signal_at` 早于 `当前消息 posted_at - 72 小时`；
3. **没有执行绑定**（`execution_binding_id IS NULL`），并且没有任何未了结的交易所腿。

**三条必须同时成立**，每一条都在挡一种误伤：

- 条件 1 保证**永远不过滤 `entered` / `holding`**。一个月前开的仓仍然是真实持仓，
  年龄不能成为看不见它的理由。
- 条件 3 是最后一道闸：只要交易所那边还有东西，无论多老都必须留在候选集合里——
  有挂单就可能成交，看不见它比看见一条旧的更危险。当前数据里这种组合是 0 条，
  但规则必须先写对。

阈值 **72 小时**，与现有 `recent_active_thread` 加分口径一致，不引入新数字。
写成模块级常量并在 docstring 里说明它与那个加分项同源。

### A2 · 过期复核超时自愈（代码）

`lifecycle_monitor` 增加一条收口路径：复核通知发出后 **7 天**无人答复、
**且无执行绑定** → 自动置为过期，`management_note` 写明是超时自动收口而非人工判定，
并计入每日一条的汇总通知（不是每条一个通知）。

**有执行绑定的一律不自动处理**，继续等人——那要撤交易所挂单，是真实写操作，
不能由超时触发。

### B · 收口现存 42 条（数据，L3）

走既有的 `expiry_expire_cancel` 状态转换，**不用裸 SQL**，让审计、通知、绑定检查
沿用现成路径。42 条都无执行绑定，所以「撤单」那一半是空操作。

L3 要求：操作前备份、`PRAGMA quick_check`、`strategy_lifecycles` 与关联表的前后计数，
并保留受影响的 42 个 id 清单。

**B 单独批准、单独执行**，不与 A 的部署混在一起。

---

## 3. 顺序与风险级别

| 步骤 | 内容 | 级别 |
|---|---|---|
| 1 | A1 + A2 代码与测试，本地完成 | 代码改动 |
| 2 | 部署 A1 + A2，观察窗 | **L2**（改变哪些策略能成为管理目标，属交易语义边界，**须单独批准**） |
| 3 | B 收口 42 条 | **L3**（生产数据修复，**须单独批准**） |

A1 改变的是「哪些策略能被选为管理目标」，直接影响管理链路的落点，按 L2 对待。

---

## 4. 验收

- 全套测试基线 **9623 passed / 4 skipped**，最终候选必须回到这个数或更高。
- A1 必须有**正反两面**的测试：一条 73 小时前、无绑定的 `pending_entry` 被挡在候选集合外；
  一条同样老、**有执行绑定**的必须仍在集合里；一条一个月前的 `entered` 必须仍在集合里。
- A2 必须有测试证明：有执行绑定的超时**不**被自动过期。
- 回放验证：对 `raw_message_id = 19030` 重算候选集合，909 不应再出现。

## 5. 明确不做

- 不改 `ACTIVE_LIFECYCLE_STATUSES` 的成员（`expired` 为什么算活跃另说，本稿不动它）。
- 不动 `entered` / `holding` 的任何判定。
- 不撤任何交易所挂单，不关闭任何自动交易开关。
- 不改首次分析契约（那是另一条线）。
