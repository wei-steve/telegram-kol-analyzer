# 阶段 6 前置（三项，按顺序）

状态文件：`docs/rest-ws-trading-status.md`。阶段 6 改保护动作的授权依据，是全项目风险最高的一步，
在它之前先补齐三项。每项一个会话，先领取。

## 6-pre-1：WS 缺口期间的入场改为可重试的推迟（L2，无需单独批准）

分支 `rest-ws/phase-6-pre-1-ws-gap-defer`。现状：`deepcoin_entry_admission` 在 WS 未订阅或缺口时以
`ws_observation_blocked_new_entry:<原因>` 抛错，入场直接不提交，每次 tg-deploy 重启的十几秒内到达的入场会丢。
改为：把该入场指令项置为推迟态（原因 `ws_observation_pending`，`visibility_next_attempt_at` = 30 秒后，
`execution_deadline_at` 沿用现有入场 deadline），交 A-3d 修好的 `entry_admission_reconciler` 到点重试；
deadline 内 WS 回到 healthy 即提交，到期则走 `entry_admission_expired` 告警。准入门本身的 fail-closed 语义不变。
测试：缺口→推迟→恢复→提交；缺口持续→到期告警；healthy 时不受影响。部署后观察按 L2。

## 6-pre-2：市价成交裸仓安全网 B-5d（L3，改交易所写入语义，需用户单独批准）

分支 `rest-ws/phase-6-pre-2-naked-fill-net`。触发条件：市价腿归属 `unverified` 超过 60 秒，且该 instId+side 上恰有
一个无人认领（不属于任何 binding / leg / 保护账本）、数量恰等于本单成交量的活跃仓位。动作：只挂止损不挂止盈
（止损价取本单 draft 的止损），不认领所有权，attribution 标 `unverified_sl_by_unique_candidate`，记 critical 告警。
不满足唯一性则只告警。历史 153/153 市价成交 posId == ordId，此网预期不触发，是"以损定量"前提的兜底。

## 6-pre-3：补测第 10 项，修改 TPSL 后 OS/TU 是否稳定（只读观测，无需批准）

分支 `rest-ws/phase-6-pre-3-tpsl-modify-observation`。用生产自然发生的 `set-position-sltp`（A-5 的止损缩量、
止盈收敛的重建、保本移动）作为观测样本：对照 WS 收件箱里修改前后的 `TriggerOrder` 帧与 REST
`trigger-orders-pending`，回答 `OS` 是否同一个 ordId、`TU` 是否仍等于 posId、行是被更新还是被替换、新旧有无可查关联。
至少 3 个样本；样本不足则挂监视器等，不为观测下单。结论写进阶段 6 文件前置一节，并 send_message 给指挥会话。
若 `OS` 改变且新旧无关联，阶段 6 的设计前提不成立，停下重新设计。

## 6-pre-4：静默到点先确认"没漏掉东西"，再决定是否重连（L2，6-pre-1 量化后新增）

分支 `rest-ws/phase-6-pre-4-silence-probe`。现状：应用层静默计时器 600 秒到点就重连 + 完整重同步。
6-pre-1 的只读量化：过去 24 小时 **145 个 WS 缺口、累计 1060.4 秒、占全天 1.23%**，
其中 **134 个是 `silence_timeout`**、只有 11 个是 `process_start`。也就是说系统每天有 1.2% 的时间
在自己制造缺口，而缺口期间新入场会被推迟（6-pre-1 之前是被丢弃）。

**探活的语义是"证明这段静默期我们没有漏掉任何东西"，不是"证明订阅还活着"。** 后者做不到，
原因两条，都已核实：
1. **Deepcoin 没有 listenkey 续期端点**，客户端只有 `/deepcoin/listenkey/acquire`；
   `deepcoin_private_ws.py` 的注释是当初就写死的判断——本程序不发明请求路径。所谓"续期"在本仓库里
   一直就是**计划内重连 + 重新 acquire**。且 2026-09-06 实测：key 是**硬 60 分钟不是滑动窗口**。
   即便调 acquire 拿一把新 key，那是给**新连接**用的，只说明 REST 凭据有效，
   与"现有这条 WS 上的订阅是否仍被路由给我们"无关——是**无效探针**。
2. **静默期间 REST 快照与本地状态"无差异"是必然的**：本地状态本就由之前的 WS 事件建起，
   而"没有新事件"正是静默的定义。原设计注释说得准：
   *活着的 pong 只证明 socket 开着，不证明业务流还在路由给我们。*
   **在没有业务事件流过时，无法从外部证明订阅还活着。**

**所以改成能被证明的那一件事**：静默到 600 秒时做一次只读快照与本地账本对照——

