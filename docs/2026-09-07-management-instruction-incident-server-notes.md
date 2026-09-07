# 两起管理指令未执行 — 只读调查记录

调查会话：`local_98b3dc80`（阶段 4 执行会话），应指挥会话 `local_858790fe` 请求。
**全部只读。没有修改任何账本、没有撤单、没有下单、没有部署、没有重启。**
记录时间：2026-09-07T07:4xZ。

> 本文件只记录**观测到的事实**与明确标注的**假设**。修复由用户决定。

---

## 一、结论先说

两起都**不是**执行层失败，也**都与阶段 4 的部署/重启无关**：

| | 峰哥 止盈 | 大镖客 保本离场 |
|---|---|---|
| 指令消息 | raw_message **15155**，chat `-1002409877375`，msg_id 9203 | raw_message **15201**，chat `-1003048800035`，msg_id 4512 |
| 内容 | `现价2510获利出局` | `上插针80500没过，再次回踩，风险加大，求稳可以保本离场观望一下` |
| 时间 (UTC) | **2026-09-06 23:10:28** | **2026-09-07 03:01:12** |
| 处理作业 | `succeeded` | **`failed`，attempt_count=5，`processing_error:RuntimeError`** |
| 识别结论 | `非策略` — **目标不唯一，需人工确认** | `非策略` — 但正文承认这是平仓离场指令 |
| 是否建管理批次 | **否** | **否** |
| 目标仓位 | binding **340**（ETH 多） | 关联到 lifecycle **1074 / binding 337**（2026-09-04 的 BTC 多） |
| 结局 | 一直持有到 **05:49:01Z 用户手工平仓** | 仓位仍在，`management_action=exit_requested` 但无执行 |

**`strategy_management_batches` 最后一行是 id 159，创建于 2026-09-05 16:58:57Z。**
也就是说，**管理批次管道已经约两天没有产出过任何一行**。
`management_message_envelopes` 最后一行更早：id 18，2026-08-28 11:16。

---

## 二、阶段 4 的部署与重启时间（UTC，用于排除重叠）

服务器本地时区为 UTC+8，下列均已换算为 UTC。来源：`journalctl -u telegram-kol-worker`
与 `deepcoin_ws_connection_gaps` 的 `process_start` 行（两者逐条吻合）。

| UTC 时间 | 动作 | worker MainPID |
|---|---|---|
| 04:43:33 | `tg-deploy da7ef255` | 2569651 |
| 04:58:53 | `tg-deploy 9a9834bf` | 2574582 |
| 05:12:32 | `tg-deploy afcad2a2` | 2578954 |
| **05:15:29** | **阶段要求的专门 worker 重启（非部署）** | 2580000 |
| 05:30:02 | `tg-deploy 59156895` | 2584684 |
| 05:43:27 | `tg-deploy d62d25f6` | 2589114 |
| 06:03:10 | `tg-deploy ddda43cb` | 2595752 |
| 06:28:49 | `tg-deploy 294bd54b` | 2604230 |

**不属于本会话**（阶段 3 会话 `local_c8d0dc4e` 所为，列出以便完整）：
`02:26:51Z`（`tg-deploy 4bdc6ba6`）与 `02:28:17Z`（其阶段要求的重启）。

### 重叠判定

- **峰哥指令 23:10:28Z**：本会话 2026-09-07 **04:08Z 才创建**，第一次部署在 **04:43:33Z**。
  比指令晚 **5 小时 33 分**。**完全不可能重叠。**
- **大镖客指令 03:01:12Z**，其处理作业最后一次失败于 **03:08:20Z**：
  第一次部署 04:43:33Z 比它晚 **1 小时 35 分**。
  更强的证据：`deepcoin_ws_connection_gaps` 显示 `process_start` 缺口从
  **02:28:19Z 之后直到 04:43:38Z 之前一条都没有**——
  worker 在 `02:28:19Z ~ 04:43:38Z` 之间**连续运行、未重启**，
  完整覆盖了大镖客消息的接收、识别、5 次重试与失败的全过程。

**结论：没有任何重启窗口与两起指令重叠。**

---

## 三、峰哥止盈（raw_message 15155）

- `message_processing_jobs` id 3394：`succeeded`，`worker_completed`，
  23:10:28.733 入队 → 23:10:28.761 完成
- `message_recognitions` id 15150（23:11:13.982）：`status=非策略`，理由原文：

  > 当前消息为平仓指令（'现价2510获利出局'），涉及ETH，但未指定目标策略。
  > 候选策略中有两个活跃ETH策略（thread_id 464和450），thread_id 464有验证仓位，
  > thread_id 450状态为entered但无验证仓位。消息无回复上下文，**无法唯一确定目标，需人工确认**。

- 识别层**正确识别出这是平仓指令**，但因为同群有两个活跃 ETH 策略、消息又没有 reply 上下文，
  按"不唯一就不猜"的原则拒绝认领——这本身符合硬性禁止第 1 条的精神。
- **但是：`strategy_management_notifications` 自 2026-09-06 22:00 起零行。**
  识别层说"需人工确认"，却**没有产生任何人工确认通知**。指令就此静默消失。
- 目标仓位 binding **340**（lifecycle 1095，thread 464，ETH 多，
  2026-09-06 15:22 入场，均价 2477.46）一直持有，
  直到 **2026-09-07 05:49:01Z 被系统外手工平仓**（成交 2509.75，+10.0099 USDT）。
  该手工平仓的详细归因见同目录 `findings.md` 第 5 条。

**峰哥指令与手工平仓之间相隔 6 小时 38 分。**

---

## 四、大镖客保本离场（raw_message 15201）

### 处理作业失败

`message_processing_jobs` id 3440：`status=failed`，`attempt_count=5`，
`last_reason=processing_error:RuntimeError`，
入队 `03:01:12.244` → 最后一次 `03:08:20.069`。

journal 里的确切异常（`11:04:49+08` = `03:04:49Z`）：

```
ERROR message_processing_worker message processing job failed raw_message_id=15201 status=pending
RuntimeError: authoritative_execution_outcome_unknown
RuntimeError: automatic retry blocked by active or uncertain authoritative execution
ERROR context_resolution_worker context reanalysis failed raw_message_id=15201 context_attempt_id=4864
RuntimeError: authoritative execution is already in progress or outcome is uncertain
```

### 根因线索：9 条卡住的 `uncertain` 权威执行尝试

`authoritative_execution_attempts` 现状：总 420 行，
`succeeded` 398 / `failed_safe` 13 / **`uncertain` 9**。九条全部未解决：

| id | raw_message_id | uncertain_at (UTC) |
|---|---|---|
| 6 | 14825 | 2026-09-04 12:35:00 |
| 25 | 14843 | 2026-09-04 12:49:16 |
| 77 | 14889 | 2026-09-04 14:49:30 |
| 191 | 15006 | 2026-09-05 13:44:54 |
| 199 | 15013 | 2026-09-05 16:59:04 |
| 319 | 15144 | 2026-09-06 15:23:12 |
| 360 | 15186 | 2026-09-07 02:33:46 |
| **372** | **15201（大镖客本条）** | **2026-09-07 03:04:49** |
| 379 | 15204 | 2026-09-07 03:45:55 |

worker 每约 2 分钟持续刷这一组 ERROR（早在本会话之前就在刷）：

```
ERROR web_app recognition execution finding family=active_authoritative_attempt
      row_id=6|25|77|191|199|319|360 phase=uncertain action=observe_uncertain
```

**假设（未验证，需代码侧确认）**：这批未解决的 `uncertain` 尝试触发了
"automatic retry blocked by active or uncertain authoritative execution" 这条闸门，
使 15201 的 5 次重试全部被拒，管理批次因此从未创建。
最早一条自 **2026-09-04** 起就卡着，与"管理批次管道两天无产出"在时间上吻合。
**这是相关性，不是已证实的因果，请以代码判定为准。**

### 另一个独立问题：目标关联可能指错策略

`message_recognitions` id 15192（03:04:45.004）`status=非策略`，理由原文：

> 消息明确建议'求稳可以保本离场观望一下'，属于对已有仓位的平仓离场指令。
> 消息提及'上插针80500没过'，与策略线程443（lifecycle_id 1074，BTC long，
> 止盈位81100/81800/82500）的市场状态关联性较强，且该策略已entered并持有多个仓位，
> 符合保本离场语境。**未通过reply明确指向，但基于价格描述和策略活跃状态，可关联此策略。**

据此写入 `strategy_lifecycles` id **1074**（03:04:44.999）：
`management_action='exit_requested'`，`management_signal_message_id=4512`。

**但 lifecycle 1074 是 2026-09-04 的老线程**（thread 443，message_id 4495，
binding **337**，`entered_at=2026-09-04 08:05:33`）。
同一个群里真正对应当天行情的是 **lifecycle 1097**
（thread 466，message_id **4509**，binding **341**，`entered_at=2026-09-07 01:00:03`）——
就是大镖客 `00:59:19Z` 那条 `BTC 方向：多 建仓：80000-79100` 建立的仓位，
而 `03:01:12Z` 的保本离场紧随其后。

**需要人工判断：这条保本离场到底该指向 1074 还是 1097，或两者都要。**
识别层自己写明了它是靠"价格描述和策略活跃状态"推断的，没有 reply 锚点。

补充：binding **337** 状态 `active`，
`last_exchange_status='position_attribution_evidence_unavailable'`；
它上一次的管理批次是 id 158（2026-09-04 09:22:59），
`status=recovery_required`，`reason_code=close_final_preflight_failed`——**至今未解决**。

### 相邻现象：间歇 401 第 4 次复现

`03:00:49Z`（大镖客消息前 **23 秒**）：

```
ERROR web_app Deepcoin pending trigger order load failed for ETH-USDT-SWAP
httpx.HTTPStatusError: Client error '401 Unauthorized' for url
  'https://api.deepcoin.com/deepcoin/trade/trigger-orders-pending?instType=SWAP&instId=ETH-USDT-SWAP&limit=100'
```

这是该端点第 4 次 401（前三次 09-06 17:04Z、09-06 18:31Z、09-07 02:27Z）。
时间上紧邻，**但没有证据表明二者有因果关系**——该 401 发生在既有的
`web_app._load_deepcoin_pending_tpsl_orders` 路径，语义正确（记为证据不可用）。
仅因时间接近而记录在此，供排查参考。

---

## 五、当前实盘持仓（只读快照，07:19:48Z）

- 交易所：`complete=true`，**2 个仓位、0 挂单**，
  fingerprint `ddcaa6a0aae69c2f4fef9d224844a5e59d8b3260f79b3f27208e8acb2956effc`
