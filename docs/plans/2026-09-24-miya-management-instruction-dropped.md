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
