# 37 条 uncertain 执行尝试的收口 + 扫描器噪音 · 设计稿

日期：2026-09-26
基线：`origin/main`，生产 HEAD `7b5f0053`
缘起：`recognition execution finding ... observe_uncertain` 每天约 2.7 万行 ERROR
状态：**设计稿，等批准**

---

## 1. 事实（全部用生产数据核过，不是推断）

### 1.1 噪音怎么来的

`recognition_execution_scanner` 每 60 秒扫一轮，只扫非终态的行；扫到末尾把游标
**归零重扫**（`_wrap_cursor` 写 `last_seen_id=0`）。目前非终态的只有 37 条
`authoritative_execution_attempts.status='uncertain'`，一轮扫完、下一轮空、再下一轮从头——
于是这 37 条每约两分钟被重报一次，**每天约 2.7 万行**，级别是 `ERROR`，
而 action 是 `observe_uncertain`：**只观察，什么都不做**。

### 1.2 这 37 条是什么

全部产生于 2026-09；**没有一条恢复过**（同一消息之后再没有成功的尝试）——
这是设计使然（跨过副作用边界的未知结果"永不可重放"），但也**没有任何路径去收口它们**。

按我们自己的账本（`execution_events`）分桶：

| 桶 | 条数 | 含义 |
|---|---|---|
| A | **29** | 该消息名下一条执行事件都没有——从未向交易所发过任何东西 |
| B1 | **6** | 只有 `management_target_confirmation_reminder` / `_timeout`，两者都在代码的 `NON_EXCHANGE_WRITING_EXECUTION_ACTIONS` 白名单里，是通知/审计行 |
| B2 | **2** | 真写过：`open_market_position` + `set_position_tpsl`（峰哥 ETH 多，09-06/09-07），对应绑定 340 / 342 **现均为 `closed`** |

**即 35 条从未联系过交易所，2 条联系过且仓位已了结。就交易所敞口而言这批积压已经全部落地。**

另外 34/37 的 `evidence_refs_json` 是空的，而代码自己的注释写着：A-6b 之后
"没有写入的拒绝应落到 `failed_safe` 而不是这里，所以一条没有写入的 uncertain
**是值得报警的矛盾**"。这批行正是那条注释想抓的东西。

### 1.3 这不是"限价单超时没触发入场"

那条路是 `pending_entry` 生命周期 + 3 小时超时复核，交易所上有真实挂单。
这 37 条连下单那一步都没走到（35 条从未写入），B2 那 2 条还是**市价单**。两者无关。

## 2. 收口的核心安全选择：**只收尝试行，不动决定行**

`recognition_decisions.comparison_status = 'execution_uncertain'` 会让该消息的任何
新识别写入抛 `AuthoritativeExecutionInProgress`——**消息被永久冻结、不可重新识别**。

所以：

- **收口 `authoritative_execution_attempts` 那一行**（它是扫描器读的表，噪音的来源）；
- **`recognition_decisions` 那 37 行一个字不动**，消息继续冻着。

这样收口**不可能触发任何重放**：解冻才会带来"9 月的策略被重新识别进而下单"的风险，
而我们根本不解冻。用户 2026-09-26 的原话是"时间太久了不要下单"，这条必须由结构保证，
不是靠小心。要有回归用例钉住：收口后该消息仍然不可重新识别。

## 3. 终态怎么给

不复用 `failed_safe`：它的语义是"副作用之前就拒绝了"，而这些行 `side_effect_started_at`
有值，硬套等于在账本上说谎。

新增终态 `closed_no_write`（名字可议），并在 `error_summary` 追加收口原因与日期。
影响面已核，只有两处消费者会看到差别：

1. `recognition_execution_scanner` 的扫描集合（`claimed/executing/outcome_recorded/uncertain`）
   ——收口后的行不再被扫，**噪音随之消失**；
2. `message_processing_backlog_expiry` 把 `('executing','uncertain','outcome_recorded')`
   算作"仍活跃"的守卫——收口后这些行不再挡住积压过期。**这是行为改变，要写进文档**，
   但方向是对的：一条早已无敞口的行不该再挡任何东西。

B2 那两条（真写过、绑定已 closed）**用不同的收口原因**（例如 `closed_settled_binding`），
与 A/B1 的 `closed_no_write` 分开，事后可分辨。

## 4. 工具形状：照抄仓库已有的先例

`worker-command-reconcile`（审计一条 uncertain worker command，只应用已确认的结果）
与 `expire-message-processing-backlog`（带 `--expected-*` 守卫的原子过期）已经定下了形状：

- 一条新 CLI 子命令，**默认 dry-run**，打印逐条清单（id / 消息 / 群 / 桶 / 拟定收口原因）；
- `--apply` 才写，并要求 `--expected-count`（与 dry-run 数出的条数一致才执行），
  防止"跑的时候库已经变了"；
- 输出里带 `exchange_write_count: 0`，与既有两条命令一致——**这条命令永不写交易所**；
- 分桶判定用 `execution_events` 与 `NON_EXCHANGE_WRITING_EXECUTION_ACTIONS`，
  不新写一套"算不算写入"的规则。

## 5. 噪音本身（即使收口完也要做）

收口只清掉现存的 37 条；**下一条 uncertain 出现时，同样会每两分钟喊一次**。所以：

1. **同一 `(family, row_id)` 的重复上报加节流**，形状照抄今天给删除退出加的那个
   （同一行在 N 分钟内只报一次；状态变化立即报）；
2. **级别从 `ERROR` 降为 `WARNING`**：一个 action 是 `observe_*` 的发现不该是 ERROR，
   它没有失败、也没有动作。真正需要人看的是"这条 uncertain 存在"，而那件事由
   runtime incident 承担（`capture_recognition_execution_state` 已经在写）；
3. 顺带确认 runtime incident 那一侧不会因为节流而漏记（它按指纹 coalesce，
   本来就是一条记录多次累加）。

## 6. 明确不做

- 不解冻任何消息、不重放、不下单；
- 不改 `mark_authoritative_execution_uncertain` 的冻结语义（那是正确的 fail-closed）；
- 不碰"限价单超时"那条路（3 小时超时复核）——两回事；
- 不追查 34 条证据为空的**根因**（边界追踪器为什么没记 evidence）——那是更深的洞，
  单独立项；本稿只收口既成事实并止血噪音。

## 7. 验证

- dry-run 先出 37 行清单，人眼过一遍再 `--apply`；
- 收口后：扫描器那条日志应当归零（当前 37 条全部收口，没有新的 uncertain）；
- 回归用例：收口后的消息仍不可重新识别（决定行没动）；
- 观察窗：7 天内若出现新的 uncertain，检查它是否只被报一次而不是每两分钟一次。
