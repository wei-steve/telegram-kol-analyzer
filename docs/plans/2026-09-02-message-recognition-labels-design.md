# 群组消息 AI 识别人工标注设计

## 目标与硬边界

在群组消息页为单条消息提供“正确 / 错了 / 不确定”的人工标注入口，并把标注当时的权威识别现场一并固化。本期只新增 `message_recognition_labels` 表，不修改任何现有表、识别、上下文、候选、执行、通知或交易语义。

标注表是纯观测真值记录：只有 Web 展示/API 路径可以读写；任何识别、上下文、候选、执行和通知模块都不得导入、查询或消费它。这条边界必须同时写入模型和服务代码注释。

## 表结构与不变式

`message_recognition_labels` 包含：

- `id` 自增主键；`raw_message_id` 外键指向 `raw_messages.id`，建立索引和唯一约束。不使用级联删除。
- `verdict` 只能为 `correct`、`incorrect` 或 `uncertain`。
- `error_kind` 可空，仅 `incorrect` 可以携带，且只能为已批准的八个错误类型。`incorrect` 本身不强制要求错误类型。
- `note` 可空，数据库与 API 双层限制长度不超过 2000 字符。
- 现场快照：`labeled_recognition_result`、`labeled_event_type`、`labeled_confidence`、`labeled_model`、`labeled_prompt_versions_json`、`labeled_prompt_versions_source`、`labeled_signal_candidate_count`、`labeled_accepted_candidate_count`、`labeled_context_attempt_status`。
- `labeled_prompt_versions_source` 只能为 `mimo_run`、`recognition_decision` 或 `NULL`。所有取不到的快照项都存 `NULL`，不用零、空串或任何推断值填充。
- `created_at` 在首次标注时写入并保留；`updated_at` 每次 upsert 更新。

数据库约束同时覆盖 verdict/error kind 词表、非 incorrect 不得携带 error kind、note 长度、prompt provenance 词表以及非负计数。

## 快照来源

写入快照与 upsert 在同一个数据库事务中执行。服务端按页面已有权威投影语义取值，客户端无法传入或覆盖任何 `labeled_*` 字段。

- recognition result 和 event type 来自 `RecognitionDecision.authoritative_payload_json` 的原始权威值。
- confidence 和 model 与当前页面 MiMo 权威投影保持一致。
- prompt versions 首选与当前页面权威结果绑定的 `MimoRecognitionRun.prompt_versions_json`，并记录 source=`mimo_run`；无对应 run 时降级使用 `RecognitionDecision.prompt_versions_json`，并记录 source=`recognition_decision`；两者都缺失则值和来源都为 `NULL`。
- candidate 总数取实际关联的 `SignalCandidate` 数；已接纳数取当前 `system_acceptance` 投影。投影不存在时保持 `NULL`，不用零代替未知。
- context attempt status 取当前上下文调用记录；未调用时为 `NULL`。

## API 与角色边界

`POST /api/messages/{raw_message_id}/recognition-label` 只接受 `verdict`、`error_kind`、`note`，额外键、非法枚举、非 incorrect 的 error kind 或超长 note 都返回 422。消息不存在返回 404。同一 `raw_message_id` 重复提交在唯一约束上 upsert，保留 `created_at` 并重新捕获全部现场快照。

`GET /api/messages/{raw_message_id}/recognition-label` 仅回读标注：消息存在但未标注返回 `label: null`，消息不存在返回 404。

两个路由只由 runtime role `all` 或 `web` 拥有；`worker`/`ingest` 返回 503 和 `label_not_owned_by_runtime_role`。路由不发 Telegram、不重跑识别、不触发候选或执行。

## 页面展示

现有消息批量投影只新增一次标注表批量查询，不使用逐消息 GET。每条 chip 行末尾显示低调标注按钮和内联表单。错误类型仅在 verdict=`incorrect` 时显示。

已标注 chip 使用人工专属视觉：正确和不确定为中性灰，错了为紫色，不复用系统红/黄异常色。筛选新增“已标注 / 未标注”，统计增加“已标注 N 条”，仍只基于已加载 DOM 且在加载更多后重算。

渲染时将当前 `recognition_result`、`lifecycle_event_type`、`confidence` 与标注快照做严格的 nullable 比对。任一项不同时在人工 chip 旁显示中性灰“识别已变更”，title 为“该标注针对的是标注当时的识别结果”。该提示不写库、不自动作废/删除/改写标注，不计入“需关注”。

## 精确 L3 变更与回滚

变更只新增一张表、它的约束和索引，既有 schema 与业务数据不变。生产步骤严格分为：

1. 候选代码完成聚焦测试、一次完整 pytest、审查、显式路径提交与推送。
2. 从三个角色各自的 `/api/runtime/deployment-identity`、systemd drop-in 和 release manifest 核实实际运行版本，实测 Web SHA 是 runtime rollback commit。
3. 为生产数据库建立经校验备份，在独立副本上演练精确 DDL、重复 bootstrap 幂等性、`PRAGMA quick_check`、外键检查以及受影响表/关键业务表前后计数。
4. 独立 schema 步骤在运行时控制锁下使用一个 `BEGIN IMMEDIATE` 事务创建新表/索引，再次校验备份 hash、quick check、外键与计数；不 stage、不 activate、不重启服务。
5. schema 验收后创建新的 Web-only runtime receipt，action manifest 声明 `schema_changed=false`，再单独 stage/activate Web。不写入任何虚假生产 verdict；真实 upsert 在生产库副本演练，生产只做无写验收。
6. 按 L1 Web 风险边界观察 15 分钟或 5 条真实消息（先到为止），然后更新运行角色 SHA 和已有“47 条待标注”状态。

完整回滚顺序固定为：

1. 如新表已有人工标注，先导出并校验标注数据。
2. 先将 Web runtime 回滚到部署前实测 SHA 并验证身份/健康，避免新 runtime 的 `Base.metadata.create_all()` 重建表。
3. 再在独立 schema 事务中删除 `message_recognition_labels`，并重新执行 quick check、外键和关键表计数校验。

若仅回滚 runtime 而保留新表，旧 Web 可完全恢复且标注数据不丢失。删表才是完整 schema 回滚，且会丢失未导出的标注。
