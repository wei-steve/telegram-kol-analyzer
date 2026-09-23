# 持仓页止盈止损单「无法归属」的展示层缺口

**日期**：2026-09-22
**风险等级**：L1（纯展示读取路径；不改交易所写入语义，不改 `can_mutate`，不改保护单账本写入）
**触发**：用户观察到「交易执行台 → 持仓」页长期、反复出现归属不上的止盈/止损单，
而 REST+WS 改造后保护单归属本应是确定性的。

---

## 1. 归属证据本身是对的，被展示层自己挡住了

`trigger-orders-pending` 返回的 TPSL 行**没有 `posId`**——这是实读结论，写在
`deepcoin_trigger_rows.py` 模块头：该行只带 `instId`、`posSide`、
`slTriggerPrice` / `tpTriggerPrice`。所以「这张保护单属于哪个仓位」这个问题，
**唯一**的答案来源是保护单账本 `position_protection_ledger`
（`venue + order_id` 唯一索引 → `pos_id`）。

账本自己的判据只有两条（`protection_ledger.py:15`）：

```python
ACTIVE_OWNERSHIP_STATUSES = frozenset({"verified", "protected"})
```

（本次把它从 `_ACTIVE_OWNERSHIP_STATUSES` 改成公开名并加上注释，正是为了让
需要同一个答案的读者引用它，而不是各自重述一遍。）

`build_account_protection_ownership` 按 `venue`、`status`、`order_id`、`pos_id`
认人，**不看腿、不看绑定、不看价格方向时间**。只读审计器
`tpsl_ownership_audit.load_readonly_protection_ledger` 就是这么用的：
按 venue 取账本行，其余交给上面那个函数。

持仓页没有这么做。`web_app._load_deepcoin_live_position_rows` 在查账本**之前**
先算了一个 `verified_live_leg_ids`，然后拿它当过滤条件：

```python
verified_live_leg_ids = {          # leg.attribution_status == "verified"
    ...                            # and leg.status == "active"
}                                  # and binding.status == "active"
                                   # and has_authoritative_persisted_position(leg)
ledger_rows = (... .filter(PositionProtectionLedger.execution_order_leg_id
                           .in_(verified_live_leg_ids))
                  .filter(PositionProtectionLedger.status == "verified") ...)
```

于是三道与「这张单是谁的」无关的闸挡在前面：

| 页面要求 | 与之冲突的真实状态 | 后果 |
|---|---|---|
| `leg.status == "active"` | `"partially_filled"`（`execution_bindings.py:1098` **刻意保留**，两腿入场的常态）、`"filled"`（改单成交，`entry_revision_executor.py:1034`）、`"restored"` / `"recovery_required"` | 该仓位**全部**保护单变「无法归属」 |
| `binding.status == "active"` | 同一函数下方 `binding_is_live` 用的是 `{"open", "active"}` | 页面内部两套判据自相矛盾 |
| `ledger.status == "verified"` | 账本认 `{"verified", "protected"}`；`protection_authority`、`protection_snapshot`、`runtime_incident_scanner`、`break_even_convergence_executor` 用的集合都更宽 | 页面是全仓库最窄的一个 |

任一条不满足 → 账本行被 SQL 丢掉 → `position_tpsl_display` 拿不到 owner →
订单本身又没有 `posId` → 落进「无法归属」。

**同一时刻、同一份数据，`audit-tpsl-ownership` 会把这些单算进
`owned_pending_order_ids`，页面却把它们列进「未归属交易所保护单」。**

## 2. order_id 口径漂移

账本记的 `order_id` 与页面读的 `order_id` 必须是同一个字符串。仓库里有两套别名表：

- **多数派**（`position_tpsl_display.py:96`、`tpsl_ownership_audit.py:71`、
  `native_tpsl.py:125`、`deepcoin_order_matching.py:103` 等十余处）：
  `OrderSysID, ordId, orderId, order_id, algoId, triggerOrderId, id`
- **少数派**：`web_app._exchange_order_row:3347`（「当前委托 / 历史委托」两个分页的行构造）
  与 `protection_attribution.match_position_protection:216`——**都漏了 `OrderSysID`**

Deepcoin 官方 TriggerOrder 结构的订单号字段就是 `OrderSysID`
（`docs/2026-09-05-deepcoin-api-deterministic-link-research.md` §TriggerOrder）。
只要某条路径的响应体是 PascalCase，少数派读到的就是别的字段甚至 `None`，
与账本永远对不上。

