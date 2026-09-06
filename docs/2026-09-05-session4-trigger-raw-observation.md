# 会话 4：触发单原始字段观测（本次采集已结束）

## 已证明的结果

T0 窗口：2026-09-05 07:06:30.466803–07:06:49.314882 UTC（北京时间 15:06:30–15:06:49）。
生产身份回读 release：`9501a5f39f0c5f196cc29f24f3e3b8786267126b`，`loaded_artifact_verified=true`。

BTC 3 行、ETH 2 行、SOL 2 行未触发条件单，每行字段集合完全相同，均为 23 个字段。**这 7 行没有 posId、positionId、parentOrdId、triggerOrdId、algoId，也没有其他明确指向持仓或父单的字段。** BTC 类型为 Conditional 2 行、TPSL 1 行；ETH、SOL 各 Conditional 2 行。不能仅凭类型断言那一行 TPSL 来自哪张父单。

T0 的 1 行仓位包含 posId、不包含订单 ID 字段；所采 fills 行有 ordId、没有 posId。该仓位 posId 的字符串值与所采 fills 的一行 ordId、普通历史的一行 ordId 相同，这是本次样本的直接等值事实，不能扩展成所有订单/仓位通用契约，更不能据此关联附带止损。

所有者先提供 ETH/USDT 空单 2455.99，后更正为 2454.11、0.01 ETH、止损 2458.99，并告知已入场。新窗口已捕获 07:15:50 UTC 的真实成交、订单历史及 merge 仓位。**尚未捕获具有父触发单身份的 Conditional 行，也没有父→子引用，因此不能声称完整证明一次父触发单生命周期。** T3 稳定态已于成交 5 分钟后成功采集。

## 原始返回值与字段全集

字段名来自本次保存的数据，不以文档示例替代。字典返回值包含完整 `code,msg,data`；list 方法保存完整原始行列表，外层 HTTP 响应不是这些方法的返回值，未声称保存其外壳或线上的原始字节。

### trigger-orders-pending：23 字段

```text
cTime closeSLPrice closeSLTriggerPrice closeTPPrice closeTPTriggerPrice
instId instType lever ordId ordPx ordType posSide side slPrice
slTriggerPrice sz tdMode tpPrice tpTriggerPrice triggerOrderType
triggerPx triggerPxType uTime
```

### fills：15 字段

```text
billId clOrdId execType fee feeCcy fillPx fillSz instId instType
ordId posSide side tag tradeId ts
```

BTC、ETH 各返回 100 行，SOL 0 行。完整保存了方法本次返回的所有行；**历史覆盖不完整/未知**，没有将首页 100 行声称为全部历史。没有 posId 或父触发单引用字段。

### account/positions：20 字段

```text
avgPx cTime ccy instId instType isFollow isLeading lastPx lever
liqPx mgnMode mrgPosition pos posId posSide slTriggerPx tpTriggerPx
uTime unrealizedProfit useMargin
```

T0 全账户 1 行；T3 全账户 2 行（原有 BTC 与新增 ETH）。没有 ordId、parentOrdId 等产生该仓位的订单 ID 字段。

### orders-pending

T0 全账户 0 行，空列表已保存。**无法从空样本列出行对象字段**，待实验新增行。

### orders-history：37 字段

```text
accFillSz avgPx cTime category ccy clOrdId fee feeCcy fillPx fillSz
fillTime instId instType lever ordId ordType pnl posSide px rebate
rebateCcy reduceOnly side slOrdPx slTriggerPx slTriggerPxType source
state sz tag tdMode tgtCcy tpOrdPx tpTriggerPx tpTriggerPxType tradeId uTime
```

BTC 100 行、ETH 87 行、SOL 0 行。BTC 达到页大小，全部历史覆盖未知。没有 posId 或明确父触发单引用；source 是字段值，不能把来源类别当作父单外键。已捕获本次普通成交订单 1001125138097724；其 ordType=limit、category=normal、source 为空，不能据此将其断言为某张父条件单的子单。

