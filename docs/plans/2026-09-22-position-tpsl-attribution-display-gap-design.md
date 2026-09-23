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

---

## 7. 部署记录（2026-09-22）

| 项 | 值 |
|---|---|
| 部署 sha | `75da0b4ef2fc3890e9ea565ef52a67c7724cdf7d` |
| 回滚 sha | `d4b77b23347a1524dbe1426743a07dd5b87cc257` |
| 回滚命令 | `tg-deploy d4b77b23347a1524dbe1426743a07dd5b87cc257` |
| 候选分支 | `origin/claude/tpsl-attribution-display-2026-09-22` |
| 全套测试 | 9685 passed / 4 skipped |

四步都做了：推自己分支 → `tg-deploy` → 把**同一个 sha** 推共享分支 → 双向核对。
部署前核对候选是生产 HEAD 的直系后代（PASS）；部署后 `PROD == SHARED` 且
`OFFENDERS` 为空（PASS）。**该 `OFFENDERS` 检查另用一个必然 FAIL 的输入
（旧生产 sha 对候选）验过它自己会说 FAIL**，不是只跑了会 PASS 的那一半。

重启后 worker / web / ingest 三个单元均 `active`，`/positions-panel` 与
`/positions-panel/tabs/open-orders` 均 200，窗口内 web 与 worker 日志无
traceback / CRITICAL。

### 正向实盘样本：未到达，照实记

部署时生产 **0 持仓**，历史 `verified` 账本行 345 条**全部**落在
`leg=manually_closed|closed` + `binding=closed` 上。也就是说本次修复针对的那三种
活跃状态（`partially_filled` / `filled` / 绑定 `open`）此刻在生产数据里一个样本都没有，
**修复的正向效果尚未被实盘证明**，只被测试证明。下一个真实持仓 + 保护单到达时，
要用 `audit-tpsl-ownership` 与页面逐笔对照，两者必须给出同一个答案。

账本 `status` 直方图（只读）：`verified` 345、`retired` 266、`cancelled` 102、
`protection_missing` 47、`stop_trigger_failed` 1，**`protected` 为 0**——
所以把页面放宽到 `{verified, protected}` 目前不改变任何一行的结果，
它修的是判据一致性，不是当下的数据。

## 8. 入场自带止损的归属：改造已经解决，机制是 WS 的 `TU`

> 本节取代了三份早先的错误记录。2026-09-22 的初稿曾断言「未成交入场单自带的保护单
> 没有归属载体，是结构性缺口」，并据此说「WS 给 id 但不给关联」。**两条都错**，
> 错因是查错了列：`deepcoin_ws_events.position_id` 只承接 `Position` 帧的 `PI`，
> 而 `TriggerOrder` 帧的归属证据在 **`TU`**。错误的原文已删，结论以本节为准。

### 事实

REST 那一半用户早就查过：**入场未成交时，下单回执不返回自带止损的 id。**
WS 这一半的答案是：**给 id，也给归属**——只是归属那一步要等成交。

```
TriggerOrder 帧 293 条：TU 为 posId 形态 219，TU = "default" 74
```

入场未成交时 `TU = "default"`（此刻根本不存在可归属的仓位）；入场成交后交易所把
`TU` 改成 posId 重推一次，那一次推送就是确定性归属证据。

### 阶段 6e 正是为它建的

`protection_adoption.py` 的模块头把问题写得一字不差：迁移后的限价入场把
`slTriggerPx` 带在订单本身上，交易所在成交那一刻挂上这个止损，
**而它的 order id 从不出现在下单回执里**。既有两条入场保护记账路径
（`entry_protection_response` 读回执、`execution_bindings` 的采纳路径要求
`order_kind == "trigger_limit"` 且请求同时带 `tpTriggerPx` 与 `slTriggerPx`）
都会漏掉它。2026-09-10 的生产后果：两个活仓各自在交易所上有止损、账本一行没有、
旧匹配器报 `absent`、`backup_stop_blocked` 被记了两次。

6e 不放宽旧闸，另开一条以阶段 6 证据为准的路：

```
TriggerOrder 帧上 TU == posId
  + 该 ordId 确实在 trigger-orders-pending 里
  + 合约与 posSide 一致
  + triggerOrderType == "TPSL"
```

采纳行标 `evidence_source = "exchange_adopted_by_tu"`，**只写账本、不发交易所请求**。

### 闭环核对（只读，2026-09-23）

流里出现过的 `(OS, TU=posId)` 去重配对共 **108** 个：

| 结果 | 条数 |
|---|---|
| 账本有行且 `pos_id` 与 `TU` **一致** | **101** |
| 账本有行但 `pos_id` 不同 | **0** |
| 账本无行 | 7 |

## 9. 那 7 条「无账本行」逐条查清了，没有一条是该记而没记

`TS`（TriggerStatus）解释了一切。正常保护单的形态是 **TS=1 挂上 → TS=4 随平仓一起退场**，
被触发的那张走 **TS=2**。两个「6e 之后」的疑似漏网都不是这个形态：

**09-15，posId `…277441648`：**

```
03:06:11  OS=…277445871  TS=1  TP 77200        ← 账本有行
06:53:12  OS=…277445871  TS=2  TP 77200        ← 这张 TP 被触发
06:53:12  OS=…279571459  TS=0 → TS=4  TP 77200 ← 同一秒生灭，账本无行
```

**09-21，posId `…370127138`：**

```
15:03:34  OS=…370127711  TS=1  SL 87200        ← 账本有行
20:26:32  OS=…370127711  TS=4, TS=2  SL 87200  ← 止损触发，仓位平掉
20:26:32  其余三张 TS=4                         ← 一起退场
20:26:32  OS=…373782707  TS=0 → 1 → 3  SL 87200 ← 同一秒生灭，账本无行
```

两条都是**保护单被触发的那一瞬间交易所生成的执行侧对象**：与被触发的那张同价、
同方向、同合约，在同一秒内从 `TS=0` 走到 `3`/`4`，**从未作为 pending 保护单存在过**。
6e 的核验条件「该 ordId 确实在 trigger-orders-pending 里」正确地拒绝了它们——
这不是漏接，是失败关闭按设计生效。

余下 5 条（09-07 ×3、09-09 ×2）全部早于阶段 6 收口（2026-09-12，生产 `1fd45bc2`），
是历史遗留。

**结论：阶段 6e 上线之后，没有一条该记而没记的保护单。这条链是闭环的。**
本文第 1–7 节修的是另一件事——展示层把账本查询收窄了，与本节无关。
