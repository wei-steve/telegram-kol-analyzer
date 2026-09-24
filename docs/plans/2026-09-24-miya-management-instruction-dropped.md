# 米娅 msg 696 的管理指令为何没有执行（2026-09-24 只读查证）

**现象**：米娅 vip会员群（`-1003825498321`，`auto_trade`）15:03 发出
「BTC现价83400附近，止盈50%，剩余仓位止损位下移至84500，做无风险持仓！」，
系统判为「非策略」，止盈与移止损**都没有执行**。仓位由用户手动平仓。

**结论**：模型两次判对，最终被第三个答案覆盖。丢失发生在上下文解析的
**结果选用**，不在识别能力。

---

## 1. 六次尝试的实际内容

`context_resolution_attempts`（raw_message_id 18895）不是「同一件事失败六次」，
而是三种不同的结果：

| id | 阶段 | 状态 | 模型给出的判断 |
|---|---|---|---|
| 6670 | initial_resolution | exhausted | `manage_thread`，但 target 不在候选集（连问 2 次都是） |
| **6671** | **reanalysis** | **completed** | **`manage_thread` / `partial_take_profit`，thread_id 688** ✅ |
| 6672 | initial_resolution | exhausted | 同 6670 |
| 6673 | initial_resolution | exhausted | 同 6670 |
| **6674** | **reanalysis** | **completed** | **线程688（lifecycle_id 1319），止盈50%，移止损至84500** ✅ |
| 6675 | initial_resolution | reanalysis_capped | **`unresolved`**，`conflict_types: [exchange_state_conflict]` |

6674 的 reason 逐字写着「执行部分止盈50%并移动止损至84500，属于部分止盈的仓位管理操作，
**与消息时间线和交换状态一致**」——**它完全判对了**。

**最终生效的是 6675**：`unresolved`，理由是「脱敏交易所状态显示该订单仍为
pending_entry、无持仓，无法确认可执行的仓位管理目标」。识别据此落为「非策略」。

## 2. 两个答案看到的交易所状态是相反的

- 6674（15:0x）：「与交换状态一致」
- 6675（15:15）：「该订单仍为 pending_entry、**无持仓**」

同一条消息、相隔几分钟，`redacted_exchange_state` 给出了相反的事实。
6675 自己记下的触发器是 `["exchange_state_changed","strategy_state_changed"]`。

而数据库里的实情是：binding 379 在 **14:46:05** 就已 `active` 且带 `pos_id`，
lifecycle 1319 在 **14:45:39** 就是 `entered`。**无持仓的描述在任何时刻都不成立**——
除非那份脱敏状态描述的是**另一条线程**。

这里有一个极易混淆的点：候选集是 `[223,283,375,657,688]`，这些是 **thread_id**；
而 `thread 688` 的 root 是 **msg 693**（当前活仓），`thread 657` 的 root 才是
**msg 688**（前一天那条 `expired`、无仓位的旧策略）。同一个数字 688 在两个 id 空间里
各指一件事，而「pending_entry、无持仓」恰好是 msg 688 那条的真实状态。

## 3. `target_outside_candidate_set` 的诊断留白

三次 initial_resolution 都因这个错误被拒：

```json
{"decision":"manage_thread","error_class":"target_outside_candidate_set","target_thread_count":1}
```

校验在 `context_resolution.parse_context_resolution_decision`：
`target_thread_ids` 里任一 id 不在候选集即拒。

**诊断记了「有 1 个 target」「它不在集合里」，却没记「它是什么」。**
所以无法判断模型填的是 693（消息号）、1319（lifecycle）还是别的。
一个连续失败六次的校验，每次都只留下「不匹配」三个字，
下一个来查的人只能靠猜——这是最该先补的一处。

## 4. 机制小结

- **一个 attempt 最多问模型 2 次**（`terminal = attempt_number == 2`），两次都被拒就 `exhausted`。
- 一旦某个 `context_fingerprint` 有 `exhausted` 行，同指纹的后续解析**直接抛错、不再问模型**。
- **reanalysis** 是上下文变化后重新问（触发器如 `exchange_state_changed`、
  `strategy_state_changed`、`reply_target_available`）。
- **cap**：`DEFAULT_MAX_REANALYSIS_PER_MESSAGE = 5` / 24 小时。到顶即 `reanalysis_capped`，
  注释写明这是为了防止「语义死胡同无限重问同一个问题」。
- 解析抛异常时识别落为「识别失败」；本例没有走这条——6675 返回的是**合法的
  `unresolved`**，所以识别正常完成，只是结论是「非策略」。

## 5. 三处值得修（均未动手）

1. **把被拒的 target 值记进诊断**（L1，纯可观测性）。没有它，同类问题每次都要重查。
2. **正确答案不该被后来的错误答案覆盖**。6674 已 `completed` 且判定明确，
   之后又跑了一次 initial_resolution 并以 `unresolved` 收尾。
   需要确认「最后写入者胜出」是否是有意设计；若是，至少要在
   一个 `completed` 的 `manage_thread` 被后续 `unresolved` 取代时留下记录。