### trigger-orders-history：25 字段

```text
cTime closeSLPrice closeSLTriggerPrice closeTPPrice closeTPTriggerPrice
errorCode errorMsg instId instType lever ordId ordType posSide px side
slPrice slTriggerPrice sz tdMode tpPrice tpTriggerPrice triggerPx
triggerPxType triggerTime uTime
```

BTC、ETH 各 100 行，SOL 0 行，完整历史覆盖未知。没有 posId、父子订单引用字段。

## 当前代码消费对照（限定到已核对函数）

源码依据是服务器已安装 release 的文件，不用共享工作树替代生产实现。此处“未消费”仅指所列函数；不是全仓库无人读取。

| 端点/路径 | 已消费的本次字段 | 未进入该投影的本次字段 | 确定性身份能力 |
| --- | --- | --- | --- |
| pending → native_tpsl.py:60 normalize_native_tpsl | triggerOrderType、ordId、instId、posSide、sz、cTime/uTime、slTriggerPrice/closeSLTriggerPrice、tpTriggerPrice/closeTPTriggerPrice | closeSLPrice、closeTPPrice、instType、lever、ordPx、ordType、side、slPrice、tdMode、tpPrice、triggerPx、triggerPxType；原行同时保存在 raw | ordId 标识本行；样本没有仓位/父单引用。另一个市场价校验函数 :107 消费 tpPrice |
| fills → execution_bindings.py:3245 附近 _snapshot_fill_evidence | ordId、clOrdId、instId、posSide/side、fillSz、fillPx、ts | billId、execType、fee、feeCcy、instType、tag、tradeId | ordId 可连接同 ID 普通订单；无显式 posId |
| positions → position_attribution.py:130 附近仓位经济快照 | posId、instId、posSide、pos、avgPx、mgnMode、mrgPosition | cTime、ccy、instType、isFollow、isLeading、lastPx、lever、liqPx、slTriggerPx、tpTriggerPx、uTime、unrealizedProfit、useMargin | posId 标识仓位；无父单或保护单 ID |
| orders-history → 同一 FillEvidence 构造 | ordId、clOrdId、instId、posSide/side、fillSz/accFillSz/sz、fillPx/avgPx/px、fillTime/cTime/uTime | category、ccy、fee、feeCcy、instType、lever、ordType、pnl、rebate、rebateCcy、reduceOnly、slOrdPx、slTriggerPx、slTriggerPxType、source、state、tag、tdMode、tgtCcy、tpOrdPx、tpTriggerPx、tpTriggerPxType、tradeId 不进入该构造；state 在前置判断另有消费 | 未发现显式父单引用；实验子单待捕获 |
| trigger-history → 同一 FillEvidence 构造 | ordId、instId、posSide/side、sz、px、triggerTime/cTime/uTime | closeSLPrice、closeSLTriggerPrice、closeTPPrice、closeTPTriggerPrice、errorCode、errorMsg、instType、lever、ordType、slPrice、slTriggerPrice、tdMode、tpPrice、tpTriggerPrice、triggerPx、triggerPxType 不进入该构造；errorCode 在前置判断另有消费 | 未发现子单/仓位引用 |
| orders-pending | 方法原样返回行，本次无行 | 无法据空样本做差集 | 未知 |

需要更正背景中的一处判断：**当前生产并非从未尝试 posId**。`native_tpsl.py:78` 已读取 `PositionID/posId/pos_id/positionId`；`execution_bindings.py` 的 FillEvidence 构造也读取 `posId/pos_id/positionId`。`protection_snapshot.py:533` 获取原始读取方法，`:560` 返回完整行；`:480` 的观察摘要才只归集订单 ID 等元数据。因此，单纯增加 posId 读取不能补出本次交易所样本不存在的字段。

## 五个问题的当前状态