- **一致** → 即使订阅已经死了，这段时间也**没有我们漏掉的业务变化**，于是重置静默计时、**不重连**。
- **不一致** → 我们确实错过了东西（订阅死了或漏帧），**立即重连 + 走现有 `deepcoin_ws_resync` 完整重同步**，
  并把差异内容写进缺口统计。这比"到点就重连"更早、更有针对性。
- **读失败 / 限速** → 硬性禁止第 4 条：读不到不等于没事，**照旧重连**。

**对照口径**（避免踩已知的坑）：用 `posId` / `ordId` 集合与 `sz`、`posSide`；
**不用 `slTriggerPx`**（ARCHITECTURE 第 6 节：仓位行的该字段只反映最近一对 TPSL，判不了有没有止损）。
`trigger-orders-pending` 按既有分页取全。**若本地有未了结的普通挂单腿**再加一次 V2 `orders-pending`，
否则不加，**最多三个 GET**。

**残留风险，如实写明**：本方案不能排除"订阅已死但恰好无事发生"。那种情况下不重连**不产生任何信息损失**，
而暴露窗口的上界是**下一次探活（600 秒后）或 60 分钟硬过期的计划内重连，二者取先**。
60 分钟硬过期的计划内重连、ping/pong 超时与真正断线（socket 关闭、`50118`）的立即重连，**全部不变**。

**其他约束**：探活受 5/s 限速与既有 REST 读预算约束，**复用现有客户端、不加新查询通道**，
与入场准入、各 reconciler 共用限速器，**不占入场读预算的紧急额度**；探活频率上限每 600 秒一次；
**探活本身不写交易所**。探活期间 WS 状态仍算 `healthy`，不触发入场推迟（探活不断开连接）。
每次探活结果（pass / fail、耗时、原因、差异摘要）写进现有 WS 缺口统计。

测试：探活一致不重连、快照不一致重连、读失败重连、真正断线立即重连、探活限频、
60 分钟硬过期路径不受影响。部署后按 L2 收窗，再挂 24 小时缺口统计（数量、秒数、占比）
对照基线 **145 次 / 1060 秒 / 1.23%** 作为追记，不阻塞阶段完成。

## 6-pre-5：撤单回执丢失后自动确认（L2）

改单批次撤旧单时 POST 回执丢失（`submit_unknown` / `revision_cancel_outcome_unknown`）会让批次停在 `recovery_required`
且永不自愈（2026-09-09 批次 7）。改为：回执未知时在下一轮 reconcile 用 `trigger-order-history` 的 `uTime` / `triggerTime`
与 `trigger-orders-pending` 是否仍在来确认撤单结果；确认已撤 → leg cancelled、批次继续；仍挂着 → 按原意图重试撤单一次；
两者都读不到 → 保持 recovery_required 并告警。

## 6-pre-6：入场改单授权租约不得成为死锁（L2）

2026-09-09：批次 7 的持有者（`legacy-raw:15633`）在批次进入 `recovery_required` 后从未归还
`trading_settings.entry_revision_exchange_authority` 租约；下一个申请者（raw 15668，峰哥 BTC 限价多）发现过期后把文档翻成
`blocked`，而 `blocked` 没有任何自动或人工复位路径，此后所有改单被拒，直到指挥会话批准一次性复位。改为：
(1) 批次进入 recovery_required / resolved / blocked 等任何终态时必须归还租约；(2) `blocked` 超过 10 分钟且无存活持有者时
自动复位为 idle 并告警（ALWAYS_NOTIFIED）；(3) `acquire` 见 blocked 时先检查前持有者进程是否仍存活——为此 `_blocked_document()` 必须把 `held` 文档的 `owner_pid` 与 `owner_start_ticks` 一并带过去（目前只留 token_sha256 与 prior_owner_kind，翻成 blocked 后查不出持有者）；idle 文档键集固定为 {schema_version, state, generation, released_at}，任何额外字段会让文档被判非法，复位理由只能写进审计行；
(4) 授权过期被拒记为确定性拒绝（A-6c 已在边界侧处理）。

## 6-pre-7：新入场路径的租约同形洞（L2）

`recovery_live_submit` 的新入场路径在 `attempted_writes > 0` 时不释放 `entry_revision_exchange_authority`，持有者形如
`signal:<trade_signal_id>`，没有批次行可证终态，6-pre-6 的收尾扫描明确跳过它。用 `trade_signals` 的终态
（succeeded / failed / expired / voided 等）做同样的按 generation 归还扫描；运行中的信号不扫；静态守护限制调用者；写审计。

## 完成条件

七项完成后状态文件 `current_phase: 6`（顺序：6-pre-5 → 6-pre-7 → 6-pre-4），等待用户对阶段 6 的单独批准（批准前须向用户出示阶段 5 的逐笔保护确认与第 10 项结论）。
