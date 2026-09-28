# 2026-09-28 12 小时核对问题修复设计稿（待批准）

- 来源：`docs/plans/2026-09-28-12h-group-action-audit.md`（分支 `claude/youthful-wu-db596d`，提交 `bf1db60e`）
- 基线：生产 HEAD / `origin/main` = `546d991346fd063d1c0d3098cf24de2589476fcd`
- 核实方式：服务器 `VACUUM INTO` 快照（487 190 528 字节，sha256
  `8c588272c595ff1d2483e462bb16e5df26aae7fc8583c4b40621d747967bf94f`，2026-09-28 17:57Z 取，
  查完已删除）；值守状态库 `/var/lib/telegram-kol-oncall/state.db` 只读查询；
  `/etc/telegram-kol-worker.env` 只读了告警类型相关的三个键。生产库本身没有查询，没有交易所调用。
- 状态：**已批准（2026-09-28，用户：第 6 节全部按推荐，Q3a 选 A）**，实施中。

## 0. 结论速览

| # | 问题 | 根因（已用生产数据核实） | 修法 | 风险级别 |
|---|---|---|---|---|
| 1 | 陈哥 #19598 保本被拒 | 旧匹配器 `match_position_protection` 只要有一张账本认不出的 TPSL 单，就把**全部**仓位判为不明确；大漂亮两条挂单入场腿自带的止损正是这种单 | 调用前排除「挂单中入场腿自带的止损」（复用已有判定），并把不明确的范围收窄到真正可能相关的那个仓位 | L2 |
| 2 | 米娅 #19597 减仓被拒 | ① 按「%」切段不认句子边界，原文和模型复述拼接后「剩余」跨过换行绑到了复述里的「减50%」；② 修好 ① 之后还有第二道拦截：原文「**加仓后**浮盈600点」被当成加仓指令 | ① 动词和百分数之间隔着句末标点 / 换行就不绑定；② 「加仓后」按叙述处理 | L2 |
| 3 | 高严重度告警没发出 | ① `management_fraction_rejected` 既不在代码「必发」清单，也不在生产白名单，从 09-20 起 8 条 auto_trade 告警全部停在 pending；② `authoritative_recognition_failed` 自 09-22 起刻意静音、交给值守，但值守当天 13:25（北京）用完每日 30 条上限，之后 15 条（含 #19514、#19597）全部被静默 | ① 加入必发清单；② 值守上限不再把「已恢复 / 诊断」跟进计入，auto_trade 群的高严重度开案不受上限约束 | L1（值守）/ L1（告警类型） |
| 4 | 舒琴 #19639「小幅跌破2520一点」被拒 | 止损字段能正确抽出唯一价 2520，但字段「标签白名单」不认识「跌破 / 下方 / 小幅 / 一点」等修饰词，整条判为不明确 | 在校验层把这类修饰词加入止损字段白名单，止损直接取 X；**不改识别提示词** | L2 |

## 1. 问题 1：改止损被「保护状态不明确」整体拒绝

### 1.1 生产事实

- 批次 184（陈哥 #19598，`break_even_by_market`）在预检失败：
  `protection_rows_unattributed_on_exchange:1001125406857038:1001125407523144,1001125407523252`。
- 这两张单是大漂亮 binding 388 两条限价入场腿的自带止损。13:44 时：

  | 入场腿 | 挂单号 | 状态（13:44） | 请求 (instId, posSide, sz, slTriggerPx) | 止损单 | 止损单的 WS `TU` |
  |---|---|---|---|---|---|
  | 662 | …523145 | pending（16:23 才成交） | BTC-USDT-SWAP, short, 4, 86700 | …523144 | 07:59:12 起只有 `default` |
  | 663 | …523253 | pending（至今） | BTC-USDT-SWAP, short, 7, 86700 | …523252 | 07:59:14 起只有 `default` |

  `TU=default` 是交易所在说「这张单还没有仓位」。16:23 腿 662 成交后，…523144 出现第二帧
  `TU=1001125407523145`，16:24 被 `protection_adoption` 按 TU 收编进账本（incident 2421），流程正确。
- 陈哥两张仓位是 **long**，大漂亮的挂单止损是 **short**；两者毫无关系，却因为旧匹配器的全局布尔值被一起冻住。

