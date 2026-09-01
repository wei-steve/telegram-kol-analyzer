# AI 上下文解析触发、有效性、窗口与成本分析

> 日期：2026-08-31
>
> 性质：只读生产数据分析，不是改造方案的实施记录
>
> 边界：本次未修改代码、settings、白名单、词表、阈值或数据库；未重启、未部署、未执行 Deepcoin 写入。

## 1. 结论摘要

1. 35 个日历日内有 4,245 条上下文解析 attempt，对应 2,928 条不同原始消息、5,923 次 provider 请求。在同期 6,163 条原始消息中，曾触发上下文解析的消息占 **47.51%**。
2. `multiple_same_source_candidates` 出现 3,906 次，覆盖 **92.01%** 的 attempt，是绝对主因。但它不能直接整体关闭：“仅该触发器”仍有 124 次实质改变。
3. 在 2,767 条有可比较最终决策的 attempt 中，580 条（**20.96%**）实质改变了第一层结论，2,187 条（**79.04%**）未改变。以全部 4,245 条 attempt 为分母，实质改变产出率为 **13.66%**；其余 86.34% 是未改变或未留下可比较决策。
4. 每条持久化请求平均 77.23 KB，其中 `message_context` 平均 68.86 KB，占 **89.16%**；历史消息本身平均 60.78 KB。上下文最大的成本来源是历史消息，不是 reply 链。
5. 盲目收缩为 20 条 / 24 小时，历史回放可将平均持久化请求从 77.23 KB 降到 38.03 KB，加权节省约 **50.75%**；但 580 次真正改变决策的调用中，72 次（**12.41%**）会丢失模型明确引用的支持/反对消息。因此该简单收缩不安全。
6. 上下文解析的 35 日混合均值约为 **3.51–4.67M 输入 token/日**；剔除 8 月 21–23 日网络错误造成的密集双次重试后，正常日代理值约 **1.67–2.22M/日**。主识别按近期 218 条消息/日估算为 **4.3–6.2M 输入 token/日**。这是 UTF-8 字节代理，不是 provider 账单。
7. 在同一不可变备份中，`context_resolution_attempts` 主 B-tree 为 333.951 MB，8 个索引合计 0.741 MB，合计 **334.692 MB，占全库 41.44%**。六个 JSON 列为 329.466 MB，占表本体 98.66%，日增约 9.41 MB。
8. 2026-08-31 陈旧 lifecycle/binding 清理晚于本数据集最后一条 attempt；清理后 30 分钟观测窗口内又没有新消息。因此**没有清理后的触发率分母，不能宣称触发率已下降**。

## 2. 数据源、口径与限制

### 2.1 生产数据源

所有数据库查询均使用生产维护证据中的不可变备份，以 SQLite `-readonly` 方式直接查询：

- 备份：`/var/lib/telegram-kol-maintenance-evidence/unified-claim-alignment-00cda060-20260831T143155Z/research-before.db`
- 字节数：807,567,360（770.156 MiB）
- SHA-256：`d8b1ebd73da9bb2da2af10e1094adad1a0d19d0311a74b1f6d21b5b8eca96a27`
- `page_size=4096`，`page_count=197160`，`freelist_count=0`
- attempt 时间：2026-07-27 09:38:38.230117 至 2026-08-30 01:17:57.357331，横跨 35 个日历日

结构、触发语义和窗口构建逻辑以当前代码为准：

- `src/telegram_kol_research/authoritative_recognition.py`
- `src/telegram_kol_research/contextual_message_window.py`
- `src/telegram_kol_research/context_resolution.py`
- `src/telegram_kol_research/context_resolution_prompt.py`

### 2.2 `reanalysis_triggers_json` 的实际语义

`reanalysis_triggers_json` 不是本次调用前的 8 个确定性触发器。它持久化的是上下文模型返回的“未来在什么事件下重分析”类型，例如 `strategy_state_changed`。

因此，本文的 8 触发器分布不是直接枚举该列，而是对每条 `request_summary_json` 使用当前 `requires_context_resolution()` 逻辑进行精确重建。数据中无无效 request JSON，且每条 attempt 都至少重建出一个触发器。

### 2.3 “实质变化”定义

对比第一层结果与 `decision_json`，构建如下语义签名：

- 动作族：`new` / `no_action` / `manage` / `cancel` / `exit` / `revise`；
- 目标 lifecycle / strategy；
- 决策是否可应用；
- 可静态确定的风险例外。

理由文字和 confidence 数值的变化不算实质变化；同一 `manage` 动作族内的更细 management action 变化也未计入。因此，本文的“改变”是保守下界，“不变”是上界。

## 3. 触发分布

### 3.1 8 个触发器

分母为 4,245 条 attempt。一条 attempt 可同时命中多个触发器，因此占比之和大于 100%。

| 触发器 | 次数 | attempt 占比 |
|---|---:|---:|
| `multiple_same_source_candidates` | 3,906 | 92.01% |
| `management_without_exact_target` | 533 | 12.56% |
| `entered_holder_language` | 515 | 12.13% |
| `revision_language` | 208 | 4.90% |
| `text_image_conflict` | 93 | 2.19% |
| `apparent_entry_may_be_revision` | 70 | 1.65% |
| `cancellation_language` | 17 | 0.40% |
| `reply_target_disagreement` | 7 | 0.16% |

### 3.2 单触发与多触发

| 同时命中数 | attempt 数 | 占比 |
|---:|---:|---:|
| 1 | 3,306 | 77.88% |
| 2 | 780 | 18.37% |
| 3 | 153 | 3.60% |
| 4 | 6 | 0.14% |

没有 0 触发器的 attempt，也没有同时命中 5 个及以上的样本。

### 3.3 触发率与 2026-08-31 清理前后对比

清理前的完整历史口径：

- 同期原始消息：6,163 条；
- 曾触发上下文解析的不同原始消息：2,928 条；
- 按消息计算的触发率：**47.51%**；
- attempt 行：4,245，其中 1,317 行是同一消息的重分析/新 fingerprint；
- provider 请求：5,923 次，即 169.23 次/日、1.395 次/attempt。

2026-08-31 14:41:14Z 终结 35 个陈旧 lifecycle 和 46 个陈旧 binding。但数据集中最后一条 attempt 为 2026-08-30 01:17:57，早于清理。清理后 2026-08-31T16:12:36.995Z–16:42:37Z 的 30 分钟窗口内是 0 条新消息、0 次 AI 调用。

**所以清理后样本不是“少”，而是没有分母，无法计算触发率，也无法得出清理已降低触发率的结论。**

仅作历史反事实上界：4,245 条 attempt 中，1,101 条包含后来被清理的 lifecycle 候选；如从当时窗口删去这些候选，320 条会失去 `multiple_same_source_candidates`，其中最多 232 条在其他简单触发器上也不再命中，历史对应 325 次 provider 请求，即最多约 9.29 次/日、占历史 provider 请求 5.5%。从请求 JSON 删去这些 candidate/active 项的加权字节节省约 1.25%。这是上界估算，不是清理后观测结果。

## 4. 上下文解析的有效性

### 4.1 总体改变率

数据质量：

- attempt 总数：4,245；
- `decision_json` 为空：1,478；
- 有效可比较决策：2,767；
- 无效 request JSON：0；
- 无效非空 decision JSON：0。

| 口径 | 实质改变 | 结论不变/无可比较决策 | 改变率 |
|---|---:|---:|---:|
| 仅 2,767 条有决策 attempt | 580 | 2,187 | 20.96% |
| 全部 4,245 条 attempt | 580 | 3,665 | 13.66% |
| 仅 `completed` 的 2,560 条 | 457 | 2,103 | 17.85% |

第二行中 3,665 包含“结论不变”和“调用没有留下决策”，因此 13.66% 是每条已记录 attempt 的实际改变产出率，不能与 20.96% 混用。

状态分布也说明了为什么有 1,478 条无决策：

| 状态 | attempt 行 | provider 请求 | 留下决策 |
|---|---:|---:|---:|
| `completed` | 2,560 | 2,734 | 2,560 |
| `exhausted` | 1,439 | 2,901 | 25 |
| `superseded` | 178 | 198 | 178 |
| `failed` | 68 | 90 | 4 |

### 4.2 按触发器分组

同一 attempt 可进入多行。“有决策改变率”的分母只是非空决策；“全部触发产出率”以所有该触发器 attempt 为分母，把失败/无决策也算入成本。

| 触发器 | 触发 attempt | 有决策 | 实质改变 | 有决策改变率 | 全部触发产出率 |
|---|---:|---:|---:|---:|---:|
| `multiple_same_source_candidates` | 3,906 | 2,508 | 474 | 18.90% | 12.14% |
| `management_without_exact_target` | 533 | 380 | 377 | 99.21% | 70.73% |
| `entered_holder_language` | 515 | 380 | 119 | 31.32% | 23.11% |
| `revision_language` | 208 | 130 | 22 | 16.92% | 10.58% |
| `text_image_conflict` | 93 | 48 | 8 | 16.67% | 8.60% |
| `apparent_entry_may_be_revision` | 70 | 59 | 23 | 38.98% | 32.86% |
| `cancellation_language` | 17 | 11 | 4 | 36.36% | 23.53% |
| `reply_target_disagreement` | 7 | 7 | 4 | 57.14% | 57.14% |

最接近“几乎从不改变结论”的组合不是整个 `multiple_same_source_candidates`，而是“仅该触发器，没有其他简单触发”：

- 3,005 条 attempt；
- 4,273 次 provider 请求；
- 1,885 条有决策；
- 124 条实质改变；
- 有决策改变率 6.58%，全 attempt 改变产出率 4.13%。

`text_image_conflict` 的全 attempt 改变产出率也只有 8.60%，`revision_language` 为 10.58%。但这些组都不是零，尤其“仅 multiple”仍有 124 次实质改变，因此历史数据不支持直接关闭。

## 5. 上下文窗口的实际规模

### 5.1 字节构成

以持久化 JSON 的 UTF-8 字节数为准；这是线上 request 大小的最佳现有代理，不是 provider tokenizer 结果。

| 组件 | 平均 | P50 | P90 | P95 | 最大 | 其他 |
|---|---:|---:|---:|---:|---:|---:|
| 整个 request | 77,231.90 B | 78,850 | 120,437 | 137,262 | 161,598 | — |
| `message_context` | 68,859.88 B | 70,337 | 106,053 | 121,840 | 142,493 | 占 request 89.16% |
| `recent_messages` | 60,781.81 B | 63,102 | 98,609 | 104,380 | 122,945 | 占 request 78.70% |
| active strategies | 6,088.85 B | 5,500 | 13,057 | 13,972 | 15,193 | — |
| candidates | 3,561.36 B | 2,116 | 10,551 | 10,638 | 11,836 | — |
| first pass | 1,561.40 B | 1,510 | 2,212 | 2,432 | 5,029 | — |
| saved evidence | 1,482.19 B | — | — | — | — | — |

