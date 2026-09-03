# Entry-assembly wakeup 重复执行只读核查

核查时间：2026-09-02（America/Vancouver；生产证据采集时间为 2026-09-03 UTC）

## 结论

> **结论（甲）：已确认当前生产代码存在下游持久化幂等保护；同一个
> entry-assembly wakeup 在“交易所已收到写请求、本地尚未完成、进程死亡”后被再次唤醒时，
> 不能第二次跨越 Deepcoin 写边界。**

决定性保护点不是 5 分钟 wake claim、普通进程锁、`execution_bindings`、
`execution_order_legs`，也不依赖 Deepcoin 是否按 `client_order_id` 去重，而是：

1. 当前生产的 multi-instruction 路径先把对应 `MessageInstructionItem` 由 `pending` CAS 为
   `executing`；进程硬退出会留下 `executing`。同一消息再次执行时不会 claim 新 item。
2. 即使走到没有 item 的 legacy 入口，entry 路径仍会按稳定业务键复用同一条 `TradeSignal`，
   并在任何 Deepcoin 写调用之前只允许 `status=pending` CAS 为 `processing`。
   交易所已经收到请求后发生进程死亡时，该行保持 `processing`；重放复用同一行并在 CAS
   处得到 `trade_signal_claim_failed:processing`，到不了交易所 adapter。
3. instruction execution contract 的 `submitting` 状态及 entry/revision exchange authority 的
   `held→blocked` 行为提供额外持久化阻断，但结论不依赖这两层。

因此，本轮按门禁进入设计文档修订。核查也确认 wakeup 自身仍缺少独立 side-effect fence：
当前安全性依赖更下游状态机，且进程死亡可造成永久卡住或静默失败；这不构成本轮发现的重复写路径，
但必须在 lease recovery 设计中补齐。

## 范围、方法与运行身份

- 代码基线：`7f1a1b803a777777012351056f616c257402da17`。
- 生产角色：Web=`5aa7ca077fa45728c0f3d8df93e0e90a33a4a262`；
  ingest/worker=`0de19c1cbb2089fd58b8940d9b01a65096f9a063`；三个角色的
  `loaded_artifact_verified=true`。
- 对本报告涉及的 worker 执行路径，逐文件比较工作区基线与 worker immutable release；相关源码
  SHA-256 一致。`models.py` 因后续 web-only 变更不一致，因此约束结论以生产 SQLite 的实际
  `sqlite_master`/`PRAGMA index_list` 为准。
- 生产 SQLite 只用 `mode=ro`/`sqlite3 -readonly` 访问；每次连接强制
  `PRAGMA query_only=ON`，busy timeout 为 1 秒；未建表、未写行。`PRAGMA quick_check=ok`。
- 未抓取 Web 页面，未调用任何交易所写接口，未更改 29 条 `execution_running` 行。

## 1. 从 wakeup 到 Deepcoin 写边界

### 1.1 wake claim 本身不是安全边界

`authoritative_recognition.py:1523-1545` 在主权威执行完成后 claim wakeup，随后直接对
`wake_claim.strategy_raw_message_id` 调用 `auto_trade_executor()`，最后才写 wakeup 成功或失败。

`entry_assembly_admission.py:653-675` 会把 `status=claimed` 且
`wake_claimed_at <= now-5min` 的 attempt 无条件改回 `pending`，清空 token 和 claimed time；
这里没有副作用判据。因此这一层单独看确实允许第二次进入 executor，不能作为幂等证明。

### 1.2 完整检查点

