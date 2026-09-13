# AI Provider & Model Routing Status（AI 提供商 / 按环节模型选择 / 备用切换）

设计：`docs/plans/2026-09-13-ai-provider-model-routing-design.md`（唯一设计真相）。
本文件是本项目的进度、决策与证据真相；每个阶段完成后更新。

```yaml
project: ai-provider-model-routing
started: 2026-09-13
integration_branch: codex/deepcoin-auto-trading-v1
base_commit: 8b9f20d2   # 开工时共享分支尖端
implementer: 子代理 opus-implementer（Opus 5 / high）
commander: Claude Fable 5.1 指挥会话
current_phase: 1
phase_status: planned        # planned | in_progress | completed | blocked
deploy: 未部署；部署与推送共享分支由指挥会话与用户决定
```

## 阶段总览

| 阶段 | 内容 | 状态 | 提交 | 测试 |
|---|---|---|---|---|
| 1 | 配置层：schema v2、迁移、load/save、派生兼容视图、stage 目录、`ai-config-show` | planned | | |
| 2 | 路由 + 权威识别链接线 + attempts `model` 列 + 健康线按链首过滤 + 预算/租约测试 | planned | | |
| 3 | 其余环节接线（context_resolution / semantic_review / strategy_alert / research_chat / batch_* / 探测 / 提示词测试） | planned | | |
| 4 | Web：`/api/ai-providers*`、`/api/ai-stages`、两页模板 + JS + CSS、旧接口兼容、浏览器验证截图 | planned | | |
| 5 | 文档：ARCHITECTURE 新节、example.yaml v2、README、本文件收口 | planned | | |

## 设计未覆盖、由实施者决定的事项

（实施中逐条追加：决定了什么、为什么、影响面。）

## 已知限制 / 后续课题

- 主模型故障期间每条消息都先付一次主模型失败的代价；跨消息熔断/冷却未做（设计 §4）。
- 上下文结合分析触发频率偏高，本次不改（`docs/known-issues-and-deferred-work.md`）。

## 证据

（每阶段：测试计数、关键用例名、浏览器截图路径。）
