# A-3d 任务 1：`instruction_execution_contract_mode` 只读评估

2026-09-08，会话 `local_22ee72a5-d88c-4ba2-9b17-366585562d10`。**全程只读：未改生产数据、未改代码、未部署。**
生产读数取自 `data/research.db` 的 `mode=ro` 连接与源码直读。

## 0. 一句话结论

**超时那一半在 shadow 下跑，重试那一半不跑。** 同一个机制的两条腿被两个不同的门控条件分开了：
`instruction_execution_reconciliation` 的门是 `mode == "disabled"` 才返回（所以 shadow 下它照常把
到期的 deferred 合约判成 `expired`），而 `entry_admission_reconciler` 的门是 `mode != "live"` 就返回
（所以 shadow 下它一次都没跑过）。结果是生产**只会让被推迟的入场超时，不会让它重试**——过去 30 天
7 条自动交易群的入场就是这样静默失效的。

## 1. 三档各自门控什么（逐处）

`instruction_execution_contract_mode` 在生产代码里只有 5 个直接读取点，但它会被解析成"每个指令项的
生效档位"再往下传，真正的分支在下游。下表按"读取点 → shadow 与 live 的差别"逐条列出。

### 1.1 直接读取点

| # | 位置 | disabled | shadow | live |
|---|---|---|---|---|
| 1 | `trading_settings.py:89,514` | 定义与解析。`Literal["disabled","shadow","live"]`，**代码默认 `disabled`**；`_rollout_mode`（748 行）对非三档值抛错，不静默降级 | 同 | 同 |
| 2 | `instruction_execution_projection.py:34` `project_instruction_execution_contracts` | 直接 `return ()`，不建任何合约 | 为水位线之上的 mimo 权威指令项建 `InstructionExecutionContract` | **与 shadow 完全相同** |
| 3 | `instruction_execution_projection.py:88` `instruction_execution_mode_for_item` | 返回 `"disabled"` | 水位线之下也返回 `"disabled"`，之上返回 `"shadow"` | 之上返回 `"live"` |
| 4 | `system_operator_bot.py:2705` `_run_operator_maintenance_cycle` | 不构造 Deepcoin 客户端，`execution_reconciliation_client=None` | **构造客户端**并传进维护 tick | 同 shadow |
| 5 | `runtime_incident_snapshot.py:95,236` → `_execution_contract_is_future:318` | `mode not in {shadow,live}` → 恒 False，不投影任何"执行契约矛盾"观测 | 按水位线投影矛盾 | **与 shadow 完全相同** |

### 1.2 下游按"每项生效档位"分支的点

| # | 位置 | disabled | shadow | live |
|---|---|---|---|---|
| 6 | `entry_admission_reconciler.py:47` `reconcile_due_entry_admissions` | `return` 空结果 | **`return` 空结果** | 唯一会真正执行的档：到点重取准入、释放或过期 |
| 7 | `instruction_execution_reconciliation.py:103` `reconcile_instruction_execution_contracts` | `return` | **照常执行**：到期 deferred → `expired / execution_contract_deadline_elapsed`（152 行）；`submitting`/`submit_unknown` 走交易所**只读**快照回读 | 与 shadow 相同（该模块内 `mode` 只用于 disabled 判断，别处一律不分档） |
| 8 | `instruction_execution_management_adapter.py:817,896` | `return None`，不建管理/改单合约 | 建合约 | 与 shadow 相同 |
| 9 | `instruction_execution_management_adapter.py:1024` 耐久镜像收敛 | 不执行（外层已 disabled） | `mode != "live": continue`——**不收敛** | 把 `verified`/`failed` 的合约状态写回指令项终态 |
| 10 | `auto_trade_execution.py:341` + 825/1400/1450 三处 | 传 `disabled`，合约投影异常被吞 | 合约投影异常**fail-open**（记录失败但继续） | 同样的三处改为 **`raise`，fail-closed** |
| 11 | `message_instruction_items.py:520` `finish_message_instruction_item` | 无守卫 | 无守卫 | 终态写入额外加一道**合约镜像 CAS 守卫**（合约 id/state/version 必须匹配才允许改指令项） |
| 12 | `strategy_management_worker.py:234` | 传 `disabled` | 传 `shadow`，只影响 10/11 两类行为 | 传 `live`，同上 |

