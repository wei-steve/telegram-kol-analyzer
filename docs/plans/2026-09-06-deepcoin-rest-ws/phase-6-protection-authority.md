# 阶段 6：新绑定链驱动 TPSL 修改、撤销与平仓

风险等级：**L3**（改变交易所写入语义，且直接决定止损是否挂上）。
这是全项目风险最高的阶段。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

**领取前必须取得用户对本阶段的单独批准。** 不能引用阶段 5 的批准。
批准前必须向用户出示阶段 5 的"逐笔保护挂载确认"结果与补测第 10 项的结论。

## 目标

让阶段 4/5 建立的精确绑定链成为保护动作的授权依据：
TPSL 的**修改**、**撤销**与仓位**平仓**，从"候选筛选 + fencing"改为
"绑定链给出的确切 posId / 保护 ordId"。

这一阶段结束时，`trigger_protection_candidate_predates_fill` 这类
"候选时间早于成交所以拒绝"的失败模式应当不再产生新条目——
因为不再需要靠候选筛选。

## 前置（缺一不可）

- 阶段 5 已 `completed`，且窗口内每一笔新入场都确认挂上了可验证保护。
- 用户已针对本阶段单独批准。
- 补测第 10 项（修改 TPSL 后 OS/TU 是否稳定）已完成，结论明确。
- 阶段 4 的差异报告里 `shadow_only` 与 `ledger_only` 已全部归因，
  没有未解释的差异。

## 补测第 10 项：修改 TPSL 后 OS/TU 是否稳定

这一项必须在改代码之前做完，因为整个阶段的前提就是"绑定链在保护被修改后仍然成立"。

对一个已有 `exact` 绑定的活仓，走一次保护修改（`set-position-sltp` 或
`replace-order-sltp`），然后回答：

- 修改后 `TriggerOrder.OS` 是否还是同一个 ordId？
- `TU` 是否仍等于同一个 posId？
- 若 `OS` 变了，新旧之间有没有任何可查的关联？
- REST 的 `trigger-orders-pending` 里那一行是被更新还是被替换？

**若 `OS` 在修改后改变且新旧之间无可查关联，本阶段的设计前提不成立**，
必须停下来报告用户重新设计（可能需要"每次修改都重新走一遍绑定判据"，
成本与风险都要重估）。不要在前提未确认的情况下继续往下写。

这一项需要一个活仓。用生产自然产生的活仓观测优先；
若必须构造，那是一次独立的受控实验，需要用户单独批准。

### 结论（2026-09-09，6-pre-3 会话 local_4a6676b0，全程只读，未下单）

样本取自生产自然发生的 `set-position-sltp`：**5 个仓位、18 次已确认写入**
（`position_mutation_intents.operation='set_position_sltp' AND status='confirmed'`，
2026-09-07 01:00Z ~ 2026-09-09 04:45Z），远超本项要求的 3 个。逐仓次数：
`1001125163581280` 2 次、`1001125164628529` 3 次（跨 13 小时，含 A-5 的止损缩量/保本移动）、
`1001125179691393` 5 次、`1001125194995925` 4 次、`1001125195880289` 4 次。
对照物是 `deepcoin_ws_events` 里 70 条 `TriggerOrder` 帧与 REST `trigger-orders-pending` 实时读数。

**四问四答：**

1. **`OS` 不是同一个 ordId——每一次修改都产生一张新单。**
   18 次写入各自拿到互不相同的 `order_id`；例如同一个 posId `1001125195880289`
   在 04:44:46~04:44:57 的四次写入依次得到 `...885731 / ...889043 / ...889849 / ...890530`。

2. **`TU` 恒等于 posId，30/30 成立。** 把每条 `TriggerOrder` 帧按 `order_sys_id`
   与写入意图的 `order_id` 连接，比较 `trade_unit_id` 与该意图的 `pos_id`：
   **30 条连得上的帧全部相等，零例外**。入场腿另有一次
   `TU: default → posId` 的翻转（阶段 4 补测 1 已知），本次又观测到 5 例，
   且翻转后的值恒为 `OS + 1`（`...480→...481`、`...257→...258`、`...581→...582`、
   `...542→...543`、`...288→...289`），与 ARCHITECTURE 4.7 "条件单的仓位以它派生的子单命名"一致。

