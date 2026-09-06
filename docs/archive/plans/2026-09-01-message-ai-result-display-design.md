# 群组消息 AI 识别结果展示设计

## 目标与边界

在群组消息正文下方提供一行可扫读的结论 chip，并把现有 AI 结论、图片证据、上下文结论与技术明细重新分层。筛选和统计只读取当前已加载 DOM；加载更多后重新计算。不得新增查询、网络路径、schema、写操作，也不得改变识别、上下文、交易、split runtime 代理或监听徽章逻辑。

## 数据流

`web_queries.py` 只复用已批量加载的数据，增加 `recognition_result`、`lifecycle_event_type`、`signal_candidate_count`，并在 `context_resolution` 中增加三个 shadow 观测字段。缺失值保留为 `None`，`lifecycle_event_type=None` 与原始值 `none` 不合并。

模板根据权威原始字段与项目事件词表生成分类、置信度、运行异常、图片、上下文、shadow 观测和候选落地 chip，同时为消息卡片写入只读 `data-*`。JS 只用这些 DOM 属性执行互斥筛选与已加载统计；CSS 负责风险色、层级和折叠表现。

## 展示语义

- 分类优先显示识别异常与待上下文；其余依据原始 recognition result 和 lifecycle event type 区分开仓、仓位管理、闲聊或未记录。
- candidate 落地严格三档：策略类且总数为零显示红色未生成；总数大于零但接纳数为零显示黄色未接纳及已有原因；接纳数大于零不显示。
- shadow chip 仅在已存在上下文调用、明确评估为不一致时显示，保持中性灰，不计入异常或需关注。
- 任何缺失值均显示未记录或不显示可选 chip，绝不填充推断值。

## 验证

先用渲染与静态资产测试覆盖只读投影、六类 chip、置信度三档、图片未送模型、三档候选落地、shadow 条件、缺失值、详情字段保留、筛选统计和加载更多更新。随后运行相关聚焦测试和最终候选的一次完整 pytest。部署为 web-only、`schema_changed=false`，回滚 SHA 取部署前三个角色的权威 runtime identity 实测值。