3. **`reanalysis_capped` 应当告警到人**。一条明确的管理指令被静默丢弃了
   1.6 小时，无人知晓——与 2026-09-23 双重止损缺口同一类问题：
   机制失败了，但没有任何东西会说出来。

---

## 6. 定性：不是模型能力，不是提示词，是系统逻辑

### 首次分析（`first_pass`）两件事都判对了

它的输出完整保存在 `recognition_decisions.authoritative_payload_json`
的 `_context_resolution.first_pass` 里：

```json
{
  "recognition_result": "非策略",
  "lifecycle_event": {
    "confidence": 0.99,
    "event_type": "position_update",
    "management_action": "partial_take_profit, move_stop_to_protect",
    "reason": "BTC空单止盈50%，剩余仓位止损位下移至84500，明确为部分止盈并移动止损至保护位。",
    "side": "short", "symbol": "BTC", "stop_loss": "84500",
    "target_lifecycle_id": 1319
  }
}
```

**两个字段各司其职，而且都对**：
`recognition_result="非策略"` 是对的——这条消息不开新仓，它不是新策略；
`lifecycle_event.event_type="position_update"` 才是回答「是不是仓位管理」的字段，
它判了 `position_update`，动作、方向、标的、止损价全对，
**并且自己就给出了 `target_lifecycle_id: 1319`**——正是米娅这笔活仓的 lifecycle。

所以提示词要它判的两件事，它都判了，也都判对了。**这两处都没有问题。**

### 抹除发生在一行代码

`authoritative_recognition.py:663-674`：

```python
if (decision.confidence < 0.7 and not exact_risk_reduction_authorized) \
   or decision.decision in {"hold", "unresolved"}:
    payload.update(
        recognition_result="非策略",
        reason=decision.reason or "context resolution produced no executable action",
        strategy={},
        lifecycle_event={"event_type": "none", "confidence": 0.0},   # ← 首次分析在这里被抹平
        confidence=decision.confidence,
    )
    return replace(mimo, payload=payload, status="非策略")
```

6675 返回 `unresolved`，命中这个分支，于是那个带着 `target_lifecycle_id: 1319`、
`partial_take_profit`、`stop_loss: 84500` 的判断，被整体替换成
`{"event_type": "none"}`。下游再也看不到有过管理意图这件事。

### 这个降级本身是有意的，但它这次关错了东西

失败关闭的用意很正当：目标不确定就不要动仓位，动错仓位比不动更糟。
代码注释（A-16e）也说明作者**清楚**这一步会抹掉首次分析，所以特地把四个字段
存进 `first_pass` 留证——**但那是为了事后可查，不是为了执行**。

问题在于它**只认上下文解析这一个来源**：

- 首次分析给出了 `target_lifecycle_id = 1319`——**它根本不需要上下文解析去找目标**；
- 两次 `reanalysis`（6671、6674）已 `completed` 且判定一致；
- 只有最后一次 initial_resolution 说「不确定」，而它看到的是一份把
  `thread 688` 与 `msg 688` 混淆的交易所状态。

三个来源里两个（实为三次判定）指向同一个正确目标，降级却由第三个决定。
**这不是「不知道目标」，是「知道，但问错了人」。**

### 因此修的方向也变了

比第 5 节的三条更靠前的一条：**降级判据不该只看上下文解析的 decision**。
当首次分析已给出 `target_lifecycle_id`，且该 lifecycle 通过
`verify_lifecycle_targets` 验证为活仓时，`unresolved` 至少不应把
`lifecycle_event` 抹成 `none`——可以不执行，但要让它以「待确认的管理指令」
的形态留下来并告警，而不是伪装成「这条消息与交易无关」。

---

## 7. 与设计意图的逐条核对（用户 2026-09-24 提出）

### 7.1 首次分析的任务：记忆基本完整，可补两类

实际契约（`ai_recognition_config.py:134`）是**两个字段**分工：

- `recognition_result`：是策略 / 非策略 —— 只回答「这是不是一条**新开仓**策略」
- `lifecycle_event.event_type`：`none | entry_confirm | cancel_entry | exit_position | position_update`

用户的三分法映射过来：

| 用户说的 | 契约里的位置 | 备注 |
|---|---|---|
| 新策略 | `recognition_result = 是策略` | 标的 + 入场 + 止损（止盈可缺） |
| 策略管理 | `cancel_entry`（撤未入场的）、`entry_confirm`（「半仓入场」定量） | 用户举的两个例子都属 `entry_confirm` |
| 仓位管理 | `position_update` | 部分止盈、移止损保护、调价、继续持有 |
| **（补）完全离场** | **`exit_position`** | 与 `position_update` 分开，提示词专门强调过不可混淆 |

