# 图片不可用时降级为文字识别 · 设计稿

- 日期：2026-10-01
- 状态：**已批准**（2026-10-01 用户拍板：Q1 乙、Q2 不告诉、Q3 不重跑、Q4 另开议题、Q5 部署后重跑 20102、Q6 2 小时），实施中
- 起因：raw 20102（颜驰 11分组，chat -1003942765613，notify_only，无资金影响）
- 风险级别：L2（改识别输入，并改 ingest 补下载与任务收口）
- 与其它线的关系：**不改提示词**（四分类阶段 3 批次 B 约 10-06 改提示词）；不碰交易所对账器（陈哥 not_unique 修复 61339348 在改）

---

## 1. 事件经过（已用生产数据核对）

| 时间（UTC） | 事件 |
|---|---|
| 17:48:37 | 消息发出（message_id 183，带一张图，文字 193 字） |
| 17:49:08 | ingest 实时下载图片 **30 秒超时**，日志 `Telegram media download failed for -1003942765613/183:`（异常文本为空，符合 `asyncio.TimeoutError`）。消息照常入库、入队 |
| 17:49:09 → 17:52:56 | 权威识别 5 次（run 11017～11021），每次都在 `recognition_experiments.py:540` 的前置拦截处失败，`provider_requests=0`——**文字也没送出去** |
| 17:52:56 | 任务 `processing_error:AuthoritativeProcessingFailed` 达上限 5 次，`failed`；决策 `识别失败 / mimo_authoritative_failed_exhausted` |
| 17:53:29 | ingest 的 reconcile（约每 5.6 分钟一轮）补下载成功，文件 `-1003942765613/183.jpg`（75374 字节，ingest:runtime 0660）出现。**晚了 33 秒** |
| 之后 | 没有任何东西重新排队识别这条消息，至今仍是识别失败 |

所以这次的原因是：**下载超时 + 重试窗口（约 3.5 分钟）比补下载周期（约 5.6 分钟）短 + 补下载成功后不会重新入队**。

排除的原因：

- 权限：媒体目录 root:telegram-kol-runtime 0775，文件 0660，worker 在 runtime 组，可读。
- 清理定时器：`telegram-kol-media-cleanup.timer` 每天 03:30 CST，上一次在 09-29 19:30Z，早于本消息；保留期 14 天。
- 路径：worker / web / ingest 三进程的 media_root 都是 `/opt/telegram-kol-analyzer/data/media`。

## 2. 近 30 天统计（2026-08-31 起，VACUUM INTO 快照上统计）

35 次失败 run，**9 条消息**。快照 `/var/backups/telegram-kol/20261001-image-unavailable/snap.db`，499429376 字节，sha256 `583b864a481dc6e10769345bedba0601bbc44ca5dea9b9207d3c967a4265813c`，统计完已删除。

| raw | 群 | 模式 | 发出（UTC） | 文字 | 失败次数 | 任务 | 结局 | 图片 |
|---|---|---|---|---|---|---|---|---|
| 14447 | 币圈所长 | notify_only | 09-02 10:54 | 有（52 字） | 5 | failed | 识别失败 | 当时没下到，现已被 14 天清理 |
| 17301 | 比特币陈哥 | **auto_trade** | 09-17 10:32 | 有（134 字，战绩宣传） | 5 | failed | 识别失败 | **至今没下到** |
| 18368 | 比特币飞扬 | notify_only | 09-22 13:59 | 无 | 4 | succeeded | 非策略 | 约 2 分 47 秒后补到 |
| 18422 | 米娅 vip | **auto_trade** | 09-23 00:46 | 有（184 字，行情分析） | 1 | succeeded | 非策略 | 约 34 秒后补到 |
| 18580 | ROSE | notify_only | 09-23 13:34 | 有（48 字） | 4 | succeeded | 非策略 | 约 6.5 分钟后补到 |
| 19869 | 币圈所长 | notify_only | 09-30 00:58 | 有（88 字，止损管理） | 3 | succeeded | 非策略（hypothetical_condition） | 约 1 分 50 秒后补到 |
| 20025 | 比特币飞扬 | notify_only | 09-30 13:06 | 无 | 5 | failed | 识别失败 | **至今没下到** |
| 20060 | 比特币飞扬 | notify_only | 09-30 13:33 | 无 | 3 | succeeded | 非策略 | 约 2 分 11 秒后补到 |
| 20102 | 颜驰 | notify_only | 09-30 17:48 | 有（193 字，止盈管理） | 5 | failed | 识别失败 | 4 分 52 秒后补到，没人重跑 |