`message_context` 合计 292,310,206 B，其中 `recent_messages` 合计 258,018,789 B。因此窗口优化的主要对象是历史消息文本与附属元数据。

### 5.2 条数和上限利用率

| 窗口项 | 上限 | 平均 | P50 | P90 | P95 | 最大 | 达上限 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 历史消息 | 50 | 36.71（73.42%） | 42 | 50 | 50 | 50 | 1,677（39.51%） |
| reply 链 | 5 | 0.07（1.40%） | 0 | 0 | 1 | 3 | 0 |
| active strategies | 50 | 20.42（40.84%） | 19 | 43 | 46 | 50 | 70（1.65%） |
| candidates | 20 | 6.26（31.30%） | 4 | 20 | 20 | 20 | 539（12.70%） |

72 小时年龄上限的实际利用：

- 最旧 `recent_message` 平均年龄 56.95 小时，为上限的 79.10%；
- P50 62.58 小时（86.92%），P90 71.52，P95 71.75，最大 72；
- 4,240 条可计算年龄的 attempt 中，3,975 条（**93.64%**）包含超过 24 小时的历史。

reply 深度上限 5 从未命中，实际最大只有 3，但 reply 本身的平均字节规模很小，收紧它的节省也很小。

## 6. 20 条 / 24 小时收缩回放

对每条已持久化窗口执行不改变排序规则的静态截断：

| 指标 | 现行窗口 | 20 条 / 24h | 变化 |
|---|---:|---:|---:|
| 历史消息平均条数 | 36.71 | 13.10 | -64.32% |
| 历史消息 P50 | 42 | 14 | — |
| 历史消息 P90/最大 | 50/50 | 20/20 | — |
| 假设 request 平均 | 77,231.90 B | 38,033.72 B | -39,198.18 B |
| 假设 request P50 | 78,850 B | 37,680 B | — |
| 假设 request P90 | 120,437 B | 60,911 B | — |
| 假设 request 最大 | 161,598 B | 89,668 B | — |

按“总删减字节 / 总原字节”计算，节省为 **50.75%**；先对每条计算百分比再取平均则为 48.34%。

对 580 次实质改变决策的调用，以决策中明确持久化的 supporting/opposing message ID 为依赖代理：

| 收缩方式 | 会丢失至少一条引用消息的改变决策 | 占 580 次改变 |
|---|---:|---:|
| 仅限 20 条 | 37 | 6.38% |
| 仅限 24 小时 | 70 | 12.07% |
| 20 条 + 24 小时 | 72 | 12.41% |

580 次改变中，463 次留下了 supporting/opposing ID，117 次没有；另有 1 个引用 ID 无法在当次持久化窗口中匹配。模型引用是当前最强的历史依赖代理，但它不是因果证明；没有引用也不等于没有依赖。因此 12.41% 是可观测的明确风险，不是风险上界。

## 7. Token 成本估算

### 7.1 方法

当前表未持久化 provider 的 prompt/completion token usage，因此不能还原账单。估算方法是：

1. 对持久化 request 及实际 system prompt 计算 UTF-8 字节；
2. 用 3–4 UTF-8 字节/token 给出范围，而不伪造单点精度；
3. 输出只按持久化 decision JSON 估算，不包含未持久化的失败 raw response；
4. 图片 token、provider 内部 thinking token、cache 命中与计价差异都未计入。

### 7.2 上下文解析

- 上下文 system prompt：2,455 B；前缀：67 B；
- 按 provider 调用次数加权的 request：80,351.73 B/次；
- 含 system prompt 后：约 82,873.73 B/次；
- 历史 5,923 次 / 35 日 = 169.23 次/日；
- 输入约 14,024,603 B/日 = **3.51–4.67M token/日**；
- 成功 decision JSON 平均约 493 B，可观测输出约 0.02–0.03M token/日。

35 日均值受 8 月 21–23 日网络故障强烈污染：1,367 条 `network_error` exhausted attempt 产生 2,734 次 provider 请求，每条恰好重试两次。剔除这些行后：

- 2,874 条 attempt，3,185 次 provider 请求；
- 82.11 attempt/日，91 provider 请求/日；
- 73,303 B/请求，6,670,586 B/日；
- 正常日代理值约 **1.67–2.22M 输入 token/日**。

这个正常日值仍然是历史代理，不是 2026-08-31 清理后实测；清理后没有足够样本。

### 7.3 主识别

当前主识别也在调用前构建完整上下文。主识别没有持久化完整 wire request，所以只能用上下文 attempt 中的窗口大小作代理：

- 共享 system prompt 约 7,450 B，MiMo vision 附加 prompt 约 1,401 B，连同分隔符合计约 8,853 B；
- 窗口代理平均 68.9 KB，加当前消息和元数据后估计 78–85 KB/调用；
- 按近期 218 条消息/日、稳定日 prompt invocation 约为消息数 1.01 倍估算，输入约 17–19 MB/日；
- 折算为 **4.3–6.2M 输入 token/日**；
- 已持久化 authoritative output 平均约 776–873 B，对应可观测输出约 0.04–0.07M token/日。

主识别估算的不确定性高于上下文解析：只有已触发第二层的消息才留下完整 request 样本，它们的窗口可能比未触发消息更大；图片 token 也没有计入。

### 7.4 合并看每日成本

| 阶段 | 正常日输入 token 代理 | 35 日故障混合口径 | 置信度 |
|---|---:|---:|---|
| 主识别 | 4.3–6.2M/日 | 未单独还原 | 中低：没有 wire usage |
| 上下文解析 | 1.67–2.22M/日 | 3.51–4.67M/日 | 中：有 request 字节，无 tokenizer usage |
| 合计 | 5.97–8.42M/日 | 不建议混合相加 | 中低 |

正常日口径下，上下文解析约占两层文本输入的 26%–28%；在网络故障混合均值中会接近 45%。这些比例同样不包图片和 thinking token。

## 8. 对数据库体积的贡献

### 8.1 物理体积与全库占比

在与 4,245 条 attempt 完全同时点的不可变备份中：

| 项目 | 字节 | MB（10^6） | 占 807,567,360 B 全库 |
|---|---:|---:|---:|
| 表主 B-tree | 333,950,976 | 333.951 | 41.35% |
| 8 个索引 | 741,376 | 0.741 | 0.09% |
| 表 + 索引 | 334,692,352 | 334.692 | **41.44%** |
| 六个 JSON 的逻辑字节 | 329,466,347 | 329.466 | 40.80% |

六个 JSON 占表主 B-tree 的 98.66%，占表+索引的 98.44%，平均 **77,612.8 B/行**（十进制 77.6 KB，二进制 75.8 KiB）。具体构成：

| JSON 列 | 字节 | 占六列 |
|---|---:|---:|
| `request_summary_json` | 327,849,401 | 99.51% |
| `decision_json` | 1,364,177 | 0.41% |
| `prompt_versions_json` | 195,270 | 0.06% |
| `trigger_event_json` | 32,708 | 0.01% |
| `reanalysis_triggers_json` | 19,060 | <0.01% |
| `rejected_response_diagnostic_json` | 5,731 | <0.01% |

另一个 2026-08-31 运维时点的生产实测为：全库 814,436,352 B，`context_resolution_attempts` 主表 334,036,992 B，占 41.01%。该口径与不可变备份的 41.35% 差异只有 0.34 个百分点；“生产库约 814 MB、该表约 334 MB”的量级是正确的，但不应把不同时点的分子和分母混在一个精确百分比里。

### 8.2 日增速率

4,245 行横跨 35 个日历日：

- 121.29 行/日；
- 六 JSON 合计 9,413,324.2 B/日，即 **9.41 MB/日（8.98 MiB/日）**；
- 表+索引总体按同期线性摊分为 9,562,638.6 B/日，即 **9.56 MB/日**。

这是全期实测平均，不是未来增长保证。请求量会受消息量、重分析和故障重试影响。

### 8.3 与日常管理审计 1.2 GB 内存峰值的关系

生产证据已确认，日常管理审计先构建两个完整私有 SQLite 快照，若两者不同则回退到一次完整 SQLite online copy。HTTP 源完成后的 cgroup 峰值约 1.2 GB，与整库拷贝及 page cache 相符。

`context_resolution_attempts` 占整库约 41%，因此它会按同样比例放大每次整库快照的 I/O 和可能的 page-cache 压力。但不能把 1.2 GB 中的 41% 直接归因为该表驻留内存：

- 峰值是整库复制、页缓存和审计进程共同结果；
- 另一个大表 `pending_tpsl_snapshot_observations` 已占 281,985,024 B；
- 实际 RSS/cgroup 下降取决于 SQLite 快照实现、OS page cache 和并发时序，不会与文件缩小一对一。

可量化的是复制字节：如果某个裁剪方案经过数据库重写后将文件实际缩小 277 MB，两个完整快照每次将少处理约 554 MB 数据；但必须用生产副本重放才能把它换算为可信的内存峰值降幅。

### 8.4 存量裁剪方案（本文仅分析，未执行）

以备份时点 2026-08-31 14:39:29 为基准，“保留 N 天”意味着裁剪早于该截止时间的存量。

#### 选项 A：保留行和小 JSON，只裁剪 `request_summary_json`

| 保留期 | 可裁剪行 | 逻辑可删 `request_summary_json` | 按当前物理/逻辑比例估算的缩库上限* | 两快照少处理字节* |
|---|---:|---:|---:|---:|
| 7 天 | 3,658 | 273.017 MB | 约 277.35 MB | 约 554.69 MB |
| 14 天 | 1,547 | 85.138 MB | 约 86.49 MB | 约 172.98 MB |
| 30 天 | 242 | 10.388 MB | 约 10.55 MB | 约 21.11 MB |

保留：attempt ID、原始消息关联、状态、重试次数、时间、错误、`decision_json`、未来 reanalysis trigger 和 trigger event。
损失：当时完整窗口、first pass、candidate/active strategy 快照、saved evidence 和其他 request 细节；无法按当时输入做完整离线回放或重建 8 触发器。

#### 选项 B：保留行，裁剪六个 JSON