### 1.2 根因

`protection_attribution.match_position_protection` 里的 `unowned_order_present` 是全账户一个布尔值：
任何一张账本不认识的 TPSL 单都会让所有「有账本保护行」的仓位变成
`present_but_ambiguous (global_unowned_order_present)`。

阶段 6 的新模块 `protection_authority` 早已解决了同一个问题：
`resting_entry_attached_stop_order_ids()` 用「WS 帧的 TU 只有 `default`」**且**
「(instId, posSide, sz, slTriggerPx) 等于我们自己某条挂单入场腿的请求」两个条件认出挂单入场腿的自带止损，
并且 `trigger_backup_stop_executor` 已在生产上用它（16:24 大漂亮的备份止损就是在 …523252 挂着时加上的）。
但是下列三个地方仍直接调用旧匹配器、没有用这条规则：

- `strategy_management_executor.reserve_break_even_market_actions`（本次失败的地方，约 672 行）
- `strategy_management_executor._preflight_exact_protection_rows`（约 3364 行；`present_but_ambiguous` 在这里不走账本兜底，直接报 `protection_preflight_rows_ambiguous_or_drifted`）
- `strategy_management_planner`（约 940 行；这里对非 verified 有账本兜底，影响较小）
- `web_app`（约 2185 行，只影响页面显示）

### 1.3 修法

1. `match_position_protection` 新增两个可选入参（纯函数，不查库）：
   - `excluded_order_ids`：直接跳过的 TPSL 单（挂单入场腿的自带止损）。跳过不等于认领——它既不算任何仓位的保护，也不会被撤。
   - `order_trade_unit_pos_ids`：`order_id → TU 帧一致指向的唯一 posId`（取自 `_trade_unit_values` + `_sole_position_trade_unit`，与 `protection_authority` 同一规则）。
2. 「不明确」按仓位判定，替换全局布尔值。对一张账本不认识、也没被排除的 TPSL 单 R：
   - R 的 TU 一致指向仓位 P → 只让 **P** 不明确（其它仓位不受影响）；
   - 否则 R 有 `posSide` = S → 只让 **posSide = S** 的仓位不明确（仓位行缺 `posSide` 的按不明确处理）；
   - 否则（没有 posSide）→ 与现在一样，所有仓位不明确。
   仍然 fail-closed：凡是可能是这个仓位的止损，这个仓位就拒绝；只是不再株连明确无关的仓位。
3. 上面四个调用点在读到 `pending` 后，用同一个 session 算出 `excluded_order_ids`（调用
   `resting_entry_attached_stop_order_ids`）和 `order_trade_unit_pos_ids`，传给匹配器；
   `_unattributed_exchange_tpsl_order_ids` 报错时也排除这些单，报错里只列真正认不出的单。
4. 不改 `protection_authority` 的规则，不改收编流程，不做交易所写入。

### 1.4 回放用例（修复前失败 / 修复后通过）

用 13:44 的真实结构造测试（仓位、账本行、挂单行、WS 帧、入场腿请求全部按上表）：

- R1-a 陈哥 #19598：两张 long 仓位（…857038 / …857169）有完整账本保护；交易所另有 …523144、…523252 两张 short、TU=default、签名匹配两条 pending 入场腿。
  修复前：`protection_rows_unattributed_on_exchange`；修复后：两张仓位都 `verified`，批次进入 `set_break_even`。
- R1-b 大漂亮现状：…523145 short 仓位（账本 843–846）＋ …523252 挂着。修复前：`present_but_ambiguous`；修复后：`verified`，移止损 / 保本可执行。
- R1-c（fail-closed 保持）：同样场景，但多一张 short、无 TU 帧、签名不匹配的未知 TPSL → short 仓位仍拒绝，long 仓位不受影响。
- R1-d（fail-closed 保持）：未知 TPSL 无 posSide → 所有仓位仍拒绝。
- R1-e：TU 帧指向另一个仓位的未知单 → 只冻住那个仓位。
- R1-f：WS 帧缺失（从没收到过 TriggerOrder）→ 不排除，同方向仓位仍拒绝（「没有仓位」和「没听到」是两回事）。

## 2. 问题 2：「减 50% 仓位，剩余止损上移至 83200」被误拒