要点：

- 自动交易群 2 条（17301 战绩宣传、18422 行情分析），**都不是真实的入场或管理指令**，没有资金影响。
- 9 条里 6 条有文字；3 条纯图（全在比特币飞扬）。
- 9 条里 5 条靠重试**等到了**补下载。**这不是确定性失败**——这一点修正了任务单里的前提，见 §5。
- 2 条（17301、20025）图片永远不会来：reconcile 只补 `message_id > checkpoint − 5` 的消息，群里再发几条消息，旧的就出了补下载窗口。
- 趋势：09-30 一天 4 条，比前几周多。下载超时本身是 Telegram 侧的，我们管不了频率，只能管恢复。

## 3. 目标

1. 图片读不到但有文字时，**当场按文字识别**，结果带「图片缺失」标记。文字不足以判断就按现有规则 fail-closed，但不因缺图整条放弃。
2. 缺图的识别结果，执行层有一道**按价位来源**的闸门（§4.3，需拍板）。
3. 不再「盲重试 5 次」：有文字的当场降级；纯图的等图片到了再跑。
4. 修补下载：失败的图尽快补、不再永久丢失、补到后纯图消息重新排队。

非目标：不改提示词、不改契约、不改 schema、不改交易所写入语义、不改对账器。

## 4. 设计

### 4.1 识别输入降级（`recognition_experiments.run_mimo_authoritative_for_message`）

现状：`:540-562` 只要有一张图读不到就整条返回错误。

改为：

| 情况 | 行为 |
|---|---|
| 所有图都读得到 | 不变 |
| 有图读不到，**文字非空** | 继续识别，只送读得到的图（`_build_mimo_payload` 本来就会跳过读不到的图）；`input_kind` 记为 `text+image_missing`（≤32 字符，无需迁移） |
| 有图读不到，**文字为空**，还有别的图读得到 | 同上，按读得到的图识别，`input_kind` 记 `image_missing` 前缀的值（`image+image_missing`） |
| 图全读不到，文字为空 | 仍失败，错误串不变，但任务按 §4.4 收口 |

- **送给模型的内容不加任何「图片缺失」提示**（推荐，见问题 Q2）。纯文字输入时 `input_reading.image_quality = none`，契约 §3.5 禁止模型输出 `图片不可读`，模型只能按文字的真实类别判；这和现在处理纯文字消息完全同一条路径，不引入新的模型行为。
- 缺图标记记在我们自己的记录里：`MimoAuthoritativeResult` 增加 `missing_image_asset_ids`；`input_kind` 在 `recognition_experiments` / `mimo_recognition_runs` / `recognition_decisions` 三处都会带上 `text+image_missing`，网页、值守 casefile 直接能看到。
- `message_evidence.normalize_mimo_evidence`：降级成功时 `extraction_status` 仍记 `completed`（回放 / 恢复路径只复用 `completed` 的证据行），缺图标记 `_input_degradation` 存进 `normalized_evidence`，重建时据此恢复 `input_kind=text+image_missing`。（实施时修正：原稿写的是新状态 `image_unavailable_text_only`，那样会让回放路径复用不到这行证据。）
- 需要逐一检查所有 `input_kind == "text+image"` 的精确比较，确认新取值不会落进错误分支（实现时列清单）。
- `evidence_backfill.py:333` 同样调这个函数：回放旧消息（图片已被 14 天清理）时也会降级为文字，结果带标记——这是想要的。
- `prompt_testing.py:245` 的独立拦截**不动**（提示词测试就是要测图）。

