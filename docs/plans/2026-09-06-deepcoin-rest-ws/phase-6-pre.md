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

## 完成条件

三项完成后状态文件 `current_phase: 6`，等待用户对阶段 6 的单独批准（批准前须向用户出示阶段 5 的逐笔保护确认与第 10 项结论）。