- binding **341** BTC 多，pos `1001125163581280`，`position_ownership_verified`，
  另有一条 `pending` 未成交的条件入场腿（leg 587）
- binding **342** ETH 多，pos `1001125164628529`，`position_ownership_verified`
- binding **337** BTC 多，`active`，`position_attribution_evidence_unavailable`，
  两条 entry leg（pos `1001125123045253` / `1001125126414222`）——
  **这两个 posId 不在当前交易所持仓列表里**，需人工核对
- binding **340** 已 `closed`（05:49:01Z 手工平仓）

---

## 六、本会话明确没有做的事

- 没有修改任何账本行（阶段 4 影子写入有会话级守卫，flush 前拒绝非影子表对象）
- 没有撤单、改单、下单；阶段 4 代码全程只调用既有 `list_*` GET
- 没有触碰 `authoritative_execution_attempts` 的任何一行
- 没有为本次调查做任何部署或重启
- 收到指挥会话通知后，未再进行任何部署或重启

## 七、建议交给用户决定的下一步（本会话不执行）

1. 9 条 `uncertain` 权威执行尝试如何处置——它们很可能是管理管道停摆的闸门。
2. 峰哥这类"目标不唯一"的平仓指令，识别层判"需人工确认"后**没有任何通知**，
   这条静默路径需要补上告警。
3. 大镖客 03:01 的保本离场指向 lifecycle 1074 还是 1097，需要人工判定；
   binding 337 的 `position_attribution_evidence_unavailable` 与
   批次 158 的 `recovery_required` 也一并需要处理。
4. 当前 2 个实盘仓位（binding 341/342）是否仍要按原指令离场，由用户决定。

---
---

# 补充调查（第二轮）— 应指挥会话要求

调查会话 `local_98b3dc80`，2026-09-07T08:0xZ。**全部只读，未修改任何内容。**

## 0. 先更正我第一轮的一个错误结论

第一轮我写"9 条历史 uncertain 挡住了 15201 的重试"。**这是错的。**
指挥会话核实闸门 `authoritative_recognition.py:1610-1639` 是按
`raw_message_id` 查该消息**自己**的 attempt，不是全局闸。
经复核该判断正确：15201 的 5 次重试被拒，是因为**它自己的第一次尝试
（attempt 372）先落成了 uncertain**，与另外 8 条历史 uncertain 无关。
下面第 1 条查的就是"372 第一次为什么 uncertain"。

---

## 1. attempt 372（raw 15201）为什么第一次就 uncertain

### 数据库全行

```
id                      = 372
raw_message_id          = 15201
authoritative_generation= f619c910f295415db4426ccb1ee3fd11
status                  = uncertain
claim_token             = 88841fa190bd4aa3b10f4648268df2f4
owner_runtime_role/pid  = worker / 2526485
claimed_at              = 2026-09-07 03:04:44.939115
heartbeat_at            = 2026-09-07 03:04:45.022977
lease_expires_at        = 2026-09-07 03:06:44.939115
side_effect_started_at  = 2026-09-07 03:04:45.022977
outcome_recorded_at     = None          ← 关键
exchange_effect         = outcome_unknown
automation_status       = None
automation_reason       = None
evidence_refs_json      = None          ← 关键：没有任何交易所写入被登记
error_class             = ExecutionBoundaryOutcomeUnknown
error_summary           = partial_failed ← 关键：这是适配器返回的 raw_status
uncertain_at/completed_at = 2026-09-07 03:04:49.152353
```

时间线：认领 `03:04:44.939` → 副作用开始 `03:04:45.023`（+84 ms）
→ 判 uncertain `03:04:49.152`（副作用开始后 **4.13 秒**）。
raw 15201 只有这一条 attempt，没有第二条。

### journal 完整日志段（03:01:12Z–03:04:49Z）

**除了那两条 ERROR 之外，这段时间里关于 15201 没有任何 warning、
没有任何交易所异常、没有 401、没有超时、没有其他 Traceback。**
（该窗口内唯一的其他日志是 `lifecycle_monitor` 每分钟一条的
`Skipping simulated lifecycle exit for live execution binding:
lifecycle_id=1074 binding_id=337 …` 与 `lifecycle_id=1095 binding_id=340 …`，
与本次处理无关。）

第一次尝试抛出的调用栈（`11:04:49+08` = `03:04:49Z`）：

```
ERROR message_processing_worker message processing job failed raw_message_id=15201 status=pending
Traceback (most recent call last):
  message_processing_worker.py:606  in _run_claim_body      -> await job_processor(
  message_processing_worker.py:197  in process_message_job  -> await asyncio.to_thread(
  web_app.py:5858                   in <lambda>             -> _run_authoritative_processor(
  web_app.py:4253  in _run_authoritative_processor          -> process_authoritative_message(
  authoritative_recognition.py:1561 in process_authoritative_message
                                    -> _run_leased_authoritative_execution(
  authoritative_recognition.py:1939 in _run_leased_authoritative_execution
                                    -> raise RuntimeError("authoritative_execution_outcome_unknown")
RuntimeError: authoritative_execution_outcome_unknown
```

随后第 2–5 次重试全部撞上按 raw_message_id 的闸门：

```
  authoritative_recognition.py:1455 in process_authoritative_message
                                    -> _load_completed_execution_for_automatic_retry(
  authoritative_recognition.py:1638 in _load_completed_execution_for_automatic_retry
                                    -> raise RuntimeError(
RuntimeError: automatic retry blocked by active or uncertain authoritative execution
```

以及 `03:05:17Z` 的 `context_resolution_worker context reanalysis failed
raw_message_id=15201 context_attempt_id=4864` →
`RuntimeError: authoritative execution is already in progress or outcome is uncertain`。

### 机制（读 `execution_boundary.py` 得出）

`error_summary` 存的就是适配器返回的 `raw_status`。
`build_execution_boundary_outcome()`（`execution_boundary.py:217-286`）里：

```python
_KNOWN_UNKNOWN_STATUSES = {"unknown", "partial_failed", "recovery_required",
                           "in_progress", "reconciling", "operator_required", ...}
...
if raw_status in _KNOWN_UNKNOWN_STATUSES:
    exchange_effect = "outcome_unknown"
elif raw_status in _KNOWN_EFFECT_STATUSES and not writes and not item_refs:
    exchange_effect = "outcome_unknown"
...
if exchange_effect == "outcome_unknown":
    status = "outcome_unknown"
```

所以：**适配器返回 `partial_failed`，按定义就无法证明交易所侧结果，
边界层据此冻结为 `outcome_unknown` —— 这是设计上的 fail-closed，不是 bug。**

`evidence_refs_json` 为 NULL 说明 `tracker.writes` 为空，
即**这次尝试没有登记任何交易所写入**。
但边界层按硬性禁止第 2/4 条的精神，拒绝把"没登记到写入"说成"什么都没发生"，
只报 unknown。这一点是对的。

### 真正的诊断缺口

**适配器为什么返回 `partial_failed`，系统里没有任何地方记录。**
`automation_reason` 为 NULL、`evidence_refs_json` 为 NULL、
journal 里也没有对应的日志行。9 条 uncertain 全部如此。
**这是本次最该补的可观测性缺口**：一次 fail-closed 冻结没有留下可归因的原因，
下次复现还是查不出来。

---

## 2. 其余 8 条 uncertain 是否同一形态

**是，完全同一形态。** 九条全部：
`error_class=ExecutionBoundaryOutcomeUnknown`、`exchange_effect=outcome_unknown`、
`outcome_recorded_at=NULL`、`evidence_refs_json=NULL`、
`automation_status/automation_reason=NULL`，且 `side_effect_started_at` 均已置位。

| id | raw | error_summary (= 适配器 raw_status) | uncertain_at (UTC) | owner pid |
|---|---|---|---|---|
| 6 | 14825 | `in_progress` | 2026-09-04 12:35:00 | 1338473 |
| 25 | 14843 | `in_progress` | 2026-09-04 12:49:16 | 1338473 |
| 77 | 14889 | `completed` | 2026-09-04 14:49:30 | 1338473 |
| 191 | 15006 | `completed` | 2026-09-05 13:44:54 | 1525316 |
| 199 | 15013 | `partial_failed` | 2026-09-05 16:59:04 | 1874433 |
| 319 | 15144 | `in_progress` | 2026-09-06 15:23:12 | 2284211 |
| 360 | 15186 | `in_progress` | 2026-09-07 02:33:46 | 2526485 |
| **372** | **15201** | **`partial_failed`** | 2026-09-07 03:04:49 | 2526485 |
| 379 | 15204 | `partial_failed` | 2026-09-07 03:45:55 | 2526485 |

三种 raw_status：`in_progress` ×4、`partial_failed` ×3、`completed` ×2。
前两种直接命中 `_KNOWN_UNKNOWN_STATUSES`；
`completed` 那两条命中的是"`completed` 但既无 writes 又无 item_refs"分支。
**九条分布在 5 个不同的 worker PID、四天之内**，
所以不是某一次进程异常，是**反复出现的常态**。

---

## 3. 管理管道到底是不是"停摆"

**我第一轮说的"两天零产出"口径不准，需要更正。**
按结构化字段（`_context_resolution.decision` / `management_action` /
`lifecycle_event.event_type`）精确统计，而不是关键词匹配：

`2026-09-05T00:00Z` 起共 **302** 条 `recognition_decisions`，
其中**真正带管理意图的只有 11 条**。所以不是"有很多管理指令但都没执行"，
而是**管理指令本来就少；但这 11 条里执行成功的是 0 条**。

| raw | 时间 (UTC) | action / event / decision | automation | 结局 |
|---|---|---|---|---|
| 14965 | 09-05 03:20:30 | – / position_update / manage_thread | skipped / `mimo_authoritative_not_safely_applied` | 无批次 |
| 14969 | 09-05 05:45:57 | hold_update / position_update / manage_thread | skipped / 同上 | 无批次 |
| 14989 | 09-05 12:01:02 | hold_update / position_update / manage_thread | skipped / 同上 | 无批次 |
| 15006 | 09-05 13:44:54 | hold_update / position_update / manage_thread | **uncertain** | 无批次 |
| 15013 | 09-05 16:58:57 | **partial_take_profit** / position_update / manage_thread | **uncertain** | **批次 159 → blocked / protection_price_or_size_mismatch** |
| 15059 | 09-06 04:25:04 | – / exit_position / exit_thread | skipped / 同上 | 无批次 |
| 15136 | 09-06 15:01:11 | **exit_full** / exit_position / exit_thread | deferred / `waiting_source_deletion_exit` | 无批次 |
| 15165 | 09-06 23:58:10 | **exit_full** / exit_position / exit_thread | skipped / 同上 | 无批次 |
| 15170 | 09-07 00:40:56 | hold_update / position_update / manage_thread | skipped / 同上 | 无批次 |
| **15201** | **09-07 03:04:44** | **exit_full**（大镖客保本离场） | **uncertain** | 无批次 |
| 15204 | 09-07 03:45:54 | **exit_full** / exit_position / exit_thread | **uncertain** | 无批次 |