| 检查点 | 代码位置与确切判据 | 上次已跨交易所边界但未完成时能否阻止第二次写 |
|---|---|---|
| Entry assembly item release | `entry_assembly_admission.py:106-132, 745-766`：匹配 exact entry item；只有 `status=pending` 且 result 为 `deferred/adjacent_entry_context_pending` 才释放延迟。item 不为 pending 时 wake claim 被还原 | **能（当前生产 item 路径）**。硬退出留下 item=`executing`，不会再次调用 executor |
| MessageInstructionItem claim | `message_instruction_items.py:149-231`：只 CAS `pending→executing`；同 raw message 已有 `executing` item 时禁止再 claim | **能**。进程死亡不会自动把 `executing` 恢复为 pending |
| Instruction execution contract | `instruction_execution_entry_adapter.py:459-549`：写边界前 `pending→submitting`；`submitting`、`submit_unknown` 和终态均拒绝再次 prepare | **能（启用范围内）**。`submitting` 要求 reconciliation，不自动重放；生产该 contract 模式只覆盖 activation watermark 之后的 item，因此不作为唯一证明 |
| SignalCandidate | candidate 在这里提供执行输入；没有独立的“已消费”CAS | **不能单独保护** |
| TradeSignal 稳定复用 | `trade_signals.py:359-440`：由 venue/source/chat/message/symbol/side/action 构造稳定 `signal_uid`；已存在则复用，且不会重置状态 | **能，与下一行合并构成决定性保护** |
| TradeSignal claim | `trade_signals.py:507-533`、`recovery_live_submit.py:925-933`：仅允许同一信号 `pending→processing`，且注释明确无自动 crash recovery；该 CAS 在任何 Deepcoin 写之前 | **能，决定性保护**。post-write crash 留下 `processing`；重放在 CAS 失败，不能调用 adapter |
| Active execution binding gate | `recovery_live_submit_gate.py:26-130`：提交前拒绝同策略已有 active binding | **只能部分保护**。若 binding 已落库则阻止；若交易所收单后、本地 binding 前死亡则不能 |
| execution binding/order leg 存在性 | binding/leg 在得到交易所结果后逐步落库 | **不能单独保护** crash window；它们是结果证据，不是所有入口之前的原子门 |
| Entry/revision exchange authority | `recovery_live_submit.py:940-980, 1022-1032` 与 `entry_revision_exchange_authority.py:183-320`：写前持久化取得 `held` authority；只有确认 `attempted_writes=0` 的异常才释放；过期 held 会转永久 `blocked`，不能再取得 | **能，额外保护**。post-write crash 保持 held，之后只能 blocked。`mark_entry_revision_exchange_write_boundary()` 当前没有调用方，故本结论不依赖其 `write_boundary_reached` 字段 |
| Composite/revision claim | management/revision 路径有 durable batch/component claim，以及 `already_claimed`/`awaiting_exchange` 等阻断状态 | **能覆盖相应管理分支**；本次 entry wakeup 首先仍受 item claim 约束，不把这些分支作为 entry 的主证明 |
| `position_authority_lock`/source lock | `position_authority_lock.py:10-27` 等为进程内 `RLock` | **不能**。进程死亡后锁消失 |
| Deepcoin `client_order_id` | `execution_bindings.py:195-219`：由 strategy instance、purpose、leg index（或 KOL code/message ID）确定性生成 | 同一 instruction 重放会生成相同 ID；但代码只证明 ID 稳定，**不能证明 Deepcoin 一定去重**，交易所行为需进一步确认。本结论不依赖交易所去重 |

### 1.3 交易所写调用与事务顺序

entry 路径先执行 `claim_pending_trade_signal()`，再取得 entry/revision authority，随后才进入：

- market order：`recovery_live_submit.py:1324-1330` 的 `deepcoin_client.place_order()`；
- limit/trigger order：`recovery_live_submit.py:1419-1443, 1465-1471` 的
  `deepcoin_client.trigger_order()`。

`DeepcoinClient` 只是在 `deepcoin_client.py:323-337, 615-683` 发 POST。网络错误、HTTP 错误、
非 JSON 返回等会转为 `DeepcoinRequestOutcomeUnknown`；源码没有“同 client order ID 必然由交易所
拒绝第二单”的契约。因此不能把 client ID 当作安全证明。

若 adapter 正常返回，TradeSignal 由 `processing` 变 `submitted`；若捕获异常，按已跨边界的
进度进入 `unknown_exchange_outcome`、`partial_submission_failed` 等非 pending 状态。进程在这些
本地终结之前硬退出，则保持 `processing`。上述所有状态都不能再次通过 `pending→processing` CAS。

## 2. 指定故障时序的逐步证明

1. 第一次 wake 将 attempt 设为 `claimed` 并调用 executor。
2. 本次生产历史中的 7 条 EntryAssemblyAttempt 均有 durable instruction item；该路径先令 item
   `pending→executing`。若进程在交易所调用后死亡，该 item 保持 `executing`；五分钟后的 stale
   wake 在 item release 检查即停止。
3. 即使把 item 层视为 legacy 缺失，entry executor 仍按稳定 UID load/create 同一 TradeSignal，
   并在写前将其 `pending→processing`。
4. Deepcoin 收到请求后进程死亡，TradeSignal 最差保持 `processing`。下一次 executor 复用该行，
   CAS 因 observed status=`processing` 失败；Deepcoin 写函数不可达。
5. authority `held` 与 contract `submitting` 还会继续 fail-closed；普通进程锁、非唯一 client ID
   索引以及 Deepcoin 未确证的去重行为均不在证明链内。

所以在题设故障窗口内，第二个 Deepcoin 写请求不可能从当前部署代码发出。

## 3. 数据库约束核对

生产 DDL/索引实际值：