### 2.1 生产事实与根因

- #19597 原文：「目前BTC现价83800，加仓后浮盈600点，相当于正常仓位1200点收益，加仓后仓位比较大，减50%仓位，剩余仓位止损位上移至83200！」
- 模型 `observed_text`：「BTC现价83800；加仓后浮盈600点；减50%仓位；剩余仓位止损上移至83200。」
- `_authoritative_current_message_text` 把两段用换行拼接；`_percentage_values` 按「%」切段，第二段从原文「剩余」一直延伸到复述里的「减50」，取值「仓位止损位上移至83200！\n…减50」→ `management_fraction_invalid (retained_percentage)`。
- **第二道拦截（核对报告没有提到）**：只改 ① 以后，`resolve_management_directive` 仍会因为原文含「加仓」（「加仓后浮盈」「加仓后仓位比较大」）把它判成 `risk_increasing_fanout_forbidden`。本机用快照载荷回放已确认。两处都修以后回放结果是 `partial_then_break_even`，比例 0.5，止损 83200（`stop_price_source=current_message_text`），与 KOL 意思一致。

### 2.2 影响面比报告更大

用快照回放 09-09 以来全部 19 条 `management_fraction_rejected`：

- **10 条**是拼接造成的误拒（原文单独、复述单独都通过）：#16891、#16897、#17356、#17900、#17901、#17936、#18029、#18153、#18154、#19597。
  其中米娅群（auto_trade）#17900/#17901（「止盈40%，剩余仓位止损上移至80600」）、#18153/#18154（「止盈60%，剩余仓位止损位上移至64100」）在 09-20/21 同样被误拒；陈哥群 #17936 也是。
- 另有几条是**单条消息内**跨句错配：如 #18603「全部出局！…持仓收益高达370％」、#16454「统一止损位在2550！…持仓比例在15％以内」——整条消息（包括「全部出局」）因此被拒。

### 2.3 修法

1. `_percentage_values`：找到离百分数最近的数量动词后，若动词与百分数之间出现句子边界（换行、`。！!？?；;‼`），或出现「逗号前那一小段里带数字」的情况（如「平仓78031.7，盈利126.05%」），就认为这个百分数**不属于**这个动词，跳过；其余情况与现在完全一样（`-20%`、`50-60%`、`>100%` 等仍然拒绝）。
   这一条同时解决拼接错配和单条消息内跨句错配，且不需要改 `_authoritative_current_message_text` 的拼接（拼接本身是为了让图片里的文字也参与校验）。
2. `_RISK_INCREASING_TERMS` 判定前，把「加仓后」与现有的「平加仓」一样当叙述剔除。只剔「加仓后」这一种写法；「可以加仓」「加仓了」「补仓」等照旧算风险增加。
3. 跳过一个百分数的代价：如果真有「动词……句号……百分数」这种写法，比例会退回模型给的值或默认 50%。第 2.4 节用例覆盖正常写法不受影响。

### 2.4 回放用例

- R2-a #19597（原文＋真实 observed_text，走 `validate_management_fraction_payload` 和 `resolve_management_directive`）：修复前 `management_fraction_invalid` / `risk_increasing_fanout_forbidden`；修复后 `partial_then_break_even`、0.5、止损 83200。
- R2-b #17900、#18153（米娅历史误拒）：修复前拒绝；修复后分别 0.4 / 0.6。
- R2-c #18603「全部出局…370％」：修复后不再被比例校验拦。
- R2-d 回归（仍需拒绝）：`减仓-20%`、`止盈50-60%`、`保留120%`、`剩余100%`。
- R2-e 回归（仍需正常取值）：`止盈，70%`、`减仓约30%`、`分批止盈80%！留尾仓`、`保留剩余30％冲击止盈位`。
- R2-f 「可以加仓同等仓位」（#19514）仍判风险增加。

## 3. 问题 3：高严重度告警没发出

### 3.1 生产事实