3. **`trigger-orders-pending` 里的行既不是被更新、也不是被替换——是"新增"，旧行原样留着。**
   这是本项最重要、也是与原假设最不同的一条。实时读数显示 posId `1001125195880289`
   上同时挂着 **5 张** TPSL 行：入场自带的 `...880288`（SL 2530, sz 2.3）、
   `...885731`（SL 2535.06, sz 0 即全仓）、`...889043`（TP 2470）、`...889849`（TP 2450）、
   `...890530`（TP 2430）。**其中 `...880288` 与 `...885731` 都是止损且价格不同**
   （2530 与 2535.06），一次"止损修改"并没有撤掉前一张止损。
   posId `1001125179691393` 同样并存 `...691762`（SL 2530）与 `...695536`（SL 2535.06）——
   这正是 ARCHITECTURE 第 6 节 A-5b 记录的"两张止损单"现象的成因。

4. **新旧之间有可查的关联，就是 `TU == posId`。** `OS` 每次都变，但同一仓位上所有
   保护单的 `TU` 都指回同一个 posId，因此"按 posId 收集这个仓位的全部保护单"始终成立。

**对阶段 6 设计前提的判定：前提成立，不需要停下重新设计。**
`OS` 确实改变，但第 4 条给出了可查关联，满足本节"若 OS 变了，新旧之间有没有任何可查的关联"的放行条件。

**但第 3 条改变了阶段 6 的一项任务定义，必须在动手前吸收：**
`set-position-sltp` 是**叠加**语义而不是**修改**语义。所以阶段 6 里的"修改 TPSL"
不能实现成"再发一次 set-position-sltp 就算改完"——那样只会多挂一张，
让同一仓位上并存两张触发价不同的止损，实际生效的是先被触及的那一张
（对止损而言就是**更靠近现价的那张先执行**，等于修改没有生效）。
正确形状（2026-09-10 指挥会话裁定，采用 A 线 A-5e 已上线并验证的顺序）：
**先挂新止损 → 回读确认新单在 `trigger-orders-pending`（按 `slTriggerPrice` / `posSide` / `sz`）→
以 `TU == posId` 收集该仓位上"旧"保护单的全集并逐张撤销 → 回读确认旧单不在 → 才改账本**。
仓位在任何时刻都不裸奔；撤旧任一步失败则**保留新单**、记 `stop_resize_replace_incomplete` 类告警并冻结该仓位的后续修改，由人处理。
原先写的"先撤旧、再挂新"会在撤与挂之间留下裸奔窗口，不采用。短暂并存两张止损是可接受的过渡态（先触及者执行，
方向仍是保护）。这一点写进本阶段任务 1 的验收条件。

**判据字段名的复述**（ARCHITECTURE 第 6 节已有，此处再钉一次，因为本项全靠它）：
`trigger-orders-pending` 上是 `slTriggerPrice` / `tpTriggerPrice`，**不是**仓位行的
`slTriggerPx` / `tpTriggerPx`；取错键得到 `None`，读起来与"交易所没有这张单"完全一样。

**方法与边界**：全程只读——`sqlite3 -readonly` 读账本与 WS 收件箱，
worker 真实凭据经 `/proc/<MainPID>/environ` 取得（ARCHITECTURE 第 6 节），
`python -B` 避免在发布目录写字节码，只调 `list_trigger_orders_pending` /
`list_positions` / `get_trigger_order_history_by_id` 等 GET。
**未下单、未撤单、未改任何账本行。**

## 任务

### 1. 修改 TPSL

`position_mutation_gateway` 的 `set_position_sltp` 路径改为：
目标 posId 与目标保护 ordId 由绑定链给出，不再走候选筛选。

- 绑定为 `unverified` → **拒绝修改**，保持现状并告警。这一条不能有例外。
- 保留现有的回读校验 `_set_position_sltp_readback_matches`：
  绑定链解决的是"改哪个"，回读解决的是"改成功了没有"，两者都不能省。
- 保留 `DeepcoinTpslWriteLimiter`（15/秒、450/分）。
- 修改的执行顺序按"补测第 10 项"一节的裁定：先挂新、回读确认、再按 `TU == posId` 撤旧全集、确认撤净、再改账本；
  撤旧失败保留新单并告警冻结。复用 A-5e 的 `stop_loss_size_convergence.py` 替换序列，不另写第二套。

### 2. 撤销保护

`cancel_position_sltp` / `cancel_trigger_order` 同理：只按绑定链给出的确切
ordId 撤销。

**撤销是本阶段最危险的动作**：撤错了就是把一个活仓的止损拿掉。
因此撤销必须额外满足：撤销前用 REST 精确回读一次该 ordId 仍存在且属性与绑定一致，
回读不一致就放弃撤销并告警。

