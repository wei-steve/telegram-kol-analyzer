# Deepcoin order 与 trigger-order 对照

核对日期：2026-09-05。范围：V1公开REST文档与本次ETH实验已保存证据。本轮无交易、生产代码或配置修改。

## 核心区别

`POST /deepcoin/trade/order` 创建普通交易委托，ordType可为market、limit、post_only、ioc；它不是只下市价单的接口。普通限价单提交后进入普通订单处理/撮合，是否立即成交取决于限价与盘口。

`POST /deepcoin/trade/trigger-order` 创建价格条件。triggerPrice条件满足后，系统再产生orderType指定的market或limit委托；limit时price是触发后的委托限价。triggerPxType可为last/index/mark。触发成功不保证限价单成交。

官方来源：[普通下单](https://www.deepcoin.com/docs/DeepCoinTrade/order)、[条件下单](https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrder)。

## 字段支持必须区分创建、响应、查询和管理

| 字段 | 普通order | trigger-order |
| --- | --- | --- |
| ordId | 创建时服务端生成，回执返回；查询/撤销/改单可引用 | 同样服务端生成父触发ID，可用于专用触发历史/撤销/改单；不是客户端自定义字段 |
| clOrdId | 创建请求明确支持1–20位大小写字母数字；GET详情支持查询 | 创建请求表未声明支持；响应表虽有列，但不构成支持输入/传播的承诺；修改触发单文档明确回显目前为空 |
| tag | 请求表列有，但明确暂不支持 | 创建请求表未声明支持，响应示例为空；无继承承诺 |
| 类型字段 | ordType | orderType |
| 限价字段 | px | price |
| 入场触发字段 | 无triggerPrice入口 | triggerPrice、triggerPxType |

普通GET /trade/order同时给ordId和clOrdId时ordId优先；复用clOrdId可能只返回最新匹配单，因此实验应每次唯一生成。不要拿普通GET详情作为通用触发单查询端点。

更新日志明确记录：2026-06-04普通POST /trade/order接受并返回clOrdId，GET支持clOrdId；2026-06-10普通撤单/改单增加clOrdId定位。这些记录没有将能力扩展为trigger-order输入或父子继承保证。[更新日志](https://www.deepcoin.com/docs/changelog)、[触发单修改](https://www.deepcoin.com/docs/DeepCoinTrade/amendTriggerOrder)、[触发单撤销](https://www.deepcoin.com/docs/DeepCoinTrade/cancelTriggerOrder)。

## 用本次实验理解差别

以下2457.52是已完成实验的历史基准，不是当前报价：

- 普通limit买2456.52、卖2458.52：提交这两张普通限价委托，等待对应盘口满足成交条件；不需要先创建父触发单。
- trigger限价买：先等待last达到2456.52，再提交2456.52限价买委托。空侧同理先等触发，再提交限价卖。
- 两者最终成交价格可能接近，但订单激活时刻、盘口排队、可见性和跳价后的行为不同，不能认为完全等价。
- 若目的是“上涨突破2460后再买”，直接提交普通2460限价买单，在卖价低于该限价时可能立即成交。它不会替你等待上涨突破；应保留触发语义。

以上为依据官方订单类型与触发机制作出的执行语义解释，不是收益或成交率结论。

## 对系统身份链的意义

对单纯低位买入/高位卖出的挂单意图，普通order limit是值得验证的直接实现：本地唯一clOrdId对应直接创建的普通ordId，可以省去“父trigger ID→新普通order ID”这一层身份发现。这不是建议把所有trigger订单批量改为普通限价；必须保留原策略的突破/回落触发条件、last/mark/index价格来源、止损和并发语义。

普通order支持clOrdId也不代表附带TPSL会继承该值。普通请求表列有tpTriggerPx/slTriggerPx，但其说明较简略；不能把trigger-order独有的全部TP/SL参数未经核对原样搬入普通POST。普通order→附带SL的关联与止损生命周期仍需独立验证。

本次ETH实验证明：自定义EL4305ffecaf2aL未见传播；父ID1001125142960934不同于普通单1001125142983995。普通详情clOrdId为子单自身ID，历史/fills为空，新TPSL1001125142983994缺父单或posId引用。这一结果与当前trigger文档缺少传播契约相符，但不证明所有未来版本/参数组合永久不支持。

## 文档读取与保存

web读取遇到403/502后，经本地Python HTTPS成功取得官方页面HTTP200（本机无PowerShell运行时）。原HTML和提取文本保存于`data/deepcoin_api_order_comparison_20260905/`，包括order、triggerOrder、changelog、cancelOrder、replaceOrder。未使用交易所写接口验证文档。