## 3. 本次做什么

**一条原则：页面要回答的是「这张挂单是谁的」，不是「哪些入场腿还活着」。
所以按挂单表里的 order_id 反查账本，不要按腿正查。**

1. **新增共享读取器** `deepcoin_trigger_rows.order_id_or_none(row)`——多数派别名表，
   一个函数。`position_tpsl_display` 与 `web_app` 都改用它，别名表不再各写各的
   （与该模块既定意图一致：「Import one of these instead of writing a key tuple」）。
2. **持仓页**：用挂单表里的 order_id 集合反查账本
   （`venue='deepcoin' AND order_id IN (...)`，走 `uq_position_protection_ledger_venue_order`
   唯一索引，行数等于当前挂单数，不是全表扫描），状态放宽到账本自己的
   `{"verified", "protected"}`，**归属判定去掉 leg/binding 活跃性前置**。

   **但「归属」与「保护」是两个问题，只放宽前者。** 原来的严格集合
   （腿 `active` + 绑定 `active` + 账本 `verified`）保留下来作为
   `mutation_scoped_order_ids`，于是保护单行有三种状态：

   | 状态 | 含义 | 是否计入「这个仓位有没有止损」 |
   |---|---|---|
   | `已验证归属` | 交易所自报 posId，或账本归属且入场腿仍是活跃可变更的那条 | 是（行为与改动前完全一致） |
   | `已归属（未计入保护）` | 账本指名了确切仓位，但入场腿已 `closed` / `filled` / `partially_filled` 等 | **否**，失败关闭 |
   | `无法归属` | 账本无行，订单也没有 posId | 否 |

   第二态是本次新增的。它存在的理由是 posId 可能被交易所复用：腿关掉之后，
   账本仍能说出这张单是谁下的，但不能再证明交易所此刻用同一个 posId 报出来的
   仓位就是当初那一个。所以**卡片上照常列出并标明归属**（用户要的），
   而 `_summarize_verified_exchange_protection_rows` 只认第一态，
   `tests/test_web_app.py::test_execution_dashboard_does_not_use_ledger_for_closed_entry_leg`
   这条既有判据一字未改地继续成立。
3. **当前委托 / 历史委托分页**：`_attach_exchange_order_bindings` 的账本查询同样
   放宽到 `{"verified", "protected"}`；`_exchange_order_row` 改用共享读取器。
4. 删掉死代码 `web_app._exchange_protection_display_rows`——它是靠 `closePosId`
   的旧实现，生产路径早已换成 `position_tpsl_display`，只剩测试在调。
   留着它会让人以为归属还走 `closePosId`。

## 4. 本次明确不做

- **不动 `match_position_protection`**（含它漏掉的 `OrderSysID`）。它的结果流向
  `protection.can_mutate` → 保护单改写/撤销闸门，属于交易所写入语义，
  按 `AGENTS.md` 需单独批准，且它本就是阶段 7 要退役的对象。
  因此 `exact_order_position_ids` 继续由**原来那套严格集合**生成，
  与展示用的 ownership 分开两份，严格的那份传给匹配器，宽的那份只进展示。
- 不改账本写入、不改 `protection_authority` 的任何判据、不发起任何交易所写入。
- 不碰 `tpsl_ownership_audit`（它的别名表已经是多数派，改动只会扩大 diff）。

## 5. 验证（L1）

- 聚焦测试：参数化用例覆盖「腿是 `partially_filled` / `filled`、绑定是 `open`、
  账本状态是 `protected`」四种情形下保护单仍归属到正确仓位（四例在修复前全红）；
  腿 `closed` 时归属可见但不计入保护摘要；`OrderSysID`-only 的触发委托行
  在「当前委托」分页能与账本对上（修复前红）。
- 最终候选跑一次全套。
- 部署后按 L1 观察 15 分钟或 5 条真实消息，并在有实盘保护单时用
  `audit-tpsl-ownership` 与页面逐笔对照——两者必须给出同一个答案。

## 6. 遗留

`match_position_protection` 的 `OrderSysID` 缺口未修，它会让
`protection_status` / `can_mutate` 在 PascalCase 响应下失准。归入阶段 7
（`docs/plans/2026-09-06-deepcoin-rest-ws/phase-7-retire-legacy-matcher.md`），
或在用户单独批准改交易所写入语义时处理。
