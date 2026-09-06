# 普通 order 与止盈止损字段研究及首轮实验

核对日期2026-09-05。用户确认首轮ETH最小量、入场P±1、止盈10 USDT、止损10 USDT。本轮准备独立实验，不修改项目生产路由，不替用户执行真实下单。

## 已确认的三类接口

| 阶段 | 接口 | 目标标识 | TP/SL字段与关键语义 |
| --- | --- | --- | --- |
| 普通限价入场时附带 | POST /deepcoin/trade/order | 请求自定义clOrdId；响应普通ordId | tpTriggerPx、slTriggerPx；创建表没有明确列出tpOrdPx/slOrdPx或触发来源选择，不能未经实测宣称这些额外参数受支持 |
| 未成交普通限价委托修改保护 | POST /deepcoin/trade/replace-order-sltp | orderSysID（该普通委托ID，大小写如此） | tpTriggerPx、slTriggerPx为数值；遗漏或0表示取消对应设置；响应data={}，没有新保护ID |
| 已有仓位设置保护 | POST /deepcoin/trade/set-position-sltp | split时必须有明确posId | tpTriggerPx/slTriggerPx、tpTriggerPxType/slTriggerPxType、tpOrdPx/slOrdPx、sz；响应保护ordId |

普通order的ordType=limit、价格字段px，必须tdMode；不使用triggerPrice、orderType、price、isCrossMargin。clOrdId为1–20位大小写字母数字，自定义且每次唯一；tag暂不支持，ordId由交易所生成。