**汇总：11 条管理意图 → 7 条 `skipped/mimo_authoritative_not_safely_applied`、
3 条 `uncertain`、1 条建了批次但 `blocked`。执行成功 0 条。**

两个结构性观察：

1. **主导失败原因是 `mimo_authoritative_not_safely_applied`（7/11），不是 uncertain（3/11）。**
   我第一轮把注意力放错了地方。这 7 条根本没走到执行边界就被跳过了。
2. **这 11 条的 `authoritative_status` 全部是 `非策略`。**
   即：权威识别把管理类消息一律判为"非策略"，而管理意图是由
   `_context_resolution`（`decision=manage_thread` / `exit_thread`）单独给出的。
   两者之间的衔接是否就是 `mimo_authoritative_not_safely_applied` 的来源，
   **需要代码侧确认**——我只能观察到这个共现，不能断定因果。

**峰哥的 15155 不在这 11 条里。** 它的 `_context_resolution` 判的是
`decision=unresolved` / `conflict_types=[target_ambiguous]`，
`management_action` 为 null，所以连"管理意图"都没成立就结束了（与 09-05 的 14951 同型）。

---

## 4. `strategy_management_notifications` 有没有过"需人工确认"类型

**从来没有。这是设计缺口，不是最近坏掉。**

- 全表 **95 行**，最新一行 id 95（batch 159，2026-09-05 16:59:04）
- `state` 只有三种：`blocked` 71、`recovery_required` 22、`partial_failed` 2
- **`management_batch_id` 为 NULL 的行数：0** ——
  **每一行都必须挂在一个已存在的管理批次上**

推论（结构上成立，不需要读代码即可断定）：
**一条管理指令如果没能建成批次，就不可能产生任何通知。**
而第 3 节显示 11 条管理意图里有 10 条没建成批次。
所以峰哥那条"目标不唯一、需人工确认"静默消失，
不是通知发送失败，而是**这条路径上根本没有可以发通知的对象**。

---

## 5. binding 337 两条 entry leg 的仓位是怎么平掉的

两条都在 **2026-09-04T13:07:38Z 同一时刻被止损单触发平掉，且都是全平。**

| posId | 开仓 (UTC) | 平仓 (UTC) | avgPx → closeAvgPx | pos/closePos | pnl |
|---|---|---|---|---|---|
| `1001125123045253` | 09-04 08:05:34 | 09-04 13:07:38 | 80490 → 80149.8 | 10 / 10（全平） | -3.4015 |
| `1001125126414222` | 09-04 12:36:58 | 09-04 13:07:38 | 79688.2 → 79200 | 14 / 14（全平） | -6.83500001 |

**是止损触发，不是手工平仓**，证据：同一时刻的条件单历史里有两张
`triggerTime = 1788527258`（= 13:07:38Z）的 TPSL：

```
ordId 1001125123045252  sz=10  triggerPx=79200  slTriggerPrice=79200  triggerTime=1788527258
ordId 1001125126414221  sz=14  triggerPx=79200  slTriggerPrice=79200  triggerTime=1788527258
```

对应成交的市价 reduceOnly 卖单（`clOrdId` 为空）：
`1001125126868760` sz14 @79200 pnl -6.835、`1001125126868759` sz5 @79200 pnl -6.45。
止损价 79200 与成交价 79200 完全吻合。

### 账本为什么还是 active

- `execution_bindings` id 337：`status=active`，
  `last_exchange_status='position_attribution_evidence_unavailable'`
- 两条 entry leg（579 / 580）：`status=active`、`attribution_status=verified`、
  `terminal_reason=NULL`
- **账本已经落后交易所约 3 天**（09-04 13:07:38Z 平掉 → 至今仍 active）
- 同一 binding 上还挂着管理批次 **158**（09-04 09:22:59，
  `status=recovery_required`，`reason_code=close_final_preflight_failed`），
  以及通知 id 94（`recovery_required`，`status=pending`）——**均未解决**
- `lifecycle_monitor` 每分钟仍在打
  `Skipping simulated lifecycle exit for live execution binding:
   lifecycle_id=1074 binding_id=337 to_status=exited reason=stop_loss`
  ——**它已经看出该止损了，但因为是 live binding 而跳过模拟平仓**

**这直接解释了大镖客那条指令的第二个问题**：识别层把 03:01 的保本离场
关联到了 lifecycle 1074 / binding 337，而这个 binding 的仓位**早在 09-04 就已被止损清空**。
即使管理批次当时建成了，它要平的也是一个**已经不存在的仓位**。
真正还持有 BTC 多头的是 lifecycle 1097 / binding 341。

---

## 六（补充）、修正后的整体判断

1. **两起指令都与阶段 4 的部署/重启无关**（第一轮已用 `process_start` 缺口行证明，结论不变）。
2. **15201 的 5 次重试被拒是它自己第一次 uncertain 的后果**，
   与另外 8 条历史 uncertain 无关（更正第一轮的错误结论）。
3. **管理指令执行成功率：09-05 以来 11 条 → 0 条成功。**
   主导原因是 `mimo_authoritative_not_safely_applied`（7 条），
   其次是执行边界 fail-closed 冻结（3 条），批次层 blocked（1 条）。
4. **"需人工确认"类通知在结构上不存在**（通知必须挂批次，没批次就没通知）。
5. **binding 337 账本落后交易所 3 天**，且大镖客指令被关联到了这个空仓 binding。

### 建议交用户决定（本会话不执行）

- 优先补 `partial_failed` / `in_progress` 的**原因记录**：
  现在一次 fail-closed 冻结不留任何可归因信息，这是复现后仍查不出的根源。
- 查清 `mimo_authoritative_not_safely_applied` 为什么占了 7/11——这是最大头。
- 为"识别出管理指令但未能建成批次"补一条独立的人工确认告警路径
  （现有通知表挂在批次上，天然覆盖不到）。
- binding 337 / lifecycle 1074 的账本与交易所对齐（仓位 09-04 已止损清空），
  以及批次 158 的 `recovery_required`、通知 94 的 pending。
- 当前实盘仅 binding 341（BTC 多）与 342（ETH 多）仍持仓。

---
---

# 补充调查（第三轮）— 告警投递为什么"停了"

调查会话 `local_98b3dc80`，2026-09-07T07:5xZ。**全部只读，未修改、未重启。**
凭据处理：环境变量一律**只列键名**；确需判断的非机密运行开关才打印值；
token / key / secret / chat_id 一律只报"是否设置 + 长度"，绝不输出值。

## 结论先说：**告警投递没有停。**

`2026-09-05T16:27:07Z` 那条不是"最后一次成功投递"，而是
**最后一条"可投递类型"的事件本身**。之后系统产生的每一条事件都属于
"采集但按配置不投递"的类型，所以用户收不到告警——
**但这不是投递故障，是类型白名单的设计结果。**

证明（见第 4 节）：**id > 272 的可投递类型事件共 196 条，全部 `delivered`，
`pending` 为 0。** 投递器一条都没漏。

---

## 1. worker 环境变量：门禁退役重写 drop-in **没有**丢通知相关变量

`EnvironmentFiles=/etc/telegram-kol-worker.env (ignore_errors=no)`
drop-in 只有 `/etc/systemd/system/telegram-kol-worker.service.d/10-telegram-kol-release.conf`。

drop-in 键名 diff（只比键名）：

| 键名 | 当前 drop-in | `worker-dropin.pregate-removal-20260906T035636Z` | `worker-dropin.bak-20260906T034520Z` |
|---|---|---|---|
| `PYTHONPATH` | ✅ | ✅ | ✅ |
| `PYTHONDONTWRITEBYTECODE` | ✅ | ✅ | ✅ |
| `TELEGRAM_KOL_RELEASE_COMMIT` | ❌ 已移除 | ✅ | ✅ |
| `TELEGRAM_KOL_RELEASE_MANIFEST_SHA256` | ❌ 已移除 | ✅ | ✅ |
| `TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN` | ❌ 已移除 | ❌ | ✅ |
| `ReadOnlyPaths=`（非环境变量） | ❌ 已移除 | ✅ | ✅ |

**移除的三个全是已退役的门禁变量**（状态文件 `identity-note` 已记载此为预期结果），
**drop-in 里从来就没有过任何通知 bot 变量**，所以"重写 drop-in 丢了通知变量"这条假设不成立。

`/etc/telegram-kol-worker.env` 里通知相关键名齐全（**mtime 2026-08-23 08:31 +08，
数周未改动**）：
`TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN`、`TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID`、
`TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS`、`TELEGRAM_KOL_SYSTEM_BOT_TOKEN`、
`TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID`、`TELEGRAM_KOL_ALERT_BOT_TOKEN`、
`TELEGRAM_KOL_ALERT_CHAT_ID`、以及
`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_{ENABLED,TYPES,AFTER_ID}`、
`TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES`。

凭据存在性（**值一律未读取、未输出**）：

```
TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN    = <SET, 46 chars>
TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID  = <键存在，但值为空，长度 0>   ← 注意
TELEGRAM_KOL_SYSTEM_BOT_TOKEN          = <SET, 46 chars>
TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID        = <SET, 10 chars>
TELEGRAM_KOL_ALERT_BOT_TOKEN           = <SET, 46 chars>
TELEGRAM_KOL_ALERT_CHAT_ID             = <SET, 10 chars>
```

**`TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 是空值。**
`system_operator_bot_enabled()` 要求 token 与 chat_id **同时非空**
（`system_operator_bot.py:142-143`），所以
`app.state.notification_bot_config` 恒为 `None`。

**但这不是本次告警停发的原因**，两点理由：
1. 该 env 文件 **2026-08-23 之后再没被修改过**，而投递一直正常工作到 09-05 16:27Z；
2. 事件通知循环用的**不是**这个 config——
   `web_app.py:5215-5220` 传的是 `config=app.state.system_operator_bot_config`
   （`SYSTEM_BOT_TOKEN` + `SYSTEM_BOT_CHAT_ID`，两者都已设置）。

`NOTIFICATION_BOT_CHAT_ID` 为空**是一个独立的既有问题**：凡是走
`notification_bot_config` 的通知通道（如语义分歧通知）应当一直是禁用状态。
建议单独确认这是有意为之还是遗漏。

## 2. journal 里的通知/bot 报错

journal 保留区间：**2026-08-23T08:35+08 至今**，完整覆盖所求窗口。

`2026-09-06 00:00 +08`（= `2026-09-05T16:00Z`）起，
`-p warning` 且匹配 `notification|incident|bot|telegram.org` 的行：**0 条。**

**投递器没有报过任何错。** 这与第 4 节"没有可投递事件"互相印证。

### 但发现另一件事：`system_operator_bot_command_task` 崩过

```
2026-09-07T03:38:39+08:00 (= 2026-09-06T19:38:39Z)
ERROR web_app Background task system_operator_bot_command_task exited with error
Traceback … httpx/_transports/default.py → httpcore/_async/http11.py
   _receive_response_headers(timeout=…)      ← 对 Telegram 长轮询的网络异常
