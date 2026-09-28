# 「动态」页改为跨群时间线 — 设计稿

日期：2026-09-28（调研于 2026-09-27，生产 HEAD 4ff0d43c）
状态：**待用户确认**（确认前不写代码）
风险级别：L1（只读页面改动，不碰识别 / 执行 / 交易所；无 schema 变更）

## 1. 现状（读代码得到的事实）

| 环节 | 「群组」页右侧 | 「动态」页 |
|---|---|---|
| 容器 | `index.html` 的 `[data-workbench-panel="groups"] [data-detail-panel]` | `[data-workbench-panel="activity"] [data-detail-panel]` |
| 取数 | `GET /groups/{chat_id}/detail` → `_strategy_detail.html` → include `_messages.html` | **同一个路由、同一个模板**，只是塞进另一个容器 |
| 群从哪来 | 左侧选中的群 | 也是左侧选中的群；打开动态页前会先强制加载整个群组页（`ensureWorkbenchViewLoaded('groups')`） |
| 查询 | `web_queries.load_group_message_page` → `_group_messages_query(chat_id=…)`，`ORDER BY posted_at DESC, message_id DESC`，走 `ix_raw_messages_chat_posted_message` | 同左 |
| 加载更多 | `GET /groups/{chat_id}/messages?before_message_id=` | 同左 |
| 卡片内容 | `_serialize_raw_messages`（约 20 条按 `raw_message_id IN (...)` 的批量查询） | 同左 |
| 13 个识别筛选按钮 + 统计行 | **纯前端**：读卡片上的 `data-message-*` 属性（`messageMatchesInsightFilter` / `updateMessageInsightView`），只统计已加载的卡片 | 同左 |
| 搜索 / 发送人筛选 | 服务端 `LIKE`，限定在单群内 | 同左 |

结论：两页共用的是**整套卡片模板 + 序列化层 + 前端绑定**，区别只有外面套哪个容器。`_serialize_raw_messages` 本来就按 `(chat_id, message_id)` 批量取执行绑定，天然支持多群混排，不需要改。

另外两个与本需求相关的现状：

- **消息列表现在没有自动刷新。** 5 秒轮询 `/api/freshness` 和 SSE 回调里都判断 `activeView === 'messages'`，但工作台视图集合早已是 `strategies/positions/activity/groups/more`（`d9d921b9` 懒加载改造后），`messages` 永远不成立。生产 web 是独立进程，SSE broker 收不到 ingest 的事件，实际只剩轮询在刷左侧群组列表。群组页和动态页的消息列表都只能手动「立即刷新」。
- **前端大量函数取「页面上第一个」消息面板**（`getMessagePanel()` = `document.querySelector('[data-messages-panel]')`），群组页的面板在 DOM 里排在动态页之前。两页同时加载过时，动态页上的部分按钮其实在操作群组页的面板。改造时动态页必须全程用自己那个面板。

## 2. 数据怎么取

### 2.1 查询

新函数 `web_queries.load_timeline_message_page(session_factory, *, page_size, before_raw_message_id=None, ...)`：

```sql
-- 第一页
SELECT * FROM raw_messages ORDER BY posted_at DESC, id DESC LIMIT :page_size + 1;
-- 下一页：游标 = 上一页最后一条的 raw_messages.id
--   先按主键点查它的 posted_at，再做 keyset：
SELECT * FROM raw_messages
 WHERE posted_at < :p OR (posted_at = :p AND id < :id)
 ORDER BY posted_at DESC, id DESC LIMIT :page_size + 1;
```

- 排序键 `(posted_at DESC, id DESC)`。跨群时 `message_id` 是各群自己的编号，不能当平局裁决；`id` 是全局自增主键。
- 游标只传一个整数 `before_raw_message_id`，服务端按主键取它的 `posted_at`，前端不用处理时间格式。游标行若已被删除（保留期清理），返回空页并结束「加载更多」，不报错。
- 结果交给现有 `_serialize_raw_messages`，一行不改。

### 2.2 EXPLAIN QUERY PLAN（生产库只读，`mode=ro`，2026-09-27，SQLite 3.42.0，`raw_messages` max id 19504）

