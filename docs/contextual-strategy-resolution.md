# 上下文策略线程二次判断

系统先由 MiMo 对每条 Telegram 消息做一次多模态识别，并把文字证据、图片证据、模型与提示词版本保存为不可变证据版本。后续需要跨消息判断时，DeepSeek 只读取已经保存的结构化证据、带时间的消息窗口、引用关系、候选策略线程和当前策略状态，不再重复读取图片，因此不会把当前文字与旧图片混成一条来源不明的指令。

二次判断只处理更新、取消、已有入场/持仓、引用消息、证据更新等需要上下文的情况。引用链是强证据，但不是唯一依据；同群组、币种、方向、时间邻近和当前策略状态会一起参与判断。无法唯一确定目标时必须返回 `unresolved`/`hold`，不允许猜测归属。

## 实盘开关

默认关闭。只有以下条件同时成立时才调用上下文模型：

- `auto_trade_enabled=true`
- `management_execution_mode=live`
- `context_resolution_enabled=true`
- 当前 Telegram `chat_id` 位于 `context_resolution_live_chat_ids`

本功能不使用 shadow 模式。关闭或不在白名单时，不调用二次判断模型，也不写入后台比较结果。

## 自动重分析

未解决决策会声明下一次触发条件。系统在同群新消息、引用目标补齐、策略/入场状态变化、交易所快照变化、消息编辑或证据版本变化时调度一次有界重分析。相同状态指纹不会再次调用 AI；并发 worker 只能领取同一代中的一个任务。已有 `submitted`、`succeeded`、`unknown` 或已对账指令时禁止重放。

命令行可安全处理一项调度任务：

```bash
telegram-kol-research resolve-context-once
```

该命令本身不配置交易所执行器。

## 排障

策略消息页面会显示线程根消息、关联关系、引用消息、证据版本和输入类型、二次决策置信度及证据消息 ID，以及未解决原因和下一触发条件。页面不会显示原始模型响应、图片 base64、密钥或完整交易所响应。

若任务持续失败，检查 `context_resolution_attempts` 的 `status`、`last_error`、`attempts` 和 `trigger_event_json`。达到最大次数后状态为 `exhausted`，只发送一次最终失败提醒。