### 4.2 文字不足时

不新增规则。模型只看到文字后，按现有的契约、触发判据、上下文解析和 `management_actionability` 闸门走：说不出目标就 `unknown` → fail-closed；没止损就不是新策略；预告类不交易。
20102 预期就是这样：文字说「832-828 附近做止盈」，但文字里没有币种，目标只能靠上下文候选；候选说不清就 fail-closed。

### 4.3 缺图时的执行闸门（需拍板，Q1）

风险在于：文字能被读懂，但图里有文字没有的关键信息（币种、改过的价位、「已撤」）。模型只看到文字和上下文，价位只可能来自文字或**上下文里的旧消息**。

推荐方案**乙**：`input_kind` 带 `image_missing` 的决策，在执行层加一条确定性规则：

- 入场（新策略）：入场价和止损价**都必须在本条文字里以数字出现**，否则只通知不执行；
- 带价位的管理动作（调止盈、调止损、改入场价）：新价位必须在本条文字里出现；
- 不带价位的管理动作（全平、保本、撤单、减仓比例）：照常，仍受现有目标解析约束。

放在现有执行层闸门（阶段 3 的 `9eb85f40` 那一处，`management_actionability` 同层）作为新拒绝原因 `image_missing_price_not_in_text`，只做拒绝方向。

备选：**甲**＝缺图一律只通知不执行（最保守，但缺图的真实指令全部丢执行）；**丙**＝不加闸门（文字能判就照判）。

### 4.4 任务收口：不再盲重试

| 情况 | 现在 | 改后 |
|---|---|---|
| 有文字、图缺 | 失败 → 重试 5 次 → failed | §4.1 当场降级识别，**一次完成**，不进重试 |
| 纯图、图全缺 | 失败 → 重试 5 次（有时等到图，有时等不到） | 第一次就按新终态原因 `media_unavailable_waiting` 收口，任务 `succeeded` 不再排队（照 `TerminalAuthoritativeProcessingFailed` 的现成路径）；**图片补到时由 ingest 重新入队**（§4.5-3） |

对照阶段 3 ③：③ 把「重试也修不好」的契约失败终态化。纯图缺图不同——重试**确实**可能修好（5/9 修好了），但修好它的是图片到达，不是时间流逝。所以不是简单归入 ③，而是「先终态，由图片到达事件唤醒」：不浪费 5 次，也不会像 20102 那样图到了却没人管。

`recognition_failure_attribution` 里新增该原因；入场准入屏障把它当终态读（与 `first_pass_contract_violation` 同处理）。

### 4.5 修补下载（ingest，`telegram_live_listener`）

1. **补下载窗口改按时间**：`_load_orphan_media_message_ids` 现在是 `message_id > checkpoint − 5`；改为「`message_id > checkpoint − 5` **或** 发出时间（`posted_at`）在最近 2 小时内」。17301、20025 这种就不会永久丢。用发出时间而不是入库时间：历史回填会在现在写入几个月前的消息，不能因此扩大窗口。限制：reconcile 每轮只取每个群最近 50 条，2 小时内超过 50 条的群，更早的仍补不到。
2. **失败后尽快补**：实时下载失败时，由 ingest 安排一次定向补下载（推荐 +20 秒、+60 秒两次，只针对这一条），不等 5.6 分钟的 reconcile。
3. **补到后重新入队**：reconcile / 定向补下载把某条消息的 `local_path` 从不可用变成可用时，若该消息当前决策是「缺图终态」（§4.4）→ 重新入队识别，`last_reason=media_repaired_enqueued`。**已按文字降级识别成功的不重跑**（Q3）。

不改：实时下载 30 秒超时（它会把整条消息的入库推迟 30 秒，这是另一个时延问题，见 Q4）；媒体保留期。

## 5. 对任务单前提的修正