来源：[普通下单](https://www.deepcoin.com/docs/DeepCoinTrade/order)、[未成交限价保护修改](https://www.deepcoin.com/docs/DeepCoinTrade/replaceTPSL)、[持仓保护设置](https://www.deepcoin.com/docs/DeepCoinTrade/setPositionSlTp)。

## A：普通限价入场直接附带 TP/SL

以下价格为字段示例，并非当前报价；真正实验会在运行时刷新市场价、合约最小量和步长。历史合约规格sz=0.1张、ctVal=0.1 ETH/张，对应0.01 ETH。

```json
{
  "instId": "ETH-USDT-SWAP",
  "tdMode": "cross",
  "mrgPosition": "split",
  "side": "buy",
  "posSide": "long",
  "ordType": "limit",
  "px": "2456.52",
  "sz": "0.1",
  "clOrdId": "EOexampleL",
  "tpTriggerPx": "2466.52",
  "slTriggerPx": "2446.52"
}
```

空单side=sell、posSide=short，TP=入场-10，SL=入场+10。入场回执应记录clOrdId与普通ordId；随后分别按两个ID查询GET /trade/order，并对比orders-pending、orders-history、fills、positions及trigger-orders-pending/history。

本次只发送普通创建表明确列出的TP/SL价格字段。不能把trigger-order的slOrdPx=-1原样塞入后就视为市价保护已证实。需检查新TPSL的slPrice/tpPrice、slTriggerPrice/tpTriggerPrice及完整原始字段；该行是否存在也与它是否明确关联目标仓位分开判断。API成功回执不等于已生成可用止损。

## B：挂单尚未成交时修改附带保护

字段示例，不能用占位ID提交：

```json
{
  "orderSysID": "<本次已确认的普通ordId>",
  "tpTriggerPx": 2466.52,
  "slTriggerPx": 2446.52
}
```

最容易误用的是只提交TP：按文档，遗漏SL字段可能取消SL设置。必须保留想要继续生效的另一侧已知值，并分别核对请求前后挂单与TPSL。该接口只适用于尚未成交限价单；查询后提交前可能成交，不应失败后盲目改调其他写接口。本轮首轮命令不会自动调用B。

## C：成交后按已确认仓位设置保护

字段示例，先取得并核验真实split posId：

```json
{
  "instType": "SWAP",
  "instId": "ETH-USDT-SWAP",
  "posSide": "long",
  "mrgPosition": "split",
  "tdMode": "cross",
  "posId": "<已确认的真实posId>",
  "tpTriggerPx": "2466.52",
  "tpTriggerPxType": "last",
  "tpOrdPx": "-1",
  "slTriggerPx": "2446.52",
  "slTriggerPxType": "last",
  "slOrdPx": "-1",
  "sz": "0.1"
}
```

该接口明确支持last/index/mark触发，tpOrdPx/slOrdPx=-1为市价，具体价格为限价；sz控制部分仓位，空值表示全仓位。响应返回SLTP ordId，应与**原请求posId**持久化形成创建账本。这是“指定哪个仓位创建哪张保护”的确定性本地证据，比事后用时间和价格反推更明确。

但普通入场ordId与实际split posId的关系仍应查回核验，不能仅凭历史单次等值就作为所有模式的保证。文档提示设置新保护可能覆盖已有保护，不能在已有附带SL的仓位上重复set来假设“新增而不覆盖”。最小量不适合拆成多个低于最小步长的分批止盈腿；多TP与全量SL的数量关系、覆盖/OCO/部分成交行为要另测。本轮命令不自动调用C，避免把尚未确认的保护替换掉。

## 修改、撤销已存在保护

- POST /deepcoin/trade/modify-position-sltp：使用保护ordId；split还需posId，以及对应合约/方向/保证金模式和TP/SL字段。
- POST /deepcoin/trade/cancel-position-sltp：使用instType、instId、保护ordId。
- 没有已确认的保护ordId↔posId关系，不以方向、价格、数量接近执行修改或撤销。

来源：[持仓保护修改](https://www.deepcoin.com/docs/DeepCoinTrade/modifyPositionSlTp)、[持仓保护撤销](https://www.deepcoin.com/docs/DeepCoinTrade/cancelPositionSlTp)。

## 首轮用户操作命令

```bash
ssh -t tecent 'python3 -B /var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-test-20260905/deepcoin_order_tpsl_experiment.py --execute-order-tpsl-pair'
```

这个入口会真实提交一多一空两笔普通限价单，附带TP10/SL10；每侧动态最小量，最多0.01 ETH且名义额不超过50 USDT。使用服务器worker现有凭据，不复制凭据到本机或文件。已有ETH仓位会阻断，旧挂单只记录不修改。当前只读预检positions返回0行，未来启动时会重新检查。

两侧接受后观察300秒；部分/未知结果留证30秒，无自动补单或重试。只撤销本次已接受普通订单的未成交余量；不会自动平掉已成交仓位、撤销或覆盖TP/SL。用户需在Deepcoin处理剩余仓位，留存后续关闭信息用于完成成本和保护生命周期分析。

独占LIVE-ATTEMPT.json阻止重复粘贴再次下单。结果目录为该服务器目录下live-<run_id>，其中manifest、每侧request/response、raw.jsonl、frames.jsonl、order-tpsl-frames.jsonl、live-summary.json共同构成证据。新TPSL只标为候选，不自动宣布保护归属成功。

首轮应回答：clOrdId能否回显和查询；普通ordId在挂单到成交是否保持；posId如何对应；附带TP/SL何时创建、各用哪些ID和执行价字段；撤未成交/部分成交普通单后保护如何处理。只有真实触发/成交发生的部分才能下结论。

## 本轮验证与保留事项

57项focused离线测试通过；独立审查提出的撤单回读异常被当空、最终保护快照截断问题已修复。测试覆盖普通字段白名单、TP/SL方向、qty/price上限、默认不执行、未知提交不重试、精确撤普通单、部分成功仍采集、收尾失败非零状态以及原trigger默认白名单不变。

代码仅新增独立实验并对通用I/O添加显式白名单参数；生产src、数据库、配置、服务未修改。实际普通订单提交、附带保护行为以及更换生产路由的接受标准尚未证明。助手未执行任何本轮真实交易。

官方HTML/提取文本保留于`data/deepcoin_order_tpsl_research_20260905/`。市场示例不是实时交易建议，最小量限制也不保证损失上限；成本最终以实际成交、手续费、退出价和资金费记录为准。

服务器独立实验目录已安装，三份脚本SHA256与本地一致，远端仅执行AST解析及文件校验，没有运行交易入口。安装清单：`/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-test-20260905/installation.json`；主脚本SHA256：`513e50138410776805134f63e52362cd4745644bb35a685079e802c3e883f00a`。

## 首轮真实执行结果：未取得成功下单样本

运行 `a401cbabd233`，2026-09-05 16:23:57.875 UTC 同时提交两笔，每侧0.01 ETH。多单限价2459.35、TP2469.35、SL2449.35；空单限价2461.35、TP2451.35、SL2471.35。

两笔均HTTP200、外层code=0，但内层sCode=14、sMsg=DuplicateAction、ordId为空。因此外层成功不能解释为挂单成功。回执分别回显EOa401cbabd233L和EOa401cbabd233S，这只证明错误回执回显请求标识，不证明已落订单或保护继承。

执行器未重试，未发撤单请求。运行结束与16:26:51 UTC起的补充只读核查：两个clOrdId查询均code=0/data=[]，ETH仓位和普通挂单查询均为空。原观察帧没有新TPSL候选。历史trigger接口最新100条覆盖限制仍保留，不宣称全历史完整。

证据在服务器 `/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-test-20260905/live-a401cbabd233/`；补充GET证据在其 `readonly-review-2026-09-05T162651.825Z/`。未执行新的交易请求，未移除一次性锁。

根因尚未确认。两个clOrdId不同且符合官方1-20位字母数字格式；不能仅据DuplicateAction认定用户重复执行、字母不支持或并发必然出错。后续应单变量分阶段验证：先单笔普通限价+clOrdId+相同TP/SL，区分并发因素；仍拒绝时再设计字段对照，不能在同一轮自动切换参数重试。当前没有证据支持替换生产trigger-order路由。

## DuplicateAction专项只读排查

重新读取服务器两份持久化请求/回执及一次性标记；脚本SHA256与已审查本地版本一致。两笔提交开始时间同为2026-09-05T16:23:57.875Z，分别于16:23:58.068Z、16:23:58.070Z收到拒绝。提交函数每侧只有一次网络调用，禁重定向，没有重试循环。现有日志没有独立记录实际签名头的时间戳，因此不能将“提交开始时间相同”误写为“已证明DC-ACCESS-TIMESTAMP完全相同”。

字段校验：两个clOrdId长14位且不同，符合官方字母数字1-20位约束；sz=0.1张=0.01ETH符合当时minSz/lotSz；价格均满足0.01 tick；多SL<入场<TP、空TP<入场<SL；cross/split、buy/long与sell/short一致。官方agent-cli构建相同的clOrdId、tpTriggerPx、slTriggerPx字符串字段，未见必须额外传入的订单请求去重字段。官方Python示例也使用同一普通order入口和TP/SL字段。

官方errorCode页面仅列出501xx等接入错误，没有业务sCode=14的判重键、作用域、有效期或触发条件。官方CLI、Python例子、agent-skills及CCXT实现未提供DuplicateAction的可用细化解释。这里不能套用其他交易所的“重复client ID”解释。

结论：明确的是交易所返回业务拒绝DuplicateAction；客户端可核对字段未发现文档级违规，但具体服务端判重原因尚未得到证实。没有证据归咎用户重复执行、余额不足或IP白名单。原先提出单笔对照只是诊断下一步，不是已验证修复。尤其两笔均失败，不能把单纯双请求互相撞车当成定论。客户端总状态partial_or_unknown_submission是脚本将非两笔成功统一归类，原始两笔均明确rejected，不能误报一笔成功。

可提交给Deepcoin技术支持的最小问题：请查2026-09-05 16:23:57.875 UTC的POST /deepcoin/trade/order，两笔唯一clOrdId EOa401cbabd233L / EOa401cbabd233S，均返回HTTP200/code0/sCode14 DuplicateAction且ordId空；GET对应clOrdId为空。请说明实际冲突的内部键、被判重复的原始请求、该键作用域和有效期，以及该账户普通order在clOrdId+split+TP/SL组合下是否有已知问题。不要发送API密钥、签名或Passphrase。此问题稿没有发送给任何外部人员。

来源：[官方错误码](https://www.deepcoin.com/docs/errorCode)、[官方CLI下单实现](https://github.com/deepcoinapi/agent-cli/blob/main/cmd/trade/trade.go)、[官方Python下单示例](https://github.com/deepcoinapi/openapi_python_example/blob/main/rest/trade/post_order.py)。抓取资料保留于data/deepcoin_order_failure_20260905。此次只读排查没有提交新交易。

## 第二轮：仅空单单变量实验

新增 `--execute-order-tpsl-short` 模式，只生成并提交一笔ETH普通空单：动态最小数量，限价为即时市价加1，TP为入场限价减10，SL为入场限价加10。该模式保留独立clOrdId、一次性锁、无重试、原始请求/回执和五分钟观察；只撤本次未成交余量，不自动平已成交仓位。

服务器独立目录为 `/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-test-20260905/`，主脚本SHA256为 `bd43601e9639c43bb194f62b6c96d3b3396eba7c8261c64ee2c40e28f1e3abb7`。远端只完成哈希和AST解析，没有运行真实交易。61项相关离线测试通过。

用户执行命令：

```bash
ssh -t tecent 'python3 -B /var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-test-20260905/deepcoin_order_tpsl_experiment.py --execute-order-tpsl-short'
```

### 单空真实执行结果

第一次执行已于2026-09-05 16:39:18.924 UTC提交一笔空单请求：参考价2465.28，限价2466.28，0.1张即0.01 ETH，clOrdId为EO4abb30c56e28S，TP2456.28，SL2476.28。交易所返回HTTP200/code0，但业务结果仍为sCode14/DuplicateAction，ordId为空，明确拒绝。没有重试或撤单写请求。

随后再次运行同一命令时，一次性锁按设计阻断，产生“already been attempted”异常；这是第二次运行的本地保护提示，不是交易所的新错误。2026-09-05 16:45:18 UTC补充只读回查：按该clOrdId查询0行、ETH仓位0行、普通挂单0行。

单笔请求仍复现DuplicateAction，因而可以排除“同一脚本内多空两笔并发互相判重”这一假设。服务端实际判重键仍未知，下一轮不应简单删除锁重发。

## 第三轮：单空去掉clOrdId

按单变量方法保留上轮的一笔空单、动态最小量、市价加1限价、TP减10、SL加10、cross/split和无重试，仅删除请求中的clOrdId。没有client ID时，如交易所接受请求，后续只使用回执ordId查询和精确撤销。

新目录为 `/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-no-clordid-test-20260905/`。主脚本SHA256为 `6daf38ea6056ffd3af2997de039246ee4b8f200440532eabc9ab41fad47487d2`；64项相关离线测试通过，远端仅完成哈希与AST解析，未执行交易。

```bash
ssh -t tecent 'python3 -B /var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-no-clordid-test-20260905/deepcoin_order_tpsl_experiment.py --execute-order-tpsl-short-no-clordid'
```

### 无clOrdId真实执行阶段结果

运行3a22b4272f01于2026-09-05 16:54:12.516 UTC提交单空请求，交易所接受并返回入场ordId `1001125143973255`。请求参考市价2471.62，限价2472.62，0.1张即0.01 ETH，TP2462.62，SL2482.62；16:54:25 UTC全部成交，成交均价2472.62，手续费0.00494524 USDT。

入场单GET显示state=filled、ordId=1001125143973255，并把未提交的clOrdId显示成同值1001125143973255；orders-history和fills中的clOrdId却为空。因此GET详情里的该字段是交易所回填表现，不能解释为客户端自定义ID，也不能跨接口当稳定关联键。

成交后split仓位posId为 `1001125143973255`，与本次入场ordId相等；仓位回显TP2462.62、SL2482.62。另有TPSL挂单ordId `1001125143973254`、side=buy、posSide=short、sz=0.1、同一TP/SL价格和时间。TPSL行没有返回父入场ordId或posId，故“它属于该仓位”可由本次干净基线、数量/方向/价格/时间及仓位共同强支持，但不是TPSL记录自身提供的显式ID外键。

16:57 UTC快照时仓位仍为0.1张，TPSL仍在pending；当时只是普通入场成交，止盈或止损尚未触发。由于此前带不同且合法clOrdId的单空请求被DuplicateAction拒绝，而本次只删除clOrdId即被接受，现有对照强烈指向Deepcoin此账户/当前REST普通order路径对clOrdId的实际处理与公开文档不一致。该结论仍应标注为实测行为，不扩大为所有账户的协议保证。

### 实验TPSL绑定字段审计

2026-09-05 17:07:11 UTC再次读取公开REST。仓位行包含 `posId=1001125143973255`、`posSide=short`、`pos=0.1`、`avgPx=2472.62`、`tpTriggerPx=2462.62`、`slTriggerPx=2482.62`。公开pending接口把止盈和止损合并为一条TPSL行：`ordId=1001125143973254`、`instId=ETH-USDT-SWAP`、`side=buy`、`posSide=short`、`sz=0.1`、`tpTriggerPrice=2462.62`、`slTriggerPrice=2482.62`、`triggerOrderType=TPSL`、`cTime=1788627252000`、`uTime=1788627265000`。

该TPSL行没有 `posId`、`PositionID`、`TradeUnitID`、`closePosId`、父入场ordId、clOrdId或tag。因此公开REST中没有单一字段可直接证明TPSL ordId与实验posId的捆绑。合约、方向、反向平仓side、数量、两侧价格和时间全部一致，结合本次干净基线可形成很强的实验归属证据，但在同一合约、同方向、同数量和同价格存在多个split仓时不具唯一性。

官网此前实证研究显示其网页内部 `trigger_order` 数据保留独立 `PositionID`，界面以 `order.positionId == position.positionId` 关联；公开REST省略该字段。私有WebSocket文档虽把TradeUnitID描述为Position ID，但其示例与官网独立PositionID模型不一致，不能未经实测直接替代。可审查证据见 `docs/deepcoin-tpsl-live-verification-2026-07-25.md`。

### 查漏复核：相邻ID、时间链与WebSocket缺口

2026-09-05对原始证据和已结束的交易所历史再做只读查漏。实验仓位已由止损触发平仓；没有下单、改单、撤单或配置修改。新证据保存在服务器：

```text
/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-no-clordid-test-20260905/live-3a22b4272f01/field-gap-audit/
```

本次受控普通order实验的完整时间链是：

- 入场限价单 `ordId=1001125143973255`，`cTime=1788627252000`；
- 附带TPSL `ordId=1001125143973254`，`cTime=1788627252000`；
- 成交后分仓 `posId=1001125143973255`，`cTime=1788627265000`；
- TPSL在 `triggerTime=1788629593` 触发，其 `uTime=1788629593000`；
- 同一仓位历史的平仓 `uTime=1788629593000`，开仓均价 `2472.62`、平仓均价 `2482.67`、本次SL触发价 `2482.62`。

第一帧成功联合回读发生在 `2026-09-05T16:54:20.083Z`：主单仍为 `state=live, accFillSz=0`，TPSL `1001125143973254` 已在 pending，同时仓位列表为空。下单回执只有主单 `ordId=1001125143973255`，没有返回TPSL ID。因此该附带TPSL实际上在限价单成交前已创建，而不是成交后才首次出现。`2026-09-05T16:54:33.401Z` 回读时主单已成交，仓位 `posId=1001125143973255` 才出现。

因此，本实验不仅有方向、数量和价格吻合，还有三条更强的结构证据：入场 `ordId` 与分仓 `posId` 相等；TPSL ID恰好为入场ID减1；入场单与TPSL创建毫秒相同，TPSL触发毫秒又与仓位平仓更新毫秒相同。另一次受控触发入场实验也出现了普通入场 `1001125142983995` / TPSL `1001125142983994` 的相邻ID和相同创建毫秒。这些证据强力证明交易所内部把附带TPSL与本次入场及其仓位一起创建和执行，但“ID减1”仍是样本中的分配模式，不是回包外键或已公布契约。

补查 `orderByID`、`finishOrderByID`、V2 `orders-detail` 和 V2 `orders-algo-pending` 未找到入场单与已触发TPSL之间的额外字段。TPSL在普通订单详情中也不是可查普通单；V2 pending不返回已结束TPSL。

真正未被本次实验采集的候选字段是私有WebSocket `TriggerOrder` 中的 `TU/TradeUnitID`。原实验没有在下单前建立WebSocket订阅，仓位和TPSL现已结束，历史REST不回传该字段，所以无法从这一笔已结束数据追溯验证。若要判定它是否等于真实split `posId`，下一次受控实验必须在提交普通order之前订阅 `Order,Position,Trade,TriggerOrder`，同时保存原始推送。

### 主ordId反查TPSL对照

2026-09-05又用本次实验的两个真实ID对同一历史接口做只读对照：

- `GET /deepcoin/trade/trigger-orders-history?...&ordId=1001125143973255`（主入场限价单ID）返回 `data=[]`；
- `GET /deepcoin/trade/trigger-orders-history?...&ordId=1001125143973254`（TPSL自身ID）准确返回本次TPSL历史。

这证明“查询已触发条件单”的 `ordId` 参数是条件单/TPSL自身ID过滤器，不是主普通订单ID的反向关联键。

“查询未触发条件单”的公开请求参数本身没有 `ordId`。本次额外传入主单ID时，服务端未报错，但仍返回账户当前两张无关条件单，即该多余参数未产生按主单筛选的效果。对照原始结果保存于上述 `field-gap-audit/main-vs-tpsl-ordid-query.json`。