| 保留期 | 逻辑可删六 JSON | 比只删 request 多回收 | 估算缩库上限* |
|---|---:|---:|---:|
| 7 天 | 274.308 MB | 1.292 MB | 约 278.66 MB |
| 14 天 | 85.947 MB | 0.808 MB | 约 87.31 MB |
| 30 天 | 10.500 MB | 0.112 MB | 约 10.67 MB |

相对选项 A 只多回收 0.1–1.3 MB，却额外损失最终决策、模型 reanalysis 意图和拒绝诊断。从空间/可追溯性性价比看，不如只裁剪 `request_summary_json`。

#### 选项 C：整行删除

整行删除的最低可回收量不少于选项 B 的 JSON 字节，还可多回收行头、标量列和索引页。但现有只读数据不能在不重写副本的情况下精确计算整行删除后的 B-tree 页数，不应伪造比选项 B 多几 MB 的精度。

损失最大：连 attempt 是否发生、重试过程、错误和最终决策都无法从本表追溯。不建议作为第一选择。

#### 选项 D：压缩归档后再裁剪

先将超期 `request_summary_json` 按日/周生成带行 ID、hash 和 manifest 的压缩只读归档，校验可恢复后再按选项 A 裁剪主库。主库可回收量与选项 A 相同，归档文件仍占额外空间，具体压缩比没有在本轮实测，不给假数字。

优点是保留完整追溯与离线回放能力；代价是增加归档、校验、恢复和保留期管理的复杂度。

\* SQLite 中把 JSON 更新为 `NULL`/空值后，主库文件通常不会立即缩小；页可能只是变成库内可复用空间。表中的“缩库上限”用当前（表+索引）/六 JSON 物理比 1.015862 估算；只有在生产副本上执行合适的安全重写/VACUUM 演练后，才能确定真正的文件回收量和峰值影响。本轮没有执行任何这类操作。

## 9. 优化选项（按优先级）

识别逻辑是交易核心。除可观测性外，下列任何改动都只是建议，不是授权实施。

| 优先级 | 选项 | 预期节省 | 实施风险 | 不改变系统决策的验证方式 | 类型 |
|---:|---|---|---|---|---|
| P0 | 清理后先观测至少 7 个正常日或 500 条消息 | 直接节省 0；历史反事实上界为 9.29 provider 请求/日、请求字节 1.25% | 很低，只读度量 | 固定分母，同时记录消息数、attempt、provider call、8 触发器和改变率；与清理前同口径对比 | 运维观测，无配置/代码变更 |
| P1 | 持久化真实 8 触发器、阶段、attempt/provider usage 和组件字节 | 直接节省 0；使后续节省可精确归因 | 低，但是 schema/写路径增量 | 只增字段，旧决策链不读新字段；影子期对比计数与 provider 账单 | 需改代码/可能需 schema |
| P2 | 为网络错误增加熔断/指数退避，避免立即重发大 prompt | 本历史中最多可避免 1,367 次第二次请求，占 5,923 次的 23.08%；收益集中在故障日 | 中：174 条 completed attempt 是第二次才成功，不能粗暴取消所有重试 | 故障注入比较新旧最终决策；保留延时 probe、不把未知当成 no-action，并验证 174 个第二次成功样本不丢失 | 需改代码 |
| P3 | 精炼“仅 multiple candidate”触发条件 | 理论涉及 4,273/5,923 provider 请求，但在证明召回率前安全节省为未知 | **高**：该组有 124 次实质改变 | 对 3,005 个历史样本回放，新 gate 对 124 个改变样本必须 100% 召回；生产影子期运行旧决策、只记新 gate 差异 | 需改代码/词汇或阈值属决策变更 |
| P4 | 做“相关性保留”的窗口压缩，不盲目改成 20/24 | 历史 request 字节理论上限约 50.75% | **高**：盲目 20/24 会影响至少 72/580 个改变决策 | 必须保留 direct reply、candidate/lifecycle root、已引用消息和 72 个风险样本；历史回放后再进行旧窗口/新窗口双路影子，比对动作、目标和可应用性 | 需改代码 |
| P5 | 收紧无关新消息导致的 reanalysis | 重分析占 2,424/5,923 provider 请求（40.93%）；仅 `next_same_chat_message` 上限为 217 次（3.66%、约 6.2 次/日） | 中高：可比较 `next_same_chat` 样本中有 2 次改变 | 按 trigger event 回放，保留这 2 次；影子记录“新消息是否真与候选关联”及新旧决策差异 | 需改代码 |
| P6 | 主识别首轮仅发当前消息/图片/直接 reply，需要时再发完整上下文 | 理论可节省约 3–5M 输入 token/日，但精确值未持久化 | **最高**：会直接改变第一层输入和触发基础 | 只能双路影子运行，旧路继续权威；按动作、目标 lifecycle、风险参数和最终订单投影做零差异门槛 | 需改代码，最高风险 |

关于 reanalysis 的补充数据：266 条带 trigger event 的 attempt 中，`next_same_chat_message` 为 163 行/217 次 provider call，exchange event 为 77/89，strategy state 为 21/26。与同一消息的上一决策可比较时，`next_same_chat_message` 51 个样本改变 2 个（3.92%），exchange event 40 个改变 5 个（12.5%），strategy state 16 个改变 0 个。样本不大，只能用来定义影子验证重点，不足以直接删除 reanalysis 路径。

## 10. 纯配置改动与代码改动的边界

### 10.1 现有纯配置手段

| 配置手段 | 能否节省 | 风险/结论 |
|---|---|---|
| `context_resolution_enabled=false` | 可节省所有第二层调用 | 会改变至少 580 个历史决策；不建议 |
| 收窄 `context_resolution_live_chat_ids` | 按被排除 chat 的流量节省 | 会直接改变这些 chat 的决策路径；当前白名单覆盖 33/34 个已启用 chat，现实上几乎不门控 |
| 更换模型/价格配置 | 可能降低金额，不降低 token 量 | 模型变更也可改变决策，需同等最高风险验证 |

因此，**当前没有一个“只改 settings、既有显著节省又能从数据上证明不改决策”的选项**。

### 10.2 需要代码/schema/运维数据政策的选项

- 窗口 50 条 / 72 小时 / reply 5 层 / active strategy 50 条的限制：需改代码，不是现有 settings；
- 8 触发器词汇、multiple candidate 逻辑、网络熔断、reanalysis 关联、主识别输入拆分：都需改代码；
- provider usage/触发器持久化：需改代码，并可能需 schema 变更；
- `context_resolution_attempts` 保留期、JSON 归档/裁剪：属于数据保留政策与受控数据操作，不是现有纯 settings；如要自动化才需增加代码/作业。

## 11. 建议的决策顺序

1. 先获得清理后有分母的真实触发率，不用反事实上界代替观测。
2. 先补 usage/触发器可观测性，再谈精确 token 账单和触发器 ROI。
3. 先处理故障日的重复大 prompt，因为它的节省不需假设某类交易语义没用。
4. 窗口优化必须是“保留相关证据”，不能直接改数字；20/24 的 12.41% 明确引用损失已足以否定盲目上线。
5. 存量体积优先考虑“保留行和 decision，只裁剪/归档过期 `request_summary_json`”；六列全裁剪的额外空间收益很小，却会丢失更多追溯信息。

任何进入实施的方案，都应保留现行路径为权威，先做历史回放和生产影子对比；在动作、目标 lifecycle、风险参数和最终订单投影上没有达到事先定义的零差异/全召回门槛前，不应接管交易决策。

## 12. P0 观测口径与首轮结果

### 12.1 固定窗口与数据完整性

本轮将恢复观察窗口结束时的原始消息水位 `14158` 作为开区间边界，并固定本轮截止时间，避免多条查询使用滑动分母：

- 开始：`2026-08-31T16:42:37Z`；
- 截止：`2026-08-31T21:35:00Z`；
- 时长：4 小时 52 分 23 秒（0.203 个 24 小时日）；
- 原始消息口径：`raw_messages.id > 14158 AND created_at <= '2026-08-31 21:35:00'`；
- 本轮截止时的最大 raw message ID：`14168`；
- 新消息共 10 条，ID 连续为 `14159`–`14168`；10 条的 `created_at` 和 `posted_at` 均晚于窗口起点，没有把维护期旧消息或迟到旧帖算入分母。

所有 SQLite 查询使用 `sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db`，核心链路查询使用同一个只读 `BEGIN/COMMIT` 快照。交易所证据使用 worker 进程的只读 exchange snapshot 与“当前委托” GET 端点；未调用任何 POST、取消、下单或重放端点。

### 12.2 运行状态与身份

2026-08-31T21:44:28Z 的进程自证结果：

| role | PID | systemd 状态 | release | artifact | entry freeze | role-specific health |
|---|---:|---|---|---|---|---|
| web | 3,746,349 | active/running，`NRestarts=0` | `6e2321cecbb3adf61d7a5972d391e662d4aea300` | verified | `false` | event loop `true` |
| ingest | 3,746,355 | active/running，`NRestarts=0` | 同上 | verified | `false` | event loop、live listener、reconcile 均 `true` |
| worker | 3,746,343 | active/running，`NRestarts=0` | 同上 | verified | `false` | event loop、worker command、message processing 均 `true` |

三个进程的 manifest SHA-256 均为 `4d011a9569dde31468db08cce20e1ce4e6570fad3a828f86d20db7832444cbb7`，`loaded_cwd` 均为 `/opt/telegram-kol-analyzer`。systemd 显示三个进程均于主机时间 2026-09-01 00:09:59（UTC `16:09:59Z`）启动，早于本 P0 窗口；当前 PID 与 16:42Z 恢复结束证据一致，因此窗口内无 PID 漂移。

worker 的 management、break-even、reconcile、close、TPSL、protection 和 rescue 循环全部 `fresh=true, successful=true`，management/close/TPSL/rescue 的有效权限均为 `true`，`global_exchange_authority=true`。设置端点实测 `auto_trade_enabled=true`，`worker_command_mode=queue`。

| role | P50 | P95 | P99 | 近期最大 | stall count | watchdog |
|---|---:|---:|---:|---:|---:|---|
| web | 1.260 ms | 1.764 ms | 2.264 ms | 4.543 ms | 0 | attached |
| ingest | 1.296 ms | 1.788 ms | 2.231 ms | 10.383 ms | 0 | attached |
| worker | 0.878 ms | 6.299 ms | 24.336 ms | 312.744 ms | 0 | attached |

web/ingest 的 worker-only 健康位为 `false`，worker 的 ingest-only 健康位为 `false`，这是进程角色隔离，不是循环故障。

