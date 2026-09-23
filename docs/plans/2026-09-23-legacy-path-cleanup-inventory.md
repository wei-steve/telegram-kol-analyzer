# 旧链路与死代码盘点（2026-09-23）

**范围**：止盈止损归属这条主线上的旧链路，加上一次全包孤儿模块扫描。
**风险等级**：L1（删除零引用代码；不改任何在线判据）

---

## 1. 真正的「旧链路」删不得，不是没人管

`protection_attribution.match_position_protection` 是改造前的推断式匹配器
（按价格、数量、方向、时间距离猜哪张保护单属于哪个仓位），
`protection_authority_shadow` 是它与新绑定链的对照。两者都是
**阶段 7 的删除对象**，而阶段 7 的前置是 6b（撤销走新链）与 6g（自动管理走新链）
两个切换，**两者都在等一个真实样本，至今未到**，所以
`docs/plans/2026-09-06-deepcoin-rest-ws/phase-7-retire-legacy-matcher.md`
写明本阶段 `planned` 且**现在不可开始**：

> 在它们完成之前删掉旧匹配器，等于把那四条路径变成没有实现。

今天确认它仍在四条生产路径上（`strategy_management_executor` ×2、
`strategy_management_planner`、`deepcoin_execution_actions`）。**保留。**

它身上还带着一个已知缺口：`match_position_protection:216` 读 order id 时
**漏了 `OrderSysID`**，与全仓多数派别名表不一致。这个缺口流向
`protection.can_mutate`（撤单/改单闸门），属交易所写入语义，
同样归阶段 7，不在本次范围。

## 2. 影子模块全部在线，保留

| 模块 | 在线引用 |
|---|---|
| `protection_authority_shadow` | `web_app` |
| `deepcoin_shadow_binding` / `_diff` / `_ownership` | `web_app`、`models`、`cli`、`deepcoin_ordinary_entry_binding` |
| `break_even_shadow`、`naked_fill_shadow` | `web_app` |
| `management_cancel_precheck_shadow` | `strategy_management_executor`、`runtime_incident_adapters` |
| `context_resolution_shadow` | `context_resolution` |

## 3. 一次性修复模块：沿用 step-5 的既有判定，不重复裁决

2026-09-06 的清理步骤 5 已用 AST import 图（含函数体内延迟 import）从三个运行时根
做过传递闭包：243 个 `.py` 里 188 个在线，47 个候选判为
**keep-online 39 / move-one-off 1 / unsure 7**，口径是
**「没有执行记录就不算完成，宁可少动」**。

今天的扫描没有推翻它任何一条。例如 `frozen_exchange_empty_state_alignment`
（958 行、硬编码生产 lifecycle id）我一度以为是明显可删的一次性工具，
step-5 判的是 `unsure`，理由是**全仓文档零记录，既无计划也无执行**——
分不清它是「做完了」还是「造好还没用」，而删掉后者等于删在建工作。
这个判断是对的，**保留**。

## 4. 本次实际删除

### 4.1 `web_app._exchange_protection_display_rows`（已随 `75da0b4e` 上线）

靠 `closePosId` 的旧展示实现。生产路径早已换成 `position_tpsl_display`，
只剩测试在调。留着会让人以为归属还走 `closePosId`——而
`trigger-orders-pending` 的 TPSL 行**根本没有 posId**。

### 4.2 `deepcoin_symbol_capability`（155 行）+ 其测试（104 行）

**这是一个从未接线的并行实现，不是安全缺口。** 查证过程：

它和 `DeepcoinContractSpecProvider.lookup_contract_spec` 诞生于**同一个提交**
（`b1f13073`，2026-08-08，`feat: gate symbols by Deepcoin capabilities`），
判据逐条相同，**连 reason 字符串都一样**：

