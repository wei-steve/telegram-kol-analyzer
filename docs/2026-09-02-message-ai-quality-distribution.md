# 最近 500 条消息的 AI 识别展示分布

## 结论

本次样本显示，`runtime_not_authoritative` 不会造成“满屏黄框”：它只命中 11/500（2.2%），而且 11 条全部已因 `runtime_failed` 成为红卡，对黄色卡片数的独立贡献为 0。建议保留该信息，但降级为中性观测 chip，避免在已有红色失败语义时重复告警。

当前红色密度主要由 `<0.6` 触发：低置信度命中 148/500（29.6%），其中 129 条是唯一的 danger 原因；最终红卡为 161/500（32.2%）。为恢复“红色=尾部高关注”的视觉层级，建议将红色阈值调整为 `<0.3`，黄色区间调整为 `0.3–0.8`，`>=0.8` 保持中性。在本样本上，这会将红卡降至 88/500（17.6%），黄卡为 141/500（28.2%），无染色仍为 271/500（54.2%）。这是展示优先级建议，不是对识别准确率的校准；准确率阈值仍需待人工标注数据积累后再评估。

`image_not_sent` 只命中 8/500（1.6%），占模板口径“有图”消息的 8/255（3.14%）；其中只有 1 条以它作为唯一 danger 原因。它稀少但代表图像未进入模型的静默失效，建议保留红色。

## 数据范围与口径

- 观测时间：2026-09-02 11:37 UTC 后的单次短查询。
- 运行身份：Web=`b78f16098c591978fe764e15c9b793182fc97f5b`，manifest SHA-256=`4fb293c5d38bda320e00b60ee59d91f07dff08d57cf09222e04847f580842816`，`loaded_artifact_verified=true`。
- 样本：按 `raw_messages.id DESC` 取最近 500 条，实际 500 条，ID 13,949–14,448；`posted_at` 无 NULL，时间跨度为 2026-08-30 12:34:48 UTC 至 2026-09-02 11:19:47 UTC。
- 访问方式：直接连接生产 SQLite，URI `mode=ro`，同时强制 `PRAGMA query_only=ON`、`temp_store=MEMORY`和 1 秒 busy timeout。主聚合持续 1.399 秒，未建表、未写入、未抓取 Web 页面。
- 字段投影直接复用候选 release 中 `web_queries._serialize_raw_messages()`，并逐条复制 [`_messages.html`](../src/telegram_kol_research/templates/_messages.html) 的 Jinja 布尔顺序。当前 MiMo run 选择也与 Web 一致：先用当前 evidence 绑定的 run，否则选最新的 `became_authoritative=true` run，再否则选最新 run。上下文使用每条消息 ID 最新的 attempt。
- 模板名称“有图”的实际判定是 `message.media_assets` 非空，不会再按 `kind`/`mime_type` 排除视频或其他媒体。本报告为与 Jinja 完全一致也使用这一宽口径；因此 255 是“存在任何 media asset”，不是经语义重新确认的纯图片数。
- 百分比默认以 500 条为分母。特定分母会在表前明示。由于警示原因可重叠，“独立触发数”不应相加为卡片总数。

核心布尔口径与模板一致：

```text
context_called = context 存在 且 attempt_status 非 NULL
context_in_progress = context_called 且 attempt_status ∈ {pending, running, retry_pending, pending_reanalysis}
context_unresolved = unresolved_reason 为非空值
context_exhausted = context_called 且 attempt_status = exhausted
recognition_error = runtime_failed 或 projection_failed 或 context_exhausted

accepted_candidate_count = system_acceptance 存在时的投影值，否则为 0
missing_candidate = 策略类结论 且 signal_candidate_count = 0
candidate_not_accepted = 策略类结论 且 signal_candidate_count > 0 且 accepted_candidate_count = 0
image_not_sent = media_assets 非空 且 runtime 存在 且 runtime.input_kind = text
runtime_not_authoritative = runtime 存在 且 became_authoritative = false

danger_state = recognition_error 或 image_not_sent 或 missing_candidate 或 low_confidence
warning_state = 非 danger_state 且
                (medium_confidence 或 context_in_progress 或 context_unresolved 或
                 candidate_not_accepted 或 runtime_not_authoritative)
```