### 12.3 monitor 自 16:42Z 起的每个周期

| 周期开始（UTC） | release 自证 | healthy | reason codes | monitor error | audit ran |
|---|---|---|---|---|---|
| 17:01:48 | `6e2321ce...`, verified | true | `[]` | null | false |
| 17:31:43 | 同上 | true | `[]` | null | false |
| 18:00:18 | 同上 | true | `[]` | null | false |
| 18:30:12 | 同上 | true | `[]` | null | false |
| 19:01:21 | 同上 | true | `[]` | null | false |
| 19:31:57 | 同上 | true | `[]` | null | false |
| 20:01:13 | 同上 | true | `[]` | null | false |
| 20:31:53 | 同上 | true | `[]` | null | false |
| 21:00:27 | 同上 | true | `[]` | null | false |
| 21:30:19 | 同上 | true | `[]` | null | false |

10 个完整周期均加载同一 release，无 reason code，没有 SHA 漂移。

### 12.4 真实消息到执行的链路

| 阶段 | 消息数 | 说明 |
|---|---:|---|
| 新原始消息 | 10 | raw ID `14159`–`14168` |
| 进入主识别 | 10 | 10 条均有 `message_recognitions`、authoritative MiMo run 和 `recognition_decisions` |
| 队列 `succeeded` | 10 | 全部 `worker_completed` |
| 产生 signal candidate | 1 条消息 / 1 条 candidate | raw `14166`，candidate `2123` |
| 走 `expired` | 0 | — |
| 仍 `pending/claimed` | 0 | — |

首条恢复后完整交易链路已实际出现，不再是零消息推断：

`raw_message 14166`（posted `19:31:18Z`）→ authoritative recognition `是策略`→ signal candidate `2123`（BTC short, confidence 0.95）→ lifecycle `1037`→ binding `321`→ execution events `3878/3879`→ Deepcoin 当前触发委托精确回读。

lifecycle `1037` 的 `signal_candidate_id` 为空，因此 candidate 到 lifecycle 的追溯使用了精确 `(chat_id, message_id)=(-1002370796392, 3605)` 以及 binding 反向关联，而不是直接外键。这不改变本次交易所一致性结论，但是后续追溯时需保留的数据质量注记。

### 12.5 Deepcoin 写入与 lifecycle/binding 对账

窗口内有 2 笔 Deepcoin 写入，均来自 raw message `14166`。该消息的 `created_at=2026-08-31T19:31:19.021891Z`，比恢复窗口起点晚 2 小时 48 分 42 秒，不是维护期历史消息的重放。

| event | symbol/方向 | 数量 | 触发/委托价 | raw message | 下单时间（UTC） | order ID | 当前交易所状态 |
|---|---|---:|---:|---:|---|---|---|
| 3878 | BTC-USDT-SWAP / sell short | 7 张 | 80,510 | 14166 | 19:31:41.572328 | `1001125071413372` | 当前触发委托中，`Conditional`，未成交 |
| 3879 | BTC-USDT-SWAP / sell short | 7 张 | 81,110 | 14166 | 19:31:41.903314 | `1001125071413427` | 当前触发委托中，`Conditional`，未成交 |

2026-08-31T21:44:29.892471Z 的交易所只读回读：持仓 0，普通当前委托 0，当前触发委托 2。两个 exchange order ID、方向、数量和价格与本地 order legs `555/556` 精确一致，exchange UI 投影均显示“已绑定”。

| lifecycle | binding | 本地状态 | pos ID | 交易所证据 | 一致性 |
|---:|---:|---|---|---|---|
| 1037 | 321 | `pending_entry`; binding `open`; `last_exchange_status=entry_order_pending`; 两条 leg 均 `pending` | 空 | 0 持仓，2 条精确对应的 pending trigger orders | 一致：尚未成交，等待两个入场触发单 |

leg 的 `attribution_status=unassigned` 表示尚未产生可归属的成交持仓；在 pos ID 为空、触发单未成交的当前状态下，它不与 exchange 回读矛盾。

### 12.6 P0 触发率首轮基线

与本文第 3、4 节完全同口径：8 个调用前确定性触发器从 `request_summary_json` 重建，不把 `reanalysis_triggers_json` 误当成这 8 个触发器。

- 固定消息分母：10；
- context-resolution attempt：2；
- 涉及不同原始消息：2；
- provider 请求：2；
- 首轮描述性触发率：2/10 = 20.00%。

该 20.00% 只是 10 条消息的首轮描述，不能与清理前 6,163 条消息的 47.51% 作统计性高低结论。

| 触发器 | 次数 | attempt 占比 |
|---|---:|---:|
| `revision_language` | 1 | 50.00% |
| `cancellation_language` | 0 | 0.00% |
| `entered_holder_language` | 0 | 0.00% |
| `management_without_exact_target` | 1 | 50.00% |
| `multiple_same_source_candidates` | 0 | 0.00% |
| `reply_target_disagreement` | 0 | 0.00% |
| `text_image_conflict` | 0 | 0.00% |
| `apparent_entry_may_be_revision` | 0 | 0.00% |

| 同时命中数 | attempt | 占比 |
|---:|---:|---:|
| 1 | 2 | 100.00% |
| 2–8 | 0 | 0.00% |

| 状态 | attempt | provider 请求 | 留下决策 |
|---|---:|---:|---:|
| `completed` | 2 | 2 | 2 |
| `exhausted` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `failed` | 0 | 0 | 0 |

实质改变口径与第 4.1 节一致。本次 SQL 在历史 `id<=4245` 数据上复算为 2,767 条可比决策、580 条改变、2,187 条不变，与本文原始结果完全相同。首轮结果：

| 分母口径 | 分母 | 实质改变 | 不变/无决策 | 比率 |
|---|---:|---:|---:|---|
| 有可比较 decision | 2 | 1 | 1 | **不报告** |
| 全部 attempt | 2 | 1 | 1 | **不报告** |
| `completed` attempt | 2 | 1 | 1 | **不报告** |

**只有 2 条 attempt，样本不足以支撑改变率结论。** 因此上表仅保留计数，不用 1/2 生成百分比，也不根据该小样本判断哪个触发器更有效。

### 12.7 距离 P0 目标

P0 目标是“7 个正常日或 500 条消息，哪个先到用哪个”。本轮仅覆盖 4 小时 52 分 23 秒和 10 条消息：

- 按连续时长计，还差 6 天 19 小时 7 分 37 秒；当前尚未完成 1 个整的正常日；
- 按消息数计，还差 490 条；
- 完成度为消息目标的 2.00%，不应把本轮命名为“清理后最终基线”。

### 12.8 可重复的原始查询

下列是形成本节最终数字的四组只读查询。后续观测只替换 `start_raw_message_id`、`window_start` 和 `window_end`；触发器和改变率 SQL 不改，才能与清理前和本轮直接比较。

#### A. 运行身份、循环、monitor 和交易所只读证据

```bash
ssh tecent 'set -e
printf "UTC_NOW\n"
date -u +%Y-%m-%dT%H:%M:%SZ
printf "SYSTEMD\n"
for unit in telegram-kol-web.service telegram-kol-ingest.service telegram-kol-worker.service; do
  systemctl show "$unit" --property=Id,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,Result --no-pager
done
printf "IDENTITIES_AND_LOOPS\n"
for port in 8000 8001 8002; do
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/api/runtime/deployment-identity"
  printf "\n"
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/api/runtime/loop-health"
  printf "\n"
done
printf "TRADING_SETTINGS_SAFE_FIELDS\n"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/trading-settings | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in (\"auto_trade_enabled\",\"worker_command_mode\")},sort_keys=True))"
printf "MONITOR_RESULTS\n"
journalctl -u telegram-kol-monitor.service --since "2026-08-31 16:42:00 UTC" --until "2026-08-31 21:35:00 UTC" --no-pager -o short-iso-precise | grep -E "runtime-deployment-identity-v1|audit_ran"
printf "EXCHANGE_COUNTS\n"
curl --fail --silent --show-error --max-time 20 http://127.0.0.1:8002/api/runtime-agent/read-only-exchange-snapshot
printf "\nEXCHANGE_PENDING_TRIGGER_ROWS\n"
curl --fail --silent --show-error --max-time 30 http://127.0.0.1:8002/positions-panel/tabs/open-orders | grep -E "data-exchange-tab-(loaded|item-count|captured-at)|BTC-USDT-SWAP|side-badge|条件/开空|exchange-attribution-chip|order 100112507141|Conditional|委托价格|数量|创建时间|更新时间|暂无当前委托" | head -n 120
'
```

#### B. 消息漏斗、真实执行链、lifecycle/binding 和 Deepcoin 写入

以下内容原样通过标准输入传给 `ssh tecent "sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db"`：