| 检查 | `decide_deepcoin_symbol_capability` | `lookup_contract_spec` |
|---|---|---|
| 无快照 | `contract_spec_sync_unavailable` | 同 |
| `now < fetched_at` | `contract_spec_invalid` | 同 |
| `now >= expires_at` | `contract_spec_stale` | 同 |
| 合约不在能力表 | `venue_instrument_unsupported` | 同 |
| `state != "live"` | `venue_instrument_not_live` | 同 |
| 通过 | `tradable` | `available` |

多出来的第一层（全局白名单）由
`recovery_order_confirmation.evaluate_deepcoin_entry_capability` 做，
它的 docstring 就是这件事的原话——*"Apply global allowlist first, then pin the
venue/spec decision."*——而且**比被删的那份更严**：它在 lookup 前后各取一次
snapshot，快照身份变了就拒（`contract_spec_invalid`），防止查询期间快照被换掉。

这道闸在**三条生产入场路径**上：`recovery_live_submit:524`（实际下单）、
`auto_trade_execution:979`（自动交易）、`recovery_order_confirmation:213`。

测试覆盖也不降：取代链的测试覆盖了除 `global_not_allowed` 外全部 reason，
而白名单那一层（`symbol_not_allowed`）在 `test_auto_trade_execution`、
`test_recovery_live_submit_gate`、`test_oncall_detector`、
`test_historical_state_repair` 四处有覆盖。

**结论：删除不留任何未被覆盖的检查。**

`docs/archive/plans/2026-08-08-deepcoin-dynamic-contract-specs.md` 里仍有它的
`Create:` / `Test:` 行——按 step-5 的口径，归档计划是**建造记录而非执行记录**，
它记录的是当时做了什么，是历史事实，不改。

## 5. 文档订正

`docs/plans/2026-09-22-position-tpsl-attribution-display-gap-design.md` 的第 8–10 节
一度断言「未成交入场单自带的保护单没有归属载体」并据此说「WS 给 id 但不给关联」，
**两条都错**（查错了列：`TriggerOrder` 帧的归属证据在 `TU`，不在 `position_id`）。
错误原文已删除，重写为一节正确的叙述，而不是留下「先读错的、再读更正」的结构。

## 6. 盘点中发现的真缺口：展示层没有执行 §4.8 的排除规则

`docs/ARCHITECTURE.md` §4.8 有一条明确规则：

> **挂在「尚未成交的限价入场单」上的止损要排除掉，它不是任何仓位的保护单。**
> 判据是两条**同时**成立才排除：(a) 该 ordId 的 `TriggerOrder` 帧 `TU == "default"`
> （仓位还不存在），且 (b) `(instId, posSide, sz, slTriggerPrice)` 等于我们自己某条
> **仍 pending 的入场腿**的请求四元组。**这是排除不是认领**——排除后既不进保护集合、
> 也不冻结、更不会被撤。

`protection_authority.resolve_protection_authority()` 实现了它
（`protection_authority.py:283-295`，`excluded.append(order_id)`），
影子按 `excluded_pending_entry_stops` 计数。2026-09-10 的实测理由也记在文档里：
binding 347 的两张在挂入场单，让**每一个 BTC 空头仓位**在整个影子窗口里被冻结 26 次。

**展示层完全没有这个概念。** `position_tpsl_display` 只问账本，账本没有就是
`无法归属`，于是同一批单在两条路径上得到相反的答案：

| | 保护权威链 | 持仓页 |
|---|---|---|
| 未成交入场腿自带的止损 | **排除**：不进保护集合、不冻结、不撤 | **无法归属**，列进「未归属交易所保护单」 |

2026-09-23 的实盘三张（`…385572806`、`…381417341`、`…381417387`）就是这种。
它们的 `TU` 都只有 `default`，对应入场腿都还挂着没成交。

**建议**：展示层复用同一条判据，把这类单从「未归属保护单」里挪出来，
单独标成「挂单入场自带止损 · 尚未成交」。判据已在生产跑了近两周，
展示层只是读它的结论，不新增任何推断。这是 L1，但它改变页面语义，
**需要用户点头后再做**。