### 3. 平仓

`close_bound_position`（`worker_command_jobs` 四条命令之一）改为按绑定链的
确切 posId 平仓。

- web 角色仍然没有执行权限，路径不变：web → `worker_command_jobs` → worker。
- 绑定为 `unverified` → 拒绝自动平仓，走人工。

### 4. 重启与停机的保护语义

交接文档明确："交易所侧附带止盈止损的价值之一，是 worker 停机时保护仍由
Deepcoin 执行。系统恢复时不得因为本地没有收到事件而重新创建一套保护，
否则可能产生重复 TPSL。先查询、核对和认领，确认确实缺失后才能走受控补挂流程。"

实现要点：

- 重启后按阶段 2 的重同步流程取得完整观测，**再**判断保护是否缺失。
- "本地没有记录"不等于"交易所没有保护"。判断缺失必须以 REST 查询为准。
- 确认缺失后的补挂必须是受控的：先绑定、再补挂、再回读，
  不能"没查到就直接挂一套"。

### 5. 旧路径的处置

`trigger_protection_intents` / `trigger_protection_rescue_worker` /
`entry_protection_ledger_repair` 这些为 trigger-order 归属服务的路径：

- **本阶段不删。** 历史 binding 和真正的条件触发策略腿仍然需要它们。
- 新绑定链覆盖的对象走新路径，未覆盖的走旧路径，判据要明确。
- 两条路径不得对同一个对象同时动作。要有互斥判据并有测试守护。

删除旧路径是以后的独立工作，不在本阶段。

## 禁止

- 禁止在补测第 10 项得出明确结论之前修改任何保护写入代码。
- 禁止对 `unverified` 绑定做任何自动修改、撤销或平仓。
- 禁止跳过撤销前的 REST 精确回读。
- 禁止因为"本地没有保护记录"就重新创建一套保护。
- 禁止让新旧两条保护路径同时对同一对象动作。
- 禁止删除 `trigger_protection_*` 相关模块。
- 禁止用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领。
- 禁止把 `unknown_exchange_outcome` 自动重发。
- 禁止把外层 `code=0` 当成功（`sCode` 必查）。
- 禁止引入运行时模式开关做灰度；回滚走 `tg-deploy <pre-deploy-sha>`。
- 禁止用 `git add -A`。

## 验证等级与具体检查项

等级 **L3**（交易所写入语义变更）。本阶段的核心失败模式是"止损没挂上"
或"止损被误撤"，所有检查项都围绕这两点。

### 补测项（交接文档 12 项中的第 10 项）

- [ ] **第 10 项：修改 TPSL 后 OS/TU 是否稳定。** 结论四选一并如实记录：
      OS 与 TU 都稳定 / OS 变但有可查关联 / OS 变且无可查关联（前提不成立，停止）/
      前提未出现无法验证（停止）。

### 测试

- [ ] focused：`unverified` 绑定下修改/撤销/平仓全部被拒绝（三个独立用例）。
- [ ] 撤销前回读不一致时放弃撤销并告警。
- [ ] 重启后"本地无记录但交易所有保护"的场景不产生重复 TPSL。
- [ ] 重启后"交易所确实无保护"的场景走受控补挂并回读成功。
- [ ] 新旧路径互斥：同一对象不会被两条路径同时处理（静态或运行时断言守护）。
- [ ] 限流器仍然生效。
- [ ] `sCode=14` 一类软拒绝被识别为失败。
- [ ] 最终候选跑一次全量套件（记录已知既有失败）。

### 生产观察

- [ ] 观察 30 分钟，覆盖至少 5 条真实消息、尽量 2 个群；
      不足 5 条不算失败：按 AGENTS.md L2 用服务器端只读后台监视器持续采样直到凑够（上限 24 小时），会话用 /loop 定时查看。
- [ ] **逐笔核对交易所侧的每一次保护写入**：修改了哪个 ordId、
      改成什么、回读结果是什么。这一项不能只给计数。
- [ ] 窗口内所有活仓的保护状态在窗口开始与结束时各取一次，
      **任何一个活仓在窗口内出现过"无可验证保护"都必须立刻回滚并报告**。
- [ ] 重启 worker 一次，重启前后各取一次全量保护快照，
      确认没有重复 TPSL、没有丢失保护。
