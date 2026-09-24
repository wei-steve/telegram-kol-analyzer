# 识别 + 上下文解析链路盘点（2026-09-24，只读）

**缘起**：用户要求整理首次分析与上下文分析的整体流程，并在改完后
**把旧设计删干净**，以免新会话读到旧代码被带偏方向。

**先说结论**：不需要重新设计。**2026-08-08 已经有一份完整的退役设计与 14 任务
实施计划**，它的目标与用户此刻的要求逐字一致；那份计划**封堵部分做完了，
删除部分没做完**，然后连同 385 份计划一起被归档进 `docs/archive/plans/`。
现在要做的是**把它收尾**，外加这次米娅事故暴露的两处新问题。

---

## 1. 当前链路（以生产实际调用为准）

```
Telegram raw message
  └─ message_processing_worker
       └─ authoritative_recognition.assess_message_authoritatively()   ← 唯一权威入口
            ├─ 首次分析 MiMo v2（gpt-5.6-luna）
            │    ├─ recognition_result : 是策略 / 非策略
            │    └─ lifecycle_event    : none | entry_confirm | cancel_entry
            │                            | exit_position | position_update
            │                            (+ target_lifecycle_id / management_action / 价位)
            ├─ requires_context_resolution()   ← 确定性触发判据，8 条原因取或
            ├─ [条件触发] context_resolution.resolve_contextual_strategy()
            │    ├─ 候选集 strategy_thread_candidates（确定性生成）
            │    ├─ 模型二次裁决 → decision ∈ {new_thread, manage_thread,
            │    │                             revise_thread, hold, unresolved}
            │    └─ parse_context_resolution_decision() 契约校验
            ├─ _resolved_mimo_result()          ← 用解析结果改写首次分析
            └─ recognition_decisions（落库）
  └─ context_resolution_worker                  ← 状态变化后的 reanalysis，独立 worker
```

权威边界由 `tests/test_recognition_authority_architecture.py` 静态守护：
`PRODUCTION_AUTHORITY_MODULES` 只有三个模块，且不得 import 六个 V1 符号。

## 2. 8 月退役计划的执行状态

`docs/archive/plans/2026-08-08-recognition-context-path-retirement{,-design}.md`，
14 个任务，绞杀者式分阶段。实测痕迹：

| 阶段 | 任务 | 现状 |
|---|---|---|
| 封堵 | 1 冻结权威边界（架构测试） | **已做**：`test_recognition_authority_architecture.py` 存在并在跑 |
| 封堵 | 2–5 实时/历史入口封堵、去掉 web 的 V1 默认、缺权威告警 | **已做**：四个 V1 符号的 `EXPECTED_LEGACY_IMPORTERS` 均为空集 |
| 观察 | 6 部署并观察七天 | 无记录 |
| 提取 | 7–9 抽出权威投影、移动依赖闭包 | 部分：`authoritative_recognition.py` 已独立，但仍从 `message_recognition` 取投影函数 |
| **删除** | **10–11 删 V1 入口、群组档案、本地生命周期改写** | **未做**：`message_recognition.py` 仍有 **5543 行** |
| **删除** | **12 上下文解析收敛为两种调用模式** | **未做**：`attempt_phase` 仍是自由字符串 |
| 收尾 | 13–14 回归与自然验证 | 无记录 |

**当前状态文档（`docs/*.md`）里没有任何一条它的完成记录**——这正是用户担心的
形态：计划被归档，代码停在半路，下一个人读到的是两条路径并存的仓库。

`message_recognition.py` 现在只剩三个 import 方：`authoritative_recognition`
（取投影函数，活）、`lifecycle_monitor`、`prompt_testing`。

## 3. 这次米娅事故暴露的两处新问题（不在 8 月计划范围内）

**(a) 触发判据被关键词盖过**（`authoritative_recognition.py:215-224`）
`management_without_exact_target` 这条正确判据存在，但另有三条**纯关键词**触发：
`REVISION_LANGUAGE` / `CANCELLATION_LANGUAGE` / `ENTERED_HOLDER_LANGUAGE`。
msg 696 因文末「做无风险**持仓**」命中第三条而触发解析，尽管首次分析
**已经给出 `target_lifecycle_id = 1319`**。这次解析本不该发生。

**(b) 降级抹掉首次分析**（`authoritative_recognition.py:663-674`）
解析返回 `hold` / `unresolved` 时，`lifecycle_event` 被整体替换为
`{"event_type": "none"}`。首次分析读到的管理动作、目标、价位全部消失，
下游再也看不出这条消息曾被判为仓位管理。

**(c) 契约类失败也会重问模型**（`context_resolution.py`，`terminal = attempt_number == 2`）
`target_outside_candidate_set` 是确定性的结构错误，第二次问不会有新信息。
应与网络失败分开处置。

## 4. 建议的整理顺序（分阶段，每阶段独立可部署可回滚）

1. **写下「当前唯一真相」流程文档**（L0）：本文件第 1 节的完整版，放 `docs/`
   而非 `docs/archive/plans/`，并在 `docs/ARCHITECTURE.md` 挂链接。
   新会话读到的第一份材料必须是它。
2. **修 (a)**（L1）：首次分析已给出可验证目标时，不因关键词触发解析。
   省 token，且消除这次事故的直接成因。
3. **修 (b)**（L2）：`unresolved` 不再抹掉 `lifecycle_event`，改为保留并标记为
   「待确认的管理指令」+ 告警。**改变哪些消息进入管理链路，需单独批准。**
4. **修 (c)**（L1）：契约类失败不重问。
5. **收尾 8 月的 Task 10–12**（L2/L3）：删 V1 入口与群组档案路径，
   把 `message_recognition.py` 缩到只剩被权威路径使用的投影函数。
   **这一步最大，且必须在 1–4 落地并观察之后**——否则又是一次
   「删除与行为改动混在一起」，正是那份设计自己拒绝过的做法。
6. **归档与删除**（L0）：删掉已失效的历史计划，或在文件头标注
   「已被 X 取代」。8 月那份计划应在收尾后标注完成状态，而不是留在
   `archive/` 里让人以为还有效。

## 5. 未决

- 第 5 步要不要连 `lifecycle_monitor` / `prompt_testing` 对
  `message_recognition` 的依赖一起处理，需要先看这两个调用方是否仍在线。
- `context_resolution` 的 8 条触发原因里，除 (a) 之外的几条是否都仍有必要，
  需要按真实消息统计触发分布后再决定，不能凭读代码删。
