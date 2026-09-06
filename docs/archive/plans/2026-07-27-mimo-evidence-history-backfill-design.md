# MiMo 历史证据补录设计

## 目标

为已经写入 `raw_messages`、但尚未保存当前 MiMo 首次识别证据的历史消息补齐
`message_evidence_versions`。补录只负责生成并持久化文字/图片分离的证据，不执行
策略识别结果，不调用 DeepSeek，不创建或修改策略线程，也不触发任何 Deepcoin
读写。

首次生产补录只覆盖明确指定的上下文白名单群组。命令不从“全库”隐式扩张范围；
调用者必须通过一个或多个 `--chat-id` 指定群组，或显式选择已保存的上下文群组
列表。

## 方案选择

采用独立的、可恢复的批处理命令。相比服务启动时自动扫描，它不会占用监听服务的
关键路径；相比按需懒补，它能在启用跨消息二次判断前形成完整且可审计的历史证据。

补录状态直接由不可变证据表表示：

- 相同输入指纹已有 `completed` 证据：跳过，不调用 MiMo；
- 相同输入指纹已有失败证据：默认跳过，只有显式 `--retry-failed` 才重试；
- 消息文字、编辑时间或媒体内容发生变化：写入下一证据版本并 supersede 旧版本；
- 中断后再次运行：从仍然缺失或已变化的消息继续，不依赖内存游标。

MiMo 自身的有界重试继续生效；批处理不会无限循环失败记录。

## 数据流和安全边界

1. 按 `chat_id`、时间范围和稳定的 `(posted_at, message_id, id)` 顺序读取历史消息。
2. 计算消息文字、编辑时间和媒体内容哈希组成的输入指纹。
3. 根据当前证据版本决定 `process`、`skip_completed`、`skip_failed` 或
   `skip_empty`。
4. `--dry-run` 只输出计划统计；`--apply` 才调用 MiMo。
5. `--apply` 对每条消息只调用现有 `run_mimo_authoritative_for_message`，随后调用
   `persist_mimo_message_evidence`。
6. 每条消息独立提交，因此进程中断不会丢失已完成进度。
7. 批次之间按配置限速；输出仅含计数、消息数据库 ID、状态和简短错误，不输出模型
   原始响应、图片内容或凭据。

补录器不得调用以下入口：

- `assess_message_authoritatively`
- `process_authoritative_message`
- `resolve_contextual_strategy`
- 任意自动交易或策略管理执行器

这条静态边界会由测试约束。

## 命令接口

```bash
telegram-kol-research backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 100 \
  --delay-seconds 2
```

默认是 dry-run。实际执行必须增加 `--apply`。支持：

- 重复的 `--chat-id`；
- `--start-at` / `--end-at`；
- `--limit`（限制本批次最多调用 MiMo 的消息数，而不是被跳过的旧记录数）；
- `--scan-limit` / `--scan-cursor`（限制单次扫描 I/O 并按稳定 keyset 分页续跑）；
- `--delay-seconds`；
- `--retry-failed`；
- `--use-configured-context-chats`。

若最终没有任何群组 ID，命令拒绝运行。`--retry-failed` 只让同一失败指纹在本次
命令中重新尝试一次；模型内部仍保持现有有界重试。

实时识别和补录共用持久化消息级 claim/lease，避免同一输入被并发付费识别。模型
返回后必须再次核对输入指纹；发生编辑时丢弃旧结果。对外结果只返回稳定错误码。

## 验证

单元测试覆盖：

- completed 相同指纹不调用 MiMo；
- 缺失证据和变化指纹会补录；
- failed/image_unavailable 默认不重试，显式参数才重试；
- 只处理指定群组和时间范围；
- oldest-first、limit 和中断续跑；
- dry-run 零模型调用、零证据写入；
- 补录过程零 DeepSeek、零策略/交易写入；
- 文字+图片证据分离持久化；
- CLI 空群组范围时 fail closed。

服务器执行顺序为：先 dry-run 查看数量，再用小批次 `--apply`，核对证据版本和失败
率后逐步增加批次。上下文实时执行开关在整个历史补录期间保持关闭。