### 1.3 从这张表能读出的三件事

1. **shadow 与 live 在"建不建合约、投不投影矛盾"上完全一样**（#2、#5、#7、#8）。这两档的差别只有四处：
   #6（准入重试）、#9（耐久镜像收敛）、#10（fail-open vs fail-closed）、#11（终态 CAS 守卫）。
2. **入场提交本身不受这个开关门控**。`_auto_process_single_message_trade_signal` 在任何一档都会执行，
   `execution_contract_mode` 只作为参数往下传，用于上面那四处。**翻到 live 不会"打开"任何一条新的
   交易所写入路径**，它只会让既有路径在合约簿记失败时从 fail-open 变成 fail-closed，并给终态写入加一道守卫。
3. **#6 与 #7 是同一个机制的两条腿，却用了不同的门。** 这就是本步要处理的缺陷。

## 2. 生产当前值、水位线与历史

```
key = global，updated_at = 2026-09-04 20:33:43.655259
instruction_execution_contract_mode              = shadow
instruction_execution_entry_after_item_id        = 483
instruction_execution_management_after_item_id   = 9223372036854775807   ← maxint
```

- **管理侧被水位线整个关掉。** `9223372036854775807` 是 int64 上限，任何指令项 id 都不可能大于它，
  所以 `instruction_execution_mode_for_item` 对**每一条管理指令**都返回 `"disabled"`。
  换句话说：**即便把开关翻成 live，管理侧也一行都不会变**，受影响的只有 id > 483 的 entry 指令项。
- **为什么停在 shadow。** 原方案 `docs/archive/plans/2026-08-10-unified-execution-truth.md` 把上线拆成两个任务：
  - 任务 18「Promote future-only shadow observation」：只改 `contract_mode=shadow` + 一个新的 entry 水位线，
    并明确要求「Keep the management watermark inactive」——生产现在正停在这一步的产物上（水位线 483，
    管理水位线设成 maxint 就是"inactive"的实现方式）。
  - 任务 19「Promote entry enforcement, then management enforcement separately」：翻 entry 到 live
    需要「reviewed shadow evidence with zero unexplained divergence, healthy monitoring, exact rollback,
    and a fresh deterministic `live_promotion` preflight」+ **explicit approval**；管理侧还要另一次
    独立审批与独立观察窗。
  - **任务 19 从未执行**：仓库里没有任何 shadow 观察窗的评审记录、没有 `live_promotion` preflight 结果、
    也没有用户对 entry live 的批准记录。`docs/plans/2026-09-06-post-migration-cleanup/step-5-flag-inventory.md:41`
    在 09-06 盘点时把这个开关的生产值记为 `unknown`、处置建议 `ask-owner`——**本评估是第一次把生产实际值读出来**。
- 所以"为何停在 shadow"的答案是：**没有人做任务 19，不是有人评估后决定不做**。没有找到任何一条把它
  退回或压住的决定记录。

## 3. 翻到 live 会开启什么，既有测试覆盖什么

### 3.1 会变的四件事（都在 id > 483 的 entry 指令项上）

1. **#6 准入重试开始运行**：到点重取准入，条件已解除的释放（等价于把 `visibility_next_attempt_at`
   清空、attempt 置 `woken`），条件仍不满足的保持 deferred，过 deadline 的判 `expired`。
2. **#9 耐久镜像收敛开始运行**：`verified`/`failed` 的合约把指令项写成对应终态。
3. **#10 fail-open → fail-closed**：三处合约投影（deferred 合约、安全拒绝、几何拒绝）一旦抛异常，
   整条入场处理就抛出去，而不是记一条日志继续。**这是唯一一处"翻 live 会让原本能走完的路径变成失败"的地方**，
   也是翻档真正的风险面。
4. **#11 终态写入加 CAS 守卫**：合约状态与指令项终态必须一致才允许写，不一致就写不进（rowcount=0）。

### 3.2 会不会产生新的交易所写入

**不会产生新种类的写入，但会让既有写入路径被更频繁地走到。** 具体说：

