# 备份止损这条线：只读查证（2026-09-23）

**问题**：用户指出「先附带止损 → 成交后撤销重设两重止损」这套流程的动机
（归属查不清）已随 REST+WS 消失，问能不能简化。动手前要先查清：
成交后那「两重」里的备份止损，有没有独立于归属的风控理由。

**纯只读**，SQLite 以 `mode=ro` + `PRAGMA query_only` 打开，按索引列取数。

---

## 1. 先纠正一个前提：当前并没有「撤销重设」

对全部 `purpose='stop_loss'` 的账本行按 posId 排时间线：

```
入场自带止损之后又出现止损的 posId：75 个
  其中后一张与前一张价格相同（即「重挂同一张」）： 0 个
  价格不同                                      ：75 个
入场自带止损之后没有任何止损的 posId：19 个
```

**同价重挂一个都没有。** 再看两个此刻仍活着的仓位——两张止损**同时** `verified`：

```
posId …386078842   87200.0 (entry_protection_response)  verified
                   87374.4 (position_mutation_intent)   verified   delta = 20.0 bps
posId …386087627   88300.0 (entry_protection_response)  verified
                   88476.6 (position_mutation_intent)   verified   delta = 20.0 bps
```

**入场自带的那张没有被撤，它就是主止损**；成交后写的是**另加**一张更宽的备份止损。
差值恰好 20 bps，来自 `trading_settings.trigger_backup_stop_buffer_bps = 20.0`，
由 `trigger_backup_stop.calculate_backup_stop_price()` 按
`primary × (1 ∓ bps/10000)` 向**亏损方向**取整算出。

所以**没有一个「撤销重设」的流程在等着被简化**——要谈的只是「要不要那张备份止损」。

## 2. 备份止损有两条来源，只有一条与归属有关

### 2.1 与归属有关的那条，已经实际停用

`trigger_backup_stop_executor`，即 2026-08-06 设计里的**通道 B**。设计原文：

> 优先完成通道 A（原生主止损归属）。**若在有界可见性窗口内仍无法归属原生止损**，
> 通道 B 使用入场请求中持久化的止损价，创建一张直接绑定精确 `posId` 的备份止损。
> 它不取消原生止损……**这使已验证备份止损成为原生止损归属暂时不完整时的活性证据。**

触发条件就是「归属不完整」，所以**用户的判断对这条完全成立**：阶段 6e 用
`TU == posId` 把归属补上之后，它的理由就没有了。而且它本来就已经近乎停用：

- 它是 opt-in，靠一份**代码常量**里的 posId 名单放行（刻意不做成配置，
  改它要走代码+全套+部署）；
- 名单现在是空的——两个 BTC id 2026-09-11 退役，两个 ETH id 2026-09-12（6k）退役；
- 全库经它写下的账本行共 **2 条**（`trigger_backup_stop_pending_readback`）。

### 2.2 活跃的那条，与归属无关

`strategy_management_market_policy.decide_composite_stop_replacement()` 在**每次**
写主止损时顺带算出一张 20 bps 的备份止损（`CompositeStopReplacementDecision`），
由复合止损替换路径写入。它回答的是另一个问题：**主止损没打到怎么办**——
交易所侧故障、撤挂之间的空窗、或我们自己替换操作的竞态。
这与「这张单是谁的」无关，**归属修好了它也照样需要**。

09-23 还在产生（上面两个仓位就是）。

## 3. 结论

| 问 | 答 |
|---|---|
| 有「成交后撤销重设」要简化吗 | **没有**。自带止损被保留为主止损，成交后只是**另加**一张备份 |
| 备份止损的理由是否独立于归属 | **一半独立**。通道 B 不独立（已停用）；20 bps 复合备份独立（活跃） |
| 能不能因为归属修好了就去掉备份止损 | **不能**，去掉的是冗余，不是归属的副产品 |
| 可以清理什么 | 通道 B 那条线：`trigger_backup_stop_executor` + 其释放常量 + 对应影子。**它仍是一张空着的安全网，删之前要单独确认** |

## 4. 顺带发现的一处记账口径不一致

那些 20 bps 的备份止损，账本 `purpose` 记的是 **`stop_loss`** 而不是 `backup_stop`
（`evidence_source = position_mutation_intent_readback`）。全库 `purpose='backup_stop'`
只有 **7 条**，而实际上几乎每个仓位都有一张备份。

后果：直接按 `purpose` 数「这个仓位有几张主止损」会得到 2 而不是 1。
目前页面不受影响（`_summarize_verified_exchange_protection_rows` 是按价格排序
区分主/备，不看 `purpose`），但任何将来按 `purpose` 判断的读者都会读错。
**记录在此，未修**——改它要动账本写入口径，值得单独评估。