```

保留期内共崩溃 **4 次**：`2026-08-24T01:12:45Z`、`2026-08-25T19:56:27Z`（-08-24T19:56Z）、
`2026-08-26T02:13:32Z`、`2026-09-06T19:38:39Z`。

`_log_background_task_result` 只记录、**不重启**，所以该任务死后要等进程重启才恢复。
`2026-09-06T19:38:39Z` 那次，直到阶段 3 会话在 `2026-09-07T02:26:51Z` 部署重启
才恢复，**中间约 6 小时 48 分钟操作员 bot 命令循环是死的**。

**这是接收侧不是发送侧**：它处理的是操作员通过 bot 发回的指令/确认。
峰哥指令（`2026-09-06T23:10:28Z`）正落在这个窗口内——
但峰哥那条的失败发生在识别层（见第一/二轮），与本任务无关；
只是说明**那段时间即便有人想通过 bot 回复确认，也不会被处理**。

## 3. `runtime_incident_notification` 任务是否存活

- `/api/runtime/deployment-identity` 的 `health` 字段**不包含**该任务：
  `{event_loop, ingest_live_listener, ingest_reconcile, worker_command,
    message_processing, deepcoin_private_ws}` ——**端点无法直接回答这个问题**（可观测性缺口）。
- 间接证据表明**存活**：
  1. 启动条件满足（`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED=true`，
     `web_app.py:5206-5213`）；
  2. 整个保留期内 journal **从未出现**
     `runtime_incident_notification_task exited with error`（对比：
     `system_operator_bot_command_task` 出现过 4 次），说明它从没崩过；
  3. 第 4 节显示 id>272 的可投递事件 196/196 全部 delivered，零遗漏。
- 未找到"上次投递时间"的健康端点；该信息只能从
  `runtime_incidents.notified_at` 取（见第 4 节）。

## 4. `runtime_incidents` 分布 —— 关键数据

```
notification_status:  pending 1868 | delivered 199        总计 2067（id 1..2067）
最早 pending : id=4     management_recovery_required  2026-07-21 05:25:55
最新 pending : id=2067  context_worker_exhausted      2026-09-07 04:03:20
最后 delivered: id=2064 severe_protection_incident
                created 2026-09-05 16:26:13 / notified 2026-09-05 16:27:07
```

### 1868 条 pending 的构成 —— 没有一条是"投递失败"

| incident_type | pending | 是否在 `TELEGRAM_TYPES` |
|---|---:|---|
| `context_worker_exhausted` | 1466 | **否** |
| `severe_protection_incident` | 256 | 是 |
| `provider_retry_exhausted` | 124 | **否** |
| `management_recovery_required` | 12 | **否** |
| `monitor_adapter_failure` | 9 | **否** |
| `notification_delivery_failure` | 1 | **否** |

- **1612 条**类型根本不在 `TELEGRAM_TYPES` 白名单里 → **永远不会投递（设计如此）**
- **256 条** `severe_protection_incident` 全部是 **id 8..269**，
  创建于 `2026-07-21 ~ 2026-08-06`，**低于 `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID=272`**
  → 这是启用该功能时**有意压制的历史积压**，也永远不会投递

**→ `pending` 在这里的含义是"从来不符合投递资格"，不是"排队卡住了"。**

### 决定性验证

```
id > 272 且类型可投递的事件： delivered 196 条，  pending 0 条，  id 273..2064
2026-09-05 16:26:13 之后创建的全部事件：
    context_worker_exhausted / pending : 3 条 (09-07 02:44:00 ~ 04:03:20)  [不可投递]
    severe_protection_incident / delivered : 1 条 (即 id 2064 本身)        [可投递，已投递]
```

**自 09-05 16:26 起，系统总共只产生了 3 条事件，全部是不可投递类型。**
投递器无事可做，不是投递失败。

### 与前两轮调查的接点（重要）

`2026-09-07T03:10:41Z` 生成的 **incident 2066**：

```
id=2066  source_kind=context_resolution_attempt  source_record_id=4865
incident_type=context_worker_exhausted   severity=high   notification_status=pending
redacted_summary={"error_type":"RuntimeError","operation":"raw_message_15201",
                  "reason_code":"context_reanalysis_exhausted", …}
```

**大镖客保本离场（raw 15201）确实生成了事件——但类型是
`context_worker_exhausted`，不在白名单里，所以用户永远收不到这条告警。**

另两条 uncertain（raw 15006、15204）**根本没有生成任何事件**。

历史上出现过的全部类型与可投递性：

| incident_type | 累计 | |
|---|---:|---|
| `context_worker_exhausted` | 1466 | 采集但从不投递 |
| `severe_protection_incident` | 412 | 可投递 |
| `provider_retry_exhausted` | 124 | 采集但从不投递 |
| `management_target_refused` | 38 | 可投递 |
| `management_recovery_required` | 14 | 采集但从不投递 |
| `monitor_adapter_failure` | 9 | 采集但从不投递 |
| `unclassified_operation_failure` | 2 | 可投递 |
| `notification_delivery_failure` | 1 | 采集但从不投递 |
| `management_partial_failed` | 1 | 可投递 |

注意 `management_recovery_required` **不在白名单**——
批次 158（binding 337）正是这个状态，所以它 09-04 卡住至今也从未告警过。

### `strategy_management_notifications`

```
总 95 行： delivered 28 | pending 67
最后一次 delivered： id=28  batch 28  partial_failed  notified 2026-07-21 15:21:03
```

**该表自 2026-07-21 起再没有成功投递过任何一条**（67 条 pending 全部堆在那里），
比 runtime_incidents 停得早得多，且与本次事件无关——是另一条长期坏掉的通道。
结合第二轮第 4 节（该表每行都必须挂 `management_batch_id`），
这条通道既覆盖不到"没建成批次"的指令，自身也已停摆一个半月。

## 修正后的整体判断

1. **告警投递没有停**，投递器健康、零报错、可投递事件 196/196 全部送达。
2. 用户"收不到告警"的真实原因：**近两天发生的所有事件都属于
   `TELEGRAM_TYPES` 白名单之外的类型**——包括大镖客那条指令产生的
   `context_worker_exhausted`（incident 2066）。
3. **1868 条 pending 不是积压**：1612 条类型不可投递 + 256 条低于 AFTER_ID 门槛，
   两者都永远不会投递。把它当成"待发送队列"会得出错误结论。
4. `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空是**独立的既有问题**
   （env 文件 08-23 后未改），使 `notification_bot_config` 恒为 `None`；
   但事件通知走的是 `system_operator_bot_config`，与本次无关。
5. `system_operator_bot_command_task` 在 `2026-09-06T19:38:39Z` 因网络异常崩溃后
   **不会自愈**，直到 `2026-09-07T02:26:51Z` 进程重启才恢复（死了约 6h48m）。
6. `strategy_management_notifications` 自 **2026-07-21** 起零投递，独立长期故障。

### 建议交用户决定（本会话不执行）

1. **最直接的一条**：把 `management_recovery_required`、`management_submit_unknown`、
   `context_worker_exhausted`（或至少其 `operation=raw_message_*` 的子集）
   加进 `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`——
   否则"管理指令处理失败"这类事件永远不会告警。
2. 给 `deployment-identity` 的 `health` 补上 `runtime_incident_notification`
   与 `system_operator_bot_command`，现在无法直接观测它们死没死。