- `reconcile_due_entry_admissions` 的 docstring 就是 “Release or expire due attempts **without invoking
  any exchange writer**”，代码核对属实：它只写 `entry_assembly_attempts`、`message_instruction_items`、
  `instruction_execution_contracts` 三张表，唯一的外部调用是
  `assess_entry_assembly_admission(mode="live")`——那个函数只读库、只写一行 `EntryAssemblyAttempt`，
  零交易所调用。
- 但它**释放**之后，指令项变成立即可认领，随后 `auto_trade_execution` 会真的下单。
- **关键对照：这条"释放→下单"的链在 shadow 下今天已经在跑。** 事件驱动的那条唤醒路径
  `authoritative_recognition._run_entry_assembly_wakeups`（2070 行）→ `claim_ready_entry_assembly_wakeups`
  → `run_claimed_entry_assembly_wakeup` **完全不看这个开关**。生产数据可证：attempt 9 / 12 / 13
  都是 `woken` → 指令项 `submitted` → 合约 `verified / entry_submission_verified`，全部发生在 shadow 期间。
- 所以准确的说法是：**翻 live（或按选项 b 把 #6 放到 shadow 下跑）不会打开一条新的写入路径，
  只会让一条今天已经在写的路径多一个触发时机（定时补偿，而不只是事件驱动）。**

### 3.3 既有测试覆盖

`tests/test_entry_admission_reconciler.py` 共 **14 个测试**，其中 12 个显式传 `execution_contract_mode="live"`，
覆盖：只释放精确的那一项且零交易所调用、水位线之下不释放、历史 pending 不饿死未来项、
失败的未来项不阻塞下一项、畸形 deferred 被过期而不是占位、未到点不动、已过 deadline fail-closed、
`submit_unknown` 合约被排除、重复 tick 幂等、历史 succeeded 不被重放、blocked 重取判过期、
合约状态竞态不撕裂指令项与 attempt、释放 CAS 失败保持 pending 等下一轮。

**两个覆盖缺口：**
- `test_disabled_mode_never_releases_historical_deferred_item` 用的是**默认档 `disabled`**，
  **没有任何一个测试断言 `shadow` 下该函数是空操作**。也就是说 shadow 的惰性只是 `!= "live"` 的
  副产物，没有被测试锁定——**改成让 shadow 也跑，不需要削弱或删除任何现有测试**，只需新增 shadow 用例。
- 没有测试覆盖 §3.1 第 3 条（#10 的 fail-open → fail-closed 翻转）在 live 下对整条入场处理的影响。
  这是翻档最需要证据的一处，恰恰没有测试。

`tests/test_system_operator_bot.py` 与 `tests/test_authoritative_recognition.py` 各有一处调用，
验证的是两个调用点把设置正确传下去，不是档位语义。

## 4. 能不能把"到点重试准入"从 live 门控里剥出来，在 shadow 下跑

**可以，而且这是三个选项里改动面最小、语义变化最可辩护的一个。** 理由按证据强弱排：

1. **它的两条腿今天已经被拆开了，而且是错误的那一半在跑。** §1.3 第 3 条：超时（#7）在 shadow 下跑，
   重试（#6）不跑。把 #6 的门从 `!= "live"` 改成 `== "disabled"`，只是让两条腿回到同一个门下，
   与 #7、#8、#2、#5 的门保持一致——**shadow 一档里"建合约、投影矛盾、判超时"本来就都做**，
   唯独不做"重试"，这个不一致没有任何设计文档支持。
2. **它不改写入语义。** #6 只写三张本地表，零交易所调用（§3.2）。它下游触发的下单路径，
   与今天事件驱动唤醒走的是同一条、同一个 `auto_trade_executor`、同一套准入判据
   （`assess_entry_assembly_admission` 返回 `ready` 才释放，仍 pending 就继续等，blocked 就判过期）。
   **它不会在上下文仍不完整时放行入场**。
3. **写入语义仍受原门控约束。** #9（耐久镜像收敛）、#10（fail-closed）、#11（CAS 守卫）
   这三处继续只在 live 生效，一行都不动。也就是说本方案**不触碰**"合约簿记如何影响真实执行结果"的任何一处。
4. **测试成本低。** §3.3：没有测试锁定 shadow 的惰性，新增"shadow 下也释放/过期"的用例即可，
   现有 12 个 live 用例全部照旧通过。