任务单写「图片读不到属于确定性失败，不应重试 5 次」。生产数据显示：9 条里 5 条是重试期间图片补到后成功的；真正修不好的是 reconcile 窗口外的 2 条和被清理的 1 条。所以本方案的做法是「不盲重试」，而不是「当成确定性失败直接放弃」：有文字当场降级；纯图终态后等图片到达再唤醒。

## 6. 回放用例

离线测试（固定数据，不调模型）：

| 用例 | 输入 | 期望 |
|---|---|---|
| 20102 形态 | 文字 + 文件不存在的图 | 模型被调用，请求只含文字；`input_kind=text+image_missing`；任务一次完成 |
| 18368 / 20025 形态 | 纯图 + 文件不存在 | 不调模型；终态 `media_unavailable_waiting`；任务 succeeded 不重试 |
| 补到后唤醒 | 上一行之后文件出现、reconcile 修复 local_path | 重新入队一次；已降级成功的消息不入队 |
| 17301 形态 | checkpoint 已前进 >5 条、消息 1 小时前 | 仍在补下载集合里 |
| 两图一缺 | 文字 + 一张可读 + 一张缺 | 只送可读那张；标记缺图 |
| 闸门乙 | 缺图 + 新策略、止损价不在文字里 | 只通知不执行；在文字里 → 放行 |
| 闸门乙 | 缺图 + 全平 | 放行（仍受目标解析约束） |
| 旧行为保留 | 图全可读 | 与现在逐字节相同的请求体 |

真实模型回放（服务器只读，`python -B`，不写生产库）：对 6 条有文字的样本（14447、17301、18422、18580、19869、20102）只送文字跑一次权威识别，与带图时的实际结论对比，记录有无「文字判可执行、带图判不可执行」或反之的分歧。**这组对比决定 §4.3 是否足够**。

## 7. 验证（L2）

- 开发中跑聚焦测试：`test_recognition_experiments`、`test_message_processing_worker`、`test_first_pass_phase3_contract_failures`、`test_telegram_fetch` 及 live listener 相关、`test_message_evidence`、新增测试。
- 最终候选跑一次全量 `uv run python -m pytest -q`。
- 部署由调度会话排期；观察 30 分钟 ≥5 条真实消息。缺图本身很少（30 天 9 条），观察窗内大概率碰不到正向样本——正向样本等首例，届时核对 `input_kind=text+image_missing` 的决策与执行。
- 回滚：`tg-deploy <部署前 sha>`，无 schema、无数据改动。
- 20102 本身：修复部署后可手工重新入队一次（notify_only，无资金影响）；是否做由你决定（Q5）。

## 8. 需要你拍板的问题

- **Q1 缺图的执行闸门**：推荐 **乙**（价位必须出现在文字里，不带价位的管理动作照常）。备选：甲＝缺图一律只通知；丙＝不加闸门。
- **Q2 要不要告诉模型「原消息有图但缺失」**：推荐**不告诉**。告诉它要在 user 消息里加一句话，这实际上是一段提示词，会和批次 B（约 10-06）的提示词改动互相影响，也可能让模型在 `image_quality=none` 时输出 `图片不可读` 而触发契约违规。
- **Q3 已按文字降级识别的消息，图片后来补到了要不要重跑**：推荐**不重跑**（重跑可能对同一条消息产生第二个动作，要走编辑消息那套去重，范围扩大）。只在网页上标「图片缺失」。
- **Q4 实时下载 30 秒超时顺带阻塞整条消息入库 30 秒**：20102 就晚了 31 秒才入库。推荐**本方案不动**，另开一条时延议题（改成先入库后下载涉及和 worker 的时序）。
- **Q5 20102 要不要在部署后手工重新入队**：notify_only 群，推荐重跑一次作为首个真实样本。
- **Q6 补下载时间窗 N**：推荐 2 小时。更长会让 reconcile 每轮多下一些旧图；更短可能漏掉夜间积压。

## 9. 实施结果（2026-10-01）