```sql
.headers on
.mode list
BEGIN;
SELECT 'window' AS section, 14158 AS start_raw_message_id,
       '2026-08-31T16:42:37Z' AS window_start,
       '2026-08-31T21:35:00Z' AS window_end,
       MAX(id) AS database_max_raw_message_id
FROM raw_messages;
SELECT 'message_funnel' AS section,
       COUNT(*) AS messages,
       COUNT(DISTINCT mr.raw_message_id) AS entered_recognition,
       COUNT(DISTINCT sc.raw_message_id) AS candidate_messages,
       COUNT(DISTINCT CASE WHEN j.status='expired' THEN j.raw_message_id END) AS expired,
       COUNT(DISTINCT CASE WHEN j.status IN ('pending','claimed') THEN j.raw_message_id END) AS pending_or_claimed,
       COUNT(DISTINCT CASE WHEN j.status='succeeded' THEN j.raw_message_id END) AS succeeded
FROM raw_messages r
LEFT JOIN message_processing_jobs j ON j.raw_message_id=r.id AND j.shadow=0
LEFT JOIN message_recognitions mr ON mr.raw_message_id=r.id
LEFT JOIN signal_candidates sc ON sc.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00';
SELECT 'message_chain' AS section, r.id AS raw_message_id, r.chat_id,
       r.message_id, r.posted_at, r.created_at,
       j.status AS job_status, j.last_reason, j.enqueued_at, j.completed_at,
       mr.status AS recognition_status,
       mrr.status AS mimo_run_status, mrr.attempt_count AS mimo_provider_attempts,
       rd.authoritative_status, rd.automation_status,
       COUNT(DISTINCT sc.id) AS signal_candidates,
       COUNT(DISTINCT cra.id) AS context_attempts
FROM raw_messages r
LEFT JOIN message_processing_jobs j ON j.raw_message_id=r.id AND j.shadow=0
LEFT JOIN message_recognitions mr ON mr.raw_message_id=r.id
LEFT JOIN mimo_recognition_runs mrr ON mrr.raw_message_id=r.id AND mrr.became_authoritative=1
LEFT JOIN recognition_decisions rd ON rd.raw_message_id=r.id
LEFT JOIN signal_candidates sc ON sc.raw_message_id=r.id
LEFT JOIN context_resolution_attempts cra ON cra.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
GROUP BY r.id ORDER BY r.id;
SELECT 'candidate' AS section, sc.id, sc.raw_message_id, sc.symbol, sc.side,
       sc.event_type, sc.confidence, sc.review_status, sc.created_at
FROM signal_candidates sc JOIN raw_messages r ON r.id=sc.raw_message_id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
ORDER BY sc.id;
SELECT 'new_lifecycle_binding' AS section, sl.id AS lifecycle_id,
       eb.id AS binding_id, r.id AS raw_message_id, r.created_at AS raw_created_at,
       r.posted_at, sl.chat_id, sl.message_id, sl.symbol, sl.side,
       sl.lifecycle_status, sl.signal_at, sl.execution_binding_id,
       eb.status AS binding_status, eb.last_exchange_status,
       eb.order_id, eb.client_order_id, eb.pos_id
FROM strategy_lifecycles sl
LEFT JOIN execution_bindings eb ON eb.id=sl.execution_binding_id
LEFT JOIN raw_messages r ON r.chat_id=sl.chat_id AND r.message_id=sl.message_id
WHERE sl.created_at>='2026-08-31 16:42:37'
  AND sl.created_at<='2026-08-31 21:35:00'
ORDER BY sl.id;
SELECT 'deepcoin_write' AS section, ee.id AS execution_event_id,
       ee.execution_binding_id, r.id AS raw_message_id, r.created_at AS raw_created_at,
       ee.action, ee.status, ee.symbol, ee.side,
       COALESCE(json_extract(ee.request_json,'$.sz'),json_extract(ee.request_json,'$.size')) AS quantity,
       json_extract(ee.request_json,'$.triggerPrice') AS trigger_price,
       ee.order_id, ee.client_order_id, ee.pos_id,
       ee.created_at AS submitted_at
FROM execution_events ee
LEFT JOIN raw_messages r ON r.chat_id=ee.chat_id AND r.message_id=ee.source_message_id
WHERE ee.created_at>='2026-08-31 16:42:37'
  AND ee.created_at<='2026-08-31 21:35:00'
ORDER BY ee.id;
SELECT 'order_leg' AS section, eol.id, eol.execution_binding_id,
       eol.leg_index, eol.purpose, eol.order_kind, eol.order_id,
       eol.client_order_id, eol.pos_id, eol.attribution_status,
       eol.status, eol.created_at, eol.updated_at,
       json_extract(eol.request_json,'$.instId') AS instrument,
       json_extract(eol.request_json,'$.sz') AS quantity,
       json_extract(eol.request_json,'$.side') AS order_side,
       json_extract(eol.request_json,'$.posSide') AS position_side,
       json_extract(eol.request_json,'$.triggerPrice') AS trigger_price,
       json_extract(eol.request_json,'$.price') AS order_price
FROM execution_order_legs eol
JOIN execution_bindings eb ON eb.id=eol.execution_binding_id
WHERE eb.created_at>='2026-08-31 16:42:37'
  AND eb.created_at<='2026-08-31 21:35:00'
ORDER BY eol.id;
COMMIT;
```

#### C. P0 触发器、状态、provider 请求和实质改变率

以下内容原样通过标准输入传给 `ssh tecent "sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db"`：

```sql
.headers on
.mode list
BEGIN;
SELECT 'p0_totals' AS section,
       COUNT(DISTINCT r.id) AS messages,
       COUNT(DISTINCT a.id) AS attempts,
       COUNT(DISTINCT a.raw_message_id) AS distinct_raw_messages,
       COALESCE(SUM(a.attempts),0) AS provider_requests
FROM raw_messages r
LEFT JOIN context_resolution_attempts a ON a.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00';

WITH base AS (
  SELECT a.id, a.raw_message_id, a.request_summary_json,
         lower(COALESCE(json_extract(a.request_summary_json,
                                    '$.message_context.current.text'),'')) AS text,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.recognition_result'),'') AS recognition_result,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.lifecycle_event.event_type'),'none') AS event_type,
         json_extract(a.request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS target_lifecycle_id,
         COALESCE(json_array_length(json_extract(a.request_summary_json,
                                                 '$.candidate_strategy_threads')),0) AS candidate_count
  FROM context_resolution_attempts a
  JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), sets AS (
  SELECT b.*,
         COALESCE((
           SELECT group_concat(thread_id, ',') FROM (
             SELECT DISTINCT
                    CAST(json_extract(link.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,
                            '$.message_context.reply_chain[0].strategy_links') AS link
             WHERE json_extract(link.value,'$.strategy_thread_id') IS NOT NULL
             ORDER BY thread_id
           )
         ),'') AS reply_threads,
         COALESCE((
           SELECT group_concat(thread_id, ',') FROM (
             SELECT DISTINCT
                    CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,'$.candidate_strategy_threads') AS c
             WHERE b.target_lifecycle_id IS NOT NULL
               AND CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                   = CAST(b.target_lifecycle_id AS INTEGER)
             UNION
             SELECT DISTINCT
                    CAST(json_extract(s.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,
                            '$.message_context.active_strategies') AS s
             WHERE b.target_lifecycle_id IS NOT NULL
               AND CAST(json_extract(s.value,'$.lifecycle_id') AS INTEGER)
                   = CAST(b.target_lifecycle_id AS INTEGER)
               AND json_extract(s.value,'$.strategy_thread_id') IS NOT NULL
             ORDER BY thread_id
           )
         ),'') AS target_threads
  FROM base b
), flags AS (
  SELECT *,
         (instr(text,'更新')>0 OR instr(text,'修改')>0 OR
          instr(text,'改为')>0 OR instr(text,'调整')>0 OR
          instr(text,'replace')>0 OR instr(text,'update')>0) AS revision_language,
         (instr(text,'取消')>0 OR instr(text,'撤销')>0 OR
          instr(text,'撤单')>0 OR instr(text,'cancel')>0) AS cancellation_language,
         (instr(text,'有入场')>0 OR instr(text,'已入场')>0 OR
          instr(text,'持仓')>0 OR instr(text,'保护成本')>0 OR
          instr(text,'保本')>0 OR instr(text,'继续持有')>0) AS entered_holder_language,
         (event_type<>'none' AND target_lifecycle_id IS NULL) AS management_without_exact_target,
         (candidate_count>1) AS multiple_same_source_candidates,
         (reply_threads<>'' AND target_threads<>'' AND
          reply_threads<>target_threads) AS reply_target_disagreement,
         (COALESCE(json_array_length(json_extract(request_summary_json,
                                                   '$.saved_evidence.conflicts')),0)>0) AS text_image_conflict,
         (recognition_result='是策略' AND candidate_count>0 AND (
            (instr(text,'更新')>0 OR instr(text,'修改')>0 OR
             instr(text,'改为')>0 OR instr(text,'调整')>0 OR
             instr(text,'replace')>0 OR instr(text,'update')>0)
            OR EXISTS (
              SELECT 1
              FROM json_each(request_summary_json,
                             '$.candidate_strategy_threads') AS c,
                   json_each(c.value,'$.reasons') AS reason
              WHERE reason.value='overlapping_entry'
            )
          )) AS apparent_entry_may_be_revision
  FROM sets
), trigger_rows AS (
  SELECT id,'revision_language' AS trigger FROM flags WHERE revision_language
  UNION ALL SELECT id,'cancellation_language' FROM flags WHERE cancellation_language
  UNION ALL SELECT id,'entered_holder_language' FROM flags WHERE entered_holder_language
  UNION ALL SELECT id,'management_without_exact_target' FROM flags WHERE management_without_exact_target
  UNION ALL SELECT id,'multiple_same_source_candidates' FROM flags WHERE multiple_same_source_candidates
  UNION ALL SELECT id,'reply_target_disagreement' FROM flags WHERE reply_target_disagreement
  UNION ALL SELECT id,'text_image_conflict' FROM flags WHERE text_image_conflict
  UNION ALL SELECT id,'apparent_entry_may_be_revision' FROM flags WHERE apparent_entry_may_be_revision
), trigger_names(trigger, ordinal) AS (
  VALUES ('revision_language',1),
         ('cancellation_language',2),
         ('entered_holder_language',3),
         ('management_without_exact_target',4),
         ('multiple_same_source_candidates',5),
         ('reply_target_disagreement',6),
         ('text_image_conflict',7),
         ('apparent_entry_may_be_revision',8)
)
SELECT 'trigger_distribution' AS section, n.trigger,
       COUNT(t.id) AS attempts,
       round(100.0*COUNT(t.id)/(SELECT COUNT(*) FROM flags),2) AS attempt_pct
FROM trigger_names n
LEFT JOIN trigger_rows t ON t.trigger=n.trigger
GROUP BY n.trigger,n.ordinal
ORDER BY n.ordinal;

WITH base AS (
  SELECT a.id, a.request_summary_json,
         lower(COALESCE(json_extract(a.request_summary_json,
                                    '$.message_context.current.text'),'')) AS text,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.recognition_result'),'') AS recognition_result,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.lifecycle_event.event_type'),'none') AS event_type,
         json_extract(a.request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS target_lifecycle_id,
         COALESCE(json_array_length(json_extract(a.request_summary_json,
                                                 '$.candidate_strategy_threads')),0) AS candidate_count
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), sets AS (
  SELECT b.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT
                  CAST(json_extract(link.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,
                          '$.message_context.reply_chain[0].strategy_links') AS link
           WHERE json_extract(link.value,'$.strategy_thread_id') IS NOT NULL
           ORDER BY thread_id
         )),'') AS reply_threads,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE b.target_lifecycle_id IS NOT NULL
             AND CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(b.target_lifecycle_id AS INTEGER)
           UNION
           SELECT DISTINCT
                  CAST(json_extract(s.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,
                          '$.message_context.active_strategies') AS s
           WHERE b.target_lifecycle_id IS NOT NULL
             AND CAST(json_extract(s.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(b.target_lifecycle_id AS INTEGER)
             AND json_extract(s.value,'$.strategy_thread_id') IS NOT NULL
           ORDER BY thread_id
         )),'') AS target_threads
  FROM base b
), flags AS (
  SELECT *,
    (instr(text,'更新')>0 OR instr(text,'修改')>0 OR instr(text,'改为')>0 OR
     instr(text,'调整')>0 OR instr(text,'replace')>0 OR instr(text,'update')>0) AS f1,
    (instr(text,'取消')>0 OR instr(text,'撤销')>0 OR instr(text,'撤单')>0 OR instr(text,'cancel')>0) AS f2,
    (instr(text,'有入场')>0 OR instr(text,'已入场')>0 OR instr(text,'持仓')>0 OR
     instr(text,'保护成本')>0 OR instr(text,'保本')>0 OR instr(text,'继续持有')>0) AS f3,
    (event_type<>'none' AND target_lifecycle_id IS NULL) AS f4,
    (candidate_count>1) AS f5,
    (reply_threads<>'' AND target_threads<>'' AND reply_threads<>target_threads) AS f6,
    (COALESCE(json_array_length(json_extract(request_summary_json,
                                              '$.saved_evidence.conflicts')),0)>0) AS f7,
    (recognition_result='是策略' AND candidate_count>0 AND (
      (instr(text,'更新')>0 OR instr(text,'修改')>0 OR instr(text,'改为')>0 OR
       instr(text,'调整')>0 OR instr(text,'replace')>0 OR instr(text,'update')>0)
      OR EXISTS (SELECT 1 FROM json_each(request_summary_json,
                                         '$.candidate_strategy_threads') c,
                              json_each(c.value,'$.reasons') reason
                 WHERE reason.value='overlapping_entry'))) AS f8
  FROM sets
), counts AS (
  SELECT id, f1+f2+f3+f4+f5+f6+f7+f8 AS trigger_count FROM flags
), bucket_names(simultaneous_triggers, ordinal) AS (
  VALUES ('1',1),('2-8',2)
), observed AS (
  SELECT CASE WHEN trigger_count=1 THEN '1' ELSE '2-8' END AS simultaneous_triggers,
         COUNT(*) AS attempts
  FROM counts GROUP BY simultaneous_triggers
)
SELECT 'trigger_multiplicity' AS section,
       n.simultaneous_triggers,
       COALESCE(o.attempts,0) AS attempts,
       round(100.0*COALESCE(o.attempts,0)/(SELECT COUNT(*) FROM counts),2) AS attempt_pct
FROM bucket_names n
LEFT JOIN observed o ON o.simultaneous_triggers=n.simultaneous_triggers
ORDER BY n.ordinal;

WITH status_names(status, ordinal) AS (
  VALUES ('completed',1),('exhausted',2),('superseded',3),('failed',4)
), observed AS (
  SELECT a.status, COUNT(*) AS attempts, SUM(a.attempts) AS provider_requests,
         SUM(a.decision_json IS NOT NULL) AS decisions
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
  GROUP BY a.status
)
SELECT 'attempt_status' AS section, n.status,
       COALESCE(o.attempts,0) AS attempts,
       COALESCE(o.provider_requests,0) AS provider_requests,
       COALESCE(o.decisions,0) AS decisions
FROM status_names n LEFT JOIN observed o ON o.status=n.status
ORDER BY n.ordinal;

WITH raw AS (
  SELECT a.*,
         json_extract(request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS first_lifecycle_id,
         CASE
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.recognition_result')='是策略' THEN 'new'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='position_update' THEN 'manage'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='cancel_entry' THEN 'cancel'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='exit_position' THEN 'exit'
           ELSE 'no_action'
         END AS first_family,
         CASE
           WHEN json_extract(decision_json,'$.decision') IN ('hold','unresolved')
             OR CAST(json_extract(decision_json,'$.confidence') AS REAL)<0.7 THEN 'no_action'
           WHEN json_extract(decision_json,'$.decision')='new_thread' THEN 'new'
           WHEN json_extract(decision_json,'$.decision')='revise_thread' THEN 'revise'
           WHEN json_extract(decision_json,'$.decision')='manage_thread' THEN 'manage'
           WHEN json_extract(decision_json,'$.decision')='cancel_thread' THEN 'cancel'
           WHEN json_extract(decision_json,'$.decision')='exit_thread' THEN 'exit'
           ELSE 'no_action'
         END AS context_family
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), normalized AS (
  SELECT raw.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(raw.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(raw.first_lifecycle_id AS INTEGER)
           ORDER BY thread_id
         )),'') AS first_targets,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(t.value AS INTEGER) AS thread_id
           FROM json_each(raw.decision_json,'$.target_thread_ids') AS t
           ORDER BY thread_id
         )),'') AS context_targets
  FROM raw
), scored AS (
  SELECT *, CASE
    WHEN decision_json IS NULL THEN 0
    WHEN first_family<>context_family THEN 1
    WHEN first_family=context_family
      AND first_family IN ('manage','cancel','exit','revise')
      AND first_targets<>context_targets THEN 1
    ELSE 0 END AS changed
  FROM normalized
)
SELECT 'change_rate_comparable_decisions' AS section,
       SUM(decision_json IS NOT NULL) AS denominator,
       SUM(CASE WHEN decision_json IS NOT NULL THEN changed ELSE 0 END) AS changed,
       SUM(CASE WHEN decision_json IS NOT NULL THEN 1-changed ELSE 0 END) AS unchanged_or_no_decision
FROM scored
UNION ALL
SELECT 'change_rate_all_attempts', COUNT(*), SUM(changed), SUM(1-changed) FROM scored
UNION ALL
SELECT 'change_rate_completed_attempts', SUM(status='completed'),
       SUM(CASE WHEN status='completed' THEN changed ELSE 0 END),
       SUM(CASE WHEN status='completed' THEN 1-changed ELSE 0 END)
FROM scored;
COMMIT;
```