3. 给崩溃的后台任务加自愈重启（现在只记录不重启，一次网络抖动就静默死到下次部署）。
4. 确认 `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空是有意还是遗漏。
5. `strategy_management_notifications` 自 07-21 零投递，需单独立项。

---
---

# 补充调查（第四轮）— 管理成功率的转折点，与那批 skipped 的字段原文

调查会话 `local_98b3dc80`，2026-09-07T08:1xZ。**全部只读，未修改、未重启。**

## 先更正我自己的两个数字

1. 我第二轮写"7 条 `skipped/mimo_authoritative_not_safely_applied`"，**实际是 6 条**
   （14965、14969、14989、15059、15165、15170）。第 7 条是 15136，
   它的 automation 是 `deferred/waiting_source_deletion_exit`，不是 skipped。
2. 我第二轮把统计起点定在 09-05，据此说"管理管道两天零产出"。
   把窗口推到 08-28 后可以看到，**这个说法把一个更早、更长期的问题说成了近两天的问题**（见下）。

---

## A. 管理指令成功率的转折点：**2026-09-03T12:42:14Z**

`2026-08-28T00:00Z` 起共 `recognition_decisions` **1680** 条，
其中带真实管理意图的 **95** 条。

### 这 95 条的 automation_status/reason 分布

| automation_status / reason | 条数 |
|---|---:|
| `skipped / mimo_authoritative_not_safely_applied` | **46** |
| `completed / None` | 19 |
| `in_progress / None` | 9 |
| `uncertain / authoritative_execution_outcome_unknown` | 7 |
| `deferred / waiting_source_deletion_exit` | 4 |
| `partial_failed / None` | 4 |
| `failed / authoritative_execution_abandoned_before_side_effect` | 3 |
| `unknown / None` | 2 |
| `blocked / source_message_deleted` | 1 |

### 全部管理批次（08-28 起，共 9 条）

| batch | raw | intent | status / reason_code | created_at (UTC) |
|---|---|---|---|---|
| 151 | 14218 | move_stop_to_break_even | **succeeded** / all_position_protection_replaced | 2026-09-01 05:40:50 |
| 152 | 14382 | partial_then_break_even | blocked / protection_visibility_retry_expired | 2026-09-02 03:34:51 |
| 153 | 14424 | partial_then_break_even | resolved / history_no_submission_confirmed | 2026-09-02 09:42:00 |
| 154 | 14521 | full_exit | **succeeded** / management_close_exchange_confirmed | 2026-09-02 23:48:05 |
| 155 | 14562 | partial_take_profit | **succeeded** / all_position_protection_replaced | 2026-09-03 07:15:25 |
| 156 | 14601 | partial_take_profit | **succeeded** / all_position_protection_replaced | **2026-09-03 12:42:14** |
| 157 | 14770 | partial_then_break_even | blocked / protection_visibility_retry_expired | 2026-09-04 05:38:08 |
| 158 | 14797 | partial_take_profit | recovery_required / close_final_preflight_failed | 2026-09-04 09:22:59 |
| 159 | 15013 | partial_then_break_even | blocked / protection_price_or_size_mismatch | 2026-09-05 16:58:57 |

**最后一次成功是 batch 156，`2026-09-03T12:42:14Z`。**
此后 3 条批次全部失败（blocked / recovery_required / blocked），
`2026-09-05T16:58:57Z` 之后**再没有建成过任何批次**。

### 与部署时间线对照 —— **不支持"09-05 管理门控提交导致"的假设**

- 转折点 `2026-09-03T12:42:14Z`（最后一次成功）→ `2026-09-04T05:38:08Z`（首次失败）
  **完全落在 09-05/06 那次 `af8676dc → 0335de71`（含 `d1e3d858` / `3e8b8848`）之前**。
- 更强的一点：`skipped/mimo_authoritative_not_safely_applied` **最早出现在
  `2026-08-28T02:11:31Z`**（raw 13601），比 09-05 部署早**一周多**，
  且在 08-28 至 09-03 这段"还有成功批次"的时期里就已大量出现
  （08-28 当天就有 8 条）。**它不是新引入的。**
- 09-04 起的三次失败原因各不相同
  （`protection_visibility_retry_expired`、`close_final_preflight_failed`、
  `protection_price_or_size_mismatch`），**都是保护单可见性/价量校验类**，
  与"管理门控"无关。

**结论：管理指令的执行成功率不是在 09-05 部署后掉下来的。
可归因的转折在 09-03～09-04 之间，且三次失败集中指向保护单可见性与价量校验。**
（本文件只陈述观测，不断定因果。）

---

## B. 那 6 条 `skipped/mimo_authoritative_not_safely_applied` 的字段原文

### 关键：两个"状态"在**两个不同的对象**上

指挥会话问"这个跳过只在 `recognition.status == '识别失败'` 时触发，
而你看到的是'非策略'，是哪个对象的哪个字段"。答案是：

| 对象.字段 | 取值 |
|---|---|
| `recognition_decisions.authoritative_status` | **`'非策略'`** |
| `message_recognitions.status` | **`'识别失败'`** ← 代码判定用的是这个 |
| `message_recognitions.reason` | **`'MiMo lifecycle event could not be applied safely'`** |

**六条完全一致，无一例外。** 所以那个跳过分支确实被满足了——
但满足它的是 `message_recognitions.status`，不是我第二轮引用的
`recognition_decisions.authoritative_status`。两者同时存在、取值不同。

而 `reason` 是 `'MiMo lifecycle event could not be applied safely'`，
**不是 `management_fraction_invalid`**（见下一节）。

### 六条的公共字段（完全相同）

```
authoritative_model    = 'mimo-v2.5'
auxiliary_status       = None
agreement_status       = 'review_disabled'
comparison_status      = 'completed'
comparison_error       = None
disagreement_severity  = None
automation_status      = 'skipped'
automation_reason      = 'mimo_authoritative_not_safely_applied'
message_recognitions.status = '识别失败'
message_recognitions.reason = 'MiMo lifecycle event could not be applied safely'
message_recognitions.engine = 'mimo-v2.5'   prompt_version = None
```

### 逐条：上下文解析 / lifecycle_event / 正文原文

| raw | chat / msg | ctx.decision | ctx.management_action | target_thread_ids | lifecycle_event.event_type | lifecycle_event.management_action | target_lifecycle_id | conf |
|---|---|---|---|---|---|---|---|---|
| 14965 | -1003095914903 / 3279 | `manage_thread` | `None` | `[458]` | `position_update` | `None` | 1089 | 0.9 |
| 14969 | -1003095914903 / 3281 | `manage_thread` | `hold_update` | `[458]` | `position_update` | `hold_update` | 1089 | 0.8 |
| 14989 | -1003095914903 / 3283 | `manage_thread` | `hold_update` | `[458]` | `position_update` | `hold_update` | 1089 | 0.9 |
| 15059 | -1002199068560 / 13790 | `exit_thread` | `None` | `[444]` | `exit_position` | `None` | 1075 | 0.85 |
| 15165 | -1003095914903 / 3295 | `exit_thread` | `exit_full` | `[460]` | `exit_position` | `exit_full` | 1091 | 0.95 |
| 15170 | -1002337721508 / 10400 | `manage_thread` | `hold_update` | `[465]` | `position_update` | `hold_update` | 1096 | 0.9 |

正文原文（供判断校验器为何判无效）：

```
14965 (09-05 03:19:43): 兄弟们，跟上节奏，直接进场‼️
                        🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️
14969 (09-05 05:44:01): 🔥窄幅震荡，空单继续持有…🔥
14989 (09-05 11:59:54): 🔥行情持续横盘！
                        🔥现目前在进场价附近…！
                        🔥空单继续持有即可…
15059 (09-06 04:24:03): 周日睡醒发现被针对了家人们，我们周五合约viP策略和直播都讲到了78588加仓做多，
                        而且还特别说挂这个点位的人特别多要分开挂。
                        今天睡醒涨到了8万U，赌狗和求稳的伙伴挂单80388全部卖出你的多单仓位。
                        注：如果你想长拿要有跌到72000还不心慌的魄力，不然你根本拿不住多单。#BTC。
15165 (09-06 23:57:25): 🔥触发保护价79900直接出局！🔥
                        🔥空仓观望，今天重新进场即可！
15170 (09-07 00:39:29): 很多会员朋友在私信，BTC多单昨晚上还在持有的可以继续拿着不变。
```

（每条末尾还有统一的 `@Tarderfengge QQ:158241758` 转发水印，已省略。）

**六条正文里没有任何百分比符号、"一半"、分数或仓位比例数字。**
其中 4 条是"继续持有 / 保持不变"类（`hold_update`），
2 条是"全部出局"类（`exit_full` / `exit_position`），
**都不是需要 fraction 的部分平仓指令。**

### `message_instruction_items`

表共 **1003** 行；**这 6 条 raw_message_id 的行数为 0**。
即：跳过发生在建立指令项之前，这 6 条从未进入指令项阶段。

---

## 指挥会话那条决定性查询的结果：**该代码路径从未在生产触发过**

```
runtime_incidents where incident_type = 'management_fraction_rejected'   → 0 行
任何 incident_type 含 'fraction'                                          → 无
message_recognitions.reason 含 'fraction'                                 → 0 条
```

对照（说明查询本身有效、不是查错了字段）：

```
message_recognitions.status = '识别失败'            → 7401 条（全期）
recognition_decisions.authoritative_status = '识别失败' → 6927 条（全期）
```

**结论：`3e8b8848` 引入的 `validate_management_fraction_payload` 在生产上一次都没有触发过，
没有写过任何 `management_fraction_rejected` 事件，也没有产生过
`management_fraction_invalid` 这个 reason。**
这 6 条走的是同一个"识别失败"分类下的**另一个分支**，
reason 是 `'MiMo lifecycle event could not be applied safely'`。

与 A 节互相印证：该跳过原因早在 `2026-08-28` 就大量存在，
而 `3e8b8848` 是 09-05 才部署的——**时间上也不可能是它造成的。**

---

## 本轮结论汇总

1. 管理执行成功率的转折点是 **2026-09-03T12:42:14Z**（最后一次成功 batch 156），
   **早于** 09-05/06 的 `af8676dc → 0335de71`（含 `d1e3d858` / `3e8b8848`）。
2. `skipped/mimo_authoritative_not_safely_applied` **不是新问题**：
   最早 `2026-08-28T02:11:31Z`，08-28 起 95 条管理意图里占 46 条，
   且在"还有成功批次"的时期就已大量出现。
3. 那个跳过由 **`message_recognitions.status = '识别失败'`** 触发
   （不是 `recognition_decisions.authoritative_status`，后者是 `'非策略'`），
   reason 为 **`'MiMo lifecycle event could not be applied safely'`**。
4. **`management_fraction_rejected` 事件 0 条，`fraction` 相关 reason 0 条——
   `3e8b8848` 的校验器从未触发。** 这条假设可以排除。
5. 6 条被跳过的消息全是"继续持有"或"全部出局"，**没有一条涉及仓位比例**，
   本来也走不到 fraction 校验。
6. 09-04 起三次批次失败的 reason_code 分别是
   `protection_visibility_retry_expired`、`close_final_preflight_failed`、
   `protection_price_or_size_mismatch`，**集中在保护单可见性与价量校验**，
   与管理门控无关。这是我认为下一步最值得查的方向。

---
---

# 补充调查（第五轮）— 三次批次失败的判定现场、pending TPSL 的 sz、六条被跳过的目标

调查会话 `local_98b3dc80`，2026-09-07T08:3xZ。**全部只读，未修改、未重启。**

## 先更正我自己在阶段 4 写下的一句话

我在阶段 4 `findings.md` 第 2 条写过
**"REST 的 pending TPSL 行不带 posId，也不带有效 sz（`sz` 恒为 `0`）"**。
**后半句作为对该端点的一般性结论是错的**，我当时只看了今天这一批样本就外推了。

真实语义见下面第 2 节：**`sz = "0"` 表示"该保护单覆盖整个仓位"**，
系统自己就是这么建模的（`row_snapshots[].full_position = true`）。
`sz` 非 0 时表示按数量分档保护。09-04 的生产快照里两种同时存在。

（前半句"不带 posId"仍然成立，且 09-01～09-04 的历史快照同样没有 posId，
所以那不是新变化——见第 2 节。）

---

## 1. 批次 157 / 158 / 159 的判定现场

### 批次 157 — `protection_visibility_retry_expired`（2026-09-04 05:38:08）

raw 14770，binding 336，lifecycle 1072，`partial_then_break_even`，fraction 0.5。

```json
target_snapshot_json = {
  "blocked_reason": "protection_missing_cancellable_order_id",
  "execution_mode": "live",
  "identity": {"execution_binding_id": 336,
               "strategy_instance_id": "deepcoin:-1002337721508:10342:BTC:short",
               "target_lifecycle_id": 1072},
  "positions": []
}
management_contract_json = null   ；strategy_management_legs 无行
```

**`positions` 为空数组，且真正的阻塞原因是
`protection_missing_cancellable_order_id`**——不是价格或数量不一致，
而是**根本没找到可撤销的保护单单号**。`reason_code` 记的
`protection_visibility_retry_expired` 是重试耗尽后的外层结论，
内层 `blocked_reason` 才是判定点。

### 批次 158 — `close_final_preflight_failed`（2026-09-04 09:22:59）

raw 14797，binding 337，lifecycle 1074，`partial_take_profit`，fraction 0.5。
这是三条里**唯一带完整快照**的。

账本侧（`positions[0]`，leg 579）：

```json
{"pos_id": "1001125123045253", "side": "long", "instrument_id": "BTC-USDT-SWAP",
 "size": "5", "trusted_start_size": "5", "target_remaining_size": "3",
 "avg_entry_price": "80490", "min_quantity": "1", "quantity_step": "1"}
