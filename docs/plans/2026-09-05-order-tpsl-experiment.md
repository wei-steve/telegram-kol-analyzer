# Ordinary Order TP/SL Experiment Implementation Plan

**Goal:** 先验证普通order的clOrdId及附带TP/SL实际行为，准备用户自行执行的ETH最小量实验；不替换生产路由。

**Architecture:** 独立普通订单实验入口，复用已测试的原始GET采集和持久请求/回执逻辑。普通入口仅允许POST /trade/order、/trade/cancel-order；不会执行set/modify/cancel-position-sltp或replace-order-sltp。原trigger实验默认写入白名单保持不变。

**Tech Stack:** Python标准库、Decimal、pytest，服务器独立证据目录。

## 已确认范围

- 用户要求先测试研究普通order和止盈止损设置，再考虑当前项目trigger-order替换。
- 延用ETH-USDT-SWAP、cross/split、多限价P-1、空限价P+1、每侧最小合法量且不超过0.01 ETH和50 USDT名义额。
- 用户明确选择止盈10 USDT；止损延用10 USDT。多TP=入场+10、SL=入场-10，空侧反向。
- 新实验创建请求只用官方普通order参数：instId、tdMode、mrgPosition、side、posSide、ordType=limit、px、sz、clOrdId、tpTriggerPx、slTriggerPx。不给客户端ordId，不提交暂不支持的tag，不将trigger-order专属字段混入。
- 普通order文档没有明确列出附带保护的slOrdPx/tpOrdPx创建参数，因此不承诺附带保护必然采用指定执行类型；通过实际生成记录验证。set-position-sltp则有明确执行价参数，但属于后续独立实验，不自动覆盖当前保护。
- 第一阶段只有两笔真实普通限价单，各自附带TP/SL、唯一clOrdId；由用户亲自运行命令。助手仅运行模拟测试及只读验证、安装文件，不执行真实入口。
- 预检已有ETH仓位即停止；旧挂单只记为基线，绝不撤销。15秒行情时效、精确tick、最小量和金额上限、一次性标记、超时不重提继续有效。
- 两侧成功后观察300秒；部分/未知30秒留证，两侧明确拒绝也保留一次完整快照。每轮明确查询本次普通ordId/clOrdId，保存附带TPSL创建/历史及持仓字段。不以共享价格/时间代替归属。
- 收尾只撤销本次成功返回的普通ordId的未成交余量。拒绝/未知/仍pending均标记未解决。不会猜仓位或TPSL归属去平仓/撤保护。已成交仓位由用户自行处理。
- 当附带保护未能证实存在或覆盖不清，记录unknown并提示；不自动覆盖、补挂或扩大交易。

## 三种保护入口的研究结论

1. POST order附带tpTriggerPx/slTriggerPx：本轮实测对象。
2. POST replace-order-sltp：未成交普通限价委托，使用orderSysID；遗漏或0会取消对应设置。必须同时维护TP/SL的已知值；返回data={}，不新增保护ID证明。待首轮确定后再实验。
3. POST set-position-sltp：已有split仓位必须用posId，可指定tp/sl触发来源、执行价-1表示市价、sz控制部分数量；返回保护ordId，应持久化请求posId→响应ordId。可能覆盖已有保护，重复调用和多TP+全仓SL关系需单独测试。

修改保护：modify-position-sltp同时引用保护ordId与split posId；撤保护：cancel-position-sltp用保护ordId。附带TPSL无明确外键时不能用候选执行这些接口。

## 执行与验证

1. 先写失败测试：ordinary字段白名单、TP/SL方向、最小量、不带trigger字段、唯一clOrdId、未知结果不重提、普通撤单精确ID、旧trigger默认白名单不变。
2. 最小实现独立ordinary脚本与显式可选I/O白名单；原trigger入口默认路径保持。
3. 模拟端到端测试证明真实入口只能由显式CLI标志开启、部分失败仍采集、收尾失败非零退出。
4. 独立审查关键下单/保护/收尾边界，完成最终focused suite。
5. 上传独立服务器目录，静态AST和SHA256校验；不执行写入入口。给用户一条真实启动命令。

不改生产src、服务、数据库、交易模式或部署路由。本次独立工具用focused tests覆盖，无生产迁移或全套服务验证。