- [ ] `authoritative_execution_attempts` 新增 `uncertain` 条数与归因。
- [ ] `trigger_protection_intents` 里新增 `manual_review` 条数：
      本阶段的目标之一是让它归零，若仍在增长要说明原因。
- [ ] 若窗口内没有发生真实保护动作，**不要**为了验证去构造，
      把阶段留 `in_progress` 并说明还缺什么。

## 完成条件

1. 补测第 10 项结论明确且支持本设计。
2. 上面全部检查项通过，或流量不足/无真实样本已如实记录且阶段留 `in_progress`。
3. 提交已推送并 `tg-deploy` 部署，回滚 SHA 已记录且回滚路径已验证。
4. 更新 `docs/rest-ws-trading-status.md`：`current_phase=done`、
   `phase_status=completed`，证据区追加一行。
5. 更新 `docs/ARCHITECTURE.md`：本阶段改变了生产的交易所写入语义，
   按 `docs/post-migration-cleanup-status.md` 的流程规则，
   **同一个提交必须同步更新架构文档**。
6. 发消息给 `brain_session_id`，摘要必须包含每一次保护写入的逐笔核对结果。

## 汇报格式

```text
阶段 6 完成 / 阻塞 / 无真实样本留 in_progress
用户批准：<引用用户批准的原话时间点>
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>，回滚路径已验证
补测第 10 项：<四选一结论 + 证据>
新旧路径边界：新链覆盖什么、旧路径保留什么、互斥判据
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟），真实消息数、覆盖群数
保护写入逐笔核对：N 笔，逐笔列出 ordId / 动作 / 回读结果
活仓保护状态：窗口起 N 个活仓全部有保护 / 窗口止 N 个活仓全部有保护
重启前后保护快照比对：无重复、无丢失
新增 uncertain：条数与归因
新增 manual_review：条数与归因
ARCHITECTURE.md 已同步：是
异常与遗留：
证据路径（服务器）：
```

## 6g — 自动管理路径改走绑定链共用件（只读调查先行）

**范围（2026-09-11 指挥会话按查证后的版本定稿）**：把 `strategy_management_executor`
的 replace/cancel 改走 `protection_replacement` 共用件，撤单前对**将撤的那张单**做四项回读，
删掉执行器自己那套替换序列。**不是"接上绑定链"——它已经接着了。**

### 先查证后立项：一句被推翻的印象

我最初报告说该路径"不走绑定链、不走撤前四项回读"。**前半句错。** 逐层追实际调用链：

```
strategy_management_executor:841 / 1599   close_exact_position(...)
strategy_management_executor:3894         cancel_exact_position_sltp(...)
    → position_mutation_gateway:968 / :936   模块级适配器
    → :982 / :949  _build_fresh_authority     重建 PositionMutationAuthority
    → 网关方法 :283 _load_verified_binding     绑定链、要求 verified 归属
```

后半句要拆：**平仓无撤单动作，"撤前回读"对它不适用**（我把它算进去是范畴错误）；
撤单侧 `_cancel_old_protection_after_replacement`（3880-3905）**确实缺**——
其 docstring "每笔替换完成回读之后才撤旧" 回读的是**新单**，不是**即将被撤的那张旧单**。

`grep pre_cancel_check` 命中 `break_even_convergence_executor` / `deepcoin_execution_actions` /
`protection_replacement`，**不含 `strategy_management_executor`**；后者也未导入
`replace_stop_group` / `replace_take_profit_group` / `resolve_protection_authority` /
`evaluate_cancel_precheck`。

### 生产样本（只读，2026-09-11 查）

| 项 | 值 |
|---|---|
| `management:*:cancel:*` intent | **60**，全部 `confirmed` |
| 跨批次 | **16** 个批次，2026-07-28 ~ 2026-09-01 |
| 被撤单的用途 | **止损 34 / 止盈 26** |
| 撤了账本不认识的 ordId | **0** |
| 非成功回执 | **0** |

**所以这条路径有充足生产样本，不是"无生产样本"。** 它跑过 60 次、每次都成功。

### 一个比"缺回读"更要紧的发现：这条路径不留时序证据

我本想用 intent 时间戳量出"挂新单到撤旧单之间的暴露窗口"，得到的是**全部批次 gap = 0.0 秒**。
**那是假象。** 逐行看批次 97：

```
237 cancel …034448  reserved=submitted=2026-08-03 14:32:22.189520  confirmed=14:32:32.726971
238 cancel …038461  reserved=submitted=同上                        confirmed=同上
239 cancel …039342  reserved=submitted=同上                        confirmed=同上
240 set stop_loss:0 reserved=submitted=confirmed=14:32:22.189520
241 set stop_loss:1 同上
242 set take_profit:2 同上
```