#### D. 第 4.1 节历史改变率同口径复算

为证明 C 中改变率定义没有漂移，以下是实际执行的完整历史复算查询：

```sql
WITH raw AS (
  SELECT a.*,
         json_extract(request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS first_lifecycle_id,
         CASE
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.recognition_result')='是策略' THEN 'new'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='position_update' THEN 'manage'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='cancel_entry' THEN 'cancel'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='exit_position' THEN 'exit'
           ELSE 'no_action'
         END AS first_family,
         CASE
           WHEN json_extract(decision_json,'$.decision') IN ('hold','unresolved')
             OR CAST(json_extract(decision_json,'$.confidence') AS REAL)<0.7 THEN 'no_action'
           WHEN json_extract(decision_json,'$.decision')='new_thread' THEN 'new'
           WHEN json_extract(decision_json,'$.decision')='revise_thread' THEN 'revise'
           WHEN json_extract(decision_json,'$.decision')='manage_thread' THEN 'manage'
           WHEN json_extract(decision_json,'$.decision')='cancel_thread' THEN 'cancel'
           WHEN json_extract(decision_json,'$.decision')='exit_thread' THEN 'exit'
           ELSE 'no_action'
         END AS context_family
  FROM context_resolution_attempts a
  WHERE a.id<=4245
), normalized AS (
  SELECT raw.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(raw.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(raw.first_lifecycle_id AS INTEGER)
           ORDER BY thread_id
         )),'') AS first_targets,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(t.value AS INTEGER) AS thread_id
           FROM json_each(raw.decision_json,'$.target_thread_ids') AS t
           ORDER BY thread_id
         )),'') AS context_targets
  FROM raw
), scored AS (
  SELECT *, CASE
    WHEN first_family<>context_family THEN 1
    WHEN first_family=context_family
      AND first_family IN ('manage','cancel','exit','revise')
      AND first_targets<>context_targets THEN 1
    ELSE 0 END AS changed
  FROM normalized
)
SELECT COUNT(*) AS comparable_decisions,
       SUM(changed) AS changed,
       SUM(1-changed) AS unchanged
FROM scored
WHERE decision_json IS NOT NULL;
```

输出为 `2767|580|2187`，与本文第 4.1 节一致。A 中交易所 HTML 的 `grep` 含本轮已知 order ID 前缀，它是为了保留本轮原始命令的结果定位条件；下轮可重复观测应用当期新 binding 的 order ID 替换该前缀，不改变 SQL 口径。

## 13. P0 第二个固定窗口 — through 2026-09-01T07:12:19Z

### 13.1 Window and denominator

This checkpoint continues section 12 without changing its denominator contract. The previous fixed cutoff and raw-message high-water mark become the new open boundary:

- incremental start: `2026-08-31T21:35:00Z`, `raw_messages.id > 14168`;
- fixed end: `2026-09-01T07:12:19Z`;
- duration: 9 hours 37 minutes 19 seconds, or 0.4009 24-hour days;
- messages: 64, IDs 14169–14232;
- all 64 `created_at` and `posted_at` values are after the start, so no delayed maintenance-period message entered the denominator;
- attempts: 41 attempts over 36 distinct messages;
- comparable provider requests: 42, using the section-12 legacy `attempts` fallback only for the four pre-observability attempts;
- directly observed provider requests: 38 across 37 attempts; four legacy attempts have NULL in the five new observability fields and are never used to infer triggers, tokens or request-component bytes.

The incremental descriptive context-resolution trigger rate is therefore 36/64 = 56.25%. This is a traffic description, not a stable rate estimate: the cumulative cohort still falls short of both P0 stopping rules.

### 13.2 Direct eight-trigger observations

The following values come only from `invocation_triggers_json`; no request-payload reconstruction is used. The denominator is the 37 directly observed attempts. Trigger categories are non-exclusive, so their percentages do not sum to 100%.

| invocation trigger | attempts | descriptive share of 37 |
|---|---:|---:|
| `revision_language` | 0 | 0.00% |
| `cancellation_language` | 0 | 0.00% |
| `entered_holder_language` | 8 | 21.62% |
| `management_without_exact_target` | 12 | 32.43% |
| `multiple_same_source_candidates` | 33 | 89.19% |
| `reply_target_disagreement` | 0 | 0.00% |
| `text_image_conflict` | 1 | 2.70% |
| `apparent_entry_may_be_revision` | 0 | 0.00% |

These shares are reported only because the fixed P0 specification asks for a descriptive distribution. With 64 messages and 37 observable attempts, they must not be used to rank trigger quality or justify a trigger/threshold change.

### 13.3 Direct provider usage

All 38 `provider_usage_json` entries report `available=true`; there is no imputed token row. The partial-window totals are 510,453 prompt tokens, 53,214 completion tokens and 563,667 total tokens, including 67,584 cached prompt tokens.