- **候选 sha：`7388588a`**（基于 origin/main `9941109b`；WIP `168dfe32` 已并入其历史）。未推送、未部署。
- 全量 `uv run python -m pytest -q`：**10942 passed, 4 skipped**（25 分 33 秒）。
- 聚焦（16 个文件，含新文件）：411 passed。
- 新测试 `tests/test_image_unavailable_text_fallback.py`，24 条，回放 20102 / 18368 / 20025 / 17301 四种形态，外加闸门乙、证据回放、告警、补下载重新入队、定向重试。
- **修复前失败的证据**：新测试文件在修复前代码（`9941109b`）上连收集都会失败（新符号不存在）；另用只依赖旧接口的探针在修复前代码上确认：20102 形态与纯图形态都抛可重试的 `AuthoritativeProcessingFailed`，模型调用 0 次（文字从未送出）；17301 形态（checkpoint 已过 >5 条、1 小时前发出）不在补下载集合里。探针与临时目录已删除。
- 改动文件：`recognition_experiments.py`、`message_evidence.py`、`authoritative_recognition.py`、`image_missing_price_gate.py`（新）、`recognition_failure_attribution.py`、`message_processing_worker.py`（仅注释）、`telegram_live_listener.py`、`oncall_alerts.py`、`templates/_messages.html`。**未改提示词、未改交易所对账器**、无 schema 变更、无依赖变更。
- 改写的旧测试：`test_recognition_experiments.py` 里原来锁定「整条放弃」的那条，改为锁定降级行为。

部署要点（交调度会话排期）：
- L2。部署涉及 ingest 与 worker，`tg-deploy` 会按 worker → web → ingest 重启。
- 观察：30 分钟 ≥5 条真实消息；缺图正向样本 30 天仅 9 条，窗口内大概率碰不到，首例出现时核对 `input_kind=text+image_missing` 的决策、执行与告警。
- 部署后手工重新入队 raw 20102 一次（Q5，notify_only 群，无资金影响）。它当前的决策原因是 `mimo_authoritative_failed_exhausted`，不会被补下载自动重新入队。
- 回滚：`tg-deploy <部署前生产 sha>`。

### 部署与观察（2026-10-01，用户在本会话确认「部署」）

- rebase 到 origin/main `60b010a6` 后，代码 sha 变为 **`08eaec4dd6be1addd8a94cf183bdf29a3bc44957`**（原候选 `7388588a` 的同内容）。受影响测试 517 passed，全量 **10960 passed, 4 skipped**。
- 预检：生产 HEAD `45e404bc`，候选是其后代且包含 origin/main；实盘绑定 391–396 全部 closed，之后无新绑定；最近管理批次 195 succeeded；消息任务无积压。
- `tg-deploy 08eaec4d…`：02:08:39Z 完成，worker / web / ingest 均 active，ingest 重启后无报错。随后同一 sha 推 `origin/main`（非强推）；双向核对通过，判决式检查 `PASS: 0 code files beyond production`。
- **回滚**：`tg-deploy 45e404bc27a9fc7ad699f3b528d49a97b23beeb8`。
- **L2 观察 PASS**：服务器只读监视器 `/root/image-unavailable-l2/monitor.log`，02:14–02:44Z 每分钟 31 个采样全部健康（三服务 active、HEAD 不变、无 Traceback、无积压超 5 分钟的任务、无 failed 任务、无失败的识别 run），窗口内滚动 30 分钟消息数 19–21 条（起始）至 5 条（结束），2–4 个群。缺图正向样本未出现（30 天仅 9 条，预期如此），首例出现时核对。
- **raw 20102 重跑（Q5）未能产生识别样本**：02:09Z 用 `_enqueue_processing_jobs(resume_terminal_jobs=True)` 以 worker 用户重新入队（任务 8341，原 `failed/5`）。worker 的时效保护随即以 `expired_stale_instruction` 作废了这个任务，决策改为 `skipped / authoritative_gap_recovery_expired`（`recovery_guard`），没有调用模型：消息已约 16 小时，超出补识别窗口。这是防止旧消息被重新识别成交易的既有保护，fail-closed 正确；颜驰群 notify_only，该原因只在 auto_trade 群告警。没有绕过它。