**`reserved_at` / `submitted_at` / `created_at` 全部等于批次的 `executed_at`**
（调用点 `now_provider=lambda: executed_at`，如 852 / 1060 / 1189 / 3904 行），
只有撤单的 `confirmed_at` 来自后续确认，比其余晚 10.5 秒。

**后果**：库里**无法重建这条路径的动作顺序，也无法量出暴露窗口**。
连 id 顺序都不可用作证据——批次 97 里撤单的 id（237-239）反而**小于**挂单的 id（240-242），
那只反映预留顺序，不反映执行顺序。**"gap = 0" 是未知，不是零。**

**这条给 6g 增加了一个目标**：共用件不只带来撤前四项回读，还带来**逐步骤的回读与确认记录**，
从而使"当时有没有一瞬间没有止损"这个问题**事后可查**。现在它不可查。

### 待办（起窗前补齐）

- 找一次**管理指令驱动的保护替换**作为起点样本（intent 670 是**平仓**，
  而平仓是这条路径上已经正确的那一半，**不能证明 6g 要修的缺陷**）；
- 影子 → 切换两步，判据起窗前写，切换按治理规则由指挥会话放行。

### 6g 起点样本：批次 155 / 腿 137（pos `1001125104601308`，2026-09-03 07:15:25Z）

从 60 笔里挑的、前后状态最完整的一个。原样：

```
605 cancel  137:precancel:1001125104601392  stop_loss   76500  cancelled  management_protection_precancel
606 cancel  137:precancel:1001125104602638  backup_stop 76347  cancelled  management_protection_precancel
607 cancel  137:precancel:1001125104603160  take_profit 78300  cancelled  management_protection_precancel
608 cancel  137:precancel:1001125104603274  take_profit 79000  cancelled  management_protection_precancel
609 cancel  137:precancel:1001125104603426  take_profit 79700  cancelled  management_protection_precancel
610 close_position  137:close:TM4B26…       （部分平仓）
611 set     137:set:stop_loss:0             76500  verified  management_tpsl_replacement
612 set     137:set:stop_loss:1             76347  verified  management_tpsl_replacement
613 set     137:set:take_profit:2           79000  verified  management_tpsl_replacement
614 set     137:set:take_profit:3           79700  verified  management_tpsl_replacement
```

**顺序是"先撤光全部保护 → 部分平仓 → 按剩余仓位重挂"**，由
`_cancel_exact_risk_reduction_protection_before_close` 发出
（docstring：*Durably reserve and cancel the exact old TPSL set before reducing risk*）。

**这不是一个显而易见的缺陷，不要当成缺陷写**：先撤是**有意的**——旧 TPSL 按全仓尺寸，
部分平仓后若不先撤会超额平仓（与共用件"止盈先撤后挂"同一条理由）；
而且已有补偿路径 `_restore_precancelled_protection_for_rejected_close`（1626 / 1681），
平仓被拒时把保护放回去（A 线 A-11b 做的就是这一段）。

**6g 对这条路径实际增加的只有两件，两件都不改变它的顺序**：

1. **撤前对将撤的那张单做四项回读**（instId / posSide / 触发价 / 数量）。
   现在 605-609 直接按 ordId 撤，**不验证那张单还是不是解析时的那张**。
   `grep pre_cancel_check` 在本文件命中 0 次。
2. **逐步骤真实 wall clock 与回读记录**。现在 605-614 的
   `created_at` / `reserved_at` / `submitted_at` **全部是批次的 `executed_at`**，
   所以**"撤光到重挂之间，剩余仓位裸露了多久"在库里不可查**。
   注意这里的措辞：**是不可查，不是为零**——这段裸露是这条流程结构上必然存在的，
   问题不在于它存在，而在于**它的长度没有任何证据**。

**所以 6g 的产出应当是**：同一条顺序、同一个业务语义，
但撤每一张单之前先证明它还是那张，且事后能回答"裸露了多久"。

### 分场景顺序：查证结果推翻了"需要 6g-2 分序"这个前提

指挥会话 2026-09-11 裁定：部分平仓场景先撤后挂不是缺陷；但**纯移动止损（数量不变）
应走 A-5e 先挂后撤**，并让 6g-2 在量出真实暴露窗口后决定"按场景分序"。