**必须同时补的一件事（步骤文件任务 3）**：现在 `EntryAdmissionReconcileResult.incidents` 这个计数器
**从头到尾没有任何一处对它 +1**（`entry_admission_reconciler.py` 里只有 33 行的定义和 87 行的初始化），
仓库里也**不存在 `entry_admission_expired` 这个 incident 类型**，生产 `runtime_incidents` 里
`entry_admission%` 命中 **0 条**。所以到达 deadline 仍未成交的入场，今天是**完全静默**的——
7 条过期的入场（§5）没有产生过一条告警。剥离门控如果不同时补告警，只会把"静默过期"变成
"多试几次后仍然静默过期"。

**不建议直接翻开关**（选项 a）的理由：它会同时打开 §3.1 的第 3、4 两条——把三处合约簿记从 fail-open
变成 fail-closed，并给指令项终态写入加 CAS 守卫。这两处**没有任何测试覆盖它们在 live 下对真实入场
成败的影响**，而它们的失败模式是"入场处理整条抛出去"。在只需要修"到点不重试"这一个缺陷的场合，
翻开关的风险面比问题本身大一个量级。翻档本身仍应按原方案任务 19 走独立评审与用户批准，不与本步捆绑。

## 5. 过去 30 天被 `adjacent_entry_context_pending` 推迟且最终过期的入场

**统计方法说明**：不能用 `message_instruction_items.result_json like '%adjacent_entry_context_pending%'`
统计——过期时 `_expire_deferred_entry_truth` 会把 `result_json` 置 NULL，已过期的那些全查不到
（那样查只能查到 2 条，是漏报）。正确口径是 `entry_assembly_attempts`：该表**只在
`selection.status == "pending"` 时才建行**（`entry_assembly_admission.py:613`），
而 `select_adjacent_entry_fragments` 只在 161-162 行产出 `status="pending"`，且恒带 `reason_code="adjacent_entry_context_pending"`（全模块只此一处产 pending），
所以这张表的每一行按构造都是一次相邻上下文推迟。

**结果：7 条，全部在过去 30 天内（2026-08-17 ~ 2026-09-04），全部来自 `auto_trade` 群。**

| attempt | 指令项 | 群 | 消息 | 推迟于 | deadline | 终态 |
|---|---|---|---|---|---|---|
| 3 | 602 | 比特币陈哥会员群（auto_trade） | 10051 | 08-17 03:00 | 08-17 09:00 | 合约 `expired`，指令项 `failed` |
| 4 | 691 | 比特币陈哥会员群 | 10091 | 08-20 09:00 | 08-20 15:00 | 同上 |
| 5 | 728 | 峰哥高级会员群（auto_trade） | 8854 | 08-22 01:40 | 08-22 07:40 | 同上 |
| 6 | 742 | 舒琴会员群（auto_trade） | 3567 | 08-23 06:15 | 08-23 12:15 | 同上 |
| 7 | 745 | 峰哥高级会员群 | 8945 | 08-23 10:57 | 08-23 16:57 | 同上 |
| 8 | 945 | 舒琴会员群 | 3613 | 09-03 11:00 | 09-03 17:00 | 同上 |
| 11 | **969** | **峰哥高级会员群** | **9181** | 09-04 12:49 | 09-04 18:49 | 同上 |
| 14 | 1029 | 峰哥高级会员群 | 9227 | **09-08 14:09（进行中）** | 09-08 20:09 | 当前 `deferred`，本步不干预 |

按群：峰哥 3 条（+今天 1 条在途）、陈哥 2 条、舒琴 2 条。**三个群都是 `auto_trade`。**
7 条的 attempt 状态全部停在 `pending`（从未被唤醒），合约原因码全部是
`execution_contract_deadline_elapsed`（由 #7 写入，不是 #6 写的 `entry_admission_deadline_expired`
——**#6 一次都没跑过，这是它在生产从未生效的直接证据**）。

**与 A-4 的接点**：attempt 11 / 指令项 969 就是 raw 14843（峰哥群 message 9181）。
A-4 刚刚归档的"峰哥幽灵" lifecycle 1081 正是这条消息的 lifecycle——它在 09-04 12:50 被标成
`entered`（`entry_price_actual=2443.12`），而它的入场指令在六小时后静默过期。
**同一条消息，账面显示已入场，实际从未下单，无告警。** 这条链现在两端都有了证据。

