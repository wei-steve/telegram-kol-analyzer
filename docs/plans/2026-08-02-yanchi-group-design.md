# 颜驰 11分组接入设计

## 目标

将 Telegram 群组“颜驰 11分组”加入生产系统的目标群组配置，使系统同步该群消息，但不启用 AI 策略识别和自动交易。

## 方案

1. 使用生产 Telegram 会话按完整群名发现群组，取得权威 `chat_id`。
2. 在 `config/groups.yaml` 中增加唯一群组项，安全默认值与当前群组保持一致：
   - `enabled: true`
   - `ai_strategy_enabled: false`
   - `trading_mode: notify_only`
   - `max_loss_usdt: 100.0`
   - `symbol_whitelist: [BTC, ETH]`
3. 为该 `chat_id` 在 `config/kol_codes.yaml` 中分配唯一短码 `YC`，保留日后启用交易时的稳定归属标识。

## 数据流与安全性

监听器只会将该 `chat_id` 的消息纳入已配置群组的同步范围。由于 AI 策略开关保持关闭，新群不会生成 AI 交易策略；`notify_only` 同时确保不会提交真实订单。现有识别、上下文解析和仓位管理路径不做任何修改。

## 验证与回滚

本地加载 YAML 并检查群名、`chat_id` 和关闭的自动化开关。生产上先执行只发现模式，再安装可编辑包、重启服务，最后检查服务状态和群组配置。回滚时删除该群组项及 `YC` 映射，然后重启服务。
