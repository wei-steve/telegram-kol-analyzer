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

## 8. 部署当天发现的另一个缺口（未修）

当时挂在交易所上的四张单（ETH-USDT-SWAP，群「大漂亮社区-11分组」）：

- 两张**未成交的限价开空单** `…417342` / `…417388` —— 页面显示「已绑定」，正确。
- 两张**它们各自自带的止损** `…417341` / `…417387`（触发价 3100）——
  页面显示「未归属 · 缺少 position_protection_ledger 强证据」。

只读核对确认：这两个 order_id 在 `position_protection_ledger` 里**没有行**。
原因是结构性的，不是本次修的那个 bug：`position_protection_ledger.pos_id`
是 `NOT NULL`，而入场尚未成交时**根本没有 posId**，所以一张「未成交入场单自带的
保护单」在今天的账本里没有任何可落脚的载体。于是它必然落到未归属，
而页面的「自动管理已冻结」是**诚实的失败关闭**，不是误判。

**观察窗内它又发生了一次。** 2026-09-23 11:22，群「大镖客-11分组」的两腿限价开多
`…572797` / `…572807` 连同各自自带的止损 `…572796` / `…572806`（触发价 2700）挂上，
形态与前一组逐字相同：入场单「已绑定」，自带止损「未归属」。**两腿入场每发生一次，
就多出两条这样的行。** 在无持仓期间，页面上的「未归属保护单」几乎全部是这一类，
所以用户看到的「经常归属不上」，主要来源大概率是这个缺口，而不是本次修的那个。
两者都是真的，但它们是两件事，本次只修了其中一件。

注意这两对 id 相差 1（`…341`/`…342`、`…387`/`…388`），正是 `README` §硬性禁止
第 1 条点名的分配模式——**不可以据此认领归属**。要修只能新增一个「入场腿自带保护」
的归属载体（按 `client_order_id` 或入场腿 id 记账，成交后回填 posId），
那是一次独立的、需要单独批准的改动——而且**要先查清一件事**：下单回执里到底给不给
这张自带 TPSL 的 order_id。若不给，我们就根本无从记账，能走的只剩回读挂单表，
而按 id 相邻去认领是明令禁止的。所以这件事的第一步是只读调研，不是改代码。

## 9. WebSocket 侧的只读核对（2026-09-23）

用户指出 REST 那一半早就查过：**入场单未成交时，回执不返回自带止损的 id**。
剩下的问题是 WS 给不给。生产 `deepcoin_ws_events` 里有现成答案，按
`order_sys_id`（该列有索引）直接查那四张单，不需要扫表。

**WS 给 id。** 四张自带止损每一张都有一帧 `TriggerOrder / PushTriggerOrder`，
`OS` 就是它自己的 order id：

```
656 Order        PushOrder         OS=…572797   (入场腿，L=OS，P=2762，V=1.5)
657 TriggerOrder PushTriggerOrder  OS=…572796   (自带止损，SLT=2700)
659 Order        PushOrder         OS=…572807   (入场腿，P=2732，V=1.6)
660 TriggerOrder PushTriggerOrder  OS=…572806   (自带止损，SLT=2700)
```

**但 WS 不给关联。** `PushTriggerOrder` 的 `data` 只有
`OS / I / D / o / OPT / OT / TO / TS / Tr / SLT / IT / U / l` 加账户字段——
**没有 `L`（LocalID）、没有 `PositionID`、没有任何指向父订单 `OS` 的字段，
连 `V`（数量）都没有**。全库统计把这条钉死：

```
TriggerOrder  PushTriggerOrder  n=293  带 position_id 的: 0
Position      PushPosition      n=140  带 position_id 的: 140
```

**293 条触发推送，带仓位 id 的一条都没有。**

### 为什么"那就按价格/时间配"也不行，这次有现成反例

大镖客那两腿的自带止损 `…572796` 与 `…572806`：同一合约、同一方向、
**`SLT` 都是 2700**，帧里没有数量可以区分，时间戳只差 1 秒，`OS` 相邻。
也就是说即便破例允许按属性配对，**这两张也分不出谁属于哪条腿**——
而 `OS` 相邻、时间接近正是 `README` 硬性禁止第 1 条点名禁掉的两种。

### 由此得到的边界

- **腿级归属：做不到。** REST 不给 id，WS 给 id 不给关联，属性无法区分同价两腿。
- **集合级归属：也许可以，但需要单独批准。** 我们是唯一写方且下单串行
  （`worker_command_jobs`），所以"在我们自己的一次提交前后对挂单表做差集，
  新出现的触发单就是这次提交产生的"是执行边界内的集合证据，而不是按价格
  或时间接近去认领。它能把这类单标成「大镖客 · ETH long · 入场自带止损
  （腿级未定）」，**只用于展示，绝不作为撤单/改单授权**。
  这是一个新的归属来源，按硬性禁止第 1 条的精神必须由用户单独批准。
- 现状（显示未归属、自动管理冻结）是**诚实的失败关闭**，不是误判。