| per-provider-request total tokens | value |
|---|---:|
| minimum | 4,800 |
| P50 | 15,065 |
| mean | 14,833.34 |
| P90 | 21,278 |
| P95 | 22,821 |
| maximum | 23,719 |

P50/P90/P95 are empirical nearest-rank values on only 38 requests. They are measurements, not population estimates.

Trigger-group averages are also direct and non-exclusive: a request is counted in every trigger it carried.

| trigger | provider requests | mean total tokens | range |
|---|---:|---:|---:|
| `entered_holder_language` | 9 | 17,264.78 | 14,653–21,278 |
| `management_without_exact_target` | 12 | 11,663.42 | 6,051–15,977 |
| `multiple_same_source_candidates` | 34 | 15,178.79 | 6,051–23,719 |
| `text_image_conflict` | 1 | 4,800.00 | 4,800–4,800 |
| the other four triggers | 0 | not observed | — |

Direct token telemetry begins at `2026-09-01T01:06:27.730519Z`. Dividing 563,667 tokens by the 6 hours 5 minutes 51 seconds from that first directly observed attempt through the fixed end yields a purely exposure-normalized 2.219 million tokens per 24 hours. That value is highly unstable: the window is one quarter of a day, contains only 38 requests, and excludes four earlier legacy requests whose token usage is unknowable. It is not the final daily baseline.

### 13.4 Request component bytes

The 37 directly observed attempts recorded 1,486,391 total canonical request bytes. `message_context_bytes` is a nested aggregate and must not be added to the disjoint components below.

| component | bytes | share of request total | interpretation |
|---|---:|---:|---|
| `message_context_bytes` | 1,199,302 | 80.69% | nested whole message-context aggregate |
| `active_strategies_bytes` | 254,197 | 17.10% | disjoint subcomponent |
| `reply_chain_bytes` | 74 | 0.005% | disjoint subcomponent |
| `current_message_bytes` | 10,658 | 0.72% | disjoint top-level component |
| `remainder_bytes` | 1,218,724 | 81.99% | disjoint remainder, including context other than reply/active |
| canonical structural overhead | 2,738 | 0.18% | total minus the four disjoint components |

The disjoint rows sum to 100%. The nested `message_context_bytes` value independently confirms that historical/context material remains the dominant request component in this live cohort.

### 13.5 Attempt outcomes and network behavior

| status / phase | attempts | distinct messages | direct provider requests |
|---|---:|---:|---:|
| completed / initial resolution | 30 | 30 | 31 |
| completed / reanalysis | 5 | 3 | 5 |
| completed / legacy unobserved | 4 | 4 | 0 directly observed; 4 comparable |
| exhausted / initial resolution | 2 | 2 | 2 |

The two exhausted rows have one successful, usage-bearing provider request each, `error_class=NULL`, `last_error=RuntimeError`, and no scheduled retry; they are not network failures. Attempt 4279 used two provider requests because the first response targeted a thread outside the allowed candidate set (`target_outside_candidate_set`), then completed after the existing contract-correction retry. It is also not a network retry.

Across all 38 direct usage entries there are zero `provider_request_failed` or other unavailable entries, zero `network_error` rows, zero `retry_pending` rows, and zero non-NULL `next_attempt_at` rows at the fixed snapshot. The provider circuit is process-local and the authorized R1 failure/rollback changed the worker PID inside this wider window, so the durable evidence supports the bounded conclusion that no network-error backoff or circuit-open episode was recorded; it is not a claim about unpersisted in-memory state before that restart.

### 13.6 Cumulative distance to P0 target

The cumulative cohort remains anchored at `2026-08-31T16:42:37Z` and raw ID 14158. Through the fixed end it contains 74 messages, 43 attempts and 44 comparable provider requests over 14 hours 29 minutes 42 seconds (0.604 normal days).

- time criterion: 6 days 9 hours 30 minutes 18 seconds remain to 7 full normal days;
- message criterion: 426 messages remain to 500; current completion is 14.80%;
- neither criterion is met, so this is a P0 checkpoint rather than the final baseline.

The small cohort is sufficient to confirm that the five live observability fields are producing internally consistent direct measurements. It is not sufficient to conclude that the 56.25% descriptive trigger rate, any per-trigger token average, or the 2.219-million-token daily normalization is representative. No trigger, word list, threshold, prompt, context window, setting, row, release or service was changed while collecting this checkpoint.

## 14. Token cost ROI, relevance-window replay, and main-recognition instrumentation design

### 14.1 Fixed snapshot and metric contracts

This section is read-only. It changed no code, setting, whitelist, threshold, prompt, database row, release, service or exchange state. The production SQLite database was opened in read-only mode and the core queries ran inside one read transaction. The fixed snapshot is:

- query time: `2026-09-01T10:00:29.583Z`;
- `context_resolution_attempts.id <= 4307`;
- `raw_messages.id <= 14256`;
- current whitelist: 33 chat IDs from `trading_settings.global.context_resolution_live_chat_ids`;
- legacy decision-recall cohort: the already accepted `id <= 4245` cohort, containing the 580 material changes used throughout sections 4 and 6.

The following definitions prevent three common overclaims:

1. **Invocation count** is reported as `context_resolution_attempts` rows and provider requests separately. For legacy rows, provider requests use the persisted `attempts` count; for R1 rows, the exact number of entries in `provider_usage_json` is authoritative.
2. **Material change** uses the unchanged section-4 contract: action family plus target thread set, with the existing confidence/applicability rule. Reason wording alone is not a material change.
3. **Real exchange-write impact** is deliberately strict. It requires, after the attempt timestamp, both a `signal_candidates` row for the same `raw_message_id` and a directly linked `execution_events.source_message_id` whose action is an exchange-write action. Historical execution events that predate the attempt, local reconciliation/audit actions, `auto_trade_skipped`, and reservation-only rows do not count.

Only two attempts satisfy the third contract:

| attempt | chat | candidate | directly linked exchange write |
|---:|---:|---|---|
| 1308 / raw 10901 | -1002337721508 | candidate 1776, `position_update`, 14:36:05.948847Z | event 3505 cancel deferred trigger entry and event 3514 close submit, both 14:36:05.968916Z |
| 1898 / raw 11780 | -1003825498321 | candidate 1898, `close_signal`, 08:10:26.334182Z | event 3630 management close submit, 08:10:26.432135Z |

R1 provides 58 directly measured provider requests across 56 attempt rows: 828,098 total tokens over 2,274,427 canonical request bytes. Rows without usage are estimated with the observed aggregate conversion `0.3640908 total token / canonical request byte`; the per-request ratio has P10 `0.3388752`, median `0.3618721`, and P90 `0.4258418`. This proxy incorporates prompt overhead and completion tokens only through the small R1 cohort. It is not a provider bill and must not be presented as measured usage. Direct telemetry covers only 58/5,987 = **0.97%** of provider requests in this snapshot.

### 14.2 Per-chat context-resolution ROI

`A` below means directly observed tokens and `P` means the calibrated byte proxy. `changed` is material first-layer change; `write impact` is the strict post-attempt lineage defined above. Chats are sorted first by observed exchange-write impact, then by historical token burden. Token totals cover the full stored history through attempt 4307, not one day.