strategy_management_legs#139: preflight_size="5"  planned_close_size="2"
```

交易所侧（同一快照的 `protection.row_snapshots`，4 张保护单）：

| order_id | purpose | size | trigger_price | full_position |
|---|---|---:|---|---|
| 1001125123045252 | stop_loss | **10** | 79200 | false |
| 1001125123048630 | stop_loss | **0** | 79041.6 | **true** |
| 1001125123049649 | take_profit | 3 | 81800 | false |
| 1001125123049805 | take_profit | 2 | 82500 | false |

**不一致的字段是"数量"：账本认为该仓位 `size = 5`，
而交易所的止损单 `1001125123045252` 覆盖 `10`。**

第三方佐证（今天只读拉取的 `list_position_history`）：
posId `1001125123045253` 的历史行是 `"pos": "10", "closePos": "10"`。
**即交易所侧该仓位确实是 10，账本记的 5 是错的（恰好是一半）。**

所以 `close_final_preflight_failed` 的现场是：
**预平仓前的最终校验发现账本尺寸（5）与交易所实际（10）对不上而拒绝执行。**
（本文件只陈述字段差异，不断定"5 从哪来"。）

### 批次 159 — `protection_price_or_size_mismatch`（2026-09-05 16:58:57）

raw 15013，binding 339，lifecycle 1088，`partial_then_break_even`，fraction 0.5。

```json
target_snapshot_json = {
  "blocked_reason": "protection_price_or_size_mismatch",
  "identity": {"execution_binding_id": 339,
               "strategy_instance_id": "deepcoin:-1003048800035:4501:BTC:long",
               "target_lifecycle_id": 1088},
  "positions": []
}
management_contract_json = null ；strategy_management_legs 无行
```

**同样 `positions` 为空**，快照里没有保留被比对的具体价格/数量对，
所以**这一条无法从账本还原"哪个字段不一致"**——
快照在判定失败时只落了结论，没落对比材料。这是可观测性缺口，
与第一轮记的"fail-closed 冻结不留归因信息"是同一类问题。

**三条的共同点：157 和 159 的 `positions` 都是 `[]`，`management_contract_json` 都是 null，
且都没有 leg 行——即它们在"组装可管理仓位清单"这一步就失败了，
根本没进入下单前的比对。只有 158 走到了 preflight 并留下了可比对的数字。**

---

## 2. pending TPSL 的 `sz`：**没有证据表明 Deepcoin 改了该端点**

### 三个时间点的原始行

**(a) 2026-09-05 实验证据**
（`eth-rest-ws-tpsl-short-no-clordid-test-20260905/live-ab734b3900f6/raw.jsonl`，
路径 `/deepcoin/trade/trigger-orders-pending`）：

```json
{"instId":"ETH-USDT-SWAP","ordId":"1001125145471183","side":"buy","posSide":"short",
 "sz":"0.1","triggerPx":"0","triggerOrderType":"TPSL",
 "slTriggerPrice":"2488.78","tpTriggerPrice":"2468.78",
 "closeSLTriggerPrice":"","closeTPTriggerPrice":"","cTime":"1788635962000"}
```

**(b) 2026-09-04 09:22:59 生产快照**（批次 158，同端点、生产自己的读取）：

```json
{"ordId":"1001125123045252","sz":"10","slTriggerPrice":"79200",
 "closeSLTriggerPrice":"","tpTriggerPrice":"0","closeTPTriggerPrice":""}
{"ordId":"1001125123048630","sz":"0", "slTriggerPrice":"79041.6",
 "closeSLTriggerPrice":"79041.6","tpTriggerPrice":"0","closeTPTriggerPrice":"0"}
{"ordId":"1001125123049649","sz":"3", "slTriggerPrice":"0",
 "closeSLTriggerPrice":"0","tpTriggerPrice":"81800","closeTPTriggerPrice":"81800"}
```

**(c) 2026-09-07 今天只读拉取**：

```json
{"ordId":"1001125157891310","sz":"0","triggerPx":"0","side":"sell","posSide":"long",
 "triggerOrderType":"TPSL","slTriggerPrice":"2430","closeSLTriggerPrice":"2430",
 "tpTriggerPrice":"0","closeTPTriggerPrice":"0"}
```

### 逐字段比对

| 字段 | 09-05 实验 | 09-04 生产 | 09-07 今天 | 判读 |
|---|---|---|---|---|
| **键集合** | 23 键 | 23 键 | 23 键 | **完全相同，无字段增删** |
| `posId` | 不存在 | 不存在 | 不存在 | **一直没有，不是新变化** |
| `sz` | `"0.1"` | `"10"` / `"0"` / `"3"` / `"2"` | `"0"` | 见下 |
| `slTriggerPrice` | 2488.78 | 79200 / 79041.6 / 0 | 2430 | 按单据用途不同 |
| `closeSLTriggerPrice` | `""` | `""` / 79041.6 / 0 | 2430 | 按单据用途不同 |
| `tpTriggerPrice` | 2468.78 | 0 / 0 / 81800 | 0 | 按单据用途不同 |

### 结论：**不是 API 变了，是保护单的构成变了**

关键证据是 **09-04 那一批里 `sz` 同时出现 `10`、`0`、`3`、`2` 四种值**，
而系统对它们的解码是：

```
sz="10" → full_position=false   （按数量分档的止损）
sz="0"  → full_position=TRUE    （覆盖整个仓位的止损）
sz="3"  → full_position=false   （分档止盈）
sz="2"  → full_position=false   （分档止盈）
```

**`sz="0"` 不是"缺数量"，而是"全仓保护"这一语义**，且 09-04 就已存在。
今天的样本全是 `sz="0"`，只说明**当前挂着的保护单恰好都是全仓止损**，
没有任何按数量分档的止盈单。

**一个可以解释"为什么今天没有分档止盈单"的既有缺陷（假设，未验证）**：
阶段 0 结论第 2 条记录的 `convergence_pending_alias_conflict` 全局否决，
使三档止盈收敛无法产出——而分档止盈正是 `sz` 非 0 的那类单据。
若该否决自 09-03/04 起持续生效，就会呈现"分档单消失、只剩全仓止损"的现象。
**这是相关性推测，需要代码/数据进一步验证，本文件不作因果断定。**

09-05 实验那条 `sz="0.1"` 是实验脚本用**显式数量**下的单，与生产路径不同，
不能与生产样本直接对比。

---

## 3. 六条 "MiMo lifecycle event could not be applied safely" 的目标状态

### `lifecycle_events` 表**不存在**

```
lifecycle_events            : DOES NOT EXIST
strategy_lifecycle_events   : DOES NOT EXIST
（库里只有 strategy_lifecycles 一张与 lifecycle 相关的表）
```

**所以"尝试写入被拒"没有留下任何记录**——这条路径完全不可审计。

### 四个目标 lifecycle 的状态

| lifecycle | thread | symbol/side | 决策时 status | `execution_binding_id` | entered_at | exited_at |
|---|---|---|---|---|---|---|
| **1089** (14965/14969/14989) | 458 | BTC short | `entered` | **NULL** | 2026-09-05 03:20 | 2026-09-06 23:46 |
| **1075** (15059) | 444 | BTC long | `entered` | **NULL** | 2026-09-04 04:29 | 2026-09-07 03:44 |
| **1091** (15165) | 460 | BTC short | `entered` | **NULL** | 2026-09-06 03:02 | — |
| **1096** (15170) | 465 | BTC long | `entered` | **NULL** | 2026-09-07 00:37 | — |

**四个目标 lifecycle 的 `execution_binding_id` 全部为 NULL。**

也就是说，这六条管理指令指向的都是**没有任何交易所绑定的策略线程**——
它们在 `strategy_lifecycles` 里是 `entered`，但账本里没有对应的
`execution_bindings` 行，交易所上也就没有可管理的仓位。

对照：真正持仓的 binding 340/341/342 对应的 lifecycle 是 1095/1097/1100，
**都不在这六条的目标里**。

（`_apply_lifecycle_event_decision` 返回 False 的具体判定条件由指挥会话核对；
本文件只提供这四个 lifecycle 在决策时刻的字段原文。
"binding 为 NULL"与"拒绝"之间的因果关系，我不做断定。）

---

## 本轮结论

1. **157 的真实判定点是 `protection_missing_cancellable_order_id`**
   （找不到可撤销的保护单号），不是价量不一致；`positions` 为空。
2. **158 是唯一留下可比对数字的一条**：账本 `size=5` vs 交易所止损单 `sz=10`，
   而交易所 position history 显示该仓位确实是 `10`——**账本尺寸偏小一半**。
3. **159 的快照只落了结论没落对比材料**，无法从账本还原不一致字段（可观测性缺口）。
4. **没有证据显示 Deepcoin 改了 trigger-orders-pending**：键集合三个时点完全一致，
   `posId` 一直缺席，`sz="0"` 在 09-04 就存在且语义是"全仓保护"。
   我阶段 4 那句"`sz` 恒为 0"是从单批样本过度外推，**在此更正**。
5. **六条被跳过的管理指令，目标 lifecycle 的 `execution_binding_id` 全为 NULL**，
   且 `lifecycle_events` 表不存在，拒绝过程零留痕。

### 我认为下一步最值得查的

- **账本尺寸为什么是交易所的一半**（158 的 5 vs 10）。binding 337 有两条 entry leg
  （579 pos …045253、580 pos …126414222），两个仓位在 09-04 13:07:38Z 被同一次止损
  全平，pos 分别是 10 和 14。若账本把某个 leg 的尺寸记成了一半，
  会同时解释 preflight 失败与后续账本落后。
- **为什么这六条管理指令都指向没有 binding 的 lifecycle**：
  是上下文解析选错了线程，还是这些线程本就不该被管理。

---
---

# 补充调查（第六轮）— "账本一半"的真相，与那六条所在群的交易模式

调查会话 `local_98b3dc80`，2026-09-07T08:5xZ。**全部只读，未修改、未重启。**

## 先更正我上一轮的一个推断

我第五轮写"账本记的 5 是错的（恰好一半）"。**这个推断是错的。**
查到 TP1 的成交记录后可以确定：**批次 158 快照里的 `size = "5"` 是正确的**，
错的是我把 `position_history` 的累计 `pos=10` 当成了当时的在仓数量。
真正不一致的是**另一个字段**——见下。

---

## 1. binding 337 的"一半"问题：不是账本错，是止损单没跟着缩

### 下单时的账本值（与交易所回执一致，无分歧）

| | leg 579 | leg 580 |
|---|---|---|
| `request_json.sz` | **`"10.0"`** | **`"14.0"`** |
| `clOrdId` | TKDBK4495E1 | TKDBK4495E2 |
| 回执 | `code=0 sCode=0 ordId=1001125120426454` | `code=0 sCode=0 ordId=1001125120426471` |
| `pos_id` | 1001125123045253 | 1001125126414222 |
| 归属证据 | `direct_pos_id`（tier 0） | `prior_authoritative_position_audit` |

`execution_bindings.337.order_id = "1001125120426454,1001125120426471"`。
**账本从头到尾记的都是 10 和 14，没有"记成一半"。**

leg 579 的保护账本（`position_protection_ledger`）也自洽：

| order_id | purpose | size_text | trigger | status |
|---|---|---:|---|---|
| 1001125123045252 | stop_loss | **10** | 79200 | verified |
| 1001125123048630 | stop_loss(backup) | null | 79041.6 | verified |
| 1001125123049529 | take_profit | **5** | 81100 | **protection_missing** |
| 1001125123049649 | take_profit | **3** | 81800 | verified |
| 1001125123049805 | take_profit | **2** | 82500 | verified |

三档止盈 5 + 3 + 2 = **10**，正是仓位全量。
（`position_protection_legs.planned_size` 记的 50.0/30.0/20.0 是**百分比**，
换算到 10 的仓位就是 5/3/2，两张表口径不同但结果一致。）

### 关键：TP1 在批次 158 之前 **48 分钟就已经成交了**

只读拉取 `list_trigger_order_history`：

```json
{"ordId":"1001125123049529","ordType":"TPSL","sz":"5","triggerPx":"81100",
 "tpTriggerPrice":"81100","triggerTime":"1788510883","uTime":"1788510883000"}