```
-- 第一页
SCAN raw_messages USING COVERING INDEX ix_raw_messages_posted_at
-- keyset 下一页
SEARCH raw_messages USING COVERING INDEX ix_raw_messages_posted_at (posted_at<?)
```

- 第一页的 `SCAN ... USING INDEX` 是**按索引顺序走、拿到 LIMIT 条就停**，不是全表扫描；没有 `USE TEMP B-TREE`（不需要排序）。
- 能不排序的原因：`ix_raw_messages_posted_at` 是单列索引，SQLite 的普通索引隐含带 rowid，而 `id` 就是 rowid，所以 `(posted_at, id)` 的顺序索引里已经有了。对照组：沿用 `message_id` 当平局裁决会出现 `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`——这是改用 `id` 的第二个理由。
- 生产 `posted_at IS NULL` 的行数为 0（按索引点查得到），游标不需要处理 NULL。代码里仍对 NULL 行做兜底：它们排在最后，并且不能作为游标。
- 生产没有 `sqlite_stat1`，计划不受统计信息漂移影响。

**不需要新索引，没有 schema 变更。** 实现时加一条测试：对真实编译出的 SQL 跑 `EXPLAIN QUERY PLAN`，断言走 `ix_raw_messages_posted_at`、没有裸 `SCAN raw_messages`、没有 `TEMP B-TREE`，防止以后有人改排序键时悄悄退化成全表扫描。

### 2.3 跨群下不做服务端文本搜索

单群搜索的 `LIKE '%x%'` 被 `chat_id` 限住了范围；跨群时一个少见的词会沿索引把全部约 2 万行（连同 `text` 列）读一遍才凑不满 LIMIT，这就是一次变相的全表扫描。所以动态页**不提供**「Search / Sender」表单（见第 7 节第 3 点）。13 个识别筛选按钮是前端过滤，不受影响。

### 2.4 路由

- `GET /activity/timeline` → 整个动态页片段（头部 + 首页卡片）。
- `GET /activity/messages?before_raw_message_id=` → 下一页，返回与现在 `/groups/{chat_id}/messages` 同结构的 `_messages.html` 片段，前端沿用「取 `[data-message-list]` 追加、替换 footer」的逻辑。
- 两个路由都只读，web 角色可服务；不做 `load_group_rows` 那种全群聚合。

## 3. 卡片上醒目显示群名

- `_messages.html` 加一个模板开关 `show_group_name`（默认 false，群组页不传，渲染结果与现在逐字节相同）。
- 为 true 时，卡片头部最左边、发送人之前加一个群名标签：`<span class="message-group-badge" data-chat-id=… style="--group-hue: …">群名</span>`。颜色按 `chat_id` 固定映射到一组色相，同一个群在整页上颜色一致，扫一眼就能分组；文字本身写群名，不靠颜色传达信息。
- 群名来源：`group_config` 里该群的 `custom_group_label` 或 `chat_title`，再经 `group_labels_by_title` 映射，与群组页左侧列表同名；没配置的群显示 `群 <chat_id>`。全部在内存里算，不查库。
- 点群名标签跳到「群组」页并选中该群（复用现有 `[data-group-link]` 点击）。
- 卡片右上角的 `#message_id` 保留；跨群时它不唯一，但它是 Telegram 里的编号，排查时要用。

## 4. 筛选按钮与统计行

| 控件 | 跨群下是否成立 | 处理 |
|---|---|---|
| 识别异常 / 低置信度 / 用了上下文 / 有图片 / 未生成候选 / 已标注 / 未标注 / 分类不一致 / 管理类 / 目标未知 / 目标精确 / 契约违规 | 全部成立：判据只看单张卡片自身的属性，与群无关 | 原样复用 |
| 上方一排「全部 / 已识别策略 / 未识别 / 图片消息 / 异常」 | 成立，同样只看卡片属性 | 原样复用 |
| 展开全部 / 恢复默认 / 折叠全部 | 成立 | 原样复用 |
| 人工标注表单、立即识别、查看策略记录 | 成立：都按 `raw_message_id` / `lifecycle_id` 操作，不依赖所选群 | 复用；「立即识别」完成后刷新的是动态页自己的第一页，而不是所选群 |
| Search / Sender 表单 | 不成立（见 2.3） | 动态页不渲染 |
| 群头部「最新消息 · 共 N 条 · 上下文二次判断：已启用」「刷新策略」 | 单群语义 | 动态页不渲染 |