- `/etc/telegram-kol-worker.env`：`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` 是白名单（8 类），`..._AFTER_ID=2069`。白名单会自动并入代码里的「必发」清单 `ALWAYS_NOTIFIED_INCIDENT_TYPES`，但要扣掉 `TELEGRAM_QUIET_INCIDENT_TYPES`。
- `management_fraction_rejected`：不在白名单，也不在必发清单 → **从来没投递过**。id>2069 且 `auto_trade_deliverable` 的有 8 条仍是 pending：2239、2241、2244、2267、2268、2284、2286、2414。（A-8c 的设计明明是「auto_trade 群要送达」，还专门把它路由到「Kol运行通知」，但漏了加入必发清单。）
- `authoritative_recognition_failed`（2408 #19514、2415 #19597）：自 09-22 起属于 `TELEGRAM_QUIET_INCIDENT_TYPES`，刻意不推送，改由值守覆盖。
- 值守确实开了案：case 28（D3，#19514）、case 30/31（D6c「从未通知」，2414/2415）。但 `alerts` 表里没有任何一条对应告警：
  `daily_alerts:2026-09-28 = 30`（上限 30），`daily_suppressed = 15`，`cap_reached` 通知在 05:25Z（北京 13:25）发出。
  当天 23:13Z（前一日）到 05:24Z 的 case 17–26 以及一次健康告警，每个案例都发了「开案 + 诊断 + 已恢复」，
  全部计入上限（`_note_capped_alert` 对跟进消息同样计数）。
  所以从北京 13:25 到午夜，所有值守开案都被静默；case 30/31 在第 59 分钟因「不再发生」自动 resolved，
  case 28 变成 stale——**同一天里再也没有机会补发**。

### 3.2 修法

1. `management_fraction_rejected` 加入 `ALWAYS_NOTIFIED_INCIDENT_TYPES`。notify_only 群的这类记录在写入时已经是 `suppressed`，不会多发。
2. 值守上限（`oncall_alerts.compose_case_alerts` 等）：
   - 「已恢复」「诊断」这两类**跟进**消息不再计入每日上限，也不受上限拦截（它们只跟在已经发出的开案后面，数量天然受开案约束）；
   - 高严重度、且案例属于 auto_trade 群或属于 D6c（告警从未送达）的**开案**不受上限拦截，但仍计数；
   - 其余开案维持上限 30。
3. `authoritative_recognition_failed` 仍维持静音、由值守覆盖（09-22 的裁定不动）；上面第 2 条保证值守这条路不会再因上限失效。见第 6 节 Q3b。

### 3.3 回放用例

- R3-a 配置按生产白名单 → `management_fraction_rejected`（auto_trade）被 `claim_next_runtime_incident_notification` 认领并投递到「Kol运行通知」；修复前认领不到。
- R3-b 值守：先用 30 条普通告警打满上限，再来一个 auto_trade 群 D3 高严重度案例、一个 D6c 案例 → 修复前 `alerted_at` 为空；修复后入队。普通 notify_only 案例仍被上限拦截。
- R3-c 30 条里如果含「已恢复 / 诊断」，不再占用额度。

## 4. 问题 4：「小幅跌破 X 一点」类止损（用户已裁定取 X）

### 4.1 事实与根因

- #19639 识别结果：`strategy.stop_loss = "2520下方一点"`，证据字段原文「小幅跌破2520一点」。
- `validate_candidate_entry_price_geometry` 先抽价（得到唯一值 2520，正确），再用字段标签白名单证明「除了价格没有别的内容」；「下方 / 跌破 / 小幅 / 一点」都不在白名单 → `entry_price_geometry_ambiguous (stop_loss)`。
- 本机复现：`2520下方一点`、`小幅跌破2520一点`、`跌破2520`、`2520下方`、`2520以下` 全部被拒，只有 `2520附近` 通过。

### 4.2 修法（校验层，不改提示词）

在止损字段的白名单里加入一组「模糊止损修饰词」：
`跌破 / 突破 / 涨破 / 破位 / 站上 / 有效 / 小幅 / 下方 / 上方 / 以下 / 以上 / 之下 / 之上 / 一点 / 一些 / 少许 / 左右 / 上下`。