**查证后：这两个场景已经分序了，而且分对了。判别依据不是我的推断，是 idempotency key 自己写着的。**

| 路径 | key 形状 | 顺序 | intent 数 | 仓位数 | 时间跨度 |
|---|---|---|---|---|---|
| `_cancel_old_protection_after_replacement` | `management:<b>:<leg>:cancel:<ordId>` | **先挂后撤（A-5e）** | **60** | 16 | 2026-07-28 ~ 2026-09-01 |
| `_cancel_exact_risk_reduction_protection_before_close` | `management:<b>:<leg>:precancel:<ordId>` | **先撤后挂** | **17** | 4 | 2026-08-12 ~ 2026-09-03 |

**且 4 个 `precancel` 批次 100% 都含 `close_position`**——**先撤只发生在带平仓的场景**，
正是它正确的那个场景。

实例，纯移动止损（批次 151 / 腿 134，无平仓）：

```
573 set    134:set:stop_loss:0   新 78669.0133333334
574 set    134:set:stop_loss:1   新 78669.0133333334
575 cancel 134:cancel:…084853    旧 77400.0   cancelled
576 cancel 134:cancel:…085673    旧 77245.2   cancelled
```

**先挂新、再撤旧**。批次 149 同形。

**更正我自己先前的一处混用**：我先前报"管理撤单路径跑过 60 次"，那 60 只是
`:cancel:` 这一条路径（`LIKE '%:cancel:%'` **不匹配** `:precancel:`）；
随后我却拿 `precancel` 批次 155 当起点样本并引用那个 60。
**两条路径被我并在一句话里说了。** 管理撤单总数是 **77 = 60 + 17**。

**对 6g-2 的影响**：**"按场景分序"这件事已经做到了，不需要立项去做。**
6g-2 若还要立，问题应改成另外两个——而它们都要等 wall clock 数据：
(a) `precancel` 场景里那段结构性裸露**实际有多长**（现在不可查）；
(b) 那 17 笔里有没有出现过"撤了、但平仓没成、恢复路径也没跑成"的组合
（`_restore_precancelled_protection_for_rejected_close` 是否真的兜住过）。

**6g 本步范围不变**：两条路径都加撤前四项回读与逐步骤 wall clock，**不动顺序**。

### 6g 影子判据（起窗前写；先量到达率再写判据）

**写判据之前先问了一个问题：这个影子在 30 分钟里有没有可能观察到任何东西？**

近 21 天实测：管理批次共 **17 个**，其中**产生保护替换的只有 5 个**——**约每 4 天一次**。
30 分钟窗内观察到一次替换的期望值约 **0.005**。

**所以 30 分钟 L2 窗不能作为 6g 影子核心观测量的收窗判据。**
（这与 6f 的"消息量"是同一形状：判据要的量，其到达率低于窗口的分辨率。
区别在于这次我在写判据之前就量了，而不是收窗时才发现。）

判据因此分成两类，**两类的强度不同，不混写**：

**甲类 · 本窗可判定（30 分钟）**
1. 影子仪表在线：`head_ok`/`units_ok` 全程 1、零重置、reconcile 轮持续；
2. **行为零改变**：窗内 `position_mutation_intents` 若有新增，
   **逐笔按 idempotency key 前缀归因**，其中**由 6g 仪表产生的必须为 0**
   （仪表只读只记，不产生 intent）；
3. 窗内若有任何路径写入 intent，**新增的逐步骤 wall clock 字段必须被写上**
   （这一条能否取到样本取决于窗内有没有写入，取不到就记"无样本"）。

**乙类 · 本窗不可判定，记为待观测项并附触发条件**
4. **撤前四项回读的判定结果**（将撤那张单的 instId / posSide / 触发价 / 数量
   是否仍与解析时一致）——需要一次真实的管理保护替换；
5. **`precancel` 场景的真实裸露时长**——需要一次真实的风险削减批次
   （该路径 21 天内 0 次，历史总计 17 笔 / 4 个仓位）。

**乙类的设计要求（因为它等不到）**：第一次产生回读判定时**必须自己叫人**，
落 `position_protection_incidents` 或等价告警，**不能依赖有人记得回头去查**。
理由是本仓库已经写下的那条：**一条只在人记得时才执行的判据，等于没有判据**；
而一个每 4 天才出现一次的观测量，正是最容易被忘记回看的那种。

**收窗表述必须区分**："甲类全部达成、乙类无样本"，
**不得写成"6g 影子窗通过"**——后者会让读者以为回读判定已经被验证过。