```

`triggerTime = 1788510883` = **2026-09-04T08:34:43Z**（已触发，非 0）。

时间线：

| 时刻 (UTC) | 事件 | 在仓数量 |
|---|---|---:|
| 09-04 08:05:34 | leg 579 入场成交，pos 1001125123045253 建仓 | **10** |
| 09-04 08:34:43 | **TP1（sz 5 @81100）触发成交** | **10 → 5** |
| 09-04 08:34:51 | 系统生成 `severe_protection_incident` id 2057（**8 秒后**，已投递） | 5 |
| 09-04 09:22:59 | **批次 158 快照：`size="5"`** ← **正确** | 5 |
| 09-04 13:07:38 | 止损 1001125123045252 触发，平掉剩余 | 5 → 0 |

`position_history` 的 `pos:"10" / closePos:"10"` 是**该仓位一生的累计**
（TP1 平 5 + 止损平 5），不是批次 158 时刻的在仓量。我上一轮读错了这一点。

### 真正不一致的字段：**止损单的数量没有跟着缩**

批次 158 时刻，交易所上：

```
止损 1001125123045252  sz = 10   ← 仍是建仓时的全量
实际在仓                    5    ← TP1 成交后只剩一半
TP1  1001125123049529          ← 已成交，账本标 protection_missing（识别正确）
TP2  1001125123049649  sz = 3
TP3  1001125123049805  sz = 2
```

**止损覆盖 10，仓位只有 5——保护量是仓位的两倍。**
`close_final_preflight_failed` 就发生在这里：
系统在再做一次部分止盈之前的最终校验中发现保护量与在仓量对不上而拒绝执行。
**这是正确的 fail-closed，不是账本错误。**

（`position_protection_ledger` 里止损那行的 `size_text` 至今仍是 `"10"`，
也就是说 TP1 成交后**账本侧的止损数量同样没有更新**——
账本与交易所在这一点上是一致的，两边都停在 10。）

### 批次归属与"binding 337 有没有被减过仓"

| batch | binding | lifecycle | intent | status |
|---|---|---|---|---|
| 151 | 322 | 1041 | move_stop_to_break_even | succeeded |
| 152 | 324 | 1050 | partial_then_break_even | blocked |
| 153 | 325 | 1054 | partial_then_break_even | resolved |
| 154 | 327 | 1060 | full_exit | succeeded |
| **155** | **330** | 1063 | partial_take_profit | succeeded |
| **156** | **328** | 1061 | partial_take_profit | succeeded |
| 157 | 336 | 1072 | partial_then_break_even | blocked |
| **158** | **337** | 1074 | partial_take_profit | **recovery_required** |
| 159 | 339 | 1088 | partial_then_break_even | blocked |

- **09-03 成功的两次减仓（155/156）属于 binding 330 与 328，与 337 无关。**
- **binding 337 只被一个批次针对过，就是失败的 158。**
  即：**337 从未被任何管理批次成功减过仓**，它的 10 → 5 完全是
  交易所侧 TP1 自己触发的，不是系统管理动作的结果。

### leg 580 的止盈从未挂上

`position_protection_legs` 中 leg 580 的三档止盈（852/853/854）
`exchange_order_id = null`、`status = protection_recovery_pending`——
**这三张止盈单从来没有在交易所建立**。
leg 580 只有 primary_stop（sz 14）与 backup_stop 两张保护单。
这与第五轮"今天只剩全仓止损、没有分档止盈"的观察方向一致。

---

## 2. 那六条所在群的交易模式

### 先更正一处前提

`trading_mode` **不在数据库里**。库里 92 张表没有任何按群的模式表：
`trading_settings` 只有 4 个键（`global` / `low_confidence_group_exit_cutoff` /
`entry_revision_v2_activation` / `entry_revision_exchange_authority`），
其 `global` 值里也没有 `trading_mode`；`sources` 表只有
`id/telegram_sender_id/chat_id/username/display_name/custom_label/is_active/created_at`。

实际来源是 **`config/groups.yaml`**（`group_config.py` 读取，默认
`trading_mode="notify_only"`）。数据库只在写设置时经
`group_config.py:133` 把 `auto_trade_enabled` 翻译成 `trading_mode` **写回 yaml**。

### 实测值（`config/groups.yaml`，共 34 个群）

| chat_id | 群名 | `trading_mode` | `ai_strategy_enabled` | 本次相关 |
|---|---|---|---|---|
| -1003095914903 | 欧阳火箭滚仓班🚀 | **`notify_only`** | true | 被跳过 **4 条**（14965/14969/14989/15165） |
| -1002199068560 | 三马哥会员群 | **`notify_only`** | true | 被跳过 **1 条**（15059） |
| -1002337721508 | 比特币陈哥会员群 | **`auto_trade`** | true | 被跳过 **1 条**（15170） |
| -1002409877375 | 峰哥高级会员群 | `auto_trade` | true | 对照（峰哥止盈） |
| -1003048800035 | 大镖客 | `auto_trade` | true | 对照（大镖客保本离场） |

### 结论：**6 条里 5 条是设计内行为，1 条不是**

- **5 条来自 `notify_only` 群**（欧阳 4 条 + 三马哥 1 条）。
  这些群本来就只提醒不交易，**不会建立 `execution_bindings`**——
  这正好解释了第五轮那个发现：目标 lifecycle 1089 / 1091（欧阳）、1075（三马哥）
  的 `execution_binding_id` 全是 NULL。
  对这些群的管理指令"跳过"是**设计内的正确行为**。
  问题只在于**语义误导**：它被记成 `message_recognitions.status = '识别失败'`
  + `automation_reason = 'mimo_authoritative_not_safely_applied'`，
  读起来像"识别失败/无法安全应用"，实际是"这个群不交易"。
- **1 条（15170，陈哥群，`auto_trade`）不能用 notify_only 解释。**
  它的目标 lifecycle 1096 的 `execution_binding_id` 同样是 NULL，
  但该群是 auto_trade——说明**这条策略入场本身没有建成 binding**，
  需要单独查（lifecycle 1096 entered 于 09-07 00:37，
  来源消息 raw 15169 "陈哥合约交易策略 BTC，80000附近，做多"）。

峰哥与大镖客两个群都是 `auto_trade`，**所以那两条指令的失败与群模式无关**，
维持前几轮的结论不变。

---

## 本轮结论

1. **账本没有"记成一半"**：leg 579/580 从下单到保护账本一路都是 10 / 14。
   我上一轮的推断作废。
2. **批次 158 的 `size="5"` 是对的**——TP1（sz 5 @81100）已在
   `2026-09-04T08:34:43Z` 触发成交，比批次早 48 分钟。
3. **真正不一致的是止损单数量**：TP1 成交后仓位剩 5，
   止损 1001125123045252 仍是 `sz=10`，保护量为在仓量的两倍，
   `close_final_preflight_failed` 是正确的 fail-closed。
   账本侧该行的 `size_text` 同样停在 "10"，两边一致地没有缩量。
4. **binding 337 从未被任何管理批次成功减过仓**（只被失败的 158 针对过）；
   09-03 成功的 155/156 属于 binding 330 / 328。
5. **`trading_mode` 来自 `config/groups.yaml`，不在数据库里**（更正前提）。
6. **六条被跳过里 5 条来自 `notify_only` 群，属设计内行为**，
   只是被记成"识别失败"造成语义误导；
   **剩下 1 条（15170，陈哥，auto_trade）不能这样解释**，需单独查。

### 建议下一步

- **止损单在部分止盈成交后为什么没有缩量**：这是 158 失败的直接原因，
  也可能是 157（`protection_missing_cancellable_order_id`）与
  159（`protection_price_or_size_mismatch`）的同类根因。
  注意系统在 TP1 成交后 8 秒就产生了 `severe_protection_incident` id 2057
  **且已投递**——告警发出来了，但保护量没有被自动修正。
- **lifecycle 1096（陈哥，auto_trade）为什么没有 binding**：
  这是六条里唯一不能用群模式解释的一条。
- 把"该群为 notify_only"这一跳过原因**与真正的识别失败区分开**，
  现在两者共用 `识别失败` + `mimo_authoritative_not_safely_applied`。

---
---

# 补充调查（第七轮，收尾）— 一次自动交易群的静默入场失败，与止损未缩量的收敛路径

调查会话 `local_98b3dc80`，2026-09-07T09:0xZ。**全部只读，未修改生产、未重启。**

## 1. lifecycle 1096（陈哥群，auto_trade）为什么没有 binding

### 链路逐步

raw **15169**，chat `-1002337721508`，msg 10399，`2026-09-07T00:35:10Z`：

```
⚠️⚠️⚠️⚠️⚠️⚠️
陈哥合约交易策略
BTC，80000附近，做多
止损预计：77800
止盈预计：81400-83500
⚠️⚠️⚠️⚠️⚠️⚠️
```

| 步骤 | 结果 |
|---|---|
| `message_processing_jobs` 3408 | **succeeded**，`worker_completed`，00:35:11 |
| `message_recognitions` 15164 | **`是策略`**，理由"提供了完整的新开仓策略参数…符合新开仓识别标准" |
| `recognition_decisions` 15166 | `authoritative_status=是策略`，**`automation_status=deferred`**，**`automation_reason=waiting_source_deletion_exit`** |
| `authoritative_execution_attempts` 342 | `status=succeeded`，**`exchange_effect=not_started`**，automation 同上 |
| `signal_candidates` 2210 | 已建，BTC/long/entry_signal，confidence 0.95，`review_status=pending` |
| `message_instruction_items` **988** | `instruction_kind=entry`，**`status=pending`**，`error_json=null`，`last_progress_at=null`，`escalation_state=null` |
| `execution_events` | **零行** |
| `runtime_incidents` | **零行** |
| `execution_bindings` | **未创建**（该群最近一个 binding 是 336，09-03） |
| `strategy_lifecycles` 1096 | `entered`、`entry_price_actual=80115.25`、**`execution_binding_id=NULL`** |

**识别成功、意图完整、指令项已建，然后停在 `pending` 再没动过——
零事件、零告警、零通知。** 到调查时已 **8 小时 30 分钟**。

### 这不是孤例：29 条指令项卡在 `pending`

`message_instruction_items` 状态分布：
`succeeded 595 / submitted 257 / failed 108 / **pending 29** / unknown 15`。

**29 条 `pending` 最早可追到 `2026-07-22 05:16:25`**，且**全部 `last_progress_at=null`、
`escalation_state=null`**——从未被任何流程推进过，也从未升级告警。

近期卡住的（按时间倒序，节选）：

| item | raw | kind | strategy_instance | 创建于 |
|---|---|---|---|---|
| 1002 | 15228 | management | `-1002199068560:13805:ETH:long` | 09-07 05:36:41 |
| 1001 | 15227 | entry | `-1002199068560:13805:ETH:long` | 09-07 05:35:17 |
| 998 | 15209 | entry | `-1002960443256:4550:ZEC:short` | 09-07 04:14:59 |
| 997 | 15207 | entry | `-1002960443256:4548:ZEC:short` | 09-07 04:10:09 |
| 993 | 15194 | entry | `-1003095914903:3297:BTC:short` | 09-07 03:03:13 |
| **988** | **15169** | **entry** | **`-1002337721508:10399:BTC:long`** | **09-07 00:35:33** |
| 985 | 15136 | management | `-1002337721508:10382:BTC:long` | 09-06 15:01:11 |
| 975 | 14944 | entry | `-1002337721508:10382:BTC:long` | 09-05 00:51:40 |
| 943 | 14558 | management | `-1002337721508:10315:BTC:long` | 09-03 06:55:17 |

**仅陈哥群（auto_trade）就有 4 条卡住**（988 / 985 / 975 / 943）。

### 那个 defer 原因在等什么

`recognition_decisions` 里 `automation_reason='waiting_source_deletion_exit'`
全期共 **21 条**，`2026-08-03 14:34:14` ~ `2026-09-07 05:36:41`。

但 `source_message_deletion_exits` 表里**最近的行全部 `state='succeeded'`**
（如 id 239/238/237/236，`last_reason` 为 `non_strategy_or_unlinked` 或
`exchange_flat_confirmed`）——**它等待的那个删除退出流程本身已经完成了，
但被 defer 的指令项没有被唤醒重跑。**

> 观测到的是"defer 之后没有恢复执行的路径被触发过"；
> 具体是没有恢复循环、还是恢复条件不满足，需要代码侧确认。本文件不作因果断定。

**用户影响：这是一次自动交易群的入场静默失败。** 消息被正确识别为完整开仓策略，
但没有下单、没有事件、没有告警，lifecycle 却被标成 `entered`
（`entry_price_actual=80115.25`），从界面上看像是已入场。

---

## 2. TP1 成交后止损为什么没缩量：**路径存在、跑了、拒绝了、也告警了**

### 负责的循环是 `trigger_take_profit_convergence`

`trigger_take_profit_convergences` **id 222**（binding 337 / leg 579 / pos …045253）：

```json
desired_take_profits_json = [{"allocation_pct":"50","price":"81100"},
                             {"allocation_pct":"30","price":"81800"},
                             {"allocation_pct":"20","price":"82500"}]