`ai_recognition_config.py:154` 有一条针对性的规则：
「"第一止盈位 60950 移动止损至成本价"这类表达只是部分止盈并把止损推到成本保护，
**不是全量平仓/离场；必须判定为 position_update，不能判定为 exit_position**。」

用户举的「BTC准备87500做空，半仓入场」（有标的有入场价、无止损）与
「完整策略 + 下一条『半仓入场』」两种，都是 `entry_confirm`。
**这一块今天已由另一个会话实现**（`006972e1`
"a confirmation message sizes the next entry instead of opening its own"）。

### 7.2 「首次分析明确目标就不该再做上下文分析」——判据已存在，但被别的触发条件盖过

`requires_context_resolution()` 的触发原因是一个**或**集合，其中确实有用户说的那条：

```python
if event_type != "none" and target_lifecycle_id in (None, ""):
    reasons.add("management_without_exact_target")     # 管理动作 + 没有确切目标
if len(candidates) > 1:
    reasons.add("multiple_same_source_candidates")     # 候选多于一个
```

**但另有三条是纯文本关键词匹配**：

```python
REVISION_LANGUAGE     = ("更新", "修改", "改为", "调整", "replace", "update")
CANCELLATION_LANGUAGE = ("取消", "撤销", "撤单", "cancel")
ENTERED_HOLDER_LANGUAGE = ("有入场", "已入场", "持仓", "保护成本", "保本", "继续持有")
```

**msg 696 命中的正是 `entered_holder_language`**——就因为文末写了
「做无风险**持仓**」这四个字。而此时首次分析**已经给出
`target_lifecycle_id = 1319`**，`management_without_exact_target` 并不成立，
候选虽有 5 个但目标已定。

也就是说：**这次上下文解析本来就不必发生**。它不仅多花了 token
（该次请求 28,274 字节、两次模型调用），还因为看到一份错误的交易所状态
而把正确答案覆盖掉。用户的判断在这里是对的，且代码里已有正确判据，
只是关键词那几条优先级上盖过了它。

### 7.3 上下文分析的初衷：比「多策略选一个」更早、更具体

`docs/archive/plans/2026-07-27-contextual-strategy-thread-resolution-design.md` 开篇：

> 部分 Telegram 群组会连续发布同一策略的开仓、更新、取消和持仓管理消息，
> 并大量使用"更新""先取消""有入场的""保护成本"等**依赖上下文的表达**……
> **结果是同一策略的"更新"可能被创建为独立策略，后续"取消"只作用于新策略，
> 旧挂单仍可能成交。**

所以最初要解决的是**修订链断裂**（更新被当成新策略，取消打偏），
「同群多个策略要选哪个」是其中一种情形（对应今天的
`multiple_same_source_candidates`），不是全部。用户记的方向对，范围更窄一些。

### 7.4 「不该重试」——要把两种重试分开

**六次尝试的输入指纹两两不同**（`context_fingerprint` 与 `rendered_prompt_sha256`
均各不相同），所以那不是「同一个问题问六遍」：

```
6670 15:04:34  ctx=d92390507  6671 15:06:07  ctx=b38cc7b33
6672 15:07:04  ctx=4ab0bb13e  6673 15:09:52  ctx=2475554b5
6674 15:10:40  ctx=42f8cae9a  6675 15:12:06  ctx=e57885709
```

这是原设计第 5 条：「**歧义不应立即升级人工。系统先等待后续消息或状态变化
并自动重分析。**」——`reanalysis` 是有意设计。

**但用户说的那种重试确实存在，在 attempt 内部**：
`terminal = attempt_number == 2`，同一份输入连问模型两次，两次都被同一个
契约校验拒绝才罢休。对 `target_outside_candidate_set` 这种**确定性契约错误**，
第二次问不会有任何新信息——这一次就白白多花了三次（6670/6672/6673 各两问）。

值得分开处置：
- **契约类失败**（`target_outside_candidate_set`、`unknown_decision` 等）→ 不该重问，
  它不是网络抖动，是答案结构不合法；
- **网络/供应商失败** → 该重试（现有 `ContextNetworkRetryPolicy` 已区分对待）。

### 7.5 八分钟里六份不同的上下文，是另一条值得查的线

同一条消息在 15:04–15:12 之间产生了六份互不相同的上下文，其中交易所状态
从「与交换状态一致」变成「pending_entry、无持仓」。而这笔仓位的保护单账本行
（`exchange_adopted_by_tu`）直到 **16:17** 才写入。

也就是说，解析发生在「仓位已建立、但保护归属尚未建立」的窗口里，
而 `redacted_exchange_state` 在这个窗口里描述同一个对象的说法会变。
**这条线本文未展开**，但它决定了 7.2 之外还要不要修状态快照本身。