## 1. 互斥结论分类

Jinja 的优先级是：识别异常 → 需要上下文 → 开仓信号 → 仓位管理 → 闲聊无关 → 结论未记录。

| 分类 | 条数 | 占比 |
|---|---:|---:|
| 开仓信号 | 0 | 0.0% |
| 仓位管理 | 20 | 4.0% |
| 闲聊无关 | 92 | 18.4% |
| 需要上下文 | 133 | 26.6% |
| 结论未记录 | 242 | 48.4% |
| 识别异常 | 13 | 2.6% |
| **合计** | **500** | **100.0%** |

结论未记录不可解读为非策略或正常。`recognition_result` 和 `lifecycle_event_type` 各有 226/500（45.2%）为 NULL；`mimo_analysis` 和 runtime 各有 206/500（41.2%）为 NULL。原始结论值为：`是策略` 27、`非策略` 247、NULL 226；event type 为 `none` 253、`position_update` 15、`exit_position` 5、`cancel_entry` 1、NULL 226。

## 2. 置信度

| 区间 | 条数 | 占比 |
|---|---:|---:|
| `>=0.8` | 121 | 24.2% |
| `0.6–0.8` | 23 | 4.6% |
| `<0.6` | 148 | 29.6% |
| 未记录 | 208 | 41.6% |
| **合计** | **500** | **100.0%** |

分位数仅在 292 条非 NULL 置信度上计算，采用 R-7 线性插值：

| 最小值 | P25 | 中位数 | P75 | 最大值 |
|---:|---:|---:|---:|---:|
| 0.00 | 0.20 | 0.50 | 0.95 | 0.95 |

置信度是离散分布。关键值的条数为：0.0=19、0.1=46、0.2=9、0.3=59、0.4=7、0.5=8、0.6=12、0.65=3、0.7=7、0.75=1、0.8=9、0.85=5、0.9=32、0.95=75。这也是为什么阈值从 0.6 小幅改到 0.4 或 0.5 并不能显著降低红色密度。

## 3. 警示原因独立触发数

| 原因 | 条数 | 占比 |
|---|---:|---:|
| `runtime_failed` | 11 | 2.2% |
| `projection_failed` | 0 | 0.0% |
| `context_exhausted` | 2 | 0.4% |
| `image_not_sent` | 8 | 1.6% |
| `missing_candidate` | 11 | 2.2% |
| `low_confidence` | 148 | 29.6% |
| `medium_confidence` | 23 | 4.6% |
| `context_in_progress` | 0 | 0.0% |
| `context_unresolved` | 134 | 26.8% |
| `candidate_not_accepted` | 0 | 0.0% |
| `runtime_not_authoritative` | 11 | 2.2% |

`low_confidence` 是唯一 danger 原因的消息有 129 条（25.8%）。在没有任何 danger 原因的前提下，`context_unresolved` 是唯一 warning 原因的消息有 48 条（9.6%），`medium_confidence` 是唯一 warning 原因的有 5 条（1.0%）。

## 4. 卡片染色

| 结果 | 条数 | 占比 |
|---|---:|---:|
| `danger_state` | 161 | 32.2% |
| `warning_state` | 68 | 13.6% |
| 无染色 | 271 | 54.2% |
| **合计** | **500** | **100.0%** |

Jinja 先计算 danger，只有在非 danger 时才计算 warning，因此两类互斥。当前有染色卡片合计 229/500（45.8%）。

### 置信度阈值敏感性

只替换低/中置信度分界，其他 Jinja 条件不变：