- 止损直接取抽出的唯一价 X，**不加缓冲**（用户裁定）。
- 仍然拒绝：两个以上价格（「跌破2520或2510」）、相对写法（「跌破2520 20个点」「入场价下方30点」由既有的相对表达检测拦下）、没有数字（「跌破前低」）。
- 方向照旧由几何校验兜底：多单止损必须低于入场区间下沿，空单必须高于上沿。
- 选校验层而不是提示词：确定性、可单测、回放就能证明；提示词改动要等模型行为、也会打断「首次分析四分类」阶段 2 用提示词 v9 的统计。**本方案不改任何识别提示词**，所以不需要在 `docs/first-pass-classification-status.md` 标注改动时刻；如果审阅后改为走提示词，会补上。

### 4.3 回放用例

- R4-a #19639：多单，入场「2540-2553附近」，止损「2520下方一点」，止盈「2620附近/2700附近/2790」→ 修复前 `ambiguous`，修复后 `valid`，止损 2520。证据字段原文「小幅跌破2520一点」同样通过。
- R4-b 空单「突破86700一点」→ 86700 通过。
- R4-c 仍拒绝：「跌破2520或2510」「跌破2520 20个点」「跌破前低」；多单写「2560下方一点」（高于入场下沿）→ `stop_side_invalid`。

## 5. 风险级别、测试与部署计划

- 级别：问题 1、2、4 在执行路径上 → **L2**；问题 3 是告警 → L1。合并为一个候选，按 L2 处理。
- 不涉及：数据库结构、识别提示词、交易所写入语义、自动交易开关。
- 开发中跑相关的聚焦测试；最终候选上跑一次全量 `uv run python -m pytest -q`。
- 部署（由调度会话排期，本会话不部署、不推 `origin/main`）：
  1. 部署前核对候选是当前生产 HEAD 的后代；不在策略时效操作进行中部署。
  2. `tg-deploy <候选 sha>`（重启 worker → web → ingest）。
  3. 值守代码有改动，**要单独重启** `telegram-kol-oncall.service`（tg-deploy 不重启它）。
  4. 8 条旧的 `management_fraction_rejected` pending 行的处理见 Q3a。
  5. L2 观察：一个连续 30 分钟、≥5 条真实消息的窗口；重点看有没有新的
     `protection_rows_unattributed_on_exchange` / `global_unowned_order_present`、有没有新的
     `management_fraction_invalid`、incident 投递是否正常。
  6. 回滚：`tg-deploy 546d991346fd063d1c0d3098cf24de2589476fcd`，并重启值守。
- 大漂亮腿 2（…523253，85810×7）仍挂着：部署前若大漂亮发移止损 / 保本且被拒，仍需人工在交易所调整；部署后这一拦截消失。

## 6. 需要用户拍板

- **Q1（问题 1 的收窄范围）**：除了排除挂单入场腿的自带止损，是否同时做「按 TU / posSide 把不明确收窄到相关仓位」？
  推荐：**做**。只排除挂单止损能解决今天的两个案例，但下一次出现一张来源不明的 short 止损时，long 仓位还会被株连。
- **Q2（「加仓后」按叙述处理）**：是否同意把「加仓后」从风险增加词里剔除（只剔这一种写法）？
  推荐：**同意**。不改的话 #19597 修好比例后仍会被拒。
- **Q3a（8 条从未投递的旧告警）**：部署时怎么处理 2239、2241、2244、2267、2268、2284、2286、2414？
  - 选项 A（推荐）：部署前按主键把这 8 行 `notification_status` 改成 `suppressed`（小数据改动，记录前后计数），避免上线后一次性补发 8 条过时告警；
  - 选项 B：不动，上线后让它们各发一次。
- **Q3b（值守上限）**：是否同意第 3.2 节的上限调整（跟进消息不计数；auto_trade 高严重度开案和 D6c 开案不受上限拦截）？
  另一种做法是撤销 09-22 的静音，让 `authoritative_recognition_failed` 在 auto_trade 群直接由 worker 推送。推荐前者：不推翻 09-22 的裁定，也修好了「上限一满全天失声」这个更大的洞。
- **Q4（模糊止损词表）**：第 4.2 节的词表是否合适？是否要对称地支持空单「突破 X 一点 / X 上方一点」？推荐：词表照此，空单对称支持。

### 6.1 裁定（2026-09-28）

Q1 做；Q2 同意；Q3a 选 A（部署前按主键把 8 行标成 `suppressed`，记录前后计数，属部署步骤，不是代码）；Q3b 同意；Q4 同意（空单对称支持）。