1. 未触发条件单字段及链接字段：T0 已回答，7 行均无显式持仓/父单引用。
2. 新附带止损首次出现的端点、帧及父关联：已捕获新增 TPSL 首帧及真实成交，详见后文；没有确定性父引用。
3. fills 字段及 posId：T0 已回答，无 posId；有 ordId。单一样本 ID 等值不能证明一般仓位关联契约。
4. positions 字段和订单 ID：T0 已回答，有 posId，无订单 ID 字段。
5. 本次普通成交单形态及父引用：已捕获 filled/limit，未见父引用；是否为条件单子单未证明，pending 持续未捕获该订单。

**阶段结论：所采 T0 数据不足以建立父触发单 → 附带止损 → 仓位的确定性链。** 缺少保护行到仓位/父单的明确引用，且缺少可确认的父条件单提交、触发与子单血缘证据；成交事件本身已经捕获。不能据此断言交易所在所有情形都不提供关联字段。后续若出现新字段，再定位归属解析与证据持久化需要的最小变更；本任务不实施代码变更。

## 安全、证据及完整性

- 现成 Deepcoin 客户端从 immutable release/src 导入，`python -B`；所有调用 uid=989（telegram-kol-worker）。root 子进程读取 worker 环境后立即清空原始环境缓冲并降权；root 父进程不读取凭据，仅接收已过滤的业务结果。
- 静态列出读取方法并用直接 lambda 调用，未动态派发，不调用任何交易写入方法。
- T0 顺序调用，前一次结束后至少间隔 1.25 秒；保留客户端本身行为。代码中未发现跨进程通用 GET 限速器，不把现成客户端说成与生产共享全局 GET 配额；额外采集主动串行降速。
- 原返回值递归过滤鉴权字段后保存，T0 的 auth_redacted 全部为 false；未删除业务字段。没有打印/落盘凭据，没有 DB、配置或服务变更。
- 服务端证据目录：`/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-20260905T0705Z`，root-owned 0700；目录内文件 root-owned 0600，权限已核对。
- 14 个原始 JSON 文件及各自 metadata；`manifest.json` 含每个原始文件 SHA-256、调用起止时间、行数、逐行字段集合。
- T0 manifest SHA-256：`8a7489a233bf2ac73340c6db27ed4815691e236d06702a1767e539c808ac74a2`。
- T0 采集子进程已 waitpid 回收、/proc PID 不存在；release 的 .pyc 路径/大小/mtime 清单前后相同。采集脚本通过 stdin 执行，服务器未落盘临时脚本。
- ETH 第一段窗口目录：`/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-ETH-window-20260905T0710Z`。四个端点串行错峰、约 5 秒一轮；第一段已主动停止并回收子进程；四端点分别采得 66/66/66/65 次，原始文件 SHA 校验通过。实际相邻请求起始间隔 4.887–5.142 秒（目标 5 秒，包含调用耗时抖动）。末轮在 positions 前停止，保留为不完整末轮。
- T0 的 6 张 Conditional 与 execution_order_legs 的精确 order_id 命中，1 张 BTC TPSL 与 position_protection_ledger 命中，均可标记为系统已记录订单。见 source-origin-readonly.json；数据库以 mode=ro 且 query_only=1 访问。初始 2455.99 普通单和新增 ETH TPSL 未命中该次两表精确 order_id 查询，单凭未命中不证明手动来源；与所有者描述一致时标记为“所有者测试候选”，不伪造 binding。


## 已捕获的实验时间线（UTC）