**对照组（说明机制本身是好的）**：attempt 9 / 12 / 13 在 shadow 期间被**事件驱动**的唤醒路径正常唤醒，
指令项 `submitted`、合约 `verified / entry_submission_verified`。也就是说：阻塞源消息按时结束时，
入场能正常完成；只有当那个事件丢失或阻塞条件以别的方式解除时，才会掉进"只有超时、没有重试"的坑。

## 6. 建议（待指挥会话裁定）

按风险从低到高：

- **首选：选项 b + 告警。** 把 `entry_admission_reconciler.py:47` 的门从 `!= "live"` 改成
  `== "disabled"`，让准入重试与它的超时孪生走同一个门；同时实现 `entry_admission_expired`
  运行时告警（并加入 `ALWAYS_NOTIFIED_INCIDENT_TYPES`），让 `incidents` 计数器第一次真正被 +1。
  新增 shadow 用例，现有 12 个 live 用例不动。风险等级 L2（改的是恢复时机，不是写入语义）。
- **不建议在本步翻开关**（选项 a）：会连带打开两处未被测试覆盖的 fail-closed 语义变化，风险面大于本步要修的问题；
  翻档应按原方案任务 19 独立走评审 + 用户批准。
- **无论选哪个，任务 3 的告警都必须做**：7 条静默过期的入场里有 3 条在峰哥、2 条在陈哥、2 条在舒琴，
  全是自动交易群，全程零告警。只补重试而不补告警，下一次仍然没人知道。

**本评估未做任何写入、未部署。等裁定后再动代码。**

---

## 附录：任务 2/3 的实施（2026-09-08，指挥会话裁定选项 b + 告警后）

**代码提交 `04a5643c7b3bdbed630e51ecdfd79413e33cff57`**（含 A-4 的 one_off，随本次一起上线）。
部署前生产 HEAD `a6869acf559776c43b608437eb68a88eeadb9874` 为回滚参考（回滚即
`tg-deploy a6869acf559776c43b608437eb68a88eeadb9874`）。部署前 `active_write_count=0`、在途管理批次 0。
全量 **7923 passed / 4 skipped / 0 failed**。

### 改了什么

1. **门控对齐**：`entry_admission_reconciler.reconcile_due_entry_admissions` 的门由
   `execution_contract_mode != "live"` 改为 `== "disabled"`，与它的超时孪生
   （`instruction_execution_reconciliation`）同门。`#9` 耐久镜像收敛、`#10` fail-closed 合约投影、
   `#11` 终态写入 CAS 守卫**一字未动**，仍只在 `live` 生效。
2. **过期不再静默**：新增 `capture_entry_admission_expired`（severity `high`，
   `source_kind=message_instruction_item`），summary 含 item id（`operation`）、`raw_message_id`、
   `chat_id`、推迟原因（`reason_code`）与 `deadline_at`；加入 `ALWAYS_NOTIFIED_INCIDENT_TYPES`。
   `EntryAdmissionReconcileResult.incidents` 这个此前从未被 +1 过的计数器现在真的计数。
   告警失败（抛异常或被拒）**不回滚已提交的过期**，只是不计数——账本变更先提交且是耐久事实。
3. **两个封闭字段集的扩项（本步唯一一处扩大既有边界，需备案）**：
   `runtime_incidents._SUMMARY_FIELDS` 增加 `chat_id` 与 `deadline_at`。理由与 A-2 当初加入
   `raw_message_id` / `attempt_id` / `task_name` 完全相同——操作者必须能在不开数据库的情况下
   知道这条告警属于哪个群、什么时候到期。**deadline 必须是独立字段而不是 `impact` 的一部分**：
   `record_runtime_incident` 的不透明串启发式会把嵌进长标签的时间戳读成一个高熵 token，
   实测 **400 个不同 deadline 的复合形式全部（400/400）被拒**、整条详细 summary 退回最小版；
   独立字段下同样 400 个全部通过。这条实测被写成回归测试
   （`test_the_deadline_never_costs_us_the_detailed_summary`）锁住。