| 红色区间 | 低/中/高/未记录 | 红卡 | 黄卡 | 无染色 |
|---|---:|---:|---:|---:|
| `<0.6`（当前） | 148 / 23 / 121 / 208 | 161（32.2%） | 68（13.6%） | 271（54.2%） |
| `<0.5` | 140 / 31 / 121 / 208 | 153（30.6%） | 76（15.2%） | 271（54.2%） |
| `<0.4` | 133 / 38 / 121 / 208 | 146（29.2%） | 83（16.6%） | 271（54.2%） |
| `<0.3`（建议） | 74 / 97 / 121 / 208 | 88（17.6%） | 141（28.2%） | 271（54.2%） |

调整为 `<0.3` 只改变红/黄优先级，不会将任何已有提示变成无染色；总关注卡仍为 229 条。

## 5. `runtime_not_authoritative` 深挖

11 条按可观测结构分成互斥三档：

| 结构类型 | 条数 | 占该类 11 条 |
|---|---:|---:|
| 选中的 run 是 `v1_fallback` | 0 | 0.0% |
| 同一消息存在 ID 更大的新 run | 1 | 9.09% |
| 其他 | 10 | 90.91% |

“其他”10 条的选中 run 全是 `v1_authoritative/failed`。全部 11 条都同时命中 `runtime_failed`、都已是 danger card，没有一条由 `runtime_not_authoritative` 单独产生 warning card。

这三档不是 Jinja 原有逻辑，也不是数据库中已记录的因果 reason code。它们是为本次分析增加的结构诊断：先判定选中 run 是否 `v1_fallback`，再判定是否存在更新 run，剩余列为其他。因为没有专用原因字段，它只能支持“降级为中性提示”，不支持删除溯源信息。

## 6. 媒体未送模型

| 口径 | 条数 | 占比 |
|---|---:|---:|
| 样本中 `media_assets` 非空 | 255 | 51.0%（占全样本） |
| 且当前 runtime `input_kind=text` | 8 | 3.14%（占 255 条媒体消息） |
| 同上，占全样本 | 8 | 1.6% |

## 7. 上下文与 shadow

| 口径 | 条数 | 占比 |
|---|---:|---:|
| 存在上下文调用记录 | 167 | 33.4%（占全样本） |
| `shadow_agrees_with_authoritative=false` | 35 | 20.96%（占 167 条已调用） |

35 条分歧的 `shadow_disagreement_direction` 全部为 `shadow_would_skip`，无其他方向或未记录方向。该观测 chip 未进入 danger/warning 判定。

## 8. candidate 落地三档

Jinja 只对已分类为“开仓信号”或“仓位管理”的消息计算落地 chip，因此本节分母是 20，不是 500。`signal_candidate_count` 是全部关联 candidate 数；接纳数严格使用 `system_acceptance.accepted_candidate_count`。

| 落地档位 | 条数 | 占 20 条策略类结论 |
|---|---:|---:|
| 无候选 | 11 | 55.0% |
| 有候选但零接纳 | 0 | 0.0% |
| 有接纳 | 9 | 45.0% |
| **合计** | **20** | **100.0%** |

“有候选但零接纳”为 0，因此本样本没有可按 `system_acceptance.reason_code` 分组的行，不存在未记录 reason 被当作零的情况。

## 建议与边界

1. **`runtime_not_authoritative`：降级为中性提示，不删除。** 11/500 不会刷屏，但 11/11 都与 `runtime_failed` 重叠，黄色语义冗余；保留中性 chip 仍可观测“未成为权威”。
2. **置信度：建议调整为 `<0.3` 红、`0.3–0.8` 黄、`>=0.8` 中性。** 当前 `<0.6` 使 148/500 命中低置信度、红卡达 32.2%；`<0.3` 把红卡降至 17.6%，而 `<0.4` 与 `<0.5` 仍为 29.2% 和 30.6%。高置信度界线 0.8 保留，因为它将 121 条高档与 97 条建议中档清晰分开。不得把 208 条未记录改成 0 或任何正常值。
3. **`image_not_sent`：保留红色。** 它只影响 1.6% 的全样本、3.14% 的媒体消息，且只独立产生 1 张红卡，不会造成刷屏；对静默图像输入漏失保持高可见性更重要。

本轮只提供分布与建议，没有修改阈值、代码、配置、schema 或任何生产数据。