| 交易所时间/采集时间 | 原始证据 |
| --- | --- |
| 07:07:05（cTime） | 普通订单 1001125138063057，px=2455.99，sz=0.1，side=sell，posSide=short，ordType=limit |
| 07:08:34.349 起 | ETH 第一段连续采集开始；在已保存的普通 pending 响应中没有捕获上述订单，不能倒推它从未存在 |
| 07:09:40（uTime） | 该普通订单历史状态 canceled，accFillSz=0、fillSz=0；不推断取消者或取消原因 |
| 07:10:25（cTime） | 后续历史返回的新普通订单 1001125138097724 和 TPSL 1001125138097723 的创建时间都为此秒；相同时间不是父子证明 |
| 07:10:24.349 / 07:10:29.349 | 第一段 frame 22 没有新增 TPSL，frame 23 首次出现；文件 window-0092-read_trigger_orders_pending-ETH-USDT-SWAP.raw.json |
| 07:14:09（TPSL uTime） | 新窗口看见同一 TPSL 的 uTime 改变，其他关联字段仍不存在；不能只凭这个值确认哪一项操作 |
| 07:15:50（fills ts、仓位 cTime/uTime、订单 fillTime） | ordId=1001125138097724，卖出 short，fillPx=2454.11，fillSz=0.1；普通订单 state=filled |
| 07:15:51.567（第二段 frame 7） | 首次捕获该 fills 行：window-0037-list_trade_fills-ALL.raw.json |
| 07:15:52.584（第二段 frame 7） | 首次捕获 posId=1001125138097724，pos=0.1、mrgPosition=merge、slTriggerPx=2458.99：window-0038-list_positions-ALL.raw.json |
| 07:15:53.496（第二段 frame 7） | 首次捕获 filled 普通历史：window-0039-read_order_history-ALL.raw.json |

所有者口述 0.01 ETH，交易所本次 sz/fillSz/pos 原文为 0.1，原样保存，不在证据层改写单位。

### 新 TPSL 首帧的完整业务行

```json
{"instType":"SWAP","instId":"ETH-USDT-SWAP","ordId":"1001125138097723","triggerPx":"0","ordPx":"0","sz":"0.1","ordType":"","side":"buy","posSide":"short","tdMode":"cross","triggerOrderType":"TPSL","triggerPxType":"last","lever":"125","slPrice":"0","slTriggerPrice":"2458.99","tpPrice":"0","tpTriggerPrice":"0","closeSLPrice":"","closeSLTriggerPrice":"","closeTPPrice":"","closeTPTriggerPrice":"","cTime":"1788592225000","uTime":"1788592225000"}
```

**该行在入场成交前就可见**，之后可见 uTime 更新。没有 posId、parentOrdId 或其他明确血缘字段。不得用相邻的订单号码（…723 与 …724）、相同数量、价格或秒级时间替代确定性引用。

### 归属结论的边界

- 本样本订单→成交可以通过相同 ordId 直接连接；成交行自身没有 posId。
- 本样本成交 ordId 与 positions.posId 的字符串完全相同，可准确展示本次等值连接。但是当前是 merge 仓位，不能从单次样本推导后续合并加仓、拆仓或 split 模式的普遍 ID 契约。
- 新 TPSL 行与仓位没有显式 ID 连接。父 Conditional 行没有被捕获，历史普通单的 source 也为空。因此 **完整的父条件单→普通成交单→仓位→附带止损确定性归属仍未证明；仅凭本次可见字段无法构造这条完整链。**
- 当前 native_tpsl 已尝试 posId 别名；本次不是补读一个遗漏的现成 posId 就能解决。缺少交易所返回的保护单→仓位/父单引用，或能够确定绑定两端的原始创建回执契约。
- 如果后续取得这种明确引用，相关入口是 native_tpsl.py（字段归一化）、protection_attribution.py（归属校验）、execution_bindings.py（父子/成交证据）、protection_snapshot.py / pending_tpsl_snapshot_observations（原始观测留存）。这是待证实后的改动定位，不是本次修改方案或已证明可行性。

第二段目录：`/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-ETH-revised-20260905T0715Z`。沿用 5 秒周期，4 个核心端点外增加 read_order_history，5 个调用顺序执行、调用间隔至少 0.75 秒，不突发并发。第一段与第二段之间约有一分钟采集间隙，不能声称自最初提交起无间断捕获；实际成交前后位于第二段连续窗口。