4. **A-3 作废工具增加可选参数 `void_reason`（默认值不变）**：见下节。默认仍是
   `stale_pending_voided_2026_09_07`，归档那次运行逐字节可复现；调用方可传自己的日期。

### 测试

`tests/test_entry_admission_reconciler.py` 由 14 条增至 **22 条**，**既有 14 条一条未改、未削弱**。
新增：shadow 下释放到期项、`disabled` 仍然惰性（门从 live 移到 disabled 后这一条才有意义）、
过期产生告警且字段齐全、告警抛异常不撤销过期、告警被拒计为未上报、
默认路径（不注入替身）真的写出一行 `runtime_incidents`、
`entry_admission_expired` 在 always-notified 基线内、400 个 deadline 的 summary 契约回归。
**变异检验**：把门改回 `!= "live"`，4 条新测试转红；改回后 22 条全绿。
`tests/test_stale_pending_instruction_void.py` 由 7 条增至 9 条（自带日期的 reason、默认仍是归档值）。

### 部署前的一次性作废（指挥会话要求）

item **1029**（raw 15496，峰哥群 message 9227，14:09:25Z 推迟，deadline 20:09:25Z）在部署时刻
仍 `pending` 且未过 deadline。它承载的是 **6 小时前**的入场意图（ETH 多，约 2460），
恢复器一上线就会按这个过时意图放行下单，因此先作废。

- **动手前只读核实零交易所敞口**：`execution_bindings` 0 行、`execution_order_legs` 0 行、
  `execution_events` 0 行、`position_protection_ledger` 0 行。
- 备份 `/root/evidence/step3d/research-backup-20260908T160550Z.db`
  （sha256 `1211db3521448276a255452136ad7d8f46cda078053efd8bd34203ab8f08426b`，quick_check ok），
  从备份复制一份完整演练后再对生产执行，**两者逐字段一致**。
- **改前 → 改后**：item 1029 `pending` → `failed`，
  `error_json={"reason":"stale_pending_voided_2026_09_08"}`，`escalation_state='expired'`，
  盖 `last_progress_at`；lifecycle 1121 `entered` → `cancelled`，
  `exit_reason='stale_pending_voided_2026_09_08'`，
  `management_action='stale_pending_instruction_voided'`。
- **全库行数只动了这两处**：指令项 `pending 3→2`、`failed 143→144`；
  lifecycle `entered 11→10`、`cancelled 8→9`；其余状态一字未动。执行后 `PRAGMA quick_check` = ok。
- **通知**：一条 SYSTEM bot 消息，`message_id=4067`，430 字符，写明作废的行、推迟原因、deadline、
  为什么现在作废、以及"如仍需这笔入场请人工下单"。
- **为什么给 A-3 的工具加参数而不是直接复用**：该工具的 `VOID_REASON` 写死为
  `stale_pending_voided_2026_09_07`。给一个 09-08 的决定盖 09-07 的标签，会让这两行唯一的
  审计痕迹记错日期。新增的 `void_reason` 参数默认值就是原常量，归档那次运行的行为一字未变。
- **一处遗留**：被作废的是**指令项**，它的合约 327 仍是 `deferred`，会在 20:09Z 由超时孪生
  判成 `execution_contract_deadline_elapsed`。这不会触发本步的新告警（新告警只从恢复器发出），
  也不会再被恢复器碰到（恢复器只选 `status='pending'` 的指令项，而它已是 `failed`）。

### 生产侧的告警可达性（部署后只读核实）

用 **worker 进程的真实环境**（`/proc/<pid>/environ`，含 systemd 的
`EnvironmentFile=/etc/telegram-kol-worker.env`）加载 `load_runtime_incident_config(environment_only=True)`：
`captures("entry_admission_expired") = True`、`notifies(...) = True`、`telegram_notifications_enabled = True`。
生产 env 的两个白名单都非空，因而按设计与代码基线取并集，新类型自动在内。
**一处过程教训**：第一次核实时只喂了 `systemctl show -p Environment` 的内容（只有
`TELEGRAM_KOL_RUNTIME_ROLE`），漏掉了 `EnvironmentFile`，得到 `captures=False` 的错误结论。
在这台机器上判断"进程实际看到什么环境变量"，只有 `/proc/<pid>/environ` 是可信的。
