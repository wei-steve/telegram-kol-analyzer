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

---

## 8. 第一步完成：v2 契约已删除并上线（2026-09-24）

**生产 `6c7d9ebeeed9ac19dea7edcc9b2dfcdebe1def78`**，回滚参考
`6495fee139ee354d510a7d6a17720cddff2ca3a3`。净删除 **10,059 行**。
四步流程走完，双向核对 PASS。

### 为什么删而不是启用

v2 是**主动放弃**的，不是忘了开：2026-08-11 的隔离重放在 raw message 10505 上
**两次**失败，`unsafe_evidence_mismatch` / `text_field_attribution_changed`——
v1 已有字段被 v2 改了归属。用户的判断（「可能就是分类太细，所以识别率出错」）
与这个失败模式吻合：意图切得越碎，同一句话被拆进不同意图、字段跟着跑偏的机会越多。
**这也是四分类比 v2 的十一类更可取的实证依据。**

生产从未产生过任何 v2 数据：9796 / 9796 次权威识别都是 v1，
`recognition_decisions` 里 v2 契约行 0 条，熔断器表 0 行。所以删的是一条
**从未执行过的路径**，不改变任何生产行为，也不需要保留读取历史 v2 数据的能力。

### 删除范围

四个模块（`mimo_v2_contract`、`mimo_v2_execution_adapter`、`mimo_v2_replay`、
`mimo_contract_circuit`）；`authoritative_recognition` 的启用判据、v2 推理、
v1 回退、熔断记录与历史证据 v2 读取分支；`recognition_experiments` 的 8 个函数
与 3 个数据类；`message_evidence` 的 v2 持久化；`web_queries` 的投影分支；
`web_app` 的熔断展示、激活水位线校验与设置分支；`trading_settings` 的
`mimo_contract_mode` 与 `mimo_v2_activation_after_raw_message_id`；
提示词种子与组装分支；CLI `replay-mimo-v2`；设置表单与其 JavaScript；
6 个 v2 测试文件与 88 个 v2 测试定义。

**保留** `contract_version` 列与使用它的 9796 条 v1 记录——那是历史事实。

### 过程中的一次误伤，值得记下

按「定义体含 v2 关键词」整块删，会把同一条 `import` 语句里的非 v2 符号
（`SHARED_TRADING_PROMPT` 等）和两个只是「提到 v2」的 fixture 一起带走。
第一次全套跑出 **101 个失败**，逐条恢复后才归零。
**「删干净」不能只靠关键词匹配，要靠测试验收。**

### 上线验证

三个单元 active，3 分钟内 0 条 traceback / CRITICAL，
`/`、`/positions-panel`、`/execution` 与交易设置 API 均 200，
设置页残留 v2 控件 **0** 个。

### 遗留一项

提示词 `trading.analysis.mimo_v2_authoritative`（def=6）在**生产数据库里仍是
active**。代码已不再读它，留着无害，但要「删干净」需一条独立的数据库操作，
**建议单独做、单独确认**。

## 9. 下一步（按用户指定的顺序）

**在首次分析上做四分类**：`新策略` / `策略管理` / `仓位管理` / `闲话`，
外加一个配合分类的字段，并明确空值语义。现在这条链路只剩一套契约
（`trading.analysis.shared` v8 + `lifecycle_event`），改造有了干净的起点。

### L1 观察窗（17 分钟）与窗口内那一条 traceback

HEAD `6c7d9ebe`，三个单元 active，窗口内**新消息 0**（群静默，按 L1 规则照实记，
不放宽、不重跑）。最近三条识别结果形态正常。

窗口内出现 **1 条 traceback**，查清如下，**不是本次删除的回归**：

```
web_app.py:2034 _load_deepcoin_live_position_rows
  → deepcoin_client_factory()
  → build_deepcoin_client_from_env
  → DeepcoinClientError: missing Deepcoin credentials
```

它由**我自己的验证请求** `GET /execution` 触发：`web` 角色本就没有交易所凭据
（`AGENTS.md`：web 角色没有执行权限），该页要读实时仓位，必然失败，
而代码用 `except Exception` 捕获并降级——**页面确实返回了 200**。

**证据的限度要说清楚**：7 天内 `/execution` 只被访问过 **1 次**（就是这一次），
所以没有「同样访问、没有报错」的对照样本可比。判断依据是代码路径：
`deepcoin_client_factory()` 这一段**本次一行未改**，本次改动都落在它成功返回之后，
且与 v2 契约无关。按此判为既有降级路径而非回归；
**若将来 `web` 角色被赋予凭据或该页改走 worker，这条应当消失。**