统计行照旧**只统计已加载的卡片**，文案前加上群数：`已加载 N 条（来自 K 个群）：识别成功 … · 需关注 … · 已标注 …`。K 由前端按卡片的 `data-chat-id` 去重得到，不另查库。不做全库口径的统计，那需要聚合查询。

## 5. 「群组」页保持不变

- 不改 `/groups/{chat_id}/detail`、`/groups/{chat_id}/messages`、`load_group_message_page`、`_group_messages_query`、`_strategy_detail.html`。
- `_messages.html` 只新增由 `show_group_name` / `timeline_scope` 控制的分支，群组页走的都是默认值。测试里对群组页片段做一次渲染对比：改动前后关键结构一致、没有群名标签。
- 动态页不再依赖左侧选中的群，打开动态页也**不再先加载整个群组页**（现在会触发一次 `load_group_rows` 全群聚合）。
- 顺带修掉第 1 节说的「取第一个面板」问题，但只在动态页相关的调用路径上改为传入面板本身；群组页的调用在只有它一个面板时行为不变。

## 6. 自动刷新 / 新鲜度

- 头部保留「数据库最新消息时间」「数据新鲜度」「监控状态」「立即刷新」四项（全局口径，本来就是全局的）；去掉单群的「最后入库时间」。
- 新消息提示：动态页记住首页最大的 `raw_messages.id`。现有 5 秒轮询 `/api/freshness` 已经返回全局 `raw_message_id`，动态页可见时拿它比较，更大就在顶部显示「有新消息 ↑」按钮，点击重新加载第一页并回到顶部。**不增加任何新的轮询或查询。**
- 默认不自动替换列表（见第 7 节第 4 点）：用户可能正在展开某张卡片或填标注表单，自动重排会把内容从手底下拿走。
- 顺带发现（不在本次范围）：`/api/freshness` 的全局部分 `SELECT max(id), max(created_at), max(posted_at) FROM raw_messages` 在生产是 `SCAN raw_messages`（`created_at` 没有索引），每个打开的浏览器页每 5 秒一次。这是现有问题，本方案不加重也不修，另开任务处理。

## 7. 需要你拍板的点

1. **显示哪些群**：推荐「raw_messages 里所有有消息的群」，与群组页左侧列表一致（含未配置交易的群）。另一个选项是只显示已配置的群。
2. **按什么时间排序**：推荐 Telegram 发送时间 `posted_at`（与群组页一致）。代价：补拉（reconcile）进来的迟到消息会按原发送时间插在下面，不会出现在顶部；按入库时间排序则相反，但同一群的消息顺序会被打乱。
3. **跨群文本搜索**：推荐动态页不提供（理由见 2.3），要搜索回群组页在单群内搜。如果一定要，可以做成「只在已加载的卡片里搜」的前端过滤。
4. **新消息到达时**：推荐只显示「有新消息 ↑」按钮、由你点击后刷新；另一个选项是「列表在顶部且没有打开的表单时自动刷新，否则显示按钮」。
5. **每页条数**：推荐沿用 20 条，滚到底自动加载下一页（与群组页一致）。多群混排时可改 30。
6. **群名标签可点击跳转到群组页**：推荐要。

## 8. 实施与验证

- 查询层测试：多群混排按 `(posted_at DESC, id DESC)` 排序；同一时刻平局按 `id`；游标翻页不重不漏（三页拼起来等于全量顺序）；游标行不存在时返回空页；`posted_at` 为 NULL 的行排在最后；`EXPLAIN QUERY PLAN` 断言走索引。
- 渲染测试：`/activity/timeline` 与 `/activity/messages` 返回卡片带群名标签和 `data-chat-id`、没有 Search 表单、「加载更多」携带 `before_raw_message_id`；群组页片段没有群名标签（回归）。
- 前端：`node --check`；本地起服务，用本地库截图对比改动前后。
- 开发中跑相关测试，最终候选跑一次 `uv run python -m pytest -q`。
- 交付到「全量通过、候选 sha 就绪」为止，不部署、不推 origin/main；部署前 rebase 到最新 origin/main。上线观察按 L1：15 分钟或 5 条真实消息。
