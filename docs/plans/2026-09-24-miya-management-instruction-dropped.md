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