## 第二段采集异常与 T3 安排

第二段 frame 0–42 完成全部 5 个读取方法；frame 43 在 07:18:49.768864 UTC 调用 read_trigger_orders_pending 时返回 DeepcoinClientError，未返回原始字典，错误原因未知（不能宣称一定是限频、网络或交易所业务错误）。采集器随即停止，没有继续调用余下端点；失败帧保留 error_type、起止时间，不能当空数组。

第二段已连续覆盖 07:15:50 成交前后及成交后超过 170 秒，满足成交后至少 60 秒窗口。T3 于 07:20:50.223106–07:21:01.424613 UTC 成功完成全部 6 个读取方法，也是失败后的唯一重试；没有再次失败，没有进一步轮询。

第二段 manifest SHA-256：`d6a7eca00fa0f7465bae0b74d34dd8d66899ba0dd7f0876c59c1b510fc9e6172`。采集子进程已回收、release .pyc 清单未变化。
第一段 manifest SHA-256：`9eb0b28810652795c3a2a0bd1d99795e5219a7ecfbc3de3a9ba3887f0ba56f69`。


## 最终交付与清理

**本次已完成 T0、提交后/成交前的观测、成交前后及成交后超过 60 秒的连续帧、成交后 5 分钟的 T3。未获得父 Conditional 行或明确父子引用，故“从父触发单提交开始的完整血缘链”未被证明。** 不将快照阶段完成冒充该链路已经被证实。

- 成交证据：1001125138097724，2454.11，short，交易所 fillSz=0.1；用户报告为 0.01 ETH。T3 ETH 仓位仍为 merge，posId=1001125138097724；TPSL 1001125138097723 仍可见，无新增关联字段。
- T0 的 7 行系统已记录订单与测试样本分开。测试成交订单及 TPSL 在结束时的 execution_order_legs / position_protection_ledger 精确查询仍无记录。成交参数与所有者报告一致，报告将其标记为“所有者报告对应的成交样本”；TPSL 是同价格测试候选，**不把这一实验标注当成可用于自动交易的确定性 binding**。
- 所采 T3 最近 fills 在本次窗口内仅出现上述一条 ETH 成交，没有捕获其他新增系统入场成交；此结论限定到返回页，不扩展为全账户历史完备声明。
- 共保存 **509 份成功客户端原始返回值 JSON**，另有逐次 metadata、失败记录、各段 manifest、来源核对和总索引；全部原始文件的 SHA-256 已重新校验。
- 六个服务端证据目录均已确认 root-owned 0700，文件 root-owned 0600。所有采集子进程已退出并回收，无残留 worker 用户临时采集进程。服务器从未落盘采集脚本；本机本任务创建的 7 份临时脚本（包括上一轮未执行的手写 HTTP 草稿）均已删除。
- 所有段的 immutable release .pyc 清单前后相同；没有生产代码、数据库、配置、服务状态或交易写入变更。文档是本地唯一仓库新增产物，不提交、不部署。

总证据索引：

```text
/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-20260905T0705Z/final-evidence-index.json
SHA-256: a1965f750ab3d7fc8f7a7306a6ba7bab1977fa3e3a547dd677e606c805d2e6a2
```

总索引给出全部六个目录、各段 manifest SHA-256、原始文件数量、哈希/权限/进程核验结果、末次来源查询和 T3 仓位证据。
T3 目录：`/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-T3-20260905T072050Z`，manifest SHA-256：`f6e4f0046aee4efbeada3b1ac22d1b8110019ca6ac927962645f6c62e62ce00d`。

**最终结论：本样本的普通订单、fills 与 posId 可作直接等值对照；父条件单→子单及 TPSL→仓位的确定性归属不可由本次返回字段建立。** 缺的是明确血缘引用，不是当前解析器漏读了本次样本里已有的 posId。merge 样本也不能替代生产 split 模式的完整验证。