- `ix_execution_bindings_client_order`：普通索引，`unique=0`；
- `ix_execution_order_legs_client_order`：普通索引，`unique=0`；
- binding 的业务唯一约束为 `(venue, chat_id, message_id, symbol, side)`；
- order leg 的业务唯一约束为 `(execution_binding_id, purpose, leg_index)`；
- `uq_execution_order_legs_venue_pos` 只约束存在 `pos_id` 的 `(venue, pos_id)`；
- TradeSignal 对稳定 `signal_uid` 及 source/action identity 有唯一约束；MessageInstructionItem 对
  message/candidate identity 有唯一约束；instruction contract 对 item 有唯一约束。

因此，名字中带 `client_order` 的两个索引本身不阻止重复记录；真正的 pre-write 唯一身份与状态
CAS 是 TradeSignal 层。

## 4. 生产历史核对

### 4.1 EntryAssemblyAttempt 现状

全库共有 7 条：

| attempt_id | strategy_raw_message_id | attempt status | item_id | item status | contract_id/state |
|---:|---:|---|---:|---|---|
| 1 | 9997 | expired | 440 | succeeded | 未启用 |
| 2 | 10236 | expired | 460 | succeeded | 未启用 |
| 3 | 11194 | pending | 602 | failed | 63 / expired |
| 4 | 11817 | pending | 691 | failed | 114 / expired |
| 5 | 12265 | pending | 728 | failed | 134 / expired |
| 6 | 12587 | pending | 742 | failed | 148 / deferred |
| 7 | 12623 | pending | 745 | failed | 151 / deferred |

确认结果：

- 7/7 均有 exact matching durable MessageInstructionItem；
- 当前 `claimed=0`、`woken=0`、`wake_claimed_at IS NOT NULL=0`、`woken_at IS NOT NULL=0`；
- 5 条 pending 行的 `updated_at=created_at`，可确认从未走过 claimed→pending 重置；
- 2 条 expired 行后来发生过更新时间变化，但表没有 transition log，重置会清空 token/time，代码也
  不记录 reset 日志；现库与 journald 均不足以重建它们是否曾重置。因此“生产中实际触发 reset 的
  精确次数”为**无法从现有证据确认**，而不是按 0 填充；该缺口不影响上述状态机证明。
- 7 条中没有 wakeup 来源的自动 entry TradeSignal。attempt 2 对应策略后来有 binding/leg/event，
  但其 TradeSignal 来源为 `operator_manual_market_entry`，时间也不在 attempt wakeup 时段，不能归为
  wakeup 重放。

### 4.2 重复执行迹象

- instruction contract 多次进入 `submitting`：0 组；
- 重复 `execution_order_leg.client_order_id`：0 组；
- 重复 `(strategy_instance_id, purpose, leg_index)`：0 组；
- 同一 strategy instance 多条 execution binding：0 组；
- entry execution event 同 identity 重复：0 组；
- entry event 同 identity、相隔 240–900 秒的成对记录：0 对；
- 全部带 order/client identity 的 execution event 中有 40 个重复 identity 组：38 组同一时间、
  1 组小于 4 分钟、1 组相隔 508.982 秒。最后一组是 event 2904→2905、
  `cancel_reviewed_legacy_conditional` 从 `confirmed_pending_readback` 到 `confirmed` 的同一操作读回
  进展，不是 entry 写请求重复。
- `trade_signals.attempt_count>1` 有 26 条；其中 entry/open_position 仅 signal 1（raw 3619，
  5 attempts，发生于 2026-06-29），与 7 条 EntryAssemblyAttempt 均无关联。

历史数据没有发现 entry-assembly wakeup 造成重复交易所写的实例。该“零实例”是辅助证据；结论甲
的决定性依据仍是 current deployed path 的持久化 pre-write CAS。

## 5. 已确认与需进一步确认

已确认：

- 当前 5 分钟 `claimed→pending` 本身不安全，缺少 side-effect 判断；
- 当前生产下游 item 与 TradeSignal 两层均 fail-closed；TradeSignal CAS 是覆盖 legacy/no-item 的
  决定性屏障；
- 数据库的 client-order 索引不是唯一索引；
- 历史未见与 entry-assembly wakeup 关联的重复 entry 写迹象。

需进一步确认，但不影响结论甲：

- Deepcoin 对相同 `clOrdId` 的服务端幂等/拒绝契约；
- 两条 expired EntryAssemblyAttempt 是否曾触发过 stale reset，以及确切次数。现有表会抹除该证据，
  journald 也无对应日志。

本报告只回答当前部署路径是否可能因 wake stale reset 发出第二次写请求，不把“不会重复写”等同于
“不会丢失执行”或“状态一定能自动恢复”。后两项仍是 lease recovery 设计要解决的问题。