| whitelisted chat | attempts / provider requests | token total (`A + P`) | changed | write impact | token / write impact |
|---|---:|---:|---:|---:|---:|
| 米娅 vip会员群 11分组 (`-1003825498321`) | 100 / 132 | 1.630M (`0.041M + 1.590M`) | 17 | 1 | 1.630M |
| 比特币陈哥会员群-11分组 (`-1002337721508`) | 175 / 210 | 6.318M (`0.017M + 6.302M`) | 43 | 1 | 6.318M |
| 三马哥会员群-11分组 (`-1002199068560`) | 527 / 711 | 28.441M (`0.045M + 28.396M`) | 110 | 0 | not defined |
| 欧阳火箭滚仓班 (`-1003095914903`) | 573 / 765 | 23.673M (`0.235M + 23.438M`) | 102 | 0 | not defined |
| 比特币军长 (`-1002282384698`) | 509 / 736 | 21.088M (`0.104M + 20.984M`) | 104 | 0 | not defined |
| 币圈所长会员群 (`-1002368892075`) | 449 / 586 | 20.806M (`0.224M + 20.582M`) | 23 | 0 | not defined |
| 峰哥高级会员群 (`-1002409877375`) | 385 / 666 | 20.229M (`0 + 20.229M`) | 6 | 0 | not defined |
| 比特币飞扬 (`-1002960443256`) | 484 / 693 | 17.923M (`0.021M + 17.902M`) | 54 | 0 | not defined |
| 大镖客 (`-1003048800035`) | 364 / 518 | 17.046M (`0.042M + 17.004M`) | 51 | 0 | not defined |
| 舒琴会员群 (`-1002370796392`) | 213 / 300 | 8.430M (`0 + 8.430M`) | 7 | 0 | not defined |
| ROSE会员群 (`-1002344190971`) | 48 / 62 | 2.197M (`0 + 2.197M`) | 19 | 0 | not defined |
| 米哥会员群 (`-1002367395169`) | 164 / 192 | 2.100M (`0.074M + 2.026M`) | 18 | 0 | not defined |
| 比特智 智哥 (`-1003344714145`) | 103 / 137 | 1.783M (`0 + 1.783M`) | 18 | 0 | not defined |
| weichang tan (`-1003415020968`) | 37 / 64 | 0.622M (`0 + 0.622M`) | 1 | 0 | not defined |
| 书 shu-crypto (`-1003053031367`) | 21 / 22 | 0.531M (`0.012M + 0.519M`) | 2 | 0 | not defined |
| 提阿非罗 初塔 (`-1002918719121`) | 32 / 51 | 0.458M (`0.005M + 0.453M`) | 0 | 0 | not defined |
| 大漂亮社区 (`-1002805019371`) | 46 / 53 | 0.454M (`0.009M + 0.445M`) | 13 | 0 | not defined |
| 超V社区搬运群 (`-1001716834927`) | 56 / 68 | 0.349M (`0 + 0.349M`) | 0 | 0 | not defined |
| 三姐精准策略群 (`-1003000736304`) | 8 / 8 | 0.046M (`0 + 0.046M`) | 2 | 0 | not defined |
| unlabeled (`-1002451280921`) | 9 / 9 | 0.033M (`0 + 0.033M`) | 6 | 0 | not defined |
| 龚有财策略群 (`-1002458558902`) | 4 / 4 | 0.021M (`0 + 0.021M`) | 1 | 0 | not defined |
| `-1002274336512` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-5025043055` | 0 / 0 | 0 | 0 | 0 | no sample |
| 躺赢社区 (`-1001966634720`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1002270839757` | 0 / 0 | 0 | 0 | 0 | no sample |
| 凉兮 (`-1003585604552`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4781287606` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1003496877217` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-5046697627` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1002494435354` | 0 / 0 | 0 | 0 | 0 | no sample |
| il Capo Of Crypto (`-1002168259610`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4606125205` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4623349778` | 0 / 0 | 0 | 0 | 0 | no sample |

Totals are 4,307 attempts, 5,987 provider requests, approximately 174.179M total tokens (`0.828M` measured plus `173.351M` proxy), 597 material changes, and two strict exchange-write impacts.

#### Can any zero-impact chat be removed harmlessly?

No current row supports the word **harmless**:

- 31/33 whitelisted chats have zero strict exchange-write impacts, but 19 of them have positive sample and many still materially changed a decision. A no-write or risk-reducing decision can be valuable precisely because it prevents an order; the lineage metric cannot observe that counterfactual benefit.
- The largest zero-impact cohorts have 364–573 attempts. The rule-of-three 95% upper bound for an unseen per-attempt write impact is approximately 0.52%–0.82%, but that bound says nothing about prevented bad writes or safer lifecycle targeting.
- The only positive-sample chats with zero material changes are 超V (56 attempts) and 提阿非罗 (32 attempts). Their corresponding rule-of-three upper bounds are still 5.36% and 9.38% per attempt. That is not enough evidence for a permanent behavior change.
- Twelve whitelisted chats have no attempt at all. Zero observations are absence of evidence, not evidence of zero value.

Changing `context_resolution_live_chat_ids` is a **pure configuration** action and the gate is read dynamically in `trading_settings.py:205-210`; it does not require a code deployment. It still changes the recognition/management decision path. The evidence supports, at most, a future bounded shadow/removal experiment for selected chats. It does not support calling any immediate removal harmless.

### 14.3 Relevance-preserving window replay

The replay retained the current message and existing non-history request components in all variants. The deployable, causal selector was evaluated with information available before the provider call:

- direct reply ancestors;
- full candidate/lifecycle root messages when present in the stored history;
- active-lifecycle source messages;
- messages already linked to a candidate thread;
- message IDs cited by an earlier decision in the same chat;
- for the semantic variant, any first-pass message already classified as a strategy or lifecycle event, plus any message carrying a strategy link.

The result on all stored request bytes through attempt 4307, with explicit-reference recall evaluated on the accepted 580-change legacy cohort, is:

| replay policy | weighted request-byte saving | changed attempts losing at least one observable explicit reference | lost reference IDs | online-causal? |
|---|---:|---:|---:|---|
| accepted blind 20 messages / 24 hours | 50.75% | 72 / 580 | accepted section-6 result | yes, but unsafe |
| structural relevance only | 49.18% | 49 / 580 | 66 | yes |
| structural + first-pass semantic relevance | 41.43% | 34 / 580 | 44 | yes |
| semantic relevance + conservative 20/24 fallback | 26.23% | 8 / 580 | 15 | yes |
| semantic relevance + the **current** decision's explicit references | 41.37% | 0 / 580 matchable | 0; one legacy reference was absent from its stored request | **no; post-hoc oracle** |

The last row gives the requested zero-loss audit ceiling: average request bytes would fall by approximately **41.37%**, and every explicit supporting/opposing message that actually existed in the stored request would remain. It is not deployable as written, because the provider's current `decision_json` does not exist until after the request has already been sent. Treating that row as an online strategy would be look-ahead.

The eight residual causal misses explain which evidence class is still absent. They are strategy-episode continuations that the first pass labelled `非策略/event=none`: paired entry and stop messages, “我的空还在”, profit/status updates, and narrative continuations such as the prior SNDK thesis. They are neither direct replies nor reliably linked lifecycle rows. To reach zero without look-ahead, the selector needs a durable **strategy-episode membership** relation that keeps these false-negative-but-related messages. A keyword-only patch is insufficient: these examples include pronouns and author-specific continuation patterns with no symbol.

Therefore the safe design is:

1. build a non-authoritative selector from reply ancestry, candidate/lifecycle roots, thread links, prior citations, and a strategy-episode relation;
2. run it in shadow while the full window remains authoritative;
3. require 100% recall of all explicit references and zero action/target/applicability differences on a predeclared sample before any cutover;
4. if the selector cannot causally recall all references, retain the full window. The safe deployable saving is then **0%**, not the 41.37% oracle ceiling.

This is materially safer than the blind 20/24 proposal: blind truncation offers about 9.38 percentage points more theoretical byte saving than the post-hoc relevance ceiling, but already loses explicit evidence in 72 material-change attempts. The current causal relevance selector reduces that observed risk to eight attempts, but has not reached the zero-recall target.

#### R1 byte cross-check

The R1 cohort now contains 56 attempt rows (IDs 4252–4307) and 58 provider requests over about nine hours. All 56 stored `request_total_bytes` values exactly equal an independent canonical UTF-8 re-encoding of `request_summary_json`. Mean request size is 39,160.34 B, P50 38,927.5 B, P90 58,034 B; mean `message_context` is 31,558.14 B and contributes 80.59% of aggregate request bytes.

This validates the measurement implementation and independently confirms that context remains the dominant component. It is not enough to replace the historical 89.16% estimate: 56 attempts cover less than one day, only a subset of chats, and a different post-cleanup traffic mix.

### 14.4 Main-recognition daily cost: best available estimate

There is still no true provider usage on the main-recognition path. `ai_prompt_invocations` records invocation identity/status but no tokens; `mimo_recognition_attempts` records provider-attempt status/duration but no request components or provider usage. Consequently every main-recognition token number below is a proxy.

For the fixed 35-calendar-day comparison window `[2026-07-27, 2026-08-31)`:

- 6,163 raw messages arrived;
- `ai_prompt_invocations.feature='message_recognition'` contains 7,035 high-level invocations over 5,821 distinct raw messages, of which 7,000 completed;
- this is 201.0 invocations/day and 1.141 high-level invocations per raw message;
- applying the already accepted 20K–28K token/request byte proxy yields **4.02M–5.63M tokens/day**.

The failure-storm dates 2026-08-21 through 2026-08-23 contain 1,776 invocation records. Removing those three abnormal dates leaves 5,259 invocations over 32 days, or 164.34/day and **3.29M–4.60M proxy tokens/day**. This exclusion describes normal throughput; it is not a billing adjustment, because an HTTP error may or may not have reached the provider and no usage was persisted.

The current MiMo audit tables reinforce the uncertainty: since 2026-08-12 they contain 4,463 completed and 184 HTTP-error provider attempts, but none has usage. Image inputs further weaken any byte-to-token mapping because provider image accounting is not UTF-8 text accounting.

Against the accepted normal context-resolution proxy of 1.67M–2.22M/day, the full-window main estimate is roughly **1.81×–3.37×**. The previously stated 2.5×–3× expectation lies inside that uncertainty band, but is not yet a measurement. Optimization priority should therefore be:

1. instrument main recognition first, because it is probably the larger cost and currently the larger measurement blind spot;
2. keep collecting direct context-resolution usage;
3. only then choose between chat gating and relevance-window work using comparable measured cost and decision-safety metrics.

### 14.5 Additive main-recognition instrumentation design

The correct grain is one provider attempt, not one raw message. The existing durable row is `mimo_recognition_attempts`, which already separates retries. Add nullable R1-shaped fields there:

- `provider_usage_json`: the provider's unmodified usage object wrapped with `available`, request ordinal and an explicit unavailable reason;
- `request_component_bytes_json`: encoding/version, total canonical request bytes, system-prompt bytes, current-message metadata/text bytes, rendered authoritative-context bytes, image data-URL bytes and structural overhead.

The write path is narrowly defined:

1. `recognition_experiments.py:1091-1126` already builds the exact in-memory payload and receives the provider JSON. Measure that same object without mutating or rebuilding it, and copy `data['usage']` when present.
2. Carry the two telemetry objects with the existing provider result/exception metadata through both the v1 authority wrapper and the v2 attempt path.
3. Extend `record_mimo_attempt()` in `mimo_recognition_runs.py:166-242` to persist telemetry on the matching ordinal. Missing provider usage must be recorded as unavailable, never as zero.
4. Keep `ai_prompt_invocations` as the high-level feature audit; do not aggregate retries into it and do not make trading code read the new columns.

Safety properties and acceptance tests:

- the exact payload object passed to `httpx.Client.post(..., json=payload)` is unchanged byte-for-byte in canonical form;
- model, prompt, context builder, image sequence, temperature, JSON mode and thinking mode are unchanged;
- parsed content, canonical decision fingerprint and execution projection are identical with telemetry enabled/disabled;
- one retry produces two attempt rows with two separate usage records;
- provider-omitted usage is explicit `available=false`, while returned prompt/completion/total tokens are preserved exactly;
- component bytes sum to total with a named structural-overhead remainder, and image bytes are reported separately rather than converted to fake text tokens.

This is a **code + nullable L3 schema migration + deployment** change. It is pure additive storage/observability and should not change model input or decision behavior. It is not a configuration-only change. A later dashboard or cost alert could read the fields without touching recognition authority.

### 14.6 Action-type summary

| proposal | category | deployment required? | behavior change? | current evidence decision |
|---|---|---:|---:|---|
| remove selected chat IDs from context whitelist | pure configuration | no | yes | do not call harmless; insufficient causal evidence |
| historical ROI and replay queries | read-only analysis | no | no | completed in this section |
| relevance selector / strategy-episode relation | code | yes | yes, changes model input | shadow only until causal zero-recall and decision-equivalence gates pass |
| main-recognition usage/component telemetry | additive code + nullable schema | yes | intended no | highest-priority implementation candidate |

### 14.7 Validation and limitations

The per-chat totals independently sum to 4,307 attempts, 5,987 provider requests, 597 material changes and two strict trade impacts. The two impacts were manually cross-checked against candidate/event timestamps and direct raw-message lineage. All 56 R1 component rows passed canonical-byte equality. The legacy material-change query remains 580 on `id <= 4245`.

The main limitations are substantive, not formatting details: only 0.97% of historical context provider requests have true usage; only two strict trade impacts exist; prevented trades have no counterfactual outcome; one legacy explicit reference was not present in its stored request; and a current model citation cannot be known before the current call. These limitations prohibit a whitelist removal claim, a production window cutover, or a precise main/context billing ratio from this dataset alone.