created_at   = 2026-09-04 03:49:26
reserved_at  = 2026-09-04 08:05:46      ← 建三档止盈
status       = "conflicted"
reason_code  = "convergence_partial_position_unexplained"
completed_at = 2026-09-04 08:34:51      ← TP1 成交后 8 秒
response_json = null   error_json = null
```

它确实建了三档止盈（`position_mutation_intents` 631/632/633，
idempotency_key `tp-convergence:222:set:{0,1,2}`，
sz 分别 5 / 3 / 2，全部 `status=confirmed`，回执 `sCode=0`）。

### TP1 成交后它做了什么

**`2026-09-04T08:34:51Z`（TP1 触发后 8 秒），收敛被判为 `conflicted`，
原因码 `convergence_partial_position_unexplained` —— "仓位被部分减少但无法解释"。**

连带产生：

| 记录 | 内容 | 投递 |
|---|---|---|
| `position_protection_incidents` **383** | `protection_missing`，evidence `{"order_id":"1001125123049529"}`（即 TP1） | `delivery_status=pending`，**`notified_at=null`** |
| `runtime_incidents` **2057** | `severe_protection_incident`，**severity=critical**，source `tp-convergence-222-conflicted-convergence_partial_position_unexplained`，summary `{"component":"position_protection","reason_code":"convergence_partial_position_unexplained","source_status":"recovery_required"}` | **`delivered` @ 08:56:49** |
| `position_protection_incidents` **384**（09-04 09:23:01，批次 158 失败后 2 秒） | `backup_stop_blocked`，evidence `{"reason_code":"backup_management_in_progress"}` | `delivery_status=pending`，**`notified_at=null`** |

### 结论：**有路径，但它的设计是 fail-closed 而不是自动修正**

- 收敛循环**检测到了**仓位与保护不匹配（8 秒内）；
- 它**拒绝**继续（`conflicted`），**没有**去缩止损数量；
- 它**发出了 critical 告警并成功投递**（incident 2057）。

所以不是"没有这个收敛路径"，也不是"被 `convergence_pending_alias_conflict` 拦了"
（222 的原因码是 `convergence_partial_position_unexplained`，不是 alias 冲突）。
**是路径在无法解释仓位变化时按设计冻结，把处置交给人，而没有人处置。**

`execution_events` 里 binding 337 在 TP1 成交后只剩 **1 行**
（09-04 12:36:58 给 **leg 580** 建 backup stop），
**对 leg 579 / pos …045253 再没有任何交易所动作**——止损始终停在 `sz=10`。

### 账本还留了两处未收敛的痕迹

- `position_take_profit_orders` 195/196/197 至今全是 `status='active'`、
  `cancelled_at=null`、`completed_at=null`——**包括已经成交的 TP1（195）**。
- `position_protection_health_observations` id 1（09-04 09:22:59）把该仓位分类为
  **`healthy_current_evidence`**，与同一时刻批次 158 的 preflight 失败结论相反。

### 更大的图景：近期收敛**没有一次成功**

`trigger_take_profit_convergences` 最新 6 条：

| id | pos_id | status | reason_code | created |
|---|---|---|---|---|
| 231 | 1001125164628529 | conflicted | `convergence_exact_leg_not_verified` | 09-07 02:38:34 |
| 230 | 1001125163581280 | conflicted | **`convergence_pending_alias_conflict`** | 09-07 01:00:03 |
| 229 | null | waiting_backup_stop | `convergence_waiting_backup_stop` | 09-07 01:00:08 |
| 228 | 1001125157891231 | conflicted | `convergence_exact_leg_not_verified` | 09-06 15:28:18 |
| 227 | 1001125135694798 | conflicted | **`convergence_pending_alias_conflict`** | 09-05 03:06:46 |
| 226 | null | waiting_backup_stop | `convergence_waiting_backup_stop` | 09-05 03:06:48 |

**六条全部 `conflicted` 或 `waiting`，零成功。**
其中 227 / 230 正是阶段 0 记录的 `convergence_pending_alias_conflict` 既有缺陷，
**至今仍在生效**。这也解释了第五轮的观察——今天交易所上只剩全仓止损、
没有任何分档止盈单。

---

## 本轮结论

1. **raw 15169（陈哥，auto_trade）是一次静默入场失败**：识别为"是策略"，
   automation `deferred/waiting_source_deletion_exit`，指令项 988 卡在 `pending`
   8.5 小时，**零事件、零告警**，而 lifecycle 1096 却显示 `entered`。
2. **这是系统性的**：29 条指令项卡在 `pending`，最早自 `2026-07-22`，
   全部 `last_progress_at=null`；仅陈哥群就有 4 条。
   `waiting_source_deletion_exit` 全期 21 次，而它等待的删除退出流程**都已 succeeded**。
3. **止损未缩量不是缺路径**：`trigger_take_profit_convergence` #222 在 TP1 成交后
   8 秒判 `conflicted / convergence_partial_position_unexplained`，
   **拒绝动作并发出 critical 告警（incident 2057，已投递）**。
   它按设计 fail-closed，把处置交给人。
4. **不是 `convergence_pending_alias_conflict` 拦的**（222 的原因码不同），
   但该缺陷在 227 / 230 上**至今仍在生效**。
5. **近期 6 次收敛全部 conflicted 或 waiting，零成功。**
6. 两条 `position_protection_incidents`（383 `protection_missing`、
   384 `backup_stop_blocked`）`delivery_status` 至今 `pending`、`notified_at=null`
   ——**这一类保护事件的通知通道也没送出去**。

### 给用户的建议（本会话不执行）

- **最紧急**：29 条 `pending` 指令项里有真实的自动交易群入场
  （988 / 15169 陈哥 BTC 多）。需要决定这些是补执行、作废还是人工处理，
  并给"defer 后未恢复"补一条超时告警——现在它完全静默。
- 收敛在 `convergence_partial_position_unexplained` 冻结后**没有人工处置入口**：
  告警发了（2057 critical 已投递），但保护量至今没被修正，且 TP1 已成交
  却仍在 `position_take_profit_orders` 里标 `active`。
- `position_protection_incidents` 的投递通道（383/384 仍 pending）需与
  `strategy_management_notifications`（自 07-21 零投递）一并排查。
